# -*- coding: utf-8 -*-
"""样例数据导入：一条本地命令即可得到 漏拆 / 串台 / 盘差 等可复核场景。

运行：python3 -m app.seed            （默认 data/antitheft.db）
"""
import sys
from datetime import datetime

from . import db as dbmod
from . import services
from .adapters import MockGateAdapter, MockPosAdapter
from .errors import ServiceError


def _at(hour, minute=0):
    return datetime.now().replace(hour=hour, minute=minute, second=0,
                                  microsecond=0).isoformat(timespec='seconds')


def seed(db_path='data/antitheft.db', evidence_dir='evidence', reset=True):
    if reset:
        import pathlib
        p = pathlib.Path(db_path)
        if p.exists():
            p.unlink()
        for suffix in ('-wal', '-shm'):
            wp = pathlib.Path(str(p) + suffix)
            if wp.exists():
                wp.unlink()
        # 清掉上一次的报警凭证（文件名含随机单号），避免堆积
        ev = pathlib.Path(evidence_dir)
        if ev.exists():
            for old in ev.glob('*.svg'):
                old.unlink()
    dbmod.init_db(db_path)
    conn = dbmod.connect(db_path)
    conn.execute('BEGIN')
    try:
        _seed(conn, evidence_dir)
        conn.execute('COMMIT')
    except Exception:
        conn.execute('ROLLBACK')
        raise
    finally:
        conn.close()
    return db_path


