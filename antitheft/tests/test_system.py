# -*- coding: utf-8 -*-
"""系统测试：数量守恒 / 重复事件 / 并发版本校验 / 职责分离 / 盘差审批。

运行：python3 -m unittest discover -s tests -v
每个用例使用独立临时数据库，互不影响。
"""
import os
import tempfile
import threading
import unittest
from datetime import datetime

from app import db as dbmod
from app import services
from app.inventory import (adjust, balance, cas_adjust, new_move_id,
                           transfer)
from app.errors import (DuplicateEvent, PermissionDenied, ServiceError,
                        VersionConflict)


def at(hour, minute=0):
    return datetime.now().replace(hour=hour, minute=minute, second=0,
                                  microsecond=0).isoformat(timespec='seconds')


class BaseCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, 't.db')
        self.ev_dir = os.path.join(self.tmp.name, 'evidence')
        dbmod.init_db(self.db_path)
        self.conn = dbmod.connect(self.db_path)
        self.conn.execute('BEGIN')
        self._master()
        self.bdate = datetime.now().date().isoformat()

    def tearDown(self):
        self.conn.execute('ROLLBACK')
        self.conn.close()
        self.tmp.cleanup()

    def _master(self):
        c = self.conn
        c.execute("INSERT INTO stores (id,code,name,city) VALUES (1,'S','旗舰店','上海')")
        c.executemany('INSERT INTO floors (id,store_id,code,name,seq) VALUES (?,?,?,?,?)',
                      [(1, 1, 'F1', '一楼', 1), (2, 1, 'F2', '二楼', 2)])
        c.executemany('INSERT INTO tag_types (id,code,name,spec) VALUES (?,?,?,?)',
                      [(1, 'HARD', '硬扣', ''), (2, 'INK', '墨水扣', '')])
        c.executemany('INSERT INTO users (id,username,display_name,role) VALUES (?,?,?,?)',
                      [(1, 'a1', '导购甲', 'ASSOCIATE'), (2, 'a2', '导购乙', 'ASSOCIATE'),
                       (3, 'c1', '收银甲', 'CASHIER'), (4, 'm1', '店长甲', 'MANAGER')])
        c.executemany('INSERT INTO recovery_boxes (id,store_id,code,register_id,status) VALUES (?,?,?,?,?)',
                      [(1, 1, 'BX-01', 1, 'ACTIVE'), (2, 1, 'BX-02', 2, 'ACTIVE')])
        c.executemany('INSERT INTO deactivators (id,store_id,code,register_id,status) VALUES (?,?,?,?,?)',
                      [(1, 1, 'D-01', 1, 'OK'), (2, 1, 'D-02', 2, 'OK')])
        c.executemany(
            'INSERT INTO registers (id,store_id,code,name,floor_id,box_id,deactivator_id) VALUES (?,?,?,?,?,?,?)',
            [(1, 1, 'R-01', '一台', 1, 1, 1), (2, 1, 'R-02', '二台', 2, 2, 2)])
        c.executemany(
            'INSERT INTO products (id,store_id,sku,name,tag_type_id,price) VALUES (?,?,?,?,?,?)',
            [(1, 1, 'P1', '连衣裙', 1, 100.0), (2, 1, 'P2', '衬衫', 2, 200.0)])
        adjust(c, new_move_id(), 1, 1, 'STOCK', 100, '期初', operator='sys', ts=at(8))
        adjust(c, new_move_id(), 1, 2, 'STOCK', 50, '期初', operator='sys', ts=at(8))

    def totals(self, tag_type_id):
        """该扣型在所有 scope 的数量合计（守恒恒等于期初）。"""
        c = self.conn
        return c.execute(
            'SELECT COALESCE(SUM(qty),0) s FROM inventory WHERE tag_type_id=?',
            (tag_type_id,)).fetchone()['s']


