"""飞书依赖注入与能力联动测试（DESIGN-飞书接入Python化 §6/§7）。

三处联动红线（开发机假绿防线）：无凭据时 feishu_fetch 工具不注册、run_command 白名单
无 lark-cli、source_import prompt 无飞书抓取分支；feishu-forbidden 不入可重试白名单
（§7：重试直接 409 不烧 token）。
"""

from __future__ import annotations

from pathlib import Path

from platform_sdlc.agent import prompts
from platform_sdlc.agent.tools import build_tools
from platform_sdlc.config import Settings
from platform_sdlc.models import Requirement
from platform_sdlc.services.task import _RETRYABLE_REASONS


def _card() -> Requirement:
    return Requirement(id="REQ-T", name="测试卡", source_type="feishu",
                       source_url="https://hailiang.feishu.cn/wiki/x", status="clarifying")


def test_tools_register_feishu_fetch_only_with_client() -> None:
    without = [getattr(t, "name", "") for t in build_tools(None)]
    assert "feishu_fetch" not in without
    assert "run_command" in without  # 其余工具面不受影响

    class _Stub:  # 最小桩：build_tools 只判 None 与否
        pass

    with_client = [getattr(t, "name", "") for t in build_tools(_Stub())]  # type: ignore[arg-type]
    assert "feishu_fetch" in with_client


def test_prompt_feishu_branch_gated_by_capability() -> None:
    card = _card()
    enabled = prompts._source_import_steps("common", card, "/repo", Path("/out"), feishu=True)
    disabled = prompts._source_import_steps("common", card, "/repo", Path("/out"), feishu=False)
    assert "feishu_fetch" in enabled
    assert "feishu-unconfigured" in disabled and "feishu_fetch" not in disabled
    # 无能力分支不得残留 lark-cli 序列（防模型 fallback：开发机有 lark-cli 会假绿）
    assert "lark-cli" not in disabled and "lark-cli" not in enabled


def test_run_command_allowlist_drops_lark_cli() -> None:
    from platform_sdlc.agent import tools

    assert "lark-cli" not in tools._CMD_ALLOW
    assert "python3" in tools._CMD_ALLOW  # 其余白名单不受影响


def test_feishu_forbidden_not_retryable() -> None:
    """§7：feishu-forbidden 不加白名单——重试接口直接 409，权限问题重试不烧 token。"""
    assert "feishu-forbidden" not in _RETRYABLE_REASONS


def test_settings_feishu_fields() -> None:
    s = Settings(feishu_app_id="cli_x", feishu_app_secret="secret")
    assert s.feishu_app_id == "cli_x"
    assert s.feishu_media_max_bytes == 10 * 1024 * 1024  # 默认 10MB（§5 拍板）
