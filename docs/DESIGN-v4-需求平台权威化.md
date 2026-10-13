# SDLC 平台 · 需求平台权威化（v4 设计）

> 状态：设计定稿，未实施。日期：2026-10-13。
> 前置：`DEV-PLAN.md`（v1 范围锚点，本文档升级其铁律 1 与需求线范围）、`DESIGN-v3-Python迁移.md`（实现映射）。
> 定稿方式：16 条待定点 + 5 个过程新增决策逐条裁决（见附录决策清单）。

---

## 0. 背景与决策

v1 的需求链路：PM 提交飞书链接 → skill/agent 拉飞书原文 → 质检三件套 → 平台澄清/发布。飞书是需求权威——
PM 调整需求必须去飞书改文档，再拉回平台重走流程。痛点按需求所处阶段分三窗：

| 窗口 | 现状 | 体感问题 |
|---|---|---|
| 未发布（submitted/reviewing/clarifying） | 飞书改 → 重跑质检 → 重推 | 编辑动作离开平台，人肉触发重质检 |
| 已发布（published，终态锁） | 只能开新卡 | 历史答复、FR 点关联全部作废 |
| 已放行（gate released 后） | 无通道（GR-3 只盯设计/用例） | 口头协调 |

**核心决策：平台成为需求的唯一权威，飞书降级为初始导入渠道（单向、一次性）。**
配套三个支柱：

1. **块表关系化**——原文以「块」为单位一行一块落库，锚点即主键；
2. **变更单 + 章节级局部处理**——已发布需求不开新卡，走变更单机械算影响面、只重质检受影响章节；
3. **评论系统**——块级/卡片级评论，可升级为澄清项，接住「质检前/漏网/发布后」三窗的疑问。

v1 锚点升级（DEV-PLAN 铁律 1）：~~本地 md = skill 工作权威~~ → **需求原文与产物以平台为唯一权威，
本地 md 退化为 skill 过程工作区**。「明确不做」中的富文本编辑器调整为：做**块级结构编辑**（非自由富文本）。

## 1. 总体架构

```
飞书文档 ──(source_import 任务：拉取+parse_doc+评论导入，一次性)──┐
文本粘贴 ──(source_import 任务：--md 分支，确定性拍平)───────────┤
                                                                ▼
                                                    req_block（块表，权威原文）
                                                    req_comment（评论，双 origin）
                                                                │
        ┌──────────────────┬──────────────────┬─────────────────┤
        ▼                  ▼                  ▼                 ▼
  quality_check       窗口1 直接编辑      窗口2/3 变更单      评论→升级澄清项
  （全量质检，纯分析）  （未发布，块级）    （draft→confirm）   （L 系编号）
        │                                     │ confirm 原子生效
        ▼                                     ▼
  digest/audit/澄清项              changed 态 + 影响面（章节级）
  （草稿→人工采纳）                            │
                                    incremental_check（局部质检）
                                    （受影响章节切片→草稿→采纳合并）
                                               │
                                               ▼ 重算 summary / RTM /
                                    澄清处置完 → standard 全量重合成 → 重新发布
```

三窗口策略：

| 窗口 | 交互 | 守卫 |
|---|---|---|
| 1 未发布 | 块级编辑器直接改块表（version+1）+ 手动「重新质检」 | 拒 published（直接编辑是新写入口，继承的只有该谓词——与 push_artifacts 不同通道不同守卫对象）；编辑保存时存在 running/queued 质检任务 → 409（防产物 anchor 基于编辑混合态——先编辑后质检，与质检启动的块表快照时序互斥） |
| 2 已发布 | 变更单 confirm → `published→changed` → 局部处理 → 重新发布 | 终态锁语义精确化（见 §9） |
| 3 已放行 | 同窗口 2 + GR-3 横幅（content_rev 基线）驱动 reopen | GR-3 两窗口口径不变 |

## 2. 块模型与存储（req_block）

### 2.1 粒度 = 飞书文档的「编辑器块」

不是章节/句子/字符，是飞书文档模型的原生可寻址单元（飞书块 ID 原样保留）。
实测 REQ-2026-001：159 块 = title 1 + heading 14 + paragraph 12 + list-item 97 + image 23 + comment 12。

| 飞书元素 | 块类型 | 粒度规则 |
|---|---|---|
| 文档标题 | title | 1 块 |
| 标题 h1–h9 | heading | 一个标题一块，带 level + path 面包屑（派生） |
| 段落 | paragraph | 一个段落一块；内联元素（粗体/删除线/@人）拍平为 markdown 风格文本，不成块 |
| 列表 | list-item | **一个列表项一块**（非整列表），带 marker/ordinal（"2.c"，派生）/depth/parent |
| 表格 | table-row | **一行一块**（head + cells），单元格内嵌套列表/图作为子块挂行上 |
| 图片/附件/画板 | image/attachment/whiteboard | 一个一块，media = 内容哈希 ID（media 表） |
| 评论 | comment | 一条评论线程一块（thread/reactions/quote），anchorBlockId 回链宿主块；宿主块带 comment-refs |

### 2.2 表结构

```sql
CREATE TABLE IF NOT EXISTS req_block (
  req_id TEXT NOT NULL,
  block_id TEXT NOT NULL,      -- 锚点即主键：导入沿用飞书块 ID；平台编辑新发 blk-<ulid>（随机派生，禁内容哈希——重复文本是常态，内容哈希即主键冲突）
  seq INTEGER NOT NULL,        -- 文档序，间隔步长 1000（中点插入两次即耗尽，间隔过小重排成常态）；重排=事务内整体重排属常规操作——冻结面一律不引用 seq 数值（见 §5.4），重排因此无害
  type TEXT NOT NULL,          -- title|heading|paragraph|list-item|table-row|image|attachment|whiteboard（comment 不入块表，评论单一权威在 req_comment）
  text TEXT DEFAULT '',
  attrs_json TEXT DEFAULT '',  -- level/marker/depth/parent/media/name/alt/head/cells/rowLabel…
  version INTEGER NOT NULL DEFAULT 1,
  updated_at TEXT, updated_by TEXT,
  deleted TEXT DEFAULT '',     -- 软删：变更留痕
  PRIMARY KEY (req_id, block_id)
);
```

派生字段（path、ordinal）**不落库、读时推导**——编辑后永远一致，无脏数据面。

### 2.3 还原机制（为什么不需要树）

块模型是「**扁平流 + 每块自带结构属性**」，不是树：

- **线性文档流**：`ORDER BY seq` 顺序遍历，渲染器按 type/level/depth 摆视觉层级。层级是渲染态不是存储态，
  无 closure table / 物化路径。
- **章节子树**：seq 区间语义——一个章节 = 从该 heading 的 seq 到下一个 level ≤ 它的 heading 的 seq。
  区间查询纯 SQL，对插入稳定（不依赖 path 字符串匹配）。
- **真嵌套**（列表项→子项、表格行→行内子块）：parent 指针，全库唯一结构外键。**行内子块的
  seq 紧随其父块分配**（写入约束）——保证 seq 区间（影响面/章节）与 parent 递归（delete 传播）
  两套机制对同一子块给出一致答案，不产生「父块删了子块还活在区间外」的孤儿。
