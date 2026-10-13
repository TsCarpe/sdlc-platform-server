"""v4.2 块模型服务（DESIGN-v4 §2.3/§3）：块表 → 同形状 {blocks,media} 组装与评论兼容导出。

读面权威在块表：非 comment 块按 seq 展平，path/ordinal 读时推导（§2.2 派生不落库，推导规则对齐
parse_doc：heading 栈面包屑 ' › ' 分隔、list-item ordinal 按 parent 链拼 marker 清理段）；
评论按 thread 根分组拼回 comment 块形态插宿主块后（parse_doc 输出同构——resolve_anchors 与前端
OriginTab 消费零适配），宿主块 comment-refs 同步补回；锚定软删块的评论追加尾部（信息不丢）。
media 从 media 表组装（id/name/mime/bytes/sha256；kind/status 属导入态展示，块表权威卡不输出）。
写入通道（source_import 完成回调落库）在 v4-3 接入，workspace_root 参数为彼预置。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from platform_sdlc.common import is_json_obj, jint
from platform_sdlc.db import Database
from platform_sdlc.models import ReqComment
from platform_sdlc.repositories import Repos


class BlockService:
    def __init__(self, db: Database, repos: Repos, workspace_root: Path) -> None:
        self.db = db
        self.repos = repos
        self.workspace_root = workspace_root  # v4-3 source_import 输出定位根（tasks/<task_id>/out）

    # ---------- 读面组装 ----------

    def doc_payload(self, req_id: str) -> dict[str, Any] | None:
        """{blocks,media} 同形状导出；块表为空返回 None（调用方按场景回退 doc artifact）。"""
        blocks = self.repos.req_block.list_by_req(req_id)
        if not blocks:
            return None
        return {"blocks": self._render_blocks(req_id, blocks), "media": self._media_list(req_id)}

    def doc_export(self, req_id: str) -> dict[str, Any] | None:
        """doc.json 端点语义：块表权威组装；空表回退 doc artifact content（老卡兜底，v4-3 导入覆盖前）。"""
        payload = self.doc_payload(req_id)
        if payload is not None:
            return payload
        a = self.repos.artifact.find(req_id, "doc")
        if a is None:
            return None
        try:
            raw: Any = json.loads(a.content_json or "")
        except json.JSONDecodeError:
            return None
        return raw if is_json_obj(raw) else None

    def comments_payload(self, req_id: str) -> list[dict[str, Any]]:
        """comments.json 兼容格式（resolve_anchors --comments 契约：数组 + comment_id 键，按序号 →
        comment-id 映射）；附加键（block_id/author/resolved 等）供人工核对，锚点管线不消费。"""
        out: list[dict[str, Any]] = []
        for c in self.repos.req_comment.list_by_req(req_id):
            out.append({
                "comment_id": c.external_id or f"cm-{c.id}",
                "block_id": c.block_id, "author": c.author, "text": c.text,
                "reply_to": c.reply_to, "resolved": c.resolved, "created_at": c.created_at,
                "origin": c.origin,
            })
        return out

    def doc_view(self, req_id: str) -> dict[str, Any] | None:
        """detail.doc 形态（对齐 _artifact_view：{markdown,version,pushedAt,content}）。"""
        payload = self.doc_payload(req_id)
        if payload is None:
            return None
        row = self.db.query_one(
            "SELECT MAX(version) AS v, MAX(updated_at) AS at FROM req_block WHERE req_id=? AND deleted=''", (req_id,))
        version = int(row["v"]) if row is not None and row["v"] is not None else 1
        pushed = str(row["at"]) if row is not None and row["at"] else None
        return {"markdown": "", "version": version, "pushedAt": pushed, "content": payload}

    # ---------- 私有：渲染 ----------

    def _render_blocks(self, req_id: str, rows: list[Any]) -> list[dict[str, Any]]:
        groups = _thread_groups(self.repos.req_comment.list_by_req(req_id))
        list_ids = {b.block_id for b in rows if b.type == "list-item"}
        out: list[dict[str, Any]] = []
        heading_stack: list[tuple[int, str]] = []
        last_heading = ""
        chains: dict[str, list[str]] = {}
        sibling: dict[str, int] = {}
        ref_no = 0
        for b in rows:  # rows 已过滤软删（list_by_req deleted=''）
            attrs = _load_attrs(b.attrs_json)
            blk: dict[str, Any] = {"id": b.block_id, "type": b.type, "text": b.text, **attrs}
            if b.type == "heading":
                level = jint(attrs, "level", 1)
                while heading_stack and heading_stack[-1][0] >= level:
                    heading_stack.pop()
                heading_stack.append((level, b.text))
                last_heading = b.text
                blk["path"] = " › ".join(t for _, t in heading_stack)
            elif b.type == "list-item":
                parent = str(attrs.get("parent") or "")
                base = chains.get(parent, []) if parent in list_ids else []
                n = sibling.get(parent, 0) + 1
                sibling[parent] = n
                seg = re.sub(r"[^0-9A-Za-z一-鿿]", "", str(attrs.get("marker") or "")) or str(n)
                chain = [*base, seg]
                chains[b.block_id] = chain
                blk["ordinal"] = ".".join(chain)
                blk["path"] = " › ".join(p for p in (last_heading, blk["ordinal"]) if p)
            elif b.type != "title":
                blk["path"] = last_heading
            out.append(blk)
            host_groups = groups.pop(b.block_id, None)
            if host_groups:
                refs: list[str] = []
                for g in host_groups:
                    ref_no += 1
                    refs.append(f"c{ref_no}")
                    out.append({
                        "id": g["id"], "type": "comment", "ref": refs[-1], "anchorBlockId": b.block_id,
                        "quote": "", "thread": g["thread"], "reactions": None, "path": "评论区",
                    })
                blk["comments"] = ",".join(refs)
        for host_id, host_groups in groups.items():  # 宿主已软删的评论：追加尾部（§4.3 信息不丢）
            for g in host_groups:
                ref_no += 1
                out.append({
                    "id": g["id"], "type": "comment", "ref": f"c{ref_no}", "anchorBlockId": host_id,
                    "quote": "", "thread": g["thread"], "reactions": None, "path": "评论区（原块已删除）",
                })
        return out

    def _media_list(self, req_id: str) -> list[dict[str, Any]]:
        return [{"id": m.media_id, "name": m.name, "mime": m.mime, "bytes": m.bytes, "sha256": m.sha256}
                for m in self.repos.media.list_by_req(req_id)]


def _load_attrs(raw: str) -> dict[str, Any]:
    try:
        v: Any = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    return dict(v) if is_json_obj(v) else {}


def _thread_groups(comments: list[ReqComment]) -> dict[str, list[dict[str, Any]]]:
    """按宿主块分组、thread 根为组身份（external_id 幂等键；platform 行以根行 id 代——同块可多线程）。
    组内按回复链树展平（id 序）。"""
    by_id = {c.id: c for c in comments}
    children: dict[int, list[ReqComment]] = {}
    roots: list[ReqComment] = []
    for c in comments:
        if c.reply_to is None or c.reply_to not in by_id:
            roots.append(c)
        else:
            children.setdefault(c.reply_to, []).append(c)
    groups: dict[str, list[dict[str, Any]]] = {}
    for root in sorted(roots, key=lambda c: c.id):
        chain: list[ReqComment] = []
        seen: set[int] = set()
        stack = [root]
        while stack:
            cur = stack.pop(0)
            if cur.id in seen:
                continue
            seen.add(cur.id)
            chain.append(cur)
            stack = [*children.get(cur.id, []), *stack]
        groups.setdefault(root.block_id, []).append({
            "id": root.external_id or f"cm-{root.id}",
            "thread": [{"user": c.author, "text": c.text} for c in chain],
        })
    return groups
