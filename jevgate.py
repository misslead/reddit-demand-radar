#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""JEV 闸门封装：一次性批量问 N 个维度，返回概率/等级；不可用则保守降级。

v1.1 修复（Codex BLOCKER #3/#4/#5）：
  1. subprocess 显式 encoding="utf-8"（原 GBK 下解中文 JSON 抛错被宽 except 吞掉 → 全维度静默退化 0.5）
  2. json.loads 单独 try；解析失败 -> ({}, {"jev_degraded": True})，不伪装成"低置信"
  3. score 维度下标封顶：n = len(legend) or max(idx)+1，v = clamp(idx/(n-1), 0, 1)
  4. noul 无 confidence，按概率定灰区：0.35 <= p <= 0.65 记 low_conf
入参: 簇 dict；出参: {dim: {"value_raw":..., "confidence":..., "low_conf":bool}}
"""
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
JEV = os.path.join(os.path.dirname(os.path.dirname(HERE)), "tools", "jev.py")
PY = sys.executable or "python"

DIMS = ["pain", "pay", "audience", "gap", "price", "scene", "repeat", "policy", "giant"]

QUESTIONS = {
    "pain":     {"type": "score",  "instructions": "这个需求对用户有多痛", "criteria": ["轻微", "一般", "痛", "极痛"]},
    "pay":      {"type": "noul",   "instructions": "这类用户会为解决这个问题付费吗"},
    "audience": {"type": "score",  "instructions": "目标用户群是否明确", "criteria": ["模糊", "一般", "清晰"]},
    "gap":      {"type": "noul",   "instructions": "现有工具是否明显不适配这个具体场景"},
    "price":    {"type": "score",  "instructions": "用户可接受的价格档（每月或买断）", "criteria": ["<$5", "$5-15", "$15-50", ">$50"]},
    "scene":    {"type": "choice", "instructions": "购买场景", "criteria": {"工作必需可报销": None, "工作必需自费": None, "爱好自愿": None, "纯吐槽": None}},
    "repeat":   {"type": "noul",   "instructions": "用户会重复或持续使用吗"},
    "policy":   {"type": "noul",   "instructions": "是否存在合规风险或平台政策风险"},
    "giant":    {"type": "noul",   "instructions": "是否已有巨头产品直接覆盖这个需求"},
}

CHOICE_MAP = {"工作必需可报销": 1.0, "工作必需自费": 0.7, "爱好自愿": 0.4, "纯吐槽": 0.0}

NOUL_GRAY = (0.35, 0.65)      # noul 无 confidence，落在灰区即视为低置信


def load_compliance():
    p = os.path.join(HERE, "config", "compliance.json")
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except OSError:
        return {}


def build_state(c):
    return (
        f"需求簇：{c['title']}\n"
        f"目标用户：{c.get('who')}（来自 r/{','.join(c.get('subs', []))}）\n"
        f"使用频率：{c.get('frequency')}；独立提及用户：{c.get('unique_authors')} 人；帖子数：{c.get('posts')}\n"
        f"当前笨办法：{', '.join(c.get('workaround_kinds', [])) or '无'}\n"
        f"现用工具：{', '.join(c.get('tools_used', [])[:6]) or '无'}\n"
        f"付费信号帖子数：{c.get('pay_posts')}\n"
    )


def model_tag():
    """判定模型标识。**换模型时必须设 `JEV_MODEL=<名字>`**，否则缓存会复用旧模型答案。"""
    return os.environ.get("JEV_MODEL", "").strip()


def model_warn(meta):
    """v1.1.4：未设 JEV_MODEL 时补告警（Codex 收口复核的 PARTIAL）。
    未设时缓存 key 里的模型标识是占位符 'unset'，换模型会命中旧答案 —— 必须让调用方可观测。"""
    if not model_tag():
        meta["jev_model_unset"] = True
        meta["jev_model_hint"] = "未设 JEV_MODEL；换判定模型前请 set JEV_MODEL=<模型名> 或跑 jevgate.py --clear-cache"
    return meta


def cache_key(c, tag=None):
    """簇指纹 + 模型标识：同一簇同一模型复用上次判定，保证同输入同输出。

    注意：未设 JEV_MODEL 时 model 字段是空串（不改成 "unset"）—— 保持 key 稳定，
    否则一次「语义优化」就会作废全部既有缓存。可观测性交给 model_warn()。
    """
    import hashlib
    raw = json.dumps({"fp": {k: c.get(k) for k in ("cluster_id", "who", "unique_authors", "posts",
                                                   "frequency", "workaround_kinds", "pay_posts", "subs")},
                      "model": tag if tag is not None else model_tag()},
                     sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:16]


def _cache_get(db_path, key):
    if not db_path:
        return None, None
    import sqlite3
    try:
        con = sqlite3.connect(db_path)
        con.executescript(open(os.path.join(HERE, "schema.sql"), encoding="utf-8").read())
        row = con.execute("SELECT answers, model FROM jev_cache WHERE cache_key=?", (key,)).fetchone()
        con.close()
        return (json.loads(row[0]), row[1]) if row else (None, None)
    except (sqlite3.Error, ValueError):
        return None, None


def _cache_put(db_path, key, cluster_id, answers, model):
    if not db_path or not answers:
        return
    if any(d not in answers for d in DIMS):        # 不完整的答案不缓存
        return
    import sqlite3
    import time
    try:
        con = sqlite3.connect(db_path)
        con.executescript(open(os.path.join(HERE, "schema.sql"), encoding="utf-8").read())
        con.execute("INSERT INTO jev_cache(cache_key,cluster_id,answers,model,created_at) VALUES (?,?,?,?,?) "
                    "ON CONFLICT(cache_key) DO UPDATE SET answers=excluded.answers, "
                    "model=excluded.model, created_at=excluded.created_at",
                    (key, cluster_id, json.dumps(answers, ensure_ascii=False), model, int(time.time())))
        con.commit()
        con.close()
    except sqlite3.Error:
        pass


def ask(c, timeout=120, cache_db=None):
    """返回 (answers, meta)。JEV 不可用 -> ({}, {"jev_unavailable": True})。

    v1.1.2：cache_db 命中即复用（meta.cached=true）—— 同一簇两次跑结果一致，也省 token。
    """
    key = cache_key(c)
    if cache_db:
        cached, model = _cache_get(cache_db, key)
        if cached:
            meta = model_warn({"cached": True, "model": model, "cache_key": key, "model_tag": model_tag()})
            missing = [d for d in DIMS if d not in cached]
            if missing:                       # 缓存里维度不全 -> 明确告知，不假装完整
                meta["jev_partial"] = missing
            return cached, meta
    req = {"state": build_state(c), "questions": QUESTIONS}
    fd, path = tempfile.mkstemp(suffix=".json", prefix="jevreq_")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(req, f, ensure_ascii=False)
    try:
        p = subprocess.run([PY, JEV, "batch", path], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        if p.returncode != 0:
            return {}, {"jev_unavailable": True, "returncode": p.returncode,
                        "stderr": (p.stderr or "")[:300]}
        try:
            data = json.loads(p.stdout)
        except (ValueError, TypeError) as e:
            # 不伪装成低置信：显式标记降级，下游按"缺维度"处理（默认 0.5 + low_conf）
            return {}, {"jev_degraded": True, "error": f"json_decode: {e}"[:200],
                        "stdout_head": (p.stdout or "")[:200]}
        answers = data.get("answers") or {}
        meta = model_warn({"usage": data.get("usage", {}), "model": data.get("model")})
        missing = [d for d in DIMS if d not in answers]
        if missing:
            meta["jev_partial"] = missing
        else:
            _cache_put(cache_db, key, c.get("cluster_id"), answers, data.get("model"))
        return answers, meta
    except subprocess.TimeoutExpired:
        return {}, {"jev_unavailable": True, "error": "timeout"}
    except Exception as e:                      # noqa: BLE001 进程级异常统一降级
        return {}, {"jev_unavailable": True, "error": f"{type(e).__name__}: {e}"[:300]}
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def _clamp(v, lo=0.0, hi=1.0):
    return max(lo, min(hi, v))


def num(ans, dim, default=0.5):
    """统一取值：score -> idx/(n-1)（封顶 1）；noul -> p（按灰区定 low_conf）；choice -> 映射表。
    返回 (value, confidence, low_conf)。维度缺失 -> (default, 0.0, True)。"""
    a = ans.get(dim)
    if not a:
        return default, 0.0, True
    t = a.get("type")
    conf = a.get("confidence")
    if t == "score":
        legend = a.get("legend") or {}
        probs = a.get("probabilities") or {}
        if probs:
            try:
                idx = sum(int(k) * float(v) for k, v in probs.items())
            except (TypeError, ValueError):
                idx = float(a.get("score") or 0)
        else:
            idx = float(a.get("score") or 0)
        n = len(legend) or (int(idx) + 1)          # 无 legend 时按实际下标定长，避免不封顶
        denom = max(n - 1, 1)
        v = _clamp(idx / denom)
        low = conf is not None and conf < 0.5
        return round(v, 3), (conf if conf is not None else 0.0), low
    if t == "noul":
        p = a.get("noul")
        if p is None:
            return default, 0.0, True
        p = float(p)
        low = NOUL_GRAY[0] <= p <= NOUL_GRAY[1]
        return round(_clamp(p), 3), 1.0, low
    if t == "choice":
        ch = a.get("choice")
        v = CHOICE_MAP.get(ch, default)
        low = conf is not None and conf < 0.5
        return round(v, 3), (conf if conf is not None else 0.0), low
    return default, 0.0, True


def clear_cache(db_path):
    """清空 JEV 缓存（换模型 / 改了提问词后建议清一次）。"""
    import sqlite3
    if not os.path.exists(db_path):
        return 0
    con = sqlite3.connect(db_path)
    try:
        con.executescript(open(os.path.join(HERE, "schema.sql"), encoding="utf-8").read())
        n = con.execute("SELECT COUNT(*) FROM jev_cache").fetchone()[0]
        con.execute("DELETE FROM jev_cache")
        con.commit()
        return n
    finally:
        con.close()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--clear-cache":
        db = sys.argv[2] if len(sys.argv) > 2 else os.path.join(HERE, "db", "factory.db")
        print(f"[jevgate] 清空 jev_cache：{clear_cache(db)} 条（{db}）")
        raise SystemExit(0)
    demo = {"title": "批量按客户规则重命名交付文件", "who": "摄影师",
            "subs": ["photography"], "frequency": "weekly", "unique_authors": 10, "posts": 12,
            "workaround_kinds": ["script", "spreadsheet"], "tools_used": ["excel", "python"], "pay_posts": 3}
    ans, meta = ask(demo)
    print(json.dumps({"meta": meta}, ensure_ascii=False, indent=2))
    for d in DIMS:
        print(d, num(ans, d))
