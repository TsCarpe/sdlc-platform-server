#!/usr/bin/env node
// v2.1 Phase ② 验收：进程内 agent（openai-agents 兼容端点）端到端冒烟。
// 前提：平台已启动（cwd=仓根）、sdlc.agent.enabled=true、模型可用（需余额）。
// v4-5 两段链路（DESIGN-v4 §12 一期判据：质检改读块模型）：
//   ① source_import 自动排 → agent 执行（write_file 源 md + parse_doc --md → out/doc.json）→ done 落库；
//   ② quality_check → agent 纯分析（get_requirement_card 的 doc + list_req_comments）→ digest_bundle 草稿，
//      抽查 items[].anchor ∈ doc.json 块 ID 集（输入源切换的黑盒判据）。
// 用法：node scripts/verify-agent.mjs [BASE=http://127.0.0.1:8080]
const BASE = process.argv[2] || 'http://127.0.0.1:8080';
const assert = (cond, label, extra = '') => {
  console.log(`${cond ? 'PASS' : 'FAIL'}  ${label}${extra ? ': ' + extra : ''}`);
  if (!cond) process.exitCode = 1;
  return cond;
};
const api = async (method, p, body) => {
  const r = await fetch(BASE + p, { method, headers: body ? { 'Content-Type': 'application/json' } : {}, body: body ? JSON.stringify(body) : undefined });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(d.error || `HTTP ${r.status}`);
  return d;
};
const waitTask = async (reqId, taskId, label) => {
  let t = { status: 'queued', result: '' };
  for (let i = 0; i < 96 && (t.status === 'queued' || t.status === 'running'); i++) {
    await new Promise(r => setTimeout(r, 5000));
    const list = await api('GET', `/api/tasks?req_id=${reqId}`);
    t = list.find(x => x.id === taskId) || t;
  }
  console.log(`${label} 任务 #${taskId} 终态 ${t.status}：${(t.result || '').slice(0, 160)}`);
  return t;
};

// 1. agent 状态
const st = await api('GET', '/api/agent/status');
assert(st.enabled === true && st.running === true && st.paused === false, '进程内 agent 已启用且在运行', JSON.stringify(st));

// 2. 演练需求（建卡自动排 source_import —— v4-4）
const NAME = `AGENT冒烟-${new Date().toISOString().slice(11, 19).replace(/:/g, '')}`;
const req = await api('POST', '/api/requirements', {
  name: NAME, iter: 'AGENT-E2E', sourceType: 'text', submittedBy: 'verify-agent',
  sourceText: `# ${NAME}\n\n## 背景\n内部工具小需求，用于 agent 冒烟。\n\n## 功能\n1. 用户提交申请表单（姓名、事由）；\n2. 管理员审批后通知提交人；\n3. 全部记录可导出。\n\n## 待明确\n- 审批驳回后能否重新提交未说明。`
});
const imports = await api('GET', `/api/tasks?req_id=${req.id}`);
const imp = imports.find(x => x.type === 'source_import');

// 3. ①导入段：轮询 done → 断言块表权威导出
const ti = await waitTask(req.id, imp.id, 'source_import');
if (!assert(ti.status === 'done', 'source_import 由进程内 agent 执行完成（落库走完成回调）', (ti.result || '').slice(0, 160))) {
  console.log('导入未完成，中止后续断言'); process.exit(1);
}
const stats = JSON.parse(ti.result || '{}');
assert(stats.blocks > 0, '落库统计 blocks > 0', ti.result);
const doc = await api('GET', `/api/requirements/${req.id}/doc.json`);
assert(Array.isArray(doc.blocks) && doc.blocks.length > 0, 'doc.json 块表权威导出非空', `blocks=${(doc.blocks || []).length}`);

// 4. ②质检段：纯分析读块模型 → digest_bundle 草稿 + anchor 命中块 ID 集
const task = await api('POST', `/api/requirements/${req.id}/tasks`, { type: 'quality_check' });
const tq = await waitTask(req.id, task.id, 'quality_check');
if (!assert(tq.status === 'done', 'quality_check 由进程内 agent 执行完成', (tq.result || '').slice(0, 160))) {
  process.exit(1);
}
const d = await api('GET', `/api/requirements/${req.id}/drafts/digest_bundle`);
const p = JSON.parse(d.contentJson || '{}');
assert(!!p.digest && !!p.audit && Array.isArray(p.items) && p.items.length > 0, 'digest_bundle 草稿结构完整',
  `items=${(p.items || []).length}`);
const blockIds = new Set(doc.blocks.map(b => b.id));
const anchored = (p.items || []).filter(x => x.anchor && blockIds.has(x.anchor));
assert(anchored.length > 0, '输入源切换判据：items[].anchor 命中 doc.json 块 ID 集',
  `${anchored.length}/${(p.items || []).length}`);
const comments = await api('GET', `/api/requirements/${req.id}/comments.json`);
assert(Array.isArray(comments), 'comments.json 兼容导出可读', `${comments.length} 条`);
console.log(process.exitCode ? '\nagent 冒烟存在失败项 ❌（若是模型余额/能力问题，先解决 sdlc.agent.* 配置后重跑）' : '\nagent 冒烟通过 ✅（导入 → 块模型质检 → 锚定 全链）');
