# SDLC 平台 · v4 需求平台权威化 · 实施执行计划（EXEC-PLAN-v4）

> 状态：定稿待实施。日期：2026-10-13。
> 权威输入：`docs/DESIGN-v4-需求平台权威化.md`（设计定稿，§12 三期切分 + 附录决策清单）——本计划逐条映射其条款，不引入新设计。
> 契约权威：人读 = `docs/ER图与业务流程.md` §13；机器可读 = `docs/openapi.json`（CI 快照门）。DEV-PLAN.md 已归档。

## 0. 批次纪律

- 每批次 ≈ 一个 commit（中文主题，如 `v4-1：块表/评论表落库地基与存量迁移`），批内自洽、批间串行。
- 每批验收底座：`uv run ruff check src tests` + `uv run pyright` + `uv run pytest -q` 全绿；涉及契约面的批次追加 `node scripts/export-openapi.mjs` 并随批 commit `docs/openapi.json`。
- 涉及任务模板的批次追加 `uv run python scripts/check_task_templates.py`（v4-3 新增第五道 CI 门）。
- 黑盒验收：`scripts/verify-all.sh` 随批全绿；`node scripts/verify-agent.mjs`（需 SDLC_VERIFY_AGENT=1 + 模型余额）v4-5 后纳入。
- 迁移批次上线前 `scripts/backup.sh` 快照 `data/platform.db`。

## 1. 总批次表（18 批）

| 期 | 批次 | 一行摘要 | 依赖 |
|---|---|---|---|
| 一 | **v4-1** | req_block/req_comment 建表 + clarify_item.origin 列 + 存量迁移（doc JSON 搬块表/origin 回填/anchor 健康度登记），行为零变化 | — |
| 一 | **v4-2** | 块模型读侧：`{blocks,media}` 同形状组装 + `GET …/doc.json`、`GET …/comments.json` 端点 + detail.doc 切源（空表回退 artifact）+ get_requirement_card 双源 | v4-1 |
| 一 | **v4-3** | source_import 任务通道：类型注册+分源封存守卫、双分支模板双写、`finish` 服务端直读落库、`tasks/<task_id>` 定位、工作区根 meta 下发、noDraftTypes 两处 selfCheck 跳过、CI 三处一致性守护 | v4-1、v4-2 |
| 一 | **v4-4** | L2 编排与导入守卫：建卡自动排 source_import、质检启动守卫（块表空→failed 引导）、存量 text 卡首导补排、新增 verify-v4.mjs | v4-3 |
| 一 | **v4-5** | 质检输入切换：quality_check 纯分析读块模型（拉取序列与 text 特殊分支退役）+ 只读评论工具（comments.json 兼容）+ verify-agent 链路改造 | v4-2、v4-4 |
| 一 | **v4-6** | 一期收尾：契约与红线文档同步（ER 图/DESIGN-v3 §5/CLAUDE.md/openapi 复核） | v4-1~5 |
| 二 | **v4-7** | 块级直接编辑 API：全量块数组→服务端 diff（LIS）→块表应用；blk-<ulid> 自实现；编辑×质检互斥守卫 | 一期 |
| 二 | **v4-8** | 评论系统 API：块级/卡片级 CRUD+回复串+resolve；升级澄清项（L 系编号+src_comment_id 回链+quote/anchor 带入）；未解决角标数据面 | v4-1 |
| 二 | **v4-9** | push_artifacts 按来源合并：重推删 skill+incremental 行、保留 local 行；origin 出参 | v4-8 |
| 二 | **v4-10** | 手动「重新质检」收口 + 窗口 1 黑盒闭环（编辑→重质检→产物锚定新块） | v4-7、v4-9 |
| 三 | **v4-11** | 变更单数据面：req_change/req_change_item 表、draft 保存（diff 重算）、影响面预览（递归 CTE、章节 seq 区间读时解析、impact 冻结 block_id） | 二期 |
| 三 | **v4-12** | confirm 事务：items 应用+乐观锁、content_rev+1、published→changed（表外通道）、澄清重开、on_change_confirmed 钩子 | v4-11 |
| 三 | **v4-13** | 放弃变更：全量逆向回滚（move 逆、pending-only 恢复、rev 不回退、aborted） | v4-12 |
| 三 | **v4-14** | incremental_check 任务 + incremental_bundle 草稿 + selfCheck 机械校验 + approve 章节级机械合并 + on_merged 钩子 | v4-12 |
| 三 | **v4-15** | changed 守卫矩阵全量落地（作答/任务/草稿/编辑关闭/发布双重守卫/gate.open 拒 changed）+ on_answered 窗口化 | v4-12、v4-14 |
| 三 | **v4-16** | GR-3 扩展：baseline 加 reqRev、change_status 改查变更单（不比 rev）、横幅数据面 | v4-12 |
| 三 | **v4-17** | 存量对账重导：kind=reconcile 系统变更单 + kind=rebaseline 放行一次全量 + 评论幂等 upsert | v4-12、v4-14 |
| 三 | **v4-18** | skill 仓迁移（跨仓 claude-sdlc-skills，DESIGN-v4 §13 全清单） | v4-15 |

