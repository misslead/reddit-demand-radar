-- Reddit 需求工厂 v1.1
-- 合规：不存正文全文、不存用户名、不存邮箱。只存需求摘要与溯源链接。
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;            -- v1.1: 之前有 FK 声明但未开启

CREATE TABLE IF NOT EXISTS posts (
  post_id     TEXT PRIMARY KEY,
  sub         TEXT NOT NULL DEFAULT '',
  created_utc INTEGER NOT NULL DEFAULT 0,
  title       TEXT NOT NULL DEFAULT '',
  url         TEXT,
  pain_hits   TEXT,                -- JSON array
  pay_hits    TEXT,                -- JSON array
  fetched_at  INTEGER NOT NULL,
  last_seen   INTEGER,             -- v1.1: 重复出现时更新（判 WATCH 二次出现）
  seen_count  INTEGER DEFAULT 1,   -- v1.1
  gone        INTEGER DEFAULT 0,   -- v1.1.2: 404/410「疑似失效」标记（需二次确认才删）
  gone_at     INTEGER              -- v1.1.2: 首次标记时间；早于本次运行 = 已确认可删
);

CREATE TABLE IF NOT EXISTS signals (
  post_id          TEXT PRIMARY KEY REFERENCES posts(post_id) ON DELETE CASCADE,
  who              TEXT,
  problem          TEXT,
  pain_why         TEXT,
  workaround       TEXT,
  frequency        TEXT,           -- daily|weekly|monthly|once|unknown
  tools_used       TEXT,           -- JSON array
  gap              TEXT,
  pay_signal       TEXT,
  workaround_score INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS clusters (
  cluster_id       TEXT PRIMARY KEY,
  title            TEXT,
  posts            INTEGER DEFAULT 0,
  unique_authors   INTEGER DEFAULT 0,
  workaround_kinds TEXT,           -- JSON array
  frequency        TEXT,
  subs             TEXT,           -- JSON array
  pay_posts        INTEGER DEFAULT 0,
  sample_urls      TEXT,           -- JSON array
  first_seen       INTEGER,
  last_seen        INTEGER,
  appearances      INTEGER DEFAULT 1  -- v1.1: 出现过几个批次
);

CREATE TABLE IF NOT EXISTS scores (
  cluster_id  TEXT PRIMARY KEY REFERENCES clusters(cluster_id) ON DELETE CASCADE,
  O           REAL, M REAL, R REAL, final REAL,
  penalty     REAL DEFAULT 0,
  decision    TEXT,                -- BUILD|WATCH|DROP|NEEDS_HUMAN
  dims        TEXT,                -- JSON object 各维度明细
  jev_raw     TEXT,                -- JSON object 原始响应 / jev_unavailable
  needs_human INTEGER DEFAULT 0,
  scored_at   INTEGER,
  econ_hash   TEXT,                -- v1.1: econ 参数快照指纹，用于复现
  threshold_json TEXT              -- v1.1: 本次使用的阈值来源（分位/保底）
);

CREATE TABLE IF NOT EXISTS runs (
  run_id     TEXT PRIMARY KEY,
  started_at INTEGER, finished_at INTEGER,
  provider   TEXT, fetched INTEGER, extracted INTEGER, clustered INTEGER,
  status     TEXT, note TEXT
);

-- v1.1: 限流与合规计数落库（跨日配额）
CREATE TABLE IF NOT EXISTS api_calls (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  ts        INTEGER NOT NULL,
  day       TEXT NOT NULL,          -- YYYY-MM-DD，本地日
  kind      TEXT NOT NULL,          -- oauth|browser|read|url_check
  sub       TEXT,
  ok        INTEGER DEFAULT 1,
  note      TEXT
);

-- v1.1.2: JEV 回答缓存。同一簇指纹复用上次判定 -> 同输入同输出（消掉 JEV 抽样噪声）+ 省 token
CREATE TABLE IF NOT EXISTS jev_cache (
  cache_key  TEXT PRIMARY KEY,
  cluster_id TEXT,
  answers    TEXT NOT NULL,
  model      TEXT,
  created_at INTEGER
);

CREATE INDEX IF NOT EXISTS idx_posts_sub ON posts(sub);
CREATE INDEX IF NOT EXISTS idx_posts_time ON posts(created_utc);
CREATE INDEX IF NOT EXISTS idx_api_day ON api_calls(day, kind);
