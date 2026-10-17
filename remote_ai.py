# -*- coding: utf-8 -*-
"""remote_ai.py —— 「远程可做 + AI 可做」可行性判定（v1.1.7 新增）。

用户约束（2026-10-05）：项目必须 (a) 能完全远程做，不需线下到场/本地设备/现场作业，
(b) 能用 AI 辅助开发。

JEV 判定（choice）：**硬过滤 + 单独标注合规敏感领域**（prob 0.42，confidence 0.23 灰区 → 取概率最高项）。
理由：软打分会把约束稀释成排序里的一个权重，用户明确说「特别需要远程可做 AI 可做的项目」，
说明这是**准入门槛**而不是偏好。合规敏感领域（税务/法律/医疗/保险）不直接砍 —— 它们恰恰是
付费意愿最强的池子 —— 但必须打警示标，让人工决定要不要接。

三条否决线（任一命中即剔除）：
  1. 线下到场    —— 必须去现场、面对面、上门、寄实物才能做
  2. 物理操作    —— 需要真的动手/搬东西/开车/操作硬件
  3. 实时线下响应 —— 必须本人在线守着（本地跑批、盯着屏幕、接打即时电话）
AI 辅助度（0-100）：
  强 —— 代码/文本/图像/音频/数据处理类，AI 能写掉 70%+ 的实现
  中 —— 有明确规则但需要大量领域配置
  弱 —— 核心是判断/沟通/手感，AI 帮不上

⚠️ 这不是「保证能做成」的判断，只是把明显做不了的挡在门外。真正的可行性还要落地页验。
"""
from __future__ import annotations

import re

# ---------------------------------------------------------------- 硬否决线
# 命中即剔除。注意：这些是**细分业务对象**层面的判断，不是帖子层面 ——
# 一条业务对象可能既能远程做又有线下变体，那属于「needs_judgment」交人工。

# 1) 线下到场 / 必须面对面
OFFLINE_PRESENCE = [
    ("in_person_sales", r"\b(in[- ]person|on[- ]site|face[- ]to[- ]face|walk[- ]in|"
                         r"at (the|my) (store|shop|location|office)|in the (store|shop))\b"),
    ("home_visit",       r"\b(home (visit|health|inspection|survey)|site (visit|survey|inspection)|"
                         r"visit(s|ing)? (the )?(client|customer|home|house|property)|"
                         r"on[- ]location|onsite)\b"),
    ("physical_class",   r"\b(in[- ]person (class|training|session|lesson|tutorial)|"
                         r"at (my|the) (kitchen|garage|garden|yard|basement))\b"),
]

# 2) 物理操作 / 必须动手
PHYSICAL_ACTION = [
    ("repair_fix",     r"\b(repair|fix (the|a|my) (leak|hole|fence|roof|machine|engine|pipe))|"
                       r"(plumbing|electrical|hvac|plaster|roofing|tiling|masonry)\b"),
    ("3d_hand_work",   r"\b(assembl(e|ing|y)|weld(ing)?|solder(ing)?|knit(t|ting)|sew(ing|n)?|"
                       r"carpentry|wood ?work(ing)?|paint(ing)? (the|my|a) (wall|room|house))\b"),
    ("ship_logistics", r"\b(ship(ping|ment)|fulfil+ment|pick(ing|up)? (and|and )?pack|"
                       r"courier|freight|delivery (driver|route)|last mile)\b"),
    ("vehicle",        r"\b(vehicle|car|truck|fleet|driving|delivery route|"
                       r"towing|engine|brake|tyre|tire|transmission)\b"),
    ("fishing_field",  r"\b(on the water|trolling|casting|boating|reef|"
                       r"(at|on) the (lake|river|sea)|fly ?fish(ing)?)\b"),
]

# 3) 实时线下响应 / 必须本人在线守着（本地跑批、盯屏、即时接打）
REALTIME_ONLINE = [
    ("live_monitoring", r"\b(24/7|real[- ]time|around the clock|live (chat|support)|"
                        r"on[- ]call|standby|monitor(ing)? (the|it) (live|now))\b"),
    ("local_batch",     r"\b(runs? (on|in) (my|our|the) (local|machine|pc|laptop|desktop)|"
                        r"offline|air[- ]?gapped|on[- ]premise|on[- ]prem)\b"),
    ("in_person_shift", r"\b(shift|on[- ]site (staff|cover)|cover the (desk|store|office))\b"),
]

