#!/usr/bin/env node
// SDLC 平台任务 Runner（v2.0 全流程线上化的执行侧）
// 轮询平台任务队列 → spawn Claude Code headless 执行对应 skill 管线 → 产物以「草稿」回传平台。
// 红线：Runner 侧只允许 PUT drafts 与 POST media；正式产物（/artifacts、/publish、/api/cases）
// 只能由人在平台上「采纳/发布」触发——线上化不改守卫，只改触发方。
//
// 环境变量：
//   SDLC_PLATFORM_URL   平台地址（默认 http://127.0.0.1:8080）
//   SDLC_RUNNER_NAME    Runner 标识（默认 local-<host>）
//   SDLC_RUNNER_INTERVAL 轮询间隔毫秒（默认 3000）
//   SDLC_RUNNER_TIMEOUT_MIN 单任务超时分钟（默认 30）
//   SDLC_SKILLS_REPO    claude-sdlc-skills 仓路径（权威技能源）
//   SDLC_CLAUDE_CLI     headless CLI 路径（默认 ~/.local/bin/claude）
//
// 用法：scripts/runner.sh  （或 node runner/runner.mjs）

import { spawn } from 'node:child_process'
import { homedir } from 'node:os'
import { existsSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const PLATFORM = process.env.SDLC_PLATFORM_URL || 'http://127.0.0.1:8080'
const RUNNER = process.env.SDLC_RUNNER_NAME || `local-${process.pid}`
const INTERVAL = Number(process.env.SDLC_RUNNER_INTERVAL || 3000)
const TIMEOUT_MIN = Number(process.env.SDLC_RUNNER_TIMEOUT_MIN || 30)
const REPO = process.env.SDLC_SKILLS_REPO || path.join(homedir(), 'IdeaProjects/claude-sdlc-skills')
const CLI = process.env.SDLC_CLAUDE_CLI || path.join(homedir(), '.local/bin/claude')
const WORKSPACE = process.env.SDLC_WORKSPACE || path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')

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
    const src = card.card.sourceType === 'feishu'
      ? `飞书需求文档：${card.card.sourceUrl}`
      : `需求原文（直接以此为原文全文，无 doc 块管线）：\n<原文>\n${card.card.sourceText}\n</原文>`
    return `${common}
按以下步骤执行需求质检（sdlc-intent 完整两段 + v1.2 锚点管线）：

1. 读取技能权威文档：
   - ${INTENT}/SKILL.md（两段式工作流）
   - ${INTENT}/references/digest-template.md（梳理 8 区块模板）
   - ${INTENT}/references/dimensions.md 与 references/report-template.md（体检六层维度与报告模板）
   - ${INTENT}/references/doc-pipeline.md（原文预处理与块级锚点管线）
   - ${SYNC_CONTRACT}（payload 契约 v1.2）
2. 输入：${src}
   ${card.card.sourceType === 'feishu' ? '按 doc-pipeline.md 执行：lark-cli 拉 markdown 正文 + 评论区 JSON + 块级 XML（--detail with-ids）→ python3 ' + INTENT + '/scripts/parse_doc.py 产出 doc.json 与 media/（--download）。' : ''}
3. 第一段梳理：按 digest-template 产出 digest（doc.json 存在时出处尽量写到条款级，用 scripts/list_clauses.py 查条款树）。
4. 第二段体检：按 dimensions 六层逐层检查，产出 audit 与澄清清单（每个澄清问题 seq 用体检 P 编号，theme 必填）。
5. 组装 v1.2 payload（严格按 payload-schema）：{snapshot:{md 原文全文}, domains:[全局需求域顺序], digest:{summary,roles,features,fr_points,md}, audit:{red,yellow,blue,items}, items:[澄清清单], ${card.card.sourceType === 'feishu' ? 'doc:{generator,source,blocks,media}' : '（text 源不带 doc）'}}。
   ${card.card.sourceType === 'feishu' ? 'doc.media 里的每个文件先上传到平台媒体端点（mediaId 与 doc.media[].id 一致）；再用 python3 ' + INTENT + '/scripts/resolve_anchors.py 给 payload 附加 anchor（--comments 优先传 list-comments JSON）。' : ''}
6. 把最终 payload 原样写入临时 json 文件，然后（payload 较大时务必用 --data-binary @文件）：
   curl -X PUT ${PLATFORM}/api/requirements/${task.reqId}/drafts/digest_bundle -H 'Content-Type: application/json' --data-binary @<payload.json>
7. 确认响应 200 后输出 SDLC_TASK_RESULT 行（含问题数/需求点数/媒体数/锚点命中统计）。`
  }

  if (task.type === 'standard_doc') {
    return `${common}
执行标准需求文档合成（重写版）。平台已有该需求的全部澄清答复。

1. 拉取平台数据：curl -s ${PLATFORM}/api/requirements/${task.reqId}（含 card/snapshot/digest/audit/items/domains/doc；澄清答复在 items[].answer）。
   澄清项处置语义（DESIGN-v2.2 D21）：items[].status=rejected 的问题不进定稿（连同 answer 一并忽略，PM 已认定不是问题）；status=deferred 的记入遗留假设（需求.Axx）。
2. 读取合成规范：${REPO}/skills/sdlc-sync/references/standard-doc-spec.md（结构/消化规则/质量自查清单，必须逐项遵守：❓ 清零、不发明需求、原文规则全集等价覆盖、✅/⚠️/删除线标注、保留 需求.FR-xx / 体检.Pxx / 需求.Axx / 裸 Pxx 引用记号）。
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
3. 分档扇出（读用例头部风险分级，无字段按 A）：A=4 角色（架构一致性/数据模型与SQL/测试可测性/交叉审查只产分歧）B=2（#2+#4）C=五问自查；headless 单会话按各角色检查单逐角色独立过审并标注角色。
4. 对抗式输出六字段：标题/场景（白话 2-3 句）/原文（≤2 句摘引）/位置/严重度（high|mid|low）/依据（hard|soft）；分歧 div:true；权衡 tradeoff:true 建议驳回。
5. RECONCILE 四分类 + 同根因合并（mergedUnit 相同）。
6. 产出 issues 文件（issue-template 结构，头部 评审状态=待裁决）。
7. 写载荷文件 {"md": <issues 文件全文>, "contentJson": "{\\"issues\\":[{code,sev,theme,role,scene,quote,loc,basis,div,tradeoff,filed,mergedUnit}]"}} 后 curl -X PUT ${PLATFORM}/api/requirements/${task.reqId}/drafts/gate_issues -H 'Content-Type: application/json' --data-binary @<载荷>。
8. 输出 SDLC_TASK_RESULT 行（发现数/裁决单元数/分歧数/备案数）。`
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

/** 机械自查：草稿真的回传了、结构合法，才允许记 done */
async function selfCheck(task) {
  const draftType = task.type === 'quality_check' ? 'digest_bundle'
    : task.type === 'standard_doc' ? 'standard'
    : (task.type === 'design_gen' || task.type === 'design_rev') ? 'design'
    : task.type === 'gate_review' ? 'gate_issues' : 'cases'
  let d
  try { d = await api('GET', `/api/requirements/${task.reqId}/drafts/${draftType}`) } catch { return { ok: false, error: `草稿未回传（${draftType}）` } }
  if (draftType === 'standard') {
    if (!d.md || d.md.trim().length < 200) return { ok: false, error: 'standard 草稿为空或过短' }
  } else if (draftType === 'design') {
    if (!d.md || d.md.trim().length < 200) return { ok: false, error: 'design 草稿为空或过短' }
    if (!d.md.includes('§0')) return { ok: false, error: 'design 草稿缺 §0–§8 结构标头（design-template 九节）' }
  } else if (draftType === 'gate_issues') {
    if (!d.md || d.md.trim().length < 100) return { ok: false, error: 'gate_issues 草稿缺 issues 文件全文（md）' }
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
    await api('POST', `/api/tasks/${task.id}/failed`, { error: `未知任务类型：${task.type}` })
    return
  }
  log(`执行 #${task.id} ${task.type} @ ${task.reqId}（超时 ${TIMEOUT_MIN}min）`)
  const { code, out, err } = await runClaude(prompt)
  const { summary, isError, subtype } = extractResult(out)
  const chk = await selfCheck(task)
  if (code === 0 && chk.ok && !isError) {
    const result = summary || JSON.stringify({ draft: chk.draftType })
    await api('POST', `/api/tasks/${task.id}/done`, { result })
    log(`#${task.id} 完成：${result.slice(0, 160)}`)
  } else {
    const reason = !chk.ok ? chk.error
      : code !== 0 ? `CLI 退出码 ${code}：${(err || out).slice(-400)}`
      : `CLI 异常结束（${subtype}）：${summary || out.slice(-400)}`
    await api('POST', `/api/tasks/${task.id}/failed`, { error: reason }).catch(e => log('回报失败也失败：', e.message))
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
