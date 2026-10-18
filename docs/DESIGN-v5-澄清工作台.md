# SDLC 平台 · 澄清工作台与质检体验重构（v5 设计）

> 状态：设计定稿（r4，两轮对抗审查修订版）。日期：2026-10-16。
> 修订记录：r1 初稿 → 第一轮 sdlc-doubt（16 条发现，14 条可行动回写）→ r2
> → 第二轮 sdlc-doubt 复审（15 条发现 #1-#15，全部可行动：已定论卡编号权威分叉 / audit id 前缀 /
> tab 键名幽灵 / D17 半闭合 / savingSeqs 语义误述 / 迁移清单三字段缺口 / 选项回显与 PUT 装配 /
> expandAll 兜底实参 / layer=0 层名来源 / answerText 残缺结构断口，全部回写，见 §11.2）→ r3（#1-#9）→ r4（#10-#15）。
> 前置：`DESIGN-v4-需求平台权威化.md`（块表权威/变更单/评论系统——本文档在其之上重构澄清阶段的交互与产物结构）。
> 交互原型：`sdlc-platform-web/design-mock/index.html`（可交互，含真实数据模式 REQ-2026-001）。

---

## 0. 背景与决策

v4 完成需求平台权威化后，PM 在澄清阶段的体验存在三个结构性问题：

| # | 痛点 | 根因 |
|---|---|---|
| 1 | 需求摘要、体检报告体感弱 | 体检报告 ≈ 澄清清单的只读镜像（同源 P 编号、同序分组、同摘引），没有独占任务；摘要与作答动线零联动（🤔 断点/假设清单躺在折叠 md 里），PM 无理由切过去看 |
| 2 | 原始需求与澄清割裂 | 摘引质量不稳（短至 7 字 / 多段拼接 / 元描述），"看上下文"的成本是一次 tab 横跳——26 题即 26+ 轮横跳 |
| 3 | 九个 tab 信息架构压迫 | 文档中心组织（每产物一页）vs 任务中心心智（把 26 个问题答完） |

**核心决策：以「澄清工作台」为澄清阶段的唯一主战场——体检报告并入、原文以常驻侧栏并置、
作答升级为选择题（AI 推荐可一键采纳）；摘要重定位为"AI 理解校验 + 问题导航"。**

配套四个支柱：

1. **合并**——audit tab 删除；audit 是 clarify 的**超集**（含 ✅已定论条目），超集部分以「已定论卡」形态在工作台获得宿主（r2，审查 A2）；
2. **并置**——原文上下文侧栏（可拖宽 320-760px、可收起），点摘引不切 tab 直接联动定位；
3. **降级兼容**——新数据字段（options/layer/module）全部可选，存量卡与旧 payload 走降级路径，前端先行零后端依赖；
4. **单一解构出口**——服务端出参统一提供 `answerText`（解构后纯文本），前端展示、本地回写、合成输入全走它，杜绝 JSON 串外溢（r2，审查 B1/B2/A4）。

## 1. 信息架构重构

### 1.1 Tab 分阶段分组

```
发布前：原始需求 | 需求摘要(35) | 澄清工作台(26) | 标准需求文档 ⁞ 发布后：设计 🔒 | 门禁 🔒
```

- tab 数 9 → 8（audit 删除）；
- **置灰仅限 设计/门禁**（`status ∉ {published, changed}` 时锁形置灰；现状两者已有空态，统一为置灰视觉）。
  **变更、用例两个 tab 不置灰**：reconcile 对账入口住在「变更」tab 且服务端明确放行未发布卡
  （`change.py` create_reconcile：未发布卡 confirm = 应用 + 保持原状态，r2 审查 A5）；用例 tab 现状无守卫，维持。

### 1.2 模块归宿（净删除仅 AuditTab 一个组件）

