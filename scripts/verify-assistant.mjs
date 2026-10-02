#!/usr/bin/env node
// P5 验收：查询聊天助理端到端冒烟。
// 前提：平台已启动、SDLC_AGENT_* 已配置（需模型余额）、sync-real 种子已入库（REQ-2026-001 在）。
// 断言：SSE 流式事件结构（delta/done）+ 数据接地（回答包含真实数据线索）+ 会话记忆端点。
// 用法：node scripts/verify-assistant.mjs [BASE=http://127.0.0.1:8080]
const BASE = process.argv[2] || 'http://127.0.0.1:8080'
const SID = 'verify-assistant-' + Date.now().toString(36)
let failures = 0
const check = (name, actual, expected) => {
  const ok = JSON.stringify(actual) === JSON.stringify(expected)
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${ok ? '' : '：' + JSON.stringify(actual) + '（期望 ' + JSON.stringify(expected) + '）'}`)
  if (!ok) failures++
}

async function chat(message) {
  const res = await fetch(BASE + '/api/assistant/chat', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ sessionId: SID, message })
  })
  if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`)
  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buf = '', text = '', sawDone = false
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buf += decoder.decode(value, { stream: true })
    let idx
    while ((idx = buf.indexOf('\n\n')) >= 0) {
      const frame = buf.slice(0, idx); buf = buf.slice(idx + 2)
      const line = frame.split('\n').find(l => l.startsWith('data: '))
      if (!line) continue
      const ev = JSON.parse(line.slice(6))
      if (ev.type === 'delta') text += ev.text
      if (ev.type === 'done') sawDone = true
      if (ev.type === 'error') throw new Error('SSE error: ' + ev.message)
    }
  }
  return { text, sawDone }
}

// 1. 参数守卫
const bad = await fetch(BASE + '/api/assistant/chat', {
  method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ message: 'x' })
})
check('缺 sessionId 返回 400', bad.status, 400)

// 2. 数据接地的查询（种子库必有一条 REQ-2026-001 区域教学技能比赛）
const r1 = await chat('平台现在一共有多少条需求？请列出编号')
check('SSE 流式正常收尾（done 事件）', r1.sawDone, true)
check('回答非空', r1.text.length > 10, true)
check('回答引用了真实需求编号', r1.text.includes('REQ-'), true)

// 3. 会话记忆：第二轮追问不重给上下文
const r2 = await chat('刚才你提到的第一条需求的编号是什么？')
check('会话记忆（第二轮仍连贯）', r2.text.includes('REQ-'), true)

// 4. 会话管理端点
const sids = await (await fetch(BASE + '/api/assistant/sessions')).json()
check('会话已登记', sids.includes(SID), true)
const cleared = await (await fetch(`${BASE}/api/assistant/sessions/${SID}`, { method: 'DELETE' })).json()
check('清空会话', cleared.ok, true)

console.log(failures === 0 ? '\n助理冒烟通过 ✅' : `\n助理冒烟 ${failures} 项失败 ❌`)
process.exit(failures === 0 ? 0 : 1)
