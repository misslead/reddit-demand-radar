#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""限流 / 退避 / 失败留痕（v1.1 合规落地件）

对外承诺（写进代码，不放文档）：
  - 跨日请求上限 DAILY_MAX=300（计数落 DB 表 api_calls，按本地日切分）
  - 同一 sub 连续两次请求间隔 >= MIN_GAP_S=5s
  - 全局并发 = 1（单进程串行，不开线程池）
  - 失败重试 3 次，退避 60 / 120 / 240 秒
  - 三次全败 -> 停手，写 logs/fail.md

用法:
    rl = RateLimiter(db_path)                 # 联网 provider 必用
    rl.wait_gap(sub)                          # 阻塞到允许发起
    if not rl.quota_ok(): raise SystemExit()
    ok, data, err = rl.with_backoff(lambda: fetch(u), sub=sub, kind="oauth")
    rl.record("oauth", sub, ok)

manual / sample provider 读本地文件，不联网，不需要走本模块（run_daily 只记 run）。
"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
LOGS = os.path.join(HERE, "logs")
FAIL_MD = os.path.join(LOGS, "fail.md")

DAILY_MAX = 300
MIN_GAP_S = 5.0
BACKOFF_WAITS = (60, 120, 240)
SESSION_PAGE_MAX = 50          # 登录态浏览器单会话页数上限（合规）


def _day(ts=None):
    return time.strftime("%Y-%m-%d", time.localtime(ts if ts is not None else time.time()))


class RateLimiter:
    def __init__(self, db_path=None, daily_max=DAILY_MAX, min_gap_s=MIN_GAP_S,
                 sleep_fn=time.sleep, now_fn=time.time, log=True,
                 session_page_max=SESSION_PAGE_MAX, daily_max_by_kind=None):
        self.db_path = db_path
        self.daily_max = daily_max
        self.min_gap_s = min_gap_s
        self._sleep = sleep_fn            # 注入：测试时传 no-op
        self._now = now_fn
        self._last_at = {}                # sub -> ts（进程内间隔控制）
        self.session_page_max = session_page_max
        self._session_requests = 0        # v1.1.2: 计真实请求数（由 with_backoff 自增）
        self.daily_max_by_kind = daily_max_by_kind or {}
        self.log = log

    # ---------- DB ----------
    def _con(self):
        if not self.db_path:
            return None
        import sqlite3
        d = os.path.dirname(os.path.abspath(self.db_path))
        if d:
            os.makedirs(d, exist_ok=True)
        con = sqlite3.connect(self.db_path)
        con.executescript(open(os.path.join(HERE, "schema.sql"), encoding="utf-8").read())
        return con

    def record(self, kind, sub=None, ok=True, note=None):
        con = self._con()
        if not con:
            return
        try:
            con.execute("INSERT INTO api_calls(ts,day,kind,sub,ok,note) VALUES (?,?,?,?,?,?)",
                        (int(self._now()), _day(self._now()), kind, sub, 1 if ok else 0, note))
            con.commit()
        finally:
            con.close()

    def used_today(self, kind=None):
        con = self._con()
        if not con:
            return 0
        day = _day(self._now())
        try:
            if kind:
                cur = con.execute("SELECT COUNT(*) FROM api_calls WHERE day=? AND kind=?", (day, kind))
            else:
                cur = con.execute("SELECT COUNT(*) FROM api_calls WHERE day=?", (day,))
            return cur.fetchone()[0]
        finally:
            con.close()

    # ---------- 配额 / 间隔 ----------
    def quota_ok(self, kind="net"):
        """总配额 + 分类配额双查（v1.1.2：kind 参数不再被忽略）。"""
        if self.used_today() >= self.daily_max:
            return False
        cap = self.daily_max_by_kind.get(kind)
        return not (cap and self.used_today(kind) >= cap)

    def wait_gap(self, sub=None):
        """阻塞到满足同 sub 最小间隔；无论是否等待都要刷新时间戳（否则间隔永不生效）。"""
        if sub is None:
            return
        last = self._last_at.get(sub)
        if last is not None:
            wait = self.min_gap_s - (self._now() - last)
            if wait > 0:
                self._sleep(wait)
        self._last_at[sub] = self._now()

    def tick(self, sub=None, kind="net", note=None):
        """发起前调用：只查配额 + 计页数，不等待、不计数。

        计数与间隔统一由 with_backoff 负责 —— 否则 tick 与 with_backoff 各记一次，
        配额会被虚高 2 倍、间隔会被重复等待。（v1.1.1 自查修）
        返回 False = 当日配额用尽。
        """
        if not self.quota_ok(kind):
            self.fail_log("quota", f"当日请求数已达上限 {self.daily_max}，停手（kind={kind}）")
            return False
        return True

    def page_limit_ok(self):
        """登录态浏览器单会话请求数上限（v1.1.2：改为计真实请求，不再计"页"导致回退分支被漏算）。"""
        return self._session_requests < self.session_page_max

    @property
    def session_requests(self):
        return self._session_requests

    # ---------- 退避 ----------
    def with_backoff(self, fn, tries=3, waits=BACKOFF_WAITS, sub=None, kind="net"):
        """返回 (ok, result, err)。三次全败不抛异常，由调用方决定停手。"""
        err = None
        for i in range(tries):
            if not self.quota_ok(kind):
                return False, None, "quota_exhausted"
            if not self.page_limit_ok():
                self.fail_log("page_limit", f"单会话请求数达上限 {self.session_page_max}，停手（kind={kind}）")
                return False, None, "page_limit"
            self.wait_gap(sub)
            self._session_requests += 1
            try:
                r = fn()
                self.record(kind, sub, ok=True)
                return True, r, None
            except Exception as e:                  # noqa: BLE001 网络层任何异常都退避
                err = f"{type(e).__name__}: {e}"
                self.record(kind, sub, ok=False, note=err[:200])
                if i < tries - 1:
                    self._sleep(waits[min(i, len(waits) - 1)])
        self.fail_log(kind, f"{tries} 次重试全败：{err}")
        return False, None, err

    # ---------- 失败留痕 ----------
    def fail_log(self, kind, msg):
        if not self.log:
            return
        os.makedirs(LOGS, exist_ok=True)
        new = not os.path.exists(FAIL_MD)
        with open(FAIL_MD, "a", encoding="utf-8") as f:
            if new:
                f.write("# 失败记录（三次重试全败 / 配额用尽）\n\n| 时间 | 类型 | 说明 |\n|---|---|---|\n")
            f.write(f"| {time.strftime('%Y-%m-%d %H:%M:%S')} | {kind} | {msg} |\n")
        print(f"[ratelimit][fail] {kind}: {msg}", file=sys.stderr)


