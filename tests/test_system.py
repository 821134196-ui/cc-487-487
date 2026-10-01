"""测试：数量守恒、重复事件、并发版本冲突、职责分离、盘差审批。

运行：python3 -m unittest discover -s tests -v
"""
import os
import sys
import tempfile
import threading
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tagguard.db import init_db
from tagguard.service import (Service, AuthError, DuplicateDetach,
                              VersionConflict, ShortageError, TagTypeMismatch,
                              BoxCrossError, DeauthFail, NoTransactionAlarm,
                              CountDiff, STOCKROOM)
from tagguard.seed import seed
from tagguard.adapters import GateAdapter, POSAdapter


# 人员：1 李导购 2 王导购 3 陈收银 4 周收银 5 林店长
CLERK, CLERK2, CASHIER, CASHIER2, MANAGER = 1, 2, 3, 4, 5


class CaseBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "t.db")
        self.evd = os.path.join(self.tmp.name, "evidence")
        self.conn = init_db(self.db)
        self.info = seed(self.conn, evidence_dir=self.evd)
        self.svc = Service(self.conn, self.evd)
        self.date = datetime.now().strftime("%Y-%m-%d")

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    # 便捷查询
    def q(self, sql, *a):
        return self.conn.execute(sql, a).fetchone()[0]

    def inv(self, floor, tid):
        return self.q("SELECT qty FROM inventory WHERE floor=? AND tag_type_id=?",
                      floor, tid)

    def make_txn(self, floor="1F", reg="1F-01", pid=1, ttype=1, qty=1,
                 hm="20:01:00"):
        """新建一条样例 POS 成交（默认未拆扣）。"""
        cur = self.conn.execute(
            "INSERT INTO transactions(store_id,floor,register_no,product_id,"
            "tag_type_id,qty,amount,paid_at,source) "
            "VALUES(1,?,?,?,?,?,?,?,'pos')",
            (floor, reg, pid, ttype, qty, 99.0, f"{self.date} {hm}"))
        self.conn.commit()
        return cur.lastrowid


class TestConservation(CaseBase):
    """数量守恒：全店扣具 来源 = 去向，流水可复核。"""

    def test_issue_equals_all_sinks(self):
        # 完整处理样例：确认漏拆、调正串台，再闭店
        self.svc.review_alarm(self.info["miss_alarm"], "1F-01", CASHIER,
                              "miss", "顾客返回")
        self.svc.resolve_box_cross(1, CASHIER)
        self.svc.run_closing(1, self.date, MANAGER)

        for tid, issued in ((1, 300), (2, 200), (3, 100)):
            stock_sum = self.q(
                "SELECT COALESCE(SUM(qty),0) FROM inventory WHERE tag_type_id=?", tid)
            attached = self.q(
                "SELECT COALESCE(SUM(attached_qty),0) FROM tag_batches WHERE tag_type_id=?", tid)
            detached = self.q(
                "SELECT COALESCE(SUM(qty),0) FROM detaches WHERE tag_type_id=? "
                "AND status IN('confirmed','box_mismatch')", tid)
            spare_dmg = self.q(
                "SELECT COALESCE(SUM(qty),0) FROM tag_damages WHERE tag_type_id=? "
                "AND on_shelf=0", tid)
            # 守恒式：入库 = 各处库存 + 在架(挂-拆) + 备用扣报损
            self.assertEqual(
                issued, stock_sum + attached - detached + spare_dmg,
                f"扣型 {tid} 数量不守恒")

    def test_ledger_rows_sum_to_inventory(self):
        """流水每条余额连续；按位置汇总变动=当前库存。"""
        for floor, tid in (("1F", 1), ("2F", 2), ("3F", 3), (STOCKROOM, 1)):
            rows = self.conn.execute(
                "SELECT change_qty,balance_after FROM stock_ledger "
                "WHERE floor=? AND tag_type_id=? ORDER BY id", (floor, tid)).fetchall()
            self.assertTrue(rows)
            running = 0
            for chg, bal in rows:
                running += chg
                self.assertEqual(running, bal, "流水结余不连续")
            self.assertEqual(running, self.inv(floor, tid),
                             "流水汇总与库存不一致")

    def test_no_negative_balance_ever(self):
        bal_min = self.q("SELECT MIN(balance_after) FROM stock_ledger")
        self.assertGreaterEqual(bal_min, 0)

    def test_closing_chain_balanced_for_clean_rows(self):
        """无注入差异时，除在架破损口径外的干净行平衡式成立。"""
        rows, _ = self.svc.run_closing(1, self.date, MANAGER)
        for r in rows:
            if r["attached"] == 0:
                self.assertTrue(r["balanced"])
            self.assertEqual(
                r["requisition"] + r["detached"],
                r["attached"] + r["floor_left"] + r["spare_damaged"],
                f'{r["floor"]}/t{r["tag_type_id"]} 领用数量链不守恒')


