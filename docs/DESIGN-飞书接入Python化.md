# DESIGN：飞书接入 Python 化（runner 下线前置）

> 目标：让进程内 agent（openai-agents + DeepSeek）具备完整飞书原文导入能力，为 runner 进程下线铺路。
> 状态：设计定稿（2026-10-28，经对抗审查修订 v2），未实施。
> 关联：`docs/EXEC-PLAN-K8S部署与中间件改造.md`（部署总计划，本方案是其「任务通道改造」前置）。

## §0 背景与教训

现状：飞书原文导入只存在于外置 runner 通道——prompt 里写死 3 条 `lark-cli` 命令，由 claude CLI
headless 执行。runner 准备下线（2026-10-28 拍板），进程内 agent 必须补齐同等能力。

v1 方案被对抗审查否决（5 P0 + 5 P1，全部经代码验证属实）。核心教训：**lark-cli 不是薄封装**——
它做了 block→XML 序列化、评论筛选（规则未公开）、用户 ID→名字解析、附件/画板特殊下载。

| v1 致命问题 | 事实 |
|---|---|
| 附件全丢 | 误写 `23=grid`，实际 **23=file（附件）**。样本里 5 个附件含 30MB m4a、原型 zip、3 个 html |
| manifest 跑不通 | ① `parse_doc.py:1022` 必须传 `--download` 才进下载 ② `download_media` 第一行查 lark-cli，无则全 failed return ③ 白板 `sha256("wb:"+token)`（`parse_doc.py:633`）与图片 `sha256(token)` 规则不同 |
| 守恒自检恒真 | `guard.textSha` 由同一份 XML 算出（`services/block.py:94`），序列化器漏内容时两侧同时缺，校验照样通过——自检只保证「XML 内部自洽」，不保证「与源等价」 |
| 验收不可执行 | 与 lark-cli 逐块对比无冻结基线且和无 CLI 环境互斥；PATH 清理绕不过 `_find_lark_cli()` 的 `~/.nvm/*/bin` glob |
| 凭据注入缺失 | `TaskRunContext`（`agent/context.py`）无 Settings；`ALL_TOOLS`（`agent/tools.py:390`）是模块级常量；pydantic-settings 读 `.env` 不进 `os.environ` |

修订原则：
1. **fail-closed 优于静默降级**——未知块类型必须报错，不允许跳过
2. **独立 oracle**——用 blocks API 原始响应做三方比对，不依赖「自己校验自己」
3. **以业务正确性为准，不复刻 lark-cli 的未知行为**

## §1 POC 已验证的事实（2026-10-28，样本：REQ-2026-1165 的源文档）

| 事实 | 验证结果 |
|---|---|
| 应用凭据（tenant token）可读文档 | ✅ 784 块 100% 带 block_id |
| lark-cli XML 的 `id` 属性 = blocks API 的 `block_id` | ✅ 两条链路数据同源 |
| 评论锚点：`file_comment.list` + `need_relation=true` 才返回 `extra.content_anchor_id` 与 `relation.content_deleted` | ✅ **SDK `FileComment` 模型无这两个字段**（unmarshal 丢弃）→ 必须原始 HTTP |
| 评论筛选：29 条 → 8 条 `content_deleted=true` 排除 → 21 条存活中 lark-cli 只收 11 条 | ⚠ 剩余规则逆向失败（非时间/非引文/非 whole），**不复刻** |
| `<msg user="沈超杰">` 需真实用户名，API 只给 `user_id` | 需 `contact.v3.user.get`（额外 scope） |
| 图片下载 `drive.media.download` / 画板 `board.whiteboard.download_as_image` | ✅ 画板走专用 API（media.download 不行） |
| 附件块 `<figure><source name mime size token/>` | ✅ 5 个，与 block_type=23 数量对上 |
| `raw/doc.md` 与 `raw/comments.json` 在 source_import 流程中无消费方 | ✅ 省 Markdown 序列化器 |
| `mediaId = "m-" + sha256(token)[:12]`（图片/附件）；白板加 `"wb:"` 前缀 | ✅ `parse_doc.py:623/633` |

