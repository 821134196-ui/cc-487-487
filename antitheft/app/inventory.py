# -*- coding: utf-8 -*-
"""库存窄表读写：乐观锁版本校验 + 数量流水（每次增减必留痕）。"""
import uuid
from datetime import datetime

from .errors import VersionConflict

SCOPES = ('STOCK', 'FLOOR', 'ONGOODS', 'BOX', 'DAMAGED', 'ADJUST')
# SQLite 唯一索引把每个 NULL 视为不同值，无维度统一用 0 占位
NONE_REF = 0


def norm_ref(v):
    return NONE_REF if v is None else v


def _ts(ts):
    return ts or datetime.now().isoformat(timespec='seconds')


def new_move_id() -> str:
    return 'M' + uuid.uuid4().hex[:12]


def ensure_row(conn, store_id, tag_type_id, scope, floor_id=None, box_id=None):
    conn.execute(
        """INSERT INTO inventory
           (store_id, tag_type_id, scope, floor_id, box_id, qty, version)
           VALUES (?,?,?,?,?,0,0)
           ON CONFLICT(store_id, tag_type_id, scope, floor_id, box_id) DO NOTHING""",
        (store_id, tag_type_id, scope, norm_ref(floor_id), norm_ref(box_id)),
    )


def balance(conn, store_id, tag_type_id, scope, floor_id=None, box_id=None):
    floor_id, box_id = norm_ref(floor_id), norm_ref(box_id)
    ensure_row(conn, store_id, tag_type_id, scope, floor_id, box_id)
    row = conn.execute(
        """SELECT qty, version FROM inventory
           WHERE store_id=? AND tag_type_id=? AND scope=? AND floor_id=? AND box_id=?""",
        (store_id, tag_type_id, scope, floor_id, box_id),
    ).fetchone()
    return row['qty'], row['version']


def _write_ledger(conn, ts, move_id, store_id, tag_type_id, scope, floor_id, box_id,
                  delta, new_qty, new_version, reason, ref_type, ref_id, operator):
    conn.execute(
        """INSERT INTO inventory_ledger
           (ts, move_id, store_id, tag_type_id, scope, floor_id, box_id,
            delta, balance_after, version, reason, ref_type, ref_id, operator)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (_ts(ts), move_id, store_id, tag_type_id, scope, floor_id, box_id,
         delta, new_qty, new_version, reason, ref_type, ref_id, operator),
    )


def adjust(conn, move_id, store_id, tag_type_id, scope, delta, reason,
           ref_type=None, ref_id=None, operator=None, floor_id=None, box_id=None,
           ts=None):
    """带乐观锁的增减；delta 可正可负，余额不允许为负。"""
    if delta == 0:
        return
    floor_id, box_id = norm_ref(floor_id), norm_ref(box_id)
    ensure_row(conn, store_id, tag_type_id, scope, floor_id, box_id)
    row = conn.execute(
        """SELECT id, qty, version FROM inventory
           WHERE store_id=? AND tag_type_id=? AND scope=? AND floor_id=? AND box_id=?""",
        (store_id, tag_type_id, scope, floor_id, box_id),
    ).fetchone()
    new_qty = row['qty'] + delta
    if new_qty < 0:
        raise VersionConflict(
            f'库存不能为负：{scope} 现有 {row["qty"]}，变动 {delta}')
    cur = conn.execute(
        'UPDATE inventory SET qty=?, version=version+1 WHERE id=? AND version=?',
        (new_qty, row['id'], row['version']),
    )
    if cur.rowcount == 0:
        raise VersionConflict('库存版本已变化，请刷新后重试（乐观锁）')
    _write_ledger(conn, ts, move_id, store_id, tag_type_id, scope, floor_id, box_id,
                  delta, new_qty, row['version'] + 1, reason, ref_type, ref_id, operator)


def cas_adjust(conn, move_id, store_id, tag_type_id, scope, base_qty,
               expected_version, delta, reason, ref_type=None, ref_id=None,
               operator=None, floor_id=None, box_id=None, ts=None):
    """两人并发调整用的乐观锁提交：基于加载时的 base_qty/version 提交。

    版本已被他人改动时抛 VersionConflict，调用方须重新加载后重试。
    """
    if delta == 0:
        raise ValueError('变动数量不能为 0')
    floor_id, box_id = norm_ref(floor_id), norm_ref(box_id)
    ensure_row(conn, store_id, tag_type_id, scope, floor_id, box_id)
    row = conn.execute(
        'SELECT id FROM inventory WHERE store_id=? AND tag_type_id=? AND scope=? AND floor_id=? AND box_id=?',
        (store_id, tag_type_id, scope, floor_id, box_id),
    ).fetchone()
    new_qty = base_qty + delta
    if new_qty < 0:
        raise VersionConflict(f'库存不能为负：基线 {base_qty}，变动 {delta}')
    cur = conn.execute(
        'UPDATE inventory SET qty=?, version=version+1 WHERE id=? AND version=?',
        (new_qty, row['id'], expected_version),
    )
    if cur.rowcount == 0:
        raise VersionConflict('库存版本已被他人修改，请刷新后重试（乐观锁）')
    _write_ledger(conn, ts, move_id, store_id, tag_type_id, scope, floor_id, box_id,
                  delta, new_qty, expected_version + 1, reason, ref_type, ref_id,
                  operator)


def transfer(conn, store_id, tag_type_id, src, dst, qty, reason,
             ref_type=None, ref_id=None, operator=None, ts=None,
             src_floor=None, src_box=None, dst_floor=None, dst_box=None,
             move_id=None):
    """一次借贷转移：两行流水共享 move_id，总量守恒。"""
    if qty <= 0:
        raise ValueError('转移数量必须为正整数')
    mid = move_id or new_move_id()
    adjust(conn, mid, store_id, tag_type_id, src, -qty, reason,
           ref_type, ref_id, operator, src_floor, src_box, ts)
    adjust(conn, mid, store_id, tag_type_id, dst, qty, reason,
           ref_type, ref_id, operator, dst_floor, dst_box, ts)
    return mid
