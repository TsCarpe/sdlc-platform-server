"""任务 prompt 模板（自 AgentScopeAgentExecutor.java 逐字移植，与 runner.mjs promptFor 同源）。

改模板须同步 claude-sdlc-skills 的 digest-template/dimensions/report-template/payload-schema
（速查卡同源约束见 Java 版 qualityQuickRef 注释）。
"""

from __future__ import annotations

import json
from pathlib import Path

from platform_sdlc.common import is_json_obj
from platform_sdlc.models import Requirement


def sys_prompt(task_type: str, card: Requirement, skills_repo: Path, repos_of: list[dict[str, str]],
               import_out_dir: Path | None = None) -> str:
    repo = str(skills_repo)
    common = (
        "你是 SDLC 平台的任务执行器。平台能力以进程内工具提供（不要 curl 平台）。"
        f"需求卡 {card.id}（{card.name}），工作区 sdlc/{card.name}/。\n"
        "完成后必须输出单独一行结果摘要（最后一行，机器解析）：\n"
        'SDLC_TASK_RESULT {"关键统计": "..."}\n'
        "硬性红线（违反即失败）：\n"
        "1. 产物只能通过 put_draft 工具写草稿；平台工具面上不存在任何正式产物写入通道（正式产物只能由人在平台采纳/发布）。\n"
        "2. 原文图片/附件/画板媒体先逐个 upload_media 上传（mediaId 与 doc.media[].id 一致），再推引用它们的 doc。\n"
        "3. 忠实原文：梳理不评价不脑补，体检逐条附原文定位，❓ 禁止自行补全。\n"
        "4. 技能仓（含其绝对路径）文档一律用 read_file 工具读取（工作区文件工具访问不了技能仓）；"
        "run_command 在当前任务工作区目录执行，脚本产物（doc.json、media/ 等）直接落工作区。\n"
        "5. 磁盘中介工作法（防上下文膨胀，必须遵守）：每读完一份文档，把要点记为工作区笔记文件；"
        "中间产物（doc.json、digest.md、audit.md、最终 payload.json）一律用 write_file/脚本落工作区，不要在对话里来回粘贴大段内容；"
        "最终回传用 put_draft(contentJsonPath=工作区内 payload.json 路径)（standard 用 mdPath），不要把大 payload 作为内联参数；"
        "已读过的文件不要重读，找不到文件先 run_command(ls; find . -name xxx) 而不是重跑上游步骤。\n"
    )
    if task_type == "source_import":
        return _source_import_steps(common, card, repo, import_out_dir)
    if task_type == "quality_check":
        return f"{common}\n{_quality_check_steps(repo, card)}\n{_quality_quick_ref()}"
    if task_type == "incremental_check":
        return (f"{common}\n执行变更后局部质检（受影响章节切片增量梳理，v4 DESIGN §6）：\n"
                f"1. read_file 读取增量规范：{repo}/skills/sdlc-intent/references/incremental-guide.md"
                "（受影响章节重梳理、作废行清单、anchor 越界纪律）。\n"
                "2. get_requirement_card（编辑后块模型 doc）+ get_digest_bundle（digest/audit 结构行）"
                "+ 只读评论工具取评论；已答澄清项经 list_clarify_items 工具获取（其答复是重梳理的"
                "口径输入——一律取 answerText，answer 原文可能是结构化 JSON 串，不要直接消费）；"
                "变更单冻结面（impact 的 blocks/sections/四类清单）从任务 payload 的"
                " note 说明经 GET /changes 与 /changes/{id}/impact 获取（run_command curl 直落 raw/）。\n"
                "3. 只重梳理受影响章节：新版本 digest 行/audit 条目/FR 点 + 作废行清单 + 新澄清（P 系编号"
                "由平台合并时分配，items 不携带 seq；v1.6：新澄清带 module/layer 与 options——纪律同全量"
                "质检，冲突/取舍类选项化 2-4 项、缺失/开放类不硬造）。\n"
                "4. 组装增量包 {changeId, sections:[heading block_id 禁 seq 数值], digestRows:{features,"
                "flows,states,auditItems(每条带 layer),frUpserts,frRemoves}, invalidate:{digestAnchors,auditIds}, "
                "clarify:{items(带 options/module/layer)}, summary, riskTier}——新行 anchor 必须 ∈ impact.blocks（越界即拒），"
                "空 anchor/global 行不产不废；flows 新行 pids 仅引用存量已分配 seq（list_clarify_items 可见编号），"
                "本包新问题编号未分配引用必悬空，禁引用。\n"
                "5. put_draft(type=incremental_bundle, contentJson=增量包)。\n"
                "6. 输出 SDLC_TASK_RESULT 行（受影响章节数/新行数/作废数/新澄清数）。")
    if task_type == "standard_doc":
        return (f"{common}\n执行标准需求文档合成（重写版）。平台已有该需求的全部澄清答复：\n"
                "1. get_requirement_card + list_clarify_items 获取卡、原文与逐问答复（答复一律取 answerText"
                "——服务端解构后的纯文本，answer 原文可能是结构化 JSON 串，任何情况下不要直接消费）；"
                "get_digest_bundle 获取 digest/audit。\n"
                "   澄清项处置语义（DESIGN-v2.2 D21）：status=rejected 的问题不进定稿（连同其答复一并忽略，PM 已认定不是问题）；status=deferred 的记入遗留假设（需求.Axx）并标注〔Pxx·挂起〕。\n"
                f"2. read_file 读取合成规范：{repo}/skills/sdlc-sync/references/standard-doc-spec.md（结构/消化规则/质量自查清单，必须逐项遵守：❓清零、不发明需求、原文规则全集等价覆盖、✅/⚠️/删除线标注、保留 需求.FR-xx / 体检.Pxx / 需求.Axx / 裸 Pxx 引用记号；澄清结论固化的条款尾部带〔Pxx〕尾缀、挂起项带〔Pxx·挂起〕——与 spec §3 消化规则同源）。\n"
                "3. 以快照为底稿逐条消化体检定论与澄清答复，合成完整定稿 markdown。\n"
                "4. put_draft(type=standard, md=合成文档)。\n"
                "5. 输出 SDLC_TASK_RESULT 行（章节结构摘要/消化条数/遗留假设数）。")
    if task_type == "case_gen":
        return (f"{common}\n执行测试用例生成与登记（sdlc-test 的 cases 阶段）：\n"
                f"1. read_file 读取规范：{repo}/skills/sdlc-test/SKILL.md 的「cases 用例生成」阶段、"
                f"{repo}/skills/sdlc-test/references/cases/ 与 references/design-techniques.md、以及 {repo}/skills/sdlc-sync/references/payload-schema.md 的用例契约。\n"
                "2. get_digest_bundle 获取 digest/audit（独立性红线：不读任何设计文档）。\n"
                "3. 按 cases.md 产物结构生成用例（总览表为权威清单 + 双向追踪 points → 需求.FR-xx + 重点验证项 verifyRefs → 体检.Pxx/需求.Axx）。\n"
                '4. 组装 payload {items:[{id,title,points,priority,pre,steps,expect,owner:"sdlc-test",tool:"sdlc-test",status:"draft",verifyRefs,defectRefs}]}，缺陷只上编号引用文本。\n'
                "5. put_draft(type=cases, contentJson=完整 payload)。\n"
                "6. 输出 SDLC_TASK_RESULT 行（用例数/FR 覆盖数/verifyRefs 数）。")
    if task_type == "design_gen":
        tier = card.risk_tier or "A"
        sb: list[str] = []
        for i, b in enumerate(repos_of, 1):
            line = f"   {i}. [{b.get('role') or '未标注'}|{b.get('kind')}] {b.get('ref')}"
            if b.get("branch"):
                line += f" @{b['branch']}"
            sb.append(line)
        repos_text = "\n".join(sb)
        tier_desc = "全量骨架" if tier == "A" else "mini 裁剪表"
        return (f"{common}\n执行技术设计生成（sdlc-design）：\n"
                f"1. read_file 读取规范：{repo}/skills/sdlc-design/SKILL.md 与 references/design-template.md（头部字段 + §0–§8 九节结构，结构权威）。\n"
                "2. 输入锚点（GR-1 冻结，不读其他需求态）：get_digest_bundle 获取 digest/audit；get_requirement_card 取标准文档。\n"
                f"3. 前置闸三道自查：Q 表硬阻塞（只锁对应功能点不锁全局，被锁契约行标「锁定待拍板（确认.Q-xx）」）；风险分级已拍板（{tier}）；tier 只升不降——存量勘察发现影响面证据时在 contentJson.tierProposal 上报提案，不擅自改分级。\n"
                f"4. 存量勘察（多仓逐一覆盖，只读；读不到某仓时降级 grep/读文件并在文档内如实注明降级，禁止跳过不报）：\n{repos_text}\n"
                f"5. 按 design-template 产出完整 design.md（{tier} 级：{tier_desc}；齐套自检六项：D 表八类别显式决策、公共资产六类、接口契约每行挂 F 编号+口径来源、切分约束三件、体检 🔴🟡 去向逐条有落点、Q 表就位）。\n"
                "6. put_draft(type=design, md=完整 design.md)；有升级提案时附 contentJson={\"tierProposal\":{...}}。\n"
                "7. 输出 SDLC_TASK_RESULT 行（齐套自检结果/Q 表数/tier 提案有无/勘察仓覆盖数）。")
    if task_type == "design_rev":
        return (f"{common}\n执行设计修订（sdlc-design 增量模式，GR-4 落改闭环）：\n"
                "1. get_design_doc 读取已定稿设计（md/version/目标仓）与修订依据。\n"
                "2. 修订范围 = goal 指令给出的落改工作项（逐条落点与改动摘要）；只改受影响章节，其余保持原样，不重写未涉及章节。\n"
                "3. 偏差回写规则：仅影响 ER/接口契约/公共组件/公共资产的偏差回写设计文档，且只重审受影响切片。\n"
                "4. put_draft(type=design, md=修订后完整 design.md)（版本号由平台定稿时递增，文档内不手改版本）。\n"
                "5. 输出 SDLC_TASK_RESULT 行（修订条数/涉及章节）。")
    if task_type == "gate_review":
        tier = card.risk_tier or "A"
        return (f"{common}\n执行门禁评审（sdlc-gate，{tier} 档）：\n"
                f"1. read_file 读取规范：{repo}/skills/sdlc-gate/SKILL.md 与 references/issue-template.md（issues 文件结构权威）、references/cross-check-guide.md。\n"
                "2. 输入三件（CONTRACT）：get_digest_bundle（intake digest/audit）、get_design_doc（设计终稿）、list_cases（用例登记，读其头部风险分级）。\n"
                "3. 分档扇出纪律：本进程内通道只执行 C 级（A/B 级任务在派发前已被平台拒绝，不会到达这里）——C=主会话五问自查，不开子代理。\n"
                "   issues 头部必须写扇出留痕三行（P1-9，平台 ingest 机械校验）："
                "`| reviewMode | degraded |`、`| fanout | 0 |`、`| contextMode | single-session |`——"
                "进程内通道无 spawn 能力，必须如实标注降级，禁止写成 external/fresh。\n"
                "4. 对抗式输出六字段：标题/场景（白话 2-3 句，禁代号）/原文（≤2 句摘引，禁只给行号）/位置/严重度（high|mid|low）/依据（hard=原文直接支撑｜soft=推导）；分歧条目 div:true；权衡条目 tradeoff:true 并建议驳回。\n"
                "5. RECONCILE 四分类（契约误读/噪音剔除，不进清单；有效可行动/有效权衡进清单）+ 同根因合并（同根因条目 mergedUnit 相同，人裁决的是裁决单元不是发现数）。\n"
                "6. 产出 issues 文件（issue-template 结构：头部 评审状态=待裁决/风险分级/设计文档/用例文件/CONTRACT 基准/业务背景/编号体系 + 速览 + 分歧清单 + 角色三表 + 备案区 + 裁决记录汇总留空）。\n"
                '7. put_draft(type=gate_issues, md=issues 文件全文, contentJson={"issues":[{code:"D-01|A-xx|S-xx|T-xx",sev:"high|mid|low",theme,role,scene,quote,loc,basis:"hard|soft",div,tradeoff,filed,mergedUnit}]})。\n'
                "8. 输出 SDLC_TASK_RESULT 行（发现数/裁决单元数/分歧数/备案数）。")
    raise ValueError(f"未知任务类型：{task_type}")


