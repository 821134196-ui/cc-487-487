"""领域服务：防盗扣领用、挂扣、拆扣、报警复核、报损、闭店平衡、盘差审批。

数量守恒口径（按 门店×楼层×扣型×营业日）：
    期望在架 = 挂扣 - 拆回 - 报损
    盘点差异 = 实盘在架 - 期望在架
    平衡链   = 领用 → 挂扣 →（在架 + 拆回 + 报损）
库存移动全部记 stock_ledger 数量流水，库存更新带 version 乐观锁。
"""
from datetime import datetime
import os

STOCKROOM = "仓库"


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------- 业务异常（八类分别处理） ----------------
class DomainError(Exception):
    code = "domain"


class AuthError(DomainError):
    code = "forbidden"          # 职责分离


class TagTypeMismatch(DomainError):
    code = "tag_mismatch"       # 扣型不匹配


class ShortageError(DomainError):
    code = "shortage"           # 领用短缺


class BoxCrossError(DomainError):
    code = "box_cross"          # 回收盒串台


class DuplicateDetach(DomainError):
    code = "duplicate"          # 重复拆扣


class DeauthFail(DomainError):
    code = "deauth_fail"        # 消磁失败


class NoTransactionAlarm(DomainError):
    code = "no_txn"             # 无交易报警


class TagBroken(DomainError):
    code = "broken"             # 扣具破损


class CountDiff(DomainError):
    code = "count_diff"         # 盘点差异


class VersionConflict(DomainError):
    code = "version_conflict"   # 并发版本冲突


ROLE_LABEL = {"clerk": "导购", "cashier": "收银", "manager": "店长"}