- **导出兼容**：服务端一条 `ORDER BY seq` SELECT 组装回 `{blocks:[…], media:[…]}` 同形状 JSON——
  前端 OriginTab、agent `get_requirement_card`、质检 prompt 输入三方的**消费格式零改动**。
  配套裁决：**doc artifact 行退役**——块表权威后它若保留就是第二权威（每次编辑/confirm/对账都要
  双写，漏一次即漂移）。消费面修正（现状核对）：detail 读 artifact(doc)；**get_requirement_card
  现状只读 requirement 行、不读 doc artifact**——其「需求原文」语义 v4 后改双源（块表组装的
  {blocks,media} + 保留 sourceText 出参兼容），docstring/出参的调整列入改动清单。迁移时
  doc artifact 行随迁后归档不再更新。

边界：heading 栈是位置性的——插入新 heading 后续块的章节归属（派生 path）会变，但**锚点不受影响**
（锚块 ID 不锚章节路径）。受影响子树计算必须用 seq 区间。

### 2.4 ID 体系与「导入一次性」

- 飞书源块 ID = 飞书原生 ID（稳定）；text 源经 `parse_markdown` 的 `new_id()` 自造（**每次重跑全换**）。
- 导入/重导规则（**分源封存**，封存动机对齐实际破坏面）：
  - **块表为空 = 首次导入，永远合法**（存量 text 卡补导入即此情形——不是重导）。
  - **text 源：块表非空后封存**——重跑 ID 全换族，锚定下游（澄清项/FR/digest 的 anchor）全部漂移。
  - **封存只约束「全量替换式重导」**（覆盖一切的重建）。**对账式重导（服务端 diff → 系统变更单
    → 人确认，§10）不覆盖任何平台数据，永远合法**——不受平台评论/平台编辑触发（否则未发布卡
    一条平台评论就永久封死「最后一次对账」通道）。
  - 飞书源全量替换式重导的封存条件：块表有平台编辑痕迹（version>1 或存在**未软删**的 blk-
    前缀块——放弃变更的 insert 逆留下的软删行不算痕迹）或存在 origin=platform 评论。
  - 封存后的原文变更只走变更单；「全量替换」语义仅存于首导/无下游时的失败重试（§3 幂等与
    §10 对账的口径由此统一）。

## 3. 导入链路（source_import）

把「原文进平台」与「质检」解耦：

| | 现在 | 之后 |
|---|---|---|
| quality_check | 拉飞书 + 解析成块 + 分析（三合一） | 纯分析（读块模型） |
| source_import（新） | — | 拉取 + 解析 + 评论导入 → 直接写块表/评论表 |

- **双通道可执行**（runner / 进程内 agent 同步支持）。
- **飞书分支**：沿用现 prompt 的固定拉取序列（`lark-cli docs +fetch` → `parse_doc.py --xml`）。
- **text 分支**：sourceText 落临时 md → `parse_doc.py --md`，与飞书分支同粒度。解析逻辑单一来源
  （parse_doc.py），服务端零解析代码；`prompts.py` 的文本源特殊分支退役。
- **评论导入**：comment 块（作者/时间/回复串/飞书解决态）→ `req_comment`（origin=feishu，
  external_id=飞书 comment-id），宿主映射 parse_doc 已做好（comment-refs → 块 ID）。
  **comment 块不落块表**——评论的单一权威在 req_comment，块表只存内容块；导出兼容 JSON 时
  由 req_comment 读时组装拼回 comment 块形态（resolve_anchors 消费兼容），宿主块的 comment-refs
  同理不落库（评论归属由 req_comment.block_id 承载）——避免同一评论两表双存重蹈第二权威。
  导入后飞书侧冻结。
- **幂等**：重跑 = 全量替换该卡块 + origin=feishu 评论行；平台侧评论不动。受 §2.4 一次性规则约束。
- **失败面**：权限不足/文档不存在 → failed 带明确文案（沿用「报权限而非重试」）；缺媒体 missingMedia
  计数 + 占位渲染。
- **触发**：建卡（POST /api/requirements）成功 → L2 钩子自动排（`exists_by_type` 只自动一次）；
  失败/取消后人工可重试。
- **质检前置**：不做任务 DAG。质检任务启动时守卫查块表非空，空则 failed 带文案
  「原文未导入，先完成 source_import」。

**红线精确化（agent 写通道）**：原「agent 工具面不存在正式产物写入通道」精确化为——
**「AI 生成/判断的内容不得直接成为正式产物（必须草稿→采纳）；服务端直读脚本输出的确定性管道除外」**。
落库通道**不走 agent 工具**：「解析器确定性」≠「管道无 LLM」——agent 工具的传参即 LLM 可塑，
且飞书文档内容不可信，提示注入可借「转录」夹带私货。source_import 任务中 agent 只负责执行拉取与
解析命令，**块表/评论落库由任务完成回调（服务端）直读 parse_doc 输出文件完成**——内容不经 LLM
上下文，「确定性」判据可验证（落库内容 = 脚本输出文件字节）。不新增 import_blocks 工具。

**落库机制细则**（与双通道既有机制的接缝）：

- **挂点**：`services.task.finish` 处理内（runner 的 `POST /tasks/{id}/done` 与进程内 executor
  回报的唯一汇合点）——type=source_import 时 finish 读输出文件落库，落库成功才置 done。
- **输出文件定位：按任务 ID，不按需求名**——`<工作区根>/tasks/<task_id>/out/doc.json`。两通道
  现状的子目录算法不同（进程内 safe_name 安全化 vs runner 用卡名原名），需求名含特殊字符时按
  需求名映射会分叉落空，且有路径逃逸面（卡名仅 ≤120 字校验）；task_id 是整数，映射无歧义。
- **工作区根统一下发**：现状两通道是两个不同名配置（SDLC_WORKSPACE / agent_workspace_root）
  且子目录拼法不同——统一是**新改造而非既有事实**，列入一期交付：工作区根进 task_rules meta
  下发（runner 从 meta 拉，DEFAULT_RULES 带缺省），进程内沿用服务端配置。
- **selfCheck 适配（两处代码分支，非数据登记）**：runner 的 selfCheck 现行机制是「草稿回传了才
  记 done」——source_import 不产草稿，`DRAFT_TYPES` 无值可填且回退 `|| 'cases'` 会错查。
  task_rules 新增 `noDraftTypes` 键登记 source_import；**跳过逻辑是 runner.mjs 与进程内
  loop.py 两处代码改动**（数据条目改变不了硬编码回退行为），列入一期交付；CI 三处比对覆盖
  数据面，行为面由 verify 黑盒覆盖（source_import 任务两通道都能记 done）。
- **媒体**：parse_doc `--download` 落盘的媒体文件走**既有 upload_media 工具**上传（二进制内容，
  非文本指令，无提示注入面；mediaId=内容哈希由服务端计算）——属「执行拉取命令」的伴生动作而非
  内容经手。finish 落库时做 missingMedia 同款校验（块引用的 mediaId ∈ media 表，缺失计数占位）。
- **注入残余风险（诚实降格）**：「落库 = 文件字节」拦不住「agent 伪造文件本身」（注入后不跑
  parse_doc 而自写同路径文件）——彻底闭合需服务端重跑解析比对，成本不值。残余面与平台哲学一致
  （无登录：防误操作不防故意），在此显式记录而非假装闭合。

## 4. 评论系统（req_comment）

### 4.1 定位：与澄清项划清地盘

| | 评论 | 澄清项 |
|---|---|---|
| 性质 | 自由讨论，低门槛，随时发 | 结构化追踪单元，有处置状态 |
| 来源 | 任何人、任何阶段 | 质检产物 ∨ 评论升级 |
| 生命周期 | 未解决 → 已解决（轻量两态） | 待处置 → 已答/驳回/挂起/重开 |