def _source_import_steps(common: str, card: Requirement, repo: str, out_dir: Path | None) -> str:
    """v4-3（DESIGN-v4 §3）：原文导入任务——拉取 + 解析 + 评论导入，产物 = parse_doc 输出文件，
    落库由任务完成回调（服务端直读文件字节）执行，不经对话上下文，不产草稿。"""
    out = str(out_dir) if out_dir is not None else "tasks/<任务ID>/out"
    feishu = card.source_type == "feishu"
    head = (
        "你是 SDLC 平台的原文导入执行器。平台能力以进程内工具提供（不要 curl 平台）。\n"
        f"需求卡 {card.id}（{card.name}），工作区 sdlc/{card.name}/。\n"
        "完成后必须输出单独一行结果摘要（最后一行，机器解析）：\n"
        'SDLC_TASK_RESULT {"blocks": "...", "comments": "...", "media": "..."}\n'
        "硬性红线（违反即失败）：\n"
        f"1. 导入产物 = 且仅 = parse_doc.py 的输出文件 {out}/doc.json——必须由 parse_doc 产出，"
        "禁止自写、改写、拼接该文件；落库由平台在任务完成回调直读该文件字节执行（不经对话上下文）。\n"
        "2. 本任务无草稿回传（不 put_draft）；媒体文件照常先逐个 upload_media（mediaId 与 doc.media[].id 一致）。\n"
        "3. 忠实原文：逐字转录不评价不脑补；拉取/解析失败如实报错终止，禁止降级自造产物。\n"
        "4. raw/ 原始文件一律不整读（只经脚本消费，防上下文膨胀）。\n"
        "5. parse_doc 必须 exit 0 才可回传任务完成（exit 1=媒体下载失败、exit 2=守恒自检失败、"
        "exit 3=用法错误，均中止任务并按其输出提示修复后重跑，禁止带病回传）。\n"
        "6. doc.json 顶层 guard 是平台落库终检依据（缺失或与产物不一致任务将 failed），"
        "不得手工增删改 guard 与产物内容。\n"
        "7. 媒体必须全部 upload 成功后才可报 done（块引用的媒体不在平台将 missingMedia 阻断拒绝落库）。\n"
    )
    steps = "按以下步骤执行原文导入：\n1. run_command: mkdir -p raw intake/media\n"
    if feishu:
        steps += (
            "2. 飞书源固定拉取序列（<URL>=卡内 sourceUrl，逐条 run_command；"
            "禁止自写 grep/inspect 类脚本重造解析管线）：\n"
            "   lark-cli docs +fetch --doc <URL> --doc-format markdown > raw/doc.md\n"
            "   lark-cli drive +list-comments --url <URL> --solved-status all --comment-scope all --need-relation --format json > raw/comments.json\n"
            "   lark-cli docs +fetch --doc <URL> --doc-format xml --detail with-ids > raw/fetch.json\n"
            "   write_file 落盘 tools/extract.py 后 run_command 执行（run_command 按 |;&& 分段校验，"
            "引号感知——引号内的分号不切段，单行 python3 -c 整体加引号即可直接执行）：\n"
            "     import json\n"
            "     d = json.load(open('raw/fetch.json'))['data']['document']\n"
            "     open('raw/content.xml', 'w').write(d['content'])\n"
            "     rm = d['reference_map']\n"
            "     open('raw/refmap.json', 'w').write(json.dumps(rm.get('comments', rm)))\n"
            f"   python3 {repo}/skills/sdlc-intent/scripts/parse_doc.py --xml raw/content.xml --refmap raw/refmap.json"
            f" --out {out}/doc.json --media-dir intake/media --download --url <URL>\n"
            "3. run_command(ls intake/media) 盘点下载的媒体，逐个 upload_media"
            "（mediaId=doc.media[].id，path=intake/media/<文件名>）。\n"
            "4. 输出 SDLC_TASK_RESULT 行（块数/评论线程数/媒体数）。\n"
        )
    else:
        steps += (
            "2. 文本源：get_requirement_card 取卡内 sourceText，write_file 落盘 raw/source.md（原文逐字，不增删）。\n"
            f"3. python3 {repo}/skills/sdlc-intent/scripts/parse_doc.py --md raw/source.md --out {out}/doc.json"
            "（text 源无媒体/评论，块 ID 为解析器自造）。\n"
            "4. 输出 SDLC_TASK_RESULT 行（块数）。\n"
        )
    _ = common  # 导入任务不沿用通用红线段（无草稿面），保留参数形制与其它分支一致
    return head + steps