二期/三期为框架级（交付判据与范围要点见 §3/§4）；文件级清单在前期落地后按一期同格式细化，细化批次表须走查对齐当期代码现状。

## 2. 一期批次详情

### v4-1 块表/评论表落库地基 + 存量迁移

**设计依据**：DESIGN-v4 §2.2（表结构/seq 间隔 1000/软删/blk-ID 禁内容哈希）、§4.2（req_comment）、§6.3（origin 列）、§10（doc JSON→块表一次性搬、块 ID 原样、origin 回填 'skill'、anchor 健康度校验）。

| 动作 | 文件 | 内容 |
|---|---|---|
| 修改 | `src/platform_sdlc/schema.sql` | 建表 `req_block`（§2.2 逐列）、`req_comment`（§4.2 逐列）；索引 `idx_req_block_req_seq(req_id,seq)`、`idx_req_comment_req(req_id)`、`idx_req_comment_ext(req_id,external_id)` |
| 修改 | `src/platform_sdlc/migrations.py` | ① `_COLUMNS` += clarify_item.origin；② `_TABLES` += 两表 DDL；③ 账本三步骤：`backfill_clarify_origin`（UPDATE … WHERE origin=''，幂等）、`migrate_doc_artifact_to_blocks`（doc artifact content_json → 非 comment 块按序 seq=(i+1)*1000 落块表，块 ID 原样；comment 块转 req_comment（origin='feishu'，external_id 取 attrs 中飞书 comment-id——**实施时对 REQ-2026-001 实际 doc JSON 核对字段名**）；doc artifact 行保留归档不删）、`audit_anchor_health`（digest/audit/fr_point/clarify_item 的 anchor ∉ 块表 → log.warning 登记；运行面强拒随三期 §5.7 落地） |
| 修改 | `src/platform_sdlc/models.py` | ReqBlock/ReqComment 实体；ClarifyItem 加 origin |
| 新增 | `src/platform_sdlc/repositories/req_block_repo.py` | replace_all / list_by_req（deleted='' ORDER BY seq）/ has_edit_trace（version>1 或未软删 blk- 前缀）/ empty（EXISTS） |
| 新增 | `src/platform_sdlc/repositories/req_comment_repo.py` | insert_many / list_by_req / delete_feishu / has_platform |
| 修改 | `src/platform_sdlc/repositories/__init__.py`、`clarify_item_repo.py` | Repos 注册；insert 带 origin 列 |
| 修改 | `src/platform_sdlc/services/requirement.py` | push_artifacts 重建行携带 `origin='skill'`（**关键同期点**：否则首次重推把回填冲零，§10 点名风险） |
| 修改 | `tests/test_repos.py` 或新增 `tests/test_v4_blocks.py` | 迁移三断言组 |

要点：派生字段（path/ordinal）不落库读时推导（§2.2）；comment 不入块表（§2.2 表注）；已发布卡只迁存储不改状态（§10）；一期出参不加 origin 键（契约零变化，origin 出参随二期 v4-9）。

**验收**：pytest（迁移幂等连跑两次/块 ID 原样 seq 间隔 1000/comment 行入评论表/origin 回填+重建行携带/anchor 健康度 caplog 断言）；ruff+pyright 绿；verify-all 全绿（零行为变化）；真库演练 backup.sh → 启动观察 REQ-2026-001 搬运计数。

