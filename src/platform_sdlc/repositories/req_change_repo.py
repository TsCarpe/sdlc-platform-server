"""v4.11 变更单仓储（DESIGN-v4 §5.2/§5.4）：draft CRUD + items 全量重算替换 + delete 子孙递归 CTE。"""

from __future__ import annotations

from platform_sdlc.db import Database
from platform_sdlc.models import ReqChange, ReqChangeItem


class ReqChangeRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    def insert(self, c: ReqChange) -> int:
        return self.db.insert(
            "INSERT INTO req_change(req_id,title,detail,proposed_by,confirmed_by,status,impact_json,"
            "created_at,confirmed_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (c.req_id, c.title, c.detail, c.proposed_by, c.confirmed_by, c.status, c.impact_json,
             c.created_at, c.confirmed_at),
        )

    def find(self, req_id: str, change_id: int) -> ReqChange | None:
        row = self.db.query_one("SELECT * FROM req_change WHERE req_id=? AND id=?", (req_id, change_id))
        return ReqChange.from_row(row) if row else None

    def list_by_req(self, req_id: str) -> list[ReqChange]:
        return [ReqChange.from_row(r) for r in self.db.query_all(
            "SELECT * FROM req_change WHERE req_id=? ORDER BY id DESC", (req_id,))]

    def update_draft_meta(self, change_id: int, title: str, detail: str, proposed_by: str) -> int:
        """draft 元信息更新（items 由 replace_items 全量重算，不在此面）。"""
        return self.db.execute(
            "UPDATE req_change SET title=?, detail=?, proposed_by=? WHERE id=? AND status='draft'",
            (title, detail, proposed_by, change_id))

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
