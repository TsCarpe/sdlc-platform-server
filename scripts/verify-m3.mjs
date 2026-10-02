// M3（v1.1 新增）：用例域追溯数据断言 —— R1 用例↔需求点双向、R2 用例↔体检问题、
// R4 缺陷编号引用、旧形 payload 降级兼容。
// 用法：node scripts/verify-m3.mjs [baseUrl]   （默认 http://127.0.0.1:8080）
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

// ===== 1. 自足夹具：小需求 + 快照 + 双域 + 2 需求点 + 2 体检 + 2 澄清 =====
const { id } = await call('POST', '/api/requirements', {
  name: 'M3 追溯关联验证', sourceType: 'text', sourceText: 'M3 fixture', submittedBy: 'dev'
})
await call('POST', `/api/requirements/${id}/artifacts`, {
  snapshot: { md: '# M3 快照\n\n## 详述6.1 列表\n\n> 6.1.1 管理列表按创建时间倒序。\n\n## 详述8.6 颁奖\n\n> 8.6.2 至少一件已颁奖作品才可确认颁奖。' },
  domains: ['查询与展示口径', '评审与颁奖规则'],
  digest: {
    summary: 'M3 夹具', roles: ['区管'],
    features: [{ role: '区域管理员', m: '⭐ 管理列表', func: '列表', cov: '✅', src: '详述6.1' }],
    fr_points: [
      { id: '需求.FR-01', name: '列表排序', source: '详述6.1.1', group: '§6.1 管理列表', module: '管理列表' },
      { id: '需求.FR-02', name: '确认颁奖', source: '详述8.6.2', group: '§8.6 颁奖', module: '颁奖' }
    ],
    md: ''
  },
  audit: {
    red: 1, yellow: 0, blue: 1,
    items: [
      { id: '体检.P01', level: 'red', title: '排序口径冲突', where: '详述6.1.1', go: '⭕待澄清', group: '查询与展示口径', quote: '「管理列表按创建时间倒序」' },
      { id: '体检.P02', level: 'blue', title: '颁奖文案建议', where: '详述8.6.2', go: '跳过', group: '评审与颁奖规则', quote: '' }
    ],
    md: ''
  },
  items: [
    { seq: 'P01', level: 'red', theme: '查询与展示口径', question: '排序以哪个为准？', ctx: '详述6.1.1', answer: '', quote: '「管理列表按创建时间倒序」' },
    { seq: 'P02', level: 'blue', theme: '评审与颁奖规则', question: '文案是否调整？', ctx: '详述8.6.2', answer: '', quote: '' }
  ]
})
const detail = await call('GET', `/api/requirements/${id}`)
check('fixture：快照回读（md/at/version）',
  [(detail.snapshot?.md || '').includes('详述6.1'), !!detail.snapshot?.at, detail.snapshot?.version], [true, true, 1])
check('fixture：domains 定序回读', detail.domains, ['查询与展示口径', '评审与颁奖规则'])
const fr1 = detail.frPoints.find(f => f.id === '需求.FR-01')
check('fixture：FR 溯源字段回读', [fr1.source, fr1.group, fr1.module, fr1.coveredBy], ['详述6.1.1', '§6.1 管理列表', '管理列表', []])
const aP01 = detail.audit.content.items.find(i => i.id === '体检.P01')
const aP02 = detail.audit.content.items.find(i => i.id === '体检.P02')
check('fixture：体检 quote 可选', [aP01.quote !== '', aP02.quote === ''], [true, true])
check('fixture：relatedCases 初始为空', [aP01.relatedCases, aP02.relatedCases], [[], []])