### v4-2 块模型读侧——同形状导出 + 消费面切源

**设计依据**：§2.3（ORDER BY seq 一条 SELECT 组装 {blocks,media}、消费三方零改动、doc artifact 退役裁决、get_requirement_card 双源+sourceText 兼容）、§3（comments.json 兼容格式读时组装）、§12 一期交付「doc.json/comments.json 文件端点 curl 直落」。

| 动作 | 文件 | 内容 |
|---|---|---|
| 新增 | `src/platform_sdlc/services/block.py` | BlockService：`doc_payload(req_id)` → {blocks,media}（非 comment 块展平 + req_comment 按 block_id 拼回 comment 块形态插宿主后 + 宿主 attrs 补 comment-refs——**兼容形状以 skills 仓 resolve_anchors.py/parse_doc.py 消费代码为契约核对源，实施时逐字段比对**）；`comments_payload(req_id)` → comments.json 兼容格式 |
| 修改 | `src/platform_sdlc/services/__init__.py`、`main.py` | 注册 BlockService；`Services.create` 签名加 workspace_root（main 传 paths.workspace_root，tests 传 tmp_path）——为 v4-3 落库预置 |
| 修改 | `src/platform_sdlc/routers/requirements.py` | 新增 `GET /api/requirements/{req_id}/doc.json`、`GET …/comments.json` |
| 修改 | `src/platform_sdlc/services/requirement.py`（detail） | out["doc"] 切源：块表非空 → 块表组装；**空且 doc artifact 存在 → 回退 _artifact_view**（过渡安全网，防未导入卡断崖） |
| 修改 | `src/platform_sdlc/agent/tools.py` | get_requirement_card 出参加 doc 键（块表组装），保留 sourceText；docstring 同步 |
| 修改 | tests + `docs/openapi.json` 重导 | 端点/切源断言；快照随批 commit |

**验收**：pytest（迁移夹具卡 detail.doc.content 与原 doc artifact content_json 深比较同形状/空表回退/端点 404·200）；verify-all 全绿；契约快照门绿；人工冒烟 curl -o 直落后 parse_doc/resolve_anchors 直接消费（消费链零适配）。

### v4-3 source_import 任务通道（双分支 + 完成回调落库 + CI 守护）

**设计依据**：§3 全节（任务形态/双分支/评论导入/幂等/失败面/红线精确化/落库机制细则）+ §2.4（分源封存）+ §6.1（模板三处一致性升守护）。

