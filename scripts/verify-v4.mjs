// V4（需求平台权威化）：source_import 任务通道 + L2 建卡自动排 + 质检启动守卫 + 窗口 1 块级直接编辑
// + 评论系统 断言。
// 覆盖：建卡自动排（只一次）、自建自领自落文件全链（完成回调落库→doc.json/comments.json）、
// 分源封存守卫、块表空质检 failed 引导、块表就位后质检放行、
// 块级编辑（diff 预览四态/PUT 应用 blk-ID/幂等/published 与活跃质检双守卫，v4-7）、
// 评论（CRUD/线程级解决/升级 L 系带 quote·anchor·回链/幂等，v4-8）、
// 窗口 1 闭环（编辑→重质检→产物 anchor ∈ 新块集 + L 系幸存，v4-9/10）、
// 变更单数据面（draft CRUD 四态重算/影响面预览 CTE，v4-11）。
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

// ===== 6. 窗口 1 块级直接编辑（v4-7：diff 预览 + PUT 应用 + 幂等 + 双守卫） =====
// 第 2 段卡块（h1/p1/li1）：li1 挪最前 = move、p1 改文本 = edit、h1 不提交 = delete、空 id 新块 = insert
const editBody = {
  updatedBy: 'dev',
  blocks: [
    { id: 'v4-li1', type: 'list-item', marker: '1. ', depth: 1, text: '项一' },
    { id: 'v4-p1', type: 'paragraph', text: '原文（已修订）。' },
    { type: 'paragraph', text: '补充说明' }
  ]
}
const preview = await call('POST', `/api/requirements/${req}/blocks/diff`, editBody)
check('diff 预览：四态各一（LIS 判 move）', preview.counts, { edit: 1, insert: 1, delete: 1, move: 1 })
check('diff 预览：按提交序 + delete 尾置',
      preview.items.map(i => i.op), ['move', 'edit', 'insert', 'delete'])
check('diff 预览：edit 行文本对照',
      [preview.items.find(i => i.op === 'edit').oldText, preview.items.find(i => i.op === 'edit').newText],
      ['原文。', '原文（已修订）。'])
check('diff 预览：insert 行 blockId 为提交临时 id（空）',
      preview.items.find(i => i.op === 'insert').blockId, '')

const applied = await call('PUT', `/api/requirements/${req}/blocks`, editBody)
check('PUT 应用：counts 同预览', applied.counts, preview.counts)
const newId = applied.items.find(i => i.op === 'insert').blockId
check('insert 服务端发 blk-<ulid>（26 字符 Crockford）',
      [newId.startsWith('blk-'), newId.length, /^[0-9A-HJKMNP-TV-Z]{26}$/.test(newId.slice(4))],
      [true, 30, true])

const doc2 = await call('GET', `/api/requirements/${req}/doc.json`)
check('doc.json：编辑生效（h1 软删不渲染、新序、改文本、新块）',
      doc2.blocks.filter(b => b.type !== 'comment').map(b => [b.id, b.text]),
      [['v4-li1', '项一'], ['v4-p1', '原文（已修订）。'], [newId, '补充说明']])
check('doc.json：评论随宿主块（v4-p1 的 comment-refs 不受编辑影响）',
      doc2.blocks.find(b => b.id === 'v4-p1').comments, 'c1')

// 幂等：同数组重发（insert 块带正式 id）→ diff 为空、零写入
const again = await call('PUT', `/api/requirements/${req}/blocks`, {
  updatedBy: 'dev2',
  blocks: [
    { id: 'v4-li1', type: 'list-item', marker: '1. ', depth: 1, text: '项一' },
    { id: 'v4-p1', type: 'paragraph', text: '原文（已修订）。' },
    { id: newId, type: 'paragraph', text: '补充说明' }
  ]
})
check('幂等重发：items 为空', [again.items, again.counts], [[], { edit: 0, insert: 0, delete: 0, move: 0 }])

