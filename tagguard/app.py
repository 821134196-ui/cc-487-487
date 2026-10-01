"""本地启动入口（零依赖，Python 标准库）。

一条命令导入漏拆、串台和盘差样例并启动：
    python3 app.py --demo

其他：
    python3 app.py                 # 端口 8000
    python3 app.py --port 8080
    python3 app.py --reset         # 重新导入样例
"""
import argparse
import json
import os
import sys
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tagguard.db import init_db
from tagguard.service import (Service, DomainError, AuthError, VersionConflict,
                              ROLE_LABEL)
from tagguard.seed import seed_and_close, seed
from tagguard.scheduler import Scheduler
from tagguard import web

DB_PATH = os.environ.get("TAGGUARD_DB", "data/tagguard.db")
EVIDENCE_DIR = os.environ.get("TAGGUARD_EVIDENCE", "data/evidence")
STORE_ID = 1

# 进程内全局
STATE = {}


def today():
    return datetime.now().strftime("%Y-%m-%d")


class Handler(BaseHTTPRequestHandler):
    svc = None

    def log_message(self, fmt, *args):
        sys.stderr.write("[web] " + fmt % args + "\n")

    # ---------- 工具 ----------
    def _conn(self):
        return self.svc.conn

    def _store(self):
        return self._conn().execute("SELECT * FROM stores WHERE id=?",
                                    (STORE_ID,)).fetchone()

    def _users(self):
        return self._conn().execute("SELECT * FROM users ORDER BY id").fetchall()

    def _current_user(self):
        uid = self.headers.get("X-Uid")
        if not uid:
            cookie = self.headers.get("Cookie", "")
            for part in cookie.split(";"):
                if part.strip().startswith("uid="):
                    uid = part.strip()[4:]
        if uid and uid.isdigit():
            r = self._conn().execute("SELECT * FROM users WHERE id=?",
                                     (int(uid),)).fetchone()
            if r:
                return r
        return self._users()[0]

    def _params(self):
        return {k: v[0] for k, v in parse_qs(self.body).items()}

    def _tag_types(self):
        return self._conn().execute("SELECT * FROM tag_types ORDER BY id").fetchall()

    def _products(self):
        return self._conn().execute("SELECT * FROM products ORDER BY id").fetchall()

    def _reminders(self):
        return self._conn().execute(
            "SELECT * FROM reminders WHERE store_id=? ORDER BY id DESC LIMIT(50)",
            (STORE_ID,)).fetchall()

    def _send(self, content, status=200, ctype="text/html; charset=utf-8",
              extra_headers=None):
        if isinstance(content, str):
            content = content.encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(content)))
        for k, v in (extra_headers or []):
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(content)

    def _redirect(self, path, flash=None):
        loc = path
        if flash:
            import base64
            token = base64.urlsafe_b64encode(
                json.dumps(flash, ensure_ascii=False).encode()).decode()
            loc += ("&" if "?" in path else "?") + "f=" + token
        self._send(b"", 303, extra_headers=[("Location", loc)])

    def _flash(self, qs):
        f = qs.get("f", [None])[0]
        if not f:
            return None
        import base64
        try:
            return tuple(json.loads(base64.urlsafe_b64decode(f)))
        except Exception:
            return None

    def _render(self, fn, *args):
        qs = parse_qs(urlparse(self.path).query)
        flash = self._flash(qs)
        html_text = fn(*args)
        if flash:
            marker = "<main>"
            cls, msg = flash
            box = (f'<div class="flash {"ok" if cls=="ok" else "err"}">'
                   f'{web.esc(msg)}</div>')
            html_text = html_text.replace(marker, marker + box, 1)
        self._send(html_text)

    # ---------- GET ----------
    def do_GET(self):
        u = urlparse(self.path)
        path, qs = u.path, parse_qs(u.query)
        s, conn = self.svc, self._conn()
        store, cu, users = self._store(), self._current_user(), self._users()
        try:
            if path == "/":
                self._render(web.render_home, s, store, cu, users, self._reminders())
            elif path == "/inventory":
                self._render(web.render_inventory, s, store, cu, users,
                             self._tag_types())
            elif path == "/requisitions":
                self._render(web.render_requisitions, s, store, cu, users)
            elif path == "/batches":
                self._render(web.render_batches, s, store, cu, users,
                             self._products(), self._tag_types())
            elif path == "/recycle":
                self._render(web.render_recycle, s, store, cu, users)
            elif path == "/alarms":
                self._render(web.render_alarms, s, store, cu, users)
            elif path.startswith("/alarm/"):
                aid = int(path.rsplit("/", 1)[-1])
                self._render(web.render_alarm_detail, s, aid, cu, users)
            elif path == "/faults":
                self._render(web.render_faults, s, store, cu, users)
            elif path == "/closing":
                self._render(web.render_closing, s, store, cu, users,
                             qs.get("d", [today()])[0])
            elif path == "/adjustments":
                self._render(web.render_adjustments, s, store, cu, users)
            elif path == "/ledger":
                self._render(web.render_ledger, s, store, cu, users)
            elif path == "/health":
                self._send("ok", ctype="text/plain")
            else:
                self._send("<h1>404</h1>", 404)
        except Exception as e:
            self._send(f"<h1>服务异常</h1><pre>{web.esc(e)}</pre>", 500)

    # ---------- POST ----------
    def do_POST(self):
        u = urlparse(self.path)
        path = u.path
        length = int(self.headers.get("Content-Length", 0))
        self.body = self.rfile.read(length).decode()
        p = self._params()
        s, conn = self.svc, self._conn()
        cu = self._current_user()
        uid = cu["id"]
        referer = self.headers.get("Referer", "/")
        back = urlparse(referer).path or "/"

        def done(msg, ok=True, goto=None):
            self._redirect(goto or back, ("ok" if ok else "err", msg))

        try:
            if path == "/switch-user":
                self.send_response(303)
                self.send_header("Set-Cookie", f"uid={p['uid']}; Path=/")
                self.send_header("Location", back)
                self.end_headers()
                return
            elif path == "/issue":
                bal = s.issue_tags(STORE_ID, int(p["tag_type_id"]),
                                   int(p["qty"]), uid)
                done(f"入库成功，仓库结余 {bal}")
            elif path == "/requisition":
                rid = s.requisition(STORE_ID, p["floor"], int(p["tag_type_id"]),
                                    int(p["qty"]), uid, p.get("note", ""))
                done(f"领用成功（单号 #{rid}）")
            elif path == "/batch":
                bid = s.attach_batch(
                    STORE_ID, p["floor"], int(p["product_id"]),
                    int(p["tag_type_id"]), int(p["required_qty"]),
                    int(p["attached_qty"]), uid, p.get("note", ""))
                done(f"挂扣批次 #{bid} 已登记")
            elif path == "/damage":
                did = s.damage(
                    STORE_ID, p["floor"], int(p["tag_type_id"]),
                    int(p["qty"]), p.get("reason", ""), uid,
                    on_shelf=p.get("on_shelf") == "1")
                done(f"报损已登记（单号 #{did}）")
            elif path == "/detach":
                did = s.detach(int(p["txn_id"]), p["box_register"], uid)
                done(f"拆扣已确认并回收（记录 #{did}）", goto="/recycle")
            elif path == "/detach-fail":
                # 消磁失败由领域层拦截并提示，异常冒泡到统一错误处理
                s.detach(int(p["txn_id"]), "", uid, deauth_ok=False)
                done("消磁失败应被拦截", False)
            elif path == "/cross/resolve":
                s.resolve_box_cross(int(p["cross_id"]), uid)
                done("串台已调正，扣具数量回应属盒")
            elif path.startswith("/alarm/") and path.endswith("/review"):
                aid = int(path.split("/")[2])
                s.review_alarm(aid, p["register_no"], uid, p["verdict"],
                               p.get("note", ""))
                done("复核完成：" + ("已确认漏拆并回收扣具" if p["verdict"] == "miss"
                                    else "判为误报"), goto=f"/alarm/{aid}")
            elif path.startswith("/alarm/") and path.endswith("/no-txn"):
                aid = int(path.split("/")[2])
                s.handle_no_txn_alarm(aid, uid, p.get("note", ""))
                done("无交易报警已核查关闭", goto=f"/alarm/{aid}")
            elif path == "/fault/handle":
                s.handle_fault(int(p["fault_id"]), uid, p.get("note", ""))
                done("故障已处理")
            elif path.startswith("/adjust/"):
                approve = p.get("approve") == "1"
                adj_id = int(path.rsplit("/", 1)[-1])
                s.approve_adjustment(adj_id, uid, approve, p.get("reason", ""))
                done("盘差已" + ("批准，库存已调整并写流水" if approve else "驳回"),
                     goto="/adjustments")
            elif path == "/scheduler/run-closing":
                STATE["scheduler"].trigger_closing()
                done("今日闭店清点已生成", goto="/closing")
            else:
                self._send("<h1>404</h1>", 404)
        except (DomainError, AuthError, VersionConflict) as e:
            label = ROLE_LABEL.get(getattr(e, "code", ""), "")
            msg = str(e)
            done(msg, False)
        except Exception as e:
            self._send(f"<h1>服务异常</h1><pre>{web.esc(e)}</pre>", 500)