关键生命周期：**评论 → 升级为澄清项**（显式动作，自动带入块文本作 quote、块 ID 作 anchor，
`clarify_item` 加 `src_comment_id` 列回链原评论——「被升级的评论可跳回原讨论上下文」的载体，
与幂等重导的 external_id 保 id 稳定配套）。评论区是漏斗口，澄清项是追踪面。

### 4.2 存储

```sql
CREATE TABLE IF NOT EXISTS req_comment (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  req_id TEXT NOT NULL,
  block_id TEXT DEFAULT '',        -- 空=卡片级评论；非空=锚定原文块
  origin TEXT DEFAULT 'platform',  -- platform | feishu（导入）
  external_id TEXT DEFAULT '',     -- 飞书 comment-id（origin=feishu 必填）：幂等重导的唯一定位键
  author TEXT DEFAULT '',          -- 手填身份（无登录约定，case_comment 同款）
  text TEXT NOT NULL,
  reply_to INTEGER,                -- 回复串
  resolved INTEGER DEFAULT 0,
  created_at TEXT
);
```

飞书评论与平台评论同表同管道（origin 区分）：呈现统一、质检输入源切换简单、飞书体验完整平移。
输入源切换的落点：原 `lark-cli drive +list-comments` 步退役，agent 改用**只读评论工具**——工具的
输出格式**直接设计成 resolve_anchors 的 comments.json 兼容格式**（平台任务侧 agent 拉取 →
write_file 原样落盘 raw/comments.json → 喂脚本，锚点管线零适配；该格式同时是 §13.2 B(iii) 本地
兜底链的导出格式，一份映射两处复用）。

### 4.3 呈现

- **原文 Tab**：锚定块级评论——有评论的块带角标，点开侧边评论流，回复串 + 解决/未解决（主交互面）。
- **卡片级评论**：挂卡片头部/概览区（「整体疑问」）。
- **列表页**：「N 条未解决评论」角标——质检前/漏网问题被看见的最低成本通道（不做通知推送，v1 边界维持）。
- **澄清项面板**：被升级的评论可跳回原讨论上下文。
- **软删块上的评论**（变更单 delete 后）：评论**不删**，块不再渲染但锚定它的评论收进该变更单的
  diff 视图（「已删块上的评论」折叠区，人审 diff 时可见讨论上下文）；锚定软删块的**未解决评论
  直接进 incremental_check 的输入**（切片尾部附加，标注「原块已删除」——机械规则简单且信息不丢，
  LLM 判断作废依据需要知道删了什么，故增量切片对软删块以「[已删除] <原文>」标记**包含**呈现）；
  已解决（resolved）评论**可升级**为澄清项（已结束的讨论仍可能需要正式追踪），升级是追加动作、
  不改评论自身 resolved 态。

## 5. 变更流程（req_change / req_change_item + changed 态）

### 5.1 状态机

**两条 changed 流转不进 `REQ_FLOW`**（表内 = 无事务副作用的纯流转白名单，供
`POST /status` 通用路由使用——states.py 既有惯例自证：「publish 终态推进不在此表」。把
`(published,changed)/(changed,published)` 写进表会让通用 `set_status` 成为守卫旁路：不经
confirm 事务即可转 changed（不冻影响面、不重开澄清、content_rev 不动），不经发布守卫即可回
published）。三条专用通道各携全套事务语义：

| 流转 | 唯一通道 | 事务语义 |
|---|---|---|
| published → changed | 变更单 confirm（§5.5） | 冻结影响面 + 重开澄清 + content_rev+1 |
| changed → published | publish 动作（§5.1 发布守卫） | 澄清全处置 + 影响面已消化 + 草稿一致性 |
| changed → published（放弃） | 放弃变更动作（§5.6） | 逆向恢复重开项，变更单置 aborted |

- `changed` 守卫矩阵（逐点对齐现有守卫的实现位置，防「隐式推进吞态」——push_artifacts 末尾的
  `!= "clarifying" → set_status(clarifying)` 会把 changed 悄悄改写，影响面冻结态随之丢失）：
  - 作答：`set_answer` 的状态拒绝扩为 `not in ("clarifying","changed")`（允许，只面对重开+新增项，
    未受影响项保留已答）。
  - 任务：`create_guarded` 分两行——T_STANDARD 扩为 `in ("clarifying","changed")`（L2 在 changed
    下排 standard_doc 不被拒）；**case_gen/design_gen/design_rev 从 `== published` 扩为
    `in ("published","changed")`**——changed 本就是发布后状态的合法变体，且 §7 的 in-review
    落改通道（design_rev）依赖此行，否则评审中变更单一生效落改即被堵死、流程顺序倒挂。
    **gate_review 保持 `== published`**：开闸本身拒 changed（§7），changed 下无开闸即无
    gate_review，放行只会造出「无轮可挂」的陷阱入口。
  - **拒 quality_check / digest_bundle**——changed 的原文消化只走增量通道：全量重推既会被隐式推进
    吞掉 changed 态，又会按来源删除丢掉 incremental 接编的已答澄清项（见 §6.3）；回 published 后
    全量通道重新开放。增量通道不可行的兜底 = 放弃变更（§5.6），不是放开全量。
  - 草稿：`put_draft` 类型白名单加 `incremental_bundle`；changed 下许可面 = incremental_bundle + standard。
  - 直接编辑：changed 态**关闭窗口 1 的块级直接编辑**（编辑入口 = 新变更单 draft）——否则块编辑
    与已冻结的影响面脱节，增量切片与实际块态漂移。
  - 变更单 confirm：守卫 = **需求状态 ∈ {published, changed}** ∧ 无活跃增量任务/未采纳增量草稿
    ∧ **无影响面未消化的前序 confirmed 单（串行 confirm）**。未发布卡的原文变更走窗口 1 直接
    编辑，不允许 confirm——**唯一例外：kind=reconcile 的系统对账单**（§10）在未发布卡上
    confirm = 应用块表 + 保持原状态（changed 是「发布后发现变更」的语义，未发布卡不需要）。
    串行的动机：连环单下旧单未消化即可 confirm，会构造「最新单影响面为空 → 发布守卫 ∨ 右支
    放行」的旁路（旧单块变更从未进 digest 即发布），且后单可删除前单冻结影响面引用的 heading
    （读时重解析悬空）。处理中途补改 = 提 draft，前序消化完（分钟级）即可 confirm。
  - 发布：入口从「仅 clarifying」扩为「clarifying ∨ changed」。clarifying 分支零新增校验
    （现状契约不动——verify-m2 直推 standardMd 无草稿前置）。changed 分支双重守卫——
    ①**影响面已消化**：不存在「影响面 digest/FR 非空 ∧ `digest.pushed_at < confirmed_at`」的
    confirmed 变更单（串行 confirm 保证同时至多一张未消化单，此式即全量校验），否则人可在
    confirm 后不等增量质检直接答完重开项发布未合并旧 digest；②**重合成已发生**：changed 下
    必须存在 standard 草稿且 `body.standardMd` 与其哈希一致——无草稿（重合成被取消/失败，
    「窗口内终局」不等于可跳过重合成直接发布）即 409；有草稿但不符 409「内容与最新草稿不符」
    （堵旧草稿趁增量刚合并发布过时文档——发布内容取自 body，平台现状不校验其来源）。

### 5.2 表结构

