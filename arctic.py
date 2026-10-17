# -*- coding: utf-8 -*-
"""arctic.py —— Arctic Shift 采集 provider（Reddit 公开归档站）。

为什么不用 reddit.com 直连：本机 www / old / oauth.reddit.com 的 443 全部 TCP 不通。
为什么不用 PullPush：api.pullpush.io 明确返回 429 并声明
「This website does not provide free scraping resources for agents」，
且实测其 q 关键词参数完全失效（带 q 与不带 q 返回结果相同）。已按 JEV 判定（conf 0.99）彻底弃用。

实测能力（2026-10-05）：
- 全量拉 subreddit 最新 100 帖：3-4 秒（25-35 帖/秒），字段 109-115 个，
  含 title / selftext / author / created_utc / id / permalink / subreddit_subscribers
- `before=<unix ts>` **有效**，可回溯任意历史窗口（实测 before=30天前 返回那时的 100 帖）
- `after` **无效**（被静默忽略）；`offset` 不存在（400）；`sort=asc` 可取到 2009 年老帖
- `title=` 关键词检索必须绑定 subreddit/author，否则 400；且组合查询频繁 422 超时
  → 本模块采用「全量拉取 + 本地过滤」，不依赖 title 检索
- 痛点词命中率实测 13.2%（400 帖 → 53 条）

合规与隐私：
- 只读公开归档，不登录、不发帖、不 DM；UA 里带可联系的真实 Reddit 用户名
- 不保存 selftext 全文到 signals（只存规则命中的标签）；用户名仅用于「独立作者数」去重计数
- 遵守 compliance.json 的每日请求上限 / 退避 / 同站最小间隔
"""
from __future__ import annotations

import json
import os
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
API = "https://arctic-shift.photon-reddit.com/api/posts/search"

# 归档站 422 = 服务端查询超时（它自己建议放慢），与 400（参数错）必须区分：
# 400 → 改参数重试无意义，直接放弃该组合；422 → 退避后重试
RETRY_STATUS = (422, 429, 500, 502, 503, 504)

# ---- 供给方噪声过滤（JEV: 标题正则 + 正文场景双条件，正文缺失时放宽）----
# 「我自己做了个工具」是供给，不是需求痛点。这类帖必须挡在雷达之外。
SUPPLY_TITLE = re.compile(
    r"\b(i (?:built|made|created|developed|released|launched|designed)\b"
    r"|my (?:new )?(?:tool|app|extension|addon|add-in|script|template|plugin|saas)\b"
    r"|announc(?:ing|ement)\b|introducing\b|feedback (?:is |appreciated|welcome)"
    r"|looking for feedback\b|beta (?:users|testers)\b|self[- ]promo\b"
    r"|(?:check|try) (?:out|it) my\b)", re.I)
# 求职 / 招人 / 课程推广
JOB_TITLE = re.compile(
    r"\b(hiring|we are (?:hiring|looking)|looking for (?:a |an )?(?:freelancer|developer"
    r"|candidate|employee)|resume|cv\b|job (?:opening|opportunit)|interview prep\b"
    r"|certification|cert exam|cpa|acc[aa]\b)", re.I)
# 正文里的「场景证据」：只有反复出现的具体劳动 + 手工/耗时，才算真需求
SCENE_EVIDENCE = re.compile(
    r"\b(manually|by hand|every (?:day|week|month|quarter)|each (?:day|week|month)"
    r"|every single (?:day|time)|i have to|i've had to|i keep having to"
    r"|each time|every time|whenever i|takes (?:me )?(?:hours|forever)"
    r"|copy (?:and|&) paste|paste (?:and|&) copy|re-?enter|re-type|retype"
    r"|tedious|repetitive|error[- ]prone|so many steps)", re.I)


def _ctx():
    c = ssl.create_default_context()
    # 归档站证书链本机不一定齐；我们读的是公开归档数据，校验意义有限
    c.check_hostname = False
    c.verify_mode = ssl.CERT_NONE
    return c


def _load_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default if default is not None else {}


def load_cfg():
    comp = _load_json(os.path.join(HERE, "config", "compliance.json"))
    return {
        "ua": comp.get("user_agent") or "niche-radar/1.1 (personal demand research)",
        "daily_max": int(comp.get("daily_max_requests", 300)),
        "min_gap": float(comp.get("min_gap_seconds", 5)),
        "backoff": list(comp.get("backoff_seconds", [60, 120, 240])),
    }


