"""飞书 URL/token 解析与 wiki 节点解析（DESIGN-飞书接入Python化 §8 wiki.py 职责）。

输入面是需求卡 sourceUrl（用户粘贴的任意形态：wiki/docx/docs URL 或裸 token），
输出面是 fetch 编排可用的 docx document_id。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from platform_sdlc.integrations.feishu.client import FeishuClient, FeishuError

_URL_TOKEN = re.compile(r"/(docx|docs|wiki)/([A-Za-z0-9]+)")


@dataclass(frozen=True)
class DocRef:
    """解析结果：docx token 必有；wiki 形态时附原始 wiki token 与节点标题。"""

    document_id: str
    wiki_token: str = ""
    title: str = ""


def parse_ref(raw: str) -> tuple[str, str]:
    """URL 或裸 token → (形态 wiki|docx, token)。/docs/ 视为 docx（老短链同样指向 docx）。"""
    m = _URL_TOKEN.search(raw)
    if m:
        kind = "wiki" if m.group(1) == "wiki" else "docx"
        return kind, m.group(2)
    return "docx", raw.strip()


def resolve_document(client: FeishuClient, raw_url_or_token: str) -> DocRef:
    """URL/token → DocRef。wiki 需二次解析且要求 obj_type=docx（bitable/sheet 拒绝）。"""
    kind, token = parse_ref(raw_url_or_token)
    if kind != "wiki":
        return DocRef(document_id=token)
    node: dict[str, Any] = client.resolve_wiki(token)
    obj_type = str(node.get("obj_type") or "")
    if obj_type != "docx":
        raise FeishuError("wiki resolve", 0, f"wiki 节点不是 docx 文档：obj_type={obj_type or '(空)'}")
    obj_token = str(node.get("obj_token") or "")
    if not obj_token:
        raise FeishuError("wiki resolve", 0, f"wiki 节点缺 obj_token：{token}")
    return DocRef(document_id=obj_token, wiki_token=token, title=str(node.get("title") or ""))
