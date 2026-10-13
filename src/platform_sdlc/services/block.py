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
import logging
import re
from pathlib import Path
from typing import Any

from platform_sdlc.common import ApiException, is_json_list, is_json_obj, jint
from platform_sdlc.db import Database
from platform_sdlc.models import BLOCK_ATTR_EXCLUDED, ReqBlock, ReqComment, Task
from platform_sdlc.repositories import Repos

log = logging.getLogger(__name__)


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

    # ---------- 写面：source_import 完成回调落库（DESIGN-v4 §3） ----------

    def import_from_output(self, task: Task) -> dict[str, Any]:
        """任务完成回调直读输出文件落库——内容不经 LLM 上下文（「确定性」判据可验证：
        落库内容 = 脚本输出文件字节），落库成功才置 done（挂点 = TaskService.finish，两通道汇合）。
        幂等语义（§3）：重跑 = 全量替换该卡块 + origin=feishu 评论行，平台侧评论不动；
        missingMedia 同款校验（块引用 mediaId ∈ media 表，缺失计数——宽松落库，前端渲染占位）。"""
        out_path = self.workspace_root / "tasks" / str(task.id) / "out" / "doc.json"
        if not out_path.is_file():
            raise ApiException(409, f"导入输出文件不存在：{out_path}（服务端工作区根={self.workspace_root}；"
                                    "两通道统一落 tasks/<任务ID>/out/doc.json，runner 侧确认 meta 下发的 workspaceRoot 生效）")
        try:
            raw: Any = json.loads(out_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise ApiException(409, f"导入输出文件解析失败：{out_path}（{e}）") from e
        if not is_json_obj(raw):
            raise ApiException(409, f"导入输出文件不是 JSON 对象：{out_path}")
        blocks = raw.get("blocks")
        if not is_json_list(blocks) or not blocks:
            raise ApiException(409, f"导入输出文件 blocks 为空：{out_path}")

        req_id = task.req_id
        block_rows: list[ReqBlock] = []
        comment_chains: list[list[ReqComment]] = []
        wanted_media: list[str] = []
        seq = 0
        for b in blocks:
            if not is_json_obj(b):
                continue
            seq += 1000
            btype = str(b.get("type") or "")
            if btype == "comment":
                chain = _comment_chain(req_id, b)
                if chain:
                    comment_chains.append(chain)
                continue
            attrs = {k: v for k, v in b.items() if k not in BLOCK_ATTR_EXCLUDED}
            media_ref = attrs.get("media")
            if isinstance(media_ref, str) and media_ref and media_ref not in wanted_media:
                wanted_media.append(media_ref)
            block_rows.append(ReqBlock(
                req_id=req_id, block_id=str(b.get("id") or ""), seq=seq, type=btype,
                text=str(b.get("text") or ""),
                attrs_json=json.dumps(attrs, ensure_ascii=False, separators=(",", ":")),
            ))
        present = set(self.repos.media.ids_of(req_id, wanted_media))
        missing = [m for m in wanted_media if m not in present]
        n_comments = 0
        with self.db.tx():
            self.repos.req_block.replace_all(req_id, block_rows)
            self.repos.req_comment.delete_feishu(req_id)
            for chain in comment_chains:
                prev: int | None = None
                for c in chain:
                    c.id = self.repos.req_comment.insert(_dataclass_with(c, reply_to=prev))
                    prev = c.id
                    n_comments += 1
        out = {"blocks": len(block_rows), "comments": n_comments, "missingMedia": len(missing)}
        log.info("source_import 落库 %s（任务 #%s）：%s", req_id, task.id, out)
        return out


def _comment_chain(req_id: str, cb: dict[str, Any]) -> list[ReqComment]:
    """comment 块（parse_doc 契约）→ req_comment 链：thread 首条为根，后续 reply_to 逐条；
    external_id=块 id（幂等重导定位键）、origin='feishu'、block_id=anchorBlockId。"""
    chain: list[ReqComment] = []
    thread: list[Any] = cb.get("thread") or []
    for msg in thread:
        if not is_json_obj(msg):
            continue
        text = str(msg.get("text") or "").strip()
        if not text:
            continue
        chain.append(ReqComment(
            id=0, req_id=req_id, block_id=str(cb.get("anchorBlockId") or ""), origin="feishu",
            external_id=str(cb.get("id") or ""), author=str(msg.get("user") or ""), text=text,
        ))
    return chain


def _dataclass_with(c: ReqComment, reply_to: int | None) -> ReqComment:
    """链式插入时的 reply_to 回填（dataclass replace 语义，避免可变共享）。"""
    from dataclasses import replace

    return replace(c, reply_to=reply_to)


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
