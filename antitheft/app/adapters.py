# -*- coding: utf-8 -*-
"""模拟适配器：以内存队列返回收银成交与门禁报警，替代真实 POS/门禁硬件。

- MockPosAdapter.poll() 逐条吐出成交报文，ingest_payment 落为交易+拆扣事件；
- MockGateAdapter.poll() 吐出报警报文，证据快照写入本地目录 evidence/。
"""
from datetime import datetime

from . import services


def _today_at(hour, minute=0):
    return datetime.now().replace(hour=hour, minute=minute, second=0,
                                  microsecond=0).isoformat(timespec='seconds')


def write_evidence(evidence_dir, alarm_code, gate, ts, kind):
    """生成一张本地 SVG 报警凭证（模拟门禁抓拍）。"""
    import pathlib
    pathlib.Path(evidence_dir).mkdir(parents=True, exist_ok=True)
    fname = f'{alarm_code}.svg'
    path = pathlib.Path(evidence_dir) / fname
    svg = f"""<svg xmlns='http://www.w3.org/2000/svg' width='480' height='300'>
  <rect width='480' height='300' fill='#1f1f24'/>
  <rect width='480' height='56' fill='#e6007e'/>
  <text x='20' y='37' fill='#ffffff' font-family='sans-serif' font-size='22'>门禁报警凭证（模拟抓拍）</text>
  <text x='20' y='105' fill='#ffffff' font-family='sans-serif' font-size='17'>事件：{alarm_code}</text>
  <text x='20' y='145' fill='#ffffff' font-family='sans-serif' font-size='17'>出口：{gate}</text>
  <text x='20' y='185' fill='#ffffff' font-family='sans-serif' font-size='17'>类型：{kind}</text>
  <text x='20' y='225' fill='#ffffff' font-family='sans-serif' font-size='17'>时间：{ts}</text>
  <text x='20' y='270' fill='#9aa0a6' font-family='sans-serif' font-size='13'>EAS GATE MOCK ADAPTER</text>
</svg>
"""
    path.write_text(svg, encoding='utf-8')
    return str(path)


class MockPosAdapter:
    """收银 POS 适配器：queue 中为预置成交报文。"""
    name = 'MOCK-POS'

    def __init__(self, payments):
        self._queue = list(payments)

    def has_pending(self):
        return bool(self._queue)

    def poll(self):
        return self._queue.pop(0) if self._queue else None

    def ingest_all(self, conn, store_id):
        """拉取全部报文并入库，返回 {外部成交号: txn_id} 供门禁关联。"""
        products = {r['sku']: r for r in conn.execute(
            'SELECT * FROM products WHERE store_id=?', (store_id,))}
        registers = {r['code']: r for r in conn.execute(
            'SELECT * FROM registers WHERE store_id=?', (store_id,))}
        cashiers = {r['username']: r for r in conn.execute('SELECT * FROM users')}
        boxes = {r['code']: r for r in conn.execute(
            'SELECT * FROM recovery_boxes WHERE store_id=?', (store_id,))}
        txn_map = {}
        while self.has_pending():
            pay = self.poll()
            reg = registers[pay['register_code']]
            cashier = cashiers[pay['cashier_username']]
            # 默认投本台绑定盒；报文显式给出别的盒子即串台
            box = boxes[pay['box_code']] if pay.get('box_code') \
                else _get_box(conn, reg['box_id'])
            items = []
            for sku, sold, detached in pay['items']:
                p = products[sku]
                items.append((p['id'], sold, detached))
            ts = _today_at(pay['hour'], pay.get('minute', 0))
            txn = services.pos_sale_detach(
                conn, store_id, reg['id'], cashier['id'], items, box['id'], ts=ts)
            txn_map[pay['ext_no']] = txn['id']
        return txn_map


def _get_box(conn, box_id):
    return conn.execute('SELECT * FROM recovery_boxes WHERE id=?',
                        (box_id,)).fetchone()


class MockGateAdapter:
    """门禁 EAS 适配器：报警报文，凭证落本地目录。"""
    name = 'MOCK-GATE'

    def __init__(self, alarms, evidence_dir):
        self._queue = list(alarms)
        self.evidence_dir = evidence_dir

    def has_pending(self):
        return bool(self._queue)

    def poll(self):
        return self._queue.pop(0) if self._queue else None

    def ingest_all(self, conn, store_id, txn_map):
        tag_types = {r['code']: r for r in conn.execute('SELECT * FROM tag_types')}
        out = []
        while self.has_pending():
            a = self.poll()
            ts = _today_at(a['hour'], a.get('minute', 0))
            txn_id = txn_map.get(a.get('ext_no'))
            ttid = tag_types[a['tag_type_code']]['id'] if a.get('tag_type_code') else None
            # 先生成报警号再写凭证文件
            alarm = services.ingest_alarm(
                conn, store_id, a['gate'], ts, txn_id=txn_id, tag_type_id=ttid)
            path = write_evidence(self.evidence_dir, alarm['code'], a['gate'], ts,
                                  'MATCHED_TXN' if txn_id else 'NO_TXN')
            conn.execute('UPDATE alarms SET evidence_path=? WHERE id=?',
                         (path, alarm['id']))
            out.append(alarm['id'])
        return out
