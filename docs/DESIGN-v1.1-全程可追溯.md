# 技术设计：SDLC 产物协作平台 v1.1 —— 产物全程可追溯

> 版本：v1.1 ｜ 状态：待实施 ｜ 需求依据：`docs/REQ-v1.1-全程可追溯.md`（下称 REQ，D1–D8/R1–R5 编号沿用）
> 交互基准：`claude-sdlc-skills/platform-demo/index-v11.html` + `data-v11.js`（已验收原型，函数名可直接对照移植）
> 设计范围：平台后端数据结构与 API、前端交互架构、sdlc-sync 契约、回归脚本。**本文档只定"怎么做"，"做什么"以 REQ 为准。**

---

## 0. 设计总览

### 0.1 设计原则

1. **增量演进，不推倒**：v1.0 的六张表、13 个端点、全量替换语义全部保留；v1.1 只加列、加 artifact 类型、加可选 payload 字段。
2. **新字段全部可选**：payload 不带新字段 → 存默认值；旧库不迁移也能打开（启动迁移器保证结构升级，NULL/空值即"优雅降级"）。
3. **反向索引计算式生成，不冗余存储**：用例↔需求点、用例↔体检问题的"反向"一侧由后端读时聚合（真实数据 42 用例/192 需求点，内存聚合零成本），杜绝双向写不一致。
4. **零新依赖**：前端不加 vue-router/状态库，跳转用一个 reactive 模块；markdown 渲染沿用 marked + DOMPurify。
5. **守卫零回归**：REQ 继承红线涉及的四处代码（§5）只读不改语义，回归脚本先行锁住。

### 0.2 需求断层 → 设计落点对照

| REQ 断层/诉求 | 设计落点 |
|---|---|
| #1 原始需求在外部（D1） | artifact 新类型 `snapshot`（全文 md + pushed_at 时间戳），origin tab 全文渲染 + 依据版本横条（§1.1、§3.3） |
| #2 需求点不知来源（D2/D3） | `fr_point` 加 `source/group_name/module` 三列；前端按 group 出处分组渲染；module ↔ 功能地图双向 chips（§1.2、§3.3） |
| #3 体检有出处无内容（D4/D5） | audit `content_json.items[]` 增 `group/quote`；clarify 增 `quote`、theme 必填；体检/澄清同域分组 + 互跳（§1.2、§3.3） |
| #4 标准文档错位（D6/D7） | publish 的 standardMd 语义变重写版（payload 不变）；前端 linkifyRefs 四类引用链接化（§2.6、§3.3） |
| R1 用例↔需求点双向 | 用例 points 既有；`frPoints[].coveredBy` 后端计算；前端双侧 chips（§1.5、§3.4） |
| R2 用例↔体检问题 | `test_case` 加 `verify_refs` 列（skill 从 cases.md 重点验证项提取）；audit 条目 `relatedCases` 后端反推（§1.2、§1.5） |
| R3 标准文档→用例两跳 | 复用 R1 的 coveredBy，无新增数据 |
| R4 缺陷编号引用 | `test_case` 加 `defect_refs` 列（纯展示文本），点击 toast 提示留本地（§1.2、§3.4） |
| §2.6 全局需求域顺序 | `requirement` 加 `domains` 列，push payload 顶层可选 `domains[]`（REQ §3.5 漏列，本设计补上，见 §1.4） |

### 0.3 改造面全景

- **backend**：schema.sql、新增 SchemaMigrator、5 个 model/repo 小改、RequirementService（push/detail）、CaseService（upsert/view/list）。
- **frontend**：新增 `src/trace.js`（跳转基建），ReqDetail.vue 五 tab 重度改造，CaseList.vue 中度改造，App.vue 薄改（消费跳转），style.css 增高亮/引用样式。
- **sdlc-sync**：payload-schema.md 升 v1.1，SKILL.md 三个动作流程更新。
- **scripts**：sync-real/verify-guard/verify-m2 扩展断言与 payload，新增 verify-m3.mjs。

---

## 1. 数据模型设计

### 1.1 快照载体：artifact 新类型 `snapshot`（D1）

**决策**：快照不进 `requirement` 表，落 `artifact` 表一行 `type='snapshot'`。

| 考量 | 结论 |
|---|---|
| 全文 md 体积（几十 KB） | artifact.markdown 本就是全文载体（digest/audit 同款）；不进 requirement 表可避免 `listAll()` 列表查询拖大 blob（RequirementRepo.listAll 走 SQL 列别名，快照列若在主表有被误捞风险） |
| 时间戳/版本 | 直接复用 `pushed_at`（=快照时间戳）与 `version`（每次刷新 +1），与 REQ"每次推送刷新并更新时间戳"一致 |
| 发布冻结（D1） | 不需要新守卫：`RequirementService.pushArtifacts:80-82` 已拒绝 published 后推送，快照随推送刷新 ⇒ 天然冻结。`publish()` 不触碰 snapshot |
| 读路径 | `detail()` 组装 `snapshot:{md, at, version}`（at = pushedAt，改名对齐 demo 的 `snapshot.at`），无快照时该 key 缺省 |

**payload 侧**：push-artifacts 顶层可选 `snapshot:{md}`；md 非空才刷新（可选语义：不带 = 保留旧快照）。真实全文由 sdlc-sync 经 lark-cli 拉取（REQ §6），平台不做抓取。

### 1.2 表结构变更清单

> `group` 是 SQL 保留字，列名用 `group_name`，JSON 字段名仍为 `group`（RowMapper 映射一次即可）。其余新列名与 JSON 名一致。

