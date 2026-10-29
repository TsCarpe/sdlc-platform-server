// 导出 OpenAPI 契约到 docs/openapi.json（AUDIT D4-4 / 批次5：机器可读契约权威，
// 与 docs/ER图与业务流程.md §13 的人读权威互补）。自建临时实例抓取，不依赖运行中的服务。
// 契约面变更（端点/方法/出参模型）的批次验收含重跑本脚本并 commit 快照（防权威再漂移）。
// 用法：SDLC_DB_DSN=mysql://... node scripts/export-openapi.mjs
//   MySQL 化（EXEC-PLAN-K8s 阶段 2）：临时实例 = 同一 server 上的临时 schema
//   （scripts/mysql_admin.py 建删，用后即 DROP）。
import { spawnSync } from 'node:child_process'
import { spawn } from 'node:child_process'
import { setTimeout as sleep } from 'node:timers/promises'
import { writeFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const PORT = 19500 + Math.floor(Math.random() * 1000)
const BASE = `http://127.0.0.1:${PORT}`
const OUT = path.join(ROOT, 'docs', 'openapi.json')

const DSN = process.env.SDLC_DB_DSN || ''
if (!DSN) { console.error('需要 SDLC_DB_DSN（mysql://user:pass@host:3306/db）'); process.exit(1) }
const DB_NAME = `sdlc_export_${process.pid}`
const DB_DSN = DSN.replace(/\/[^/]*$/, `/${DB_NAME}`)
const admin = (cmd) => spawnSync('uv', ['run', 'python', 'scripts/mysql_admin.py', cmd, DB_NAME],
  { cwd: ROOT, env: process.env, encoding: 'utf8' })

let created = false
const adminOut = admin('create-db')
if (adminOut.status !== 0) { console.error('临时库创建失败：\n' + (adminOut.stderr || '')); process.exit(1) }
created = true

const child = spawn('uv', ['run', 'uvicorn', 'platform_sdlc.main:create_app', '--factory',
  '--host', '127.0.0.1', '--port', String(PORT)], {
  cwd: ROOT,
  env: { ...process.env, SDLC_DB_DSN: DB_DSN, SDLC_AGENT_ENABLED: 'false', SDLC_STATIC_DIR: '/tmp/sdlc-export-no-static' },
  stdio: ['ignore', 'pipe', 'pipe']
})
let bootLog = ''
child.stdout.on('data', d => { bootLog += d })
child.stderr.on('data', d => { bootLog += d })

try {
  let up = false
  for (let i = 0; i < 60; i++) {
    try { const r = await fetch(BASE + '/api/health'); if (r.ok) { up = true; break } } catch { /* 未就绪 */ }
    await sleep(500)
  }
  if (!up) { console.error('临时服务未就绪：\n' + bootLog.slice(-2000)); process.exit(1) }
  const spec = await (await fetch(BASE + '/openapi.json')).json()
  writeFileSync(OUT, JSON.stringify(spec, null, 1) + '\n', 'utf-8')
  const paths = Object.keys(spec.paths || {})
  console.log(`已导出 ${paths.length} 个路径到 docs/openapi.json（${OUT}）`)
} finally {
  child.kill('SIGTERM')
  await sleep(300)
  try { child.kill('SIGKILL') } catch { /* 已退出 */ }
  if (created) admin('drop-db')
}
