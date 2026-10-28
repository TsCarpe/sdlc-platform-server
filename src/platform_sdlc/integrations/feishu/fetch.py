"""飞书原文抓取编排（DESIGN §2/§13：feishu_fetch 工具内核，产出 source_import 消费面）。

管线：wiki 解析 → docs_ai XML（服务端渲染）→ 评论（筛选/refmap/注入 comment-refs）
→ parse_doc 离线产 doc.json → **oracle 门禁** → 媒体下载（manifest）→ parse_doc 二跑
（--media-manifest，媒体 status 回填）。

落盘布局（与 source_import prompt 契约一致）：
  <out_dir>/doc.json                 最终产物（平台完成回调直读）
  <out_dir>/media-manifest.json      媒体清单
  <out_dir>/raw/{content.xml,refmap.json,comments.json}   原始留档（审计/重放）
  <out_dir>/intake/media/            媒体文件（mediaId 命名）

版本门禁：parse_doc 必须支持 --media-manifest（旧版未知参数 exit 3 会误报用法错误），
启动即探测，缺失明确报「skills 仓版本不匹配」。
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from platform_sdlc.integrations.feishu.client import FeishuClient, FeishuError, as_dict
from platform_sdlc.integrations.feishu.comments import build_refmap, select_comments
from platform_sdlc.integrations.feishu.docx import inject_comment_refs
from platform_sdlc.integrations.feishu.media import DEFAULT_MAX_BYTES, download_all
from platform_sdlc.integrations.feishu.oracle import verify as oracle_verify
from platform_sdlc.integrations.feishu.wiki import resolve_document


class SkillsVersionMismatch(Exception):
    """姊妹仓 parse_doc.py 不支持 --media-manifest（版本门禁）。"""


@dataclass
class FetchResult:
    document_id: str
    revision_id: int
    title: str
    blocks: int
    comments: int
    media_total: int
    media_skipped: int
    out_dir: Path


def _assert_parse_doc_version(parse_doc: Path) -> None:
    r = subprocess.run([sys.executable, str(parse_doc), "--help"],
                       capture_output=True, text=True, timeout=30, check=False)
    if "--media-manifest" not in (r.stdout + r.stderr):
        raise SkillsVersionMismatch(
            f"skills 仓 parse_doc.py 不支持 --media-manifest（{parse_doc}）——"
            "请升级 claude-sdlc-skills 到配套版本（两仓版本对应关系见 README）")


def _run_parse_doc(parse_doc: Path, *, xml: Path, refmap: Path, out: Path, media_dir: Path,
                   manifest: Path | None, url: str, document_id: str, revision_id: int) -> None:
    cmd = [sys.executable, str(parse_doc), "--xml", str(xml), "--refmap", str(refmap),
           "--out", str(out), "--media-dir", str(media_dir), "--url", url,
           "--document-id", document_id, "--revision-id", str(revision_id)]
    if manifest is not None:
        cmd += ["--media-manifest", str(manifest)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300, check=False)
    if r.returncode != 0:
        raise FeishuError("parse_doc", r.returncode, f"exit {r.returncode}：{(r.stderr or r.stdout)[-400:]}")


def fetch_document(client: FeishuClient, url: str, out_dir: Path, parse_doc: Path,
                   max_media_bytes: int = DEFAULT_MAX_BYTES) -> FetchResult:
    """完整抓取管线；out_dir 由调用方给出（source_import 的 tasks/<id>/out 或工作区）。"""
    _assert_parse_doc_version(parse_doc)
    (out_dir / "raw").mkdir(parents=True, exist_ok=True)
    (out_dir / "intake" / "media").mkdir(parents=True, exist_ok=True)

    ref = resolve_document(client, url)
    document_id = ref.document_id

    # ---- 服务端渲染 XML + revision（docs_ai 与 blocks 双源，oracle 数据面）----
    fetched = client.fetch_document(document_id)
    content_xml = str(fetched.get("content") or "")
    revision_id = int(fetched.get("revision_id") or -1)
    if not content_xml or revision_id < 0:
        raise FeishuError("docs_ai fetch", 0, f"响应缺 content/revision（doc={document_id}）")
    title = ref.title or document_id

    # ---- 评论：筛选 → refmap → comment-refs 注入 ----
    raw_comments = client.list_comments(document_id)
    alive, dropped_orphan = select_comments(raw_comments, content_xml)
    _ = dropped_orphan  # 孤儿锚点计数（留档于 raw/comments.json 重放可复算）
    user_ids = sorted({str(as_dict(r).get("user_id") or "")
                       for e in alive for r in e.replies} - {""})
    names = client.user_names(user_ids)
    refmap, anchor_refs = build_refmap(alive, content_xml, names)
    xml = inject_comment_refs(content_xml, anchor_refs)

    raw_dir = out_dir / "raw"
    (raw_dir / "content.xml").write_text(xml, encoding="utf-8")
    (raw_dir / "refmap.json").write_text(json.dumps(refmap, ensure_ascii=False, indent=1),
                                         encoding="utf-8")
    (raw_dir / "comments.json").write_text(
        json.dumps({"file_token": document_id, "items": raw_comments}, ensure_ascii=False, indent=1),
        encoding="utf-8")

    # ---- parse_doc 离线产 doc.json（媒体 pending）----
    doc_path = out_dir / "doc.json"
    _run_parse_doc(parse_doc, xml=raw_dir / "content.xml", refmap=raw_dir / "refmap.json",
                   out=doc_path, media_dir=out_dir / "intake" / "media", manifest=None,
                   url=url, document_id=document_id, revision_id=revision_id)
    doc: dict[str, Any] = as_dict(json.loads(doc_path.read_text(encoding="utf-8")))

    # ---- oracle 门禁（blocks API 独立源三方比对，验收 #3 的运行时面）----
    items = client.list_blocks(document_id)
    oracle_verify(items, doc)

    # ---- 媒体下载 + manifest → parse_doc 二跑回填 status ----
    manifest = download_all(client, doc, out_dir / "intake" / "media", max_bytes=max_media_bytes)
    manifest_path = out_dir / "media-manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    if manifest:
        _run_parse_doc(parse_doc, xml=raw_dir / "content.xml", refmap=raw_dir / "refmap.json",
                       out=doc_path, media_dir=out_dir / "intake" / "media",
                       manifest=manifest_path, url=url, document_id=document_id,
                       revision_id=revision_id)
        doc = as_dict(json.loads(doc_path.read_text(encoding="utf-8")))  # 二跑后媒体 status 就位

    media = cast("list[Any]", doc.get("media") or [])
    return FetchResult(
        document_id=document_id, revision_id=revision_id, title=title,
        blocks=len(doc.get("blocks") or []),
        comments=len(alive),
        media_total=len(media),
        media_skipped=sum(1 for m in media
                          if str(as_dict(m).get("status") or "").startswith("skipped:")),
        out_dir=out_dir)
