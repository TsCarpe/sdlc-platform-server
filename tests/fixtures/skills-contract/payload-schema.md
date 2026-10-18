# sdlc-sync payload 契约

> 结构键快照（hermetic fixture，非权威文档）：权威源 `claude-sdlc-skills`
> `skills/sdlc-sync/references/payload-schema.md`，快照基线见 `contract-version.json`。
> 只保留平台测试锁定的结构键；版本字面量不在契约面内。

## POST /api/requirements/{id}/artifacts —— 推体检产物（三件套）

- `items[].options`（可选）：选择题化选项数组 `[{key, text, why, recommended}]`——2-4 项；
  `key` 大写字母唯一（A-Z）；`text` 为自包含选项全文快照；`why` 一句话理由；
  `recommended` 至多一项 `true`（AI 推荐项）。
- `items[].module`（可选）：问题归属功能模块，与 `fr_points[].module` 同体系。
- `items[].layer`（可选）：命中层号 `"1"-"7"` 字符串；**不设 0 号**；平台宽进归一
  （数字 `"3"`/`3` → `"3"`，越界归空串）。
- `digest.flows[].pids`（可选）：字符串**数组**，值为与该断点相关的澄清问题 P 编号。
  全量 payload 中仅允许引用本 payload `items[].seq` 或存量 `local`（L 系）seq；
  增量新行仅允许引用**存量已分配** seq。悬空 pid 平台不改写不拒收（宽进透传）。
- 返回 `{..., "danglingPids": N}`：payload 内 `flows[].pids` 悬空计数——观察信号非守卫。

## incremental_bundle 草稿契约

变更单 confirm 后局部质检的回传草稿（`PUT /api/requirements/{id}/drafts/incremental_bundle`），
人审采纳后由平台机械合并。

- `digestRows.*` 新版本行：**行结构与全量质检同构**——features `{role,m,func,cov,src,anchor}`、
  flows `{role,step,next,gap,src,anchor,pids}`、states `{obj,from,on,to,gap,src,anchor}`、
  auditItems `{id,level,title,where,go,group,quote,anchor,layer}`（9 键含 `layer`）。
- `digestRows.flows[].pids`：**仅允许引用存量已分配 seq**（平台 detail `items[]` 可见的编号）——
  本包新问题编号由平台合并时分配、合并不回填，引用本包新问题必然悬空；
  采纳出参 `danglingPids` 仅计本次 new 行悬空。
- `clarify.items`：新澄清问题 `{level,theme,question,ctx,quote,anchor,options,module,layer}`——
  **不携带 seq**（平台合并时分配）；`options/module/layer` 纪律同全量 push。

## GET /api/requirements/{id}/answers

- `items[].answerText`：`answer` 的服务端解构出口——结构化 option → `optionText`、
  结构化 text → `text`、非/残缺结构化 → 原文透传。本地回写（pull-answers）、合成输入、
  变更重开 note 一律消费 `answerText`。
