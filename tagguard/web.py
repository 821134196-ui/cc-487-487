"""页面渲染：门店白 / 雅黑 / 醒目洋红。

设计约束：
- 库存按类型×楼层组成窄表；
- 闭店平衡用一条「领用 → 挂扣 → 拆回 → 在架/报损」数量链；
- 报警复核页收银时间线与扣具类型优先，状态用小徽章，不使用整屏红色。
"""
import html

MAGENTA = "#d41472"
INK = "#202124"
GREY = "#5f6368"
LINE = "#e7e7ea"
BG = "#f6f6f8"

STATUS_STYLE = {
    "open":            ("待复核", "#9a6200", "#fdf3e0"),
    "no_txn":          ("无交易", "#8a4b00", "#fdeede"),
    "confirmed_miss":  ("漏拆已回收", "#146c39", "#e6f6ec"),
    "false_alarm":     ("误报",      "#5f6368", "#eef0f2"),
    "closed":          ("已关闭",    "#5f6368", "#eef0f2"),
    "pending":         ("待审批",    "#9a6200", "#fdf3e0"),
    "approved":        ("已批准",    "#146c39", "#e6f6ec"),
    "rejected":        ("已驳回",    "#5f6368", "#eef0f2"),
    "short":           ("短缺",      "#b04800", "#fdeede"),
    "active":          ("正常",      "#146c39", "#e6f6ec"),
    "box_mismatch":    ("串台",      "#b04800", "#fdeede"),
    "confirmed":       ("已回收",    "#146c39", "#e6f6ec"),
}


def esc(s):
    return html.escape(str(s)) if s is not None else ""


def badge(status):
    label, fg, bgc = STATUS_STYLE.get(status, (status, GREY, "#eef0f2"))
    return (f'<span class="badge" style="color:{fg};background:{bgc}">'
            f'{esc(label)}</span>')


def page(title, body, current_user=None, users=None, active="", flash=None):
    user_bar = ""
    if users is not None:
        opts = "".join(
            f'<option value="{u["id"]}" {"selected" if current_user and u["id"]==current_user["id"] else ""}>'
            f'{esc(u["name"])} · { {"clerk":"导购","cashier":"收银","manager":"店长"}[u["role"]] }</option>'
            for u in users)
        user_bar = f'''<form method="post" action="/switch-user" class="userbar">
            <span class="dot"></span><b>{esc(current_user["name"]) if current_user else "未选择"}</b>
            <span class="muted">当前操作身份</span>
            <select name="uid" onchange="this.form.submit()">{opts}</select></form>'''
    nav = [("/", "总览"), ("/inventory", "扣具库存"),
           ("/requisitions", "楼层领用"), ("/batches", "挂扣批次"),
           ("/recycle", "收银回收"), ("/alarms", "出口报警"),
           ("/faults", "消磁故障"), ("/closing", "闭店平衡"),
           ("/adjustments", "盘差审批"), ("/ledger", "数量流水")]
    nav_html = "".join(
        f'<a href="{u}" class="{"on" if u==active else ""}">{t}</a>'
        for u, t in nav)
    flash_html = f'<div class="flash {flash[0]}">{esc(flash[1])}</div>' if flash else ""
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)} · 防盗扣管理</title>
<style>
:root{{--mg:{MAGENTA};--ink:{INK};--grey:{GREY};--line:{LINE};--bg:{BG};}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--ink);
 font-family:"Microsoft YaHei","微软雅黑","PingFang SC","Helvetica Neue",Arial,sans-serif;
 font-size:14px;line-height:1.6}}