| 动作 | 文件 | 内容 |
|---|---|---|
| 修改 | `src/platform_sdlc/services/task.py` | ① `T_SOURCE_IMPORT` 入 _TASK_TYPES（不进 _POST_PUBLISH_TYPES——published 由既有终态锁拒）；② create_guarded 分源封存守卫（text 源∧块表非空→409；feishu 源∧(has_edit_trace∨has_platform)→409，§2.4）；③ **finish 挂点**：type=source_import ∧ ok → 读输出文件落库，成功才置 done；异常转 failed（error="导入落库失败…"） |
| 修改 | `src/platform_sdlc/services/block.py` | `import_from_output(task)`：读 `<workspace_root>/tasks/<task_id>/out/doc.json`（不存在→failed 明确文案附服务端生效根路径）→ 校验结构 → 单事务 replace_all（comment 块分流 req_comment：origin='feishu'/external_id/author/时间/解决态）→ missingMedia 校验（mediaId ∈ media 表，缺失计数+占位）→ 统计进 result |
| 修改 | `src/platform_sdlc/task_rules.py` | `NO_DRAFT_TYPES={'source_import'}`；payload 出参加 noDraftTypes + workspaceRoot |
| 修改 | `src/platform_sdlc/routers/meta.py` | task-rules 端点传 ctx.paths.workspace_root |
| 修改 | `runner/runner.mjs` | ① DEFAULT_RULES 加 noDraftTypes/workspaceRoot 缺省；② WORKSPACE 解析优先级：SDLC_WORKSPACE env > meta workspaceRoot > 仓根默认；③ selfCheck 首行 noDraftTypes 跳过（**消除 `RULES.draftTypes[type] \|\| 'cases'` 回退错查**）；④ promptFor 加 source_import 分支：飞书=沿用现 quality_check 固定拉取序列（lark-cli fetch + parse_doc --xml → out 绝对路径 + 媒体 POST media）；text=落临时 md → parse_doc --md 同 out 路径；红线段增补「doc.json 必须由 parse_doc 产出，禁止自写/改写；落库由平台回调执行，无需 put_draft」 |
| 修改 | `src/platform_sdlc/agent/prompts.py` | sys_prompt 加 source_import 分支（同义双写：run_command/write_file/parse_doc 输出绝对路径 tasks/<task_id>/out/doc.json/upload_media）；**goal_prompt 加分支（否则落 case_gen 兜底）；不加分支即 ValueError，必须同期** |
| 修改 | `src/platform_sdlc/agent/executor.py` | source_import 时注入输出目录绝对路径（workspace_root/tasks/<task.id>/out）——**根必须与 services 侧同源 paths.workspace_root** |
| 修改 | `src/platform_sdlc/agent/loop.py` | self_check 首行 NO_DRAFT_TYPES 跳过（**消除 `_draft_type_of` 回退 'cases' 错查**——§3 明示两处代码分支之二） |
| 新增 | `scripts/check_task_templates.py` | 三处一致性守护（仿姊妹仓 check_adversarial_core.py 标记区间提取+比对+exit 1）：① prompts.py sys_prompt 分支集 ∪ ② runner.mjs promptFor 分支集 ∪ ③ task_rules.DRAFT_TYPES/NO_DRAFT_TYPES ↔ DEFAULT_RULES 逐键相等 + SELF_CHECK 键集一致；提不出分支=红（宁红不静默） |
| 修改 | `.github/workflows/ci.yml` | pytest 后加 check_task_templates.py（第五道门） |
| 修改 | tests（test_guards / 新增 test_v4_import） | 封存守卫/finish 落库（tmp workspace 造 out/doc.json→done→块表断言）/无文件 failed 文案/task_rules 出参 |

**验收**：四底座绿 + 守护自测（临时删 runner 分支验证 exit 1）；半黑盒冒烟（runner 通道全链：建任务→领取→手工落真实 parse_doc 产物→done→doc.json 断言）；进程内通道 finish 同函数 + self_check 跳过单测（真模型端到端在 v4-5）；export+diff 确认零漂移。

### v4-4 L2 编排与导入守卫 + verify-v4 黑盒

**设计依据**：§3 触发（建卡自动排/只自动一次/失败人工可重试）、§3 质检前置（无 DAG、启动守卫查块表非空）、§10（text 存量卡首导补排）。

| 动作 | 文件 | 内容 |
|---|---|---|
| 修改 | `src/platform_sdlc/services/orchestration.py` | `on_created(req_id)`：建卡后自动排 source_import（create_auto + any_task_of_type 只一次 + 异常吞掉——沿三钩子惯例） |
| 修改 | `src/platform_sdlc/routers/requirements.py` | create 路由成功后挂 on_created |
| 修改 | `src/platform_sdlc/services/task.py`（claim） | 质检启动守卫：领取到 quality_check ∧ 块表空 → 直接 finish(False,「原文未导入，先完成 source_import」)（双通道汇合点单点实现，§3） |
| 修改 | `src/platform_sdlc/migrations.py` | 账本 `backfill_text_cards_import`：存量存活 text 卡∧块表空∧无 source_import 任务 → INSERT queued（幂等=存在即跳过；测试卡一并补排=实施裁量，导入无害） |
| 新增 | `scripts/verify-v4.mjs` | 断言组：①建卡自动排（createdBy=auto:card-created）②只自动一次 ③自建自领自落文件→done→doc.json 块数/类型/result 统计 ④落库后重导→409 封存 ⑤块表空卡质检→failed 含引导文案 ⑥评论导入+comments.json 兼容形状 |
| 修改 | `scripts/verify-all.sh`、`scripts/verify-m5.mjs` | 循环列表加 verify-v4；**m5 夹具改造（本批必改否则红）**：夹具卡自动排的 source_import 使块表空卡质检领取即被守卫置 failed——夹具先经导入通道塞块数据再领取质检 |
| 修改 | `tests/test_guards.py` | on_created 只一次/编排失败不连累建卡/claim 守卫单测 |

