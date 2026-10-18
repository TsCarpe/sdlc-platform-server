"""agent 平台工具面（对应 Java AgentPlatformTools，v2.1 红线原样平移）。

本工具面上【不存在】任何对应 POST /artifacts、POST /publish、POST /api/cases 的
正式产物写入工具——从「prompt 明令禁止」升级为「工具面缺失」。
所有阻塞操作（文件 IO / 子进程 / sqlite）经 asyncio.to_thread 执行，不冻结事件循环。
工具出错返回 {"error": ...} 字符串给模型（对应 Java 工具抛 ApiException 的回灌语义）。
"""

from __future__ import annotations

import asyncio
import json
import re
import shlex
from pathlib import Path
from typing import Any

from agents import RunContextWrapper, Tool, function_tool

from platform_sdlc.agent.context import TaskRunContext

# run_command 分段白名单（每段首 token 必须命中；飞书原文抓取 / 平台数据 curl 直落 raw/（增量
# 任务变更单冻结面数据源，三期 D-M 修复死路）/ 锚点解析脚本所需）。curl 信任面记档：进程内 agent
# 可对任意端点发 HTTP——接受依据：python3 已在白名单（本就可执行任意代码，信任下界未降）、
# runner 通道（独立进程）本就 curl 平台且数据面文件化一致、agent 会话由平台固定任务 prompt 驱动
_CMD_ALLOW = {"lark-cli", "python3", "node", "mkdir", "cat", "ls", "jq", "grep", "find", "curl"}

# 工具描述（LLM 可见面）运行时由 _CMD_ALLOW 构造——手抄清单会随白名单演进失修（三期 D-M：
# 原文案 9 项只列 6 项即此病灶）；经显式 __doc__ 赋值 + 裸调用 function_tool 装饰生效
# （f-string 非 string literal、写在函数体首语句不成 docstring）
_CMD_DOC = (
    "在当前任务工作区目录执行命令（飞书原文抓取、平台数据 curl 直落 raw/、锚点解析脚本等）。"
    "支持管道与重定向；按 | ; && 分段后每段首命令须在白名单：" + "/".join(sorted(_CMD_ALLOW)) +
    "。分段为引号感知（引号内的 ; | 是字面量不切段——URL/代码内分号用引号包裹即可），"
    "未闭合引号会被拒绝。禁止命令替换（反引号、$()）。\n\n"
    "Args:\n"
    "    command: 完整命令行（多段用 | 或 ; 连接，每段首命令须白名单内）\n"
    "    timeout_seconds: 超时秒数，默认 300\n"
    "    "
)

_MEDIA_ID = re.compile(r"^m-[0-9a-f]{12}$")


def _err(msg: str) -> str:
    return json.dumps({"error": msg}, ensure_ascii=False)


def _resolve_in_workspace(ctx: TaskRunContext, rel: str) -> Path:
    p = (ctx.workspace / rel).resolve()
    # is_relative_to 逐组件比较（基准先 resolve：workspace 由调用方拼出，skills_repo 仅 expanduser）
    if not p.is_relative_to(ctx.workspace.resolve()):
        raise ValueError(f"路径越界：{rel}")
    return p


def _resolve(ctx: TaskRunContext, rel: str) -> Path:
    a = (ctx.skills_repo.resolve() / rel).resolve()
    if a.is_relative_to(ctx.skills_repo.resolve()) and a.exists():
        return a
    return _resolve_in_workspace(ctx, rel)


def _fuzzy_find(ctx: TaskRunContext, rel: str) -> Path | None:
    """按文件名在技能仓内检索（弱模型路径拼写容错）：优先浅层命中。"""
    name = rel.rsplit("/", 1)[-1]
    if not name:
        return None
    hits = sorted(ctx.skills_repo.rglob(name), key=lambda p: len(p.parts))
    return hits[0] if hits else None


def _read_text(p: Path) -> str:
    return p.read_text(encoding="utf-8", errors="replace")


# ---------- 只读平台数据（锁定当前任务 reqId，无法越权读别的需求） ----------


