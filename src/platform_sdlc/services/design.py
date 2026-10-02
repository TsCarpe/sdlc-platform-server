"""设计 lane 域服务（对应 DesignService，DESIGN-v2.4 D34/D39）。

设计域独立于需求状态机：skill 权威落库仅「已定稿｜轻量跳过」（design_final_json + artifact design），
running/failed 由任务推导。视图（DesignView）以 camelCase dict 组装，NON_NULL 键不输出。
"""

from __future__ import annotations

import json
from typing import Any

from platform_sdlc.common import ApiException, Times, jrequired, jtext
from platform_sdlc.db import Database
from platform_sdlc.models import DraftArtifact, Requirement
from platform_sdlc.repositories import Repos

A_DESIGN = "design"


class DesignService:
    def __init__(self, db: Database, repos: Repos) -> None:
        self.db = db
        self.repos = repos

    def view(self, req_id: str) -> dict[str, Any]:
        r = self.require(req_id)
        out: dict[str, Any] = {"reqId": req_id, "tier": r.risk_tier, "targetRepo": self.repos.requirement.get_target_repo(req_id)}
        repos = self.repos_of(req_id)
        if repos:  # D39：多仓清单；空仓不输出（NON_NULL）
            out["repos"] = repos
        fin = _parse(self.repos.requirement.get_design_final(req_id))

        skip = fin.get("skipDeclared")
        if isinstance(skip, dict):
            out["skipDeclared"] = skip

        fb: dict[str, Any] | None = None
        finalized = fin.get("finalized")
        if isinstance(finalized, dict):
            fb = {"by": _text(finalized.get("by")), "at": _text(finalized.get("at"))}
            a = self.repos.artifact.find(req_id, A_DESIGN)
            if a is not None:
                fb["md"] = a.markdown
                fb["version"] = a.version
        if fb is not None:
            out["finalized"] = fb

        d = self.repos.draft.find(req_id, A_DESIGN)
        if d is not None:
            out["draft"] = {"md": d.md, "taskId": d.task_id, "updatedAt": d.updated_at}

        proposal = fin.get("tierProposal")
        if not isinstance(proposal, dict) and d is not None and d.content_json.strip():
            dp = _parse(d.content_json).get("tierProposal")
            if isinstance(dp, dict):
                proposal = dp
        if isinstance(proposal, dict):
            out["tierProposal"] = proposal

        # 六态推导输入：最新 design_gen/design_rev 任务（含终态，UI 据此亮「失败可重试」）
        for t in self.repos.task.list(req_id, None):
            if t.type in ("design_gen", "design_rev"):
                out["activeTask"] = {"id": t.id, "type": t.type, "status": t.status, "createdAt": t.created_at}
                break  # list 按 id 倒序，首个即最新

        if "skipDeclared" in out:
            out["phase"] = "skipped"
        elif fb is not None:
            out["phase"] = "final"
        elif d is not None:
            out["phase"] = "draft"
        else:
            out["phase"] = "none"
        return out

    def set_target_repos(self, req_id: str, body: dict[str, Any] | None) -> None:
        """多目标仓全量替换（D39）：整体写入 target_repos，首仓镜像进 target_repo。
        空 repos = 清空配置；上限 8 条；kind 白名单 local|github；ref 必填 ≤300；role ≤20；branch ≤100。"""
        self.require(req_id)
        items = None if body is None else body.get("repos")
        if items is None:
            raise ApiException(400, "缺少 repos 字段（空数组 = 清空配置）")
        if not isinstance(items, list):
            raise ApiException(400, "repos 必须是数组（空数组 = 清空配置）")
        if len(items) > 8:
            raise ApiException(400, f"目标仓最多 8 条（当前 {len(items)}）")
        if not items:
            self.repos.requirement.set_target_repos(req_id, "")
            self.repos.requirement.set_target_repo(req_id, "")
            return
        parsed: list[dict[str, str]] = []
        seen: set[str] = set()
        for r in items:
            role = jtext(r, "role", "").strip()
            if len(role) > 20:
                raise ApiException(400, "角色标签过长（≤20 字）")
            kind = jtext(r, "kind", "").strip()
            if kind not in ("local", "github"):
                raise ApiException(400, f"非法 kind：{kind}（仅支持 local | github）")
            ref = jrequired(r, "ref").strip()
            if len(ref) > 300:
                raise ApiException(400, "仓址/路径过长（≤300 字）")
            branch = jtext(r, "branch", "").strip()
            if len(branch) > 100:
                raise ApiException(400, "分支过长（≤100 字）")
            key = f"{kind}|{ref}|{branch}"
            if key in seen:
                raise ApiException(400, f"目标仓重复：{ref}")
            seen.add(key)
            parsed.append({"role": role, "kind": kind, "ref": ref, "branch": branch})
        self.repos.requirement.set_target_repos(req_id, _json(parsed))
        self.repos.requirement.set_target_repo(req_id, parsed[0]["ref"])  # 首仓镜像（旧读面/降级兜底）

    def repos_of(self, req_id: str) -> list[dict[str, str]]:
        """读取多仓清单：target_repos 列优先，空则回退旧单仓列包成单条目（迁移兜底）。"""
        raw = self.repos.requirement.get_target_repos(req_id)
        if raw.strip():
            try:
                v = json.loads(raw)
                if isinstance(v, list) and v:
                    return [{"role": str(b.get("role", "")), "kind": str(b.get("kind", "")),
                             "ref": str(b.get("ref", "")), "branch": str(b.get("branch", ""))}
                            for b in v if isinstance(b, dict)]
            except Exception:
                pass  # 坏值走回退
        legacy = self.repos.requirement.get_target_repo(req_id)
        if not legacy.strip():
            return []
        return [{"role": "", "kind": "github" if legacy.startswith("http") else "local",
                 "ref": legacy, "branch": ""}]

    def skip(self, req_id: str, body: dict[str, Any] | None) -> None:
        """C 级轻量跳过声明（留痕可审计）：声明后 design_gen 拒绝，门禁以自查档执行。"""
        r = self.require(req_id)
        if r.risk_tier != "C":
            raise ApiException(409, f"仅 C 级需求可声明轻量跳过（当前分级：{r.risk_tier or '未分级，按 A'}）")
        fin = _parse(self.repos.requirement.get_design_final(req_id))
        if isinstance(fin.get("skipDeclared"), dict):
            raise ApiException(409, "已声明轻量跳过，无需重复声明")
        if isinstance(fin.get("finalized"), dict) or self.repos.draft.find(req_id, A_DESIGN) is not None:
            raise ApiException(409, "已存在设计草稿/终稿，与轻量跳过互斥")
        nxt = dict(fin) if isinstance(fin, dict) else {}
        nxt["skipDeclared"] = {"by": jtext(body or {}, "by", "").strip(), "at": Times.now()}
        self.repos.requirement.set_design_final(req_id, _json(nxt))

    def assert_gen_allowed(self, r: Requirement) -> None:
        """design_gen 建任务前置守卫（TaskService.create_guarded 调用，守卫口径集中在设计域）。"""
        tier = r.risk_tier or "A"
        if tier == "C":
            raise ApiException(409, "C 级需求走轻量路径（不产设计文档）；可声明轻量跳过后由门禁自查档把关")
        if isinstance(_parse(self.repos.requirement.get_design_final(r.id)).get("skipDeclared"), dict):
            raise ApiException(409, "已声明轻量跳过，生成设计文档与其互斥")
        if not self.repos_of(r.id):
            raise ApiException(400, "未配置目标代码仓（存量勘察需要它，至少 1 条）；先在设计页配置目标仓")

    def finalize(self, req_id: str, d: DraftArtifact, by: str) -> int:
        """定稿采纳（TaskService.approve 的 design 分支）：draft design → artifact design
        （version+1 承接落改修订）+ design_final_json.finalized 留痕。与轻量跳过互斥。"""
        self.require(req_id)
        fin = _parse(self.repos.requirement.get_design_final(req_id))
        if isinstance(fin.get("skipDeclared"), dict):
            raise ApiException(409, "已声明轻量跳过，与设计定稿互斥")
        self.repos.artifact.upsert(req_id, A_DESIGN, d.md or "", d.content_json or "", Times.now())
        a = self.repos.artifact.find(req_id, A_DESIGN)
        ver = a.version if a is not None else 1
        nxt = dict(fin) if isinstance(fin, dict) else {}
        nxt["finalized"] = {"by": (by or "").strip(), "at": Times.now(), "ver": ver}
        proposal = _parse(d.content_json or "").get("tierProposal")
        if isinstance(proposal, dict):
            nxt["tierProposal"] = proposal
        self.repos.requirement.set_design_final(req_id, _json(nxt))
        return ver

    def require(self, req_id: str) -> Requirement:
        r = self.repos.requirement.find(req_id)
        if r is None:
            raise ApiException(404, f"需求不存在：{req_id}")
        return r


def _parse(json_str: str) -> dict[str, Any]:
    """坏值降级空对象：设计域字段缺失按未声明处理，不阻塞读面。"""
    try:
        v = json.loads(json_str.strip() or "{}")
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


def _text(v: Any) -> str:
    return "" if v is None else str(v)


def _json(o: Any) -> str:
    return json.dumps(o, ensure_ascii=False, separators=(",", ":"))