**验收**：verify-v4 全绿 + verify-all 全绿（11 脚本 + token，逐脚本核对 R7 竞态）；pytest；真库启动观察补排日志。

### v4-5 质检输入切换——quality_check 纯分析读块模型

**设计依据**：§3 任务形态表（质检→纯分析）、§3 text 特殊分支退役、§4.2（list-comments 步退役→只读评论工具输出 comments.json 兼容格式→write_file 落盘喂脚本）、§12 一期判据。

| 动作 | 文件 | 内容 |
|---|---|---|
| 修改 | `src/platform_sdlc/agent/prompts.py` | `_quality_check_steps` 重写单分支：get_requirement_card（含 doc{blocks,media}）→ 只读评论工具取评论 write_file raw/comments.json → doc 落盘 intake/doc-*.json → resolve_anchors.py → put_draft；`_quality_quick_ref` 表述同步；**text 特殊分支字样彻底删除** |
| 修改 | `runner/runner.mjs` | promptFor quality_check 同步重写：curl doc.json/comments.json 直落 + 后续步骤同源双写；删 sourceType 双岔与 text 内联段 |
| 修改 | `src/platform_sdlc/agent/tools.py` | 新增只读工具 `list_req_comments`（锁当前任务 reqId，输出=comments_payload 原样）+ ALL_TOOLS 注册 |
| 修改 | `scripts/verify-agent.mjs` | 两段链路：建卡→轮询 source_import done→断言 doc.json 块非空→排 quality_check→轮询 done→断言 digest_bundle 结构 + items[].anchor ∈ doc.json 块 ID 集（输入源切换黑盒判据） |
| 修改 | tests | 新工具存在性/锁 reqId 断言 |
| 核对 | skills 仓 parse_doc.py/resolve_anchors.py（只读核对） | 确认零改动可消费新输入（§13.2 B 判据） |

**验收**：SDLC_VERIFY_AGENT=1 verify-agent 绿（text 源全链）；verify-all 全绿；人工冒烟清单（飞书分支：真实飞书卡建卡自动导入→核对评论/external_id/媒体→重导幂等→质检纯分析→anchor 抽查）。

### v4-6 一期收尾——契约与红线文档同步

**设计依据**：§9（红线/守卫变更对照）、§3（注入残余显式记录）、§12 一期交付全项。

| 动作 | 文件 | 内容 |
|---|---|---|
| 修改 | `docs/ER图与业务流程.md` | §2 ER 加 req_block/req_comment；§6 任务通道加 source_import/noDraftTypes/workspaceRoot；§13 需求域行加 doc.json/comments.json 端点；§14 红线：第 7 条精确化（确定性管道例外）+ 新增「导入一次性封存」条目 |
| 修改 | `docs/DESIGN-v3-Python迁移.md` | §5 守卫对照清单增补三行（导入封存/质检启动守卫/模板三处一致性） |
| 修改 | `CLAUDE.md` | 业务红线段同步；架构总览加块模型权威一句；修正「权威文档：DEV-PLAN.md」为 ER图§13 + openapi.json（已过时） |
| 复核 | `docs/openapi.json` | 重跑 export+diff 确认零漂移 |

**验收**：verify-all + CI 五门全绿；文档走查（红线条目与代码落点一一对应）。

## 3. 二期批次框架（窗口 1 · 编辑）

交付判据（§12）：**窗口 1 全程平台内闭环；L 系行经重推不丢；变更单 diff 视图可用**。