## §2 架构

```
blocks API ──┬─► docx.py 序列化 ──► content.xml ──┐
             │                                      ├─► parse_doc.py ──► doc.json
             ├─► oracle.py 独立校验 ──► 不一致即失败  │
             │                                      │
comments API ─► comments.py ──► refmap.json ───────┘
media API ───► media.py ──► intake/media/ + manifest.json
```

**oracle**（解决「守恒自检恒真」）：序列化完成后，用 blocks API 的**原始响应**独立计算三组事实
——块类型计数（排除容器块后的叶子块）、按文档序拼接的 `text_run.content` 文本流、media token 集合
——与序列化结果 + 最终 doc.json 三方比对，任一不一致 → 任务立即失败并输出差异。
**必须做成运行时门禁而非单测**：这是唯一能发现「序列化器漏内容」的检查。

## §3 块类型映射（按官方枚举重写，样本计数交叉验证）

| block_type | 官方名 | XML 标签 | 样本数 | 状态 |
|---|---|---|---|---|
| 1 | page | `<title>` | 1 | ✅ |
| 2 | text | `<p>` | 194 | ✅ |
| 3/4/5/6 | heading1-4 | `<h1>`~`<h4>` | 4/7/15/12 | ✅ |
| 7-11 | heading5-9 | `<h5>`~`<h9>` | 0 | 实施步 0 补样本 |
| 12 | bullet | `<ul><li>` | 221 | ✅ |
| 13 | ordered | `<ol><li>` | 100 | ✅ |
| 14 | code | `<pre><code>` | 0 | 补样本 |
| 15 | quote | `<blockquote>` | 0 | 补样本 |
| 17 | todo | `<ul class="todo"><li>` | 0 | 补样本 |
| 19 | callout | `<callout>` | 4 | ✅ |
| 22 | divider | `<hr>` | 0 | 补样本 |
| **23** | **file（附件）** | **`<figure><source …/>`** | **5** | ✅（v1 写错为 grid） |
| **24** | **grid** | **`<grid>`** | **10** | ✅ |
| **25** | **grid_column** | **`<column>`** | **20** | ✅ |
| 27 | image | `<img src=token>` / `<figure><img>` | 42 | ✅ |
| 30 | sheet | `<sheet>` | 0 | 补样本 |
| 31 | table | `<table><colgroup><thead><tbody>` | 9 | ✅ |
| 32 | table_cell | `<td>`/`<th>` | 128 | ✅ |
| 33 | view | 待确认 | 4 | **待确认** |
| 43 | board | `<whiteboard token>` | 2 | ✅ |

硬性要求：
- 实施第一步用官方枚举核对完整表（open.feishu.cn document-block/list 文档）
- 序列化器遇到**未覆盖的 block_type 直接抛错**（错误带类型号与块 ID），任务立即 failed——不允许静默跳过
- 每个已覆盖类型一份 fixture（真实文档裁剪）；内联元素 `mention_user`/`mention_doc`/`equation`/
  `inline_block`/`reminder`/`file` 必须逐一确认降级策略（样本只覆盖 `text_run` + `inline_block`，
  lark-cli XML 中无 mention/cite/a 标签——其行为待样本对照）

## §4 评论策略（✅ 拍板：可解释规则）

```
收录条件 = content_deleted == false AND content_anchor_id 非空
```

- 比 lark-cli 多收约 10 条（锚定在现存块上的评论）→ 信息更全；先宽后收，上线后观察噪音再收紧
- 已删块评论（8 条）排除——锚定不存在的块，落库也是孤儿
- 用户名解析失败降级为 `user_id` 前 8 位 + 摘要计数告警
- **锚点有效性自行校验**：平台 `_comment_chain`（`block.py:547`）不校验 anchorBlockId，
  comments.py 必须确认 anchor ∈ blocks 集合，不在则丢弃并计数