// ===== 2. R1：用例↔需求点 双向 =====
await call('POST', '/api/cases', {
  reqId: id,
  items: [
    { id: 'TC-A', title: '排序用例', points: ['需求.FR-01'], priority: 'P0', steps: 's', expect: 'e', owner: 'dev', tool: 'sdlc-test', status: 'reviewed', verifyRefs: ['体检.P01', '需求.A1'], defectRefs: ['BUG-01（R1·待修复）'] },
    { id: 'TC-B', title: '颁奖+排序用例', points: ['需求.FR-01', '需求.FR-02'], priority: 'P1', steps: 's', expect: 'e', owner: 'dev', tool: 'sdlc-test', status: 'draft' }
  ]
})
const d2 = await call('GET', `/api/requirements/${id}`)
check('R1：FR-01 coveredBy=[TC-A,TC-B]', d2.frPoints.find(f => f.id === '需求.FR-01').coveredBy, ['TC-A', 'TC-B'])
check('R1：FR-02 coveredBy=[TC-B]', d2.frPoints.find(f => f.id === '需求.FR-02').coveredBy, ['TC-B'])

// ===== 3. R2：用例↔体检问题（平台反推，不双向冗余存储） =====
const aP01b = d2.audit.content.items.find(i => i.id === '体检.P01')
const aP02b = d2.audit.content.items.find(i => i.id === '体检.P02')
check('R2：体检 P01 relatedCases=[TC-A]', aP01b.relatedCases, ['TC-A'])
check('R2：体检 P02 relatedCases=[]（无引用不误挂）', aP02b.relatedCases, [])
check('R2：需求.A1 引用不进体检反推（A 的落点是 digest 假设）',
  d2.audit.content.items.some(i => (i.relatedCases || []).includes('TC-A') && i.id !== '体检.P01'), false)
const tcA = await call('GET', `/api/cases/${id}/TC-A`)
check('R2：TC-A verifyRefs 回读', tcA.verifyRefs, ['体检.P01', '需求.A1'])

// ===== 4. R4：缺陷编号引用（仅文本，不上链） =====
check('R4：TC-A defectRefs 回读', tcA.defectRefs, ['BUG-01（R1·待修复）'])
const list = await call('GET', `/api/cases?reqId=${id}`)
check('R4：列表行输出 verifyRefs/defectRefs 数组',
  [list.find(r => r.id === 'TC-A').verifyRefs, list.find(r => r.id === 'TC-B').defectRefs], [['体检.P01', '需求.A1'], []])

// ===== 5. 表单通道：verifyRefs/defectRefs 恒空 =====
await call('POST', '/api/cases/form', {
  reqId: id, title: '表单用例', points: ['需求.FR-02'], steps: 's', expect: 'e', owner: '张三'
})
const web1 = await call('GET', `/api/cases/${id}/WEB-001`)
check('表单用例：verifyRefs/defectRefs 默认空', [web1.verifyRefs, web1.defectRefs], [[], []])

// ===== 5b. 全局登记序号（评测 P0-4）：跨需求唯一递增、detail/list 返回 caseNo、编号歧义非阻塞警告 =====
check('caseNo：sync 用例有序号', typeof tcA.caseNo, 'number')
check('caseNo：表单用例序号 > sync 用例（全局递增）', web1.caseNo > tcA.caseNo, true)
const listNo = (await call('GET', `/api/cases?reqId=${id}`)).find(r => r.id === 'TC-A')?.caseNo
check('caseNo：列表行返回同一序号', listNo, tcA.caseNo)
const warn = await call('POST', '/api/cases', {
  reqId: id,
  items: [
    { id: 'TC-A', title: '排序用例', points: ['需求.FR-01'], steps: 's', expect: 'e', owner: 'dev', status: 'reviewed' },
    { id: 'TC-B', title: '颁奖+排序用例', points: ['需求.FR-01', '需求.FR-02'], steps: 's', expect: 'e', owner: 'dev', status: 'draft' },
    { id: 'TC-1', title: '前导零探针一', points: [], steps: 's', expect: 'e', owner: 'dev', status: 'draft' },
    { id: 'TC-01', title: '前导零探针二', points: [], steps: 's', expect: 'e', owner: 'dev', status: 'draft' }
  ]
})
check('idWarnings：TC-1 与 TC-01 歧义被提示', (warn.idWarnings || []).some(w => w.includes('TC-1') && w.includes('TC-01')), true)
check('idWarnings：批次仍正常应用', [warn.inserted, warn.updated], [2, 2])
// B6：skill 通道 priority 宽容降级（评测 P1-4：人严格机器宽容）
const soft = await call('POST', '/api/cases', {
  reqId: id,
  items: [
    { id: 'TC-A', title: '排序用例', points: ['需求.FR-01'], steps: 's', expect: 'e', owner: 'dev', status: 'reviewed' },
    { id: 'TC-B', title: '颁奖+排序用例', points: ['需求.FR-01', '需求.FR-02'], steps: 's', expect: 'e', owner: 'dev', status: 'draft', priority: 'P3' }
  ]
})
check('skill priority 非法宽容归一 P2', soft.normalizedPriority, 1)
check('skill 归一后落库 P2', (await call('GET', `/api/cases/${id}/TC-B`)).priority, 'P2')

