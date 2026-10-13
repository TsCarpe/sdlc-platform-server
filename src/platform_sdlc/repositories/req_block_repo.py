"""v4.1 原文块仓储（DESIGN-v4 §2.2）：一行一块，锚点（block_id）即主键。"""

from __future__ import annotations

from platform_sdlc.db import Database
from platform_sdlc.models import ReqBlock


class ReqBlockRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    def replace_all(self, req_id: str, blocks: list[ReqBlock]) -> None:
        """全量替换该卡块（§3 导入幂等语义：重跑=替换该卡全部块；调用方事务内）。"""
        self.db.execute("DELETE FROM req_block WHERE req_id=?", (req_id,))
        if not blocks:
            return
        self.db.executemany(
            "INSERT INTO req_block(req_id,block_id,seq,type,text,attrs_json,version,updated_at,updated_by,deleted)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            [(b.req_id, b.block_id, b.seq, b.type, b.text, b.attrs_json, b.version, b.updated_at, b.updated_by, b.deleted)
             for b in blocks],
        )

    def list_by_req(self, req_id: str) -> list[ReqBlock]:
        """文档序读取（§2.3 线性文档流：ORDER BY seq；软删行不进读面）。"""
        return [ReqBlock.from_row(r) for r in self.db.query_all(
            "SELECT * FROM req_block WHERE req_id=? AND deleted='' ORDER BY seq", (req_id,))]

    def has_edit_trace(self, req_id: str) -> bool:
        """平台编辑痕迹（§2.4 飞书源全量重导封存判定）：version>1 或存在未软删的 blk- 前缀块
        ——放弃变更的 insert 逆留下的软删行不算痕迹。"""
        return bool(self.db.query_scalar(
            "SELECT EXISTS(SELECT 1 FROM req_block WHERE req_id=?"
            " AND (version>1 OR (deleted='' AND block_id LIKE 'blk-%')))",
            (req_id,)))

    def empty(self, req_id: str) -> bool:
        """块表是否为空（§2.4「块表为空=首次导入」：含软删行——曾有导入即非空）。"""
        return not bool(self.db.query_scalar(
            "SELECT EXISTS(SELECT 1 FROM req_block WHERE req_id=?)", (req_id,)))

    def deleted_ids(self, req_id: str) -> set[str]:
        """软删块 ID 集（v4-7 编辑面基线过期检测：提交 id 命中即拒——软删块的复活是三期放弃变更
        的逆向语义，不属窗口 1 直接编辑，引导前端刷新基线）。"""
        return {str(r["block_id"]) for r in self.db.query_all(
            "SELECT block_id FROM req_block WHERE req_id=? AND deleted!=''", (req_id,))}
