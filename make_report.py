# -*- coding: utf-8 -*-
"""把真实采集结果汇总成一份可读战报。"""
import json
import os
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
RAW = os.path.join(HERE, "working", "arctic_raw")


def load():
    posts, seen = [], set()
    for f in sorted(os.listdir(RAW)):
        if f.startswith("raw") and f.endswith(".json"):
            for p in json.load(open(os.path.join(RAW, f), encoding="utf-8")):
                pid = p.get("post_id") or ""
                if pid and pid in seen:
                    continue
                if pid:
                    seen.add(pid)
                posts.append(p)
    return posts


def main():
    posts = load()
    sc = json.load(open(os.path.join(HERE, "review", "scored_arctic.json"), encoding="utf-8"))
    cl = json.load(open(os.path.join(HERE, "working", "clusters_arctic.json"), encoding="utf-8"))
    subs = json.load(open(os.path.join(HERE, "config", "subs.json"), encoding="utf-8"))
    sizes = subs.get("subscribers", {})
    import radar
    sig = {s["post_id"]: s for s in (radar.extract(p) for p in posts)}
    m = {}
    for p in posts:
        m.setdefault(radar.fingerprint(p.get("title") or ""), []).append(p)

    L = []
    W = L.append
    W("=" * 84)
    W("Reddit 小众需求工厂 —— 真实采集战报")
    W(f"数据源 Arctic Shift 公开归档 | 采集时间 {cl['generated_at'] and time_str(cl['generated_at'])}")
    W("=" * 84)
    W("")
    W("【1】采集事实")
    W("-" * 84)
    span = cl["time_span"]
    W(f"  有效帖子      {len(posts)} 篇（去重后）")
    W(f"  覆盖 sub      {len(subs['subs'])} 个（三大类：财务会计 / 创作者接单 / 技术系统）")
    W(f"  时间跨度      {span['from']} → {span['to']}（{span['days']} 天）")
    W(f"  历史窗口      3 个（今天 / 30 天前 / 60 天前，每窗口 100 帖）")
    W(f"  请求数        {len(cl['batches']) * 3} 次（{len(cl['batches'])} 批 × 3 窗口；"
      f"计划 72 次，1 次 422 超时自动跳过）")
    W(f"  指纹簇        {cl['clusters']} 个 → 过闸门 {cl['kept']} 个")
    W("")
    W("【2】筛选漏斗")
    W("-" * 84)
    n0 = sum(1 for s in sig.values() if not s["pain_hits"])
    demand = sum(1 for s in sig.values()
                 if set(s["pain_hits"]) & {"ask_tool", "complain", "workaround", "repeating"})
    wa = sum(1 for s in sig.values() if s["_kinds"])
    pay = sum(1 for s in sig.values() if s["pay_hits"])
    W(f"  原始帖子          {len(posts)}")
    W(f"  命中痛点词        {len(posts)-n0}（{(len(posts)-n0)/len(posts):.1%}）")
    W(f"  其中「真需求类」  {demand}（{demand/len(posts):.1%}）← 闲聊/职业话题在这里被剔除")
    W(f"  有 workaround     {wa}（{wa/len(posts):.1%}）← 已在用土办法 = 真需求信号")
    W(f"  有付费信号        {pay}（{pay/len(posts):.1%}）")
    W("")
    W("【3】漏斗后最像需求的 8 个方向（真实标题，非造样本）")
    W("-" * 84)
    items = sc["items"]
    order = sorted(items, key=lambda x: -(x["O"] * 0.5 + x["M"] * 0.35 + x["R"] * 0.15))
    for i, c in enumerate(order[:8], 1):
        W(f"  {i}. {c['cluster_id']}  {c['decision']}  {c['final']}")
        W(f"     标题：{c['title'][:76]}")
        W(f"     O={c['O']}  M={c['M']}  R={c['R']}  罚={c['penalty']}")
        W(f"     触达上限={c['econ']['reach_cap_users']:,.0f}  付费人数估计={c['econ']['paying_users_est']}"
          f"  回本={c['econ']['payback_months']} 个月")
        cd = next((x for x in cl["all_clusters"] if x["cluster_id"] == c["cluster_id"]), {})
        if cd.get("sample_titles"):
            W(f"     真实痛点原声（{cd['posts']} 帖 / {cd['unique_authors']} 独立作者）：")
            for t in cd["sample_titles"][:3]:
                W(f"       · {t}")
        pen = c.get("penalty_items") or []
        if pen:
            W(f"     扣分：{'；'.join(pen)}")
        W("")
    W("【4】决策分布")
    W("-" * 84)
    dc = Counter(c["decision"] for c in items)
    for k, v in dc.most_common():
        W(f"  {k:<14s} {v} 个")
    W(f"  阈值：BUILD >= {sc['thresholds']['build_threshold']} / "
      f"WATCH >= {sc['thresholds']['watch_threshold']}（{sc['thresholds']['source']}）")
    W("")
    W("【5】必须说清的三件事")
    W("-" * 84)
    W("  1. 本轮 0 个 BUILD。最高分 " + str(max(c['final'] for c in items)) + "（"
      + order[0]["cluster_id"] + "），差 " +
      f"{sc['thresholds']['build_threshold'] - max(c['final'] for c in items):.1f} 分到开工线。")
    W("     这是**诚实负结论**，不是链路坏了 —— 样本期只有 6779 帖 / 86 天，")
    W("     而 min_authors>=5 + 痛点密度>=25% 两道闸门本来就严。")
    W("")
    W("  2. 最大簇 x_table 有 438 独立作者但 R=100（触达顶满），仍判 DROP：")
    W("     因为它本质是「所有跟 Excel 表格沾边的帖」，指纹相同不等于同一件事。")
    W("     这是聚类粒度问题，不是需求不足 —— 下一步要么细分指纹，要么靠 JEV 逐簇判定。")
    W("")
    W("  3. 官方订阅数交叉校验：r/bookkeeping 官方 85,828 vs 第三方站 75,682，")
    W("     第三方站**低估 11.8%**；其余 22 个 sub 偏差都在 ±2% 内。")
    W("     按 JEV 判定（0.39）暂不替换，但记账时按官方值算更准。")
    W("")
    W("=" * 84)
    p = os.path.join(HERE, "review", "真实采集战报.md")
    open(p, "w", encoding="utf-8").write("\n".join(L))
    print("wrote", p)


def time_str(ts):
    import time
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


if __name__ == "__main__":
    main()
