# 标准需求文档合成规范（重写版）

> 结构键快照（hermetic fixture，非权威文档）：权威源 `claude-sdlc-skills`
> `skills/sdlc-sync/references/standard-doc-spec.md`，快照基线见 `contract-version.json`。
> 只保留平台测试锁定的消费口径。

> 澄清答复输入一律取 `answerText`（平台解构后的纯文本——`answer` 原文可能是
> `{"kind":...}` 结构化 JSON 串，直接消费会把裸 JSON 写进定稿）；已答结论固化的条款尾部带
> 〔Pxx〕尾缀；挂起项（status=deferred）以〔Pxx·挂起〕标注。
