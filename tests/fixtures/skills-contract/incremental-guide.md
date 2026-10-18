# 增量梳理规范（incremental_check）

> 结构键快照（hermetic fixture，非权威文档）：权威源 `claude-sdlc-skills`
> `skills/sdlc-intent/references/incremental-guide.md`，快照基线见 `contract-version.json`。
> 只保留平台测试锁定的纪律行。

## 输入面

| 数据 | 来源 | 用途 |
|---|---|---|
| digest/audit 结构行 + 澄清 | `GET /api/requirements/{id}`（detail） | 区间外行原样保留的基线；已答澄清项的答复一律取 `items[].answerText`（平台解构后的纯文本，不要直接消费原始 `answer`） |

## pids 引用纪律（D-L）

**仅允许引用存量已分配 seq**（detail `items[]` 可见的编号）：本包新问题编号由平台合并时分配、
合并不回填，引用本包新问题必然悬空。
