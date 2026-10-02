"""任务仓储（对应 TaskRepoImpl）：claim 的条件 UPDATE 是双通道互斥的原子性来源。"""

from __future__ import annotations

from collections.abc import Collection

from platform_sdlc.db import Database
from platform_sdlc.models import Task


class TaskRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    def insert(self, t: Task) -> int:
        return self.db.insert(
            "INSERT INTO task(req_id,type,status,payload,result,runner,created_by,created_at,updated_at) VALUES(?,?,?,?,?,'',?,?,?)",
            (t.req_id, t.type, t.status, t.payload, t.result, t.created_by, t.created_at, t.created_at),
        )

    def find(self, task_id: int) -> Task | None:
        row = self.db.query_one("SELECT * FROM task WHERE id=?", (task_id,))
        return Task.from_row(row) if row else None

    def list(self, req_id: str | None, status: str | None) -> list[Task]:
        sql = "SELECT * FROM task WHERE 1=1"
        args: list[object] = []
        if req_id:
            sql += " AND req_id=?"
            args.append(req_id)
        if status:
            sql += " AND status=?"
            args.append(status)
        sql += " ORDER BY id DESC LIMIT 100"
        return [Task.from_row(r) for r in self.db.query_all(sql, tuple(args))]

    def count_active(self, req_id: str, type_: str) -> int:
        n = self.db.query_scalar(
            "SELECT COUNT(*) FROM task WHERE req_id=? AND type=? AND status IN ('queued','running')",
            (req_id, type_),
        )
        return int(n or 0)

    def claim(self, runner: str, at: str) -> Task | None:
        """原子领取：SELECT 后带 status 条件 UPDATE，affected=1 即领取成功。"""
        row = self.db.query_one("SELECT id FROM task WHERE status='queued' ORDER BY id LIMIT 1")
        if row is None:
            return None
        task_id = int(row["id"])
        if (
            self.db.execute(
                "UPDATE task SET status='running', runner=?, updated_at=? WHERE id=? AND status='queued'",
                (runner, at, task_id),
            )
            != 1
        ):
            return None
        return self.find(task_id)

    def finish(self, task_id: int, status: str, result_json: str, at: str) -> bool:
        """仅 running 可回报终态，防竞态覆盖。"""
        return (
            self.db.execute(
                "UPDATE task SET status=?, result=?, updated_at=? WHERE id=? AND status='running'",
                (status, result_json, at, task_id),
            )
            == 1
        )

    def retry(self, task_id: int, at: str) -> bool:
        return (
            self.db.execute(
                "UPDATE task SET status='queued', runner='', result='', updated_at=? WHERE id=? AND status='failed'",
                (at, task_id),
            )
            == 1
        )

    def cancel_queued(self, task_id: int, result_json: str, at: str) -> bool:
        """排队中任务可直接取消（执行中任务须等 Runner 回报或超时恢复）。"""
        return (
            self.db.execute(
                "UPDATE task SET status='failed', result=?, updated_at=? WHERE id=? AND status='queued'",
                (result_json, at, task_id),
            )
            == 1
        )

    def cancel_queued_by_req(self, req_id: str, exclude_types: Collection[str] | None, result_json: str, at: str) -> int:
        """批量取消同需求 queued 任务（单条 UPDATE；exclude_types 非空则排除这些类型，None = 全量取消）。"""
        sql = "UPDATE task SET status='failed', result=?, updated_at=? WHERE req_id=? AND status='queued'"
        args: list[object] = [result_json, at, req_id]
        if exclude_types:
            sql += f" AND type NOT IN ({','.join('?' * len(exclude_types))})"
            args.extend(sorted(exclude_types))
        return self.db.execute(sql, tuple(args))

    def exists_by_type(self, req_id: str, type_: str) -> bool:
        """该需求是否建过某类型任务（不限状态）——无 LIMIT 的存在性直查（L2 只自动一次的判断依据）。"""
        return bool(self.db.query_scalar(
            "SELECT EXISTS(SELECT 1 FROM task WHERE req_id=? AND type=?)", (req_id, type_)))

    def expire_stale_running(self, cutoff: str, fail_result: str, at: str) -> int:
        """Runner 启动恢复：把超时仍 running 的任务置 failed（执行器崩溃遗留）。"""
        return self.db.execute(
            "UPDATE task SET status='failed', result=?, updated_at=? WHERE status='running' AND updated_at < ?",
            (fail_result, at, cutoff),
        )
