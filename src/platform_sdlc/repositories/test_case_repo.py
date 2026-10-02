"""测试用例仓储（对应 TestCaseRepoImpl）。"""

from __future__ import annotations

from contextlib import suppress

from platform_sdlc.db import Database
from platform_sdlc.models import CaseListRow, TestCase, string_list


class TestCaseRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    _COLS = (
        "req_id,id,title,points,priority,pre,steps,expect,owner,tool,status,version,created_at,updated_at,verify_refs,defect_refs,case_no"
    )

    def insert(self, t: TestCase) -> None:
        """全局登记序号（评测 P0-4）：调用方不指定时从单调账本取号（硬删不复用）。"""
        if t.case_no is None:
            t.case_no = int(self.allocate_case_nos(1))
        self.db.execute(
            f"INSERT INTO test_case({self._COLS}) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                t.req_id,
                t.id,
                t.title,
                t.points,
                t.priority,
                t.pre,
                t.steps,
                t.expect,
                t.owner,
                t.tool,
                t.status,
                t.version,
                t.created_at,
                t.updated_at,
                t.verify_refs,
                t.defect_refs,
                t.case_no,
            ),
        )

    def allocate_case_nos(self, n: int) -> int:
        """批量分配 n 个全局序号，返回起始号（含）。走 seq_case_no 单调账本（仅增不减）；
        UPDATE+SELECT 在事务内一次持锁完成（对齐 Java「单连接下无竞态」论断，显式更稳）。"""
        with self.db.tx() as conn:
            conn.execute("UPDATE seq_case_no SET val = val + ? WHERE id = 1", (n,))
            row = conn.execute("SELECT val FROM seq_case_no WHERE id = 1").fetchone()
        v = int(row["val"]) if row is not None else n
        return v - n + 1

    def find(self, req_id: str, case_id: str) -> TestCase | None:
        row = self.db.query_one("SELECT * FROM test_case WHERE req_id=? AND id=?", (req_id, case_id))
        return TestCase.from_row(row) if row else None

    def update_from_sync(self, t: TestCase) -> None:
        """同步更新：version+1，来源字段（tool/owner/status/verify_refs/defect_refs）以推送为准。"""
        self.db.execute(
            "UPDATE test_case SET title=?,points=?,priority=?,pre=?,steps=?,expect=?,owner=?,tool=?,status=?,"
            "verify_refs=?,defect_refs=?,version=version+1,updated_at=? WHERE req_id=? AND id=?",
            (
                t.title,
                t.points,
                t.priority,
                t.pre,
                t.steps,
                t.expect,
                t.owner,
                t.tool,
                t.status,
                t.verify_refs,
                t.defect_refs,
                t.updated_at,
                t.req_id,
                t.id,
            ),
        )

    def update_form(
        self, req_id: str, case_id: str, title: str, points: str, priority: str, pre: str, steps: str, expect: str, at: str
    ) -> int:
        """表单编辑：不改 tool/owner/status。"""
        return self.db.execute(
            "UPDATE test_case SET title=?,points=?,priority=?,pre=?,steps=?,expect=?,version=version+1,updated_at=? "
            "WHERE req_id=? AND id=?",
            (title, points, priority, pre, steps, expect, at, req_id, case_id),
        )

    def list(self, iter_name: str | None, req_id: str | None, status: str | None, tool: str | None, q: str | None) -> list[CaseListRow]:
        sql = (
            "SELECT tc.req_id, tc.id, tc.title, tc.points, tc.priority, tc.status, tc.owner, tc.tool, tc.version,"
            " tc.updated_at, tc.verify_refs, tc.defect_refs, tc.case_no,"
            " r.iter AS iter, r.name AS req_name FROM test_case tc JOIN requirement r ON r.id=tc.req_id"
            " WHERE r.deleted_at=''"
        )
        args: list[object] = []
        if iter_name:
            sql += " AND r.iter=?"
            args.append(iter_name)
        if req_id:
            sql += " AND tc.req_id=?"
            args.append(req_id)
        if status:
            sql += " AND tc.status=?"
            args.append(status)
        if tool:
            sql += " AND tc.tool=?"
            args.append(tool)
        if q:
            sql += " AND tc.title LIKE ?"
            args.append("%" + q + "%")
        sql += " ORDER BY tc.id"
        out: list[CaseListRow] = []
        for row in self.db.query_all(sql, tuple(args)):
            out.append(
                CaseListRow(
                    req_id=str(row["req_id"]),
                    id=str(row["id"]),
                    title=str(row["title"]),
                    points=str(row["points"]),
                    priority=str(row["priority"]),
                    status=str(row["status"]),
                    owner=str(row["owner"]),
                    tool=str(row["tool"]),
                    version=int(row["version"]),
                    updated_at=str(row["updated_at"]),
                    verify_refs=string_list(row["verify_refs"]),
                    defect_refs=string_list(row["defect_refs"]),
                    case_no=None if row["case_no"] is None else int(row["case_no"]),
                    iter=str(row["iter"] or ""),
                    req_name=str(row["req_name"] or ""),
                )
            )
        return out

    def count_by_req(self, req_id: str) -> int:
        n = self.db.query_scalar("SELECT COUNT(*) FROM test_case WHERE req_id=?", (req_id,))
        return int(n or 0)

    def list_by_req(self, req_id: str) -> list[TestCase]:
        """需求内全部用例（实体），供需求 detail 做反向索引聚合。"""
        return [TestCase.from_row(r) for r in self.db.query_all("SELECT * FROM test_case WHERE req_id=? ORDER BY id", (req_id,))]

    def next_web_id(self, req_id: str) -> str:
        """表单新用例编号：WEB-<序号>，按需求内递增。"""
        rows = self.db.query_all("SELECT id FROM test_case WHERE req_id=? AND id LIKE 'WEB-%'", (req_id,))
        mx = 0
        for r in rows:
            with suppress(ValueError):
                mx = max(mx, int(str(r[0])[4:]))
        return f"WEB-{mx + 1:03d}"

    def stale_sync_ids(self, req_id: str, keep_ids: set[str]) -> list[str]:
        """全量替换补齐（A1）：skill 来源、不在 payload 中的用例 = 本地已删除 → 平台同步删除。"""
        return [t.id for t in self.list_by_req(req_id) if t.tool == "sdlc-test" and t.id not in keep_ids]

    def mark_reviewed_by_gate(self, req_id: str, at: str) -> int:
        """v2.4 关卡互认（D35）：gate 放行视同关卡1 通过，sdlc-test 草稿置 reviewed。"""
        return self.db.execute(
            "UPDATE test_case SET status='reviewed', updated_at=? WHERE req_id=? AND tool='sdlc-test' AND status='draft'",
            (at, req_id),
        )

    def delete(self, req_id: str, case_id: str) -> None:
        self.db.execute("DELETE FROM test_case WHERE req_id=? AND id=?", (req_id, case_id))
