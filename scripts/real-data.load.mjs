// 种子加载 + v1.1 字段合并：
//   real-data.seed.js  = v1.0 基础字段（真实需求的平台化表示）
//   real-data.v11.js   = v1.1 追溯字段（生成自交互原型 data-v11.js，勿手改）
// 用法：const { req, cases, v11 } = await loadReal()
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

export async function loadReal() {
  globalThis.window = globalThis
  const dir = dirname(fileURLToPath(import.meta.url))
  await import(join(dir, 'real-data.seed.js'))
  await import(join(dir, 'real-data.v11.js'))
  const base = window.REAL_DATA
  const v11 = window.REAL_DATA_V11
  const src = base.reqs[0]

  const req = {
    ...src,
    digest: {
      ...src.digest,
      features: (src.digest.features || []).map(f => ({ ...f, src: (v11.features[f.m] || {}).src || '' })),
      fr: src.digest.fr.map(f => ({ ...f, ...(v11.fr[f.id] || {}) }))
    },
    audit: { ...src.audit, items: src.audit.items.map(i => ({ ...i, ...(v11.audit[i.id] || {}) })) },
    checklist: src.checklist.map(c => ({ ...c, theme: v11.theme[c.id] || '', quote: v11.quote[c.id] || '' }))
  }
  const cases = base.cases.map(c => ({ ...c, verifyRefs: [], defectRefs: [], ...(v11.cases[c.id] || {}) }))
  return { req, cases, v11 }
}
