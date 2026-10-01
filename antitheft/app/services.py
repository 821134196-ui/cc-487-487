# -*- coding: utf-8 -*-
"""领域服务：领用 / 挂扣 / 拆扣回收 / 出口报警 / 消磁故障 / 报损 / 闭店平衡 / 盘差审批。

所有数量变动都走 inventory.transfer/adjust，保证流水留痕与总量守恒。
角色：ASSOCIATE 导购可领用挂扣；CASHIER 收银拆扣与报警复核；MANAGER 店长只批盘差。
"""
import uuid
from datetime import datetime

from .errors import (DuplicateEvent, NotFound, PermissionDenied,
                     ServiceError, VersionConflict)
from . import inventory as inv

ROLE_ISSUE = {'ASSOCIATE'}
ROLE_DETACH = {'CASHIER'}
ROLE_APPROVE = {'MANAGER'}
ROLE_APPLY_VARIANCE = {'ASSOCIATE', 'CASHIER'}


def now_ts():
    return datetime.now().isoformat(timespec='seconds')


def _code(prefix):
    return f'{prefix}-{uuid.uuid4().hex[:10].upper()}'


def _bdate(ts):
    return ts[:10]


def _user(conn, user_id):
    row = conn.execute('SELECT * FROM users WHERE id=?', (user_id,)).fetchone()
    if not row:
        raise NotFound('用户不存在')
    return row


def require_role(conn, user_id, allowed):
    u = _user(conn, user_id)
    if u['role'] not in allowed:
        raise PermissionDenied(f'{u["display_name"]}（{u["role"]}）无权执行该操作')
    return u


def _incident(conn, store_id, kind, message, ref_type=None, ref_id=None,
              severity='WARN', ts=None):
    conn.execute(
        """INSERT INTO incidents (ts, store_id, kind, severity, message, ref_type, ref_id)
           VALUES (?,?,?,?,?,?,?)""",
        (ts or now_ts(), store_id, kind, severity, message, ref_type, str(ref_id) if ref_id is not None else None),
    )


def _get(conn, table, row_id):
    row = conn.execute(f'SELECT * FROM {table} WHERE id=?', (row_id,)).fetchone()
    if not row:
        raise NotFound(f'{table}#{row_id} 不存在')
    return row