| 处置 | 模块 | 归宿 |
|---|---|---|
| 不动 | OriginTab / StdTab / ChangesTab / CaseList / DesignTab / GateTab / TaskPanel / CommentsPanel | 后端 API、状态机全部不动 |
| 合并 | **AuditTab.vue 删除** | 字段归宿见下 |
| ↗ | └ 红/黄/蓝统计卡 | → 就绪度覆盖地图（§2.2） |
| ↗ | └ `it.go`（去向）、`relatedCases` | → 问题卡「影响分析」折叠区；**已定论卡同样挂影响分析**（relatedCases 的展示面由此保留，r2 审查 A2） |
| ↗ | └ **✅已定论条目**（audit 有、clarify 无的行） | → 工作台「已定论」折叠组：只读卡（级别徽标/标题/去向/摘引/关联用例），**带 `data-pid = pidOf(audit.id)`**——`audit:` 跳转协议对已定论编号同样命中（r2 A2）。**渲染条件（r3 二审#1/#2）：`pidOf(audit.id) ∉ clarify seqs` ∧ `go` 以 ✅ 开头**（✅ 为生成侧既有纪律，见 report-template 去向标注）；差集 ∧ 无 ✅ 的「孤儿行」不渲染成卡，工作台页尾折叠上方显示提示条「⚠ N 条体检条目未关联澄清项且无定论标记（可能编号漂移）」点开查 audit.md 原文——防增量通道 audit id（LLM 自带）与 clarify seq（平台 `next_machine_seq` 分配）编号权威分叉时的"同题两卡" |
| ↗ | └ 问题列表/分组/摘引 | 工作台问题卡（clarify 侧同源）；「澄清 Pxx ↗」删（同页），「来自体检.Pxx」徽标保留 |
| ↗ | └ 查看体检报告 markdown 折叠 | 保留，挪至工作台页尾 |
| 升级 | ChecklistTab → WorkbenchTab | 现有能力全保留：失焦自动保存 / 三态筛选 / 跳到下一待处置 / 分组折叠 / 驳回·挂起·重开 / L 系评论回链 / incremental 徽标 / changed 横幅 / published 只读态 |
| 升级 | DigestTab 重定位 | 全部区块保留（折叠 md 不可删——asm 跳转协议依赖）；新增 §3 所列四项 |

**工作台空态**（r2 审查 C3）：`items 空 ∧ audit 无`（submitted/reviewing）→ EmptyState「质检产物尚未推送——推送后此处为澄清工作台」，**右栏原文侧栏照常渲染**（不依赖产物数据）。

## 2. 澄清工作台

### 2.1 双栏布局（核心）

```
┌ 就绪度地图（§2.2）────────────────┐ ┌ 澄清进度 0/26 ──────────┐
│ 维度条 × 5-7（点击过滤）           │ │ [跳到下一待处置][只看待处置]│
└──────────────────────────────────┘ └─────────────────────────┘
┌ 问题流（左，分组折叠）────────────┐ ⇤拖宽 ┌ 原文上下文（右，sticky）─┐
│ ▼ 教研活动 12 题                  │ 320-760 │ 文档导航 · 跟随定位开关   │
│   ▶ 已定论（体检✅项，只读折叠组）  │        │ 📍来自 P06 · 高亮块+前后  │
│   P06 存疑 [来自体检.P06]         │        │ 块级渲染（含图片，点开灯箱）│
│   问题 / 摘引(点击→右栏联动)       │        │ 块上挂「Pxx 待澄清」角标  │
│   选项区(AI推荐✦/B/C) + 补充说明   │        └─────────────────────────┘
│   影响分析(折叠: 去向+关联用例)    │
│   驳回/挂起                       │
└──────────────────────────────────┘
```

- **右栏定位协议**：优先 anchor（块 ID 直达）+ needle 回退。**空 anchor 行降级**（r2 审查 C1，权衡接受）：
  右栏显示静态出处卡（ctx + quote 文本）+「去原文搜索」按钮（跳原始需求 tab 走 needle）——不弹失败 toast；
  反向角标仅对有 anchor 的行挂载。此为 v1.2 前存量卡的已知边界。
- **跟随开关**：默认开（点击摘引自动定位）；关闭后自由滚动不被打断。
- **拖宽/收起**：左缘拖拽 320-760px（localStorage 记忆）；收起 → 单栏 + 右缘竖条恢复。
- **图片三层体验**：侧栏缩略（AI 图述 caption）→ 拖宽 → 点图灯箱。lazy loading。
- **反向角标**：有澄清问题的块挂 `Pxx 待澄清` pill（复用 v4 评论角标挂载机制 `platformByBlock`，DocBlock 已验证可行），点击滚回左栏对应问题并 flash。
- 已处置卡展示：选项全文快照 / 自由文本原样 + 「重新作答」（r2 审查 C4：不引入 EARS 化展示——EARS 仅发生在 std 合成侧）。

### 2.2 就绪度覆盖地图（体检报告的重塑）

- **维度 = 七层缺陷分类法（`layer`，直接挂在 clarify `items[]` 上，v1.6——不做 audit↔clarify join，r2 审查 B4）**，与 `theme`（需求域，作分组）正交；
- **分母口径 = clarify 表**（与发布门禁 `counts` 完全一致，r2 审查 B4）：L 系/incremental/layer 空的行入「未分类」桶（计分母）——地图与门禁永不分裂；**已定论卡不计分母**（不在 clarify 表，与门禁语义一致）；
- 点击维度 → 过滤左栏（与三态筛选叠加）；处置进度实时回写；
- **降级（r2 审查 C2，修正）**：audit/items 无 `layer` → **不渲染维度条**（只留进度条），不以 theme 冒充维度；重推质检后生效。

### 2.3 选择题化作答（Spec Kit 机制借鉴）

