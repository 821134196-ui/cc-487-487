# -*- coding: utf-8 -*-
"""SQLite 数据层：库存窄表 + 数量流水 + 全部业务表。"""
import sqlite3
from pathlib import Path

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS stores (
  id INTEGER PRIMARY KEY,
  code TEXT UNIQUE NOT NULL,
  name TEXT NOT NULL,
  city TEXT
);

CREATE TABLE IF NOT EXISTS floors (
  id INTEGER PRIMARY KEY,
  store_id INTEGER NOT NULL REFERENCES stores(id),
  code TEXT NOT NULL,
  name TEXT NOT NULL,
  seq INTEGER DEFAULT 0,
  UNIQUE(store_id, code)
);

CREATE TABLE IF NOT EXISTS tag_types (
  id INTEGER PRIMARY KEY,
  code TEXT UNIQUE NOT NULL,
  name TEXT NOT NULL,
  spec TEXT
);

-- 导购 ASSOCIATE 可领用/挂扣/报损；收银 CASHIER 可拆扣/复核报警；店长 MANAGER 批盘差
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY,
  username TEXT UNIQUE NOT NULL,
  display_name TEXT NOT NULL,
  role TEXT NOT NULL CHECK(role IN ('ASSOCIATE','CASHIER','MANAGER'))
);

CREATE TABLE IF NOT EXISTS recovery_boxes (
  id INTEGER PRIMARY KEY,
  store_id INTEGER NOT NULL REFERENCES stores(id),
  code TEXT UNIQUE NOT NULL,
  register_id INTEGER,
  status TEXT NOT NULL DEFAULT 'ACTIVE'  -- ACTIVE / SEALED
);

CREATE TABLE IF NOT EXISTS deactivators (
  id INTEGER PRIMARY KEY,
  store_id INTEGER NOT NULL REFERENCES stores(id),
  code TEXT UNIQUE NOT NULL,
  register_id INTEGER,
  status TEXT NOT NULL DEFAULT 'OK'      -- OK / FAULT
);

CREATE TABLE IF NOT EXISTS registers (
  id INTEGER PRIMARY KEY,
  store_id INTEGER NOT NULL REFERENCES stores(id),
  code TEXT NOT NULL,
  name TEXT NOT NULL,
  floor_id INTEGER REFERENCES floors(id),
  box_id INTEGER REFERENCES recovery_boxes(id),
  deactivator_id INTEGER REFERENCES deactivators(id),
  UNIQUE(store_id, code)
);

CREATE TABLE IF NOT EXISTS products (
  id INTEGER PRIMARY KEY,
  store_id INTEGER NOT NULL REFERENCES stores(id),
  sku TEXT UNIQUE NOT NULL,
  name TEXT NOT NULL,
  tag_type_id INTEGER NOT NULL REFERENCES tag_types(id),
  price REAL NOT NULL
);

-- 库存窄表：门店 × 扣型 × 位置维度（总仓 STOCK / 楼层散扣 FLOOR / 在架商品 ONGOODS / 回收盒 BOX / 报损 DAMAGED / 盘差调整 ADJUST）
-- floor_id/box_id 为 0 表示无该维度（SQLite 唯一索引视 NULL 互不相同，故用 0 占位）
CREATE TABLE IF NOT EXISTS inventory (
  id INTEGER PRIMARY KEY,
  store_id INTEGER NOT NULL REFERENCES stores(id),
  tag_type_id INTEGER NOT NULL REFERENCES tag_types(id),
  scope TEXT NOT NULL CHECK(scope IN ('STOCK','FLOOR','ONGOODS','BOX','DAMAGED','ADJUST')),
  floor_id INTEGER NOT NULL DEFAULT 0,
  box_id INTEGER NOT NULL DEFAULT 0,
  qty INTEGER NOT NULL DEFAULT 0,
  version INTEGER NOT NULL DEFAULT 0,
  UNIQUE(store_id, tag_type_id, scope, floor_id, box_id)
);

-- 数量流水：每次增减一行，带移动单号 move_id（借贷两行共享）、余额与版本
CREATE TABLE IF NOT EXISTS inventory_ledger (
  id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL,
  move_id TEXT NOT NULL,
  store_id INTEGER NOT NULL,
  tag_type_id INTEGER NOT NULL,
  scope TEXT NOT NULL,
  floor_id INTEGER NOT NULL DEFAULT 0,
  box_id INTEGER NOT NULL DEFAULT 0,
  delta INTEGER NOT NULL,
  balance_after INTEGER NOT NULL,
  version INTEGER NOT NULL,
  reason TEXT NOT NULL,
  ref_type TEXT,
  ref_id TEXT,
  operator TEXT
);
CREATE INDEX IF NOT EXISTS ix_ledger_date ON inventory_ledger(ts);
CREATE INDEX IF NOT EXISTS ix_ledger_ref ON inventory_ledger(ref_type, ref_id);