# ---------------------------------------------------------------- 楼层领用
def issue_tags(conn, store_id, floor_id, tag_type_id, qty_requested, user_id, ts=None):
    """导购按到货批次从总仓领扣到楼层；库存不足时按实发登记并记短缺异常。"""
    ts = ts or now_ts()
    u = require_role(conn, user_id, ROLE_ISSUE)
    if qty_requested <= 0:
        raise ServiceError('领用数量必须为正整数')
    stock_qty, _ = inv.balance(conn, store_id, tag_type_id, 'STOCK')
    qty_issued = min(stock_qty, qty_requested)
    status = 'OK'
    note = ''
    if qty_issued < qty_requested:
        status = 'SHORT'
        note = f'申请 {qty_requested}，总仓仅 {stock_qty}，实发 {qty_issued}'
        _incident(conn, store_id, 'ISSUE_SHORT',
                  f'领用短缺：{note}', 'TAG_TYPE', tag_type_id, ts=ts)
    if qty_issued == 0:
        raise ServiceError(f'总仓该扣型无库存，无法领用（申请 {qty_requested}）')
    code = _code('ISS')
    mid = inv.transfer(
        conn, store_id, tag_type_id, 'STOCK', 'FLOOR', qty_issued,
        '楼层领用', 'ISSUE', code, u['username'], ts, dst_floor=floor_id)
    cur = conn.execute(
        """INSERT INTO tag_issues
           (code, store_id, floor_id, tag_type_id, qty_requested, qty_issued,
            status, business_date, ts, operator, note)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (code, store_id, floor_id, tag_type_id, qty_requested, qty_issued,
         status, _bdate(ts), ts, u['username'], note),
    )
    return _get(conn, 'tag_issues', cur.lastrowid), mid


# ---------------------------------------------------------------- 挂扣批次
def attach_batch(conn, store_id, floor_id, product_id, tag_type_id, expected_qty,
                 attached_qty, user_id, issue_id=None, ts=None):
    """登记到货商品应挂/实挂；处理扣型不匹配与挂扣短缺。"""
    ts = ts or now_ts()
    u = require_role(conn, user_id, ROLE_ISSUE)
    product = _get(conn, 'products', product_id)
    code = _code('BAT')
    bdate = _bdate(ts)

    # 扣型不匹配：商品指定扣型与领用扣型不一致，不动库存，只登记异常批次
    if product['tag_type_id'] != tag_type_id:
        need = _get(conn, 'tag_types', product['tag_type_id'])
        got = _get(conn, 'tag_types', tag_type_id)
        note = f'商品 {product["sku"]} 应使用 {need["name"]}，实领 {got["name"]}'
        _incident(conn, store_id, 'TAG_MISMATCH', f'扣型不匹配：{note}',
                  'BATCH', code, 'ERROR', ts)
        cur = conn.execute(
            """INSERT INTO tag_batches
               (code, store_id, floor_id, issue_id, product_id, tag_type_id,
                expected_qty, attached_qty, status, business_date, ts, operator, note)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (code, store_id, floor_id, issue_id, product_id, tag_type_id,
             expected_qty, 0, 'MISMATCH', bdate, ts, u['username'], note),
        )
        return _get(conn, 'tag_batches', cur.lastrowid), None

    if attached_qty < 0 or expected_qty <= 0:
        raise ServiceError('数量不合法')
    loose_qty, _ = inv.balance(conn, store_id, tag_type_id, 'FLOOR', floor_id)
    if attached_qty > loose_qty:
        _incident(conn, store_id, 'ATTACH_SHORT',
                  f'挂扣短缺：需挂 {attached_qty}，楼层散扣仅 {loose_qty}',
                  'BATCH', code, 'ERROR', ts)
        raise ServiceError(f'楼层散扣不足：需 {attached_qty}，余 {loose_qty}')

    status = 'OK' if attached_qty == expected_qty else 'SHORT'
    note = ''
    if attached_qty < expected_qty:
        note = f'应挂 {expected_qty}，实挂 {attached_qty}，差 {expected_qty - attached_qty}'
        _incident(conn, store_id, 'ATTACH_SHORT', f'挂扣未足：{note}',
                  'BATCH', code, ts=ts)

    mid = None
    if attached_qty > 0:
        mid = inv.transfer(
            conn, store_id, tag_type_id, 'FLOOR', 'ONGOODS', attached_qty,
            '商品挂扣', 'BATCH', code, u['username'], ts,
            src_floor=floor_id, dst_floor=floor_id)
    cur = conn.execute(
        """INSERT INTO tag_batches
           (code, store_id, floor_id, issue_id, product_id, tag_type_id,
            expected_qty, attached_qty, status, business_date, ts, operator, note)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (code, store_id, floor_id, issue_id, product_id, tag_type_id,
         expected_qty, attached_qty, status, bdate, ts, u['username'], note),
    )
    return _get(conn, 'tag_batches', cur.lastrowid), mid


# ---------------------------------------------------------------- 收银拆扣
def _register_view(conn, register_id):
    reg = _get(conn, 'registers', register_id)
    box = _get(conn, 'recovery_boxes', reg['box_id']) if reg['box_id'] else None
    deact = _get(conn, 'deactivators', reg['deactivator_id']) if reg['deactivator_id'] else None
    return reg, box, deact


def pos_sale_detach(conn, store_id, register_id, user_id, items, box_id, ts=None,
                    manual=None):
    """收银成交并登记拆扣回收。

    items: [(product_id, sold_qty, detached_qty?)]，detached 缺省等于 sold；
    漏拆样例可令 detached < sold。同一交易只生成一条 POS 拆扣事件。
    """
    ts = ts or now_ts()
    u = require_role(conn, user_id, ROLE_DETACH)
    reg, correct_box, deact = _register_view(conn, register_id)
    if not correct_box:
        raise ServiceError('该收银台未绑定回收盒')

    # 回收盒串台：投入了别的收银台的盒子
    if box_id != correct_box['id']:
        wrong = _get(conn, 'recovery_boxes', box_id)
        msg = f'回收盒串台：{reg["code"]} 应投 {correct_box["code"]}，扫描到 {wrong["code"]}'
        _incident(conn, store_id, 'CROSS_BOX', msg, 'REGISTER', reg['code'], 'ERROR', ts)
        raise ServiceError(msg)
    if correct_box['status'] != 'ACTIVE':
        raise ServiceError(f'回收盒 {correct_box["code"]} 已封盒，无法投入')

    mode = 'AUTO'
    if (manual is None and deact and deact['status'] == 'FAULT') or manual:
        mode = 'MANUAL'

    # 汇总交易行（金额、按扣型售出/拆回）
    amount = 0.0
    sold_by_type = {}
    detach_by_type = {}
    norm_items = []
    for product_id, sold_qty, *rest in items:
        if sold_qty <= 0:
            raise ServiceError('销售数量必须为正整数')
        detached = rest[0] if rest else sold_qty
        if detached < 0 or detached > sold_qty:
            raise ServiceError('拆扣数量必须在 0 与销售数量之间')
        p = _get(conn, 'products', product_id)
        amount += p['price'] * sold_qty
        sold_by_type[p['tag_type_id']] = sold_by_type.get(p['tag_type_id'], 0) + sold_qty
        detach_by_type[p['tag_type_id']] = detach_by_type.get(p['tag_type_id'], 0) + detached
        norm_items.append((product_id, p['tag_type_id'], sold_qty))

    bdate = _bdate(ts)
    txn_code = _code('TXN')
    cur = conn.execute(
        """INSERT INTO txns (code, store_id, register_id, floor_id, cashier_id,
                             ts, business_date, amount, status)
           VALUES (?,?,?,?,?,?,?,?, 'PAID')""",
        (txn_code, store_id, register_id, reg['floor_id'], user_id, ts, bdate, amount),
    )
    txn_id = cur.lastrowid
    conn.executemany(
        'INSERT INTO txn_items (txn_id, product_id, tag_type_id, qty) VALUES (?,?,?,?)',
        [(txn_id, pid, ttid, q) for pid, ttid, q in norm_items],
    )

    detach_code = _code('DTC')
    total_detached = 0
    event_items = []
    detach_pk = None
    for ttid, dq in detach_by_type.items():
        if dq <= 0:
            continue
        ongoods, _ = inv.balance(conn, store_id, ttid, 'ONGOODS', reg['floor_id'])
        if dq > ongoods:
            _incident(conn, store_id, 'DETACH_OVER',
                      f'拆扣数超过在架量：{ttid} 拆 {dq}，在架 {ongoods}',
                      'TXN', txn_code, 'ERROR', ts)
            raise ServiceError(f'在架扣具不足，无法拆 {dq}（余 {ongoods}）')
        inv.transfer(
            conn, store_id, ttid, 'ONGOODS', 'BOX', dq,
            '收银拆扣回收', 'TXN', txn_code, u['username'], ts,
            src_floor=reg['floor_id'], dst_box=box_id)
        total_detached += dq
        event_items.append((ttid, dq))

    if event_items:
        cur = conn.execute(
            """INSERT INTO detach_events
               (code, source, txn_id, store_id, register_id, floor_id, box_id,
                cashier_id, mode, total_qty, ts, business_date, note)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (detach_code, 'POS', txn_id, store_id, register_id, reg['floor_id'],
             box_id, user_id, mode, total_detached, ts, bdate,
             '消磁器故障，手工拆扣' if mode == 'MANUAL' else ''),
        )
        detach_pk = cur.lastrowid
        conn.executemany(
            'INSERT INTO detach_event_items (detach_id, tag_type_id, qty) VALUES (?,?,?)',
            [(detach_pk, t, q) for t, q in event_items],
        )

    missed = sum(sold_by_type.values()) - total_detached
    if missed > 0:
        _incident(conn, store_id, 'POS_MISSED',
                  f'交易 {txn_code} 有 {missed} 件商品未拆扣，存在漏拆风险',
                  'TXN', txn_code, 'ERROR', ts)
    if mode == 'MANUAL':
        _incident(conn, store_id, 'DEACT_FAIL',
                  f'交易 {txn_code} 消磁失败，改用手工拆扣（mode=MANUAL）',
                  'TXN', txn_code, 'WARN', ts)
        if event_items:
            conn.execute(
                """UPDATE deactivator_faults SET detach_id=?
                   WHERE register_id=? AND status='OPEN'""",
                (detach_pk, register_id))
    return _get(conn, 'txns', txn_id)


