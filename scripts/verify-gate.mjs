// v2.4 批次3 验收：门禁域（DESIGN-v2.4 D35）
// 覆盖：GR-2 三条件守卫/开闸建轮+排 gate_review/重复开闸 409/gate_issues 入库机械校验（md×json 同源）/
//       裁决三处置必填守卫/裁决单元批量/GR-4 放行校验（未闭环 409 → 全绿放行+关卡互认）/
//       GR-3 变更检测与复审轮 r2（旧轮只读归档）
// 用法：node scripts/verify-gate.mjs [baseUrl]   （默认 http://127.0.0.1:8080）
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
const DESIGN_MD_V1 = '# 设计\n\n## §0 方案总览\n\n决策。\n\n## §1 数据模型\n\n无变更。\n'
const claimTask = async targetId => {
  for (let i = 0; i < 50; i++) {
    const r = await fetch(`${base}/api/tasks/next?runner=verify-gate`, { method: 'POST' })
    if (r.status === 204) break
    const tk = await r.json()
    if (tk.id === targetId) return tk
    await call('POST', `/api/tasks/${tk.id}/done`, { result: '{"drainedBy":"verify-gate"}' })
  }
  return null
}
const runTask = async (id, type) => {
  const t = await call('POST', `/api/requirements/${id}/tasks`, { type })
  return (await claimTask(t.id)) || t
}
try { await fetch(base + '/api/agent/pause', { method: 'POST' }) } catch {}

// ===== 1. 夹具：B 级发布 → 用例登记 → 设计定稿 =====
const { id: R } = await call('POST', '/api/requirements', { name: '门禁域验证卡', sourceType: 'text', sourceText: 'gate fixture', submittedBy: 'verify-gate', risk_tier: 'B' })
await call('POST', `/api/requirements/${R}/artifacts`, {
  digest: { summary: 's', risk_tier: 'B', roles: [], features: [], fr_points: [{ id: '需求.FR-01', name: '点一' }] },
  audit: { red: 0, yellow: 1, blue: 0, items: [] },
  items: [{ seq: 'P01', level: 'yellow', theme: '门禁域', question: '问题一' }]
})
await call('PUT', `/api/requirements/${R}/answers/P01`, { answer: '答' })
await call('POST', `/api/requirements/${R}/publish`, { standardMd: '# 标准文档' })
await call('POST', '/api/cases', {
  reqId: R,
  items: [
    { id: 'TC-G1', title: '覆盖用例', points: ['需求.FR-01'], priority: 'P0', steps: 's', expect: 'e', owner: 'sdlc-test', tool: 'sdlc-test', status: 'reviewed' },
    { id: 'TC-G2', title: '草稿用例', points: ['需求.FR-01'], priority: 'P1', steps: 's', expect: 'e', owner: 'sdlc-test', tool: 'sdlc-test', status: 'draft' }
  ]
})

// ===== 2. GR-2 开闸守卫 =====
let noDesign = null
try { await call('POST', `/api/requirements/${R}/gate/open`, { by: '李工' }) } catch (e) { noDesign = e.status }
check('设计未定稿开闸 409', noDesign, 409)
const gv0 = await call('GET', `/api/requirements/${R}/gate`)
check('初始 phase=blocked 且三条件就位', [gv0.phase, gv0.checks.length, gv0.checks[0].ok, gv0.checks[1].ok], ['blocked', 3, false, false])

await call('PUT', `/api/requirements/${R}/target-repos`, { repos: [{ role: '后端', kind: 'local', ref: '/tmp' }] })
const t1 = await runTask(R, 'design_gen')
await call('PUT', `/api/requirements/${R}/drafts/design`, { md: DESIGN_MD_V1 })
await call('POST', `/api/tasks/${t1.id}/done`, { result: '{"draft":"design"}' })
await call('POST', `/api/requirements/${R}/drafts/design/approve`, { by: '李工' })

let casesOpen = null
// 用例未全部定稿（TC-G2 draft）仍 409
try { await call('POST', `/api/requirements/${R}/gate/open`, { by: '李工' }) } catch (e) { casesOpen = e.status }
check('用例未全部定稿开闸 409', casesOpen, 409)
await call('POST', '/api/cases', { reqId: R, items: [{ id: 'TC-G2', title: '草稿用例', points: ['需求.FR-01'], priority: 'P1', steps: 's', expect: 'e', owner: 'sdlc-test', tool: 'sdlc-test', status: 'reviewed', expectedVersion: 1 }] })

const gv1 = await call('POST', `/api/requirements/${R}/gate/open`, { by: '李工' })
check('三条件全绿开闸 r1', [gv1.phase, gv1.round.roundNo, gv1.round.status], ['running', 1, '待裁决'])
let twice = null
try { await call('POST', `/api/requirements/${R}/gate/open`, { by: '李工' }) } catch (e) { twice = e.status }
check('进行中轮重复开闸 409', twice, 409)
check('开闸自动排 gate_review 任务', gv1.activeTask?.type, 'gate_review')