@function_tool
async def get_requirement_card(wrapper: RunContextWrapper[TaskRunContext]) -> str:
    """读取当前需求卡：id、名称、状态、风险分级、来源类型、来源URL、已发布标准文档；需求原文双源
    （DESIGN-v4 §2.3）——doc 为块表组装的 {blocks,media}（导入后权威，质检/梳理以它为输入），
    sourceText 保留兼容（未导入卡的降级输入）。"""
    def impl() -> str:
        c = wrapper.context
        r = c.services.requirements.require(c.req_id)
        data: dict[str, Any] = {
            "id": r.id, "name": r.name, "status": r.status, "riskTier": r.risk_tier,
            "sourceType": r.source_type, "sourceUrl": r.source_url,
            "sourceText": r.source_text, "standardDoc": r.standard_doc or "",
        }
        doc = c.services.blocks.doc_payload(c.req_id)
        if doc is not None:
            data["doc"] = doc
        return json.dumps(data, ensure_ascii=False)

    return await asyncio.to_thread(impl)


@function_tool
async def list_clarify_items(wrapper: RunContextWrapper[TaskRunContext]) -> str:
    """列出澄清问题清单（含 PM 已填答复；合成时答复一律消费 answerText——服务端解构后的
    纯文本，answer 原文可能是结构化 JSON 串〔DESIGN-v5 D15〕，不要直接消费）。"""
    def impl() -> str:
        c = wrapper.context
        from platform_sdlc.schemas import wire

        return json.dumps([wire(x) for x in c.repos.clarify_item.list_by_req(c.req_id)], ensure_ascii=False)

    return await asyncio.to_thread(impl)


@function_tool
async def list_req_comments(wrapper: RunContextWrapper[TaskRunContext]) -> str:
    """列出当前需求卡的评论（v4-5 质检输入源）：飞书导入 + 平台评论统一列表，输出即
    resolve_anchors --comments 兼容格式（数组 + comment_id 键）——原样 write_file 落盘
    raw/comments.json 即可喂锚点管线，不要改写结构。"""
    def impl() -> str:
        c = wrapper.context
        return json.dumps(c.services.blocks.comments_payload(c.req_id), ensure_ascii=False)

    return await asyncio.to_thread(impl)


@function_tool
async def get_digest_bundle(wrapper: RunContextWrapper[TaskRunContext]) -> str:
    """读取已采纳的质检产物：digest 与 audit 的 markdown 与 contentJson（case_gen / standard_doc 的输入）。"""
    def impl() -> str:
        c = wrapper.context
        m: dict[str, str] = {}
        a = c.repos.artifact.find(c.req_id, "digest")
        if a is not None:
            m["digestMd"] = a.markdown
            m["digestJson"] = a.content_json
        audit = c.repos.artifact.find(c.req_id, "audit")
        if audit is not None:
            m["auditMd"] = audit.markdown
            m["auditJson"] = audit.content_json
        if not m:
            raise ValueError("该需求尚无已采纳的 digest/audit 产物")
        return json.dumps(m, ensure_ascii=False)

    try:
        return await asyncio.to_thread(impl)
    except ValueError as e:
        return _err(str(e))


@function_tool
async def get_design_doc(wrapper: RunContextWrapper[TaskRunContext]) -> str:
    """读取已定稿的技术设计（design_rev 修订输入）：终稿 markdown、版本、目标代码仓、风险分级。只读工具——正式设计文档无写入通道，修订一律回 design 草稿。"""
    def impl() -> str:
        c = wrapper.context
        r = c.services.requirements.require(c.req_id)
        a = c.repos.artifact.find(c.req_id, "design")
        if a is None:
            raise ValueError("该需求尚无已定稿设计（design_rev 修订前提）")
        return json.dumps({
            "riskTier": r.risk_tier,
            "targetRepo": c.repos.requirement.get_target_repo(c.req_id),
            "md": a.markdown, "version": a.version,
        }, ensure_ascii=False)

    try:
        return await asyncio.to_thread(impl)
    except ValueError as e:
        return _err(str(e))


@function_tool
async def list_cases(wrapper: RunContextWrapper[TaskRunContext]) -> str:
    """列出已登记测试用例（gate_review 输入三件之一：双向追踪 points/verifyRefs/状态）。"""
    def impl() -> str:
        c = wrapper.context
        return json.dumps(c.services.cases.list(None, c.req_id, None, None, None), ensure_ascii=False)

    return await asyncio.to_thread(impl)


# ---------- 写入面（仅草稿与媒体——正式产物的唯一人道在平台「采纳/发布」） ----------