- 每题 2-4 个互斥选项（review #15 修正——原稿「2-5」与契约/runner/agent/references 全链统一的「2-4」口径漂移，实现对齐为 2-4），AI 必须给推荐项（带 1-2 句理由）；「以上都不对」展开自由 textarea（补充说明语义）；驳回/挂起不变；
- **保存并发口径（r2 C5 + r3 二审#6/#7 修正）**：
  - **点选即 PUT（无 debounce）**——选项是离散单值；body 显式携带 `{answer, status:'answered'}`，不依赖服务端按 answer 非空推断；
  - **新写 per-seq 串行发送链**（最后值胜）：同 seq 的 PUT 依次排队、前一个完成才发下一个、始终发送最新值——**不是**沿用 `savingSeqs`（现状语义是"在途直接丢弃"，对 textarea blur 无害，对点选即 PUT 是静默丢答）；textarea 的 blur 保存沿用现状在途丢弃，不进发送链；
  - **处置互斥双向**：处置弹窗打开期间 ① 该题选项禁用 ② 抑制该题 blur 保存（`disposeDlg` 非空时 `saveAnswer` 直接 return）③ `confirmDispose` 的 PUT 进入同一 per-seq 发送链（防弹窗确认与在途作答 PUT 后到者胜——`set_disposition` 是 answer/status/note 三元组整行覆写）；「重新作答」（reopen）后行回 pending、选项解锁；
  - **选项回显与选中态（r4 二审#11）：一律不做选中态恢复**——已答卡展示 `answerText`（选项快照/文本），不与新 options 做高亮对齐；「重新作答」= 清空重选，不预选。optionKey 仅作 PUT 载荷内部标识，**永不做回显匹配键**（重推 options 再生成后 key 对新文本即张冠李戴，快照文本匹配则可能无命中——两者都不做，问题消解）；
  - **PUT 装配规则（r4 二审#12）：全量装配、来源装配**——前端 `picked`（选项）与 `extra`（textarea）为独立本地态，textarea v-model 永不直绑 answer；任一 PUT（点选或 blur）都装配完整 answer（一期：`【A】选项全文`[＋`（补充：…）`]；二期：kind=option 携 optionKey/optionText/当前 extra 全量）——不存在部分覆写路径；
- 降级：无 `options` → 自由 textarea（存量卡兼容）。

## 3. 需求摘要重定位

| 区块 | 变更 |
|---|---|
| AI 理解摘要卡（新增） | 概述 + 角色 chips + 模块 chips（挂待澄清数角标） |
| 功能点地图 | 每行挂待澄清数 chip |
| 主流程 | 表格 → **泳道图**（角色泳道/时序流动/🤔 虚线节点/出处小字/跨泳道折线/独立子流程并列）；表格保留为降级导出形态 |
| 状态机流转 | 二期同法泳道化（未排期——D13 backlog，见 §10） |
| 遗留假设 | 从折叠 md 提出为独立卡；折叠 md 保留（asm 协议依赖） |

**数据路径（r2 审查 B3，修正）**：模块待澄清数依赖 v1.6 给 `items[]` 增可选 `module`（与 `fr_points[].module` 同体系，生成侧从需求域映射）；🤔 断点直达依赖 flows 增可选 `pids[]`。**降级不冒充**：字段缺失 → 模块角标隐藏、断点点击仅跳原文定位（不定位题目）。

三 tab 关系：**摘要 = AI 理解了什么，就绪度 = 哪里不清楚，工作台 = 把不清楚变清楚的地方**。

## 4. 原文反向角标

- 有 anchor 的澄清块挂 `Pxx 待澄清` 角标（复用评论角标机制），点击 → 工作台定位对应问题并 flash；
- 全宽阅读、块级编辑、目录、灯箱等现状能力不动。

## 5. 标准文档 Pxx 引用（Kiro Refine 思想）

- 合成约束落在**合成权威规范 `standard-doc-spec.md`**（r2 审查 A3：runner prompt 与本地 sdlc-sync publish 双通道都"必须逐项遵守"它，只改 runner prompt 一处不够）：
  澄清固化条款后缀 `〔Pxx〕`；挂起项遗留假设区带 `〔Pxx·挂起〕`；结构化 answer 解构（kind=option 取 optionText）。
- 前端零改动：`linkifyRefs` 正则 `\b(P\d{2,})\b` 已将 `〔P06〕`/`〔P06·挂起〕` 内裸 P 编号链接化为 `check:Pxx`（全角括号与 · 非 `\w`，词边界成立——审查核实相符），点击走既有协议直达工作台。

## 6. 数据与契约变更（payload v1.6）

### 6.1 澄清 items 增可选 `options` / `module` / `layer`

```json
{"seq":"P06","level":"yellow","theme":"教研活动","module":"教研活动","layer":2,
 "question":"…","ctx":"…","quote":"…","anchor":"blk-…",
 "options":[
   {"key":"A","text":"仅参与教研组全员可见可收，站内信+日历双渠道…","why":"与原文最小语义改动…","recommended":true},
   {"key":"B","text":"两校区全员可见可收"}]}
```