class TestDuplicateEvents(CaseBase):
    """重复事件：同交易拆扣只确认一次；报警只关闭一次。"""

    def test_duplicate_detach_blocked(self):
        txn = self.make_txn()
        self.svc.detach(txn, "1F-01", CASHIER)
        with self.assertRaises(DuplicateDetach):
            self.svc.detach(txn, "1F-01", CASHIER2)

    def test_alarm_confirm_once(self):
        aid = self.info["miss_alarm"]
        self.svc.review_alarm(aid, "1F-01", CASHIER, "miss", "漏拆")
        # 数据库约束：detaches 中该交易仅一条
        n = self.q("SELECT COUNT(*) FROM detaches WHERE txn_id=?",
                   self.info["txn_miss"])
        self.assertEqual(n, 1)
        with self.assertRaises(DuplicateDetach):
            self.svc.review_alarm(aid, "1F-01", CASHIER2, "miss")
        # 报警状态已是终态
        st = self.q("SELECT status FROM exit_alarms WHERE id=?", aid)
        self.assertEqual(st, "confirmed_miss")

    def test_alarm_after_normal_detach_cannot_collect_again(self):
        """已正常拆扣的交易，即使有报警也不能再回收。"""
        txn = self.make_txn()
        self.svc.detach(txn, "1F-01", CASHIER)
        aid = self.svc.raise_alarm(1, "东门", txn, 1, "x.txt", b"x")
        with self.assertRaises(DuplicateDetach):
            self.svc.review_alarm(aid, "1F-01", CASHIER2, "miss")

    def test_box_cross_resolve_once(self):
        # seed 已为 t2 生成串台事件 #1（2F-01 → 2F-02，2 枚）
        def box(reg):
            r = self.conn.execute(
                "SELECT qty FROM recycle_boxes WHERE register_no=?",
                (reg,)).fetchone()
            return r["qty"] if r else 0
        self.assertEqual((box("2F-02"), box("2F-01")), (2, 0))
        self.svc.resolve_box_cross(1, CASHIER)
        with self.assertRaises(Exception):
            self.svc.resolve_box_cross(1, CASHIER)
        self.assertEqual((box("2F-02"), box("2F-01")), (0, 2))

    def test_deauth_fail_records_and_blocks(self):
        # 样例 t3 消磁失败已登记
        n = self.q("SELECT COUNT(*) FROM deactivator_faults WHERE txn_id=?",
                   self.info["txn_miss"])
        self.assertEqual(n, 1)
        # 该交易没有拆扣记录
        self.assertEqual(self.q(
            "SELECT COUNT(*) FROM detaches WHERE txn_id=?", self.info["txn_miss"]), 0)

    def test_no_txn_alarm_cannot_be_collected(self):
        # 样例报警 #2 是无交易报警
        with self.assertRaises(NoTransactionAlarm):
            self.svc.review_alarm(2, "1F-01", CASHIER, "miss")
        # 走专门的核查关闭流程后即终态
        self.svc.handle_no_txn_alarm(2, MANAGER, "设备误报")
        with self.assertRaises(DuplicateDetach):
            self.svc.handle_no_txn_alarm(2, MANAGER, "再来一次")


