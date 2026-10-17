"""fetch_sub_sizes.py - 抓取各 sub 的订阅数，填进 subs.json 的 subscribers。

为什么需要：ROI 的「触达上限 = min(作者数×300, Σsub订阅数×5%)」需要真实订阅数。
本机直连 reddit.com:443 不通（TCP 超时），所以走第三方只读统计站交叉取数。

**多源交叉，不采信单源**：不同站的数据差异可达 10%+（实测 r/excel：gummysearch 899k /
reddifier 889,871 / 某过时期文 450K）。取中位数并记录各源原值与离散度，
离散度过大 -> 标 low_conf，宁可打折也不当成真值。

只读 GET，不碰 reddit 本体，不登录、不写任何东西。UA 用真实可联系用户名（YOUR_REDDIT_USERNAME）。
stdout 在本机会被吞，所以结果一律写文件。

用法：
    python fetch_sub_sizes.py            # 只抓，写 review/sub_sizes.json（候选，不改配置）
    python fetch_sub_sizes.py --apply    # 抓完直接写进 config/subs.json 的 subscribers
    python fetch_sub_sizes.py --refresh  # --apply + 清掉已填的（重新抓全部）
"""

import argparse
import json
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
SUBS_JSON = HERE / "config" / "subs.json"
COMPLIANCE = HERE / "config" / "compliance.json"
OUT_JSON = HERE / "review" / "sub_sizes.json"

# 源间中位数与各源最大相对偏差超过它 -> low_conf
SPREAD_LIMIT = 0.15
# 合理性护栏：实测 r/spreadsheet 被解析成 "1000 members"（真实十万级），
# 不设下限就会让该簇触达上限暴跌 -> R 误杀。低于下限一律判解析错误丢弃。
MIN_PLAUSIBLE = 5_000
# 超过上限不丢弃（r/DIY 这种超大 sub 真实存在），但标 suspect 让人工过一眼。
MAX_PLAUSIBLE = 30_000_000
LOW_CONF_DISCOUNT = 0.5   # JEV: low_conf 的数在 ROI 里再打 5 折（conf 0.83）
GAP_S = 3.0          # 同源两次请求之间的礼貌间隔（实测 1.5s 会被 gummysearch 限流成连续超时/502）
TIMEOUT = 20
RETRIES = 3          # 单源失败重试次数
BACKOFF = (5, 20, 45)  # 重试退避秒数；被限流时退避太短等于继续撞墙

SOURCES = (
    ("gummysearch", "https://gummysearch.com/r/{sub}/",
     re.compile(r"([\d.,]+\s*[km])\s*members", re.I)),
    ("reddifier", "https://reddifier.com/free-subreddit-analysis-tool/r/{sub}",
     re.compile(r"([\d,]{4,12})\s*members", re.I)),
    # 第三个源：给精确到个位的数。实测 gummysearch/reddifier 都没有的
    # r/bookkeeping、r/videoediting 只有它有（75,682 / 501,263）。
    # 正则**强制要求千分位**：页面上 "Subscribers" 附近还有别的数字（日期、统计数），
    # 宽松匹配会抓到 "26" 这种垃圾（实测踩过）。
    ("redpulse", "https://redpulse.io/subreddit-search/r/{sub}",
     re.compile(r"Subscribers[\s\S]{0,400}?\b(\d{1,3}(?:,\d{3}){1,3})\b", re.I)),
)

_SIZE_RE = re.compile(r"^\s*([\d.,]+)\s*([km])?\s*$", re.I)


def parse_size(text):
    """'899k' / '894,883' / '1.2m' -> int。解析不出来返回 None（不猜）。"""
    m = _SIZE_RE.match(str(text))
    if not m:
        return None
    num, unit = m.group(1), (m.group(2) or "").lower()
    try:
        n = float(num.replace(",", ""))
    except ValueError:
        return None
    n *= {"k": 1_000, "m": 1_000_000}.get(unit, 1)
    return int(round(n))


def fetch(url, ua, ctx):
    req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept-Language": "en"})
    last = None
    for attempt in range(RETRIES):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx) as r:
                return r.status, r.read(400_000).decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (404, 410):        # 页面不存在，重试无意义
                break
            time.sleep(BACKOFF[min(attempt, len(BACKOFF) - 1)])
        except Exception as e:  # noqa: BLE001  超时/连接抖动都重试
            last = e
            time.sleep(BACKOFF[min(attempt, len(BACKOFF) - 1)])
    raise last


def probe_one(sub, ua, ctx, log):
    """返回 {源名: 订阅数}；该源抓不到/数值不合理就不出现在结果里。"""
    out = {}
    for name, tmpl, pat in SOURCES:
        url = tmpl.format(sub=sub)
        try:
            status, body = fetch(url, ua, ctx)
        except urllib.error.HTTPError as e:
            log.append(f"  {name:12s} HTTP {e.code}")
            continue
        except Exception as e:  # noqa: BLE001
            log.append(f"  {name:12s} FAIL {type(e).__name__}: {str(e)[:80]}")
            continue
        m = pat.search(body)
        n = parse_size(m.group(1)) if m else None
        if n and n < MIN_PLAUSIBLE:
            log.append(f"  {name:12s} {n:,} < 合理下限 {MIN_PLAUSIBLE:,} -> 判为解析错误，丢弃")
            n = None
        if n:
            out[name] = n
            log.append(f"  {name:12s} {n:>12,}  (HTTP {status}, len={len(body)})")
        else:
            log.append(f"  {name:12s} 无匹配（HTTP {status}, len={len(body)}）")
        time.sleep(GAP_S)
    return out