-- 楼层领用
CREATE TABLE IF NOT EXISTS tag_issues (
  id INTEGER PRIMARY KEY,
  code TEXT UNIQUE NOT NULL,
  store_id INTEGER NOT NULL,
  floor_id INTEGER NOT NULL,
  tag_type_id INTEGER NOT NULL,
  qty_requested INTEGER NOT NULL,
  qty_issued INTEGER NOT NULL,
  status TEXT NOT NULL,             -- OK / SHORT
  business_date TEXT NOT NULL,
  ts TEXT NOT NULL,
  operator TEXT NOT NULL,
  note TEXT
);

-- 商品挂扣批次：应挂数量 vs 实挂数量
CREATE TABLE IF NOT EXISTS tag_batches (
  id INTEGER PRIMARY KEY,
  code TEXT UNIQUE NOT NULL,
  store_id INTEGER NOT NULL,
  floor_id INTEGER NOT NULL,
  issue_id INTEGER REFERENCES tag_issues(id),
  product_id INTEGER NOT NULL REFERENCES products(id),
  tag_type_id INTEGER NOT NULL,
  expected_qty INTEGER NOT NULL,
  attached_qty INTEGER NOT NULL,
  status TEXT NOT NULL,             -- OK / SHORT / MISMATCH
  business_date TEXT NOT NULL,
  ts TEXT NOT NULL,
  operator TEXT NOT NULL,
  note TEXT
);

-- 收银交易
CREATE TABLE IF NOT EXISTS txns (
  id INTEGER PRIMARY KEY,
  code TEXT UNIQUE NOT NULL,
  store_id INTEGER NOT NULL,
  register_id INTEGER NOT NULL,
  floor_id INTEGER NOT NULL,
  cashier_id INTEGER NOT NULL,
  ts TEXT NOT NULL,
  business_date TEXT NOT NULL,
  amount REAL NOT NULL,
  status TEXT NOT NULL DEFAULT 'PAID'
);
CREATE TABLE IF NOT EXISTS txn_items (
  id INTEGER PRIMARY KEY,
  txn_id INTEGER NOT NULL REFERENCES txns(id),
  product_id INTEGER NOT NULL REFERENCES products(id),
  tag_type_id INTEGER NOT NULL,
  qty INTEGER NOT NULL
);

-- 拆扣回收事件（POS 收银拆扣 / ALARM 漏拆回收），一个事件含多扣型明细
CREATE TABLE IF NOT EXISTS detach_events (
  id INTEGER PRIMARY KEY,
  code TEXT UNIQUE NOT NULL,
  source TEXT NOT NULL CHECK(source IN ('POS','ALARM')),
  txn_id INTEGER REFERENCES txns(id),
  alarm_id INTEGER UNIQUE REFERENCES alarms(id),
  store_id INTEGER NOT NULL,
  register_id INTEGER NOT NULL,
  floor_id INTEGER NOT NULL,
  box_id INTEGER NOT NULL,
  cashier_id INTEGER NOT NULL,
  mode TEXT NOT NULL DEFAULT 'AUTO',  -- AUTO 消磁 / MANUAL 故障或漏拆手工拆
  total_qty INTEGER NOT NULL,
  ts TEXT NOT NULL,
  business_date TEXT NOT NULL,
  note TEXT
);
CREATE TABLE IF NOT EXISTS detach_event_items (
  id INTEGER PRIMARY KEY,
  detach_id INTEGER NOT NULL REFERENCES detach_events(id),
  tag_type_id INTEGER NOT NULL,
  qty INTEGER NOT NULL
);
-- 同一交易的 POS 拆扣事件只允许确认一次（ALARM 漏拆回收不在此列）
CREATE UNIQUE INDEX IF NOT EXISTS ux_detach_pos_txn
  ON detach_events(txn_id) WHERE source='POS';

-- 出口报警
CREATE TABLE IF NOT EXISTS alarms (
  id INTEGER PRIMARY KEY,
  code TEXT UNIQUE NOT NULL,
  store_id INTEGER NOT NULL,
  gate TEXT NOT NULL,
  ts TEXT NOT NULL,
  business_date TEXT NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('MATCHED_TXN','NO_TXN')),
  txn_id INTEGER REFERENCES txns(id),
  tag_type_id INTEGER,
  status TEXT NOT NULL,              -- OPEN / CONFIRMED_MISSED / FALSE_ALARM / NO_TXN_ESCALATED
  detach_id INTEGER,
  evidence_path TEXT,
  resolver_id INTEGER,
  resolved_ts TEXT,
  resolution_note TEXT
);