def pos_detach_already(conn, txn_id):
    return conn.execute(
        "SELECT 1 FROM detach_events WHERE txn_id=? AND source='POS' LIMIT 1",
        (txn_id,),
    ).fetchone() is not None


def reconfirm_pos_detach(conn, txn_id, user_id):
    """同一交易二次确认拆扣 -> 拒绝（重复拆扣）。"""
    require_role(conn, user_id, ROLE_DETACH)
    if pos_detach_already(conn, txn_id):
        txn = _get(conn, 'txns', txn_id)
        _incident(conn, txn['store_id'], 'REPEAT_DETACH',
                  f'交易 {txn["code"]} 的拆扣事件已确认，禁止重复拆扣',
                  'TXN', txn['code'], 'ERROR')
        raise DuplicateEvent(f'交易 {txn["code"]} 拆扣事件只允许确认一次')

# ---------------------------------------------------------------- 消磁器故障
def report_fault(conn, store_id, deactivator_id, fault_kind, user_id, ts=None, note=''):
    ts = ts or now_ts()
    u = require_role(conn, user_id, ROLE_DETACH)
    deact = _get(conn, 'deactivators', deactivator_id)
    conn.execute("UPDATE deactivators SET status='FAULT' WHERE id=?", (deactivator_id,))
    code = _code('FLT')
    cur = conn.execute(
        """INSERT INTO deactivator_faults
           (code, store_id, deactivator_id, register_id, ts, business_date,
            fault_kind, status, handler_id, note)
           VALUES (?,?,?,?,?,?, 'OPEN', ?,?,?)""",
        (code, store_id, deactivator_id, deact['register_id'], ts, _bdate(ts),
         fault_kind, user_id, note),
    )
    _incident(conn, store_id, 'DEACT_FAIL',
              f'消磁器 {deact["code"]} 故障：{fault_kind}，已转手工拆扣',
              'FAULT', code, 'ERROR', ts)
    return _get(conn, 'deactivator_faults', cur.lastrowid)


