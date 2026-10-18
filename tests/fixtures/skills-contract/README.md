# skills 契约 fixture（hermetic）

跨仓契约测试的仓内快照：CI 只 checkout server 时也能验证结构键，不再依赖
`~/IdeaProjects/claude-sdlc-skills`（P1-2）。

- 权威源：`claude-sdlc-skills` 仓（快照基线 commit 见 `contract-version.json`）。
- 只锁**结构键**（`options` / `module` / `layer` / `answerText` / `danglingPids` /
  `incremental_bundle 草稿契约` / `仅允许引用存量已分配 seq`），**不断言人类文档标题里的
  版本字面量**——版本标注属于发布卫生，不是契约面（skills `5f3673c` 去掉标题版本即此教训）。
- 刷新：从 skills 仓同路径文件复制后裁剪为结构键节，并更新 `contract-version.json` 的
  `snapshotOf` / `capturedAt`。本地有 skills 检出时，`test_skills_contract_live_aligned_when_available`
  会对 live 文件做同键对账（缺失即 skip，CI 不红）。
