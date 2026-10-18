"""任务域服务（对应 TaskService）：任务队列守卫 + 草稿两态 + approve 守卫继承。

核心原则：线上化不改守卫，只改触发方——草稿 approve 直接走 push_artifacts/sync_upsert，
发布锁定 / seq 答复保留 / P 编号漂移防护 / 用例乐观锁全部自动继承。
"""

from __future__ import annotations

import json
import logging
from typing import Any, cast

from platform_sdlc.common import ApiException, IntegrityException, Times, is_json_obj, jint_or_400, jrequired, jtext
from platform_sdlc.db import Database
from platform_sdlc.models import DraftArtifact, Task
from platform_sdlc.repositories import Repos

log = logging.getLogger(__name__)

T_QUALITY = "quality_check"
T_STANDARD = "standard_doc"
T_CASES = "case_gen"
T_DESIGN = "design_gen"
T_DESIGN_REV = "design_rev"
T_GATE = "gate_review"
T_SOURCE_IMPORT = "source_import"  # v4-3（DESIGN-v4 §3）：原文导入——拉取+解析+评论导入，落库走完成回调
T_INCREMENTAL = "incremental_check"  # v4-14（DESIGN-v4 §6）：变更后局部质检——受影响章节切片增量梳理

D_DIGEST = "digest_bundle"
D_INCREMENTAL = "incremental_bundle"
D_STANDARD = "standard"
D_CASES = "cases"
D_DESIGN = "design"
D_GATE_ISSUES = "gate_issues"

_TASK_TYPES = {T_QUALITY, T_STANDARD, T_CASES, T_DESIGN, T_DESIGN_REV, T_GATE, T_SOURCE_IMPORT,
                T_INCREMENTAL}


# 发布后合法的任务类型（终态锁/发布收尾的统一豁免面，D36）：用例/设计/修订/门禁评审
_POST_PUBLISH_TYPES = frozenset({T_CASES, T_DESIGN, T_DESIGN_REV, T_GATE})

# 可重试 reason 白名单（N-1）：失败任务是「执行没跑成」才可重试；生命周期取消/作废
# （req-deleted / superseded-by-publish / change-aborted / cancelled）一律终局——重试复活
# 会向已删需求/已放弃变更写数据，违反终态锁红线的延伸语义。
_RETRYABLE_REASONS = frozenset({
    "import-failed", "integrity-failed", "timeout", "no-draft", "agent-exception", "blocks-empty",
})


def _post_publish_task(type_: str) -> bool:
    """发布后合法的任务类型（终态锁豁免面，D36）：用例/设计/修订/门禁评审都是发布后流程。"""
    return type_ in _POST_PUBLISH_TYPES


def _reason_of(result_json: str) -> str:
    """失败结果的机器码（_fail_map 落库形态；纯文本/旧格式无 reason 归空串）。"""
    try:
        v: Any = json.loads(result_json or "{}")
    except json.JSONDecodeError:
        return ""
    if not isinstance(v, dict):
        return ""
    return str(cast("dict[str, Any]", v).get("reason") or "")


class _ImportFailure(Exception):
    """source_import 落库失败：抛出以回滚导入事务（块表/评论不留半途写入），
    再由 finish 在独立事务中记 failed 终态。"""

    def __init__(self, body: dict[str, Any]) -> None:
        super().__init__(str(body.get("error") or "导入失败"))
        self.body = body


