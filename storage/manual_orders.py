"""Proof-based orders stay separate from the verified online payment ledger."""
import json
import time
import uuid

from services.zpay import PaymentError, money_fen, yuan
from storage.db import db_query
from storage.payments import transaction
from storage.plan_groups import plan_groups


def get_details(db_file, order_id):
    row = db_query(db_file, 'SELECT * FROM manual_order_details WHERE order_id=?', (order_id,), one=True)
    return dict(row) if row else None


def pending_order(db_file, tg_id, order_id=None):
    row = db_query(db_file, """SELECT o.* FROM orders o
        WHERE o.tg_id=? AND o.status='pending' AND (? IS NULL OR o.order_id=?)
        AND NOT EXISTS (SELECT 1 FROM payment_intents p WHERE p.product_kind='subscription' AND p.business_id=o.order_id)
        ORDER BY o.created_at DESC, o.id DESC LIMIT 1""", (tg_id, order_id, order_id), one=True)
    return dict(row) if row else None


def create_order(db_file, tg_id, plan_key, order_type, target_user_id, payment_method, *,
                 group_uuid=None, channel_code=None, menu_message_id=None):
    if payment_method not in {'manual_review', 'usdt'} or order_type not in {'new', 'renew'}:
        raise PaymentError('invalid_manual_order')
    target = int(target_user_id or 0)
    if (order_type == 'new' and target) or (order_type == 'renew' and target <= 0):
        raise PaymentError('invalid_target')
    with transaction(db_file) as conn:
        if order_type == 'renew' and not conn.execute(
            'SELECT 1 FROM subscriptions WHERE tg_id=? AND user_id=?', (tg_id, target)
        ).fetchone():
            raise PaymentError('subscription_not_owned')
        if order_type == 'new' and conn.execute(
            'SELECT 1 FROM subscriptions WHERE tg_id=? AND user_id IS NULL', (tg_id,)
        ).fetchone():
            raise PaymentError('legacy_subscription_unresolved')
        existing = conn.execute("""SELECT o.* FROM orders o JOIN manual_order_details m ON m.order_id=o.order_id
            WHERE o.tg_id=? AND o.plan_key=? AND o.order_type=? AND COALESCE(o.target_user_id,0)=?
            AND m.payment_method=? AND o.status='pending'
            AND NOT EXISTS (SELECT 1 FROM payment_intents p WHERE p.product_kind='subscription' AND p.business_id=o.order_id)
            ORDER BY o.id DESC LIMIT 1""", (tg_id, plan_key, order_type, target, payment_method)).fetchone()
        if existing:
            return dict(existing)
        if conn.execute("SELECT count(*) FROM orders WHERE tg_id=? AND status='pending'", (tg_id,)).fetchone()[0] >= 10:
            raise PaymentError('too_many_pending_orders')
        plan = conn.execute('SELECT * FROM plans WHERE key=?', (plan_key,)).fetchone()
        if not plan:
            raise PaymentError('plan_not_found')
        snapshot = dict(plan)
        price_key = 'usdt_price' if payment_method == 'usdt' else 'price'
        snapshot[price_key] = yuan(money_fen(snapshot.get(price_key) or '', display=payment_method == 'manual_review'))
        if (not isinstance(snapshot.get('days'), int) or snapshot['days'] < 1
                or not isinstance(snapshot.get('gb'), int) or snapshot['gb'] < 0
                or snapshot.get('reset_strategy') not in {'NO_RESET', 'DAY', 'WEEK', 'MONTH', 'MONTH_ROLLING'}):
            raise PaymentError('invalid_plan')
        groups = plan_groups(snapshot, group_uuid)
        if order_type == 'new' and not groups:
            raise PaymentError('panel_group_not_configured')
        if snapshot.get('group_uuids') is not None:
            snapshot['group_uuids'] = groups
        snapshot['group_uuid'] = group_uuid
        now = int(time.time())
        order_id = uuid.uuid4().hex[:12]
        conn.execute("""INSERT INTO orders
            (order_id,tg_id,plan_key,order_type,target_user_id,status,menu_message_id,channel_code,created_at,updated_at)
            VALUES (?,?,?,?,?,'pending',?,?,?,?)""",
                     (order_id,tg_id,plan_key,order_type,target or None,menu_message_id,channel_code,now,now))
        conn.execute('INSERT INTO manual_order_details VALUES (?,?,?)',
                     (order_id,payment_method,json.dumps(snapshot,ensure_ascii=False)))
        conn.execute("""INSERT INTO order_audit_logs(order_id,action,actor_id,detail,created_at)
            VALUES (?,'create',?,?,?)""", (order_id,tg_id,payment_method,now))
        return dict(conn.execute('SELECT * FROM orders WHERE order_id=?', (order_id,)).fetchone())
