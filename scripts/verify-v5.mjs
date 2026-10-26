// V5 二期验收（DESIGN-v5 §6.1/§6.3/§6.4 黑盒主路径）：澄清契约 v1.6 端到端——
// push 解析新字段（options/module/layer/pids/audit layer）→ PUT 结构化 option JSON →
// answers/detail 双出口 answerText 解构（含 D-E 残缺透传 + extra 连接符）。
// 用法：node scripts/verify-v5.mjs [baseUrl]（需平台已运行；自建卡自足，不依赖真实数据）
const base = process.argv[2] || 'http://127.0.0.1:8888'

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
  if (!res.ok) throw new Error(`${method} ${path} -> ${res.status}: ${data.error || ''}`)
  return data
}

// 1. 建卡 + push v1.6 产物（数字 layer 主线形态 + flows pids + audit layer 混合形态）
const created = await call('POST', '/api/requirements', {
  name: 'V5 二期验收（黑盒主路径）', sourceType: 'text',
  sourceText: '# 原文\n\n## 背景\n\n一段说明。', submittedBy: 'verify-v5',
})
const reqId = created.id
await call('POST', `/api/requirements/${reqId}/artifacts`, {
  digest: { md: '', summary: 's', roles: ['用户'], states: [], features: [], flows: [
    { role: '用户', step: '提交', next: '审核', anchor: '', pids: ['P01'] }] },
  audit: { md: '', red: 0, yellow: 1, blue: 0, items: [
    { id: 'P01', level: 'yellow', title: 't', where: 'w', go: '', group: 'g', quote: '', anchor: '', layer: 2 },
    { id: 'P02', level: 'yellow', title: 't', where: 'w', go: '', group: 'g', quote: '', anchor: '', layer: '9' }] },
  items: [
    { seq: 'P01', level: 'yellow', theme: '域一', question: '选择题？', anchor: '',
      options: [{ key: 'A', text: '选项A全文' }, { key: 'B', text: '选项B全文' }],
      module: '教研', layer: 3 },
    { seq: 'P02', level: 'yellow', theme: '域一', question: '开放题？', anchor: '' },
    { seq: 'P03', level: 'yellow', theme: '域一', question: '挂起题？', anchor: '' }],
})

const detail0 = await call('GET', `/api/requirements/${reqId}`)
const items0 = Object.fromEntries(detail0.items.map(i => [i.seq, i]))
check('push 解析：P01 options 快照（四键白名单归一，review #11）', items0.P01.options,
  [{ key: 'A', text: '选项A全文', why: '', recommended: false }, { key: 'B', text: '选项B全文', why: '', recommended: false }])
check('push 解析：P01 module/layer（数字 3 → "3"）', [items0.P01.module, items0.P01.layer], ['教研', '3'])
check('push 解析：P02 旧形态零扰动（空串/空数组）', [items0.P02.options, items0.P02.module, items0.P02.layer], [[], '', ''])
check('push 解析：flows[].pids 一律数组', detail0.digest.content.flows[0].pids, ['P01'])
const auditItems0 = Object.fromEntries(detail0.audit.content.items.map(i => [i.id, i]))
check('audit layer 归一：数字 2 → "2"、越界 "9" → ""', [auditItems0.P01.layer, auditItems0.P02.layer], ['2', ''])

// 2. 作答主路径：点选项（结构化 JSON）→ 双出口 answerText 解构
const optionJson = '{"kind":"option","optionKey":"A","optionText":"选项A全文","extra":"补充口径"}'
await call('PUT', `/api/requirements/${reqId}/answers/P01`, { answer: optionJson, status: 'answered' })
await call('PUT', `/api/requirements/${reqId}/answers/P02`, { answer: '纯文本答复' })
const answers = Object.fromEntries((await call('GET', `/api/requirements/${reqId}/answers`)).map(a => [a.seq, a]))
check('answers：P01 answer 原样落库（宽进）', answers.P01.answer, optionJson)
check('answers：P01 answerText 解构（optionText＋（补充：extra））', answers.P01.answerText, '选项A全文（补充：补充口径）')
check('answers：P02 纯文本 answerText 原文', answers.P02.answerText, '纯文本答复')
const detail1 = await call('GET', `/api/requirements/${reqId}`)
const p01 = detail1.items.find(i => i.seq === 'P01')
check('detail（wire 出口）：P01 answerText 与 answers 同值', p01.answerText, '选项A全文（补充：补充口径）')

// 3. D-E 残缺结构：原文透传（不产空串、不外溢解构失败）
const brokenJson = '{"kind":"option","optionKey":"B"}'   // 有 kind 无 optionText
await call('PUT', `/api/requirements/${reqId}/answers/P02`, { answer: brokenJson, status: 'answered' })
const answers2 = Object.fromEntries((await call('GET', `/api/requirements/${reqId}/answers`)).map(a => [a.seq, a]))
check('D-E 残缺：answerText 原文透传（非空串）', answers2.P02.answerText, brokenJson)

// 4. deferred 挂起 + note（A4 前置：挂起行 answer 空 → answerText 空串形态合法）
await call('PUT', `/api/requirements/${reqId}/answers/P03`, { status: 'deferred', note: '下期确认' })
const answers3 = Object.fromEntries((await call('GET', `/api/requirements/${reqId}/answers`)).map(a => [a.seq, a]))
check('deferred：status/note 落库', [answers3.P03.status, answers3.P03.note], ['deferred', '下期确认'])

// 5. 生产端静态断言（三期 ①②）：runner/agent prompt 携带 v1.6 产出指令与 D-M 白名单——防回退
const { readFileSync } = await import('node:fs')
const root = new URL('..', import.meta.url)
const runnerSrc = readFileSync(new URL('runner/runner.mjs', root), 'utf8')
const qc = runnerSrc.split("task.type === 'quality_check'")[1]
const inc = runnerSrc.split("task.type === 'incremental_check'")[1].split("task.type === 'standard_doc'")[0]
check('runner①：六层文案已退役（qc/inc 产出段级负向，review #20b）', [/六层/.test(qc), /六层/.test(inc)], [false, false])
check('runner①②：七层 + v1.6 契约引用 + 组装版本', [runnerSrc.includes('七层'), runnerSrc.includes('payload 契约 v1.6'), runnerSrc.includes('组装 v1.6 payload')], [true, true, true])
check('runner①：options/module/layer/pids 产出指令', ['options', 'module', 'layer', 'pids'].every(k => qc.includes(k)), true)
check('runner②：新字段 + 存量 pids 引用纪律', ['options', 'layer', 'pids', '存量已分配'].every(k => inc.includes(k)), true)
const promptsPy = readFileSync(new URL('src/platform_sdlc/agent/prompts.py', root), 'utf8')
check('agent 速查卡：options/pids/layer 字段', ['options', 'pids', 'layer'].every(k => promptsPy.includes(k)), true)
const toolsPy = readFileSync(new URL('src/platform_sdlc/agent/tools.py', root), 'utf8')
check('D-M：curl 白名单在位（_CMD_ALLOW 集合成员资格，review #20b）', /_CMD_ALLOW\s*=\s*\{[^}]*"curl"/.test(toolsPy), true)

console.log(failures ? `\n存在失败项 ❌（${failures}）` : '\nV5 黑盒主路径全部通过 ✅')
process.exit(failures ? 1 : 0)
