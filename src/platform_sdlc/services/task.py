"""任务域服务（对应 TaskService）：任务队列守卫 + 草稿两态 + approve 守卫继承。

核心原则：线上化不改守卫，只改触发方——草稿 approve 直接走 push_artifacts/sync_upsert，
发布锁定 / seq 答复保留 / P 编号漂移防护 / 用例乐观锁全部自动继承。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from platform_sdlc.common import ApiException, Times, jint_or_400, jrequired, jtext
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

D_DIGEST = "digest_bundle"
D_STANDARD = "standard"
D_CASES = "cases"
D_DESIGN = "design"
D_GATE_ISSUES = "gate_issues"

_TASK_TYPES = {T_QUALITY, T_STANDARD, T_CASES, T_DESIGN, T_DESIGN_REV, T_GATE}


# 发布后合法的任务类型（终态锁/发布收尾的统一豁免面，D36）：用例/设计/修订/门禁评审
_POST_PUBLISH_TYPES = frozenset({T_CASES, T_DESIGN, T_DESIGN_REV, T_GATE})


def _post_publish_task(type_: str) -> bool:
    """发布后合法的任务类型（终态锁豁免面，D36）：用例/设计/修订/门禁评审都是发布后流程。"""
    return type_ in _POST_PUBLISH_TYPES


class TaskService:
    def __init__(self, db: Database, repos: Repos, requirement_service: Any, case_service: Any,
                 design_service: Any) -> None:
        # 服务依赖（对应 Java TaskService 注入 Requirement/Case/DesignService）
        self.db = db
        self.repos = repos
        self.requirements = requirement_service
        self.cases = case_service
        self.designs = design_service

    # ---------- 任务 ----------

    def create(self, req_id: str, body: dict[str, Any]) -> Task:
        return self.create_guarded(req_id, jrequired(body, "type"), jtext(body, "note", ""), "")

    def create_auto(self, req_id: str, type_: str, created_by: str, note: str) -> Task:
        """L2 编排自动建任务入口：与人工建任务同一套守卫（create_guarded）。

        created_by 记录自动来源，前端据此展示「自动」标记，人可 cancel（仅 queued）。"""
        return self.create_guarded(req_id, type_, note, created_by)

    def any_task_of_type(self, req_id: str, type_: str) -> bool:
        """该需求是否曾建过某类型任务（不限状态）——L2 钩子「只自动一次」判断。

        走 EXISTS 直查而非 list()：task.list 带 LIMIT 100，任务行溢出窗口后最早记录滑出，
        已取消的自动任务会被 L2 误判为「从未建过」而复活（红线：人取消即终局）。"""
        return self.repos.task.exists_by_type(req_id, type_)

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
            if type_ in (T_DESIGN, T_DESIGN_REV):
                # 设计 lane 前置（D34）：发布后才可生成；C 级/已跳过/未配仓守卫收敛在 DesignService
                if r.status != "published":
                    raise ApiException(409, f"设计 lane 属发布后流程：先完成澄清并发布（当前：{r.status}）")
                if type_ == T_DESIGN:
                    self.designs.assert_gen_allowed(r)
                elif self.repos.artifact.find(req_id, "design") is None:
                    raise ApiException(400, "无已定稿设计可修订（design_rev 用于 GR-4 落改闭环）")
            if type_ == T_GATE and r.status != "published":
                # 门禁 lane 前置（与 gate.open 同口径）：排队与开闸同一守卫面，堵「排队即被发布误作废」的口径缝隙
                raise ApiException(409, f"门禁属发布后流程：先完成澄清并发布（当前：{r.status}）")
            if type_ == T_STANDARD:
                if r.status != "clarifying":
                    raise ApiException(409, f"仅待澄清状态可生成标准文档（当前：{r.status}）")
                # D21 处置口径：驳回/挂起计入已处置（不作答也可关闭问题）
                counts = self.repos.clarify_item.counts(req_id)
                if counts["total"] == 0 or counts["answered"] < counts["total"]:
                    raise ApiException(400, f"存在未处置问题（{counts['answered']}/{counts['total']}），"
                                            "全部问题答复/驳回/挂起后才可生成标准文档")
            if self.repos.task.count_active(req_id, type_) > 0:
                raise ApiException(409, "该需求已有同类型任务在排队或执行中，请等待完成")
            t = Task(id=0, req_id=req_id, type=type_, status="queued",
                     payload=_json({"note": note}), created_by=created_by, created_at=Times.now())
            t.id = self.repos.task.insert(t)
        return t

    def claim(self, runner: str) -> Task | None:
        return self.repos.task.claim(runner, Times.now())

    def finish(self, task_id: int, ok: bool, body: dict[str, Any] | None) -> Task:
        t = self.repos.task.find(task_id)
        if t is None:
            raise ApiException(404, f"任务不存在：{task_id}")
        body = body or {}  # done/failed 可无 body，全 null 字段即默认值语义
        # 通道兜底（评测 P0-2b）：外置 runner 通道撞终态锁的失败回报，统一补作废语义
        if not ok and self.superseded_by_publish(t):
            if not jtext(body, "error", "").strip():
                body["error"] = "需求已发布（终态锁定），任务作废"
            body["reason"] = "superseded-by-publish"
            body["hint"] = "标准文档已由人工定稿；该任务不可重试"
        result = jtext(body, "result", "") if ok else _json(_fail_map(body))
        if not self.repos.task.finish(task_id, "done" if ok else "failed", result, Times.now()):
            raise ApiException(409, f"仅执行中（running）的任务可回报结果（当前：{t.status}）")
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
        t = self.repos.task.find(task_id)
        if t is None:
            raise ApiException(404, f"任务不存在：{task_id}")
        if self.superseded_by_publish(t):
            raise ApiException(409, "该任务随需求发布已作废，不可重试（用例生成不受影响）")
        if not self.repos.task.retry(task_id, Times.now()):
            raise ApiException(409, "仅失败（failed）的任务可重试")
        out = self.repos.task.find(task_id)
        assert out is not None
        return out

    def cancel(self, task_id: int) -> Task:
        t = self.repos.task.find(task_id)
        if t is None:
            raise ApiException(404, f"任务不存在：{task_id}")
        if not self.repos.task.cancel_queued(task_id, '{"error":"排队中被取消"}', Times.now()):
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
        if type_ not in (D_DIGEST, D_STANDARD, D_CASES, D_DESIGN, D_GATE_ISSUES):
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
            d = DraftArtifact(
                req_id=req_id, type=type_, md=jtext(body, "md", ""), content_json=jtext(body, "contentJson", ""),
                task_id=jint_or_400(body, "taskId"), updated_at=Times.now(),
            )
            if type_ == D_DIGEST and not d.content_json.strip():
                raise ApiException(400, "digest_bundle 草稿必须携带 contentJson（完整推送 payload）")
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
                # 草稿 contentJson → 推送请求（与 HTTP 通道同一绑定口径：宽松 dict + 服务层校验）
                out = self.requirements.push_artifacts(req_id, _parse_object(d.content_json, "推送产物"))
            elif type_ == D_CASES:
                out = self.cases.sync_upsert(req_id, _parse_object(d.content_json, "cases 推送"))
            elif type_ == D_DESIGN:
                out = {"finalizedVersion": self.designs.finalize(req_id, d, by)}
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
    if not isinstance(v, dict):
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