- 全部可选；`layer` 枚举 **1-7**（层名与 dimensions.md 逐层对齐；r4 二审#14：**不设 0 号**——维度外的"补充"类命中按 SKILL.md 既有纪律标注，**不携带 layer、落「未分类」桶**，不为它发明无出处层名）；`module` 与 `fr_points[].module` 同体系；缺省各走 §2.2/§2.3/§3 降级。
- `digest.flows[]` 增可选 `pids[]`（断点直达）。

### 6.2 audit items 增可选 `layer`（已定论卡同样可见维度）

```json
{"id":"体检.P06","level":"yellow","title":"…","where":"…","go":"✅已确认：…","group":"教研活动","layer":2,"quote":"…","anchor":"…"}
```

- **id 形态（r3 二审#2）**：存量与 v1.5 数据带「体检.」前缀（真实数据核实），**所有消费（已定论判定、data-pid、跳转匹配）一律过 `pidOf()` 剥前缀**——与 AuditTab/CaseList/trace.js 现状消费方同口径；v1.6 不强制规范化 id 形态（避免存量兼容面扩大）。

### 6.3 answer 结构化 + `answerText` 单一解构出口

存储态（DB 列仍 TEXT）：

```json
{"kind":"option","optionKey":"A","optionText":"仅参与教研组全员可见可收…（全文快照）","extra":"补充说明"}
{"kind":"text","text":"自由文本答复"}
```

- **判别规则（r2 B1 + r4 二审#15 第四条件；二期终稿 D-E 收紧）**：`json.loads` 成功 ∧ 含 `kind` 键 ∧ `kind ∈ {option,text}` ∧ 分支负载非空（kind=option 含非空 `optionText` ∧ kind=text 含非空 `text`）才算结构化，否则**一律按纯文本原样透传**（`answerText` = 原文）——残缺结构化（有 kind 无负载）不会产出空串 answerText（下游当未答、条款漏固化），也不会在解构层外溢 JSON（当纯文本展示无害）；
- **写入 wire（r2 审查 B1）**：`PUT /answers/{seq}` 的 `answer` 字段仍为字符串，内容为上述 JSON；服务端不强制校验内部结构（宽进）；
- **`answerText`（r2 审查 B2/A4，D15）**：`answers()` 与 detail 的 items 出参统一增 `answerText`——服务端解构（option → `optionText`[＋`（补充：extra）`]，**extra 连接符定稿全角括号中文冒号形态（二期实现定稿）**；text → text；非结构化 → 原文）。**前端展示、sdlc-sync pull-answers 本地回写、变更重开 note、合成输入全部消费 `answerText`**，原始 `answer` 仅透传给需要结构的一方（前端选项回显）；
- 选项全文快照（D5）：防重推 options 再生成后 key 漂移。

### 6.4 服务端与生成端改动清单（r2 全面修正）

