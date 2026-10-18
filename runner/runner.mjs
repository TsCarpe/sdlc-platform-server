#!/usr/bin/env node
// SDLC 平台任务 Runner（v2.0 全流程线上化的执行侧）
// 轮询平台任务队列 → spawn Claude Code headless 执行对应 skill 管线 → 产物以「草稿」回传平台。
// 红线：Runner 侧只允许 PUT drafts 与 POST media；正式产物（/artifacts、/publish、/api/cases）
// 只能由人在平台上「采纳/发布」触发——线上化不改守卫，只改触发方。
//
// 环境变量：
//   SDLC_PLATFORM_URL   平台地址（默认 http://127.0.0.1:8080）
//   SDLC_RUNNER_NAME    Runner 标识（默认 local-<hostname>-runner；稳定名，不含 pid——
//                       recover-stale 按名字归属，pid 名重启即变会让旧 running 永远无人认领。
//                       同主机多实例必须显式设置不同名字）
//   SDLC_RUNNER_INTERVAL 轮询间隔毫秒（默认 3000）
//   SDLC_RUNNER_TIMEOUT_MIN 单任务超时分钟（默认 30）
//   SDLC_SKILLS_REPO    claude-sdlc-skills 仓路径（权威技能源）
//   SDLC_CLAUDE_CLI     headless CLI 路径（默认 ~/.local/bin/claude）
//
// 用法：scripts/runner.sh  （或 node runner/runner.mjs）

