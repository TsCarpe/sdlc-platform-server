#!/usr/bin/env python3
"""飞书黄金样本冻结（DESIGN-飞书接入Python化.md §9 步骤 0）。

一条命令冻结一篇文档的黄金资产（序列化器/oracle/评论规则测试的离线对照基线）：
  blocks.json     docx blocks API 原始响应（items 保序合并，未过 SDK 模型——oracle 数据源；
                  模型 unmarshal 会丢未知字段，原始 HTTP 才是「原始响应」语义）
  comments.json   评论原始 HTTP 响应（need_relation=true；extra.content_anchor_id 与
                  relation.content_deleted 只在原始响应存活，SDK FileComment 模型会丢弃）
  content.xml     lark-cli docs +fetch --doc-format xml --detail with-ids 的产物
                  （黄金基线 = lark-cli 链路；新链路产出与它比对，验收 #2）
  docsai.xml      docs_ai v1 fetch 服务端渲染的原始 XML（新链路实际数据源，DESIGN §13：
                  与 lark-cli 版仅差 comment-refs 属性——那是客户端从评论锚点本地注入的）
  refmap.json     同上 reference_map.comments（key 匹配 c\\d+，value.data 是 <comment> XML 串）
  doc.json        parse_doc.py(content.xml + refmap.json) 离线产物（无 --download，媒体 pending）
  meta.json       document_id / revision_id / 块类型分布 / 工具基线 / 冻结时间

一致性门：lark-cli fetch 的 revision_id 必须等于 blocks API 的 document_revision_id——
文档在冻结窗口内被编辑即中止（黄金资产必须是同一瞬间的快照，混两个版本比对必假红）。

用法：
  冻结：python3 scripts/freeze_feishu_golden.py --name <样本名> --url <文档URL或token> [--note 说明]
  探测：python3 scripts/freeze_feishu_golden.py --probe --url <…>   # 只统计块类型分布，不落盘

凭据：LARK_APP_ID / LARK_APP_SECRET 环境变量优先，缺则回退读 ~/PycharmProjects/Askly/.env
（共用 Askly 应用，DESIGN §8 拍板）；密钥不打印、不写入任何产物。纯标准库，无 lark-oapi 依赖。
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn, cast

REPO = Path(__file__).resolve().parent.parent
FEISHU = "https://open.feishu.cn"
ASKLY_ENV = Path.home() / "PycharmProjects/Askly/.env"
PARSE_DOC_DEFAULT = Path.home() / "IdeaProjects/claude-sdlc-skills/skills/sdlc-intent/scripts/parse_doc.py"

# 官方 block_type 枚举（DESIGN §3 映射表同源；探测输出用，冻结不依赖它判断合法性）
BLOCK_TYPES: dict[int, str] = {
    1: "page", 2: "text", 3: "heading1", 4: "heading2", 5: "heading3", 6: "heading4",
    7: "heading5", 8: "heading6", 9: "heading7", 10: "heading8", 11: "heading9",
    12: "bullet", 13: "ordered", 14: "code", 15: "quote", 17: "todo", 19: "callout",
    22: "divider", 23: "file", 24: "grid", 25: "grid_column", 26: "iframe", 27: "image",
    30: "sheet", 31: "table", 32: "table_cell", 33: "view", 34: "quote_container",
    43: "board",
}


def die(msg: str) -> NoReturn:
    print(f"❌ {msg}", file=sys.stderr)
    raise SystemExit(1)


def load_creds() -> tuple[str, str]:
    import os
    app_id = os.environ.get("LARK_APP_ID", "")
    app_secret = os.environ.get("LARK_APP_SECRET", "")
    if app_id and app_secret:
        return app_id, app_secret
    if ASKLY_ENV.is_file():
        env: dict[str, str] = {}
        for line in ASKLY_ENV.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
        if env.get("LARK_APP_ID") and env.get("LARK_APP_SECRET"):
            return env["LARK_APP_ID"], env["LARK_APP_SECRET"]
    die("缺凭据：设 LARK_APP_ID / LARK_APP_SECRET 或提供 ~/PycharmProjects/Askly/.env")


class Feishu:
    """原始 HTTP 客户端（tenant_token 复用；code!=0 即抛错——fail-closed 不静默）。"""

    def __init__(self, app_id: str, app_secret: str) -> None:
        req = urllib.request.Request(
            FEISHU + "/open-apis/auth/v3/tenant_access_token/internal",
            data=json.dumps({"app_id": app_id, "app_secret": app_secret}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=30) as r:
            tok = json.loads(r.read())
        if tok.get("code") != 0:
            die(f"取 tenant_access_token 失败：code={tok.get('code')} msg={tok.get('msg')}")
        self.token = str(tok["tenant_access_token"])

    def get(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        url = FEISHU + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"Authorization": "Bearer " + self.token})
        with urllib.request.urlopen(req, timeout=60) as r:
            body = json.loads(r.read())
        if body.get("code") != 0:
            die(f"API 失败 {path}：code={body.get('code')} msg={body.get('msg')}")
        return body["data"]

    def post(self, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        req = urllib.request.Request(
            FEISHU + path, data=json.dumps(payload or {}).encode(),
            headers={"Authorization": "Bearer " + self.token,
                     "Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=120) as r:
            body = json.loads(r.read())
        if body.get("code") != 0:
            die(f"API 失败 POST {path}：code={body.get('code')} msg={body.get('msg')}")
        return body["data"]


def parse_token(raw: str) -> tuple[str, str]:
    """URL 或裸 token → (形态 wiki|docx, token)。支持 /docx/ /docs/ /wiki/ 路径。"""
    m = re.search(r"/(docx|docs|wiki)/([A-Za-z0-9]+)", raw)
    if m:
        kind = "wiki" if m.group(1) == "wiki" else "docx"
        return kind, m.group(2)
    return "docx", raw.strip()


def resolve_document(f: Feishu, raw: str) -> dict[str, str]:
    """URL/token → {document_id, wiki_token?, title}。wiki 需二次解析，非 docx 拒绝。"""
    kind, token = parse_token(raw)
    if kind != "wiki":
        return {"document_id": token, "wiki_token": "", "title": ""}
    node_raw = f.get("/open-apis/wiki/v2/spaces/get_node",
                     {"token": token, "obj_type": "wiki"}).get("node")
    node: dict[str, Any] = cast("dict[str, Any]", node_raw) if isinstance(node_raw, dict) else {}
    if node.get("obj_type") != "docx":
        die(f"wiki 节点不是 docx 文档：obj_type={node.get('obj_type')}")
    return {"document_id": str(node.get("obj_token") or ""), "wiki_token": token,
            "title": str(node.get("title") or "")}


def fetch_blocks(f: Feishu, doc_id: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    page_token = ""
    while True:
        params: dict[str, str] = {"page_size": "500"}
        if page_token:
            params["page_token"] = page_token
        data = f.get(f"/open-apis/docx/v1/documents/{doc_id}/blocks", params)
        items.extend(data.get("items") or [])
        if not data.get("has_more"):
            return items
        page_token = str(data.get("page_token") or "")
        if not page_token:
            return items


def fetch_revision(f: Feishu, doc_id: str) -> int:
    """文档当前 revision（blocks list 响应不带它；一致性门与 lark-cli 对齐用）。"""
    doc_raw = f.get(f"/open-apis/docx/v1/documents/{doc_id}").get("document")
    doc = cast("dict[str, Any]", doc_raw) if isinstance(doc_raw, dict) else None
    if doc is None:
        die(f"documents get 响应异常：无 document 节点（{doc_id}）")
    return int(doc.get("revision_id") or -1)


def fetch_comments(f: Feishu, file_token: str) -> list[dict[str, Any]]:
    """评论原始响应（need_relation=true 是锚点/删除态的唯一来源，DESIGN §4）。"""
    items: list[dict[str, Any]] = []
    page_token = ""
    while True:
        params: dict[str, str] = {"file_type": "docx", "need_relation": "true", "page_size": "50"}
        if page_token:
            params["page_token"] = page_token
        data = f.get(f"/open-apis/drive/v1/files/{file_token}/comments", params)
        items.extend(data.get("items") or [])
        page_token = str(data.get("page_token") or "")
        if not page_token or not data.get("has_more"):
            return items


def run_lark_fetch(doc_id: str) -> dict[str, Any]:
    """lark-cli docs +fetch（黄金基线链路）。返回 data.document 节点。"""
    cli = shutil.which("lark-cli")
    if not cli:
        die("未找到 lark-cli（黄金基线必须由 lark-cli 产出；探测模式不需要它）")
    r = subprocess.run(
        [cli, "docs", "+fetch", "--doc", doc_id, "--doc-format", "xml",
         "--detail", "with-ids", "--format", "json"],
        capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        die(f"lark-cli +fetch 失败（exit {r.returncode}）：{(r.stderr or r.stdout)[-300:]}")
    try:
        doc = json.loads(r.stdout)["data"]["document"]
    except (json.JSONDecodeError, KeyError):
        die(f"lark-cli +fetch 输出形态异常：{r.stdout[:200]}")
    return doc


def block_type_counts(items: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[int, int] = {}
    for blk in items:
        t = int(blk.get("block_type") or 0)
        counts[t] = counts.get(t, 0) + 1
    return {f"{t}:{BLOCK_TYPES.get(t, 'UNKNOWN')}": n for t, n in sorted(counts.items())}


def main() -> int:
    ap = argparse.ArgumentParser(description="冻结飞书黄金样本（DESIGN-飞书接入Python化 步骤 0）")
    ap.add_argument("--name", help="样本名（产出目录 tests/fixtures/feishu/<name>）")
    ap.add_argument("--url", required=True, help="飞书文档 URL 或 token（wiki/docx 均可）")
    ap.add_argument("--note", default="", help="样本说明（记入 meta.json）")
    ap.add_argument("--probe", action="store_true", help="只统计块类型分布，不落盘（挑扩样本用）")
    ap.add_argument("--out-root", default=str(REPO / "tests/fixtures/feishu"))
    ap.add_argument("--parse-doc", default=str(PARSE_DOC_DEFAULT))
    args = ap.parse_args()
    if not args.probe and not args.name:
        die("冻结模式必须传 --name（或改用 --probe）")

    app_id, app_secret = load_creds()
    f = Feishu(app_id, app_secret)
    info = resolve_document(f, args.url)
    doc_id = info["document_id"]
    if not doc_id:
        die("无法解析 document_id")
    title = info["title"] or doc_id
    print(f"目标：{title}（docx={doc_id[:12]}…{f'，wiki={info['wiki_token'][:10]}…' if info['wiki_token'] else ''}）")

    items = fetch_blocks(f, doc_id)
    revision = fetch_revision(f, doc_id)
    print(f"blocks：{len(items)} 块，revision_id={revision}")
    counts = block_type_counts(items)
    print(f"块类型分布：{counts}")

    if args.probe:
        wanted = [k for k in counts if any(w in k for w in (":code", ":quote", ":todo", ":divider", ":sheet"))]
        print(f"\n扩样本目标类型命中：{wanted or '无（code/quote/todo/divider/sheet 均未覆盖）'}")
        return 0

    comments = fetch_comments(f, doc_id)
    print(f"comments：{len(comments)} 条（need_relation=true）")

    # ---- lark-cli 黄金基线（revision 一致性门）----
    lark_doc = run_lark_fetch(doc_id)
    lark_revision = int(lark_doc.get("revision_id") or -1)
    if lark_revision != revision:
        die(f"revision 不一致：blocks API={revision}，lark-cli={lark_revision}——文档在冻结窗口内被编辑，稍后重跑")
    content_xml = str(lark_doc.get("content") or "")
    rm_raw = lark_doc.get("reference_map")
    rm = cast("dict[str, Any]", rm_raw) if isinstance(rm_raw, dict) else {}
    cm_raw = rm.get("comments")
    refmap = cast("dict[str, Any]", cm_raw) if isinstance(cm_raw, dict) else {}
    print(f"lark-cli 基线：content.xml={len(content_xml)} 字符，refmap={len(refmap)} 条评论，revision 对齐 ✅")

    # docs_ai 双基线（DESIGN §13）：服务端渲染 XML 是新链路数据源；与 lark-cli 版的差异
    # 必须仅为 comment-refs（客户端注入面）——超出即 docs_ai 行为漂移，冻结时即暴露
    docsai_doc = f.post(f"/open-apis/docs_ai/v1/documents/{doc_id}/fetch")
    docsai_doc_node = cast("dict[str, Any]", docsai_doc.get("document") or {})
    docsai_xml = str(docsai_doc_node.get("content") or "")
    if int(docsai_doc_node.get("revision_id") or -1) != revision:
        die(f"docs_ai revision 不一致：{docsai_doc_node.get('revision_id')} vs {revision}——冻结窗口内文档被编辑，稍后重跑")
    stripped = re.sub(r' comment-refs="[^"]*"', "", content_xml)
    if docsai_xml != stripped:
        i = next((i for i in range(min(len(docsai_xml), len(stripped)))
                  if docsai_xml[i] != stripped[i]), min(len(docsai_xml), len(stripped)))
        die(f"docs_ai 与 lark-cli 差异超出 comment-refs 面 @{i}："
            f"docsai={docsai_xml[max(0, i - 60):i + 60]!r} larkcli={stripped[max(0, i - 60):i + 60]!r}")
    print(f"docs_ai 基线：{len(docsai_xml)} 字符（剥 comment-refs 后与 lark-cli 一致 ✅）")

    out_dir = Path(args.out_root) / args.name
    if out_dir.exists():
        shutil.rmtree(out_dir)  # 样本目录可整体重建；失败半成品同样不留
    out_dir.mkdir(parents=True)
    try:
        (out_dir / "blocks.json").write_text(
            json.dumps({"document_id": doc_id, "document_revision_id": revision, "items": items},
                       ensure_ascii=False, indent=1), encoding="utf-8")
        (out_dir / "comments.json").write_text(
            json.dumps({"file_token": doc_id, "items": comments}, ensure_ascii=False, indent=1),
            encoding="utf-8")
        (out_dir / "content.xml").write_text(content_xml, encoding="utf-8")
        (out_dir / "docsai.xml").write_text(docsai_xml, encoding="utf-8")
        (out_dir / "refmap.json").write_text(
            json.dumps(refmap, ensure_ascii=False, indent=1), encoding="utf-8")

        # parse_doc 离线产物（黄金 doc.json 基线；无 --download，媒体 status=pending）
        parse_doc = Path(args.parse_doc).expanduser()
        if not parse_doc.is_file():
            die(f"parse_doc.py 不存在：{parse_doc}")
        r = subprocess.run(
            [sys.executable, str(parse_doc), "--xml", str(out_dir / "content.xml"),
             "--refmap", str(out_dir / "refmap.json"), "--out", str(out_dir / "doc.json"),
             "--media-dir", str(out_dir / "media"), "--url", args.url,
             "--document-id", doc_id, "--revision-id", str(revision)],
            capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            die(f"parse_doc.py 失败（exit {r.returncode}）：\n{r.stdout}\n{r.stderr}")
        print(r.stdout.strip())

        skills_head = subprocess.run(["git", "-C", str(parse_doc.parent.parent.parent),
                                      "rev-parse", "--short", "HEAD"],
                                     capture_output=True, text=True).stdout.strip()
        meta = {
            "name": args.name, "note": args.note, "url": args.url,
            "title": title, "document_id": doc_id, "wiki_token": info["wiki_token"],
            "revision_id": revision, "frozen_at": datetime.now(UTC).isoformat(),
            "block_count": len(items), "block_types": counts, "comment_count": len(comments),
            "golden_chain": "lark-cli 1.x + claude-sdlc-skills parse_doc.py",
            "parse_doc_baseline": skills_head,
        }
        (out_dir / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    except SystemExit:
        shutil.rmtree(out_dir, ignore_errors=True)
        raise
    print(f"\n✅ 冻结完成：{out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