| 仓 | 文件 | 改动 |
|---|---|---|
| sdlc-platform-server | `migrations.py` | `clarify_item` 加 **三列**（r4 二审#10，与 §6.1 字段集一致）：`options TEXT DEFAULT ''`、`module TEXT DEFAULT ''`、`layer TEXT DEFAULT ''`（启动自动 ALTER） |
| 〃 | `models.py` / `clarify_item_repo.py` | ClarifyItem 字段 + insert SQL |
| 〃 | `services/requirement.py` | `push_artifacts` 解析 options/module/layer（宽松）；`_audit_view` 透传 layer；`set_answer` 宽进判别；`answers()`/detail 出参增 `answerText`；**携带规则修正（r2 B5 + r3 二审#5）：平台已处置一律以平台为准携带，skill 自带 answer 仅在平台无处置时生效**——修复重开/挂起态被本地同步 answer 吞掉的既有缺陷（选择题化会放大其触发面） |
| 〃 | **`repositories/clarify_item_repo.py`**（r3 二审#5） | `disposition_map_by_seq` 收录口径扩为 `answer 非空 ∨ status≠pending ∨ note 非空`——**重开行（answer=''、status='pending'、note='因变更单#N 重开…'）必须进携带 map**，否则携带规则修正对 B5 点名的主场景无效（"照单必漏改"复发点，单独列行） |
| 〃 | **`services/change.py`**（r2 审查 A4/B6） | ① `_reopen_rows` note 拼 `answerText`（解构后文本，防 JSON 串进 note 与增量合成输入）；② 增量合并 `_machine_clarify` 补 options/module/layer 字段（否则静默丢弃，增量新行永远降级）；③ 增量 auditItems 合并与 `_audit_view` 白名单**统一显式字段清单**（layer 不再一路隐式保留一路显式构造） |
| 〃 | `runner/runner.mjs` | ① 质检 prompt：澄清问题带 options+recommended+理由、items 带 module/layer，**纠正现状"六层"过时文案为七层**；② incremental prompt：产出新字段 ∧ **输入侧消费 detail 的 `answerText` 字段**（r3 二审#4——增量输入直拉 detail 的 items[].answer，不切 answerText 则结构化 JSON 流入增量合成链）；③ standard_doc prompt：answer 消费切 `answerText` + 条款后缀〔Pxx〕。期次拆分（r3 二审#8，**二期 r4 修订**）：③ 与 **② 的消费半边（增量输入取 answerText 指令+数据源）随二期批 2 前移**——堵前端切结构化 JSON（§8「二期切换」）到三期生产端上线之间的 D15 漏点窗口；① 与 ② 的生产端（产出新字段）仍随三期（依赖 payload-schema 契约） |
| claude-sdlc-skills | `sdlc-sync/references/payload-schema.md` | v1.6：§6.1/6.2 字段 + answer 判别规则 + `answerText` 出参 + **incremental_bundle 的 clarify.items/auditItems 结构同步入契约**（r2 审查 B6） |
| 〃 | **`sdlc-sync/references/standard-doc-spec.md`**（r2 审查 A3） | 合成消化规则：answerText 消费 + 澄清固化条款后缀〔Pxx〕 + 挂起项〔Pxx·挂起〕（runner 与本地 publish 双通道共遵） |
| 〃 | `sdlc-sync/SKILL.md` | pull-answers 回写格式：答复列写 `answerText`（**二期 r4 修订：前移批 2〔D-H〕**——堵前端切结构化 JSON 后他通道 pull-answers 回写裸 JSON 的窗口；①② 生产端仍三期） |
| 〃 | `sdlc-intent/references/report-template.md` | pm-checklist 增选项/module/layer 产出约定（双通道一致） |
| 〃 | `sdlc-intent/references/dimensions.md` | 层号枚举落 items 的产出纪律 |
| sdlc-platform-web | ReqDetail/WorkbenchTab/DigestTab/OriginTab/StdTab/trace-jump/group-collapse | §1-§5 全部前端改造（含 audit 组名映射，r2 A1 + r4 二审#13：`revealGrpFor('audit', id)` 的组解析域 = **check 组 ∪ 已定论组**（两侧组集因"增量质检"组与 L 系行分叉，不假设同源同序）；组名 miss 时 **expandAll 展开工作台全部分组（含已定论折叠组）**——只展开 check 列表会让 audit-only 目标卡困在折叠容器里，flashEl 的 scrollIntoView 静默失效）。**tab 键名保留 `'checklist'` 不改**（r3 二审#3）——仅组件/文案更名，`TABS` 数组、load() 的 def 回退、trace-jump 的 check 分支零改动，消除"换键名 → check: 跳转与默认页指向幽灵 tab → 内容区空白"路径 |

## 7. 回归影响面

| # | 波及点 | 处置 |
|---|---|---|
| 1 | `audit:` 跳转协议 4 个发起方（CaseList:106 / DigestTab:66 / ChecklistTab:184 / linkifyRefs 共用） | 协议不动；applyJump audit 分支目标为工作台；**已定论卡带 data-pid，audit-only 编号同样命中**（r2 A2）；组名映射 miss 时 expandAll 兜底（r2 A1） |
| 2 | `group-collapse.js` | audit kind 映射 check 列表 + 兜底展开（见 §6.4 末行） |
| 3 | `TABS` / 默认 tab | 去 'audit'；tab 不进 URL hash（审查核实相符），无书签失效 |
| 4 | **answer 下游**：standard_doc 合成、incremental 合成、reopen note、pull-answers 回写 | **全部切 `answerText`**（D15 单一解构出口）；runner 与 standard-doc-spec 双通道同批上线 |
| 5 | 重推携带 | **修正为平台处置权威**（§6.4，r2 B5）——非"不变" |
| 6 | 存量卡无新字段 | 前端降级：无 options→textarea；无 layer→不渲染维度条；无 module→隐藏模块角标；空 anchor→静态出处卡（§2.1） |
| 7 | runner 30 分钟超时 | 34 题 × 3 选项理由 ≈ 增产 2-4K token，可忽略 |
| 8 | 未发布卡「变更」tab | **不置灰**（reconcile 通道，r2 A5） |

## 8. 实施分期（r2 修正分期缝隙）