```sql
-- fr_point：需求点溯源三列（D2/D3）
ALTER TABLE fr_point   ADD COLUMN source     TEXT DEFAULT '';  -- 行级出处，如 详述6.3.1+评论区
ALTER TABLE fr_point   ADD COLUMN group_name TEXT DEFAULT '';  -- 出处分组组头，如 §7.2 修改比赛（状态×字段矩阵）
ALTER TABLE fr_point   ADD COLUMN module     TEXT DEFAULT '';  -- 归属功能模块，对应 features[].m 去 ⭐ 后

-- clarify_item：原文摘引（REQ §3.4）
ALTER TABLE clarify_item ADD COLUMN quote TEXT DEFAULT '';

-- requirement：全局需求域顺序（§2.6），JSON 字符串数组，空串=未给定
ALTER TABLE requirement ADD COLUMN domains TEXT DEFAULT '';

-- test_case：用例域追溯两列（R2/R4），JSON 字符串数组
ALTER TABLE test_case   ADD COLUMN verify_refs TEXT NOT NULL DEFAULT '[]';  -- ["体检.P06","需求.A2"]
ALTER TABLE test_case   ADD COLUMN defect_refs TEXT NOT NULL DEFAULT '[]';  -- ["BUG-02（R1·待修复）"] 纯展示文本
```

schema.sql 同步补齐以上列定义（新库直达），存量库靠 §1.3 迁移器补列。

**content_json 内部结构扩展**（无表变更，透传）：
- digest 视图 `features[]` 增 `src`（功能地图出处列数据源，REQ §3.5 漏列，本设计补上）。
- audit 视图 `items[]` 增 `group`、`quote`。
- 提取点：`RequirementService.digestDigestView():256-271`、`auditView():273-290` 各加字段透传。

### 1.3 启动迁移器 SchemaMigrator

SQLite 无 `ADD COLUMN IF NOT EXISTS`，且 schema.sql 对已存在的表不加列。新增 `config/SchemaMigrator.java`（`ApplicationRunner`，启动时执行一次）：

```
MIGRATIONS = [ ("fr_point","source"), ("fr_point","group_name"), ("fr_point","module"),
               ("clarify_item","quote"), ("requirement","domains"),
               ("test_case","verify_refs"), ("test_case","defect_refs") ]
对每对 (table, column)：PRAGMA table_info(table) 无该列 → 执行 ALTER TABLE，打日志
```

幂等、顺序无关。`HealthController` 版本号随本迭代 0.1.0 → 0.2.0（连同 pom.xml）。

### 1.4 全局需求域顺序（domains）的存取与回退

REQ §2.6 要求域顺序"由 sdlc-sync 推送时给定"，但 REQ §3.5 契约清单未列载体——**本设计补上：push-artifacts payload 顶层可选 `domains: string[]`**。

- **存**：`RequirementRepo` 新增两个专用方法 `getDomains(reqId)` / `setDomains(reqId, json)`；**Requirement 实体不加字段**（detail 的 `card` 是 POJO 直出，避免出现 JSON 字符串字段污染 card）。push 时 payload 带 `domains[]`（非空校验：去空白、保序去重）→ 覆盖写；不带 → 保留旧值（可选字段语义）。
- **读（回退解析，detail() 内做）**，输出顶层 `domains: string[]`：
  1. 库内 domains 非空 → 直接用；
  2. 否则扫 audit 视图 `items[].group` 首现顺序；
  3. 否则扫澄清 `items[].theme` 首现顺序；
  4. 否则返回 `[]`（前端全部落"其他"桶）。
- 旧数据（REQ-2026-001 现存 62 条 theme 全空）走 2/3 步即可得到可用组序，无需迁移。

### 1.5 反向索引：计算式生成

detail() 内一次 `caseRepo.list(reqId)`（42 行量级）内存聚合两张表：

| 计算字段 | 生成规则 | 消费方 |
|---|---|---|
| `frPoints[].coveredBy: string[]` | 遍历用例，`parsePoints(t.points)`（CaseService.java:201-208 既有方法）中等于 `fr.id`（全 id 匹配，如 `需求.FR-27`）→ 收集用例 id | 需求点行"被哪些用例覆盖" chips（R1②）、R3 两跳 |
| audit `items[].relatedCases: string[]` | 遍历用例，`verify_refs` 数组中含该条 id 全称（如 `体检.P06`，精确相等）→ 收集用例 id | 体检问题行"关联用例" chips（R2） |

实现位置：`RequirementService.detail():212-240` 组装 frPoints 后补 coveredBy；audit 取 `artifactView` 后解析 content 逐条补 relatedCases（或 artifactView 增补逻辑，实施自选，保持 detail 单出口）。`verify_refs` 中的 `需求.Axx` 不参与 audit 反推（它的落点是 digest 假设清单，见 §3.4）。

### 1.6 实体与 Repo 改动清单

| 文件 | 改动 |
|---|---|
| `model/FrPoint.java` | + `source/group/module`（String，默认 ""）；`group` 字段 ↔ `group_name` 列映射 |
| `model/ClarifyItem.java` | + `quote`（String，默认 ""）——detail 的 items 是 POJO 直出，加字段即出现在响应 |
| `model/TestCase.java` | + `verifyRefs="[]"`、`defectRefs="[]"`（JSON 字符串，仿 points 模式） |
| `model/Requirement.java` | **不动**（domains 走 repo 专用方法，§1.4） |
| `repo/FrPointRepo.java` | insert/replaceAll/RowMapper 补三列 |
| `repo/ClarifyItemRepo.java` | insert/RowMapper 补 quote（answerMapBySeq 等答复逻辑不动） |
| `repo/TestCaseRepo.java` | MAP/COLS/INSERT 补两列；**`updateFromSync():52-56` 补写入 verify_refs/defect_refs（来源字段，随推送走）**；**`updateForm():59-64` 不碰这两列**（表单不产生验证/缺陷引用）；`list():66-78` SQL 加 `tc.verify_refs AS verifyRefs, tc.defect_refs AS defectRefs`（字符串，service 层转数组后出） |
| `repo/RequirementRepo.java` | + getDomains/setDomains（find 的 `SELECT *` + 显式映射不受新列影响，无需改） |
| `repo/ArtifactRepo.java` | 不动（upsert 天然支持新 type） |

