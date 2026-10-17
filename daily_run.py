# -*- coding: utf-8 -*-
"""daily_run.py —— 每日一键：采集 → 细分 → 出付费意愿日报。

给定时任务用（automation 每天调这个）。四步，任何一步失败都写日志并以非 0 退出，
不做「假装成功」—— 定时任务里静默失败比报错更糟。

用法：
    python daily_run.py              # 增量（默认跳过已采批次，省请求）
    python daily_run.py --fresh      # 全量重采（每日首次运行用，能拿到新帖）
    python daily_run.py --no-fetch   # 只重算细分与日报（调试用，不发请求）

合规：请求数由 arctic provider 内部按 compliance.json 的每日 300 上限截断。
48 个 sub × 3 窗口 = 144 请求/次，--fresh 每天跑一次不会越限；
若同时开了第二个实例，radar 的 ratelimit 会共享同一张 api_calls 表挡住越限。
"""
from __future__ import annotations

import datetime as dt
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
REPORTS = os.path.join(HERE, "reports")
LOGS = os.path.join(HERE, "review", "daily")


def step(name, args, log_fh, timeout=900):
    t0 = time.time()
    log_fh.write(f"\n{'=' * 70}\n[{name}] {' '.join(args)}\n")
    log_fh.flush()
    try:
        p = subprocess.run([PY] + args, cwd=HERE, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        log_fh.write(f"[{name}] ✗ 超时 {timeout}s\n")
        log_fh.flush()
        return False, f"{name} 超时"
    out = (p.stdout or "").strip()
    err = (p.stderr or "").strip()
    if out:
        log_fh.write(out + "\n")
    if err:
        log_fh.write("--- stderr ---\n" + err[-3000:] + "\n")
    log_fh.write(f"[{name}] exit={p.returncode} {time.time() - t0:.0f}s\n")
    log_fh.flush()
    ok = p.returncode == 0
    tail = [ln for ln in out.splitlines() if ln.strip()][-3:] if out else []
    return ok, " ｜ ".join(tail) if ok else f"{name} exit={p.returncode}"


def main():
    argv = sys.argv[1:]
    do_fetch = "--no-fetch" not in argv
    fresh = "--fresh" in argv
    today = dt.date.today().isoformat()
    os.makedirs(LOGS, exist_ok=True)
    os.makedirs(REPORTS, exist_ok=True)
    log_path = os.path.join(LOGS, f"{today}.log")

    summary = []
    rc = 0
    with open(log_path, "w", encoding="utf-8") as lf:
        lf.write(f"每日雷达运行 {dt.datetime.now():%Y-%m-%d %H:%M:%S} "
                 f"fetch={do_fetch}{' --fresh' if fresh else ''}\n")
        if do_fetch:
            args = ["fetch_all_arctic.py", "run", "0,30,60", "100"]
            if fresh:
                args.append("--fresh")
            ok, msg = step("采集", args, lf, timeout=5400)
            summary.append(("采集", ok, msg))
            if not ok:
                rc = 1
            ok, msg = step("合并聚类", ["fetch_all_arctic.py", "merge"], lf)
            summary.append(("合并聚类", ok, msg))
            if not ok:
                rc = 1
        ok, msg = step("细分", ["segment.py"], lf, timeout=300)
        summary.append(("细分", ok, msg))
        if not ok:
            rc = 1
        ok, msg = step("日报", ["daily_report.py"], lf, timeout=120)
        summary.append(("日报", ok, msg))
        if not ok:
            rc = 1

        lf.write(f"\n{'=' * 70}\n汇总：\n")
        for name, ok, msg in summary:
            lf.write(f"  {'✅' if ok else '❌'} {name}: {msg}\n")
        lf.write(f"退出码 {rc}\n")

    report = os.path.join(REPORTS, f"daily-{today}.md")
    print(f"log: {log_path}")
    for name, ok, msg in summary:
        print(f"  {'OK ' if ok else 'FAIL'} {name}: {msg}")
    print(f"report: {report if os.path.exists(report) else '(未生成)'}")
    sys.exit(rc)


if __name__ == "__main__":
    main()
