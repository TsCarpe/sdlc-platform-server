# DESIGN v2.3 —— digest 流程/状态机上摘要 + mermaid 渲染

> 依据：2026-09-27 图例/图表辅助理解调研——digest 产物符号体系（❓/✅⚠️❌/风险分级/FR-xx/A编号）无集中图例、平台「需求摘要」缺 §5/§6 流程与状态机视图（PM 澄清期答复断点/流转类问题缺全局上下文）、前端不渲染 mermaid（折叠区原文展开是源码文本）。业界依据：C4「every diagram should have a key/legend + title」、Moody《Physics of Notations》Dual Coding/Semantic Transparency；spec-kit 内嵌 mermaid / Kiro 对照。方案经全新上下文对抗式审核（1 阻断：resolve_anchors 扩展；10 建议全吸收——含 check_trace/guard_exec 措辞坑、render DOM 时序、openGrps 组名撞名）。skills 仓同批改动见其 CHANGELOG v0.11.0（payload 契约 v1.4 权威在 skills 仓，本仓只留实现）。
> 范围决策（用户拍板）：图例加产物级不进图内、不新增图类型；摘要区外移**表**不外移图（投影到澄清工作集粒度）；标准文档维持逻辑文本化（仅 skills 仓 standard-doc-spec 显式化 ❓ 断点取材）；交互原型 platform-demo 不同步（v2.x 历来未同步先例）。

## 1. 关键决策

| # | 决策 | 理由 |
|---|---|---|
| D27 | `digestDigestView` 白名单扩 `flows[]`/`states[]`（payload v1.4，全部可选）；states 组装源 = skills 仓 digest §6 **流转边表直投**（模板已同步由状态表升级为边表），平台数据以表为准、不从 mermaid 源码提边 | 提边有漏边/漏出处风险（边上出处写在括号内）；边表每行一条流转、`?` 行=断点，投影零转换成本且直接对应 sdlc-test「S2→S3 边」用例引用 |
| D28 | 摘要 tab 两表：flows 单表（主流程通常一条）、states 按 `obj` 分组折叠复用 `.grp` 模式与共享 `openGrps`，**组名加「状态机·」前缀**；`gap:true` 行加 `.row-gap` 高亮（--yellow-light）+ ❓ 标记 | openGrps 跨 audit/checklist/fr 共享且组名同源 domains，obj 组名裸用可能撞名致联动展开；断点行是 PM 答澄清的高价值信号，视觉优先级最高 |
| D29 | mermaid 引入（**「前端零新依赖」红线唯一豁免**，CLAUDE.md 两处同步登记）：main.js 全局 MutationObserver 单例监听 `code.language-mermaid` 插入，惰性 `import('mermaid')` 后 `mermaid.run()`；render() 同步纯函数不动 | render() 返回 HTML 字符串、mermaid.run() 需挂载后 DOM——逐调用方加 nextTick 钩子是 6 处侵入，observer 是真单点；首图才加载 1MB+ chunk 不进首屏。安全顺序：marked 输出先过 DOMPurify，mermaid `securityLevel:'strict'` 自带消毒、其 SVG **不再过** DOMPurify（默认配置剥 SVG 内联样式致图破损）；details 收起时 `offsetParent===null` 跳过留待展开（observer 兼听 `open` 属性） |
| D30 | Runner 速查卡（AgentScopeAgentExecutor）两处同步：digest 8 区块行补「符号图例小节 + §5/§6 图题要求（图 N：<对象名> 状态机——obj 唯一载体）」，payload 枚举行补 flows/states 并**顺手补齐 v1.3 `risk_tier` 既有缺口**（该枚举此前漏 risk_tier，Runner 通道产的 digest 一直缺风险分级） | 速查卡注释声明与 skills 仓同源，不同步则 Runner 通道产物形态与 skill 版分叉（无图例无图题、缺新字段），「两仓一起改」只完成一半 |
| D31 | verify-m4 增两组断言：flows/states 透传（gap/anchor/obj）+ 无字段降级空数组 | m4 已对 detail.digest.content 逐字段断言（features[0].anchor），新字段照例；降级路径（旧 payload 无 flows/states）回归防 500 |

## 2. 数据契约（payload v1.4，全部可选；权威定义在 skills 仓 payload-schema.md）

```jsonc
// POST /api/requirements/{id}/artifacts —— digest 增两组可选数组
{ "digest": {
    "flows":  [ { "role": "区域管理员", "step": "创建比赛（五步向导）", "next": "提交审核",
                  "gap": false, "src": "详述6.2", "anchor": "（可选，resolve_anchors.py 附加）" },
                { "role": "区域管理员", "step": "提交审核", "next": "?",
                  "gap": true,  "src": "详述6.2", "anchor": "" } ],          // gap=true = 原文 ❓ 断点，next 填 "?"
    "states": [ { "obj": "比赛", "from": "草稿", "to": "报名中", "on": "发布",
                  "gap": false, "src": "详述6.3", "anchor": "" } ],
    … } }

// GET /api/requirements/{id} —— digest.content 白名单投影后含 flows/states（缺失 → 空数组）
{ "digest": { "content": { "summary": "…", "roles": [], "features": [],
              "flows":  [ { "role": "…", "step": "…", "next": "…", "gap": false, "src": "…", "anchor": "…" } ],
              "states": [ { "obj": "…", "from": "…", "on": "…", "to": "…", "gap": false, "src": "…", "anchor": "…" } ] } } }
```

- `states[].obj` 取自 digest §6 图题「图 N：<对象名> 状态机」的对象名（图题是唯一载体，skill 通道与 Runner 通道双约束）
- anchor 复用 v1.2 机制：sdlc-intent 的 resolve_anchors.py 已扩至六类条目（+flows/states），前端 `locate(src, anchor)` 照抄 features 模式
- 后端仅 `digestDigestView` 一处必改（白名单投影）；Controller/存储/详情链路零改动，草稿采纳通道（TaskService approve）复用同入口自动覆盖

## 3. 改动清单

| 位置 | 改动 |
|---|---|
| backend common/Json.java | 增 `boolVal`（gap 投影用，照 intVal 风格） |
| backend RequirementService.digestDigestView | flows/states 两组白名单循环 |
| backend AgentScopeAgentExecutor.qualityQuickRef | 8 区块行（图例/图题/边表）+ payload 枚举行（flows/states/risk_tier） |
| frontend ReqDetail.vue | 摘要 tab flows 卡 + states 分组卡（D28）；script 增 digestFlows/digestStates/stateGroups |
| frontend style.css | `.row-gap` 断点行高亮 |
| frontend main.js | mermaid MutationObserver（D29） |
| frontend package.json | + mermaid ^11（红线豁免登记 CLAUDE.md 两处） |
| scripts/verify-m4.mjs | D31 两组断言 |

## 4. 验收

- verify-m4：flows/states 透传 + 降级空数组断言全绿（服务 `scripts/dev.sh` 后 `node scripts/verify-m4.mjs`）
- 摘要 tab：两表渲染、gap 行高亮、locate 跳转（anchor 有/无两路）、states 组名不与 domains 组撞名；旧数据（contentJson 无 flows/states）表区不渲染
- mermaid：digest 折叠区/标准文档/快照/TaskPanel 草稿预览的 ```mermaid 块渲染为图；控制台无 sanitize 告警；首屏 network 无 mermaid chunk（首图才加载）；details 展开后图正常渲染（非压扁）
