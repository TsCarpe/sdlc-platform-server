# DESIGN v1.2 —— 原文预处理与块级引用

> 依据：2026-09-19 用户反馈三问题（详述展示堆叠 / 原文图片不显示 / 后续环节对详述的引用大量丢失）。
> 本文件记录 v1.2 的关键决策与数据契约；v1.1 的「不做锚点 ID 体系」（REQ-v1.1 §6、DESIGN-v1.1 §8）由本版推翻取代。

## 1. 根因（v1.1 现状）

| 问题 | 根因 |
|---|---|
| 详述堆叠 | 飞书导出 md 中「需求功能详述」为巨型内嵌 HTML `<table>`（含空行），marked 按空行拆 HTML block 后结构打散；`.doc` CSS 无嵌套列表/图片规则 |
| 图片不显示 | `<img src="飞书图片token">` 非 URL；后端无媒体端点，前端无重写 |
| 引用丢失 | 定位=needle 文本匹配（TreeWalker 单文本节点 indexOf，跨元素即失败）；巨表把 DOM 文本切碎；`comment-refs` 被 DOMPurify 剥除 |

## 2. 关键决策

| # | 决策 | 理由 |
|---|---|---|
| D12 | 拆分维度=**格式无关通用块模型**（heading/paragraph/list-item/table-row/image/attachment/whiteboard/comment），不绑定任何需求文档模板；最小引用单位=list-item（条款）/ table-row | 需求文档格式与质量各异，本档「详述表」不能当模板；列表项/表格行是任何结构化文档的通用原子 |
| D13 | 锚点=**飞书原生块 ID**（DocxXML 每元素自带 id），不自造 ID、不做哈希匹配 | 块 ID 天然跨拉取稳定（块未删则不变），零维护成本 |
| D14 | 结构解析=**确定性脚本**（html.parser），LLM 不参与切分；脚本随 sdlc-intent 分发（claude-sdlc-skills 仓 `skills/sdlc-intent/scripts/`） | 延续「结构化由 agent 侧完成、平台不写解析器」（DEV-PLAN）；解析可回归测试 |
| D15 | 平台只存不析：新 artifact type=`doc`（content_json=blocks+media 清单），原始快照 md 保留不动（D1 冻结语义不变） | doc 是快照的结构化视图，非第二权威 |
| D16 | 媒体内容寻址：`mediaId=m-<sha256前12>`，文件存 `sdlc.media-dir`（默认 ./data/media），DB `media` 表存元数据；上传可带显式 mediaId（与 doc.media[].id 一致），幂等 | doc.blocks 引用一致；重复上传/重推零副作用 |
| D17 | 跳转=**anchor 直达优先，needle 回退**：`[data-blk="<块ID>"]` scroll+flash；无锚/锚失效 → 原 needle 多策略 → 诚实 toast | 旧卡（v1.0/v1.1 数据）与无锚出处全兼容，行为同 v1.1 |
| D18 | 人读出处格式**不变**（详述6·步骤一 / 评论区#N / 「引文」），anchor 仅作机器字段随 payload/落库传输 | sdlc-test / sdlc-gate 的模板与 check_trace.py 命名空间体系零改动 |

## 3. 数据契约（payload v1.2，全部可选）

```jsonc
{
  "doc": {                       // 顶层可选；非空即刷新 artifact type='doc'
    "generator": "sdlc-intent/parse_doc.py 0.2",
    "source": { "url": "", "documentId": "", "revisionId": 0 },
    "blocks": [                  // 扁平 + parent 指针
      { "id": "doxcn…", "type": "heading|paragraph|list-item|table-row|image|attachment|whiteboard",
        "text|cells|alt|media": "…", "path": "人读路径", "parent": "块ID|null",
        "marker": "1.", "ordinal": "1.2.3", "comments": "c1 c2" }
    ],
    "media": [ { "id": "m-…", "name": "", "mime": "", "bytes": 0, "sha256": "", "kind": "media|whiteboard", "status": "ok" } ]
  }
}
// fr_points[] / features[] / audit.items[] / items[] 各增 "anchor": "<飞书块ID>"（可空）
```

- 媒体上传：`POST /api/requirements/{id}/media?name=x.png&mediaId=m-…`（raw bytes，头 `X-Media-Mime`；须在推 doc 前完成）
- 媒体读取：`GET /api/requirements/{id}/media/{mediaId}`（ETag=sha256，304 协商缓存）
- 推送返回增 `docUpdated` / `missingMedia`（引用了未上传媒体时如实计数，doc 仍落库）
- 发布后冻结：published 拒推守卫同样覆盖 doc/媒体

## 4. 管线

```
lark-cli +fetch --doc-format xml --detail with-ids   ← 块级结构 + 原生块 ID + 评论 reference_map
        ↓
parse_doc.py（确定性解析）→ sdlc/<需求>/intake/doc-<日期>.json + media/（+media-download 画板缩略图）
        ↓
resolve_anchors.py（出处/引文 → anchor，确定性：详述N→行块→后代条款；评论区#N→comment块；quote 子串匹配）
        ↓
sdlc-sync push（v1.2：先传媒体，再推 doc + anchors）
        ↓
平台：artifact(doc) + media 表/文件 → 前端 DocBlock 结构化渲染 + anchor 直达/needle 回退
```

## 4.1 首批增强（同日迭代）

| # | 增强 | 实现 |
|---|---|---|
| E1 | 出处「⌖ 直达」标识：带锚引用在 digest 功能地图/需求点注册表/体检/澄清四处显示青绿 ⌖ 徽标（.cite.anchored 实线下划线），与未锚出处（虚线+needle）视觉区分 | ReqDetail.vue 六处引用点 + style.css |
| E2 | 标准文档出处引用链接化：standardMd 中的 `详述N(.N)*(·小节名)?` 自动链接化（src: 前缀），点击 resolveDocPath 解析块锚直达原文（行号→序数→小节名三级匹配），回退 needle | linkifyRefs 正则扩展 + stdRefClick src 分支 + resolveDocPath |
| E3 | 梳理期条款级引用：`list_clauses.py --doc doc.json [--row N]` 输出条款树（序数=详述N·1.a.i），梳理时出处可写到条款级；resolve_anchors.py 增序数匹配（精确/尾段），序数与小节名均可解析 | skills/sdlc-intent/scripts + digest-template 标注约定 |

## 5. 验收

- `scripts/verify-m4.mjs`：媒体上传幂等/字节/ETag-304/404、doc 落库回读、四类 anchor 透传、重推版本递增、无 doc 降级不 500（16 项断言）
- 真实卡 REQ-2026-002（区域教学技能比赛，561 块/29 媒体/25 评论）：堆叠消除、图片/画板缩略图可见、FR 出处点击直达条款块、评论徽标内联展开
- 回归：verify-m2 / verify-m3 / verify-guard 全部通过
