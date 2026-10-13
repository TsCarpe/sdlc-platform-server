// M2 验收：澄清闭环全链路（演练副本，不动真实需求）
// 建演练副本 → 推产物 → 模拟 PM 全量作答 → 拉回答复 → 发布标准文档 → 回读校验
// 用法：node scripts/verify-m2.mjs [baseUrl]
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'
import { loadReal } from './real-data.load.mjs'

const base = process.argv[2] || 'http://127.0.0.1:8080'
const { req } = await loadReal()

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
  if (!res.ok) throw new Error(`${method} ${path} -> ${res.status}: ${data.error || ''}`)
  return data
}

// 1. 演练副本建卡 + 推产物
// v2.4 迭代 1:1（DESIGN-v2.4 D32）：演练副本不占用真实迭代——iter 置空（副本可按名称识别，不属于任何迭代）
const created = await call('POST', '/api/requirements', {
  name: req.name + '（M2 演练副本）', iter: '', sourceType: 'text',
  sourceText: '演练副本，验证澄清闭环全链路', submittedBy: 'dev',
  risk_tier: 'B'   // v1.3：建卡带分级，推产物 digest 后到覆盖（DESIGN-v2.2 D19）
})
// v1.3 risk_tier：建卡落库
const tier0 = await call('GET', `/api/requirements/${created.id}`)
check('risk_tier：建卡携带 B', tier0.card.riskTier, 'B')
let tierBad = false
try { await call('POST', '/api/requirements', { name: 'tier 非法值演练', sourceType: 'text', sourceText: 'x', risk_tier: 'D' }) }
catch (e) { tierBad = e.message.includes('非法 risk_tier') }
check('risk_tier：非法值 400', tierBad, true)
const pushPayload = {
  digest: {
    summary: req.digest.summary, roles: req.digest.roles, features: req.digest.features || [],
    fr_points: req.digest.fr.map(f => ({ id: f.id, name: f.name })), md: '',
    risk_tier: 'A'
  },
  audit: { red: req.audit.red, yellow: req.audit.yellow, blue: req.audit.blue, items: req.audit.items, md: '' },
  items: req.checklist.map(c => ({
    seq: c.id, level: c.level, theme: c.theme, question: c.q, ctx: c.ctx, answer: c.answer || '', quote: c.quote || ''
  }))
}
await call('POST', `/api/requirements/${created.id}/artifacts`, pushPayload)
console.log('演练副本：', created.id)

// 1.0 v4-9 判据（DESIGN-v4 §12 二期）：L 系行经重推不丢——评论升级 → 作答 → 重推机器行 → local 幸存
const lc = await call('POST', `/api/requirements/${created.id}/comments`, { author: 'dev', text: '演练：范围边界疑问' })
await call('POST', `/api/requirements/${created.id}/comments/${lc.id}/escalate`, {})
await call('PUT', `/api/requirements/${created.id}/answers/L01`, { answer: '演练：按口径 A' })
const repush = await call('POST', `/api/requirements/${created.id}/artifacts`, pushPayload)
const afterRepush = await call('GET', `/api/requirements/${created.id}/answers`)
const l01 = afterRepush.find(a => a.seq === 'L01')
check('v4-9：L01 经重推不丢（已答保留 + origin=local）', [l01.answer, l01.origin], ['演练：按口径 A', 'local'])
check('v4-9：机器行重建 origin=skill（两族区分）', afterRepush.find(a => a.seq === req.checklist[0].id).origin, 'skill')
check('v4-9：重推 carriedAnswers=机器行零答基线', repush.carriedAnswers, 0)

// 1.1 risk_tier 后到覆盖：digest.risk_tier=A 应覆盖建卡值 B（DESIGN-v2.2 D19）
const tier1 = await call('GET', `/api/requirements/${created.id}`)
check('risk_tier：digest 后到覆盖 B→A', tier1.card.riskTier, 'A')

// 2. 未答完时发布必须被拒（v2.2：文案口径 = 未处置）
let rejected = false
try {
  await call('POST', `/api/requirements/${created.id}/publish`, { standardMd: '# 不该成功' })
} catch (e) {
  rejected = e.message.includes('未处置')
}
check('未答完发布被拒', rejected, true)

