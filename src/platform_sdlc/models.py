"""表实体（对应 Java domain.model 实体 + 仓储 RowMapper）。

属性 snake_case（Python 惯例）；HTTP 出入参的 camelCase 由 schemas.py 在边界转换。
各 from_row 保持 Java MAP 的 null 归一口径（如 risk_tier/status/note null → ""）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

Row = Any  # sqlite3.Row


def _s(row: Row, col: str, default: str = "") -> str:
    v = row[col]
    return default if v is None else str(v)


def _sn(row: Row, col: str) -> str | None:
    v = row[col]
    return None if v is None else str(v)


def _i(row: Row, col: str) -> int:
    return int(row[col])


def _in(row: Row, col: str) -> int | None:
    return None if row[col] is None else int(row[col])


def string_list(json_str: str | None) -> list[str]:
    """JSON 数组列行内反序列化；坏值回退空数组（对应 Java stringList 降级）。"""
    try:
        v = json.loads(json_str or "[]")
        return [str(x) for x in v] if isinstance(v, list) else []
    except Exception:
        return []


def parse_json_obj(json_str: str) -> dict[str, Any]:
    """JSON 对象列行内反序列化；坏值/非对象降级空 dict（design/gate 域口径：字段缺失按
    未声明处理，不阻塞读面——AUDIT D2-5 统一收口）。requirement._artifact_view 的
    「content 键缺失」语义更严（suppress 不附加键），保留原地不强行统一。"""
    try:
        v = json.loads(json_str.strip() or "{}")
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


# ---------- 需求域 ----------


@dataclass
class Requirement:
    id: str
    name: str
    iter: str = ""
    source_type: str = ""
    source_url: str = ""
    source_text: str = ""
    status: str = ""
    submitted_by: str = ""
    standard_doc: str = ""
    created_at: str | None = None
    synced_at: str | None = None
    risk_tier: str = ""

    @classmethod
    def from_row(cls, row: Row) -> Requirement:
        return cls(
            id=_s(row, "id"),
            name=_s(row, "name"),
            iter=_s(row, "iter"),
            source_type=_s(row, "source_type"),
            source_url=_s(row, "source_url"),
            source_text=_s(row, "source_text"),
            status=_s(row, "status"),
            submitted_by=_s(row, "submitted_by"),
            standard_doc=_s(row, "standard_doc"),
            created_at=_sn(row, "created_at"),
            synced_at=_sn(row, "synced_at"),
            risk_tier=_s(row, "risk_tier"),
        )


@dataclass
class RequirementListRow:
    id: str
    name: str
    iter: str
    source_type: str
    status: str
    submitted_by: str
    created_at: str
    synced_at: str | None  # 未推送过为 None（出参 null，与详情侧 _sn 口径一致）
    risk_tier: str
    total_q: int
    answered_q: int
    case_count: int


@dataclass
class ClarifyItem:
    id: int
    req_id: str
    seq: str
    level: str
    theme: str = ""
    question: str = ""
    ctx: str = ""
    answer: str = ""
    answered_at: str | None = None
    quote: str = ""
    anchor: str = ""
    status: str = ""
    note: str = ""

    @classmethod
    def from_row(cls, row: Row) -> ClarifyItem:
        return cls(
            id=_i(row, "id"),
            req_id=_s(row, "req_id"),
            seq=_s(row, "seq"),
            level=_s(row, "level"),
            theme=_s(row, "theme"),
            question=_s(row, "question"),
            ctx=_s(row, "ctx"),
            answer=_s(row, "answer"),
            answered_at=_sn(row, "answered_at"),
            quote=_s(row, "quote"),
            anchor=_s(row, "anchor"),
            status=_s(row, "status"),
            note=_s(row, "note"),
        )


@dataclass
class Artifact:
    req_id: str
    type: str
    markdown: str = ""
    content_json: str = ""
    version: int = 1
    pushed_at: str | None = None

    @classmethod
    def from_row(cls, row: Row) -> Artifact:
        return cls(
            req_id=_s(row, "req_id"),
            type=_s(row, "type"),
            markdown=_s(row, "markdown"),
            content_json=_s(row, "content_json"),
            version=_i(row, "version"),
            pushed_at=_sn(row, "pushed_at"),
        )


@dataclass
class FrPoint:
    req_id: str
    id: str
    name: str = ""
    source: str = ""
    group: str = ""
    module: str = ""
    anchor: str = ""


@dataclass
class MediaFile:
    req_id: str
    media_id: str
    sha256: str = ""
    name: str = ""
    mime: str = ""
    bytes: int = 0
    created_at: str | None = None

    @classmethod
    def from_row(cls, row: Row) -> MediaFile:
        return cls(
            req_id=_s(row, "req_id"),
            media_id=_s(row, "media_id"),
            sha256=_s(row, "sha256"),
            name=_s(row, "name"),
            mime=_s(row, "mime"),
            bytes=_i(row, "bytes"),
            created_at=_sn(row, "created_at"),
        )


# ---------- 用例域 ----------


@dataclass
class TestCase:
    req_id: str
    id: str
    title: str
    points: str = "[]"
    priority: str = "P2"
    pre: str = ""
    steps: str = ""
    expect: str = ""
    owner: str = ""
    tool: str = "表单"
    status: str = "draft"
    version: int = 1
    created_at: str | None = None
    updated_at: str | None = None
    verify_refs: str = "[]"
    defect_refs: str = "[]"
    case_no: int | None = None

    @classmethod
    def from_row(cls, row: Row) -> TestCase:
        return cls(
            req_id=_s(row, "req_id"),
            id=_s(row, "id"),
            title=_s(row, "title"),
            points=_s(row, "points"),
            priority=_s(row, "priority"),
            pre=_s(row, "pre"),
            steps=_s(row, "steps"),
            expect=_s(row, "expect"),
            owner=_s(row, "owner"),
            tool=_s(row, "tool"),
            status=_s(row, "status"),
            version=_i(row, "version"),
            created_at=_sn(row, "created_at"),
            updated_at=_sn(row, "updated_at"),
            verify_refs=_s(row, "verify_refs", "[]"),
            defect_refs=_s(row, "defect_refs", "[]"),
            case_no=_in(row, "case_no"),
        )

    def verify_ref_list(self) -> list[str]:
        return string_list(self.verify_refs)

    def defect_ref_list(self) -> list[str]:
        return string_list(self.defect_refs)


@dataclass
class CaseListRow:
    req_id: str
    id: str
    title: str
    points: str
    priority: str
    status: str
    owner: str
    tool: str
    version: int
    updated_at: str
    verify_refs: list[str] = field(default_factory=list)
    defect_refs: list[str] = field(default_factory=list)
    case_no: int | None = None
    iter: str = ""
    req_name: str = ""


@dataclass
class CaseComment:
    id: int
    req_id: str
    case_id: str
    by: str = ""
    text: str = ""
    created_at: str | None = None

    @classmethod
    def from_row(cls, row: Row) -> CaseComment:
        return cls(
            id=_i(row, "id"),
            req_id=_s(row, "req_id"),
            case_id=_s(row, "case_id"),
            by=_s(row, "by"),
            text=_s(row, "text"),
            created_at=_sn(row, "created_at"),
        )


# ---------- 任务域 ----------


@dataclass
class Task:
    id: int
    req_id: str
    type: str  # quality_check | standard_doc | case_gen | design_gen | design_rev | gate_review
    status: str = "queued"
    payload: str = ""
    result: str = ""
    runner: str = ""
    created_by: str = ""
    created_at: str | None = None
    updated_at: str | None = None

    @classmethod
    def from_row(cls, row: Row) -> Task:
        return cls(
            id=_i(row, "id"),
            req_id=_s(row, "req_id"),
            type=_s(row, "type"),
            status=_s(row, "status"),
            payload=_s(row, "payload"),
            result=_s(row, "result"),
            runner=_s(row, "runner"),
            created_by=_s(row, "created_by"),
            created_at=_sn(row, "created_at"),
            updated_at=_sn(row, "updated_at"),
        )


@dataclass
class DraftArtifact:
    req_id: str
    type: str
    md: str = ""
    content_json: str = ""
    task_id: int | None = None
    updated_at: str | None = None

    @classmethod
    def from_row(cls, row: Row) -> DraftArtifact:
        return cls(
            req_id=_s(row, "req_id"),
            type=_s(row, "type"),
            md=_s(row, "md"),
            content_json=_s(row, "content_json"),
            task_id=_in(row, "task_id"),
            updated_at=_sn(row, "updated_at"),
        )


# ---------- 迭代域 ----------


@dataclass
class Iteration:
    name: str
    note: str = ""
    created_by: str = ""
    created_at: str | None = None

    @classmethod
    def from_row(cls, row: Row) -> Iteration:
        return cls(name=_s(row, "name"), note=_s(row, "note"), created_by=_s(row, "created_by"), created_at=_sn(row, "created_at"))


@dataclass
class IterationListRow:
    name: str
    note: str
    created_by: str
    created_at: str
    req_id: str | None
    req_name: str | None
    req_status: str | None
    risk_tier: str
    submitted_by: str
    total_q: int
    answered_q: int
    case_count: int
    design_phase: str
    gate_stage: str


# ---------- 门禁域 ----------


@dataclass
class GateRound:
    id: int | None
    req_id: str
    round_no: int
    status: str
    checks_json: str = ""
    baseline_json: str = ""
    released_at: str | None = None
    released_by: str = ""
    created_by: str = ""
    opened_at: str | None = None

    @classmethod
    def from_row(cls, row: Row) -> GateRound:
        return cls(
            id=_in(row, "id"),
            req_id=_s(row, "req_id"),
            round_no=_i(row, "round_no"),
            status=_s(row, "status"),
            checks_json=_s(row, "checks_json"),
            baseline_json=_s(row, "baseline_json"),
            released_at=_sn(row, "released_at"),
            released_by=_s(row, "released_by"),
            created_by=_s(row, "created_by"),
            opened_at=_sn(row, "opened_at"),
        )


@dataclass
class GateIssue:
    id: int | None
    round_id: int
    code: str
    sev: str = ""
    theme: str = ""
    role: str = ""
    scene: str = ""
    quote: str = ""
    loc: str = ""
    basis: str = ""
    div_flag: int = 0
    tradeoff: int = 0
    merged_unit: int = 0
    filed: int = 0
    verdict: str = ""
    verdict_note: str = ""
    verdict_by: str = ""
    verdict_at: str | None = None
    rev_location: str = ""
    rev_status: str = ""

    @classmethod
    def from_row(cls, row: Row) -> GateIssue:
        return cls(
            id=_in(row, "id"),
            round_id=_i(row, "round_id"),
            code=_s(row, "code"),
            sev=_s(row, "sev"),
            theme=_s(row, "theme"),
            role=_s(row, "role"),
            scene=_s(row, "scene"),
            quote=_s(row, "quote"),
            loc=_s(row, "loc"),
            basis=_s(row, "basis"),
            div_flag=_i(row, "div_flag"),
            tradeoff=_i(row, "tradeoff"),
            merged_unit=_i(row, "merged_unit"),
            filed=_i(row, "filed"),
            verdict=_s(row, "verdict"),
            verdict_note=_s(row, "verdict_note"),
            verdict_by=_s(row, "verdict_by"),
            verdict_at=_sn(row, "verdict_at"),
            rev_location=_s(row, "rev_location"),
            rev_status=_s(row, "rev_status"),
        )