```sql
CREATE TABLE IF NOT EXISTS req_change (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  req_id TEXT NOT NULL,
  title TEXT DEFAULT '',        -- 一句话变更说明
  detail TEXT DEFAULT '',       -- 背景/原因
  proposed_by TEXT DEFAULT '',  -- 提单人（手填）
  confirmed_by TEXT DEFAULT '', -- 确认人（手填，可同提单人）
  status TEXT NOT NULL DEFAULT 'draft',   -- draft | confirmed | aborted
  impact_json TEXT DEFAULT '',  -- 确认时冻结的影响面快照（含人工增删）
  created_at TEXT, confirmed_at TEXT
);

CREATE TABLE IF NOT EXISTS req_change_item (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  change_id INTEGER NOT NULL,
  block_id TEXT NOT NULL,       -- 目标块（新块在草稿期即有平台 ID）
  op TEXT NOT NULL,             -- edit | insert | delete | move
  old_text TEXT DEFAULT '', new_text TEXT DEFAULT '',
  old_attrs TEXT DEFAULT '', new_attrs TEXT DEFAULT ''
);
```

不设审批环：提单人 + 确认人手填（可同一人），确认界面可见 diff + 影响面后果即可（防误操作不防故意）。

### 5.3 draft 期：编辑与 diff

- 前端提交**编辑后的全量块数组**，服务端与块表 diff 出 items（同 ID 文本/属性变 = edit；新 ID = insert；
  消失 = delete；顺序变 = move）。**move 判定用 LIS（最长递增子序列）**：新序列中保持相对顺序的最大
  子集视为未动，其余才是 move——朴素逐位对比会把一次挪动判成 N 个 move，把 N 个无关行拉进影响面，
  直接违背「宁可漏报不可误报」。几百块 O(n log n) 无压力。**diff 逻辑只在服务端一份**，前端零 diff
  代码，SQLite 单写者天然防并发。
- draft 可反复保存（items 全量重算替换）；**draft 可直接删**（未碰块表，零代价）。
- 影响面预览：每次保存后服务端现算（机械 join，不冻结）返回前端。

### 5.4 影响面：机械规则 + 人工增删（章节级粒度）

机械规则（宁可漏报不可误报，漏的由人补勾）：

- **edit / move**：只标直接锚定行（move 视同 edit——列表项挪序可能是优先级语义；预览可勾掉）。
- **delete**：块本身 + 其子孙块（真嵌套：列表项删→子项删、表格行删→行内子块删）。子孙按 parent
  **反向**递归取全集，用 **SQLite 递归 CTE 一次查询**——应用层逐层 SELECT 子块即循环内查库，违反
  「循环体内无数据库查询」铁律。**heading 删除不带章节体**（对齐飞书：删标题块只删标题行，章节体
  上提；删整章 = 编辑器显式「删除章节」= 批量 delete items）。

**粒度 = 章节**（seq 区间）：预览、合并、LLM 上下文三者一个粒度贯穿——
「本次影响 2.2 节、3.1 节」，人按章节增删，冻结进 impact_json。
**impact_json 冻结章节的表示 = heading 的 block_id**（读时重解析 seq 区间），**禁止冻结 seq 数值区间**——
整体重排会改写全表 seq，冻结数值即失真；锚 block_id 则重排天然无害。

确认界面 = 块级 diff + 四类清单（受影响章节的 digest 行 / 澄清项 / FR 点 / 关联用例），
默认机械值，人可增删：勾掉 = 不受影响保留现状；补勾 = 机械漏了也要处理
（heading 语义变化这类机械判断不了的）。

### 5.5 confirm = 一个事务

1. items 应用块表：edit/move 更新 + version+1 + updated_by；insert 插入；delete 软删。
2. 变更单置 confirmed，impact_json 冻结（含人工增删）。
3. `requirement.content_rev += 1`；状态 `published→changed`（若当前 published）。
4. 受影响澄清项重开：旧答复挪 note 前缀「因变更单#N 重开，原答复：…」，answer 清空回 pending。
5. 应用校验（变更单乐观锁）：edit/delete 项逐条校验 `old_text == 块表现值`，不符即整单 409 拒绝——
   连环 confirm 场景（draft#2 基于 confirm#1 之前的块态冻结）直接应用会无感覆盖 confirm#1 的修改；
   语义同用例乐观锁 409 惯例，拒绝后重新编辑 draft 重 diff。
6. L2 钩子（事务后）：影响面 digest 行/FR 点非空 → 自动排 incremental_check。钩子失败的人工逃生门：
   incremental_check 开放手动创建，守卫 = changed 态 ∧ 存在 confirmed 变更单 ∧ 影响面非空
   （对照 on_gate_opened 排不上时人可经门禁页补建的先例——不能让需求停在 changed + 影响面冻结
   + 增量无人排的中间态）。

**confirm 守卫**：需求状态 ∈ {published, changed}（reconcile 对账单豁免，见 §5.1/§10）∧ 无
running/queued 的 incremental_check 任务 ∧ 无未采纳的 incremental_bundle/digest_bundle 草稿
（否则产物合并时 anchor 已漂移）∧ **无影响面未消化的前序 confirmed 单（串行 confirm，动机见
§5.1）**。draft 随时可提（不碰块表）；处理中途补改 = 提 draft，前序消化完即可 confirm。

### 5.6 作废与放弃语义

- draft 自由删；**confirmed 不作废**——但新增**「放弃变更」动作**作为 changed 态的受控逃生门
  （增量通道死锁时的出口：incremental_check 连续失败、或维护者就是要全量重质检时，changed 卡
  不能全量（拒）、不能发布（守卫）、不能作废——没有这个动作就是死锁）：
  - 条件：changed 态 ∧ 该变更单**无已采纳的 incremental_bundle**（影响面从未被部分消化）∧
    **为最新 confirmed 单**（串行 confirm 下即唯一未消化单，LIFO——只能从最新弃起）。
  - 前置校验（放弃的乐观锁）：逆向 edit 前逐条校验块现值 == new_text（confirm 后无人再动过
    这些块），不符即 409「块已被后续修改，无法放弃」。
  - 事务（**全量逆向回滚**，不是只放弃流程）：confirm 应用的 items 逐条逆向——edit 逆
    （new_text → old_text，version 再 +1，updated_by=放弃人）、insert 逆（软删，不构成封存
    痕迹——§2.4 封存判定排除 deleted 行）、delete 逆（复活清 deleted）、**move 逆（恢复原 seq）**；
    重开澄清项恢复原答复——**仅限仍为 pending 的项**（changed 期间已被人再处置的保留新答复，
    note 追加「变更单#N 已放弃，此为放弃后处置」，防人工新作答无痕丢失）；`content_rev`
    **不回退**（单调递增，仅作展示计数；GR-3 判定不依赖 rev，见 §7）；变更单置 `aborted`；
    状态 changed→published（表外专用动作，同 §5.1 三通道模式）。**必须回滚块表的理由**：若保留
    编辑，已编辑块与未消化 digest 的不一致没有任何登记载体——人重提变更单时 diff 从当前块表
    现值出发，已编辑块不在新 items 里，影响面二次漏。放弃 = 当无事发生（除变更单留痕），这是
    唯一无残留的语义。

### 5.7 stale / 需复核：读时推导，零落标

| 对象 | 推导式 |
|---|---|
| digest 行 stale | 行 anchor ∈ 累计 **confirmed（status=confirmed，不含 aborted——其 items 已逆向回滚，块与 digest 重新一致）** items 的 block_id 且该变更单 confirmed_at > digest.pushed_at |
| FR 点 stale | 同上（anchor join） |
| 澄清项重开 | 非推导——confirm 事务里的实际状态变更（落库） |
| 用例需复核 | 关联 FR 点 ∈ 受影响集 且 case.updated_at < 变更单 confirmed_at（更新过 = 已处理） |

