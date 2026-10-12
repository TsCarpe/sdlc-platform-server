// v2.4 批次2 验收：设计 lane（DESIGN-v2.4 D34/D36）
// 覆盖：发布前 design_gen 409/未配仓 400/配仓后建任务/design 草稿回传/定稿 approve（version+留痕）/
//       修订 re-finalize version+1/C 级：design_gen 409 → 轻量跳过声明 → 互斥守卫/软删 404
// 用法：node scripts/verify-design.mjs [baseUrl]   （默认 http://127.0.0.1:8080）
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
async function publishFixture(name, tier) {
  const { id } = await call('POST', '/api/requirements', { name, sourceType: 'text', sourceText: '设计 lane 夹具', submittedBy: 'verify-design', risk_tier: tier })
  await call('POST', `/api/requirements/${id}/artifacts`, {
    digest: { summary: 's', risk_tier: tier, roles: [], features: [], fr_points: [{ id: '需求.FR-01', name: '点一' }] },
    audit: { red: 0, yellow: 1, blue: 0, items: [] },
    items: [{ seq: 'P01', level: 'yellow', theme: '设计域', question: '问题一' }]
  })
  await call('PUT', `/api/requirements/${id}/answers/P01`, { answer: '答' })
  await call('POST', `/api/requirements/${id}/publish`, { standardMd: '# 标准文档' })
  return id
}
const DESIGN_MD = '# 设计\n\n## §0 方案总览与备选\n\n决策一。\n\n## §1 数据模型落地\n\n无表变更。\n'
// 自建自领：排空历史排队任务，直到领到本夹具任务（对齐 verify-delete 口径；agent 已在脚本入口暂停）
const runTask = async (id, type) => {
  const t = await call('POST', `/api/requirements/${id}/tasks`, { type })
  for (let i = 0; i < 50; i++) {
    const r = await fetch(`${base}/api/tasks/next?runner=verify-design`, { method: 'POST' })
    if (r.status === 204) break
    const tk = await r.json()
    if (tk.id === t.id) return tk
    await call('POST', `/api/tasks/${tk.id}/done`, { result: '{"drainedBy":"verify-design"}' })
  }
  return t
}
// 回归期间暂停进程内 agent（防抢领），结束时恢复
try { await fetch(base + '/api/agent/pause', { method: 'POST' }) } catch {}

// ===== 1. 发布前守卫 =====
const pre = await call('POST', '/api/requirements', { name: '设计 lane 未发布卡', sourceType: 'text', sourceText: 'x', risk_tier: 'B' })
let notPub = null
try { await call('POST', `/api/requirements/${pre.id}/tasks`, { type: 'design_gen' }) } catch (e) { notPub = e.status }
check('未发布建 design_gen 409', notPub, 409)
const view0 = await call('GET', `/api/requirements/${pre.id}/design`)
check('初始 phase=none 且 activeTask/tier 正确', [view0.phase, view0.tier, view0.targetRepo], ['none', 'B', ''])
await call('DELETE', `/api/requirements/${pre.id}`)

// ===== 2. B 级主链：未配仓 400 → 配仓 → 生成 → 草稿 → 定稿 =====
const R = await publishFixture('设计 lane 主链验证卡', 'B')
let noRepo = null
try { await call('POST', `/api/requirements/${R}/tasks`, { type: 'design_gen' }) } catch (e) { noRepo = e.status }
check('B 级未配仓 design_gen 400', noRepo, 400)
let badKind = null
try { await call('PUT', `/api/requirements/${R}/target-repos`, { repos: [{ role: '前端', kind: 'svn', ref: '/tmp/x' }] }) } catch (e) { badKind = e.status }
check('非法 kind 400', badKind, 400)
await call('PUT', `/api/requirements/${R}/target-repos`, { repos: [] })
check('空 repos = 清空配置（键省略）', (await call('GET', `/api/requirements/${R}/design`)).repos ?? null, null)
await call('PUT', `/api/requirements/${R}/target-repos`, { repos: [
  { role: '前端', kind: 'local', ref: '/Users/hljy/IdeaProjects/sdlc-platform/frontend' },
  { role: '后端', kind: 'github', ref: 'https://github.com/org/sdlc-platform', branch: 'main' }
] })
const dvRepo = await call('GET', `/api/requirements/${R}/design`)
check('多仓配置（2 条 + kind/branch + 首仓镜像）',
  [dvRepo.repos.length, dvRepo.repos[0].role, dvRepo.repos[1].kind, dvRepo.repos[1].branch, dvRepo.targetRepo],
  [2, '前端', 'github', 'main', '/Users/hljy/IdeaProjects/sdlc-platform/frontend'])
