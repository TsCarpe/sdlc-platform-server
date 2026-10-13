// V4 三期（需求平台权威化·变更）：变更单全链路黑盒（v4-12 起）。
// 覆盖：draft 建/存（四态 items + blk- 草稿期平台 ID）、confirm 事务（changed 表外通道 /
// content_rev+1 / 块表应用 / 澄清重开 / impact 冻结）、守卫（未发布拒、串行 confirm、重复 confirm）。
// 乐观锁与原子性由 pytest 覆盖（需 db 直改模拟并发漂移，黑盒不构造）。
// 用法：node scripts/verify-change.mjs [baseUrl]   （默认 http://127.0.0.1:8080；服务从仓根启动）
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

// ===== 0. 启动排空 + 建卡（导入通道落块基线） =====
for (let i = 0; i < 50; i++) {
  const t = await fetch(`${base}/api/tasks/next?runner=verify-change`, { method: 'POST' }).then(r => r.status === 200 ? r.json() : null)
  if (!t) break
  await call('POST', `/api/tasks/${t.id}/done`, { result: '{"drainedBy":"verify-change"}' })
}
const { id: req } = await call('POST', '/api/requirements', {
  name: '变更单链路验证', sourceType: 'text', sourceText: '# 变更', submittedBy: 'dev'
})
const imp = await call('POST', '/api/tasks/next?runner=verify-change')
dropDoc(imp.id, { blocks: [
  { id: 'ch-t1', type: 'title', text: '标题' },
  { id: 'ch-h1', type: 'heading', level: 2, text: '背景' },
  { id: 'ch-p1', type: 'paragraph', text: '一段说明' },
  { id: 'ch-tr1', type: 'table-row', head: ['列A'], cells: [['v1']] },
  { id: 'ch-trc1', type: 'paragraph', parent: 'ch-tr1', text: '行内块' },
  { id: 'ch-li1', type: 'list-item', marker: '1. ', depth: 1, parent: null, text: '列表项一' },
  { id: 'ch-li2', type: 'list-item', marker: 'a. ', depth: 2, parent: 'ch-li1', text: '子项 a' }
], media: [] })
await call('POST', `/api/tasks/${imp.id}/done`, { result: 'change seed' })

// ===== 1. 发布链（澄清带 anchor）→ published =====
await call('POST', `/api/requirements/${req}/artifacts`, {
  digest: { md: '', summary: 's', roles: [], flows: [], states: [], features: [
    { role: '用户', m: '注册', func: '手机号注册', anchor: 'ch-p1' }] },
  audit: { md: '', red: 0, yellow: 0, blue: 0, items: [] },
  items: [
    { seq: 'P1', level: 'yellow', theme: 't', question: 'q1', anchor: 'ch-p1' },
    { seq: 'P2', level: 'yellow', theme: 't', question: 'q2', anchor: 'ch-li2' },
    { seq: 'P3', level: 'yellow', theme: 't', question: 'q3', anchor: 'ch-trc1' }]
})
for (const s of ['P1', 'P2', 'P3']) {
  await call('PUT', `/api/requirements/${req}/answers/${s}`, { answer: `答 ${s}` })
}
await call('POST', `/api/requirements/${req}/publish`, { standardMd: '# 定稿' })
check('发布到位', (await call('GET', `/api/requirements/${req}`)).card.status, 'published')
// 秒级时间戳：digest.pushed_at 与 confirm 同秒会命中「已消化」边界（§5.1 严格小于口径）——隔秒再确认
await new Promise(r => setTimeout(r, 1100))

// ===== 2. 未发布守卫旁路（此处已 published，改为验证 changed 表外通道拒绝通用 set_status） =====
await expectErr('POST', `/api/requirements/${req}/status`, { status: 'changed' }, 409, 'changed 不进 REQ_FLOW（通用流转拒）')