class TaskService:
    def __init__(self, db: Database, repos: Repos, requirement_service: Any, case_service: Any,
                 design_service: Any, block_service: Any, change_service: Any = None) -> None:
        # 服务依赖（对应 Java TaskService 注入 Requirement/Case/DesignService）
        self.db = db
        self.repos = repos
        self.requirements = requirement_service
        self.cases = case_service
        self.designs = design_service
        self.changes = change_service
        self.blocks = block_service  # v4-3：source_import 完成回调落库（§3 挂点 = finish）

    # ---------- 任务 ----------

    def create(self, req_id: str, body: dict[str, Any]) -> Task:
        return self.create_guarded(req_id, jrequired(body, "type"), jtext(body, "note", ""), "")

    def create_auto(self, req_id: str, type_: str, created_by: str, note: str) -> Task:
        """L2 编排自动建任务入口：与人工建任务同一套守卫（create_guarded）。

        created_by 记录自动来源，前端据此展示「自动」标记，人可 cancel（仅 queued）。"""
        return self.create_guarded(req_id, type_, note, created_by)

    def any_task_of_type(self, req_id: str, type_: str, since: str | None = None) -> bool:
        """该需求是否曾建过某类型任务（不限状态）——L2 钩子「只自动一次」判断（v4-15 窗口化：
        since 非空 = 只看澄清完备窗口起点之后——变更 confirm 开新窗口，旧取消不再是永久终局，
        「人取消即终局」收窄为「窗口内终局」，显式重新裁决见 DESIGN-v4 §8）。

        走 EXISTS 直查而非 list()：task.list 带 LIMIT 100，任务行溢出窗口后最早记录滑出，
        已取消的自动任务会被 L2 误判为「从未建过」而复活。"""
        return self.repos.task.exists_by_type(req_id, type_, since)

    def create_guarded(self, req_id: str, type_: str, note: str, created_by: str) -> Task:
        with self.db.tx():
            r = self.repos.requirement.find(req_id)
            if r is None:
                raise ApiException(404, f"需求不存在：{req_id}")
            if type_ not in _TASK_TYPES:
                raise ApiException(400, f"非法任务类型：{type_}")
            if r.status == "published" and not _post_publish_task(type_):
                # 终态锁定只约束质检与标准文档；用例/设计/修订本来就是发布后的正常动作
                log.warning("建任务被终态锁拒绝：%s %s（已发布，非发布后类型）", req_id, type_)
                raise ApiException(409, f"该需求已发布标准文档（终态锁定），不能新建{type_}任务")
            if r.status == "changed" and type_ == T_QUALITY and not self.changes.rebaseline_open(req_id):
                # v4-15（§5.1 矩阵）：changed 拒全量质检——原文消化只走增量通道（回 published 后
                # 重新开放；增量不可行的兜底 = 放弃变更）。v4-17 豁免：rebaseline 单放行一次全量（§10）
                raise ApiException(409, "该需求处于变更中（changed），全量质检已关闭——走增量质检合并"
                                        "（rebaseline 变更单例外，放行一次全量）")
            if type_ in (T_DESIGN, T_DESIGN_REV):
                # 设计 lane 前置（D34）：发布后才可生成；C 级/已跳过/未配仓守卫收敛在 DesignService。
                # v4-15（§5.1 矩阵）：changed 是发布后状态的合法变体——in-review 落改通道（design_rev）
                # 依赖此行，否则变更单一生效落改即被堵死、流程顺序倒挂
                if r.status not in ("published", "changed"):
                    raise ApiException(409, f"设计 lane 属发布后流程：先完成澄清并发布（当前：{r.status}）")
                if type_ == T_DESIGN:
                    self.designs.assert_gen_allowed(r)
                elif self.repos.artifact.find(req_id, "design") is None:
                    raise ApiException(400, "无已定稿设计可修订（design_rev 用于 GR-4 落改闭环）")
            if type_ == T_GATE and r.status != "published":
                # 门禁 lane 前置（与 gate.open 同口径）：排队与开闸同一守卫面，堵「排队即被发布误作废」的口径缝隙
                raise ApiException(409, f"门禁属发布后流程：先完成澄清并发布（当前：{r.status}）")
            if type_ == T_STANDARD:
                if r.status not in ("clarifying", "changed"):
                    # v4-15（§5.1 矩阵）：changed 下 L2 可排重合成（on_merged/on_answered 点火）
                    raise ApiException(409, f"仅待澄清/变更中状态可生成标准文档（当前：{r.status}）")
                # D21 处置口径：驳回/挂起计入已处置（不作答也可关闭问题）
                counts = self.repos.clarify_item.counts(req_id)
                if counts["total"] == 0 or counts["answered"] < counts["total"]:
                    raise ApiException(400, f"存在未处置问题（{counts['answered']}/{counts['total']}），"
                                            "全部问题答复/驳回/挂起后才可生成标准文档")
            if type_ == T_SOURCE_IMPORT:
                self._guard_import_sealed(r)
            if type_ == T_INCREMENTAL:
                # v4-14（§5.5 第 6 步）：人工逃生门——L2 排不上时人可手动补建，不能让需求停在
                # changed + 影响面冻结 + 增量无人排的中间态。守卫 = changed ∧ 存在 confirmed 单 ∧ 影响面非空。
                if r.status != "changed":
                    raise ApiException(409, f"增量质检属变更后流程：需求处于 changed 才可（当前：{r.status}）")
                self.changes.guard_incremental(r)
            if self.repos.task.count_active(req_id, type_) > 0:
                raise ApiException(409, "该需求已有同类型任务在排队或执行中，请等待完成")
            t = Task(id=0, req_id=req_id, type=type_, status="queued",
                     payload=_json({"note": note}), created_by=created_by, created_at=Times.now())
            t.id = self.repos.task.insert(t)
        return t

    def claim(self, runner: str) -> Task | None:
        t = self.repos.task.claim(runner, Times.now())
        if t is not None and t.type == T_QUALITY and self.repos.req_block.empty(t.req_id):
            # v4-4（DESIGN-v4 §3 质检前置）：不做任务 DAG——启动守卫查块表非空，空则 failed 引导。
            # 双通道（runner next 路由 / 进程内 loop）都汇合在 TaskService.claim，单点实现；
            # 返回 None = 领取方本轮无任务，下一轮询自然前进（不自旋查库）。
            self.finish(t.id, False, {
                "error": "原文未导入，先完成 source_import", "reason": "blocks-empty",
                "hint": "等待导入任务完成或人工重试导入后，重新创建质检任务",
                "runner": t.runner, "attempt": t.attempt})
            return None
        return t

    def _guard_import_sealed(self, r: Any) -> None:
        """v4-3（DESIGN-v4 §2.4）：导入一次性分源封存——封存只约束「全量替换式重导」。
        text 源：块表非空即封存（重跑 ID 全换族，锚定下游全部漂移）；
        飞书源：有平台编辑痕迹（version>1 或未软删 blk- 块）或平台评论即封存；
        对账式重导（服务端 diff → 变更单，三期）不覆盖平台数据，永远合法不在此守卫面。
        失败重试块表仍空 = 合法（首导/无下游时的失败重试）。"""
        if r.source_type == "text" and not self.repos.req_block.empty(r.id):
            raise ApiException(409, "文本源已导入（块表非空），全量重导已封存；"
                                    "原文变更请直接编辑块（二期上线）或走变更单（三期）")
        if r.source_type == "feishu" and (self.repos.req_block.has_edit_trace(r.id)
                                          or self.repos.req_comment.has_platform(r.id)):
            raise ApiException(409, "飞书源已导入且存在平台编辑痕迹/平台评论，全量重导已封存；原文变更走变更单")

    def finish(self, task_id: int, ok: bool, body: dict[str, Any] | None) -> Task:
        """任务终态回报（双通道汇合点）。

        P1-6 顺序修正：终态校验（状态 / 执行器 / attempt）与 source_import 落库、终态置位收进
        同一事务——校验不过在任何副作用之前 409；导入异常整体回滚（块表/评论保持原状）后另起
        事务记 failed。attempt 是 retry 递增的 fencing 代数，旧执行器迟到回报不得覆盖新 attempt。
        body 的 runner/attempt 可选（旧脚本兼容，缺省不做对应校验）；runner/agent 两通道始终携带。
        """
        body = body or {}  # done/failed 可无 body，全 null 字段即默认值语义
        runner = jtext(body, "runner", "")
        attempt_raw = body.get("attempt")
        attempt: int | None = None
        if attempt_raw is not None:
            try:
                attempt = int(attempt_raw)
            except (TypeError, ValueError) as e:
                raise ApiException(400, f"attempt 须为整数（收到：{attempt_raw!r}）") from e
        out: Task | None = None
        try:
            with self.db.tx():
                t = self.repos.task.find(task_id)
                if t is None:
                    raise ApiException(404, f"任务不存在：{task_id}")
                # 先校验、后副作用：取消/重复回报/旧 attempt 回报一律在导入之前 409
                if t.status != "running":
                    raise ApiException(409, f"仅执行中（running）的任务可回报结果（当前：{t.status}）")
                if runner and t.runner and runner != t.runner:
                    raise ApiException(409, f"执行器不匹配（任务归属：{t.runner}，回报：{runner}）")
                if attempt is not None and attempt != t.attempt:
                    raise ApiException(409, f"执行尝试已过期（当前 attempt={t.attempt}，回报 {attempt}）"
                                            "——结果已被 retry 取代，丢弃")
                # 通道兜底（评测 P0-2b）：外置 runner 通道撞终态锁的失败回报，统一补作废语义
                if not ok and self.superseded_by_publish(t):
                    if not jtext(body, "error", "").strip():
                        body["error"] = "需求已发布（终态锁定），任务作废"
                    body["reason"] = "superseded-by-publish"
                    body["hint"] = "标准文档已由人工定稿；该任务不可重试"
                # v4-3（DESIGN-v4 §3 落库机制细则）：source_import 完成回调——服务端直读输出文件落库
                # （内容不经 LLM 上下文），落库成功才置 done；失败转 failed 可 retry。
                # 两通道（runner POST done / 进程内 loop）在此汇合，落库只此一处。
                if ok and t.type == T_SOURCE_IMPORT:
                    r = self.repos.requirement.find(t.req_id)
                    if r is None:
                        # N-1：软删需求不落库（claim 已过滤，此处堵「领取后删除」竞态）
                        raise _ImportFailure({"error": "需求不存在或已删除，导入落库拒绝",
                                              "reason": "req-deleted", "hint": "任务随需求删除作废，不可重试"})
                    try:
                        self._guard_import_sealed(r)  # P1-6③：与建任务同一口径的导入前复检
                    except ApiException as e:
                        raise _ImportFailure({"error": str(e), "reason": "sealed",
                                              "hint": "封存后原文变更走变更单；该任务不可重试"}) from e
                    try:
                        imported = self.blocks.import_from_output(t)
                        body = {"result": _json(imported)}
                    except IntegrityException as e:
                        # 守恒终检未过（guard 对账/媒体状态/missingMedia）：与一般导入失败分流
                        raise _ImportFailure({"error": str(e), "reason": "integrity-failed",
                                              "hint": "守恒终检未过：按错误内文案修复后 retry"}) from e
                    except ApiException as e:
                        raise _ImportFailure({"error": str(e), "reason": "import-failed",
                                              "hint": "确认任务工作区 out/doc.json 已由 parse_doc 产出后重试"}) from e
                    except Exception as e:  # noqa: BLE001  # 落库异常兜底转 failed
                        raise _ImportFailure({"error": f"导入落库失败：{e}", "reason": "import-failed",
                                              "hint": "可重试（retry 重新排队）"}) from e
                result = jtext(body, "result", "") if ok else _json(_fail_map(body))
                if not self.repos.task.finish(task_id, "done" if ok else "failed", result,
                                              Times.now(), attempt=t.attempt):
                    raise ApiException(409, f"仅执行中（running）的任务可回报结果（当前：{t.status}）")
                out = self.repos.task.find(task_id)
        except _ImportFailure as f:
            # 导入事务已随异常回滚（块表/评论原状）；独立事务记 failed 终态
            with self.db.tx():
                cur = self.repos.task.find(task_id)
                if cur is None:
                    raise ApiException(404, f"任务不存在：{task_id}") from f
                if not self.repos.task.finish(task_id, "failed", _json(_fail_map(f.body)),
                                              Times.now(), attempt=attempt):
                    raise ApiException(409, f"仅执行中（running）的任务可回报结果（当前：{cur.status}）") from f
                out = self.repos.task.find(task_id)
        assert out is not None
        log.info("任务 #%s %s：%s（runner=%s）", task_id, out.status, out.type, out.runner)
        return out

    def settle_terminal(self, req_id: str) -> int:
        """发布终态收尾（评测 P0-2b）：取消同需求 queued 的质检/标准文档任务（发布后物理上不可能成功）。
        豁免面与 _post_publish_task 统一（D36）：case_gen/design_gen/design_rev/gate_review 都是发布后流程，
        排队中的不误作废。tx 持锁整个方法，堵住查与取消之间被领取的窗口。"""
        with self.db.tx():
            return self.repos.task.cancel_queued_by_req(
                req_id, _POST_PUBLISH_TYPES,
                _json(_fail_map({}, error="需求已发布（终态锁定），任务自动作废", reason="superseded-by-publish",
                                hint="标准文档已由人工定稿；如需重新质检请新开一轮需求")),
                Times.now())

    def superseded_by_publish(self, t: Task) -> bool:
        """领取后被需求发布作废（质检/合成类 + 需求终态）——agent/Runner 领取后的兜底检查。
        v2.4 D36：发布后合法类型（用例/设计/修订）不构成作废。"""
        r = self.repos.requirement.find(t.req_id)
        return r is not None and r.status == "published" and not _post_publish_task(t.type)

    def retry(self, task_id: int) -> Task:
        """失败任务重排（N-1）：reason 白名单 + 需求存活复检——生命周期取消/作废一律终局，
        软删需求的任务不得复活（否则 source_import 会向已删卡块表/评论写数据）。"""
        with self.db.tx():
            t = self.repos.task.find(task_id)
            if t is None:
                raise ApiException(404, f"任务不存在：{task_id}")
            if self.repos.requirement.find(t.req_id) is None:
                raise ApiException(409, "需求已删除，任务不可重试")
            if self.superseded_by_publish(t):
                raise ApiException(409, "该任务随需求发布已作废，不可重试（用例生成不受影响）")
            reason = _reason_of(t.result)
            if reason not in _RETRYABLE_REASONS:
                raise ApiException(409, f"该任务失败原因不可重试：{reason or '未知'}"
                                        "（生命周期取消/作废为终局，人取消即终局）")
            if not self.repos.task.retry(task_id, Times.now()):
                raise ApiException(409, "仅失败（failed）的任务可重试")
            out = self.repos.task.find(task_id)
            assert out is not None
            return out

    def cancel(self, task_id: int) -> Task:
        t = self.repos.task.find(task_id)
        if t is None:
            raise ApiException(404, f"任务不存在：{task_id}")
        if not self.repos.task.cancel_queued(
                task_id, '{"error":"排队中被取消","reason":"cancelled","hint":"人取消即终局，不可重试"}',
                Times.now()):
            raise ApiException(409, "仅排队中（queued）的任务可取消")
        out = self.repos.task.find(task_id)
        assert out is not None
        return out

    def list(self, req_id: str | None, status: str | None, limit: int = 100) -> list[Task]:
        return self.repos.task.list(req_id, status, limit)

    def expire_stale(self, older_than_minutes: int, runner: str | None = None) -> int:
        """执行器启动恢复：超时 running 置 failed，返回处理条数。
        runner 非空时按归属过滤（双通道各清自己名下任务）。"""
        from datetime import datetime, timedelta

        cutoff = (datetime.now() - timedelta(minutes=older_than_minutes)).strftime("%Y-%m-%d %H:%M:%S")
        return self.repos.task.expire_stale_running(
            cutoff, '{"error":"任务超时（执行器中断）","reason":"timeout","hint":"可重试再次排队"}', Times.now(),
            runner=runner)

    # ---------- 草稿 ----------

    def put_draft(self, req_id: str, type_: str, body: dict[str, Any]) -> DraftArtifact:
        if type_ not in (D_DIGEST, D_STANDARD, D_CASES, D_DESIGN, D_GATE_ISSUES, D_INCREMENTAL):
            raise ApiException(400, f"非法草稿类型：{type_}")
        with self.db.tx():
            # 终态检查与写同事务：堵「检查后发布、发布后落草稿」的拒草稿终态锁 TOCTOU 窗口
            r = self.repos.requirement.find(req_id)
            if r is None:
                raise ApiException(404, f"需求不存在：{req_id}")
            post_publish = type_ in (D_CASES, D_DESIGN, D_GATE_ISSUES)
            if r.status == "published" and not post_publish:
                # 终态锁不约束用例/设计草稿：两者都是发布后的正常动作（与 create 的豁免口径一致）
                raise ApiException(409, "该需求已发布（终态锁定），不能写草稿")
            if r.status == "changed" and type_ == D_DIGEST and not self.changes.rebaseline_open(req_id):
                # v4-15（§5.1 草稿面矩阵）：changed 下许可面 = incremental_bundle + standard；
                # v4-17 豁免：rebaseline 单放行一次（§10——空 anchor 存量行的全量重建通道）
                raise ApiException(409, "该需求处于变更中（changed），全量质检草稿已关闭（许可面："
                                        "incremental_bundle + standard；rebaseline 变更单例外）")
            d = DraftArtifact(
                req_id=req_id, type=type_, md=jtext(body, "md", ""), content_json=jtext(body, "contentJson", ""),
                task_id=jint_or_400(body, "taskId"), updated_at=Times.now(),
            )
            if type_ == D_DIGEST and not d.content_json.strip():
                raise ApiException(400, "digest_bundle 草稿必须携带 contentJson（完整推送 payload）")
            if type_ == D_INCREMENTAL:
                if not d.content_json.strip():
                    raise ApiException(400, "incremental_bundle 草稿必须携带 contentJson（增量包结构）")
                # v4-14（§6.1 selfCheck 机械校验，服务端单点）：章节区间 = heading block_id 禁 seq
                # 数值、新行 anchor ∈ 受影响块集（防 LLM 越界重写）、作废清单行真实存在
                self.changes.validate_bundle(req_id, d.content_json)
            self.repos.draft.upsert(d, d.updated_at or "")
            out = self.repos.draft.find(req_id, type_)
            assert out is not None
            return out

    def list_drafts(self, req_id: str) -> list[DraftArtifact]:
        if self.repos.requirement.find(req_id) is None:
            raise ApiException(404, f"需求不存在：{req_id}")
        return self.repos.draft.list_by_req(req_id)

    def get_draft(self, req_id: str, type_: str) -> DraftArtifact:
        """需求存在性（含软删过滤）与草稿存在性双 404（delete_draft 经本方法继承）。"""
        if self.repos.requirement.find(req_id) is None:
            raise ApiException(404, f"需求不存在：{req_id}")
        d = self.repos.draft.find(req_id, type_)
        if d is None:
            raise ApiException(404, f"草稿不存在：{type_}")
        return d

    def delete_draft(self, req_id: str, type_: str) -> dict[str, Any]:
        d = self.get_draft(req_id, type_)
        self.repos.draft.delete(req_id, type_)
        return {"ok": True, "deleted": d.type}

    def approve(self, req_id: str, type_: str, by: str) -> dict[str, Any]:
        """草稿采纳（人审关口放行）：digest_bundle → push_artifacts（全守卫继承）；cases → sync_upsert；
        design → 定稿（D34：artifact design version+1 + design_final_json 留痕）。
        standard 无 approve——发布是终态动作，走现有 POST /publish。"""
        with self.db.tx():
            # 采纳原子性：副作用（内层 tx 可重入加入外层）与草稿删除同事务，异常整体回滚（草稿保留可重试）
            d = self.get_draft(req_id, type_)
            if type_ == D_DIGEST:
                # 草稿 contentJson → 推送请求（与 HTTP 通道同一绑定口径：宽松 dict + 服务层校验）；
                # doc 为平台导出回环（块表权威）不再落 artifact；守恒门只守外部导入（source_import / sdlc-sync）
                payload = _parse_object(d.content_json, "推送产物")
                payload.pop("doc", None)
                out = self.requirements.push_artifacts(req_id, payload)
            elif type_ == D_CASES:
                out = self.cases.sync_upsert(req_id, _parse_object(d.content_json, "cases 推送"))
            elif type_ == D_DESIGN:
                out = {"finalizedVersion": self.designs.finalize(req_id, d, by)}
            elif type_ == D_INCREMENTAL:
                # v4-14（§6.2）：采纳 = 服务端纯机械合并一个事务（章节级整组替换，人审关口放行）
                out = self.changes.merge_incremental(req_id, d, by)
            else:
                raise ApiException(400, "标准文档草稿无采纳动作，请在预览后直接发布（终态）")
            self.repos.draft.delete(req_id, type_)
            out["draftApproved"] = True
            return out


def _parse_object(content_json: str, label: str) -> dict[str, Any]:
    try:
        v = json.loads(content_json.strip() or "{}")
    except Exception as e:
        raise ApiException(400, f"草稿 contentJson 解析失败：{e}") from e
    if not is_json_obj(v):
        raise ApiException(400, "草稿 contentJson 必须是 JSON 对象")
    _ = label
    return v


def _fail_map(body: dict[str, Any], error: str | None = None, reason: str = "", hint: str = "") -> dict[str, Any]:
    """失败信息结构化（评测 P1-1）：error=结论 / reason=机器码 / hint=下一步 / diag=诊断尾部。"""
    m: dict[str, Any] = {"error": error if error is not None else jtext(body, "error", "执行失败")}
    reason_v = reason or jtext(body, "reason", "")
    hint_v = hint or jtext(body, "hint", "")
    if reason_v:
        m["reason"] = reason_v
    if hint_v:
        m["hint"] = hint_v
    diag = jtext(body, "diag", "")
    if diag:
        m["diag"] = diag
    return m


def _json(o: Any) -> str:
    return json.dumps(o, ensure_ascii=False, separators=(",", ":"))