def resolve_fault(conn, fault_id, user_id, ts=None, note=''):
    ts = ts or now_ts()
    require_role(conn, user_id, ROLE_APPROVE)
    fault = _get(conn, 'deactivator_faults', fault_id)
    conn.execute("UPDATE deactivators SET status='OK' WHERE id=?",
                 (fault['deactivator_id'],))
    conn.execute(
        """UPDATE deactivator_faults SET status='RESOLVED', handler_id=?,
           resolved_ts=?, note=COALESCE(note,'')||? WHERE id=?""",
        (user_id, ts, f'｜恢复：{note}', fault_id),
    )
    return _get(conn, 'deactivator_faults', fault_id)


# ---------------------------------------------------------------- 扣具报损
def damage_tags(conn, store_id, floor_id, tag_type_id, scope_from, qty, reason,
                user_id, ts=None):
    ts = ts or now_ts()
    u = require_role(conn, user_id, ROLE_ISSUE)
    if scope_from not in ('FLOOR', 'ONGOODS') or qty <= 0:
        raise ServiceError('报损来源须为 FLOOR/ONGOODS，数量为正')
    code = _code('DMG')
    inv.transfer(
        conn, store_id, tag_type_id, scope_from, 'DAMAGED', qty,
        f'扣具报损：{reason}', 'DAMAGE', code, u['username'], ts,
        src_floor=floor_id)
    cur = conn.execute(
        """INSERT INTO tag_damages
           (code, store_id, floor_id, tag_type_id, scope_from, qty, reason,
            business_date, ts, operator)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (code, store_id, floor_id, tag_type_id, scope_from, qty, reason,
         _bdate(ts), ts, u['username']),
    )
    return _get(conn, 'tag_damages', cur.lastrowid)


# ---------------------------------------------------------------- 出口报警
def ingest_alarm(conn, store_id, gate, ts, txn_id=None, tag_type_id=None,
                 evidence_path=None):
    """门禁适配器上报；能关联交易则 MATCHED_TXN，否则 NO_TXN。"""
    kind = 'MATCHED_TXN' if txn_id else 'NO_TXN'
    code = _code('ALM')
    cur = conn.execute(
        """INSERT INTO alarms
           (code, store_id, gate, ts, business_date, kind, txn_id, tag_type_id,
            status, evidence_path)
           VALUES (?,?,?,?,?,?,?,?,'OPEN',?)""",
        (code, store_id, gate, ts, _bdate(ts), kind, txn_id, tag_type_id,
         evidence_path),
    )
    alarm = _get(conn, 'alarms', cur.lastrowid)
    _incident(conn, store_id, 'ALARM_OPEN',
              f'出口 {gate} 报警（{kind}）事件 {code}',
              'ALARM', code, 'ERROR' if kind == 'NO_TXN' else 'WARN', ts)
    return alarm


def _missed_by_type(conn, txn_id):
    """交易售出扣具数 - 已 POS 拆回数 = 漏拆数（按扣型）。"""
    sold = {r['tag_type_id']: r['q'] for r in conn.execute(
        'SELECT tag_type_id, SUM(qty) q FROM txn_items WHERE txn_id=? GROUP BY tag_type_id',
        (txn_id,))}
    done = {r['tag_type_id']: r['q'] for r in conn.execute(
        """SELECT i.tag_type_id, SUM(i.qty) q
           FROM detach_event_items i JOIN detach_events e ON e.id=i.detach_id
           WHERE e.txn_id=? GROUP BY i.tag_type_id""", (txn_id,))}
    return {t: q - done.get(t, 0) for t, q in sold.items() if q - done.get(t, 0) > 0}


def resolve_alarm(conn, alarm_id, action, user_id, ts=None, note=''):
    """收银台复核：CONFIRMED_MISSED 确认漏拆并回收 / FALSE_ALARM 误报 / NO_TXN 升级。"""
    ts = ts or now_ts()
    u = require_role(conn, user_id, ROLE_DETACH)
    alarm = _get(conn, 'alarms', alarm_id)
    if alarm['status'] != 'OPEN':
        raise DuplicateEvent(f'报警 {alarm["code"]} 已关闭（{alarm["status"]}），不可重复处理')

    if action == 'NO_TXN_ESCALATE':
        if alarm['kind'] != 'NO_TXN':
            raise ServiceError('仅无交易报警可升级')
        conn.execute(
            """UPDATE alarms SET status='NO_TXN_ESCALATED', resolver_id=?,
               resolved_ts=?, resolution_note=? WHERE id=?""",
            (user_id, ts, note or '无交易凭证，升级店长/防损核查', alarm_id),
        )
        _incident(conn, alarm['store_id'], 'NO_TXN_ALARM',
                  f'报警 {alarm["code"]} 无关联交易，已升级核查', 'ALARM',
                  alarm['code'], 'ERROR', ts)
        return _get(conn, 'alarms', alarm_id)

    if action == 'FALSE_ALARM':
        conn.execute(
            """UPDATE alarms SET status='FALSE_ALARM', resolver_id=?,
               resolved_ts=?, resolution_note=? WHERE id=?""",
            (user_id, ts, note or '设备误报', alarm_id),
        )
        return _get(conn, 'alarms', alarm_id)

    if action != 'CONFIRMED_MISSED':
        raise ServiceError('未知复核动作')
    if alarm['kind'] != 'MATCHED_TXN':
        raise ServiceError('无交易报警不能按漏拆回收，请升级核查')

    missed = _missed_by_type(conn, alarm['txn_id'])
    if not missed:
        _incident(conn, alarm['store_id'], 'REPEAT_DETACH',
                  f'报警 {alarm["code"]} 复核时交易已无漏拆，拦截重复回收',
                  'ALARM', alarm['code'], 'ERROR', ts)
        raise DuplicateEvent('该交易已全部拆扣，无漏拆可回收（疑似重复操作）')

    txn = _get(conn, 'txns', alarm['txn_id'])
    reg, box, _ = _register_view(conn, txn['register_id'])
    detach_id = None
    for ttid, mq in missed.items():
        ongoods, _ = inv.balance(conn, store_id=alarm['store_id'],
                                 tag_type_id=ttid, scope='ONGOODS',
                                 floor_id=txn['floor_id'])
        if mq > ongoods:
            raise ServiceError(f'漏拆 {mq} 超过在架量 {ongoods}，请先盘点')
        if detach_id is None:
            code = _code('DTC')
            cur = conn.execute(
                """INSERT INTO detach_events
                   (code, source, txn_id, alarm_id, store_id, register_id, floor_id,
                    box_id, cashier_id, mode, total_qty, ts, business_date, note)
                   VALUES (?,?,?,?,?,?,?,?,?,'MANUAL',?,?,?, '出口报警漏拆复核回收')""",
                (code, 'ALARM', txn['id'], alarm_id, alarm['store_id'],
                 txn['register_id'], txn['floor_id'], box['id'], user_id,
                 sum(missed.values()), ts, _bdate(ts)),
            )
            detach_id = cur.lastrowid
        inv.transfer(
            conn, alarm['store_id'], ttid, 'ONGOODS', 'BOX', mq,
            '漏拆复核回收', 'ALARM', alarm['code'], u['username'], ts,
            src_floor=txn['floor_id'], dst_box=box['id'])
        conn.execute(
            'INSERT INTO detach_event_items (detach_id, tag_type_id, qty) VALUES (?,?,?)',
            (detach_id, ttid, mq),
        )
    conn.execute(
        """UPDATE alarms SET status='CONFIRMED_MISSED', detach_id=?, resolver_id=?,
           resolved_ts=?, resolution_note=? WHERE id=?""",
        (detach_id, user_id, ts, note or f'确认漏拆 {sum(missed.values())} 个，已回收',
         alarm_id),
    )
    for ttid in missed:
        _refresh_close_snapshot(conn, alarm['store_id'], _bdate(ts),
                                txn['floor_id'], ttid)
    return _get(conn, 'alarms', alarm_id)


# ---------------------------------------------------------------- 闭店清点
def run_close_count(conn, store_id, business_date, actual_override=None):
    """按 楼层×扣型 计算 领用→挂扣→拆回→在架→散扣→报损 数量链并核对平衡。

    actual_override: {(floor_id, tag_type_id): (loose_actual, on_shelf_actual)}
    """
    actual_override = actual_override or {}
    rows = conn.execute(
        """SELECT DISTINCT floor_id, tag_type_id FROM (
             SELECT floor_id, tag_type_id FROM tag_issues WHERE store_id=? AND business_date=?
             UNION
             SELECT floor_id, tag_type_id FROM tag_batches
               WHERE store_id=? AND business_date=? AND status IN ('OK','SHORT')
             UNION
             SELECT e.floor_id, i.tag_type_id FROM detach_events e
               JOIN detach_event_items i ON i.detach_id=e.id
               WHERE e.store_id=? AND e.business_date=?
             UNION
             SELECT floor_id, tag_type_id FROM tag_damages WHERE store_id=? AND business_date=?
           )""",
        (store_id, business_date,
         store_id, business_date,
         store_id, business_date,
         store_id, business_date),
    ).fetchall()

    results = []
    for r in rows:
        fid, ttid = r['floor_id'], r['tag_type_id']

        def sums(table, qcol, where):
            d = {x['tag_type_id']: x['q'] for x in conn.execute(
                f'SELECT tag_type_id, COALESCE(SUM({qcol}),0) q FROM {table} '
                f'WHERE store_id=? AND business_date=? AND floor_id=? AND {where} '
                'GROUP BY tag_type_id',
                (store_id, business_date, fid)).fetchall()}
            return d.get(ttid, 0)

        issued = sums('tag_issues', 'qty_issued', "status IN ('OK','SHORT')")
        attached = sums('tag_batches', 'attached_qty', "status IN ('OK','SHORT')")
        detached = conn.execute(
            """SELECT COALESCE(SUM(i.qty),0) q
               FROM detach_events e JOIN detach_event_items i ON i.detach_id=e.id
               WHERE e.store_id=? AND e.business_date=? AND e.floor_id=?
                 AND i.tag_type_id=?""",
            (store_id, business_date, fid, ttid)).fetchone()['q']
        dmg_floor = conn.execute(
            """SELECT COALESCE(SUM(qty),0) q FROM tag_damages
               WHERE store_id=? AND business_date=? AND floor_id=? AND tag_type_id=?
                 AND scope_from='FLOOR'""",
            (store_id, business_date, fid, ttid)).fetchone()['q']
        dmg_ongoods = conn.execute(
            """SELECT COALESCE(SUM(qty),0) q FROM tag_damages
               WHERE store_id=? AND business_date=? AND floor_id=? AND tag_type_id=?
                 AND scope_from='ONGOODS'""",
            (store_id, business_date, fid, ttid)).fetchone()['q']
        wo_floor = _approved_writeoff(conn, store_id, business_date, fid, ttid, 'FLOOR')
        wo_shelf = _approved_writeoff(conn, store_id, business_date, fid, ttid, 'ONGOODS')

        loose_book, _ = inv.balance(conn, store_id, ttid, 'FLOOR', fid)
        shelf_book, _ = inv.balance(conn, store_id, ttid, 'ONGOODS', fid)
        if (fid, ttid) in actual_override:
            loose_actual, shelf_actual = actual_override[(fid, ttid)]
            loose_actual = loose_book if loose_actual is None else loose_actual
            shelf_actual = shelf_book if shelf_actual is None else shelf_actual
        else:
            ci = conn.execute(
                """SELECT * FROM count_inputs
                   WHERE store_id=? AND business_date=? AND floor_id=? AND tag_type_id=?""",
                (store_id, business_date, fid, ttid)).fetchone()
            loose_actual = ci['loose_actual'] if ci and ci['loose_actual'] is not None else loose_book
            shelf_actual = ci['on_shelf_actual'] if ci and ci['on_shelf_actual'] is not None else shelf_book

        chain_ok = (issued == attached + loose_book + dmg_floor + wo_floor and
                    attached == detached + shelf_book + dmg_ongoods + wo_shelf)
        variance = (loose_actual + shelf_actual) - (loose_book + shelf_book)
        status = 'BALANCED' if chain_ok and variance == 0 else 'PENDING_VARIANCE'
        ts = now_ts()
        conn.execute(
            """INSERT INTO close_counts
               (code, store_id, business_date, floor_id, tag_type_id, issued_qty,
                attached_qty, detached_qty, loose_book, loose_actual, on_shelf_book,
                on_shelf_actual, damaged_floor_qty, damaged_ongoods_qty,
                writeoff_floor_qty, writeoff_ongoods_qty, chain_ok,
                variance_qty, status, created_ts)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(store_id, business_date, floor_id, tag_type_id) DO UPDATE SET
                 issued_qty=excluded.issued_qty, attached_qty=excluded.attached_qty,
                 detached_qty=excluded.detached_qty, loose_book=excluded.loose_book,
                 loose_actual=excluded.loose_actual, on_shelf_book=excluded.on_shelf_book,
                 on_shelf_actual=excluded.on_shelf_actual,
                 damaged_floor_qty=excluded.damaged_floor_qty,
                 damaged_ongoods_qty=excluded.damaged_ongoods_qty,
                 writeoff_floor_qty=excluded.writeoff_floor_qty,
                 writeoff_ongoods_qty=excluded.writeoff_ongoods_qty,
                 chain_ok=excluded.chain_ok, variance_qty=excluded.variance_qty,
                 status=excluded.status, created_ts=excluded.created_ts""",
            (_code('CLS'), store_id, business_date, fid, ttid, issued, attached,
             detached, loose_book, loose_actual, shelf_book, shelf_actual,
             dmg_floor, dmg_ongoods, wo_floor, wo_shelf, 1 if chain_ok else 0,
             variance, status, ts),
        )
        cc = conn.execute(
            """SELECT * FROM close_counts
               WHERE store_id=? AND business_date=? AND floor_id=? AND tag_type_id=?""",
            (store_id, business_date, fid, ttid)).fetchone()
        results.append(cc)
        if not chain_ok:
            _incident(conn, store_id, 'CHAIN_BROKEN',
                      f'闭店数量链不平衡：楼层 {fid} 扣型 {ttid}，'
                      f'领 {issued}＝挂 {attached}+散 {loose_book}+损 {dmg_floor}+盘差 {wo_floor}？'
                      f' 挂 {attached}＝拆 {detached}+架 {shelf_book}+损 {dmg_ongoods}+盘差 {wo_shelf}？',
                      'CLOSE', cc['code'], 'ERROR', ts)
        if variance != 0:
            _incident(conn, store_id, 'COUNT_VARIANCE',
                      f'盘点差异 {variance:+d}：楼层 {fid} 扣型 {ttid}（实物-账面）',
                      'CLOSE', cc['code'], 'ERROR', ts)
    return results


# ---------------------------------------------------------------- 盘差审批
def _approved_writeoff(conn, store_id, business_date, floor_id, tag_type_id, scope):
    """当日已批准盘差中从指定 scope 净调出的数量（盘亏为正，盘盈为负）。"""
    row = conn.execute(
        """SELECT COALESCE(SUM(CASE WHEN direction='SHORT' THEN qty ELSE -qty END),0) q
           FROM variance_requests v JOIN close_counts c ON c.id=v.close_count_id
           WHERE v.store_id=? AND c.business_date=? AND v.floor_id=?
             AND v.tag_type_id=? AND v.scope=? AND v.status='APPROVED'""",
        (store_id, business_date, floor_id, tag_type_id, scope)).fetchone()
    return row['q']


def _refresh_close_snapshot(conn, store_id, business_date, floor_id, tag_type_id):
    """调账/回收后，用最新账面与拆回刷新闭店快照并重算差异与状态。"""
    cc = conn.execute(
        """SELECT * FROM close_counts WHERE store_id=? AND business_date=?
           AND floor_id=? AND tag_type_id=?""",
        (store_id, business_date, floor_id, tag_type_id)).fetchone()
    if not cc:
        return
    loose_book, _ = inv.balance(conn, store_id, tag_type_id, 'FLOOR', floor_id)
    shelf_book, _ = inv.balance(conn, store_id, tag_type_id, 'ONGOODS', floor_id)
    detached = conn.execute(
        """SELECT COALESCE(SUM(i.qty),0) q FROM detach_events e
           JOIN detach_event_items i ON i.detach_id=e.id
           WHERE e.store_id=? AND e.business_date=? AND e.floor_id=? AND i.tag_type_id=?""",
        (store_id, business_date, floor_id, tag_type_id)).fetchone()['q']
    wo_floor = _approved_writeoff(conn, store_id, business_date, floor_id, tag_type_id, 'FLOOR')
    wo_shelf = _approved_writeoff(conn, store_id, business_date, floor_id, tag_type_id, 'ONGOODS')
    chain_ok = (cc['issued_qty'] == cc['attached_qty'] + loose_book + cc['damaged_floor_qty'] + wo_floor
                and cc['attached_qty'] == detached + shelf_book + cc['damaged_ongoods_qty'] + wo_shelf)
    remaining = (cc['loose_actual'] + cc['on_shelf_actual']) - (loose_book + shelf_book)
    conn.execute(
        """UPDATE close_counts SET loose_book=?, on_shelf_book=?, detached_qty=?,
           writeoff_floor_qty=?, writeoff_ongoods_qty=?, chain_ok=?,
           variance_qty=?, status=? WHERE id=?""",
        (loose_book, shelf_book, detached, wo_floor, wo_shelf,
         1 if chain_ok else 0, remaining,
         'BALANCED' if chain_ok and remaining == 0 else 'PENDING_VARIANCE', cc['id']),
    )


def create_variance(conn, close_count_id, scope, qty, direction, reason,
                    applicant_id, ts=None):
    """导购/收银发起盘差（申请人不得是店长）。"""
    ts = ts or now_ts()
    u = require_role(conn, applicant_id, ROLE_APPLY_VARIANCE)
    cc = _get(conn, 'close_counts', close_count_id)
    if cc['status'] != 'PENDING_VARIANCE':
        raise ServiceError('只有存在差异的闭店记录可发起盘差')
    if scope not in ('FLOOR', 'ONGOODS') or qty <= 0:
        raise ServiceError('盘差范围须为 FLOOR/ONGOODS，数量为正')
    if direction not in ('SHORT', 'SURPLUS'):
        raise ServiceError('差异方向须为 SHORT/SURPLUS')
    dup = conn.execute(
        """SELECT 1 FROM variance_requests WHERE close_count_id=? AND scope=?
           AND status='PENDING'""", (close_count_id, scope)).fetchone()
    if dup:
        raise DuplicateEvent('该范围已有待审批盘差单')
    code = _code('VAR')
    cur = conn.execute(
        """INSERT INTO variance_requests
           (code, store_id, close_count_id, floor_id, tag_type_id, scope, qty,
            direction, reason, status, applicant_id, ts)
           VALUES (?,?,?,?,?,?,?,?,?,'PENDING',?,?)""",
        (code, cc['store_id'], close_count_id, cc['floor_id'], cc['tag_type_id'],
         scope, qty, direction, reason, applicant_id, ts),
    )
    return _get(conn, 'variance_requests', cur.lastrowid)


def decide_variance(conn, variance_id, decision, approver_id, ts=None, note=''):
    """店长批准/驳回；批准后按方向调整账面（经 ADJUST 过渡，流水守恒）。"""
    ts = ts or now_ts()
    approver = require_role(conn, approver_id, ROLE_APPROVE)
    vr = _get(conn, 'variance_requests', variance_id)
    if vr['status'] != 'PENDING':
        raise DuplicateEvent(f'盘差单 {vr["code"]} 已{vr["status"]}，不可重复审批')
    if approver['id'] == vr['applicant_id']:
        raise PermissionDenied('审批人不得与申请人为同一人')

    if decision == 'REJECTED':
        conn.execute(
            """UPDATE variance_requests SET status='REJECTED', approver_id=?,
               decided_ts=?, decision_note=? WHERE id=?""",
            (approver_id, ts, note, variance_id),
        )
        return _get(conn, 'variance_requests', variance_id)

    if decision != 'APPROVED':
        raise ServiceError('审批决定须为 APPROVED/REJECTED')

    scope = vr['scope']
    signed = -vr['qty'] if vr['direction'] == 'SHORT' else vr['qty']
    mid = inv.new_move_id()
    # 盘亏：散扣/在架 -> ADJUST；盘盈反向。同一 move_id 两行流水守恒
    inv.adjust(conn, mid, vr['store_id'], vr['tag_type_id'], scope, signed,
               '盘差批准调账', 'VARIANCE', vr['code'], approver['username'],
               floor_id=vr['floor_id'], ts=ts)
    inv.adjust(conn, mid, vr['store_id'], vr['tag_type_id'], 'ADJUST', -signed,
               '盘差批准调账', 'VARIANCE', vr['code'], approver['username'], ts=ts)
    conn.execute(
        """UPDATE variance_requests SET status='APPROVED', approver_id=?,
           decided_ts=?, decision_note=? WHERE id=?""",
        (approver_id, ts, note, variance_id),
    )

    # 刷新闭店快照：调账后账面与差异归零
    cc_row = _get(conn, 'close_counts', vr['close_count_id'])
    _refresh_close_snapshot(conn, vr['store_id'], cc_row['business_date'],
                            vr['floor_id'], vr['tag_type_id'])
    return _get(conn, 'variance_requests', variance_id)
