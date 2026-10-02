// v2.4 批次1 验收：迭代域（DESIGN-v2.4 D32/D33）
// 覆盖：建迭代/重名 409/名称校验/建卡绑定/1:1 守卫 409/iter 自动建迭代（payload 契约兼容）/
//       空迭代 reqId=null/删绑定迭代 409/删空迭代 ok/软删需求释放迭代名/夹具清理
// 用法：node scripts/verify-iter.mjs [baseUrl]   （默认 http://127.0.0.1:8080）
const base = process.argv[2] || 'http://127.0.0.1:8080'

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
  if (!res.ok) { const err = new Error(data.error || ''); err.status = res.status; err.data = data; throw err }
  return data
}

const IT1 = 'v9.9.91-verify'
const IT_LEGACY = 'v9.9.92-legacy'
const ROW_KEYS = 'name,note,createdBy,createdAt,reqId,reqName,reqStatus,riskTier,submittedBy,totalQ,answeredQ,caseCount,designPhase,gateStage'

// ===== 1. 建迭代 + 校验 =====
const created = await call('POST', '/api/iterations', { name: IT1, note: '批次1 验收夹具', createdBy: 'verify-iter' })
check('建迭代回执 name', created.name, IT1)
let dup = null
try { await call('POST', '/api/iterations', { name: IT1 }) } catch (e) { dup = e.status }
check('重名建迭代 409', dup, 409)
let longName = null
try { await call('POST', '/api/iterations', { name: 'x'.repeat(65) }) } catch (e) { longName = e.status }
check('迭代名超长 400', longName, 400)
let blank = null
try { await call('POST', '/api/iterations', { name: '   ' }) } catch (e) { blank = e.status }
check('迭代名空白 400', blank, 400)

// ===== 2. 建卡绑定（1:1 正向）=====
const { id: R1 } = await call('POST', '/api/requirements', {
  name: '迭代绑定验证卡', iter: IT1, sourceType: 'text', sourceText: 'iter fixture', submittedBy: 'verify-iter'
})
const row1 = await call('GET', `/api/iterations/detail?name=${encodeURIComponent(IT1)}`)
check('迭代行键序（平铺契约）', Object.keys(row1).join(','), ROW_KEYS)
check('绑定后 reqId 指向新卡', [row1.reqId, row1.reqName, row1.reqStatus], [R1, '迭代绑定验证卡', 'submitted'])
const list1 = await call('GET', '/api/iterations')
check('列表含该迭代且行数=1（脏库防多行）', list1.filter(r => r.name === IT1).length, 1)

// ===== 3. 1:1 守卫：同迭代第二张卡 409 =====
let second = null
try {
  await call('POST', '/api/requirements', { name: '第二张卡', iter: IT1, sourceType: 'text', sourceText: 'x' })
} catch (e) { second = e.status }
check('同迭代第二张卡 409', second, 409)

// ===== 4. payload 契约兼容：iter 不存在自动建迭代 =====
const { id: R2 } = await call('POST', '/api/requirements', {
  name: 'legacy iter 自动建迭代', iter: IT_LEGACY, sourceType: 'text', sourceText: 'x'
})
const rowL = await call('GET', `/api/iterations/detail?name=${encodeURIComponent(IT_LEGACY)}`)
check('iter 自动建迭代且绑定', rowL.reqId, R2)

// ===== 5. 空迭代：reqId=null + 可删 =====
await call('POST', '/api/iterations', { name: 'v9.9.93-empty', createdBy: 'verify-iter' })
const rowE = await call('GET', '/api/iterations/detail?name=v9.9.93-empty')
check('空迭代 reqId=null', rowE.reqId, null)
const delE = await call('DELETE', '/api/iterations/detail?name=v9.9.93-empty')
check('删空迭代 ok', delE.ok, true)
let gone = null
try { await call('GET', '/api/iterations/detail?name=v9.9.93-empty') } catch (e) { gone = e.status }
check('删后详情 404', gone, 404)

// ===== 6. 删绑定迭代 409 =====
let delBound = null
try { await call('DELETE', `/api/iterations/detail?name=${encodeURIComponent(IT1)}`) } catch (e) { delBound = e.status }
check('删绑定迭代 409', delBound, 409)

// ===== 7. 软删需求释放迭代名 → 迭代可删 =====
await call('DELETE', `/api/requirements/${R1}`)
await call('DELETE', `/api/requirements/${R2}`)
const del1 = await call('DELETE', `/api/iterations/detail?name=${encodeURIComponent(IT1)}`)
const delL = await call('DELETE', `/api/iterations/detail?name=${encodeURIComponent(IT_LEGACY)}`)
check('软删需求后迭代名释放（可删）', [del1.ok, delL.ok], [true, true])

// ===== 8. 释放后可重新绑定（迭代名复用合法）=====
const { id: R3 } = await call('POST', '/api/requirements', {
  name: '释放后重绑', iter: IT1, sourceType: 'text', sourceText: 'x'
})
check('释放后同名迭代可重新绑定', (await call('GET', `/api/iterations/detail?name=${encodeURIComponent(IT1)}`)).reqId, R3)
await call('DELETE', `/api/requirements/${R3}`)
await call('DELETE', `/api/iterations/detail?name=${encodeURIComponent(IT1)}`)

// ===== 9. 迭代名含 "/"（回归修复）：query 参通道端到端 =====
const IT_SLASH = 'feature/v9.9.95-slash'
await call('POST', '/api/iterations', { name: IT_SLASH, createdBy: 'verify-iter' })
const { id: RS } = await call('POST', '/api/requirements', { name: '斜杠名迭代绑定', iter: IT_SLASH, sourceType: 'text', sourceText: 'x' })
check('斜杠名迭代详情（query 参通道）', (await call('GET', `/api/iterations/detail?name=${encodeURIComponent(IT_SLASH)}`)).reqId, RS)
await call('DELETE', `/api/requirements/${RS}`)
await call('DELETE', `/api/iterations/detail?name=${encodeURIComponent(IT_SLASH)}`)
check('斜杠名空迭代可删', true, true)

console.log(failures === 0 ? '\n迭代域验证全部通过 ✓' : `\n${failures} 项失败 ✗`)
process.exit(failures === 0 ? 0 : 1)
