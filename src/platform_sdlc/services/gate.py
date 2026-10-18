"""门禁域服务（对应 GateService，DESIGN-v2.4 D35）。

skill 权威落库仅两值（round.status=待裁决｜已放行）；六态 phase 为读面推导（页面标注）。
GR-2/3/4 为平台化机械校验。
"""

from __future__ import annotations

import json
import re
from typing import Any

from platform_sdlc.common import ApiException, Times, is_json_list, is_json_obj, jbool, jint_or_400, jrequired, jtext
from platform_sdlc.db import Database
from platform_sdlc.models import GateIssue, GateRound, TestCase, string_list
from platform_sdlc.models import parse_json_obj as _parse  # JSON 口径统一收口（AUDIT D2-5）
from platform_sdlc.repositories import Repos
from platform_sdlc.services import task as task_mod
from platform_sdlc.services.design import A_DESIGN, DesignService

_CODE_P = re.compile(r"^[DAST]-\d{2}$")
_SEVS = {"high", "mid", "low"}
_VERDICTS = {"revise", "reject", "query"}  # 落改/驳回/存疑（中文口径见文案与 ER 图）


class GateService:
    def __init__(self, db: Database, repos: Repos, designs: DesignService) -> None:
        self.db = db
        self.repos = repos
        self.designs = designs

    # ---------- GR-2 开闸三条件（现算） ----------

    def checks(self, req_id: str) -> list[dict[str, Any]]:
        dv = self.designs.view(req_id)
        phase = dv.get("phase")
        c1: dict[str, Any] = {"key": "design", "name": "设计已定稿 ∨ 轻量跳过", "ok": phase in ("final", "skipped")}
        if phase == "final":
            f: dict[str, Any] = dv.get("finalized") or {}
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
            if latest is not None and latest.status == "pending":
                raise ApiException(409, f"已有进行中的评审轮 r{latest.round_no}（待裁决），先完成裁决或放行")
            if self.repos.task.count_active(req_id, task_mod.T_GATE) > 0:
                # 原语义保持（AUDIT D1-4 / 对抗审查 M1）：建轮与排评审任务曾同事务，排不上则整个
                # 开闸回滚——排布挪入 orchestration.on_gate_opened 后，此预判承担同等守卫
                raise ApiException(409, "该需求已有同类型任务在排队或执行中，请等待完成")
            self._create_round(req_id, len(rounds) + 1, cs, (body or {}).get("by"))
        return self.view(req_id)

    def reopen(self, req_id: str, body: dict[str, Any] | None) -> dict[str, Any]:
        """GR-3 复审：放行后检测到实质变更，人确认开新一轮（旧轮只读归档）。"""
        with self.db.tx():
            self.require(req_id)
            rounds = self.repos.gate.list_rounds(req_id)
            latest = rounds[-1] if rounds else None
            if latest is None or latest.status != "released":
                raise ApiException(409, "仅「已放行」后可开复审轮（GR-3 两窗口之一）")
            ch = self.change_status(req_id)
            if not ch["changed"]:
                raise ApiException(409, f"GR-3 内容变更检测未命中（设计/用例与 r{latest.round_no} 基线一致），不开新轮")
            self._create_round(req_id, len(rounds) + 1, self.checks(req_id), (body or {}).get("by"))
        return self.view(req_id)

    def _create_round(self, req_id: str, round_no: int, cs: list[dict[str, Any]], by: Any) -> None:
        rnd = GateRound(id=None, req_id=req_id, round_no=round_no, status="pending",
                        checks_json=_json(cs), baseline_json=self._baseline_json(req_id),
                        created_by=(by or "") if isinstance(by, str) else "", opened_at=Times.now())
        self.repos.gate.insert_round(rnd)  # 评审任务排布在 orchestration.on_gate_opened（路由成功后）

    def _baseline_json(self, req_id: str) -> str:
        """GR-3 基线：设计终稿全文（内容变更检测，非 hash）+ 用例最新更新时间 + reqRev（v4-16
        §7：需求内容快照——变更判定查变更单不比 rev，rev 仅入基线作展示对照）。"""
        d = self.repos.artifact.find(req_id, A_DESIGN)
        r = self.repos.requirement.find(req_id)
        return _json({"designVer": d.version if d else 0, "designMd": d.markdown if d else "",
                      "casesMaxUpdated": self._cases_max_updated(req_id),
                      "reqRev": r.content_rev if r else 0})

    def _cases_max_updated(self, req_id: str) -> str:
        mx = ""
        for t in self.repos.test_case.list_by_req(req_id):
            if t.updated_at is not None and t.updated_at > mx:
                mx = t.updated_at
        return mx

    # ---------- issues 入库（机械校验） ----------

    def ingest(self, req_id: str, round_id: int, body: dict[str, Any] | None) -> dict[str, Any]:
        """gate_issues 草稿确认入库：编号/严重度/场景列/引用可达（md×json 同源）机械校验，全绿才落 gate_issue。
        P1-9：issues 头 reviewMode/fanout/contextMode 解析落库；A/B 级 degraded/扇出不足拒收。"""
        _ = body
        with self.db.tx():
            r = self.require(req_id)
            rnd = self.repos.gate.find_round(round_id)
            if rnd is None:
                raise ApiException(404, f"评审轮不存在：{round_id}")
            if rnd.req_id != req_id:
                raise ApiException(404, "评审轮不属于该需求")
            if rnd.status != "pending":
                raise ApiException(409, "已放行轮只读，不能入库 issues")
            d = self.repos.draft.find(req_id, task_mod.D_GATE_ISSUES)
            if d is None:
                raise ApiException(404, "无 gate_issues 草稿（评审任务尚未回传）")
            if not d.md.strip():
                raise ApiException(400, "issues 草稿缺 md（issues 文件全文）")
            issues_raw = _parse(d.content_json).get("issues")
            if not is_json_list(issues_raw) or not issues_raw:
                raise ApiException(400, "issues 草稿 contentJson.issues 为空")

            meta = _review_meta(d.md)
            self._validate_review_meta(r.risk_tier, meta)
            self.repos.gate.set_review_meta(round_id, str(meta["reviewMode"]), int(meta["fanout"]),
                                            str(meta["contextMode"]))

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
                merged_unit = jint_or_400(n, "mergedUnit", 0) if is_json_obj(n) else 0
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
            if rnd.status != "pending":
                raise ApiException(409, "已放行轮只读，不能裁决")
            v = jrequired(body, "verdict").strip()
            if v not in _VERDICTS:
                raise ApiException(400, f"非法处置：{v}（revise|reject|query）")
            note = jtext(body, "note", "").strip()
            loc = jtext(body, "revLocation", "").strip()
            if v == "revise":
                if not loc:
                    raise ApiException(400, f"{issue.code} 落改必填落点（<文件+位置>）")
            elif not note:
                suffix = "理由（沉淀误报模式）" if v == "reject" else "去向"
                raise ApiException(400, f"{issue.code} {v}必填{suffix}")
            at = Times.now()
            self.repos.gate.apply_verdict(rnd.id or 0, issue.merged_unit, v, note,
                                          jtext(body, "by", "").strip(), at)
            if v == "revise":
                rev_status = jtext(body, "revStatus", "pending")
                if rev_status not in ("pending", "done"):
                    raise ApiException(400, f"非法修订状态：{rev_status}（pending|done）")
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
            if rnd.status != "pending":
                raise ApiException(409, "已放行轮只读")
            st = jrequired({"revStatus": rev_status}, "revStatus")
            if st not in ("pending", "done"):
                raise ApiException(400, "非法修订状态（pending|done）")
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
            if rnd.status != "pending":
                raise ApiException(409, "该轮已放行，不能重复放行")
            issues = self.repos.gate.list_issues(round_id)
            blocked: list[str] = []
            for g in issues:
                if g.filed == 1:
                    continue  # 备案区除外（skill 口径）
                if not g.verdict:
                    blocked.append(f"{g.code} 未裁决")
                elif g.verdict == "revise" and (not g.rev_location or g.rev_status != "done"):
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
        """仅「已放行后」出变更横幅；评审中变更走落改通道，不开新轮（GR-3 两窗口口径）。
        v4-16（§7）：需求侧变更判定 = 开闸时间之后存在 confirmed（不含 aborted——其 items 已逆向
        回滚，块与产物重新一致）变更单——**查变更单不比 rev**（rev 单调计数器非快照，回退的 rev
        与基线相等性比对互构误报），横幅带变更单号供点入 items；diff 数据来源 = req_change_item
        本身（rev 是计数器非快照，不从它重建）。"""
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
        if cur_cases > str(b.get("casesMaxUpdated", "")) and cur_cases:
            # 严格 >（P1-8）：基线取开闸时点快照，放行后无人改用例时两者相等——>= 会把「无变化」
            # 判成「已变更」，每次都误报复审。同秒内改用例仍会漏判（秒级时间戳的已知边界，记档接受）。
            reasons.append("用例在基线后有更新")
        out: dict[str, Any] = {"changed": bool(reasons), "reasons": reasons,
                               "window": "released" if latest.status == "released" else "in-review"}
        if latest.status == "released" and latest.released_at:
            req_changes = [c for c in self.repos.req_change.list_by_req(req_id)
                           # >=：开闸同秒 confirm 的变更单也计「内容已变更」（秒级时间戳保守取向）
                           if c.status == "confirmed" and (c.confirmed_at or "") >= (latest.released_at or "")]
            if req_changes:
                out["changed"] = True
                out["reqChangeIds"] = [c.id for c in req_changes]
                out["reasons"].append(
                    f"需求内容已变更（变更单 #{','.join(str(c.id) for c in req_changes)}，"
                    f"reqRev {int(b.get('reqRev') or 0)} → 现值）")
        return out

    @staticmethod
    def _validate_review_meta(tier: str, meta: dict[str, Any]) -> None:
        """P1-9（A+B 策略）：扇出留痕机械校验——A/B 级必须外置 runner 全新上下文扇出。
        未标注（存量 issues 均为迁移窗口前产物）放行，由通道 prompt/模板在新建产物时强制写。"""
        if not meta["present"]:
            return
        mode = str(meta["reviewMode"])
        ctx = str(meta["contextMode"])
        fanout = int(meta["fanout"])
        if mode not in ("external", "degraded"):
            raise ApiException(400, f"issues 头 reviewMode 非法：{mode or '空'}（external|degraded）")
        if ctx not in ("fresh", "single-session"):
            raise ApiException(400, f"issues 头 contextMode 非法：{ctx or '空'}（fresh|single-session）")
        if fanout < 0:
            raise ApiException(400, "issues 头 fanout 须为非负整数")
        tier_v = tier if tier in ("A", "B", "C") else "A"  # 未分级按 A（与 skill 分档规则同口径）
        if tier_v in ("A", "B"):
            need = 4 if tier_v == "A" else 2
            if mode == "degraded" or ctx != "fresh":
                raise ApiException(409, f"{tier_v} 级评审须用外置 runner 全新上下文扇出"
                                        f"（reviewMode=external / fanout≥{need} / contextMode=fresh）；"
                                        "进程内单会话降级评审仅限 C 级")
            if fanout < need:
                raise ApiException(409, f"{tier_v} 级评审扇出不足：fanout={fanout}（须 ≥{need}）")
        elif ctx != "single-session":
            raise ApiException(400, "C 级评审为主会话自查：contextMode 须为 single-session")

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
                    u["verdict"] = u["verdict"] or "filed"  # 备案区（skill 口径）
            out["issues"] = issue_blocks
            out["units"] = list(units.values())
        out["change"] = self.change_status(req_id)
        t = self.repos.task.latest_of_types(req_id, (task_mod.T_GATE,))  # 无滑窗（LIMIT 100 任务史溢出漏判）
        if t is not None:
            out["activeTask"] = {"id": t.id, "type": t.type, "status": t.status, "createdAt": t.created_at}
        out["phase"] = self._derive_phase(out, latest)
        return out

    def _derive_phase(self, out: dict[str, Any], latest: GateRound | None) -> str:
        """六态推导（UI 口径，非落库权威）：blocked/running/summary/deciding/released。"""
        if latest is None:
            return "blocked"
        if latest.status == "released":
            return "released"
        if out.get("issues"):
            return "deciding"
        at: dict[str, Any] = out.get("activeTask") or {}
        ts = at.get("status", "")
        return "summary" if ts == "done" else "running"

    def _round_block(self, g: GateRound) -> dict[str, Any]:
        return {"id": g.id, "roundNo": g.round_no, "status": g.status, "openedAt": g.opened_at,
                "releasedAt": g.released_at, "releasedBy": g.released_by,
                # P1-9 扇出留痕（ingest 时点回写；空值 = 迁移窗口前存量产物）
                "reviewMode": g.review_mode, "fanout": g.fanout, "contextMode": g.context_mode}

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





def _json(o: Any) -> str:
    return json.dumps(o, ensure_ascii=False, separators=(",", ":"))


def _review_meta(md: str) -> dict[str, Any]:
    """issues 头（首个 `## ` 标题前）解析 P1-9 扇出留痕三行：
    `| reviewMode | external \\| degraded |`、`| fanout | N |`、`| contextMode | fresh \\| single-session |`。
    三行全缺 → present=False（迁移窗口前存量产物，不校验不展示）。"""
    header = md.split("\n## ", 1)[0]

    def kv(field: str) -> str:
        m = re.search(rf"^\|\s*{re.escape(field)}\s*\|\s*([^|]*)\|", header, re.M)
        return m.group(1).strip() if m else ""

    fanout_raw = kv("fanout")
    try:
        fanout = int(fanout_raw) if fanout_raw else 0
    except ValueError:
        fanout = -1  # 非法值留给 _validate_review_meta 报 400
    mode, ctx = kv("reviewMode"), kv("contextMode")
    return {"reviewMode": mode, "fanout": fanout, "contextMode": ctx,
            "present": bool(mode or fanout_raw or ctx)}