class TestConcurrency(CaseBase):
    """两人并发调整同一库存：version 乐观锁只允许一人成功。"""

    def test_version_conflict_on_concurrent_adjust(self):
        v, qty = self.svc.get_inventory_version(1, "1F", 1)
        barrier = threading.Barrier(2)
        results = []

        def adjust(delta):
            try:
                barrier.wait(timeout=5)
                # 两人持同一版本 v 同时提交
                self.svc.adjust_qty_versioned(
                    1, "1F", 1, delta, "adjust", None, MANAGER, v)
                results.append(("ok", delta))
            except VersionConflict as e:
                results.append(("conflict", str(e)))
            except Exception as e:  # noqa
                results.append(("other", repr(e)))

        t1 = threading.Thread(target=adjust, args=(5,))
        t2 = threading.Thread(target=adjust, args=(3,))
        t1.start(); t2.start(); t1.join(5); t2.join(5)
        statuses = sorted(r[0] for r in results)
        self.assertEqual(statuses, ["conflict", "ok"], results)
        # 只有 +5 或 +3 之一落账
        self.assertIn(self.inv("1F", 1), (qty + 5, qty + 3))
        # 版本只增加一次
        v2 = self.q("SELECT version FROM inventory WHERE floor='1F' AND tag_type_id=1")
        self.assertEqual(v2, v + 1)

    def test_sequential_version_bump(self):
        """读到新版本后再次调整应成功，版本继续递增。"""
        v1, _ = self.svc.get_inventory_version(1, "1F", 1)
        self.svc.adjust_qty_versioned(1, "1F", 1, 1, "adjust", None, MANAGER, v1)
        v2, _ = self.svc.get_inventory_version(1, "1F", 1)
        self.assertEqual(v2, v1 + 1)
        self.svc.adjust_qty_versioned(1, "1F", 1, 1, "adjust", None, MANAGER, v2)
        v3, _ = self.svc.get_inventory_version(1, "1F", 1)
        self.assertEqual(v3, v2 + 1)


class TestRoleSeparation(CaseBase):
    """职责分离：导购领用/挂扣、收银拆扣/复核、店长批盘差。"""

    def test_clerk_can_requisition_and_attach(self):
        rid = self.svc.requisition(1, "3F", 1, 5, CLERK)
        self.assertTrue(rid)
        bid = self.svc.attach_batch(1, "3F", 1, 1, 5, 5, CLERK)
        self.assertTrue(bid)

    def test_cashier_cannot_requisition(self):
        with self.assertRaises(AuthError):
            self.svc.requisition(1, "3F", 1, 5, CASHIER)

    def test_clerk_cannot_detach(self):
        with self.assertRaises(AuthError):
            self.svc.detach(self.info["txn_ok"][0], "1F-01", CLERK)

    def test_clerk_and_cashier_cannot_approve_adjustment(self):
        self.svc.run_closing(1, self.date, MANAGER, {("1F", 1): 37})
        adj = self.q("SELECT id FROM inventory_adjustments LIMIT 1")
        with self.assertRaises(AuthError):
            self.svc.approve_adjustment(adj, CLERK, True)
        with self.assertRaises(AuthError):
            self.svc.approve_adjustment(adj, CASHIER, True)

    def test_only_manager_runs_closing(self):
        with self.assertRaises(AuthError):
            self.svc.run_closing(1, self.date, CLERK)
        with self.assertRaises(AuthError):
            self.svc.run_closing(1, self.date, CASHIER)


