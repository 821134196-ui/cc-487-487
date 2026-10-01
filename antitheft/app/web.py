# -*- coding: utf-8 -*-
"""本地 Web 服务：库存窄表 / 楼层领用 / 挂扣批次 / 收银回收 /
出口报警复核 / 消磁故障 / 闭店平衡数量链 / 盘差审批。

零依赖，python3 -m app.web 启动。配色：门店白底、雅黑、醒目洋红。
"""
import html
import json
import urllib.parse
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import db as dbmod
from . import services
from .errors import (DuplicateEvent, PermissionDenied, ServiceError,
                     VersionConflict)

MAGENTA = '#e6007e'
STORE_ID = 1

CSS = """
:root{--mg:#e6007e;--ink:#202124;--muted:#6b7075;--line:#e8e8ea;--bg:#f6f6f8;--ok:#15803d;--warn:#b45309;--err:#b91c1c;}
*{box-sizing:border-box;}
body{margin:0;background:var(--bg);color:var(--ink);
 font-family:"Microsoft YaHei","微软雅黑","PingFang SC","Hiragino Sans GB",sans-serif;}
header.top{background:#fff;border-bottom:3px solid var(--mg);padding:0 24px;
 display:flex;align-items:center;gap:20px;position:sticky;top:0;z-index:5;}
.brand{font-size:19px;font-weight:700;color:var(--ink);padding:14px 0;}
.brand b{color:var(--mg);}
nav{display:flex;gap:4px;flex-wrap:wrap;}
nav a{color:#444;text-decoration:none;padding:16px 12px;font-size:14px;border-bottom:3px solid transparent;}
nav a:hover{color:var(--mg);}
nav a.on{color:var(--mg);border-bottom-color:var(--mg);font-weight:700;}
.who{margin-left:auto;font-size:13px;color:var(--muted);display:flex;align-items:center;gap:8px;}
.who select{padding:5px 8px;border:1px solid var(--line);border-radius:6px;font-family:inherit;}
main{max-width:1180px;margin:22px auto;padding:0 24px 60px;}
h2{font-size:18px;margin:0 0 4px;}
.sub{color:var(--muted);font-size:13px;margin-bottom:16px;}
.card{background:#fff;border:1px solid var(--line);border-radius:10px;padding:18px 20px;margin-bottom:18px;}
table{border-collapse:collapse;width:100%;font-size:13.5px;}
th,td{padding:9px 10px;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap;}
th{color:var(--muted);font-weight:600;background:#fafafa;}
tr:last-child td{border-bottom:none;}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums;}
.chip{display:inline-block;padding:2px 9px;border-radius:999px;font-size:12px;font-weight:600;}
.chip.ok{background:#e8f6ee;color:var(--ok);}
.chip.warn{background:#fdf1e1;color:var(--warn);}
.chip.err{background:#fbeaea;color:var(--err);}
.chip.mg{background:#fde7f3;color:var(--mg);}
.chip.grey{background:#eeeef0;color:#555;}
.btn{display:inline-block;border:1px solid var(--mg);background:var(--mg);color:#fff;
 padding:6px 14px;border-radius:7px;font-size:13px;cursor:pointer;font-family:inherit;text-decoration:none;}
.btn.ghost{background:#fff;color:var(--mg);}
.btn.mini{padding:3px 10px;font-size:12px;}
.btn:disabled{opacity:.45;cursor:not-allowed;}
.flash{padding:10px 14px;border-radius:8px;margin-bottom:14px;font-size:13.5px;background:#fde7f3;color:#9b0b59;border:1px solid #f6c3e0;}
.flash.err{background:#fbeaea;color:var(--err);border-color:#f1c6c6;}
.kpis{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:18px;}
.kpi{background:#fff;border:1px solid var(--line);border-radius:10px;padding:14px 18px;min-width:150px;}
.kpi .v{font-size:24px;font-weight:700;}
.kpi .v.mg{color:var(--mg);}
.kpi .l{font-size:12.5px;color:var(--muted);margin-top:2px;}
/* 闭店数量链 */
.chain{display:flex;align-items:stretch;gap:0;overflow-x:auto;padding:6px 0 14px;}
.node{background:#fff;border:1px solid var(--line);border-radius:10px;padding:10px 14px;min-width:104px;text-align:center;}
.node .n{font-size:21px;font-weight:700;}
.node .t{font-size:12px;color:var(--muted);margin-top:3px;}
.node.hl{border-color:var(--mg);box-shadow:0 0 0 1px var(--mg) inset;}
.node.hl .n{color:var(--mg);}
.arrow{display:flex;align-items:center;color:#b9bcc1;font-size:20px;padding:0 8px;}
/* 报警复核：白底卡片 + 洋红强调，避免整屏红色 */
.alarm-grid{display:grid;grid-template-columns:340px 1fr;gap:18px;align-items:start;}
.alarm-list a{display:block;text-decoration:none;color:inherit;border:1px solid var(--line);
 border-radius:10px;padding:12px 14px;margin-bottom:10px;background:#fff;}
.alarm-list a.on{border-color:var(--mg);box-shadow:0 0 0 1px var(--mg) inset;}
.alarm-list .code{font-weight:700;color:var(--ink);}
.timeline{list-style:none;margin:8px 0 0;padding:0;}
.timeline li{position:relative;padding:0 0 16px 22px;border-left:2px solid var(--line);margin-left:6px;}
.timeline li::before{content:'';position:absolute;left:-6px;top:3px;width:10px;height:10px;border-radius:50%;
 background:#c9ccd1;border:2px solid #fff;}
.timeline li.hot::before{background:var(--mg);}
.timeline .time{font-size:12px;color:var(--muted);}
.timeline .ev{font-size:13.5px;margin-top:2px;}
form.inline{display:inline;}
.muted{color:var(--muted);}
.right{text-align:right;}
.tagline{font-size:12px;color:var(--muted);}
.evi{max-width:300px;border-radius:8px;border:1px solid var(--line);}
"""