def merge(sources):
    """多源 -> 中位数 + 离散度。只有 1 个源、或离散过大、或数值可疑 -> low_conf。"""
    vals = sorted(sources.values())
    n = len(vals)
    med = vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) // 2
    spread = (vals[-1] - vals[0]) / med if n > 1 and med else 0.0
    suspect_high = med > MAX_PLAUSIBLE
    return {
        "n": med,
        "src": "+".join(sources),
        "at": time.strftime("%Y-%m-%d"),
        "raw": sources,
        "spread": round(spread, 3),
        "suspect_high": suspect_high,
        "low_conf": bool(n < 2 or spread > SPREAD_LIMIT or suspect_high),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="写进 config/subs.json")
    ap.add_argument("--refresh", action="store_true", help="清掉已填的，全部重抓")
    ap.add_argument("--only", help="只抓这些 sub（逗号分隔），用于补抓失败的；无视已填状态")
    args = ap.parse_args()

    comp = json.loads(COMPLIANCE.read_text(encoding="utf-8"))
    ua = comp.get("user_agent") or f"niche-radar/1.1 (personal demand research; contact u/{comp.get('reddit_username', 'YOUR_REDDIT_USERNAME')})"

    cfg = json.loads(SUBS_JSON.read_text(encoding="utf-8"))
    subs = list(cfg.get("subs", {}).keys())
    known = dict(cfg.get("subscribers", {}))
    if args.refresh:
        known = {}
    log = [f"UA: {ua}"]
    # 已填过的（不论 {n,src,at} 结构还是老的纯 int）默认跳过，除非 --refresh / --only
    if args.only:
        want = {x.strip() for x in args.only.split(",") if x.strip()}
        todo = [s for s in subs if s in want]
        unknown = want - set(subs)
        if unknown:
            log.append(f"⚠️ --only 里有不在 subs.json 的 sub，已忽略: {', '.join(sorted(unknown))}")
    elif args.refresh:
        todo = subs
    else:
        todo = [s for s in subs if s not in known]
    log += [f"待抓 {len(todo)} / 共 {len(subs)} 个 sub", ""]

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE      # 第三方站证书链本机不一定齐

    result, failed = {}, []
    for i, s in enumerate(todo, 1):
        log.append(f"[{i}/{len(todo)}] r/{s}")
        srcs = probe_one(s, ua, ctx, log)
        if not srcs:
            failed.append(s)
            log.append("  -> 无有效来源，保留为空（不猜）")
            continue
        result[s] = merge(srcs)
        flags = [k for k in ("low_conf", "suspect_high") if result[s][k]]
        log.append(f"  -> 中位数 {result[s]['n']:,}  离散 {result[s]['spread']:.1%}"
                   + (f"  [{' '.join(flags)}]" if flags else ""))
        log.append("")

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(
        {"fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"), "ua": ua,
         "spread_limit": SPREAD_LIMIT, "min_plausible": MIN_PLAUSIBLE,
         "max_plausible": MAX_PLAUSIBLE, "low_conf_discount": LOW_CONF_DISCOUNT,
         "got": len(result), "failed": failed, "sizes": result},
        ensure_ascii=False, indent=2), encoding="utf-8")
    suspect = [s for s, v in result.items() if v.get("suspect_high")]
    lowc = [s for s, v in result.items() if v["low_conf"] and not v.get("suspect_high")]
    log.append(f"候选结果 -> {OUT_JSON}")
    log.append(f"成功 {len(result)} / 失败 {len(failed)}: {', '.join(failed) or '无'}")
    log.append(f"low_conf（单源或离散> {SPREAD_LIMIT:.0%}，ROI 里再打 {LOW_CONF_DISCOUNT:.0%}）"
               f"共 {len(lowc)} 个: {', '.join(lowc) or '无'}")
    log.append(f"suspect_high（>{MAX_PLAUSIBLE:,}，需人工确认）: {', '.join(suspect) or '无'}")
    log.append(f"合理下限护栏：< {MIN_PLAUSIBLE:,} 一律丢弃（实测 r/spreadsheet 被解析成 1000）")

    if args.apply:
        merged = {**known, **result}
        cfg["subscribers"] = merged
        tmp = SUBS_JSON.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(SUBS_JSON)
        log.append(f"已写入 {SUBS_JSON}（共 {len(merged)} 条）")
        if failed:
            log.append(f"仍为空（抓不到，不填）：{', '.join(failed)}")
    else:
        log.append("未加 --apply，config/subs.json 未改动。确认后加 --apply 落盘。")

    (HERE / "review" / "fetch_sub_sizes.log").write_text("\n".join(log), encoding="utf-8")
    print("\n".join(log[-3:]))


if __name__ == "__main__":
    main()
