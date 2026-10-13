"""v4.8 评论系统服务（DESIGN-v4 §4）：块级/卡片级评论 CRUD + 线程级解决 + 升级澄清项（L 系）。

与澄清项划清地盘（§4.1）：评论=自由讨论低门槛随时发（无状态守卫，任何阶段可发），
澄清项=结构化追踪单元；「升级」是显式动作，自动带入块文本作 quote、块 ID 作 anchor，
src_comment_id 回链原评论——「被升级的评论可跳回原讨论上下文」的载体。
已解决评论仍可升级（已结束的讨论仍可能需要正式追踪），升级不改评论自身 resolved 态。
"""

from __future__ import annotations

import logging
from typing import Any

from platform_sdlc.common import ApiException, Times, jbool, jint_or_400, jrequired, jtext
from platform_sdlc.db import Database
from platform_sdlc.models import ClarifyItem, ReqComment
from platform_sdlc.repositories import Repos

log = logging.getLogger(__name__)

_LEVELS = ("red", "yellow", "blue")


class CommentService:
    def __init__(self, db: Database, repos: Repos) -> None:
        self.db = db
        self.repos = repos

    def create(self, req_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """发评论：blockId 空=卡片级（挂卡片头部「整体疑问」），非空锚定原文块（含软删块——
        §4.3 软删块上的评论不删、收进变更单 diff 折叠区）；replyTo 校验同卡存在；
        author 手填身份（无登录约定，case_comment 同款可空）。"""
        self._require(req_id)
        block_id = jtext(body, "blockId", "").strip()
        reply_to = jint_or_400(body, "replyTo", None)
        if block_id and self.repos.req_block.find_text_any(req_id, block_id) is None:
            raise ApiException(400, f"blockId 不存在：{block_id}（先完成导入或刷新原文）")
        if reply_to is not None and self.repos.req_comment.find(req_id, reply_to) is None:
            raise ApiException(400, f"replyTo 不存在：{reply_to}（回复目标必须是本卡已有评论）")
        c = ReqComment(id=0, req_id=req_id, block_id=block_id, origin="platform", external_id="",
                       author=jtext(body, "author", ""), text=jrequired(body, "text"),
                       reply_to=reply_to, resolved=0, created_at=Times.now())
        c.id = self.repos.req_comment.insert(c)
        log.info("评论 #%s %s（block=%s reply_to=%s by=%s）", c.id, req_id, block_id or "(卡片级)",
                 reply_to, c.author or "(匿名)")
        return self._view(c)

    def list(self, req_id: str) -> dict[str, Any]:
        """列表 + 未解决计数（§4.3 列表页角标：质检前/漏网问题被看见的最低成本通道）。"""
        self._require(req_id)
        rows = self.repos.req_comment.list_by_req(req_id)
        return {"comments": [self._view(c) for c in rows],
                "unresolvedCount": sum(1 for c in rows if not c.resolved)}

    def resolve(self, req_id: str, comment_id: int, body: dict[str, Any]) -> dict[str, Any]:
        """解决/重开：线程级（目标行归根，根 + 回复链全置同值）——对齐飞书「解决整串」体验；
        幂等（重复 resolve 同值零变化）。"""
        with self.db.tx():
            self._require(req_id)
            target = self._require_comment(req_id, comment_id)
            chain = self._thread_of(req_id, target)
            self.repos.req_comment.set_resolved_many([c.id for c in chain], jbool(body, "resolved", True))
        return {"ok": True}

    def delete(self, req_id: str, comment_id: int) -> dict[str, Any]:
        """单条物理删（回复行保留——悬空 reply_to 在组装面归根处理；自由讨论低门槛不设回收站）。"""
        with self.db.tx():
            self._require(req_id)
            self._require_comment(req_id, comment_id)
            self.repos.req_comment.delete_one(req_id, comment_id)
        return {"ok": True}

    def escalate(self, req_id: str, comment_id: int, body: dict[str, Any]) -> dict[str, Any]:
        """升级为澄清项（§4.1 显式动作）：插 clarify_item 行——L<两位> 编号（origin='local'）、
        quote=块文本、anchor=块 ID（卡片级两者皆空）、src_comment_id 回链；已解决评论仍可升级；
        幂等：同评论重复升级 409（不重复插行）。"""
        with self.db.tx():
            self._require(req_id)
            c = self._require_comment(req_id, comment_id)
            if self.repos.clarify_item.exists_by_src_comment(req_id, comment_id):
                raise ApiException(409, f"评论 #{comment_id} 已升级为澄清项（每条评论只可升级一次）")
            level = jtext(body, "level", "yellow")
            if level not in _LEVELS:
                raise ApiException(400, f"非法 level：{level}（仅支持 red/yellow/blue）")
            quote, anchor = "", ""
            if c.block_id:
                quote = self.repos.req_block.find_text_any(req_id, c.block_id) or ""
                anchor = c.block_id
            item = ClarifyItem(
                id=0, req_id=req_id, seq=self.repos.clarify_item.next_local_seq(req_id), level=level,
                theme=jtext(body, "theme", "评论升级") or "评论升级", question=c.text,
                quote=quote, anchor=anchor, status="pending", origin="local", src_comment_id=c.id,
            )
            self.repos.clarify_item.insert(item)
        log.info("评论 #%s 升级澄清项 %s %s（%s）", c.id, item.seq, req_id, item.anchor or "卡片级")
        return {
            "seq": item.seq, "level": item.level, "theme": item.theme, "question": item.question,
            "quote": item.quote, "anchor": item.anchor, "status": item.status,
            "origin": item.origin, "srcCommentId": item.src_comment_id,
        }

    # ---------- 私有 ----------

    def _require(self, req_id: str) -> None:
        if self.repos.requirement.find(req_id) is None:
            raise ApiException(404, f"需求不存在：{req_id}")

    def _require_comment(self, req_id: str, comment_id: int) -> ReqComment:
        c = self.repos.req_comment.find(req_id, comment_id)
        if c is None:
            raise ApiException(404, f"评论不存在：#{comment_id}")
        return c

    def _thread_of(self, req_id: str, target: ReqComment) -> list[ReqComment]:
        """目标行所在线程全链（向上找根 + 向下收集回复，一次查询内存构链——评论量小，
        循环体内无数据库查询铁律对齐）。环防御：reply_to 指向自身/成环时止步。"""
        all_rows = self.repos.req_comment.list_by_req(req_id)
        by_id = {c.id: c for c in all_rows}
        root = target
        seen = {target.id}
        while root.reply_to is not None and root.reply_to in by_id and root.reply_to not in seen:
            root = by_id[root.reply_to]
            seen.add(root.id)
        children: dict[int, list[ReqComment]] = {}
        for c in all_rows:
            if c.reply_to is not None:
                children.setdefault(c.reply_to, []).append(c)
        chain: list[ReqComment] = []
        stack = [root]
        while stack:
            cur = stack.pop(0)
            chain.append(cur)
            stack = [*children.get(cur.id, []), *stack]
        return chain

    @staticmethod
    def _view(c: ReqComment) -> dict[str, Any]:
        return {
            "id": c.id, "blockId": c.block_id, "origin": c.origin, "externalId": c.external_id,
            "author": c.author, "text": c.text, "replyTo": c.reply_to,
            "resolved": bool(c.resolved), "createdAt": c.created_at,
        }
