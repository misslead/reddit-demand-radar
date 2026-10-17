#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""每日流水线：radar -> score -> 日报 + BUILD 任务草稿（等 G0 人工放行，不自动开工）

用法:
  run_daily.py [--provider manual|sample|browser|oauth] [--form web-single-tool]
               [--inbox inbox|sample] [--no-jev] [--dry-run] [--max-build-per-week 2]

v1.1 修复（Codex BLOCKER #9 + 合规 §2.4）:
  - task id 带日期（task-YYYYMMDD-NNN），不再每日覆盖
  - 原子写（.tmp -> os.replace）+ status 状态机（PENDING_G0 -> ...）
  - 近 7 日放行 BUILD 数 <= 2（默认），避免每天 3 个 = 21/周
  - landing 文案全部自撰（禁止照搬原帖标题 / who 原话）
  - 写 runs 表；结束时跑 retention（90 天 TTL）
"""
import argparse
import datetime as dt
import glob
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable or "python"

VERB_CN = {"rename": "批量重命名", "convert": "格式转换", "merge": "合并", "split": "拆分", "export": "批量导出",
           "import": "批量导入", "clean": "数据清洗", "deduplicate": "去重", "dedupe": "去重", "format": "格式整理",
           "batch": "批处理", "automate": "自动化", "sort": "排序", "extract": "信息提取", "generate": "批量生成",
           "calculate": "批量计算", "track": "跟踪", "compare": "对比", "organize": "整理归档", "resize": "批量缩放",
           "compress": "批量压缩", "translate": "批量翻译", "scrape": "抓取整理", "sync": "同步", "backup": "备份",
           "tag": "批量打标", "fill": "批量填充", "match": "匹配", "parse": "解析"}
OBJ_CN = {"file": "文件", "table": "表格数据", "doc": "文档", "money": "账务单据", "media": "字幕音频",
          "text": "文本记录", "x": "数据"}


def run(args, dry_run=False):
    p = subprocess.run([PY] + args + (["--dry-run"] if dry_run else []),
                       cwd=HERE, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    print((p.stdout or "").strip())
    # v1.1.2：成功时也回显 stderr —— 子步骤的 [warn]（如写 scores 失败）不能被吞掉
    if (p.stderr or "").strip():
        print((p.stderr or "").strip(), file=sys.stderr)
    if p.returncode != 0:
        raise SystemExit(f"[fail] {' '.join(args)} exit={p.returncode}")
    return p.stdout or ""


def self_authored_name(s):
    """自撰产品名：只用聚类指纹（动词+对象）+ 用户群，绝不照搬原帖标题。"""
    cid = s.get("cluster_id") or ""
    verb, _, obj = cid.partition("_")
    v_cn = VERB_CN.get(verb, "批处理")
    o_cn = OBJ_CN.get(obj, "数据")
    return f"{v_cn}{o_cn}助手"


def recent_build_tasks(days=7):
    """近 N 日已生成的开工任务（BUILD 与 PILOT 都算占额度）。"""
    cut = dt.date.today() - dt.timedelta(days=days)
    n = 0
    for f in glob.glob(os.path.join(HERE, "tasks", "*.json")):
        base = os.path.basename(f)
        if not (base.startswith("task-") or base.startswith("pilot-")):
            continue
        try:
            d = dt.datetime.strptime(base.split("-", 2)[1], "%Y%m%d").date()
        except (IndexError, ValueError):
            continue
        if d >= cut:
            n += 1
    return n


def make_task(s, idx, day):
    name = self_authored_name(s)
    is_pilot = s.get("decision") == "PILOT"
    prefix = "pilot" if is_pilot else "task"
    task = {
        "id": f"{prefix}-{day}-{idx:03d}",
        "created": dt.date.today().isoformat(),
        "cluster_id": s["cluster_id"],
        "summary": f"{name}（{s['who']}）：{s['frequency']}频率，{len(s['workaround_kinds'])} 种现成笨办法",
        "decision": s.get("decision", "BUILD"),
        "stage": "landing-only" if is_pilot else "full-mvp",
        "final_score": s["final"],
        "form": s["form"],
        "status": "PENDING_G0",          # PENDING_G0 -> APPROVED -> IN_PROGRESS -> QA -> DONE / REJECTED
        "status_history": [{"at": int(time.time()), "to": "PENDING_G0", "by": "run_daily"}],
        "scope": {
            "must": (["Landing Page + 1 个核心动作演示（无需后端）", "埋点：访问/点击/留邮箱"]
                     if is_pilot else
                     ["输入 -> 处理 -> 输出 的单一闭环", "结果可下载/复制", "3 条测试用例"]),
            "must_not": ["账号系统", "支付", "数据库", "多语言", "团队协作"],
        },
        "acceptance": (["30 秒内看懂做什么", "有明确 CTA", "无 console 报错"] if is_pilot
                       else ["首次使用 30 秒内出结果", "空输入/非法输入不崩", "无 console 报错"]),
        # 合规：文案自撰，不含原帖标题 / 用户原话
        "landing": {"headline": f"{name}——{VERB_CN.get((s['cluster_id'] or '').partition('_')[0], '批量处理')}一次搞定",
                    "sub": f"给{s['who']}的轻量工具，免费、不用注册",
                    "cta": "立即试用"},
        "deadline_hours": 8,
        "evidence_urls": s["sample_urls"],
        "econ": s.get("econ", {}),
    }
    d = os.path.join(HERE, "tasks")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"{task['id']}.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(task, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return path


def write_run(db_path, run_id, provider, fetched, extracted, clustered, status, note, t0):
    if not db_path:
        return
    import sqlite3
    d = os.path.dirname(os.path.abspath(db_path))
    if d:
        os.makedirs(d, exist_ok=True)
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA foreign_keys=ON")
    con.executescript(open(os.path.join(HERE, "schema.sql"), encoding="utf-8").read())
    con.execute("""INSERT INTO runs(run_id,started_at,finished_at,provider,fetched,extracted,clustered,status,note)
                   VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(run_id) DO UPDATE SET finished_at=excluded.finished_at,
                     status=excluded.status, note=excluded.note""",
                (run_id, int(t0), int(time.time()), provider, fetched, extracted, clustered, status, note))
    con.commit()
    con.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", default="manual", choices=["manual", "sample", "browser", "oauth"])
    ap.add_argument("--form", default="web-single-tool")
    ap.add_argument("--inbox", default="inbox", help="manual 时的输入目录：inbox=真实输入；sample=内置样本")
    ap.add_argument("--no-jev", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--max-build-per-week", type=int, default=2)
    args = ap.parse_args()

    DB = os.path.join(HERE, "db", "factory.db")
    os.makedirs(os.path.join(HERE, "logs"), exist_ok=True)
    os.makedirs(os.path.join(HERE, "tasks"), exist_ok=True)
    day = dt.date.today().strftime("%Y%m%d")
    run_id = f"run-{day}-{int(time.time()) % 100000}"
    t0 = time.time()
    provider = "sample" if args.inbox == "sample" else args.provider

    radar_args = [os.path.join(HERE, "radar.py"), "--provider", provider,
                  "--out", os.path.join(HERE, "working", "clusters.json"),
                  "--db", DB]
    if provider == "manual":
        radar_args += ["--inbox", os.path.join(HERE, args.inbox)]
    try:
        run(radar_args, args.dry_run)
    except SystemExit as e:
        # 采集不可达/三次全败：写 runs 留痕后干净退出，不硬撑
        write_run(DB, run_id, provider, 0, 0, 0, "FAILED", f"radar 中止：{e}", t0)
        print(f"[run] 中止（已写 runs.status=FAILED，详见 logs/fail.md）：{e}", file=sys.stderr)
        raise SystemExit(2)

    run([os.path.join(HERE, "score.py"),
         "--clusters", os.path.join(HERE, "working", "clusters.json"),
         "--out", os.path.join(HERE, "review", "scored.json"),
         "--db", DB,
         "--form", args.form] + (["--no-jev"] if args.no_jev else []), args.dry_run)

    clusters = json.load(open(os.path.join(HERE, "working", "clusters.json"), encoding="utf-8"))
    scored = json.load(open(os.path.join(HERE, "review", "scored.json"), encoding="utf-8"))

    build = [x for x in scored["items"] if x["decision"] == "BUILD"]
    pilot = [x for x in scored["items"] if x["decision"] == "PILOT"]
    todo = build + pilot                      # BUILD 优先，PILOT 排后
    already = recent_build_tasks(7)
    quota = max(0, args.max_build_per_week - already)
    made = []
    for i, s in enumerate(todo[:quota], 1):
        made.append(make_task(s, already + i, day))
    skipped = len(todo) - len(made)

    try:
        from retention import purge_ttl
        removed = purge_ttl(DB)
        if removed:
            print(f"[retention] TTL 清理 {len(removed)} 帖")
    except Exception as e:                          # noqa: BLE001 清理失败不阻断主流程
        print(f"[warn] retention: {e}", file=sys.stderr)

    note = (f"BUILD {len(build)} / PILOT {len(pilot)} / 放行 {len(made)}"
            f"（近7日已有 {already}，本周上限 {args.max_build_per_week}）"
            f"{'；超出未放行 %d 个' % skipped if skipped else ''}")
    write_run(DB, run_id, provider, clusters.get("posts"), clusters.get("posts"),
              clusters.get("clusters"), "OK", note, t0)

    print(f"[run] {time.strftime('%Y-%m-%d %H:%M')} 用时 {time.time()-t0:.1f}s；{note}")
    for m in made:
        print("  ->", m)


if __name__ == "__main__":
    main()
