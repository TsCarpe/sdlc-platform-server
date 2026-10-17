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
], media: [], guard: { algo: 'markdown-fallback' } })   // c09d260 守恒终检后夹具必带（同 verify-v4/m5）
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

// 第 3 段 confirm 自动排的增量任务：先取消（人取消即终局；增量人工逃生门可重建）
for (const t of (await call('GET', `/api/tasks?req_id=${req}`)).filter(
    x => x.type === 'incremental_check' && x.status === 'queued')) {
  await call('POST', `/api/tasks/${t.id}/cancel`)
}
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

// ===== 4.6 增量链（v4-14）：confirm 自动排增量 → 落草稿（机械校验）→ 采纳合并 → 串行解除 =====
const incTasks = (await call('GET', `/api/tasks?req_id=${req}`)).filter(
    t => t.type === 'incremental_check' && t.status === 'queued')
check('confirm 自动排 incremental_check（on_change_confirmed 钩子生效；前序已人取消）',
      [incTasks.length, incTasks[0].createdBy], [1, 'auto:change-confirmed'])
let incClaimed = null
for (let i = 0; i < 5; i++) {  // FIFO 顺路排空他卡遗留任务
  const t = await call('POST', '/api/tasks/next?runner=verify-change')
  if (t.type === 'incremental_check') { incClaimed = t; break }
  await call('POST', `/api/tasks/${t.id}/done`, { result: '{"drained":"verify-change-inc"}' })
}
check('增量任务正常领取', [incClaimed.type, incClaimed.status], ['incremental_check', 'running'])
await call('POST', `/api/tasks/${incClaimed.id}/done`, { result: '{"sim":"verify-change"}' })

const bundle = JSON.stringify({
  changeId: c3.id, sections: ['ch-h1'],
  digestRows: {
    features: [{ role: '用户', m: '注册', func: '邮箱注册（增量黑盒）', anchor: 'ch-p1' }],
    flows: [], states: [],
    auditItems: [], frUpserts: [], frRemoves: []
  },
  invalidate: { digestAnchors: [], auditIds: [] },
  clarify: { items: [] }, summary: '增量后摘要', riskTier: 'B'
})
// 机械校验负例：sections 禁 seq 数值
const badBundle = JSON.stringify({ ...JSON.parse(bundle), sections: ['2000'] })
await expectErr('PUT', `/api/requirements/${req}/drafts/incremental_bundle`, { contentJson: badBundle },
                400, '机械校验：sections 禁 seq 数值 400')
const putDraft = await call('PUT', `/api/requirements/${req}/drafts/incremental_bundle`, { contentJson: bundle })
check('增量包落草稿（机械校验通过）', putDraft.type, 'incremental_bundle')
const merged = await call('POST', `/api/requirements/${req}/drafts/incremental_bundle/approve`, { by: '采纳人' })
check('采纳合并：区间整组替换统计', [merged.ok, merged.removedRows >= 1, merged.newRows >= 1], [true, true, true])
const detailM = await call('GET', `/api/requirements/${req}`)
check('合并后 digest 区间替换 + riskTier 覆写',
      [detailM.digest.content.features.map(f => f.func), detailM.card.riskTier],
      [['邮箱注册（增量黑盒）'], 'B'])
// 合并即推送：串行锁解除（digest.pushed_at > c3.confirmed_at）→ 第三单可 confirm
const c4 = await call('POST', `/api/requirements/${req}/changes`, {
  title: '第三单', proposedBy: '张三', blocks: [
    { id: 'ch-t1', type: 'title', text: '标题' },
    { id: 'ch-h1', type: 'heading', level: 2, text: '背景' },
    { id: 'ch-p1', type: 'paragraph', text: '一段说明（三修）' },
    { id: 'ch-tr1', type: 'table-row', head: ['列A'], cells: [['v1']] },
    { id: 'ch-trc1', type: 'paragraph', parent: 'ch-tr1', text: '行内块' },
    { id: 'ch-li1', type: 'list-item', marker: '1. ', depth: 1, text: '列表项一' },
    { id: 'ch-li2', type: 'list-item', marker: 'a. ', depth: 2, parent: 'ch-li1', text: '子项 a' }] })
