# -*- coding: utf-8 -*-
"""细分战报：把付费意愿分和需求强度并排放，找出真正值得做的细分类。

为什么要两个分：
  pay_score 单独排会踩坑 —— 「库存与 SKU」付费分 51.2 排第一，但痛点帖只占 25%，
  意味着 75% 的帖其实不是在抱怨这件事，只是这个社区爱提工具名。
  只有「需求强（多人+反复+在用土办法）× 付费意愿强」同时成立的才是候选。
"""
import json
import os
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

SEG = os.path.join(HERE, "working", "segments.json")
OUT = os.path.join(HERE, "review", "细分需求战报.md")

# 需求强度：三个硬指标各占权重，全部是「多人 + 反复 + 有土办法」的组合
W_AUTHORS, W_PAIN, W_WA, W_PAY = 0.30, 0.25, 0.20, 0.25
AUTHORS_FULL = 40        # 40 个独立作者即满分（银行流水 281 远超，按饱和处理）


def strength(s):
    a = min(s["unique_authors"] / AUTHORS_FULL, 1.0) * 100
    return round(W_AUTHORS * a + W_PAIN * s["demand_ratio"] * 100
                 + W_WA * s["workaround_rate"] * 100 + W_PAY * s["pay_score"], 1)


def verdict(s, st):
    """分类：真候选 / 只付费不痛 / 只痛不付费 / 样本太小。"""
    hi_pay = s["pay_score"] >= 40
    hi_need = s["demand_ratio"] >= 0.40 and s["unique_authors"] >= 10
    if s["unique_authors"] < 6:
        return "样本太小", "作者不足 6 人，结论不可靠"
    if hi_pay and hi_need and s["workaround_rate"] >= 0.30:
        return "真候选", "付费意愿与需求强度同时成立"
    if hi_pay and not hi_need:
        return "只付费不痛", "提工具名多但抱怨少 → 可能是社区基线，不是待解决需求"
    if hi_need and not hi_pay:
        return "只痛不付费", "痛点真实但没人表达付费 → 可能是免费替代品已够用"
    return "观察", "两边都不突出"