---

## 2. API 契约设计

### 2.1 push-artifacts v1.1 payload（POST /api/requirements/{id}/artifacts）

加粗 = v1.1 新增/变更；未列出的既有字段不变。

```
{
  snapshot: { md }                              // 可选；md 非空则刷新快照（§1.1）
  domains: [string]                             // 可选；全局需求域顺序（§1.4）
  digest: {
    summary, roles[], md?                       // 不变
    features: [{ role, m, func, cov, src }]     // src 可选：出处，如 详述6.2-6.7
    fr_points: [{ id*, name, source, group, module }]  // 后三个可选：行级出处/出处分组/归属模块
  }
  audit: {
    red, yellow, blue, md?                      // 不变
    items: [{ id*, level, title, where, go, group, quote }]  // group/quote 可选
  }
  items: [{ seq*, level, theme*, question*, ctx, answer?, quote }]
  //                                             theme 升为必填（REQ §3.5）；quote 可选
}
```

**校验规则增量**（`RequirementService.pushArtifacts:78-152`）：

| 规则 | 处理 |
|---|---|
| `theme` 缺失/空 | `Json.required` → 400「缺少必填字段：theme」（契约 v1.1 起；存量库数据不迁移，仅新推送强制） |
| `domains[]` 元素空白/重复 | 去空白、保序去重后入库；全空视同未给定 |
| fr_points 的 `group/module/source` | 不校验取值域（组名/模块名是本需求的业务词汇，平台不做枚举）；空串合法 |
| audit items 的 `quote` | 任意长度（平台无 20 字限制，REQ §2.3）；空串合法 |
| 快照 | `snapshot.md` 空缺省不刷新；**published 拒推既有守卫不动**（快照冻结的保障） |

**既有守卫保留**：seq 批内去重（:105）、fr id 批内去重（:117）、重推按 seq 保留答复（:121-133）、全删重插 + @Transactional（:135-136）。

**响应**：`{ok, items, frPoints, carriedAnswers}` 不变（可加 `snapshotUpdated:boolean` 供脚本断言，可选）。

### 2.2 用例批量 upsert payload（POST /api/cases）

`items[]` 每条新增可选字段（`CaseService.syncUpsert:43-112` 解析处）：

```
verifyRefs: string[]   // 缺省 []；"体检.Pxx" / "需求.Axx" 全称，来自 cases.md 重点验证项表
defectRefs: string[]   // 缺省 []；纯展示文本，如 "BUG-02（R1·待修复）"，来自缺陷跟踪表
```

- 序列化沿用 `pointsJson()` 模式加 `stringsJson()` 帮助方法；`formCreate()` 两字段固定 `[]`（表单通道不产生）。
- **红线**：defectRefs 仅编号引用文本，平台不做缺陷详情、不做解析建模（R4）；verifyRefs 不做平台侧编辑。

### 2.3 detail 响应（GET /api/requirements/{id}）v1.1 目标 shape

```
{ card: {...不变},
  snapshot?: { md, at, version },          // 无快照时缺省（§1.1）
  domains: [string],                        // 回退解析后的组序（§1.4）
  items: [{ seq, level, theme, question, ctx, answer, answeredAt, quote }],
  digest: { content: { summary, roles[], features[{ role, m, func, cov, src }] },
            markdown, version, pushedAt },
  audit:  { content: { red, yellow, blue,
                       items[{ id, level, title, where, go, group, quote, relatedCases[] }] },
            markdown, version, pushedAt },
  frPoints: [{ id, name, source, group, module, coveredBy[] }],
  answered, totalQ, frCount,                // 不变
  standard?: { markdown, version, pushedAt } // 不变（语义=重写版）
}
```

### 2.4 用例列表/详情响应变更

- 列表行（`TestCaseRepo.list`）与详情（`CaseService.view():185-194`）均输出 `verifyRefs: string[]`、`defectRefs: string[]`（**数组**，service 层解析后出，前端不做 JSON.parse；既有 points 保持 JSON 字符串现状不动，避免无谓扩散）。
- `readonly` 标记、评论、乐观锁逻辑全部不动。

### 2.5 不变项

- `PUT answers/{seq}`、`GET answers`（答复读写不涉及新字段；pull-answers 回写只取答复与状态）、`publish`（payload 不变，standardMd 语义变重写版）、状态机、`GET /api/fr-points`（软校验数据源，含新列与否不影响其形状）。
- 全量替换语义、artifact version+1、published 拒推。

---

## 3. 前端设计

### 3.1 跨页跳转基建 `src/trace.js`

现状痛点：无路由，App.vue 用 `page/currentReqId` 两个 ref 切页；跳转需要"跨页 + 切 tab + 定位"三段描述，用 prop/emit 链会穿过三层组件。**方案：模块级 reactive 单例**：

```js
// src/trace.js
import { reactive } from 'vue'
export const pendingJump = reactive({ nonce: 0, page: '', reqId: '', tab: '', kind: '', id: '', needle: '' })
export function jumpTo(d) { Object.assign(pendingJump, d, { nonce: Date.now() }) }
// + 高亮/定位工具（§3.2 移植）: flashEl(el), flashNeedle(needle, container), highlightIn(...), firstQuote(s), covP(s)
```

**跳转描述符协议**（kind → 目标与定位方式）：