**anchor 健康度前置**（v1 展示引用升 v4 join 键的配套守卫）：digest/FR/澄清项的 anchor 是 LLM 产物，
可空可幻觉——空 anchor 行永远不 stale、无法归入任何章节区间，「区间内/区间外」二分对它失效。
全量质检与增量合并的**采纳点机械校验 anchor ∈ 块表**（空或不存在即拒绝该行，要求补锚或显式标记
global——全局行不参与区间二分，任何变更单 confirm 视为需复核，保守侧）。存量 digest 行的 anchor
校验纳入迁移（脏 anchor 登记在案而非静默进关键路径）。

## 6. 局部质检（incremental_check）

### 6.1 任务形态

| | quality_check | incremental_check |
|---|---|---|
| 输入 | 全量块模型 | 受影响章节切片（含变更 diff 标记）+ 全量 digest/audit 结构行紧凑视图 + 保留澄清项及已答 + 受影响章节内未解决平台评论 |
| 产出 | digest 8 区块从零建 | 受影响范围新版本行 + 作废行清单 + 新问题 + summary/risk_tier 重算 |
| 触发 | 人工创建（现状：建卡路由只调 svc.create，无自动排质检的机制） | 变更单 confirm 后 L2 自动排（影响面 digest/FR 非空才排） |

新任务类型（不复用 quality_check——输入面/产物面/selfCheck 规则都不同，混编互相污染）。
双通道同步支持（runner.mjs + 进程内 prompts 同步加）。模板双写从「注意」升为**守护**：新增 CI
比对**三处**一致性（仿 scripts/check_adversarial_core.py 的跨文件比对先例）——①prompts.py 任务
分支 ∪ ②runner.mjs promptFor 分支 ∪ ③task_rules.DRAFT_TYPES ↔ runner.mjs 的 DEFAULT_RULES
硬编码回退副本（meta 拉取失败时生效的第三写面，漏比它则「DEFAULT_RULES 陈旧 + meta 不可达」
组合下新任务走错映射且 CI 不红）；含 source_import 的无草稿类型语义。每新增一种类型
（source_import / incremental_check）三处同步，漏一处 CI 红。

**selfCheck 机械校验**：产出行的 anchor 必须全部落在受影响块集合内（防 LLM 越界重写）；
作废清单里的行必须真实存在；**incremental_bundle 草稿的章节区间表示 = heading 的 block_id**
（对齐 §5.4「禁止冻结 seq 数值区间」的禁令，LLM 在草稿 contentJson 里写 seq 数值即拒）。

### 6.2 产物与采纳合并

- **新草稿类型 `incremental_bundle`**（结构：changeId + 章节区间 + 新版本行集 + 作废清单 +
  澄清项增量 + summary/risk_tier）。`task.approve` 分流表加一条：`incremental_bundle → 增量合并`
  （现有：digest_bundle→push_artifacts，cases→sync_upsert）。
- **采纳 = 服务端纯机械合并**（一个事务，章节级替换）：
  - 区间内旧 digest 行删除、新行写入；区间外原样。**行的定位规则**：digest 行无稳定行 ID，合并
    按「anchor 落入区间」**整组替换**（不逐行定位）——同章节多行共享 anchor 也整组重写；
    **空 anchor / global 行不属任何章节，增量合并永不触碰**（它们的消化 = 回 published 后的全量
    重质检，见 §5.7 前置守卫）。作废清单按 anchor 列示，同 anchor 多行时附行文本匹配，
    selfCheck 校验其真实存在。
  - FR 点：区间内 upsert + remove；区间外不动。
  - 机器澄清项按区间替换；**平台来源（local）行永不被动**。**audit 行同 digest 规则**：anchor
    落区间的体检条目整组替换、作废清单同样适用（selfCheck 校验存在性）、空 anchor/global 行
    不触碰——detail 出参的 relatedCases 读时聚合不受影响。
  - digest/audit 的 content_json 合并；**md 整篇重渲染**（md 正式退化为展示物，权威在结构行）。
  - summary/risk_tier 覆写；**走 artifact.upsert：version+1 且 pushed_at 刷新**——§5.1 发布守卫的
    `digest.pushed_at >= confirmed_at` 依赖此语义（对 digest 而言合并也是一次推送；
    pushed_at 的出参语义 = 「该 artifact 最近一次内容变更时间」）。
- LLM 产物永远不直接进正式面（草稿→人工采纳两态红线原样）。

### 6.3 澄清项 origin 与编号

`clarify_item` 加 `origin` 列（schema 变更走 migrations 判列补列惯例）：

| origin | 编号 | 谁产生 | 谁会删它 |
|---|---|---|---|
| skill | P 系 | 全量质检 | 全量质检重推 |
| incremental | P 系（接 max+1） | 局部质检 | 全量质检重推、所在章节下次局部质检 |
| local | L 系（L01…） | 评论升级 | 仅人工删 |

机器产物统一 P 系（对 PM 呈现无差别），平台产物 L 系；UNIQUE(req_id, seq) 两族天然不冲突，
删除规则按 origin。

**重推语义变更**：push_artifacts 的 `delete_by_req` 全量替换精确化为**按来源合并**——
全量质检重推删 skill+incremental 行、保留 local 行（否则评论升级的澄清项被冲掉）。
与「重推按 seq+文本保留已答」同族演化：全量替换 → 按 anchor/来源合并。如实表述边界：保留机制是
「新清单同 seq 行的回填」——incremental 接编的 P 系行在全量新清单中无同 seq 项时回填无目标、
已答即丢失，这正是 §5.1 changed 拒全量重推要封死的面；回 published 后全量重推 = 权威重建，
该丢失属预期语义而非缺陷。

## 7. gate 联动（GR-3 扩展）

- `requirement.content_rev INTEGER DEFAULT 0`，变更单 confirm 时 +1，**单调递增不回退**（放弃
  不减）——rev 仅作展示计数（「历史提过 N 次变更」），**不承担变更判定**（回退的非单调 rev 与
  基线相等性比对互构误报：回退后 rev 与基线相同而内容实变，或反之）。
- **变更判定查变更单，不比 rev**：`change_status` 的需求侧比对 = 「开闸时间之后存在
  status=confirmed（不含 aborted）的变更单」→ 横幅「需求内容已变更（变更单 #N）」，点入看该单
  的 items——**块级 diff 的数据来源 = req_change_item 本身**（rev 是计数器非快照，不从它重建）。
- 语义按「与放行基线相比变过」：changed 态（处理中）与重新发布后的 published 都算 changed=true——
  前者卡片状态本身显式，后者需要横幅驱动 reopen。
- **gate.open 拒 changed**（开闸只在 published；changed 下无开闸即无 gate_review，§5.1 任务守卫
  同口径）。
- **两窗口口径不变**：released 后出横幅→人确认 reopen 新轮（旧轮只读归档）；in-review 中变更
  走落改通道不开新轮，需求变更加入后不加特殊规则。

## 8. 发布与 L2 编排适配

- **standard_doc 全量重合成**（局部性止于质检层，发布层整体语义）：changed 态澄清全处置 →
  L2 排 standard_doc（输入 = 合并后全量 digest/audit/澄清）→ 草稿 → 人预览 → POST /publish →
  `changed→published`。
- **on_answered「只自动一次」精确化为「每个澄清完备窗口只自动一次」**：exists_by_type 查询加时间下界
  （created_at > 窗口起点；窗口起点 = max(standard artifact pushed_at, 最新 confirmed 变更单
  confirmed_at)，两者均有现成存储）。**这是对「人取消即终局」红线的显式重新裁决**：取消语义从
  「永久不再自动排」收窄为「窗口内终局」——变更 confirm 开新窗口后自动合成重新获得机会（新内容使
  旧取消意图失效）。已裁决放行，orchestration.py 的动机注释（「人取消即终局，不复活」）据此同步更新。