import { spawn } from 'node:child_process'
import { homedir, hostname } from 'node:os'
import { existsSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const PLATFORM = process.env.SDLC_PLATFORM_URL || 'http://127.0.0.1:8080'
const RUNNER = process.env.SDLC_RUNNER_NAME || `local-${hostname()}-runner`
const INTERVAL = Number(process.env.SDLC_RUNNER_INTERVAL || 3000)
const TIMEOUT_MIN = Number(process.env.SDLC_RUNNER_TIMEOUT_MIN || 30)
const REPO = process.env.SDLC_SKILLS_REPO || path.join(homedir(), 'IdeaProjects/claude-sdlc-skills')
const CLI = process.env.SDLC_CLAUDE_CLI || path.join(homedir(), '.local/bin/claude')
// 工作区根三级优先（v4-3 §3）：SDLC_WORKSPACE env > 平台 meta 下发 workspaceRoot > 仓根默认；
// loadRules 时按 meta 覆盖（env 已配置则不动）——与服务端落库回调同源
const WORKSPACE_DEFAULT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
let WORKSPACE = process.env.SDLC_WORKSPACE || WORKSPACE_DEFAULT

const sleep = ms => new Promise(r => setTimeout(r, ms))
const log = (...a) => console.log(new Date().toISOString().slice(11, 19), `[${RUNNER}]`, ...a)

async function api(method, p, body) {
  const res = await fetch(PLATFORM + p, {
    method,
    headers: {
      ...(body ? { 'Content-Type': 'application/json' } : {}),
      // 启用写令牌的部署（SDLC_WRITE_TOKEN）随请求注入 X-Token（AUDIT D1-2）
      ...(process.env.SDLC_WRITE_TOKEN ? { 'X-Token': process.env.SDLC_WRITE_TOKEN } : {})
    },
    body: body ? JSON.stringify(body) : undefined
  })
  if (res.status === 204) return null
  const data = await res.json().catch(() => ({}))
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`)
  return data
}

// ---------- 任务通道共享规则（AUDIT D1-5）：启动从平台拉单一来源（draftTypes 映射 + selfCheck 阈值），
// 拉取失败回退内置同款默认——阈值/映射变更只改平台 task_rules.py，双通道不再各自漂移 ----------
const DEFAULT_RULES = {
  draftTypes: { quality_check: 'digest_bundle', standard_doc: 'standard', design_gen: 'design', design_rev: 'design', gate_review: 'gate_issues', case_gen: 'cases', incremental_check: 'incremental_bundle' },
  noDraftTypes: ['source_import'],
  selfCheck: {
    standard: { minMd: 200 },
    design: { minMd: 200, requireHeading: '§0' },
    gate_issues: { minMd: 100 },
    digest_bundle: {},
    cases: {},
    incremental_bundle: { structure: ['changeId', 'sections', 'digestRows', 'invalidate'], sectionsNonEmpty: true }
  }
}
let RULES = DEFAULT_RULES
async function loadRules() {
  try {
    const r = await api('GET', '/api/meta/task-rules')
    RULES = { draftTypes: { ...DEFAULT_RULES.draftTypes, ...(r.draftTypes || {}) },
              noDraftTypes: r.noDraftTypes || DEFAULT_RULES.noDraftTypes,
              selfCheck: { ...DEFAULT_RULES.selfCheck, ...(r.selfCheck || {}) } }
    // v4-3（DESIGN-v4 §3）工作区根统一下发：env 显式配置最高 > meta workspaceRoot > 仓根默认。
    // 与服务端完成回调同源（ResolvedPaths.workspace_root）——分叉则 source_import 落库找不到输出文件。
    if (!process.env.SDLC_WORKSPACE && r.workspaceRoot) WORKSPACE = path.resolve(r.workspaceRoot)
    log(`任务规则已从平台加载（${Object.keys(RULES.draftTypes).length} 类任务），工作区=${WORKSPACE}`)
  } catch (e) { log('任务规则拉取失败，使用内置默认：', e.message) }
}

// ---------- 任务指令模板（按任务类型） ----------

const SYNC_CONTRACT = `${REPO}/skills/sdlc-sync/references/payload-schema.md`
const INTENT = `${REPO}/skills/sdlc-intent`

function promptFor(task, card) {
  const common = `你是 SDLC 平台的任务执行器。平台地址 ${PLATFORM}，需求卡 ${task.reqId}（${card.card.name}）。
所有产物落盘到工作区 sdlc/${card.card.name}/ 目录（当前目录 ${WORKSPACE}）。
完成后必须输出单独一行结果摘要（最后一行，机器解析）：
SDLC_TASK_RESULT {"关键统计": "..."}
硬性红线（违反即失败）：
1. 只准把产物写到草稿端点 PUT ${PLATFORM}/api/requirements/${task.reqId}/drafts/<type>；
   绝对禁止调用 POST /artifacts、POST /publish、POST /api/cases（正式产物只能由人在平台采纳）。
2. 原文图片/附件/画板媒体先逐个上传 POST ${PLATFORM}/api/requirements/${task.reqId}/media?name=<文件名>&mediaId=<doc.media[].id>（raw bytes，头 X-Media-Mime），再推引用它们的 doc。
3. 忠实原文：梳理不评价不脑补，体检逐条附原文定位，❓ 禁止自行补全。
`

  if (task.type === 'quality_check') {
    return `${common}
按以下步骤执行需求质检（sdlc-intent 完整两段 + v1.2 锚点管线；v4-5：纯分析读块模型——原文/评论从平台取；v1.6：澄清选择题化）：

1. 读取技能权威文档：
   - ${INTENT}/SKILL.md（两段式工作流）
   - ${INTENT}/references/digest-template.md（梳理 8 区块模板）
   - ${INTENT}/references/dimensions.md 与 references/report-template.md（体检七层维度与报告模板、pm-checklist 选项化/module/layer 产出约定）
   - ${SYNC_CONTRACT}（payload 契约 v1.6）
2. 输入（块表权威，curl 直落供管线消费）：
   curl -s ${PLATFORM}/api/requirements/${task.reqId}/doc.json -o intake/doc-task.json
   curl -s ${PLATFORM}/api/requirements/${task.reqId}/comments.json -o raw/comments.json
   （doc.json 404 = 原文未导入：如实终止报错，禁止以卡内 sourceText 降级替代；媒体已在平台，无上传步。）
3. 第一段梳理：按 digest-template 产出 digest（出处尽量写到条款级，用 python3 ${INTENT}/scripts/list_clauses.py --doc intake/doc-task.json 查条款树）。
4. 第二段体检：按 dimensions 七层逐层检查，产出 audit 与澄清清单（每个澄清问题 seq 用体检 P 编号，theme 必填；v1.6 新字段——每个澄清问题带 module（=fr_points[].module 同体系的归属功能模块）与 layer（命中层号 "1"-"7" 字符串，判层纪律见 dimensions.md 第 8 条，维度外补充类不携带 layer）；冲突类/取舍类问题尽量选项化：2-4 项 {key:A-Z 大写唯一, text:自包含选项全文, why:一句理由}、至多一项 recommended:true，纯缺失类/开放类问题不硬造选项（不带 options））。
5. 组装 v1.6 payload（严格按 payload-schema）：{snapshot:{md 原文全文}, domains:[全局需求域顺序], digest:{summary,roles,features,fr_points,md}, audit:{red,yellow,blue,items（每条带 layer）}, items:[澄清清单（带 options/module/layer）]}——flows 断点行（gap:true）带 pids:["Pxx"]（字符串数组，仅引用本 payload items 的 P 编号）；不含 doc：原文块表权威已落库，透传会在采纳时被剥除（intake/doc-task.json 仍用于第 3 步条款树与第 6 步锚点解析）。
6. 用 python3 ${INTENT}/scripts/resolve_anchors.py 给 payload 附加 anchor（--doc intake/doc-task.json --comments raw/comments.json）。
7. 把最终 payload 原样写入临时 json 文件，然后（payload 较大时务必用 --data-binary @文件）：
   curl -X PUT ${PLATFORM}/api/requirements/${task.reqId}/drafts/digest_bundle -H 'Content-Type: application/json' --data-binary @<payload.json>
8. 确认响应 200 后输出 SDLC_TASK_RESULT 行（含问题数/需求点数/锚点命中统计）。`
  }

  if (task.type === 'incremental_check') {
    return `${common}
执行变更后局部质检（sdlc-intent 增量梳理；v4：受影响章节切片，产出 incremental_bundle 草稿）。

1. 读取增量规范：${REPO}/skills/sdlc-intent/references/incremental-guide.md（受影响章节重梳理、作废行清单、anchor 越界纪律——必须逐项遵守）。
2. 拉取输入（块表权威 + 变更单冻结面）：
   curl -s ${PLATFORM}/api/requirements/${task.reqId}/doc.json -o intake/doc-inc.json   （编辑后全文块模型）
   curl -s ${PLATFORM}/api/requirements/${task.reqId}/changes | python3 -c "import sys,json;d=json.load(sys.stdin);print(json.dumps([c for c in d if c['status']=='confirmed'][0],ensure_ascii=False))" > raw/change.json
   curl -s ${PLATFORM}/api/requirements/${task.reqId}/changes/$(python3 -c "import json;print(json.load(open('raw/change.json'))['id'])")/impact > raw/impact.json
   curl -s ${PLATFORM}/api/requirements/${task.reqId} > raw/card.json   （digest/audit 结构行 + 保留澄清项及已答；已答澄清项的答复一律取 items[].answerText——answer 原文可能是结构化 JSON 串，不要直接消费）
   curl -s ${PLATFORM}/api/requirements/${task.reqId}/comments.json -o raw/comments.json
3. 按 incremental-guide 只重梳理受影响章节：新版本 digest 行/audit 条目/FR 点，作废行清单（原文已删/口径已变），新澄清问题（机器 P 系——seq 由平台合并时分配，不携带；v1.6 新字段：带 module/layer 与 options——纪律同全量质检，冲突/取舍类选项化 2-4 项、缺失/开放类不硬造）。
4. 组装增量包（严格结构）：{changeId, sections:[受影响章节 heading 的 block_id——禁 seq 数值], digestRows:{features,flows,states,auditItems（每条带 layer）,frUpserts,frRemoves}, invalidate:{digestAnchors,auditIds}, clarify:{items（带 options/module/layer）}, summary, riskTier}
   红线：新行 anchor 必须全部 ∈ impact.blocks（越界即拒）；空 anchor 行不产不废（全局行增量永不触碰）；flows 新行 pids 仅引用存量已分配 seq（raw/card.json items[] 可见编号）——本包新问题编号未分配，引用必悬空，禁引用。
5. 写临时 json 后回传：curl -X PUT ${PLATFORM}/api/requirements/${task.reqId}/drafts/incremental_bundle -H 'Content-Type: application/json' --data-binary @bundle.json
6. 输出 SDLC_TASK_RESULT 行（受影响章节数/新行数/作废数/新澄清数）。`
  }

  if (task.type === 'standard_doc') {
    return `${common}
执行标准需求文档合成（重写版）。平台已有该需求的全部澄清答复。

1. 拉取平台数据：curl -s ${PLATFORM}/api/requirements/${task.reqId}（含 card/snapshot/digest/audit/items/domains/doc；澄清答复一律取 items[].answerText——它是服务端解构后的纯文本，items[].answer 原文可能是结构化 JSON 串，任何情况下不要直接消费）。
   澄清项处置语义（DESIGN-v2.2 D21）：items[].status=rejected 的问题不进定稿（连同其答复一并忽略，PM 已认定不是问题）；status=deferred 的记入遗留假设（需求.Axx）并标注〔Pxx·挂起〕。
2. 读取合成规范：${REPO}/skills/sdlc-sync/references/standard-doc-spec.md（结构/消化规则/质量自查清单，必须逐项遵守：❓ 清零、不发明需求、原文规则全集等价覆盖、✅/⚠️/删除线标注、保留 需求.FR-xx / 体检.Pxx / 需求.Axx / 裸 Pxx 引用记号；澄清结论固化的条款尾部带〔Pxx〕尾缀、挂起项带〔Pxx·挂起〕——与 spec §3 消化规则同源）。
3. 以快照为底稿逐条消化体检定论与澄清答复，合成完整定稿 markdown。
4. curl -X PUT ${PLATFORM}/api/requirements/${task.reqId}/drafts/standard -H 'Content-Type: application/json' -d '{"md": <合成文档 JSON 转义>}'（文档大时同样写临时文件 --data-binary @文件）。
5. 输出 SDLC_TASK_RESULT 行（含章节结构摘要/消化条数/遗留假设数）。`
  }

  if (task.type === 'case_gen') {
    return `${common}
执行测试用例生成与登记（sdlc-test 的 cases 阶段）。

1. 读取：${REPO}/skills/sdlc-test/SKILL.md 中「cases 用例生成」阶段的规范、${REPO}/skills/sdlc-test/references/cases/ 与 references/design-techniques.md，以及 ${SYNC_CONTRACT} 的用例契约。
2. 输入：工作区 sdlc/${card.card.name}/intake/ 三件套（digest/audit）。独立性红线：不读任何设计文档。
3. 按 cases.md 产物结构生成用例（总览表为权威清单 + 双向追踪 points → 需求.FR-xx + 重点验证项 verifyRefs → 体检.Pxx/需求.Axx）。
4. 组装 payload {items:[{id,title,points,priority,pre,steps,expect,owner:"sdlc-test",tool:"sdlc-test",status:"draft",verifyRefs,defectRefs}]}，缺陷只上编号引用文本。
5. curl -X PUT ${PLATFORM}/api/requirements/${task.reqId}/drafts/cases -H 'Content-Type: application/json' --data-binary @<payload.json>
6. 输出 SDLC_TASK_RESULT 行（用例数/FR 覆盖数/verifyRefs 数）。`
  }

  if (task.type === 'design_gen') {
    const tier = card.card.riskTier || 'A'
    return `${common}
执行技术设计生成（sdlc-design）。

1. 读取规范：${REPO}/skills/sdlc-design/SKILL.md 与 ${REPO}/skills/sdlc-design/references/design-template.md（头部字段 + §0–§8 九节结构，结构权威）。
2. 输入锚点（GR-1 冻结）：curl -s ${PLATFORM}/api/requirements/${task.reqId}（card.standardDoc=标准文档；digest/audit 字段为质检产物）。
3. 前置闸三道自查：Q 表硬阻塞（只锁对应功能点，被锁契约行标「锁定待拍板（确认.Q-xx）」）；风险分级已拍板（${tier}）；tier 只升不降——勘察发现影响面证据时 contentJson 携带 {"tierProposal":{...}} 上报，不擅自改分级。
4. 存量勘察（多仓逐一覆盖）：GET ${PLATFORM}/api/requirements/${task.reqId}/design → repos 数组逐仓勘察（[角色|local 本地路径] 直接只读；[角色|github 远程仓] 本通道无网络白名单时如实注明降级并记录仓址），禁止跳过不报。
5. 按 design-template 产出完整 design.md（${tier} 级齐套自检六项：D 表八类别、公共资产六类、接口契约挂 F 编号、切分约束、体检 🔴🟡 去向、Q 表）。
6. curl -X PUT ${PLATFORM}/api/requirements/${task.reqId}/drafts/design -H 'Content-Type: application/json' --data-binary @<design 载荷文件>（载荷 {"md": "...", "contentJson": "{\\"tierProposal\\":{...}}"}，无提案时省略 contentJson）。
7. 输出 SDLC_TASK_RESULT 行（齐套自检结果/Q 表数/tier 提案有无/勘察仓覆盖数）。`
  }

  if (task.type === 'design_rev') {
    return `${common}
执行设计修订（sdlc-design 增量模式，GR-4 落改闭环）。

1. 读取已定稿设计：curl -s ${PLATFORM}/api/requirements/${task.reqId}/design（finalized.md + version + targetRepo）。
2. 修订范围 = 任务备注中的落改工作项（逐条落点与改动摘要）；只改受影响章节，不重写未涉及章节。
3. 偏差回写规则：仅影响 ER/接口契约/公共组件/公共资产的偏差回写设计文档，且只重审受影响切片。
4. curl -X PUT ${PLATFORM}/api/requirements/${task.reqId}/drafts/design -H 'Content-Type: application/json' --data-binary @<修订载荷>（{"md": 修订后完整 design.md}；版本由平台定稿时递增，文档内不手改版本）。
5. 输出 SDLC_TASK_RESULT 行（修订条数/涉及章节）。`
  }

  if (task.type === 'gate_review') {
    return `${common}
执行门禁评审（sdlc-gate）。

1. 读取规范：${REPO}/skills/sdlc-gate/SKILL.md、references/issue-template.md（issues 文件结构权威）、references/cross-check-guide.md。
2. 输入三件（CONTRACT）：digest/audit 读平台详情（GET ${PLATFORM}/api/requirements/${task.reqId} 的 digest/audit 字段）、设计终稿（GET ${PLATFORM}/api/requirements/${task.reqId}/design → finalized.md）、用例登记（curl -s "${PLATFORM}/api/cases?reqId=${task.reqId}"）。
3. 分档扇出（读用例头部风险分级，无字段按 A）：A=4 角色（架构一致性/数据模型与SQL/测试可测性/交叉审查只产分歧）B=2（#2+#4）C=五问自查。A/B 必须用 Agent 工具开全新上下文子代理（禁止单会话逐角色串行冒充扇出）；headless 确实无 Agent 工具时按 degraded 如实标注。
   issues 头部必须写扇出留痕三行（平台 ingest 机械校验）：A → | reviewMode | external | / | fanout | 4 | / | contextMode | fresh |；B → external/2/fresh；C → external 或 degraded / 0 / single-session。A/B 级 degraded 或扇出不足会被平台 409 拒收。
4. 对抗式输出六字段：标题/场景（白话 2-3 句）/原文（≤2 句摘引）/位置/严重度（high|mid|low）/依据（hard|soft）；分歧 div:true；权衡 tradeoff:true 建议驳回。
5. RECONCILE 四分类 + 同根因合并（mergedUnit 相同）。
6. 产出 issues 文件（issue-template 结构，头部 评审状态=待裁决）。
7. 写载荷文件 {"md": <issues 文件全文>, "contentJson": "{\\"issues\\":[{code,sev,theme,role,scene,quote,loc,basis,div,tradeoff,filed,mergedUnit}]"}} 后 curl -X PUT ${PLATFORM}/api/requirements/${task.reqId}/drafts/gate_issues -H 'Content-Type: application/json' --data-binary @<载荷>。
8. 输出 SDLC_TASK_RESULT 行（发现数/裁决单元数/分歧数/备案数）。`
  }

  if (task.type === 'source_import') {
    const OUT_DIR = `${WORKSPACE}/tasks/${task.id}/out`
    const head = `你是 SDLC 平台的原文导入执行器。平台地址 ${PLATFORM}，需求卡 ${task.reqId}（${card.card.name}）。
完成后必须输出单独一行结果摘要（最后一行，机器解析）：
SDLC_TASK_RESULT {"blocks": "...", "comments": "...", "media": "..."}
硬性红线（违反即失败）：
1. 导入产物 = 且仅 = parse_doc.py 的输出文件 ${OUT_DIR}/doc.json——必须由 parse_doc 产出，禁止自写、改写、拼接该文件；落库由平台在任务完成回调直读该文件字节执行（不经对话上下文）。
2. 本任务无草稿回传（不推 drafts 端点）；媒体文件照常先逐个 POST media（mediaId 与 doc.media[].id 一致）。
3. 忠实原文：逐字转录不评价不脑补；拉取/解析失败如实报错终止，禁止降级自造产物。
4. raw/ 原始文件一律不整读（只经脚本消费，防上下文膨胀）。
5. parse_doc 必须 exit 0 才可回传任务完成（exit 1=媒体下载失败、exit 2=守恒自检失败、exit 3=用法错误，均中止任务并按其输出提示修复后重跑，禁止带病回传）。
6. doc.json 顶层 guard 是平台落库终检依据（缺失或与产物不一致任务将 failed），不得手工增删改 guard 与产物内容。
7. 媒体必须全部 upload 成功后才可报 done（块引用的媒体不在平台将 missingMedia 阻断拒绝落库）。
按以下步骤执行原文导入：
1. mkdir -p raw intake/media
`
    if (card.card.sourceType === 'feishu') {
      return `${head}2. 飞书源固定拉取序列（<URL>=${card.card.sourceUrl}）：
   lark-cli docs +fetch --doc <URL> --doc-format markdown > raw/doc.md
   lark-cli drive +list-comments --url <URL> --solved-status all --comment-scope all --need-relation --format json > raw/comments.json
   lark-cli docs +fetch --doc <URL> --doc-format xml --detail with-ids > raw/fetch.json
   cat > tools/extract.py <<'PYEOF'
import json
d = json.load(open('raw/fetch.json'))['data']['document']
open('raw/content.xml', 'w').write(d['content'])
rm = d['reference_map']
open('raw/refmap.json', 'w').write(json.dumps(rm.get('comments', rm)))
PYEOF
   python3 tools/extract.py
   python3 ${INTENT}/scripts/parse_doc.py --xml raw/content.xml --refmap raw/refmap.json --out ${OUT_DIR}/doc.json --media-dir intake/media --download --url <URL>
3. ls intake/media 盘点下载的媒体，逐个上传：curl -X POST "${PLATFORM}/api/requirements/${task.reqId}/media?name=<文件名>&mediaId=<doc.media[].id>" -H 'X-Media-Mime: <mime>' --data-binary @intake/media/<文件>
4. 输出 SDLC_TASK_RESULT 行（块数/评论线程数/媒体数）。`
    }
    return `${head}2. 文本源：curl -s ${PLATFORM}/api/requirements/${task.reqId} 取卡内 sourceText，原文逐字（不增删）写入 raw/source.md。
3. python3 ${INTENT}/scripts/parse_doc.py --md raw/source.md --out ${OUT_DIR}/doc.json（text 源无媒体/评论，块 ID 为解析器自造）。
4. 输出 SDLC_TASK_RESULT 行（块数）。`
  }

  return null
}

// ---------- 执行 ----------

function runClaude(prompt) {
  return new Promise(resolve => {
    const child = spawn(CLI, ['-p', prompt, '--permission-mode', 'bypassPermissions', '--output-format', 'json'], {
      cwd: WORKSPACE,
      env: process.env,
      stdio: ['ignore', 'pipe', 'pipe']
    })
    let out = '', err = ''
    const timer = setTimeout(() => {
      log('任务超时，终止进程')
      child.kill('SIGKILL')
    }, TIMEOUT_MIN * 60 * 1000)
    child.stdout.on('data', d => { out += d })
    child.stderr.on('data', d => { err += d })
    child.on('close', code => { clearTimeout(timer); resolve({ code, out, err }) })
  })
}

function extractResult(claudeJson) {
  // claude --output-format json 的 result 字段是最终文本；从中抠 SDLC_TASK_RESULT 行
  try {
    const j = JSON.parse(claudeJson)
    const m = /SDLC_TASK_RESULT\s+(\{.*\})\s*$/.exec(String(j.result || '').trim())
    return { text: String(j.result || ''), summary: m ? m[1] : '', isError: !!j.is_error, subtype: j.subtype }
  } catch { return { text: claudeJson.slice(0, 2000), summary: '', isError: true, subtype: 'parse' } }
}

/** 机械自查：草稿真的回传了、结构合法，才允许记 done。
 *  v4-3（DESIGN-v4 §3，两处代码分支之一）：noDraftTypes（source_import）无草稿面——
 *  落库正确性由平台 finish 完成回调（直读输出文件）校验，跳过草稿自查。 */
async function selfCheck(task) {
  if ((RULES.noDraftTypes || []).includes(task.type)) return { ok: true, noDraft: true }
  const draftType = RULES.draftTypes[task.type] || 'cases'
  const rule = RULES.selfCheck[draftType] || {}
  const minMd = rule.minMd || 200
  const heading = rule.requireHeading || '§0'
  let d
  try { d = await api('GET', `/api/requirements/${task.reqId}/drafts/${draftType}`) } catch { return { ok: false, error: `草稿未回传（${draftType}）` } }
  if (draftType === 'standard') {
    if (!d.md || d.md.trim().length < minMd) return { ok: false, error: 'standard 草稿为空或过短' }
  } else if (draftType === 'design') {
    if (!d.md || d.md.trim().length < minMd) return { ok: false, error: 'design 草稿为空或过短' }
    if (!d.md.includes(heading)) return { ok: false, error: 'design 草稿缺 §0–§8 结构标头（design-template 九节）' }
  } else if (draftType === 'gate_issues') {
    if (!d.md || d.md.trim().length < (rule.minMd || 100)) return { ok: false, error: 'gate_issues 草稿缺 issues 文件全文（md）' }
    try {
      const p = JSON.parse(d.contentJson || '{}')
      if (!Array.isArray(p.issues) || p.issues.length === 0) return { ok: false, error: 'gate_issues 草稿 contentJson.issues 为空' }
    } catch (e) { return { ok: false, error: 'gate_issues 草稿 contentJson 解析失败：' + e.message } }
  } else {
    try {
      const p = JSON.parse(d.contentJson)
      const ok = draftType === 'digest_bundle'
        ? p.digest && p.audit && Array.isArray(p.items) && p.items.length > 0
        : Array.isArray(p.items) && p.items.length > 0
      if (!ok) return { ok: false, error: '草稿 contentJson 结构不完整（缺 digest/audit/items）' }
    } catch (e) { return { ok: false, error: '草稿 contentJson 解析失败：' + e.message } }
  }
  return { ok: true, draftType }
}

async function handle(task) {
  const card = await api('GET', `/api/requirements/${task.reqId}`)
  const prompt = promptFor(task, card)
  if (!prompt) {
    await api('POST', `/api/tasks/${task.id}/failed`,
      { error: `未知任务类型：${task.type}`, reason: 'unknown-type', runner: RUNNER, attempt: task.attempt })
    return
  }
  log(`执行 #${task.id} ${task.type} @ ${task.reqId}（超时 ${TIMEOUT_MIN}min）`)
  const { code, out, err } = await runClaude(prompt)
  const { summary, isError, subtype } = extractResult(out)
  const chk = await selfCheck(task)
  if (code === 0 && chk.ok && !isError) {
    if (!summary) log(`#${task.id} 注意：输出无 SDLC_TASK_RESULT 结果行（草稿自查已过仍记 done；进程内通道此场景会续跑一轮）`)
    const result = summary || JSON.stringify(chk.draftType ? { draft: chk.draftType } : { imported: true })
    await api('POST', `/api/tasks/${task.id}/done`,
      { result, runner: RUNNER, attempt: task.attempt })
    log(`#${task.id} 完成：${result.slice(0, 160)}`)
  } else {
    const reason = !chk.ok ? chk.error
      : code !== 0 ? `CLI 退出码 ${code}：${(err || out).slice(-400)}`
      : `CLI 异常结束（${subtype}）：${summary || out.slice(-400)}`
    // reason 机器码与进程内通道同构（self-check 失败 = no-draft；CLI 异常 = agent-exception）——
    // retry 白名单据此判定可重试性（N-1）；runner/attempt 供 fencing（P1-6）
    const reasonCode = !chk.ok ? 'no-draft' : 'agent-exception'
    await api('POST', `/api/tasks/${task.id}/failed`,
      { error: reason, reason: reasonCode, runner: RUNNER, attempt: task.attempt })
      .catch(e => log('回报失败也失败：', e.message))
    log(`#${task.id} 失败：${reason.slice(0, 200)}`)
  }
}