| kind | page/tab | 定位方式 | 触发场景 |
|---|---|---|---|
| `fr` | reqs / digest | `[data-frid="<全id>"]` 滚动+闪烁 | 标准文档 FR 引用、用例 points chip、功能地图关联 chips |
| `audit` | reqs / audit | `[data-pid="<Pxx>"]` 滚动+闪烁 | 澄清回跳、标准文档 体检.Pxx、覆盖薄弱行 |
| `check` | reqs / checklist | `[data-pid="<Pxx>"]` | 体检→澄清（D5）、标准文档裸 Pxx |
| `asm` | reqs / digest | 先展开 digest 原文折叠 → `flashNeedle(id)` | 标准文档 需求.Axx、用例 verifyRefs 中 A 引用 |
| `needle` | reqs / origin | `flashNeedle(needle)` 快照文本定位 | 各处出处 cite（locate） |
| `case` | cases | 按 reqId 过滤 + 打开详情弹窗 + 闪烁行 | 需求点 coveredBy chips、体检 relatedCases chips |

**消费方式**：App.vue `watch(() => pendingJump.nonce)` → 设 page/currentReqId；ReqDetail / CaseList 各自 watch nonce（reqId 匹配时）执行本页动作。时序常量照抄 demo：`nextTick` 后 80ms 再定位（asm 150ms）；闪烁 class 2300ms 后移除；toast 2600ms。

### 3.2 定位与高亮内核（自 demo 移植）

| demo 函数（index-v11.html） | 移植去向 | 说明 |
|---|---|---|
| `highlightIn(container, needle)`（:481，TreeWalker 文本节点 + surroundContents） | trace.js | 原样；跨节点失败返回 false |
| `flashNeedle(needle)`（:437，清旧 mark → 高亮 → 滚动） | trace.js | `mark#snapshot-hl` 复用；调用方传容器（origin 快照容器 / digest 折叠容器） |
| `locate(needle)`（:501） | ReqDetail | **升级为多 needle 策略**：`[原文, /详述(\d+(?:\.\d+)*)/ 提取, 首个编号提取]` 依次尝试（处理 `详述6.2-6.7` 这类区间出处），全失 → 诚实 toast：「当前快照未包含「…」；若出处为小节号，可能对应快照未收录段落」 |
| `jumpAudit/jumpClarify`（:508+） | ReqDetail | 改为 `jumpTo({kind:'audit'/'check', id})` |
| `linkifyRefs`（:394）/`stdRefClick`（:408） | ReqDetail std tab | 正则与 data-ref 映射原样（`需求.FR-xx`/`体检.Pxx`/`需求.Axx`/裸`Pxx`/裸`FR-xx` 五种） |
| `firstQuote`（:477）/`covP`（:476） | trace.js | 「」摘引锚提取 / cov 中 Pxx 提取 |
| `groupItems(items,key)`（:459） | ReqDetail | **改签名** `groupItems(items, key, domains)`——DOMAINS 常量（demo :322）不再前端硬编码，改消费 detail 返回的 `domains`；未知域仍落"其他"桶追加尾部 |
| `frGroups`（Map 首现序聚桶） | ReqDetail computed | 需求点出处分组 |
| `frByModule(m)`（:455） | ReqDetail | 功能地图行 → 需求点 chips（module 匹配，m 去 `⭐ ` 前缀） |

### 3.3 需求域五 tab 改造点（ReqDetail.vue）

**origin（原始需求）**
- 有 `snapshot`：顶部横条 = 快照时间戳 + 依据版本声明（"本快照是质检与澄清的依据版本；最新修订以飞书文档为准"）+ 飞书链接 + "重推产物时同步刷新；发布后冻结"；下方 marked 渲染全文（容器挂 ref，供 flashNeedle）。
- 无 snapshot：回退现状（链接/文本形态）+ 提示"快照将由 sdlc-sync 首次推送生成"。

**digest（结构化 Digest）**，自上而下：
1. 需求概述、角色 chips：不变。
2. 功能点地图：每行加两列——出处 `src`（cite → `locate`）；覆盖列沿用 cov 渲染 + `covP(cov)` 命中时挂 `体检.Pxx ↗`（→ kind:audit）；行尾关联需求点 chips（`frByModule`）→ 点击 kind:fr。
3. 需求点注册表（D2）：`frGroups` 按 group 出处分组渲染，组头 = 组名 + 计数，组序 = 首现序；行 = 编号（`<code :data-frid="f.id">`）/ 简题 / 行级出处 / 归属模块 / **coveredBy chips**（kind:case 跳用例页）。group 为空的旧行落"未分组"桶。
4. digest 原文折叠：默认收起；kind:asm 跳入时自动展开 + `flashNeedle` 定位假设行（A 编号文本直接可匹配渲染后 DOM）。

**audit（体检报告）**
- 三色统计不变。明细 = `groupItems(audit.items, 'group', domains)`，组头（域名/计数/红黄蓝分布）、组内 `levelRank` 排序照 demo。
- 每条：`data-pid` 锚点；出处 cite → `locate(firstQuote(quote) || where)`；quote 非空渲染引用块 + "在快照中定位 ↗"；`澄清 Pxx ↗`（kind:check）；go 状态文本保留；**relatedCases chips**（kind:case）。

**checklist（澄清清单）**
- `groupItems(items, 'theme', domains)`——与 audit 同一函数同一 domains，保证组序一致（D4）；横条对应声明 + 进度条保留。
- 每条：ctx cite → locate；quote 引用块（同 audit）；`来自体检.Pxx ↗`（kind:audit）；**PM 答复交互、blur 保存、published 只读、allAnswered banner 全部不动**。

**std（标准需求文档）**
- `renderStd()` = `linkifyRefs(marked 渲染)`，容器 `@click` 委托 `stdRefClick`（四种引用 → kind:fr / audit / check / asm）；顶部横条声明重写版语义 + "全文引用可跳转"。mark→DOMPurify 顺序注意：**先 sanitize 再 linkify**（链接是受控生成的 span，非用户内容，仍保持在 sanitize 之后插入）。