// ===== 3. gate_issues 入库（机械校验；领取开闸自动排的评审任务）=====
const t2 = await claimTask(gv1.activeTask.id)
const ISSUES = [
  { code: 'S-01', sev: 'high', theme: '数据模型', role: '数据审查', scene: '列表查询没建索引，数据量大后明显变慢。', quote: 'ALTER TABLE works ADD publish_at', loc: '§1', basis: 'hard', mergedUnit: 10 },
  { code: 'T-01', sev: 'mid', theme: '可测性', role: '可测性审查', scene: '定时任务没有时钟可控的验证入口。', quote: 'TC-09 无时间注入', loc: '用例 TC-09', basis: 'soft', mergedUnit: 11 },
  { code: 'D-01', sev: 'high', theme: '接口契约', role: '交叉审查者', scene: '用例期望 409 而设计写 200+业务码，联调必炸。', quote: '设计 §2 vs TC-07', loc: '§2 F03', basis: 'hard', div: true, mergedUnit: 12 }
]
const ISSUES_MD = '# issues（评审状态：待裁决）\n\n|编号|S-01|\n|T-01|\n|D-01|\n'
await call('PUT', `/api/requirements/${R}/drafts/gate_issues`, { md: ISSUES_MD, contentJson: JSON.stringify({ issues: ISSUES }) })
await call('POST', `/api/tasks/${t2.id}/done`, { result: '{"issues":3}' })
let badIngest = null
try {
  await call('PUT', `/api/requirements/${R}/drafts/gate_issues`, { md: '不含编号的 md', contentJson: JSON.stringify({ issues: ISSUES }) })
  await call('POST', `/api/requirements/${R}/gate/rounds/${gv1.round.id}/ingest`, { by: '李工' })
} catch (e) { badIngest = e.status }
check('md×json 不同源入库 400', badIngest, 400)
await call('PUT', `/api/requirements/${R}/drafts/gate_issues`, { md: ISSUES_MD, contentJson: JSON.stringify({ issues: ISSUES }) })   // 恢复同源草稿
const gv2 = await call('POST', `/api/requirements/${R}/gate/rounds/${gv1.round.id}/ingest`, { by: '李工' })
check('入库后 issues=3 且 phase=deciding', [gv2.issues.length, gv2.phase], [3, 'deciding'])
check('裁决单元聚合=3', gv2.units.length, 3)

// ===== 4. 裁决三处置守卫 =====
const issueOf = code => gv2.issues.find(i => i.code === code)
let rejNoNote = null
try { await call('POST', `/api/gate/issues/${issueOf('T-01').id}/verdict`, { verdict: '驳回' }) } catch (e) { rejNoNote = e.status }
check('驳回缺理由 400', rejNoNote, 400)
let fixNoLoc = null
try { await call('POST', `/api/gate/issues/${issueOf('S-01').id}/verdict`, { verdict: '落改' }) } catch (e) { fixNoLoc = e.status }
check('落改缺落点 400', fixNoLoc, 400)
let badV = null
try { await call('POST', `/api/gate/issues/${issueOf('T-01').id}/verdict`, { verdict: '改掉', note: 'x' }) } catch (e) { badV = e.status }
check('非法处置 400', badV, 400)

const gv3 = await call('POST', `/api/gate/issues/${issueOf('T-01').id}/verdict`, { verdict: '驳回', note: '回归成本可接受，沉淀误报模式', by: '李工' })
check('驳回落库', gv3.issues.find(i => i.code === 'T-01').verdict, '驳回')
await call('POST', `/api/gate/issues/${issueOf('S-01').id}/verdict`, { verdict: '落改', revLocation: '§1 idx_publish_at｜待执行', by: '李工' })
await call('POST', `/api/gate/issues/${issueOf('D-01').id}/verdict`, { verdict: '落改', revLocation: '§2 F03 改 409｜已执行', revStatus: '已执行', by: '李工' })

// ===== 5. GR-4 放行 =====
let notClosed = null
try { await call('POST', `/api/gate/rounds/${gv1.round.id}/release`, { by: '李工' }) } catch (e) { notClosed = e.status }
check('落改未闭环放行 409', notClosed, 409)
const gv4 = await call('POST', `/api/gate/issues/${issueOf('S-01').id}/revision`, { revLocation: '§1 idx_publish_at｜已执行', revStatus: '已执行' })
check('修订推进 已执行', gv4.issues.find(i => i.code === 'S-01').revStatus, '已执行')
const rel = await call('POST', `/api/gate/rounds/${gv1.round.id}/release`, { by: '李工' })
check('GR-4 全绿放行', [rel.ok, rel.roundNo, rel.casesConfirmed], [true, 1, 0])
const gv5 = await call('GET', `/api/requirements/${R}/gate`)
check('放行后 phase=released', gv5.phase, 'released')
check('关卡互认：draft 用例置 reviewed', (await call('GET', `/api/cases?reqId=${R}`)).every(c => c.status === 'reviewed'), true)

// ===== 6. GR-3 变更检测与复审轮 =====
let noChange = null
try { await call('POST', `/api/requirements/${R}/gate/reopen`, { by: '李工' }) } catch (e) { noChange = e.status }
check('无实质变更复审 409', noChange, 409)
const t3 = await runTask(R, 'design_rev')
await call('PUT', `/api/requirements/${R}/drafts/design`, { md: DESIGN_MD_V1 + '\n## §2 修订（F03 改 409 + idx 补齐）\n' })
await call('POST', `/api/tasks/${t3.id}/done`, { result: '{"rev":2}' })
await call('POST', `/api/requirements/${R}/drafts/design/approve`, { by: '王工' })
const ch = await call('GET', `/api/requirements/${R}/gate/change-status`)
check('GR-3 命中设计变更（released 窗口）', [ch.changed, ch.window], [true, 'released'])
const gv6 = await call('POST', `/api/requirements/${R}/gate/reopen`, { by: '李工' })
check('复审轮 r2 建立（旧轮归档）', [gv6.round.roundNo, gv6.round.status, gv6.rounds.length, gv6.rounds[0].status], [2, '待裁决', 2, '已放行'])

// ===== 7. 清理 =====
await call('DELETE', `/api/requirements/${R}`)
try { await fetch(base + '/api/agent/resume', { method: 'POST' }) } catch {}

console.log(failures === 0 ? '\n门禁域验证全部通过 ✓' : `\n${failures} 项失败 ✗`)
process.exit(failures === 0 ? 0 : 1)
