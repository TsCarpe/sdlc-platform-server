"""L2 生命周期编排（对应 OrchestrationService，v2.1 Phase ③）：确定性事件钩子——非 agent 决策。

规则：
  ① 澄清全部答完（setAnswer 成功后的边界跃迁）→ 自动排队 standard_doc；
  ② 标准文档发布成功 → 先做终态收尾（取消 queued 的质检/合成任务），再自动排队 case_gen；
  ③ 开闸建轮成功 → 自动排队 gate_review（批次7 收敛自 gate.open 事务内调用，AUDIT D1-4）。

铁律：
  - 守卫全继承：走 TaskService.create_auto → create_guarded（状态机/全部答完/countActive 防重）；
  - 只自动一次：该类型任务一旦存在（不限状态），钩子不再自动建——人取消即终局，不复活；
  - 编排失败绝不连累人的主操作（作答/发布/开闸）：一切异常吞掉记日志。

触发面约定：钩子挂在 PUT /answers/{seq}、POST /publish、POST /gate/open 三个路由入口
（主操作成功后调用），不在领域服务内部挂钩，避免服务相互依赖成环。gate.open 事务内保留
count_active 预判承担原子性守卫（对抗审查 M1：消「轮已建、任务没排上」的主因）。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from platform_sdlc.common import ApiException, is_json_obj
from platform_sdlc.services import task as task_mod

if TYPE_CHECKING:
    from platform_sdlc.repositories import Repos
    from platform_sdlc.services.task import TaskService

log = logging.getLogger(__name__)


class OrchestrationService:
    def __init__(self, repos: Repos, tasks: TaskService) -> None:
        self.repos = repos
        self.tasks = tasks

    def _clarify_window_start(self, req_id: str) -> str | None:
        """澄清完备窗口起点（§8）：max(standard artifact pushed_at, 最新 confirmed 变更单
        confirmed_at)——两者均有现成存储；均无（首发轮）返回 None 即不限。"""
        marks: list[str] = []
        std = self.repos.artifact.find(req_id, "standard")
        if std is not None and std.pushed_at:
            marks.append(std.pushed_at)
        changes = [c for c in self.repos.req_change.list_by_req(req_id) if c.status == "confirmed"]
        if changes and changes[0].confirmed_at:
            marks.append(changes[0].confirmed_at)
        return max(marks) if marks else None

    def on_answered(self, req_id: str) -> None:
        """澄清作答后：全部答完且本窗口内从未建过 standard_doc 任务 → 自动排队标准文档合成。

        v4-15（§8 显式重新裁决）：「只自动一次」精确化为「每个澄清完备窗口只自动一次」——
        取消语义从「永久不再自动排」收窄为「窗口内终局」：变更 confirm 开新窗口后自动合成
        重新获得机会（新内容使旧取消意图失效）。窗口起点 = max(standard pushed_at, 最新
        confirmed_at)，见 _clarify_window_start。"""
        try:
            counts = self.repos.clarify_item.counts(req_id)
            if counts["total"] == 0 or counts["answered"] < counts["total"]:
                return
            if self.tasks.any_task_of_type(req_id, task_mod.T_STANDARD,
                                           since=self._clarify_window_start(req_id)):
                return
            t = self.tasks.create_auto(req_id, task_mod.T_STANDARD, "auto:clarify-complete", "澄清全部答完，L2 自动排队")
            log.info("[L2] %s 澄清全部答完 → 自动创建 #%s standard_doc", req_id, t.id)
        except ApiException as e:
            log.info("[L2] %s 自动排 standard_doc 跳过：%s", req_id, e)  # 预期内：已有同类活跃任务（409）等
        except Exception as e:  # noqa: BLE001  # 编排失败绝不连累作答主操作
            log.warning("[L2] %s 自动排 standard_doc 失败（不影响作答）：%s", req_id, e)

    def on_created(self, req_id: str) -> None:
        """建卡后（v4-4，DESIGN-v4 §3 触发/§8）：自动排队 source_import——只自动一次（人取消即终局，
        失败/取消后人工可重试）；建卡是主操作，编排失败不连累（沿 ①② 容错惯例）。"""
        try:
            if self.tasks.any_task_of_type(req_id, task_mod.T_SOURCE_IMPORT):
                return
            t = self.tasks.create_auto(req_id, task_mod.T_SOURCE_IMPORT, "auto:card-created", "建卡自动导入原文")
            log.info("[L2] %s 建卡 → 自动创建 #%s source_import", req_id, t.id)
        except ApiException as e:
            log.info("[L2] %s 自动排 source_import 跳过：%s", req_id, e)
        except Exception as e:  # noqa: BLE001  # 编排失败绝不连累建卡主操作
            log.warning("[L2] %s 自动排 source_import 失败（不影响建卡）：%s", req_id, e)

    def on_published(self, req_id: str) -> None:
        """发布成功后：终态收尾（取消 queued 残留）→ 自动排队用例生成。"""
        # 第一段：终态收尾——独立容错，清理失败不阻断 case_gen 排队，更不影响已完成的发布
        try:
            cancelled = self.tasks.settle_terminal(req_id)
            if cancelled > 0:
                log.info("[L2] %s 发布收尾：取消 %d 个终态残留排队任务（质检/合成）", req_id, cancelled)
        except Exception as e:  # noqa: BLE001  # 不影响发布
            log.warning("[L2] %s 发布收尾取消残留任务失败（不影响发布）：%s", req_id, e)
        # 第二段：自动排队 case_gen（cancel 仅对 queued 生效，是人手中的逃生门）
        try:
            if self.tasks.any_task_of_type(req_id, task_mod.T_CASES):
                return
            t = self.tasks.create_auto(req_id, task_mod.T_CASES, "auto:published", "标准文档已发布，L2 自动排队")
            log.info("[L2] %s 已发布 → 自动创建 #%s case_gen", req_id, t.id)
        except ApiException as e:
            log.info("[L2] %s 自动排 case_gen 跳过：%s", req_id, e)
        except Exception as e:  # noqa: BLE001  # 不影响发布
            log.warning("[L2] %s 自动排 case_gen 失败（不影响发布）：%s", req_id, e)

    def on_change_confirmed(self, req_id: str, change_id: int, impact: dict[str, Any] | None) -> None:
        """变更单 confirm 后（v4.12，DESIGN-v4 §8）：影响面 digest/FR 非空 → 自动排 incremental_check
        （v4-14 注册任务类型——此前 create_guarded 400 被容错吞掉，钩子结构先落，上线即通）。
        人工逃生门（§5.5 第 6 步）：incremental_check 开放手动创建——不能让需求停在 changed +
        影响面冻结 + 增量无人排的中间态。"""
        try:
            n = 0
            if is_json_obj(impact) and is_json_obj(impact.get("counts")):
                counts = impact["counts"]
                n = int(counts.get("digestRows") or 0) + int(counts.get("frPoints") or 0)
            if n == 0:
                log.info("[L2] %s 变更单 #%s 影响面无 digest/FR 行，不排增量质检", req_id, change_id)
                return
            t = self.tasks.create_auto(req_id, "incremental_check", "auto:change-confirmed",
                                       f"变更单 #{change_id} 确认，L2 自动排增量质检")
            log.info("[L2] %s 变更单 #%s confirm → 自动创建 #%s incremental_check", req_id, change_id, t.id)
        except ApiException as e:
            log.info("[L2] %s 变更单 #%s 自动排增量质检跳过（任务类型随 v4-14 注册）：%s",
                     req_id, change_id, e)
        except Exception as e:  # noqa: BLE001  # 编排失败不连累 confirm 主操作
            log.warning("[L2] %s 变更单 #%s 自动排增量质检失败（不影响确认）：%s", req_id, change_id, e)

    def on_merged(self, req_id: str) -> None:
        """增量合并采纳后（v4-14，DESIGN-v4 §8）：changed ∧ 澄清全处置 → 自动排 standard_doc
        （重合成点火点——补断链：影响面不含澄清项的纯 digest/FR 变更无人作答、on_answered 永不
        触发）。窗口判重本批用「只自动一次」基线；on_answered 窗口化（时间下界）随 v4-15 精确化。"""
        try:
            r = self.repos.requirement.find(req_id)
            if r is None or r.status != "changed":
                return
            counts = self.repos.clarify_item.counts(req_id)
            # total==0 视为全处置：本钩子正是为「纯 digest/FR 变更无人作答」补的重合成断链（§8）
            if counts["total"] != 0 and counts["answered"] < counts["total"]:
                return
            if self.tasks.any_task_of_type(req_id, task_mod.T_STANDARD,
                                           since=self._clarify_window_start(req_id)):
                return
            t = self.tasks.create_auto(req_id, task_mod.T_STANDARD, "auto:incremental-merged",
                                       "增量已合并且澄清全处置，L2 自动排队重合成")
            log.info("[L2] %s 增量合并完成 → 自动创建 #%s standard_doc", req_id, t.id)
        except ApiException as e:
            log.info("[L2] %s 合并后排 standard_doc 跳过：%s", req_id, e)
        except Exception as e:  # noqa: BLE001  # 编排失败不连累采纳主操作
            log.warning("[L2] %s 合成后自动排 standard_doc 失败（不影响采纳）：%s", req_id, e)

    def on_gate_opened(self, req_id: str, round_no: int) -> None:
        """开闸建轮后：自动排队 gate_review（收敛自 gate.open 事务内 create_auto——批次7，AUDIT D1-4）。

        与 ①② 的容错差异（对抗审查 M1）：原实现中建轮与排任务同事务（排不上则开闸整体回滚）；
        挪出后「轮已建、评审任务没排上」的残余窗口由 open 事务内 count_active 预判消除主因，
        此处失败以 log.error 高可见（人可经门禁页手动建任务补救），不回滚已建的轮。"""
        try:
            t = self.tasks.create_auto(req_id, task_mod.T_GATE, "gate:open",
                                       f"评审轮 r{round_no}（开闸三条件全绿）")
            log.info("[L2] %s 开闸 r%d → 自动创建 #%s gate_review", req_id, round_no, t.id)
        except ApiException as e:
            log.error("[L2] %s 开闸 r%d 自动排 gate_review 被守卫拒绝（轮已建，需人工在门禁页补建）：%s",
                      req_id, round_no, e)
        except Exception as e:  # noqa: BLE001  # 编排失败不连累开闸主操作
            log.error("[L2] %s 开闸 r%d 自动排 gate_review 失败（轮已建，需人工在门禁页补建）：%s",
                      req_id, round_no, e)
