"""需求域服务（对应 RequirementService）：状态机守卫、推送产物守卫（重推保留答复）、发布终态锁。

视图（RequirementDetail/QualityView/CoverageView 等）以 camelCase dict 组装，
键序与 NON_NULL 语义对齐 Java DTO（None 键不输出）。
"""

from __future__ import annotations

import json
import logging
import re
from contextlib import suppress
from datetime import datetime
from typing import Any

from platform_sdlc.common import ApiException, Times, is_json_list, is_json_obj, jarray, jbool, jint, jrequired, jstrings, jtext
from platform_sdlc.db import Database
from platform_sdlc.models import Artifact, ClarifyItem, FrPoint, Requirement, string_list
from platform_sdlc.repositories import Repos
from platform_sdlc.schemas import wire
from platform_sdlc.services.block import BlockService
from platform_sdlc.services.iteration import NAME_MAX
from platform_sdlc.states import req_can_transition

log = logging.getLogger(__name__)

_A = "anchor"
_EMPTY_LIST: list[Any] = []


class RequirementService:
    def __init__(self, db: Database, repos: Repos, blocks: BlockService,
                 changes: Any = None) -> None:
        self.db = db
        self.repos = repos
        self.blocks = blocks  # v4.2：detail.doc 读面切源块表（空表回退 doc artifact）
        self.changes = changes  # v4.17：rebaseline 放行一次全量的豁免判定（§10）

    def list_all(self, iter_name: str | None, status: str | None) -> list[dict[str, Any]]:
        return [self._list_row_dict(r) for r in self.repos.requirement.list_all(iter_name, status)]

    @staticmethod
    def _list_row_dict(r: Any) -> dict[str, Any]:
        return {
            "id": r.id, "name": r.name, "iter": r.iter, "sourceType": r.source_type, "status": r.status,
            "submittedBy": r.submitted_by, "createdAt": r.created_at, "syncedAt": r.synced_at,
            "riskTier": r.risk_tier, "totalQ": r.total_q, "answeredQ": r.answered_q, "caseCount": r.case_count,
        }

    def delete(self, req_id: str) -> dict[str, Any]:
        """软删除（评测 P0-3）：deleted_at 打标，行与子表原样保留。running 任务先拒；
        queued 单条 bulk UPDATE 取消；tx 全程持锁堵「running 检查与取消之间被领取」的 TOCTOU 窗口。"""
        with self.db.tx():
            self.require(req_id)
            if self.repos.task.list(req_id, "running"):
                raise ApiException(409, "该需求有任务执行中，请等待任务完成后再删除")
            cancelled = self.repos.task.cancel_queued_by_req(
                req_id, None, '{"error":"需求已删除，任务自动作废","reason":"req-deleted"}', Times.now())
            if self.repos.requirement.soft_delete(req_id, Times.now()) == 0:
                raise ApiException(404, f"需求不存在或已删除：{req_id}")
        log.info("软删除需求 %s（取消 queued 任务 %d 个，行与子表保留）", req_id, cancelled)
        return {
            "ok": True,
            "deletedAt": Times.now(),
            "cancelledTasks": cancelled,
            "recoverHint": "软删除：行与子表保留，恢复仅需 UPDATE requirement SET deleted_at='' WHERE id=?",
        }

    def create(self, body: dict[str, Any]) -> Requirement:
        """建卡（v2.4 D32）：iter 非空时同事务完成 1:1 守卫 + 迭代自动建行。"""
        name = jrequired(body, "name")
        if len(name) > 120:
            raise ApiException(400, f"需求名称过长（≤120 字，当前 {len(name)} 字）")
        iter_name = jtext(body, "iter", "").strip()
        if len(iter_name) > NAME_MAX:
            raise ApiException(400, f"iter 过长（≤{NAME_MAX} 字，当前 {len(iter_name)} 字）")
        source_type = jtext(body, "sourceType", "text")
        if source_type not in ("feishu", "text"):
            raise ApiException(400, "sourceType 仅支持 feishu | text")
        source_url = jtext(body, "sourceUrl", "")
        source_text = jtext(body, "sourceText", "")
        if source_type == "feishu" and not source_url:
            raise ApiException(400, "飞书来源必须提供 sourceUrl")
        if source_type == "text" and not source_text:
            raise ApiException(400, "文本来源必须提供 sourceText")
        # v1.3 风险分级（D19）：契约键为下划线 risk_tier（Java @JsonProperty），可选白名单校验；建卡早于定级留空
        risk_tier = jtext(body, "risk_tier", "")
        if risk_tier and risk_tier not in ("A", "B", "C"):
            raise ApiException(400, f"非法 risk_tier：{risk_tier}（仅支持 A|B|C）")
        with self.db.tx():
            if iter_name:
                bound = self.repos.requirement.alive_id_by_iter(iter_name)
                if bound is not None:
                    raise ApiException(409, f"迭代 {iter_name} 已绑定需求 {bound}（迭代与需求 1:1，DESIGN-v2.4 D32）")
                self.repos.iteration.ensure(iter_name, Times.now())
            r = Requirement(
                id="", name=name, iter=iter_name, source_type=source_type, source_url=source_url,
                source_text=source_text, status="submitted", submitted_by=jtext(body, "submittedBy", ""),
                standard_doc="", created_at=Times.now(), synced_at=None, risk_tier=risk_tier,
            )
            r.id = self.repos.requirement.next_id(str(datetime.now().year))
            self.repos.requirement.insert(r)
        return r

    def push_artifacts(self, req_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """推送质检产物。机器行按来源合并（v4-9，DESIGN-v4 §6.3/§9）：重推删 skill+incremental 行、
        保留 local 行（评论升级 L 系经重推不丢）；已答保留按 seq+问题文本不变携带——seq 相同而
        问题文本已变（P 编号漂移）时不携带，防旧答复张冠李戴。tx 全程持锁。"""
        with self.db.tx():
            r = self.repos.requirement.find(req_id)
            if r is None:
                raise ApiException(404, f"需求不存在：{req_id}")
            if r.status == "published":
                raise ApiException(409, "该需求已发布标准文档；实质变更请新开一轮流程")
            if r.status == "changed" and not (self.changes and self.changes.rebaseline_open(req_id)):
                # v4-15（§5.1 矩阵）：changed 拒全量重推——既会被隐式推进吞掉 changed 态，又会按
                # 来源删除丢掉 incremental 接编的已答澄清项；原文消化只走增量通道（回 published
                # 后全量通道重新开放；增量不可行的兜底 = 放弃变更 §5.6）。v4-17 豁免：rebaseline
                # 单放行一次全量重推（§10——采纳即消化，重走全量流后重新发布回 published）
                raise ApiException(409, "该需求处于变更中（changed），全量重推已关闭——原文消化走增量"
                                        "质检合并，或放弃变更后重新开放（rebaseline 变更单例外）")

            digest = body.get("digest")
            audit = body.get("audit")
            items = body.get("items") or _EMPTY_LIST
            if not is_json_obj(digest) or not is_json_obj(audit):
                raise ApiException(400, "缺少 digest 或 audit")
            if not is_json_list(items):
                raise ApiException(400, "items 必须是数组")
            if not items:
                raise ApiException(400, "澄清清单 items 不能为空")

            seqs: set[str] = set()
            parsed: list[ClarifyItem] = []
            for n in items:
                seq = jrequired(n, "seq")
                # seq 是答复唯一键且进 REST 路径段：只允许字母/数字/._-（2026-09-30 回归）
                if not re.fullmatch(r"[A-Za-z0-9._-]{1,32}", seq):
                    raise ApiException(400, f"非法 seq：{seq}（仅字母/数字/._-，≤32 字）")
                level = jtext(n, "level", "yellow")
                if level not in ("red", "yellow", "blue"):
                    raise ApiException(400, f"非法 level：{level}（seq={seq}）")
                c = ClarifyItem(
                    id=0, req_id=req_id, seq=seq, level=level,
                    theme=jrequired(n, "theme"), question=jrequired(n, "question"),
                    ctx=jtext(n, "ctx", ""), answer=jtext(n, "answer", ""),
                    answered_at=None, quote=jtext(n, "quote", ""), anchor=jtext(n, "anchor", ""),
                    status="pending",  # 重建行基线（D21）：携带/skill 自带 answer 分支另行覆写
                    origin="skill",  # v4.1（DESIGN-v4 §6.3/§10）：重推产生的机器行来源标记，
                    # 与账本回填同批——否则首次重推把存量回填冲零，「按来源合并」匹配不到任何行
                )
                if seq in seqs:
                    raise ApiException(400, f"澄清清单 seq 重复：{seq}")
                seqs.add(seq)
                parsed.append(c)

            frs: list[FrPoint] = []
            fr_ids: set[str] = set()
            for n in jarray(digest, "fr_points"):
                p = FrPoint(
                    req_id=req_id, id=jrequired(n, "id"), name=jtext(n, "name", ""),
                    source=jtext(n, "source", ""), group=jtext(n, "group", ""),
                    module=jtext(n, "module", ""), anchor=jtext(n, "anchor", ""),
                )
                if p.id in fr_ids:
                    raise ApiException(400, f"需求点编号重复：{p.id}")
                fr_ids.add(p.id)
                frs.append(p)

            # 保留已有处置（A2/D23）：answer+status+note；问题文本已变 = P 编号漂移，不携带并计数
            existing = self.repos.clarify_item.disposition_map_by_seq(req_id)
            carried = 0
            changed = 0
            for c in parsed:
                old = existing.get(c.seq)
                if old is not None and not c.answer:
                    if old.question.strip() != c.question.strip():
                        changed += 1
                        continue
                    c.answer = old.answer
                    c.answered_at = old.answered_at
                    c.status = old.status
                    c.note = old.note
                    carried += 1
                elif c.answer:
                    c.answered_at = Times.now()
                    c.status = "answered"

            self.repos.clarify_item.delete_machine_by_req(req_id)
            self.repos.clarify_item.insert_many(parsed)  # 批量重建（AUDIT D2-4：循环逐条消除）

            at = Times.now()
            self.repos.artifact.upsert(req_id, "digest", jtext(digest, "md", ""), _json(self._digest_view(digest)), at)
            self.repos.artifact.upsert(req_id, "audit", jtext(audit, "md", ""), _json(self._audit_view(audit)), at)
            self.repos.fr_point.replace_all(req_id, frs)

            snapshot_updated = False
            snapshot = body.get("snapshot")
            snap_md = jtext(snapshot, "md", "") if is_json_obj(snapshot) else ""
            if snap_md.strip():
                self.repos.artifact.upsert(req_id, "snapshot", snap_md, "", at)
                snapshot_updated = True

            doc_updated = False
            missing_media = 0
            doc = body.get("doc")
            if is_json_obj(doc) and is_json_list(doc.get("blocks")) and doc.get("blocks"):
                doc_out: dict[str, Any] = {"blocks": doc.get("blocks"), "media": doc.get("media") or []}
                if doc.get("source") is not None:
                    doc_out["source"] = doc.get("source")
                if doc.get("generator") is not None:
                    doc_out["generator"] = doc.get("generator")
                wanted = [mid for mid in (jtext(mf, "id", "") for mf in doc_out["media"]) if mid]
                present = set(self.repos.media.ids_of(req_id, wanted))  # 一次 IN 查询（AUDIT D2-4）
                missing_media = len([mid for mid in wanted if mid not in present])
                # 宽松策略：doc 始终落库（缺失媒体由 missingMedia 如实计数，前端渲染占位）
                self.repos.artifact.upsert(req_id, "doc", "", _json(doc_out), at)
                doc_updated = True

            domains: list[str] = []
            for d in body.get("domains") or _EMPTY_LIST:
                t = (d or "").strip() if isinstance(d, str) else ""
                if t and t not in domains:
                    domains.append(t)
            if domains:
                self.repos.requirement.set_domains(req_id, _json(domains))

            tier = jtext(digest, "risk_tier", "").strip()
            if tier and tier not in ("A", "B", "C"):
                raise ApiException(400, f"非法 digest.risk_tier：{tier}（仅支持 A|B|C）")
            if tier:
                self.repos.requirement.set_risk_tier(req_id, tier)

            if r.status != "clarifying":
                self.repos.requirement.set_status(req_id, "clarifying")
            self.repos.requirement.touch_synced(req_id, at)

        log.info("推送质检产物 %s：%d 问 %d 点（保留答复 %d / 漂移 %d，缺媒体 %d）",
                 req_id, len(parsed), len(frs), carried, changed, missing_media)
        return {
            "ok": True, "items": len(parsed), "frPoints": len(frs), "carriedAnswers": carried,
            "changedQuestions": changed, "snapshotUpdated": snapshot_updated,
            "docUpdated": doc_updated, "missingMedia": missing_media,
        }

    def answers(self, req_id: str) -> list[dict[str, Any]]:
        self.require(req_id)
        out: list[dict[str, Any]] = []
        for c in self.repos.clarify_item.list_by_req(req_id):
            out.append({
                "seq": c.seq, "level": c.level, "theme": c.theme, "question": c.question,
                "answer": c.answer, "answeredAt": c.answered_at, "status": c.status, "note": c.note,
                "origin": c.origin,  # v4-9：P/L 两族呈现区分（skill/incremental=机器，local=评论升级）
            })
        return out

    def set_answer(self, req_id: str, seq: str, body: dict[str, Any]) -> None:
        """澄清项处置统一入口（D21/D2=A）：作答/驳回/挂起/重开共用；answered_at = 最近处置时间。"""
        r = self.require(req_id)
        if r.status not in ("clarifying", "changed"):
            # v4-15（§5.1 矩阵）：changed 下允许作答——只面对重开+新增项，未受影响项保留已答
            raise ApiException(409, f"仅待澄清/变更中状态可作答（当前：{r.status}）")
        ans = jtext(body, "answer", "")
        st = jtext(body, "status", "").strip()
        note = jtext(body, "note", "").strip()
        dispose = st in ("rejected", "deferred")
        reopen = st == "pending"  # 显式重开：驳回/挂起行转回待确认（answer 保留）
        if not dispose and not reopen and st and st != "answered":
            raise ApiException(400, f"非法 status：{st}（仅支持 rejected|deferred|pending，作答无需携带）")
        if st == "rejected" and not note:
            raise ApiException(400, "驳回必须填写理由（note）")
        if (dispose or reopen) and not ans:
            for c in self.repos.clarify_item.list_by_req(req_id):
                if c.seq == seq:
                    ans = c.answer
                    break
        final_status = st if dispose else ("pending" if reopen else ("answered" if ans else "pending"))
        if self.repos.clarify_item.set_disposition(req_id, seq, ans, final_status, note, Times.now()) == 0:
            raise ApiException(404, f"澄清问题不存在：{seq}")

    def publish(self, req_id: str, body: dict[str, Any]) -> dict[str, Any]:
        with self.db.tx():
            # 发布原子性：状态检查、artifact 写、置 published、清草稿同事务（原三语句间可交错出半发布态）
            r = self.require(req_id)
            if r.status not in ("clarifying", "changed"):
                # v4-15（§5.1）：发布入口扩 clarifying ∨ changed；changed 分支双重守卫在下方
                raise ApiException(409, f"仅待澄清/变更中状态可发布（当前：{r.status}）")
            counts = self.repos.clarify_item.counts(req_id)
            total, answered = counts["total"], counts["answered"]
            if (total == 0 or answered < total) and r.status == "clarifying":
                raise ApiException(400, f"存在未处置问题（{answered}/{total}），全部问题答复/驳回/挂起后才可发布（DESIGN-v2.2 D21）")
            if total != 0 and answered < total:
                # v4-15：changed 分支 0 问 = 全处置（纯 digest/FR 变更无澄清重开面——on_merged 的
                # 重合成断链正是为此类变更补的，发布守卫不能反过来堵死）
                raise ApiException(400, f"存在未处置问题（{answered}/{total}），全部问题答复/驳回/挂起后才可发布")
            md = jrequired(body, "standardMd")
            if r.status == "changed":
                # v4-15（§5.1 发布双重守卫，仅 changed 分支——clarifying 零新增校验保 verify-m2 契约）：
                # ①影响面已消化：不存在「影响面 digest/FR 非空 ∧ digest.pushed_at < confirmed_at」的
                #   confirmed 单（串行 confirm 保证同时至多一张未消化单，此式即全量校验）——否则人可在
                #   confirm 后不等增量质检直接发布未合并旧 digest；
                # ②重合成已发生：changed 下必须存在 standard 草稿且提交内容与其一致（哈希比对）——
                #   堵旧草稿趁增量刚合并发布过时文档（发布内容取自 body，平台校验其来源）
                _dg = self.repos.artifact.find(req_id, "digest")
                undigested = self.repos.req_change.confirmed_undigested(req_id, _dg.pushed_at if _dg else None)
                if undigested:
                    raise ApiException(409, f"变更单 #{undigested[0]} 影响面未消化（先完成增量质检与合并）")
                draft = self.repos.draft.find(req_id, "standard")
                if draft is None:
                    raise ApiException(409, "变更中发布前必须先重合成标准文档（L2 自动排 standard_doc，"
                                            "取消后可手动补建）")
                import hashlib
                if hashlib.sha256(md.encode("utf-8")).hexdigest() != hashlib.sha256(
                        draft.md.encode("utf-8")).hexdigest():
                    raise ApiException(409, "发布内容与最新草稿不符（body.standardMd 哈希 ≠ standard 草稿），"
                                            "请以草稿预览后发布")
            at = Times.now()
            self.repos.artifact.upsert(req_id, "standard", md, "", at)
            self.repos.requirement.set_standard(req_id, md, at)
            # 发布终态即清除标准文档草稿（草稿使命完成，避免已发布卡上残留「待发布」块）
            self.repos.draft.delete(req_id, "standard")
            log.info("发布标准文档 %s（%d/%d 问已处置）", req_id, answered, total)
            return {"ok": True, "publishedAt": at}

    def set_status(self, req_id: str, body: dict[str, Any]) -> None:
        target = jrequired(body, "status")
        r = self.require(req_id)
        if not req_can_transition(r.status, target):  # 流转表见 states.py（AUDIT D3-1 收口）
            raise ApiException(409, f"不允许的状态流转：{r.status} → {target}")
        self.repos.requirement.set_status(req_id, target)

    def detail(self, req_id: str) -> dict[str, Any]:
        r = self.require(req_id)
        items = self.repos.clarify_item.list_by_req(req_id)
        audit_view = self._artifact_view(req_id, "audit")

        # 用例域反向索引（读时聚合，不落库）：coveredBy=R1③、relatedCases=R2；RTM 覆盖率顺路收集（D20）
        frs = self.repos.fr_point.list_by_req(req_id)
        fr_ids = {p.id for p in frs}
        covered_by: dict[str, list[str]] = {}
        related_by: dict[str, list[str]] = {}
        case_total = 0
        case_linked = 0
        dangling_cases: list[str] = []
        for t in self.repos.test_case.list_by_req(req_id):
            case_total += 1
            linked = True
            valid_points: list[str] = []
            for fr_id in string_list(t.points):
                if not fr_id:
                    continue
                valid_points.append(fr_id)
                if fr_id not in fr_ids:
                    linked = False  # 悬空 FR 引用：不算有效追溯（coveredBy 键照记，静默容忍）
                covered_by.setdefault(fr_id, []).append(t.id)
            if not valid_points:
                linked = False  # 未挂需求点
            if linked:
                case_linked += 1
            else:
                dangling_cases.append(t.id)
            for ref in string_list(t.verify_refs):
                if ref:
                    related_by.setdefault(ref, []).append(t.id)

        # 体检条目补 relatedCases（含空数组，形状稳定）；未推产物的卡无 audit 视图，跳过
        if audit_view is not None and is_json_obj(audit_view.get("content")):
            audit_items = audit_view["content"].get("items")
            if is_json_list(audit_items):
                for it in audit_items:
                    if is_json_obj(it):
                        it["relatedCases"] = related_by.get(jtext(it, "id", ""), [])

        out: dict[str, Any] = {
            "card": wire(r),
            "items": [wire(c) for c in items],
            "digest": self._artifact_view(req_id, "digest"),
            "audit": audit_view,
        }
        snap = self.repos.artifact.find(req_id, "snapshot")
        if snap is not None:
            out["snapshot"] = {"md": snap.markdown, "at": snap.pushed_at, "version": snap.version}
        # v4.2 读面切源（DESIGN-v4 §2.3）：块表非空 → 块表组装（权威）；空且 doc artifact 存在 → 回退
        # （未导入卡/verify 夹具过渡安全网——doc artifact 写面已随块表权威退役，仅存读兜底）
        out["doc"] = self.blocks.doc_view(req_id) or self._artifact_view(req_id, "doc")
        out["domains"] = self._resolve_domains(req_id, audit_view, items)

        fr_maps: list[dict[str, Any]] = []
        for p in frs:
            fr_maps.append({
                "id": p.id, "name": p.name, "source": p.source, "group": p.group,
                "module": p.module, "anchor": p.anchor, "coveredBy": covered_by.get(p.id, []),
            })
        out["frCount"] = len(frs)
        out["frPoints"] = fr_maps
        out["coverage"] = self._coverage_view(frs, covered_by, case_total, case_linked, dangling_cases)
        out["quality"] = self._quality_view(audit_view)
        counts = self.repos.clarify_item.counts(req_id)
        out["answered"] = counts["answered"]
        out["totalQ"] = counts["total"]
        std = self.repos.artifact.find(req_id, "standard")
        if std is not None:
            out["standard"] = {"markdown": std.markdown, "version": std.version, "pushedAt": std.pushed_at}
        return {k: v for k, v in out.items() if v is not None}  # NON_NULL：null 键不输出（既有契约）

    def fr_points(self, req_id: str) -> dict[str, Any]:
        self.require(req_id)
        return {"reqId": req_id, "points": [p.id for p in self.repos.fr_point.list_by_req(req_id)]}

    def require(self, req_id: str) -> Requirement:
        r = self.repos.requirement.find(req_id)
        if r is None:
            raise ApiException(404, f"需求不存在：{req_id}")
        return r

    # ---------- 私有：视图组装（键序/缺省对齐 Java） ----------

    def _quality_view(self, audit_view: dict[str, Any] | None) -> dict[str, Any] | None:
        """星级质量分（D24，口径 D1=A）：一律从 items[].level 现场数——顶部计数缺省即 0，无法区分真 0 与缺失。
        blue 不计入；无 audit 产物 → None（NON_NULL 不输出）。5=无阻断无严重｜4=无阻断且严重≤5｜3=阻断≤2｜2=阻断≤5｜1=其余。"""
        if audit_view is None or not isinstance(audit_view.get("content"), dict):
            return None
        items = audit_view["content"].get("items")
        if not is_json_list(items) or not items:
            return None
        red = sum(1 for it in items if is_json_obj(it) and jtext(it, "level", "") == "red")
        yellow = sum(1 for it in items if is_json_obj(it) and jtext(it, "level", "") == "yellow")
        if red == 0 and yellow == 0:
            stars = 5
        elif red == 0 and yellow <= 5:
            stars = 4
        elif red <= 2:
            stars = 3
        elif red <= 5:
            stars = 2
        else:
            stars = 1
        basis = f"阻断 {red} 项" if red > 0 else f"无阻断、严重 {yellow} 项"
        return {"stars": stars, "basis": basis}

    def _coverage_view(self, frs: list[FrPoint], covered_by: dict[str, list[str]],
                       case_total: int, case_linked: int, dangling_cases: list[str]) -> dict[str, Any]:
        """RTM 覆盖率（D20）：total=0 时 pct=None——「未登记」与「0% 覆盖」是两件事。"""
        uncovered = [{"id": p.id, "name": p.name, "group": p.group}
                     for p in frs if not covered_by.get(p.id)]
        forward = {
            "covered": len(frs) - len(uncovered), "total": len(frs),
            "pct": None if not frs else round((len(frs) - len(uncovered)) * 1000.0 / len(frs)) / 10.0,
            "uncovered": uncovered,
        }
        backward = {
            "linked": case_linked, "total": case_total,
            "pct": None if case_total == 0 else round(case_linked * 1000.0 / case_total) / 10.0,
        }
        return {"forward": forward, "backward": backward, "danglingCases": dangling_cases}

    def _resolve_domains(self, req_id: str, audit_view: dict[str, Any] | None,
                         items: list[ClarifyItem]) -> list[str]:
        """全局需求域顺序：库列优先，空则按 audit group → 澄清 theme 首现顺序推导（§1.4）。"""
        out = string_list(self.repos.requirement.get_domains(req_id))
        if out:
            return out
        if audit_view is not None and is_json_obj(audit_view.get("content")):
            audit_items = audit_view["content"].get("items")
            if is_json_list(audit_items):
                for it in audit_items:
                    g = jtext(it, "group", "")
                    if g and g not in out:
                        out.append(g)
        for c in items:
            if c.theme and c.theme not in out:
                out.append(c.theme)
        return out

    def _digest_view(self, digest: dict[str, Any]) -> dict[str, Any]:
        features = [{"role": jtext(f, "role", ""), "m": jtext(f, "m", ""), "func": jtext(f, "func", ""),
                     "cov": jtext(f, "cov", ""), "src": jtext(f, "src", ""), "anchor": jtext(f, _A, "")}
                    for f in jarray(digest, "features")]
        flows = [{"role": jtext(f, "role", ""), "step": jtext(f, "step", ""), "next": jtext(f, "next", ""),
                  "gap": jbool(f, "gap", False), "src": jtext(f, "src", ""), "anchor": jtext(f, _A, "")}
                 for f in jarray(digest, "flows")]
        states = [{"obj": jtext(s, "obj", ""), "from": jtext(s, "from", ""), "on": jtext(s, "on", ""),
                   "to": jtext(s, "to", ""), "gap": jbool(s, "gap", False), "src": jtext(s, "src", ""),
                   "anchor": jtext(s, _A, "")}
                  for s in jarray(digest, "states")]
        return {"summary": jtext(digest, "summary", ""), "roles": jstrings(digest, "roles"),
                "features": features, "flows": flows, "states": states}

    def _audit_view(self, audit: dict[str, Any]) -> dict[str, Any]:
        items = [{"id": jtext(i, "id", ""), "level": jtext(i, "level", "yellow"), "title": jtext(i, "title", ""),
                  "where": jtext(i, "where", ""), "go": jtext(i, "go", ""), "group": jtext(i, "group", ""),
                  "quote": jtext(i, "quote", ""), "anchor": jtext(i, _A, "")}
                 for i in jarray(audit, "items")]
        return {"red": jint(audit, "red", 0), "yellow": jint(audit, "yellow", 0),
                "blue": jint(audit, "blue", 0), "items": items}

    def _artifact_view(self, req_id: str, type_: str) -> dict[str, Any] | None:
        a: Artifact | None = self.repos.artifact.find(req_id, type_)
        if a is None:
            return None
        view: dict[str, Any] = {"markdown": a.markdown, "version": a.version, "pushedAt": a.pushed_at}
        if a.content_json:
            with suppress(Exception):
                view["content"] = json.loads(a.content_json)  # 解析失败无 content 键（契约微变已登记）
        return view


def _json(o: Any) -> str:
    return json.dumps(o, ensure_ascii=False, separators=(",", ":"))
