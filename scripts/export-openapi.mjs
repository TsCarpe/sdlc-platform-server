// 导出 OpenAPI 契约到 docs/openapi.json（AUDIT D4-4 / 批次5：机器可读契约权威，
// 与 docs/ER图与业务流程.md §13 的人读权威互补）。自建临时实例抓取，不依赖运行中的服务。
// 契约面变更（端点/方法/出参模型）的批次验收含重跑本脚本并 commit 快照（防权威再漂移）。
// 用法：node scripts/export-openapi.mjs
import { spawn } from 'node:child_process'
import { setTimeout as sleep } from 'node:timers/promises'
import { rmSync, writeFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const PORT = 19500 + Math.floor(Math.random() * 1000)
const BASE = `http://127.0.0.1:${PORT}`
const DB = `/tmp/sdlc-export-openapi-${process.pid}.db`
const OUT = path.join(ROOT, 'docs', 'openapi.json')

const child = spawn('uv', ['run', 'uvicorn', 'platform_sdlc.main:create_app', '--factory',
  '--host', '127.0.0.1', '--port', String(PORT)], {
  cwd: ROOT,
  env: { ...process.env, SDLC_DB_PATH: DB, SDLC_AGENT_ENABLED: 'false', SDLC_STATIC_DIR: '/tmp/sdlc-export-no-static' },
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
  for (const f of [DB, DB + '-wal', DB + '-shm']) { try { rmSync(f, { force: true }) } catch { /* 忽略 */ } }
}