// ===== 3. draft → confirm 事务 =====
const draftBlocks = [
  { id: 'ch-t1', type: 'title', text: '标题' },
  { id: 'ch-h1', type: 'heading', level: 2, text: '背景' },
  { id: 'ch-p1', type: 'paragraph', text: '一段说明（修订）' },
  { type: 'paragraph', text: '变更新增段' },
  { id: 'ch-li1', type: 'list-item', marker: '1. ', depth: 1, text: '列表项一' },
  { id: 'ch-li2', type: 'list-item', marker: 'a. ', depth: 2, parent: 'ch-li1', text: '子项 a' }
]  // ch-tr1/ch-trc1 不提交 = delete
const draft = await call('POST', `/api/requirements/${req}/changes`, {
  title: '口径修订', proposedBy: '张三', blocks: draftBlocks })
check('draft 建：四态 items', draft.counts, { edit: 1, insert: 1, delete: 2, move: 0 })
check('draft insert 草稿期即有平台 ID', draft.items.find(i => i.op === 'insert').blockId.startsWith('blk-'), true)
const draftInsert = draft.items.find(i => i.op === 'insert').blockId

const docBeforeConfirm = await call('GET', `/api/requirements/${req}/doc.json`)  // confirm 前基线（放弃后应回此状）
const confirmed = await call('POST', `/api/requirements/${req}/changes/${draft.id}/confirm`, { confirmedBy: '李四' })
check('confirm：状态 + contentRev', [confirmed.status, confirmed.contentRev], ['confirmed', 1])
const card = (await call('GET', `/api/requirements/${req}`)).card
check('changed（表外通道）+ contentRev 出参', [card.status, card.contentRev], ['changed', 1])
check('impact 冻结（含四类清单与 counts）',
      [confirmed.impact.counts.blocks, confirmed.impact.counts.clarify, Array.isArray(confirmed.impact.sections)],
      [3, 2, true])
const doc = await call('GET', `/api/requirements/${req}/doc.json`)
check('块表应用：edit 生效 / delete 软删不渲染 / insert 落 blk-',
      doc.blocks.map(b => [b.id, b.text]).filter(([id]) => !id.startsWith('blk-')),
      [['ch-t1', '标题'], ['ch-h1', '背景'], ['ch-p1', '一段说明（修订）'], ['ch-li1', '列表项一'], ['ch-li2', '子项 a']])
check('块表应用：insert 新块在序', doc.blocks.some(b => b.id === draftInsert && b.text === '变更新增段'), true)

const answers = await call('GET', `/api/requirements/${req}/answers`)
const bySeq = Object.fromEntries(answers.map(a => [a.seq, a]))
check('澄清重开：受影响行（P1/P3）回 pending + note 前缀',
      [bySeq.P1.status, bySeq.P1.answer, bySeq.P1.note.includes(`因变更单#${draft.id} 重开，原答复：答 P1`),
       bySeq.P3.status],
      ['pending', '', true, 'pending'])
check('澄清重开：未受影响行（P2）原样', [bySeq.P2.status, bySeq.P2.answer], ['answered', '答 P2'])
check('重开行 origin 出参（机器 skill 与 v4-9 两族）', bySeq.P1.origin, 'skill')

await expectErr('POST', `/api/requirements/${req}/changes/${draft.id}/confirm`, { confirmedBy: 'x' }, 409, '重复 confirm：409')

// ===== 4. 串行守卫：前序 confirmed 未消化拒第二单 =====
const c2 = await call('POST', `/api/requirements/${req}/changes`, {
  title: '第二单', proposedBy: '张三', blocks: [
    { id: 'ch-t1', type: 'title', text: '标题' },
    { id: 'ch-h1', type: 'heading', level: 2, text: '背景' },
    { id: 'ch-p1', type: 'paragraph', text: '一段说明（修订二）' },
    { id: draftInsert, type: 'paragraph', text: '变更新增段' },
    { id: 'ch-li1', type: 'list-item', marker: '1. ', depth: 1, text: '列表项一' },
    { id: 'ch-li2', type: 'list-item', marker: 'a. ', depth: 2, parent: 'ch-li1', text: '子项 a' }] })
await expectErr('POST', `/api/requirements/${req}/changes/${c2.id}/confirm`, { confirmedBy: '王五' }, 409,
                '串行 confirm：前序未消化 409')