header{{background:#fff;border-bottom:1px solid var(--line);position:sticky;top:0;z-index:5}}
.hwrap{{max-width:1180px;margin:0 auto;padding:10px 20px;display:flex;
 align-items:center;gap:18px;flex-wrap:wrap}}
.brand{{font-weight:700;font-size:16px;white-space:nowrap}}
.brand b{{color:var(--mg)}}
nav{{display:flex;gap:2px;flex-wrap:wrap}}
nav a{{padding:6px 11px;border-radius:7px;color:var(--grey);text-decoration:none;font-size:13px}}
nav a:hover{{background:#f3e9ef;color:var(--mg)}}
nav a.on{{background:var(--mg);color:#fff}}
.userbar{{margin-left:auto;display:flex;align-items:center;gap:7px;font-size:13px}}
.userbar .dot{{width:9px;height:9px;border-radius:50%;background:var(--mg)}}
.userbar select{{padding:4px 8px;border:1px solid var(--line);border-radius:7px}}
.muted{{color:var(--grey)}}
main{{max-width:1180px;margin:22px auto;padding:0 20px 60px}}
h1{{font-size:20px;margin:0 0 4px}} h2{{font-size:15px;margin:26px 0 10px}}
.card{{background:#fff;border:1px solid var(--line);border-radius:12px;padding:18px 20px;margin-bottom:16px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px}}
.tile{{background:#fff;border:1px solid var(--line);border-radius:12px;padding:15px 17px}}
.tile .n{{font-size:26px;font-weight:700}}
.tile .n.mg{{color:var(--mg)}}
.tile .l{{color:var(--grey);font-size:12.5px;margin-top:2px}}
table{{border-collapse:collapse;width:100%;font-size:13.5px}}
th,td{{text-align:left;padding:9px 11px;border-bottom:1px solid var(--line);white-space:nowrap}}
th{{color:var(--grey);font-weight:600;font-size:12.5px;background:#fbfbfc}}
tr:last-child td{{border-bottom:none}}
td.num,th.num{{text-align:right;font-variant-numeric:tabular-nums}}
.badge{{display:inline-block;padding:2px 9px;border-radius:999px;font-size:12px;line-height:1.7}}
.chain{{display:flex;align-items:stretch;gap:0;overflow-x:auto;padding:4px 0 10px}}
.node{{background:#fff;border:1px solid var(--line);border-radius:10px;padding:10px 14px;min-width:108px}}
.node .v{{font-size:20px;font-weight:700;font-variant-numeric:tabular-nums}}
.node .k{{color:var(--grey);font-size:12px}}
.node.mg .v{{color:var(--mg)}}
.arrow{{display:flex;align-items:center;padding:0 9px;color:var(--mg);font-size:18px;font-weight:700}}
form.inline{{display:inline}}
input,select,textarea{{padding:6px 9px;border:1px solid var(--line);border-radius:7px;
 font-family:inherit;font-size:13px;background:#fff;color:var(--ink)}}
button,.btn{{background:var(--mg);color:#fff;border:none;border-radius:8px;padding:7px 15px;
 font-family:inherit;font-size:13px;cursor:pointer;text-decoration:none;display:inline-block}}
button.ghost,.btn.ghost{{background:#fff;color:var(--mg);border:1px solid var(--mg)}}
button.subtle,.btn.subtle{{background:#f1f1f3;color:var(--ink)}}
.flash{{padding:10px 15px;border-radius:9px;margin-bottom:14px;font-size:13.5px}}
.flash.ok{{background:#e6f6ec;color:#146c39;border:1px solid #bfe5cc}}
.flash.err{{background:#fdeeee;color:#a32626;border:1px solid #f3cccc}}
.rowform{{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:10px}}
.timeline{{list-style:none;margin:0;padding:0}}
.timeline li{{position:relative;padding:0 0 18px 22px;border-left:2px solid #eadfe4;margin-left:6px}}
.timeline li:last-child{{border-left-color:transparent;padding-bottom:0}}
.timeline li::before{{content:"";position:absolute;left:-7px;top:3px;width:12px;height:12px;
 border-radius:50%;background:#fff;border:3px solid var(--mg)}}
.timeline .t{{font-weight:600}} .timeline .d{{color:var(--grey);font-size:13px}}
.twocol{{display:grid;grid-template-columns:1.4fr 1fr;gap:16px}}
@media(max-width:860px){{.twocol{{grid-template-columns:1fr}}}}
.evidence{{background:#fbfbfc;border:1px dashed var(--line);border-radius:9px;
 padding:11px 13px;font-family:Menlo,Consolas,monospace;font-size:12px;white-space:pre-wrap;color:var(--grey)}}
a{{color:var(--mg)}}
.flag{{color:var(--mg);font-weight:600}}
.small{{font-size:12.5px;color:var(--grey)}}
</style></head>
<body>
<header><div class="hwrap"><div class="brand">🛡️ 防盗扣 <b>领用 · 漏拆复核</b></div>
<nav>{nav_html}</nav>{user_bar}</div></header>
<main>{flash_html}{body}</main></body></html>"""


# ---------------- 总览 ----------------
def render_home(svc, store, current_user, users, reminders):
    conn = svc.conn
    def scalar(sql, *a):
        r = conn.execute(sql, a).fetchone()
        return r[0] if r else 0
    open_alarms = scalar("SELECT COUNT(*) FROM exit_alarms WHERE store_id=? AND status IN('open','no_txn')", store["id"])
    cross = scalar("SELECT COUNT(*) FROM box_cross_events WHERE store_id=? AND resolved=0", store["id"])
    faults = scalar("SELECT COUNT(*) FROM deactivator_faults WHERE store_id=? AND handled=0", store["id"])
    pend = scalar("SELECT COUNT(*) FROM inventory_adjustments WHERE store_id=? AND status='pending'", store["id"])
    boxed = scalar("SELECT COALESCE(SUM(qty),0) FROM recycle_boxes WHERE store_id=?", store["id"])
    total_ledger = scalar("SELECT COALESCE(SUM(change_qty),0) FROM stock_ledger WHERE store_id=?", store["id"])
    tiles = f"""
    <div class="grid">
      <div class="tile"><div class="n mg">{open_alarms}</div><div class="l">待复核出口报警</div></div>
      <div class="tile"><div class="n">{cross}</div><div class="l">回收盒串台待调正</div></div>
      <div class="tile"><div class="n">{faults}</div><div class="l">消磁器故障待处理</div></div>
      <div class="tile"><div class="n mg">{pend}</div><div class="l">盘差待店长审批</div></div>
      <div class="tile"><div class="n">{boxed}</div><div class="l">回收盒现存扣具</div></div>
      <div class="tile"><div class="n">{total_ledger}</div><div class="l">流水净余额（守恒应=在架外存量）</div></div>
    </div>"""
    rem_rows = "".join(
        f'<tr><td>{"🔔" if not r["read"] else ""}</td><td>{esc(r["title"])}</td>'
        f'<td class="muted">{esc(r["body"])}</td><td class="small">{esc(r["created_at"])}</td></tr>'
        for r in reminders) or '<tr><td colspan="4" class="muted">暂无提醒</td></tr>'
    body = f"""
    <h1>{esc(store["name"])}</h1>
    <p class="muted">楼层：{esc(store["floors"].replace(",", " · "))}　·　样例营业数据，进程内任务自动生成闭店清点与差异提醒</p>
    {tiles}
    <h2>任务提醒</h2>
    <div class="card"><table>
      <tr><th></th><th>事项</th><th>内容</th><th>时间</th></tr>{rem_rows}</table></div>
    <form method="post" action="/scheduler/run-closing" class="rowform">
      <button class="ghost" type="submit">立即生成今日闭店清点（店长）</button></form>
    """
    return page("总览", body, current_user, users, "/")


# ---------------- 扣具库存（窄表） ----------------
def render_inventory(svc, store, current_user, users, tag_types):
    rows = svc.inventory_matrix(store["id"])
    trs = "".join(
        f'<tr><td>{esc(r["floor"])}</td>'
        f'<td><span class="flag">{esc(r["tag_name"])}</span>'
        f' <span class="small">{esc(r["code"])}</span></td>'
        f'<td class="num">{r["qty"]}</td><td class="num muted">v{r["version"]}</td></tr>'
        for r in rows)
    type_opts = "".join(f'<option value="{t["id"]}">{esc(t["name"])}（{esc(t["code"])}）</option>'
                        for t in tag_types)
    floors = [f for f in store["floors"].split(",") if f and f != "仓库"]
    floor_opts = "".join(f'<option>{f}</option>' for f in floors)
    dmg_rows = svc.conn.execute(
        "SELECT d.*, t.name tag_name, u.name uname FROM tag_damages d "
        "JOIN tag_types t ON t.id=d.tag_type_id JOIN users u ON u.id=d.created_by "
        "WHERE d.store_id=? ORDER BY d.id DESC LIMIT(30)",
        (store["id"],)).fetchall()
    dmg_trs = "".join(
        f'<tr><td>#{d["id"]}</td><td>{esc(d["floor"])}</td><td>{esc(d["tag_name"])}</td>'
        f'<td class="num">{d["qty"]}</td>'
        f'<td>{"在架破损" if d["on_shelf"] else "备用扣破损"}</td>'
        f'<td class="small muted">{esc(d["reason"])}</td><td>{esc(d["uname"])}</td>'
        f'<td class="small">{esc(d["created_at"])}</td></tr>' for d in dmg_rows) or \
        '<tr><td colspan="8" class="muted">暂无报损记录</td></tr>'
    body = f"""
    <h1>扣具库存</h1>
    <p class="muted">按「楼层 × 扣型」组成的窄表；每次调整 version +1，并发调整以后提交者版本冲突失败。</p>
    <div class="card"><table>
      <tr><th>位置</th><th>扣具类型</th><th class="num">数量</th><th class="num">版本</th></tr>
      {trs}</table></div>
    <h2>扣具到货入库（店长/导购，入仓库）</h2>
    <div class="card"><form method="post" action="/issue" class="rowform">
      <select name="tag_type_id">{type_opts}</select>
      <input name="qty" type="number" min="1" value="50" style="width:90px">
      <button>入库</button></form></div>
    <h2>楼层领用（导购，仓库 → 楼层）</h2>
    <div class="card"><form method="post" action="/requisition" class="rowform">
      <select name="floor">{floor_opts}</select>
      <select name="tag_type_id">{type_opts}</select>
      <input name="qty" type="number" min="1" value="20" style="width:90px">
      <input name="note" placeholder="备注（可选）" style="width:200px">
      <button>领用</button></form></div>
    <h2>扣具报损（导购登记，区分在架/备用扣）</h2>
    <div class="card"><form method="post" action="/damage" class="rowform">
      <select name="floor">{floor_opts}</select>
      <select name="tag_type_id">{type_opts}</select>
      <input name="qty" type="number" min="1" value="1" style="width:80px">
      <label class="small"><input type="checkbox" name="on_shelf" value="1"
        style="width:auto"> 在架商品破损（挂扣已出账，闭店计损）</label>
      <input name="reason" placeholder="破损原因（必填）" style="width:220px">
      <button>报损</button></form></div>
    <h2>报损记录</h2>
    <div class="card"><table>
      <tr><th>单号</th><th>楼层</th><th>扣型</th><th class="num">数量</th>
      <th>类型</th><th>原因</th><th>登记人</th><th>时间</th></tr>
      {dmg_trs}</table></div>
    """
    return page("扣具库存", body, current_user, users, "/inventory")


# ---------------- 楼层领用 ----------------
def render_requisitions(svc, store, current_user, users):
    rows = svc.conn.execute(
        "SELECT r.*, t.name tag_name, u.name uname FROM requisitions r "
        "JOIN tag_types t ON t.id=r.tag_type_id JOIN users u ON u.id=r.created_by "
        "WHERE r.store_id=? ORDER BY r.id DESC", (store["id"],)).fetchall()
    trs = "".join(
        f'<tr><td>#{r["id"]}</td><td>{esc(r["floor"])}</td><td>{esc(r["tag_name"])}</td>'
        f'<td class="num">{r["qty"]}</td><td>{badge(r["status"])}</td>'
        f'<td>{esc(r["uname"])}</td><td class="small">{esc(r["created_at"])}</td>'
        f'<td class="small muted">{esc(r["note"])}</td></tr>' for r in rows)
    body = f"""<h1>楼层领用</h1>
    <div class="card"><table>
    <tr><th>单号</th><th>楼层</th><th>扣型</th><th class="num">数量</th><th>状态</th>
    <th>领用人</th><th>时间</th><th>备注</th></tr>{trs}</table></div>"""
    return page("楼层领用", body, current_user, users, "/requisitions")


# ---------------- 挂扣批次 ----------------
def render_batches(svc, store, current_user, users, products, tag_types):
    rows = svc.conn.execute(
        "SELECT b.*, p.name product_name, t.name tag_name, u.name uname,"
        " rt.name req_name FROM tag_batches b JOIN products p ON p.id=b.product_id "
        "JOIN tag_types t ON t.id=b.tag_type_id "
        "JOIN tag_types rt ON rt.id=p.required_tag_type_id "
        "JOIN users u ON u.id=b.created_by WHERE b.store_id=? ORDER BY b.id DESC",
        (store["id"],)).fetchall()
    trs = "".join(
        f'<tr><td>#{r["id"]}</td><td>{esc(r["floor"])}</td><td>{esc(r["product_name"])}</td>'
        f'<td class="num">{r["required_qty"]}</td><td class="num">{r["attached_qty"]}</td>'
        f'<td>{"<span class=flag>"+esc(r["tag_name"])+"</span>" if r["mismatch"] else esc(r["tag_name"])}'
        f'{" ⚠ 改用（应 "+esc(r["req_name"])+"）" if r["mismatch"] else ""}</td>'
        f'<td>{badge("open" if r["status"]=="open" else "confirmed")}</td>'
        f'<td>{esc(r["uname"])}</td><td class="small muted">{esc(r["note"] or "")}</td></tr>'
        for r in rows)
    popts = "".join(f'<option value="{p["id"]}">{esc(p["name"])}（应挂 {esc(tt_name(svc,p))}）</option>'
                    for p in products)
    topts = "".join(f'<option value="{t["id"]}">{esc(t["name"])}</option>' for t in tag_types)
    floors = [f for f in store["floors"].split(",") if f != "仓库"]
    fopts = "".join(f'<option>{f}</option>' for f in floors)
    body = f"""<h1>商品挂扣批次</h1>
    <p class="muted">到货商品按批次领取扣具并记录应挂数量；扣型不匹配须注明改用原因。</p>
    <div class="card"><table>
    <tr><th>批次</th><th>楼层</th><th>商品</th><th class="num">应挂</th><th class="num">实挂</th>
    <th>实际扣型</th><th>状态</th><th>操作人</th><th>备注</th></tr>{trs}</table></div>
    <h2>新建挂扣批次（导购）</h2>
    <div class="card"><form method="post" action="/batch" class="rowform">
      <select name="floor">{fopts}</select>
      <select name="product_id">{popts}</select>
      <select name="tag_type_id">{topts}</select>
      <input name="required_qty" type="number" min="1" value="10" style="width:80px" title="应挂">
      <input name="attached_qty" type="number" min="0" value="10" style="width:80px" title="实挂">
      <input name="note" placeholder="扣型不匹配时必填改用原因" style="width:230px">
      <button>登记批次</button></form></div>"""
    return page("挂扣批次", body, current_user, users, "/batches")


def tt_name(svc, p):
    r = svc.conn.execute("SELECT name FROM tag_types WHERE id=?",
                         (p["required_tag_type_id"],)).fetchone()
    return r["name"] if r else "?"


# ---------------- 收银回收 / 回收盒 ----------------
def render_recycle(svc, store, current_user, users):
    txns = svc.conn.execute(
        "SELECT t.*, p.name product_name, d.id did, d.status dstatus "
        "FROM transactions t JOIN products p ON p.id=t.product_id "
        "LEFT JOIN detaches d ON d.txn_id=t.id WHERE t.store_id=? ORDER BY t.id",
        (store["id"],)).fetchall()
    def _detach_cell(r):
        dis = " disabled" if r["did"] else ""
        return (f'<td><form method="post" action="/detach" class="inline">'
                f'<input type="hidden" name="txn_id" value="{r["id"]}">'
                f'<input name="box_register" value="{esc(r["register_no"])}" style="width:74px">'
                f'<button class="subtle"{dis}>拆扣</button></form> '
                f'<form method="post" action="/detach-fail" class="inline">'
                f'<input type="hidden" name="txn_id" value="{r["id"]}">'
                f'<button class="subtle"{dis} title="登记消磁失败">消磁失败</button>'
                f'</form></td>')

    trows = "".join(
        f'<tr><td>#{r["id"]}</td><td>{esc(r["paid_at"])}</td><td>{esc(r["register_no"])}</td>'
        f'<td>{esc(r["floor"])}</td><td>{esc(r["product_name"])}</td>'
        f'<td class="num">{r["qty"]}</td>'
        f'<td>{badge(r["dstatus"]) if r["did"] else "<span class=flag>未拆扣</span>"}</td>'
        + _detach_cell(r) + '</tr>' for r in txns)
    boxes = svc.conn.execute(
        "SELECT * FROM recycle_boxes WHERE store_id=? ORDER BY register_no", (store["id"],)).fetchall()
    brows = "".join(
        f'<tr><td>{esc(b["register_no"])} 号台盒</td><td class="num">{b["qty"]}</td>'
        f'<td class="small">{esc(b["updated_at"] or "")}</td></tr>' for b in boxes)
    crosses = svc.conn.execute(
        "SELECT c.*, t.name tag_name FROM box_cross_events c "
        "JOIN tag_types t ON t.id=c.tag_type_id WHERE c.store_id=? ORDER BY c.id DESC",
        (store["id"],)).fetchall()
    def _cross_cell(c):
        if c["resolved"]:
            return "<td></td>"
        return (f'<td><form method="post" action="/cross/resolve" class="inline">'
                f'<input type="hidden" name="cross_id" value="{c["id"]}">'
                f'<button class="subtle">调正回盒</button></form></td>')

    crows = "".join(
        f'<tr><td>#{c["id"]}</td><td>{esc(c["expected_register"])} → {esc(c["actual_register"])}</td>'
        f'<td>{esc(c["tag_name"])}</td><td class="num">{c["qty"]}</td>'
        f'<td>{"已调正" if c["resolved"] else "<span class=flag>待调正</span>"}</td>'
        + _cross_cell(c) + '</tr>'
        for c in crosses)
    body = f"""<h1>收银拆扣与回收盒</h1>
    <p class="muted">收银成交后拆扣入绑定回收盒；同一交易只允许确认一次；投错盒即登记串台，调正后数量回盒。</p>
    <h2>回收盒存量</h2>
    <div class="card"><table><tr><th>回收盒</th><th class="num">扣具数</th><th>最近更新</th></tr>{brows}</table></div>
    <h2>回收盒串台</h2>
    <div class="card"><table><tr><th>事件</th><th>应投 → 实投</th><th>扣型</th>
    <th class="num">数量</th><th>状态</th><th></th></tr>{crows}</table></div>
    <h2>收银流水与拆扣登记（收银）</h2>
    <div class="card"><table><tr><th>交易</th><th>成交时间</th><th>收银台</th><th>楼层</th>
    <th>商品</th><th class="num">件数</th><th>拆扣状态</th><th>操作（投入盒台号）</th></tr>{trows}</table></div>
    """
    return page("收银回收", body, current_user, users, "/recycle")


# ---------------- 出口报警 ----------------
def render_alarms(svc, store, current_user, users):
    rows = svc.conn.execute(
        "SELECT a.*, t.name tag_name FROM exit_alarms a "
        "LEFT JOIN tag_types t ON t.id=a.tag_type_id "
        "WHERE a.store_id=? ORDER BY a.status IN('open','no_txn') DESC, a.id DESC",
        (store["id"],)).fetchall()
    trs = "".join(
        f'<tr><td><a href="/alarm/{r["id"]}"><b>#{r["id"]}</b></a></td>'
        f'<td>{esc(r["gate"])}</td>'
        f'<td>{"#"+str(r["txn_id"]) if r["txn_id"] else "<span class=flag>无交易</span>"}</td>'
        f'<td>{esc(r["tag_name"] or "-")}</td><td>{badge(r["status"])}</td>'
        f'<td>{esc(r["register_no"] or "-")}</td>'
        f'<td class="small">{esc(r["fired_at"])}</td></tr>' for r in rows)
    body = f"""<h1>出口门禁报警</h1>
    <p class="muted">报警由门禁适配器上报，凭证存本地目录。状态使用徽章标识，请进入事件查看收银时间线复核。</p>
    <div class="card"><table><tr><th>事件</th><th>门禁</th><th>关联交易</th><th>扣具类型</th>
    <th>状态</th><th>复核台</th><th>触发时间</th></tr>{trs}</table></div>"""
    return page("出口报警", body, current_user, users, "/alarms")


def render_alarm_detail(svc, alarm_id, current_user, users):
    data = svc.checkout_timeline(alarm_id)
    if not data:
        return page("报警不存在", "<p>事件未找到。</p>", current_user, users, "/alarms")
    a, tt, txn, tl = data["alarm"], data["tag_type"], data["txn"], data["timeline"]
    tl_html = "".join(
        f'<li><div class="t">{esc(i["event"])} '
        f'<span class="small muted">{esc(i["time"])}</span></div>'
        f'<div class="d">{esc(i["detail"])}</div></li>' for i in tl)
    evidence = ""
    if a["evidence_path"]:
        try:
            with open(a["evidence_path"], encoding="utf-8", errors="replace") as f:
                evidence = f.read()
        except OSError:
            evidence = "（凭证文件不可读）"
    regs = ["1F-01", "1F-02", "2F-01", "2F-02", "3F-01"]
    reg_opts = "".join(f"<option>{r}</option>" for r in regs)
    if a["status"] in ("open", "no_txn"):
        if a["txn_id"]:
            actions = f"""
            <div class="card">
              <h2 style="margin-top:0">收银台复核操作</h2>
              <form method="post" action="/alarm/{a["id"]}/review" class="rowform">
                <label>复核台 <select name="register_no">{reg_opts}</select></label>
                <input name="note" placeholder="复核备注" style="width:230px">
                <button name="verdict" value="miss">确认漏拆并回收</button>
                <button class="subtle" name="verdict" value="false">判为误报</button>
              </form>
              <p class="small">确认漏拆：自动登记该交易的拆扣回收（每交易仅一次），扣具入复核台回收盒并回补库存。</p>
            </div>"""
        else:
            actions = f"""
            <div class="card">
              <h2 style="margin-top:0">无交易报警 · 安保核查</h2>
              <form method="post" action="/alarm/{a["id"]}/no-txn" class="rowform">
                <input name="note" placeholder="核查情况（如：未购商品夹带已拦下 / 设备误报）" style="width:320px">
                <button class="ghost">登记核查并关闭</button></form>
              <p class="small">无成交记录不能按漏拆回收，必须先核查再关闭。</p>
            </div>"""
    else:
        actions = f'<div class="card"><h2 style="margin-top:0">处理结果</h2>' \
                  f'<p>{badge(a["status"])} 复核台 {esc(a["register_no"] or "-")} · ' \
                  f'{esc(a["closed_at"] or "")}</p><p class="muted">{esc(a["note"])}</p></div>'
    body = f"""
    <p><a href="/alarms">← 返回报警列表</a></p>
    <h1>报警事件 #{a["id"]}　{badge(a["status"])}</h1>
    <div class="twocol">
      <div>
        <div class="card">
          <h2 style="margin-top:0">收银时间线</h2>
          <div class="small muted" style="margin-bottom:10px">
            门禁：{esc(a["gate"])} ｜ 触发：{esc(a["fired_at"])} ｜
            扣具类型：<span class="flag">{esc(tt["name"] if tt else "-")}</span>
            {f'｜ 关联交易：#{a["txn_id"]}' if a["txn_id"] else '｜ <span class="flag">无关联交易</span>'}
          </div>
          <ol class="timeline">{tl_html}</ol>
        </div>
        {actions}
      </div>
      <div>
        <div class="card"><h2 style="margin-top:0">门禁凭证（本地目录）</h2>
          <div class="evidence">{esc(evidence)}</div>
          <p class="small" style="word-break:break-all">{esc(a["evidence_path"] or "")}</p></div>
        <div class="card"><h2 style="margin-top:0">报警备注</h2>
          <p class="muted">{esc(a["note"] or "（无）")}</p></div>
      </div>
    </div>"""
    return page(f"报警 #{a['id']}", body, current_user, users, "/alarms")


# ---------------- 消磁故障 ----------------
def render_faults(svc, store, current_user, users):
    rows = svc.conn.execute(
        "SELECT f.*, CASE WHEN f.txn_id IS NOT NULL THEN '交易 #'||f.txn_id ELSE '—' END tx "
        "FROM deactivator_faults f WHERE f.store_id=? ORDER BY f.handled, f.id DESC",
        (store["id"],)).fetchall()
    def _fault_cell(r):
        if r["handled"]:
            return "<td></td>"
        return (f'<td><form method="post" action="/fault/handle" class="inline">'
                f'<input type="hidden" name="fault_id" value="{r["id"]}">'
                f'<input name="note" placeholder="处理说明" style="width:180px">'
                f'<button class="subtle">处理</button></form></td>')

    trs = "".join(
        f'<tr><td>#{r["id"]}</td><td>{esc(r["register_no"])}</td>'
        f'<td>{"消磁失败" if r["kind"]=="deauth_fail" else "设备故障"}</td>'
        f'<td>{esc(r["tx"])}</td><td class="num">{r["qty"]}</td>'
        f'<td>{"已处理" if r["handled"] else "<span class=flag>待处理</span>"}</td>'
        f'<td class="small muted">{esc(r["note"])}</td>'
        + _fault_cell(r) + '</tr>' for r in rows)
    body = f"""<h1>消磁器故障</h1>
    <p class="muted">单笔消磁失败拦截拆扣并等待出口漏拆流程；设备故障登记维修/更换。</p>
    <div class="card"><table><tr><th>故障单</th><th>收银台</th><th>类型</th><th>关联</th>
    <th class="num">数量</th><th>状态</th><th>说明</th><th></th></tr>{trs}</table></div>"""
    return page("消磁故障", body, current_user, users, "/faults")


# ---------------- 闭店平衡（数量链） ----------------
def render_closing(svc, store, current_user, users, business_date):
    rows = svc.conn.execute(
        "SELECT c.*, t.name tag_name FROM closing_counts c "
        "JOIN tag_types t ON t.id=c.tag_type_id "
        "WHERE c.store_id=? AND c.business_date=? ORDER BY c.floor, t.id",
        (store["id"], business_date)).fetchall()
    if not rows:
        body = f"""<h1>闭店平衡 · {esc(business_date)}</h1>
        <div class="card"><p class="muted">今日清点尚未生成。可由总览或下方按钮触发生成（进程内任务每日 21:50 自动执行）。</p>
        <form method="post" action="/scheduler/run-closing"><button>立即生成闭店清点（店长）</button></form></div>"""
        return page("闭店平衡", body, current_user, users, "/closing")
    cards = []
    for r in rows:
        warn = "" if r["balanced"] else '<span class="badge" style="color:#b04800;background:#fdeede">不平衡</span>'
        ok = '<span class="badge" style="color:#146c39;background:#e6f6ec">平衡</span>' if r["balanced"] else ""
        diff_txt = (f'<span class="flag">差异 {r["diff"]:+d}</span>' if r["diff"] else
                    '<span class="small muted">差异 0</span>')
        cards.append(f"""
        <div class="card">
          <div style="display:flex;align-items:center;gap:10px;margin-bottom:12px">
            <b>{esc(r["floor"])} · {esc(r["tag_name"])}</b> {ok}{warn} {diff_txt}
          </div>
          <div class="chain">
            <div class="node"><div class="v">{r["requisition_qty"]}</div><div class="k">楼层领用</div></div>
            <div class="arrow">→</div>
            <div class="node"><div class="v">{r["attached_qty"]}</div><div class="k">商品挂扣</div></div>
            <div class="arrow">→</div>
            <div class="node mg"><div class="v">{r["detached_qty"]}</div><div class="k">收银拆回</div></div>
            <div class="arrow">→</div>
            <div class="node"><div class="v">{r["on_shelf_qty"]}</div><div class="k">实盘在架</div></div>
            <div class="arrow">·</div>
            <div class="node"><div class="v">{r["damaged_qty"]}</div><div class="k">在架报损</div></div>
          </div>
          <div class="small muted">
            校验式：挂扣 {r["attached_qty"]} = 拆回 {r["detached_qty"]} + 在架 {r["on_shelf_qty"]}
            ＋ 在架报损 {r["damaged_qty"]} ＋ 差异 {r["diff"]:+d}；
            领用平衡：领用 {r["requisition_qty"]} ＋ 拆回 {r["detached_qty"]}
            = 挂扣 {r["attached_qty"]} ＋ 楼层余存 {r["floor_left_qty"]}
            ＋ 备用扣报损 {r["spare_damaged_qty"]}
          </div>
        </div>""")
    body = f"""<h1>闭店平衡 · {esc(business_date)}</h1>
    <p class="muted">每个「楼层 × 扣型」一条从领用到回收的数量链；不平衡行生成盘差单，由店长在盘差审批处理。</p>
    <form method="post" action="/scheduler/run-closing" style="margin-bottom:14px">
      <button class="ghost">重新生成今日清点（店长）</button></form>
    {"".join(cards)}"""
    return page("闭店平衡", body, current_user, users, "/closing")


# ---------------- 盘差审批 ----------------
def render_adjustments(svc, store, current_user, users):
    rows = svc.conn.execute(
        "SELECT a.*, t.name tag_name, u.name req_name, m.name rev_name "
        "FROM inventory_adjustments a JOIN tag_types t ON t.id=a.tag_type_id "
        "JOIN users u ON u.id=a.requested_by "
        "LEFT JOIN users m ON m.id=a.reviewed_by WHERE a.store_id=? ORDER BY "
        "a.status='pending' DESC, a.id DESC", (store["id"],)).fetchall()
    def _adj_cell(r):
        if r["status"] != "pending":
            return "<td></td>"
        return (f'<td><form method="post" action="/adjust/{r["id"]}" class="inline">'
                f'<input name="reason" placeholder="审批意见" style="width:150px">'
                f'<button name="approve" value="1">批准</button> '
                f'<button class="subtle" name="approve" value="0">驳回</button>'
                f'</form></td>')

    trs = "".join(
        f'<tr><td>#{r["id"]}</td><td>{esc(r["floor"])}</td><td>{esc(r["tag_name"])}</td>'
        f'<td class="num"><b class="{"flag" if r["diff"]<0 else ""}">{r["diff"]:+d}</b></td>'
        f'<td>{badge(r["status"])}</td><td>{esc(r["req_name"])}</td>'
        f'<td>{esc(r["rev_name"] or "—")}</td><td class="small muted">{esc(r["reason"])}</td>'
        + _adj_cell(r) + '</tr>' for r in rows)
    body = f"""<h1>盘点差异审批</h1>
    <p class="muted">仅店长可批准；批准后按实盘调整楼层库存（盘盈补入 / 盘亏出账），
    该行闭店平衡转为平衡并写数量流水。导购与收银无此权限。</p>
    <div class="card"><table><tr><th>审批单</th><th>楼层</th><th>扣型</th>
    <th class="num">差异</th><th>状态</th><th>发起人</th><th>审批人</th><th>原因</th><th></th></tr>
    {trs}</table></div>"""
    return page("盘差审批", body, current_user, users, "/adjustments")


# ---------------- 数量流水 ----------------
def render_ledger(svc, store, current_user, users):
    rows = svc.conn.execute(
        "SELECT l.*, t.name tag_name, u.name uname FROM stock_ledger l "
        "JOIN tag_types t ON t.id=l.tag_type_id LEFT JOIN users u ON u.id=l.created_by "
        "WHERE l.store_id=? ORDER BY l.id DESC LIMIT(300)", (store["id"],)).fetchall()
    trs = "".join(
        f'<tr><td>{r["id"]}</td><td>{esc(r["floor"])}</td><td>{esc(r["tag_name"])}</td>'
        f'<td class="num" style="color:{"#146c39" if r["change_qty"]>=0 else "#a32626"}">'
        f'{r["change_qty"]:+d}</td><td class="num">{r["balance_after"]}</td>'
        f'<td>{esc(r["ref_type"])}{("#"+str(r["ref_id"])) if r["ref_id"] else ""}</td>'
        f'<td>{esc(r["uname"] or "—")}</td><td class="small">{esc(r["created_at"])}</td></tr>'
        for r in rows)
    body = f"""<h1>数量流水</h1>
    <p class="muted">所有库存增减的不可变凭证：issue 入库 / requisition 领用 / attach 挂扣出账 /
    detach 拆回 / damage 报损 / adjust 盘差调整。</p>
    <div class="card"><table><tr><th>#</th><th>位置</th><th>扣型</th><th class="num">变动</th>
    <th class="num">结余</th><th>来源</th><th>操作人</th><th>时间</th></tr>{trs}</table></div>"""
    return page("数量流水", body, current_user, users, "/ledger")
