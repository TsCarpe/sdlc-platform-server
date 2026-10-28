"""飞书接入（DESIGN-飞书接入Python化.md，§13 修订架构：docs_ai 服务端渲染 + 客户端薄层）。"""

from platform_sdlc.integrations.feishu.client import (
    FeishuClient,
    FeishuError,
    FeishuForbiddenError,
)

__all__ = ["FeishuClient", "FeishuError", "FeishuForbiddenError"]