- on_change_confirmed（新钩子）：confirm 成功后排 incremental_check（编排失败不连累主流程，
  人工逃生门见 §5.5 第 6 步）。
- on_merged（新钩子）：incremental_bundle 采纳合并成功后——changed 态 ∧ 澄清全处置 → 排
  standard_doc（窗口判重同 on_answered）。补断链：影响面不含澄清项的纯 digest/FR 变更无人作答、
  on_answered 永不触发，重合成的点火点只能在合并完成后。

## 9. 红线/守卫变更对照

| 红线/守卫 | 变更 | 精确化后的表述 |
|---|---|---|
| 发布终态锁 | 加出口 | published 仍拒静默推/草稿/任务/作答；**显式通道 = 变更单 confirm → changed**，锁的是「不声不响地变」不是「变」 |
| agent 写通道 | 加确定性管道例外 | AI 生成/判断内容必须草稿→采纳；服务端直读脚本输出的确定性管道（source_import 完成回调落库）除外——内容不经 LLM 上下文，判据可验证，不新增 agent 工具 |
| L2 只自动一次 | 窗口化 + 新钩子 | 每个澄清完备窗口只一次（加时间下界）；取消 = 窗口内终局（显式重新裁决）；新增 on_change_confirmed / on_merged |
| push_artifacts 全量替换 | 按来源合并 | 重推删机器行（skill+incremental），保留 local 行；已答保留规则不变 |
| GR-3 变更检测 | 对象扩展 | 基线加 reqRev；两窗口口径不变 |
| 编辑权限 | 不设限 | 手填身份必填 + 块级 version/updated_by + 变更单 + 评论全量留痕；防误操作不防故意 |

## 10. 存量迁移

- 有 doc JSON 的卡（现库仅 REQ-2026-001，45KB/159 块）：content_json → req_block 一次性搬，
  块 ID 原样（anchor 零迁移）。**已发布卡只迁存储不改状态**，且快照时点 = 当年 push 时刻——与飞书
  现状的漂移属未登记变更，走变更单补登记（权威切换时点后的历史漂移不在迁移责任内）。
- **未发布已质检的飞书卡：迁移搬入后重导对账（三期，随变更单机制——分期依赖不倒置）**——
  不是静默全量替换：服务端 diff 块表 vs 新解析结果，**机械生成一张 kind=reconcile 的系统 draft
  变更单**（proposed_by=迁移对账，items 由 diff 得出：块文本变=edit、新增=insert、飞书侧已删=
  delete），人过 diff 后走 confirm——**reconcile 单在未发布卡上 confirm = 应用块表 + 保持原
  状态**（§5.1 状态域对此单豁免）。stale/影响面/澄清重开/软删留痕全部复用变更单机制。评论行
  **幂等 upsert**（按 external_id=飞书 comment-id 唯一定位保原 id——不用 block_id+author+text
  组合键：同块同作者同文本的重复发言会使 upsert 目标不确定、resolved 态可能错配）。
- **text 存量卡：首导后强制一次全量重质检**——存量澄清项/FR 的 anchor 为空（text 源当年无块
  管线），补导入后块 ID 是 x- 族新 ID，存量产物面与块模型零关联，首次变更单的机械影响面对这些
  行**系统性全漏**。未发布卡直接跑全量重质检接上 anchor；**已发布的 text 存量卡走
  kind=rebaseline 系统变更单**：影响面 = 全卡，confirm 进 changed 后**该 kind 放行一次全量
  quality_check/digest_bundle**（豁免挂在单的 kind 上而非全局开关——changed 拒全量的守卫不
  动），采纳即消化、重新发布回 published。否则这类卡的全量重质检在任何状态都无通道（published
  拒、changed 拒、增量永不触碰空 anchor 行），空 anchor 行永远无法重建。
- text 源存量卡：块表为空 = **首次导入**（§2.4），排 source_import 补；测试卡不管。
- `clarify_item.origin` 存量回填 **'skill'**——判列补列默认空串会让「按来源合并」永远匹配不到
  存量行，每次重推累积一份。
- 存量 digest/FR/澄清项的 **anchor 健康度校验**（空/幻觉 anchor 登记，§5.7 前置守卫的存量面）。
- 迁移幂等（migrations.py 判存量执行，八步惯例）。

## 11. 未决事项（实施前需回到桌面）

1. ~~编辑器技术选型~~ **已裁决（2026-10-13）：方案 C 块级聚焦编辑**（详见 §14），Tiptap 留作
   演进路径不封死——后期按使用反馈再调整升级。
2. 块类型编辑分期（并入 §14）：编辑面 heading/paragraph/list-item；table-row 整块替换（行表单）；
   image 上传替换。
3. 评论通知：不做推送（v1 边界），角标方案先跑。
4. ~~skill 侧迁移策略~~ **已裁决（2026-10-13）：做方向 a，范围 = pull-artifacts 一个子命令 +
   定位表述更新 + 评论导出兼容格式，不重构消费链**，详见 §13。
5. **飞书侧单向盲区**（已知限制，显式接受）：平台对飞书无写约束，「冻结」是期望而非机制——PM 按习惯
   继续在飞书评论/解决评论，平台永远看不到且无检测，resolved 态静默分叉。缓解：导入后在卡片详情页
   明示「原文已迁入平台，飞书侧评论不再同步」。

## 12. 实施切期（方案全做，执行分期）

| 期 | 内容 | 交付判据 |
|---|---|---|
| 一期·地基 | req_block/req_comment 表 + source_import 任务（双分支+评论导入，完成回调服务端直读落库；tasks/<task_id> 目录定位 + 工作区根 meta 下发 + runner/loop 两处 selfCheck 代码分支 + noDraftTypes 键）+ 读侧同形状导出（doc.json/comments.json 文件端点，curl 直落供质检管线）+ 存量迁移（origin 回填/anchor 校验/text 卡首导；**对账重导与 rebaseline 随变更单机制归三期**）+ 红线精确化 + 模板双写 CI 守护 | 建卡自动导入，原文/评论平台可读，质检改读块模型（verify 黑盒质检链路断言随输入源切换同步改造后全绿）；source_import 两通道均能记 done |
| 二期·编辑 | 块级编辑器（方案 C：聚焦编辑，§14）+ 未发布直接编辑 + 手动「重新质检」+ 评论（呈现+升级澄清项 L 系）+ clarify_item.origin **+ push_artifacts 按来源合并**（与 L 系同期货：有 origin 列才有 L 系，有 L 系就必须有合并保护——否则二期每次重推冲掉已答 L 系行） | 窗口 1 全程平台内闭环；L 系行经重推不丢；变更单 diff 视图可用 |
| 三期·变更 | 变更单全流程 + changed 态 + 影响面（机械+人工增删）+ incremental_check/incremental_bundle + 章节级合并 + content_rev/GR-3 + L2 适配（on_answered 窗口化、on_change_confirmed、on_merged）+ skill 侧迁移（§13：pull-artifacts / incremental-guide / 定位更新） | 窗口 2/3 不开新卡完成变更；verify-*.mjs 新增变更链路黑盒；skill 触发回归通过 |

每期遵守既有工程纪律：schema.sql IF NOT EXISTS + migrations 判列补列、pyright strict、
verify-*.mjs 黑盒验收、模板双写同步（runner.mjs + prompts.py）。

## 13. skill 侧迁移（claude-sdlc-skills 仓）

### 13.1 前提认知：skills 仓已是平台任务的运行时规范库