// 秒级时间戳：c4 confirm 与 4.6 合并同秒会命中「已消化」边界（§5.1 严格小于口径）——隔秒确认
await new Promise(r => setTimeout(r, 1100))
const c4c = await call('POST', `/api/requirements/${req}/changes/${c4.id}/confirm`, { confirmedBy: '钱七' })
check('合并消化串行锁解除（第三单 confirm）', c4c.status, 'confirmed')

// ===== 4.7 changed 守卫矩阵（v4-15） =====
// c4 confirm 后卡处于 changed：全量通道关闭、许可面开放、发布双重守卫
await expectErr('POST', `/api/requirements/${req}/tasks`, { type: 'quality_check' }, 409,
                'changed 拒全量质检任务：409')
await expectErr('PUT', `/api/requirements/${req}/drafts/digest_bundle`, { contentJson: '{}' }, 409,
                'changed 拒全量质检草稿：409')
await expectErr('POST', `/api/requirements/${req}/artifacts`, {
  digest: { summary: '', roles: [], features: [], flows: [], states: [] },
  audit: { red: 0, yellow: 0, blue: 0, items: [] },
  items: [{ seq: 'P9', level: 'yellow', theme: 't', question: 'q' }]
}, 409, 'changed 拒全量重推（隐式推进吞态收口）：409')
await expectErr('PUT', `/api/requirements/${req}/blocks`, {
  updatedBy: 'x', blocks: [{ id: 'ch-t1', type: 'title', text: '标题' }]
}, 409, 'changed 关闭窗口 1 编辑：409')
// 许可面：standard 草稿可写 + 变更单 draft 可提（编辑入口）
const okDraft = await call('POST', `/api/requirements/${req}/changes`, {
  title: '矩阵期单', proposedBy: '张三',
  blocks: [{ id: 'ch-t1', type: 'title', text: '标题' },
    { id: 'ch-h1', type: 'heading', level: 2, text: '背景' },
    { id: 'ch-p1', type: 'paragraph', text: '一段说明（三修）' },
    { id: 'ch-tr1', type: 'table-row', head: ['列A'], cells: [['v1']] },
    { id: 'ch-trc1', type: 'paragraph', parent: 'ch-tr1', text: '行内块' },
    { id: 'ch-li1', type: 'list-item', marker: '1. ', depth: 1, text: '列表项一' },
    { id: 'ch-li2', type: 'list-item', marker: 'a. ', depth: 2, parent: 'ch-li1', text: '子项 a' }] })
check('changed 许可面：变更单 draft 可提（编辑入口）', okDraft.status, 'draft')
await call('DELETE', `/api/requirements/${req}/changes/${okDraft.id}`)
await call('PUT', `/api/requirements/${req}/drafts/standard`, { md: '# 重合成草稿' })
// 发布双重守卫：未消化拒（c4 影响面未消化）→ 消化后草稿不符拒 → 一致过 → published
await expectErr('POST', `/api/requirements/${req}/publish`, { standardMd: '# 重合成草稿' }, 409,
                '发布守卫①：影响面未消化 409')

// ===== 5. 未发布卡守卫（建卡走窗口 1 语义） =====
const { id: reqU } = await call('POST', '/api/requirements', {
  name: '未发布变更验证', sourceType: 'text', sourceText: '# U', submittedBy: 'dev' })
let impU = null
for (let i = 0; i < 5; i++) {  // FIFO 顺路排空发布链 L2 自动任务（case_gen 等）
  const t = await call('POST', '/api/tasks/next?runner=verify-change')
  if (t.type === 'source_import') { impU = t; break }
  await call('POST', `/api/tasks/${t.id}/done`, { result: '{"drained":"verify-change"}' })
}
dropDoc(impU.id, { blocks: [{ id: 'u-p1', type: 'paragraph', text: '基线' }], media: [], guard: { algo: 'markdown-fallback' } })
await call('POST', `/api/tasks/${impU.id}/done`, { result: 'u seed' })
const du = await call('POST', `/api/requirements/${reqU}/changes`, {
  title: '未发布单', proposedBy: '张三',
  blocks: [{ id: 'u-p1', type: 'paragraph', text: '基线（改）' }] })