def esc(v):
    return html.escape('' if v is None else str(v))


def chip(status):
    m = {
        'OK': ('ok', '正常'), 'SHORT': ('warn', '短缺'), 'MISMATCH': ('err', '扣型不匹配'),
        'BALANCED': ('ok', '平衡'), 'PENDING_VARIANCE': ('warn', '差异待处理'),
        'OPEN': ('warn', '待处理'), 'CONFIRMED_MISSED': ('mg', '确认漏拆已回收'),
        'FALSE_ALARM': ('grey', '误报关闭'), 'NO_TXN_ESCALATED': ('err', '无交易升级'),
        'PENDING': ('warn', '待店长批准'), 'APPROVED': ('ok', '已批准'),
        'REJECTED': ('grey', '已驳回'), 'RESOLVED': ('ok', '已恢复'),
        'FAULT': ('err', '故障'), 'PAID': ('ok', '已成交'), 'ACTIVE': ('ok', '在用'),
        'SEALED': ('grey', '已封盒'), 'AUTO': ('ok', '消磁'), 'MANUAL': ('warn', '手工拆'),
    }
    cls, text = m.get(status, ('grey', status))
    return f'<span class="chip {cls}">{text}</span>'


class App:
    def __init__(self, db_path, evidence_dir):
        self.db_path = db_path
        self.evidence_dir = evidence_dir

    def conn(self):
        return dbmod.connect(self.db_path)


def rows(conn, sql, args=()):
    return conn.execute(sql, args).fetchall()


# ---------------------------------------------------------------- 页面渲染
def page_shell(title, nav_on, uid, body, flash=None):
    navs = [
        ('/', '总览', 'home'),
        ('/inventory', '扣具库存', 'inventory'),
        ('/issues', '楼层领用', 'issues'),
        ('/batches', '挂扣批次', 'batches'),
        ('/pos', '收银回收', 'pos'),
        ('/alarms', '出口报警', 'alarms'),
        ('/faults', '消磁故障', 'faults'),
        ('/close', '闭店平衡', 'close'),
        ('/variance', '盘差审批', 'variance'),
        ('/incidents', '异常事件', 'incidents'),
    ]
    nav_html = ''.join(
        f'<a href="{u}" class="{"on" if k == nav_on else ""}">{n}</a>'
        for u, n, k in navs)
    users = [
        (1, '顾晓雯·导购', 'ASSOCIATE'), (3, '尹收银·收银', 'CASHIER'),
        (5, '章店长·店长', 'MANAGER')]
    uopts = ''.join(
        f'<option value="{i}" {"selected" if i == uid else ""}>{n}</option>'
        for i, n, _ in users)
    role = {1: '导购 ASSOCIATE', 3: '收银 CASHIER', 5: '店长 MANAGER'}.get(uid, '')
    flash_html = f'<div class="flash {"err" if flash and flash[0] else ""}">{esc(flash[1])}</div>' if flash else ''
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)}｜防盗扣复核系统</title><style>{CSS}</style></head><body>
<header class="top">
 <div class="brand">云裳·<b>防盗扣</b>领用与漏拆复核</div>
 <nav>{nav_html}</nav>
 <div class="who"><span>{esc(role)}</span>
  <form method="post" action="/switch-user" class="inline">
   <select name="uid" onchange="this.form.submit()">{uopts}</select></form>
 </div>
