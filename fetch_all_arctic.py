# -*- coding: utf-8 -*-
"""分批跑全量 Arctic 采集 —— 后台任务有 2 分钟硬限，单次 72 请求跑不完。

用法（前台顺序执行，每批独立落盘）：
    python fetch_all_arctic.py 6      # 每批 6 个 sub × 3 窗口 = 18 请求 ≈ 90s
最后一步自动合并 + 抽取 + 聚类（传 merge）。
"""
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
WORK = os.path.join(HERE, "working")
RAW = os.path.join(WORK, "arctic_raw")
LOGDIR = os.path.join(HERE, "review")
BATCH = 6
MAX_AGE_DAYS = 120     # 只要近 120 天的帖；归档站 sort=desc 偶尔失效会混进 2013 年老帖
FRESH = "--fresh" in sys.argv


def load_subs():
    cfg = json.load(open(os.path.join(HERE, "config", "subs.json"), encoding="utf-8"))
    return list((cfg.get("subs") or {}).keys())


def run_batches(subs, windows, limit):
    """按批采集。默认跳过已存在批次；--fresh 时重采（每日任务用 --fresh 才能拿到新帖）。"""
    os.makedirs(RAW, exist_ok=True)
    batches = [subs[i:i + BATCH] for i in range(0, len(subs), BATCH)]
    for bi, batch in enumerate(batches):
        out = os.path.join(RAW, f"batch{bi:02d}.json")
        raw = os.path.join(RAW, f"raw{bi:02d}.json")
        if os.path.exists(out) and not FRESH:
            print(f"[skip] batch{bi:02d} 已存在（--fresh 可重采）")
            continue
        # 只把本批 sub 写进临时配置，避免 --shard 索引随 subs.json 变动而错位
        tmp_cfg = os.path.join(RAW, f"_cfg{bi:02d}.json")
        json.dump({"subs": {s: [] for s in batch}},
                  open(tmp_cfg, "w", encoding="utf-8"), ensure_ascii=False)
        cmd = [PY, os.path.join(HERE, "radar.py"), "--provider", "arctic",
               "--subs", tmp_cfg, "--arctic-limit", str(limit),
               "--windows", windows, "--out", out,
               "--raw-out", raw,
               "--db", os.path.join(HERE, "db", "factory.db"),
               "--top", "200"]
        print(f"=== batch {bi+1}/{len(batches)}  {len(batch)} sub ===", flush=True)
        t0 = time.time()
        try:
            p = subprocess.run(cmd, cwd=HERE, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=600)
        except subprocess.TimeoutExpired:
            print(f"    !! batch {bi} 超时 600s，跳过（保留上次的 raw）")
            continue
        print(f"    exit={p.returncode} {time.time()-t0:.0f}s")
        if p.stderr:
            for line in p.stderr.strip().splitlines()[-4:]:
                print("   ", line)
        if p.returncode != 0:
            print(f"    !! batch {bi} 失败（raw{bi:02d}.json 未更新，沿用旧数据）")
    return len(batches)


MAX_AGE_DAYS = 120     # 只要近 120 天的帖；归档站 sort=desc 偶尔失效会混进 2013 年老帖


def merge(max_age_days=MAX_AGE_DAYS):
    """合并各批原始帖 -> 全局去重 -> 抽取 -> 全局聚类（聚类必须全局做，跨 sub 才有意义）。"""
    sys.path.insert(0, HERE)
    import radar
    import arctic
    cut = time.time() - max_age_days * 86400
    posts, seen, batches, stale = [], set(), [], 0
    for f in sorted(os.listdir(RAW)):
        if not (f.startswith("raw") and f.endswith(".json")):
            continue
        got = json.load(open(os.path.join(RAW, f), encoding="utf-8"))
        new = fresh = 0
        for p in got:
            pid = p.get("post_id") or ""
            if pid and pid in seen:
                continue
            if pid:
                seen.add(pid)
            ts = p.get("created_utc") or 0
            try:
                ts = int(ts)
            except (TypeError, ValueError):
                ts = 0
            if ts and ts < cut:
                # v1.1.7：归档站 sort=desc 并非对所有 sub 生效，会返回 2013 年老帖。
                # 老帖的语言/工具生态与今天不同，混进来会系统性失真，直接丢。
                stale += 1
                continue
            posts.append(p)
            fresh += 1
            new += 1
        batches.append({"file": f, "posts": len(got), "kept": new, "stale": len(got) - fresh})
    sig = [radar.extract(p) for p in posts]
    cl = radar.cluster(sig)
    keep = [c for c in cl if c["passed"]]
    # 时间跨度 + 分布，用于人工判断样本代表性
    ts = [p.get("created_utc") or 0 for p in posts if p.get("created_utc")]
    span = {}
    if ts:
        span = {"from": time.strftime("%Y-%m-%d", time.localtime(min(ts))),
                "to": time.strftime("%Y-%m-%d", time.localtime(max(ts))),
                "days": round((max(ts) - min(ts)) / 86400, 1)}
    out = os.path.join(WORK, "clusters_arctic.json")
    radar.write_json(out, {"generated_at": int(time.time()), "provider": "arctic_shift",
                           "posts": len(posts), "stale_dropped": stale,
                           "max_age_days": max_age_days,
                           "time_span": span, "batches": batches,
                           "clusters": len(cl), "kept": len(keep),
                           "candidates": keep, "all_clusters": cl})
    print(f"\n[merge] 原始帖 {sum(b['posts'] for b in batches)} → 过老丢弃 {stale} "
          f"(>{max_age_days}天) → 有效 {len(posts)}，时间跨度 {span}")
    print(f"[merge] 簇 {len(cl)}，通过闸门 {len(keep)} -> {out}")
    return out


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "run"
    subs = load_subs()
    if mode == "merge":
        merge()
    else:
        windows = sys.argv[2] if len(sys.argv) > 2 else "0,30,60"
        limit = int(sys.argv[3]) if len(sys.argv) > 3 else 100
        print(f"共 {len(subs)} 个 sub，{BATCH} 个一批，windows={windows}, limit={limit}")
        run_batches(subs, windows, limit)
        print("\n全部批次完成，跑 merge：python fetch_all_arctic.py merge")
