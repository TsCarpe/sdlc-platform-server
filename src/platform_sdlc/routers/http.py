"""路由层公共助手：宽松 JSON 读取（复刻 Java「@RequestBody 实体绑定 + 服务层校验」通道）。"""

from __future__ import annotations

import json
from typing import Any

from fastapi import Request

from platform_sdlc.common import ApiException


async def read_json(request: Request, required: bool = False) -> dict[str, Any]:
    """读取 JSON 请求体为 dict（缺体 → {}）。

    - 非法 JSON / 顶层非对象 → 400「请求体不是合法 JSON 或字段类型不符」（对应 HttpMessageNotReadable）；
    - Content-Type 非 JSON 且有内容 → 415（对应 HttpMediaTypeNotSupportedException）；
    - required=True 且无内容 → 400（对应 @RequestBody 必填体缺失）。
    """
    body = await request.body()
    if not body:
        if required:
            raise ApiException(400, "请求体不是合法 JSON 或字段类型不符")
        return {}
    ct = request.headers.get("content-type", "application/json")
    if not ct.split(";")[0].strip().lower() == "application/json":
        raise ApiException(415, f"请求体类型不支持：{ct}")
    try:
        v = json.loads(body)
    except Exception as e:
        raise ApiException(400, "请求体不是合法 JSON 或字段类型不符") from e
    if not isinstance(v, dict):
        raise ApiException(400, "请求体不是合法 JSON 或字段类型不符")
    return v