@function_tool
async def put_draft(wrapper: RunContextWrapper[TaskRunContext], type: str, md: str = "",
                    contentJson: str = "", mdPath: str = "", contentJsonPath: str = "") -> str:
    """写草稿——唯一合法的产物回传通道。type=digest_bundle|standard|cases|design|gate_issues|incremental_bundle。大内容优先走文件路径（磁盘中介，防上下文膨胀）：contentJsonPath/mdPath 指工作区内文件；inline 参数仅在内容很小时使用。

    Args:
        type: digest_bundle | standard | cases | design | gate_issues | incremental_bundle
        md: markdown 正文内联值（standard 用；给了 mdPath 则忽略）
        contentJson: payload JSON 内联值（digest_bundle/cases 用；给了 contentJsonPath 则忽略）
        mdPath: 工作区内 markdown 文件相对路径（可选）
        contentJsonPath: 工作区内 payload JSON 文件相对路径（可选，推荐）
    """
    def impl() -> str:
        c = wrapper.context
        md_val, json_val = md or "", contentJson or ""
        if not json_val and contentJsonPath.strip():
            json_val = _read_text(_resolve_in_workspace(c, contentJsonPath.strip()))
        if not md_val and mdPath.strip():
            md_val = _read_text(_resolve_in_workspace(c, mdPath.strip()))
        c.services.tasks.put_draft(c.req_id, type, {"md": md_val, "contentJson": json_val, "taskId": c.task_id})
        return json.dumps({"ok": True, "type": type}, ensure_ascii=False)

    try:
        return await asyncio.to_thread(impl)
    except Exception as e:  # noqa: BLE001  # 守卫拒绝（非法类型/终态锁 409 等）回灌给模型
        return _err(str(e))


@function_tool
async def upload_media(wrapper: RunContextWrapper[TaskRunContext], name: str, mediaId: str, mime: str, path: str) -> str:
    """上传原文媒体文件（图片/附件/画板缩略图）。mediaId 必须与 doc.media[].id 一致；path 为工作区内相对路径。

    Args:
        name: 文件名
        mediaId: 内容寻址 id（doc.media[].id）
        mime: MIME 类型
        path: 工作区内相对路径
    """
    def impl() -> str:
        c = wrapper.context
        data = _resolve_in_workspace(c, path).read_bytes()
        c.services.media.upload(c.req_id, name, mime, data, explicit_id=mediaId or None)
        return json.dumps({"ok": True, "mediaId": mediaId}, ensure_ascii=False)

    try:
        return await asyncio.to_thread(impl)
    except Exception as e:  # noqa: BLE001
        return _err(f"媒体读取失败：{path}：{e}")


# ---------- 文件工具（锁工作区 + 技能仓；带输出上限防上下文膨胀） ----------


@function_tool
async def read_file(wrapper: RunContextWrapper[TaskRunContext], path: str, max_chars: int = 24000) -> str:
    """读技能仓或工作区下的文本文件（技能手册、模板、脚本输入等）。路径未命中时按文件名在技能仓内自动检索。

    Args:
        path: 相对技能仓根或工作区根的路径（也可只给文件名）
        max_chars: 最多返回字符数（防撑爆上下文，默认与上限均 24000；截断续读用脚本拼接，不要反复重读全文）
    """
    def impl() -> str:
        c = wrapper.context
        try:
            p = _resolve(c, path)
            if not p.is_file():
                p = _fuzzy_find(c, path)  # type: ignore[arg-type]
        except Exception:
            p = _fuzzy_find(c, path)
        if p is None or not p.is_file():
            return _err(f"文件未找到：{path}。提示：技能文档在技能仓下，如 skills/sdlc-intent/SKILL.md、"
                        "skills/sdlc-intent/references/digest-template.md")
        s = _read_text(p)
        lim = 24000 if max_chars <= 0 else min(max_chars, 24000)
        head = "" if path.endswith(p.name) else f"（按文件名检索命中：{p}）\n"
        return head + s if len(s) <= lim else head + s[:lim] + f"\n...[截断，共 {len(s)} 字符]"

    return await asyncio.to_thread(impl)


@function_tool
async def write_file(wrapper: RunContextWrapper[TaskRunContext], path: str, content: str) -> str:
    """写文本文件到当前任务工作区（组装 doc.json、payload 等中间产物用）；父目录自动创建，内容上限 2MB。

    Args:
        path: 工作区内相对路径
        content: 文件全文内容
    """
    def impl() -> str:
        c = wrapper.context
        p = _resolve_in_workspace(c, path)
        data = (content or "").encode("utf-8")
        if len(data) > 2 * 1024 * 1024:
            raise ValueError(f"内容超限（>2MB）：{path}")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return json.dumps({"ok": True, "path": path, "bytes": len(data)}, ensure_ascii=False)

    try:
        return await asyncio.to_thread(impl)
    except Exception as e:  # noqa: BLE001
        return _err(f"写入失败：{path}：{e}")


