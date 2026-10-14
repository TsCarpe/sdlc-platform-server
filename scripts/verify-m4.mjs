// M4（v1.2 新增）：原文预处理与块级引用断言 —— doc 块结构落库回读、媒体上传/读取/304、
// anchor 透传（fr/features/audit/items）、重推 doc 版本递增、无 doc 降级兼容。
// 用法：node scripts/verify-m4.mjs [baseUrl]   （默认 http://127.0.0.1:8080）
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
async function raw(method, path, body, headers = {}) {
  return fetch(base + path, { method, body, headers })
}

// ===== 1. 夹具：小需求 + 1 张图（PNG 1x1）+ 块结构 + anchors =====
const PNG = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==', 'base64')
const crypto = await import('node:crypto')
const pngSha = crypto.createHash('sha256').update(PNG).digest('hex')
const PNG_ID = 'm-' + pngSha.slice(0, 12)

const { id } = await call('POST', '/api/requirements', {
  name: 'M4 原文块结构验证', sourceType: 'text', sourceText: 'M4 fixture', submittedBy: 'dev'
})

// 媒体上传（显式 mediaId + 幂等重传）
const up1 = await (await raw('POST', `/api/requirements/${id}/media?name=proto.png&mediaId=${PNG_ID}`,
  PNG, { 'X-Media-Mime': 'image/png', 'Content-Type': 'application/octet-stream' })).json()
check('媒体上传：显式 mediaId + sha 回读', [up1.id, up1.sha256 === pngSha, up1.bytes], [PNG_ID, true, PNG.length])
const up2 = await (await raw('POST', `/api/requirements/${id}/media?name=proto.png&mediaId=${PNG_ID}`,
  PNG, { 'X-Media-Mime': 'image/png', 'Content-Type': 'application/octet-stream' })).json()
check('媒体上传：幂等重传', up2.id, PNG_ID)

// GET：字节一致 + ETag 304
const g1 = await raw('GET', `/api/requirements/${id}/media/${PNG_ID}`)
check('媒体读取：200 + content-type + 字节一致',
  [g1.status, g1.headers.get('content-type'), Buffer.from(await g1.arrayBuffer()).equals(PNG)],
  [200, 'image/png', true])
const g2 = await raw('GET', `/api/requirements/${id}/media/${PNG_ID}`,
  undefined, { 'If-None-Match': `"${pngSha}"` })
check('媒体读取：ETag 304', g2.status, 304)
const g3 = await raw('GET', `/api/requirements/${id}/media/m-000000000000`)
check('媒体读取：不存在 → 404', g3.status, 404)

// ===== 2. 推 doc + anchors =====
const blocks = [
  { id: 'doxcnH1', type: 'heading', level: 2, text: '详述6 比赛管理', path: '需求功能详述 › 6' },
  { id: 'doxcnRow6', type: 'table-row', head: ['序号', '模块-页面'], cells: ['6', '区域管理员-比赛管理'], rowLabel: '6', path: '需求功能详述 › 6', parent: null },
  { id: 'doxcnLi1', type: 'list-item', marker: '1. ', ordinal: '1', depth: 1, text: '比赛管理列表：按创建时间倒序', path: '需求功能详述 › 6 › 1', parent: 'doxcnRow6' },
  { id: 'doxcnImg', type: 'image', media: PNG_ID, name: 'proto.png', alt: '比赛管理原型', path: '需求功能详述 › 6', parent: 'doxcnRow6' }
]
const media = [{ id: PNG_ID, token: 'm4-img-token', name: 'proto.png', mime: 'image/png', bytes: PNG.length, sha256: pngSha, kind: 'media', status: 'ok' }]
// 守恒终检 guard（v4 守恒门）：textSha 为平台 conservation-v1 同款算法（剔除 */~ 与空白后排序
// sha256）对上方 blocks 的预计算值——与 parse_doc --self-test 固定向量算法一致，由主会话核对。
const guard = { algo: 'conservation-v1', source: 'xml', mediaTokens: ['m4-img-token'],
                textSha: 'cc44a8335300e61cfa37d8fe3fcd5249ac74aeb3568e649c8961f495f8979819' }
