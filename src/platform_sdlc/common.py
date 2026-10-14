"""公共工具（对应 Java common 模块）：ApiException / Times / Json 取值助手。

Json 助手操作「已解析的 JSON 值」（dict/list/str/int/float/bool/None），
取值语义与 Jackson JsonNode 对齐（缺字段给默认值、必填缺失抛 400、asText 口径）。
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, TypeGuard, overload

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


class IntegrityException(ApiException):
    """产物完整性终检失败（v4 守恒门第二道）：固定 409，走全局 ApiException 处理器转 {error}。"""

    def __init__(self, msg: str) -> None:
        super().__init__(409, msg)


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


def _jget(n: Any, field: str) -> Any:
    """dict 成员安全取值（非 dict 一律 None）。strict 下裸 dict 窄化产生的 Unknown 集中压制在此单点。"""
    if is_json_obj(n):
        return n.get(field)
    return None


def is_json_obj(v: Any) -> TypeGuard[dict[str, Any]]:
    """JSON 对象判别：strict 下裸 isinstance(v, dict) 只能窄化到 dict[Unknown, Unknown]，
    TypeGuard 显式收口到 dict[str, Any]，后续 .get() 链即为 Any（可传宽松绑定助手）。"""
    return isinstance(v, dict)


def is_json_list(v: Any) -> TypeGuard[list[Any]]:
    """JSON 数组判别（同 is_json_obj）：Any 窄化收口 list[Any]，元素 Any 可直接进宽松绑定/推导。"""
    return isinstance(v, list)


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
    v = _jget(n, field)
    return default if v is None else _as_text(v)


def jrequired(n: Any, field: str) -> str:
    v = jtext(n, field, "")
    if not v.strip():
        raise ApiException(400, f"缺少必填字段：{field}")
    return v.strip()


def jrequired_non_blank(n: Any, field: str) -> str:
    """区分缺字段与空白值（评测 P1-4）：缺失报「缺少必填字段」，存在但空白报「不能为空」。"""
    v = _jget(n, field)
    if v is None:
        raise ApiException(400, f"缺少必填字段：{field}")
    s = _as_text(v).strip()
    if not s:
        raise ApiException(400, f"{field} 不能为空（收到空白内容）")
    return s


def jint(n: Any, field: str, default: int) -> int:
    v = _jget(n, field)
    if isinstance(v, bool) or v is None:
        return default
    if isinstance(v, int):
        return v
    if isinstance(v, float) and v.is_integer():
        return int(v)
    return default


@overload
def jint_or_400(n: Any, field: str, default: int) -> int: ...
@overload
def jint_or_400(n: Any, field: str, default: int | None = None) -> int | None: ...
def jint_or_400(n: Any, field: str, default: int | None = None) -> int | None:
    """宽松整型绑定（入参面）：int / 整值 float / 纯数字串取整；存在但不可取整 → 400（对齐
    Java HttpMessageNotReadable 的绑定失败口径，替代服务层裸 int() 的 500）。缺失/None 给 default。"""
    v = _jget(n, field)
    if v is None:
        return default
    if isinstance(v, bool):
        raise ApiException(400, f"非法 {field}：必须是整数（收到：{_as_text(v)}）")
    if isinstance(v, int):
        return v
    if isinstance(v, float) and v.is_integer():
        return int(v)
    if isinstance(v, str) and re.fullmatch(r"[+-]?\d+", v.strip()):
        return int(v.strip())
    raise ApiException(400, f"非法 {field}：必须是整数（收到：{_as_text(v)}）")


def jbool(n: Any, field: str, default: bool) -> bool:
    v = _jget(n, field)
    if isinstance(v, bool):
        return v
    if v == "true":
        return True
    if v == "false":
        return False
    return default


def jarray(n: Any, field: str) -> list[Any]:
    v = _jget(n, field)
    return list(v) if is_json_list(v) else []


def jstrings(n: Any, field: str) -> list[str]:
    return [_as_text(v) for v in jarray(n, field)]
