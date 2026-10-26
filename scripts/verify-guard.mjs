// 保护逻辑回归：重推保留答复 & 版本冲突拦截 & 快照刷新语义（v1.1）
// 用法：node scripts/verify-guard.mjs [baseUrl]（与其他 verify 脚本一致；不传默认 8888）
const base = process.argv[2] || 'http://127.0.0.1:8888'
let failures = 0
const check = (n, a, e) => { const ok = JSON.stringify(a) === JSON.stringify(e); console.log(`${ok ? 'PASS' : 'FAIL'}  ${n}${ok ? '' : '：' + JSON.stringify(a)}`); if (!ok) failures++ }
const call = async (m, p, b) => {
  const res = await fetch(base + p, { method: m, headers: b ? { 'Content-Type': 'application/json' } : undefined, body: b ? JSON.stringify(b) : undefined })
  const d = await res.json().catch(() => ({}))
  if (!res.ok) { const err = new Error(d.error); err.status = res.status; err.data = d; throw err }
  return d
}
// 造一个测试需求
const { id } = await call('POST', '/api/requirements', { name: '守卫逻辑验证', sourceType: 'text', sourceText: 'x' })

// B6 输入校验收紧（评测 P1-4）：空白与缺字段区分、priority 白名单、名称长度上限
let blankSteps = null, blankMsg = ''
try { await call('POST', '/api/cases/form', { reqId: id, title: 'x', steps: '   ', expect: 'e', owner: 'o' }) }
catch (e) { blankSteps = e.status; blankMsg = e.message }
check('空白 steps 400 且文案区分「不能为空」', [blankSteps, blankMsg.includes('不能为空')], [400, true])
let badPriority = null
try { await call('POST', '/api/cases/form', { reqId: id, title: 'x', priority: 'P9', steps: 's', expect: 'e', owner: 'o' }) }
catch (e) { badPriority = e.status }
check('表单 priority 非法 400', badPriority, 400)
let longName = null
try { await call('POST', '/api/requirements', { name: '长'.repeat(121), sourceType: 'text', sourceText: 'x' }) }
catch (e) { longName = e.status }
check('需求名超 120 字 400', longName, 400)
const items = [
  { seq: 'P01', level: 'red', theme: '守卫域', question: '问题一', answer: '已有答复 A' },
  { seq: 'P02', level: 'blue', theme: '守卫域', question: '问题二', answer: '' }
]
const fr = [{ id: '需求.FR-01', name: '点一' }]
const payload = { digest: { summary: 's', roles: [], features: [], fr_points: fr }, audit: { red: 1, yellow: 0, blue: 0, items: [] }, items, snapshot: { md: '# 快照 v1' } }
await call('POST', `/api/requirements/${id}/artifacts`, payload)
const snapV1 = (await call('GET', `/api/requirements/${id}`)).snapshot
check('首推快照落库 version=1', snapV1.version, 1)
// theme 必填（v1.1 契约）：需在未发布状态上验证，否则被"已发布拒推"抢先拦截
let themeRequired = false
try {
  await call('POST', `/api/requirements/${id}/artifacts`, { digest: { summary: 's', roles: [], features: [], fr_points: [] }, audit: { red: 0, yellow: 0, blue: 0, items: [] }, items: [{ seq: 'P99', level: 'red', question: '无 theme' }] })
} catch (e) { themeRequired = e.status === 400 && e.message.includes('theme') }
check('澄清缺 theme 400', themeRequired, true)
// PM 在平台答了 P02
await call('PUT', `/api/requirements/${id}/answers/P02`, { answer: '平台作答 B' })
// 重推：payload 不带 P02 答复（模拟本地没同步过平台答复的场景），快照刷新 v2
payload.items[1].answer = ''
payload.snapshot = { md: '# 快照 v2' }
const repushed = await call('POST', `/api/requirements/${id}/artifacts`, payload)
check('重推保留平台处置 carried=2（D17：P02 平台答 + P01 首推已答机器行均整行携带）',
  repushed.carriedAnswers, 2)
check('重推刷新快照', repushed.snapshotUpdated, true)
const d1 = await call('GET', `/api/requirements/${id}`)
check('重推后 P02 答复仍在', d1.items.find(i => i.seq === 'P02').answer, '平台作答 B')
check('重推后 P01 本地答复仍在', d1.items.find(i => i.seq === 'P01').answer, '已有答复 A')
check('重推后状态仍 clarifying', d1.card.status, 'clarifying')
check('重推后快照更新（md/version）', [d1.snapshot.md, d1.snapshot.version], ['# 快照 v2', 2])
// 重推不带 snapshot → 旧快照保留（可选字段语义）
delete payload.snapshot
await call('POST', `/api/requirements/${id}/artifacts`, payload)
const d2 = await call('GET', `/api/requirements/${id}`)
check('不带快照重推：旧快照保留', [d2.snapshot.md, d2.snapshot.version], ['# 快照 v2', 2])
// A2 守卫：seq 相同但问题文本已变（P 编号漂移）→ 不携带旧答复，changedQuestions 上报
payload.items[1].question = '问题二（改写）'
const drifted = await call('POST', `/api/requirements/${id}/artifacts`, payload)
check('P 编号漂移：P02 不携带（P01 仍携带，changed=1）',
  [drifted.carriedAnswers, drifted.changedQuestions], [1, 1])
