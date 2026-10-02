// M5（v2.0 全流程线上化）：平台任务队列 + AI 草稿两态 断言。
// 覆盖：任务状态机、建任务守卫、草稿回传/回读、approve 转正（守卫继承：两轮推送 carried/changed）、
// cases 乐观锁冲突、标准文档任务守卫、发布终态锁。
// 用法：node scripts/verify-m5.mjs [baseUrl]   （默认 http://127.0.0.1:8080）
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
const expectErr = async (method, path, body, status, name) => {
  await call(method, path, body)
    .then(() => check(name, 'no-error', status))
    .catch(e => check(name, e.status, status))
}

// ===== 启动清理：排空历史运行残留的 queued 任务（逐个领走并回报 done），保证后续断言确定性 =====
for (let i = 0; i < 50; i++) {
  const t = await fetch(`${base}/api/tasks/next?runner=verify-m5`).then(r => r.status === 200 ? r.json() : null)
  if (!t) break
  await call('POST', `/api/tasks/${t.id}/done`, { result: '{"drainedBy":"verify-m5"}' })
}

// ===== v2.1：暂停进程内 agent（仅当其运行且未暂停；由本脚本暂停的结束才恢复） =====
let agentPausedByMe = false
try {
  const st = await call('GET', '/api/agent/status')
  if (st.enabled && st.running && !st.paused) {
    await call('POST', '/api/agent/pause'); agentPausedByMe = true
  }
} catch {}

// ===== 夹具：需求卡 A =====
const { id: reqA } = await call('POST', '/api/requirements', {
  name: 'M5 任务与草稿验证', sourceType: 'text', sourceText: 'M5 fixture', submittedBy: 'dev'
})

const pushPayload = (question, answer) => ({
  snapshot: { md: '# M5 快照\n\n详述1 测试' },
  domains: ['查询与展示口径'],
  digest: {
    summary: 'M5', roles: ['用户'],
    features: [{ role: '用户', m: '⭐ 列表', func: '列表', cov: '✅', src: '详述1' }],
    fr_points: [{ id: '需求.FR-01', name: '排序', source: '详述1', group: '§1 列表', module: '列表' }],
    md: '# digest'
  },
  audit: {
    red: 0, yellow: 1, blue: 0,
    items: [{ id: '体检.P01', level: 'yellow', title: '排序口径', where: '详述1', go: '待澄清', group: '查询与展示口径', quote: '「倒序」' }]
  },
  items: [{ seq: 'P01', level: 'yellow', theme: '查询与展示口径', question, ctx: '详述1', answer: answer || '', quote: '' }]
})
const casesPayload = n => ({ items: Array.from({ length: n }, (_, i) => ({
  id: `TC-${i + 1}`, title: `用例${i + 1}`, points: ['需求.FR-01'], priority: 'P1',
  pre: '', steps: '步骤', expect: '结果', owner: 'dev', tool: 'sdlc-test', status: 'draft',
  verifyRefs: ['体检.P01'], defectRefs: []
})) })

// ===== 1. 建任务守卫 =====
const t1 = await call('POST', `/api/requirements/${reqA}/tasks`, { type: 'quality_check' })
check('建任务：queued + 类型正确', [t1.status, t1.type, t1.reqId], ['queued', 'quality_check', reqA])
await expectErr('POST', `/api/requirements/${reqA}/tasks`, { type: 'quality_check' }, 409, '重复任务防抖：409')
await expectErr('POST', `/api/requirements/${reqA}/tasks`, { type: 'bogus' }, 400, '非法类型：400')
await expectErr('POST', '/api/requirements/REQ-9999-999/tasks', { type: 'quality_check' }, 404, '需求不存在：404')
// submitted 状态不可生成标准文档（状态守卫先于答复计数守卫）
await expectErr('POST', `/api/requirements/${reqA}/tasks`, { type: 'standard_doc' }, 409, '非待澄清状态生成标准文档：409')