class TestConservation(BaseCase):
    def test_issue_attach_detach_conserves(self):
        c = self.conn
        self.assertEqual(self.totals(1), 100)
        services.issue_tags(c, 1, 1, 1, 30, 1, ts=at(9))
        self.assertEqual(self.totals(1), 100)  # STOCK->FLOOR 总量不变
        services.attach_batch(c, 1, 1, 1, 1, 20, 20, 1, ts=at(10))
        self.assertEqual(self.totals(1), 100)  # FLOOR->ONGOODS
        txn = services.pos_sale_detach(c, 1, 1, 3, [(1, 5, 5)], 1, ts=at(15))
        self.assertEqual(self.totals(1), 100)  # ONGOODS->BOX
        # 报损也守恒（FLOOR->DAMAGED）
        services.damage_tags(c, 1, 1, 1, 'FLOOR', 2, '破损', 1, ts=at(16))
        self.assertEqual(self.totals(1), 100)

    def test_ledger_double_entry_balanced(self):
        """每次转移在流水里是借贷两行，move_id 内 delta 之和为 0。"""
        c = self.conn
        services.issue_tags(c, 1, 1, 1, 10, 1, ts=at(9))
        rows = c.execute(
            """SELECT move_id, SUM(delta) s FROM inventory_ledger
               GROUP BY move_id HAVING COUNT(*)>1""").fetchall()
        self.assertTrue(rows)
        for r in rows:
            self.assertEqual(r['s'], 0, f'移动单 {r["move_id"]} 借贷不平')

    def test_close_chain_balanced_full_day(self):
        """完整一天：领40挂38（留2散扣），售10拆10，散扣报损2，闭店两条平衡等式都成立。"""
        c = self.conn
        services.issue_tags(c, 1, 1, 1, 40, 1, ts=at(9))
        services.attach_batch(c, 1, 1, 1, 1, 40, 38, 1, ts=at(10))  # 应挂40实挂38
        services.pos_sale_detach(c, 1, 1, 3, [(1, 10, 10)], 1, ts=at(15))
        services.damage_tags(c, 1, 1, 1, 'FLOOR', 2, '丢针', 1, ts=at(16))
        res = services.run_close_count(c, 1, self.bdate)
        cc = [r for r in res if r['tag_type_id'] == 1 and r['floor_id'] == 1][0]
        # 平衡① 领40 = 挂38 + 散0 + 散扣报损2
        self.assertEqual(cc['issued_qty'], cc['attached_qty'] + cc['loose_book']
                         + cc['damaged_floor_qty'] + cc['writeoff_floor_qty'])
        # 平衡② 挂38 = 拆10 + 在架28
        self.assertEqual(cc['attached_qty'], cc['detached_qty'] + cc['on_shelf_book']
                         + cc['damaged_ongoods_qty'] + cc['writeoff_ongoods_qty'])
        self.assertEqual(cc['chain_ok'], 1)
        self.assertEqual(cc['status'], 'BALANCED')


