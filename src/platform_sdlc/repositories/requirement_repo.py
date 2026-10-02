"""需求仓储（对应 RequirementRepoImpl）。"""

from __future__ import annotations

from contextlib import suppress

from platform_sdlc.db import Database
from platform_sdlc.models import Requirement, RequirementListRow


class RequirementRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    def insert(self, r: Requirement) -> None:
        self.db.execute(
            "INSERT INTO requirement(id,name,iter,source_type,source_url,source_text,status,submitted_by,"
            "standard_doc,created_at,synced_at,risk_tier) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                r.id,
                r.name,
                r.iter,
                r.source_type,
                r.source_url,
                r.source_text,
                r.status,
                r.submitted_by,
                r.standard_doc,
                r.created_at,
                r.synced_at,
                r.risk_tier,
            ),
        )

    def next_id(self, year: str) -> str:
        """REQ-<yyyy>-<序号>：取当年最大序号 +1。显式含已软删行——软删编号永不复用（审计可追溯）。"""
        prefix = f"REQ-{year}-"
        ids = [str(r[0]) for r in self.db.query_all("SELECT id FROM requirement WHERE id LIKE ?", (prefix + "%",))]
        mx = 0
        for rid in ids:
            with suppress(ValueError):
                mx = max(mx, int(rid[len(prefix) :]))
        return f"{prefix}{mx + 1:03d}"

    def find(self, req_id: str) -> Requirement | None:
        """根过滤（评测 P0-3 软删除）：全部写守卫的必经入口，已删需求对服务层整体 404。"""
        row = self.db.query_one("SELECT * FROM requirement WHERE id=? AND deleted_at=''", (req_id,))
        return Requirement.from_row(row) if row else None

    def alive_id_by_iter(self, iter_name: str) -> str | None:
        """v2.4 迭代 1:1 守卫（D32）：只数存活行——软删自动释放迭代名。"""
        row = self.db.query_one(
            "SELECT id FROM requirement WHERE iter=? AND deleted_at='' ORDER BY created_at DESC, id DESC LIMIT 1",
            (iter_name,),
        )
        return str(row[0]) if row else None

    def list_all(self, iter_name: str | None, status: str | None) -> list[RequirementListRow]:
        sql = (
            "SELECT r.id, r.name, r.iter, r.source_type, r.status, r.submitted_by, r.created_at, r.synced_at, r.risk_tier,"
            " (SELECT COUNT(*) FROM clarify_item c WHERE c.req_id=r.id) AS total_q,"
            " (SELECT COUNT(*) FROM clarify_item c WHERE c.req_id=r.id AND c.status IN ('answered','rejected','deferred')) AS answered_q,"
            " (SELECT COUNT(*) FROM test_case tc WHERE tc.req_id=r.id) AS case_count"
            " FROM requirement r WHERE r.deleted_at=''"
        )
        args: list[object] = []
        if iter_name:
            sql += " AND r.iter=?"
            args.append(iter_name)
        if status:
            sql += " AND r.status=?"
            args.append(status)
        sql += " ORDER BY r.created_at DESC, r.id DESC"
        out: list[RequirementListRow] = []
        for row in self.db.query_all(sql, tuple(args)):
            out.append(
                RequirementListRow(
                    id=str(row["id"]),
                    name=str(row["name"]),
                    iter=str(row["iter"]),
                    source_type=str(row["source_type"]),
                    status=str(row["status"]),
                    submitted_by=str(row["submitted_by"]),
                    created_at=str(row["created_at"]),
                    synced_at=str(row["synced_at"]),
                    risk_tier="" if row["risk_tier"] is None else str(row["risk_tier"]),
                    total_q=int(row["total_q"]),
                    answered_q=int(row["answered_q"]),
                    case_count=int(row["case_count"]),
                )
            )
        return out

    def set_status(self, req_id: str, status: str) -> None:
        self.db.execute("UPDATE requirement SET status=? WHERE id=?", (status, req_id))

    def soft_delete(self, req_id: str, at: str) -> int:
        """软删除打标（评测 P0-3）：返回 0 表示已删（幂等防线），不物理删行。"""
        return self.db.execute("UPDATE requirement SET deleted_at=? WHERE id=? AND deleted_at=''", (at, req_id))

    def touch_synced(self, req_id: str, at: str) -> None:
        self.db.execute("UPDATE requirement SET synced_at=? WHERE id=?", (at, req_id))

    def set_standard(self, req_id: str, md: str, at: str) -> None:
        self.db.execute("UPDATE requirement SET standard_doc=?, status='published', synced_at=? WHERE id=?", (md, at, req_id))

    def get_domains(self, req_id: str) -> str:
        return self._column_of(req_id, "domains")

    def set_domains(self, req_id: str, domains_json: str) -> None:
        self.db.execute("UPDATE requirement SET domains=? WHERE id=?", (domains_json, req_id))

    def set_risk_tier(self, req_id: str, tier: str) -> None:
        self.db.execute("UPDATE requirement SET risk_tier=? WHERE id=?", (tier, req_id))

    # v2.4 设计域专用读写（不进实体，避免 card 序列化漂移）

    def get_target_repo(self, req_id: str) -> str:
        return self._column_of(req_id, "target_repo")

    def set_target_repo(self, req_id: str, repo: str) -> None:
        self.db.execute("UPDATE requirement SET target_repo=? WHERE id=?", (repo, req_id))

    def get_target_repos(self, req_id: str) -> str:
        return self._column_of(req_id, "target_repos")

    def set_target_repos(self, req_id: str, json_str: str) -> None:
        self.db.execute("UPDATE requirement SET target_repos=? WHERE id=?", (json_str, req_id))

    def get_design_final(self, req_id: str) -> str:
        return self._column_of(req_id, "design_final_json")

    def set_design_final(self, req_id: str, json_str: str) -> None:
        self.db.execute("UPDATE requirement SET design_final_json=? WHERE id=?", (json_str, req_id))

    def _column_of(self, req_id: str, column: str) -> str:
        v = self.db.query_scalar(f"SELECT {column} FROM requirement WHERE id=?", (req_id,))
        return "" if v is None else str(v)
