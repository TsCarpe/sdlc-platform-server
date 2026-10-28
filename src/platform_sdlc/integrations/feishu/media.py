"""媒体下载与 manifest（DESIGN §5：三类媒体 + 并发/超时/预算 + 大附件阈值）。

- 图/附件走 ``drive.v1.medias.download``，画板走 ``board.v1.whiteboards.download_as_image``
  （POC 实证画板不支持 media download）；文件名 = doc.json 的 mediaId（``m-<sha12>``，
  parse_doc 侧同规则）+ 按 mime 推扩展名（不依赖 HTTP 头 file_name——lark-cli 会自动
  补扩展名的隐含行为不复刻）
- 大附件阈值（``SDLC_FEISHU_MEDIA_MAX_BYTES``，默认 10MB）：超限只登记元数据标
  ``skipped: size-exceeded`` 不下载（30MB m4a 走此路）；大小取 attachment 块的 bytes
  （media 条目的 bytes 是下载后回填字段，判断时恒 0）
- manifest：``{token: {path, name, mime, bytes?, sha256?, skipped?}}``——parse_doc
  ``--media-manifest`` 的认领面（命中即本地文件，miss 才回退 lark-cli）
- 有界并发（4 线程）+ 单请求 120s + 总预算 10min：超预算抛 MediaBudgetExceeded 并附
  未完成清单，任务失败（fail-closed 不静默）
"""

from __future__ import annotations

import hashlib
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any, cast

from platform_sdlc.integrations.feishu.client import FeishuClient, FeishuError, as_dict

DEFAULT_MAX_BYTES = 10 * 1024 * 1024
_BUDGET_SEC = 600
_WORKERS = 4

# mime → 扩展名（常见面；未知名回退 name 后缀，再回退 bin）
_MIME_EXT: dict[str, str] = {
    "image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp",
    "image/bmp": ".bmp", "image/svg+xml": ".svg",
    "application/zip": ".zip", "application/x-zip-compressed": ".zip",
    "application/pdf": ".pdf", "text/html": ".html", "text/plain": ".txt", "text/markdown": ".md",
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.ms-powerpoint": ".ppt",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "audio/x-m4a": ".m4a", "audio/mpeg": ".mp3", "audio/wav": ".wav", "video/mp4": ".mp4",
    "application/octet-stream": ".bin",
}


class MediaBudgetExceeded(Exception):
    """总预算耗尽（携带未完成 token 清单）——任务失败，不静默降级。"""


def _ext_of(mime: str, name: str, kind: str) -> str:
    if kind == "whiteboard":
        return ".png"  # download_as_image 固定 PNG
    ext = _MIME_EXT.get(mime)
    if ext:
        return ext
    suffix = Path(name).suffix
    return suffix if suffix else ".bin"


def _attachment_sizes(doc: dict[str, Any]) -> dict[str, int]:
    """attachment 块的 token→bytes（大附件阈值判据；media 条目的 bytes 是下载后回填）。"""
    sizes: dict[str, int] = {}
    for raw in cast("list[Any]", doc.get("blocks") or []):
        b = as_dict(raw)
        if b.get("type") == "attachment":
            sizes[str(b.get("token") or "")] = int(b.get("bytes") or 0)
    return sizes


def download_all(client: FeishuClient, doc: dict[str, Any], media_dir: Path,
                 max_bytes: int = DEFAULT_MAX_BYTES,
                 budget_sec: int = _BUDGET_SEC, workers: int = _WORKERS) -> dict[str, dict[str, Any]]:
    """按 doc.json 的 media 清单全量下载 → manifest（含 skipped 项；失败/超预算 raise）。"""
    media_dir.mkdir(parents=True, exist_ok=True)
    entries = [as_dict(m) for m in cast("list[Any]", doc.get("media") or [])]
    sizes = _attachment_sizes(doc)
    deadline = time.monotonic() + budget_sec
    manifest: dict[str, dict[str, Any]] = {}
    todo: list[tuple[str, str, Path]] = []  # (token, kind, path)

    for m in entries:
        token = str(m.get("token") or "")
        media_id = str(m.get("id") or "")
        if not token or not media_id:
            continue
        name = str(m.get("name") or "")
        mime = str(m.get("mime") or "")
        kind = str(m.get("kind") or "media")
        size = sizes.get(token, 0)
        if kind == "media" and size > max_bytes:
            manifest[token] = {"path": None, "name": name, "mime": mime,
                               "skipped": "size-exceeded", "bytes": size}
            continue
        path = media_dir / (media_id + _ext_of(mime, name, kind))
        manifest[token] = {"path": str(path), "name": name, "mime": mime}
        todo.append((token, kind, path))

    def _download(item: tuple[str, str, Path]) -> tuple[str, bytes]:
        token, kind, _ = item
        data = (client.download_whiteboard_png(token) if kind == "whiteboard"
                else client.download_media(token))
        return token, data

    errors: dict[str, str] = {}
    if todo:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures: dict[Future[tuple[str, bytes]], Path] = {
                pool.submit(_download, item): item[2] for item in todo}
            for fut, path in futures.items():
                try:
                    token, data = fut.result()
                except Exception as e:  # noqa: BLE001——逐项聚合（超时/网络/API），收口统一处置
                    token = next(t for t, _, p in todo if p == path)
                    errors[token] = f"{type(e).__name__}: {e}"
                    continue
                path.write_bytes(data)
                entry = manifest[token]
                entry["bytes"] = len(data)
                entry["sha256"] = hashlib.sha256(data).hexdigest()

    incomplete = [t for t, _, p in todo if not p.exists() and t not in errors]
    if incomplete and time.monotonic() > deadline:
        raise MediaBudgetExceeded(f"媒体下载超预算（{budget_sec}s），未完成：{incomplete}")
    if errors:
        head = "; ".join(f"{t[:10]}… {msg}" for t, msg in list(errors.items())[:3])
        raise FeishuError("media download", 0, f"媒体下载失败 {len(errors)} 项：{head}")
    return manifest