### 3.4 用例页改造点（CaseList.vue，R1–R4）

| 诉求 | 改造 |
|---|---|
| R1① points chip 可点 | 行/详情的 points chips → `jumpTo({page:'reqs', reqId, kind:'fr', id})`；「一跳看到关联需求点原文上下文」= 落 digest 需求点行（行上有行级出处可再一跳进快照） |
| R1② 需求点行 coveredBy | 在 ReqDetail digest tab（§3.3），数据由后端计算 |
| R2 用例侧"重点验证" | 详情弹窗加"重点验证"区：verifyRefs chips，`体检.Pxx` → kind:audit，`需求.Axx` → kind:asm；列表行已返回 verifyRefs 可选渲染角标 |
| R2 体检侧"关联用例" | audit 条目 relatedCases chips（§3.3），平台反推免存储 |
| R4 缺陷引用 | 行/详情展示 defectRefs 文本 chips；点击 → toast「缺陷留在本地工作区 cases.md（不上平台）」，无详情页 |
| 消费 case 跳入 | watch pendingJump（page='cases'）→ 设 reqId 筛选 → 拉列表 → 打开目标用例详情弹窗 + 闪烁行 |
| 不变 | 迭代/需求/状态/来源/搜索筛选、表单提交、编辑乐观锁、评论 |

### 3.5 降级矩阵（旧数据 = 无任何新字段）

| 缺失 | 表现 |
|---|---|
| snapshot | origin 回退链接形态 + 生成提示；所有 locate 诚实 toast |
| fr.source/group/module | 出处列/组头不渲染；注册表整体落"未分组"单组（平铺观感） |
| quote（audit/clarify） | 不渲染引用块；出处 cite 退回 where/ctx 文本定位 |
| domains（库内且无法推导） | 全部落"其他"桶 |
| coveredBy/relatedCases | 空 → chips 不渲染（自然无 R1/R2 反向入口，正向 points chip 仍可用） |
| verifyRefs/defectRefs | 详情不渲染对应区块 |

原则：**空则隐藏，绝不报错、绝不渲染死链接**（D7 的"诚实"要求）。

### 3.6 样式增量（style.css）

自 demo 移植并适配 `:root` token：`.flash`（flashbg 2.2s 黄底渐隐 keyframes）、`mark#snapshot-hl`、`.cite`（出处链接态）、`.quote`（引用块 + .qlb 头）、`.reflink`（标准文档引用 span，悬停下划线）、`.linklike`（可点 chip）。现成的 `.chip.warn/.chip.real` 可用于 coveredBy（sdlc-test 来源）与 WEB-（表单来源）用例 chips 区分。

---

## 4. sdlc-sync skill 契约变更（claude-sdlc-skills/skills/sdlc-sync/）

### 4.1 payload-schema.md → 契约 v1.1

按 §2.1/§2.2 字段表更新；版本头标注 v1.1 及与 v1 的差异清单；明确：
- `theme` 自 v1.1 起必填（400）；`quote/source/group/module/src/domains/snapshot/verifyRefs/defectRefs` 全部可选。
- publish 一节：payload 不变，standardMd 语义 = **重写版**（骨架：元信息头含"质检依据快照时间"→ 概述 → 角色与职责 → 分需求域定稿正文（✅ 定案处、删除线废弃条款）→ 遗留假设 ⚠️ → 附录：澄清结论表）。

### 4.2 SKILL.md 流程变更

| 动作 | 变更 |
|---|---|
| push-artifacts | ① 推送前经 lark-cli 拉飞书全文 → `snapshot.md`（并报告字节数）；② 组装 `domains[]`（需求域聚类结果，体检/澄清分组同源）；③ fr_points 补 source/group/module、features 补 src、audit items 补 group/quote、澄清 items 补 quote + theme 必填；④ 红线不变：seq 不得改、重推保留平台答复 |
| pull-answers | 不变（只回写答复与处理状态） |
| publish | 合成重写版（骨架见上），**以原始需求全文快照为底稿**整合体检定论与澄清答复，规范与质量自查清单见 `references/standard-doc-spec.md`；仍必须用户过目后推送（红线不变） |
| push-cases | 从 cases.md「重点验证项」表提取 verifyRefs（`体检.Pxx`/`需求.Axx` 全称）；从「缺陷跟踪表」提取 defectRefs（仅 `BUG-xx（<状态>）` 展示文本）；红线不变：缺陷生命周期/执行结果/证据不上平台（R5 未裁决前亦然） |

### 4.3 demo 的处理

demo 已是验收基准且 REQ 与其无冲突，**本迭代不改 demo**。已知差异记录在案：demo 的 DOMAINS 是前端常量，正式实现改为 API 下发（demo 数据单需求内嵌，形态一致，不影响验收对照）。

---

## 5. 守卫不回归清单（改造中只读、回归脚本锁死）

| 守卫 | 代码位置 | verify 锁 |
|---|---|---|
| 重推按 seq 保留已答复 | RequirementService.java:121-133 + ClarifyItemRepo.answerMapBySeq | verify-guard #1-#4 |
| 发布后锁定（拒推/拒答/终态） | RequirementService.java:80-82、:172-174、:183-185 | verify-guard #5、verify-m2 #8 |
| 用例版本冲突整批拒绝 | CaseService.java:71-89（ConflictException :213-221）+ GlobalExceptionHandler | verify-guard #6-#7 |
| sdlc-test 来源只读 | CaseService.java:137-139、:192；TestCaseRepo.updateForm 不碰来源字段 | sync-real #13 + verify-m3 补断言 |