| 批次 | 范围要点 | 验收 |
|---|---|---|
| **v4-7 块级直接编辑 API** | services/block.py 扩展 `apply_full_blocks`：全量块数组→服务端 diff（edit/insert/delete/move 以 **LIS** 判定，§5.3；diff 只在服务端一份，与三期变更单 draft 共用）→ 事务应用（version+1/updated_by、insert 发 blk-<ulid> **自实现 26 字符 Crockford Base32 不引新依赖**、delete 软删、整体重排 seq、行内子块 seq 紧随父块 §2.3）；PUT 块集端点 + GET diff 预览；守卫：拒 published、编辑×running/queued 质检 → 409（§1 窗口 1 表） | pytest（diff 四态/LIS 挪 1 块只 1 move/子块约束/守卫）；verify-v4 扩编辑断言；前端编辑器组件启动（§8） |
| **v4-8 评论系统 API** | 评论 CRUD（block_id 空=卡片级/reply_to/resolved/author 手填）；升级澄清项 POST …/comments/{id}/escalate → clarify_item 插行（**L<两位> 编号、origin='local'、quote=块文本、anchor=块 ID、src_comment_id 回链**——加列随本批 migrations，§4.1/§6.3）；列表出参加未解决计数（§4.3）；已解决评论仍可升级（§4.3） | pytest（编号两族不冲突/升级幂等）；verify-v4 增评论端点断言 |
| **v4-9 push_artifacts 按来源合并** | delete_by_req 精确化：删 origin IN (skill,incremental)、保留 local（§6.3/§9）；已答保留规则不变；items 出参带 origin；skills 仓 payload-schema.md 契约镜像同步（§13.2 A 二期部分） | pytest（L 系已答行重推保留）；**verify-m2 改造断言——§12 判据「L 系行经重推不丢」** |
| **v4-10 重新质检 + 窗口 1 闭环** | 现有 quality_check 人工创建通道即「重新质检」（无新端点，补文档表述）；黑盒：编辑块→重排→重质检→digest anchor ∈ 新块集 | §12 判据「窗口 1 平台内闭环」黑盒绿 |

二期收尾：openapi 重导 + ER 图 §13/§14 同步 + DESIGN-v3 §5 增补行。

## 4. 三期批次框架（窗口 2/3 · 变更）

交付判据（§12）：**窗口 2/3 不开新卡完成变更；verify 新增变更链路黑盒；skill 触发回归通过**。

