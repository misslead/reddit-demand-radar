# -*- coding: utf-8 -*-
"""daily_report.py —— 每日付费意愿日报生成器（v1.1.7）。

用户要求（2026-10-05）：做成定时任务，每天查询并给出报告，**特别需要报告付费意愿较强的
细分项目**，不局限于财务领域，但必须**远程可做、AI 可做**。

JEV 判定（choice）：报告**严格按付费意愿分排序，只输出前 3-5 名 + 原文引证**
（prob 0.42 / conf 0.22 灰区 → 取概率最高项）。取它的理由与用户明确要求一致：
「特别需要付费意愿较强的细分」就是排序键。

为什么远程闸门不是过滤而是前置条件（JEV choice：硬过滤 + 合规标注）：
  - `remote_ok=false` 的直接不进入 TOP5，在报告末尾单独列「被远程闸门挡掉的」，
    让用户知道存在但不可做，而不是凭空消失
  - `verdict=需人工`（合规敏感）可以进 TOP5 但强制打警示 —— 这类往往付费意愿最强，
    不该因为合规就藏起来，但也不能假装没风险

输入：working/segments.json（segment.py 产出）
输出：reports/daily-YYYY-MM-DD.md（每日一份，保留 30 天）
"""
from __future__ import annotations

import datetime as dt
import glob
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from make_seg_report import strength, verdict  # noqa: E402

SEG = os.path.join(HERE, "working", "segments.json")
REPORTS = os.path.join(HERE, "reports")
KEEP_DAYS = 30
TOP_N = 5
MIN_AUTHORS = 4


def today_str():
    return dt.date.today().isoformat()


def load():
    with open(SEG, encoding="utf-8") as f:
        d = json.load(f)
    for s in d["segments"]:
        s["strength"] = strength(s)
        s["verdict"], s["verdict_why"] = verdict(s, s["strength"])
    return d


def prune():
    """只保留最近 KEEP_DAYS 份日报，防止目录无限膨胀。"""
    files = sorted(glob.glob(os.path.join(REPORTS, "daily-*.md")))
    for p in files[:-KEEP_DAYS]:
        try:
            os.remove(p)
        except OSError:
            pass


def domain_of(sub):
    """单个 sub 属于哪个领域组。sub 名大小写不敏感，可能带 r/ 前缀。"""
    try:
        cfg = json.load(open(os.path.join(HERE, "config", "subs.json"), encoding="utf-8"))
    except OSError:
        return "未分类"
    name = (sub or "").strip().lstrip("/").lower()
    if name.startswith("r/"):
        name = name[2:]
    labels = {"finance_office": "财务/办公", "dev_data": "开发/数据",
              "creator_hobby": "创作/兴趣", "business_ops": "商业运营"}
    for g, subs in (cfg.get("_domain_groups") or {}).items():
        if name in {s.lower() for s in subs}:
            return labels.get(g, g)
    return "未分类"


def domain_mix(subs):
    """一个簇常跨多个社区，领域要按多数派算（票数 = 该 sub 出现在簇里的次数不可得，
    故用社区集合 + 领域占比，取占比最高者；跨 >=2 个领域标「跨领域」）。"""
    from collections import Counter
    cnt = Counter(domain_of(s) for s in (subs or []))
    cnt.pop("未分类", None)
    if not cnt:
        return "未分类"
    top, n = cnt.most_common(1)[0]
    real = sum(cnt.values())
    if len(cnt) >= 2 and n / real < 0.6:
        return f"跨领域（主：{top}）"
    return top


