"""模拟适配器：POS 收银流水与出口门禁报警。

真实环境中这两个外部系统通过 HTTP/消息队列对接；此处用内置样例规则模拟，
返回的数据结构与真实适配器保持一致，Service 层无感知。
"""
from datetime import datetime, timedelta


class POSAdapter:
    """模拟收银 POS：按交易号返回收银成交记录。"""

    def __init__(self, conn):
        self.conn = conn

    def fetch_paid_transaction(self, txn_id):
        r = self.conn.execute(
            "SELECT t.*, p.name product_name FROM transactions t "
            "JOIN products p ON p.id=t.product_id WHERE t.id=?",
            (txn_id,)).fetchone()
        if not r:
            return None
        return {
            "txn_id": r["id"],
            "store_id": r["store_id"],
            "floor": r["floor"],
            "register_no": r["register_no"],
            "product": r["product_name"],
            "tag_type_id": r["tag_type_id"],
            "qty": r["qty"],
            "amount": r["amount"],
            "paid_at": r["paid_at"],
        }

    def list_recent(self, store_id, hours=24):
        rows = self.conn.execute(
            "SELECT t.*, p.name product_name FROM transactions t "
            "JOIN products p ON p.id=t.product_id WHERE t.store_id=? "
            "ORDER BY t.paid_at DESC", (store_id,)).fetchall()
        return [dict(r) for r in rows]

    def find_txn_for_gate(self, store_id, floor, tag_type_id, around_time):
        """门禁报警时反查：同楼层、同扣型、时间最近且未拆扣的交易。"""
        r = self.conn.execute(
            "SELECT t.* FROM transactions t LEFT JOIN detaches d ON d.txn_id=t.id "
            "WHERE t.store_id=? AND t.floor=? AND t.tag_type_id=? "
            "AND d.id IS NULL ORDER BY ABS(strftime('%s',t.paid_at)-"
            "strftime('%s',?)) LIMIT 1",
            (store_id, floor, tag_type_id, around_time)).fetchone()
        return r["id"] if r else None


class GateAdapter:
    """模拟出口门禁天线：返回报警事件（含一份伪凭证字节）。"""

    def __init__(self, service):
        self.service = service

    def fire(self, store_id, gate, floor, tag_type_id, pos: POSAdapter,
             note="", txn_id=None, evidence_text=None):
        at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if txn_id is None:
            # 自动反查可能的漏拆交易；查不到即为无交易报警
            txn_id = pos.find_txn_for_gate(store_id, floor, tag_type_id, at)
        if txn_id is None:
            note = (note + "；" if note else "") + "门禁未匹配到任何成交交易"
        evidence = None
        if evidence_text is not None or txn_id is not None:
            evidence = (evidence_text or
                        f"GATE ALERT {gate} @ {at}\nfloor={floor} "
                        f"tag_type={tag_type_id} txn={txn_id}\n").encode()
        return self.service.raise_alarm(
            store_id, gate, txn_id, tag_type_id,
            f"{gate}_{at.replace(':','').replace(' ','_')}.txt",
            evidence, note=note)


class DeactivatorAdapter:
    """模拟消磁器：设备主动上报硬件故障（区别于单笔消磁失败）。"""

    def __init__(self, conn):
        self.conn = conn

    def report_device_fault(self, store_id, register_no, uid, note="设备无响应"):
        from .service import now
        cur = self.conn.execute(
            "INSERT INTO deactivator_faults(store_id,register_no,kind,"
            "txn_id,qty,created_at,note) VALUES(?,?,?,?,?,?,?)",
            (store_id, register_no, "device_fault", None, 0, now(), note))
        self.conn.commit()
        return cur.lastrowid