def goal_prompt(task_type: str) -> str:
    if task_type == "source_import":
        return "开始执行原文导入，按系统指令拉取解析并产出 out/doc.json，平台完成回调自动落库（无草稿回传）。"
    if task_type == "quality_check":
        return "开始执行需求质检，按系统指令的步骤完成并回传 digest_bundle 草稿。"
    if task_type == "incremental_check":
        return "开始执行变更后局部质检，按系统指令的步骤完成并回传 incremental_bundle 草稿。"
    if task_type == "standard_doc":
        return "开始执行标准需求文档合成，按系统指令的步骤完成并回传 standard 草稿。"
    if task_type == "design_gen":
        return "开始执行技术设计生成，按系统指令的步骤完成并回传 design 草稿。"
    if task_type == "design_rev":
        return "开始执行设计修订，按系统指令的步骤完成并回传修订后的 design 草稿。"
    if task_type == "gate_review":
        return "开始执行门禁评审，按系统指令的步骤完成并回传 gate_issues 草稿。"
    return "开始执行测试用例生成，按系统指令的步骤完成并回传 cases 草稿。"


def payload_note(payload_json: str) -> str:
    """任务 payload 里的 note（design_rev 的落改工作项）；坏值降级空串。"""
    try:
        v = json.loads(payload_json or "{}")
        return str(v.get("note", "")) if is_json_obj(v) else ""
    except Exception:
        return ""


