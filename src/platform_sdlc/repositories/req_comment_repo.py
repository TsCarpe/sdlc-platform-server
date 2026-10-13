"""v4.1 评论仓储（DESIGN-v4 §4.2）：飞书导入与平台评论同表同管道（origin 区分）。"""

from __future__ import annotations

from platform_sdlc.db import Database
from platform_sdlc.models import ReqComment


class ReqCommentRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    def insert(self, c: ReqComment) -> int:
        """单条插入返回自增 id（reply_to 回复链定位用）。"""
        return self.db.insert(
            "INSERT INTO req_comment(req_id,block_id,origin,external_id,author,text,reply_to,resolved,created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (c.req_id, c.block_id, c.origin, c.external_id, c.author, c.text, c.reply_to, c.resolved, c.created_at),
        )

    def insert_many(self, comments: list[ReqComment]) -> None:
        """批量插入（调用方事务内；reply_to 已由调用方定位）。"""
        if not comments:
            return
        self.db.executemany(
            "INSERT INTO req_comment(req_id,block_id,origin,external_id,author,text,reply_to,resolved,created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            [(c.req_id, c.block_id, c.origin, c.external_id, c.author, c.text, c.reply_to, c.resolved, c.created_at)
             for c in comments],
        )

    def upsert_feishu(self, req_id: str, external_id: str, block_id: str, author: str,
                      text: str, at: str) -> None:
        """幂等 upsert（v4.17 §10）：external_id 唯一定位——已有行保原 id/reply_to/resolved/
        created_at（对账重导不重置平台态），只刷内容面；新行插入。"""
        row = self.db.query_one(
            "SELECT id FROM req_comment WHERE req_id=? AND origin='feishu' AND external_id=?",
            (req_id, external_id))
        if row is not None:
            self.db.execute(
                "UPDATE req_comment SET block_id=?, author=?, text=? WHERE id=?",
                (block_id, author, text, int(row["id"])))
            return
        self.db.execute(
            "INSERT INTO req_comment(req_id,block_id,origin,external_id,author,text,reply_to,resolved,"
            "created_at) VALUES(?,?, 'feishu',?,?,?,NULL,0,?)",
            (req_id, block_id, external_id, author, text, at))

    def list_by_req(self, req_id: str) -> list[ReqComment]:
        return [ReqComment.from_row(r) for r in self.db.query_all(
            "SELECT * FROM req_comment WHERE req_id=? ORDER BY id", (req_id,))]

    def delete_feishu(self, req_id: str) -> int:
        """删除该卡飞书导入行（§3 导入幂等：重跑替换 origin=feishu 行，平台侧评论不动）。"""
        return self.db.execute("DELETE FROM req_comment WHERE req_id=? AND origin='feishu'", (req_id,))

    def find(self, req_id: str, comment_id: int) -> ReqComment | None:
        """单条定位（升级/解决/删除的目标行）。"""
        row = self.db.query_one("SELECT * FROM req_comment WHERE req_id=? AND id=?", (req_id, comment_id))
        return ReqComment.from_row(row) if row else None

    def set_resolved_many(self, ids: list[int], resolved: bool) -> None:
        """线程级解决态批量落库（§4.3：解决是回复串级动作，对齐飞书体验；调用方事务内）。"""
        if not ids:
            return
        self.db.executemany(
            "UPDATE req_comment SET resolved=? WHERE id=?",
            [(1 if resolved else 0, i) for i in ids])

    def delete_one(self, req_id: str, comment_id: int) -> int:
        """单条物理删（回复行保留——悬空 reply_to 在组装面归根处理，§4.3 自由讨论低门槛）。"""
        return self.db.execute("DELETE FROM req_comment WHERE req_id=? AND id=?", (req_id, comment_id))

    def has_platform(self, req_id: str) -> bool:
        """存在平台评论（§2.4 飞书源重导封存判定条件之一）。"""
        return bool(self.db.query_scalar(
            "SELECT EXISTS(SELECT 1 FROM req_comment WHERE req_id=? AND origin='platform')", (req_id,)))