</header>
<main><h2>{esc(title)}</h2>{flash_html}{body}</main>
</body></html>"""


def render_home(app, conn):
    kpis = {}
    kpis['open_alarm'] = conn.execute(
        "SELECT COUNT(*) c FROM alarms WHERE status='OPEN'").fetchone()['c']
    kpis['pending_var'] = conn.execute(
        "SELECT COUNT(*) c FROM variance_requests WHERE status='PENDING'").fetchone()['c']
    kpis['open_fault'] = conn.execute(
        "SELECT COUNT(*) c FROM deactivator_faults WHERE status='OPEN'").fetchone()['c']
    stock = conn.execute(
        "SELECT COALESCE(SUM(qty),0) q FROM inventory WHERE scope='STOCK'").fetchone()['q']
    box = conn.execute(
        "SELECT COALESCE(SUM(qty),0) q FROM inventory WHERE scope='BOX'").fetchone()['q']
    kpi_cards = f"""
    <div class="kpis">
      <div class="kpi"><div class="v {'mg' if kpis['open_alarm'] else ''}">{kpis['open_alarm']}</div><div class="l">出口报警待复核</div></div>
      <div class="kpi"><div class="v {'mg' if kpis['pending_var'] else ''}">{kpis['pending_var']}</div><div class="l">盘差单待店长批准</div></div>
      <div class="kpi"><div class="v">{kpis['open_fault']}</div><div class="l">消磁器故障未恢复</div></div>
      <div class="kpi"><div class="v">{stock}</div><div class="l">总仓可用扣具</div></div>
      <div class="kpi"><div class="v">{box}</div><div class="l">回收盒累计收回</div></div>
    </div>"""
    al = rows(conn, """
      SELECT a.*, t.name tname, t.code tcode, g.code gate FROM alarms a
      LEFT JOIN tag_types t ON t.id=a.tag_type_id, floors g ON 1=1
      WHERE a.status='OPEN' ORDER BY a.ts DESC LIMIT 5""")
    li = ''.join(
        f'<li><a href="/alarms/{r["id"]}">{esc(r["code"])}</a> '
        f'{chip(r["kind"])} {esc(r["tname"] or "扣型未知")} · {esc(r["ts"][11:16])}</li>'
        for r in al) or '<li class="muted">暂无待复核报警</li>'
    inc = rows(conn, """
      SELECT * FROM incidents ORDER BY id DESC LIMIT 8""")
    il = ''.join(
        f'<tr><td>{esc(r["ts"][11:16])}</td><td>{esc(r["kind"])}</td>'
        f'<td>{esc(r["message"])}</td></tr>' for r in inc)
    body = f"""{kpi_cards}
    <div class="card"><h2>待复核出口报警</h2><div class="sub">报警复核页优先展示收银时间线与扣具类型</div>
      <table><tbody>{''.join(f'<tr>{x}</tr>' for x in [])}</tbody></table>
      <ul class="timeline">{li}</ul></div>
    <div class="card"><h2>最近异常事件</h2>
      <table><thead><tr><th>时间</th><th>类型</th><th>说明</th></tr></thead><tbody>{il}</tbody></table>
    </div>"""
    return page_shell('总览', 'home', app.uid, body, app.flash)


def render_inventory(app, conn):
    data = rows(conn, """
      SELECT t.name tname, t.code tcode, i.scope, f.name fname, b.code bcode, i.qty, i.version
      FROM inventory i JOIN tag_types t ON t.id=i.tag_type_id
      LEFT JOIN floors f ON f.id=i.floor_id AND i.scope IN ('FLOOR','ONGOODS')
      LEFT JOIN recovery_boxes b ON b.id=i.box_id AND i.scope='BOX'
      WHERE i.store_id=? ORDER BY t.id, i.scope, f.seq""", (STORE_ID,))
    scope_name = {'STOCK': '总仓', 'FLOOR': '楼层散扣', 'ONGOODS': '在架商品',
                  'BOX': '回收盒', 'DAMAGED': '报损', 'ADJUST': '盘差调整'}
    trs = ''.join(
        f"""<tr><td>{esc(r['tname'])}</td><td class="muted">{esc(r['tcode'])}</td>
        <td>{esc(scope_name[r['scope']])}</td><td>{esc(r['fname'] or (r['bcode'] or '—'))}</td>
        <td class="num">{r['qty']}</td><td class="num muted">v{r['version']}</td></tr>"""
        for r in data)
    totals = rows(conn, """
      SELECT t.name tname,
        SUM(CASE WHEN scope='STOCK' THEN qty ELSE 0 END) stock,
        SUM(CASE WHEN scope='FLOOR' THEN qty ELSE 0 END) loose,
        SUM(CASE WHEN scope='ONGOODS' THEN qty ELSE 0 END) ong,
        SUM(CASE WHEN scope='BOX' THEN qty ELSE 0 END) box,
        SUM(CASE WHEN scope='DAMAGED' THEN qty ELSE 0 END) dmg
      FROM inventory i JOIN tag_types t ON t.id=i.tag_type_id WHERE i.store_id=?
      GROUP BY t.id ORDER BY t.id""", (STORE_ID,))
    tot = ''.join(
        f"""<tr><td>{esc(r['tname'])}</td><td class="num">{r['stock']}</td>
        <td class="num">{r['loose']}</td><td class="num">{r['ong']}</td>
        <td class="num">{r['box']}</td><td class="num">{r['dmg']}</td></tr>"""
        for r in totals)
    body = f"""
    <div class="sub">库存窄表：每行一个「扣型 × 位置」组合，数量变动均带版本号（乐观锁）。</div>
    <div class="card"><h2>按类型汇总</h2>
      <table><thead><tr><th>扣型</th><th class="num">总仓</th><th class="num">楼层散扣</th>
      <th class="num">在架商品</th><th class="num">回收盒</th><th class="num">报损</th></tr></thead>
      <tbody>{tot}</tbody></table></div>
    <div class="card"><h2>类型 × 楼层 窄表明细</h2>
      <table><thead><tr><th>扣型</th><th>代码</th><th>位置类型</th><th>楼层/回收盒</th>
      <th class="num">数量</th><th class="num">版本</th></tr></thead><tbody>{trs}</tbody></table>
    </div>"""
    return page_shell('扣具库存', 'inventory', app.uid, body, app.flash)


def render_issues(app, conn):
    rs = rows(conn, """
      SELECT i.*, f.name fname, t.name tname FROM tag_issues i
      JOIN floors f ON f.id=i.floor_id JOIN tag_types t ON t.id=i.tag_type_id
      ORDER BY i.ts""")
    trs = ''.join(
        f"""<tr><td>{esc(r['code'])}</td><td>{esc(r['fname'])}</td><td>{esc(r['tname'])}</td>
        <td class="num">{r['qty_requested']}</td><td class="num">{r['qty_issued']}</td>
        <td>{chip(r['status'])}</td><td>{esc(r['operator'])}</td><td>{esc(r['ts'][11:16])}</td>
        <td class="muted">{esc(r['note'])}</td></tr>""" for r in rs)
    body = f"""<div class="sub">导购按到货批次从总仓领扣到楼层；总仓不足时按实发登记并产生「领用短缺」异常。</div>
    <div class="card"><table><thead><tr><th>领用单</th><th>楼层</th><th>扣型</th>
    <th class="num">申请</th><th class="num">实发</th><th>状态</th><th>操作人</th>
    <th>时间</th><th>备注</th></tr></thead><tbody>{trs}</tbody></table></div>"""
    return page_shell('楼层领用', 'issues', app.uid, body, app.flash)


def render_batches(app, conn):
    rs = rows(conn, """
      SELECT b.*, f.name fname, t.name tname, p.sku, p.name pname FROM tag_batches b
      JOIN floors f ON f.id=b.floor_id JOIN tag_types t ON t.id=b.tag_type_id
      JOIN products p ON p.id=b.product_id ORDER BY b.ts""")
    trs = ''.join(
        f"""<tr><td>{esc(r['code'])}</td><td>{esc(r['fname'])}</td>
        <td>{esc(r['sku'])} {esc(r['pname'])}</td><td>{esc(r['tname'])}</td>
        <td class="num">{r['expected_qty']}</td><td class="num">{r['attached_qty']}</td>
        <td>{chip(r['status'])}</td><td class="muted">{esc(r['note'])}</td></tr>""" for r in rs)
    body = f"""<div class="sub">到货商品按批次登记应挂/实挂数量；扣型不匹配的批次不扣库存，只登记异常。</div>
    <div class="card"><table><thead><tr><th>批次</th><th>楼层</th><th>商品</th>
    <th>实领扣型</th><th class="num">应挂</th><th class="num">实挂</th><th>状态</th>
    <th>备注</th></tr></thead><tbody>{trs}</tbody></table></div>"""
    return page_shell('挂扣批次', 'batches', app.uid, body, app.flash)


def render_pos(app, conn):
    txns = rows(conn, """
      SELECT x.*, r.name rname, f.name fname, u.display_name cashier
      FROM txns x JOIN registers r ON r.id=x.register_id
      JOIN floors f ON f.id=x.floor_id JOIN users u ON u.id=x.cashier_id
      ORDER BY x.ts""")
    cards = []
    for x in txns:
        items = rows(conn, """
          SELECT i.qty, p.sku, p.name pname, t.name tname
          FROM txn_items i JOIN products p ON p.id=i.product_id
          JOIN tag_types t ON t.id=i.tag_type_id WHERE i.txn_id=? ORDER BY i.id""", (x['id'],))
        de = conn.execute("""
          SELECT e.*, b.code bcode FROM detach_events e
          JOIN recovery_boxes b ON b.id=e.box_id
          WHERE e.txn_id=? AND e.source='POS'""", (x['id'],)).fetchone()
        ditems = rows(conn, """
          SELECT i.qty, t.name tname FROM detach_event_items i
          JOIN tag_types t ON t.id=i.tag_type_id WHERE i.detach_id=?""",
                      (de['id'],)) if de else []
        sold_total = sum(i['qty'] for i in items)
        det_total = sum(i['qty'] for i in ditems)
        missed = sold_total - det_total
        badge = (f'<span class="chip err">漏拆 {missed}</span>' if missed > 0
                 else '<span class="chip ok">已清扣</span>')
        il = ''.join(
            f'<div class="tagline">{esc(i["sku"])} {esc(i["pname"])} ×{i["qty"]} · {esc(i["tname"])}</div>'
            for i in items)
        dl = ''.join(
            f'<div class="tagline">{esc(i["tname"])} ×{i["qty"]}</div>' for i in ditems) \
            or '<div class="tagline muted">无拆扣记录</div>'
        cards.append(f"""
        <div class="card">
          <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;">
            <b>{esc(x['code'])}</b> {badge}
            <span class="muted">{esc(x['rname'])} · {esc(x['cashier'])} · {esc(x['ts'][11:16])}</span>
            <span style="margin-left:auto" class="muted">¥{x['amount']:.2f}</span>
            {chip(de['mode']) if de else ''}
            <span class="chip grey">回收盒 {esc(de['bcode']) if de else '—'}</span>
          </div>
          <div style="display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-top:10px;">
            <div><div class="tagline">售出（应拆 {sold_total}）</div>{il}</div>
            <div><div class="tagline">拆扣回收（实拆 {det_total}）</div>{dl}</div>
          </div>
        </div>""")
    body = f"""<div class="sub">收银成交即登记拆扣回收；同一交易的拆扣事件只允许确认一次，漏拆交易会醒目标注。</div>
    {''.join(cards) or '<div class="card muted">暂无交易</div>'}"""
    return page_shell('收银回收', 'pos', app.uid, body, app.flash)


def _alarm_timeline(conn, alarm):
    """收银时间线：报文中的成交、拆扣、故障与本次报警按时间排列。"""
    tl = []
    if alarm['txn_id']:
        txn = conn.execute("""
          SELECT x.*, r.name rname, u.display_name cashier
          FROM txns x JOIN registers r ON r.id=x.register_id
          JOIN users u ON u.id=x.cashier_id WHERE x.id=?""",
                           (alarm['txn_id'],)).fetchone()
        tl.append((txn['ts'], False,
                   f'收银成交 {txn["code"]}｜{txn["rname"]}｜{txn["cashier"]}｜¥{txn["amount"]:.2f}'))
        items = rows(conn, """
          SELECT ti.qty, p.sku, p.name pname, t.name tname
          FROM txn_items ti JOIN products p ON p.id=ti.product_id
          JOIN tag_types t ON t.id=ti.tag_type_id WHERE ti.txn_id=?""",
                     (txn['id'],))
        for it in items:
            tl.append((txn['ts'], False,
                       f'售出 {it["sku"]} {it["pname"]} ×{it["qty"]}（应配 {it["tname"]}）'))
        de = conn.execute("SELECT * FROM detach_events WHERE txn_id=? AND source='POS'",
                          (txn['id'],)).fetchone()
        if de:
            ditems = rows(conn, """
              SELECT i.qty, t.name tname FROM detach_event_items i
              JOIN tag_types t ON t.id=i.tag_type_id WHERE i.detach_id=?""", (de['id'],))
            det = '、'.join(f'{d["tname"]} ×{d["qty"]}' for d in ditems)
            tl.append((de['ts'], False,
                       f'收银拆扣回收 {de["code"]}｜{det}｜{("手工拆扣" if de["mode"]=="MANUAL" else "消磁拆除")}'))
            sold_total = sum(i['qty'] for i in items)
            det_total = sum(d['qty'] for d in ditems)
            if sold_total > det_total:
                tl.append((de['ts'], True,
                           f'漏拆 {sold_total - det_total} 个：售出 {sold_total}，仅拆回 {det_total}'))
    tl.append((alarm['ts'], True,
               f'出口 {alarm["gate"]} 报警触发（{"关联交易" if alarm["kind"]=="MATCHED_TXN" else "无关联交易"}）'))
    if alarm['resolved_ts']:
        tl.append((alarm['resolved_ts'], False,
                   f'事件关闭：{chip(alarm["status"])}｜{esc(alarm["resolution_note"] or "")}'))
    tl.sort(key=lambda z: z[0])
    return tl


def render_alarms(app, conn, alarm_id=None):
    alarms = rows(conn, """
      SELECT a.*, t.name tname FROM alarms a LEFT JOIN tag_types t ON t.id=a.tag_type_id
      ORDER BY (a.status='OPEN') DESC, a.ts DESC""")
    if alarm_id is None:
        open_first = alarms[0] if alarms else None
        alarm_id = open_first['id'] if open_first else None
    if alarm_id is None:
        body = '<div class="card muted">暂无出口报警</div>'
        return page_shell('出口报警复核', 'alarms', app.uid, body, app.flash)

    alarm = conn.execute("""
      SELECT a.*, t.name tname, t.code tcode, x.code txn_code
      FROM alarms a LEFT JOIN tag_types t ON t.id=a.tag_type_id
      LEFT JOIN txns x ON x.id=a.txn_id WHERE a.id=?""", (alarm_id,)).fetchone()
    list_html = ''.join(
        f"""<a href="/alarms/{r['id']}" class="{"on" if r['id']==alarm_id else ""}">
          <div style="display:flex;justify-content:space-between;gap:8px;">
            <span class="code">{esc(r['code'])}</span>{chip(r['status'])}</div>
          <div class="tagline">{esc(r['gate'])} · {esc(r['ts'][11:16])}</div>
          <div class="tagline">扣型：{esc(r['tname'] or '未知')}</div></a>"""
        for r in alarms)

    tl = _alarm_timeline(conn, alarm)
    tl_html = ''.join(
        f"""<li class="{"hot" if hot else ""}"><div class="time">{esc(t[11:16])}</div>
        <div class="ev">{txt}</div></li>""" for t, hot, txt in tl)

    missed_html = ''
    if alarm['kind'] == 'MATCHED_TXN' and alarm['status'] == 'OPEN':
        missed = services._missed_by_type(conn, alarm['txn_id'])
        if missed:
            names = []
            for ttid, q in missed.items():
                tn = conn.execute('SELECT name FROM tag_types WHERE id=?', (ttid,)).fetchone()
                names.append(f'{tn["name"]} ×{q}')
            missed_html = (
                '<div class="flash">按收银时间线比对，系统判定漏拆：'
                f'<b>{"、".join(names)}</b>。确认后扣具回收到本台回收盒并关闭事件。</div>')
        else:
            missed_html = '<div class="flash err">该交易已全部拆扣，无漏拆可回收（拦截重复操作）。</div>'

    if alarm['status'] == 'OPEN':
        if alarm['kind'] == 'MATCHED_TXN':
            actions = f"""
            <form method="post" action="/alarms/{alarm['id']}/resolve" class="inline">
              <input type="hidden" name="action" value="CONFIRMED_MISSED">
              <button class="btn">确认漏拆并回收</button></form>
            <form method="post" action="/alarms/{alarm['id']}/resolve" class="inline">
              <input type="hidden" name="action" value="FALSE_ALARM">
              <button class="btn ghost">判为误报</button></form>"""
        else:
            actions = f"""
            <form method="post" action="/alarms/{alarm['id']}/resolve" class="inline">
              <input type="hidden" name="action" value="NO_TXN_ESCALATE">
              <button class="btn">升级防损/店长核查</button></form>
            <form method="post" action="/alarms/{alarm['id']}/resolve" class="inline">
              <input type="hidden" name="action" value="FALSE_ALARM">
              <button class="btn ghost">判为误报</button></form>"""
    else:
        actions = f'<div>事件已关闭：{chip(alarm["status"])}</div>'

    evi = ''
    if alarm['evidence_path']:
        import os
        if os.path.exists(alarm['evidence_path']):
            import urllib.request
            evi = f'<img class="evi" src="/evidence/{os.path.basename(alarm["evidence_path"])}" alt="报警凭证">'
    body = f"""
    <div class="alarm-grid">
      <div class="alarm-list">{list_html}</div>
      <div>
        <div class="card">
          <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;">
            <b style="font-size:16px;">{esc(alarm['code'])}</b>{chip(alarm['status'])}
            <span class="muted">{esc(alarm['gate'])} · {esc(alarm['ts'])}</span>
            <span style="margin-left:auto" class="chip mg">扣型 {esc(alarm['tname'] or '未知')}</span>
          </div>
          {missed_html}
          <div class="sub" style="margin-top:14px;">收银时间线（优先于报警本身，先看成交与拆扣再下结论）</div>
          <ul class="timeline">{tl_html}</ul>
          <div style="margin-top:14px;display:flex;gap:10px;align-items:center;flex-wrap:wrap;">
            {actions}
            <span class="tagline">复核操作限收银角色；同一事件只可处理一次。</span>
          </div>
        </div>
        <div class="card"><h2>报警凭证</h2>{evi or '<span class="muted">无凭证文件</span>'}</div>
      </div>
    </div>"""
    return page_shell('出口报警复核', 'alarms', app.uid, body, app.flash)


def render_faults(app, conn):
    rs = rows(conn, """
      SELECT f.*, d.code dcode, r.name rname, u.display_name handler
      FROM deactivator_faults f JOIN deactivators d ON d.id=f.deactivator_id
      JOIN registers r ON r.id=f.register_id LEFT JOIN users u ON u.id=f.handler_id
      ORDER BY f.ts""")
    trs = ''.join(
        f"""<tr><td>{esc(r['code'])}</td><td>{esc(r['dcode'])}</td><td>{esc(r['rname'])}</td>
        <td>{esc(r['fault_kind'])}</td><td>{esc(r['ts'][11:16])}</td>
        <td>{chip('OPEN' if r['status']=='OPEN' else 'RESOLVED')}</td>
        <td>{esc(r['handler'] or '—')}</td><td class="muted">{esc(r['note'])}</td></tr>"""
        for r in rs)
    body = f"""<div class="sub">消磁器故障期间交易自动转 <b>MANUAL 手工拆扣</b>；恢复由店长处理。</div>
    <div class="card"><table><thead><tr><th>故障单</th><th>消磁器</th><th>收银台</th>
    <th>故障类型</th><th>上报时间</th><th>状态</th><th>处理人</th><th>记录</th></tr></thead>
    <tbody>{trs}</tbody></table></div>"""
    return page_shell('消磁故障', 'faults', app.uid, body, app.flash)


def render_close(app, conn):
    bdate = conn.execute('SELECT MAX(business_date) d FROM close_counts').fetchone()['d']
    if not bdate:
        return page_shell('闭店平衡', 'close', app.uid,
                          '<div class="card muted">尚无闭店记录</div>', app.flash)
    rs = rows(conn, """
      SELECT c.*, f.name fname, t.name tname FROM close_counts c
      JOIN floors f ON f.id=c.floor_id JOIN tag_types t ON t.id=c.tag_type_id
      WHERE c.business_date=? ORDER BY f.seq, t.id""", (bdate,))
    blocks = []
    for r in rs:
        # 数量链：领用 → 挂扣 → 拆回 → 在架；散扣/报损作为挂扣去向并列
        chain = f"""
        <div class="chain">
          <div class="node"><div class="n">{r['issued_qty']}</div><div class="t">楼层领用</div></div>
          <div class="arrow">→</div>
          <div class="node"><div class="n">{r['attached_qty']}</div><div class="t">商品挂扣</div></div>
          <div class="arrow">→</div>
          <div class="node hl"><div class="n">{r['detached_qty']}</div><div class="t">收银拆回</div></div>
          <div class="arrow">→</div>
          <div class="node"><div class="n">{r['on_shelf_book']}</div><div class="t">在架</div></div>
        </div>"""
        equ1_ok = (r['issued_qty'] == r['attached_qty'] + r['loose_book']
                   + r['damaged_floor_qty'] + r['writeoff_floor_qty'])
        equ2_ok = (r['attached_qty'] == r['detached_qty'] + r['on_shelf_book']
                   + r['damaged_ongoods_qty'] + r['writeoff_ongoods_qty'])
        wo1 = f' ＋ 盘差调出 {r["writeoff_floor_qty"]}' if r['writeoff_floor_qty'] else ''
        wo2 = f' ＋ 盘差调出 {r["writeoff_ongoods_qty"]}' if r['writeoff_ongoods_qty'] else ''
        body_rows = f"""
          <div class="tagline">去向平衡① 领用 {r['issued_qty']} ＝ 挂扣 {r['attached_qty']}
            ＋ 楼层散扣 {r['loose_book']} ＋ 散扣报损 {r['damaged_floor_qty']}{wo1}
            {'<span class="chip ok">平</span>' if equ1_ok else '<span class="chip err">不平</span>'}</div>
          <div class="tagline">去向平衡② 挂扣 {r['attached_qty']} ＝ 拆回 {r['detached_qty']}
            ＋ 在架 {r['on_shelf_book']} ＋ 在架报损 {r['damaged_ongoods_qty']}{wo2}
            {'<span class="chip ok">平</span>' if equ2_ok else '<span class="chip err">不平</span>'}</div>
          <div class="tagline">实物清点：散扣 {r['loose_actual']}（账面 {r['loose_book']}）
            · 在架 {r['on_shelf_actual']}（账面 {r['on_shelf_book']}）
            · 盘差 <b>{r['variance_qty']:+d}</b></div>"""
        blocks.append(f"""
        <div class="card">
          <div style="display:flex;gap:10px;align-items:center;margin-bottom:8px;">
            <b>{esc(r['fname'])} · {esc(r['tname'])}</b>
            <span class="tagline">{esc(r['code'])}</span>
            <span style="margin-left:auto">{chip(r['status'])}</span>
          </div>
          {chain}{body_rows}
        </div>""")
    rerun = ('<form method="post" action="/close/rerun" class="inline" style="margin-bottom:14px;">'
             '<button class="btn ghost">重新生成当日清点</button></form>')
    body = f"""<div class="sub">营业日 {esc(bdate)}：一条从领用到回收的数量链；数量守恒逐段校验，差异转入盘差审批。</div>
    {rerun}{''.join(blocks)}"""
    return page_shell('闭店平衡', 'close', app.uid, body, app.flash)


def render_variance(app, conn):
    rs = rows(conn, """
      SELECT v.*, f.name fname, t.name tname,
             a.display_name applicant, p.display_name approver
      FROM variance_requests v JOIN floors f ON f.id=v.floor_id
      JOIN tag_types t ON t.id=v.tag_type_id JOIN users a ON a.id=v.applicant_id
      LEFT JOIN users p ON p.id=v.approver_id ORDER BY v.ts""")
    trs = []
    for r in rs:
        act = ''
        if r['status'] == 'PENDING':
            act = f"""
            <form method="post" action="/variance/{r['id']}/decide" class="inline">
              <input type="hidden" name="decision" value="APPROVED">
              <button class="btn mini">批准调账</button></form>
            <form method="post" action="/variance/{r['id']}/decide" class="inline">
              <input type="hidden" name="decision" value="REJECTED">
              <button class="btn mini ghost">驳回</button></form>"""
        trs.append(f"""<tr><td>{esc(r['code'])}</td><td>{esc(r['fname'])}</td>
        <td>{esc(r['tname'])}</td>
        <td>{'盘亏' if r['direction']=='SHORT' else '盘盈'} {r['qty']}</td>
        <td class="muted">{esc(r['reason'])}</td><td>{esc(r['applicant'])}</td>
        <td>{esc(r['approver'] or '—')}</td><td>{chip(r['status'])}</td><td>{act}</td></tr>""")
    body = """<div class="sub">盘差由导购/收银发起、店长批准（申请人与审批人必须分离）；批准后按盘亏/盘盈调整账面并写入流水。</div>
    <div class="card"><table><thead><tr><th>盘差单</th><th>楼层</th><th>扣型</th>
    <th>差异</th><th>原因</th><th>申请人</th><th>审批人</th><th>状态</th><th>操作</th></tr></thead>
    <tbody>""" + ''.join(trs) + "</tbody></table></div>"
    return page_shell('盘差审批', 'variance', app.uid, body, app.flash)


def render_incidents(app, conn):
    rs = rows(conn, "SELECT * FROM incidents ORDER BY id DESC LIMIT 60")
    trs = ''.join(
        f"""<tr><td>{esc(r['ts'][11:16])}</td><td>{chip('OK') if False else ''}
        <span class="chip {'err' if r['severity']=='ERROR' else 'warn'}">{esc(r['kind'])}</span></td>
        <td>{esc(r['message'])}</td><td>{esc(r['ref_type'])} {esc(r['ref_id'] or '')}</td></tr>"""
        for r in rs)
    body = f"""<div class="sub">扣型不匹配、领用短缺、回收盒串台、重复拆扣、消磁失败、无交易报警、扣具破损、盘点差异统一归集。</div>
    <div class="card"><table><thead><tr><th>时间</th><th>类型</th><th>说明</th><th>关联单据</th></tr></thead>
    <tbody>{trs}</tbody></table></div>"""
    return page_shell('异常事件', 'incidents', app.uid, body, app.flash)


# ---------------------------------------------------------------- HTTP 处理
class Handler(BaseHTTPRequestHandler):
    app = None
    scheduler = None

    def log_message(self, *a):
        pass

    # ---- 基础工具
    def _uid(self):
        cookie = SimpleCookie(self.headers.get('Cookie', ''))
        if 'uid' in cookie:
            try:
                return int(cookie['uid'].value)
            except ValueError:
                pass
        return 3  # 默认收银

    def _send(self, content, status=200, ctype='text/html; charset=utf-8',
              extra_headers=None):
        data = content.encode('utf-8') if isinstance(content, str) else content
        self.send_response(status)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        for k, v in (extra_headers or []):
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, path, flash=None, err=False):
        loc = path
        if flash:
            loc += ('&' if '?' in loc else '?') + 'flash=' + urllib.parse.quote(flash)
            if err:
                loc += '&err=1'
        self._send('', 303, extra_headers=[('Location', loc)])

    def _flash(self, qs):
        f = qs.get('flash', [None])[0]
        if f:
            return (qs.get('err', ['0'])[0] == '1', f)
        return None

    def _render(self, fn, **kw):
        conn = self.app.conn()
        app = self.app
        app.uid = self._uid()
        app.flash = self._flash(urllib.parse.parse_qs(
            urllib.parse.urlparse(self.path).query))
        try:
            html_text = fn(app, conn, **kw) if kw else fn(app, conn)
            self._send(html_text)
        finally:
            conn.close()

    def _form(self):
        n = int(self.headers.get('Content-Length', 0))
        raw = self.rfile.read(n).decode('utf-8') if n else ''
        return urllib.parse.parse_qs(raw)

    # ---- 路由
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)
        if path.startswith('/evidence/'):
            return self._evidence(path.split('/')[-1])
        routes = {
            '/': render_home,
            '/inventory': render_inventory,
            '/issues': render_issues,
            '/batches': render_batches,
            '/pos': render_pos,
            '/alarms': render_alarms,
            '/faults': render_faults,
            '/close': render_close,
            '/variance': render_variance,
            '/incidents': render_incidents,
        }
        if path in routes:
            return self._render(routes[path])
        if path.startswith('/alarms/'):
            try:
                aid = int(path.split('/')[2])
            except (IndexError, ValueError):
                return self._send('Not Found', 404, 'text/plain; charset=utf-8')
            return self._render(render_alarms, alarm_id=aid)
        self._send('Not Found', 404, 'text/plain; charset=utf-8')

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        form = self._form()
        if path == '/switch-user':
            uid = form.get('uid', ['3'])[0]
            self.send_response(303)
            self.send_header('Location', '/')
            self.send_header('Set-Cookie', f'uid={uid}; Path=/')
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        uid = self._uid()
        conn = self.app.conn()
        conn.execute('BEGIN IMMEDIATE')
        try:
            back = self._do_action(conn, path, form, uid)
            conn.execute('COMMIT')
            self._redirect(back[0], back[1] if len(back) > 1 else None)
        except (PermissionDenied, ServiceError, DuplicateEvent, VersionConflict) as e:
            conn.execute('ROLLBACK')
            self._redirect(self._referer(), f'操作被拒绝：{e}', err=True)
        except Exception:
            conn.execute('ROLLBACK')
            raise
        finally:
            conn.close()

    def _referer(self):
        ref = self.headers.get('Referer', '/')
        return urllib.parse.urlparse(ref).path

    def _do_action(self, conn, path, form, uid):
        import re
        m = re.fullmatch(r'/alarms/(\d+)/resolve', path)
        if m:
            aid = int(m.group(1))
            action = form.get('action', [''])[0]
            note = form.get('note', [''])[0]
            services.resolve_alarm(conn, aid, action, uid, note=note)
            return (f'/alarms/{aid}', '报警已复核并关闭')
        m = re.fullmatch(r'/variance/(\d+)/decide', path)
        if m:
            vid = int(m.group(1))
            decision = form.get('decision', [''])[0]
            note = form.get('note', [''])
            services.decide_variance(conn, vid, decision, uid, note=note[0])
            return ('/variance', '盘差已审批，账面已按结论调整')
        if path == '/close/rerun':
            from datetime import datetime
            bdate = datetime.now().date().isoformat()
            services.run_close_count(conn, STORE_ID, bdate)
            return ('/close', '已重新生成当日闭店清点')
        raise ServiceError(f'未知操作：{path}')

    def _evidence(self, fname):
        import os
        base = os.path.basename(fname)
        full = os.path.join(self.app.evidence_dir, base)
        if not os.path.exists(full):
            return self._send('Not Found', 404, 'text/plain; charset=utf-8')
        ctype = 'image/svg+xml' if base.endswith('.svg') else 'application/octet-stream'
        with open(full, 'rb') as f:
            self._send(f.read(), 200, ctype)


def main():
    import argparse
    import os
    from .scheduler import TaskScheduler
    parser = argparse.ArgumentParser()
    parser.add_argument('--db', default=os.environ.get('AT_DB', 'data/antitheft.db'))
    parser.add_argument('--evidence', default=os.environ.get('AT_EVIDENCE', 'evidence'))
    parser.add_argument('--port', type=int, default=8621)
    parser.add_argument('--no-scheduler', action='store_true')
    args = parser.parse_args()

    if not os.path.exists(args.db):
        from .seed import seed
        seed(args.db, args.evidence)
        print('未发现数据库，已自动导入样例数据')

    app = App(args.db, args.evidence)
    sched = None
    if not args.no_scheduler:
        sched = TaskScheduler(args.db, STORE_ID, interval_sec=60, close_hour=22)
        sched.start()
    Handler.app = app
    httpd = ThreadingHTTPServer(('0.0.0.0', args.port), Handler)
    print(f'防盗扣复核系统已启动：http://localhost:{args.port}')
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        if sched:
            sched.stop()


if __name__ == '__main__':
    main()