新增列/字段不得进入以上任何一条的判断路径（例如 updateForm 的 UPDATE 列清单不许加 verify_refs/defect_refs/tool/owner/status）。

---

## 6. 回归脚本与验收设计（scripts/）

> 前置：real-data.seed.js 需同步扩展新字段（theme、quote、group/source/module、domains、snapshot 节选、verifyRefs/defectRefs 样例）。

| 脚本 | 变更 |
|---|---|
| sync-real.mjs | payload 补 theme/quote/fr 新字段/snapshot/domains；新增断言：snapshot 回读（md 非空、at、version≥1）、fr 首条 source/group/module 回读、无 quote 条目 detail 不报错、audit items.group 回读、domains 回读 = 7 域定序、`需求.FR-27` 的 coveredBy 含 TC-17；既有 15 断言保持 |
| verify-guard.mjs | payload 补 theme；新增：重推带 snapshot → 快照 at 更新 & version+1；新增：重推不带 snapshot → 旧快照保留；既有 7 断言保持 |
| verify-m2.mjs | payload 补 theme；8 断言不变（断言 7 的「含澄清结论」对重写版附录仍成立） |
| verify-m3.mjs（新增） | R1：用例 points ↔ frPoints.coveredBy 双向一致；R2：verifyRefs 回读 + audit item.relatedCases 反推正确（含 需求.Axx 不进 audit 反推的负断言）；R4：defectRefs 透传 + formCreate 默认 []；降级：无新字段的旧形 payload 推送后 detail 不缺 key、不 500 |

验收流程（REQ §5）：三脚本 + verify-m3 全绿 → §5.1 对照 demo 逐条人工 → 真实需求「区域教学技能比赛」清库重推（sdlc-sync 经 lark-cli 拉真实快照）→ 形态与 demo 一致。

---

## 7. 实施任务拆分（建议顺序）

| # | 任务 | 依赖 |
|---|---|---|
| T1 | SchemaMigrator + schema.sql/DDL + repo/model 字段落位（§1） | — |
| T2 | push/detail/cases 解析与响应改造（§2），含 coveredBy/relatedCases/domains 解析 | T1 |
| T3 | 前端 trace.js + 样式 + App/两页消费骨架（§3.1/3.2/3.6） | — |
| T4 | 需求域五 tab 改造（§3.3） | T2+T3 |
| T5 | 用例域 R1–R4（§3.4） | T2+T3 |
| T6 | sdlc-sync 契约文档 v1.1 + SKILL.md（§4） | T2 定稿后 |
| T7 | 脚本扩展 + verify-m3 + seed 扩展（§6） | T2 |
| T8 | 清库重推真实需求 + demo 对照人工验收 + 版本号 0.2.0 | T1–T7 |

T3 可与 T1/T2 并行；T6/T7 可并行。

---

## 8. 明确不做与开放项

- **R5（执行轮次摘要上链）**：本期不实施、不预埋字段；R1–R4 完成后向需求方呈报选项（REQ §4-R5），裁决变更时 verify_refs 同款模式加列即可，设计留有该扩展位但代码不写。
- 出处 → 快照**精确锚点 ID 体系**：不做，needle 文本定位 + 多策略回退（§3.2）。
- fr_point 的原文摘引字段：REQ 未要求，不加；如后续体检/需求点需要摘引级溯源，按 clarify.quote 模式加列。
- 缺陷详情页/缺陷建模、执行态上链、登录权限、通知自动化、措辞级对照视图：全部不做（REQ §6）。
- 平台侧编辑 domains/quote/出处：不做（唯一来源 = sdlc-sync 推送，保持"本地 md 是工作权威"）。

---

## 附录 A：v1.1 完整 ER 设计

### A.1 实体关系总览

```
                          ┌──────────────┐
                          │  requirement │  (需求主卡)
                          └──────┬───────┘
        ┌───────────────┬────────┼───────────────┬──────────────┐
        1:N             1:0..4   1:N             1:N            
        │               │        │               │              
┌───────▼──────┐ ┌──────▼─────┐ ┌▼───────────┐ ┌─▼──────────────────┐
│ clarify_item │ │  artifact  │ │  fr_point  │ │     test_case      │
│ (澄清问题)    │ │ (产物快照)  │ │ (需求点)    │ │ (测试用例)          │
└──────────────┘ └────────────┘ └────────────┘ └─────────┬──────────┘
                                   type ∈ digest|audit|  1:N
                                   standard|snapshot     │
                                             ┌───────────▼────────┐
                                             │    case_comment    │
                                             │ (用例评论)          │
                                             └────────────────────┘
```

- **无物理外键**：全部是软引用（req_id / 复合主键前缀），表间一致性由应用层守卫保证。
- artifact 每需求最多 4 行（digest / audit / standard / snapshot 各一），PK(req_id, type)。
- clarify_item / fr_point 在 push-artifacts 时**全删重插**（全量替换语义）；test_case 是 upsert（版本+1）。

### A.2 实体属性（🆕 = v1.1 新增）

**requirement（需求主卡）**

| 列 | 类型 | 约束/默认 | 说明 |
|---|---|---|---|
| id | TEXT | PK | `REQ-<yyyy>-<NNN>`，按年递增 |
| name | TEXT | NOT NULL | |
| iter | TEXT | '' | 迭代号 |
| source_type | TEXT | NOT NULL | `feishu` \| `text` |
| source_url / source_text | TEXT | '' | 来源二选一 |
| status | TEXT | NOT NULL | submitted → reviewing → clarifying → published（状态机白名单） |
| submitted_by | TEXT | '' | |
| standard_doc | TEXT | '' | 发布时冗余存重写版全文（正式读取走 artifact.standard） |
| created_at / synced_at | TEXT | | `yyyy-MM-dd HH:mm:ss` |
| domains 🆕 | TEXT | '' | JSON 字符串数组，全局需求域顺序；'' = 未给定（读取时回退解析，§1.4） |