// 双守卫①：published 终态锁拒（发布链走通到 published）
const { id: reqP } = await call('POST', '/api/requirements', {
  name: 'V4 编辑终态锁验证', sourceType: 'text', sourceText: '# P', submittedBy: 'dev'
})
await call('POST', `/api/requirements/${reqP}/artifacts`, {
  digest: { md: '', summary: 's', roles: [], features: [], flows: [], states: [] },
  audit: { md: '', red: 0, yellow: 0, blue: 0, items: [] },
  items: [{ seq: 'P1', level: 'yellow', theme: 't', question: 'q' }]
})
await call('PUT', `/api/requirements/${reqP}/answers/P1`, { answer: 'a' })
await call('POST', `/api/requirements/${reqP}/publish`, { standardMd: '# P' })
await expectErr('PUT', `/api/requirements/${reqP}/blocks`,
                { updatedBy: 'dev', blocks: [{ type: 'paragraph', text: 'x' }] }, 409, 'published 终态锁拒编辑：409')

// 双守卫②：活跃质检互斥（queued quality_check → 409；cancel 解除 → 放行）
const qc = await call('POST', `/api/requirements/${req}/tasks`, { type: 'quality_check', note: '互斥验证' })
await expectErr('PUT', `/api/requirements/${req}/blocks`, {
  updatedBy: 'dev',
  blocks: [
    { id: 'v4-li1', type: 'list-item', marker: '1. ', depth: 1, text: '项一' },
    { id: 'v4-p1', type: 'paragraph', text: '原文（已修订）。' },
    { id: newId, type: 'paragraph', text: '补充说明' }
  ]
}, 409, '活跃质检互斥：409')
await call('POST', `/api/tasks/${qc.id}/cancel`)
const after = await call('PUT', `/api/requirements/${req}/blocks`, {
  updatedBy: 'dev',
  blocks: [
    { id: 'v4-li1', type: 'list-item', marker: '1. ', depth: 1, text: '项一' },
    { id: 'v4-p1', type: 'paragraph', text: '原文（已修订）。' },
    { id: newId, type: 'paragraph', text: '补充说明' }
  ]
})
check('质检解除后编辑放行（幂等空 items）', after.items, [])

// ===== 7. 评论系统（v4-8：CRUD + 线程级解决 + 升级 L 系） =====
const cm1 = await call('POST', `/api/requirements/${req}/comments`, {
  blockId: 'v4-p1', author: '张三', text: '这句口径有歧义'
})
check('块级评论：origin=platform 未解决', [cm1.blockId, cm1.origin, cm1.resolved], ['v4-p1', 'platform', false])
const cm2 = await call('POST', `/api/requirements/${req}/comments`, {
  blockId: 'v4-p1', author: '李四', text: '同意楼上的疑问', replyTo: cm1.id
})
const card1 = await call('POST', `/api/requirements/${req}/comments`, { author: '王五', text: '整体边界不清' })
check('卡片级评论（blockId 空）+ 回复链', [card1.blockId, cm2.replyTo], ['', cm1.id])
check('校验：replyTo 不存在 400',
      await call('POST', `/api/requirements/${req}/comments`, { text: 'x', replyTo: 9999 })
        .then(() => 0).catch(e => e.status), 400)

let list1 = await call('GET', `/api/requirements/${req}/comments`)
check('列表 + 未解决计数', [list1.comments.length, list1.unresolvedCount], [5, 5])  // 含导入的 2 条 feishu 评论

// 线程级解决：resolve 叶子 → 根+链全置，他线程与 feishu 行不动
await call('POST', `/api/requirements/${req}/comments/${cm2.id}/resolve`, { resolved: true })
const list2 = await call('GET', `/api/requirements/${req}/comments`)
const byId = Object.fromEntries(list2.comments.map(c => [c.id, c]))
check('线程级解决（叶子→整链）', [byId[cm1.id].resolved, byId[cm2.id].resolved, byId[card1.id].resolved],
      [true, true, false])
