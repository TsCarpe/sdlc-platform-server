"""用例域服务（对应 CaseService）：skill 全量替换、乐观锁冲突明细、表单通道编辑权守卫。"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from platform_sdlc.common import ApiException, ConflictException, Times, jint_or_400, jrequired, jrequired_non_blank, jtext
from platform_sdlc.db import Database
from platform_sdlc.models import CaseComment, TestCase, string_list
from platform_sdlc.repositories import Repos
from platform_sdlc.schemas import wire

log = logging.getLogger(__name__)

_PRIORITIES = {"P0", "P1", "P2"}


class CaseService:
    def __init__(self, db: Database, repos: Repos) -> None:
        self.db = db
        self.repos = repos

    def sync_upsert(self, req_id_arg: str | None, body: dict[str, Any]) -> dict[str, Any]:
        """skill 批量 upsert。对 skill 来源是全量替换：不在 payload 中的 sdlc-test 用例同步删除
        （含评论）；表单来源用例不归 skill 管，一律保留。expectedVersion 提供且不符 → 整批拒绝
        并返回冲突明细（409 + conflicts）。"""
        req_id = jrequired(body if req_id_arg is None else {**body, "reqId": req_id_arg}, "reqId")
        if self.repos.requirement.find(req_id) is None:
            raise ApiException(404, f"需求不存在：{req_id}")
        items: list[Any] = body.get("items") or []
        if not isinstance(items, list):
            raise ApiException(400, "items 必须是数组（空数组走删除语义请另走表单）")
        if not items:
            raise ApiException(400, "items 不能为空")
        seen_ids: set[str] = set()

        parsed: list[TestCase] = []
        expected: list[int] = []
        normalized_priority = 0
        for n in items:
            priority = jtext(n, "priority", "P2")
            if priority not in _PRIORITIES:
                priority = "P2"  # 机器输入宽容（与人严格相对）
                normalized_priority += 1
            status = jtext(n, "status", "draft")
            if status not in ("draft", "reviewed"):
                raise ApiException(400, f"非法 status：{status}（用例 {jtext(n, 'id', '')}）")
            cid = jrequired(n, "id")
            if cid in seen_ids:
                raise ApiException(400, f"用例编号重复：{cid}")
            seen_ids.add(cid)
            t = TestCase(
                req_id=req_id, id=cid, title=jrequired(n, "title"),
                points=_strings_json(n.get("points")), priority=priority,
                pre=jtext(n, "pre", ""), steps=jtext(n, "steps", ""), expect=jtext(n, "expect", ""),
                owner=jtext(n, "owner", ""), tool=jtext(n, "tool", "sdlc-test"), status=status,
                verify_refs=_strings_json(n.get("verifyRefs")),  # R2/R4：均可选
                defect_refs=_strings_json(n.get("defectRefs")),
            )
            parsed.append(t)
            expected.append(jint_or_400(n, "expectedVersion", -1))

        with self.db.tx():
            # 乐观锁检查与写同事务（DESIGN-v3 §3「syncUpsert 全程持锁」）：检查读与 UPDATE 之间无锁窗口
            conflicts: list[dict[str, Any]] = []
            is_insert: list[bool] = []
            found_versions: list[int] = []
            for t, ev in zip(parsed, expected, strict=True):
                cur = self.repos.test_case.find(req_id, t.id)
                is_insert.append(cur is None)
                found_versions.append(-1 if cur is None else cur.version)
                if cur is not None and ev >= 0 and cur.version != ev:
                    conflicts.append({"id": t.id, "currentVersion": cur.version, "expectedVersion": ev})
            if conflicts:
                log.warning("用例同步版本冲突 %s：%d 条不一致，批次未应用", req_id, len(conflicts))
                raise ConflictException("版本冲突，批次未应用", conflicts)

            at = Times.now()
            # 全局序号一次性批量分配（循环内不查库），插入时内存自增
            next_no = self.repos.test_case.allocate_case_nos(sum(is_insert))
            inserted = 0
            updated = 0
            for i, (t, ins) in enumerate(zip(parsed, is_insert, strict=True)):
                t.created_at = at
                t.updated_at = at
                if ins:
                    t.case_no = next_no
                    next_no += 1
                    self.repos.test_case.insert(t)
                    inserted += 1
                else:
                    # 谓词 = 锁内读到的版本（非调用方 ev：-1 是「不带版本强制覆写」哨兵，作谓词会假冲突）
                    if self.repos.test_case.update_from_sync(t, found_versions[i]) == 0:
                        cur2 = self.repos.test_case.find(req_id, t.id)
                        raise ConflictException("版本冲突，批次未应用", [{
                            "id": t.id, "currentVersion": -1 if cur2 is None else cur2.version,
                            "expectedVersion": found_versions[i]}])
                    updated += 1

            # 全量替换补齐（A1）：skill 来源、不在 payload 的用例 = 本地已删除 → 平台同步删除，评论级联清理
            keep_ids = {t.id for t in parsed}
            removed_ids: list[str] = []
            for cid in self.repos.test_case.stale_sync_ids(req_id, keep_ids):
                self.repos.test_case.delete(req_id, cid)
                self.repos.comment.delete_by_case(req_id, cid)
                removed_ids.append(cid)

            id_warnings = self._id_ambiguity_warnings(req_id, parsed)
            self.repos.requirement.touch_synced(req_id, at)

        out: dict[str, Any] = {"ok": True, "inserted": inserted, "updated": updated,
                               "removed": len(removed_ids), "removedIds": removed_ids}
        if normalized_priority > 0:
            out["normalizedPriority"] = normalized_priority
        # v2.2 编号歧义警告（评测 P0-4）：非阻塞提示，不改写 id（改写会破坏 skill 全量替换匹配）
        if id_warnings:
            out["idWarnings"] = id_warnings
        return out

    def _id_ambiguity_warnings(self, req_id: str, parsed: list[TestCase]) -> list[str]:
        """normalize（去尾部数字前导零）后相同但原 id 不同的组合：payload 内部 + 与库内既有。"""
        warnings: list[str] = []
        seen: dict[str, str] = {}
        for t in parsed:
            key = _normalize_case_id(t.id)
            prev = seen.setdefault(key, t.id)
            if prev != t.id:
                warnings.append(f"{prev} 与 {t.id} 疑似同用例（仅编号格式不同）")
        for t in self.repos.test_case.list_by_req(req_id):
            key = _normalize_case_id(t.id)
            prev = seen.get(key)
            if prev is not None and prev != t.id:
                warnings.append(f"{prev} 与库内既有 {t.id} 疑似同用例（仅编号格式不同）")
        return warnings

    def form_create(self, req_id_arg: str | None, body: dict[str, Any]) -> dict[str, Any]:
        req_id = jrequired(body if req_id_arg is None else {**body, "reqId": req_id_arg}, "reqId")
        if self.repos.requirement.find(req_id) is None:
            raise ApiException(404, f"需求不存在：{req_id}")
        t = TestCase(
            req_id=req_id, id=self.repos.test_case.next_web_id(req_id),
            title=self._required_title(body.get("title")),
            points=_strings_json(body.get("points")),
            priority=self._form_priority(body.get("priority"), "P1"),
            pre=jtext(body, "pre", ""),
            steps=jrequired_non_blank(body, "steps"),
            expect=jrequired_non_blank(body, "expect"),
            owner=jrequired_non_blank(body, "owner"),
            tool="表单", status="draft", created_at=Times.now(), updated_at=Times.now(),
        )
        self.repos.test_case.insert(t)
        return self._view(t, [])

    def form_update(self, req_id: str, case_id: str, body: dict[str, Any]) -> dict[str, Any]:
        self._require_req(req_id)
        cur = self.repos.test_case.find(req_id, case_id)
        if cur is None:
            raise ApiException(404, f"用例不存在：{case_id}")
        if cur.tool != "表单":
            raise ApiException(409, f"该用例由 {cur.tool} 推送维护，平台只读；修改请评论反馈维护人 {cur.owner}")
        ev = jint_or_400(body, "expectedVersion", cur.version)
        if ev != cur.version:
            raise ApiException(409, f"版本已变化（当前 v{cur.version}，提交基于 v{ev}），请刷新后重试")
        # PATCH 语义：仅对显式携带的字段做严格校验——未携带沿用存量（历史脏值不锁死编辑通道）
        title = self._required_title(body["title"]) if body.get("title") is not None else cur.title.strip()
        priority = self._form_priority(body["priority"], cur.priority) if body.get("priority") is not None else cur.priority
        steps = jrequired_non_blank(body, "steps") if body.get("steps") is not None else cur.steps
        expect = jrequired_non_blank(body, "expect") if body.get("expect") is not None else cur.expect
        with self.db.tx():
            # 检查读与 UPDATE 同事务；版本谓词不符（0 行）→ 与前置检查同文案 409
            if self.repos.test_case.update_form(
                    req_id, case_id, title,
                    _strings_json(body.get("points")) if body.get("points") is not None else cur.points,
                    priority, jtext(body, "pre", cur.pre), steps, expect, ev, Times.now()) == 0:
                now_v = self.repos.test_case.find(req_id, case_id)
                raise ApiException(409, f"版本已变化（当前 v{now_v.version if now_v else -1}，提交基于 v{ev}），请刷新后重试")
        return self.detail(req_id, case_id)

    def add_comment(self, req_id: str, case_id: str, body: dict[str, Any]) -> dict[str, Any]:
        self._require_req(req_id)
        if self.repos.test_case.find(req_id, case_id) is None:
            raise ApiException(404, f"用例不存在：{case_id}")
        c = CaseComment(id=0, req_id=req_id, case_id=case_id, by=jtext(body, "by", ""),
                        text=jrequired(body, "text"), created_at=Times.now())
        self.repos.comment.insert(c)
        return self.detail(req_id, case_id)

    def list(self, iter_name: str | None, req_id: str | None, status: str | None,
             tool: str | None, q: str | None) -> list[dict[str, Any]]:
        out = []
        for r in self.repos.test_case.list(iter_name, req_id, status, tool, q):
            out.append({
                "reqId": r.req_id, "id": r.id, "title": r.title, "points": r.points, "priority": r.priority,
                "status": r.status, "owner": r.owner, "tool": r.tool, "version": r.version,
                "updatedAt": r.updated_at, "verifyRefs": r.verify_refs, "defectRefs": r.defect_refs,
                "caseNo": r.case_no, "iter": r.iter, "reqName": r.req_name,
            })
        return out

    def detail(self, req_id: str, case_id: str) -> dict[str, Any]:
        self._require_req(req_id)  # 软删需求下的用例直达 URL 不得命中（评测 P0-3 根过滤补位）
        t = self.repos.test_case.find(req_id, case_id)
        if t is None:
            raise ApiException(404, f"用例不存在：{case_id}")
        return self._view(t, self.repos.comment.list_by_case(req_id, case_id))

    def _require_req(self, req_id: str) -> None:
        """需求存在性（含软删过滤）——用例域入口补位。"""
        if self.repos.requirement.find(req_id) is None:
            raise ApiException(404, f"需求不存在：{req_id}")

    def _view(self, t: TestCase, comments: list[CaseComment]) -> dict[str, Any]:
        return {
            "reqId": t.req_id, "id": t.id, "caseNo": t.case_no,  # caseNo 可为 null（迁移窗口内存量）
            "title": t.title, "points": string_list(t.points), "priority": t.priority, "pre": t.pre,
            "steps": t.steps, "expect": t.expect, "owner": t.owner, "tool": t.tool, "status": t.status,
            "version": t.version, "updatedAt": t.updated_at,
            "verifyRefs": string_list(t.verify_refs), "defectRefs": string_list(t.defect_refs),
            "comments": [wire(c) for c in comments],
            "readonly": t.tool != "表单",
        }

    def _required_title(self, title: Any) -> str:
        t = jrequired_non_blank({"title": title}, "title")
        if len(t) > 120:
            raise ApiException(400, f"用例标题过长（≤120 字，当前 {len(t)} 字）")
        return t

    def _form_priority(self, p: Any, default: str) -> str:
        """表单 priority：白名单严格校验（人输入严格；skill 通道宽容降级见 sync_upsert）。
        仅在请求显式携带时校验（未携带沿用存量，历史脏值不锁死编辑）。"""
        v = jtext({"priority": p}, "priority", default)
        if v not in _PRIORITIES:
            raise ApiException(400, f"非法 priority：{v}（仅支持 P0/P1/P2）")
        return v


def _strings_json(points: Any) -> str:
    """可选字符串数组字段 → JSON 列值，缺省 []。"""
    if points is None:
        points = []
    if not isinstance(points, list):
        raise ApiException(400, "请求体不是合法 JSON 或字段类型不符")
    return json.dumps([str(x) for x in points], ensure_ascii=False)


def _normalize_case_id(case_id: str) -> str:
    """「前缀-数字尾」形态去前导零：TC-01→TC-1、WEB-001→WEB-1；其余原样。"""
    return re.sub(r"(?<=\D)0+(?=\d*$)", "", case_id)
