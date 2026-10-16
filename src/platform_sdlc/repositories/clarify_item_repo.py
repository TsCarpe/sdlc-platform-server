"""澄清项仓储（对应 ClarifyItemRepoImpl）。"""

from __future__ import annotations

from platform_sdlc.db import Database
from platform_sdlc.models import ClarifyItem
from platform_sdlc.states import CLARIFY_DISPOSED_SQL


class ClarifyItemRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    def list_by_req(self, req_id: str) -> list[ClarifyItem]:
        return [ClarifyItem.from_row(r) for r in self.db.query_all("SELECT * FROM clarify_item WHERE req_id=? ORDER BY id", (req_id,))]

    def disposition_map_by_seq(self, req_id: str) -> dict[str, ClarifyItem]:
        """seq -> 有平台处置痕迹的行（DESIGN-v5 D17：answer 非空 ∨ status≠pending ∨ note 非空。
        空答复的驳回也携带——D23；note 非空的 pending 重开行（变更单重开带原答复备注）同样携带——
        否则重开行被重推当全新行，note 丢失且 skill 自带 answer 会把重开态顶回 answered）。"""
        out: dict[str, ClarifyItem] = {}
        for c in self.list_by_req(req_id):
            has_answer = bool(c.answer)
            has_status = bool(c.status) and c.status != "pending"
            if has_answer or has_status or bool(c.note):
                out[c.seq] = c
        return out

    def delete_machine_by_req(self, req_id: str) -> None:
        """按来源合并（v4-9，DESIGN-v4 §6.3/§9）：重推删机器行（skill+incremental），保留 local
        （评论升级 L 系行经重推不丢——§12 二期判据；增量接编的 P 系行在全量新清单无同 seq 项时
        回填无目标即丢，属 §6.3 如实表述的预期语义）。"""
        self.db.execute(
            "DELETE FROM clarify_item WHERE req_id=? AND origin IN ('skill','incremental')", (req_id,))

    def insert(self, c: ClarifyItem) -> None:
        self.db.execute(
            "INSERT INTO clarify_item(req_id,seq,level,theme,question,ctx,answer,answered_at,quote,anchor,"
            "status,note,origin,src_comment_id,options,module,layer) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (c.req_id, c.seq, c.level, c.theme, c.question, c.ctx, c.answer, c.answered_at, c.quote,
             c.anchor, c.status, c.note, c.origin, c.src_comment_id, c.options_json, c.module, c.layer),
        )

    def insert_many(self, items: list[ClarifyItem]) -> None:
        """批量插入（AUDIT D2-4：重推重建清单的循环逐条消除；调用方事务内）。"""
        if not items:
            return
        self.db.executemany(
            "INSERT INTO clarify_item(req_id,seq,level,theme,question,ctx,answer,answered_at,quote,anchor,"
            "status,note,origin,src_comment_id,options,module,layer) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(c.req_id, c.seq, c.level, c.theme, c.question, c.ctx, c.answer, c.answered_at, c.quote,
              c.anchor, c.status, c.note, c.origin, c.src_comment_id, c.options_json, c.module, c.layer)
             for c in items],
        )

    def reopen_items(self, req_id: str, rows: list[tuple[str, str, str | None]]) -> None:
        """变更 confirm 的受影响澄清重开（v4.12 §5.5 第 4 步，调用方事务内）：rows = (seq, 新 note,
        answered_at=None)；answer 清空回 pending。行集合由服务层按冻结影响面算好（一次读全行，
        循环内无数据库查询铁律）。"""
        if not rows:
            return
        self.db.executemany(
            "UPDATE clarify_item SET answer='', status='pending', note=?, answered_at=? WHERE req_id=? AND seq=?",
            [(note, at, req_id, seq) for seq, note, at in rows])

    def set_disposition(self, req_id: str, seq: str, answer: str, status: str, note: str, at: str) -> int:
        return self.db.execute(
            "UPDATE clarify_item SET answer=?, status=?, note=?, answered_at=? WHERE req_id=? AND seq=?",
            (answer, status, note, at, req_id, seq),
        )

    def counts(self, req_id: str) -> dict[str, int]:
        """answered = 已处置数（status 口径，D21/D22：显式写入 + 启动回填，无 answer 读时降级）。"""
        row = self.db.query_one(
            "SELECT COUNT(*) AS total, SUM(CASE WHEN status IN " + CLARIFY_DISPOSED_SQL + " THEN 1 ELSE 0 END)"
            " AS answered FROM clarify_item WHERE req_id=?",  # 处置口径与 states.py 同源（D21/D25）
            (req_id,),
        )
        total = int(row["total"]) if row is not None else 0
        answered = int(row["answered"] or 0) if row is not None else 0
        return {"total": total, "answered": answered}

    # ---------- v4.8（DESIGN-v4 §4.1/§6.3）：评论升级 L 系 ----------

    def exists_by_src_comment(self, req_id: str, comment_id: int) -> bool:
        """该评论是否已升级过（升级幂等判定：EXISTS 直查，不重复插行）。"""
        return bool(self.db.query_scalar(
            "SELECT EXISTS(SELECT 1 FROM clarify_item WHERE req_id=? AND src_comment_id=?)",
            (req_id, comment_id)))

    def next_machine_seq(self, req_id: str, extra: set[str] | None = None) -> str:
        """P 系下一编号（§6.3：incremental 行 P 系接 max+1——机器产物统一 P 系对 PM 呈现无差别；
        extra = 本批已分配的 seq（连续分配去重）。"""
        nums: list[int] = []
        for row in self.db.query_all(
                "SELECT seq FROM clarify_item WHERE req_id=? AND seq LIKE 'P%'", (req_id,)):
            tail = str(row[0])[1:]
            if tail.isdigit():
                nums.append(int(tail))
        for seq in extra or set():
            if seq[1:].isdigit():
                nums.append(int(seq[1:]))
        return f"P{max(nums, default=0) + 1}"

    def next_local_seq(self, req_id: str) -> str:
        """L 系下一编号（§6.3：L<两位> 从 L01 起，与 P 系两族在 UNIQUE(req_id,seq) 下天然不冲突）。
        一次查询取全部 L 行，Python 侧按数值聚合——字典序 MAX 在两位补零之外（L100+）会乱序。"""
        nums: list[int] = []
        for row in self.db.query_all(
                "SELECT seq FROM clarify_item WHERE req_id=? AND seq LIKE 'L%'", (req_id,)):
            tail = str(row[0])[1:]
            if tail.isdigit():
                nums.append(int(tail))
        return f"L{max(nums, default=0) + 1:02d}"
