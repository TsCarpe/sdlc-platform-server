"""飞书原始 HTTP 客户端（DESIGN-飞书接入Python化 §13 修订后的统一 API 面）。

- docs_ai v1 fetch：服务端渲染 XML（新链路 content 数据源，与 lark-cli 同源）
- docx v1 blocks / documents：oracle 三方比对与 revision 一致性门的数据源
- drive v1 comments（need_relation=true）：评论锚点/删除态唯一来源（SDK FileComment 模型丢字段）
- wiki v2 get_node：wiki URL → docx token 解析
- contact v3 users/search：mention/评论用户名解析（无 scope 时调用方降级）
- drive v1 medias / board v1 whiteboards：三类媒体下载

全部走原始 HTTP（urllib）不经 lark-oapi SDK：评论的 extra/relation 字段与 docs_ai 接口
SDK 均无模型覆盖；凭据只经构造注入，不进 os.environ、不进 LLM 上下文（DESIGN §6）。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, cast

FEISHU = "https://open.feishu.cn"
_TOKEN_SLACK_SEC = 300  # 过期前 5 分钟即刷新


class FeishuError(Exception):
    """飞书 API 业务失败（code != 0）。log_id 供人工排查。"""

    def __init__(self, op: str, code: int, msg: str, log_id: str = "") -> None:
        super().__init__(f"飞书 API 失败 {op}：code={code} msg={msg}" + (f" log_id={log_id}" if log_id else ""))
        self.code = code
        self.msg = msg
        self.log_id = log_id


class FeishuForbiddenError(FeishuError):
    """权限类失败（文档未分享给应用等）→ fetch 层映射任务 reason=feishu-forbidden（§7，
    不入 _RETRYABLE_REASONS，重试直接 409 不烧 token）。"""


# 权限类错误码（wiki 131006 node permission denied 实测；docx/drive 权限面同族码收进来）
_FORBIDDEN_CODES = {131006, 1770002, 230002, 1061005}
_FORBIDDEN_HINTS = ("permission", "forbidden", "access denied", "no perm")


def as_dict(value: Any) -> dict[str, Any]:
    """JSON 任意节点 → dict（strict 类型收敛：原始响应里非 dict 即坏数据，空 dict 兜底）。"""
    return cast("dict[str, Any]", value) if isinstance(value, dict) else {}


class FeishuClient:
    """tenant token 自管理（缓存 + 提前刷新）；线程安全由调用方保证（agent 侧 to_thread 串行）。"""

    def __init__(self, app_id: str, app_secret: str, timeout_sec: int = 60) -> None:
        if not app_id or not app_secret:
            raise ValueError("feishu 凭据缺失：需 SDLC_FEISHU_APP_ID / SDLC_FEISHU_APP_SECRET")
        self._app_id = app_id
        self._app_secret = app_secret
        self._timeout = timeout_sec
        self._token = ""
        self._token_expire_at = 0.0

    # ---------- 基础通道 ----------

    def _ensure_token(self) -> str:
        if self._token and time.monotonic() < self._token_expire_at:
            return self._token
        req = urllib.request.Request(
            FEISHU + "/open-apis/auth/v3/tenant_access_token/internal",
            data=json.dumps({"app_id": self._app_id, "app_secret": self._app_secret}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=self._timeout) as r:
            body = json.loads(r.read())
        if body.get("code") != 0:
            raise FeishuError("tenant_access_token", int(body.get("code") or 0), str(body.get("msg") or ""))
        self._token = str(body["tenant_access_token"])
        self._token_expire_at = time.monotonic() + max(int(body.get("expire") or 0) - _TOKEN_SLACK_SEC, 60)
        return self._token

    def _request(self, method: str, path: str, params: dict[str, str] | None = None,
                 payload: dict[str, Any] | None = None) -> bytes:
        url = FEISHU + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(
            url, data=data, method=method,
            headers={"Authorization": "Bearer " + self._ensure_token(),
                     **({"Content-Type": "application/json"} if data else {})})
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            # HTTP 4xx 也带 JSON 业务体（code/msg/log_id）——统一走业务错误解析
            raw = e.read()
            try:
                body = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                raise FeishuError(f"{method} {path}", e.code, f"HTTP {e.code}") from e
            code, msg = int(body.get("code") or e.code), str(body.get("msg") or "")
            err = as_dict(body.get("error"))
            log_id = str(err.get("log_id") or body.get("log_id") or "")
            if code in _FORBIDDEN_CODES or any(h in msg.lower() for h in _FORBIDDEN_HINTS):
                raise FeishuForbiddenError(f"{method} {path}", code, msg, log_id) from e
            raise FeishuError(f"{method} {path}", code, msg, log_id) from e

    def get(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        body = json.loads(self._request("GET", path, params))
        if body.get("code") != 0:
            raise FeishuError(f"GET {path}", int(body.get("code") or 0), str(body.get("msg") or ""))
        return body.get("data") or {}

    def post(self, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        body = json.loads(self._request("POST", path, payload=payload))
        if body.get("code") != 0:
            raise FeishuError(f"POST {path}", int(body.get("code") or 0), str(body.get("msg") or ""))
        return body.get("data") or {}

    def get_bytes(self, path: str, params: dict[str, str] | None = None) -> bytes:
        return self._request("GET", path, params)

    # ---------- 文档（docs_ai 服务端渲染 = content 数据源）----------

    def fetch_document(self, document_id: str) -> dict[str, Any]:
        """docs_ai v1 fetch（空请求体）：{content, document_id, revision_id}——XML 服务端产。"""
        return as_dict(self.post(f"/open-apis/docs_ai/v1/documents/{document_id}/fetch").get("document"))

    def document_revision(self, document_id: str) -> int:
        doc = as_dict(self.get(f"/open-apis/docx/v1/documents/{document_id}").get("document"))
        return int(doc.get("revision_id") or -1)

    def list_blocks(self, document_id: str) -> list[dict[str, Any]]:
        """docx v1 blocks 全量分页（oracle 数据源；items 保序）。"""
        items: list[dict[str, Any]] = []
        page_token = ""
        while True:
            params: dict[str, str] = {"page_size": "500"}
            if page_token:
                params["page_token"] = page_token
            data = self.get(f"/open-apis/docx/v1/documents/{document_id}/blocks", params)
            items.extend(data.get("items") or [])
            page_token = str(data.get("page_token") or "")
            if not data.get("has_more") or not page_token:
                return items

    # ---------- 评论（need_relation=true 是锚点/删除态唯一来源）----------

    def list_comments(self, file_token: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        page_token = ""
        while True:
            params: dict[str, str] = {"file_type": "docx", "need_relation": "true", "page_size": "50"}
            if page_token:
                params["page_token"] = page_token
            data = self.get(f"/open-apis/drive/v1/files/{file_token}/comments", params)
            items.extend(data.get("items") or [])
            page_token = str(data.get("page_token") or "")
            if not data.get("has_more") or not page_token:
                return items

    # ---------- wiki ----------

    def resolve_wiki(self, wiki_token: str) -> dict[str, Any]:
        """wiki 节点 → {obj_type, obj_token, title, space_id}；非 docx 由调用方判定。"""
        return as_dict(self.get("/open-apis/wiki/v2/spaces/get_node",
                           {"token": wiki_token, "obj_type": "wiki"}).get("node"))

    # ---------- 用户名 ----------

    def user_names(self, user_ids: list[str]) -> dict[str, str]:
        """批量用户名解析（contact v3 users/search 逐个查——该接口无批量形态）。
        无 scope / 查不到的用户不在返回里，调用方按 user_id 前 8 位降级（DESIGN §4）。"""
        out: dict[str, str] = {}
        for uid in user_ids:
            try:
                data = self.post("/open-apis/contact/v3/users/search",
                                 {"query": uid, "page_size": 1})
            except FeishuError:
                continue  # 无 scope：整体降级，不阻断导入
            users = data.get("users")
            if isinstance(users, list) and users:
                name = as_dict(users[0]).get("name")
                if name:
                    out[uid] = str(name)
        return out

    # ---------- 媒体 ----------

    def download_media(self, token: str) -> bytes:
        """图片/附件（drive v1 media download，永久授权态免 ticket）。"""
        return self.get_bytes(f"/open-apis/drive/v1/medias/{token}/download")

    def download_whiteboard_png(self, token: str) -> bytes:
        """画板（board v1 download_as_image；media download 不支持画板——POC 实证）。"""
        return self.get_bytes(f"/open-apis/board/v1/whiteboards/{token}/download_as_image",
                              {"format": "png"})