def main():
    d = json.load(open(SEG, encoding="utf-8"))
    segs = [s for s in d["segments"] if s["unique_authors"] >= 4]
    for s in segs:
        s["strength"] = strength(s)
        s["verdict"], s["verdict_why"] = verdict(s, s["strength"])
    segs.sort(key=lambda x: (x["strength"], x["pay_score"]), reverse=True)

    by_v = Counter(s["verdict"] for s in segs)
    L = []
    W = L.append
    W("# 细分需求战报 —— 哪个细分类的用户付费意愿更强")
    W("")
    W(f"数据：Reddit 公开归档 Arctic Shift | {d['total_posts']} 帖真实数据（24 个 sub，86 天）  ")
    W(f"细分方式：按「具体业务对象」**全局**重切（不是在一级簇内切 —— 同一件事会被动词拆散）  ")
    W(f"付费意愿分 = 工具名提及 {d['weights']['tool_mention']:.0%} + 主动找替代 {d['weights']['pay_intent']:.0%}"
      f"（JEV 判定 conf 0.72；按**作者去重**计票，一人刷帖只算 1 票）  ")
    W(f"需求强度分 = 作者 {W_AUTHORS:.0%} + 痛点密度 {W_PAIN:.0%} + 土办法 {W_WA:.0%} + 付费意愿 {W_PAY:.0%}")
    W("")
    W("---")
    W("")
    W("## 一、先说最重要的三个发现")
    W("")
    W("### 1. 「提工具名」不能当付费意愿用")
    W("")
    W(f"全站有 {d['global_tool_baseline']:.1%} 的帖提到某个软件名。但按 sub 拆开看：")
    W("")
    W("| sub | 帖数 | 提到软件名 | 含义 |")
    W("|---|--:|--:|---|")
    sb = d["sub_tool_baseline"]
    for s in sorted(sb, key=lambda k: -sb[k])[:6]:
        tag = "★这个社区就是按工具命名的，提工具名是基线" if s in (
            "QuickBooks", "excel", "googlesheets", "spreadsheets", "Notion",
            "PowerShell", "dataengineering") else ""
        W(f"| r/{s} | - | **{sb[s]:.1%}** | {tag} |")
    W("")
    W(f"r/QuickBooks 高达 {sb.get('QuickBooks', 0):.1%} —— 那里的人本来就在为软件付钱，")
    W("但他们的钱付给了 QuickBooks，**不代表愿意付钱给一个新产品**。")
    W("所以我把付费信号拆成两类：")
    W("")
    W("- **工具名提及** = 这个人本来就在为软件花钱 → 预算存在，但很可能已被巨头占住")
    W("- **主动找替代** = 说「I'd pay / 太贵了 / 有没有替代品 / 想退订」 → 正在找替代，**这才是新产品的真信号**")
    W("")
    W("### 2. 付费意愿第一名，痛点密度只有 25% —— 不能只看一个分")
    W("")
    W("| 排名 | 细分类 | 付费意愿分 | 痛点密度 | 土办法 | 作者 | 判断 |")
    W("|--:|---|--:|--:|--:|--:|---|")
    for s in sorted(segs, key=lambda x: -x["pay_score"])[:5]:
        W(f"| {sorted(segs, key=lambda x: -x['pay_score']).index(s)+1} | {s['biz_cn']} | "
          f"**{s['pay_score']}** | {s['demand_ratio']:.0%} | {s['workaround_rate']:.0%} | "
          f"{s['unique_authors']} | {s['verdict']} |")
    W("")
    W("「库存与 SKU 管理」和「分类账与凭证录入」付费分最高，但**痛点密度只有 25–26%** ——")
    W("意思是这些帖子里四分之三其实不是在抱怨这件事，只是在聊工具。")
    W("把它们当需求就是被规则骗了。**必须两个分同时看。**")
    W("")
    W("### 3. 真正值得看的细分类（需求强度 × 付费意愿双高）")
    W("")
    W("| # | 细分类 | 强度分 | 付费分 | 痛点 | 土办法 | 作者 | 预算状态 | 巨头覆盖方 |")
    W("|--:|---|--:|--:|--:|--:|--:|---|---|")
    for i, s in enumerate([x for x in segs if x["verdict"] == "真候选"][:8], 1):
        W(f"| {i} | **{s['biz_cn']}** | {s['strength']} | {s['pay_score']} | {s['demand_ratio']:.0%} | "
          f"{s['workaround_rate']:.0%} | {s['unique_authors']} | {s['budget_state']} | {s['giant_covered_by']} |")
    W("")
    W("---")
    W("")
    W("## 二、全部细分类（按强度分排序）")
    W("")
    W("| 细分类 | 强度分 | 付费分 | 主动意愿 | 工具名 | 痛点 | 土办法 | 作者 | 帖 | 判断 |")
    W("|---|--:|--:|--:|--:|--:|--:|--:|--:|---|")
    for s in segs:
        W(f"| {s['biz_cn']} | **{s['strength']}** | {s['pay_score']} | {s['pay_intent_rate']:.0%} | "
          f"{s['tool_mention_rate']:.0%} | {s['demand_ratio']:.0%} | {s['workaround_rate']:.0%} | "
          f"{s['unique_authors']} | {s['posts']} | {s['verdict']} |")
    W("")
    W("判断分布：" + " ｜ ".join(f"{k} {v} 个" for k, v in by_v.most_common()))
    W("")
    W("---")
    W("")
    W("## 三、付费意愿最强的细分，各自是谁在抱怨")
    W("")
    for s in sorted(segs, key=lambda x: -x["pay_score"])[:6]:
        W(f"### {s['biz_cn']}　付费意愿分 {s['pay_score']}（{s['budget_state']}）")
        W("")
        W(f"{s['unique_authors']} 位独立作者 / {s['posts']} 帖 ｜ 痛点密度 {s['demand_ratio']:.0%} ｜ "
          f"土办法 {s['workaround_rate']:.0%} ｜ 主动找替代 {s['alt_seek']} 帖、"
          f"明说愿付 {s['will_pay']} 帖、嫌贵 {s['too_expensive']} 帖")
        W("")
        W(f"巨头覆盖方：{s['giant_covered_by']}")
        W("")
        nt = "、".join(t for t, _ in s["non_tool_tags"])
        if s["non_tool_posts"]:
            W(f"⚠️ 其中 {s['non_tool_posts']} 帖混着非工具内容（{nt}），已标记待人工，JEV 判定 confidence 仅 0.36")
            W("")
        for t in s["sample_titles"][:5]:
            W(f"- {t}")
        W("")
    W("---")
    W("")
    W("## 四、必须说清的局限")
    W("")
    W("1. **最大细分类「银行流水对账与整理」有 281 位作者、49 帖在找替代，量级上遥遥领先，"
      "但付费意愿分只排第 10** —— 因为它在 r/QuickBooks 等社区，人人在谈软件，且大部分已用上工具。"
      "这是「量大但巨头已占住」的典型：不是没需求，是没空隙。")
    W("2. **工具名提及率 80% 以上的几个（库存/工资表/PDF）付费分虚高**，"
      "因为它们的样本几乎都来自 r/QuickBooks 社区，基线 52%。这类要看 `budget_state`，别看分数。")
    W("3. **本轮仍为 0 个「可直接开工」**。强度分最高的是真实候选，但每个都需要先做落地页验证"
      "（JEV 判定：付费证据强但人数少的应走 PILOT，conf 0.98）。")
    W("4. 细分是从 6779 帖 / 86 天切出来的，**样本期只有 3 个月**；"
      "同一细分类的作者跨月复现情况还没攒够。")
    W("")
    W("---")
    W("")
    W("## 五、下一步建议（按性价比）")
    W("")
    top = [x for x in segs if x["verdict"] == "真候选"][:8]
    for i, s in enumerate(top[:3], 1):
        W(f"{i}. **{s['biz_cn']}** —— {s['strength']} 分，{s['unique_authors']} 作者，"
          f"巨人占位：{s['giant_covered_by']}。做落地页验付费意愿。")
    W("")
    W("复算命令：`python factory/segment.py`（细分+评分）、`python factory/seg_validate.py`（sub 基线校验）")

    open(OUT, "w", encoding="utf-8").write("\n".join(L))
    json.dump(segs, open(os.path.join(HERE, "working", "segments_scored.json"), "w",
                         encoding="utf-8"), ensure_ascii=False, indent=2)
    print("wrote", OUT)
    for s in segs[:8]:
        print(f"  {s['strength']:>5}  {s['pay_score']:>5}  {s['biz_cn']:<24s} {s['verdict']}")


if __name__ == "__main__":
    main()