def build_app(db_path=DB_PATH, evidence_dir=EVIDENCE_DIR, reset=False,
              with_scheduler=False):
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    os.makedirs(evidence_dir, exist_ok=True)
    if reset and os.path.exists(db_path):
        for suf in ("", "-wal", "-shm"):
            try:
                os.remove(db_path + suf)
            except OSError:
                pass
    conn = init_db(db_path)
    fresh = conn.execute("SELECT COUNT(*) c FROM stores").fetchone()["c"] == 0
    if fresh or reset:
        # 导入漏拆/串台样例并执行带盘差的闭店清点
        conn.close()
        seeded, _info, _rows, _diffs = seed_and_close(
            db_path, today(), evidence_dir)
        seeded.close()
        conn = init_db(db_path)
    svc = Service(conn, evidence_dir)
    Handler.svc = svc
    STATE["conn"] = conn
    STATE["service"] = svc
    sch = Scheduler(svc, STORE_ID, manager_uid=5)
    if with_scheduler:
        sch.start()
    # 启动即巡检一次，把待办推到提醒
    sch.patrol()
    STATE["scheduler"] = sch
    return conn, svc


def main():
    global DB_PATH, EVIDENCE_DIR
    ap = argparse.ArgumentParser(description="服装门店防盗扣领用与漏拆复核系统")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--evidence", default=None,
                    help="报警凭证本地目录（默认与数据库同目录的 evidence）")
    ap.add_argument("--demo", action="store_true",
                    help="一条命令：导入漏拆/串台/盘差样例并启动")
    ap.add_argument("--reset", action="store_true", help="重置数据库后导入样例")
    ap.add_argument("--no-scheduler", action="store_true")
    args = ap.parse_args()

    DB_PATH = args.db
    EVIDENCE_DIR = args.evidence or os.path.join(
        os.path.dirname(os.path.abspath(args.db)) or ".", "evidence")
    os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))) or ".")
    build_app(args.db, EVIDENCE_DIR,
              reset=args.reset or args.demo or not os.path.exists(args.db),
              with_scheduler=not args.no_scheduler)
    httpd = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    print(f"✅ 防盗扣系统已启动：http://localhost:{args.port}", flush=True)
    print(f"   数据库：{os.path.abspath(args.db)}　凭证目录：{EVIDENCE_DIR}",
          flush=True)
    print("   样例：1F 丝巾漏拆待报警复核 / 2F 衬衫回收盒串台待调正 / 1F 硬扣盘差待店长审批",
          flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        STATE.get("scheduler") and STATE["scheduler"].stop()


if __name__ == "__main__":
    main()