def _seed(conn, evidence_dir):
    # ---- 主数据
    conn.execute("INSERT INTO stores (id,code,name,city) VALUES (1,'S001','云裳旗舰店','上海')")
    floors = [
        (1, 1, 'F1', '一楼女装', 1),
        (2, 1, 'F2', '二楼男装', 2),
    ]
    conn.executemany('INSERT INTO floors (id,store_id,code,name,seq) VALUES (?,?,?,?,?)', floors)
    types = [
        (1, 'HARD', '墨锥硬扣', '标准硬标签'),
        (2, 'INK', '墨水瓶扣', '含墨防盗扣'),
        (3, 'WIRE', '钢丝绳扣', '高档服饰专用'),
    ]
    conn.executemany('INSERT INTO tag_types (id,code,name,spec) VALUES (?,?,?,?)', types)
    users = [
        (1, 'guwen', '顾晓雯', 'ASSOCIATE'),
        (2, 'luming', '陆明', 'ASSOCIATE'),
        (3, 'yinyin', '尹收银', 'CASHIER'),
        (4, 'chenchen', '陈晨', 'CASHIER'),
        (5, 'zhangzhang', '章店长', 'MANAGER'),
    ]
    conn.executemany('INSERT INTO users (id,username,display_name,role) VALUES (?,?,?,?)', users)
    boxes = [
        (1, 1, 'BX-01', 1, 'ACTIVE'),
        (2, 1, 'BX-02', 2, 'ACTIVE'),
    ]
    conn.executemany(
        'INSERT INTO recovery_boxes (id,store_id,code,register_id,status) VALUES (?,?,?,?,?)', boxes)
    deacts = [
        (1, 1, 'D-01', 1, 'OK'),
        (2, 1, 'D-02', 2, 'OK'),
    ]
    conn.executemany(
        'INSERT INTO deactivators (id,store_id,code,register_id,status) VALUES (?,?,?,?,?)', deacts)
    regs = [
        (1, 1, 'R-01', '1号收银台', 1, 1, 1),
        (2, 1, 'R-02', '2号收银台', 2, 2, 2),
    ]
    conn.executemany(
        """INSERT INTO registers (id,store_id,code,name,floor_id,box_id,deactivator_id)
           VALUES (?,?,?,?,?,?,?)""", regs)
    products = [
        (1, 1, 'W001', '真丝连衣裙', 1, 899.0),
        (2, 1, 'W002', '羊毛开衫', 2, 599.0),
        (3, 1, 'M001', '男士皮夹克', 3, 1599.0),
    ]
    conn.executemany(
        'INSERT INTO products (id,store_id,sku,name,tag_type_id,price) VALUES (?,?,?,?,?,?)',
        products)

    # ---- 期初总仓库存：HARD 120 / INK 80 / WIRE 60
    from .inventory import adjust, new_move_id
    for ttid, qty in ((1, 120), (2, 80), (3, 60)):
        adjust(conn, new_move_id(), 1, ttid, 'STOCK', qty, '期初建账',
               operator='system', ts=_at(8, 0))

    # ---- 楼层领用（F2 WIRE 申请 80 实际仅 60 → 领用短缺）
    services.issue_tags(conn, 1, 1, 1, 50, 1, ts=_at(9, 0))    # F1 硬扣
    services.issue_tags(conn, 1, 1, 2, 30, 1, ts=_at(9, 5))    # F1 墨水扣
    services.issue_tags(conn, 1, 2, 3, 80, 2, ts=_at(9, 10))   # F2 钢丝绳扣 -> SHORT

    # ---- 挂扣批次（含一次扣型不匹配：连衣裙误用钢丝绳扣）
    services.attach_batch(conn, 1, 1, 1, 1, 30, 30, 1, ts=_at(10, 0))
    services.attach_batch(conn, 1, 1, 2, 2, 20, 20, 1, ts=_at(10, 20))
    services.attach_batch(conn, 1, 2, 3, 3, 40, 40, 2, ts=_at(10, 30))
    services.attach_batch(conn, 1, 1, 1, 3, 10, 10, 1, ts=_at(10, 40))  # MISMATCH

    # ---- 扣具报损（F1 硬扣散扣 2 个破损）
    services.damage_tags(conn, 1, 1, 1, 'FLOOR', 2, '针扣断裂/外壳破损', 1,
                         ts=_at(11, 0))

    # ---- POS 收银报文：正常 / 消磁手工 / 漏拆 / 串台
    # （消磁器故障在处理循环内按时间线上报，保证 E1001 为 AUTO、E1002 为 MANUAL）
    payments = [
        # 正常成交，全部拆扣（此时消磁器正常，mode=AUTO）
        dict(ext_no='E1001', register_code='R-01', cashier_username='yinyin',
             hour=15, minute=20, items=[('W001', 2, 2), ('W002', 1, 1)]),
        # 消磁器故障后手工拆扣（显式 manual 标记，mode=MANUAL）
        dict(ext_no='E1002', register_code='R-01', cashier_username='yinyin',
             hour=15, minute=40, manual=True, items=[('W001', 1, 1)]),
        # 漏拆：3 件连衣裙只拆 2，1 个硬扣带出店门
        dict(ext_no='E1003', register_code='R-01', cashier_username='yinyin',
             hour=18, minute=30, items=[('W001', 3, 2)]),
        # 回收盒串台：R-02 的成交扫描投了 R-01 的盒子 -> 整笔被拦截
        dict(ext_no='E1004', register_code='R-02', cashier_username='chenchen',
             hour=18, minute=45, box_code='BX-01', items=[('M001', 1, 1)]),
        # 串台纠正后 R-02 正常成交
        dict(ext_no='E1005', register_code='R-02', cashier_username='chenchen',
             hour=18, minute=50, items=[('M001', 1, 1)]),
    ]
    pos = MockPosAdapter(payments)
    # 串台那笔服务层会拒绝，单独捕获以保留 incident，其余正常入库
    txn_map = {}
    products_by_sku = {r['sku']: r for r in conn.execute('SELECT * FROM products')}
    regs_by_code = {r['code']: r for r in conn.execute('SELECT * FROM registers')}
    users_by_name = {r['username']: r for r in conn.execute('SELECT * FROM users')}
    boxes_by_code = {r['code']: r for r in conn.execute('SELECT * FROM recovery_boxes')}
    while pos.has_pending():
        pay = pos.poll()
        # 故障时间线穿插在报文之间：E1001 后上报，E1002 后修复
        if pay['ext_no'] == 'E1002':
            services.report_fault(conn, 1, 1, 'COIL_FAIL', 3, ts=_at(15, 30),
                                  note='线圈老化，消磁无响应')
        reg = regs_by_code[pay['register_code']]
        cashier = users_by_name[pay['cashier_username']]
        box = boxes_by_code[pay['box_code']] if pay.get('box_code') \
            else conn.execute('SELECT * FROM recovery_boxes WHERE id=?',
                              (reg['box_id'],)).fetchone()
        items = [(products_by_sku[s]['id'], q, d) for s, q, d in pay['items']]
        try:
            txn = services.pos_sale_detach(
                conn, 1, reg['id'], cashier['id'], items, box['id'],
                ts=_at(pay['hour'], pay['minute']), manual=pay.get('manual'))
            txn_map[pay['ext_no']] = txn['id']
        except ServiceError:
            # 串台已由服务层记录 CROSS_BOX 异常，交易不成立，继续后续报文
            continue
        if pay['ext_no'] == 'E1002':
            services.resolve_fault(conn, 1, 5, ts=_at(16, 30),
                                   note='更换线圈并复测通过')

    # ---- 门禁报警：关联 E1003 的漏拆报警（待复核）+ 无交易报警
    gate_msgs = [
        dict(hour=18, minute=35, gate='GATE-EAST', ext_no='E1003',
             tag_type_code='HARD'),
        dict(hour=19, minute=5, gate='GATE-WEST', tag_type_code='INK'),
    ]
    gate = MockGateAdapter(gate_msgs, evidence_dir)
    gate.ingest_all(conn, 1, txn_map)

    # ---- 闭店实物清点（22:00，报警发生在 18:35）：
    #   F1 硬扣散扣盘亏 1（账面18，实点17）；漏拆连衣裙已离店，在架实物 24（账面25），
    #   在架差额对应待复核的漏拆报警，复核回收后账面即与实物一致。
    bdate = datetime.now().date().isoformat()
    conn.execute(
        """INSERT INTO count_inputs
           (store_id,business_date,floor_id,tag_type_id,loose_actual,on_shelf_actual)
           VALUES (?,?,?,?,?,?)""",
        (1, bdate, 1, 1, 17, 24))
    results = services.run_close_count(conn, 1, bdate)

    # ---- 散扣盘亏发起盘差审批（导购申请，留待店长批准）；
    #   在架盘亏由待复核漏拆报警解释，不重复立盘差单
    for cc in results:
        loose_delta = cc['loose_actual'] - cc['loose_book']
        if loose_delta < 0:
            services.create_variance(
                conn, cc['id'], 'FLOOR', abs(loose_delta), 'SHORT',
                '闭店散扣实点少于账面，疑领用散落未登记', 1, ts=_at(22, 0))


if __name__ == '__main__':
    db_path = sys.argv[1] if len(sys.argv) > 1 else 'data/antitheft.db'
    ev_dir = sys.argv[2] if len(sys.argv) > 2 else 'evidence'
    seed(db_path, ev_dir)
    print(f'样例数据已导入：{db_path}，报警凭证目录：{ev_dir}/')
