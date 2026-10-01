"""进程内定时任务（后台守护线程）。

两个职责：
1. 闭店清点：每日到点（默认 21:50）按营业日聚合 领用→挂扣→拆回→在架→报损，
   不平衡时生成盘差提醒与待审批单；
2. 巡检提醒：周期性扫描未关闭报警、未调正串台、待处理消磁故障、待批盘差。

无外部依赖（不用 cron），随应用进程启动与退出。
"""
import threading
from datetime import datetime


class Scheduler:
    def __init__(self, service, store_id, manager_uid,
                 closing_time="21:50", interval_sec=60,
                 closing_overrides=None, run_closing_at_start=False):
        self.service = service
        self.conn = service.conn
        self.store_id = store_id
        self.manager_uid = manager_uid
        self.closing_time = closing_time
        self.interval = interval_sec
        self.closing_overrides = closing_overrides  # 演示用：注入实盘数制造盘差
        self.run_closing_at_start = run_closing_at_start
        self._stop = threading.Event()
        self._thread = None
        self._last_closing_date = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="tagguard-scheduler")
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _today(self):
        return datetime.now().strftime("%Y-%m-%d")

    def _loop(self):
        if self.run_closing_at_start:
            self.trigger_closing()
        while not self._stop.wait(self.interval):
            try:
                self.patrol()
                hm = datetime.now().strftime("%H:%M")
                today = self._today()
                if hm >= self.closing_time and self._last_closing_date != today:
                    self.trigger_closing()
            except Exception as exc:  # 巡检异常不能打死线程
                print(f"[scheduler] 任务异常: {exc}")

    # ---------- 闭店清点 ----------
    def trigger_closing(self, business_date=None, overrides=None):
        business_date = business_date or self._today()
        ov = overrides if overrides is not None else self.closing_overrides
        rows, diffs = self.service.run_closing(
            self.store_id, business_date, self.manager_uid, ov)
        self._last_closing_date = business_date
        unbalanced = [r for r in rows if not r["balanced"]]
        with self.conn:
            self.conn.execute(
                "INSERT INTO reminders(store_id,kind,title,body,created_at)"
                " VALUES(?,?,?,?,?)",
                (self.store_id, "closing", f"闭店清点已生成（{business_date}）",
                 f"{len(rows)} 个楼层×扣型组合，平衡 {len(rows)-len(unbalanced)}，"
                 f"不平衡 {len(unbalanced)}",
                 datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        return rows, diffs

    # ---------- 巡检提醒 ----------
    def patrol(self):
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        items = []
        for r in self.conn.execute(
                "SELECT gate,id,fired_at FROM exit_alarms WHERE store_id=? "
                "AND status IN ('open','no_txn')", (self.store_id,)):
            items.append(("出口报警待复核",
                          f"#{r['id']} {r['gate']} 触发于 {r['fired_at']}"))
        for r in self.conn.execute(
                "SELECT id,expected_register,actual_register FROM box_cross_events "
                "WHERE store_id=? AND resolved=0", (self.store_id,)):
            items.append(("回收盒串台待调正",
                          f"事件 #{r['id']}：{r['expected_register']} 号台扣具"
                          f"误入 {r['actual_register']} 号盒"))
        for r in self.conn.execute(
                "SELECT id,register_no,kind FROM deactivator_faults "
                "WHERE store_id=? AND handled=0", (self.store_id,)):
            label = "消磁失败" if r["kind"] == "deauth_fail" else "设备故障"
            items.append((f"{label}待处理", f"故障单 #{r['id']} @ {r['register_no']}号台"))
        for r in self.conn.execute(
                "SELECT id,floor,diff FROM inventory_adjustments "
                "WHERE store_id=? AND status='pending'", (self.store_id,)):
            items.append(("盘差待店长审批",
                          f"审批单 #{r['id']} {r['floor']} 差异 {r['diff']:+d}"))
        # 幂等：同标题已有未读提醒则不重复发
        with self.conn:
            for title, body in items:
                exists = self.conn.execute(
                    "SELECT 1 FROM reminders WHERE store_id=? AND title=? "
                    "AND body=? AND read=0 LIMIT 1",
                    (self.store_id, title, body)).fetchone()
                if not exists:
                    self.conn.execute(
                        "INSERT INTO reminders(store_id,kind,title,body,created_at)"
                        " VALUES(?,?,?,?,?)",
                        (self.store_id, "diff", title, body, ts))
        return items