// ===== 2. Runner 领取与回报状态机 =====
const next1 = await call('GET', '/api/tasks/next?runner=verify-m5')
check('领取：原子置 running + 记录 runner', [next1.status, next1.runner], ['running', 'verify-m5'])
const next2raw = await fetch(`${base}/api/tasks/next?runner=verify-m5`)
check('队列为空：204', next2raw.status, 204)
const doneRet = await call('POST', `/api/tasks/${next1.id}/done`, { result: '{"anchors":{"hit":3,"miss":0}}' })
check('回报 done：状态 + result 保存', [doneRet.status, doneRet.result], ['done', '{"anchors":{"hit":3,"miss":0}}'])
await expectErr('POST', `/api/tasks/${next1.id}/done`, { result: 'x' }, 409, '终态不可再回报：409')

// ===== 3. failed + retry =====
const t3 = await call('POST', `/api/requirements/${reqA}/tasks`, { type: 'case_gen' })
await call('GET', '/api/tasks/next?runner=verify-m5') // 领走 t3
await call('POST', `/api/tasks/${t3.id}/failed`, { error: '注入的失败' })
const t3view = await call('GET', `/api/tasks?req_id=${reqA}&status=failed`)
check('回报 failed：状态 + 错误保存', t3view.filter(x => x.id === t3.id).map(x => [x.status, JSON.parse(x.result).error]), [['failed', '注入的失败']])
const t3r = await call('POST', `/api/tasks/${t3.id}/retry`)
check('重试：failed → queued', t3r.status, 'queued')
await expectErr('POST', `/api/tasks/${t3.id}/retry`, undefined, 409, 'queued 不可重试：409')

// ===== 4. 草稿回传/回读 + approve 转正（两轮推送验证守卫继承）=====
const put1 = await call('PUT', `/api/requirements/${reqA}/drafts/digest_bundle`,
  { contentJson: JSON.stringify(pushPayload('排序口径？')), taskId: t1.id })
check('草稿回传：回读类型与 taskId', [put1.type, put1.taskId], ['digest_bundle', t1.id])
check('草稿回读：contentJson 完整',
  JSON.parse((await call('GET', `/api/requirements/${reqA}/drafts/digest_bundle`)).contentJson).digest.summary, 'M5')
await expectErr('PUT', `/api/requirements/${reqA}/drafts/digest_bundle`, { md: '' }, 400, '缺 contentJson：400')

// approve#1：转正 → clarifying（首轮 carried=0）
const ap1 = await call('POST', `/api/requirements/${reqA}/drafts/digest_bundle/approve`)
check('approve#1 转正：pushArtifacts 生效', [ap1.ok, ap1.items, ap1.carriedAnswers, ap1.draftApproved], [true, 1, 0, true])
check('approve 后草稿删除：清单为空', (await call('GET', `/api/requirements/${reqA}/drafts`)).length, 0)
const det = await call('GET', `/api/requirements/${reqA}`)
check('转正效果：状态 clarifying + 快照落库', [det.card.status, det.snapshot.md.includes('M5 快照')], ['clarifying', true])
// clarifying + 有未答 → standard_doc 400（答复计数守卫）
await expectErr('POST', `/api/requirements/${reqA}/tasks`, { type: 'standard_doc' }, 400, '未答完生成标准文档：400')

// PM 在平台作答（仅 clarifying 可答）——v2.1 起全答完触发 L2 自动排 standard_doc
await call('PUT', `/api/requirements/${reqA}/answers/P01`, { answer: '按创建时间倒序' })
{
  const auto = (await call('GET', `/api/tasks?req_id=${reqA}`))
    .find(t => t.type === 'standard_doc' && t.status === 'queued' && (t.createdBy || '').startsWith('auto'))
  check('L2：全答完自动排 standard_doc（createdBy=auto）', !!auto, true)
  await call('POST', `/api/tasks/${auto.id}/cancel`)   // 取消，保持后续"人工可建"断言的干净世界
}

// approve#2：同 seq 同问题文本 → carried=1（重推不冲掉人工作答）
await call('PUT', `/api/requirements/${reqA}/drafts/digest_bundle`,
  { contentJson: JSON.stringify(pushPayload('排序口径？')) })
const ap2 = await call('POST', `/api/requirements/${reqA}/drafts/digest_bundle/approve`)
check('approve#2 守卫继承：carried=1', [ap2.carriedAnswers, ap2.changedQuestions], [1, 0])

