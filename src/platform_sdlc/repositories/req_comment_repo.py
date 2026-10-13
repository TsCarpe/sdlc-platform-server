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

    def list_by_req(self, req_id: str) -> list[ReqComment]:
        return [ReqComment.from_row(r) for r in self.db.query_all(
            "SELECT * FROM req_comment WHERE req_id=? ORDER BY id", (req_id,))]

    def delete_feishu(self, req_id: str) -> int:
        """删除该卡飞书导入行（§3 导入幂等：重跑替换 origin=feishu 行，平台侧评论不动）。"""
        return self.db.execute("DELETE FROM req_comment WHERE req_id=? AND origin='feishu'", (req_id,))

    def has_platform(self, req_id: str) -> bool:
        """存在平台评论（§2.4 飞书源重导封存判定条件之一）。"""
        return bool(self.db.query_scalar(
            "SELECT EXISTS(SELECT 1 FROM req_comment WHERE req_id=? AND origin='platform')", (req_id,)))
