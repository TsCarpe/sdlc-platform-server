"""公共工具（对应 Java common 模块）：ApiException / Times / Json 取值助手。

Json 助手操作「已解析的 JSON 值」（dict/list/str/int/float/bool/None），
取值语义与 Jackson JsonNode 对齐（缺字段给默认值、必填缺失抛 400、asText 口径）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel


class ApiException(Exception):
    """业务异常：status 为 HTTP 状态码（400/404/409），由全局异常处理器转 {error} 响应体。"""

    def __init__(self, status: int, msg: str) -> None:
        super().__init__(msg)
        self.status = status


class ConflictException(ApiException):
    """乐观锁冲突（对应 CaseService.ConflictException）：409 + conflicts 明细列表。"""

    def __init__(self, error: str, conflicts: list[dict[str, Any]]) -> None:
        super().__init__(409, error)
        self.conflicts = conflicts


class ErrorResult(BaseModel):
    """统一错误响应体 {error}（键名 = 既有契约）。"""

    error: str


class OkResult(BaseModel):
    """通用操作回执 {ok:true}。"""

    ok: bool = True


class Times:
    FMT = "%Y-%m-%d %H:%M:%S"  # 库内时间戳统一本地时间、字典序可排序（与 Java Times 一致）

    @staticmethod
    def now() -> str:
        return datetime.now().strftime(Times.FMT)


# ---------- JsonNode 取值助手（文案与 Java Json.java 逐字一致） ----------


def _as_text(v: Any) -> str:
    """Jackson asText 口径：null→""、bool→true/false、数值→字面量、容器→""。"""
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, str):
        return v
    if isinstance(v, (int, float)):
        return str(v)
    return ""


def jtext(n: Any, field: str, default: str) -> str:
    v = n.get(field) if isinstance(n, dict) else None
    return default if v is None else _as_text(v)


def jrequired(n: Any, field: str) -> str:
    v = jtext(n, field, "")
    if not v.strip():
        raise ApiException(400, f"缺少必填字段：{field}")
    return v.strip()


def jrequired_non_blank(n: Any, field: str) -> str:
    """区分缺字段与空白值（评测 P1-4）：缺失报「缺少必填字段」，存在但空白报「不能为空」。"""
    v = n.get(field) if isinstance(n, dict) else None
    if v is None:
        raise ApiException(400, f"缺少必填字段：{field}")
    s = _as_text(v).strip()
    if not s:
        raise ApiException(400, f"{field} 不能为空（收到空白内容）")
    return s


def jint(n: Any, field: str, default: int) -> int:
    v = n.get(field) if isinstance(n, dict) else None
    if isinstance(v, bool) or v is None:
        return default
    if isinstance(v, int):
        return v
    if isinstance(v, float) and v.is_integer():
        return int(v)
    return default


def jbool(n: Any, field: str, default: bool) -> bool:
    v = n.get(field) if isinstance(n, dict) else None
    if isinstance(v, bool):
        return v
    if v == "true":
        return True
    if v == "false":
        return False
    return default


def jarray(n: Any, field: str) -> list[Any]:
    v = n.get(field) if isinstance(n, dict) else None
    return list(v) if isinstance(v, list) else []


def jstrings(n: Any, field: str) -> list[str]:
    return [_as_text(v) for v in jarray(n, field)]