// ===== 4.5 放弃变更（§5.6 全量逆向回滚） =====
const aborted = await call('POST', `/api/requirements/${req}/changes/${draft.id}/abort`, { abortedBy: '王五' })
check('放弃：aborted + 留痕', [aborted.status, aborted.abortedBy], ['aborted', '王五'])
const cardA = (await call('GET', `/api/requirements/${req}`)).card
check('放弃：changed→published（表外）+ content_rev 不回退', [cardA.status, cardA.contentRev], ['published', 1])
const docAfter = await call('GET', `/api/requirements/${req}/doc.json`)
check('放弃：块表逐逆向回原状（doc.json 与 confirm 前一致）',
      docAfter.blocks.map(b => [b.id, b.text]), docBeforeConfirm.blocks.map(b => [b.id, b.text]))
const ansA = await call('GET', `/api/requirements/${req}/answers`)
const bySeqA = Object.fromEntries(ansA.map(a => [a.seq, a]))
check('放弃：澄清恢复原答复（pending-only 恢复面）',
      [bySeqA.P1.answer, bySeqA.P1.status, bySeqA.P2.answer],
      ['答 P1', 'answered', '答 P2'])
await expectErr('POST', `/api/requirements/${req}/changes/${draft.id}/abort`, { abortedBy: 'x' }, 409,
                'aborted 是终态：再弃 409')
await expectErr('POST', `/api/requirements/${req}/changes/${draft.id}/confirm`, { confirmedBy: 'x' }, 409,
                'aborted 不可 confirm：409')

// 弃后通道恢复：新 draft → confirm 再次可行（串行锁随 aborted 单解除）
const c3 = await call('POST', `/api/requirements/${req}/changes`, {
  title: '弃后再变', proposedBy: '张三', blocks: [
    { id: 'ch-t1', type: 'title', text: '标题' },
    { id: 'ch-h1', type: 'heading', level: 2, text: '背景' },
    { id: 'ch-p1', type: 'paragraph', text: '一段说明（再修订）' },
    { id: 'ch-tr1', type: 'table-row', head: ['列A'], cells: [['v1']] },
    { id: 'ch-trc1', type: 'paragraph', parent: 'ch-tr1', text: '行内块' },
    { id: 'ch-li1', type: 'list-item', marker: '1. ', depth: 1, text: '列表项一' },
    { id: 'ch-li2', type: 'list-item', marker: 'a. ', depth: 2, parent: 'ch-li1', text: '子项 a' }] })
const c3c = await call('POST', `/api/requirements/${req}/changes/${c3.id}/confirm`, { confirmedBy: '赵六' })
check('弃后再变更可行（串行锁解除 + rev 继续）', [c3c.status, c3c.contentRev], ['confirmed', 2])
await call('DELETE', `/api/requirements/${req}/changes/${c2.id}`)  // draft 可直接删（收尾清理）

// ===== 5. 未发布卡守卫（建卡走窗口 1 语义） =====
const { id: reqU } = await call('POST', '/api/requirements', {
  name: '未发布变更验证', sourceType: 'text', sourceText: '# U', submittedBy: 'dev' })
let impU = null
for (let i = 0; i < 5; i++) {  // FIFO 顺路排空发布链 L2 自动任务（case_gen 等）
  const t = await call('POST', '/api/tasks/next?runner=verify-change')
  if (t.type === 'source_import') { impU = t; break }
  await call('POST', `/api/tasks/${t.id}/done`, { result: '{"drained":"verify-change"}' })
}
dropDoc(impU.id, { blocks: [{ id: 'u-p1', type: 'paragraph', text: '基线' }], media: [] })
await call('POST', `/api/tasks/${impU.id}/done`, { result: 'u seed' })
const du = await call('POST', `/api/requirements/${reqU}/changes`, {
  title: '未发布单', proposedBy: '张三',
  blocks: [{ id: 'u-p1', type: 'paragraph', text: '基线（改）' }] })
await expectErr('POST', `/api/requirements/${reqU}/changes/${du.id}/confirm`, { confirmedBy: 'x' }, 409,
                '未发布卡 confirm 拒（走窗口 1 直接编辑）')

// ===== 汇总 =====
if (failures > 0) { console.error(`\n变更单链路验证失败：${failures} 项`); process.exit(1) }
console.log('\n变更单链路验证全部通过 ✓')