- refmap 的 `data` 是 XML 字符串（`<comment comment-id block-id><quote/><msg user>…</msg></comment>`），
  按 `parse_doc.py:640-664` 契约生成，key 须匹配 `c\d+`

## §5 媒体策略

| 类型 | block_type | 下载 API | mediaId 规则 |
|---|---|---|---|
| 图片 | 27 image | `drive.v1.media.download` | `m-` + `sha256(token)[:12]` |
| 画板 | 43 board | `board.v1.whiteboard.download_as_image` | `m-` + `sha256("wb:"+token)[:12]` |
| 附件 | 23 file | `drive.v1.media.download` | `m-` + `sha256(token)[:12]` |

工程约束：
- 有界并发（4~8 线程）+ 单请求超时 120s + 总预算 10min，超预算整体失败并列未完成清单
- 文件名 `<mediaId>.<ext>`，扩展名由 mime 决定（不依赖 HTTP header 的 file_name——lark-cli 会自动
  追加扩展名，新链路不能复刻这个隐含行为）
- **大附件阈值（✅ 拍板 10MB）**：`size > SDLC_FEISHU_MEDIA_MAX_BYTES`（默认 10MB）只登记元数据不下载，
  manifest 标 `skipped: "size-exceeded"`；30MB m4a 走此路径，原型 zip/html（<10MB）正常下载。
  平台 `media_max_bytes=64MB` 可容纳，阈值仅控导入耗时与存储
- manifest 结构：`{token: {"path":…, "name":…, "mime":…}}`

`parse_doc.py` 改动（**精确 diff，不是「+15 行」**）——先认领再查 lark-cli：

```python
def download_media(media_list, media_dir, manifest=None):
    manifest = manifest or {}
    missing = [m for m in media_list if m["token"] not in manifest]
    lark = _find_lark_cli() if missing else None
    if missing and not lark:
        raise SystemExit("manifest 不完整且无 lark-cli：tokens=…")   # 显式失败，不静默降级
    # manifest 命中 → 读本地文件填充 bytes/sha256/file/status；miss 才走 lark-cli 原逻辑
```

prompt 第 3 步必须带 `--download`（`parse_doc.py:1022` 只在该 flag 下进下载）。

## §6 依赖注入（v1 完全遗漏）

- `TaskRunContext` 加 `feishu: FeishuClient | None`（executor 按 Settings 构造注入）
- `ALL_TOOLS` 模块级常量改为 `build_tools(feishu) -> list[Tool]` 工厂，executor 按能力取列表
- `feishu_fetch` 工具从 `wrapper.context.feishu` 取客户端（凭据不进 `os.environ`，不进 LLM 上下文）
- prompt 分支与工具注册**联动**：无 feishu 能力时 prompt 不生成飞书分支——不能只藏工具，否则模型
  fallback 到 `_CMD_ALLOW` 里仍留着的 lark-cli（`tools.py:27`），开发机假绿、服务器失败

## §7 失败与重试

- fetch 第一步取 tenant_token + 探测调用；权限类错误映射新 reason **`feishu-forbidden`**
- `feishu-forbidden` **不加入 `_RETRYABLE_REASONS`**（`task.py:46`，现含 timeout/agent-exception 等）
  → 重试接口直接 409，不烧 token；必须在错误第一次发生时就归类，不能等超时兜底
- 权限错误文案提示「文档需分享给应用」（Askly 应用凭据共用期，知识库授权范围外文档会命中此分支）

## §8 改动清单