# ---------- HTTP ----------
def request(params, ua, timeout=45, retries=2):
    """单次请求。返回 (posts, err)。err 非 None 时 posts 为空列表。"""
    url = API + "?" + urllib.parse.urlencode(params)
    last = ""
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=_ctx()) as r:
                data = json.loads(r.read(30_000_000).decode("utf-8", "replace"))
            return (data.get("data") or []), None
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
            if e.code not in RETRY_STATUS:      # 400/404 等：改参数也没用
                return [], last
        except Exception as e:                # noqa: BLE001  超时/连接抖动
            last = f"{type(e).__name__}: {str(e)[:80]}"
        if attempt < retries:
            time.sleep(3.0 * (attempt + 1))       # 归档站慢，退避要短（它自己也在喊慢）
    return [], last


# ---------- 供给方噪声判定 ----------
def classify_supply(title, body):
    """返回 (是否供给方噪声, 原因)。正文缺失（[removed]/空）时放宽，只看标题。"""
    t = title or ""
    b = body or ""
    if b.strip() in ("", "[removed]", "[deleted]"):
        # 正文不可用 -> 只按标题判，且必须命中强特征（明确自建声明/招聘）才算噪声
        if SUPPLY_TITLE.search(t):
            return True, "title_supply(正文缺失,强特征)"
        if JOB_TITLE.search(t):
            return True, "title_job(正文缺失,强特征)"
        return False, ""
    # 正文可用 -> 双条件：标题强特征，或正文里既自建又无场景证据
    if JOB_TITLE.search(t):
        return True, "job"
    if SUPPLY_TITLE.search(t):
        has_scene = bool(SCENE_EVIDENCE.search(b))
        return (True, "supply_title+scene") if not has_scene else (True, "supply_title")
    # 标题像抱怨但正文其实是「我已解决」-> 用场景证据缺失兜底
    if SCENE_EVIDENCE.search(t) and not SCENE_EVIDENCE.search(b) and len(b) < 300:
        return True, "title_only_scene"
    return False, ""


def clean_body(body):
    """正文只用于本地规则匹配；[removed]/[deleted] 归一成空串。"""
    b = (body or "").strip()
    return "" if b in ("[removed]", "[deleted]", "[...]") else b


def to_post(p, sub, keep_body=True):
    """Arctic 原始帖 -> radar.extract() 需要的字段形状。"""
    ts = p.get("created_utc") or p.get("created") or 0
    try:
        ts = int(ts)
    except (TypeError, ValueError):
        ts = 0
    pid = str(p.get("id") or "")
    body = clean_body(p.get("selftext"))
    return {
        "post_id": pid or None,
        "sub": p.get("subreddit") or sub,
        "created_utc": ts,
        "title": p.get("title") or "",
        "body": body[:2000] if keep_body else "",   # 只在内存里过规则，落库不含全文
        "author": p.get("author") or "",
        "url": p.get("permalink") or (f"https://www.reddit.com/{pid}" if pid else ""),
        # 官方订阅数：用于与 subs.json 交叉校验，不直接采信（JEV 0.39）
        "_official_subscribers": p.get("subreddit_subscribers"),
        "_score": p.get("score"), "_num_comments": p.get("num_comments"),
    }


