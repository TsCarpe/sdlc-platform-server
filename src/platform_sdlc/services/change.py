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
from typing import Any

from platform_sdlc.common import ApiException, Times, is_json_list, is_json_obj, jrequired, jtext
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
            row = {"id": c.id, "title": c.title, "detail": c.detail, "proposedBy": c.proposed_by,
                   "confirmedBy": c.confirmed_by, "status": c.status, "abortedBy": c.aborted_by,
                   "createdAt": c.created_at, "confirmedAt": c.confirmed_at, "abortedAt": c.aborted_at}
            out.append({k: v for k, v in row.items() if v is not None})
        return out

    def detail(self, req_id: str, change_id: int) -> dict[str, Any]:
        self._require_base(req_id)
        c = self._require_change(req_id, change_id)
        items = self.repos.req_change.list_items(change_id)
        return {
            "id": c.id, "reqId": c.req_id, "title": c.title, "detail": c.detail,
            "proposedBy": c.proposed_by, "confirmedBy": c.confirmed_by, "status": c.status,
            "abortedBy": c.aborted_by, "createdAt": c.created_at,
            "confirmedAt": c.confirmed_at, "abortedAt": c.aborted_at,
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
            if r.status not in ("published", "changed"):
                raise ApiException(409, f"仅已发布需求可确认变更单（当前：{r.status}）；"
                                        "未发布卡的原文变更走窗口 1 直接编辑")
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
            # 副作用的流转走专用动作，通用 set_status 成为守卫旁路的口子不能开）
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

    # ---------- 放弃变更（§5.6：changed 态的受控逃生门——增量通道死锁时的出口） ----------

    def abort(self, req_id: str, change_id: int, body: dict[str, Any]) -> dict[str, Any]:
        """放弃 = 当无事发生（除变更单留痕）的全量逆向回滚，一个事务：
        ①守卫：confirmed 单 ∧ changed 态 ∧ 无已采纳 incremental_bundle（v4-14 前以草稿不存在近似）
        ∧ 为最新 confirmed 单（串行 confirm 下即唯一未消化单，LIFO——只能从最新弃起）；
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
            latest = max((x.id or 0) for x in self.repos.req_change.list_by_req(req_id)
                         if x.status == "confirmed")
            if latest != change_id:
                raise ApiException(409, f"只能放弃最新的 confirmed 单（当前最新：#{latest}），LIFO")
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
            note = f"因变更单#{change_id} 重开，原答复：{item.answer}" if item.answer else f"因变更单#{change_id} 重开"
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
