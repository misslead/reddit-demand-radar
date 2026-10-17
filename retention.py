#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""保留期与删除权（v1.1 合规最大缺口补齐）

规则（写进代码）：
  - 保留期 TTL_DAYS=90：posts/signals 超期未复现即删除（级联清 signals）
  - 90 天内复现过的帖子刷新 last_seen，不倒计时
  - `--check-urls` 校验溯源 URL：404/410 首次只标记 `gone=1`，**下次运行仍失效才**级联删除
    （单次 404 可能来自限流/网络被阻；403 一律不删）
  - 只删本地摘要，不请求、不回写 Reddit

用法:
    python retention.py --db db/factory.db [--ttl-days 90] [--dry-run] [--check-urls]
"""
import argparse
import json
import os
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

TTL_DAYS = 90
# 只有真正「不存在」才级联删除。403 可能是被限流/网络被阻/需要登录，不能当已删帖，
# 否则一次网络异常就会误删全部本地记录。（v1.1.1 自查修）
GONE_CODES = (404, 410)
URL_CHECK_MAX = 50


def _ensure(db_path):
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA foreign_keys=ON")
    con.executescript(open(os.path.join(HERE, "schema.sql"), encoding="utf-8").read())
    return con


def purge_ttl(db_path, ttl_days=TTL_DAYS, dry_run=False, now=None):
    """删除 last_seen（缺失退回 fetched_at）早于 now-ttl_days 的帖子。"""
    now = int(now or time.time())
    cut = now - ttl_days * 86400
    con = _ensure(db_path)
    try:
        rows = con.execute(
            "SELECT post_id FROM posts WHERE COALESCE(last_seen, fetched_at) < ?", (cut,)
        ).fetchall()
        ids = [r[0] for r in rows]
        if ids and not dry_run:
            q = ",".join("?" * len(ids))
            con.execute(f"DELETE FROM signals WHERE post_id IN ({q})", ids)
            con.execute(f"DELETE FROM posts   WHERE post_id IN ({q})", ids)
            con.commit()
        return ids
    finally:
        con.close()


def confirm_urls(db_path, urls, now=None, dry_run=False):
    """404/410 二次确认（v1.1.2）：本次标记，下次运行仍失效才级联删除。

    单次 404 可能来自限流/网络被阻/临时故障，直接删会永久丢数据。
    返回 {"marked": [首次标记的 post_id], "deleted": [确认删除的 post_id]}
    """
    if not urls:
        return {"marked": [], "deleted": []}
    now = int(now or time.time())
    con = _ensure(db_path)
    try:
        q = ",".join("?" * len(urls))
        rows = con.execute(f"SELECT post_id, gone, gone_at FROM posts WHERE url IN ({q})", urls).fetchall()
        marked = [pid for pid, gone, _at in rows if not gone]
        deleted = [pid for pid, gone, at in rows if gone and at and at < now]
        if not dry_run:
            for pid in marked:
                con.execute("UPDATE posts SET gone=1, gone_at=? WHERE post_id=?", (now, pid))
            if deleted:
                qq = ",".join("?" * len(deleted))
                con.execute(f"DELETE FROM signals WHERE post_id IN ({qq})", deleted)
                con.execute(f"DELETE FROM posts   WHERE post_id IN ({qq})", deleted)
            con.commit()
        return {"marked": marked, "deleted": deleted}
    finally:
        con.close()


def check_urls(db_path, limit=50, dry_run=False):
    """校验溯源 URL 是否仍可达。走限流；网络不通时返回 unknown，不误删。"""
    import urllib.error
    import urllib.request
    from ratelimit import RateLimiter
    rl = RateLimiter(db_path)
    now = int(time.time())
    con = _ensure(db_path)
    try:
        # v1.1.2：被标记 gone=1 的帖也要复检 —— 否则二次确认永远触发不了
        # （上一版只查 gone=0，标记完就再没人管它）
        rows = con.execute("SELECT post_id, url, gone FROM posts WHERE url IS NOT NULL "
                           "AND (gone=0 OR gone_at IS NULL OR gone_at < ?) "
                           "ORDER BY gone DESC, fetched_at ASC LIMIT ?", (now, limit)).fetchall()
    finally:
        con.close()

    gone, revived, checked = [], [], 0
    for pid, url, was_gone in rows:
        if checked >= URL_CHECK_MAX or not rl.quota_ok("url_check"):
            break

        def probe():
            req = urllib.request.Request(url, method="HEAD",
                                         headers={"User-Agent": rl_ua()})
            return urllib.request.urlopen(req, timeout=15).status

        ok, _r, err = rl.with_backoff(probe, sub=None, kind="url_check")
        checked += 1
        if ok:
            if was_gone:                      # 又活了 -> 撤销标记，不再计入删除候选
                revived.append(pid)
            continue
        code = next((c for c in GONE_CODES if str(c) in (err or "")), None)
        if code:
            gone.append(url)
        elif "403" in (err or ""):
            # 403 只告警不删（可能是限流/网络被阻），避免误删
            rl.fail_log("url_check", f"403（不删，待人工确认）：{url}")
    if revived and not dry_run:
        con = _ensure(db_path)
        try:
            q = ",".join("?" * len(revived))
            con.execute(f"UPDATE posts SET gone=0, gone_at=NULL WHERE post_id IN ({q})", revived)
            con.commit()
        finally:
            con.close()
    res = confirm_urls(db_path, gone, now=now, dry_run=dry_run)   # 首次标记，二次确认才删
    return {"checked": checked, "gone": len(gone), "marked": len(res["marked"]),
            "revived": len(revived), "deleted": res["deleted"]}


def rl_ua():
    """真实可联系 User-Agent（合规要求，非占位符）。"""
    return load_compliance().get("user_agent", "niche-radar/1.1")


def load_compliance():
    p = os.path.join(HERE, "config", "compliance.json")
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.join(HERE, "db", "factory.db"))
    ap.add_argument("--ttl-days", type=int, default=TTL_DAYS)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--check-urls", action="store_true")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print(f"[retention] db 不存在：{args.db}")
        return
    ids = purge_ttl(args.db, args.ttl_days, args.dry_run)
    print(f"[retention] TTL {args.ttl_days}d -> {'待删' if args.dry_run else '已删'} {len(ids)} 帖")
    if args.check_urls:
        r = check_urls(args.db, dry_run=args.dry_run)
        print(f"[retention] URL 校验：checked={r['checked']} gone={r['gone']} "
              f"本次标记={r['marked']} 撤销标记={r['revived']} 二次确认后删除={len(r['deleted'])}")


if __name__ == "__main__":
    main()
