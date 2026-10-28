"""docs_ai XML 后处理（DESIGN §13：序列化器退役后的薄层）。

唯一职责：把评论锚点（comments.build_refmap 的 anchor_refs）注入 docs_ai 服务端渲染的
XML——lark-cli 的「local comment anchors」客户端行为复刻。注在锚定块开标签上（parse_doc
从块标签读 comment-refs；lark-cli 注在内层 span，宿主块 comments 字段两侧均不在黄金比对
面，取让下游真正生效的一侧）。
"""

from __future__ import annotations

import re

# 开标签匹配：<tag id="…" …>，仅取带 id 的元素（锚点即块 id）；XML 单行无嵌套歧义风险
_OPEN_TAG = re.compile(r'<([a-z0-9]+)\s+id="([A-Za-z0-9]+)"([^>]*)>')


def inject_comment_refs(xml: str, anchor_refs: dict[str, list[str]]) -> str:
    """block_id → ["c1","c5"] 注入为 ``comment-refs="c1 c5"``（多线程空格分隔）。

    幂等：已有 comment-refs 的标签跳过（同 id 多标签时首现即锚块——docs_ai 输出不含
    comment-refs，此防护只为重复调用安全）。"""
    done: set[str] = set()

    def _sub(m: re.Match[str]) -> str:
        tag, bid, rest = m.group(1), m.group(2), m.group(3)
        refs = anchor_refs.get(bid)
        if not refs or bid in done or "comment-refs=" in rest:
            return m.group(0)
        done.add(bid)
        return f'<{tag} id="{bid}"{rest} comment-refs="{" ".join(refs)}">'

    return _OPEN_TAG.sub(_sub, xml)