class TestDuplicateEvents(BaseCase):
    def _one_sale(self, detached=1):
        c = self.conn
        services.issue_tags(c, 1, 1, 1, 10, 1, ts=at(9))
        services.attach_batch(c, 1, 1, 1, 1, 5, 5, 1, ts=at(10))
        return services.pos_sale_detach(
            c, 1, 1, 3, [(1, 1, detached)], 1, ts=at(15))

    def test_pos_detach_only_once(self):
        c = self.conn
        txn = self._one_sale()
        self.assertTrue(services.pos_detach_already(c, txn['id']))
        with self.assertRaises(DuplicateEvent):
            services.reconfirm_pos_detach(c, txn['id'], 3)
        # 数据库层也兜底：同 txn 第二条 POS 事件违反唯一索引
        with self.assertRaises(Exception):
            c.execute(
                """INSERT INTO detach_events
                   (code,source,txn_id,store_id,register_id,floor_id,box_id,
                    cashier_id,mode,total_qty,ts,business_date)
                   VALUES ('DTC-X','POS',?,1,1,1,1,3,'AUTO',1,?,?)""",
                (txn['id'], at(16), self.bdate))

    def test_alarm_resolved_once(self):
        c = self.conn
        txn = self._one_sale(detached=0)  # 全部漏拆
        alarm = services.ingest_alarm(c, 1, 'G1', at(15, 30), txn_id=txn['id'],
                                      tag_type_id=1)
        services.resolve_alarm(c, alarm['id'], 'CONFIRMED_MISSED', 3)
        box_before = c.execute("SELECT qty FROM inventory WHERE scope='BOX' AND tag_type_id=1").fetchone()['qty']
        # 再次复核必须被拒，且不产生第二次回收
        with self.assertRaises(DuplicateEvent):
            services.resolve_alarm(c, alarm['id'], 'CONFIRMED_MISSED', 3)
        box_after = c.execute("SELECT qty FROM inventory WHERE scope='BOX' AND tag_type_id=1").fetchone()['qty']
        self.assertEqual(box_before, box_after)

    def test_variance_decided_once(self):
        c = self.conn
        services.issue_tags(c, 1, 1, 1, 10, 1, ts=at(9))
        services.run_close_count(c, 1, self.bdate)  # 无差异
        # 手工造一条差异闭店记录以发起盘差
        c.execute("UPDATE close_counts SET status='PENDING_VARIANCE' WHERE tag_type_id=1")
        cc_id = c.execute('SELECT id FROM close_counts WHERE tag_type_id=1').fetchone()['id']
        vr = services.create_variance(c, cc_id, 'FLOOR', 1, 'SHORT', '测试', 1)
        services.decide_variance(c, vr['id'], 'APPROVED', 4)
        with self.assertRaises(DuplicateEvent):
            services.decide_variance(c, vr['id'], 'APPROVED', 4)