async function recoverStale() {
  // 启动恢复走服务端（POST /api/tasks/recover-stale，按本 runner 名归属过滤）：
  // 替代客户端逐条判定——旧实现受 list LIMIT 100 窗口约束、硬编码 +08:00 时区，
  // 且两通道各用独立超时配置判定全队列 running，存在跨通道互杀窗口（AUDIT D1-3）
  try {
    const r = await api('POST', `/api/tasks/recover-stale?runner=${encodeURIComponent(RUNNER)}`)
    if (r.expired > 0) log(`恢复：${r.expired} 个超时 running 任务置 failed（本 runner 名下）`)
  } catch (e) { log('启动恢复失败：', e.message) }
}

async function main() {
  if (!existsSync(CLI)) { console.error(`CLI 不存在：${CLI}`); process.exit(1) }
  log(`启动。平台=${PLATFORM} 工作区=${WORKSPACE} 技能源=${REPO}`)
  await loadRules()
  await recoverStale().catch(e => log('启动恢复失败：', e.message))
  for (;;) {
    try {
      const task = await api('POST', `/api/tasks/next?runner=${encodeURIComponent(RUNNER)}`)
      if (task) { await handle(task); continue }
    } catch (e) { log('轮询异常：', e.message) }
    await sleep(INTERVAL)
  }
}

main()
