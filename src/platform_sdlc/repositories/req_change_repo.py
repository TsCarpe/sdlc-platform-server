"""v4.11 变更单仓储（DESIGN-v4 §5.2/§5.4）：draft CRUD + items 全量重算替换 + delete 子孙递归 CTE。"""

from __future__ import annotations

import json
from typing import Any

from platform_sdlc.common import is_json_obj
from platform_sdlc.db import Database
from platform_sdlc.models import ReqChange, ReqChangeItem


class ReqChangeRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    def insert(self, c: ReqChange) -> int:
        return self.db.insert(
            "INSERT INTO req_change(req_id,kind,title,detail,proposed_by,confirmed_by,status,impact_json,"
            "blocks_json,created_at,confirmed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (c.req_id, c.kind, c.title, c.detail, c.proposed_by, c.confirmed_by, c.status, c.impact_json,
             c.blocks_json, c.created_at, c.confirmed_at),
        )

    def find(self, req_id: str, change_id: int) -> ReqChange | None:
        row = self.db.query_one("SELECT * FROM req_change WHERE req_id=? AND id=?", (req_id, change_id))
        return ReqChange.from_row(row) if row else None

    def list_by_req(self, req_id: str) -> list[ReqChange]:
        return [ReqChange.from_row(r) for r in self.db.query_all(
            "SELECT * FROM req_change WHERE req_id=? ORDER BY id DESC", (req_id,))]

    def update_draft_meta(self, change_id: int, title: str, detail: str, proposed_by: str,
                          blocks_json: str) -> int:
        """draft 元信息 + 全量块数组留底更新（items 由 replace_items 全量重算，不在此面）。"""
        return self.db.execute(
            "UPDATE req_change SET title=?, detail=?, proposed_by=?, blocks_json=? WHERE id=? AND status='draft'",
            (title, detail, proposed_by, blocks_json, change_id))

    def set_confirmed(self, change_id: int, impact_json: str, confirmed_by: str, at: str) -> int:
        """confirm 落库（§5.5 第 2 步）：冻结影响面 + 确认人/时间；draft 之外不可 confirm。"""
        return self.db.execute(
            "UPDATE req_change SET status='confirmed', impact_json=?, confirmed_by=?, confirmed_at=?"
            " WHERE id=? AND status='draft'",
            (impact_json, confirmed_by, at, change_id))

    def set_snapshot(self, change_id: int, snapshot_json: str) -> None:
        """confirm 时点快照落库（§5.6：放弃的逆向回滚依据——baseline 块序 + 重开澄清原值）。"""
        self.db.execute("UPDATE req_change SET snapshot_json=? WHERE id=?", (snapshot_json, change_id))

    def set_aborted(self, change_id: int, aborted_by: str, at: str) -> int:
        """放弃落库（§5.6）：confirmed 之外不可放弃（draft 走 delete 通道，aborted 是终态）。"""
        return self.db.execute(
            "UPDATE req_change SET status='aborted', aborted_by=?, aborted_at=? WHERE id=? AND status='confirmed'",
            (aborted_by, at, change_id))

    def confirmed_undigested(self, req_id: str, digest_pushed_at: str | None) -> list[int]:
        """影响面未消化的前序 confirmed 单（§5.1 串行 confirm）：digest/FR 非空 ∧ 消化时点早于
        confirm 时点。digest_pushed_at = digest artifact 最近推送时间（None = 从未推送，视为未消化）。"""
        out: list[int] = []
        for c in self.list_by_req(req_id):
            if c.status != "confirmed":
                continue
            if not c.impact_json:
                out.append(c.id or 0)
                continue
            try:
                impact: Any = json.loads(c.impact_json)
            except json.JSONDecodeError:
                out.append(c.id or 0)
                continue
            n = 0
            if is_json_obj(impact) and is_json_obj(impact.get("counts")):
                counts = impact["counts"]
                n = int(counts.get("digestRows") or 0) + int(counts.get("frPoints") or 0)
            if n == 0:
                continue  # 影响面为空的单无产物面，不阻塞后单
            if digest_pushed_at is None or (c.confirmed_at or "") > digest_pushed_at:
                out.append(c.id or 0)
        return out

    def delete(self, req_id: str, change_id: int) -> int:
        """draft 直接删（调用方守卫仅 draft；items 级联删——draft 未碰块表零代价，§5.6）。"""
        n = self.db.execute("DELETE FROM req_change WHERE req_id=? AND id=?", (req_id, change_id))
        if n:
            self.db.execute("DELETE FROM req_change_item WHERE change_id=?", (change_id,))
        return n

    # ---------- items ----------

    def replace_items(self, change_id: int, items: list[ReqChangeItem]) -> None:
        """全量重算替换（§5.3：draft 反复保存，items 全量重算；调用方事务内）。
        change_id 取方法参数（items 行的 change_id 是 diff 期占位 0）。"""
        self.db.execute("DELETE FROM req_change_item WHERE change_id=?", (change_id,))
        if not items:
            return
        self.db.executemany(
            "INSERT INTO req_change_item(change_id,block_id,op,old_text,new_text,old_attrs,new_attrs)"
            " VALUES(?,?,?,?,?,?,?)",
            [(change_id, i.block_id, i.op, i.old_text, i.new_text, i.old_attrs, i.new_attrs)
             for i in items],
        )

    def list_items(self, change_id: int) -> list[ReqChangeItem]:
        return [ReqChangeItem(
            id=int(r["id"]), change_id=int(r["change_id"]), block_id=str(r["block_id"]),
            op=str(r["op"]), old_text=str(r["old_text"] or ""), new_text=str(r["new_text"] or ""),
            old_attrs=str(r["old_attrs"] or ""), new_attrs=str(r["new_attrs"] or ""),
        ) for r in self.db.query_all(
            "SELECT * FROM req_change_item WHERE change_id=? ORDER BY id", (change_id,))]

    # ---------- 影响面数据面（§5.4） ----------

    def descendant_blocks(self, req_id: str, block_ids: list[str]) -> set[str]:
        """delete 的子孙全集：parent 反向递归，SQLite 递归 CTE 一次查询（§5.4 裁决——应用层逐层
        SELECT 子块即循环内查库，违反铁律）。parent 存于 attrs_json，经 json_extract 进 join；
        无 parent / parent:null 的行 json_extract 得 NULL 自然不中。软删行不进影响面（块已不在
        读面，锚定其上的产物行在 diff 视图另呈）。"""
        if not block_ids:
            return set()
        seeds = ",".join("?" for _ in block_ids)
        # NULLIF 防 attrs_json='' 空串（json_extract 对空串抛 malformed JSON，对 NULL 返回 NULL）
        rows = self.db.query_all(
            "WITH RECURSIVE desc(block_id) AS ("
            f"  SELECT block_id FROM req_block WHERE req_id=? AND deleted='' AND block_id IN ({seeds})"
            "  UNION ALL"
            "  SELECT c.block_id FROM req_block c JOIN desc d"
            "    ON c.req_id=? AND c.deleted=''"
            "   AND json_extract(NULLIF(c.attrs_json, ''), '$.parent') = d.block_id"
            ") SELECT block_id FROM desc",
            (req_id, *block_ids, req_id))
        return {str(r["block_id"]) for r in rows}