# ---------------------------------------------------------------- 合规敏感（不砍，标警示）
# 这些细分付费意愿最强，但需要专业资质 / 受监管，AI 工具只能做「辅助」不能做「替代」
COMPLIANCE_SENSITIVE = [
    ("tax_advice",     r"\b(tax (advice|filing|return|season)|file(d|ing)? (my|the) taxes?|"
                       r"irs\b|cpa\b|tax prepar)\b"),
    ("legal_advice",   r"\b(legal (advice|opinion)|attorney|lawyer|bar exam|"
                       r"contract review|dispute|litigat|lawsuit)\b"),
    ("medical_clinical", r"\b(diagnos(is|e)|symptom|symptom(s)?|prescri(be|ption)|dosage|"
                         r"treatment|therapy|mental health|patient|clinical|medical (advice|record))\b"),
    ("insurance_advice", r"\b(insurance (claim|claim|adjuster|underwrit)|policy (renewal|holder)|"
                         r"coverage (decision|dispute)|pre[- ]existing condition)\b"),
    ("financial_advice", r"\b(invest(ment|or) advice|financial plan(n|ning)|retirement plan|"
                         r"tax shelter|portfolio allocation|rebalanc)\b"),
    ("immigration",    r"\b(visa|immigration|asylum|deport|work permit|green card)\b"),
]

# ---------------------------------------------------------------- AI 辅助度基准（v1.1.7）
# 实测踩坑：原来只拿「业务对象的中英文名」去撞通用词表（AI_STRONG 里含 csv/excel/
# report/invoice…），结果 `budget`（预算与现金流）这种**名字里没有可自动化词**的细分
# 一条都撞不上 → 恒得 40 分被判「弱」，而它其实是典型的文本/表格自动化活。
# 改成：每个业务对象给一个**人工标定的基准分**（依据是该细分的核心产出物是否
# 可由 AI 生成/变换），再叠加命中词的微调。基准分是主，词表命中只作说明。
BIZ_AI_BASE = {
    # 文档/表格/数据类 —— AI 能写掉大部分实现
    "bank_stmt": 85, "reconcile": 85, "csv_file": 90, "ledger": 75, "ap_ar": 80,
    "invoice": 85, "expense": 85, "budget": 80, "report": 85, "formula": 90,
    "format": 85, "payroll": 80, "tax": 60, "inventory": 75, "email_doc": 85,
    "pdf": 85, "data_pipeline": 95, "devops_site": 85, "api_data": 95,
    # 内容/多媒体类 —— AI 强项
    "photo": 90, "video": 80, "subtitle": 95, "social_content": 90,
    "design_asset": 85, "podcast": 90, "seo_content": 90, "ads_campaign": 80,
    "language_text": 95, "genealogy": 70, "crm_sales": 75,
    # 规则密集但需领域配置 —— AI 帮得上，要人喂规则
    "ecom_ops": 70, "hr_ops": 65, "customer_support": 80, "realestate_ops": 60,
    "legal_admin": 60, "health_admin": 55, "propertymanagement": 60,
    "home_admin": 55, "prepmaterial": 70, "prep_material": 70,
    "chase_docs": 65, "scope_quote": 55,
    # 强依赖线下/手工/判断 —— AI 帮不上
    "diy": 35, "3dprint": 30, "fishing": 25, "game": 40, "model": 20,
    "keyboard": 45, "sysadmin": 75, "appointment": 40,
}
DEFAULT_AI_BASE = 65
# 强 = 代码/文本/图像/音频/数据处理，AI 能写掉大部分实现
AI_STRONG = [
    ("text_data",   r"\b(csv|excel|spreadsheet|report|dashboard|transcri|translat|"
                    r"summary|summariz|invoice|bill(ing)?|stat(istic)?|data|export|import|"
                    r"catalog|inventory|seo|keyword|content|copy|script|automation|"
                    r"spreadsheet|formula|database|api|integration)\b"),
    ("image_video", r"\b(photo|image|video|edit(ing)?|render(ing)?|thumbnail|design|"
                    r"template|layout|color|colou?r|font|logo|audio|music|podcast|"
                    r"subtitle|caption|transcri|ocr|lightroom|premiere)\b"),
    ("code_web",    r"\b(code|script|automation|api|integration|web ?site|wordpress|"
                    r"plugin|extension|browser|macro|bot|scrap(e|ing)|deploy|server)\b"),
]
AI_MODERATE = [
    ("rule_heavy",  r"\b(compliance|policy|procedure|template|workflow|checklist|"
                    r"schedule|calendar|invoice|billing|payroll|contract|lease|"
                    r"claim|renewal|onboarding|training|curriculum|survey|feedback)\b"),
]
# 弱：核心是判断/沟通/手感/现场
AI_WEAK = [
    ("taste_judgment", r"\b(desi(g[n|gn]?|gn) (my|your) |look|feel|quality|"
                       r"client (relations?|manage)|negotia(t|te)|sales ?clos(e|ing)|"
                       r"interview|teach(ing)?|train(ing)?|counsel(ing)?|therap)\b"),
]

BARRIERS = {
    "in_person_sales": "必须面对面/到店成交，远程交付不了",
    "home_visit": "需要上门（家访/巡检/现场勘察）",
    "physical_class": "必须线下当面授课/操作",
    "repair_fix": "需要动手修，装配类工作",
    "3d_hand_work": "需要手工组装/焊接/缝纫",
    "ship_logistics": "依赖真实发货与物流履约",
    "vehicle": "依赖车辆/引擎/驾驶等实体硬件",
    "fishing_field": "需要本人到水边/户外实操",
    "live_monitoring": "要求 7×24 实时在线响应",
    "local_batch": "必须在本机/内网跑，不能上云",
    "in_person_shift": "需要排班在岗（门店/前台）",
}