| 期 | 内容 | 依赖 |
|---|---|---|
| **一期（前端 + server 热修小包）** | 工作台双栏+侧栏+已定论卡+空态 / audit 合并 / tab 分组（置灰仅设计/门禁）/ 摘要重定位（理解卡+泳道+假设独立；模块角标/断点直达按字段有无降级）/ 反向角标 / trace-jump audit 分支 | 现有数据即可 |
| **一期热修（r3 二审#9）：B5 携带规则修正** | `clarify_item_repo.disposition_map_by_seq` 口径扩展 + `push_artifacts` 携带分支——**纯 service 逻辑、零新字段依赖，随一期先行**：选择题 UI（放大触发面）与携带修正不得存在上线窗口差，否则窗口期内 PM 平台作答 → pull-answers 回写 → push-artifacts 重推会吞答复/吞驳回 | 无（5 行级改动） |
| **一期作答形态（r2 B1）** | 选择题 UI 上线，**answer 写 optionText 纯文本**（`【A】选项全文` 格式）——在二期结构化上线前，所有下游（合成/回写/note）消费的仍是合法纯文本，**零污染窗口** | 无 |
| **二期（server 批次）** | migrations / push_artifacts 解析新字段 / views / set_answer / **answerText 出参** / change.py 三处（A4/B6）/ **runner ③ + standard-doc-spec 合成切 answerText 同批**（r4 修订：**SKILL.md pull-answers 与 incremental-guide 的 answerText 消费亦前移本批〔D-H〕**，堵「二期切换→三期」窗口的 D15 漏点） | 一期已验证 |
| **三期（skills + runner ①② 批次，r3 二审#8）** | payload-schema v1.6（含 incremental_bundle 契约）/ standard-doc-spec / report-template / dimensions / **runner ①② 生产端（v1.6 字段，依赖契约）**。**二期 r4 边界记档（D-L/D-M）**：D-L——增量新行 pids 引用悬空 seq（P 系编号平台合并时才分配、合并不回填），引用完整性规则（仅允许引用存量 seq / 平台合并后回填）在本期 payload-schema v1.6 起草时定义，二期仅白名单透传（无生产方无害）；D-M——agent incremental 分支提示 curl 但 `_CMD_ALLOW` 不含 curl（既有死路），本期 agent 通道全面梳理时处理 | 与二期同节奏合入 |
| **三期完成（2026-10-17，r3 计划经 sdlc-doubt 三轮对抗审查）** | skills cf0d6a8（契约 v1.6 四文件：options/module/layer/pids + answerText 判别 + incremental_bundle 契约节 + danglingPids 出参；report-template 选项化/module/layer 产出约定；dimensions 第 8 条层号纪律；incremental-guide 新字段 + 七层）；server a2070e2（runner ①② + agent 速查卡生产端 + 跨仓锁序断言）+ 6d7a027（**D-I** features/states 白名单三侧同构（二期 r4 留档项，本行补记档）+ **D-M** curl 白名单与文案/docstring 运行时构造 + danglingPids 观测出参）+ 322dff4（verify 失修全面适配：m2/gate/guard/change 断言对齐 D17 与 GR-3 保守判定语义、m5/change 夹具补守恒 guard、verify-v5 生产端静态断言）+ 79620d3（agent 质检 steps 补 v1.6 动作级产出指令——深卡长会话速查卡强度不足，存量刷新实战发现）。**D-L 落地形态**：契约纪律（payload-schema 合法集两类 + incremental-guide 存量引用）+ push/合并出参 danglingPids 观测计数（不改写不拒收——悬空可测可观测，行为零变更）。standard-doc-spec 二期已完成（r4 前移），本期零改动。验收：pytest 149 collected（144+5；环境无 SDLC_EXISTING_DB 时实跑 148 passed + 1 skipped——review #14 复核口径）、verify-all 325 PASS 全绿（1 SKIP=verify-agent 通道需 SDLC_VERIFY_AGENT=1）、verify-v5 17 断言（11 功能 + 6 静态；原注记「11+7」为笔误——review #14 复核口径） | — |
| 二期切换 | 前端 answer 写入从纯文本切结构化 JSON（读侧 answerText 不变） | 二期上线后 |
| 存量刷新 | 需要新字段的卡重推一次 quality_check（依赖三期 runner ①② 上线后自然产出） | 不重推走降级 |

## 9. 决策清单

