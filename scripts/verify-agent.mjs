#!/usr/bin/env node
// v2.1 Phase ② 验收：进程内 agent（AgentScope + GLM）端到端冒烟。
// 前提：平台已启动、sdlc.agent.enabled=true、GLM 模型可用（glm-5.3 需余额/资源包）。
// 流程：agent 状态 → 建演练需求（AGENT-E2E）→ quality_check 任务 → 轮询终态 → 断言草稿结构。
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

// 1. agent 状态
const st = await api('GET', '/api/agent/status');
assert(st.enabled === true && st.running === true && st.paused === false, '进程内 agent 已启用且在运行', JSON.stringify(st));

// 2. 演练需求
const NAME = `AGENT冒烟-${new Date().toISOString().slice(11, 19).replace(/:/g, '')}`;
const req = await api('POST', '/api/requirements', {
  name: NAME, iter: 'AGENT-E2E', sourceType: 'text', submittedBy: 'verify-agent',
  sourceText: `# ${NAME}\n\n## 背景\n内部工具小需求，用于 agent 冒烟。\n\n## 功能\n1. 用户提交申请表单（姓名、事由）；\n2. 管理员审批后通知提交人；\n3. 全部记录可导出。\n\n## 待明确\n- 审批驳回后能否重新提交未说明。`
});

// 3. 任务 + 轮询（最长 8 分钟）
const task = await api('POST', `/api/requirements/${req.id}/tasks`, { type: 'quality_check' });
console.log(`任务 #${task.id} @ ${req.id}，轮询中…`);
let t = task;
for (let i = 0; i < 96 && (t.status === 'queued' || t.status === 'running'); i++) {
  await new Promise(r => setTimeout(r, 5000));
  const list = await api('GET', `/api/tasks?req_id=${req.id}`);
  t = list.find(x => x.id === task.id) || t;
}
if (t.status !== 'done') {
  console.log(`FAIL  任务终态 ${t.status}：${(t.result || '').slice(0, 300)}`);
  process.exitCode = 1;
} else {
  assert(true, 'quality_check 由进程内 agent 执行完成', t.result.slice(0, 120));
  const d = await api('GET', `/api/requirements/${req.id}/drafts/digest_bundle`);
  const p = JSON.parse(d.contentJson || '{}');
  assert(!!p.digest && !!p.audit && Array.isArray(p.items) && p.items.length > 0, 'digest_bundle 草稿结构完整',
    `items=${(p.items || []).length}`);
}
console.log(process.exitCode ? '\nagent 冒烟存在失败项 ❌（若是模型余额/能力问题，先解决 sdlc.agent.* 配置后重跑）' : '\nagent 冒烟通过 ✅');
