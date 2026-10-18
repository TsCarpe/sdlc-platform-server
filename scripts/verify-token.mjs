// 写令牌端到端冒烟（AUDIT D4-1 / D1-2 验收）：自建临时实例（独立库 + 随机端口 + SDLC_WRITE_TOKEN），
// 不依赖主服务配置。覆盖：无 token 写面 401（含领取端点）／GET 领取 405／带 token 建卡·建任务·领取·回报／
// 读面不受令牌限制／recover-stale 归属过滤端点可用。
// 用法：node scripts/verify-token.mjs
import { spawn } from 'node:child_process'
import { setTimeout as sleep } from 'node:timers/promises'
import { rmSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const TOKEN = 'verify-token-' + Date.now()
const PORT = 18000 + Math.floor(Math.random() * 2000)
const BASE = `http://127.0.0.1:${PORT}`
const DB = `/tmp/sdlc-verify-token-${process.pid}.db`

let failures = 0
const assert = (cond, label, extra = '') => {
  console.log(`${cond ? 'PASS' : 'FAIL'}  ${label}${extra ? ': ' + extra : ''}`)
  if (!cond) failures++
}

const child = spawn('uv', ['run', 'uvicorn', 'platform_sdlc.main:create_app', '--factory',
  '--host', '127.0.0.1', '--port', String(PORT)], {
  cwd: ROOT,
  env: { ...process.env, SDLC_DB_PATH: DB, SDLC_WRITE_TOKEN: TOKEN, SDLC_AGENT_ENABLED: 'false',
         SDLC_STATIC_DIR: '/tmp/sdlc-verify-token-no-static' },
  stdio: ['ignore', 'pipe', 'pipe']
})
let bootLog = ''
child.stdout.on('data', d => { bootLog += d })
child.stderr.on('data', d => { bootLog += d })

async function call(method, p, body, token) {
  const r = await fetch(BASE + p, {
    method,
    headers: { ...(body ? { 'Content-Type': 'application/json' } : {}), ...(token ? { 'X-Token': token } : {}) },
    body: body ? JSON.stringify(body) : undefined
  })
  const data = await r.json().catch(() => ({}))
  return { status: r.status, data }
}

try {
  let up = false
  for (let i = 0; i < 60; i++) {
    try { const r = await fetch(BASE + '/api/health'); if (r.ok) { up = true; break } } catch { /* 未就绪 */ }
    await sleep(500)
  }
  if (!up) { console.error('临时服务未就绪：\n' + bootLog.slice(-2000)); process.exit(1) }

  // 1) 无 token 写面 401（含领取端点——写语义，POST 形态纳入守卫）
  let r = await call('POST', '/api/requirements', { name: 'token冒烟', sourceType: 'text', sourceText: 'x' })
  assert(r.status === 401 && /^未授权/.test(r.data.error || ''), '无 token 建卡 401（统一 {error} 形状，AUDIT T-10）', `status=${r.status} body=${JSON.stringify(r.data)}`)
  r = await call('POST', '/api/tasks/next?runner=verify-token')
  assert(r.status === 401, '无 token 领取任务 401', `status=${r.status}`)

  // 2) GET 领取端点已被 POST 取代（平台标准 405 兜底）
  const g = await fetch(BASE + '/api/tasks/next?runner=verify-token')
  assert(g.status === 405, 'GET 领取端点 405（已改 POST）', `status=${g.status}`)

  // 3) 带 token 全链路：建卡 → 建任务 → 领取 → 回报
  r = await call('POST', '/api/requirements', { name: 'token冒烟', sourceType: 'text', sourceText: '内容' }, TOKEN)
  assert(r.status === 200 && r.data.id, '带 token 建卡成功', r.data.id || JSON.stringify(r.data))
  const reqId = r.data.id
  // v4-4：先消化建卡自动排的 source_import（领走即 failed，防其插队领取与落库噪音）；
  // quality_check 领取另有块表非空守卫——令牌冒烟用 case_gen
  const imp = await call('POST', '/api/tasks/next?runner=verify-token', undefined, TOKEN)
  if (imp.status === 200 && imp.data && imp.data.type === 'source_import') {
    await call('POST', `/api/tasks/${imp.data.id}/failed`, { error: 'verify-token 夹具跳过导入' }, TOKEN)
  }
  r = await call('POST', `/api/requirements/${reqId}/tasks`, { type: 'case_gen' }, TOKEN)
  assert(r.status === 200, '带 token 建任务成功', `status=${r.status}`)
  r = await call('POST', '/api/tasks/next?runner=verify-token', undefined, TOKEN)
  assert(r.status === 200 && r.data.status === 'running', '带 token 领取成功（running）', `status=${r.status}`)
  const taskId = r.data.id
  r = await call('POST', `/api/tasks/${taskId}/done`, { result: '{"ok":1}' }, TOKEN)
  assert(r.status === 200 && r.data.status === 'done', '带 token 回报 done', `status=${r.status}`)

  // 4) 读面不受令牌限制
  r = await call('GET', `/api/requirements/${reqId}`)
  assert(r.status === 200, '无 token 读详情 200（读面开放）', `status=${r.status}`)

  // 5) 崩溃恢复端点（按 runner 归属过滤；无超时任务应返回 0）
  r = await call('POST', '/api/tasks/recover-stale?runner=verify-token', undefined, TOKEN)
  assert(r.status === 200 && r.data.ok === true && r.data.expired === 0,
    'recover-stale 可用（本 runner 无超时任务）', JSON.stringify(r.data))
} finally {
  child.kill('SIGTERM')
  await sleep(300)
  try { child.kill('SIGKILL') } catch { /* 已退出 */ }
  for (const f of [DB, DB + '-wal', DB + '-shm']) { try { rmSync(f, { force: true }) } catch { /* 忽略 */ } }
}

console.log(failures === 0 ? '\n写令牌冒烟全部通过 ✅' : `\n${failures} 项失败 ❌`)
process.exit(failures === 0 ? 0 : 1)
