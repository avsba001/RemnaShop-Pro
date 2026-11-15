"""Plan inventory is reserved in the same transaction as checkout creation."""
from services.zpay import PaymentError

MAX_STOCK = 2147483647


def parse_stock(text):
    value = text.strip()
    if value in {'不限量', '-1'}:
        return None
    if not value.isascii() or not value.isdigit() or len(value) > 10 or int(value) > MAX_STOCK:
        raise ValueError('请输入 0–2147483647 的整数，或输入 不限量')
    return int(value)


def stock_label(plan):
    stock = dict(plan).get('stock')
    return '不限量' if stock is None else ('已售罄（剩余 0 份）' if stock == 0 else f'剩余 {stock} 份')


def reserve_stock(conn, plan_key, order_id):
    plan = conn.execute('SELECT stock FROM plans WHERE key=?', (plan_key,)).fetchone()
    if not plan:
        raise PaymentError('plan_not_found')
    if plan['stock'] is None:
        return
    if not conn.execute('UPDATE plans SET stock=stock-1 WHERE key=? AND stock>0', (plan_key,)).rowcount:
        raise PaymentError('plan_sold_out')
    conn.execute('UPDATE orders SET stock_reserved=1 WHERE order_id=?', (order_id,))
