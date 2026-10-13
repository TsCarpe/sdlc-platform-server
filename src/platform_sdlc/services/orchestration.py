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
from typing import TYPE_CHECKING

from platform_sdlc.common import ApiException
from platform_sdlc.services import task as task_mod

if TYPE_CHECKING:
    from platform_sdlc.repositories import Repos
    from platform_sdlc.services.task import TaskService

log = logging.getLogger(__name__)


class OrchestrationService:
    def __init__(self, repos: Repos, tasks: TaskService) -> None:
        self.repos = repos
        self.tasks = tasks

    def on_answered(self, req_id: str) -> None:
        """澄清作答后：全部答完且从未建过 standard_doc 任务 → 自动排队标准文档合成。"""
        try:
            counts = self.repos.clarify_item.counts(req_id)
            if counts["total"] == 0 or counts["answered"] < counts["total"]:
                return
            if self.tasks.any_task_of_type(req_id, task_mod.T_STANDARD):
                return
            t = self.tasks.create_auto(req_id, task_mod.T_STANDARD, "auto:clarify-complete", "澄清全部答完，L2 自动排队")
            log.info("[L2] %s 澄清全部答完 → 自动创建 #%s standard_doc", req_id, t.id)
        except ApiException as e:
            log.info("[L2] %s 自动排 standard_doc 跳过：%s", req_id, e)  # 预期内：已有同类活跃任务（409）等
        except Exception as e:  # noqa: BLE001——编排失败绝不连累作答主操作
            log.warning("[L2] %s 自动排 standard_doc 失败（不影响作答）：%s", req_id, e)

    def on_published(self, req_id: str) -> None:
        """发布成功后：终态收尾（取消 queued 残留）→ 自动排队用例生成。"""
        # 第一段：终态收尾——独立容错，清理失败不阻断 case_gen 排队，更不影响已完成的发布
        try:
            cancelled = self.tasks.settle_terminal(req_id)
            if cancelled > 0:
                log.info("[L2] %s 发布收尾：取消 %d 个终态残留排队任务（质检/合成）", req_id, cancelled)
        except Exception as e:  # noqa: BLE001——不影响发布
            log.warning("[L2] %s 发布收尾取消残留任务失败（不影响发布）：%s", req_id, e)
        # 第二段：自动排队 case_gen（cancel 仅对 queued 生效，是人手中的逃生门）
        try:
            if self.tasks.any_task_of_type(req_id, task_mod.T_CASES):
                return
            t = self.tasks.create_auto(req_id, task_mod.T_CASES, "auto:published", "标准文档已发布，L2 自动排队")
            log.info("[L2] %s 已发布 → 自动创建 #%s case_gen", req_id, t.id)
        except ApiException as e:
            log.info("[L2] %s 自动排 case_gen 跳过：%s", req_id, e)
        except Exception as e:  # noqa: BLE001——不影响发布
            log.warning("[L2] %s 自动排 case_gen 失败（不影响发布）：%s", req_id, e)

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