// approve#3：P 编号漂移（seq 同、问题文本变）→ changed=1 不携带旧答复
await call('PUT', `/api/requirements/${reqA}/drafts/digest_bundle`,
  { contentJson: JSON.stringify(pushPayload('排序口径（改）？')) })
const ap3 = await call('POST', `/api/requirements/${reqA}/drafts/digest_bundle/approve`)
check('approve#3 P 编号漂移：changed=1 不携带', [ap3.changedQuestions, ap3.carriedAnswers], [1, 0])

// ===== 5. cases 草稿 approve + 乐观锁冲突 =====
await call('PUT', `/api/requirements/${reqA}/drafts/cases`, { contentJson: JSON.stringify(casesPayload(2)) })
const cap1 = await call('POST', `/api/requirements/${reqA}/drafts/cases/approve`)
check('cases approve：登记 2 条', [cap1.ok, cap1.inserted, cap1.updated], [true, 2, 0])
const caseDetail = await call('GET', `/api/cases/${reqA}/TC-1`)
check('用例落库：verifyRefs 反推体检关联', caseDetail.verifyRefs, ['体检.P01'])
// 平台侧另一推送方更新（last-write-wins，不带 expectedVersion）→ TC-1 版本 +1
const platformChanged = casesPayload(2).items.map((c, i) => i === 0 ? { ...c, title: '平台改动' } : c)
await call('POST', '/api/cases', { reqId: reqA, items: platformChanged })
await call('PUT', `/api/requirements/${reqA}/drafts/cases`,
  { contentJson: JSON.stringify({ reqId: reqA, items: casesPayload(2).items.map((c, i) => i === 0 ? { ...c, expectedVersion: 1 } : c) }) })
await expectErr('POST', `/api/requirements/${reqA}/drafts/cases/approve`, undefined, 409, '用例版本冲突：整批 409')
check('冲突批次未应用（TC-1 仍是平台改动）', (await call('GET', `/api/cases/${reqA}/TC-1`)).title, '平台改动')

// ===== 6. 标准文档任务守卫（全部答复后可建）+ 发布终态锁 =====
await call('PUT', `/api/requirements/${reqA}/answers/P01`, { answer: '按创建时间倒序（新）' })
const t4 = await call('POST', `/api/requirements/${reqA}/tasks`, { type: 'standard_doc' })
check('全部答复后 standard_doc 任务可建', t4.status, 'queued')
await call('PUT', `/api/requirements/${reqA}/drafts/standard`, { md: '# 标准文档草稿' })
check('standard 草稿可写（发布前人工过目载体）',
  (await call('GET', `/api/requirements/${reqA}/drafts/standard`)).md, '# 标准文档草稿')
await expectErr('POST', `/api/requirements/${reqA}/drafts/standard/approve`, undefined, 400, 'standard 无 approve：400')
await call('POST', `/api/requirements/${reqA}/publish`, { standardMd: '# 定稿' })
const pub = await call('GET', `/api/requirements/${reqA}`)
check('发布终态', pub.card.status, 'published')
await expectErr('POST', `/api/requirements/${reqA}/tasks`, { type: 'quality_check' }, 409, 'published 后建任务：409')
await expectErr('PUT', `/api/requirements/${reqA}/drafts/standard`, { md: 'x' }, 409, 'published 后写草稿：409')
await expectErr('PUT', `/api/requirements/${reqA}/answers/P01`, { answer: '' }, 409, 'published 后答复锁定：409')

console.log(failures === 0 ? '\nM5 全部通过 ✅' : `\nM5 失败 ${failures} 项 ❌`)
if (agentPausedByMe) await call('POST', '/api/agent/resume').catch(() => {})

// 收尾自清理：取消 L2 发布钩子自动排的 case_gen（质检/合成残留已由发布收尾钩子自动作废——B2/P0-2b）
try {
  for (const t of await call('GET', `/api/tasks?req_id=${reqA}`)) {
    if (t.status === 'queued') await call('POST', `/api/tasks/${t.id}/cancel`).catch(() => {})
  }
} catch {}
process.exit(failures === 0 ? 0 : 1)