check('未解决计数随线程下降', list2.unresolvedCount, 3)

// 升级 L 系：quote=块文本 anchor=块 ID；已解决评论仍可升级；重复升级 409
const e1 = await call('POST', `/api/requirements/${req}/comments/${cm1.id}/escalate`, {})
check('升级：L01 + quote/anchor 带入 + origin=local + 回链',
      [e1.seq, e1.quote, e1.anchor, e1.origin, e1.srcCommentId],
      ['L01', '原文（已修订）。', 'v4-p1', 'local', cm1.id])
const cardE = await call('POST', `/api/requirements/${req}/comments/${card1.id}/escalate`, { level: 'blue' })
check('卡片级升级：quote/anchor 空 + level 可选', [cardE.seq, cardE.anchor, cardE.level], ['L02', '', 'blue'])
const answers = await call('GET', `/api/requirements/${req}/answers`)
check('answers 面：P/L 两族并存（L 行可见待处置）',
      answers.filter(a => a.seq.startsWith('L')).map(a => [a.seq, a.status]), [['L01', 'pending'], ['L02', 'pending']])
await expectErr('POST', `/api/requirements/${req}/comments/${cm1.id}/escalate`, {}, 409, '重复升级：409')
// 删除单条（回复行保留）
await call('DELETE', `/api/requirements/${req}/comments/${cm2.id}`)
const list3 = await call('GET', `/api/requirements/${req}/comments`)
check('删除单条', list3.comments.some(c => c.id === cm2.id), false)

// ===== 8. 窗口 1 闭环（v4-10）：编辑 → 重排 → 重质检 → 产物锚定新块集 =====
// 「重新质检」= 现有 quality_check 人工创建通道（无新端点）；LLM 分析环节真模型链路归 verify-agent，
// 此处黑盒走机械闭环：编辑后重排质检任务可行、领取不被启动守卫堵、产物 anchor 锚编辑后块 ID。
const rq = await call('POST', `/api/requirements/${req}/tasks`, { type: 'quality_check', note: '重新质检' })
check('编辑后重质检通道开放（人工创建即重新质检）', rq.status, 'queued')
// FIFO 队列可能先遇到 reqP 发布链 L2 自动排的任务（case_gen / 其遗留 source_import）——顺路 done 排空
let rqClaimed = null
for (let i = 0; i < 5; i++) {
  const t = await call('POST', '/api/tasks/next?runner=verify-v4')
  if (t.type === 'quality_check') { rqClaimed = t; break }
  await call('POST', `/api/tasks/${t.id}/done`, { result: '{"drained":"verify-v4 自动任务排空"}' })
}
check('重质检正常领取（块表就位不触启动守卫）', [rqClaimed.type, rqClaimed.status], ['quality_check', 'running'])
await call('POST', `/api/tasks/${rqClaimed.id}/done`, { result: '{"simulated":"verify-v4 闭环"}' })

// 重推产物：anchor 锚编辑后的新块（第 6 段 insert 的 blk-<ulid>）——闭环判据「digest anchor ∈ 新块集」
const doc3 = await call('GET', `/api/requirements/${req}/doc.json`)
const blockIds = new Set(doc3.blocks.map(b => b.id))
const pushed = await call('POST', `/api/requirements/${req}/artifacts`, {
  digest: { md: '', summary: '重质检后摘要', roles: [], features: [], flows: [], states: [] },
  audit: { md: '', red: 0, yellow: 0, blue: 0, items: [] },
  items: [{ seq: 'P1', level: 'yellow', theme: 't', question: '编辑后新问题', anchor: newId }],
})
check('重推产物成功（编辑后的卡照常走质检→澄清流）', pushed.ok, true)
const detailAfter = await call('GET', `/api/requirements/${req}`)
const p1 = detailAfter.items.find(i => i.seq === 'P1')
check('闭环判据：digest anchor ∈ 编辑后块集', [p1.anchor, blockIds.has(p1.anchor)], [newId, true])
check('闭环判据：L 系行经重推仍幸存（v4-9 合并保护）',
      detailAfter.items.filter(i => i.seq.startsWith('L')).length, 2)