class TestAdjustmentApproval(CaseBase):
    """盘差审批：生成、店长批准调账、驳回不动账、不可重复审批。"""

    def setUp(self):
        super().setUp()
        # 1F 墨黑硬扣：期望在架 39，注入实盘 37，盘亏 2
        self.rows, self.diffs = self.svc.run_closing(
            1, self.date, MANAGER, {("1F", 1): 37})

    def test_diff_adjustment_created_pending(self):
        self.assertEqual(len(self.diffs), 1)
        d = self.diffs[0]
        self.assertEqual((d["floor"], d["tag_type_id"], d["diff"]), ("1F", 1, -2))
        st = self.q("SELECT status FROM inventory_adjustments WHERE id=?",
                    d["closing_id"])
        self.assertEqual(st, "pending")

    def test_approve_changes_stock_and_balances(self):
        adj = self.q("SELECT id FROM inventory_adjustments WHERE floor='1F' "
                     "AND tag_type_id=1 AND status='pending'")
        before = self.inv("1F", 1)
        self.svc.approve_adjustment(adj, MANAGER, True, "实盘核准")
        self.assertEqual(self.inv("1F", 1), before - 2)  # 盘亏出账
        # 流水留痕
        self.assertEqual(self.q(
            "SELECT change_qty FROM stock_ledger WHERE ref_type='adjust' "
            "AND ref_id=?", adj), -2)
        # 闭店行转平衡
        balanced = self.q(
            "SELECT balanced FROM closing_counts WHERE floor='1F' AND tag_type_id=1"
            " AND business_date=?", self.date)
        self.assertEqual(balanced, 1)
        self.assertEqual(self.q(
            "SELECT status FROM inventory_adjustments WHERE id=?", adj), "approved")

    def test_reject_leaves_stock_unchanged(self):
        adj = self.q("SELECT id FROM inventory_adjustments WHERE floor='1F' "
                     "AND tag_type_id=1 AND status='pending'")
        before = self.inv("1F", 1)
        self.svc.approve_adjustment(adj, MANAGER, False, "要求复盘")
        self.assertEqual(self.inv("1F", 1), before)
        self.assertEqual(self.q(
            "SELECT status FROM inventory_adjustments WHERE id=?", adj), "rejected")

    def test_double_approval_blocked(self):
        adj = self.q("SELECT id FROM inventory_adjustments WHERE floor='1F' "
                     "AND tag_type_id=1 AND status='pending'")
        self.svc.approve_adjustment(adj, MANAGER, True)
        with self.assertRaises(Exception):
            self.svc.approve_adjustment(adj, MANAGER, True)


class TestOtherScenarios(CaseBase):
    """其余异常分支：扣型不匹配、领用短缺、破损。"""

    def test_tag_type_mismatch_requires_reason(self):
        with self.assertRaises(TagTypeMismatch):
            # 商品 1 要求硬扣(t1)，改用墨水扣(t2) 且不填原因
            self.svc.attach_batch(1, "2F", 1, 2, 5, 5, CLERK)
        # 填了原因可登记并打 mismatch 标记（2F 有墨水扣库存）
        bid = self.svc.attach_batch(1, "2F", 1, 2, 5, 5, CLERK,
                                    note="硬扣缺货，店长同意临时改用")
        self.assertEqual(self.q("SELECT mismatch FROM tag_batches WHERE id=?", bid), 1)

    def test_requisition_shortage_registered(self):
        before = self.q("SELECT COUNT(*) FROM requisitions WHERE status='short'")
        with self.assertRaises(ShortageError):
            # 3F 墨水扣仓库只有 200-60=140，领 500 必短缺
            self.svc.requisition(1, "3F", 2, 500, CLERK)
        self.assertEqual(
            self.q("SELECT COUNT(*) FROM requisitions WHERE status='short'"),
            before + 1)
        # 短缺不动库存
        self.assertEqual(self.inv(STOCKROOM, 2), 140)

    def test_damage_requires_reason(self):
        from tagguard.service import TagBroken
        with self.assertRaises(TagBroken):
            self.svc.damage(1, "2F", 2, 1, "", CLERK)

    def test_on_shelf_damage_enters_closing_chain(self):
        # 样例 3F 磁扣在架破损 1 枚：挂10=拆1+架8+损1
        rows, _ = self.svc.run_closing(1, self.date, MANAGER)
        r = next(r for r in rows if r["floor"] == "3F" and r["tag_type_id"] == 3)
        self.assertEqual((r["attached"], r["detached"], r["on_shelf"], r["damaged"]),
                         (10, 1, 8, 1))
        self.assertTrue(r["balanced"])


class TestEvidenceAndAdapters(CaseBase):
    """适配器与凭证：门禁适配器写本地凭证；POS 反查漏拆交易。"""

    def test_gate_adapter_writes_evidence(self):
        files = sorted(os.listdir(self.evd))
        self.assertTrue(any(f.startswith("alarm_") for f in files))
        # 样例漏拆报警带凭证路径
        p = self.q("SELECT evidence_path FROM exit_alarms WHERE id=?",
                   self.info["miss_alarm"])
        self.assertTrue(p and os.path.exists(p))

    def test_pos_adapter_finds_undetached_txn(self):
        pos = POSAdapter(self.conn)
        found = pos.find_txn_for_gate(1, "1F", 3, datetime.now().strftime(
            "%Y-%m-%d %H:%M:%S"))
        self.assertEqual(found, self.info["txn_miss"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
