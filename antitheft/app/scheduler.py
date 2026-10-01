# -*- coding: utf-8 -*-
"""进程内定时任务：定时生成闭店清点，并对未处理差异/未复核报警发出提醒。

使用 threading 守护线程实现，无需外部调度器；任务运行记录落 task_runs 表。
"""
import threading
import time
from datetime import datetime

from . import db as dbmod
from . import services


class TaskScheduler:
    def __init__(self, db_path, store_id=1, interval_sec=60,
                 close_hour=22, run_immediately=False):
        self.db_path = db_path
        self.store_id = store_id
        self.interval = interval_sec
        self.close_hour = close_hour
        self._stop = threading.Event()
        self._thread = None
        self._ran_dates = set()
        self.run_immediately = run_immediately

    def start(self):
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name='antitheft-scheduler')
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _conn(self):
        return dbmod.connect(self.db_path)

    def _today(self):
        return datetime.now().date().isoformat()

    def run_close_if_due(self, force=False):
        """到闭店点且当日尚未生成则清点；force 供演示/测试立即执行。"""
        conn = self._conn()
        try:
            conn.execute('BEGIN IMMEDIATE')
            bdate = self._today()
            due = force or (datetime.now().hour >= self.close_hour
                            and bdate not in self._ran_dates)
            existed = conn.execute(
                'SELECT COUNT(*) c FROM close_counts WHERE store_id=? AND business_date=?',
                (self.store_id, bdate)).fetchone()['c']
            if not due:
                return None
            results = services.run_close_count(conn, self.store_id, bdate)
            pending = [r for r in results if r['status'] == 'PENDING_VARIANCE']
            msg = (f'闭店清点完成：{len(results)} 条数量链，{len(pending)} 条存在差异/不平衡')
            conn.execute(
                """INSERT INTO task_runs (ts, task_type, status, message, payload)
                   VALUES (?,?, 'DONE', ?,?)""",
                (datetime.now().isoformat(timespec='seconds'), 'CLOSE_COUNT', msg,
                 f'{{"date":"{bdate}","rows":{len(results)},"pending":{len(pending)}}}'))
            self._ran_dates.add(bdate)
            conn.execute('COMMIT')
            return results
        except Exception:
            conn.execute('ROLLBACK')
            raise
        finally:
            conn.close()

    def remind(self):
        """差异提醒：待批盘差单、未复核报警、未处理异常各计数。"""
        conn = self._conn()
        try:
            v = conn.execute(
                "SELECT COUNT(*) c FROM variance_requests WHERE status='PENDING'"
            ).fetchone()['c']
            a = conn.execute(
                "SELECT COUNT(*) c FROM alarms WHERE status='OPEN'"
            ).fetchone()['c']
            f = conn.execute(
                "SELECT COUNT(*) c FROM deactivator_faults WHERE status='OPEN'"
            ).fetchone()['c']
            parts = []
            if v:
                parts.append(f'{v} 张盘差单待店长批准')
            if a:
                parts.append(f'{a} 起出口报警待收银复核')
            if f:
                parts.append(f'{f} 台消磁器故障未恢复')
            if not parts:
                return None
            msg = '差异提醒：' + '；'.join(parts)
            conn.execute(
                "INSERT INTO task_runs (ts, task_type, status, message) VALUES (?,?, 'REMIND',?)",
                (datetime.now().isoformat(timespec='seconds'), 'VARIANCE_REMIND', msg))
            return msg
        finally:
            conn.close()

    def _loop(self):
        if self.run_immediately:
            try:
                self.run_close_if_due(force=True)
                self.remind()
            except Exception as exc:  # 守护任务不能把线程跑崩
                print(f'[scheduler] 首次任务失败：{exc}')
        while not self._stop.wait(self.interval):
            try:
                self.run_close_if_due()
                self.remind()
            except Exception as exc:
                print(f'[scheduler] 定时任务失败：{exc}')
