"""进程内任务测试：闭店清点生成与差异提醒、巡检幂等。"""
import os
import sys
import tempfile
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tagguard.db import init_db
from tagguard.service import Service
from tagguard.seed import seed
from tagguard.scheduler import Scheduler


class TestScheduler(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = init_db(os.path.join(self.tmp.name, "t.db"))
        self.evd = os.path.join(self.tmp.name, "evidence")
        self.info = seed(self.conn, evidence_dir=self.evd)
        self.svc = Service(self.conn, self.evd)
        self.date = datetime.now().strftime("%Y-%m-%d")
        self.sch = Scheduler(self.svc, 1, manager_uid=5, interval_sec=3600)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_closing_generates_rows_and_reminders(self):
        rows, diffs = self.sch.trigger_closing(self.date, {("1F", 1): 37})
        self.assertTrue(rows)
        self.assertEqual(len(diffs), 1)
        # 闭店提醒与盘差提醒均已落库
        kinds = {r["title"] for r in self.conn.execute(
            "SELECT title FROM reminders")}
        self.assertTrue(any("闭店清点" in k for k in kinds))
        self.assertTrue(any("盘差待批" in k for k in kinds))

    def test_trigger_closing_idempotent(self):
        self.sch.trigger_closing(self.date, {("1F", 1): 37})
        n1 = self.conn.execute("SELECT COUNT(*) c FROM closing_counts").fetchone()["c"]
        self.sch.trigger_closing(self.date, {("1F", 1): 37})
        n2 = self.conn.execute("SELECT COUNT(*) c FROM closing_counts").fetchone()["c"]
        self.assertEqual(n1, n2, "同日重跑应 upsert 而非新增行")

    def test_patrol_lists_pending_items_and_is_idempotent(self):
        self.sch.trigger_closing(self.date, {("1F", 1): 37})
        items1 = self.sch.patrol()
        titles = {t for t, _ in items1}
        self.assertIn("出口报警待复核", titles)
        self.assertIn("回收盒串台待调正", titles)
        self.assertIn("盘差待店长审批", titles)
        before = self.conn.execute("SELECT COUNT(*) c FROM reminders").fetchone()["c"]
        self.sch.patrol()
        after = self.conn.execute("SELECT COUNT(*) c FROM reminders").fetchone()["c"]
        self.assertEqual(before, after, "巡检未读提醒应幂等不重复")


if __name__ == "__main__":
    unittest.main(verbosity=2)
