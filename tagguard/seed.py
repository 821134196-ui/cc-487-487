"""样例数据：门店、人员、扣型、商品、交易，并构造三类典型场景。

  漏拆：1F 丝巾成交时消磁失败 -> 扣具未拆 -> 出口门禁报警（待复核）
  串台：2F 衬衫在 2F-01 号台拆扣，扣具误投 2F-02 号盒（待调正）
  盘差：闭店实盘 1F 墨黑硬扣比期望少 2 枚（生成待店长审批单）
另含：扣型不匹配批次、在架破损、备用扣破损、消磁器设备故障、无交易报警。
"""
from datetime import datetime

from .db import init_db
from .service import Service, now
from .adapters import POSAdapter, GateAdapter, DeactivatorAdapter


def seed(conn, business_date=None, evidence_dir="data/evidence"):
    svc = Service(conn, evidence_dir)
    business_date = business_date or datetime.now().strftime("%Y-%m-%d")

    conn.execute("DELETE FROM stock_ledger")
    for t in ("reminders", "inventory_adjustments", "closing_counts",
              "tag_damages", "deactivator_faults", "exit_alarms",
              "box_cross_events", "recycle_boxes", "detaches", "transactions",
              "tag_batches", "requisitions", "inventory", "products",
              "tag_types", "users", "stores"):
        conn.execute(f"DELETE FROM {t}")
    conn.commit()

    # ---- 组织 ----
    conn.execute("INSERT INTO stores(id,name,floors) VALUES(?,?,?)",
                 (1, "锦云阁服装店（万象城店）", "仓库,1F,2F,3F"))
    users = [
        (1, "李导购", "clerk"), (2, "王导购", "clerk"),
        (3, "陈收银", "cashier"), (4, "周收银", "cashier"),
        (5, "林店长", "manager"),
    ]
    conn.executemany("INSERT INTO users(id,name,role) VALUES(?,?,?)", users)

    # ---- 扣型 ----
    types = [
        (1, "HARD-B", "墨黑硬扣", "大衣/外套常用，强力锁芯"),
        (2, "INK-S", "雅黑墨水扣", "衬衫/裤装，小型轻便"),
        (3, "MAG-R", "雅黑高敏磁扣", "丝巾/礼服，高灵敏度"),
    ]
    conn.executemany("INSERT INTO tag_types(id,code,name,note) VALUES(?,?,?,?)", types)

    # ---- 商品 ----
    products = [
        (1, "SKU-FY001", "云岚风衣", 1, 1299.0),
        (2, "SKU-SJ002", "烟罗丝巾", 3, 399.0),
        (3, "SKU-CS003", "雅黑通勤衬衫", 2, 299.0),
        (4, "SKU-XK004", "垂感西裤", 1, 459.0),
        (5, "SKU-LF005", "晚礼服", 3, 2680.0),
    ]
    conn.executemany(
        "INSERT INTO products(id,sku,name,required_tag_type_id,price) VALUES(?,?,?,?,?)",
        products)
    conn.commit()

    # ---- 到货入库（店长）----
    svc.issue_tags(1, 1, 300, 5)
    svc.issue_tags(1, 2, 200, 5)
    svc.issue_tags(1, 3, 100, 5)

    # ---- 楼层领用（导购）----
    svc.requisition(1, "1F", 1, 80, 1, "风衣区备货")
    svc.requisition(1, "1F", 3, 30, 1, "丝巾区备货")
    svc.requisition(1, "2F", 2, 60, 2, "衬衫裤装区")
    svc.requisition(1, "2F", 1, 40, 2, "外套区")
    svc.requisition(1, "3F", 3, 20, 1, "礼服区")

    # ---- 商品挂扣批次 ----
    svc.attach_batch(1, "1F", 1, 1, 40, 40, 1)                 # 正常挂满
    svc.attach_batch(1, "1F", 2, 3, 20, 18, 1)                 # 应挂20实挂18，短缺待补
    svc.attach_batch(1, "2F", 3, 2, 30, 30, 2)                 # 正常
    svc.attach_batch(1, "2F", 4, 2, 20, 10, 2,                 # 扣型不匹配（注明原因）
                     note="墨黑硬扣缺货，经店长同意临时改用墨水扣")
    svc.attach_batch(1, "3F", 5, 3, 10, 10, 1)                 # 正常

    # ---- 成交交易（样例流水，时间戳取当天，纳入闭店）----
    def txn(floor, reg, pid, ttype, qty, amount, idx):
        ts = f"{business_date} {10 + idx:02d}:12:{idx:02d}"
        cur = conn.execute(
            "INSERT INTO transactions(store_id,floor,register_no,product_id,"
            "tag_type_id,qty,amount,paid_at,source) VALUES(?,?,?,?,?,?,?,?,?)",
            (1, floor, reg, pid, ttype, qty, amount, ts, "pos"))
        conn.commit()
        return cur.lastrowid

    t1 = txn("1F", "1F-01", 1, 1, 1, 1299.0, 1)   # 风衣正常成交
    t2 = txn("2F", "2F-01", 3, 2, 2, 598.0, 2)    # 衬衫×2
    t3 = txn("1F", "1F-01", 2, 3, 1, 399.0, 3)    # 丝巾（即将漏拆）
    t4 = txn("3F", "3F-01", 5, 3, 1, 2680.0, 4)   # 礼服正常成交

    # ---- 收银拆扣 ----
    svc.detach(t1, "1F-01", 3)                      # 正常回收
    try:
        svc.detach(t2, "2F-02", 4)                  # 串台：2F-01 拆的投进 2F-02 盒
    except Exception as e:
        assert "串台" in str(e)
    try:
        svc.detach(t3, "1F-01", 3, deauth_ok=False)  # 消磁失败，未拆回
    except Exception as e:
        assert "消磁失败" in str(e)
    svc.detach(t4, "3F-01", 4)                      # 正常回收

    # ---- 报损 ----
    svc.damage(1, "3F", 3, 1, "礼服陈列期间磁扣外壳开裂", 1, on_shelf=True)
    svc.damage(1, "2F", 2, 1, "墨水扣锁芯弹簧失效", 2)          # 未挂备用扣破损

    # ---- 消磁器设备故障 ----
    DeactivatorAdapter(conn).report_device_fault(
        1, "1F-02", 5, "开机自检不通过，蜂鸣三声")

    # ---- 出口报警 ----
    pos, gate = POSAdapter(conn), GateAdapter(svc)
    # 漏拆：丝巾消磁失败后带扣出门，门禁报警（自动反查到 t3）
    miss_alarm = gate.fire(1, "东门", "1F", 3, pos,
                           note="顾客携带已购丝巾通过，门禁持续鸣响",
                           txn_id=t3,
                           evidence_text=f"GATE ALERT 东门 @ {now()}\n"
                                         f"floor=1F tag=MAG-R txn={t3}\n"
                                         f"signal=持续高电平，疑似未消磁硬标签\n")
    # 无交易报警：试衣间方向出门触发，查不到成交
    svc.raise_alarm(1, "西门", None, 1,
                    f"west_{business_date.replace('-','')}.txt",
                    f"GATE ALERT 西门 @ {now()}\n无交易匹配，疑似夹带\n".encode(),
                    note="无成交记录，顾客折返后离开，待安保核查")
    return {"miss_alarm": miss_alarm, "txn_miss": t3,
            "txn_cross": t2, "txn_ok": [t1, t4]}


def seed_and_close(db_path, business_date=None, evidence_dir="data/evidence"):
    """导入样例并执行一次闭店清点（注入 1F 墨黑硬扣盘亏 2 枚）。"""
    conn = init_db(db_path)
    info = seed(conn, business_date, evidence_dir)
    svc = Service(conn)
    overrides = {("1F", 1): 37}   # 期望在架 39，实盘 37，盘亏 2
    rows, diffs = svc.run_closing(1, business_date or
                                  datetime.now().strftime("%Y-%m-%d"),
                                  5, overrides)
    conn.commit()
    return conn, info, rows, diffs
