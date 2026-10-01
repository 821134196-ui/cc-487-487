"""SQLite 连接与建表。所有数量变动写入 stock_ledger 流水。"""
import sqlite3
import os
import threading
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS stores (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    floors TEXT NOT NULL              -- 逗号分隔楼层，如 "1F,2F,3F"
);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('clerk','cashier','manager'))
    -- clerk=导购  cashier=收银  manager=店长
);

CREATE TABLE IF NOT EXISTS tag_types (
    id INTEGER PRIMARY KEY,
    code TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    note TEXT DEFAULT ''
);

-- 门店×楼层×扣型 库存（窄表），version 做乐观锁
CREATE TABLE IF NOT EXISTS inventory (
    id INTEGER PRIMARY KEY,
    store_id INTEGER NOT NULL,
    floor TEXT NOT NULL,
    tag_type_id INTEGER NOT NULL,
    qty INTEGER NOT NULL DEFAULT 0,
    version INTEGER NOT NULL DEFAULT 1,
    UNIQUE(store_id, floor, tag_type_id)
);

CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY,
    sku TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    required_tag_type_id INTEGER NOT NULL,
    price REAL NOT NULL DEFAULT 0
);

-- 楼层领用单（从店内库存领出到楼层使用点）
CREATE TABLE IF NOT EXISTS requisitions (
    id INTEGER PRIMARY KEY,
    store_id INTEGER NOT NULL,
    floor TEXT NOT NULL,
    tag_type_id INTEGER NOT NULL,
    qty INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK(status IN ('active','short','closed')),
    note TEXT DEFAULT '',
    created_by INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

-- 商品挂扣批次
CREATE TABLE IF NOT EXISTS tag_batches (
    id INTEGER PRIMARY KEY,
    store_id INTEGER NOT NULL,
    floor TEXT NOT NULL,
    product_id INTEGER NOT NULL,
    tag_type_id INTEGER NOT NULL,        -- 实际使用扣型（可与商品要求不一致 -> 不匹配）
    required_qty INTEGER NOT NULL,       -- 应挂数量
    attached_qty INTEGER NOT NULL DEFAULT 0,  -- 实际挂上
    mismatch INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'open'
        CHECK(status IN ('open','closed')),
    created_by INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    closed_at TEXT,
    note TEXT DEFAULT ''
);

-- 模拟 POS 交易
CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY,
    store_id INTEGER NOT NULL,
    floor TEXT NOT NULL,
    register_no TEXT NOT NULL,           -- 收银台号
    product_id INTEGER NOT NULL,
    tag_type_id INTEGER,                 -- 商品成交时应带扣型
    qty INTEGER NOT NULL,
    amount REAL NOT NULL,
    paid_at TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'pos'   -- pos 适配器 / manual
);

-- 收银拆扣回收；同一交易只允许确认一次（UNIQUE 强约束）
CREATE TABLE IF NOT EXISTS detaches (
    id INTEGER PRIMARY KEY,
    txn_id INTEGER UNIQUE,               -- 可为空（无交易来源，如手工回收）
    store_id INTEGER NOT NULL,
    floor TEXT NOT NULL,
    register_no TEXT NOT NULL,
    tag_type_id INTEGER NOT NULL,
    box_id INTEGER,
    qty INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'confirmed'
        CHECK(status IN ('confirmed','duplicate_rejected','box_mismatch')),
    cashier_id INTEGER NOT NULL,
    confirmed_at TEXT NOT NULL,
    alarm_id INTEGER,
    note TEXT DEFAULT ''
);

-- 回收盒
CREATE TABLE IF NOT EXISTS recycle_boxes (
    id INTEGER PRIMARY KEY,
    store_id INTEGER NOT NULL,
    register_no TEXT UNIQUE NOT NULL,    -- 每个收银台绑定一个盒
    qty INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT
);

-- 回收盒串台记录（A 台拆的扣扔进 B 台盒）
CREATE TABLE IF NOT EXISTS box_cross_events (
    id INTEGER PRIMARY KEY,
    detach_id INTEGER,
    store_id INTEGER NOT NULL,
    expected_register TEXT NOT NULL,
    actual_register TEXT NOT NULL,
    tag_type_id INTEGER NOT NULL,
    qty INTEGER NOT NULL,
    resolved INTEGER NOT NULL DEFAULT 0, -- 已调正
    created_at TEXT NOT NULL,
    resolved_at TEXT
);

