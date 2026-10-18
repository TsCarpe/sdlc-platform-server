"""v4.11 变更单服务（DESIGN-v4 §5.2/§5.3/§5.4）：draft 期数据面——建/存/删/读 + 影响面预览。

draft 不碰块表（随时可提，不限需求状态——为 confirm 做准备）；items 由全量块数组经
BlockService.diff_blocks 得出（diff 只在服务端一份，v4-7 落地三期复用），反复保存全量重算替换；
insert 块草稿期即有平台 ID（blk- 沿用不漂移）。影响面预览机械 join 现算不冻结（impact_json 仅
confirm 时冻结，v4-12）：edit/move 只标直接锚定行、delete 子孙按 parent 递归 CTE 一次查询、
heading 删不带章节体——宁可漏报不可误报，漏的由人在确认界面补勾。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

from platform_sdlc.common import (
    ApiException,
    Times,
    answer_text_of,
    audit_item_view,
    digest_feature_view,
    digest_flow_view,
    digest_state_view,
    is_json_list,
    is_json_obj,
    jarray,
    jint_or_400,
    jrequired,
    jstrings,
    jtext,
    normalize_layer,
    option_view,
)
from platform_sdlc.db import Database
from platform_sdlc.models import ReqChange, ReqChangeItem, TestCase
from platform_sdlc.repositories import Repos
from platform_sdlc.services.block import BlockService

log = logging.getLogger(__name__)


class ChangeService:
    def __init__(self, db: Database, repos: Repos, blocks: BlockService) -> None:
        self.db = db
        self.repos = repos
        self.blocks = blocks

    # ---------- draft CRUD（§5.3） ----------

    def create(self, req_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """建 draft：元信息 + 全量块数组一次到位（items 立即 diff 落库）。proposedBy 手填必填
        （§5.2 不设审批环，防误操作不防故意）；draft 不碰块表，不限需求状态。"""
        with self.db.tx():
            self._require_base(req_id)
            items, blocks_json = self._diff_items(req_id, body)
            change_id = self.repos.req_change.insert(ReqChange(
                id=None, req_id=req_id, title=jtext(body, "title", ""), detail=jtext(body, "detail", ""),
                proposed_by=jrequired(body, "proposedBy"), status="draft",
                blocks_json=blocks_json, created_at=Times.now()))
            self.repos.req_change.replace_items(change_id, items)
        log.info("变更单 draft #%s %s：%s", change_id, req_id, _counts(items))
        return self.detail(req_id, change_id)

    def save(self, req_id: str, change_id: int, body: dict[str, Any]) -> dict[str, Any]:
        """反复保存：items 全量重算替换（§5.3），元信息缺省沿用；仅 draft 可存
        （confirmed 是冻结快照，作废走 §5.6 放弃动作——v4-13）。"""
        with self.db.tx():
            self._require_base(req_id)
            c = self._require_change(req_id, change_id)
            if c.status != "draft":
                raise ApiException(409, f"变更单 #{change_id} 已{c.status}，不可再编辑（confirmed 是冻结快照）")
            items, blocks_json = self._diff_items(req_id, body)
            self.repos.req_change.update_draft_meta(
                change_id, jtext(body, "title", c.title), jtext(body, "detail", c.detail),
                jtext(body, "proposedBy", "") or c.proposed_by, blocks_json)
            self.repos.req_change.replace_items(change_id, items)
        return self.detail(req_id, change_id)

    def delete(self, req_id: str, change_id: int) -> dict[str, Any]:
        """draft 可直接删（未碰块表零代价，§5.6）；confirmed 不作废——删除通道仅对 draft 开放。"""
        with self.db.tx():
            self._require_base(req_id)
            c = self._require_change(req_id, change_id)
            if c.status != "draft":
                raise ApiException(409, f"变更单 #{change_id} 已{c.status}，不可删除（confirmed 不作废，修正走新 draft）")
            self.repos.req_change.delete(req_id, change_id)
        return {"ok": True}

    def list(self, req_id: str) -> list[dict[str, Any]]:
        self._require_base(req_id)
        out: list[dict[str, Any]] = []
        for c in self.repos.req_change.list_by_req(req_id):
            row = {"id": c.id, "kind": c.kind, "title": c.title, "detail": c.detail,
                   "proposedBy": c.proposed_by, "confirmedBy": c.confirmed_by, "status": c.status,
                   "abortedBy": c.aborted_by, "createdAt": c.created_at,
                   "confirmedAt": c.confirmed_at, "abortedAt": c.aborted_at}
            out.append({k: v for k, v in row.items() if v is not None})
        return out

    def detail(self, req_id: str, change_id: int) -> dict[str, Any]:
        self._require_base(req_id)
        c = self._require_change(req_id, change_id)
        items = self.repos.req_change.list_items(change_id)
        return {
            "id": c.id, "reqId": c.req_id, "kind": c.kind, "title": c.title, "detail": c.detail,
            "proposedBy": c.proposed_by, "confirmedBy": c.confirmed_by, "status": c.status,
            "abortedBy": c.aborted_by, "createdAt": c.created_at,
            "confirmedAt": c.confirmed_at, "abortedAt": c.aborted_at,
            # draft 重编辑数据源（blocks_json 留底原样带出；confirmed/aborted 为当时快照，只读呈现）
            "blocks": _load_raw_list(c.blocks_json),
            "items": [{"blockId": i.block_id, "op": i.op, "oldText": i.old_text, "newText": i.new_text,
                       "oldAttrs": _load_or_none(i.old_attrs), "newAttrs": _load_or_none(i.new_attrs)}
                      for i in items],
            "counts": _counts(items),
            **({"impact": _load_or_none(c.impact_json)} if c.impact_json else {}),  # confirmed 冻结面
        }

    # ---------- confirm = 一个事务（§5.5；changed 流转不进 REQ_FLOW，表外三通道之一） ----------

    def confirm(self, req_id: str, change_id: int, body: dict[str, Any]) -> dict[str, Any]:
        """confirm 事务：①守卫（状态 ∈ {published,changed} ∧ 无活跃增量任务/未采纳增量草稿 ∧ 无
        影响面未消化的前序 confirmed 单——串行）；②items 应用块表（edit/move version+1、insert、
        delete 软删、blocks_json 留底数组整体重排 seq）+ 乐观锁（edit/delete/move 项 old_text ==
        块表现值，不符整单 409——连环 confirm 场景 draft 基于旧块态，直接应用会无感覆盖）；③
        impact_json 冻结（body.impact 携带 = 确认界面人工增删结果，缺省 = 机械预览值）；
        ④content_rev+1 + published→changed（表外直写）；⑤受影响澄清重开（旧答复挪 note 前缀，
        answer 清空回 pending）。钩子（事务后）on_change_confirmed 排增量质检——v4-14 接任务类型。"""
        with self.db.tx():
            r = self.repos.requirement.find(req_id)
            if r is None:
                raise ApiException(404, f"需求不存在：{req_id}")
            c = self._require_change(req_id, change_id)
            if c.status != "draft":
                raise ApiException(409, f"变更单 #{change_id} 已{c.status}，不可确认")
            if r.status not in ("published", "changed") and c.kind != "reconcile":
                raise ApiException(409, f"仅已发布需求可确认变更单（当前：{r.status}）；"
                                        "未发布卡的原文变更走窗口 1 直接编辑（reconcile 对账单豁免——"
                                        "应用+保持原状态，§5.1/§10）")
            # 活跃增量任务 / 未采纳增量·全量草稿（产物合并时 anchor 已漂移）
            if self.repos.task.count_active(req_id, "incremental_check") > 0:
                raise ApiException(409, "存在排队中/执行中的增量质检任务，请等待完成后再确认")
            for dtype in ("incremental_bundle", "digest_bundle"):
                if self.repos.draft.find(req_id, dtype) is not None:
                    raise ApiException(409, f"存在未采纳的 {dtype} 草稿（产物锚点将随块变更漂移），"
                                            "请先采纳或作废再确认")
            digest = self.repos.artifact.find(req_id, "digest")
            undigested = self.repos.req_change.confirmed_undigested(req_id, digest.pushed_at if digest else None)
            if undigested:
                raise ApiException(409, f"前序变更单 #{undigested[0]} 影响面未消化（先完成增量质检与"
                                        "合并），串行确认")
            confirmed_by = jrequired(body, "confirmedBy")

            # ③影响面冻结（body.impact = 确认界面人工增删结果，缺省机械预览）——在块表应用**前**
            # 计算：delete 子孙 CTE 与锚定 join 基于确认时点块态（应用后 delete 块已软删、锚定悬空）
            body_impact = body.get("impact")
            impact: dict[str, Any] = (body_impact if is_json_obj(body_impact)
                                      else self.preview_impact(req_id, change_id))
            if c.kind == "rebaseline":
                # §10：rebaseline 影响面 = 全卡（空 anchor 存量行无区间归属，全量重质检重建）
                all_live = self.repos.req_block.list_by_req(req_id)
                impact = {**impact, "blocks": [b.block_id for b in all_live],
                          "counts": {**impact.get("counts", {}),
                                     "blocks": len(all_live), "rebaseline": True}}

            # ②乐观锁 + 应用：old_text 逐条校验后按 blocks_json 留底数组整体应用
            items = self.repos.req_change.list_items(change_id)
            current = self.repos.req_block.list_by_req(req_id)
            cur_by_id = {b.block_id: b for b in current}
            for it in items:
                if it.op == "insert":
                    if it.block_id in cur_by_id:
                        raise ApiException(409, f"insert 块 {it.block_id} 已存在于块表（基线漂移），"
                                                "请重新编辑 draft")
                    continue
                cur = cur_by_id.get(it.block_id)
                if cur is None or cur.text != it.old_text:
                    raise ApiException(409, f"块 {it.block_id} 已被后续修改（变更单基于旧基线），"
                                            "请重新编辑 draft")
            if not c.blocks_json:
                raise ApiException(409, f"变更单 #{change_id} 无块数组留底（旧版本数据），请重新保存 draft")
            submitted = self.blocks.parse_blocks({"blocks": _load_raw_list(c.blocks_json)})
            self.blocks.diff_blocks(req_id, submitted, current)  # 打标（is_new/changed/moved）
            self.blocks.assign_insert_ids(req_id, submitted)
            self.blocks.apply_edits(req_id, submitted, current, confirmed_by)

            at = Times.now()
            impact_json = json.dumps(impact, ensure_ascii=False, separators=(",", ":"))
            if self.repos.req_change.set_confirmed(change_id, impact_json, confirmed_by, at) == 0:
                raise ApiException(409, "确认竞态：变更单状态已变化")

            # ④content_rev+1 + changed（表外通道：不经 REQ_FLOW/set_status 通用路由——携带事务
            # 副作用的流转走专用动作，通用 set_status 成为守卫旁路的口子不能开）。reconcile 对账单
            # 在未发布卡上豁免状态转换（保持原状态——changed 是「发布后发现变更」的语义，§10）
            self.repos.requirement.bump_content_rev(req_id)
            if r.status == "published":
                self.repos.requirement.set_status(req_id, "changed")

            # ⑤受影响澄清重开（冻结影响面口径；已答/驳回/挂起行回 pending，旧答复挪 note 前缀）
            reopen, reopen_snapshot = self._reopen_rows(req_id, change_id, impact)
            self.repos.clarify_item.reopen_items(req_id, reopen)
            # §5.6 放弃的全量逆向回滚依据：baseline 块序（应用前）+ 重开原值快照
            snapshot = {"baseline": [[b.block_id, b.seq] for b in current], "reopen": reopen_snapshot}
            self.repos.req_change.set_snapshot(change_id, json.dumps(snapshot, ensure_ascii=False,
                                                                    separators=(",", ":")))
        log.info("变更单 #%s confirm %s（by=%s，重开澄清 %d 项）", change_id, req_id, confirmed_by, len(reopen))
        out = self.detail(req_id, change_id)
        after = self.repos.requirement.find(req_id)
        out["contentRev"] = after.content_rev if after is not None else 0
        return out

    # ---------- v4-17（DESIGN-v4 §10）：存量对账重导 ----------

    def create_reconcile(self, req_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """对账重导入口（§10）：新解析结果（全量块数组 + 可选评论）→ 服务端 diff 机械生成系统
        draft——不覆盖任何平台数据，人过 diff 后 confirm（对账式永远合法，不受 §2.4 封存约束——
        全量替换式才被封存）。kind：reconcile（缺省，未发布卡 confirm = 应用 + **保持原状态**，
        §5.1 状态域豁免——changed 是「发布后发现变更」的语义，未发布卡不需要）；rebaseline
        （已发布 text 存量卡：影响面 = 全卡，confirm 后**该单放行一次全量质检**——空 anchor 存量行
        在任何状态下都无重建通道，本 kind 即为其开的门，豁免挂单 kind 不动 changed 拒全量的全局守卫）。
        评论按 external_id 幂等 upsert（保原行 id 与 resolved 态——同块同作者同文本的重复发言会使
        组合键定位不确定，external_id 是唯一稳定键）。"""
        with self.db.tx():
            self._require_base(req_id)
            kind = jtext(body, "kind", "reconcile")
            if kind not in ("reconcile", "rebaseline"):
                raise ApiException(400, f"非法 kind：{kind}（对账单仅支持 reconcile|rebaseline）")
            items, blocks_json = self._diff_items(req_id, body)
            if not items:
                raise ApiException(409, "对账 diff 为空（新解析结果与块表一致，无需对账单）")
            change_id = self.repos.req_change.insert(ReqChange(
                id=None, req_id=req_id, kind=kind, title=jtext(body, "title", "迁移对账"),
                detail=jtext(body, "detail", "服务端 diff 机械生成；对账式重导不覆盖平台数据"),
                proposed_by=jtext(body, "proposedBy", "迁移对账"), status="draft",
                blocks_json=blocks_json, created_at=Times.now()))
            self.repos.req_change.replace_items(change_id, items)
            self._upsert_feishu_comments(req_id, body)
        log.info("对账单 draft #%s（kind=%s）%s：%s", change_id, kind, req_id, _counts(items))
        return self.detail(req_id, change_id)

    def _upsert_feishu_comments(self, req_id: str, body: dict[str, Any]) -> int:
        """评论幂等 upsert（§10）：external_id 唯一定位——已有行保留原 id/resolved/created_at（只刷
        文本面），新行插入；不删除平台侧未在本次清单中的行（对账不收缩平台数据）。"""
        raw = body.get("comments")
        n = 0
        for c in _arr(raw):
            if not is_json_obj(c):
                continue
            ext = jtext(c, "externalId", "") or jtext(c, "id", "")
            if not ext:
                continue
            if jtext(c, "origin", "") == "platform":
                # 平台自身评论回环（导出体打标，前端 parseRecJson 透传）：不作为 feishu 评论
                # 导入——否则原 platform 行（external_id 空）永不匹配幂等键，同评论复制成双份
                continue
            self.repos.req_comment.upsert_feishu(
                req_id, ext, jtext(c, "blockId", "") or jtext(c, "anchorBlockId", ""),
                jtext(c, "author", ""), jtext(c, "text", ""), Times.now())
            n += 1
        return n

    def rebaseline_open(self, req_id: str) -> bool:
        """存在未消化的 confirmed rebaseline 单（§10 豁免判定：changed 拒全量的三处守卫据此放行
        一次——采纳即消化（push 刷 pushed_at 后自然解除），不动全局守卫）。"""
        dg = self.repos.artifact.find(req_id, "digest")
        undigested = self.repos.req_change.confirmed_undigested(req_id, dg.pushed_at if dg else None)
        by_id = {c.id: c for c in self.repos.req_change.list_by_req(req_id)}
        return any(by_id.get(i) is not None and by_id[i].kind == "rebaseline" for i in undigested)

    # ---------- v4-14（DESIGN-v4 §6）：增量质检守卫 / selfCheck 机械校验 / 章节级机械合并 ----------

    def guard_incremental(self, r: Any) -> None:
        """incremental_check 创建守卫（§5.5 第 6 步人工逃生门）：changed ∧ 存在 confirmed 单 ∧
        影响面非空（无影响面的单没有可增量处理的面）。"""
        confirmed = [x for x in self.repos.req_change.list_by_req(r.id) if x.status == "confirmed"]
        if not confirmed:
            raise ApiException(409, "无 confirmed 变更单，增量质检无从取影响面（先确认变更单）")
        c = confirmed[0]
        impact_v = _load_or_none(c.impact_json)
        counts_v: Any = impact_v.get("counts") if is_json_obj(impact_v) else None
        counts: dict[str, Any] = dict(counts_v) if is_json_obj(counts_v) else {}
        n = sum(int(counts.get(k) or 0) for k in ("blocks", "digestRows", "frPoints", "clarify", "cases"))
        if n == 0:
            raise ApiException(409, f"变更单 #{c.id} 影响面为空，无增量质检面（走全量或放弃变更）")

    def validate_bundle(self, req_id: str, content_json: str) -> dict[str, Any]:
        """incremental_bundle 的 selfCheck 机械校验（§6.1，put_draft 服务端单点——runner/进程内
        两通道的草稿都经此处）：①章节区间 = heading 的 block_id，禁 seq 数值（纯数字/非 heading 集
        即拒——整体重排会改写全表 seq，冻结数值即失真）；②新行 anchor 全部 ∈ 受影响块集 ∩ 存活块集
        （防 LLM 越界重写；已删块不是合法新锚点——落地即悬空僵尸行，v4.19）；③作废清单的行真实存在
        （digest 行按 anchor、audit 条目按 id）。"""
        try:
            bundle: Any = json.loads(content_json)
        except json.JSONDecodeError as e:
            raise ApiException(400, f"incremental_bundle 不是合法 JSON：{e}") from e
        if not is_json_obj(bundle):
            raise ApiException(400, "incremental_bundle 必须是 JSON 对象")
        change_id = jint_or_400(bundle, "changeId", None)
        if change_id is None:
            raise ApiException(400, "缺少必填字段：changeId")
        c = self.repos.req_change.find(req_id, change_id)
        if c is None or c.status != "confirmed":
            raise ApiException(400, f"changeId={change_id} 不是本卡 confirmed 变更单")
        impact = _load_or_none(c.impact_json)
        impact_d: dict[str, Any] = dict(impact) if is_json_obj(impact) else {}
        target = {str(b) for b in _arr(impact_d.get("blocks"))}
        if not target:
            raise ApiException(400, "变更单影响面为空，无增量面")

        blocks = self.repos.req_block.list_by_req(req_id)
        live_ids = {b.block_id for b in blocks}
        heading_ids = {b.block_id for b in blocks if b.type == "heading"}
        sections = [str(x) for x in _arr(bundle.get("sections"))]
        if not sections:
            raise ApiException(400, "sections 不能为空（受影响章节，heading block_id 表示）")
        for sec in sections:
            if sec not in heading_ids:
                raise ApiException(400, f"sections 值 {sec} 不是块表 heading 的 block_id"
                                        "（区间表示禁 seq 数值，须用 heading block_id）")

        rows_v = bundle.get("digestRows")
        rows: dict[str, Any] = dict(rows_v) if is_json_obj(rows_v) else {}
        anchors: set[str] = set()
        for key in ("features", "flows", "states", "auditItems", "frUpserts"):
            for f in _arr(rows.get(key)):
                if is_json_obj(f):
                    anchors.add(jtext(f, "anchor", ""))
        cl_v = bundle.get("clarify")
        for it in _arr(cl_v.get("items")) if is_json_obj(cl_v) else []:
            if is_json_obj(it):
                anchors.add(jtext(it, "anchor", ""))
        out_of_range = sorted(a for a in anchors if a and a not in target)
        if out_of_range:
            raise ApiException(400, f"新行 anchor 越界（不在受影响块集）：{out_of_range[:5]}"
                                    f"（共 {len(out_of_range)} 个，防越界重写）")
        # v4.19：合法锚点域再交存活块集——target 含 confirm 已软删的 delete 块（影响面在应用前
        # 冻结），新行锚上去 = 落地即悬空，且软删块永不进未来变更单的替换域（僵尸行，唯全量
        # 重推可清）。替换行应锚定存留/新增块。
        dead_anchored = sorted(a for a in anchors if a and a in target and a not in live_ids)
        if dead_anchored:
            raise ApiException(400, f"新行 anchor 锚定本单已删除的块：{dead_anchored[:5]}"
                                    f"（共 {len(dead_anchored)} 个，替换行须锚定存留/新增块）")

        inv_v = bundle.get("invalidate")
        inv: dict[str, Any] = dict(inv_v) if is_json_obj(inv_v) else {}
        digest = self.repos.artifact.find(req_id, "digest")
        digest_anchors: set[str] = set()
        if digest is not None and digest.content_json:
            dv_v = _load_or_none(digest.content_json)
            dv: dict[str, Any] = dict(dv_v) if is_json_obj(dv_v) else {}
            for key in ("features", "flows", "states"):
                for f in _arr(dv.get(key)):
                    if is_json_obj(f):
                        digest_anchors.add(jtext(f, "anchor", ""))
        for a in _arr(inv.get("digestAnchors")):
            if str(a) not in digest_anchors:
                raise ApiException(400, f"作废清单 anchor 无对应 digest 行：{a}（清单里的行必须真实存在）")
        audit = self.repos.artifact.find(req_id, "audit")
        audit_ids: set[str] = set()
        if audit is not None and audit.content_json:
            av_v = _load_or_none(audit.content_json)
            av: dict[str, Any] = dict(av_v) if is_json_obj(av_v) else {}
            for it in _arr(av.get("items")):
                if is_json_obj(it):
                    audit_ids.add(jtext(it, "id", ""))
        for i in _arr(inv.get("auditIds")):
            if str(i) not in audit_ids:
                raise ApiException(400, f"作废清单 audit 条目不存在：{i}（清单里的行必须真实存在）")
        # FR 作废清单存在性（digestRows.frRemoves 主位 + frPoints.remove 兼容）——防 LLM 幻觉 id
        fp_v = bundle.get("frPoints")
        fr_remove = [str(x) for x in (_arr(rows.get("frRemoves"))
                                      + (_arr(fp_v.get("remove")) if is_json_obj(fp_v) else []))]
        if fr_remove:
            fr_ids = {fp.id for fp in self.repos.fr_point.list_by_req(req_id)}
            ghost = [x for x in fr_remove if x not in fr_ids]
            if ghost:
                raise ApiException(400, f"作废清单 FR 不存在：{ghost[:5]}（清单里的行必须真实存在）")
        return dict(bundle)

    def merge_incremental(self, req_id: str, d: Any, by: str) -> dict[str, Any]:
        """采纳 = 服务端纯机械合并一个事务（§6.2）：区间整组替换（anchor 落区间的旧行删、新行写入；
        空 anchor/global 行永不触碰）、FR 区间 upsert+remove、机器澄清按区间替换（local 永不动）、
        audit 同 digest 规则、md 整篇重渲染（md 退化为展示物，权威在结构行）、summary/riskTier 覆写、
        artifact.upsert version+1 且 pushed_at 刷新（§5.1 发布守卫「合并也是一次推送」依赖此语义）。
        LLM 产物不直接进正式面——本合并是人审采纳后的机械执行，草稿先行红线原样。"""
        with self.db.tx():
            r = self.repos.requirement.find(req_id)
            if r is None:
                raise ApiException(404, f"需求不存在：{req_id}")
            if r.status != "changed":
                raise ApiException(409, f"增量合并属变更后流程（当前：{r.status}）")
            bundle: dict[str, Any] = self.validate_bundle(req_id, d.content_json)
            change_id = int(bundle["changeId"])
            blocks = self.repos.req_block.list_by_req(req_id)
            section_ids = _section_block_ids(blocks, [str(x) for x in _arr(bundle.get("sections"))])
            # 锚定已删块的行（change confirm 软删，活块集区间扫描不可见）并入替换域——否则永无
            # 消化通道；判定来源 = 冻结影响面块集 - 活块集
            change_row = self.repos.req_change.find(req_id, change_id)
            impact_raw_v = _load_or_none(change_row.impact_json) if change_row is not None else None
            frozen_blocks: set[str] = ({str(b) for b in _arr(impact_raw_v.get("blocks"))}
                                       if is_json_obj(impact_raw_v) else set())
            live_ids = {b.block_id for b in blocks}
            section_ids |= {b for b in frozen_blocks if b not in live_ids}  # noqa: C405 – 集合推导保持显式
            rows_v = bundle.get("digestRows")
            rows: dict[str, Any] = dict(rows_v) if is_json_obj(rows_v) else {}
            inv_v = bundle.get("invalidate")
            inv: dict[str, Any] = dict(inv_v) if is_json_obj(inv_v) else {}
            inv_anchors = {str(x) for x in _arr(inv.get("digestAnchors"))}
            inv_audit = {str(x) for x in _arr(inv.get("auditIds"))}

            digest = self.repos.artifact.find(req_id, "digest")
            dv_raw = _load_or_none(digest.content_json) if digest is not None else None
            dv: dict[str, Any] = dict(dv_raw) if is_json_obj(dv_raw) else {
                "summary": "", "roles": [], "features": [], "flows": [], "states": []}
            stats = {"removedRows": 0, "newRows": 0}
            # v1.6（D-K + 三期 D-I）：三键 new 行各过共享视图白名单归一（flows 含 pids 数组化、
            # features/states 剥未知私货键——契约见 payload-schema incremental_bundle 节）；
            # view 在调用点按 key 传（函数内无条件应用会串键集）；kept 行不改写（空 anchor/global 行
            # 「增量合并永不触碰」承诺，混合口径由 rebaseline/重推自然收敛）
            _row_views = {"features": digest_feature_view, "flows": digest_flow_view,
                          "states": digest_state_view}
            for key in ("features", "flows", "states"):
                kept = _merge_rows(_arr(dv.get(key)), _arr(rows.get(key)), section_ids, inv_anchors,
                                   stats, view=_row_views[key])
                dv[key] = kept
            if jtext(bundle, "summary", ""):
                dv["summary"] = jtext(bundle, "summary", "")

            audit = self.repos.artifact.find(req_id, "audit")
            av_raw = _load_or_none(audit.content_json) if audit is not None else None
            av: dict[str, Any] = dict(av_raw) if is_json_obj(av_raw) else {
                "red": 0, "yellow": 0, "blue": 0, "items": []}
            kept_items: list[dict[str, Any]] = []
            for it_raw in _arr(av.get("items")):
                if not is_json_obj(it_raw):
                    continue
                a = jtext(it_raw, "anchor", "")
                iid = jtext(it_raw, "id", "")
                if a and a in section_ids:
                    stats["removedRows"] += 1
                    continue
                if iid and iid in inv_audit:
                    stats["removedRows"] += 1
                    continue
                # v1.6（B6/D-J）：audit 行全量归一过共享构造器（kept+new 同口径——补默认 layer、
                # 剥未知私货键；与 push 侧 _audit_view 单一构造器，两套字段清单会漂移）。audit 行
                # 小结构、无「永不触碰」契约，与 flows 仅 new 行归一的策略有意不对称（r4 rationale）
                kept_items.append(audit_item_view(it_raw))
            new_audit = [audit_item_view(x) for x in _arr(rows.get("auditItems")) if is_json_obj(x)]
            av["items"] = [*kept_items, *new_audit]
            stats["newRows"] += len(new_audit)
            av["yellow"] = sum(1 for x in _arr(av.get("items"))
                               if is_json_obj(x) and jtext(x, "level", "yellow") == "yellow")
            av["red"] = sum(1 for x in _arr(av.get("items"))
                            if is_json_obj(x) and jtext(x, "level", "") == "red")

            frs = self.repos.fr_point.list_by_req(req_id)
            fp_v = bundle.get("frPoints")
            # FR 作废两契约位：digestRows.frRemoves（v4-14 prompts/前端主位）+ 顶层 frPoints.remove（兼容）
            remove_ids: set[str] = {str(x) for x in _arr(fp_v.get("remove"))} if is_json_obj(fp_v) else set()
            remove_ids |= {str(x) for x in _arr(rows.get("frRemoves"))}
            kept_frs: list[Any] = []
            for fp in frs:
                if fp.anchor and fp.anchor in section_ids:
                    stats["removedRows"] += 1
                    continue
                if fp.id in remove_ids:
                    stats["removedRows"] += 1
                    continue
                kept_frs.append(fp)
            from platform_sdlc.models import FrPoint
            new_frs = [FrPoint(req_id=req_id, id=jtext(f, "id", ""), name=jtext(f, "name", ""),
                               source=jtext(f, "source", ""), group=jtext(f, "group", ""),
                               module=jtext(f, "module", ""), anchor=jtext(f, "anchor", ""))
                       for f in _arr(rows.get("frUpserts")) if is_json_obj(f) and jtext(f, "id", "")]

            clarify_kept: list[Any] = []  # 仅机器行（skill/incremental）——local 行不删不重插（原样
            # 保留：仍在库中重插会撞 UNIQUE(req_id,seq)，§6.3/§12 一期判据「L 系经合并不丢」）
            local_seqs_live: set[str] = set()  # local 行合并后同样存活——danglingPids 合法集须含之
            # （与 push 侧 legal_seqs = seqs ∪ local_seqs 同口径，否则同一 L 系引用两通道判定相反，
            # 观测误报——review #10）
            for item in self.repos.clarify_item.list_by_req(req_id):
                if item.origin not in ("skill", "incremental"):
                    local_seqs_live.add(item.seq)
                    continue
                if item.anchor and item.anchor in section_ids:
                    stats["removedRows"] += 1
                    continue
                clarify_kept.append(item)
            new_clarify: list[Any] = []
            cl_v = bundle.get("clarify")
            for it_raw in _arr(cl_v.get("items")) if is_json_obj(cl_v) else []:
                if not is_json_obj(it_raw):
                    continue
                new_clarify.append(_machine_clarify(req_id, it_raw))
            stats["newRows"] += len(new_clarify)

            at = Times.now()
            # v4.19：采纳时点落 merged_at——abort 守卫的真值位（产物已按新块态重写，块回滚即分裂）
            self.repos.req_change.set_merged(change_id, at)
            self.repos.artifact.upsert(req_id, "digest", _render_digest_md(dv), _json_compact(dv), at)
            self.repos.artifact.upsert(req_id, "audit", _render_audit_md(av), _json_compact(av), at)
            self.repos.fr_point.replace_all(req_id, [*kept_frs, *new_frs])
            self.repos.clarify_item.delete_machine_by_req(req_id)
            kept_seqs = {x.seq for x in clarify_kept}
            for item in new_clarify:
                item.seq = self.repos.clarify_item.next_machine_seq(req_id, kept_seqs)
                kept_seqs.add(item.seq)
            self.repos.clarify_item.insert_many([*clarify_kept, *new_clarify])
            tier = jtext(bundle, "riskTier", "").strip()
            if tier and tier in ("A", "B", "C"):
                self.repos.requirement.set_risk_tier(req_id, tier)
            self.repos.requirement.touch_synced(req_id, at)
            # v1.6 danglingPids（观察信号非守卫，D-L）：仅计本次 new flows 行的悬空引用（pids ⊄
            # 合并后存活 seq 集 = 机器 kept ∪ new ∪ local——local 行不删不重插同样存活，review #10
            # 与 push 侧同口径）——存量 kept 行的历史悬空不混入（归因「本次生产端纪律」，历史脏行
            # 清理走重推）；照常透传落库不拒收，前端点击「未定位」提示仍是交互兜底
            live_seqs = {x.seq for x in clarify_kept} | {x.seq for x in new_clarify} | local_seqs_live
            stats["danglingPids"] = sum(1 for f in _arr(rows.get("flows")) if is_json_obj(f)
                                        for p in jstrings(f, "pids") if p not in live_seqs)
        log.info("增量合并 %s（变更单 #%s，by=%s）：%s", req_id, change_id, by, stats)
        return {"ok": True, "changeId": change_id, "mergedBy": by, **stats}

    # ---------- 放弃变更（§5.6：changed 态的受控逃生门——增量通道死锁时的出口） ----------

    def abort(self, req_id: str, change_id: int, body: dict[str, Any]) -> dict[str, Any]:
        """放弃 = 当无事发生（除变更单留痕）的全量逆向回滚，一个事务：
        ①守卫：confirmed 单 ∧ changed 态 ∧ 无未采纳 incremental_bundle 草稿 ∧ merged_at 为空
        （v4.19 前以草稿不存在近似「未采纳」，采纳即删草稿致恒空转——已采纳单放弃 = 块回滚而
        产物保持合并态的分裂数据，现按 merged_at 真值判）
        ∧ 为最新 confirmed 单（串行 confirm 下即唯一未消化单，LIFO——只能从最新弃起）
        ∧ 无执行中增量质检（queued 随单定向取消——confirm 侧 count_active 拦截的对称面，
        防 Runner 基于已回滚块态空转）；
        ②块乐观锁：逆向 edit 前逐条校验块现值 == new_text（confirm 后无人再动过），不符 409；
        ③逆向应用：edit 逆（new→old，version 再 +1，updated_by=放弃人）、insert 逆（软删——不构成
        §2.4 封存痕迹，判定排除 deleted 行）、delete 逆（复活清 deleted）、move 逆（按快照 baseline
        恢复 seq——重排常规化语义下 seq 数值无冻结面，恢复仅为呈现回到原样）；
        ④澄清恢复 pending-only：仍为 pending 的行按快照恢复原答复/处置（changed 期间已被人再处置的
        保留新答复，note 追加「变更单#N 已放弃，此为放弃后处置」——防人工新作答无痕丢失）；
        ⑤content_rev 不回退（单调递增，§5.7）+ aborted + changed→published（表外通道）。"""
        with self.db.tx():
            r = self.repos.requirement.find(req_id)
            if r is None:
                raise ApiException(404, f"需求不存在：{req_id}")
            c = self._require_change(req_id, change_id)
            if c.status != "confirmed":
                raise ApiException(409, f"变更单 #{change_id} 状态 {c.status}，放弃仅对 confirmed 单开放"
                                        "（draft 可直接删除）")
            if r.status != "changed":
                raise ApiException(409, f"放弃是 changed 态的逃生门（当前：{r.status}）")
            if self.repos.draft.find(req_id, "incremental_bundle") is not None:
                raise ApiException(409, "存在未采纳的 incremental_bundle 草稿，请先处理后再放弃")
            if c.merged_at:
                # v4.19：已采纳的单不可放弃——merge 已按新块态重写 digest/FR/澄清（touch_synced
                # + pushed_at 刷新），此后逆向回滚块表会产出「块回滚、产物保持合并态」的分裂数据，
                # 且 merge 新增的 incremental 澄清行在 published 态不可处置（死巷）。此前以「草稿
                # 不存在」近似判「未采纳」，而采纳即删草稿，近似恒空转。
                raise ApiException(409, "该单已采纳增量合并，不可放弃——产物修正请提新变更单")
            latest = max((x.id or 0) for x in self.repos.req_change.list_by_req(req_id)
                         if x.status == "confirmed")
            if latest != change_id:
                raise ApiException(409, f"只能放弃最新的 confirmed 单（当前最新：#{latest}），LIFO")
            # ①补：增量质检任务对称处置——queued 随单定向取消（事务内）；running 拦放弃
            # （Runner 已领取，等其回报终态后再来，防状态被从脚下抽走）
            self.repos.task.cancel_queued_of_type(
                req_id, "incremental_check",
                _json_compact({"reason": "change-aborted",
                               "hint": f"变更单 #{change_id} 已放弃，增量任务随单作废"}), Times.now())
            if self.repos.task.count_active(req_id, "incremental_check") > 0:
                raise ApiException(409, "存在执行中的增量质检任务（Runner 已领取），"
                                        "请等其结束（完成/失败）后再放弃")
            aborted_by = jrequired(body, "abortedBy")
            items = self.repos.req_change.list_items(change_id)
            snapshot = _load_snapshot(c.snapshot_json)
            if snapshot is None:
                raise ApiException(409, "变更单无 confirm 快照（旧版本数据），无法放弃")

            # ②块乐观锁（§5.6：逆向 edit 前逐条校验块现值 == new_text）
            live = {b.block_id: b for b in self.repos.req_block.list_by_req(req_id)}
            all_rows = {b.block_id: b for b in self.repos.req_block.list_all(req_id)}
            for it in items:
                if it.op in ("edit", "move"):
                    cur = live.get(it.block_id)
                    if cur is None or cur.text != it.new_text:
                        raise ApiException(409, f"块 {it.block_id} 已被后续修改，无法放弃")
                elif it.op == "delete":
                    row = all_rows.get(it.block_id)
                    if row is None or not row.deleted:
                        raise ApiException(409, f"块 {it.block_id} 未处于软删态，无法放弃（状态与 confirm 时不符）")
                elif it.op == "insert":
                    row = all_rows.get(it.block_id)
                    if row is not None and row.deleted:
                        raise ApiException(409, f"insert 块 {it.block_id} 已被删除，无法放弃")

            # ③逆向应用（executemany 批量；顺序无关——各 op 目标块不相交）
            at = Times.now()
            rev_edit = [(it.old_text, it.old_attrs, at, aborted_by, req_id, it.block_id)
                        for it in items if it.op in ("edit", "move") and it.old_text != it.new_text]
            if rev_edit:
                self.db.executemany(
                    "UPDATE req_block SET text=?, attrs_json=?, version=version+1, updated_at=?, updated_by=?"
                    " WHERE req_id=? AND block_id=?", rev_edit)
            rev_touch = [(at, aborted_by, req_id, it.block_id)
                         for it in items if it.op in ("edit", "move") and it.old_text == it.new_text]
            if rev_touch:
                self.db.executemany(
                    "UPDATE req_block SET version=version+1, updated_at=?, updated_by=? WHERE req_id=? AND block_id=?",
                    rev_touch)
            rev_insert = [(at, at, aborted_by, req_id, it.block_id) for it in items if it.op == "insert"]
            if rev_insert:
                self.db.executemany(
                    "UPDATE req_block SET deleted=?, updated_at=?, updated_by=? WHERE req_id=? AND block_id=?",
                    rev_insert)
            rev_delete = [(at, aborted_by, req_id, it.block_id) for it in items if it.op == "delete"]
            if rev_delete:
                self.db.executemany(
                    "UPDATE req_block SET deleted='', updated_at=?, updated_by=? WHERE req_id=? AND block_id=?",
                    rev_delete)
            # move 逆恢复原 seq（快照 baseline；软删行不动）
            baseline = [(int(seq), req_id, bid) for bid, seq in snapshot.get("baseline", [])]
            if baseline:
                self.db.executemany(
                    "UPDATE req_block SET seq=? WHERE req_id=? AND block_id=? AND deleted=''", baseline)

            # ④澄清恢复 pending-only（快照原值；已再处置行保留 + note 追加）
            by_seq = {x.seq: x for x in self.repos.clarify_item.list_by_req(req_id)}
            restore: list[tuple[str, str, str, str | None, str, str]] = []
            keep_note: list[tuple[str, str, str]] = []
            for snap in snapshot.get("reopen", []):
                seq = str(snap.get("seq") or "")
                cur = by_seq.get(seq)
                if cur is None:
                    continue
                if cur.status == "pending":
                    restore.append((str(snap.get("answer") or ""), str(snap.get("status") or "answered"),
                                    str(snap.get("note") or ""), _at_or_none(snap.get("answeredAt")),
                                    req_id, seq))
                else:
                    keep_note.append((cur.note + f"（变更单#{change_id} 已放弃，此为放弃后处置）", req_id, seq))
            if restore:
                self.db.executemany(
                    "UPDATE clarify_item SET answer=?, status=?, note=?, answered_at=? WHERE req_id=? AND seq=?",
                    restore)
            if keep_note:
                self.db.executemany(
                    "UPDATE clarify_item SET note=? WHERE req_id=? AND seq=?", keep_note)

            # ⑤aborted + changed→published（表外；content_rev 不回退）
            if self.repos.req_change.set_aborted(change_id, aborted_by, at) == 0:
                raise ApiException(409, "放弃竞态：变更单状态已变化")
            self.repos.requirement.set_status(req_id, "published")
        log.info("变更单 #%s 放弃 %s（by=%s，逆向 %d 项/恢复澄清 %d 项）",
                 change_id, req_id, aborted_by, len(items), len(restore))
        out = self.detail(req_id, change_id)
        return out

    def _reopen_rows(self, req_id: str, change_id: int,
                     impact: dict[str, Any]) -> tuple[list[tuple[str, str, str | None]], list[dict[str, Any]]]:
        """重开行构造：冻结影响面的 clarifyItems（含人工增删）∩ 已处置行——旧答复挪 note 前缀
        「因变更单#N 重开，原答复：…」（answer 空的行 note 只记重开标记）。返回 (重开写入口径,
        原值快照)——快照进 snapshot_json 供放弃时 pending-only 恢复（§5.6）。"""
        raw: Any = impact.get("clarifyItems")
        seqs = {jtext(x, "seq", "") for x in _arr(raw) if is_json_obj(x)}
        seqs.discard("")
        if not seqs:
            return [], []
        out: list[tuple[str, str, str | None]] = []
        snapshot: list[dict[str, Any]] = []
        for item in self.repos.clarify_item.list_by_req(req_id):
            if item.seq not in seqs or item.status == "pending":
                continue
            # v1.6（D15/A4）：note 拼解构后文本（answer_text_of）——结构化作答后 answer 原文是
            # JSON 串，直接拼会把裸 JSON 带进 note 与增量合成输入；快照存原值不切（放弃回滚原样恢复）
            note = (f"因变更单#{change_id} 重开，原答复：{answer_text_of(item.answer)}"
                    if item.answer else f"因变更单#{change_id} 重开")
            out.append((item.seq, note, None))
            snapshot.append({"seq": item.seq, "answer": item.answer, "status": item.status,
                             "note": item.note, "answeredAt": item.answered_at})
        return out, snapshot

    # ---------- 影响面预览（§5.4：机械 join 现算，不冻结） ----------

    def preview_impact(self, req_id: str, change_id: int) -> dict[str, Any]:
        """受影响块集 = edit/move 直接块 ∪ delete 子孙全集（递归 CTE；insert 是新块无下游锚定，
        不进影响面）。四类清单 = 块集 join 澄清项/FR/digest 行/关联用例（anchor 精确匹配——空
        anchor 行永不进影响面，§5.7）；章节 = 受影响块所在章节的 heading block_id（禁 seq 数值，
        整体重排天然无害）。heading 删不带章节体：块集不按区间扩散，只按锚定 join（§5.4）。"""
        self._require_base(req_id)
        c = self._require_change(req_id, change_id)
        items = self.repos.req_change.list_items(change_id)
        direct = [i.block_id for i in items if i.op in ("edit", "move")]
        deletes = [i.block_id for i in items if i.op == "delete"]
        target = set(direct) | self.repos.req_change.descendant_blocks(req_id, deletes)

        sections = _sections_of(self.repos.req_block.list_by_req(req_id), target)
        clarify = [{"seq": x.seq, "level": x.level, "question": x.question, "status": x.status}
                   for x in self.repos.clarify_item.list_by_req(req_id) if x.anchor in target]
        frs = [{"id": p.id, "name": p.name} for p in self.repos.fr_point.list_by_req(req_id)
               if p.anchor in target]
        hit_fr = {f["id"] for f in frs}
        cases = _cases_by_fr(self.repos.test_case.list_by_req(req_id), hit_fr)
        digest_rows = self._digest_rows_in(req_id, target)
        return {
            "changeId": c.id, "status": c.status,
            "blocks": sorted(target), "sections": sections,
            "clarifyItems": clarify, "frPoints": frs, "cases": cases, "digestRows": digest_rows,
            "counts": {"blocks": len(target), "sections": len(sections), "clarify": len(clarify),
                       "frPoints": len(frs), "cases": len(cases), "digestRows": len(digest_rows)},
        }

    # ---------- 私有 ----------

    def _require_base(self, req_id: str) -> None:
        """draft 期基础守卫：404 + 块表空无 diff 基线拒。不限需求状态、不查活跃质检——draft 不碰
        块表，与窗口 1 直接编辑（_guard_edit）分立两套守卫面（§5.1：draft 随时可提）。"""
        if self.repos.requirement.find(req_id) is None:
            raise ApiException(404, f"需求不存在：{req_id}")
        if self.repos.req_block.empty(req_id):
            raise ApiException(409, "原文未导入（块表空，无变更基线）：先完成 source_import")

    def _require_change(self, req_id: str, change_id: int) -> ReqChange:
        c = self.repos.req_change.find(req_id, change_id)
        if c is None:
            raise ApiException(404, f"变更单不存在：#{change_id}")
        return c

    def _diff_items(self, req_id: str, body: dict[str, Any]) -> tuple[list[ReqChangeItem], str]:
        """全量块数组 vs 块表 → (ReqChangeItem 列表, 规范化全量块数组 JSON 留底)。复用 diff_blocks
        四态 + LIS；insert 块经 assign_insert_ids 在草稿期即有平台 ID（§5.2）——留底存 assign 后
        数组（confirm 应用面直接可重放，空 id 临时 id 不进留底）。"""
        submitted = self.blocks.parse_blocks(body)
        current = self.repos.req_block.list_by_req(req_id)
        diff = self.blocks.diff_blocks(req_id, submitted, current)
        self.blocks.assign_insert_ids(req_id, submitted)
        items = [ReqChangeItem(
            id=None, change_id=0, block_id=d.block_id, op=d.op,
            old_text=d.old_text, new_text=d.new_text,
            old_attrs=_dumps(d.old_attrs), new_attrs=_dumps(d.new_attrs)) for d in diff]
        return items, _norm_blocks(submitted)

    def _digest_rows_in(self, req_id: str, target: set[str]) -> list[dict[str, str]]:
        """digest 行锚定 join：artifact.content_json（_digest_view 形态）解析 features/flows/states，
        anchor ∈ 块集的行带摘要（digest 行无稳定行 ID，按 anchor 定位——§6.2 合并同规则）。"""
        a = self.repos.artifact.find(req_id, "digest")
        if a is None or not a.content_json:
            return []
        try:
            content: Any = json.loads(a.content_json)
        except json.JSONDecodeError:
            return []
        if not is_json_obj(content):
            return []
        out: list[dict[str, str]] = []
        for f in _arr(content.get("features")):
            if is_json_obj(f) and jtext(f, "anchor", "") in target:
                out.append({"kind": "feature", "anchor": jtext(f, "anchor", ""),
                            "summary": f"{jtext(f, 'role', '')}·{jtext(f, 'm', '')}：{jtext(f, 'func', '')}"})
        for f in _arr(content.get("flows")):
            if is_json_obj(f) and jtext(f, "anchor", "") in target:
                out.append({"kind": "flow", "anchor": jtext(f, "anchor", ""),
                            "summary": f"{jtext(f, 'role', '')}：{jtext(f, 'step', '')}"})
        for st in _arr(content.get("states")):
            if is_json_obj(st) and jtext(st, "anchor", "") in target:
                out.append({"kind": "state", "anchor": jtext(st, "anchor", ""),
                            "summary": f"{jtext(st, 'obj', '')}：{jtext(st, 'from', '')}→{jtext(st, 'to', '')}"})
        return out


# ---------- 模块级 helper ----------


def _counts(items: list[ReqChangeItem]) -> dict[str, int]:
    c = {"edit": 0, "insert": 0, "delete": 0, "move": 0}
    for i in items:
        c[i.op] += 1
    return c


def _sections_of(blocks_all: list[Any], target: set[str]) -> list[dict[str, str]]:
    """受影响块 → 所在章节（读时推导，heading 栈同 _render_blocks 口径）：输出 heading block_id +
    面包屑——**禁 seq 数值**（整体重排会改写全表 seq，冻结数值即失真，§5.4）。"""
    heading_stack: list[tuple[int, str, str]] = []  # (level, heading_block_id, text)
    section_of: dict[str, tuple[str, str]] = {}     # block_id -> (heading_id, path)
    for b in blocks_all:
        if b.type == "heading":
            level = _level_of(b.attrs_json)
            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()
            heading_stack.append((level, b.block_id, b.text))
        if heading_stack:
            section_of[b.block_id] = (heading_stack[-1][1], " › ".join(t for _, _, t in heading_stack))
        else:
            section_of[b.block_id] = ("", "")
    out: dict[str, dict[str, str]] = {}
    for bid in target:
        hid, path = section_of.get(bid, ("", ""))
        if hid:
            out.setdefault(hid, {"headingId": hid, "path": path})
    return list(out.values())


def _level_of(attrs_json: str) -> int:
    v = _load_or_none(attrs_json)
    if v is not None and isinstance(v.get("level"), int):
        return v["level"]
    return 1


def _cases_by_fr(cases: list[TestCase], hit_fr: set[str]) -> list[dict[str, Any]]:
    """关联用例：points 引用受影响 FR 的用例（RTM 反向索引，读时聚合）。"""
    out: list[dict[str, Any]] = []
    for t in cases:
        try:
            pts: Any = json.loads(t.points or "[]")
        except json.JSONDecodeError:
            pts = []
        hit = [str(p) for p in pts] if is_json_list(pts) else []
        hit = [p for p in hit if p in hit_fr]
        if hit:
            out.append({"id": t.id, "title": t.title, "frPoints": hit})
    return out


def _arr(v: Any) -> list[Any]:
    return list(v) if is_json_list(v) else []


def _dumps(v: dict[str, Any] | None) -> str:
    return json.dumps(v, ensure_ascii=False, separators=(",", ":")) if v is not None else ""


def _load_or_none(raw: str) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        v: Any = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return dict(v) if is_json_obj(v) else None


def _norm_blocks(submitted: list[Any]) -> str:
    """draft 全量块数组留底（assign 后规范化：平台 ID + clean attrs——confirm 应用面直接重放）。"""
    return json.dumps([{**{"id": sb.block_id, "type": sb.type, "text": sb.text}, **sb.attrs}
                       for sb in submitted], ensure_ascii=False, separators=(",", ":"))


def _load_raw_list(raw: str) -> list[Any]:
    try:
        v: Any = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    return list(v) if is_json_list(v) else []


def _section_block_ids(blocks_all: list[Any], heading_ids: list[str]) -> set[str]:
    """章节区间读时解析（§6.2：区间 = 目标 heading 的 seq 到下一个 level ≤ 它的 heading）——
    返回区间内块 ID 全集（含 heading 自身）。"""
    targets = set(heading_ids)
    out: set[str] = set()
    active: list[int] = []  # 活动区间的 heading level 栈
    for b in blocks_all:
        if b.type == "heading":
            level = _level_of(b.attrs_json)
            while active and active[-1] >= level:
                active.pop()
            if b.block_id in targets:
                active.append(level)
        if active:
            out.add(b.block_id)
    return out


def _merge_rows(old_rows: list[Any], new_rows: list[Any], section_ids: set[str],
                inv_anchors: set[str], stats: dict[str, int],
                view: Callable[[Any], dict[str, Any]] | None = None) -> list[Any]:
    """§6.2 行合并：区间内旧行删（anchor 落区间）；作废清单删；区间外原样；空 anchor/global 行
    永不触碰；新行追加（同 anchor 多行整组重写——行无稳定 ID，按 anchor 定位）。v1.6：view
    仅作用于 new 行（白名单归一，调用点按 key 传——flows 传 digest_flow_view、其余 None），kept
    行永远原样透传（重写历史数据违背「永不触碰」承诺）。"""
    kept: list[Any] = []
    for f in old_rows:
        if not is_json_obj(f):
            continue
        a = jtext(f, "anchor", "")
        if not a:
            kept.append(f)  # 空 anchor/global 行：不属任何章节，增量合并永不触碰
            continue
        if a in section_ids or a in inv_anchors:
            stats["removedRows"] += 1
            continue
        kept.append(f)
    new = [view(f) if view is not None else f for f in new_rows if is_json_obj(f)]
    stats["newRows"] += len(new)
    return [*kept, *new]


def _machine_clarify(req_id: str, it: dict[str, Any]) -> Any:
    """增量新澄清行（§6.3：origin='incremental'，P 系编号由平台分配接 max+1——seq 平台权威）。
    v1.6（B6）：options/module/layer 宽松透传（layer 归一）——缺此三字段则增量新行相对
    全量推送永远降级（options 静默丢弃、layer 不点亮就绪度）；options 元素过 option_view
    四键白名单（review #11——与 push 全量/读侧 property 三侧同构）。"""
    from platform_sdlc.models import ClarifyItem
    return ClarifyItem(id=0, req_id=req_id, seq="__PENDING__", level=jtext(it, "level", "yellow"),
                       theme=jtext(it, "theme", "") or "增量质检", question=jrequired(it, "question"),
                       ctx=jtext(it, "ctx", ""), quote=jtext(it, "quote", ""),
                       anchor=jtext(it, "anchor", ""), status="pending", origin="incremental",
                       options_json=(_json_compact(opts) if (opts := [v for v in map(option_view, jarray(it, "options"))
                                                                     if v]) else ""),
                       module=jtext(it, "module", ""),
                       layer=normalize_layer(jtext(it, "layer", "")))


