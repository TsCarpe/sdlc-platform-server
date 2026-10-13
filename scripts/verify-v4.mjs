// V4（需求平台权威化·一期）：source_import 任务通道 + L2 建卡自动排 + 质检启动守卫 断言。
// 覆盖：建卡自动排（只一次）、自建自领自落文件全链（完成回调落库→doc.json/comments.json）、
// 分源封存守卫、块表空质检 failed 引导、块表就位后质检放行。
// 用法：node scripts/verify-v4.mjs [baseUrl]   （默认 http://127.0.0.1:8080；服务从仓根启动）
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
const expectErr = async (method, path, body, status, name) => {
  await call(method, path, body)
    .then(() => check(name, 'no-error', status))
    .catch(e => check(name, e.status, status))
}

import { mkdirSync, writeFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
const REPO_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const dropDoc = (taskId, doc) => {
  const outDir = path.join(REPO_ROOT, 'tasks', String(taskId), 'out')
  mkdirSync(outDir, { recursive: true })
  writeFileSync(path.join(outDir, 'doc.json'), JSON.stringify(doc))
}

// ===== 启动清理：排空历史 queued（drain 领到的 source_import 无输出文件会 failed，属预期无断言：
// migration 补排卡等待其通道 runner 正常执行） =====
for (let i = 0; i < 50; i++) {
  const t = await fetch(`${base}/api/tasks/next?runner=verify-v4`, { method: 'POST' }).then(r => r.status === 200 ? r.json() : null)
  if (!t) break
  await call('POST', `/api/tasks/${t.id}/done`, { result: '{"drainedBy":"verify-v4"}' })
}

// ===== 1. 建卡自动排 source_import（只一次） =====
const { id: req } = await call('POST', '/api/requirements', {
  name: 'V4 导入通道验证', sourceType: 'text', sourceText: '# V4\n\n原文。\n\n- 项一\n', submittedBy: 'dev'
})
const tasks1 = await call('GET', `/api/tasks?req_id=${req}`)
const auto = tasks1.filter(t => t.type === 'source_import')
check('建卡自动排：恰好 1 个 source_import（queued + createdBy）',
      auto.map(t => [t.status, t.createdBy]), [['queued', 'auto:card-created']])
check('建卡自动排：不排质检', tasks1.filter(t => t.type === 'quality_check').length, 0)

// ===== 2. 自建自领自落文件全链：完成回调落库 → 块表权威导出 =====
const imp = await call('POST', '/api/tasks/next?runner=verify-v4')
check('领取导入任务', [imp.type, imp.reqId, imp.status], ['source_import', req, 'running'])
dropDoc(imp.id, {
  blocks: [
    { id: 'v4-h1', type: 'heading', level: 1, text: 'V4', path: 'V4' },
    { id: 'v4-p1', type: 'paragraph', text: '原文。', path: 'V4', comments: 'c1' },
    { id: 'v4-cm1', type: 'comment', ref: 'c1', anchorBlockId: 'v4-p1', quote: '原文。',
      thread: [{ user: '张三', text: '评论一' }, { user: '李四', text: '回复' }], reactions: null, path: '评论区' },
    { id: 'v4-li1', type: 'list-item', marker: '1. ', depth: 1, parent: null, text: '项一', path: 'V4' }
  ], media: []
})
const done = await call('POST', `/api/tasks/${imp.id}/done`, { result: 'agent 摘要' })
check('导入 done：result 为落库统计（agent 摘要被覆盖）',
      [done.status, JSON.parse(done.result)], ['done', { blocks: 3, comments: 2, missingMedia: 0 }])
const tasks2 = await call('GET', `/api/tasks?req_id=${req}`)
check('落库后仍只有 1 个导入任务（只自动一次）', tasks2.filter(t => t.type === 'source_import').length, 1)

const doc = await call('GET', `/api/requirements/${req}/doc.json`)
check('doc.json：块表权威（3 内容块 + comment 拼回）',
      [doc.blocks.length, doc.blocks.map(b => b.type).sort()],
      [4, ['comment', 'heading', 'list-item', 'paragraph']])
check('doc.json：宿主 comment-refs + comment 块 ref',
      [doc.blocks.find(b => b.id === 'v4-p1').comments, doc.blocks.find(b => b.type === 'comment').ref], ['c1', 'c1'])
const cjson = await call('GET', `/api/requirements/${req}/comments.json`)
check('comments.json：兼容格式（数组 + comment_id + 回复链）',
      [cjson.length, cjson[0].comment_id, cjson[1].reply_to !== null], [2, 'v4-cm1', true])

// ===== 3. 落库后全量重导 → 409 封存 =====
await expectErr('POST', `/api/requirements/${req}/tasks`, { type: 'source_import' }, 409, 'text 源落库后重导封存：409')

// ===== 4. 块表空卡：质检领取即 failed 引导（导入失败跳过的联动场景） =====
const { id: reqB } = await call('POST', '/api/requirements', {
  name: 'V4 质检前置验证', sourceType: 'text', sourceText: '# B\n\nB 原文。', submittedBy: 'dev'
})
const impB = await call('POST', '/api/tasks/next?runner=verify-v4')  // 领走 reqB 的自动导入（先进先出）
check('领取到导入任务（reqB）', [impB.type, impB.reqId], ['source_import', reqB])
await call('POST', `/api/tasks/${impB.id}/failed`, { error: '演练：导入失败跳过' })  // 块表保持空
await call('POST', `/api/requirements/${reqB}/tasks`, { type: 'quality_check', note: 'blocks empty' })
const nextQ1 = await fetch(`${base}/api/tasks/next?runner=verify-v4`, { method: 'POST' })
check('块表空质检：领取即守卫拦截（204 无任务返回）', nextQ1.status, 204)
const failedTasks = await call('GET', `/api/tasks?req_id=${reqB}&status=failed`)
const q = failedTasks.find(t => t.type === 'quality_check')
check('质检 failed：引导文案 + reason',
      [q !== undefined, JSON.parse(q.result).reason, JSON.parse(q.result).error.includes('先完成 source_import')],
      [true, 'blocks-empty', true])

// ===== 5. 块表就位后质检放行（retry → 正常领取 running） =====
const impB2 = await call('POST', `/api/requirements/${reqB}/tasks`, { type: 'source_import', note: '人工重试导入' })
check('导入失败后人工重试合法（封存只看块表痕迹）', impB2.status, 'queued')
const claimed = await call('POST', '/api/tasks/next?runner=verify-v4')
dropDoc(claimed.id, { blocks: [{ id: 'v4b-h1', type: 'heading', level: 1, text: 'B' }], media: [] })
await call('POST', `/api/tasks/${claimed.id}/done`, { result: 'b import' })
const retryQ = await call('POST', `/api/tasks/${q.id}/retry`)
check('质检 retry 重新排队', retryQ.status, 'queued')
const nextQ2 = await call('POST', '/api/tasks/next?runner=verify-v4')
check('块表就位：质检正常领取（running）', [nextQ2.type, nextQ2.status], ['quality_check', 'running'])
await call('POST', `/api/tasks/${nextQ2.id}/done`, { result: '{"ok":1}' })

// ===== 汇总 =====
if (failures > 0) { console.error(`\nV4 验证失败：${failures} 项`); process.exit(1) }
console.log('\nV4 导入通道验证全部通过 ✓')