**clarify_item（澄清问题）**

| 列 | 类型 | 约束/默认 | 说明 |
|---|---|---|---|
| id | INTEGER | PK AUTOINCREMENT | |
| req_id | TEXT | NOT NULL, FK→requirement | |
| seq | TEXT | NOT NULL, **UNIQUE(req_id, seq)** | "P19"，平台答复唯一键，重推保留逻辑的匹配键 |
| level | TEXT | NOT NULL | red \| yellow \| blue |
| theme | TEXT | '' | 需求域；v1.1 契约必填，存量可空 → 前端"其他"桶 |
| question | TEXT | NOT NULL | |
| ctx | TEXT | '' | 出处文本 |
| answer / answered_at | TEXT / NULL | '' | 答复与时间（重推按 seq 携带） |
| quote 🆕 | TEXT | '' | 原文摘引，可空；渲染引用块 + 快照定位锚 |

**artifact（产物，PK(req_id, type)）**

| 列 | 类型 | 约束/默认 | 说明 |
|---|---|---|---|
| req_id | TEXT | PK 前缀 | |
| type | TEXT | PK | `digest` \| `audit` \| `standard` \| `snapshot`🆕 |
| markdown | TEXT | '' | digest/audit：原文可选；**snapshot：原文全文快照**；standard：重写版全文 |
| content_json | TEXT | '' | digest/audit 的结构化视图 JSON（含 🆕 features[].src、items[].group/quote）；standard/snapshot 为空 |
| version | INTEGER | DEFAULT 1 | upsert 每次 +1 |
| pushed_at | TEXT | | snapshot 类型下即**快照时间戳** |

digest content_json 结构：`{summary, roles[], features[{role, m, func, cov, src🆕}]}`；
audit content_json 结构：`{red, yellow, blue, items[{id, level, title, where, go, group🆕, quote🆕}]}`。

**fr_point（需求点）**

| 列 | 类型 | 约束/默认 | 说明 |
|---|---|---|---|
| req_id | TEXT | PK 前缀 | |
| id | TEXT | PK | `需求.FR-01`，需求内全局唯一，追溯外键 |
| name | TEXT | '' | 规则简题 |
| source 🆕 | TEXT | '' | 行级出处，如 `详述6.3.1+评论区` |
| group_name 🆕 | TEXT | '' | 出处分组组头，如 `§7.2 修改比赛`；JSON 字段名 `group`（SQL 保留字规避） |
| module 🆕 | TEXT | '' | 归属功能模块，对应 features[].m（去 ⭐），双向索引 D3 |

**test_case（测试用例）**

| 列 | 类型 | 约束/默认 | 说明 |
|---|---|---|---|
| req_id | TEXT | PK 前缀, INDEX idx_case_req | |
| id | TEXT | PK | `TC-17`（sdlc-test）/ `WEB-001`（表单） |
| title | TEXT | NOT NULL | |
| points | TEXT | NOT NULL '[]' | JSON 数组，如 `["需求.FR-27"]` |
| priority | TEXT | 'P2' | |
| pre / steps / expect | TEXT | '' | |
| owner | TEXT | '' | |
| tool | TEXT | '表单' | `sdlc-test` \| `表单`（只读判定依据） |
| status | TEXT | 'draft' | draft \| reviewed |
| version | INTEGER | 1 | 乐观锁（expectedVersion 整批拒绝） |
| created_at / updated_at | TEXT | | |
| verify_refs 🆕 | TEXT | NOT NULL '[]' | JSON 数组，`["体检.P06","需求.A2"]`，来自 cases.md 重点验证项 |
| defect_refs 🆕 | TEXT | NOT NULL '[]' | JSON 数组，纯展示文本 `["BUG-02（R1·待修复）"]`，缺陷本体不上链 |

**case_comment（用例评论）**：id PK / req_id / case_id / by / text NOT NULL / created_at，INDEX(req_id, case_id)。v1.1 不变。

### A.3 跨域关联（非存储：软引用与计算字段）

```
 存储的软引用（写入侧）
 ─────────────────────
 test_case.points[]      ──▶ fr_point.id                 R1 正向（既有）
 test_case.verify_refs[] ──▶ audit.items[].id（体检.Pxx）  R2（平台侧由此反推 relatedCases）
 test_case.verify_refs[] ──▶ digest 假设清单（需求.Axx）    R2（落点 = digest 原文折叠，不进 audit 反推）
 clarify_item.seq        ◀─▶ audit.items[].id            D5 同源 P 编号一一对应（无外键，靠命名约定）

 计算字段（读取时聚合，不落库，§1.5）
 ──────────────────────────────────
 frPoints[].coveredBy      ◀── test_case.points（按 fr.id 全称匹配）      R1 反向 / R3 两跳
 audit.items[].relatedCases ◀── test_case.verify_refs（按 id 全称精确匹配） R2 反向
 domains（detail 顶层）     ◀── requirement.domains，空则按 audit→checklist 首现序回退

 双向索引（业务约定，D3）
 ─────────────────────
 fr_point.module ◀════▶ digest.features[].m（去 "⭐ " 前缀后相等）

 文本定位（非外键，D7/§3.2）
 ─────────────────────────
 fr.source / audit.where / quote「」锚 / cov 中的 Pxx ──needle──▶ artifact.snapshot 渲染后 DOM
```

### A.4 设计不变式

1. 所有跨表引用都是软引用：无 FOREIGN KEY，删除/替换语义由服务层控制（全删重插 or upsert）。
2. 时间一律 TEXT `yyyy-MM-dd HH:mm:ss`（Times.java 统一格式），无时间戳类型。
3. 新列全部可空或带默认值 ⇒ 旧行天然满足，读侧"空则隐藏"（§3.5 降级矩阵）。
4. 反向索引永不落库 ⇒ 不存在双向写不一致；真实数据量级（百行）内聚合成本可忽略。