class Service:
    def __init__(self, conn, evidence_dir="data/evidence"):
        self.conn = conn
        self.evidence_dir = evidence_dir
        os.makedirs(evidence_dir, exist_ok=True)

    # ---------- 基础 ----------
    def _user(self, uid):
        r = self.conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        if not r:
            raise AuthError("用户不存在")
        return r

    def require_role(self, uid, *roles):
        u = self._user(uid)
        if u["role"] not in roles:
            need = "/".join(ROLE_LABEL[r] for r in roles)
            raise AuthError(f"{u['name']} 是{ROLE_LABEL[u['role']]}，该操作需要 {need}")
        return u

    def _tag_type(self, tid):
        r = self.conn.execute("SELECT * FROM tag_types WHERE id=?", (tid,)).fetchone()
        if not r:
            raise DomainError("扣型不存在")
        return r

    def _ensure_inv(self, store_id, floor, tag_type_id):
        r = self.conn.execute(
            "SELECT * FROM inventory WHERE store_id=? AND floor=? AND tag_type_id=?",
            (store_id, floor, tag_type_id)).fetchone()
        if r:
            return r
        self.conn.execute(
            "INSERT INTO inventory(store_id,floor,tag_type_id,qty,version) VALUES(?,?,?,0,1)",
            (store_id, floor, tag_type_id))
        return self.conn.execute(
            "SELECT * FROM inventory WHERE store_id=? AND floor=? AND tag_type_id=?",
            (store_id, floor, tag_type_id)).fetchone()

    def _apply_qty(self, store_id, floor, tag_type_id, delta, ref_type, ref_id, uid):
        """带 version 乐观锁的库存增减，写一条流水。调用方须在 tx() 内。"""
        row = self._ensure_inv(store_id, floor, tag_type_id)
        new_qty = row["qty"] + delta
        if new_qty < 0:
            raise ShortageError(
                f"库存不足：{floor} 该扣型现有 {row['qty']}，需要 {-delta}")
        cur = self.conn.execute(
            "UPDATE inventory SET qty=?, version=version+1 "
            "WHERE id=? AND version=?",
            (new_qty, row["id"], row["version"]))
        if cur.rowcount == 0:
            raise VersionConflict("库存已被他人调整，请刷新后重试（版本冲突）")
        self.conn.execute(
            "INSERT INTO stock_ledger(store_id,floor,tag_type_id,change_qty,"
            "balance_after,ref_type,ref_id,created_by,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (store_id, floor, tag_type_id, delta, new_qty,
             ref_type, ref_id, uid, now()))
        return new_qty

    def adjust_qty_versioned(self, store_id, floor, tag_type_id, delta,
                              ref_type, ref_id, uid, expected_version):
        """显式版本校验入口：两人并发调整时后提交者 VersionConflict。"""
        from .db import tx
        with tx(self.conn):
            row = self._ensure_inv(store_id, floor, tag_type_id)
            new_qty = row["qty"] + delta
            if new_qty < 0:
                raise ShortageError(f"库存不足：现有 {row['qty']}，需要 {-delta}")
            cur = self.conn.execute(
                "UPDATE inventory SET qty=?, version=version+1 "
                "WHERE id=? AND version=?",
                (new_qty, row["id"], expected_version))
            if cur.rowcount == 0:
                cur2 = self.conn.execute(
                    "SELECT version FROM inventory WHERE id=?", (row["id"],)).fetchone()
                raise VersionConflict(
                    f"版本过期：你持有的 v{expected_version}，当前 v{cur2['version']}")
            self.conn.execute(
                "INSERT INTO stock_ledger(store_id,floor,tag_type_id,change_qty,"
                "balance_after,ref_type,ref_id,created_by,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (store_id, floor, tag_type_id, delta, new_qty,
                 ref_type, ref_id, uid, now()))
            return new_qty

    def get_inventory_version(self, store_id, floor, tag_type_id):
        r = self._ensure_inv(store_id, floor, tag_type_id)
        self.conn.commit()
        return r["version"], r["qty"]

    # ---------- 到货入库 ----------
    def issue_tags(self, store_id, tag_type_id, qty, uid):
        """扣具到货，入仓库库存。"""
        if qty <= 0:
            raise DomainError("入库数量必须为正")
        self._tag_type(tag_type_id)
        self.require_role(uid, "manager", "clerk")
        from .db import tx
        with tx(self.conn):
            self._ensure_inv(store_id, STOCKROOM, tag_type_id)
            bal = self._apply_qty(store_id, STOCKROOM, tag_type_id, qty,
                                  "issue", None, uid)
        return bal

    # ---------- 楼层领用（导购） ----------
    def requisition(self, store_id, floor, tag_type_id, qty, uid, note=""):
        self.require_role(uid, "clerk", "manager")
        if qty <= 0:
            raise DomainError("领用数量必须为正")
        self._tag_type(tag_type_id)
        from .db import tx
        pending_error = None
        rid = None
        with tx(self.conn):
            stock = self._ensure_inv(store_id, STOCKROOM, tag_type_id)
            if stock["qty"] < qty:
                # 领用短缺：登记短缺单，不移动库存，等待补货
                cur = self.conn.execute(
                    "INSERT INTO requisitions(store_id,floor,tag_type_id,qty,"
                    "status,note,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (store_id, floor, tag_type_id, qty, "short",
                     f"仓库仅 {stock['qty']}，短缺 {qty - stock['qty']}。{note}",
                     uid, now()))
                rid = cur.lastrowid
                pending_error = ShortageError(
                    f"领用短缺：申请 {qty}，仓库仅 {stock['qty']}，"
                    f"短缺 {qty - stock['qty']}（单号 #{rid}）")
            else:
                rid = self.conn.execute(
                    "INSERT INTO requisitions(store_id,floor,tag_type_id,qty,"
                    "status,note,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (store_id, floor, tag_type_id, qty, "active", note, uid, now())
                ).lastrowid
                self._apply_qty(store_id, STOCKROOM, tag_type_id, -qty,
                                "requisition", rid, uid)
                self._apply_qty(store_id, floor, tag_type_id, +qty,
                                "requisition", rid, uid)
        if pending_error:
            raise pending_error
        return rid

    # ---------- 商品挂扣批次 ----------
    def attach_batch(self, store_id, floor, product_id, tag_type_id,
                     required_qty, attached_qty, uid, note=""):
        self.require_role(uid, "clerk", "manager")
        from .db import tx
        with tx(self.conn):
            p = self.conn.execute("SELECT * FROM products WHERE id=?",
                                  (product_id,)).fetchone()
            if not p:
                raise DomainError("商品不存在")
            mismatch = 1 if tag_type_id != p["required_tag_type_id"] else 0
            if attached_qty > required_qty:
                raise DomainError("实挂不能超过应挂数量")
            # 扣型不匹配：仍允许登记，但必须注明原因，批次打 mismatch 标记
            if mismatch and not note:
                req = self._tag_type(p["required_tag_type_id"])
                raise TagTypeMismatch(
                    f"扣型不匹配：{p['name']} 要求 {req['name']}，"
                    f"请填写改用原因后再提交")
            floor_stock = self._ensure_inv(store_id, floor, tag_type_id)
            if floor_stock["qty"] < attached_qty:
                raise ShortageError(
                    f"楼层该扣型仅 {floor_stock['qty']}，无法挂 {attached_qty}")
            bid = self.conn.execute(
                "INSERT INTO tag_batches(store_id,floor,product_id,tag_type_id,"
                "required_qty,attached_qty,mismatch,created_by,created_at,status)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (store_id, floor, product_id, tag_type_id, required_qty,
                 attached_qty, mismatch, uid, now(),
                 "closed" if attached_qty == required_qty else "open")
            ).lastrowid
            self._apply_qty(store_id, floor, tag_type_id, -attached_qty,
                            "attach", bid, uid)
            if attached_qty < required_qty:
                # 应挂未挂：登记为挂扣短缺（沿用批次 open 状态跟踪）
                note += f"；应挂 {required_qty} 实挂 {attached_qty}，待补挂 {required_qty - attached_qty}"
                self.conn.execute("UPDATE tag_batches SET note=? WHERE id=?",
                                  ("挂扣短缺。" + note.strip("；"), bid))
        return bid

    # ---------- 回收盒 ----------
    def ensure_box(self, store_id, register_no):
        r = self.conn.execute(
            "SELECT * FROM recycle_boxes WHERE store_id=? AND register_no=?",
            (store_id, register_no)).fetchone()
        if not r:
            self.conn.execute(
                "INSERT INTO recycle_boxes(store_id,register_no,qty,updated_at)"
                " VALUES(?,?,0,?)", (store_id, register_no, now()))
            self.conn.commit()
        return self.conn.execute(
            "SELECT * FROM recycle_boxes WHERE store_id=? AND register_no=?",
            (store_id, register_no)).fetchone()

    # ---------- 收银拆扣（同一交易只确认一次） ----------
    def detach(self, txn_id, box_register, uid, deauth_ok=True):
        """收银成交后拆扣回收。box_register=扣具实际投入的盒子所属台。

        - 同交易重复拆扣 -> DuplicateDetach（UNIQUE 兜底）
        - 投错盒（串台）-> 登记串台事件，数量先进实际盒，待调正
        - 消磁失败 -> DeauthFail，不回收，等待出口报警漏拆流程
        """
        self.require_role(uid, "cashier", "manager")
        from .db import tx
        pending_error = None
        with tx(self.conn):
            t = self.conn.execute("SELECT * FROM transactions WHERE id=?",
                                  (txn_id,)).fetchone()
            if not t:
                raise DomainError("交易不存在")
            dup = self.conn.execute(
                "SELECT id FROM detaches WHERE txn_id=?", (txn_id,)).fetchone()
            if dup:
                raise DuplicateDetach(f"交易 #{txn_id} 的拆扣已确认，禁止重复拆扣")
            existing_fault = self.conn.execute(
                "SELECT id FROM deactivator_faults WHERE txn_id=? AND handled=0",
                (txn_id,)).fetchone()
            if existing_fault and not deauth_ok:
                raise DeauthFail(
                    f"交易 #{txn_id} 已登记消磁失败（故障单 #{existing_fault['id']}），"
                    f"等待出口报警漏拆流程，无需重复登记")
            if not deauth_ok:
                fid = self.conn.execute(
                    "INSERT INTO deactivator_faults(store_id,register_no,kind,"
                    "txn_id,qty,created_at,note) VALUES(?,?,?,?,?,?,?)",
                    (t["store_id"], t["register_no"], "deauth_fail",
                     txn_id, t["qty"], now(),
                     "消磁失败，扣具未拆回，商品按成交放行，预期触发出口报警")
                ).lastrowid
                pending_error = DeauthFail(
                    f"消磁失败（故障单 #{fid}），扣具未回收，已拦截拆扣")
            else:
                cross = box_register != t["register_no"]
                status = "box_mismatch" if cross else "confirmed"
                did = self.conn.execute(
                    "INSERT INTO detaches(txn_id,store_id,floor,register_no,"
                    "tag_type_id,box_id,qty,status,cashier_id,confirmed_at,note)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (txn_id, t["store_id"], t["floor"], t["register_no"],
                     t["tag_type_id"], None, t["qty"], status, uid, now(),
                     f"实际投入 {box_register} 号台回收盒（串台）" if cross else "")
                ).lastrowid
                box = self.ensure_box(t["store_id"], box_register)
                self.conn.execute(
                    "UPDATE recycle_boxes SET qty=qty+?, updated_at=? WHERE id=?",
                    (t["qty"], now(), box["id"]))
                self.conn.execute("UPDATE detaches SET box_id=? WHERE id=?",
                                  (box["id"], did))
                # 拆回入楼层库存，保持全店数量守恒
                self._apply_qty(t["store_id"], t["floor"], t["tag_type_id"],
                                t["qty"], "detach", did, uid)
                if cross:
                    cid = self.conn.execute(
                        "INSERT INTO box_cross_events(detach_id,store_id,"
                        "expected_register,actual_register,tag_type_id,qty,created_at)"
                        " VALUES(?,?,?,?,?,?,?)",
                        (did, t["store_id"], t["register_no"], box_register,
                         t["tag_type_id"], t["qty"], now())
                    ).lastrowid
                    self.conn.execute("UPDATE detaches SET note=? WHERE id=?",
                                      (f"串台事件 #{cid}，待将数量调回 {t['register_no']} 号盒", did))
                    pending_error = BoxCrossError(
                        f"回收盒串台：应投 {t['register_no']} 号盒，实际投入 {box_register} 号盒"
                        f"（事件 #{cid}，数量已暂记，待调正）")
        # 事务提交后再抛业务异常，保证故障/串台记录已落库
        if pending_error:
            raise pending_error
        return did

    def resolve_box_cross(self, cross_id, uid):
        """店长/收银主管调正串台：数量从实际盒移回应属盒。"""
        self.require_role(uid, "cashier", "manager")
        from .db import tx
        with tx(self.conn):
            e = self.conn.execute("SELECT * FROM box_cross_events WHERE id=?",
                                  (cross_id,)).fetchone()
            if not e:
                raise DomainError("串台事件不存在")
            if e["resolved"]:
                raise DomainError("该串台事件已调正")
            actual = self.ensure_box(e["store_id"], e["actual_register"])
            expect = self.ensure_box(e["store_id"], e["expected_register"])
            self.conn.execute(
                "UPDATE recycle_boxes SET qty=qty-?, updated_at=? WHERE id=?",
                (e["qty"], now(), actual["id"]))
            self.conn.execute(
                "UPDATE recycle_boxes SET qty=qty+?, updated_at=? WHERE id=?",
                (e["qty"], now(), expect["id"]))
            self.conn.execute(
                "UPDATE box_cross_events SET resolved=1, resolved_at=? WHERE id=?",
                (now(), cross_id))
            self.conn.execute(
                "UPDATE detaches SET status='confirmed', note=? WHERE id=?",
                (f"串台 #{cross_id} 已调正", e["detach_id"]))

    # ---------- 扣具报损 ----------
    def damage(self, store_id, floor, tag_type_id, qty, reason, uid, on_shelf=False):
        """扣具破损报损。

        on_shelf=False：未挂备用扣破损，从楼层库存出账；
        on_shelf=True ：在架商品上破损，挂扣时已出账，闭店计入在架平衡。
        """
        self.require_role(uid, "clerk", "manager")
        if qty <= 0:
            raise DomainError("报损数量必须为正")
        if not reason:
            raise TagBroken("扣具破损报损必须填写原因")
        from .db import tx
        with tx(self.conn):
            if not on_shelf:
                row = self._ensure_inv(store_id, floor, tag_type_id)
                if row["qty"] < qty:
                    raise ShortageError(
                        f"楼层备用扣仅 {row['qty']}，无法报损 {qty}；"
                        f"若破损发生在架商品上请按在架破损登记")
            did = self.conn.execute(
                "INSERT INTO tag_damages(store_id,floor,tag_type_id,qty,"
                "reason,on_shelf,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (store_id, floor, tag_type_id, qty, reason,
                 1 if on_shelf else 0, uid, now())
            ).lastrowid
            if not on_shelf:
                self._apply_qty(store_id, floor, tag_type_id, -qty,
                                "damage", did, uid)
        return did

    # ---------- 出口报警 ----------
    def raise_alarm(self, store_id, gate, txn_id, tag_type_id, evidence_name,
                    evidence_bytes, note=""):
        """门禁适配器上报报警，凭证写本地目录。"""
        path = None
        if evidence_bytes is not None:
            safe = "".join(c for c in evidence_name if c.isalnum() or c in "._-")
            path = os.path.join(self.evidence_dir, f"alarm_{safe}")
            with open(path, "wb") as f:
                f.write(evidence_bytes)
        with self.conn:  # noqa: SIM117 - 简单插入
            rid = self.conn.execute(
                "INSERT INTO exit_alarms(store_id,gate,txn_id,tag_type_id,"
                "status,evidence_path,note,fired_at) VALUES(?,?,?,?,?,?,?,?)",
                (store_id, gate, txn_id, tag_type_id,
                 "open" if txn_id else "no_txn", path, note, now())
            ).lastrowid
        return rid

    def review_alarm(self, alarm_id, register_no, uid, verdict, note=""):
        """收银台复核报警。verdict: miss(漏拆回收) / false(误报)。

        确认漏拆 -> 建拆扣记录（同交易仅一次）、入回收盒、库存回补、关闭事件。
        无交易报警 -> 不允许按漏拆回收，转安保处理。
        """
        self.require_role(uid, "cashier", "manager")
        from .db import tx
        with tx(self.conn):
            a = self.conn.execute("SELECT * FROM exit_alarms WHERE id=?",
                                  (alarm_id,)).fetchone()
            if not a:
                raise DomainError("报警事件不存在")
            if a["status"] not in ("open", "no_txn"):
                raise DuplicateDetach(f"报警 #{alarm_id} 已关闭（{a['status']}），禁止重复确认")
            if a["txn_id"] is None:
                # 无交易报警：不能拆扣回收，只登记转办
                if verdict == "miss":
                    raise NoTransactionAlarm(
                        "无交易报警：找不到成交记录，不能按漏拆回收，需安保核查")
                self.conn.execute(
                    "UPDATE exit_alarms SET status='false_alarm',register_no=?,"
                    "reviewer_id=?,note=?,closed_at=? WHERE id=?",
                    (register_no, uid, note or "无交易，误报放行", now(), alarm_id))
                return
            t = self.conn.execute("SELECT * FROM transactions WHERE id=?",
                                  (a["txn_id"],)).fetchone()
            if verdict == "false":
                self.conn.execute(
                    "UPDATE exit_alarms SET status='false_alarm',register_no=?,"
                    "reviewer_id=?,note=?,closed_at=? WHERE id=?",
                    (register_no, uid, note, now(), alarm_id))
                return
            # 确认漏拆：同一交易只能确认一次（detaches.txn_id UNIQUE 强约束）
            dup = self.conn.execute("SELECT id FROM detaches WHERE txn_id=?",
                                    (t["id"],)).fetchone()
            if dup:
                raise DuplicateDetach(
                    f"交易 #{t['id']} 已有拆扣记录 #{dup['id']}，不能重复回收")
            box = self.ensure_box(t["store_id"], register_no)
            did = self.conn.execute(
                "INSERT INTO detaches(txn_id,store_id,floor,register_no,"
                "tag_type_id,box_id,qty,status,cashier_id,confirmed_at,"
                "alarm_id,note) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (t["id"], t["store_id"], t["floor"], register_no,
                 t["tag_type_id"], box["id"], t["qty"], "confirmed", uid,
                 now(), alarm_id, f"出口报警 #{alarm_id} 复核漏拆回收；{note}")
            ).lastrowid
            self.conn.execute(
                "UPDATE recycle_boxes SET qty=qty+?, updated_at=? WHERE id=?",
                (t["qty"], now(), box["id"]))
            self._apply_qty(t["store_id"], t["floor"], t["tag_type_id"],
                            t["qty"], "detach", did, uid)
            self.conn.execute(
                "UPDATE exit_alarms SET status='confirmed_miss',register_no=?,"
                "reviewer_id=?,note=?,closed_at=? WHERE id=?",
                (register_no, uid, note, now(), alarm_id))

    def handle_no_txn_alarm(self, alarm_id, uid, note):
        """无交易报警由店长/收银主管登记核查结果后关闭。"""
        self.require_role(uid, "cashier", "manager")
        with self.conn:
            a = self.conn.execute("SELECT * FROM exit_alarms WHERE id=?",
                                  (alarm_id,)).fetchone()
            if not a or a["txn_id"] is not None:
                raise DomainError("仅无交易报警可走此流程")
            if a["status"] not in ("open", "no_txn"):
                raise DuplicateDetach("事件已关闭")
            self.conn.execute(
                "UPDATE exit_alarms SET status='closed',reviewer_id=?,"
                "note=?,closed_at=? WHERE id=?",
                (uid, "无交易核查：" + note, now(), alarm_id))

    def handle_fault(self, fault_id, uid, note, replaced=False):
        """消磁器故障处理：维修/更换后关闭，关联商品转人工拆扣。"""
        self.require_role(uid, "cashier", "manager")
        with self.conn:
            f = self.conn.execute("SELECT * FROM deactivator_faults WHERE id=?",
                                  (fault_id,)).fetchone()
            if not f:
                raise DomainError("故障单不存在")
            if f["handled"]:
                raise DomainError("故障单已处理")
            self.conn.execute(
                "UPDATE deactivator_faults SET handled=1,handled_at=?,note=? WHERE id=?",
                (now(), (f["note"] or "") + f"｜处理：{note}" +
                 ("，已更换设备" if replaced else ""), fault_id))

    # ---------- 闭店清点 ----------
    def run_closing(self, store_id, business_date, uid, on_shelf_overrides=None):
        """进程内任务/手工均可调用：聚合当日数量，实盘在架默认取期望值。

        on_shelf_overrides: {(floor, tag_type_id): 实盘数} —— 注入盘差。
        返回 (closing_rows, diff_rows)。
        """
        self.require_role(uid, "manager")
        on_shelf_overrides = on_shelf_overrides or {}
        from .db import tx
        results, diffs = [], []
        with tx(self.conn):
            store = self.conn.execute("SELECT * FROM stores WHERE id=?",
                                      (store_id,)).fetchone()
            floors = [f for f in store["floors"].split(",") if f and f != STOCKROOM]
            types = self.conn.execute("SELECT id FROM tag_types").fetchall()
            day_prefix = business_date + "%"
            for floor in floors:
                for ttp in types:
                    tid = ttp["id"]
                    req = self.conn.execute(
                        "SELECT COALESCE(SUM(qty),0) c FROM requisitions "
                        "WHERE store_id=? AND floor=? AND tag_type_id=? "
                        "AND status!='short' AND created_at LIKE ?",
                        (store_id, floor, tid, day_prefix)).fetchone()["c"]
                    att = self.conn.execute(
                        "SELECT COALESCE(SUM(attached_qty),0) c FROM tag_batches "
                        "WHERE store_id=? AND floor=? AND tag_type_id=? AND created_at LIKE ?",
                        (store_id, floor, tid, day_prefix)).fetchone()["c"]
                    det = self.conn.execute(
                        "SELECT COALESCE(SUM(qty),0) c FROM detaches "
                        "WHERE store_id=? AND floor=? AND tag_type_id=? "
                        "AND status IN ('confirmed','box_mismatch') "
                        "AND confirmed_at LIKE ?",
                        (store_id, floor, tid, day_prefix)).fetchone()["c"]
                    dmg_row = self.conn.execute(
                        "SELECT COALESCE(SUM(qty),0) c FROM tag_damages "
                        "WHERE store_id=? AND floor=? AND tag_type_id=? "
                        "AND on_shelf=1 AND created_at LIKE ?",
                        (store_id, floor, tid, day_prefix)).fetchone()
                    spare_dmg = self.conn.execute(
                        "SELECT COALESCE(SUM(qty),0) c FROM tag_damages "
                        "WHERE store_id=? AND floor=? AND tag_type_id=? "
                        "AND on_shelf=0 AND created_at LIKE ?",
                        (store_id, floor, tid, day_prefix)).fetchone()["c"]
                    dmg = dmg_row["c"]
                    # 在架平衡：挂上的扣 = 在架 + 拆回 + 在架破损
                    expected_shelf = att - det - dmg
                    if expected_shelf < 0:
                        expected_shelf = 0
                    counted = on_shelf_overrides.get((floor, tid), expected_shelf)
                    diff = counted - expected_shelf
                    # 领用平衡：领来的扣 = 挂上 + 未挂余存 + 备用扣报损；
                    # 余存中含拆回数，故等式为 领用+拆回 = 挂扣+余存+备用报损
                    inv = self.conn.execute(
                        "SELECT qty FROM inventory WHERE store_id=? AND floor=? "
                        "AND tag_type_id=?", (store_id, floor, tid)).fetchone()
                    floor_left = inv["qty"] if inv else 0
                    chain_balanced = (req + det == att + floor_left + spare_dmg)
                    balanced = (diff == 0 and chain_balanced)
                    cid = self.conn.execute(
                        "INSERT INTO closing_counts(store_id,floor,tag_type_id,"
                        "business_date,requisition_qty,attached_qty,detached_qty,"
                        "on_shelf_qty,damaged_qty,expected_on_shelf,"
                        "spare_damaged_qty,floor_left_qty,diff,balanced,"
                        "created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                        "ON CONFLICT(store_id,floor,tag_type_id,business_date) "
                        "DO UPDATE SET requisition_qty=excluded.requisition_qty,"
                        "attached_qty=excluded.attached_qty,"
                        "detached_qty=excluded.detached_qty,"
                        "on_shelf_qty=excluded.on_shelf_qty,"
                        "damaged_qty=excluded.damaged_qty,"
                        "expected_on_shelf=excluded.expected_on_shelf,"
                        "spare_damaged_qty=excluded.spare_damaged_qty,"
                        "floor_left_qty=excluded.floor_left_qty,"
                        "diff=excluded.diff,balanced=excluded.balanced,"
                        "created_at=excluded.created_at",
                        (store_id, floor, tid, business_date, req, att, det,
                         counted, dmg, expected_shelf, spare_dmg, floor_left,
                         diff, balanced, now())
                    ).lastrowid
                    row = dict(floor=floor, tag_type_id=tid, requisition=req,
                               attached=att, detached=det, on_shelf=counted,
                               damaged=dmg, spare_damaged=spare_dmg,
                               floor_left=floor_left,
                               expected_shelf=expected_shelf,
                               diff=diff, balanced=balanced, closing_id=cid)
                    results.append(row)
                    if diff != 0:
                        diffs.append(row)
                        # 差异自动生成待审批单与提醒
                        existing = self.conn.execute(
                            "SELECT id FROM inventory_adjustments WHERE closing_id=?",
                            (cid,)).fetchone()
                        if not existing:
                            self.conn.execute(
                                "INSERT INTO inventory_adjustments(store_id,floor,"
                                "tag_type_id,closing_id,diff,reason,status,"
                                "requested_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                                (store_id, floor, tid, cid, diff,
                                 f"闭店盘点差异（实盘 {counted} / 期望 {expected_shelf}）",
                                 "pending", uid, now()))
                        self.conn.execute(
                            "INSERT INTO reminders(store_id,kind,title,body,created_at)"
                            " VALUES(?,?,?,?,?)",
                            (store_id, "diff",
                             f"盘差待批：{floor} 扣型#{tid}",
                             f"差异 {diff:+d}（实盘 {counted}，期望 {expected_shelf}）",
                             now()))
        return results, diffs

    # ---------- 盘差审批（仅店长） ----------
    def approve_adjustment(self, adj_id, uid, approve=True, reason=""):
        self.require_role(uid, "manager")
        from .db import tx
        with tx(self.conn):
            a = self.conn.execute("SELECT * FROM inventory_adjustments WHERE id=?",
                                  (adj_id,)).fetchone()
            if not a:
                raise DomainError("审批单不存在")
            if a["status"] != "pending":
                raise DomainError(f"该单已{a['status']}，不能重复审批")
            if approve:
                # 盘差以实盘为准调整楼层库存与在架口径：盘盈补入，盘亏出账
                try:
                    self._apply_qty(a["store_id"], a["floor"], a["tag_type_id"],
                                    a["diff"], "adjust", adj_id, uid)
                except ShortageError:
                    raise CountDiff("盘亏数量大于楼层库存，需先核查挂扣/报损记录")
                self.conn.execute(
                    "UPDATE closing_counts SET balanced=1, diff=0, on_shelf_qty="
                    "expected_on_shelf WHERE id=?", (a["closing_id"],))
                self.conn.execute(
                    "UPDATE inventory_adjustments SET status='approved',"
                    "reviewed_by=?,reviewed_at=?,reason=? WHERE id=?",
                    (uid, now(), (a["reason"] or "") + f"｜批准：{reason}", adj_id))
            else:
                self.conn.execute(
                    "UPDATE inventory_adjustments SET status='rejected',"
                    "reviewed_by=?,reviewed_at=?,reason=? WHERE id=?",
                    (uid, now(), (a["reason"] or "") + f"｜驳回：{reason}", adj_id))
        return approve

    # ---------- 查询 ----------
    def inventory_matrix(self, store_id):
        rows = self.conn.execute(
            "SELECT i.floor, t.name tag_name, t.code, i.qty, i.version "
            "FROM inventory i JOIN tag_types t ON t.id=i.tag_type_id "
            "WHERE i.store_id=? ORDER BY i.floor, t.id", (store_id,)).fetchall()
        return [dict(r) for r in rows]

    def checkout_timeline(self, alarm_id):
        """报警复核页：优先返回收银时间线 + 扣具类型。"""
        a = self.conn.execute("SELECT * FROM exit_alarms WHERE id=?",
                              (alarm_id,)).fetchone()
        if not a:
            return None
        ttype = self.conn.execute("SELECT * FROM tag_types WHERE id=?",
                                  (a["tag_type_id"],)).fetchone()
        timeline = []
        txn = None
        if a["txn_id"]:
            txn = self.conn.execute(
                "SELECT t.*, p.name product_name, p.sku FROM transactions t "
                "JOIN products p ON p.id=t.product_id WHERE t.id=?",
                (a["txn_id"],)).fetchone()
            timeline.append({"time": txn["paid_at"], "event": "收银成交",
                             "detail": f"{txn['product_name']} ×{txn['qty']} @ {txn['register_no']}号台"})
            fault = self.conn.execute(
                "SELECT * FROM deactivator_faults WHERE txn_id=? ORDER BY id",
                (a["txn_id"],)).fetchone()
            if fault:
                timeline.append({"time": fault["created_at"], "event": "消磁失败",
                                 "detail": f"{fault['register_no']}号台消磁器，扣具未拆回"})
            d = self.conn.execute("SELECT * FROM detaches WHERE txn_id=?",
                                  (a["txn_id"],)).fetchone()
            if d:
                timeline.append({"time": d["confirmed_at"], "event": "拆扣回收",
                                 "detail": f"{d['qty']} 枚 → {d['register_no']}号台盒（{d['status']}）"})
            else:
                timeline.append({"time": txn["paid_at"], "event": "未拆扣",
                                 "detail": "该交易无拆扣记录，扣具仍在商品上"})
        timeline.append({"time": a["fired_at"], "event": "出口报警",
                         "detail": f"{a['gate']} 门禁触发"})
        return {"alarm": dict(a), "tag_type": dict(ttype) if ttype else None,
                "txn": dict(txn) if txn else None, "timeline": timeline}