# 段边界 sh 元字符（shlex punctuation_chars 字符集去掉重定向 <>——重定向是段内成员非命令边界）：
# 全部由此类字符组成的 token（| ; && |; ;; ( ) 等 punctuation run）判为段边界操作符
_SPLIT_OP_CHARS = ";|&()"


def _split_segments(cmd: str) -> list[str]:
    """run_command 分段器（review #23 引号感知）：shlex POSIX 令牌化——引号与反斜杠语义与
    sh -c 词法一致（引号内 ; | 是字面量不切段；`\"` 是转义字面引号、其后的 `;` 仍是真分隔符
    ——手写引号状态机在此处会误判「引号内」放后续命令逃逸白名单，评估 A1，必须走 shlex）。
    段边界 = 全由 ;|&() 组成的操作符 token（重定向 < > 属段内），重组后返回每段剥除段首
    重定向（> file / < in——shlex 下操作符与目标各为独立 token）的段首命令；纯重定向段
    返回 ""（sh 语义下仅创建/打开文件无命令面，与旧版 continue 口径一致）。未闭合引号
    shlex 抛 ValueError、空段（操作符悬空/连打/子 shell 括号）显式抛 ValueError——一律
    fail-closed，由调用方拒绝。"""
    lex = shlex.shlex(cmd, posix=True, punctuation_chars=True)
    lex.whitespace_split = True  # punctuation_chars=True 已自动置位，显式记档意图
    lex.commenters = ""  # '#' 不剥注释行——词首 # 在 sh 是注释，留作普通 token 由段首校验处置
    tokens = list(lex)

    def _op(t: str) -> bool:
        return bool(t) and all(ch in _SPLIT_OP_CHARS for ch in t)

    segs: list[list[str]] = []
    cur: list[str] = []
    for tok in tokens:
        if _op(tok):
            if not cur:
                raise ValueError(f"命令存在空段：{cmd}")
            segs.append(cur)
            cur = []
        else:
            cur.append(tok)
    if not cur:
        raise ValueError(f"命令存在空段：{cmd}")
    segs.append(cur)
    out: list[str] = []
    for seg in segs:
        i = 0
        while i < len(seg) and seg[i] in (">", "<"):  # 段首重定向：跳过操作符及其目标 token
            i += 1
            if i < len(seg) and not _op(seg[i]):
                i += 1
        out.append(seg[i] if i < len(seg) else "")
    return out


async def run_command(wrapper: RunContextWrapper[TaskRunContext], command: str, timeout_seconds: int = 300) -> str:
    """docstring 由 _CMD_DOC 运行时赋值（见模块顶部说明——白名单与 LLM 可见面永远一致）。"""
    def impl() -> str:
        import subprocess

        c = wrapper.context
        cmd = (command or "").strip()
        if not cmd:
            raise ValueError("命令为空")
        for bad in ("`", "$(", "\n"):
            if bad in cmd:
                raise ValueError("命令含禁止记号（反引号 / $() / 多行），请改写为分段白名单命令")
        for first in _split_segments(cmd):  # 引号感知分段（#23）；空段/未闭合引号由分段器抛 ValueError
            if first and first not in _CMD_ALLOW:
                raise ValueError(f"段首命令不在白名单（{'/'.join(sorted(_CMD_ALLOW))}）：{first}")
        try:
            proc = subprocess.run(["sh", "-c", cmd], cwd=c.workspace, capture_output=True,
                                  text=True, timeout=timeout_seconds if timeout_seconds > 0 else 300)
        except subprocess.TimeoutExpired as e:
            raise ValueError(f"命令超时：{cmd}") from e
        out = (proc.stdout or "") + (proc.stderr or "")
        lim = 20000
        return json.dumps({"exit": proc.returncode, "output": out if len(out) <= lim else out[:lim]},
                          ensure_ascii=False)

    try:
        return await asyncio.to_thread(impl)
    except ValueError as e:
        return _err(str(e))
    except Exception as e:  # noqa: BLE001
        return _err(f"命令执行失败：{e}")


# 先赋动态 docstring 再裸调用装饰（@function_tool 无括号形态等价 function_tool(func)，agents SDK
# 同一代码路径；ALL_TOOLS 在下方引用的是重绑后的 Tool 实例）——拆开写是 docstring 动态化的最小改法
run_command.__doc__ = _CMD_DOC
run_command = function_tool(run_command)


ALL_TOOLS: list[Tool] = [get_requirement_card, list_clarify_items, list_req_comments, get_digest_bundle,
             get_design_doc, list_cases, put_draft, upload_media, read_file, write_file, run_command]