const t1 = await runTask(R, 'design_gen')
check('配仓后 design_gen 已建并领取（running）', t1.status, 'running')
await call('PUT', `/api/requirements/${R}/drafts/design`, {
  md: DESIGN_MD, contentJson: JSON.stringify({ tierProposal: { from: 'B', to: 'A', reason: '勘察发现跨域表结构变更' } })
})
await call('POST', `/api/tasks/${t1.id}/done`, { result: '{"draft":"design"}' })
const view1 = await call('GET', `/api/requirements/${R}/design`)
check('草稿态 phase=draft', view1.phase, 'draft')
check('tierProposal 透传', view1.tierProposal?.to, 'A')
const ap1 = await call('POST', `/api/requirements/${R}/drafts/design/approve`, { by: '李工' })
check('定稿回执 finalizedVersion=1', ap1.finalizedVersion, 1)
const view2 = await call('GET', `/api/requirements/${R}/design`)
check('定稿态 phase=final', view2.phase, 'final')
check('定稿留痕 by/at/ver', [view2.finalized?.by, view2.finalized?.version], ['李工', 1])

// ===== 3. 修订闭环（design_rev → 草稿 → 再定稿 version+1）=====
const t2 = await runTask(R, 'design_rev')
check('design_rev 可建并领取（已有终稿）', t2.status, 'running')
await call('PUT', `/api/requirements/${R}/drafts/design`, { md: DESIGN_MD + '\n## §2 追加修订内容\n' })
await call('POST', `/api/tasks/${t2.id}/done`, { result: '{"rev":1}' })
const ap2 = await call('POST', `/api/requirements/${R}/drafts/design/approve`, { by: '王工' })
check('修订再定稿 finalizedVersion=2', ap2.finalizedVersion, 2)
const view3 = await call('GET', `/api/requirements/${R}/design`)
check('终稿 version=2 且定稿人更新', [view3.finalized?.version, view3.finalized?.by], [2, '王工'])

// ===== 4. C 级轻量路径 =====
const C = await publishFixture('设计 lane C 级验证卡', 'C')
let cGen = null
try { await call('POST', `/api/requirements/${C}/tasks`, { type: 'design_gen' }) } catch (e) { cGen = e.status }
check('C 级 design_gen 409（轻量路径）', cGen, 409)
let skipAB = null
try { await call('POST', `/api/requirements/${R}/design/skip`, { by: '李工' }) } catch (e) { skipAB = e.status }
check('B 级声明跳过 409', skipAB, 409)
const skipView = await call('POST', `/api/requirements/${C}/design/skip`, { by: '李工' })
check('C 级声明跳过 phase=skipped', skipView.phase, 'skipped')
check('跳过留痕 by', skipView.skipDeclared?.by, '李工')
let again = null
try { await call('POST', `/api/requirements/${C}/design/skip`, { by: '李工' }) } catch (e) { again = e.status }
check('重复声明 409', again, 409)
let afterSkip = null
try { await call('POST', `/api/requirements/${C}/tasks`, { type: 'design_gen' }) } catch (e) { afterSkip = e.status }
check('跳过后 design_gen 409（互斥）', afterSkip, 409)

// ===== 5. 六态推导输入：activeTask 记录最新任务 =====
check('activeTask=最新 design 任务', [view3.activeTask?.type, view3.activeTask?.status], ['design_rev', 'done'])

// ===== 6. 清理夹具 =====
await call('DELETE', `/api/requirements/${R}`)
await call('DELETE', `/api/requirements/${C}`)
try { await fetch(base + '/api/agent/resume', { method: 'POST' }) } catch {}

console.log(failures === 0 ? '\n设计 lane 验证全部通过 ✓' : `\n${failures} 项失败 ✗`)
process.exit(failures === 0 ? 0 : 1)
