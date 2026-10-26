// M1/M3 验收 + M4 eval 双用：把种子数据（真实需求「区域教学技能比赛」的平台化表示）
// 通过平台 REST API 推送，并自动校验回读结果。v1.1：含快照/需求域/溯源字段/双向索引断言。
// 用法：node scripts/sync-real.mjs [baseUrl]   （默认 http://127.0.0.1:8888）
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'
import { loadReal } from './real-data.load.mjs'

const base = process.argv[2] || 'http://127.0.0.1:8888'
const { req, cases, v11 } = await loadReal()

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
  if (!res.ok) throw new Error(`${method} ${path} -> ${res.status}: ${data.error || ''}`)
  return data
}

// 1. 建卡
const created = await call('POST', '/api/requirements', {
  name: req.name, iter: req.iter, sourceType: req.source.type,
  sourceUrl: req.source.url || '', sourceText: req.source.text || '', submittedBy: req.submittedBy
})
console.log('建卡：', created.id)

// 2. 推质检产物（v1.1：含原文快照 + 全局需求域 + 溯源字段）
const payload = {
  snapshot: { md: v11.snapshot.md },
  domains: v11.domains,
  digest: {
    summary: req.digest.summary,
    roles: req.digest.roles,
    features: req.digest.features || [],
    fr_points: req.digest.fr.map(f => ({ id: f.id, name: f.name, source: f.source, group: f.group, module: f.module })),
    md: ''
  },
  audit: {
    red: req.audit.red, yellow: req.audit.yellow, blue: req.audit.blue,
    items: req.audit.items, md: ''
  },
  items: req.checklist.map(c => ({
    seq: c.id, level: c.level, theme: c.theme, question: c.q, ctx: c.ctx, answer: c.answer || '', quote: c.quote || ''
  }))
}
const pushed = await call('POST', `/api/requirements/${created.id}/artifacts`, payload)
check('推送：澄清问题数', pushed.items, req.checklist.length)
check('推送：需求点数', pushed.frPoints, req.digest.fr.length)
check('推送：快照已刷新', pushed.snapshotUpdated, true)

// 3. 回读校验
const detail = await call('GET', `/api/requirements/${created.id}`)
check('回读：状态=clarifying', detail.card.status, 'clarifying')
check('回读：澄清问题总数', detail.totalQ, 31)
check('回读：已答复数', detail.answered, 16)
check('回读：需求点数', detail.frPoints.length, 96)
check('回读：体检明细条数', detail.audit.content.items.length, 31)
check('回读：体检分级 6/13/12',
  [detail.audit.content.red, detail.audit.content.yellow, detail.audit.content.blue], [6, 13, 12])
const p07 = detail.items.find(i => i.seq === 'P07')
check('回读：硬阻断 P07 未答复', p07.answer, '')
const p06 = detail.items.find(i => i.seq === 'P06')
check('回读：P06 答复带待复核标注', p06.answer.includes('待复核'), true)

// v1.1 回读：快照 / 需求域 / 溯源
check('回读：快照 md 非空', (detail.snapshot?.md || '').length > 0, true)
check('回读：快照时间戳非空', !!detail.snapshot?.at, true)
check('回读：快照 version', detail.snapshot?.version, 1)
check('回读：domains 7 域定序', detail.domains, v11.domains)
const fr1 = detail.frPoints[0]
const v11fr1 = v11.fr['需求.FR-01']
check('回读：FR-01 source/group/module', [fr1.source, fr1.group, fr1.module], [v11fr1.source, v11fr1.group, v11fr1.module])
const auditP01 = detail.audit.content.items.find(i => i.id === '体检.P01')
check('回读：体检 P01 group', auditP01.group, v11.audit['体检.P01'].group)
check('回读：体检 P01 摘引非空', (auditP01.quote || '').length > 0, true)
check('回读：体检摘引可选（无摘引条目为空串不报错）',
  detail.audit.content.items.filter(i => !i.quote).length > 0, true)
check('回读：澄清摘引条数', detail.items.filter(i => (i.quote || '').length > 0).length,
  req.checklist.filter(c => c.quote).length)

// 4. 推用例（M3 验收，v1.1：重点验证引用 + 缺陷编号引用）
const casePayload = {
  reqId: created.id,
  items: cases.map(c => ({
    id: c.id, title: c.title, points: c.points, priority: c.pri,
    pre: c.pre, steps: c.steps, expect: c.expect,
    owner: c.owner, tool: c.tool, status: c.status,
    verifyRefs: c.verifyRefs || [], defectRefs: c.defectRefs || []
  }))
}
const caseRes = await call('POST', '/api/cases', casePayload)
check('用例：插入数', caseRes.inserted, cases.length)

const caseList = await call('GET', `/api/cases?reqId=${created.id}`)
check('用例：回读总数', caseList.length, 42)
const tc17 = await call('GET', `/api/cases/${created.id}/TC-17`)
check('用例：TC-17 只读（sdlc-test 来源）', tc17.readonly, true)
check('用例：TC-17 关联需求点', tc17.points, ['需求.FR-27'])

// v1.1 R1/R2/R4 回读
const detail2 = await call('GET', `/api/requirements/${created.id}`)
const fr27 = detail2.frPoints.find(f => f.id === '需求.FR-27')
check('R1：FR-27 coveredBy 含 TC-17', (fr27?.coveredBy || []).includes('TC-17'), true)
const auditP06 = detail2.audit.content.items.find(i => i.id === '体检.P06')
check('R2：体检 P06 relatedCases 含 TC-33', (auditP06.relatedCases || []).includes('TC-33'), true)
const tc33 = await call('GET', `/api/cases/${created.id}/TC-33`)
check('R2：TC-33 verifyRefs 回读', tc33.verifyRefs, ['体检.P06'])
const tc12 = await call('GET', `/api/cases/${created.id}/TC-12`)
check('R2：TC-12 verifyRefs 回读（需求.A 引用）', tc12.verifyRefs, ['需求.A2'])
const tc05 = await call('GET', `/api/cases/${created.id}/TC-05`)
check('R4：TC-05 defectRefs 回读', tc05.defectRefs, ['BUG-01（R1·已修复）'])
check('R4：TC-17 defectRefs 为空数组', tc17.defectRefs, [])

// 5. 软校验数据源
const frs = await call('GET', `/api/fr-points?req_id=${created.id}`)
check('软校验：FR-96 存在', frs.points.includes('需求.FR-96'), true)

console.log(failures === 0 ? `\n全部通过 ✓  平台需求编号：${created.id}` : `\n${failures} 项失败 ✗`)
process.exit(failures === 0 ? 0 : 1)