# 样本标题要多大比例同向命中才算否决（一条帖里提到 shift ≠ 这个细分要排班）
TITLE_VOTE_RATIO = 0.25


def judge(text, compliance_sensitive_subs=None, sample_titles=None):
    """判定一段业务对象的「远程可做 + AI 可做」可行性。

    返回 dict：
      remote_ok    True/False     是否通过全部硬否决线
      ai_score     0-100          AI 辅助度
      ai_level     强/中/弱
      compliance   [标签]         合规敏感标签（不否决，只警示）
      blockers     [(标签, 说明)] 命中的否决线
      verdict      可做 / 需人工 / 不建议
      reasons      [说明]

    ⚠️ v1.1.7 修正（实测踩坑）：**否决线只判业务对象名，帖子标题只能作辅助证据**。
    实测把 25 条样本标题喂进来直接判之后，`\\bshift\\b`（排班/轮班）、`\\bcar\\b`、
    `assembl(e|ing)`、`paint(ing)` 这些常见词把「分类账与凭证录入」「照片批处理」
    等 15 个细分整簇误判成不可远程 —— 一条帖提到 shift 不代表这个细分要排班。
    现在规则：**对象名命中即否决**；标题里命中数 ≥ 25% 才算数（写进 notes 供人看）。
    """
    t = text or ""
    titles = sample_titles or []
    blockers, notes = [], []
    for group in (OFFLINE_PRESENCE, PHYSICAL_ACTION, REALTIME_ONLINE):
        for name, pat in group:
            why = BARRIERS.get(name, name)
            if re.search(pat, t, re.I):
                blockers.append((name, why + "（业务对象本身命中）"))
                continue
            n_title = sum(1 for x in titles if re.search(pat, x, re.I))
            if titles and n_title / len(titles) >= TITLE_VOTE_RATIO:
                blockers.append((name, f"{why}（{n_title}/{len(titles)} 条样本标题同向命中）"))
            elif n_title:
                notes.append(f"{name} 零星提及 {n_title} 条，不足以否决")

    comp = [name for name, pat in COMPLIANCE_SENSITIVE if re.search(pat, t, re.I)]
    if compliance_sensitive_subs:
        low = {s.lower() for s in compliance_sensitive_subs}
        # 帖/簇来自这些 sub 时，只提示不否决（sub 是社区，不代表细分本身敏感）
        if any(s in low for s in re.findall(r"r/([A-Za-z0-9_]+)", t)):
            comp.append("community_sensitive")

    if blockers:
        return {"remote_ok": False, "ai_score": 0, "ai_level": "—",
                "compliance": sorted(set(comp)), "blockers": blockers,
                "verdict": "不建议",
                "reasons": [f"命中 {len(blockers)} 条硬否决线：{blockers[0][1]}"]}

    # AI 辅助度：**人工标定的业务对象基准分为主**，命中词表只作说明性微调。
    # v1.1.7 修正两处：① 基线 55 让 strong=0 的细分全挤在 55，无区分度；
    # ② 只拿业务对象名撞通用词表，导致 budget 这类名字里没有可自动化词的细分恒得低分。
    seg_key = (text or "").strip().split()[0] if (text or "").strip() else ""
    base = BIZ_AI_BASE.get(seg_key, DEFAULT_AI_BASE)
    strong = len(set(_iter_hits(AI_STRONG, t)))
    moderate = len(set(_iter_hits(AI_MODERATE, t)))
    weak = len(set(_iter_hits(AI_WEAK, t)))
    score = max(0, min(100, base + 4 * strong + 2 * moderate - 8 * weak))
    level = "强" if score >= 65 else ("中" if score >= 45 else "弱")
    parts = [f"基准 {base}（按该业务对象的产出物是否可由 AI 生成/变换人工标定）"]
    if strong or moderate or weak:
        parts.append(f"词表命中：可自动化 {strong} 类、规则密集 {moderate} 类、需判断 {weak} 类"
                     f"（±{4 * strong + 2 * moderate - 8 * weak:+d}）")
    reasons = [f"AI 辅助度{level}（{score}）：" + "；".join(parts)]

    verdict = "可做"
    if notes:
        reasons.append("线下词零星出现但未成气候：" + "；".join(notes[:3]))
    if comp:
        verdict = "需人工"
        reasons.append(f"合规敏感（{', '.join(sorted(set(comp)))}）——付费意愿最强但需资质，建议只做辅助工具不做决策替代")
    return {"remote_ok": True, "ai_score": score, "ai_level": level,
            "compliance": sorted(set(comp)), "blockers": [],
            "verdict": verdict, "reasons": reasons}


def _iter_hits(table, text):
    for name, pat in table:
        if re.search(pat, text, re.I):
            yield name