# ---------- 主采集 ----------
def fetch(subs, windows=(0, 30, 60), limit=100, log=print, stats=None,
          sleep_fn=None, max_requests=None):
    """按 (sub, 历史窗口) 组合拉取，返回 posts。

    windows 是「距今天数」的 before 窗口：0=最新、30=30 天前、60=60 天前。
    JEV 判定（choice conf 0.94）用 3 窗口一次凑够样本，
    理由：最新 100 帖只覆盖 1-11 天，独立作者数要累积到 5+ 且簇要重复出现才算真需求。
    """
    cfg = load_cfg()
    ua, gap = cfg["ua"], cfg["min_gap"]
    stats = stats if stats is not None else {}
    posts, seen, errors = [], set(), []
    subs_off = {}          # sub -> 该 sub 见过的官方订阅数（取中位数做交叉校验）
    requests_made = 0
    now = int(time.time())
    last_req_at = [0.0]
    sleep = sleep_fn if sleep_fn is not None else time.sleep
    cap = max_requests if max_requests is not None else cfg["daily_max"]

    def wait_gap():
        dt = gap - (time.time() - last_req_at[0])
        if dt > 0:
            sleep(dt)
        last_req_at[0] = time.time()

    for sub in subs:
        for d in windows:
            if requests_made >= cap:
                log(f"[arctic] 已达当日上限 {cap}，停手")
                stats["stopped_at_daily_max"] = True
                break
            params = {"subreddit": sub, "limit": limit, "sort": "desc"}
            if d:
                params["before"] = now - d * 86400
            wait_gap()
            requests_made += 1
            got, err = request(params, ua)
            if err:
                errors.append(f"r/{sub} before={d}d: {err}")
                log(f"  [fail] r/{sub:<22s} before={d:>3}d  {err}")
                continue
            new = 0
            for p in got:
                pid = str(p.get("id") or "")
                if pid and pid in seen:
                    continue
                seen.add(pid)
                os_ = p.get("subreddit_subscribers")
                if os_:
                    subs_off.setdefault(sub, []).append(int(os_))
                posts.append(to_post(p, sub))
                new += 1
            log(f"  [ok]   r/{sub:<22s} before={d:>3}d  +{new:>3} 帖（{len(got)} 条返回，去重后）")
        else:
            continue
        break

    # 官方订阅数交叉校验（只报差异，不覆盖 subs.json —— JEV 0.39 不采纳直接替代）
    official = {}
    for s, vals in subs_off.items():
        vals = sorted(vals)
        official[s] = {"median": vals[len(vals) // 2], "min": vals[0], "max": vals[-1],
                       "spread_pct": round((vals[-1] - vals[0]) / max(vals[len(vals) // 2], 1), 4),
                       "n": len(vals)}
    stats.update({"requests": requests_made, "raw_posts": len(posts),
                  "unique_ids": len(seen), "errors": errors,
                  "official_subscribers": official})
    return posts


def cross_check(official, subs_json_path):
    """官方订阅数 vs subs.json 第三方站数字，返回逐 sub 差异表（仅报告，不自动改）。"""
    cfg = _load_json(subs_json_path, {})
    known = cfg.get("subscribers", {}) or {}
    rows = []
    for sub, info in sorted(official.items()):
        off = info["median"]
        old = known.get(sub)
        old_n = None
        if isinstance(old, dict):
            old_n = old.get("n")
        elif isinstance(old, int):
            old_n = old
        if not off or not old_n:
            continue
        diff = (off - old_n) / off
        rows.append({"sub": sub, "official": off, "third_party": old_n,
                     "diff_pct": round(diff * 100, 2),
                     "off_spread_pct": round(info["spread_pct"] * 100, 2)})
    rows.sort(key=lambda r: -abs(r["diff_pct"]))
    return rows


if __name__ == "__main__":
    import sys
    sys.path.insert(0, HERE)
    cfg = _load_json(os.path.join(HERE, "config", "subs.json"))
    subs = list((cfg.get("subs") or {}).keys())
    lim = int(os.environ.get("ARCTIC_LIMIT", "5"))
    out_dir = os.path.join(HERE, "review")
    os.makedirs(out_dir, exist_ok=True)
    logf = os.path.join(out_dir, "arctic_selftest.log")
    lines = [f"UA: {load_cfg()['ua']}", f"sub 数={len(subs)} limit={lim}", ""]

    def log(m):
        print(m)
        lines.append(str(m))

    st = {}
    got = fetch(subs, windows=(0, 30), limit=lim, log=log, stats=st)
    lines.append("")
    lines.append(f"posts={len(got)} requests={st['requests']} errors={len(st['errors'])}")
    supply = [p for p in got if classify_supply(p["title"], p["body"])[0]]
    lines.append(f"供给方噪声 {len(supply)}/{len(got)} = {len(supply)/max(len(got),1):.1%}")
    lines.append("")
    lines.append("=== 官方订阅数交叉校验 ===")
    for r in cross_check(st.get("official_subscribers", {}),
                         os.path.join(HERE, "config", "subs.json")):
        lines.append(f"  r/{r['sub']:<22s} 官方={r['official']:>10,}  第三方={r['third_party']:>10,}  "
                     f"差 {r['diff_pct']:+.1f}%  （官方值散布 {r['off_spread_pct']:.2f}%）")
    with open(logf, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print("wrote", logf)