v2.0（2026-09-20）已把主路径搬上平台：sdlc-sync 的 push-artifacts / pull-answers / publish /
push-cases 降级为平台不可用时的本地兜底通道，平台 agent 任务**直接引用 skill 仓资产运行**——
quality_check 用 sdlc-intent 的 scripts 与 references，standard_doc 用 sdlc-sync 的
standard-doc-spec.md，case_gen / design_gen / gate_review 分别引用 sdlc-test / sdlc-design /
sdlc-gate 的 SKILL.md 与模板。v4 是把这个趋势推到底（权威正式翻转），**不是重构消费链**：
六个 skill 的本地读写路径（`sdlc/<需求名>/intake|design|test/`）全部不动。

### 13.2 改动清单（按期归属）

| 类 | 改动 | 期 |
|---|---|---|
| A 定位表述 | sdlc-sync SKILL.md：权威表述翻转（「本地 md 工作权威」→「平台唯一权威，本地 md = 过程工作区/离线兜底」）+ v4 定位段（对照 v2.0 段惯例）+ 兜底子命令新守卫说明（push-artifacts 在 changed 卡 409、publish 受影响面消化守卫、pull-answers 建议语改「changed 卡重算在平台」）；v1/v2 回环描述收进定位变化历史段 | 三期（随 changed 守卫上线同步，避免文档先行于能力） |
| A 契约镜像 | sdlc-sync `references/payload-schema.md` 随平台 push 语义变更同步（clarify origin + 按来源合并 = **二期**，与 L 系同期货；incremental_bundle 草稿类型 = 三期） | 二/三期 |
| B 契约适配 | 平台评论导出端点输出 `comments.json` 兼容格式（service 层映射，resolve_anchors.py 与全部 skill 脚本零改动）；parse_doc.py 零改动（source_import 复用，`--md` 分支已有） | 一期 |
| C pull 面 | sdlc-sync 新增 `pull-artifacts` 子命令：拉平台 digest/audit 的 markdown（v4 后 md 本就是平台重渲染的展示物）+ answers 接口，渲染回 `sdlc/<需求名>/intake/*.md`——本地 sdlc-design/sdlc-test 兜底链的输入源与平台权威对齐 | 三期 |
| D 新增共享资产 | `sdlc-intent/references/incremental-guide.md`（增量梳理规范：受影响章节重梳理、作废行清单、anchor 越界纪律），平台双写模板（runner.mjs + prompts.py）引用同一份——规范在 skill 仓、执行在平台，与现有分工一致且减少双写面 | 三期 |

### 13.3 开发规范约束（Agent Skills 官方最佳实践，platform.claude.com/docs/zh-CN/agents-and-tools/agent-skills/best-practices）

所有 skill 仓改动逐条对照执行，映射到本次改动：

- **长度与结构**：SKILL.md ≤ 500 行、description ≤ 1024 字符（功能 + 何时用 + 触发词）；
  新增内容优先拆 `references/` 而非膨胀 SKILL.md——incremental-guide 落 references，
  pull-artifacts 只在 SKILL.md 加薄层段（选择表加一行 + 子命令规范节）。
- **渐进式披露 + 引用一层深度**：所有参考文件直接从 SKILL.md 链接，不嵌套引用；
  incremental-guide.md 超 100 行则顶部加目录。
- **时效性信息归档**：v1 双向回环（push → 澄清 → pull-answers → 本地重算 → 再 push）的描述
  收进「定位变化历史」段（对照现有 v2.0 段写法），不散在正文冒充现行行为。
- **术语一致**：块/变更单/章节/incremental_check 等术语与 DESIGN-v4 及平台 UI 统一；
  pull-artifacts 输出文件命名对齐既有 intake 约定（digest-<日期>.md 等），本地消费链零适配。
- **脚本约定**：确定性操作首选脚本（pull-artifacts 的拉取渲染落盘如需脚本，显式错误处理、
  无魔法数字、路径一律正斜杠）；执行类命令与阅读类参考在文档中明确区分。
- **触发回归**：description 变更影响触发面——pull-artifacts 加入后须用真实场景回归
  「拉取产物到本地 / 同步 digest 到本地」类表述的触发，并确认不误触发既有 push-artifacts。
- **自由度分级**：兜底通道的守卫说明（changed 409 等）用文本指令（高自由度）；
  评论格式兼容由平台 service 层实现（低自由度，不教 LLM 转格式）。

### 13.4 验收

- 触发回归：新子命令触发词场景 + 既有子命令不误触发。
- 兜底链回归：平台停服时本地全流程仍可用（降级 = 现状），changed 卡本地重推收到明确 409 文案。
- 消费链零适配：pull-artifacts 产出的 intake md 被本地 sdlc-design / sdlc-test 直接消费。

## 14. 编辑器技术方案（已裁决：方案 C 块级聚焦编辑）

**裁决**：不做整篇即时编辑（WYSIWYG），做「渲染态 ⇄ 单块编辑态」切换的块级聚焦编辑。
判断依据：PM 改需求是低频「改文档」而非「写文档」，为低频操作背 contenteditable 的光标/IME/
撤销三大坑（中文 IME 尤甚）不成比例；且块表关系化的红利正在于**编辑器是可替换的编辑面，
权威在数据层**——后期升级路径不封死。

### 14.1 交互规格

- **渲染态**：块列表照常渲染（复用 OriginTab 现有渲染：行内 markdown 样式、图片、评论角标）。
- **块手柄**：hover 出现——拖拽排序（HTML5 原生 drag&drop，不引 dnd 库）、「+」插入兄弟块、
  菜单（升级/降级 heading level 与列表 depth、删除、块类型转换）。
- **单块编辑态**（点击进入，Esc/失焦保存退出）：
  | 块类型 | 编辑控件 |
  |---|---|
  | paragraph / list-item | textarea（markdown 文本，自动增高）+ 工具条按钮插入行内语法（选中点 B 包 `**`） |
  | heading | 级别选择 + 文本输入 |
  | table-row | 行表单（head + cells 数组编辑，整块替换） |
  | image / attachment | 上传替换（upload_media 既有通道，块引用新 mediaId） |
  | whiteboard | 只读（编辑走平台外产出再替换） |
- **输出契约**：编辑器组件输出**全量块数组**——窗口 1 直接编辑与变更单 draft 两场景共用同一
  组件，仅提交端点不同（服务端 diff + LIS 判 move，§5.3）。
- **diff 视图是编辑器组件的必做件**（非可选）：变更单 confirm 界面的块级 diff（edit/insert/
  delete/move 四态着色）复用同一渲染层。

### 14.2 明确不做（边界）

- 整篇连续编辑（回车分块/跨块选区/连续输入）——不引入 contenteditable。
- 行内所见即所得——textarea 源文本 + 语法按钮够用（需求文档行内样式实测低频：粗体/删除线/@人）。
- 多选/批量块操作——低频场景单块操作够用。
- 保存后的事务级 undo——撤销模型：会话内浏览器原生（单块 Ctrl+Z）+ 前端块操作栈（块级操作
  可撤销）；保存后回退 = draft 重拉基线（变更单场景）或重推前次版本。

### 14.3 演进路径（后期优化的调整方向）

升级触发条件：实际使用中出现高频编辑痛点、或 PM 明确要求所见即所得。届时评估 **Tiptap**
（Vue 3 生态唯一匹配的自定义块模型编辑器：heading/paragraph/list 有现成 node，table-row
自定义 NodeView；BlockNote/Lexical 为 React 优先直接排除）。**数据模型不动，换的只是编辑面**——
块表/锚点/变更单/diff 全链路零改动。此裁决记录于 2026-10-13，工作量参考：方案 C 约 1.5~2 周。