await expectErr('POST', `/api/requirements/${reqU}/changes/${du.id}/confirm`, { confirmedBy: 'x' }, 409,
                '未发布卡 confirm 拒（走窗口 1 直接编辑）')

// ===== 6. GR-3 扩展（v4-16）：放行后变更单 → 横幅带单号（查变更单不比 rev） =====
const { id: reqG } = await call('POST', '/api/requirements', {
  name: 'GR-3 变更横幅验证', sourceType: 'text', sourceText: '# G', submittedBy: 'dev' })
let impG = null
for (let i = 0; i < 5; i++) {
  const t = await call('POST', '/api/tasks/next?runner=verify-change')
  if (t.type === 'source_import') { impG = t; break }
  await call('POST', `/api/tasks/${t.id}/done`, { result: '{"drained":"verify-change-gr3"}' })
}
dropDoc(impG.id, { blocks: [{ id: 'g-p1', type: 'paragraph', text: '基线' }], media: [], guard: { algo: 'markdown-fallback' } })
await call('POST', `/api/tasks/${impG.id}/done`, { result: 'g seed' })
await call('POST', `/api/requirements/${reqG}/artifacts`, {
  digest: { md: '', summary: 's', roles: [], flows: [], states: [], risk_tier: 'C', features: [
    { role: 'u', m: 'm', func: 'f', anchor: 'g-p1' }] },
  audit: { md: '', red: 0, yellow: 0, blue: 0, items: [] },
  items: [{ seq: 'P1', level: 'yellow', theme: 't', question: 'q', anchor: 'g-p1' }] })
await call('PUT', `/api/requirements/${reqG}/answers/P1`, { answer: 'a' })
await call('POST', `/api/requirements/${reqG}/publish`, { standardMd: '# G 定稿' })
// C 级轻量跳过 + 定稿用例 + 开闸放行（无 FR 视为覆盖）
await call('POST', `/api/requirements/${reqG}/design/skip`, { by: 'dev', reason: 'GR-3 黑盒跳过' })
await call('POST', '/api/cases', { reqId: reqG, items: [{ id: 'TC-GR3', title: 'GR-3 黑盒用例',
  points: [], priority: 'P2', steps: 's', expect: 'e', owner: 'sdlc-test', tool: 'sdlc-test',
  status: 'reviewed' }] })
const gOpen = await call('POST', `/api/requirements/${reqG}/gate/open`, {})
check('GR-3 前置：开闸成功', gOpen.rounds.length, 1)
const gRnd = gOpen.rounds[0].id
await call('POST', `/api/gate/rounds/${gRnd}/release`, { by: 'dev' })
await new Promise(r => setTimeout(r, 1100))  // release 与 confirm 隔秒（严格大于口径）
const gChange = await call('GET', `/api/requirements/${reqG}/changes`)
// 「无变更单横幅不亮」在有用例卡上不可达（gate.py 有意保守判定：casesMaxUpdated >= 基线即亮，
// 宁可多提醒不漏判）——与 verify-gate 第 6 节同口径，断言保守语义而非 false
const gView0 = await call('GET', `/api/requirements/${reqG}/gate`)
check('放行后横幅=保守判定（有用例即亮，无变更单号键）',
      [gView0.change.changed, gView0.change.reqChangeIds ?? null], [true, null])
const gDraft = await call('POST', `/api/requirements/${reqG}/changes`, {
  title: 'GR-3 验证单', proposedBy: '张三',
  blocks: [{ id: 'g-p1', type: 'paragraph', text: '基线（改）' }] })
await call('POST', `/api/requirements/${reqG}/changes/${gDraft.id}/confirm`, { confirmedBy: '李四' })
const gView = await call('GET', `/api/requirements/${reqG}/gate`)
check('GR-3 命中：changed=true 带变更单号（查变更单不比 rev）',
      [gView.change.changed, gView.change.reqChangeIds], [true, [gDraft.id]])
check('GR-3 横幅文案带单号', gView.change.reasons.some(x => x.includes(`变更单 #${gDraft.id}`)), true)

// ===== 汇总 =====
if (failures > 0) { console.error(`\n变更单链路验证失败：${failures} 项`); process.exit(1) }
console.log('\n变更单链路验证全部通过 ✓')