// 3. 模拟 PM 补齐剩余 15 条（真实场景：PM 在页面输入，blur 自动 PUT）；
//    v2.2 处置通道（DESIGN-v2.2 D21）：末 3 条走驳回（空 answer）/挂起 ×2，验证混合处置后 counts 齐平
const detail = await call('GET', `/api/requirements/${created.id}`)
const pendingItems = detail.items.filter(i => !i.answer)
const answerItems = pendingItems.slice(0, -3)
const [rejItem, ...defItems] = pendingItems.slice(-3)
for (const it of answerItems) {
  await call('PUT', `/api/requirements/${created.id}/answers/${it.seq}`, { answer: `（演练答复）${it.question.slice(0, 18)}…` })
}
await call('PUT', `/api/requirements/${created.id}/answers/${rejItem.seq}`, { status: 'rejected', note: '演练：原文已明确，不是缺口' })
for (const it of defItems) {
  await call('PUT', `/api/requirements/${created.id}/answers/${it.seq}`, { status: 'deferred', note: '演练：下期处理' })
}
const after = await call('GET', `/api/requirements/${created.id}`)
check('混合处置（答+驳+挂）后 answered=32（含 L01）', after.answered, 32)
check('驳回落库 status/note', [after.items.find(i => i.seq === rejItem.seq).status, after.items.find(i => i.seq === rejItem.seq).answer], ['rejected', ''])

// 4. 拉回答复（sdlc-sync pull 的 API 侧）+ 发布（本地重算 digest 后合成标准文档推回）
const answers = await call('GET', `/api/requirements/${created.id}/answers`)
check('拉回答复：条数（30 P + L01）', answers.length, 32)
check('拉回答复：输出含处置状态', [answers.find(a => a.seq === rejItem.seq).status, answers.find(a => a.seq === defItems[0].seq).status], ['rejected', 'deferred'])
const answeredList = answers.filter(a => a.answer)
check('拉回答复：已答条数（含 L01）', answeredList.length, 29)
const md = [
  '# 标准需求文档 · ' + req.name + '（演练）', '',
  '## 澄清结论', '',
  '| 编号 | 级别 | 问题 | 结论 |', '| --- | --- | --- | --- |',
  ...answers.map(a => `| ${a.seq} | ${a.level} | ${a.question.slice(0, 30)} | ${a.answer} |`)
].join('\n')
const pub = await call('POST', `/api/requirements/${created.id}/publish`, { standardMd: md })
check('发布成功', pub.ok, true)

// 5. 回读校验
const done = await call('GET', `/api/requirements/${created.id}`)
check('回读：状态=published', done.card.status, 'published')
check('回读：标准文档已存', (done.standard?.markdown || '').includes('## 澄清结论'), true)

// 5.5 RTM 覆盖率（DESIGN-v2.2 D20）：发布后推用例（正常时序），验证 forward/backward/dangling 三口径
const frFirst = req.digest.fr[0].id
await call('POST', '/api/cases', {
  reqId: created.id,
  items: [
    { id: 'TC-COV-1', title: '覆盖率断言用例（挂首个需求点）', points: [frFirst], priority: 'P1', steps: 's', expect: 'e', owner: 'dev', tool: 'sdlc-test', status: 'reviewed' },
    { id: 'TC-COV-2', title: '覆盖率断言用例（故意不挂点）', points: [], priority: 'P2', steps: 's', expect: 'e', owner: 'dev', tool: 'sdlc-test', status: 'draft' }
  ]
})
const cov = await call('GET', `/api/requirements/${created.id}`)
check('coverage：正向 covered/total', [cov.coverage.forward.covered, cov.coverage.forward.total], [1, req.digest.fr.length])
check('coverage：正向未覆盖列表长度', cov.coverage.forward.uncovered.length, req.digest.fr.length - 1)
check('coverage：反向 linked/total', [cov.coverage.backward.linked, cov.coverage.backward.total], [1, 2])
check('coverage：未挂点用例入 dangling', cov.coverage.danglingCases, ['TC-COV-2'])

// 5.6 质量分（DESIGN-v2.2 D24，口径 D1=A）：夹具 6R/13Y/12B → 阻断 6>5 → ★（1星）
check('quality：夹具 6 阻断 → 1 星', [cov.quality.stars, cov.quality.basis], [1, '阻断 6 项'])

// 6. 发布后写入被锁
let locked = false
try {
  await call('PUT', `/api/requirements/${created.id}/answers/P07`, { answer: '发布后不应可改' })
} catch (e) {
  locked = e.message.includes('仅待澄清') /* v4-15 文案扩为「仅待澄清/变更中状态可作答」 */
}
check('发布后答复锁定', locked, true)

// 7. HTTP 语义（评测 P1-5 修复验收）：仅 POST 的路径发 GET 应 405，且文案不泄露异常类名
let err405 = ''
try { await call('GET', '/api/cases/form') } catch (e) { err405 = e.message }
check('方法不支持返回 405', err405.includes('-> 405:'), true)
check('405 文案不泄露异常类名', err405.includes('Exception'), false)

console.log(failures === 0 ? '\nM2 澄清闭环全链路通过 ✓' : `\n${failures} 项失败 ✗`)
process.exit(failures === 0 ? 0 : 1)