def _json_compact(o: Any) -> str:
    return json.dumps(o, ensure_ascii=False, separators=(",", ":"))


def _render_digest_md(dv: dict[str, Any]) -> str:
    """md 整篇重渲染（§6.2：md 正式退化为展示物，权威在结构行）——结构行的机械文本化。"""
    lines: list[str] = ["## 需求梳理（digest）", "", f"- 摘要：{jtext(dv, 'summary', '')}", ""]
    roles = _arr(dv.get("roles"))
    if roles:
        lines += ["### 角色", "", *[f"- {str(r)}" for r in roles], ""]
    feats = [f for f in _arr(dv.get("features")) if is_json_obj(f)]
    if feats:
        lines += ["### 功能点", "", "| 角色 | 模块 | 功能 | 锚点 |", "| --- | --- | --- | --- |",
                  *[f"| {jtext(f, 'role', '')} | {jtext(f, 'm', '')} | {jtext(f, 'func', '')} | "
                    f"{jtext(f, 'anchor', '')} |" for f in feats], ""]
    flows = [f for f in _arr(dv.get("flows")) if is_json_obj(f)]
    if flows:
        lines += ["### 主流程", "", "| 角色 | 步骤 | 下一 | 锚点 |", "| --- | --- | --- | --- |",
                  *[f"| {jtext(f, 'role', '')} | {jtext(f, 'step', '')} | {jtext(f, 'next', '')} | "
                    f"{jtext(f, 'anchor', '')} |" for f in flows], ""]
    states = [f for f in _arr(dv.get("states")) if is_json_obj(f)]
    if states:
        lines += ["### 状态机", "", "| 对象 | 起 | 事件 | 止 | 锚点 |", "| --- | --- | --- | --- | --- |",
                  *[f"| {jtext(f, 'obj', '')} | {jtext(f, 'from', '')} | {jtext(f, 'on', '')} | "
                    f"{jtext(f, 'to', '')} | {jtext(f, 'anchor', '')} |" for f in states], ""]
    return "\n".join(lines)


def _render_audit_md(av: dict[str, Any]) -> str:
    lines = ["## 需求体检（audit）", "",
             f"- 红 {av.get('red', 0)} / 严重 {av.get('yellow', 0)} / 观察 {av.get('blue', 0)}", ""]
    for it in [x for x in _arr(av.get("items")) if is_json_obj(x)]:
        lines.append(f"- [{jtext(it, 'level', 'yellow')}] {jtext(it, 'title', '')}"
                     f"（{jtext(it, 'where', '')}）→ {jtext(it, 'go', '')}")
    return "\n".join(lines)


def _load_snapshot(raw: str) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        v: Any = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return dict(v) if is_json_obj(v) else None


def _at_or_none(v: Any) -> str | None:
    return str(v) if isinstance(v, str) and v else None