| # | 决策 | 依据 |
|---|---|---|
| D1 | audit tab 删除并入工作台 | 体检≈澄清只读镜像，无独占任务 |
| D2 | 工作台双栏；侧栏 320-760 可拖、可收起 | 13 寸屏不挤 + 大需求带图可全宽 |
| D3 | 就绪度维度 = 七层 layer，**直接挂 clarify items**（v1.6） | 免 join；与门禁口径统一（r2 B4） |
| D4 | 选择题作答 + AI 推荐项（带理由，常显） | Spec Kit 机制；PM 从作者变审阅者 |
| D5 | answer 结构化 JSON，存选项全文快照 | 重推 options 再生成时 key 漂移不失旧答复 |
| D6 | 主流程表格 → 泳道图；表格保留为降级/导出 | 表格不利于理解；复制仍有用 |
| D7 | 标准文档条款后缀〔Pxx〕 | linkifyRefs 已兼容（审查核实相符） |
| D8 | tab 分阶段分组；**置灰仅设计/门禁** | 变更 tab 承载 reconcile 通道（r2 A5） |
| D9 | 摘要顶部 AI 理解摘要卡 + 🤔 直连 + 假设清单独立 | 摘要获得独占任务 |
| D10 | 原文反向角标（复用评论角标机制，仅 anchor 行） | 从原文出发可发现待答；不做原文就地作答 |
| D11 | 摘引（quote）生成侧本期不动 | 侧栏以 anchor+块表兜底；空 anchor 走静态出处卡（r2 C1 权衡） |
| D12 | 前端先行，新字段全可选+降级；**降级不冒充**（无 layer 不画维度条、无 module 不挂角标） | 体感 70% 零依赖；正交轴不混用（r2 C2） |
| D13 | 状态机图化、std 草稿轻编辑、变更重开 delta 对照 | backlog |
| D14 | **已定论卡**：audit-only（✅已定论）条目在工作台获得只读宿主，data-pid 过 `pidOf` | audit 是 clarify 超集；跳转与 relatedCases 不失宿主（r2 A2）；渲染条件 `差集 ∧ go✅` + 孤儿行提示条防增量编号分叉（r3 二审#1/#2） |
| D15 | **answerText 单一解构出口**（服务端出参） | 前端/回写/note/合成/增量输入统一消费，JSON 串零外溢（r2 B1/B2/A4 + r3 二审#4） |
| D16 | **点选即 PUT + 新写 per-seq 串行发送链（最后值胜）+ 处置互斥双向** | 消除 set_answer 三元组覆写竞态；savingSeqs 在途丢弃语义不适用点选（r2 C5 + r3 二审#6/#7） |
| D17 | **重推携带：平台处置权威**，skill answer 仅补空；携带 map 口径含 note（重开行） | 修复重开/挂起态被吞的既有缺陷（r2 B5 + r3 二审#5） |
| D18 | module/pids v1.6 可选；一期 answer 写纯文本 | 模块过滤数据路径 + 分期零污染窗口（r2 B3/B1） |
| D19 | **tab 键名保留 'checklist'**（组件/文案更名，协议键零改动）；runner 改动按 ③→二期 / ①②→三期拆分 | 消除幽灵 tab 白屏路径与 v1.6 生产端排期悬空（r3 二审#3/#8） |
| D20 | **B5 携带修正随一期热修先行**（纯 service 逻辑，不进二期） | 放大触发面（选择题）与修复不得有上线窗口差（r3 二审#9） |

## 10. 明确不做

- 不改状态机与后端业务守卫（published 终态锁 / changed 通道 / GR 关卡互认全部不动）；
- 不做对话式（多轮访谈）澄清——批量异步清单 + 选项化已把单题成本压到位（Kiro Quick Spec 交叉验证）；
- 不动 sdlc-sync 本地推送的传输机制（契约随 v1.6 更新，pull-answers 改写 answerText）；
- 不做状态机泳道化、StdTab 草稿轻编辑、变更重开 delta 对照（D13 backlog）；
- 不为空 anchor 存量卡补齐锚点（接受静态出处卡降级，重推质检自然修复）。

## 11. 审查裁决表

### 11.1 第一轮（r1 → r2，16 条：14 可行动 / 1 权衡 / 1 噪音）

| 发现 | 摘要 | 裁决 | 回写 |
|---|---|---|---|
| A1 | audit/check 组"同源同序"断言不实（增量质检组/L 系只在一侧） | 可行动 | §6.4 末行、§7-1 |
| A2 | audit-only（✅已定论）条目删 AuditTab 后无宿主；跳转 miss；relatedCases 失展示面 | 可行动（最重要） | D14、§1.2、§2.1 |
| A3 | 合成权威 standard-doc-spec.md 不在清单；本地 publish 双通道漂移 | 可行动 | §5、§6.4 |
| A4 | reopen note 拼接在 change.py `_reopen_rows`（非 requirement.py）——照单必漏改 | 可行动 | §6.4 change.py ①、D15 |
| A5 | 置灰"变更"tab 封死未发布卡 reconcile 通道 | 可行动 | D8、§1.1、§7-8 |
| B1 | answer 写入 wire 未定义；一期写 JSON 的分期缝隙 | 可行动 | §6.3、§8 一期作答形态、D18 |
| B2 | pull-answers 本地回写被 JSON 污染 | 可行动 | D15、§6.4 sdlc-sync SKILL.md |
| B3 | 模块过滤/断点直达无数据路径 | 可行动 | §3、§6.1、D18 |
| B4 | layer 只挂 audit 侧 join 口径未定；与门禁 totalQ 口径分裂；L/incremental 漏计 | 可行动 | D3、§2.2、§6.1 |
| B5 | 重推携带在"本地 answer 非空"时吞平台 status/note；选择题化放大触发面 | 可行动（既有缺陷） | D17、§6.4、§7-5 |
| B6 | 增量合并端无 options 静默丢弃；audit 透传两套口径 | 可行动 | §6.4 change.py ②③、payload v1.6 范围 |
| C1 | 空 anchor 存量卡：needle 跨节点必 miss、角标挂不上 | 有效权衡 | §2.1 降级、§10 |
| C2 | 降级"theme 近似维度"与正交声明自相矛盾 | 可行动 | D12、§2.2 |
| C3 | 工作台空态未定义（+prevTab 残留半条） | 可行动 / 噪音 | §1.2 空态；prevTab 半条噪音（load 已有 def 回退） |
| C4 | "EARS 化答复展示"无数据来源 | 可行动（删字样） | §2.1 末行 |
| C5 | 点选即答与 blur/处置弹窗并发未定义（set_disposition 三元组后到者胜） | 可行动 | D16、§2.3 |