// ===== 6. 降级：v1 旧形 payload（theme 有、新字段全无）→ 不报错、字段空值 =====
const { id: oldId } = await call('POST', '/api/requirements', {
  name: 'M3 旧数据降级验证', sourceType: 'text', sourceText: 'old'
})
await call('POST', `/api/requirements/${oldId}/artifacts`, {
  digest: { summary: '旧形', roles: [], features: [{ role: 'r', m: '⭐ 模块', func: 'f', cov: '✅' }], fr_points: [{ id: '需求.FR-01', name: '旧点' }], md: '' },
  audit: { red: 0, yellow: 1, blue: 0, items: [{ id: '体检.P01', level: 'yellow', title: '旧问题', where: '详述1', go: '' }], md: '' },
  items: [{ seq: 'P01', level: 'yellow', theme: '旧域', question: '旧问题？', ctx: '', answer: '' }]
})
const oldDetail = await call('GET', `/api/requirements/${oldId}`)
check('降级：无快照 → snapshot 缺省', oldDetail.snapshot === undefined, true)
check('降级：domains 回退推导（audit group 缺失 → theme 首现）', oldDetail.domains, ['旧域'])
check('降级：FR 空字段 + coveredBy 空',
  [oldDetail.frPoints[0].source, oldDetail.frPoints[0].group, oldDetail.frPoints[0].module, oldDetail.frPoints[0].coveredBy], ['', '', '', []])
check('降级：体检 quote/relatedCases 空',
  [oldDetail.audit.content.items[0].quote, oldDetail.audit.content.items[0].relatedCases], ['', []])
check('降级：功能地图 src 空不报错', oldDetail.digest.content.features[0].src, '')

// ===== 7. A1 全量替换补齐：重推子集 → 缺席的 skill 用例平台同步删除（含评论），表单用例保留 =====
await call('POST', `/api/cases/${id}/TC-B/comments`, { by: 'dev', text: '随用例删除的评论' })
const pushSubset = await call('POST', '/api/cases', {
  reqId: id,
  items: [
    { id: 'TC-A', title: '排序用例', points: ['需求.FR-01'], priority: 'P0', steps: 's', expect: 'e', owner: 'dev', tool: 'sdlc-test', status: 'reviewed', verifyRefs: ['体检.P01', '需求.A1'], defectRefs: ['BUG-01（R1·待修复）'] }
  ]
})
check('A1：重推子集 removedIds=[TC-B]（TC-1/TC-01 探针已被 5b/5c 轮清理）', pushSubset.removedIds, ['TC-B'])
const listAfter = await call('GET', `/api/cases?reqId=${id}`)
check('A1：TC-B 已删，TC-A 与表单用例保留', listAfter.map(r => r.id).sort(), ['TC-A', 'WEB-001'])
let tcBGone = false
try { await call('GET', `/api/cases/${id}/TC-B`) } catch (e) { tcBGone = e.status === 404 }
check('A1：TC-B 详情 404（用例与评论一并清理）', tcBGone, true)
const d4 = await call('GET', `/api/requirements/${id}`)
check('A1：coveredBy 幽灵用例消失、表单覆盖保留', d4.frPoints.find(f => f.id === '需求.FR-02').coveredBy, ['WEB-001'])

console.log(failures === 0 ? '\nM3 追溯关联全部通过 ✓' : `\n${failures} 项失败 ✗`)
process.exit(failures === 0 ? 0 : 1)