// ===== 9. 变更单数据面（v4-11：draft CRUD + 影响面预览） =====
// published 语义下变更走变更单；此处用未发布卡验证 draft 数据面（draft 不碰块表、不限状态）。
const draft = await call('POST', `/api/requirements/${req}/changes`, {
  title: '口径修订', detail: '演练变更单数据面', proposedBy: '张三',
  blocks: [
    { id: 'v4-p1', type: 'paragraph', text: '原文（再次修订）。' },   // edit
    { type: 'paragraph', text: '变更新增段' },                        // insert：草稿期即有平台 ID
    { id: newId, type: 'paragraph', text: '补充说明' }                // 保留（第 6 段新块）
  ]  // v4-li1 不提交 = delete
})
check('draft 建：四态 items + counts',
      [draft.status, draft.counts, draft.items.filter(i => i.op === 'insert')[0].blockId.startsWith('blk-')],
      ['draft', { edit: 1, insert: 1, delete: 1, move: 0 }, true])
const draftInsertId = draft.items.filter(i => i.op === 'insert')[0].blockId
check('draft 不碰块表（块表现值原样）',
      (await call('GET', `/api/requirements/${req}/doc.json`)).blocks.filter(b => b.type !== 'comment').length,
      3)

const saved = await call('PUT', `/api/requirements/${req}/changes/${draft.id}`, {
  title: '口径修订 v2',
  blocks: [
    { id: 'v4-li1', type: 'list-item', marker: '1. ', depth: 1, text: '项一' },
    { id: 'v4-p1', type: 'paragraph', text: '原文（再次修订）。' },
    { id: draftInsertId, type: 'paragraph', text: '变更新增段' },
    { id: newId, type: 'paragraph', text: '补充说明' }
  ]
})
check('draft 反复保存：全量重算（相对块表基线：delete 回收，edit/insert 保持）',
      [saved.title, saved.counts], ['口径修订 v2', { edit: 1, insert: 1, delete: 0, move: 0 }])

// 影响面预览（保存后 items 已无 delete）：edit(v4-p1) 直接锚定；L01 锚 v4-p1 命中、P1 锚 newId 未动不进
const impact = await call('GET', `/api/requirements/${req}/changes/${draft.id}/impact`)
check('影响面预览：块集 = edit 直接 ∪ delete 子孙 CTE', impact.blocks, ['v4-p1'])
check('影响面预览：clarify 锚定 join（L01 命中，P1 锚未动块不进）',
      impact.clarifyItems.map(c => c.seq), ['L01'])
check('影响面预览：预览不冻结（无 impact 写入面）', impact.counts.clarify, 1)

check('draft 可直接删', (await call('DELETE', `/api/requirements/${req}/changes/${draft.id}`)).ok, true)
check('删除后 404', await call('GET', `/api/requirements/${req}/changes/${draft.id}`)
  .then(() => 0).catch(e => e.status), 404)

// 收尾清理：reqP 建卡自动排的 source_import 留在队列会干扰后续脚本，领走置 failed
const leftover = await fetch(`${base}/api/tasks/next?runner=verify-v4`, { method: 'POST' })
if (leftover.status === 200) {
  const lt = await leftover.json()
  await call('POST', `/api/tasks/${lt.id}/failed`, { error: 'verify-v4 收尾清理' })
}

// ===== 汇总 =====
if (failures > 0) { console.error(`\nV4 验证失败：${failures} 项`); process.exit(1) }
console.log('\nV4 导入通道验证全部通过 ✓')
