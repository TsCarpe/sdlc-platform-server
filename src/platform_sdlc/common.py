"""公共工具（对应 Java common 模块）：ApiException / Times / Json 取值助手。

Json 助手操作「已解析的 JSON 值」（dict/list/str/int/float/bool/None），
取值语义与 Jackson JsonNode 对齐（缺字段给默认值、必填缺失抛 400、asText 口径）。
"""

from __future__ import annotations

import json
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


# ---------- v1.6（DESIGN-v5 §6.2/§6.3）：layer 归一 / answer 解构 / 澄清视图共享构造器 ----------

def normalize_layer(v: Any) -> str:
    """layer 归一单点（D-J，三用：push_artifacts 解析 / _machine_clarify / audit_item_view）：
    先 str 化再判（数字 2 → "2" 是 v1.6 推送方主线形态）；白名单外尝试浮点整值归一（review #24：
    2.0/"2.0" → "2"，转换结果必须再过一次白名单——否则 9.0 → "9" 漏越界拦截），非整值/NaN/
    不可解析一律 ''（'0'/越界/杂质归空——出参端前端对空串已有降级，入库端不点亮伪桶）。
    DB 存字符串、wire 出字符串。"""
    ok = ("1", "2", "3", "4", "5", "6", "7")
    s = str(v if v is not None else "").strip()
    if s in ok:
        return s
    try:
        f = float(s)
        s = str(int(f)) if f.is_integer() else ""
    except ValueError:
        return ""
    return s if s in ok else ""


def answer_text_of(answer: str) -> str:
    """answer → 展示文本单一解构出口（D15/D-E，前端展示/重开 note/合成输入/pull-answers 回写
    统一消费）：入口先收口 answer 基准（None/纯空白 → ""，review #13），四条件判别——
    json.loads 成功 ∧ 含 kind 键 ∧ kind ∈ {option,text} ∧ 分支负载非空白（option: optionText /
    text: text，取值 .strip() 后判空——空白负载残缺透传，review #12），全过才算结构化：
    option → optionText[＋（补充：extra）]、text → text；任一不过一律原文透传（残缺结构化
    不当空串产出——下游会当未答漏固化，也不在展示层外溢 JSON）。kind:text 当前零生产方，
    保留契约分支（宽进合法、未来写方预留，D-F）。"""
    answer = (answer or "").strip()
    if not answer.startswith("{"):
        return answer
    try:
        obj: Any = json.loads(answer)
    except ValueError:
        return answer
    if not is_json_obj(obj) or "kind" not in obj:
        return answer
    kind = str(obj.get("kind") or "")
    if kind == "option":
        text = str(obj.get("optionText") or "").strip()
        if not text:
            return answer
        extra = str(obj.get("extra") or "").strip()
        return f"{text}（补充：{extra}）" if extra else text
    if kind == "text":
        text = str(obj.get("text") or "").strip()
        return text if text else answer
    return answer


# 澄清选项行视图键集（review #11 四键白名单：写侧 push 全量 / 写侧增量合并 / 读侧 options
# property 三侧同构——裸 dict 透传会静默收纳未知键漂移，与 AUDIT_ITEM_FIELDS 同一病症）
OPTION_FIELDS: tuple[str, ...] = ("key", "text", "why", "recommended")


def option_view(o: Any) -> dict[str, Any] | None:
    """选项行视图共享构造器（四键白名单，字段集与 OPTION_FIELDS 同序）：非 json obj 返 None
    （丢弃——与 audit_item_view 的宽进哲学差异：选项是数组元素，坏元素无键可补）；obj 一律
    保留、jtext 补 ""/jbool 补 False（空 key 元素前端匹配不上无副作用）。text 取值 .strip()——
    使存储/answerText（strip 后解构）/前端展示三侧同构，写侧不 strip 入库的选项文本会在
    answerText 与前端展示间闪变（review C2）。"""
    if not is_json_obj(o):
        return None
    return {
        "key": jtext(o, "key", ""), "text": jtext(o, "text", "").strip(),
        "why": jtext(o, "why", ""), "recommended": jbool(o, "recommended", False),
    }


# audit 条目视图键集（现行 8 键 + layer；push 侧 _audit_view 与增量合并共用同一构造器——
# 两套口径会漂移出隐式字段保留，DESIGN-v5 §6.4 B6）
AUDIT_ITEM_FIELDS: tuple[str, ...] = (
    "id", "level", "title", "where", "go", "group", "quote", "anchor", "layer")


def audit_item_view(i: Any) -> dict[str, Any]:
    """audit 条目视图共享构造器（9 键，字段集与 AUDIT_ITEM_FIELDS 同序）：layer 走 normalize_layer
    归一（audit items 与澄清卡同一 1-7 体系 §6.2，不归一会点亮就绪度伪桶）+ 未知键剥除（白名单）。"""
    return {
        "id": jtext(i, "id", ""), "level": jtext(i, "level", "yellow"), "title": jtext(i, "title", ""),
        "where": jtext(i, "where", ""), "go": jtext(i, "go", ""), "group": jtext(i, "group", ""),
        "quote": jtext(i, "quote", ""), "anchor": jtext(i, "anchor", ""),
        "layer": normalize_layer(i.get("layer") if is_json_obj(i) else ""),
    }


# flows 行视图键集（现行 6 键 + pids；与 payload-schema.md flows 契约/incremental-guide 字段集核对）
DIGEST_FLOW_FIELDS: tuple[str, ...] = ("role", "step", "next", "gap", "src", "anchor", "pids")


def digest_flow_view(f: Any) -> dict[str, Any]:
    """flows 行视图共享构造器（push 侧 _digest_view 与增量合并 new 行两路同构）：pids 走 jstrings
    一律数组形态（D-K——字符串形态会以非空串绕过前端 (f.pids||[]).length 降级判定）。"""
    return {
        "role": jtext(f, "role", ""), "step": jtext(f, "step", ""), "next": jtext(f, "next", ""),
        "gap": jbool(f, "gap", False), "src": jtext(f, "src", ""), "anchor": jtext(f, "anchor", ""),
        "pids": jstrings(f, "pids"),
    }


# features/states 行视图键集（三期 D-I：v1.6 契约 incremental_bundle 节行结构——push 侧与增量合并
# 两侧同构，裸 dict 透传会静默收纳未知键漂移；与 payload-schema 契约字段集核对）
DIGEST_FEATURE_FIELDS: tuple[str, ...] = ("role", "m", "func", "cov", "src", "anchor")
DIGEST_STATE_FIELDS: tuple[str, ...] = ("obj", "from", "on", "to", "gap", "src", "anchor")


def digest_feature_view(f: Any) -> dict[str, Any]:
    """features 行视图共享构造器（D-I）：白名单 6 键 + 未知键剥除。"""
    return {
        "role": jtext(f, "role", ""), "m": jtext(f, "m", ""), "func": jtext(f, "func", ""),
        "cov": jtext(f, "cov", ""), "src": jtext(f, "src", ""), "anchor": jtext(f, "anchor", ""),
    }


def digest_state_view(s: Any) -> dict[str, Any]:
    """states 行视图共享构造器（D-I）：白名单 7 键 + 未知键剥除。"""
    return {
        "obj": jtext(s, "obj", ""), "from": jtext(s, "from", ""), "on": jtext(s, "on", ""),
        "to": jtext(s, "to", ""), "gap": jbool(s, "gap", False), "src": jtext(s, "src", ""),
        "anchor": jtext(s, "anchor", ""),
    }
