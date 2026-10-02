"""门禁域服务（对应 GateService，DESIGN-v2.4 D35）。

skill 权威落库仅两值（round.status=待裁决｜已放行）；六态 phase 为读面推导（页面标注）。
GR-2/3/4 为平台化机械校验。
"""

from __future__ import annotations

import json
import re
from typing import Any

from platform_sdlc.common import ApiException, Times, jbool, jrequired, jtext
from platform_sdlc.db import Database
from platform_sdlc.models import GateIssue, GateRound, TestCase, string_list
from platform_sdlc.repositories import Repos
from platform_sdlc.services import task as task_mod
from platform_sdlc.services.design import A_DESIGN, DesignService
from platform_sdlc.services.task import TaskService

_CODE_P = re.compile(r"^[DAST]-\d{2}$")
_SEVS = {"high", "mid", "low"}
_VERDICTS = {"落改", "驳回", "存疑"}


class GateService:
    def __init__(self, db: Database, repos: Repos, designs: DesignService, tasks: TaskService) -> None:
        self.db = db
        self.repos = repos
        self.designs = designs
        self.tasks = tasks

    # ---------- GR-2 开闸三条件（现算） ----------

    def checks(self, req_id: str) -> list[dict[str, Any]]:
        dv = self.designs.view(req_id)
        phase = dv.get("phase")
        c1: dict[str, Any] = {"key": "design", "name": "设计已定稿 ∨ 轻量跳过", "ok": phase in ("final", "skipped")}
        if phase == "final":
            f = dv.get("finalized") or {}
            c1["detail"] = f"已定稿 v{f.get('version')}（{f.get('by', '')}）"
        elif phase == "skipped":
            c1["detail"] = "已声明轻量跳过（自查档）"
        else:
            c1["detail"] = f"设计未定稿（当前：{phase}）"

        cases = self.repos.test_case.list_by_req(req_id)
        reviewed = sum(1 for t in cases if t.status == "reviewed")
        c2: dict[str, Any] = {"key": "cases", "name": "用例 ≥1 且全部定稿",
                              "ok": bool(cases) and reviewed == len(cases)}
        if not cases:
            c2["detail"] = "无用例登记"
        elif reviewed == len(cases):
            c2["detail"] = f"{len(cases)} 条 · 全部定稿"
        else:
            c2["detail"] = f"{len(cases)} 条 · {len(cases) - reviewed} 条未定稿"

        uncovered = self._uncovered_frs(req_id, cases)
        fr_total = len(self.repos.fr_point.list_by_req(req_id))
        c3: dict[str, Any] = {"key": "coverage", "name": "FR 覆盖（RTM 无未覆盖行）", "ok": uncovered == 0}
        c3["detail"] = ("无需求点注册（视为覆盖）" if fr_total == 0 else
                        f"覆盖 {fr_total - uncovered}/{fr_total}" + (f" · {uncovered} 条未覆盖" if uncovered > 0 else ""))
        return [c1, c2, c3]

    def _uncovered_frs(self, req_id: str, cases: list[TestCase]) -> int:
        """RTM 未覆盖 FR 数（读时聚合，与 RequirementService.detail.coverage 同口径：points 引用即覆盖）。"""
        referenced: set[str] = set()
        for t in cases:
            for p in string_list(t.points):
                if p:
                    referenced.add(p)
        return sum(1 for f in self.repos.fr_point.list_by_req(req_id) if f.id not in referenced)

    # ---------- 开闸 / 复审 ----------

    def open(self, req_id: str, body: dict[str, Any] | None) -> dict[str, Any]:
        """开闸（GR-2 机械校验）：三条件全绿才建轮 + 排 gate_review。"""
        with self.db.tx():
            r = self.require(req_id)
            if r.status != "published":
                raise ApiException(409, f"门禁属发布后流程：先完成澄清并发布（当前：{r.status}）")
            cs = self.checks(req_id)
            failed = [c["name"] + f"（{c['detail']}）" for c in cs if not c["ok"]]
            if failed:
                raise ApiException(409, "开闸三条件未满足：" + "；".join(failed))
            rounds = self.repos.gate.list_rounds(req_id)
            latest = rounds[-1] if rounds else None
            if latest is not None and latest.status == "待裁决":
                raise ApiException(409, f"已有进行中的评审轮 r{latest.round_no}（待裁决），先完成裁决或放行")
            self._create_round(req_id, len(rounds) + 1, cs, (body or {}).get("by"))
        return self.view(req_id)

    def reopen(self, req_id: str, body: dict[str, Any] | None) -> dict[str, Any]:
        """GR-3 复审：放行后检测到实质变更，人确认开新一轮（旧轮只读归档）。"""
        with self.db.tx():
            self.require(req_id)
            rounds = self.repos.gate.list_rounds(req_id)
            latest = rounds[-1] if rounds else None
            if latest is None or latest.status != "已放行":
                raise ApiException(409, "仅「已放行」后可开复审轮（GR-3 两窗口之一）")
            ch = self.change_status(req_id)
            if not ch["changed"]:
                raise ApiException(409, f"GR-3 内容变更检测未命中（设计/用例与 r{latest.round_no} 基线一致），不开新轮")
            self._create_round(req_id, len(rounds) + 1, self.checks(req_id), (body or {}).get("by"))
        return self.view(req_id)

    def _create_round(self, req_id: str, round_no: int, cs: list[dict[str, Any]], by: Any) -> None:
        rnd = GateRound(id=None, req_id=req_id, round_no=round_no, status="待裁决",
                        checks_json=_json(cs), baseline_json=self._baseline_json(req_id),
                        created_by=(by or "") if isinstance(by, str) else "", opened_at=Times.now())
        self.repos.gate.insert_round(rnd)
        self.tasks.create_auto(req_id, task_mod.T_GATE, "gate:open", f"评审轮 r{round_no}（开闸三条件全绿）")

    def _baseline_json(self, req_id: str) -> str:
        """GR-3 基线：设计终稿全文（内容变更检测，非 hash）+ 用例最新更新时间。"""
        d = self.repos.artifact.find(req_id, A_DESIGN)
        return _json({"designVer": d.version if d else 0, "designMd": d.markdown if d else "",
                      "casesMaxUpdated": self._cases_max_updated(req_id)})

    def _cases_max_updated(self, req_id: str) -> str:
        mx = ""
        for t in self.repos.test_case.list_by_req(req_id):
            if t.updated_at is not None and t.updated_at > mx:
                mx = t.updated_at
        return mx

    # ---------- issues 入库（机械校验） ----------

    def ingest(self, req_id: str, round_id: int, body: dict[str, Any] | None) -> dict[str, Any]:
        """gate_issues 草稿确认入库：编号/严重度/场景列/引用可达（md×json 同源）机械校验，全绿才落 gate_issue。"""
        _ = body
        with self.db.tx():
            self.require(req_id)
            rnd = self.repos.gate.find_round(round_id)
            if rnd is None:
                raise ApiException(404, f"评审轮不存在：{round_id}")
            if rnd.req_id != req_id:
                raise ApiException(404, "评审轮不属于该需求")
            if rnd.status != "待裁决":
                raise ApiException(409, "已放行轮只读，不能入库 issues")
            d = self.repos.draft.find(req_id, task_mod.D_GATE_ISSUES)
            if d is None:
                raise ApiException(404, "无 gate_issues 草稿（评审任务尚未回传）")
            if not d.md.strip():
                raise ApiException(400, "issues 草稿缺 md（issues 文件全文）")
            issues_raw = _parse(d.content_json).get("issues")
            if not isinstance(issues_raw, list) or not issues_raw:
                raise ApiException(400, "issues 草稿 contentJson.issues 为空")

            codes: set[str] = set()
            parsed: list[GateIssue] = []
            max_unit = 0
            for n in issues_raw:
                code = jrequired(n, "code").strip()
                if not _CODE_P.match(code):
                    raise ApiException(400, f"非法 issue 编号：{code}（前缀权威 = D分歧/A架构/S数据/T可测性，如 S-01）")
                if code in codes:
                    raise ApiException(400, f"issue 编号重复：{code}")
                sev = jtext(n, "sev", "mid")
                if sev not in _SEVS:
                    raise ApiException(400, f"非法严重度：{sev}（high|mid|low）")
                scene = jtext(n, "scene", "")
                if not scene.strip():
                    raise ApiException(400, f"{code} 场景列为空（check_trace 检查5：白话场景必填）")
                merged_unit = int(n.get("mergedUnit") or 0) if isinstance(n, dict) else 0
                max_unit = max(max_unit, merged_unit)
                if code not in d.md:
                    raise ApiException(400, f"机械校验失败：md 中找不到 {code}（contentJson 与 issues 文件不同源）")
                parsed.append(GateIssue(
                    id=None, round_id=round_id, code=code, sev=sev, theme=jtext(n, "theme", ""),
                    role=jtext(n, "role", ""), scene=scene, quote=jtext(n, "quote", ""), loc=jtext(n, "loc", ""),
                    basis=jtext(n, "basis", "soft"), div_flag=1 if jbool(n, "div", False) else 0,
                    tradeoff=1 if jbool(n, "tradeoff", False) else 0,
                    filed=1 if jbool(n, "filed", False) else 0, merged_unit=merged_unit,
                ))
            for g in parsed:
                if g.merged_unit == 0:
                    max_unit += 1
                    g.merged_unit = max_unit  # 未合并条目各自成单元
            self.repos.gate.insert_issues(parsed)
            self.repos.draft.delete(req_id, task_mod.D_GATE_ISSUES)
        return self.view(req_id)

    # ---------- 裁决与修订 ----------

    def verdict(self, issue_id: int, body: dict[str, Any]) -> dict[str, Any]:
        """裁决三处置（按裁决单元批量）：落改必填落点 / 驳回必填理由 / 存疑必填去向。"""
        with self.db.tx():
            issue = self.repos.gate.find_issue(issue_id)
            if issue is None:
                raise ApiException(404, f"issue 不存在：{issue_id}")
            rnd = self.repos.gate.find_round(issue.round_id)
            if rnd is None:
                raise ApiException(404, "评审轮不存在")
            if rnd.status != "待裁决":
                raise ApiException(409, "已放行轮只读，不能裁决")
            v = jrequired(body, "verdict").strip()
            if v not in _VERDICTS:
                raise ApiException(400, f"非法处置：{v}（落改|驳回|存疑）")
            note = jtext(body, "note", "").strip()
            loc = jtext(body, "revLocation", "").strip()
            if v == "落改":
                if not loc:
                    raise ApiException(400, f"{issue.code} 落改必填落点（<文件+位置>）")
            elif not note:
                suffix = "理由（沉淀误报模式）" if v == "驳回" else "去向"
                raise ApiException(400, f"{issue.code} {v}必填{suffix}")
            at = Times.now()
            self.repos.gate.apply_verdict(rnd.id or 0, issue.merged_unit, v, note,
                                          jtext(body, "by", "").strip(), at)
            if v == "落改":
                rev_status = jtext(body, "revStatus", "待执行")
                if rev_status not in ("待执行", "已执行"):
                    raise ApiException(400, f"非法修订状态：{rev_status}（待执行|已执行）")
                self.repos.gate.apply_revision(rnd.id or 0, issue.merged_unit, loc, rev_status)
            req_id = rnd.req_id
        return self.view(req_id)

    def revision(self, issue_id: int, rev_location: str | None, rev_status: str) -> dict[str, Any]:
        """修订工作项推进（落改闭环）：AI 修订草稿被人采纳后置 已执行（GR-4 放行前提）。"""
        with self.db.tx():
            issue = self.repos.gate.find_issue(issue_id)
            if issue is None:
                raise ApiException(404, f"issue 不存在：{issue_id}")
            rnd = self.repos.gate.find_round(issue.round_id)
            if rnd is None:
                raise ApiException(404, "评审轮不存在")
            if rnd.status != "待裁决":
                raise ApiException(409, "已放行轮只读")
            st = jrequired({"revStatus": rev_status}, "revStatus")
            if st not in ("待执行", "已执行"):
                raise ApiException(400, "非法修订状态（待执行|已执行）")
            self.repos.gate.apply_revision(rnd.id or 0, issue.merged_unit, (rev_location or "").strip(), st)
            req_id = rnd.req_id
        return self.view(req_id)

    # ---------- GR-4 放行 ----------

    def release(self, round_id: int, body: dict[str, Any] | None) -> dict[str, Any]:
        """放行 = 全部裁决 ∧ 落改闭环 ∧ 机械校验全绿；通过即关卡互认（用例关卡1 视同通过）。"""
        with self.db.tx():
            rnd = self.repos.gate.find_round(round_id)
            if rnd is None:
                raise ApiException(404, f"评审轮不存在：{round_id}")
            self.require(rnd.req_id)
            if rnd.status != "待裁决":
                raise ApiException(409, "该轮已放行，不能重复放行")
            issues = self.repos.gate.list_issues(round_id)
            blocked: list[str] = []
            for g in issues:
                if g.filed == 1:
                    continue  # 备案区除外（skill 口径）
                if not g.verdict:
                    blocked.append(f"{g.code} 未裁决")
                elif g.verdict == "落改" and (not g.rev_location or g.rev_status != "已执行"):
                    blocked.append(f"{g.code} 落改未闭环（落点 {'缺' if not g.rev_location else '有'} · "
                                   f"{g.rev_status or '待执行'}）")
            if blocked:
                raise ApiException(409, "GR-4 放行校验未通过：" + "；".join(blocked))
            for c in self.checks(rnd.req_id):
                if not c["ok"]:
                    blocked.append(f"{c['name']}（{c['detail']}）")
            if blocked:
                raise ApiException(409, "GR-4 机械校验未通过：" + "；".join(blocked))
            by = jtext(body or {}, "by", "").strip()
            at = Times.now()
            if self.repos.gate.set_released(rnd.id or 0, by, at) == 0:
                raise ApiException(409, "放行竞态：该轮状态已变化")
            out = {"ok": True, "releasedAt": at, "releasedBy": by, "roundNo": rnd.round_no,
                   "casesConfirmed": self.repos.test_case.mark_reviewed_by_gate(rnd.req_id, at)}  # 关卡互认
        return out

    # ---------- GR-3 变更检测 ----------

    def change_status(self, req_id: str) -> dict[str, Any]:
        """仅「已放行后」出变更横幅；评审中变更走落改通道，不开新轮（GR-3 两窗口口径）。"""
        reasons: list[str] = []
        rounds = self.repos.gate.list_rounds(req_id)
        latest = rounds[-1] if rounds else None
        if latest is None:
            return {"changed": False, "reasons": [], "window": "none"}
        b = _parse(latest.baseline_json)
        d = self.repos.artifact.find(req_id, A_DESIGN)
        cur_md = d.markdown if d else ""
        cur_cases = self._cases_max_updated(req_id)
        if cur_md != str(b.get("designMd", "")):
            reasons.append(f"设计文档内容变更（v{d.version if d else 0} ≠ 基线 v{int(b.get('designVer') or 0)}）")
        if cur_cases > str(b.get("casesMaxUpdated", "")):
            reasons.append("用例在基线后有更新")
        return {"changed": bool(reasons), "reasons": reasons,
                "window": "released" if latest.status == "已放行" else "in-review"}

    # ---------- 读面（六态推导） ----------

    def view(self, req_id: str) -> dict[str, Any]:
        r = self.require(req_id)
        out: dict[str, Any] = {"reqId": req_id, "tier": r.risk_tier, "checks": self.checks(req_id)}
        rounds = self.repos.gate.list_rounds(req_id)
        round_blocks = [self._round_block(g) for g in rounds]
        out["rounds"] = round_blocks
        latest = rounds[-1] if rounds else None
        if latest is not None:
            out["round"] = round_blocks[-1]
            issues = self.repos.gate.list_issues(latest.id or 0)
            issue_blocks: list[dict[str, Any]] = []
            units: dict[int, dict[str, Any]] = {}
            for g in issues:
                issue_blocks.append(self._issue_block(g))
                u = units.setdefault(g.merged_unit, {"unit": g.merged_unit, "codes": [], "verdict": ""})
                u["codes"].append(g.code)
                if not u["verdict"] and g.verdict:
                    u["verdict"] = g.verdict
                if g.filed == 1:
                    u["verdict"] = u["verdict"] or "备案"
            out["issues"] = issue_blocks
            out["units"] = list(units.values())
        out["change"] = self.change_status(req_id)
        for t in self.repos.task.list(req_id, None):
            if t.type == task_mod.T_GATE:
                out["activeTask"] = {"id": t.id, "type": t.type, "status": t.status, "createdAt": t.created_at}
                break  # list 按 id 倒序，首个即最新
        out["phase"] = self._derive_phase(out, latest)
        return out

    def _derive_phase(self, out: dict[str, Any], latest: GateRound | None) -> str:
        """六态推导（UI 口径，非落库权威）：blocked/running/summary/deciding/released。"""
        if latest is None:
            return "blocked"
        if latest.status == "已放行":
            return "released"
        if out.get("issues"):
            return "deciding"
        ts = (out.get("activeTask") or {}).get("status", "")
        return "summary" if ts == "done" else "running"

    def _round_block(self, g: GateRound) -> dict[str, Any]:
        return {"id": g.id, "roundNo": g.round_no, "status": g.status, "openedAt": g.opened_at,
                "releasedAt": g.released_at, "releasedBy": g.released_by}

    def _issue_block(self, g: GateIssue) -> dict[str, Any]:
        return {"id": g.id, "code": g.code, "sev": g.sev, "theme": g.theme, "role": g.role,
                "scene": g.scene, "quote": g.quote, "loc": g.loc, "basis": g.basis,
                "div": g.div_flag == 1, "tradeoff": g.tradeoff == 1, "unit": g.merged_unit,
                "filed": g.filed == 1, "verdict": g.verdict, "verdictNote": g.verdict_note,
                "verdictBy": g.verdict_by, "verdictAt": g.verdict_at,
                "revLocation": g.rev_location, "revStatus": g.rev_status}

    def require(self, req_id: str):
        r = self.repos.requirement.find(req_id)
        if r is None:
            raise ApiException(404, f"需求不存在：{req_id}")
        return r


def _parse(json_str: str) -> dict[str, Any]:
    try:
        v = json.loads(json_str.strip() or "{}")
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


def _json(o: Any) -> str:
    return json.dumps(o, ensure_ascii=False, separators=(",", ":"))