审查备查（核实为相符，不构成发现）：linkifyRefs 全角括号词边界、tab 不进 hash、audit: 四发起方行号、七层枚举对齐、评论角标机制可复用。

### 11.2 第二轮（r2 → r3/r4，15 条，全部可行动）

| # | 发现 | 摘要 | 回写 |
|---|---|---|---|
| 1 | 已定论卡编号权威分叉 | 增量通道 audit id（LLM 自带）vs clarify seq（平台 next_machine_seq 分配）——同题两卡、跳转错卡；差集=✅只是生成纪律无机械保障 | D14 渲染条件 `差集 ∧ go✅` + 孤儿行提示条（§1.2） |
| 2 | audit id 带「体检.」前缀 | 真实数据核实（REQ-2026-001）；data-pid 直挂原始 id 必 miss | §6.2：消费一律过 pidOf，示例改正 |
| 3 | tab 键名幽灵 | 换键名不同步 check 分支/def 回退 → `check:` 跳转与默认页指向不存在 tab → 白屏 | D19：键名保留 'checklist'（§6.4 前端行） |
| 4 | 增量输入侧 answerText 悬空 | §7-4 宣称"全部切"但 runner ② 只改产出；增量输入直拉 detail 的 items[].answer | §6.4 runner ② 补输入侧消费 answerText |
| 5 | D17 半闭合（照单必漏改复发） | disposition_map_by_seq 只按 answer/status 收录，note 不参与——重开行（B5 主场景）修不了 | §6.4 单列 clarify_item_repo.py 行；D17 补口径 |
| 6 | savingSeqs 语义误述 | 现状是"在途丢弃"非"排队"；点选即 PUT 沿用 = 静默丢答 | D16：新写 per-seq 串行发送链（§2.3） |
| 7 | 处置互斥单向 | confirmDispose 不经队列；弹窗打开触发 blur；无 status 的 PUT 会把 rejected/deferred 打回 answered | §2.3 互斥双向三条款 |
| 8 | runner ①② 期次悬空 | v1.6 字段生产端无排期，存量刷新依赖它 | §6.4 期次拆分（③→二期，①②→三期）+ §8 |
| 9 | B5 修复晚于放大窗口 | 携带修正排二期、选择题（放大触发面）一期上线，窗口裸奔；修正零字段依赖本可先行 | D20：随一期热修小包（§8） |
| 10 | 迁移清单与字段集矛盾 | §6.1 三字段 vs migrations 只加 options 一列——照 §6.1 补模型则 INSERT 报错，照迁移行则 module/layer 静默丢弃、维度条/模块角标永不生效 | §6.4 migrations 行改三列（r4） |
| 11 | 选项回显/选中态未定义 | 一期纯文本在二期后的回显、重新作答选中态、重推 options 后 optionKey 匹配语义全部未写 | §2.3：一律不做选中态恢复，optionKey 永不做回显匹配键（r4） |
| 12 | extra 与 answer 共列互覆 | 点选/blur 两种 PUT 的装配规则未定义——blur 按现状语义会把结构化 JSON 顶成纯文本 | §2.3：PUT 全量装配、来源装配，picked/extra 独立本地态（r4） |
| 13 | expandAll 兜底实参未定义 | 只展开 check 列表则已定论组仍折叠，audit: 跳转静默失效（比 miss toast 更隐蔽） | §6.4：组解析域 = check ∪ 已定论，兜底展开全部组（r4） |
| 14 | layer=0 无层名来源 | dimensions.md 只有 1-7 层；「对齐」断言对 0 不成立，0 号桶显示名无出处 | §6.1：枚举去 0，补充类=缺省落未分类桶（r4） |
| 15 | answerText 残缺结构断口 | kind=option ∧ optionText 缺失时解构未定义——空串则下游当未答，回退原文则 JSON 外溢违 D15 | §6.3：判别加第四条件，不满足按纯文本透传（r4） |
