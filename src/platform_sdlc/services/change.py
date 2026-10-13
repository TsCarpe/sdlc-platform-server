"""v4.11 变更单服务（DESIGN-v4 §5.2/§5.3/§5.4）：draft 期数据面——建/存/删/读 + 影响面预览。

draft 不碰块表（随时可提，不限需求状态——为 confirm 做准备）；items 由全量块数组经
BlockService.diff_blocks 得出（diff 只在服务端一份，v4-7 落地三期复用），反复保存全量重算替换；
insert 块草稿期即有平台 ID（blk- 沿用不漂移）。影响面预览机械 join 现算不冻结（impact_json 仅
confirm 时冻结，v4-12）：edit/move 只标直接锚定行、delete 子孙按 parent 递归 CTE 一次查询、
heading 删不带章节体——宁可漏报不可误报，漏的由人在确认界面补勾。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from platform_sdlc.common import ApiException, Times, is_json_list, is_json_obj, jrequired, jtext
from platform_sdlc.db import Database
from platform_sdlc.models import ReqChange, ReqChangeItem, TestCase
from platform_sdlc.repositories import Repos
from platform_sdlc.services.block import BlockService

log = logging.getLogger(__name__)


class ChangeService:
    def __init__(self, db: Database, repos: Repos, blocks: BlockService) -> None:
        self.db = db
        self.repos = repos
        self.blocks = blocks

    # ---------- draft CRUD（§5.3） ----------

    def create(self, req_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """建 draft：元信息 + 全量块数组一次到位（items 立即 diff 落库）。proposedBy 手填必填
        （§5.2 不设审批环，防误操作不防故意）；draft 不碰块表，不限需求状态。"""
        with self.db.tx():
            self._require_base(req_id)
            items = self._diff_items(req_id, body)
            change_id = self.repos.req_change.insert(ReqChange(
                id=None, req_id=req_id, title=jtext(body, "title", ""), detail=jtext(body, "detail", ""),
                proposed_by=jrequired(body, "proposedBy"), status="draft", created_at=Times.now()))
            self.repos.req_change.replace_items(change_id, items)
        log.info("变更单 draft #%s %s：%s", change_id, req_id, _counts(items))
        return self.detail(req_id, change_id)

    def save(self, req_id: str, change_id: int, body: dict[str, Any]) -> dict[str, Any]:
        """反复保存：items 全量重算替换（§5.3），元信息缺省沿用；仅 draft 可存
        （confirmed 是冻结快照，作废走 §5.6 放弃动作——v4-13）。"""
        with self.db.tx():
            self._require_base(req_id)
            c = self._require_change(req_id, change_id)
            if c.status != "draft":
                raise ApiException(409, f"变更单 #{change_id} 已{c.status}，不可再编辑（confirmed 是冻结快照）")
            items = self._diff_items(req_id, body)
            self.repos.req_change.update_draft_meta(
                change_id, jtext(body, "title", c.title), jtext(body, "detail", c.detail),
                jtext(body, "proposedBy", "") or c.proposed_by)
            self.repos.req_change.replace_items(change_id, items)
        return self.detail(req_id, change_id)

    def delete(self, req_id: str, change_id: int) -> dict[str, Any]:
        """draft 可直接删（未碰块表零代价，§5.6）；confirmed 不作废——删除通道仅对 draft 开放。"""
        with self.db.tx():
            self._require_base(req_id)
            c = self._require_change(req_id, change_id)
            if c.status != "draft":
                raise ApiException(409, f"变更单 #{change_id} 已{c.status}，不可删除（confirmed 不作废，修正走新 draft）")
            self.repos.req_change.delete(req_id, change_id)
        return {"ok": True}

    def list(self, req_id: str) -> list[dict[str, Any]]:
        self._require_base(req_id)
        out: list[dict[str, Any]] = []
        for c in self.repos.req_change.list_by_req(req_id):
            row = {"id": c.id, "title": c.title, "detail": c.detail, "proposedBy": c.proposed_by,
                   "confirmedBy": c.confirmed_by, "status": c.status, "createdAt": c.created_at,
                   "confirmedAt": c.confirmed_at}
            out.append({k: v for k, v in row.items() if v is not None})
        return out

    def detail(self, req_id: str, change_id: int) -> dict[str, Any]:
        self._require_base(req_id)
        c = self._require_change(req_id, change_id)
        items = self.repos.req_change.list_items(change_id)
        return {
            "id": c.id, "reqId": c.req_id, "title": c.title, "detail": c.detail,
            "proposedBy": c.proposed_by, "confirmedBy": c.confirmed_by, "status": c.status,
            "createdAt": c.created_at, "confirmedAt": c.confirmed_at,
            "items": [{"blockId": i.block_id, "op": i.op, "oldText": i.old_text, "newText": i.new_text,
                       "oldAttrs": _load_or_none(i.old_attrs), "newAttrs": _load_or_none(i.new_attrs)}
                      for i in items],
            "counts": _counts(items),
        }

    # ---------- 影响面预览（§5.4：机械 join 现算，不冻结） ----------

    def preview_impact(self, req_id: str, change_id: int) -> dict[str, Any]:
        """受影响块集 = edit/move 直接块 ∪ delete 子孙全集（递归 CTE；insert 是新块无下游锚定，
        不进影响面）。四类清单 = 块集 join 澄清项/FR/digest 行/关联用例（anchor 精确匹配——空
        anchor 行永不进影响面，§5.7）；章节 = 受影响块所在章节的 heading block_id（禁 seq 数值，
        整体重排天然无害）。heading 删不带章节体：块集不按区间扩散，只按锚定 join（§5.4）。"""
        self._require_base(req_id)
        c = self._require_change(req_id, change_id)
        items = self.repos.req_change.list_items(change_id)
        direct = [i.block_id for i in items if i.op in ("edit", "move")]
        deletes = [i.block_id for i in items if i.op == "delete"]
        target = set(direct) | self.repos.req_change.descendant_blocks(req_id, deletes)

        sections = _sections_of(self.repos.req_block.list_by_req(req_id), target)
        clarify = [{"seq": x.seq, "level": x.level, "question": x.question, "status": x.status}
                   for x in self.repos.clarify_item.list_by_req(req_id) if x.anchor in target]
        frs = [{"id": p.id, "name": p.name} for p in self.repos.fr_point.list_by_req(req_id)
               if p.anchor in target]
        hit_fr = {f["id"] for f in frs}
        cases = _cases_by_fr(self.repos.test_case.list_by_req(req_id), hit_fr)
        digest_rows = self._digest_rows_in(req_id, target)
        return {
            "changeId": c.id, "status": c.status,
            "blocks": sorted(target), "sections": sections,
            "clarifyItems": clarify, "frPoints": frs, "cases": cases, "digestRows": digest_rows,
            "counts": {"blocks": len(target), "sections": len(sections), "clarify": len(clarify),
                       "frPoints": len(frs), "cases": len(cases), "digestRows": len(digest_rows)},
        }

    # ---------- 私有 ----------

    def _require_base(self, req_id: str) -> None:
        """draft 期基础守卫：404 + 块表空无 diff 基线拒。不限需求状态、不查活跃质检——draft 不碰
        块表，与窗口 1 直接编辑（_guard_edit）分立两套守卫面（§5.1：draft 随时可提）。"""
        if self.repos.requirement.find(req_id) is None:
            raise ApiException(404, f"需求不存在：{req_id}")
        if self.repos.req_block.empty(req_id):
            raise ApiException(409, "原文未导入（块表空，无变更基线）：先完成 source_import")

    def _require_change(self, req_id: str, change_id: int) -> ReqChange:
        c = self.repos.req_change.find(req_id, change_id)
        if c is None:
            raise ApiException(404, f"变更单不存在：#{change_id}")
        return c

    def _diff_items(self, req_id: str, body: dict[str, Any]) -> list[ReqChangeItem]:
        """全量块数组 vs 块表 → ReqChangeItem（复用 diff_blocks 四态 + LIS；insert 块经
        assign_insert_ids 在草稿期即有平台 ID，§5.2）。"""
        submitted = self.blocks.parse_blocks(body)
        current = self.repos.req_block.list_by_req(req_id)
        diff = self.blocks.diff_blocks(req_id, submitted, current)
        self.blocks.assign_insert_ids(req_id, submitted)
        return [ReqChangeItem(
            id=None, change_id=0, block_id=d.block_id, op=d.op,
            old_text=d.old_text, new_text=d.new_text,
            old_attrs=_dumps(d.old_attrs), new_attrs=_dumps(d.new_attrs)) for d in diff]

    def _digest_rows_in(self, req_id: str, target: set[str]) -> list[dict[str, str]]:
        """digest 行锚定 join：artifact.content_json（_digest_view 形态）解析 features/flows/states，
        anchor ∈ 块集的行带摘要（digest 行无稳定行 ID，按 anchor 定位——§6.2 合并同规则）。"""
        a = self.repos.artifact.find(req_id, "digest")
        if a is None or not a.content_json:
            return []
        try:
            content: Any = json.loads(a.content_json)
        except json.JSONDecodeError:
            return []
        if not is_json_obj(content):
            return []
        out: list[dict[str, str]] = []
        for f in _arr(content.get("features")):
            if is_json_obj(f) and jtext(f, "anchor", "") in target:
                out.append({"kind": "feature", "anchor": jtext(f, "anchor", ""),
                            "summary": f"{jtext(f, 'role', '')}·{jtext(f, 'm', '')}：{jtext(f, 'func', '')}"})
        for f in _arr(content.get("flows")):
            if is_json_obj(f) and jtext(f, "anchor", "") in target:
                out.append({"kind": "flow", "anchor": jtext(f, "anchor", ""),
                            "summary": f"{jtext(f, 'role', '')}：{jtext(f, 'step', '')}"})
        for st in _arr(content.get("states")):
            if is_json_obj(st) and jtext(st, "anchor", "") in target:
                out.append({"kind": "state", "anchor": jtext(st, "anchor", ""),
                            "summary": f"{jtext(st, 'obj', '')}：{jtext(st, 'from', '')}→{jtext(st, 'to', '')}"})
        return out


# ---------- 模块级 helper ----------


def _counts(items: list[ReqChangeItem]) -> dict[str, int]:
    c = {"edit": 0, "insert": 0, "delete": 0, "move": 0}
    for i in items:
        c[i.op] += 1
    return c


def _sections_of(blocks_all: list[Any], target: set[str]) -> list[dict[str, str]]:
    """受影响块 → 所在章节（读时推导，heading 栈同 _render_blocks 口径）：输出 heading block_id +
    面包屑——**禁 seq 数值**（整体重排会改写全表 seq，冻结数值即失真，§5.4）。"""
    heading_stack: list[tuple[int, str, str]] = []  # (level, heading_block_id, text)
    section_of: dict[str, tuple[str, str]] = {}     # block_id -> (heading_id, path)
    for b in blocks_all:
        if b.type == "heading":
            level = _level_of(b.attrs_json)
            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()
            heading_stack.append((level, b.block_id, b.text))
        if heading_stack:
            section_of[b.block_id] = (heading_stack[-1][1], " › ".join(t for _, _, t in heading_stack))
        else:
            section_of[b.block_id] = ("", "")
    out: dict[str, dict[str, str]] = {}
    for bid in target:
        hid, path = section_of.get(bid, ("", ""))
        if hid:
            out.setdefault(hid, {"headingId": hid, "path": path})
    return list(out.values())


def _level_of(attrs_json: str) -> int:
    v = _load_or_none(attrs_json)
    if v is not None and isinstance(v.get("level"), int):
        return v["level"]
    return 1


def _cases_by_fr(cases: list[TestCase], hit_fr: set[str]) -> list[dict[str, Any]]:
    """关联用例：points 引用受影响 FR 的用例（RTM 反向索引，读时聚合）。"""
    out: list[dict[str, Any]] = []
    for t in cases:
        try:
            pts: Any = json.loads(t.points or "[]")
        except json.JSONDecodeError:
            pts = []
        hit = [str(p) for p in pts] if is_json_list(pts) else []
        hit = [p for p in hit if p in hit_fr]
        if hit:
            out.append({"id": t.id, "title": t.title, "frPoints": hit})
    return out


def _arr(v: Any) -> list[Any]:
    return list(v) if is_json_list(v) else []


def _dumps(v: dict[str, Any] | None) -> str:
    return json.dumps(v, ensure_ascii=False, separators=(",", ":")) if v is not None else ""


def _load_or_none(raw: str) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        v: Any = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return dict(v) if is_json_obj(v) else None