-- 出口门禁报警
CREATE TABLE IF NOT EXISTS exit_alarms (
    id INTEGER PRIMARY KEY,
    store_id INTEGER NOT NULL,
    gate TEXT NOT NULL,
    txn_id INTEGER,                      -- 关联到的交易；NULL=无交易报警
    tag_type_id INTEGER,
    status TEXT NOT NULL DEFAULT 'open'
        CHECK(status IN ('open','confirmed_miss','false_alarm','no_txn','closed')),
    register_no TEXT,                    -- 复核收银台
    reviewer_id INTEGER,
    evidence_path TEXT,                  -- 本地凭证文件路径
    note TEXT DEFAULT '',
    fired_at TEXT NOT NULL,
    closed_at TEXT
);

-- 消磁器故障
CREATE TABLE IF NOT EXISTS deactivator_faults (
    id INTEGER PRIMARY KEY,
    store_id INTEGER NOT NULL,
    register_no TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('deauth_fail','device_fault')),
    txn_id INTEGER,
    qty INTEGER NOT NULL DEFAULT 0,
    handled INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    handled_at TEXT,
    note TEXT DEFAULT ''
);

-- 扣具报损
CREATE TABLE IF NOT EXISTS tag_damages (
    id INTEGER PRIMARY KEY,
    store_id INTEGER NOT NULL,
    floor TEXT NOT NULL,
    tag_type_id INTEGER NOT NULL,
    qty INTEGER NOT NULL,
    reason TEXT NOT NULL,
    on_shelf INTEGER NOT NULL DEFAULT 0,  -- 1=在架商品破损(计入挂扣平衡) 0=未挂备用扣破损
    created_by INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

-- 闭店清点
CREATE TABLE IF NOT EXISTS closing_counts (
    id INTEGER PRIMARY KEY,
    store_id INTEGER NOT NULL,
    floor TEXT NOT NULL,
    tag_type_id INTEGER NOT NULL,
    business_date TEXT NOT NULL,
    requisition_qty INTEGER NOT NULL DEFAULT 0, -- 领用
    attached_qty INTEGER NOT NULL DEFAULT 0,    -- 挂扣
    detached_qty INTEGER NOT NULL DEFAULT 0,    -- 拆回
    on_shelf_qty INTEGER NOT NULL DEFAULT 0,    -- 在架
    damaged_qty INTEGER NOT NULL DEFAULT 0,     -- 报损
    expected_on_shelf INTEGER NOT NULL DEFAULT 0,
    spare_damaged_qty INTEGER NOT NULL DEFAULT 0, -- 未挂备用扣报损（不计在架平衡）
    floor_left_qty INTEGER NOT NULL DEFAULT 0,    -- 闭店后楼层余存（含拆回待退仓）
    diff INTEGER NOT NULL DEFAULT 0,
    balanced INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    UNIQUE(store_id, floor, tag_type_id, business_date)
);

-- 盘差审批
CREATE TABLE IF NOT EXISTS inventory_adjustments (
    id INTEGER PRIMARY KEY,
    store_id INTEGER NOT NULL,
    floor TEXT NOT NULL,
    tag_type_id INTEGER NOT NULL,
    closing_id INTEGER,
    diff INTEGER NOT NULL,               -- 正=盘盈 负=盘亏
    reason TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending','approved','rejected')),
    requested_by INTEGER NOT NULL,
    reviewed_by INTEGER,
    created_at TEXT NOT NULL,
    reviewed_at TEXT
);

-- 数量流水：所有库存增减的不可变凭证
CREATE TABLE IF NOT EXISTS stock_ledger (
    id INTEGER PRIMARY KEY,
    store_id INTEGER NOT NULL,
    floor TEXT NOT NULL,
    tag_type_id INTEGER NOT NULL,
    change_qty INTEGER NOT NULL,         -- 正入负出
    balance_after INTEGER NOT NULL,
    ref_type TEXT NOT NULL,              -- issue/requisition/attach/detach/damage/adjust/closing
    ref_id INTEGER,
    created_by INTEGER,
    created_at TEXT NOT NULL
);

-- 进程内任务提醒
CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY,
    store_id INTEGER NOT NULL,
    kind TEXT NOT NULL,                   -- closing / diff
    title TEXT NOT NULL,
    body TEXT DEFAULT '',
    read INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ledger ON stock_ledger(store_id, floor, tag_type_id);
"""


_lock = threading.RLock()


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db(db_path: str) -> sqlite3.Connection:
    first = not os.path.exists(db_path)
    conn = connect(db_path)
    with _lock:
        conn.executescript(SCHEMA)
        conn.commit()
    return conn


@contextmanager
def tx(conn):
    """串行化写事务，配合 version 乐观锁防并发覆盖。"""
    with _lock:
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