const dDrift = await call('GET', `/api/requirements/${id}`)
check('P 编号漂移：P02 答复清空待重答', dDrift.items.find(i => i.seq === 'P02').answer, '')
// 注意：payload 的问题文本保持改写版不再改回——后续重推需与库内文本一致，否则再触发漂移干扰处置保留断言
// ===== v2.2 处置通道（DESIGN-v2.2 D21/D23）：驳回/挂起/重开 =====
// 驳回必填理由
let noNote = null
try { await call('PUT', `/api/requirements/${id}/answers/P02`, { status: 'rejected' }) } catch (e) { noNote = e.status }
check('驳回缺 note 400', noNote, 400)
// 驳回无 note 的非法 status
let badStatus = null
try { await call('PUT', `/api/requirements/${id}/answers/P02`, { status: 'closed' }) } catch (e) { badStatus = e.status }
check('非法 status 400', badStatus, 400)
// 空 answer 直接驳回（最常见路径：PM 未作答即认定不是问题）
await call('PUT', `/api/requirements/${id}/answers/P02`, { status: 'rejected', note: '原文已明确，不构成缺口' })
const dRej = await call('GET', `/api/requirements/${id}`)
const rejItem = dRej.items.find(i => i.seq === 'P02')
check('驳回落库：status/note/answer', [rejItem.status, rejItem.note, rejItem.answer], ['rejected', '原文已明确，不构成缺口', ''])
// 驳回计入已处置（counts 口径与 TaskService 守卫同源）
check('驳回后 answered/totalQ', [dRej.answered, dRej.totalQ], [2, 2])
// D23 核心：空答复的驳回在重推时必须保留（携带面 = answer 非空 OR status 非空）
const repushed2 = await call('POST', `/api/requirements/${id}/artifacts`, payload)
check('空答复驳回重推保留（carried=2：P02 驳回 + P01 已答）', repushed2.carriedAnswers, 2)
const dRej2 = await call('GET', `/api/requirements/${id}`)
const rejItem2 = dRej2.items.find(i => i.seq === 'P02')
check('重推后驳回与理由仍在', [rejItem2.status, rejItem2.note], ['rejected', '原文已明确，不构成缺口'])
// 重开：驳回行重新作答 = 转 answered（answered_at 记处置时间）
await call('PUT', `/api/requirements/${id}/answers/P02`, { answer: '重开后作答' })
const dReopen = await call('GET', `/api/requirements/${id}`)
check('重开作答转 answered', dReopen.items.find(i => i.seq === 'P02').status, 'answered')
// 再挂起：answered → deferred（发布前最后形态，验证挂起也计入已处置）
await call('PUT', `/api/requirements/${id}/answers/P02`, { status: 'deferred', note: '下期处理' })
const dDef = await call('GET', `/api/requirements/${id}`)
check('挂起后仍计入已处置', [dDef.answered, dDef.totalQ], [2, 2])
check('挂起状态落库', dDef.items.find(i => i.seq === 'P02').status, 'deferred')
// 已发布拒绝重推
await call('POST', `/api/requirements/${id}/publish`, { standardMd: '# x' })
let pubLocked = false
try { await call('POST', `/api/requirements/${id}/artifacts`, payload) } catch (e) { pubLocked = e.status === 409 }
check('已发布拒绝重推产物', pubLocked, true)
// 用例版本冲突
await call('POST', '/api/cases', { reqId: id, items: [{ id: 'TC-01', title: 't', points: [], owner: 'a', tool: 'sdlc-test', status: 'reviewed' }] })
let conflict = false
try {
  await call('POST', '/api/cases', { reqId: id, items: [{ id: 'TC-01', title: 't2', points: [], owner: 'a', tool: 'sdlc-test', status: 'reviewed', expectedVersion: 99 }] })
} catch (e) { conflict = e.status === 409 && e.data.conflicts?.[0]?.currentVersion === 1 }
check('版本冲突整批拒绝并报当前版本', conflict, true)
const list = await call('GET', `/api/cases?reqId=${id}`)
check('冲突批次未应用（title 未变）', list[0].title, 't')
console.log(failures === 0 ? '\n守卫逻辑全部通过 ✓' : `\n${failures} 项失败 ✗`)
process.exit(failures)