def _quality_check_steps(repo: str, card: Requirement) -> str:
    """v4-5（DESIGN-v4 §3）：质检 = 纯分析读块模型——原文/评论从平台取（块表权威，双源统一单分支），
    拉取序列与 text 特殊分支退役（导入已在 source_import 完成；媒体已在平台，无上传步）。"""
    _ = card
    return ("按以下步骤执行需求质检（sdlc-intent 两段式 + v1.2 锚点管线；产物结构见下方速查卡）：\n"
            "1. get_requirement_card 获取输入：卡内 doc 键 = 块表权威原文 {blocks,media}（含评论拼回的 comment 块）；"
            "无 doc 键 = 原文未导入——如实终止报错（「原文未导入，先完成 source_import」），禁止以 sourceText 降级替代。\n"
            "2. 磁盘中介落盘（不要在对话里粘贴大段原文）：doc 原样 write_file 落盘 intake/doc-<YYYYMMDD>.json；"
            "list_req_comments 获取评论 → write_file 原样落盘 raw/comments.json（输出即 resolve_anchors 兼容格式）。\n"
            f"3. 查条款树：python3 {repo}/skills/sdlc-intent/scripts/list_clauses.py --doc intake/doc-<YYYYMMDD>.json [--row N]。\n"
            "4. 第一段梳理：按速查卡「digest 8 区块」产出 intake/digest-<YYYYMMDD>.md（速查卡未覆盖的列级细节才 read_file "
            f"{repo}/skills/sdlc-intent/references/digest-template.md）。\n"
            "5. 第二段体检：按速查卡「七层/五章/三层」产出 intake/audit-<YYYYMMDD>.md 与 intake/pm-checklist-<YYYYMMDD>.md（细节才 read_file "
            f"{repo}/skills/sdlc-intent/references/dimensions.md、references/report-template.md）。"
            "v1.6 产出纪律（与 runner/契约同源，逐项执行）：①每个澄清问题必须带 layer（命中层号 \"1\"-\"7\"，"
            "维度外补充类才允许不携带）与 module（=fr_points[].module 同体系的归属功能模块）；②冲突类/取舍类问题"
            "尽量选项化——payload items[].options 数组 2-4 项 {key:A-Z 大写唯一, text:自包含选项全文, why:一句理由}、"
            "至多一项 recommended:true（纯缺失类/开放类不硬造）；③flows 断点行（gap:true）带 pids:[\"Pxx\"] 字符串数组"
            "（引用本 payload items 的 P 编号）——三项缺失即生产端纪律失守。\n"
            "6. 组装 payload.json（严格按速查卡「payload 字段」；不含 doc——原文块表权威已落库，透传会在采纳时被剥除；"
            "intake/doc-*.json 仍用于第 3 步条款树与第 7 步锚点解析）。\n"
            f"7. run_command 执行 python3 {repo}/skills/sdlc-intent/scripts/resolve_anchors.py --doc intake/doc-<YYYYMMDD>.json"
            " --comments raw/comments.json --payload payload.json --out payload-anchored.json。\n"
            "8. put_draft(type=digest_bundle, contentJsonPath=payload-anchored.json)，确认返回 ok 后输出 SDLC_TASK_RESULT 行"
            "（问题数/需求点数/锚点命中统计）。")