---

## 附录 B：v1.1 完整目标 DDL

### B.1 schema.sql v1.1（新库直建，全量）

```sql
-- SDLC 产物协作平台 v1.1 表结构（全部 IF NOT EXISTS，可重复执行）
-- 设计依据：docs/DEV-PLAN.md 第 2 节 + docs/DESIGN-v1.1-全程可追溯.md 第 1 节 / 附录 A
-- v1.1 变更：requirement.domains；clarify_item.quote；fr_point.source/group_name/module；
--            test_case.verify_refs/defect_refs；快照为 artifact 新类型（无 DDL 变化）

CREATE TABLE IF NOT EXISTS requirement (
  id            TEXT PRIMARY KEY,
  name          TEXT NOT NULL,
  iter          TEXT DEFAULT '',
  source_type   TEXT NOT NULL,
  source_url    TEXT DEFAULT '',
  source_text   TEXT DEFAULT '',
  status        TEXT NOT NULL,
  submitted_by  TEXT DEFAULT '',
  standard_doc  TEXT DEFAULT '',
  created_at    TEXT,
  synced_at     TEXT,
  domains       TEXT DEFAULT ''          -- v1.1 全局需求域顺序（JSON 数组，''=未给定）
);

CREATE TABLE IF NOT EXISTS clarify_item (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  req_id      TEXT NOT NULL,
  seq         TEXT NOT NULL,
  level       TEXT NOT NULL,
  theme       TEXT DEFAULT '',
  question    TEXT NOT NULL,
  ctx         TEXT DEFAULT '',
  answer      TEXT DEFAULT '',
  answered_at TEXT,
  quote       TEXT DEFAULT '',           -- v1.1 原文摘引（可空）
  UNIQUE (req_id, seq)
);

CREATE TABLE IF NOT EXISTS artifact (
  req_id       TEXT NOT NULL,
  type         TEXT NOT NULL,            -- digest | audit | standard | snapshot(v1.1 新类型)
  markdown     TEXT DEFAULT '',          -- snapshot：原文全文快照；standard：重写版全文
  content_json TEXT DEFAULT '',          -- digest/audit 结构化视图（含 v1.1 的 src/group/quote）
  version      INTEGER NOT NULL DEFAULT 1,
  pushed_at    TEXT,                     -- snapshot 类型下 = 快照时间戳
  PRIMARY KEY (req_id, type)
);

CREATE TABLE IF NOT EXISTS fr_point (
  req_id     TEXT NOT NULL,
  id         TEXT NOT NULL,              -- 需求.FR-01
  name       TEXT DEFAULT '',
  source     TEXT DEFAULT '',            -- v1.1 行级出处，如 详述6.3.1+评论区
  group_name TEXT DEFAULT '',            -- v1.1 出处分组组头（JSON 字段名 group，SQL 保留字规避）
  module     TEXT DEFAULT '',            -- v1.1 归属功能模块（对应 features[].m）
  PRIMARY KEY (req_id, id)
);

CREATE TABLE IF NOT EXISTS test_case (
  req_id      TEXT NOT NULL,
  id          TEXT NOT NULL,             -- TC-17 / WEB-001
  title       TEXT NOT NULL,
  points      TEXT NOT NULL DEFAULT '[]',
  priority    TEXT DEFAULT 'P2',
  pre         TEXT DEFAULT '',
  steps       TEXT DEFAULT '',
  expect      TEXT DEFAULT '',
  owner       TEXT DEFAULT '',
  tool        TEXT DEFAULT '表单',       -- sdlc-test | 表单（只读判定依据）
  status      TEXT DEFAULT 'draft',      -- draft | reviewed
  version     INTEGER NOT NULL DEFAULT 1,
  created_at  TEXT,
  updated_at  TEXT,
  verify_refs TEXT NOT NULL DEFAULT '[]',-- v1.1 重点验证引用 ["体检.P06","需求.A2"]
  defect_refs TEXT NOT NULL DEFAULT '[]',-- v1.1 缺陷编号引用文本 ["BUG-02（R1·待修复）"]
  PRIMARY KEY (req_id, id)
);

CREATE INDEX IF NOT EXISTS idx_case_req ON test_case(req_id);

CREATE TABLE IF NOT EXISTS case_comment (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  req_id     TEXT NOT NULL,
  case_id    TEXT NOT NULL,
  by         TEXT DEFAULT '',
  text       TEXT NOT NULL,
  created_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_comment_case ON case_comment(req_id, case_id);
```

### B.2 存量库增量迁移（SchemaMigrator 执行，幂等：先 PRAGMA table_info 判列再 ALTER）

```sql
ALTER TABLE requirement  ADD COLUMN domains       TEXT DEFAULT '';
ALTER TABLE clarify_item ADD COLUMN quote         TEXT DEFAULT '';
ALTER TABLE fr_point     ADD COLUMN source        TEXT DEFAULT '';
ALTER TABLE fr_point     ADD COLUMN group_name    TEXT DEFAULT '';
ALTER TABLE fr_point     ADD COLUMN module        TEXT DEFAULT '';
ALTER TABLE test_case    ADD COLUMN verify_refs   TEXT NOT NULL DEFAULT '[]';
ALTER TABLE test_case    ADD COLUMN defect_refs   TEXT NOT NULL DEFAULT '[]';
```

说明：SQLite 的 `ADD COLUMN ... NOT NULL` 必须带非 NULL 默认值（'[]' 满足）；新列均无 UNIQUE/PK 约束，ALTER 可直接加；artifact 无 DDL 变化（snapshot 只是新的 type 取值）。