| 批次 | 范围要点（条款） | 验收 |
|---|---|---|
| **v4-11 变更单数据面** | req_change/req_change_item 建表（§5.2）；draft CRUD（全量块 diff 出 items、反复保存全量重算、draft 可直接删，§5.3）；影响面预览：edit/move 只标直接锚定、delete 子孙按 parent **递归 CTE 一次查询**、heading 删不带章节体（§5.4）；**impact 冻结 heading block_id 禁 seq 数值** | pytest（diff/LIS 复用 v4-7/CTE 全集/预览不冻结）；黑盒 draft 保存 items 四态 |
| **v4-12 confirm 事务 + changed 态** | changed 流转**不进 REQ_FLOW**（§5.1 表外三通道）；confirm 守卫（∈{published,changed} ∧ 无活跃增量任务/未采纳草稿 ∧ **无未消化前序 confirmed 单=串行**）；事务：items 应用+version+1、impact_json 冻结、content_rev+1（加列迁移）、澄清重开（note 前缀+answer 清空）、**乐观锁 old_text 校验不符整单 409**（§5.5）；on_change_confirmed 钩子（§8） | pytest（原子性/乐观锁/串行）；新增 verify-change.mjs 骨架 |
| **v4-13 放弃变更** | 条件（无已采纳 incremental_bundle ∧ 最新 confirmed 单 LIFO）、块乐观锁、全量逆向回滚（edit 逆/insert 逆软删不构成封存痕迹/delete 逆复活/**move 逆恢复 seq**）、pending-only 恢复澄清（防新作答无痕丢失）、content_rev 不回退、aborted、changed→published（§5.6 全节） | pytest 逐逆向；黑盒放弃后块表/digest 重新一致 |
| **v4-14 incremental_check + 合并** | 新任务类型**三处同期**（prompts/runner promptFor/task_rules DRAFT_TYPES+SELF_CHECK——CI 守护自动覆盖）+ put_draft 白名单 + selfCheck 机械校验（anchor 落受影响块集/作废清单真实存在/区间=heading block_id 禁 seq 数值，§6.1）+ approve 分流第四条 + **采纳=服务端纯机械合并一个事务**（区间整组替换/空 anchor·global 行永不触碰/FR upsert/机器澄清按区间替换 local 永不动/audit 同规则/md 重渲染/artifact.upsert version+1 刷 pushed_at——发布守卫依赖，§6.2）+ on_merged 钩子（§8）+ incremental-guide.md 模板引用（跨仓） | pytest（合并机械性/越界拒绝）；黑盒 confirm→自动增量→采纳→pushed_at 刷新→发布守卫①过 |
| **v4-15 changed 守卫矩阵 + 发布/gate/L2** | set_answer 扩 in(clarifying,changed)；create_guarded：T_STANDARD 扩、design 族扩 in(published,changed)、**gate_review 保持 ==published**、拒 quality_check/digest_bundle（§5.1 矩阵）；changed 关闭窗口 1 编辑；发布入口扩 clarifying∨changed + **changed 双重守卫（影响面已消化 + standard 草稿哈希一致；clarifying 零新增校验保 verify-m2 契约）**；gate.open 拒 changed（§7）；on_answered 窗口化（时间下界=max(standard pushed_at, 最新 confirmed_at)、取消=窗口内终局、动机注释同步，§8） | pytest 矩阵逐行；verify-m2/gate 全绿 + verify-change 增补 |
| **v4-16 GR-3 扩展** | _baseline_json 加 reqRev（含存量回填，§10）；change_status 需求侧改「开闸后存在 confirmed（不含 aborted）变更单」→横幅带变更单号、diff 来源=req_change_item（**不比 rev**，§7）；两窗口口径不变 | verify-gate 增 GR-3 断言 |
| **v4-17 存量对账重导** | kind=reconcile 系统变更单（服务端 diff 机械生成 draft、**未发布卡 confirm=应用+保持原状态的状态域豁免**，§5.1/§10）；kind=rebaseline（影响面=全卡、**该 kind 放行一次全量 quality_check/digest_bundle——豁免挂单 kind 不动全局守卫**，§10）；评论幂等 upsert（external_id 唯一定位保原 id，§10） | pytest 豁免语义；真库演练（REQ-2026-001 家族） |
| **v4-18 skill 仓迁移（跨仓）** | §13.2 全表 A/C/D 类 + §13.3 最佳实践逐条 + §13.4 三项验收（触发回归/兜底链回归/消费链零适配）；A 类定位表述**随 v4-15 守卫上线同步，避免文档先行于能力** | §13.4 验收 + skill 触发回归 |

三期每批同样遵守：schema 变更走 schema.sql + migrations 判列补列、pyright strict、verify-*.mjs 黑盒、模板双写（CI 守护自动兜住）。

## 5. 顺序约束与同期性（硬约束）

1. **块表先于一切**：v4-1→2→3→4→5 串行；**质检输入切换（v4-5）必须最后**——前提是建卡自动导入链路（v4-4）已全通，否则全平台质检被启动守卫堵死。
2. **noDraftTypes 两处代码分支与任务类型同批**（v4-3）：task_rules.NO_DRAFT_TYPES + runner.mjs selfCheck 跳过 + loop.py self_check 跳过 + 双模板分支 + goal_prompt 分支——漏任一处：ValueError（prompts 未知类型）/ failed 未知类型（runner）/ `|| 'cases'` 回退错查（runner selfCheck、loop._draft_type_of）。三期 incremental_check 同理四件同期（DRAFT_TYPES+SELF_CHECK+put_draft 白名单+approve 分流）。
3. **origin 列与机器行默认值同批**（v4-1）：列+回填+push_artifacts 重建行携带 origin='skill'——否则首次重推把回填冲零（§10 点名）。
4. **三处工作区根必须同源 paths.workspace_root**（v4-3）：服务端 finish 落库根 = 进程内 executor 根（弃用 settings 原始串直拼）= runner meta 下发 workspaceRoot。分叉则进程内/runner 通道落库找不到文件。
5. **detail.doc 切源必须带空表回退**（v4-2）：verify-m4 断言 detail.doc.content.blocks，无回退未导入卡断崖。
6. **质检启动守卫与 verify-m5 夹具改造同批**（v4-4）：守卫使 m5 质检领取断言从 running 变 failed。
7. **建卡自动排上线即影响全部 verify 夹具**（v4-4）：各脚本建卡后多一个自动 source_import 任务，验收=verify-all 全绿逐脚本核对。
8. **二期内**：v4-8（L 系产生）先于 v4-9（合并保护）——§12「有 L 系就必须有合并保护」同期货；v4-7 diff/LIS 先于三期 v4-11 复用。
9. **三期内**：v4-12 先于 v4-15/v4-16；v4-14 依赖 v4-12 钩子；v4-17 依赖变更单全流程（§10 分期依赖不倒置）；v4-18 文档表述跟随 v4-15。
10. **发布契约保护**：v4-15 的 clarifying 分支零新增校验，verify-m2「直推 standardMd 无草稿前置」断言不得红。

## 6. 风险与回滚

| # | 风险 | 缓解 | 回滚 |
|---|---|---|---|
| R1 | 迁移搬错 | doc artifact 行不删只归档；搬运前 backup.sh；账本只跑一次 | 读面回退 artifact（v4-2 回退分支常驻）；revert v4-2 即恢复旧行为 |
| R2 | 两通道 workspace 根不一致 | 根同源 paths.workspace_root（硬约束 4）+ env 显式优先 + 失败文案携带服务端生效根 | 单批 revert v4-3，任务可 retry |
| R3 | 飞书分支真环境依赖 verify 覆盖不了 | 人工冒烟清单入 v4-5/v4-6 验收；失败面「报权限而非重试」 | — |
| R4 | 窗口期（v4-2~5）兜底链推 doc 读面不可见 | 窗口期禁用本地兜底链推新 doc | — |
| R5 | OpenAPI 快照门红 | 契约面批次 export+diff 随批 commit（§7 时点表） | 补重导 |
| R6 | 守护脚本解析脆弱 | 标记区间提取而非裸正则；自测模式（构造不一致验证 exit 1）；提不出分支=红 | CI 门独立 revert |
| R7 | verify 全家与自动排任务竞态 | v4-4 验收=verify-all 逐脚本核对（m5 drain/m6 过滤/发布收尾预分析兼容） | on_created 挂点单批 revert |
| R8 | 封存守卫误伤合法重试 | 判定只看块表/痕迹，失败重试块表仍空=合法（§2.4） | 守卫单点 revert |
| R9 | 三期 confirm/放弃事务复杂 | v4-12/13 事务原子性逐逆向用例 + verify-change 黑盒 | confirmed 不可作废是设计约束，修正走新 draft（§5.6） |
| R10 | 前端与后端批次脱节 | §8 前端配合项按批次对齐；契约以 openapi.json 为准 | — |

通用：新表/新列只增不删（revert 代码无需回滚 schema）；行为切换类批次（v4-2/v4-5/v4-15）独立成批即单批 revert 边界。

## 7. 契约与文档同步时点

| 时点 | 动作 |
|---|---|
| v4-2 | export-openapi + commit 快照（doc.json/comments.json 两条 path）——快照门首个强制点 |
| v4-3 | export+diff 确认零漂移（task-rules 出参无模型） |
| v4-6 | ER 图 §2/§6/§13/§14 + 附录环境变量；DESIGN-v3 §5 增补三行；CLAUDE.md 红线段 + 契约权威表述修正；DEV-PLAN.md 仅加指引行 |
| v4-8/9 | openapi 重导（评论/升级端点）；ER 图 §13/§14（按来源合并替换「全量替换」表述）；skills 仓 payload-schema.md 镜像 |
| v4-11~17 | 每契约批次随批重导；三期收尾 ER 图 §3 状态机加 changed、§13 变更单端点、§14 增补；DESIGN-v3 §5 增补；skills 仓 v4-18 |
| 全程 | CI `git diff --exit-code docs/openapi.json` 红即视为契约面未走快照流程，禁止合并 |

## 8. 跨仓配合项（本计划不含实现）

**前端 sdlc-platform-web**：一期基本零改动（doc.json 同形状，可选 source_import 任务态入口）；二期=块级聚焦编辑器（§14 方案 C：渲染态⇄单块编辑态/块手柄/textarea+语法按钮/全量块数组输出契约）+ **diff 视图组件（必做件，三期 confirm 复用）** + 评论 UI（角标/侧边流/回复串/解决态/升级按钮/列表未解决角标）+ 重新质检按钮；三期=变更单列表/diff/confirm 界面（四类清单人工增删）/changed 态展示/GR-3 横幅（变更单号+点入 items）/incremental_bundle 采纳界面/飞书冻结提示语（§11.5）。

**skills 仓 claude-sdlc-skills**：一期零改动（parse_doc 双分支复用、comments.json 兼容由平台 service 层映射——§13.2 B 判据）；二期 payload-schema.md 镜像；三期 v4-18 全清单。
