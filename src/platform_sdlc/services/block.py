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
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from platform_sdlc.common import ApiException, Times, is_json_list, is_json_obj, jint, jrequired, jtext
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

    # ---------- 写面：窗口 1 块级直接编辑（DESIGN-v4 §5.3/§1 表，EXEC v4-7；三期变更单 draft 复用同一 diff） ----------

    def apply_full_blocks(self, req_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """全量块数组 → 服务端 diff（edit/insert/delete/move，LIS 判 move）→ 事务应用（§5.3：
        diff 只在服务端一份，前端零 diff 代码）。守卫（§1 窗口 1 表）：拒 published、活跃质检互斥、
        块表空无基线拒。insert 服务端发 blk-<ulid>（提交可带临时 id，同批 parent 引用按映射改写）；
        delete 软删留痕；应用后按提交序整体重排 seq。幂等：同数组重发 diff 为空、零写入。"""
        with self.db.tx():
            self._guard_edit(req_id)
            updated_by = jrequired(body, "updatedBy")  # 手填身份必填（§9：全量留痕，防误操作不防故意）
            submitted = self.parse_blocks(body)
            current = self.repos.req_block.list_by_req(req_id)
            items = self.diff_blocks(req_id, submitted, current)
            self.apply_edits(req_id, submitted, current, updated_by)
        log.info("块级直接编辑 %s（by=%s）：%s", req_id, updated_by, _counts_of(items))
        return self._diff_view(items)

    def preview_diff(self, req_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """diff 预览（dry-run）：同守卫同 diff 不落库——前端保存前四态预览（三期变更单 diff 视图
        复用同形态）；insert 项 blockId 为提交临时 id（正式提交时服务端才发 blk-）。"""
        self._guard_edit(req_id)
        submitted = self.parse_blocks(body)
        current = self.repos.req_block.list_by_req(req_id)
        return self._diff_view(self.diff_blocks(req_id, submitted, current))

    def _guard_edit(self, req_id: str) -> None:
        """窗口 1 编辑守卫（§1 表）：published 终态锁拒（直接编辑是新写入口，继承的只有该谓词）；
        活跃质检互斥（queued/running 的 quality_check——防产物 anchor 基于编辑混合态，与质检启动的
        块表快照时序互斥；字面量沿 task.py T_QUALITY，import 会循环依赖）；块表空无基线拒。"""
        r = self.repos.requirement.find(req_id)
        if r is None:
            raise ApiException(404, f"需求不存在：{req_id}")
        if r.status == "published":
            raise ApiException(409, "该需求已发布标准文档（终态锁定），不可直接编辑原文")
        if self.repos.task.count_active(req_id, "quality_check") > 0:
            raise ApiException(409, "存在排队中/执行中的质检任务，请等待质检完成后再编辑（防产物锚点基于编辑混合态）")
        if self.repos.req_block.empty(req_id):
            raise ApiException(409, "原文未导入（块表空，无编辑基线）：先完成 source_import")

    def parse_blocks(self, body: dict[str, Any]) -> list[_SubBlock]:
        """提交面校验：非空数组、类型白名单（comment 不入块表——评论走 req_comment 通道）、id 去重、
        parent 拓扑（§2.3：parent 必须在本批之前出现；行内子块（parent 指向 table-row）必须紧跟
        父块或前一个同父子块——保证 seq 区间与 parent 递归两套机制答案一致，不产区间外孤儿）。"""
        blocks = body.get("blocks")
        if not is_json_list(blocks) or not blocks:
            raise ApiException(400, "blocks 必须为非空数组（全量块数组契约，DESIGN-v4 §5.3）")
        out: list[_SubBlock] = []
        ids: set[str] = set()
        first_pos: dict[str, int] = {}  # id → 首现位置（前向引用判定）
        types: dict[str, str] = {}      # id → 类型（全量，供 parent 类型判定）
        for i, b in enumerate(blocks):
            if not is_json_obj(b):
                raise ApiException(400, f"blocks[{i}] 不是对象")
            btype = jtext(b, "type", "")
            if btype == "comment":
                raise ApiException(400, f"blocks[{i}] 为 comment 块：评论不入块表，全量块数组只含内容块")
            if btype not in _BLOCK_TYPES:
                raise ApiException(400, f"blocks[{i}] 非法块类型：{btype or '(空)'}")
            bid = jtext(b, "id", "").strip()
            if bid:
                if bid in ids:
                    raise ApiException(400, f"blocks[{i}] 块 ID 重复：{bid}")
                ids.add(bid)
                first_pos[bid] = i
                types[bid] = btype
            attrs = _clean_attrs({k: v for k, v in b.items() if k not in BLOCK_ATTR_EXCLUDED})
            out.append(_SubBlock(block_id=bid, type=btype, text=jtext(b, "text", ""), attrs=attrs))
        prev_id = ""
        prev_inline_parent = ""
        for i, sb in enumerate(out):
            parent = str(sb.attrs.get("parent") or "")
            if parent:
                if first_pos.get(parent, len(out)) >= i:
                    raise ApiException(400, f"blocks[{i}]（{sb.block_id or '新块'}）parent={parent}"
                                            " 未在本批之前出现（父块缺失或前向引用）")
                ptype = types.get(parent)
                if ptype == "table-row" and parent != prev_id and parent != prev_inline_parent:
                    raise ApiException(400, f"blocks[{i}]（{sb.block_id or '新块'}）为行内子块，"
                                            f"必须紧随其表格行 {parent}（§2.3 写入约束）")
                prev_inline_parent = parent if ptype == "table-row" else ""
            else:
                prev_inline_parent = ""
            prev_id = sb.block_id
        return out

    def diff_blocks(self, req_id: str, submitted: list[_SubBlock], current: list[ReqBlock]) -> list[_DiffItem]:
        """全量块数组 vs 块表（未软删基线）→ 四态 items：同 ID 文本/属性变 = edit；不在基线 = insert
        （服务端发新 ID，提交临时 id 亦然）；基线消失 = delete；顺序变 = move。move 判定 LIS：新序列中
        保持旧 seq 相对顺序的最大子集为未动，其余才是 move——朴素逐位对比会把一次挪动判成 N 个 move，
        把无关行拉进影响面（违背「宁可漏报不可误报」）；O(n log n) 几百块无压力。"""
        cur_by_id = {b.block_id: b for b in current}
        gone = self.repos.req_block.deleted_ids(req_id)
        matched: list[tuple[int, int]] = []  # (提交序, 旧 seq) 按提交序
        for i, sb in enumerate(submitted):
            if not sb.block_id:
                continue
            cur = cur_by_id.get(sb.block_id)
            if cur is not None:
                matched.append((i, cur.seq))
            elif sb.block_id in gone:
                raise ApiException(409, f"块 {sb.block_id} 已删除（编辑基线过期）：请刷新原文后重试")
        stable = _lis_positions(matched)
        items: list[_DiffItem] = []
        for i, sb in enumerate(submitted):
            cur = cur_by_id.get(sb.block_id) if sb.block_id else None
            if cur is None:
                sb.is_new = True
                item = _DiffItem(block_id=sb.block_id, op="insert", new_text=sb.text, new_attrs=dict(sb.attrs))
                sb.item = item
                items.append(item)
                continue
            old_attrs = _clean_attrs(_load_attrs(cur.attrs_json))
            sb.changed = sb.text != cur.text or sb.attrs != old_attrs
            sb.moved = i not in stable
            if sb.changed:
                it = _DiffItem(block_id=sb.block_id, op="edit", old_text=cur.text, new_text=sb.text,
                               old_attrs=old_attrs, new_attrs=dict(sb.attrs), moved=sb.moved)
                sb.item = it  # 挂引用：insert id/parent 改写后刷新 newAttrs（assign_insert_ids）
                items.append(it)
            elif sb.moved:
                items.append(_DiffItem(block_id=sb.block_id, op="move", old_text=cur.text, new_text=sb.text))
        sub_ids = {sb.block_id for sb in submitted if sb.block_id}
        for b in current:
            if b.block_id not in sub_ids:
                items.append(_DiffItem(block_id=b.block_id, op="delete", old_text=b.text,
                                       old_attrs=_clean_attrs(_load_attrs(b.attrs_json))))
        return items

    def apply_edits(self, req_id: str, submitted: list[_SubBlock],
                     current: list[ReqBlock], updated_by: str) -> None:
        """事务内应用（调用方已开 tx，§5.5 confirm 同款语义）：edit/move version+1 留痕、insert 落
        blk-<ulid> 新行（version=1，updated_by 记创建人）、delete 软删（deleted=时间戳）、存留块按
        提交序整体重排 seq=(i+1)*1000（§2.2 重排常规化；软删行不动——冻结面不引用 seq 数值因此无害）。"""
        at = Times.now()
        self.assign_insert_ids(req_id, submitted)
        edit_rows = [(sb.text, _dumps(sb.attrs), at, updated_by, req_id, sb.block_id)
                     for sb in submitted if sb.changed]
        if edit_rows:
            self.db.executemany(
                "UPDATE req_block SET text=?, attrs_json=?, version=version+1, updated_at=?, updated_by=?"
                " WHERE req_id=? AND block_id=?", edit_rows)
        move_rows = [(at, updated_by, req_id, sb.block_id)
                     for sb in submitted if sb.moved and not sb.changed]
        if move_rows:
            self.db.executemany(
                "UPDATE req_block SET version=version+1, updated_at=?, updated_by=? WHERE req_id=? AND block_id=?",
                move_rows)
        insert_rows = [(req_id, sb.block_id, (i + 1) * 1000, sb.type, sb.text, _dumps(sb.attrs), at, updated_by)
                       for i, sb in enumerate(submitted) if sb.is_new]
        if insert_rows:
            self.db.executemany(
                "INSERT INTO req_block(req_id,block_id,seq,type,text,attrs_json,version,updated_at,updated_by)"
                " VALUES(?,?,?,?,?,?,1,?,?)", insert_rows)
        sub_ids = {sb.block_id for sb in submitted}
        delete_rows = [(at, at, updated_by, req_id, b.block_id)
                       for b in current if b.block_id not in sub_ids]
        if delete_rows:
            self.db.executemany(
                "UPDATE req_block SET deleted=?, updated_at=?, updated_by=? WHERE req_id=? AND block_id=?",
                delete_rows)
        seq_rows = [((i + 1) * 1000, req_id, sb.block_id)
                    for i, sb in enumerate(submitted) if not sb.is_new]
        if seq_rows:
            self.db.executemany("UPDATE req_block SET seq=? WHERE req_id=? AND block_id=?", seq_rows)

    def assign_insert_ids(self, req_id: str, submitted: list[_SubBlock]) -> None:
        """insert 块发平台 ID：已带 blk- 前缀的沿用（变更单 draft 期前端持有的平台 ID，§5.2「新块在
        草稿期即有平台 ID」——反复保存不漂移），其余临时 id/空 id 重发 blk-<ulid>；同批临时 id 的
        行内子块 parent 引用按映射改写；item 的 blockId/newAttrs 同步刷新（编辑出参与变更单 items 共用）。"""
        id_map: dict[str, str] = {}
        for sb in submitted:
            if not sb.is_new or sb.block_id.startswith("blk-"):
                continue
            new_id = self.new_block_id(req_id)
            if sb.block_id:
                id_map[sb.block_id] = new_id
            sb.block_id = new_id
        if id_map:
            for sb in submitted:
                parent = str(sb.attrs.get("parent") or "")
                if parent in id_map:
                    sb.attrs["parent"] = id_map[parent]
        for sb in submitted:
            if sb.item is not None:
                sb.item.block_id = sb.block_id
                sb.item.new_attrs = dict(sb.attrs)

    def new_block_id(self, req_id: str) -> str:
        """blk-<ulid>（§2.2：随机派生禁内容哈希——重复文本是常态，内容哈希即主键冲突）；
        碰撞即重抽（单写者事务内 EXISTS 检查无竞态）。"""
        while True:
            bid = "blk-" + _new_ulid26()
            if not self.db.query_scalar(
                    "SELECT EXISTS(SELECT 1 FROM req_block WHERE req_id=? AND block_id=?)", (req_id, bid)):
                return bid

    @staticmethod
    def _diff_view(items: list[_DiffItem]) -> dict[str, Any]:
        out: list[dict[str, Any]] = []
        for it in items:
            row: dict[str, Any] = {"blockId": it.block_id, "op": it.op, "oldText": it.old_text,
                                   "newText": it.new_text, "oldAttrs": it.old_attrs, "newAttrs": it.new_attrs}
            if it.moved:
                row["moved"] = True  # edit 且顺序变：diff 视图附加提示（op 语义不变）
            out.append(row)
        return {"items": out, "counts": _counts_of(items)}


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


# ---------- 窗口 1 直接编辑：模型 / LIS / blk-ID（DESIGN-v4 §5.3/§2.2，EXEC v4-7） ----------

_BLOCK_TYPES = frozenset({"title", "heading", "paragraph", "list-item",
                          "table-row", "image", "attachment", "whiteboard"})

_ULID_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford Base32（无 I/L/O/U）


@dataclass
class _SubBlock:
    """提交块（编辑面）：block_id 空/不在未软删基线 = insert（is_new，服务端发 blk-，
    提交临时 id 亦可——同批 parent 引用按映射改写）；changed/moved 由 diff 打标供应用阶段消费。"""

    block_id: str
    type: str
    text: str
    attrs: dict[str, Any]
    is_new: bool = False
    changed: bool = False
    moved: bool = False
    item: _DiffItem | None = None


@dataclass
class _DiffItem:
    """diff 四态行（§5.3）：列语义对齐三期 req_change_item（old/new_text + old/new_attrs），
    变更单 draft（v4-11）直接复用本结构与 _diff——diff 只在服务端一份。"""

    block_id: str
    op: str  # edit | insert | delete | move
    old_text: str = ""
    new_text: str = ""
    old_attrs: dict[str, Any] | None = None
    new_attrs: dict[str, Any] | None = None
    moved: bool = False  # edit 且顺序变（diff 视图提示；op 语义不变）


def _new_ulid26() -> str:
    """26 字符 ULID = 48bit 毫秒时间戳 + 80bit 随机（Crockford Base32 编码 128bit），自实现不引新依赖。"""
    value = (int(time.time() * 1000) << 80) | int.from_bytes(os.urandom(10), "big")
    chars = bytearray(26)
    for i in range(25, -1, -1):
        chars[i] = ord(_ULID_ALPHABET[value & 0x1F])
        value >>= 5
    return chars.decode("ascii")


def _lis_positions(pairs: list[tuple[int, int]]) -> set[int]:
    """最长严格递增子序列（O(n log n) patience）：pairs = [(提交序, 旧 seq)] 按提交序——返回属于
    LIS 的提交序集合（move 判定的未动集）。旧 seq 全库唯一，严格递增即保持相对顺序。"""
    if not pairs:
        return set()
    tail_vals: list[int] = []   # 各长度递增子序列的最小尾元素（seq 值）
    tail_idx: list[int] = []    # 对应 pairs 下标（回溯用）
    prev: list[int] = [-1] * len(pairs)
    for i, (_, seq) in enumerate(pairs):
        lo, hi = 0, len(tail_vals)
        while lo < hi:  # bisect_left：严格递增（相等元素替换不延长）
            mid = (lo + hi) // 2
            if tail_vals[mid] < seq:
                lo = mid + 1
            else:
                hi = mid
        if lo == len(tail_vals):
            tail_vals.append(seq)
            tail_idx.append(i)
        else:
            tail_vals[lo] = seq
            tail_idx[lo] = i
        prev[i] = tail_idx[lo - 1] if lo > 0 else -1
    marks: set[int] = set()
    k = tail_idx[-1]
    while k != -1:
        marks.add(pairs[k][0])
        k = prev[k]
    return marks


def _clean_attrs(attrs: dict[str, Any]) -> dict[str, Any]:
    """剥 None 值键：导入落库存过 parent:null 一类缺省标记，编辑器省略该键的未动块与库中 null 键块
    必须等价——否则 diff 误判 edit（属性比较与落库统一过本函数）。"""
    return {k: v for k, v in attrs.items() if v is not None}


def _dumps(attrs: dict[str, Any]) -> str:
    return json.dumps(attrs, ensure_ascii=False, separators=(",", ":"))


def _counts_of(items: list[_DiffItem]) -> dict[str, int]:
    counts = {"edit": 0, "insert": 0, "delete": 0, "move": 0}
    for it in items:
        counts[it.op] += 1
    return counts


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
