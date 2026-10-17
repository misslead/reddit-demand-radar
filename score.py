#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""投入产出 / 付费获利评分：O(机会) + M(变现) + R(ROI) -> Final -> 决策（v1.1）

用法:
  score.py --clusters working/clusters.json --out review/scored.json
           [--form web-single-tool] [--db db/factory.db] [--no-jev] [--dry-run]

v1.1 修复（Codex BLOCKER #6/#7/#10/#12 + 代码 bug 13 处）:
  - 低置信维度按文档承诺落实为 0.5（原先只记录不落值）
  - hard_kill 补齐：纯吐槽场景；giant/policy 高概率不硬淘汰（JEV 0.66）改"软否决 -> NEEDS_HUMAN"
  - 未实现的淘汰规则显式输出 TODO_UNIMPLEMENTED（不再静默）
  - 扣分轴统一概率化，去掉永不生效的布尔分支；size 连续化，避免 5-7 人簇"刚进门被扣满"
  - workaround 不再重复计 script 权重；reach 改 10/8/5；title 里的 `|` 破表修复
  - 阈值分位法为主（近 30 次评分的 P95->BUILD、P60->WATCH），冷启动保底 68/53
  - econ 快照 + 指纹写进输出，保证可复现；R 输出 low/high 区间
"""
import argparse
import hashlib
import json
import math
import os
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import jevgate  # noqa: E402

ECON_PATH = os.path.join(HERE, "config", "econ.json")
ECON = json.load(open(ECON_PATH, encoding="utf-8"))
SUBS_PATH = os.path.join(HERE, "config", "subs.json")

DEFAULT_FORM = "web-single-tool"
DEV_EASE = {"web-single-tool": 10, "chrome-extension": 7, "desktop-tool": 5, "saas-with-account": 3}
FREQ_SCORE = {"daily": 20, "weekly": 15, "monthly": 8, "once": 0, "unknown": 5}

BUILD_FALLBACK, WATCH_FALLBACK = 68.0, 53.0
HISTORY_N = 30

# NEEDS_HUMAN 触发（JEV gate3：核心维度低置信 >= 2）。外部环境维度(gap/giant/policy)只做提示，不改决策。
CORE_DIMS = ("pain", "pay", "price", "scene")
ENV_DIMS = ("gap", "giant", "policy")
HUMAN_TRIGGER = 2
REQUIRE_R_FOR_BUILD = True      # JEV: BUILD 需 R>0（ROI 为正）

# 设计文档里写了、但 v1.1 仍未实现的硬淘汰（显式输出，不静默）
TODO_UNIMPLEMENTED = [
    "需要巨大资本",
    "涉及医疗/金融等高风险决策",
    "需要大量 Reddit 用户数据才能运行",
    "技术实现过于复杂",
]


def clamp(v, lo=0.0, hi=1.0):
    return max(lo, min(hi, v))


def econ_hash():
    """econ 参数 + 订阅数摘要的指纹。

    v1.1.4：订阅数并入指纹（JEV 0.71）—— 它直接进 roi()，改了会让分数变，
    若不进指纹，改了 subs.json 重跑会分数变了却仍复用旧 hash 的历史阈值。
    代价：刷新订阅数 = 历史清零、重新冷启动（这正是该有的行为）。
    """
    payload = {"econ": ECON,
               "subs": {s: SUB_SIZES[s] for s in sorted(SUB_SIZES)},
               "low_conf": sorted(LOW_CONF_SUBS - ASSUMED_SUBS)}
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:12]


def _sub_num(v):
    """subscribers 的值有两种形态：
    - 整数（手填）
    - {n, src, at, spread, low_conf}（fetch_sub_sizes.py 抓的，带来源与离散度）
    解析不出来或非正数 -> None（不当成 0，也不猜）。
    """
    if isinstance(v, dict):
        v = v.get("n")
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def load_sub_sizes():
    """返回 (订阅数, 完全未知的 sub 集合, 低置信的 sub 集合)。

    subscribers 的值支持两种形态：整数，或 fetch_sub_sizes.py 抓的
    {n, src, at, raw, spread, suspect_high, low_conf}。
    """
    try:
        cfg = json.load(open(SUBS_PATH, encoding="utf-8"))
    except OSError:
        return {}, set(), set()
    subs = cfg.get("subs", {}) or {}
    sizes = cfg.get("subscribers", {}) or {}
    min_ok = ECON.get("sub_size_min_plausible", 5000)
    assumed, low_conf = set(), set()
    out = {}
    for s in subs:
        raw = sizes.get(s)
        n = _sub_num(raw)
        # v1.1.4：低于合理下限的值当成没填（实测有站把 r/spreadsheet 解析成 1000）
        if n is not None and n < min_ok:
            n = None
        if n is not None:
            out[s] = n
            if isinstance(raw, dict) and raw.get("low_conf"):
                low_conf.add(s)
        else:
            out[s] = int(ECON.get("assumed_sub_size", 100000))
            assumed.add(s)
    for s, raw in sizes.items():          # subscribers 里有但 subs 里没有的也认
        n = _sub_num(raw)
        if n is not None and n >= min_ok and s not in out:
            out[s] = n
            if isinstance(raw, dict) and raw.get("low_conf"):
                low_conf.add(s)
    return out, assumed, low_conf - assumed


SUB_SIZES, ASSUMED_SUBS, LOW_CONF_SUBS = load_sub_sizes()


def today_start(now=None):
    import time as _t
    ts = now if now is not None else _t.time()
    lt = _t.localtime(ts)
    return int(_t.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)))


def load_history(db_path, econ_hash_val, n=HISTORY_N, before=None):
    """取**同一套 econ 参数**下最近 N 次 Final 做分位阈值。

    v1.1.2：必须按 econ_hash 过滤 —— 否则改了参数（甚至 --no-jev 的降级批次）
    会混进历史，导致同一批数据两次跑出不同阈值（Codex 复审实测到过）。
    """
    if not db_path or not os.path.exists(db_path):
        return []
    try:
        con = sqlite3.connect(db_path)
        # before：排除「今天已写入的行」，否则同一天重复跑会把本次分数吸进历史，
        # 造成同一批数据两次跑出不同阈值（Codex 复审实测到阈值 44.7→53.7 漂移）
        rows = con.execute("SELECT final FROM scores WHERE final IS NOT NULL AND econ_hash=? "
                           "AND (? IS NULL OR scored_at < ?) ORDER BY scored_at DESC LIMIT ?",
                           (econ_hash_val, before, before, n)).fetchall()
        con.close()
        return sorted(float(r[0]) for r in rows if r[0] is not None)
    except sqlite3.Error:
        return []


def percentile(sorted_vals, p):
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = (len(sorted_vals) - 1) * p
    lo, hi = int(math.floor(k)), int(math.ceil(k))
    if lo == hi:
        return sorted_vals[lo]
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


def thresholds(db_path, econ_hash_val=None):
    """分位法为主 + 保底：
    - 历史（同 econ_hash、非当日）>= 30 次：用历史 P95 / P60
    - 否则：**固定保底 68 / 53**

    v1.1.2：冷启动不再用「本批 P90」—— 那等于把本批 JEV 的抽样噪声吸进阈值，
    同一份样本连跑两次阈值会在 55.0/55.1 之间漂（Codex 复审实测），
    落在阈值 ±0.2 内的簇会在 BUILD/WATCH/DROP 间乱跳。阈值必须与「本批分数」无关。
    拒绝的代价是冷启动期 BUILD 稀少 —— 这是可接受的（JEV gate4：接受 BUILD 稀有 0.63）。
    """
    hist = load_history(db_path, econ_hash_val, before=today_start()) if econ_hash_val else []
    if len(hist) >= HISTORY_N:
        return (percentile(hist, 0.95), percentile(hist, 0.60),
                f"percentile(n={len(hist)},P95/P60)")
    return (BUILD_FALLBACK, WATCH_FALLBACK, f"fallback_cold_start_fixed(n_hist={len(hist)})")


# ---------- 扣分 ----------
def penalties(c, jev):
    items, total = [], 0.0
    a = c.get("unique_authors", 0) or 0
    size_p = 0.0 if a >= 12 else clamp((12 - a) / 7.0)          # a=5 -> 1.0, a>=12 -> 0
    kinds = set(c.get("workaround_kinds") or [])
    techdep_p = 0.7 if "automation" in kinds else 0.0
    once_p = 0.9 if c.get("frequency") == "once" else 0.0
    policy = jev.get("policy", (0.0, 1.0, True))[0]
    giant = jev.get("giant", (0.0, 1.0, True))[0]

    for name, p, cap in [("政策/合规风险", policy, 20), ("已有巨头覆盖", giant, 15),
                         ("用户规模太小", size_p, 10), ("技术依赖风险", techdep_p, 10),
                         ("一次性需求", once_p, 10)]:
        if p >= 0.7:
            v, tag = cap, ""
        elif p >= 0.3:
            v, tag = cap * 0.5, "（半扣，待人工）"
        else:
            v, tag = 0.0, ""
        if v:
            total += v
            items.append(f"{name} -{v:g}{tag}")
    return total, items


VETO_GIANT, VETO_POLICY = 0.8, 0.8


def hard_kill(c, jev):
    r = []
    if (c.get("unique_authors") or 0) <= 1:
        r.append("只被 1 个用户提过")
    if not (c.get("workaround_kinds") or []):
        r.append("只有抱怨、没有 workaround")
    if c.get("frequency") == "once":
        r.append("一次性需求")
    if jev.get("scene", (0.5, 0, False))[0] <= 0.0:
        r.append("无购买/使用场景（纯吐槽）")
    return r


def veto(c, jev):
    """高概率但不到 100% 的软否决：不静默 DROP，交人工。"""
    v = []
    if jev.get("giant", (0, 0, 0))[0] >= VETO_GIANT:
        v.append(f"已有巨头高概率覆盖（giant>={VETO_GIANT}）")
    if jev.get("policy", (0, 0, 0))[0] >= VETO_POLICY:
        v.append(f"合规/政策高风险（policy>={VETO_POLICY}）")
    return v


# ---------- ROI ----------
def roi(c, jev, form):
    price_idx = jev.get("price", (0.33, 0.0, True))[0]
    repeat = jev.get("repeat", (0.5, 0.0, True))[0]
    tier = int(round(price_idx * 3))
    sub = ECON["subscription_by_form"].get(form, False)
    conv = ECON.get("conversion_by_form", {}).get(form, ECON.get("conversion", 0.005))
    # 买断形态折算：repeat 低时只按 1 个月算，避免"单次计年收入"的口径冲突
    months = ECON.get("buyout_months_equivalent", 3) if repeat >= 0.5 else 1
    price = ECON["price_usd_by_tier"][min(tier, 3)] * ECON["usd_cny"] * (1 if sub else months)

    authors = c.get("unique_authors", 0) or 0
    # v1.1.3：未知订阅数不能全额采信，否则「幽灵 10 万」会把 R 抬起来变相放行 BUILD
    # v1.1.4：订阅数改成「三档可信度」——每档一个折扣，明细写进 notes
    #   完全未知（不在 subs.json）        -> assumed_sub_size × assumed_sub_discount(0.2)
    #   抓自第三方统计站（默认档）        -> × sub_source_discount(0.8)
    #   低置信（单源/离散>15%/数值可疑）  -> 再 × low_conf_discount(0.5)
    assumed_size = ECON.get("assumed_sub_size", 100000)
    discount = ECON.get("assumed_sub_discount", 0.2)
    src_discount = ECON.get("sub_source_discount", 0.8)
    lowc_discount = ECON.get("low_conf_discount", 0.5)
    notes, reach_cap = [], 0
    for s in (c.get("subs") or []):
        if s in LOW_CONF_SUBS and s in SUB_SIZES:
            v = SUB_SIZES[s] * src_discount * lowc_discount
            reach_cap += v
            notes.append(f"{s}:订阅数低置信({SUB_SIZES[s]}),按 ×{src_discount}×{lowc_discount} 计")
        elif s in SUB_SIZES and s not in ASSUMED_SUBS:
            reach_cap += SUB_SIZES[s] * src_discount
        else:
            reach_cap += assumed_size * discount
            notes.append(f"{s}:未知订阅数,按 {assumed_size}×{discount} 计")
    reach_cap = max(reach_cap, 0) * ECON.get("reach_cap_ratio", 0.05)
    paying = min(authors * ECON["amplify"], reach_cap) * conv
    revenue = paying * price * (12 if sub else 1)
    cost = ECON["person_days_by_form"].get(form, 1.5) * ECON["day_rate_cny"] + 12 * ECON["month_ops_cny"]
    gross = revenue * ECON["gross_margin"]
    mult = (gross - cost) / cost
    r = 100 * math.log10(max(mult, 0) + 1) / math.log10(ECON.get("roi_full_at_multiple", 20) + 1)
    band = ECON.get("roi_band", {})
    lo_m = mult * band.get("low", 0.5)
    hi_m = mult * band.get("high", 2.0)
    r_lo = 100 * math.log10(max(lo_m, 0) + 1) / math.log10(ECON.get("roi_full_at_multiple", 20) + 1)
    r_hi = 100 * math.log10(max(hi_m, 0) + 1) / math.log10(ECON.get("roi_full_at_multiple", 20) + 1)
    payback = cost / (gross / 12) if gross > 0 else None
    return clamp(r, 0, 100), {
        "price_cny": round(price, 1), "paying_users_est": round(paying, 2),
        "reach_cap_users": round(reach_cap, 1),
        # v1.1.2/1.1.3/1.1.4：订阅数分三档可信度，每档一个折扣，明细写 notes
        "sub_size_assumed": any("未知订阅数" in x for x in notes),
        "sub_size_low_conf": any("低置信" in x for x in notes),
        "sub_size_discounted": bool(notes),
        "sub_size_assumed_notes": notes,
        "no_reach_subs": not (c.get("subs") or []),      # v1.1.2：subs 空 -> R 恒 0，需显式提示
        "buyout_months": None if sub else months, "conversion": conv,
        "revenue_y1": round(revenue), "cost": round(cost), "gross_y1": round(gross),
        "roi_mult": round(mult, 2), "R_low": round(clamp(r_lo, 0, 100), 1), "R_high": round(clamp(r_hi, 0, 100), 1),
        "payback_months": round(payback, 1) if payback else None,
    }


def score_one(c, form, use_jev=True, cache_db=None):
    ans, meta = (jevgate.ask(c, cache_db=cache_db) if use_jev else ({}, {"jev_unavailable": True}))
    jev, low_conf_dims = {}, []
    for d in jevgate.DIMS:
        v, conf, low = jevgate.num(ans, d, default=0.5)
        if low:
            v = 0.5                      # v1.1: 低置信按文档承诺取保守值
            low_conf_dims.append(d)
        jev[d] = (v, conf, low)

    # ---- Opportunity (100) ----
    pain = jev["pain"][0]; payw = jev["pay"][0]
    aud = jev["audience"][0]; gap = jev["gap"][0]
    freq = FREQ_SCORE.get(c.get("frequency"), 5)
    kinds = c.get("workaround_kinds") or []
    # v1.1: 不再重复计 script 权重 —— 强度看平均分，覆盖面看种类数
    wa_strength = min(10.0, (c.get("workaround_score_avg") or 0) * 2.0)
    wa_variety = min(5.0, 2.5 * len(kinds))
    workaround = wa_strength + wa_variety
    dev = DEV_EASE.get(form, 10)
    O = 25 * pain + freq + workaround + 15 * payw + 10 * aud + dev + 5 * gap

    # ---- Monetization (100) ----
    pay_sig = min(30, (c.get("pay_posts") or 0) * 8)
    price_v = jev["price"][0]; scene = jev["scene"][0]; repeat = jev["repeat"][0]
    ns = len(c.get("subs") or [])
    reach = 10 if ns == 1 else 8 if ns <= 3 else 5 if ns else 0
    M = pay_sig + 25 * price_v + 20 * scene + 15 * repeat + reach

    # ---- ROI (100) ----
    R, econ_d = roi(c, jev, form)

    pen, pen_items = penalties(c, jev)
    w = ECON.get("weights", {"O": 0.50, "M": 0.35, "R": 0.15})
    final = round(w["O"] * O + w["M"] * M + w["R"] * R - pen, 1)
    kills = hard_kill(c, jev)
    vetos = veto(c, jev)
    core_low = [d for d in low_conf_dims if d in CORE_DIMS]
    env_low = [d for d in low_conf_dims if d in ENV_DIMS]
    human_score = len(core_low) + 0.5 * len(env_low)
    if kills:
        decision, reason = "DROP", "; ".join(kills)
    elif not ans:
        decision, reason = "NEEDS_HUMAN", "JEV 不可用/降级（全维度取保守值 0.5），无法自动判定"
    elif len(core_low) >= HUMAN_TRIGGER:
        decision, reason = "NEEDS_HUMAN", (f"低置信：核心 {','.join(core_low) or '-'} / "
                                           f"环境 {','.join(env_low) or '-'}（加权 {human_score:g}）")
    elif vetos:
        decision, reason = "NEEDS_HUMAN", "; ".join(vetos)
    else:
        decision, reason = None, ""

    return {
        "cluster_id": c["cluster_id"], "title": c["title"], "who": c.get("who"),
        "subs": c.get("subs", []), "frequency": c.get("frequency"),
        "unique_authors": c.get("unique_authors"), "workaround_kinds": kinds,
        "pay_posts": c.get("pay_posts"), "form": form,
        "O": round(O, 1), "M": round(M, 1), "R": round(R, 1), "penalty": round(pen, 1),
        "final": final, "_decision_provisional": decision, "_reason": reason,
        "dims": {"pain": round(25 * pain, 1), "frequency": freq,
                 "workaround_strength": round(wa_strength, 1), "workaround_variety": round(wa_variety, 1),
                 "pay_will": round(15 * payw, 1), "audience": round(10 * aud, 1), "dev_ease": dev,
                 "gap": round(5 * gap, 1), "pay_signal": pay_sig, "price": round(25 * price_v, 1),
                 "scene": round(20 * scene, 1), "repeat": round(15 * repeat, 1), "reach": reach},
        "econ": econ_d, "penalty_items": pen_items, "hard_kill": kills, "veto": vetos,
        "low_conf_dims": low_conf_dims, "core_low": core_low, "env_low": env_low,
        "human_score": human_score, "jev_degraded": bool(meta.get("jev_degraded")),
        "jev_meta": meta, "jev_answers": ans, "sample_urls": c.get("sample_urls", []),
    }


def apply_thresholds(scored, build_t, watch_t, source):
    b = w = p = 0
    for s in scored:
        if s["_decision_provisional"] in ("DROP", "NEEDS_HUMAN"):
            s["decision"] = s["_decision_provisional"]
            s["decision_reason"] = s["_reason"]
            continue
        strong = s["O"] >= 60 and s["M"] >= 50
        if s["final"] >= build_t and strong and (not REQUIRE_R_FOR_BUILD or s["R"] > 0):
            s["decision"], s["decision_reason"] = "BUILD", ""
            b += 1
        elif s["final"] >= watch_t and strong and s["R"] <= 0:
            # PILOT：需求信号强但 ROI 尚未为正 -> 只做 Landing Page 验证，不做产品（JEV 0.67）
            s["decision"] = "PILOT"
            s["decision_reason"] = f"ROI 未转正（R={s['R']}），先做 Landing Page 验证"
            p += 1
        elif s["final"] >= watch_t:
            s["decision"], s["decision_reason"] = "WATCH", ""
            w += 1
        else:
            s["decision"], s["decision_reason"] = "DROP", f"低于 WATCH 阈值 {watch_t:.1f}"
    for s in scored:
        s.pop("_decision_provisional", None)
        s.pop("_reason", None)
    return {"build_threshold": round(build_t, 1), "watch_threshold": round(watch_t, 1),
            "source": source, "build_count": b, "watch_count": w, "pilot_count": p}


def _cell(x):
    return str(x).replace("|", "/").replace("\n", " ")


def to_md(scored, thr):
    rows = ["| # | 需求 | 用户群 | 频率 | workaround | 付费帖 | O | M | R | Final | 决策 |",
            "|---|---|---|---|---|---|--:|--:|--:|--:|---|"]
    for i, s in enumerate(scored, 1):
        short = (s["title"][:34] + "…") if len(s["title"]) > 35 else s["title"]
        rows.append(f"| {i} | {_cell(short)} | {_cell(s['who'])} | {_cell(s['frequency'])} | "
                    f"{_cell(','.join(s['workaround_kinds']) or '-')} | "
                    f"{s['pay_posts']} | {s['O']} | {s['M']} | {s['R']} | **{s['final']}** | {s['decision']} |")
    rows.append("")
    w = ECON.get("weights", {})
    rows.append(f"> 阈值：BUILD ≥ {thr['build_threshold']}、WATCH ≥ {thr['watch_threshold']}（{thr['source']}）；"
                f"BUILD 另需 O≥60 且 M≥50" + ("且 R>0" if REQUIRE_R_FOR_BUILD else ""))
    rows.append(f"> 权重 O/M/R = {w.get('O')}/{w.get('M')}/{w.get('R')}；"
                f"NEEDS_HUMAN：核心维度（pain/pay/price/scene）低置信 ≥ {HUMAN_TRIGGER} 个"
                f"（环境维度 gap/giant/policy 低置信只作提示）")
    if TODO_UNIMPLEMENTED:
        rows.append(f"> 未实现的硬淘汰规则（TODO_UNIMPLEMENTED）：{'、'.join(TODO_UNIMPLEMENTED)}")
    return "\n".join(rows)


def write_db(db_path, scored, econ_h, thr):
    if not db_path:
        return
    d = os.path.dirname(os.path.abspath(db_path))
    if d:
        os.makedirs(d, exist_ok=True)
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA foreign_keys=ON")
    con.executescript(open(os.path.join(HERE, "schema.sql"), encoding="utf-8").read())
    now = int(time.time())
    for s in scored:
        try:
            con.execute(
                """INSERT INTO scores(cluster_id,O,M,R,final,penalty,decision,dims,jev_raw,needs_human,
                                      scored_at,econ_hash,threshold_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(cluster_id) DO UPDATE SET
                     O=excluded.O, M=excluded.M, R=excluded.R, final=excluded.final,
                     penalty=excluded.penalty, decision=excluded.decision, dims=excluded.dims,
                     jev_raw=excluded.jev_raw, needs_human=excluded.needs_human,
                     scored_at=excluded.scored_at, econ_hash=excluded.econ_hash,
                     threshold_json=excluded.threshold_json""",
                (s["cluster_id"], s["O"], s["M"], s["R"], s["final"], s["penalty"], s["decision"],
                 json.dumps(s["dims"], ensure_ascii=False),
                 json.dumps({"meta": s["jev_meta"], "answers": s.get("jev_answers")}, ensure_ascii=False),
                 1 if s["needs_human"] else 0, now, econ_h, json.dumps(thr, ensure_ascii=False)))
        except sqlite3.Error as e:
            print(f"[warn] 写 scores 失败 {s['cluster_id']}: {e}", file=sys.stderr)
    con.commit()
    con.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--clusters", default="working/clusters.json")
    ap.add_argument("--out", default="review/scored.json")
    ap.add_argument("--form", default=DEFAULT_FORM, choices=list(DEV_EASE))
    ap.add_argument("--db", default=os.path.join(HERE, "db", "factory.db"))
    ap.add_argument("--no-jev", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="限流等待改 0（仅冒烟用）")
    args = ap.parse_args()

    data = json.load(open(args.clusters, encoding="utf-8")) if os.path.isabs(args.clusters) \
        else json.load(open(os.path.join(HERE, args.clusters), encoding="utf-8"))
    cands = data.get("candidates", [])
    scored = [score_one(c, args.form, use_jev=not args.no_jev, cache_db=args.db) for c in cands]
    for s in scored:
        s["needs_human"] = s["_decision_provisional"] == "NEEDS_HUMAN"

    econ_h = econ_hash()
    # 阈值：只取同一 econ_hash 下的历史；不足 30 次时用（历史+本批）分位，并受保底值封顶
    bt, wt, src = thresholds(args.db, econ_h)
    thr = apply_thresholds(scored, bt, wt, src)
    scored.sort(key=lambda s: s["final"], reverse=True)

    # v1.1.2：降级批次（--no-jev 或 JEV 不可用/解码失败）不得写进 scores 历史，
    # 否则会给后续生产阈值注入「全维度 0.5」的假样本（Codex 复审实测到阈值漂移）
    degraded = args.no_jev or any(s["jev_degraded"] or s["jev_meta"].get("jev_unavailable") for s in scored)
    thr["history_written"] = not degraded
    if degraded:
        thr["history_note"] = "降级批次不写 scores 历史（避免污染分位阈值）"
    for s in scored:
        if s["econ"].get("no_reach_subs"):
            s["decision_reason"] = (s["decision_reason"] + "；" if s["decision_reason"] else "") + \
                "该簇无 sub 归属 → 触达上限 0 → R 恒为 0"

    out_path = args.out if os.path.isabs(args.out) else os.path.join(HERE, args.out)
    d = os.path.dirname(out_path)
    if d:
        os.makedirs(d, exist_ok=True)
    payload = {"scored_at": int(time.time()), "form": args.form, "count": len(scored),
               "econ_snapshot": ECON, "econ_hash": econ_h, "thresholds": thr,
               # v1.1.4：订阅数快照进产物，改了什么一眼看得到（配合 econ_hash 判断历史是否重置）
               "sub_size_snapshot": {s: SUB_SIZES[s] for s in sorted(SUB_SIZES)},
               "sub_size_assumed": sorted(ASSUMED_SUBS),
               "sub_size_low_conf": sorted(LOW_CONF_SUBS - ASSUMED_SUBS),
               "todo_unimplemented": TODO_UNIMPLEMENTED,
               "build": [s for s in scored if s["decision"] == "BUILD"], "items": scored}
    tmp = out_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, out_path)

    md = os.path.join(d or ".", f"daily-{time.strftime('%Y%m%d')}.md")
    with open(md, "w", encoding="utf-8") as f:
        f.write(f"# Reddit 需求雷达 {time.strftime('%Y-%m-%d')}\n\n采集 {data.get('posts')} 帖 / "
                f"{data.get('clusters')} 簇 / 进评分 {len(cands)}\n\n{to_md(scored, thr)}\n")
    if degraded:
        print(f"[score] 降级批次（{'--no-jev' if args.no_jev else 'JEV 不可用/解码失败'}）："
              f"跳过写 scores 历史（避免污染分位阈值）")
    else:
        write_db(args.db, scored, econ_h, thr)
    print(f"[score] {len(scored)} 簇 -> {out_path}（阈值 BUILD>={thr['build_threshold']} "
          f"WATCH>={thr['watch_threshold']}，{src}）")
    for s in scored:
        print(f"  {s['decision']:<12} {s['final']:>5}  O{s['O']} M{s['M']} R{s['R']}  {s['title'][:40]}")


if __name__ == "__main__":
    main()
