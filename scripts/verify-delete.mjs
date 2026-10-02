// B4 验收：软删除（评测 P0-3）
// 覆盖：删后列表不含/详情 404（写守卫经 find 根过滤继承）/用例列表空/用例详情 404/
//       同年编号不复用/二次删除 404/running 任务时拒绝 409/queued 任务随删自动作废
// 用法：node scripts/verify-delete.mjs [baseUrl]   （默认 http://127.0.0.1:8080）
const base = process.argv[2] || 'http://127.0.0.1:8080'

let failures = 0
function check(name, actual, expected) {
  const ok = JSON.stringify(actual) === JSON.stringify(expected)
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${ok ? '' : '：' + JSON.stringify(actual) + '（期望 ' + JSON.stringify(expected) + '）'}`)
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

// ===== 1. 夹具：建卡 → 推产物 → 表单建用例 + 评论 → 建 queued 任务 =====
const { id: R } = await call('POST', '/api/requirements', {
  name: 'B4 软删除验证', sourceType: 'text', sourceText: 'delete fixture', submittedBy: 'verify-delete'
})
await call('POST', `/api/requirements/${R}/artifacts`, {
  digest: { summary: 's', roles: [], features: [], fr_points: [{ id: '需求.FR-01', name: '点一' }] },
  audit: { red: 0, yellow: 1, blue: 0, items: [] },
  items: [{ seq: 'P01', level: 'yellow', theme: '删除域', question: '问题一', ctx: '', answer: '' }],
  snapshot: { md: '# 快照' }
})
const c = await call('POST', '/api/cases/form', {
  reqId: R, title: '删除验证用例', points: ['需求.FR-01'], priority: 'P1', owner: 'verify-delete', steps: '步骤', expect: '预期'
})
await call('POST', `/api/cases/${R}/${c.id}/comments`, { by: 'verify-delete', text: '随需求删除的评论' })
const task = await call('POST', `/api/requirements/${R}/tasks`, { type: 'quality_check' })
check('前置：queued 任务已建', task.status, 'queued')

// ===== 2. 删除：软删 + queued 任务自动作废 =====
const del = await call('DELETE', `/api/requirements/${R}`)
check('删除返回 ok', del.ok, true)
check('queued 任务随删自动作废', del.cancelledTasks, 1)

// ===== 3. 删后可见性与守卫 =====
const list = await call('GET', '/api/requirements')
check('列表不含已删需求', list.some(r => r.id === R), false)
let gone = null
try { await call('GET', `/api/requirements/${R}`) } catch (e) { gone = e.status }
check('详情 404（写守卫同路径继承）', gone, 404)
const cases = await call('GET', `/api/cases?reqId=${R}`)
check('用例列表按需求过滤为空', cases.length, 0)
let caseGone = null
try { await call('GET', `/api/cases/${R}/${c.id}`) } catch (e) { caseGone = e.status }
check('用例详情 404（requireReq 补位）', caseGone, 404)
let repush = null
try { await call('POST', `/api/requirements/${R}/artifacts`, { digest: { summary: 'x', roles: [], features: [], fr_points: [] }, audit: { red: 0, yellow: 0, blue: 0, items: [] }, items: [] }) } catch (e) { repush = e.status }
check('已删需求重推产物 404', repush, 404)
const tasks = await call('GET', `/api/tasks?req_id=${R}`)
check('随删作废的任务终态 failed', tasks.find(t => t.id === task.id)?.status, 'failed')
check('作废原因 reason=req-deleted', JSON.parse(tasks.find(t => t.id === task.id)?.result || '{}').reason, 'req-deleted')

// ===== 4. 编号不复用 + 二次删除 404 =====
const year = new Date().getFullYear()
const seqOf = id => Number(id.slice(id.lastIndexOf('-') + 1))
const { id: R2 } = await call('POST', '/api/requirements', {
  name: 'B4 编号不复用验证', sourceType: 'text', sourceText: 'seq fixture', submittedBy: 'verify-delete'
})
check('同年新建编号跳号（不复用已删）', seqOf(R2) > seqOf(R), true)
let twice = null
try { await call('DELETE', `/api/requirements/${R}`) } catch (e) { twice = e.status }
check('二次删除 404', twice, 404)
await call('DELETE', `/api/requirements/${R2}`)   // 清理夹具

// ===== 5. running 任务时拒绝删除（review 修复：先排空队列再自建自领，保证领到本夹具任务且不遗弃他人任务） =====
for (let i = 0; i < 50; i++) {
  const r = await fetch(`${base}/api/tasks/next?runner=verify-delete-drain`)
  if (r.status !== 200) break
  const t = await r.json()
  await call('POST', `/api/tasks/${t.id}/done`, { result: '{"drainedBy":"verify-delete"}' })
}
const { id: R3 } = await call('POST', '/api/requirements', {
  name: 'B4 running 拒删验证', sourceType: 'text', sourceText: 'running fixture', submittedBy: 'verify-delete'
})
const t3 = await call('POST', `/api/requirements/${R3}/tasks`, { type: 'quality_check' })
const mine = await fetch(`${base}/api/tasks/next?runner=verify-delete`).then(r => r.status === 200 ? r.json() : null)
check('夹具任务被本脚本领取（running）', [mine?.id, mine?.status], [t3.id, 'running'])
let busy = null
try { await call('DELETE', `/api/requirements/${R3}`) } catch (e) { busy = e.status }
check('running 任务时删除 409', busy, 409)
await call('POST', `/api/tasks/${t3.id}/done`, { result: '{"cleanup":"verify-delete"}' })
await call('DELETE', `/api/requirements/${R3}`)
check('任务完成后可删', true, true)

console.log(failures === 0 ? '\n软删除验证全部通过 ✓' : `\n${failures} 项失败 ✗`)
process.exit(failures === 0 ? 0 : 1)