class TestConcurrency(unittest.TestCase):
    """两人并发调整同一库存行：版本校验只允许一人成功。"""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, 't.db')
        dbmod.init_db(self.db_path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_stale_version_rejected(self):
        conn = dbmod.connect(self.db_path)
        conn.execute('BEGIN')
        conn.execute("INSERT INTO stores (id,code,name) VALUES (1,'S','x')")
        conn.execute("INSERT INTO tag_types (id,code,name) VALUES (1,'H','硬')")
        adjust(conn, new_move_id(), 1, 1, 'STOCK', 100, '期初', operator='s')
        base_qty, ver = balance(conn, 1, 1, 'STOCK')
        # 甲先提交 +10，版本前进
        adjust(conn, new_move_id(), 1, 1, 'STOCK', 10, '甲调整', operator='A')
        # 乙仍持旧版本号提交 +5，必须失败
        with self.assertRaises(VersionConflict):
            cas_adjust(conn, new_move_id(), 1, 1, 'STOCK', base_qty, ver, 5,
                       '乙基于旧版本调整', operator='B')
        self.assertEqual(balance(conn, 1, 1, 'STOCK')[0], 110)
        conn.execute('ROLLBACK')
        conn.close()

    def test_two_threads_only_one_wins(self):
        """两个连接/线程同时扣减同一行：先到成功，后到版本冲突。"""
        # 先在独立事务里提交主数据与期初库存
        setup = dbmod.connect(self.db_path)
        setup.execute('BEGIN')
        setup.execute("INSERT INTO stores (id,code,name) VALUES (1,'S','x')")
        setup.execute("INSERT INTO tag_types (id,code,name) VALUES (1,'H','硬')")
        adjust(setup, new_move_id(), 1, 1, 'STOCK', 100, '期初', operator='s')
        setup.execute('COMMIT')
        setup.close()

        barrier = threading.Barrier(2)

        def worker(name, result):
            conn = dbmod.connect(self.db_path)
            try:
                # 事务外两人都读到同一基线 v=0、qty=100（模拟并发加载页面）
                base_qty, ver = balance(conn, 1, 1, 'STOCK')
                barrier.wait()
                conn.execute('BEGIN IMMEDIATE')  # 竞争写锁，后者等前者提交
                try:
                    cas_adjust(conn, new_move_id(), 1, 1, 'STOCK', base_qty, ver, -1,
                               f'{name}领用', operator=name)
                    conn.execute('COMMIT')
                    result['ok'] = True
                except VersionConflict:
                    conn.execute('ROLLBACK')
                    result['ok'] = False
            except Exception as e:
                result['ok'] = False
                result['err'] = str(e)
                try:
                    conn.execute('ROLLBACK')
                except Exception:
                    pass
            finally:
                conn.close()

        ra, rb = {}, {}
        ta = threading.Thread(target=worker, args=('A', ra))
        tb = threading.Thread(target=worker, args=('B', rb))
        ta.start(); tb.start(); ta.join(); tb.join()
        self.assertNotIn('err', ra, ra)
        self.assertNotIn('err', rb, rb)
        outcomes = sorted([ra.get('ok'), rb.get('ok')])
        self.assertEqual(outcomes, [False, True])  # 一成一败，绝不超扣
        conn = dbmod.connect(self.db_path)
        self.assertEqual(balance(conn, 1, 1, 'STOCK')[0], 99)  # 只扣了一次
        conn.close()


class TestRoleSeparation(BaseCase):
    def test_associate_cannot_detach_or_approve(self):
        c = self.conn
        services.issue_tags(c, 1, 1, 1, 5, 1, ts=at(9))
        services.attach_batch(c, 1, 1, 1, 1, 3, 3, 1, ts=at(10))
        # 导购不能收银拆扣
        with self.assertRaises(PermissionDenied):
            services.pos_sale_detach(c, 1, 1, 1, [(1, 1, 1)], 1, ts=at(15))

    def test_cashier_cannot_issue(self):
        with self.assertRaises(PermissionDenied):
            services.issue_tags(self.conn, 1, 1, 1, 5, 3, ts=at(9))

    def test_cashier_cannot_approve_variance(self):
        c = self.conn
        c.execute("INSERT INTO close_counts (code,store_id,business_date,floor_id,tag_type_id,"
                  "issued_qty,attached_qty,detached_qty,loose_book,loose_actual,"
                  "on_shelf_book,on_shelf_actual,damaged_floor_qty,damaged_ongoods_qty,"
                  "chain_ok,variance_qty,status,created_ts) VALUES "
                  "('CLS-X',1,?,1,1,0,0,0,5,4,0,0,0,0,1,-1,'PENDING_VARIANCE',?)",
                  (self.bdate, at(22)))
        cc_id = c.execute("SELECT id FROM close_counts WHERE code='CLS-X'").fetchone()['id']
        vr = services.create_variance(c, cc_id, 'FLOOR', 1, 'SHORT', '测试', 1)
        with self.assertRaises(PermissionDenied):
            services.decide_variance(c, vr['id'], 'APPROVED', 3)

    def test_manager_cannot_issue(self):
        with self.assertRaises(PermissionDenied):
            services.issue_tags(self.conn, 1, 1, 1, 5, 4, ts=at(9))


class TestVarianceApproval(BaseCase):
    def _variance_row(self):
        c = self.conn
        # 领10，散扣账面10；实点9，盘亏1；在架无动作
        services.issue_tags(c, 1, 1, 1, 10, 1, ts=at(9))
        c.execute(
            """INSERT INTO count_inputs
               (store_id,business_date,floor_id,tag_type_id,loose_actual,on_shelf_actual)
               VALUES (1,?,1,1,9,NULL)""", (self.bdate,))
        res = services.run_close_count(c, 1, self.bdate)
        cc = [r for r in res if r['tag_type_id'] == 1][0]
        self.assertEqual(cc['variance_qty'], -1)
        return cc

    def test_apply_then_approve_adjusts_book(self):
        c = self.conn
        cc = self._variance_row()
        vr = services.create_variance(
            c, cc['id'], 'FLOOR', 1, 'SHORT', '散扣少1', 1, ts=at(22))
        self.assertEqual(vr['status'], 'PENDING')
        # 申请人本人不能审批（职责分离）
        with self.assertRaises(PermissionDenied):
            services.decide_variance(c, vr['id'], 'APPROVED', 1)
        # 店长批准后散扣账面 10->9，差异归零，闭店变平衡
        services.decide_variance(c, vr['id'], 'APPROVED', 4, note='批准盘亏')
        cc2 = c.execute('SELECT * FROM close_counts WHERE id=?', (cc['id'],)).fetchone()
        self.assertEqual(cc2['loose_book'], 9)
        self.assertEqual(cc2['variance_qty'], 0)
        self.assertEqual(cc2['status'], 'BALANCED')
        # 调账守恒：FLOOR 减1，ADJUST 增1，总量仍100
        self.assertEqual(self.totals(1), 100)

    def test_reject_keeps_book(self):
        c = self.conn
        cc = self._variance_row()
        vr = services.create_variance(c, cc['id'], 'FLOOR', 1, 'SHORT', '散扣少1', 1)
        services.decide_variance(c, vr['id'], 'REJECTED', 4, note='证据不足')
        vr2 = c.execute('SELECT * FROM variance_requests WHERE id=?', (vr['id'],)).fetchone()
        self.assertEqual(vr2['status'], 'REJECTED')
        self.assertEqual(balance(c, 1, 1, 'FLOOR', 1)[0], 10)  # 账面不变


class TestBusinessGuards(BaseCase):
    def test_tag_mismatch_does_not_move_stock(self):
        c = self.conn
        services.issue_tags(c, 1, 1, 2, 5, 1, ts=at(9))   # 领的是墨水扣
        before = self.totals(2)
        b, mid = services.attach_batch(c, 1, 1, 1, 2, 5, 5, 1, ts=at(10))  # 商品要硬扣
        self.assertEqual(b['status'], 'MISMATCH')
        self.assertIsNone(mid)
        self.assertEqual(self.totals(2), before)  # 不匹配不动库存

    def test_issue_shortage(self):
        c = self.conn
        # 硬扣总仓仅100，另造小库存场景：墨水扣只有50，申请80
        iss, _ = services.issue_tags(c, 1, 1, 2, 80, 1, ts=at(9))
        self.assertEqual(iss['status'], 'SHORT')
        self.assertEqual(iss['qty_issued'], 50)

    def test_cross_box_rejected(self):
        c = self.conn
        services.issue_tags(c, 1, 1, 1, 5, 1, ts=at(9))
        services.attach_batch(c, 1, 1, 1, 1, 3, 3, 1, ts=at(10))
        # 1号台投了2号台的盒子
        with self.assertRaises(ServiceError):
            services.pos_sale_detach(c, 1, 1, 3, [(1, 1, 1)], 2, ts=at(15))
        # 交易不应生成
        self.assertEqual(c.execute('SELECT COUNT(*) n FROM txns').fetchone()['n'], 0)

    def test_no_txn_alarm_must_escalate(self):
        c = self.conn
        alarm = services.ingest_alarm(c, 1, 'G9', at(20), tag_type_id=1)
        # 无交易不能按漏拆回收
        with self.assertRaises(ServiceError):
            services.resolve_alarm(c, alarm['id'], 'CONFIRMED_MISSED', 3)
        services.resolve_alarm(c, alarm['id'], 'NO_TXN_ESCALATE', 3, note='转防损')
        self.assertEqual(c.execute('SELECT status FROM alarms WHERE id=?',
                                   (alarm['id'],)).fetchone()['status'],
                         'NO_TXN_ESCALATED')


if __name__ == '__main__':
    unittest.main()