def self_test():
    """自检：所有对外方法都真能被调用（v1.1.2 加 —— 上轮 tick() 里引用了已删除的属性，
    只有跑一次 browser 路径才会暴露）。"""
    slept, t = [], [1000.0]
    db = os.path.join(HERE, "db", "_selftest.db")
    for f in (db, db + "-wal", db + "-shm"):        # 开局先清，避免上次中断残留
        if os.path.exists(f):
            os.remove(f)
    rl = RateLimiter(db, daily_max=10, min_gap_s=5,
                     sleep_fn=lambda s: (slept.append(round(s, 1)), t.__setitem__(0, t[0] + s)),
                     now_fn=lambda: t[0], log=False)
    assert rl.tick("accounting", kind="browser") is True          # 曾在此抛 AttributeError
    assert rl.page_limit_ok() is True
    assert rl.quota_ok("browser") is True

    # 同 sub 间隔：连续两次 wait_gap，第二次必须等 5s（曾因首次不记时间戳而永不生效）
    rl.wait_gap("photography")
    rl.wait_gap("photography")
    assert 5.0 in slept, slept

    def boom():
        raise RuntimeError("simulated net failure")

    ok, _r, err = rl.with_backoff(boom, sub="accounting", kind="browser")
    assert ok is False and err == "RuntimeError: simulated net failure", err
    assert rl.session_requests == 3, rl.session_requests            # 3 次尝试都计进会话请求数
    assert rl.used_today("browser") == 3, rl.used_today("browser")
    assert [s for s in slept if s >= 60] == [60, 120], slept        # 退避 60/120
    print(f"[ratelimit self-test] OK  used={rl.used_today()} browser={rl.used_today('browser')} "
          f"session_requests={rl.session_requests} backoff={[s for s in slept if s >= 60]}")
    for f in (db, db + "-wal", db + "-shm"):
        if os.path.exists(f):
            os.remove(f)


if __name__ == "__main__":
    rl = RateLimiter()
    print(f"local day={_day()} used={rl.used_today()} quota_ok={rl.quota_ok()} "
          f"daily_max={DAILY_MAX} min_gap_s={MIN_GAP_S}")
    self_test()