| 文件 | 动作 |
|---|---|
| `src/platform_sdlc/integrations/feishu/{__init__,client,wiki,docx,comments,media,oracle,fetch}.py` | 新增（约 900 行，序列化器占大头） |
| `src/platform_sdlc/agent/context.py` | `TaskRunContext` 加 `feishu` 字段 |
| `src/platform_sdlc/agent/tools.py` | `ALL_TOOLS` → `build_tools()` 工厂；`feishu_fetch` 工具 |
| `src/platform_sdlc/agent/executor.py` | 构造 FeishuClient、按能力取工具列表 |
| `src/platform_sdlc/agent/prompts.py` | source_import 飞书分支改造 + 能力联动 |
| `src/platform_sdlc/config.py` | `feishu_app_id` / `feishu_app_secret` / `feishu_media_max_bytes` |
| `src/platform_sdlc/services/task.py` | `feishu-forbidden` 保持不可重试（不加白名单） |
| `pyproject.toml` + `uv.lock` | **加 `lark-oapi`**（v1 遗漏） |
| `README.md` | 环境变量、两仓版本对应关系 |
| `claude-sdlc-skills/skills/sdlc-intent/scripts/parse_doc.py` | `--media-manifest`（先认领后查 CLI） |
| `tests/test_feishu_*.py` | 序列化器 fixture + oracle + 评论规则测试 |

凭据来源（✅ 拍板：业务闭环优先）：先共用 Askly 应用（`LARK_APP_ID/LARK_APP_SECRET`），本地写
server 仓 `.env`，K8s 走 Secret；后续换 SDLC 独立应用只改配置。

## §9 实施步骤与工期（✅ 拍板：接受 11~12 人日）

| # | 步骤 | 人日 |
|---|---|---|
| 0 | 冻结黄金资产：lark-cli 产 `golden/{content.xml,refmap.json,doc.json}` + blocks API 落盘 `golden/blocks.json` + 记录 `revision_id`；再找 2~3 篇覆盖 code/quote/todo/divider/sheet 的文档扩样本 | 1 |
| 1 | `docx.py` 序列化器（官方枚举全覆盖 + fail-closed）+ fixture 测试 | 3~4 |
| 2 | `oracle.py` 三方比对 + 集成 fetch | 1 |
| 3 | `comments.py`（原始 HTTP need_relation + 用户名解析 + 锚点校验） | 1.5 |
| 4 | `media.py`（三类媒体 + 并发/超时/预算）+ client/wiki/fetch | 1.5 |
| 5 | `parse_doc.py --media-manifest` + 离线黄金比对通过 | 1 |
| 6 | 依赖注入改造（context/tools 工厂/executor/prompts/config） | 1 |
| 7 | 端到端 + 验收 | 1 |

版本门禁：`fetch.py` 启动探测 `parse_doc.py --help` 含 `--media-manifest`，缺失明确报「skills 仓
版本不匹配」（旧版脚本未知参数 exit 3 会误报「用法错误」）。

## §10 验收（重构版）

| # | 检查项 | 判据 |
|---|---|---|
| 1 | 序列化器 fixture | 每个块类型一份 fixture，`uv run pytest tests/test_feishu_*.py` 全绿 |
| 2 | **离线黄金比对** | 与冻结 `golden/doc.json` 逐块比 `id/type/text/caption/path` + media id 集合 + comment 块四元组；**固定 revision_id**，不依赖 lark-cli 运行时 |
| 3 | oracle 门禁有效性 | 故意破坏序列化器（删一个块）→ 任务必须失败（证明 oracle 真的能抓漏） |
| 4 | 平台落库 | 建卡 → source_import → `GET doc.json` 与 golden 一致 |
| 5 | 媒体完整性 | 三类媒体条目齐全；≤10MB 附件全 `status=ok`；>10MB 标 `skipped:size-exceeded` 不阻断 |
| 6 | **真无 lark-cli 环境** | 容器内跑（PATH 清理无效——`_find_lark_cli` 会 glob nvm）；或 parse_doc 加 `--no-lark-cli` 开关强制启用 |
| 7 | 重试行为 | 无权限文档 → failed 且 reason=`feishu-forbidden`，重试 409 |
| 8 | 回归 | `scripts/verify-all.sh` 全绿 |

## §11 遗留风险（已接受）

