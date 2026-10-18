"""出参序列化（对应 Jackson 对 Java 实体的 camelCase 序列化）。

- `wire(entity)`：实体 → camelCase dict，键序 = Java 字段声明序（既有契约）；
  null 键照常输出（Jackson 默认），与 Java NON_NULL 子集 DTO 的差异由服务层组装视图时表达。
- 服务层组装的复杂视图直接以 camelCase dict 返回（对应 Java 的 XxxView DTO 组装），键序在组装处对齐。
- 入参不建 Pydantic 模型：路由直接收解析后的 JSON 值（dict），校验在服务层逐字移植
  Json.required 文案——复刻 Java「宽松绑定 + 服务层校验」通道（评审 P0-2）。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

from platform_sdlc.models import (
    Artifact,
    CaseComment,
    ClarifyItem,
    DraftArtifact,
    FrPoint,
    GateIssue,
    GateRound,
    Iteration,
    MediaFile,
    Requirement,
    Task,
    TestCase,
)

# 键序 = Java 实体字段声明序（既有契约，勿重排）
_WIRE: dict[type[Any], tuple[str, ...]] = {
    Requirement: ("id", "name", "iter", "source_type", "source_url", "source_text", "status",
                  "submitted_by", "standard_doc", "created_at", "synced_at", "risk_tier",
                  "content_rev"),  # content_rev=v4.12 契约增键（§7 展示计数）
    ClarifyItem: ("id", "req_id", "seq", "level", "theme", "question", "ctx", "answer",
                  "answered_at", "quote", "anchor", "status", "note", "origin", "src_comment_id",
                  "options", "module", "layer", "answer_text"),
    # origin=v4.9 契约增键；src_comment_id=v4.8 升级回链（交接 §6「answers 已带」——detail/answers 面补齐）；
    # options/module/layer/answer_text=v1.6 契约增键（DESIGN-v5 §6.1/§6.3——选择题化作答 + D15 单一
    # 解构出口，options/answer_text 是 @property，wire getattr 遍历自动带出）
    Artifact: ("req_id", "type", "markdown", "content_json", "version", "pushed_at"),
    FrPoint: ("req_id", "id", "name", "source", "group", "module", "anchor"),
    MediaFile: ("req_id", "media_id", "sha256", "name", "mime", "bytes", "created_at"),
    TestCase: ("req_id", "id", "title", "points", "priority", "pre", "steps", "expect", "owner", "tool",
               "status", "version", "created_at", "updated_at", "verify_refs", "defect_refs", "case_no"),
    CaseComment: ("id", "req_id", "case_id", "by", "text", "created_at"),
    # attempt=P1-6 契约增键（retry 递增的执行代数；runner 回传 done/failed 时原样带回作 fencing）
    Task: ("id", "req_id", "type", "status", "payload", "result", "runner", "attempt",
           "created_by", "created_at", "updated_at"),
    DraftArtifact: ("req_id", "type", "md", "content_json", "task_id", "updated_at"),
    Iteration: ("name", "note", "created_by", "created_at"),
    GateRound: ("id", "req_id", "round_no", "status", "checks_json", "baseline_json",
                "released_at", "released_by", "created_by", "opened_at"),
    GateIssue: ("id", "round_id", "code", "sev", "theme", "role", "scene", "quote", "loc", "basis",
                "div_flag", "tradeoff", "merged_unit", "filed", "verdict", "verdict_note",
                "verdict_by", "verdict_at", "rev_location", "rev_status"),
}


def to_camel(snake: str) -> str:
    head, *rest = snake.split("_")
    return head + "".join(w.capitalize() for w in rest)


# ---------- 出参模型（批次5 A 轨）：字段声明序 = 既有契约键序 ----------
# 仅用于纯 wire() 实体端点挂 response_model（键序由字段声明序保证 + snapshot-keys.mjs 守门）；
# NON_NULL 条件键视图端点（requirement detail 等）不挂——见 docs/AUDIT-2026-10.md D4-4 与方案批次5。


class _CamelModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class TaskOut(_CamelModel):
    id: int
    req_id: str
    type: str
    status: str
    payload: str
    result: str
    runner: str
    attempt: int
    created_by: str
    created_at: str | None = None
    updated_at: str | None = None


class DraftOut(_CamelModel):
    req_id: str
    type: str
    md: str
    content_json: str
    task_id: int | None = None
    updated_at: str | None = None


def wire(entity: Any) -> dict[str, Any]:
    keys = _WIRE[type(entity)]
    return {to_camel(k): getattr(entity, k) for k in keys}


def wire_list(entities: list[Any]) -> list[dict[str, Any]]:
    return [wire(e) for e in entities]