| # | 决策点 | 结论 |
|---|---|---|
| 1 | 块模型时机 | source_import 任务，建卡自动排，导入与分析解耦 |
| 2 | text 源同构 | 统一走 --md 分支，服务端零解析代码 |
| 3 | 飞书评论 | 导入任务一次带入（作者/时间/串/解决态），之后冻结 |
| 4 | 存量迁移 | doc JSON → 块表一次性搬，块 ID 原样，已发布卡只迁存储 |
| ① | 导入落库方式 | 直接落库（确定性转录），红线精确化 |
| ② | 触发 | L2 自动排（只自动一次） |
| ③ | 质检前置 | 无 DAG，启动守卫查块表非空，空则 failed 引导 |
| — | 导入一次性 | 有下游锚定后封存重导，变更只走变更单 |
| 5 | 变更单确认 | 不设审批环，提单/确认人手填，界面可见后果 |
| 6 | 影响语义 | edit/move 只标直接锚定；delete 传播 parent 子孙；heading 删不带章节体 |
| 7 | 人工确认环 | diff + 四类清单，默认机械值人可增删 |
| 8 | stale 存储 | 读时推导（算式见 §5.7），澄清项重开是唯一落库态 |
| 9 | 作废 | draft 可删；confirmed 不作废走修正单 |
| ⑤ | confirm 守卫 | 无活跃局部质检任务/未采纳草稿，不卡状态 |
| 10 | 任务类型 | 新 incremental_check，双通道同步 |
| 11 | 合并执行者 | incremental_bundle 草稿→采纳，服务端机械合并 |
| 12 | 全局字段 | 顺带重算 summary/risk_tier |
| 13 | 澄清项 | origin 三值，P/L 两族编号，删除按 origin |
| ③′ | 合并粒度 | 章节级（seq 区间），三粒度贯穿 |
| 14 | GR-3 联动 | content_rev 进基线，横幅报变更单号，两窗口口径不变 |
| 15 | standard 重合成 | 全量重合成；on_answered 窗口精确化 |
| 16 | 权限 | 手填身份必填+全量留痕，不加限制 |
| 18 | skill 侧迁移 | 做方向 a：pull-artifacts 子命令 + 定位表述更新 + 评论导出兼容格式，不重构消费链；改动须符合 Agent Skills 官方最佳实践（§13.3 逐条映射） |
| 19 | 编辑器方案 | 方案 C 块级聚焦编辑（§14）：渲染态⇄单块编辑态、textarea 源文本+语法按钮、零新依赖；Tiptap 留作演进路径（触发条件：高频编辑痛点/所见即所得诉求），数据层不动换编辑面 |
| — | 骨架 | 平台唯一权威；块表关系化；评论系统；编辑器选型后置；change 态；三窗口全做分期执行 |

> **2026-10-13 编辑器裁决**：§11.1 转已裁决，新增 §14（方案 C 块级聚焦编辑：交互规格/明确不做/
> Tiptap 演进路径），§12 二期行与决策清单 #19 同步。
>
> **2026-10-13 doubt-review 第三轮修订（收口轮）**：第三轮 26 条（25 可行动 + 1 低置信），已修订——
> 核心统一解 **串行 confirm**（无未消化前序单才可 confirm），一并消掉连环单发布旁路、后单删前单
> 冻结 heading 的悬空、放弃的 LIFO 语义；发布守卫②改为「changed 必须有草稿且哈希一致，clarifying
> 零新增校验」；放弃补 move 逆 + 块乐观锁 + pending-only 恢复 + rev 单调不回退；**GR-3 判定改为
> 查变更单**（rev 退化为展示计数，横幅 diff 来源 = req_change_item）；gate_review 收回 ==published、
> gate.open 拒 changed；落库定位改 tasks/<task_id>（弃需求名映射，消两通道目录分叉与路径逃逸）+
> 工作区根 meta 下发（承认是新改造）+ selfCheck 两处代码分支 + noDraftTypes 键；封存只限全量
> 替换式重导（对账式永远合法，blk- 痕迹排除软删行）；评论双存储收敛（comment 块不进块表已由
> 导入只写 req_comment 承担，本轮补 external_id 幂等键 + clarify_item.src_comment_id 回链列）；
> audit 行合并规则补齐；窗口 1 编辑×running 质检互斥；对账重导/rebaseline 归三期并加 kind 豁免
> （reconcile 未发布卡保持原状态、rebaseline 放行一次全量）；§2.3 消费面修正（get_requirement_card
> 不读 doc artifact）+ 行内子块 seq 紧随父块约束；存量基线 reqRev 回填入 §10。
>
> **2026-10-13 doubt-review 第二轮修订**：第二轮独立复查 20 条（19 可行动 + 1 权衡，零噪音），
> 已修订——§5.1 状态机重写（**changed 流转不进 REQ_FLOW**，三通道表外专用动作模式；守卫矩阵补
> 发布后任务类型 in(published,changed)、confirm 状态域 {published,changed}、发布双重守卫 ≥ 与草稿
> 哈希一致性）、§3 落库细则（finish 挂点/固定路径 out/doc.json/无草稿类型 selfCheck/媒体走
> upload_media/注入残余风险降格声明）、§5.6 放弃变更动作（aborted 逃生门）、§6.1 现状修正
> （质检现为人工排）+ selfCheck 草稿章节表示校验 + CI 守护扩三处（DEFAULT_RULES 回退副本）、
> §6.2 行定位规则（anchor 整组替换、global 行增量不触碰、upsert 刷 pushed_at）、§4.2 评论工具
> 输出=comments.json 兼容格式、§4.3 软删块评论三裁决、§2.3 doc artifact 退役裁决、§10 对账重导
> 重写为系统变更单 + text 存量卡强制重质检 + 评论幂等 upsert、§12 二期补按来源合并（origin 提前
> 与 L 系同期货）、§13.2 契约镜像拆期。
>
> **2026-10-13 第二轮增量**：#18 裁决后新增 §13 skill 侧迁移（含 Agent Skills 官方最佳实践约束映射），
> §11.4 转已裁决、§12 三期补 skill 侧交付与验收。
>
> **2026-10-13 doubt-review 修订**：对抗式独立复查 20 条发现（零噪音：17 可行动 + 2 权衡 + 1 误读），
> 已修订——§2.2（blk-ID 用 ULID 禁内容哈希；seq 间隔 1000 + 重排常规化）、§2.4（封存规则分源重写：
> 首导永远合法 / text 源块表非空即封存 / 飞书源平台数据封存、下游锚定不禁重导=对账）、§3（落库通道
> 改任务完成回调服务端直读，import_blocks 取消）、§5.1（changed 守卫矩阵逐点补全 + 发布加影响面
> 已消化守卫）、§5.3（move 判定用 LIS）、§5.4（递归 CTE + impact 冻结锚 block_id 禁 seq 数值）、
> §5.5（变更单乐观锁 old_text 校验 + incremental_check 手动创建逃生门）、§5.7（anchor 健康度前置
> 守卫）、§6.1（模板双写 CI 守护）、§6.3（已答保留边界如实表述）、§8（on_merged 补断链 + 取消终局性
> 显式重裁 + 窗口起点存储来源）、§9（两行更新）、§10（origin 回填/anchor 校验/对账重导）、§11
> （skill 迁移策略与飞书单向盲区列入未决）、§12（一期交付物补齐）。2 项权衡放行并留痕：取消终局性
> 窗口化（§8）、飞书侧单向盲区（§11.5）。