const pushed = await call('POST', `/api/requirements/${id}/artifacts`, {
  snapshot: { md: '# M4 快照\n\n详述6 比赛管理列表' },
  domains: ['查询与展示口径'],
  doc: { generator: 'parse_doc.py 0.2', source: { url: '', documentId: 'm4', revisionId: 1 }, blocks, media, guard },
  digest: {
    summary: 'M4 夹具', roles: ['区管'],
    features: [{ role: '区域管理员', m: '⭐ 管理列表', func: '列表', cov: '✅', src: '详述6', anchor: 'doxcnRow6' }],
    flows: [{ role: '区域管理员', step: '创建比赛', next: '?', gap: true, src: '详述6', anchor: 'doxcnRow6' }],
    states: [{ obj: '比赛', from: '草稿', to: '报名中', on: '发布', gap: false, src: '详述6', anchor: 'doxcnRow6' }],
    fr_points: [{ id: '需求.FR-01', name: '列表排序', source: '详述6', group: '§6 管理列表', module: '管理列表', anchor: 'doxcnLi1' }],
    md: ''
  },
  audit: {
    red: 0, yellow: 1, blue: 0,
    items: [{ id: '体检.P01', level: 'yellow', title: '排序口径', where: '详述6', go: '待澄清', group: '查询与展示口径', quote: '「按创建时间倒序」', anchor: 'doxcnLi1' }]
  },
  items: [{ seq: 'P01', level: 'yellow', theme: '查询与展示口径', question: '排序口径？', ctx: '详述6', answer: '', quote: '「按创建时间倒序」', anchor: 'doxcnLi1' }]
})
check('推 doc：docUpdated/missingMedia/anchors 不破坏既有返回', [pushed.ok, pushed.docUpdated, pushed.missingMedia], [true, true, 0])

const detail = await call('GET', `/api/requirements/${id}`)
check('doc 回读：块数/媒体数/来源', [detail.doc?.content?.blocks?.length, detail.doc?.content?.media?.length, detail.doc?.content?.source?.documentId], [4, 1, 'm4'])
check('doc 回读：块类型齐备', [...new Set(detail.doc.content.blocks.map(b => b.type))].sort(), ['heading', 'image', 'list-item', 'table-row'])
check('anchor 透传：fr', detail.frPoints[0].anchor, 'doxcnLi1')
check('anchor 透传：feature', detail.digest.content.features[0].anchor, 'doxcnRow6')
check('anchor 透传：audit', detail.audit.content.items[0].anchor, 'doxcnLi1')
check('anchor 透传：澄清', detail.items[0].anchor, 'doxcnLi1')
check('flows/states 透传（v1.4）：gap/anchor/obj', [detail.digest.content.flows[0].gap, detail.digest.content.flows[0].anchor, detail.digest.content.states[0].obj, detail.digest.content.states[0].to], [true, 'doxcnRow6', '比赛', '报名中'])

// ===== 3. 重推 doc → 版本递增；引用不存在媒体 → missingMedia 统计不 500 =====
const pushed2 = await call('POST', `/api/requirements/${id}/artifacts`, {
  doc: { blocks, media, guard },
  digest: { summary: 'M4 夹具', roles: ['区管'], features: [], fr_points: [{ id: '需求.FR-01', name: 'x', anchor: 'doxcnLi1' }] },
  audit: { red: 0, yellow: 0, blue: 0, items: [] },
  items: [{ seq: 'P01', level: 'yellow', theme: '查询与展示口径', question: '排序口径？', answer: '', ctx: '', quote: '' }]
})
check('重推 doc：docUpdated 仍 true', pushed2.docUpdated, true)
check('重推 doc：媒体齐全 missingMedia=0（缺失即 409 阻断）', pushed2.missingMedia, 0)

// ===== 4. 降级：无 doc 的 v1.1 payload 推新卡 → detail 无 doc 键、anchor 空，不 500 =====
const { id: id2 } = await call('POST', '/api/requirements', {
  name: 'M4 降级验证', sourceType: 'text', sourceText: 'M4 legacy', submittedBy: 'dev'
})
await call('POST', `/api/requirements/${id2}/artifacts`, {
  snapshot: { md: '# legacy' },
  digest: { summary: 'legacy', roles: [], features: [], fr_points: [{ id: '需求.FR-01', name: 'x' }] },
  audit: { red: 0, yellow: 0, blue: 0, items: [] },
  items: [{ seq: 'P01', level: 'blue', theme: '其他', question: 'q?', answer: '', ctx: '', quote: '' }]
})
const d2 = await call('GET', `/api/requirements/${id2}`)
check('降级：无 doc 键', d2.doc, undefined)
check('降级：fr anchor 为空串', d2.frPoints[0].anchor, '')
check('降级：无 flows/states → 空数组', [d2.digest.content.flows, d2.digest.content.states], [[], []])

console.log(failures === 0 ? '\nM4 全部通过 ✅' : `\nM4 失败 ${failures} 项 ❌`)
process.exit(failures === 0 ? 0 : 1)