- 评论比 lark-cli 多收约 10 条 → 有噪音再收紧
- >10MB 附件不入库 → 调 `SDLC_FEISHU_MEDIA_MAX_BYTES` 即可
- 块类型 view(33) 与 heading5-9/code/quote/todo/divider/sheet 样本未覆盖 → 步骤 0 扩样本；仍未覆盖者 fail-closed
- 与 Askly 共用凭据 → Askly 权限变动会波及 SDLC；`feishu-forbidden` fail-fast 缓解，独立应用是终态

## §12 后续阶段（不含本方案）

MySQL 改造、容器化 + ACK、runner 下线——见 `docs/EXEC-PLAN-K8S部署与中间件改造.md`。
本方案验收通过是 runner 下线的前置条件。

## §13 实施期修订（2026-10-28，步骤 0 完成后）

**架构变更（✅ 用户拍板）：序列化器退役，fetch 改用 docs_ai 服务端渲染。**

步骤 0 冻结黄金资产时实锤：lark-cli 的 `docs +fetch --doc-format xml` 后端 =
`POST /open-apis/docs_ai/v1/documents/{id}/fetch`（空请求体即可；XML 由**服务端**渲染）。
5 个黄金样本验证：3 个（无评论文档）与 lark-cli content.xml **逐字节一致**；2 个（含评论）
仅缺 `comment-refs` 属性——该属性是 lark-cli **客户端本地注入**的（帮助文本「local comment
anchors」自证；字节数对账：308≈11 评论×27B、1916≈89 评论×21B），注入源 = 评论 API 的
`extra.content_anchor_id`，本就是 comments.py 的职责面。

影响（替代 §2 架构图 / §8 改动清单 docx.py 行 / §9 步骤 1）：
- `docx.py` 从「约 900 行序列化器」变薄层：fetch XML + comment-refs 注入（锚点来自评论 API）
- blocks API 自写序列化路线的四大不可收敛缺口全部消失：编号风格（1./a./i.，API 无此字段）、
  mention 用户名（服务端解析带出）、图片 alt 描述（blocks API 无来源）、mermaid 画板源码
  （board 内容实体只有 token）——**blocks API 路线中 alt 与 mermaid 完全无解，黄金比对必须
  豁免**，docs_ai 路线天然逐字节对齐
- §2 的 oracle 不变且更有意义：XML（docs_ai）与块数据（docx v1 blocks）是**两个独立服务端链路**，
  三方比对（XML→doc.json vs blocks 原始响应 vs 黄金）真正双源互证
- 风险记档：docs_ai v1 属较新接口、公开文档缺位；lark-cli 1.0.95 生产在用、走正式网关 +
  tenant token，可接受；接口变更时由 oracle 门禁第一时间暴露

**步骤 0 附带产出（parse_doc 五连修，姊妹仓 be66ca8/0bd0674/f71ea40/5b4f306）**：
标题内嵌 grid 分栏丢图 / mermaid 画板 text 丢失 / code·quote·todo 三块型全丢（lark-cli 真实
形态：`<pre><code>`、`<blockquote><p>`、`<checkbox done>`——§3 表中 todo 的 `<ul class="todo">`
猜测证伪）/ 段中 br 换行丢失 / sheet 块静默丢失（守恒恒真盲区，oracle 补位）。黄金资产 =
`tests/fixtures/feishu/`（jiaoyan-report 主样本 / region-score / jiaoyan-background /
aijiyao-design / blocktype-synthetic——映射表全类型覆盖），冻结脚本
`scripts/freeze_feishu_golden.py`（revision 一致性门，可重跑）。

---

*设计依据：2026-10-28 POC 实测（样本 REQ-2026-1165 源文档，784 块/29 评论/42 图/5 附件/2 画板）+
对抗审查（5 P0 + 5 P1 全部代码验证）+ 三项拍板（可解释评论规则 / 10MB 附件阈值 / 11~12 人日）；
§13 为同日实施期修订（黄金样本实证 + docs_ai 架构拍板）。*
