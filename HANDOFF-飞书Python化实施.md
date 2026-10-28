# HANDOFF：飞书接入 Python 化实施交接（2026-10-28）

> 给实施会话的自足交接。设计已定稿**未实施一行代码**。权威文档 = `docs/DESIGN-飞书接入Python化.md`（v2，
> 经对抗审查修订），先通读它再动手；本文件只补「文档里没有的会话事实」与实施顺序。

## 一、必读（按序）

1. `docs/DESIGN-飞书接入Python化.md` —— 权威设计（架构/映射表/评论规则/媒体策略/验收 8 项）
2. `CLAUDE.md` —— 项目铁律（分层单向、SQLite 单连接、出参 camelCase、三处同步等）
3. `docs/EXEC-PLAN-K8s部署与中间件改造.md` §0/§8-4 —— 本改造在总计划中的位置（runner 下线前置）

## 二、已冻结的决策（不要重新讨论）

| 决策 | 内容 |
|---|---|
| 评论规则 | 可解释规则：`content_deleted==false 且 anchor 非空`；比 lark-cli 多收约 10 条属预期，先宽后收 |
| 大附件 | 10MB 阈值（`SDLC_FEISHU_MEDIA_MAX_BYTES`，默认 10MB），超过只登记元数据标 `skipped:size-exceeded` |
| 工期 | 11~12 人日，按设计 §9 步骤 0→7 顺序 |
| 凭据 | **先共用 Askly 应用**（业务闭环优先）；值在 `~/PycharmProjects/Askly/.env` 的 `LARK_APP_ID` / `LARK_APP_SECRET`（读键值即可，别把 secret 写进任何入库文件） |
| 模型 | 进程内 agent 用 DeepSeek |
| runner | 过渡期保留兼容（lark-cli 路径不删）；本方案验收通过后才下线 |
| RocketMQ | 降级为轮询，与本改造无关 |

## 三、会话事实（文档里没有的）

**测试文档**（现网真实数据，POC 全程用它）：
- 需求卡：`REQ-2026-1165` / `REQ-2026-1166`，source_url = `https://hailiang.feishu.cn/wiki/O86FwWwQViyikfkaz9jc7zUunBc`
- wiki token `O86FwWwQViyikfkaz9jc7zUunBc` → docx `PL5ndphOGoIoU5xQwwCcvcWWnIX`（标题「鸿儒教研AI纪要&教研报告」）
- 规模：784 块（100% 带 block_id）/ 29 评论 / 42 图 / 5 附件（含 30MB m4a）/ 2 画板 / revision_id=9639

**lark-oapi SDK 现状**：只装在 Askly 的 venv（`~/PycharmProjects/Askly/.venv`，Python 3.11，
版本 1.7.5）。**本仓还没加依赖**——实施第 4 步时 `uv add lark-oapi`。临时验证脚本可用
`cd ~/PycharmProjects/Askly && .venv/bin/python <script>` 跑（POC 就是这么跑的）。

**POC 遗留**：`/tmp/sdlc-feishu-poc/`（probe*.py + ref/fetch.json 106KB 黄金样本）。**/tmp 会丢**——
实施步骤 0 第一件事：确认它还在；不在就用 lark-cli 重新拉（命令在 DESIGN §1 的事实行里有）：
`lark-cli docs +fetch --doc PL5ndphOGoIoU5xQwwCcvcWWnIX --doc-format xml --detail with-ids --format json`
冻结到 `tests/fixtures/`（注意脱敏判断：正文是现网业务文档，入库前问用户）。

**已踩过的坑（v1 方案 5 个 P0，实施时别再踩）**：
1. block_type **23=file 附件**（`<figure><source>`），24=grid，25=grid_column——别搞混
2. `parse_doc.py:1022` 必须 `--download` 才进下载；`download_media` 第一行就查 lark-cli——manifest
   必须先认领再查 CLI（精确 diff 在 DESIGN §5）
3. 白板 mediaId 是 `sha256("wb:"+token)`，图片/附件是 `sha256(token)`（`parse_doc.py:623/633`）
4. 守恒自检是自指校验，抓不到序列化器漏内容——oracle 必须做成运行时门禁（DESIGN §2）
5. `TaskRunContext` 无 Settings、`ALL_TOOLS` 是模块级常量、pydantic `.env` 不进 os.environ——
   依赖注入按 DESIGN §6 改

**评论的两个隐藏点**：
- 锚点/删除态要**原始 HTTP** `GET /open-apis/drive/v1/files/{token}/comments?need_relation=true`
  （SDK `FileComment` 模型没有 `extra.content_anchor_id` / `relation.content_deleted` 字段）
- `<msg user>` 要真实姓名，API 只给 `user_id` → 需 `contact.v3.user.get`（注意应用可能没这个 scope，
  失败降级 user_id 前 8 位——DESIGN §4）

**验收 #6 的正确做法**：PATH 清理没用（`_find_lark_cli()` 会 glob `~/.nvm/*/bin`）——用容器跑或给
parse_doc 加 `--no-lark-cli` 开关。

## 四、实施顺序（DESIGN §9，照抄）

步骤 0 冻结黄金资产（+扩样本找 code/quote/todo/divider/sheet 文档）→ 1 序列化器 → 2 oracle →
3 评论 → 4 媒体+client → 5 parse_doc manifest → 6 依赖注入接线 → 7 端到端验收（DESIGN §10 八项）。

**每步的质量门**：`uv run pyright && uv run ruff check src tests` + `uv run pytest`；
涉及 prompt 改动后跑 `scripts/verify-all.sh`（需服务已启动，会自动暂停 runner/agent）。

**改姊妹仓（claude-sdlc-skills）的纪律**：`--media-manifest` 是可选参数，不传时行为必须与现在
完全一致（runner 通道还在用它）；改完在 skills 仓跑 parse_doc 自测（`--self-test`）。

## 五、边界

- 不做：飞书写操作、UI、MySQL、容器化、runner 删除（这些在 EXEC-PLAN 后续阶段）
- 争议回退点：若序列化器与 lark-cli 的差异无法收敛（黄金比对过不了），停下来找用户裁决，
  不要擅自放宽验收判据