def main():
    d = load()
    segs = [s for s in d["segments"] if s["unique_authors"] >= MIN_AUTHORS]
    remote_ok = [s for s in segs if s.get("remote_ok", True)]
    blocked = [s for s in segs if not s.get("remote_ok", True)]
    remote_ok.sort(key=lambda x: (x["pay_score"], x["unique_authors"]), reverse=True)
    top = remote_ok[:TOP_N]

    date = today_str()
    os.makedirs(REPORTS, exist_ok=True)
    out = os.path.join(REPORTS, f"daily-{date}.md")

    L = []
    W = L.append
    W(f"# 每日付费意愿雷达 · {date}")
    W("")
    W(f"> 数据源 Reddit 公开归档（Arctic Shift）｜ 样本 {d['total_posts']} 帖 ｜ "
      f"细分 {len(segs)} 类（作者 ≥{MIN_AUTHORS}）｜ 排序键：**付费意愿分**  ")
    W(f"> 付费意愿分 = 工具名提及 {d['weights']['tool_mention']:.0%} + 主动找替代 "
      f"{d['weights']['pay_intent']:.0%}，按**作者去重**计票  ")
    W(f"> 准入：远程可做 ✅ + AI 可做（附分）。不合规则不进入 TOP5，见文末。")
    W("")
    W("---")
    W("")
    W(f"## 今日 TOP {len(top)}：付费意愿最强的远程/AI 可做细分")
    W("")
    if not top:
        W("**今日无达标细分。** 作者数 ≥4 且通过远程闸门的候选为 0。")
    for i, s in enumerate(top, 1):
        comp = f" ⚠️合规敏感（{', '.join(s['compliance_tags'])}）" if s.get("compliance_tags") else ""
        W(f"### {i}. {s['biz_cn']}　付费意愿 **{s['pay_score']}**　AI 可做 {s['ai_score']}（{s['ai_level']}）{comp}")
        W("")
        W(f"- **规模**：{s['unique_authors']} 位独立作者 / {s['posts']} 帖 ｜ "
          f"痛点密度 {s['demand_ratio']:.0%} ｜ 土办法 {s['workaround_rate']:.0%}")
        W(f"- **付费证据**：主动找替代 {s['alt_seek']} 帖、明说愿付 {s['will_pay']} 帖、"
          f"嫌贵 {s['too_expensive']} 帖，跨 {s['cross_sub_count']} 个社区")
        W(f"- **预算状态**：{s['budget_state']}")
        W(f"- **巨头占位**：{s['giant_covered_by']}")
        W(f"- **领域**：{domain_mix(s.get('subs'))} ｜ 主要社区："
          f"{', '.join('r/' + x for x in s['subs'][:5])}")
        W(f"- **筛选判断**：{s['verdict']} —— {s['verdict_why']} ｜ 需求强度分 {s['strength']}")
        if s.get("remote_reasons"):
            W(f"- **可行性**：{' ｜ '.join(s['remote_reasons'])}")
        nt = "、".join(t for t, _ in s["non_tool_tags"])
        if s.get("non_tool_posts"):
            W(f"- ⚠️ 混有 {s['non_tool_posts']} 帖非工具内容（{nt}），已标记待人工")
        th = s.get("title_hit_posts", 0)
        if th == 0:
            W("- ⚠️ **标题级证据为 0**：本细分所有帖子都只在正文里提到这件事，"
              "主题并非该细分 —— 下方的原声只能算「顺带提及」，**结论不可信**")
        elif th < 3:
            W(f"- ⚠️ 仅 **{th} 帖标题**直接谈这件事（占 {th/max(s['posts'],1):.0%}），"
              f"其余为正文顺带提及 —— 证据偏弱，建议再攒几天样本")
        W("")
        if s["sample_titles"]:
            W("用户原声（标题直接谈这件事的帖）：")
            W("")
            for t in s["sample_titles"][:4]:
                W(f"> {t}")
            W("")
    W("---")
    W("")
    W("## 全部达标细分（按付费意愿分）")
    W("")
    W("| # | 细分 | 付费意愿 | 强度 | 主动意愿 | 痛点 | 土办法 | 作者 | 标题证据 | AI | 预算状态 | 判断 |")
    W("|--:|---|--:|--:|--:|--:|--:|--:|--:|--:|---|---|")
    for i, s in enumerate(remote_ok, 1):
        th = s.get("title_hit_posts", 0)
        ths = f"{th}/{s['posts']}" + ("⚠️" if th == 0 else ("△" if th < 3 else ""))
        W(f"| {i} | {s['biz_cn']} | **{s['pay_score']}** | {s['strength']} | "
          f"{s['pay_intent_rate']:.0%} | {s['demand_ratio']:.0%} | {s['workaround_rate']:.0%} | "
          f"{s['unique_authors']} | {ths} | {s['ai_score']} | {s['budget_state']} | {s['verdict']} |")
    W("")
    W("**标题证据** = 标题本身就谈这件事的帖数 / 总帖数。这是最硬的证据等级："
      "只在正文顺带提到的，说明这批人的主题不是这件事 ⚠️=零、△=少于 3 帖。")
    W("")
    if blocked:
        W("---")
        W("")
        W("## 被远程闸门挡掉（存在需求但不可远程交付）")
        W("")
        W("| 细分 | 付费意愿 | 作者 | 命中的否决线 |")
        W("|---|--:|--:|---|")
        for s in sorted(blocked, key=lambda x: -x["pay_score"])[:10]:
            W(f"| {s['biz_cn']} | {s['pay_score']} | {s['unique_authors']} | "
              f"{', '.join(s.get('remote_blockers') or [])} |")
        W("")
        W("> 这些不是「假需求」——它们的付费意愿可能很强，只是需要线下到场或动手，"
          "不符合远程交付条件。若哪天接受线下交付，可以回看这张表。")
        W("")
    W("---")
    W("")
    W("## 方法与局限（每日必读，避免被数字误导）")
    W("")
    W("1. **「提工具名」不是付费意愿**。在按软件命名的社区里（如 r/QuickBooks 52%）"
      "人人都在谈那个工具，那是社区基线。付费分已把「工具名提及」压到 40%，"
      "主信号是「主动找替代」（I'd pay / 太贵了 / 有没有替代品 / 想退订）。")
    W("2. **付费意愿高 ≠ 值得做**。「只付费不痛」说明多数帖不是在抱怨这件事，只是社区爱聊工具。"
      "「只痛不付费」说明痛点真实但免费替代品已够用。两者都不该开工。")
    W("3. **今日结论仍需落地页验证**。报告给的是「谁在抱怨 + 抱怨强度 + 预算证据」，"
      "不是付费预测。开工前必须用真实报价的落地页验一次。")
    W("4. **看「标题证据」这一列**。细分是按「业务对象」正则归类的，而正则匹配的是"
      "标题+正文全文 —— 一篇讲别的事的帖只要正文提了一句 budget 就会被归入预算桶。"
      "只有「标题本身就谈这件事」的帖才算主题证据。⚠️=零证据（结论不可信）、"
      "△=不足 3 帖（偏弱）。原声列表也只取标题命中的帖。")
    W(f"5. 样本来自 {d['total_posts']} 帖；细分作者数少的结论噪声大，"
      "低于 10 人仅供参考、不作决策依据。")
    W("")
    W("复算：`python factory/segment.py && python factory/daily_report.py`")

    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    # 最新副本固定名，方便「打开就能看」
    with open(os.path.join(REPORTS, "latest.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    prune()
    print("wrote", out)
    print(f"TOP{len(top)}: " + " | ".join(
        f"{s['biz_cn']} {s['pay_score']}({s['ai_score']})" for s in top))
    print(f"blocked={len(blocked)}  kept_reports={len(glob.glob(os.path.join(REPORTS, 'daily-*.md')))}")
    return out


if __name__ == "__main__":
    main()
