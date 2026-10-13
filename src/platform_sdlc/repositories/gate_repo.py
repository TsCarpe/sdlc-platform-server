"""门禁仓储（对应 GateRepoImpl）。"""

from __future__ import annotations

from platform_sdlc.db import Database
from platform_sdlc.models import GateIssue, GateRound


class GateRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    def insert_round(self, r: GateRound) -> int:
        return self.db.insert(
            "INSERT INTO gate_round(req_id,round_no,status,checks_json,baseline_json,created_by,opened_at) VALUES(?,?,?,?,?,?,?)",
            (r.req_id, r.round_no, r.status, r.checks_json, r.baseline_json, r.created_by, r.opened_at),
        )

    def find_round(self, round_id: int) -> GateRound | None:
        row = self.db.query_one("SELECT * FROM gate_round WHERE id=?", (round_id,))
        return GateRound.from_row(row) if row else None

    def list_rounds(self, req_id: str) -> list[GateRound]:
        return [
            GateRound.from_row(r) for r in self.db.query_all("SELECT * FROM gate_round WHERE req_id=? ORDER BY round_no ASC", (req_id,))
        ]

    def set_released(self, round_id: int, by: str, at: str) -> int:
        return self.db.execute(
            "UPDATE gate_round SET status='released', released_at=?, released_by=? WHERE id=? AND status='pending'",
            (at, by, round_id),
        )

    def insert_issues(self, issues: list[GateIssue]) -> None:
        for g in issues:
            self.db.execute(
                "INSERT INTO gate_issue(round_id,code,sev,theme,role,scene,quote,loc,basis,div_flag,tradeoff,"
                "merged_unit,filed) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    g.round_id,
                    g.code,
                    g.sev,
                    g.theme,
                    g.role,
                    g.scene,
                    g.quote,
                    g.loc,
                    g.basis,
                    g.div_flag,
                    g.tradeoff,
                    g.merged_unit,
                    g.filed,
                ),
            )

    def list_issues(self, round_id: int) -> list[GateIssue]:
        return [
            GateIssue.from_row(r) for r in self.db.query_all("SELECT * FROM gate_issue WHERE round_id=? ORDER BY code ASC", (round_id,))
        ]

    def find_issue(self, issue_id: int) -> GateIssue | None:
        row = self.db.query_one("SELECT * FROM gate_issue WHERE id=?", (issue_id,))
        return GateIssue.from_row(row) if row else None

    def apply_verdict(self, round_id: int, merged_unit: int, verdict: str, note: str, by: str, at: str) -> int:
        """ingest 保证每条 issue 均归属唯一裁决单元（merged_unit>0），同单元批量同裁（人裁决的是单元）。"""
        return self.db.execute(
            "UPDATE gate_issue SET verdict=?, verdict_note=?, verdict_by=?, verdict_at=? WHERE round_id=? AND merged_unit=?",
            (verdict, note, by, at, round_id, merged_unit),
        )

    def apply_revision(self, round_id: int, merged_unit: int, rev_location: str, rev_status: str) -> int:
        return self.db.execute(
            "UPDATE gate_issue SET rev_location=CASE WHEN ?='' THEN rev_location ELSE ? END, rev_status=? "
            "WHERE round_id=? AND merged_unit=?",
            (rev_location, rev_location, rev_status, round_id, merged_unit),
        )
