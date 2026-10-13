// 契约键序快照（AUDIT D4-4 / 批次5 B2 安全网）：自建临时实例 + 全生命周期造数后，
// 递归抓取核心端点响应的键序（数组取首元素），capture 出基线 / check 对比 diff。
// 键序 = 既有契约（schemas._WIRE 手工序），改造（pydantic response_model 等）前后 diff 必须为零。
// 注意：造数覆盖 detail 的全部条件键形态（快照/标准文档/质量分/doc/已放行门禁）——空库起服抓不到
// 条件键，diff=0 只证明它没覆盖到的东西（对抗审查 B2）。
// 用法：
//   node scripts/snapshot-keys.mjs capture > keys-baseline.json
//   node scripts/snapshot-keys.mjs check    keys-baseline.json
import { spawn } from 'node:child_process'
import { setTimeout as sleep } from 'node:timers/promises'
import { rmSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const PORT = 19000 + Math.floor(Math.random() * 1000)
const BASE = `http://127.0.0.1:${PORT}`
const DB = `/tmp/sdlc-snapshot-keys-${process.pid}.db`
const DESIGN_MD = '# 设计\n\n## §0 方案总览\n\n决策。\n\n## §1 数据模型\n\n无变更。\n'

const child = spawn('uv', ['run', 'uvicorn', 'platform_sdlc.main:create_app', '--factory',
  '--host', '127.0.0.1', '--port', String(PORT)], {
  cwd: ROOT,
  env: { ...process.env, SDLC_DB_PATH: DB, SDLC_AGENT_ENABLED: 'false', SDLC_STATIC_DIR: '/tmp/sdlc-snapshot-no-static' },
  stdio: ['ignore', 'pipe', 'pipe']
})
let bootLog = ''
child.stdout.on('data', d => { bootLog += d })
child.stderr.on('data', d => { bootLog += d })

async function call(method, p, body) {
  const res = await fetch(BASE + p, {
    method,
    headers: body ? { 'Content-Type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined
  })
  const data = await res.json().catch(() => ({}))
  if (!res.ok) throw new Error(`${method} ${p} -> ${res.status}: ${data.error || ''}`)
  return data
}
const claimTask = async targetId => {
  for (let i = 0; i < 50; i++) {
    const r = await fetch(`${BASE}/api/tasks/next?runner=snapshot-keys`, { method: 'POST' })
    if (r.status === 204) break
    const tk = await r.json()
    if (tk.id === targetId) return tk
    await call('POST', `/api/tasks/${tk.id}/done`, { result: '{"drainedBy":"snapshot-keys"}' })
  }
  return null
}

/** 递归键序形状：dict → 有序键 + 子形状；数组 → 首元素形状（空数组记 null）；标量 → 类型名 */
function shape(v) {
  if (Array.isArray(v)) return { '[]': v.length ? shape(v[0]) : null }
  if (v !== null && typeof v === 'object') {
    const out = {}
    for (const k of Object.keys(v)) out[k] = shape(v[k])
    return out
  }
  return typeof v
}

async function build() {
  // 卡 A：全生命周期（detail 条件键全出现 + gate released 形态）
  const { id: A } = await call('POST', '/api/requirements', { name: '快照基线-全链', sourceType: 'text', sourceText: 'fixture', submittedBy: 'snapshot', risk_tier: 'B' })
  await call('POST', `/api/requirements/${A}/artifacts`, {
    digest: { summary: 's', risk_tier: 'B', roles: ['用户'], features: [], flows: [], states: [], fr_points: [{ id: '需求.FR-01', name: '点一' }], md: '# digest md' },
    audit: { red: 0, yellow: 1, blue: 0, items: [{ id: 'P01', level: 'yellow', title: 't', where: 'w', go: '', group: '域一', quote: '「q」', anchor: '' }], md: '# audit md' },
    items: [{ seq: 'P01', level: 'yellow', theme: '域一', question: '问题一', answer: '已答' }],
    snapshot: { md: '# 原文快照\n\n段落。' },
    doc: { blocks: [{ id: 'blk-1', type: 'paragraph', text: '原文段落' }], media: [] },
    domains: ['域一']
  })
  await call('POST', `/api/requirements/${A}/publish`, { standardMd: '# 标准文档\n\n正文。' })
  await call('POST', '/api/cases', { reqId: A, items: [
    { id: 'TC-K1', title: '覆盖用例', points: ['需求.FR-01'], priority: 'P0', steps: 's', expect: 'e', owner: 'sdlc-test', tool: 'sdlc-test', status: 'reviewed' }] })
  await call('PUT', `/api/requirements/${A}/target-repos`, { repos: [{ role: '后端', kind: 'local', ref: '/tmp' }] })
  const td = await call('POST', `/api/requirements/${A}/tasks`, { type: 'design_gen' })
  await call('PUT', `/api/requirements/${A}/drafts/design`, { md: DESIGN_MD })
  await call('POST', `/api/tasks/${(await claimTask(td.id)).id}/done`, { result: '{"draft":"design"}' })
  await call('POST', `/api/requirements/${A}/drafts/design/approve`, { by: '李工' })
  const gv1 = await call('POST', `/api/requirements/${A}/gate/open`, { by: '李工' })
  const ISSUES = [{ code: 'S-01', sev: 'high', theme: '数据模型', role: '审查', scene: '场景', quote: 'q', loc: '§1', basis: 'hard', mergedUnit: 10 }]
  await call('PUT', `/api/requirements/${A}/drafts/gate_issues`, { md: '# issues（评审状态：待裁决）\n\n|编号|S-01|\n', contentJson: JSON.stringify({ issues: ISSUES }) })
  await call('POST', `/api/tasks/${(await claimTask(gv1.activeTask.id)).id}/done`, { result: '{"issues":1}' })
  const gv2 = await call('POST', `/api/requirements/${A}/gate/rounds/${gv1.round.id}/ingest`, { by: '李工' })
  await call('POST', `/api/gate/issues/${gv2.issues[0].id}/verdict`, { verdict: 'reject', note: '理由', by: '李工' })
  await call('POST', `/api/gate/rounds/${gv1.round.id}/release`, { by: '李工' })

  // 卡 B：submitted 初始形态（detail 无条件键的 NON_NULL 形态）
  const { id: B } = await call('POST', '/api/requirements', { name: '快照基线-初始', sourceType: 'text', sourceText: 'x', submittedBy: 'snapshot' })
  // 卡 C：带迭代（iterations/detail 形态）
  const { id: C } = await call('POST', '/api/requirements', { name: '快照基线-迭代', iter: 'SNAP-ITER', sourceType: 'text', sourceText: 'x', submittedBy: 'snapshot' })
  await call('PUT', `/api/requirements/${C}/drafts/standard`, { md: '# 草稿' })  // drafts 列表形态
  return { A, B, C }
}

const TARGETS = ({ A, B, C }) => [
  ['GET', '/api/requirements', 'requirements 列表'],
  ['GET', `/api/requirements/${A}`, 'detail-全链（条件键全出现）'],
  ['GET', `/api/requirements/${B}`, 'detail-初始（NON_NULL 缺键形态）'],
  ['GET', `/api/requirements/${A}/answers`, 'answers'],
  ['GET', `/api/fr-points?req_id=${A}`, 'fr-points'],
  ['GET', `/api/tasks?req_id=${A}`, 'tasks 列表'],
  ['GET', `/api/requirements/${A}/drafts`, 'drafts 列表'],
  ['GET', `/api/cases`, 'cases 列表'],
  ['GET', `/api/cases/${A}/TC-K1`, 'case detail'],
  ['GET', '/api/iterations', 'iterations 列表'],
  ['GET', '/api/iterations/detail?name=SNAP-ITER', 'iterations detail'],
  ['GET', `/api/requirements/${A}/design`, 'design view'],
  ['GET', `/api/requirements/${A}/gate`, 'gate view-released'],
  ['GET', `/api/requirements/${B}/gate`, 'gate view-none'],
  ['GET', '/api/agent/status', 'agent status'],
]

async function capture() {
  const { A, B, C } = await build()
  const out = {}
  for (const [m, p, label] of TARGETS({ A, B, C })) out[label] = shape(await call(m, p))
  return out
}

try {
  let up = false
  for (let i = 0; i < 60; i++) {
    try { const r = await fetch(BASE + '/api/health'); if (r.ok) { up = true; break } } catch { /* 未就绪 */ }
    await sleep(500)
  }
  if (!up) { console.error('临时服务未就绪：\n' + bootLog.slice(-2000)); process.exit(1) }

  const [mode, file] = process.argv.slice(2)
  if (mode === 'check') {
    const { readFileSync } = await import('node:fs')
    const baseline = JSON.parse(readFileSync(file, 'utf-8'))
    const current = await capture()
    const labels = [...new Set([...Object.keys(baseline), ...Object.keys(current)])]
    let diffs = 0
    for (const label of labels) {
      const a = JSON.stringify(baseline[label], null, 1)
      const b = JSON.stringify(current[label], null, 1)
      if (a !== b) {
        diffs++
        console.log(`DIFF  ${label}`)
        console.log(`  基线: ${a}`)
        console.log(`  现值: ${b}`)
      }
    }
    console.log(diffs === 0 ? `键序快照一致（${labels.length} 端点）✅` : `${diffs} 个端点键序漂移 ❌`)
    process.exit(diffs === 0 ? 0 : 1)
  } else {
    console.log(JSON.stringify(await capture(), null, 1))
  }
} finally {
  child.kill('SIGTERM')
  await sleep(300)
  try { child.kill('SIGKILL') } catch { /* 已退出 */ }
  for (const f of [DB, DB + '-wal', DB + '-shm']) { try { rmSync(f, { force: true }) } catch { /* 忽略 */ } }
}