-- 消磁器故障
CREATE TABLE IF NOT EXISTS deactivator_faults (
  id INTEGER PRIMARY KEY,
  code TEXT UNIQUE NOT NULL,
  store_id INTEGER NOT NULL,
  deactivator_id INTEGER NOT NULL,
  register_id INTEGER NOT NULL,
  ts TEXT NOT NULL,
  business_date TEXT NOT NULL,
  fault_kind TEXT NOT NULL,          -- NO_POWER / COIL_FAIL / SENSOR_DRIFT
  status TEXT NOT NULL DEFAULT 'OPEN',  -- OPEN / RESOLVED
  detach_id INTEGER,
  handler_id INTEGER,
  resolved_ts TEXT,
  note TEXT
);

-- 扣具报损
CREATE TABLE IF NOT EXISTS tag_damages (
  id INTEGER PRIMARY KEY,
  code TEXT UNIQUE NOT NULL,
  store_id INTEGER NOT NULL,
  floor_id INTEGER NOT NULL,
  tag_type_id INTEGER NOT NULL,
  scope_from TEXT NOT NULL,          -- FLOOR / ONGOODS
  qty INTEGER NOT NULL,
  reason TEXT NOT NULL,
  business_date TEXT NOT NULL,
  ts TEXT NOT NULL,
  operator TEXT NOT NULL
);

-- 闭店清点（实物数由门店录入，缺省取账面）
CREATE TABLE IF NOT EXISTS count_inputs (
  id INTEGER PRIMARY KEY,
  store_id INTEGER NOT NULL,
  business_date TEXT NOT NULL,
  floor_id INTEGER NOT NULL,
  tag_type_id INTEGER NOT NULL,
  loose_actual INTEGER,
  on_shelf_actual INTEGER,
  UNIQUE(store_id, business_date, floor_id, tag_type_id)
);

CREATE TABLE IF NOT EXISTS close_counts (
  id INTEGER PRIMARY KEY,
  code TEXT UNIQUE NOT NULL,
  store_id INTEGER NOT NULL,
  business_date TEXT NOT NULL,
  floor_id INTEGER NOT NULL,
  tag_type_id INTEGER NOT NULL,
  issued_qty INTEGER NOT NULL,
  attached_qty INTEGER NOT NULL,
  detached_qty INTEGER NOT NULL,
  loose_book INTEGER NOT NULL,
  loose_actual INTEGER NOT NULL,
  on_shelf_book INTEGER NOT NULL,
  on_shelf_actual INTEGER NOT NULL,
  damaged_floor_qty INTEGER NOT NULL,
  damaged_ongoods_qty INTEGER NOT NULL,
  writeoff_floor_qty INTEGER NOT NULL DEFAULT 0,
  writeoff_ongoods_qty INTEGER NOT NULL DEFAULT 0,
  chain_ok INTEGER NOT NULL,
  variance_qty INTEGER NOT NULL,     -- 实物-账面；负为盘亏
  status TEXT NOT NULL,              -- BALANCED / PENDING_VARIANCE
  created_ts TEXT NOT NULL,
  UNIQUE(store_id, business_date, floor_id, tag_type_id)
);

-- 盘差审批
CREATE TABLE IF NOT EXISTS variance_requests (
  id INTEGER PRIMARY KEY,
  code TEXT UNIQUE NOT NULL,
  store_id INTEGER NOT NULL,
  close_count_id INTEGER NOT NULL REFERENCES close_counts(id),
  floor_id INTEGER NOT NULL,
  tag_type_id INTEGER NOT NULL,
  scope TEXT NOT NULL,               -- FLOOR / ONGOODS
  qty INTEGER NOT NULL,
  direction TEXT NOT NULL,           -- SHORT 盘亏 / SURPLUS 盘盈
  reason TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'PENDING', -- PENDING / APPROVED / REJECTED
  applicant_id INTEGER NOT NULL,
  approver_id INTEGER,
  ts TEXT NOT NULL,
  decided_ts TEXT,
  decision_note TEXT
);

-- 异常事件（扣型不匹配/领用短缺/回收盒串台/重复拆扣等）
CREATE TABLE IF NOT EXISTS incidents (
  id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL,
  store_id INTEGER NOT NULL,
  kind TEXT NOT NULL,
  severity TEXT NOT NULL DEFAULT 'WARN',
  message TEXT NOT NULL,
  ref_type TEXT,
  ref_id TEXT,
  resolved INTEGER NOT NULL DEFAULT 0
);

-- 进程内任务运行记录（闭店清点 / 差异提醒）
CREATE TABLE IF NOT EXISTS task_runs (
  id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL,
  task_type TEXT NOT NULL,           -- CLOSE_COUNT / VARIANCE_REMIND
  status TEXT NOT NULL,              -- DONE / REMIND
  message TEXT NOT NULL,
  payload TEXT
);
"""


def connect(db_path: str) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


def init_db(db_path: str) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()
