#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""需求发现雷达：采集 -> 痛苦抽取 -> 需求聚类（v1.1）

用法:
  radar.py --provider manual  --inbox inbox --out working/clusters.json [--db db/factory.db]
  radar.py --provider browser --subs config/subs.json --out working/clusters.json
  radar.py --provider sample  --sample sample/sample_posts.json --out working/clusters.json

合规（代码级约束，见 config/compliance.json）:
  - 只保留需求摘要与溯源链接；不存正文全文、不存用户名、不存邮箱
  - 联网 provider 必经 ratelimit（跨日 <=300、同 sub >=5s、退避 60/120/240、三次全败停手）
  - 登录态浏览器单会话 <= 50 页；不并发；Cookie 不落盘
  - 失败三次即停，写 logs/fail.md

v1.1 修复: once 正则误杀 weekly / 单帖隔离 / zip 截断告警 / created_utc 容错 /
           403 与 HTML 响应容错 / 缺 sub 不炸整批 / first_seen 保持 + last_seen 刷新 /
           who 职业词表 + 截断 / pain_why/gap/pay_signal 实现 / 动词按标题语序取 / TOOL_WORDS 去 make
"""
import argparse
import glob
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# ---------- 词库 ----------
# v1.1.5 词库大幅扩充：样本期是手造样本，词库够用；接入 Arctic Shift 真数据后
# 发现真实标题的表达远更杂（"How do you reconcile two messy CSVs every month?"），
# 原词库只命中 13% 且大量簇是「职业话题/求助/闲聊」而非产品需求。
PAIN = {
    "ask_tool": [
        r"is there a (tool|way|app|script|software|extension|addon)",
        r"does anyone know (a|an|of a|how|if|what)",
        r"any (app|tool|extension|addon|software|template)",
        r"how do i automate|how can i automate|how to automate",
        r"(what|which) (tool|app|software|extension)",
        r"looking for (a|an) (tool|app|way|software)",
        r"best (tool|app|extension|way) (for|to)",
        r"anyone (else )?(know|have) a",
        r"how (do|does) (you|people) (do|handle|manage|deal with)",
        r"what do you use for",
    ],
    "complain": [
        r"i have to manually|i have to (do|enter|re-?enter|key in|type)",
        r"every time i have to|every time i (have to|need to|start|redo)",
        r"this is so annoying|so annoying|really annoying|frustrating|frustrated",
        r"why isn'?t there|why is there no|why doesn'?t (it|there)",
        r"there has to be a better way|must be a better way|better way to",
        r"driving me (crazy|nuts)|wasting (so much |my )?time",
        r"tired of (doing|manually|re-?entering)",
        r"painful|nightmare|a nightmare|headache|hassle",
        r"stuck doing|still (have to|need to|doing)",
        r"(what'?s|whats) the biggest (hassle|problem|pain|annoyance)",
        r"most frustrating|biggest hassle",
    ],
    "workaround": [
        r"i made a spreadsheet|i (built|made|created) a (spreadsheet|template|sheet|script|tool)",
        r"i wrote a script|i (built|made) (a|my own) (script|tool|parser|extension|addon)",
        r"i use excel for|i use (a )?spreadsheet|using excel|google sheets for",
        r"my workflow is|our workflow is|current (process|workflow)",
        r"i currently use|we currently use|right now i use",
        r"copy and paste|copy/paste|ctrl[- ]?c|ctrl[- ]?v",
        r"spreadsheet (i|that|which)|template i (made|built|use)",
        r"vba|macro|power query|app script",
        r"doing it manually|by hand manually",
    ],
    # 周期/重复劳动（真实数据里最强的信号，单独成类便于加权）
    "repeating": [
        r"every (day|week|month|quarter|year|morning)",
        r"each (day|week|month|time|quarter)",
        r"(every|each) single (day|week|month|time)",
        r"every time i\b|each time i\b",
        r"monthly|yearly|quarterly|annually|weekly|daily",
        r"over and over|again and again|repeat(ed|ing)?ly|on repeat",
        r"\d+ (times|hours) (a|per|every) (day|week|month)",
    ],
    # 支付意愿（真实数据里问句式远比 "I'd pay" 多）
    "pay_intent": [
        r"what would make you pay|would make you pay",
        r"would you pay|you'?d pay|you would pay",
        r"is it worth|worth (paying|\$|it|paying for)",
        r"happy to pay|i'?d pay|i would pay",
        r"how much (would|do) you pay|price (i|you'?d) (would|pay)|budget for",
        r"(paying|pay) for (this|that|a tool|an app|software)",
        r"cheaper than|alternative to|instead of paying",
    ],
}
WORKAROUND_RULES = [
    (r"\b(python|powershell|bash|shell|regex|macro|vba|ahk|autohotkey|power ?query)\b", 3, "script"),
    (r"\b(excel|google sheets?|spreadsheet|template|libreoffice|airtable)\b", 2, "spreadsheet"),
    (r"\b(zapier|make\.com|integromat|shortcuts|automator|bookmarklet|power ?automate)\b", 2, "automation"),
    (r"\b(manually|by hand|one by one|every time|each time|copy and paste|copy/paste)\b", 1, "manual"),
    (r"\b(download|export|re-?enter|upload)\b.{0,30}\b(download|export|upload|re-?enter)\b", 1, "shuttle"),
    # v1.1.5 真实数据补充：ERP/会计系统、CSV、PDF 票据、API 对接都是高频手工场景
    (r"\b(quickbooks|qbo|xero|netsuite|sap|oracle|erp|tally|ledger)\b", 2, "erp"),
    (r"\b(csv|tsv|json|xml|pdf|invoice|receipt|statement)\b", 1, "doc_data"),
    (r"\b(api|integration|sync|syncing|import(ing)?|export(ing)?)\b", 1, "integration"),
]
# v1.1.5：周期项前置；once 加负向前瞻，避免 "once a week" 被当成一次性
FREQ_RULES = [
    (r"every (day|morning)|daily|each day|a day", "daily"),
    (r"every (week|monday|friday)|weekly|each week|a week|per week", "weekly"),
    (r"every month|monthly|each month|every quarter|a month|per month", "monthly"),
    (r"\bonce\b(?!\s*(a|per|every)\s+(day|week|month|year|quarter))|one[- ]off|one time", "once"),
]
PAY_RULES = {
    "id_pay":      r"i'?d pay",
    "would_pay":   r"i would pay",
    "worth_paying": r"worth paying",
    "worth_amount": r"worth \$",
    "already_pays": r"i pay for",
    "has_bought":  r"i'?ve bought",
    "subscription": r"subscription",
    "happy_to_pay": r"happy to pay",
}
GAP_RULES = [
    (r"doesn'?t support|does not support|no support for|can'?t handle", "现有工具不支持该格式/场景"),
    (r"too complicated|overkill|bloated|too many features|feature bloat", "现有工具过于复杂"),
    (r"too expensive|pricey|expensive", "现有工具价格偏高"),
    (r"no (good )?(app|tool|software|way) (for|that|to)", "没有合适的工具"),
    (r"only works (on|with)|limited to|limited tokens", "适用范围受限"),
]
PAIN_WHY_RULES = [
    (r"takes (hours|forever)|so much time|time[- ]consuming|waste of time", "耗时"),
    (r"\berror|mistake|typo|wrong|mess(ed)? up|inconsistent", "易错"),
    (r"every (time|week|month)|over and over|again and again|repetitive", "重复"),
    (r"annoying|frustrating|driving me crazy|\bhate\b", "烦人"),
]
TOOL_WORDS = ["excel", "google sheets", "notion", "powershell", "python", "zapier", "make.com",
              "photoshop", "lightroom", "quickbooks", "airtable", "vba", "regex", "bash"]
STOP = set("""a an the and or but if then than that this these those i we you they he she it my our your their is are was were be been being
to of in on for with at by from as into about after before over under again more most some any all can could would should will do does did
have has had not no nor so such very just really there here what which who whom when where why how get got make makes made use using used
something someone anything anyone thing way ways now new like need needs want wants know think""".split())
VERBS = ["rename", "convert", "merge", "split", "export", "import", "clean", "deduplicate", "dedupe", "format", "batch", "automate", "sort", "extract",
         "generate", "calculate", "track", "compare", "organize", "resize", "compress", "translate", "scrape", "sync", "backup", "tag", "fill", "match", "parse"]
OBJ_GROUPS = {
    "file": ["file", "files", "photo", "photos", "image", "images", "picture", "video", "clip", "clips", "folder", "document", "doc", "pdf", "name", "names"],
    "table": ["csv", "excel", "spreadsheet", "sheet", "sheets", "xlsx", "data", "table", "column", "columns"],
    "doc": ["report", "invoice", "receipt", "contract", "quote", "resume", "letter"],
    "money": ["tax", "taxes", "booking", "payment", "expense", "budget"],
    "media": ["subtitle", "subtitles", "caption", "audio", "transcript"],
    "text": ["email", "emails", "log", "logs", "text", "note", "notes", "comment"],
}
OBJ_WORDS = sorted({w for v in OBJ_GROUPS.values() for w in v})

# v1.1: 职业词表（who 只保留职业词，不存用户原话片段）
OCCUPATIONS = [
    "photographer", "videographer", "video editor", "photo editor", "editor", "accountant", "bookkeeper",
    "freelancer", "freelance photographer", "consultant", "real estate agent", "realtor", "landlord",
    "property manager", "designer", "graphic designer", "web designer", "developer", "programmer",
    "software engineer", "sysadmin", "system administrator", "devops engineer", "data engineer",
    "data analyst", "analyst", "teacher", "professor", "nurse", "doctor", "lawyer", "attorney", "paralegal",
    "contractor", "electrician", "plumber", "carpenter", "mechanic", "landscaper", "chef", "baker", "barber",
    "therapist", "veterinarian", "farmer", "fisherman", "musician", "illustrator", "animator", "maker",
    "hobbyist", "student", "researcher", "scientist", "architect", "estimator", "dispatcher", "broker",
    "small business owner", "business owner", "shop owner", "ecommerce seller", "etsy seller",
    "amazon seller", "streamer", "creator", "podcaster", "youtuber", "virtual assistant",
    "personal trainer", "event planner", "wedding planner", "travel agent", "insurance agent",
    "loan officer", "tax preparer", "auditor", "controller", "office manager", "operations manager",
    "project manager", "product manager",
]


def load_compliance():
    try:
        with open(os.path.join(HERE, "config", "compliance.json"), encoding="utf-8") as f:
            return json.load(f)
    except OSError:
        return {}


COMPLIANCE = load_compliance()
WHO_MAX = int(COMPLIANCE.get("who_max_len", 40))

# sub -> 中文用户群（who 回退用；自撰文案，不含用户原话）
SUB_CN = {
    "accounting": "会计", "bookkeeping": "记账人员", "smallbusiness": "小微企业主", "freelance": "自由职业者",
    "consulting": "咨询顾问", "realestate": "房产从业者", "photography": "摄影师", "videoediting": "视频剪辑师",
    "excel": "Excel 用户", "googlesheets": "表格用户", "Notion": "Notion 用户", "PowerShell": "运维/自动化用户",
    "sysadmin": "系统管理员", "dataengineering": "数据工程师", "QuickBooks": "QuickBooks 用户", "spreadsheet": "表格用户",
    "3Dprinting": "3D 打印玩家", "homelab": "家庭实验室玩家", "MechanicalKeyboards": "机械键盘玩家",
    "AskPhotography": "摄影爱好者", "Fishing": "钓鱼爱好者", "boardgames": "桌游玩家", "modelmakers": "模型玩家", "DIY": "DIY 爱好者",
}
SUB_CN_L = {k.lower(): v for k, v in SUB_CN.items()}


def lemma(w):
    for suf in ("ing", "ed", "es", "s"):
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            base = w[: -len(suf)]
            if base in VERBS or base in OBJ_WORDS or (base + "e") in VERBS:
                return base if base in VERBS or base in OBJ_WORDS else base + "e"
    return w


def group_of(word):
    for g, ws in OBJ_GROUPS.items():
        if word in ws:
            return g
    return None


def norm(t):
    return re.sub(r"\s+", " ", (t or "").lower()).strip()


def safe_int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def pick_who(text, post):
    """职业词表优先；抽不到职业词回退 sub；一律截断 <= WHO_MAX，不存用户原话。"""
    for occ in OCCUPATIONS:
        if re.search(r"\b" + re.escape(occ) + r"s?\b", text):
            return occ[:WHO_MAX]
    m = re.search(r"i (?:am|'?m) (?:a|an) ([a-z ]{2,40})", text)
    if m:
        span = m.group(1)
        for occ in OCCUPATIONS:
            if occ in span:
                return occ[:WHO_MAX]
    sub = (post.get("sub") or "").strip()
    if not sub:
        return None
    return SUB_CN_L.get(sub.lower(), f"r/{sub} 社区用户")[:WHO_MAX]


def extract(post):
    """单帖 -> 需求信号。规则优先，抽不出留 null（不脑补）。"""
    title, body = post.get("title", "") or "", post.get("body", "") or ""
    text = norm(title + " . " + body)
    pain_hits, pay_hits = [], []
    for k, pats in PAIN.items():
        for p in pats:
            if re.search(p, text):
                pain_hits.append(k)
                break
    for name, p in PAY_RULES.items():
        if re.search(p, text):
            pay_hits.append(name)
    wscore, kinds = 0, set()
    for pat, w, kind in WORKAROUND_RULES:
        if re.search(pat, text):
            wscore += w
            kinds.add(kind)
    freq = "unknown"
    for pat, v in FREQ_RULES:
        if re.search(pat, text):
            freq = v
            break
    tools = [w for w in TOOL_WORDS if re.search(r"\b" + re.escape(w) + r"\b", text)]
    pain_why = [lab for pat, lab in PAIN_WHY_RULES if re.search(pat, text)][:2]
    gap = next((lab for pat, lab in GAP_RULES if re.search(pat, text)), None)
    return {
        "post_id": post.get("post_id") or hashlib.md5(text.encode()).hexdigest()[:10],
        "sub": (post.get("sub") or "").strip(),
        "created_utc": safe_int(post.get("created_utc"), 0),
        "title": title, "url": post.get("url", ""), "author": post.get("author", ""),
        "who": pick_who(text, post), "problem": title.strip(),
        "pain_why": ",".join(pain_why) or None,
        "workaround": ",".join(sorted(kinds)) or None, "frequency": freq,
        "tools_used": tools, "gap": gap,
        "pay_signal": ",".join(sorted(set(pay_hits))) or None,   # 只存规则名，不存原话
        "workaround_score": wscore, "_kinds": sorted(kinds),
        "pain_hits": pain_hits, "pay_hits": pay_hits,
    }


def fingerprint(title):
    """指纹 = 动词 + 对象语义组。动词按标题里的**出现顺序**取（不是列表序）。"""
    t = norm(title)
    words = [lemma(w) for w in re.findall(r"[a-z]{3,}", t)]
    v = next((w for w in words if w in VERBS), None)
    g = next((group_of(w) for w in words if group_of(w)), None)
    if v and g:
        return f"{v}_{g}"
    if v:
        return f"{v}_x"
    if g:
        return f"x_{g}"
    keep = [w for w in words if w not in STOP]
    return "_".join(keep[:2]) if keep else "misc"


def cluster(signals, min_authors=5, min_pain_ratio=0.25):
    """把需求信号聚成簇。

    v1.1.5 新增 `min_pain_ratio`（痛点密度门槛）：
    接上真实数据后发现最大簇 492 帖里混着大量「职业话题/求助/闲聊」（x_money 121 帖全是
    "Starting a tax firm" / "I hate taxes" 这类），因为指纹只看「动词+对象」，不看是不是需求。
    指纹相同不等于同一件事 —— 必须要求簇内多数帖真的命中痛点词，否则整簇降级。
    阈值 0.25 来自实测扫描（6779 帖 / 24 sub / 86 天）：0→30 簇、0.15→18、0.25→10、
    0.35→5、0.5→2。取 0.25 是「挤掉闲聊簇但留住真实痛点簇」的平衡点；门槛只降不升，
    避免 0.5 那种把 clean_table 这种真需求也误杀的情况。
    """
    g = defaultdict(list)
    for s in signals:
        g[fingerprint(s["title"])].append(s)
    out = []
    for cid, items in g.items():
        # v1.1.2: 不用 post_id 冒充作者（否则 unique_authors 退化成帖数 → 假通过、假 BUILD）
        authors = {i["author"] for i in items if i.get("author")}
        unknown = sum(1 for i in items if not i.get("author"))
        src_urls = [i["url"] for i in items if i.get("url")]
        kinds = sorted({k for i in items for k in i["_kinds"]})
        freq_rank = {"daily": 4, "weekly": 3, "monthly": 2, "once": 1, "unknown": 0}
        freq = max((i["frequency"] for i in items), key=lambda f: freq_rank.get(f, 0))
        # v1.1.5：痛点密度 —— 簇内命中痛点词的帖占比，以及其中「真需求类」（ask_tool/complain/
        # workaround/repeating）的占比。闲聊帖（只有 pay_intent 或啥都没中）不算。
        pain_n = sum(1 for i in items if i["pain_hits"])
        demand_n = sum(1 for i in items
                       if set(i["pain_hits"]) & {"ask_tool", "complain", "workaround", "repeating"})
        pain_ratio = pain_n / len(items) if items else 0.0
        demand_ratio = demand_n / len(items) if items else 0.0
        reasons = []
        if len(authors) < min_authors:
            reasons.append(f"独立作者不足（{len(authors)}<{min_authors}）")
        if not kinds:
            reasons.append("无 workaround")
        if unknown > len(items) * 0.5:
            reasons.append(f"超过半数帖子缺作者（{unknown}/{len(items)}）")
        if not src_urls:
            reasons.append(f"无溯源链接")
        if demand_ratio < min_pain_ratio:
            reasons.append(f"痛点密度不足（需求类占比 {demand_ratio:.0%} < {min_pain_ratio:.0%}）")
        out.append({
            "cluster_id": cid, "title": items[0]["title"][:80],
            "posts": len(items), "unique_authors": len(authors),
            "unknown_author_posts": unknown, "block_reason": "; ".join(reasons) or None,
            "pain_ratio": round(pain_ratio, 3), "demand_ratio": round(demand_ratio, 3),
            "workaround_kinds": kinds,
            "workaround_score_avg": round(sum(i["workaround_score"] for i in items) / len(items), 2),
            "frequency": freq, "subs": sorted({i["sub"] for i in items if i["sub"]}),
            "pay_posts": sum(1 for i in items if i["pay_hits"]),
            "pain_posts": pain_n,
            "sample_urls": src_urls[:5],
            "passed": not reasons,
            "who": items[0]["who"], "tools_used": sorted({t for i in items for t in i["tools_used"]}),
            # 簇代表样本：优先给「真需求 + 有 workaround」的那几条，便于人工判断簇是否成立
            "sample_titles": [i["title"][:110] for i in items
                              if set(i["pain_hits"]) & {"ask_tool", "complain", "workaround", "repeating"}
                              and i["_kinds"]][:5] or [i["title"][:110] for i in items[:3]],
        })
    out.sort(key=lambda c: (c["unique_authors"] * c["demand_ratio"]
                           + c["workaround_score_avg"] * 2 + c["pay_posts"] * 3), reverse=True)
    return out


# ---------- provider ----------
def fetch_manual(inbox):
    posts, files = [], sorted(glob.glob(os.path.join(inbox, "*.json")))
    for f in files:
        with open(f, encoding="utf-8") as fh:
            data = json.load(fh)
        posts.extend(data if isinstance(data, list) else data.get("posts", []))
    return posts


def fetch_sample(path):
    p = path if os.path.isabs(path) else os.path.join(HERE, path)
    if not os.path.exists(p):
        raise SystemExit(f"[sample] 样本不存在：{p}")
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def _ab_cmd():
    """定位 agent-browser 入口：优先 .cmd（Windows 可直接 exec），退回 .ps1（需 pwsh 包一层）。"""
    for name in ("agent-browser.cmd", "agent-browser"):
        p = shutil.which(name)
        if p and not p.lower().endswith(".ps1"):
            return [p]
    p = shutil.which("agent-browser")
    if p and p.lower().endswith(".ps1"):
        pw = shutil.which("pwsh") or shutil.which("powershell")
        if pw:
            return [pw, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", p]
    return None


def _kill_tree(pid):
    """Windows 下 subprocess.run(timeout) 杀不掉 node 孙进程会导致永久挂起，必须杀进程树。"""
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)
    else:
        try:
            os.killpg(os.getpgid(pid), 9)
        except OSError:
            pass


def _ab(args, timeout=45):
    cmd = _ab_cmd()
    if not cmd:
        raise RuntimeError("agent-browser 不可用（未安装或不在 PATH）")
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    p = subprocess.Popen(cmd + args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True, encoding="utf-8", errors="replace", creationflags=flags)
    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(p.pid)
        try:
            p.communicate(timeout=10)
        except Exception:                       # noqa: BLE001 清理阶段不再抛
            pass
        raise RuntimeError(f"agent-browser 超时 {timeout}s（已杀进程树）")
    if p.returncode != 0:
        raise RuntimeError(((err or out or "")[:200]) or f"exit={p.returncode}")
    return out or ""


def net_reachable(host="www.reddit.com", port=443, timeout=5.0):
    """合规 §1：代理不通 -> 当日跳过采集，不重试到死。"""
    import socket
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


ANCHOR_RE = re.compile(r'<a\b[^>]*class="[^"]*search-title[^"]*"[^>]*>(.*?)</a>', re.I | re.S)
HREF_RE = re.compile(r'href="([^"]+)"', re.I)
DATA_AUTHOR_RE = re.compile(r'data-author="([^"]+)"', re.I)
DATA_PERMALINK_RE = re.compile(r'data-permalink="([^"]+)"', re.I)
TAG_RE = re.compile(r"<[^>]+>")


def _last(rx, s):
    """取窗口内**最后一个**匹配：窗口是 anchor 之前的 1500 字，第一个匹配多半属于上一条结果。"""
    found = rx.findall(s)
    return found[-1] if found else None


def _parse_one(tag, inner, window, sub):
    href = HREF_RE.search(tag)
    url = (href.group(1) if href else "") or (_last(DATA_PERMALINK_RE, window) or "")
    if url.startswith("/"):
        url = "https://old.reddit.com" + url
    return {"title": TAG_RE.sub("", inner).strip(), "sub": sub, "url": url,
            "body": "", "author": _last(DATA_AUTHOR_RE, window) or ""}


def parse_search_html(html, sub):
    """从搜索结果页 HTML 提取 (title, url, author)。

    v1.1.2 修（Codex 复审指出的最严重静默失真）：
      - 必须同时抽出 `url`（合规要溯源）与 `author`（用于去重计数）
      - 抽不到 => 该条不产出；结果为空由调用方判失败，**不用 post_id 冒充作者**
    """
    out = []
    for m in ANCHOR_RE.finditer(html):
        window = html[max(0, m.start() - 1500):m.start()]
        row = _parse_one(m.group(0), m.group(1), window, sub)
        if len(row["title"]) >= 6:
            out.append(row)
    return out


def fetch_browser(subs_cfg, limit_per_query, rl, per_sub_kw=3):
    """本人登录态浏览器只读采集（v1.1 新增；网络受限时需先确认可达）。

    路径 A（首选）：agent-browser read <search_url> --raw --json  —— 走 agent-browser 内核抓原始 HTML
    路径 B（回退）：agent-browser open <url> --executable-path <edge> + agent-browser read  —— 借本人登录态
    合规：所有请求过 ratelimit（配额/间隔/退避），单会话 <= 50 页。
    """
    if not net_reachable():
        rl.fail_log("browser", "reddit.com:443 不可达（直连/代理均不通）—— 当日跳过采集，不重试")
        raise SystemExit("[radar][browser] reddit 不可达，当日跳过采集（合规 §1：不重试到死）")
    edge = COMPLIANCE.get("browser_executable_path")
    posts, fails = [], 0
    for sub, kws in subs_cfg.items():
        for kw in kws[:per_sub_kw]:
            if not rl.page_limit_ok():      # v1.1.2: 按真实请求数计，回退分支也计入
                print(f"[radar][browser] 单会话请求上限 {rl.session_page_max} 已达，停止采集", file=sys.stderr)
                return posts, {"requests": rl.session_requests, "fails": fails, "stopped": "page_limit"}
            url = ("https://old.reddit.com/r/%s/search?q=%s&restrict_sr=on&sort=new&t=year"
                   % (sub, __import__("urllib.parse", fromlist=["quote"]).quote('"' + kw + '"')))
            if not rl.tick(sub, kind="browser"):
                return posts, {"requests": rl.session_requests, "fails": fails, "stopped": "quota"}

            def grab():
                return _ab(["read", url, "--raw", "--json", "--timeout", "15000"], timeout=45)

            ok, html, err = rl.with_backoff(grab, sub=sub, kind="browser")
            if not ok:
                fails += 1
                if fails >= 3:
                    return posts, {"requests": rl.session_requests, "fails": fails, "stopped": "three_failures"}
                continue
            rows = parse_search_html(html, sub)
            if not rows and edge:                      # 回退到本人登录态
                def open_tab():
                    return _ab(["open", url, "--executable-path", edge], timeout=90)
                ok2, _r, _e = rl.with_backoff(open_tab, sub=sub, kind="browser")
                if ok2:
                    ok3, txt, _e3 = rl.with_backoff(lambda: _ab(["read", "--json"], timeout=60),
                                                    sub=sub, kind="browser")
                    if ok3:
                        rows = parse_search_html(txt, sub)
            if not rows:
                print(f"[radar][browser] 解析为空：r/{sub} {kw}（可能是 403/登录墙/HTML 结构变化）", file=sys.stderr)
                fails += 1
                if fails >= 3:
                    return posts, {"requests": rl.session_requests, "fails": fails, "stopped": "three_failures"}
                continue
            no_url = sum(1 for r in rows if not r["url"])
            no_author = sum(1 for r in rows if not r["author"])
            if no_url or no_author:
                print(f"[radar][browser] r/{sub} {kw}: {len(rows)} 条中 缺url={no_url} 缺author={no_author}"
                      "（缺的会被 cluster 的准入规则拦掉，不冒充）", file=sys.stderr)
            posts.extend(rows[:limit_per_query])
    return posts, {"requests": rl.session_requests, "fails": fails, "stopped": None}


def fetch_oauth(subs_cfg, limit_per_query, rl):
    """官方 API（OAuth）。凭证走环境变量，缺失即明确报错，不静默失败。
    v1.1：UA 用 compliance 里的真实可联系用户名；响应非 JSON/无 children 均容错。"""
    import base64
    import urllib.parse
    import urllib.request
    cid, secret = os.environ.get("REDDIT_CLIENT_ID"), os.environ.get("REDDIT_CLIENT_SECRET")
    if not (cid and secret):
        raise SystemExit("[reddit-oauth] 缺 REDDIT_CLIENT_ID/SECRET；新申请基本不批，改用 --provider manual/browser")
    ua = COMPLIANCE.get("user_agent", "niche-radar/1.1")
    req = urllib.request.Request(
        "https://www.reddit.com/api/v1/access_token",
        data=urllib.parse.urlencode({"grant_type": "client_credentials"}).encode(),
        headers={"User-Agent": ua,
                 "Authorization": "Basic " + base64.b64encode(f"{cid}:{secret}".encode()).decode()})
    with urllib.request.urlopen(req, timeout=20) as r:
        tok = json.load(r)["access_token"]
    hdr = {"User-Agent": ua, "Authorization": f"bearer {tok}"}
    posts, n = [], 0
    for sub, kws in subs_cfg.items():
        for kw in kws[:3]:
            if not rl.tick(sub, kind="oauth"):
                return posts, n
            q = urllib.parse.quote(f'{kw} subreddit:{sub}')
            u = f"https://oauth.reddit.com/search?q={q}&limit={limit_per_query}&sort=new&t=year"

            def get():
                with urllib.request.urlopen(urllib.request.Request(u, headers=hdr), timeout=25) as r:
                    return json.load(r)

            ok, data, err = rl.with_backoff(get, sub=sub, kind="oauth")
            n += 1
            if not ok:
                print(f"[warn] {sub}/{kw}: {err}", file=sys.stderr)
                continue
            children = ((data or {}).get("data") or {}).get("children")
            if not isinstance(children, list):
                print(f"[warn] {sub}/{kw}: 响应结构异常（非搜索 JSON，可能被拦）", file=sys.stderr)
                continue
            for ch in children:
                d = (ch or {}).get("data") or {}
                if not d.get("id"):
                    continue
                posts.append({"post_id": d["id"], "sub": d.get("subreddit") or sub,
                              "created_utc": safe_int(d.get("created_utc"), 0),
                              "title": d.get("title", ""), "body": (d.get("selftext") or "")[:2000],
                              "url": "https://reddit.com" + (d.get("permalink") or ""),
                              "author": d.get("author", "")})
    return posts, n


# ---------- 存储 ----------
def save_db(db_path, posts, signals, clusters):
    if not db_path:
        return {"posts": 0, "clusters": 0, "signals": 0}
    d = os.path.dirname(os.path.abspath(db_path))
    if d:
        os.makedirs(d, exist_ok=True)
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA foreign_keys=ON")
    con.executescript(open(os.path.join(HERE, "schema.sql"), encoding="utf-8").read())
    now = int(time.time())
    n_posts = n_sig = 0

    if len(posts) != len(signals):
        print(f"[warn] posts({len(posts)}) != signals({len(signals)})：按短的配对，剩余丢弃", file=sys.stderr)
    for p, s in zip(posts, signals):
        try:
            con.execute(
                """INSERT INTO posts(post_id,sub,created_utc,title,url,pain_hits,pay_hits,fetched_at,last_seen,seen_count)
                   VALUES (?,?,?,?,?,?,?,?,?,1)
                   ON CONFLICT(post_id) DO UPDATE SET
                     last_seen=excluded.last_seen,
                     seen_count=COALESCE(posts.seen_count,1)+1,
                     title=excluded.title, url=COALESCE(excluded.url,posts.url)""",
                (s["post_id"], s["sub"], s["created_utc"], s["title"], s["url"],
                 json.dumps(s["pain_hits"], ensure_ascii=False),
                 json.dumps(s["pay_hits"], ensure_ascii=False), now, now))
            con.execute(
                """INSERT INTO signals(post_id,who,problem,pain_why,workaround,frequency,tools_used,gap,pay_signal,workaround_score)
                   VALUES (?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(post_id) DO UPDATE SET
                     who=excluded.who, problem=excluded.problem, pain_why=excluded.pain_why,
                     workaround=excluded.workaround, frequency=excluded.frequency,
                     tools_used=excluded.tools_used, gap=excluded.gap,
                     pay_signal=excluded.pay_signal, workaround_score=excluded.workaround_score""",
                (s["post_id"], s["who"], s["problem"], s["pain_why"], s["workaround"], s["frequency"],
                 json.dumps(s["tools_used"], ensure_ascii=False), s["gap"], s["pay_signal"], s["workaround_score"]))
            n_posts += 1
            n_sig += 1
        except sqlite3.Error as e:                  # v1.1: 单帖失败不炸整批
            print(f"[warn] 写库失败 {s.get('post_id')}: {e}", file=sys.stderr)
    n_cl = 0
    for c in clusters:
        try:
            con.execute(
                """INSERT INTO clusters(cluster_id,title,posts,unique_authors,workaround_kinds,frequency,subs,
                                        pay_posts,sample_urls,first_seen,last_seen,appearances)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,1)
                   ON CONFLICT(cluster_id) DO UPDATE SET
                     title=excluded.title, posts=excluded.posts, unique_authors=excluded.unique_authors,
                     workaround_kinds=excluded.workaround_kinds, frequency=excluded.frequency,
                     subs=excluded.subs, pay_posts=excluded.pay_posts, sample_urls=excluded.sample_urls,
                     last_seen=excluded.last_seen,
                     appearances=COALESCE(clusters.appearances,1)+1""",
                (c["cluster_id"], c["title"], c["posts"], c["unique_authors"],
                 json.dumps(c["workaround_kinds"], ensure_ascii=False), c["frequency"],
                 json.dumps(c["subs"], ensure_ascii=False), c["pay_posts"],
                 json.dumps(c["sample_urls"], ensure_ascii=False), now, now))
            n_cl += 1
        except sqlite3.Error as e:
            print(f"[warn] 写簇失败 {c.get('cluster_id')}: {e}", file=sys.stderr)
    con.commit()
    con.close()
    return {"posts": n_posts, "signals": n_sig, "clusters": n_cl}


def write_json(path, obj):
    d = os.path.dirname(os.path.abspath(path))
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", default="manual",
                    choices=["manual", "sample", "browser", "oauth", "arctic"])
    ap.add_argument("--inbox", default="inbox")
    ap.add_argument("--sample", default=os.path.join("sample", "sample_posts.json"))
    ap.add_argument("--subs", default=os.path.join("config", "subs.json"))
    ap.add_argument("--out", default="working/clusters.json")
    ap.add_argument("--db", default="db/factory.db")
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--max-requests", type=int, default=int(COMPLIANCE.get("daily_max_requests", 300)))
    ap.add_argument("--dry-run", action="store_true", help="限流等待改为 0 秒（仅冒烟用）")
    # arctic provider 专用：--arctic-limit 每窗口每 sub 拉多少帖；--windows 历史回溯窗口（距今天数）
    ap.add_argument("--arctic-limit", type=int, default=100)
    ap.add_argument("--windows", default="0,30,60",
                    help="Arctic Shift 的 before 历史窗口（距今天数，逗号分隔）")
    ap.add_argument("--drop-supply", action="store_true", default=True,
                    help="过滤供给方噪声（我做了个工具 / 求职帖），默认开")
    ap.add_argument("--keep-supply", dest="drop_supply", action="store_false")
    # 分片：单次全量 24 sub × 3 窗口 ≈ 72 请求 > 后台任务 2 分钟上限，必须分批
    ap.add_argument("--shard", default="",
                    help="分片，形如 0/3 = 第 0 批共 3 批（按 sub 列表切分）")
    ap.add_argument("--raw-out", default="",
                    help="把抓到的原始帖落盘（含 body/author，供跨批次全局聚类复用；"
                         "posts 表不存这两列）")
    args = ap.parse_args()

    from ratelimit import RateLimiter
    sleep_fn = (lambda _s: None) if args.dry_run else time.sleep
    page_max = int(COMPLIANCE.get("session_page_max", 50))
    rl = RateLimiter(args.db, daily_max=args.max_requests,
                     min_gap_s=float(COMPLIANCE.get("min_gap_seconds", 5)), sleep_fn=sleep_fn,
                     session_page_max=page_max,
                     daily_max_by_kind={"browser": page_max, "url_check": page_max})

    net_meta = {"pages": 0, "stopped": None}
    if args.provider == "manual":
        posts = fetch_manual(args.inbox)
    elif args.provider == "sample":
        posts = fetch_sample(args.sample)
    elif args.provider == "browser":
        cfg = json.load(open(os.path.join(HERE, args.subs), encoding="utf-8"))
        posts, net_meta = fetch_browser(cfg.get("subs", cfg), 25, rl)
    elif args.provider == "arctic":
        # v1.1.5：本机 reddit.com 不通，走 Arctic Shift 公开归档（见 arctic.py 头部实测结论）
        import arctic
        cfg = json.load(open(os.path.join(HERE, args.subs), encoding="utf-8"))
        subs = list((cfg.get("subs") or {}).keys())
        if args.shard:
            i, n = (int(x) for x in args.shard.split("/", 1))
            subs = subs[i::n] if i < n else []
            print(f"[radar] 分片 {i}/{n} -> {len(subs)} 个 sub: {', '.join(subs)}", file=sys.stderr)
        windows = tuple(int(x) for x in args.windows.split(",") if x.strip())
        st = {}
        posts = arctic.fetch(subs, windows=windows, limit=args.arctic_limit,
                             sleep_fn=(lambda _s: None) if args.dry_run else time.sleep,
                             max_requests=args.max_requests, log=lambda m: print(m, file=sys.stderr))
        net_meta = {"provider": "arctic_shift", "requests": st.get("requests", 0),
                    "raw_posts": st.get("raw_posts", 0), "errors": st.get("errors", [])[:20],
                    "error_count": len(st.get("errors", [])),
                    "windows": list(windows), "limit": args.arctic_limit,
                    "stopped_at_daily_max": st.get("stopped_at_daily_max", False),
                    "official_subscribers": st.get("official_subscribers", {})}
        # 供给方噪声过滤（JEV: 标题正则 + 正文场景双条件，正文缺失时放宽）
        if args.drop_supply:
            kept, dropped = [], []
            for p in posts:
                is_sup, why = arctic.classify_supply(p.get("title"), p.get("body"))
                (dropped if is_sup else kept).append((p, why))
            net_meta["supply_dropped"] = len(dropped)
            net_meta["supply_kept"] = len(kept)
            net_meta["supply_reasons"] = sorted({w for _, w in dropped})
            print(f"[radar] 供给方噪声过滤：丢 {len(dropped)} / {len(posts)} 帖"
                  f"（{len(dropped)/max(len(posts),1):.1%}），理由分布 {net_meta['supply_reasons']}",
                  file=sys.stderr)
            posts = [p for p, _ in kept]
        # 分批采集时必须落原始帖：聚类要全局做，而 posts 表不存 body/author
        if args.raw_out:
            write_json(args.raw_out, posts)
        # 官方订阅数 vs 第三方站，只报告差异不改配置（JEV 0.39：不采纳直接替代）
        try:
            rows = arctic.cross_check(st.get("official_subscribers", {}),
                                      os.path.join(HERE, args.subs))
            net_meta["subs_cross_check"] = rows[:8]
            if rows:
                worst = rows[0]
                print(f"[radar] 官方订阅数交叉校验：最大偏差 r/{worst['sub']} "
                      f"{worst['diff_pct']:+.1f}%（官方 {worst['official']:,} vs 第三方 {worst['third_party']:,}）",
                      file=sys.stderr)
        except Exception as e:                      # noqa: BLE001
            print(f"[warn] 订阅数交叉校验失败：{type(e).__name__}: {e}", file=sys.stderr)
    else:
        cfg = json.load(open(os.path.join(HERE, args.subs), encoding="utf-8"))
        posts, net_meta = fetch_oauth(cfg.get("subs", cfg), 25, rl)

    if args.provider in ("browser", "oauth", "arctic") and not posts:
        # 联网 provider 抓到 0 帖 = 采集失败，必须留痕，不能静默报成功（v1.1.1 自查修）
        rl.fail_log(args.provider, "采集 0 帖（解析失败 / 登录墙 / 被拦），本次结果不可用")
        print(f"[radar][warn] {args.provider} 采集 0 帖 —— 结果不可用，已写 logs/fail.md", file=sys.stderr)
        net_meta["empty_fetch"] = True

    signals, failed = [], 0
    for p in posts:
        try:
            signals.append(extract(p))
        except Exception as e:                      # noqa: BLE001 单帖隔离
            failed += 1
            print(f"[warn] 抽取失败 {p.get('post_id')}: {type(e).__name__}: {e}", file=sys.stderr)
    if failed:
        print(f"[radar] 抽取失败 {failed} 帖（已跳过，不中断）", file=sys.stderr)

    clusters = cluster(signals)
    keep = [c for c in clusters if c["passed"]][:args.top]
    db_stat = save_db(args.db, posts, signals, clusters)
    write_json(args.out, {"generated_at": int(time.time()), "provider": args.provider,
                          "posts": len(posts), "extract_failed": failed,
                          "clusters": len(clusters), "kept": len(keep),
                          "network": net_meta, "db": db_stat,
                          "candidates": keep, "dropped": len(clusters) - len(keep)})
    print(f"[radar] posts={len(posts)} clusters={len(clusters)} kept={len(keep)} -> {args.out}")
    if net_meta.get("empty_fetch"):
        # v1.1.2: 联网 provider 0 帖必须非零退出，避免下游把「什么也没抓到」当成 OK
        raise SystemExit(3)


if __name__ == "__main__":
    main()