def _quality_quick_ref() -> str:
    return (
        "\n== 速查卡（对话被压缩后以此为准，勿重读技能文档）==\n"
        "digest 8 区块（全部必产出；无内容写「原文未提及」并保留标题；目录后先产无编号「符号图例」小节——❓/✅⚠️❌/风险分级<ABC>/FR-xx/A编号 释义，不进产物目录；❓=原文缺失禁止补全；评论与正文冲突以评论为准并标「正文未同步」）：\n"
        " 1 一句话概述+背景与目标 ｜ 2 角色与职责表(角色|职责|主要使用场景|出处) ｜ 3 核心概念表(术语|释义|出处|✅已定义/❓被引用未定义/⚠️已定义未使用) ｜ "
        "4 功能点地图(角色|页面/模块|功能|✅有详述/⚠️仅清单/❌仅提及|出处；核心功能清单逐行必须有落点) ｜ 5 业务主流程 mermaid flowchart(角色 subgraph 泳道，断点标❓；图题行加粗置于图上方：图 1：<流程名>（主流程，跨角色泳道），页面级流程续编号) ｜ "
        "6 状态机(每个带状态对象一张 stateDiagram-v2+流转边表；图题固定 图 N：<对象名> 状态机——对象名是 states.obj 的唯一载体；无出处流转不画，边表以?行呈现：从|触发|至|出处) ｜ 7 关键规则与口径表(编号|规则/口径|数值/约束|出处；FR-01 起连续编号不重排，下游引用写 需求.FR-xx) ｜ "
        "8 假设清单(编号|原文位置|模糊点|推断/假设|梳理时的理解；A1 起，外部产物引用写 需求.Axx)\n"
        "体检七层：1概念 2状态机与流程 3数据与字段 4规则与逻辑 5角色与场景覆盖 6文档质量 7外部参照(查缺失，先构造参照物)。严重度三档：阻断/严重/建议。\n"
        "audit 五章：执行摘要 ｜ 阻断项速览 ｜ 问题明细按层(3.1–3.7) ｜ 主题关联 ｜ 处理状态汇总。"
        "pm-checklist 三层：🔴必须确认 ｜ 🟡重要 ｜ 🔵可顺带（档→主题组→问题表；散题归「其他」不硬凑主题；"
        "冲突/取舍类问题在问题格内换行列选项 可选：A. <选项全文>，推荐项标（推荐——一句理由）常显，缺失/开放类不硬造）。\n"
        "payload 顶层字段：snapshot{md=原文全文}、domains[全局需求域顺序]、digest{summary, risk_tier(A|B|C 需求级), roles[], "
        "features[{role,m,func,cov,src}], flows[{role,step,next,gap(断点行=true且next填?),src,pids(可选,引用P编号数组)}], "
        "states[{obj(取§6图题对象名),from,on,to,gap(断点行=true且to填?),src}], "
        "fr_points[{id:\"需求.FR-xx\",name,source,group,module}], md}、"
        "audit{red,yellow,blue,items[{id:\"体检.Pxx\",level:red|yellow|blue,title,where,go,group,quote,layer(可选1-7)}], md}、"
        "items[{seq:\"Pxx\",level,theme(必填，取值=需求域),question,ctx,answer,quote,"
        "options(可选:[{key:A-Z唯一,text:自包含选项全文,why,recommended:至多一项true}]),module(可选=fr_points同体系),"
        "layer(可选\"1\"-\"7\",判层见dimensions第8条,维度外不携带)}]。flows 断点行 pids 仅引用本 payload items 的 P 编号。\n"
        "出处条款级写法：详述N·<小节名> 或 详述N·<序数>；定位不到回退章节名，禁止编造条款号/序数。"
    )


def resume_hint(workspace: Path) -> str:
    """续跑提示：工作区已有中间产物时，告知 agent 先盘点续跑不重做。"""
    found: list[str] = []
    intake = workspace / "intake"
    if intake.is_dir():
        for p in intake.iterdir():
            n = p.name
            if n.startswith(("doc-", "digest-", "audit-", "pm-checklist-")) and (n.endswith(".json") or n.endswith(".md")) or n == "media":
                found.append(f"intake/{n}")
    for p in workspace.iterdir():
        if p.name.startswith("payload") and p.name.endswith(".json"):
            found.append(p.name)
    if not found:
        return ""
    return ("\n（工作区已有中间产物：" + "、".join(found) +
            "。先 run_command(ls -la intake) 盘点现状，基于既有产物继续剩余步骤，"
            "已完成的拉取/解析/文档产出不要重做；既有 tools/ 下脚本可复用。）")
