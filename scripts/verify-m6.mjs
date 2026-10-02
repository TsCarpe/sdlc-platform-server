// M6（v2.1 L2 生命周期编排）：确定性事件钩子断言。
// 覆盖：全答完自动排 standard_doc（createdBy=auto）、只自动一次（改答/取消后不复活）、
//       守卫全继承（人工 409 与自动共用）、发布后自动排 case_gen、编排失败不连累主操作。
// 用法：node scripts/verify-m6.mjs [baseUrl]   （默认 http://127.0.0.1:8080）
const base = process.argv[2] || 'http://127.0.0.1:8080'

let failures = 0
function check(name, actual, expected) {
  const ok = JSON.stringify(actual) === JSON.stringify(expected)
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}: ${JSON.stringify(actual)}${ok ? '' : '（期望 ' + JSON.stringify(expected) + '）'}`)
  if (!ok) failures++
}
async function call(method, path, body) {
  const res = await fetch(base + path, {
    method,
    headers: body ? { 'Content-Type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined
  })
  const data = await res.json().catch(() => ({}))
  if (!res.ok) { const err = new Error(data.error || ''); err.status = res.status; err.data = data; throw err }
  return data
}
const tasksOf = async (reqId, type) =>
  (await call('GET', `/api/tasks?req_id=${reqId}`)).filter(t => t.type === type)
const activeOf = async (reqId, type) =>
  (await tasksOf(reqId, type)).filter(t => t.status === 'queued' || t.status === 'running')

// ===== 进程内 agent 暂停（仅当其在运行且未被暂停；由本脚本暂停的结束才恢复） =====
let agentPausedByMe = false
try {
  const st = await call('GET', '/api/agent/status')
  if (st.enabled && st.running && !st.paused) {
    await call('POST', '/api/agent/pause'); agentPausedByMe = true
    console.log('（已暂停进程内 agent，结束自动恢复）')
  }
} catch {}

// ===== 启动清理：排空历史 queued 任务（防止常驻执行器领走本夹具任务） =====
for (let i = 0; i < 50; i++) {
  const r = await fetch(`${base}/api/tasks/next?runner=verify-m6`)
  if (r.status !== 200) break
  const t = await r.json()
  await call('POST', `/api/tasks/${t.id}/done`, { result: '{"drainedBy":"verify-m6"}' })
}

// ===== 夹具：两问澄清清单 =====
const { id: R } = await call('POST', '/api/requirements', {
  name: 'M6 L2编排验证', sourceType: 'text', sourceText: 'M6 fixture', submittedBy: 'verify-m6'
})
const payload = {
  snapshot: { md: '# M6 快照\n\n详述1 排序\n详述2 导出' },
  domains: ['查询与展示口径', '数据导出'],
  digest: {
    summary: 'M6', roles: ['用户'],
    features: [], fr_points: [{ id: '需求.FR-01', name: '排序', source: '详述1', group: '§1', module: '列表' }], md: '# digest'
  },
  audit: { red: 0, yellow: 2, blue: 0, items: [
    { id: '体检.P01', level: 'yellow', title: '排序口径', where: '详述1', go: '待澄清', group: '查询与展示口径', quote: '' },
    { id: '体检.P02', level: 'yellow', title: '导出范围', where: '详述2', go: '待澄清', group: '数据导出', quote: '' }
  ] },
  items: [
    { seq: 'P01', level: 'yellow', theme: '查询与展示口径', question: '排序口径？', ctx: '', answer: '', quote: '' },
    { seq: 'P02', level: 'yellow', theme: '数据导出', question: '导出范围？', ctx: '', answer: '', quote: '' }
  ]
}
await call('PUT', `/api/requirements/${R}/drafts/digest_bundle`, { contentJson: JSON.stringify(payload) })
await call('POST', `/api/requirements/${R}/drafts/digest_bundle/approve`)

// ===== 1. 未答完：不自动排 =====
check('未答完：无 standard_doc 任务', (await tasksOf(R, 'standard_doc')).length, 0)
await call('PUT', `/api/requirements/${R}/answers/P01`, { answer: '按创建时间倒序' })
check('答 1/2：仍无 standard_doc 任务', (await tasksOf(R, 'standard_doc')).length, 0)

// ===== 2. 全答完：自动排 standard_doc =====
await call('PUT', `/api/requirements/${R}/answers/P02`, { answer: '全量导出' })
const autos = await activeOf(R, 'standard_doc')
check('全答完：自动排 1 个 standard_doc', autos.length, 1)
check('自动任务 createdBy 标记', autos[0]?.createdBy, 'auto:clarify-complete')

// ===== 3. 只自动一次：改答不重复建 =====
await call('PUT', `/api/requirements/${R}/answers/P02`, { answer: '全量导出（含已删）' })
check('改答：活跃任务仍 1 个（不重复建）', (await activeOf(R, 'standard_doc')).length, 1)
check('历史任务仍 1 个（只自动一次）', (await tasksOf(R, 'standard_doc')).length, 1)

// ===== 4. 守卫共享：人工建同类任务 409 =====
await call('POST', `/api/requirements/${R}/tasks`, { type: 'standard_doc' })
  .then(() => check('人工建同类任务：409', 'no-error', 409))
  .catch(e => check('人工建同类任务：409', e.status, 409))

// ===== 5. 取消即终局：不复活 =====
const autoId = (await activeOf(R, 'standard_doc'))[0].id
await call('POST', `/api/tasks/${autoId}/cancel`)
check('取消自动任务：终态 failed', (await tasksOf(R, 'standard_doc')).find(t => t.id === autoId)?.status, 'failed')
await call('PUT', `/api/requirements/${R}/answers/P01`, { answer: '按创建时间倒序（改）' })
check('取消后再改答：不复活', (await activeOf(R, 'standard_doc')).length, 0)

// ===== 6. 发布后：自动排 case_gen =====
await call('POST', `/api/requirements/${R}/publish`, { standardMd: '# M6 定稿\n\n（L2 编排验证用标准文档，满足最小长度要求的正文内容。）' })
const autoCases = await activeOf(R, 'case_gen')
check('发布后：自动排 1 个 case_gen', autoCases.length, 1)
check('case_gen createdBy 标记', autoCases[0]?.createdBy, 'auto:published')
const caseId = autoCases[0].id
await call('POST', `/api/tasks/${caseId}/cancel`)
check('取消 case_gen：终态 failed', (await tasksOf(R, 'case_gen')).find(t => t.id === caseId)?.status, 'failed')

// ===== 7. 发布收尾：queued 的质检/合成残留任务自动作废（评测 P0-2b）=====
const { id: R2 } = await call('POST', '/api/requirements', {
  name: 'M6 发布收尾验证', sourceType: 'text', sourceText: 'M6 fixture 2', submittedBy: 'verify-m6'
})
const payload2 = JSON.parse(JSON.stringify(payload))
payload2.items.forEach(it => { it.answer = '' })
await call('PUT', `/api/requirements/${R2}/drafts/digest_bundle`, { contentJson: JSON.stringify(payload2) })
await call('POST', `/api/requirements/${R2}/drafts/digest_bundle/approve`)
await call('PUT', `/api/requirements/${R2}/answers/P01`, { answer: '按创建时间倒序' })
await call('PUT', `/api/requirements/${R2}/answers/P02`, { answer: '全量导出' })
const stdQueued = (await activeOf(R2, 'standard_doc'))[0]
check('场景前置：自动排的 standard_doc 处于 queued', stdQueued?.status, 'queued')
// 直接发布（预览可编辑直发路径）：queued 的 standard_doc 应被发布收尾钩子自动作废，而不是留给 Runner 空跑
await call('POST', `/api/requirements/${R2}/publish`, { standardMd: '# M6 定稿2\n\n（发布收尾验证用标准文档，满足最小长度要求的正文内容。）' })
const stdAfter = (await tasksOf(R2, 'standard_doc')).find(t => t.id === stdQueued.id)
check('发布后：残留 standard_doc 自动作废 failed', stdAfter?.status, 'failed')
check('作废原因结构化 reason', JSON.parse(stdAfter?.result || '{}').reason, 'superseded-by-publish')
check('case_gen 仍被自动排队（收尾不阻断编排）', (await activeOf(R2, 'case_gen')).length, 1)
let retryRejected = null
try { await call('POST', `/api/tasks/${stdQueued.id}/retry`) } catch (e) { retryRejected = e.status }
check('已作废任务不可重试：409', retryRejected, 409)
// 夹具清理：取消自动排的 case_gen
for (const t of await activeOf(R2, 'case_gen')) await call('POST', `/api/tasks/${t.id}/cancel`).catch(() => {})

console.log(failures === 0 ? '\nM6 全部通过 ✅' : `\nM6 失败 ${failures} 项 ❌`)
if (agentPausedByMe) await call('POST', '/api/agent/resume').catch(() => {})
process.exit(failures === 0 ? 0 : 1)
