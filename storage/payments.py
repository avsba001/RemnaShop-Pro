"""Reusable payment ledger. Product handlers own fulfillment, not the HTTP callback.

The generic ledger is intentionally independent of Telegram/Remnawave. Only the
subscription adapter below knows about `orders`. Schema initialization NEVER
runs storage.db.init_db (which performs crash recovery for the bot).
"""
import json
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager

from services.zpay import PaymentError, money_fen
from storage.db import _connect, db_query, db_execute
from storage.plan_groups import plan_groups


VISIBLE_ORDER_SQL = "NOT EXISTS (SELECT 1 FROM order_deletions d WHERE d.order_id=orders.order_id)"


@contextmanager
def transaction(db_file):
    conn = _connect(db_file)
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_payments(db_file):
    with transaction(db_file) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS payment_intents (
            payment_id TEXT PRIMARY KEY,
            provider TEXT NOT NULL DEFAULT 'zpay',
            product_kind TEXT NOT NULL,
            business_id TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            amount_fen INTEGER NOT NULL CHECK(amount_fen>0),
            currency TEXT NOT NULL DEFAULT 'CNY',
            title TEXT NOT NULL,
            snapshot_json TEXT NOT NULL,
            checkout_token TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL DEFAULT 'new',
            trade_no TEXT,
            gateway_json TEXT,
            last_checked_at INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            paid_at INTEGER,
            UNIQUE(product_kind, business_id),
            UNIQUE(provider, trade_no)
        )""")
        if 'gateway_mode' not in {r[1] for r in conn.execute('PRAGMA table_info(payment_intents)')}:
            conn.execute("ALTER TABLE payment_intents ADD COLUMN gateway_mode TEXT NOT NULL DEFAULT 'api'")
            # Only unattempted intents can safely adopt the new flow. Never
            # reset attempted legacy API orders or reuse their merchant IDs.
            conn.execute("UPDATE payment_intents SET gateway_mode='redirect' WHERE status='new'")
        if 'creation_error_code' not in {r[1] for r in conn.execute('PRAGMA table_info(payment_intents)')}:
            conn.execute('ALTER TABLE payment_intents ADD COLUMN creation_error_code TEXT')
        conn.execute("""CREATE TABLE IF NOT EXISTS order_deletions (
            order_id TEXT PRIMARY KEY,
            deleted_by INTEGER NOT NULL,
            deleted_at INTEGER NOT NULL
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_payment_status_checked ON payment_intents(status,last_checked_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_payment_owner ON payment_intents(owner_id,created_at)")
        conn.execute("""CREATE TABLE IF NOT EXISTS payment_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            payment_id TEXT NOT NULL,
            event TEXT NOT NULL,
            created_at INTEGER NOT NULL
        )""")


def insert_intent(conn, *, product_kind, business_id, owner_id, amount_fen, title, snapshot):
    """Internal adapter API; never expose client-supplied prices over public HTTP."""
    if (not isinstance(amount_fen, int) or isinstance(amount_fen, bool) or amount_fen <= 0
            or not product_kind or not business_id or not title or len(title) > 100):
        raise PaymentError("invalid_intent")
    now = int(time.time())
    # Numeric merchant ID, 30 digits, within the documented 32-character limit.
    payment_id = str(secrets.randbelow(9 * 10**29) + 10**29)
    conn.execute("""INSERT INTO payment_intents
        (payment_id,product_kind,business_id,owner_id,amount_fen,title,snapshot_json,
         checkout_token,status,created_at,updated_at,gateway_mode) VALUES (?,?,?,?,?,?,?,?,'new',?,?,'redirect')""",
                 (payment_id, product_kind, business_id, str(owner_id), amount_fen, title,
                  json.dumps(snapshot, ensure_ascii=False), secrets.token_urlsafe(32), now, now))
    return dict(conn.execute("SELECT * FROM payment_intents WHERE payment_id=?", (payment_id,)).fetchone())


def get_intent(db_file, payment_id):
    row = db_query(db_file, "SELECT * FROM payment_intents WHERE payment_id=?", (payment_id,), one=True)
    return dict(row) if row else None


def for_business(db_file, business_id, product_kind="subscription"):
    row = db_query(db_file, "SELECT * FROM payment_intents WHERE product_kind=? AND business_id=?",
                   (product_kind, business_id), one=True)
    return dict(row) if row else None


def for_checkout(db_file, token):
    if not isinstance(token, str) or len(token) != 43:
        return None
    row = db_query(db_file, """SELECT p.*, d.deleted_at AS order_deleted_at FROM payment_intents p
        LEFT JOIN order_deletions d ON p.product_kind='subscription' AND p.business_id=d.order_id
        WHERE p.checkout_token=?""", (token,), one=True)
    return dict(row) if row else None


def subscription_checkout(db_file, tg_id, plan_key, order_type, target_user_id, *, channel_code=None, group_uuid=None):
    if order_type not in {"new", "renew"}:
        raise PaymentError("invalid_order_type")
    target_user_id = int(target_user_id or 0)
    if (order_type == "new" and target_user_id) or (order_type == "renew" and target_user_id <= 0):
        raise PaymentError("invalid_target")
    now = int(time.time())
    with transaction(db_file) as conn:
        if order_type == "renew" and not conn.execute(
            "SELECT 1 FROM subscriptions WHERE tg_id=? AND user_id=?", (tg_id, target_user_id)
        ).fetchone():
            raise PaymentError("subscription_not_owned")
        if order_type == "new" and conn.execute(
            "SELECT 1 FROM subscriptions WHERE tg_id=? AND user_id IS NULL", (tg_id,)
        ).fetchone():
            raise PaymentError("legacy_subscription_unresolved")
        existing = conn.execute("""SELECT p.* FROM payment_intents p JOIN orders o
            ON p.product_kind='subscription' AND p.business_id=o.order_id
            WHERE o.tg_id=? AND o.plan_key=? AND o.order_type=? AND COALESCE(o.target_user_id,0)=?
              AND o.status='pending' AND NOT EXISTS (SELECT 1 FROM order_deletions d WHERE d.order_id=o.order_id)
              ORDER BY p.created_at DESC LIMIT 1""",
                                (tg_id, plan_key, order_type, target_user_id)).fetchone()
        if existing:
            return dict(existing)
        outstanding = conn.execute("""SELECT count(*) FROM payment_intents p WHERE owner_id=? AND status!='paid'
            AND NOT EXISTS (SELECT 1 FROM order_deletions d WHERE p.product_kind='subscription' AND d.order_id=p.business_id)""",
                                   (str(tg_id),)).fetchone()[0]
        if outstanding >= 10:
            raise PaymentError("too_many_unpaid_orders")
        plan = conn.execute("SELECT * FROM plans WHERE key=?", (plan_key,)).fetchone()
        if not plan:
            raise PaymentError("plan_not_found")
        snapshot = dict(plan)
        amount = money_fen(snapshot.get("price"), display=True)
        if (not isinstance(snapshot.get("days"), int) or snapshot["days"] < 1
                or not isinstance(snapshot.get("gb"), int) or snapshot["gb"] < 0
                or snapshot.get("reset_strategy") not in {"NO_RESET", "DAY", "WEEK", "MONTH", "MONTH_ROLLING"}):
            raise PaymentError("invalid_plan")
        order_id = uuid.uuid4().hex[:12]
        try:
            groups = plan_groups(snapshot, group_uuid)
        except (ValueError, TypeError):
            raise PaymentError("invalid_plan_groups") from None
        if order_type == "new" and not groups:
            raise PaymentError("panel_group_not_configured")
        # Freeze explicit plan access for both new purchases and renewals.
        # Legacy plans retain their old renewal behavior (do not replace access).
        if snapshot.get("group_uuids") is not None:
            snapshot["group_uuids"] = groups
        snapshot["group_uuid"] = group_uuid
        conn.execute("""INSERT INTO orders
            (order_id,tg_id,plan_key,order_type,target_user_id,status,channel_code,created_at,updated_at)
            VALUES (?,?,?,?,?,'pending',?,?,?)""",
                     (order_id, tg_id, plan_key, order_type, target_user_id or None, channel_code, now, now))
        conn.execute("""INSERT INTO order_audit_logs(order_id,action,actor_id,detail,created_at)
            VALUES (?,'create',?,'alipay_online_snapshot',?)""", (order_id, tg_id, now))
        return insert_intent(conn, product_kind="subscription", business_id=order_id, owner_id=tg_id,
                             amount_fen=amount, title=f"{snapshot['name']} · {'续费' if order_type == 'renew' else '新购'}",
                             snapshot=snapshot)


def claim_creation(db_file, payment_id):
    # One-way write-ahead barrier: a crash/timeout never authorizes resubmission.
    return bool(db_execute(db_file, """UPDATE payment_intents SET status='creating',updated_at=?
        WHERE payment_id=? AND status='new' AND NOT EXISTS
        (SELECT 1 FROM order_deletions d WHERE payment_intents.product_kind='subscription'
         AND d.order_id=payment_intents.business_id)""", (int(time.time()), payment_id)))


def prepare_direct_payment(db_file, payment_id):
    """Issue/reopen the same unpaid redirect order only on explicit user action."""
    with transaction(db_file) as conn:
        row = conn.execute("""SELECT * FROM payment_intents p
            WHERE payment_id=? AND gateway_mode='redirect' AND status IN ('new','redirected')
            AND NOT EXISTS (SELECT 1 FROM order_deletions d
                WHERE p.product_kind='subscription' AND d.order_id=p.business_id)""",
                           (payment_id,)).fetchone()
        if not row:
            return None
        if row['status'] == 'new':
            conn.execute("UPDATE payment_intents SET status='redirected',updated_at=? WHERE payment_id=?",
                         (int(time.time()), payment_id))
        return dict(conn.execute("SELECT * FROM payment_intents WHERE payment_id=?", (payment_id,)).fetchone())


def claim_redirect(db_file, payment_id):
    """One-shot issuance barrier before handing signed fields to the browser."""
    return bool(db_execute(db_file, """UPDATE payment_intents SET status='redirected',updated_at=?
        WHERE payment_id=? AND status='new' AND gateway_mode='redirect' AND NOT EXISTS
        (SELECT 1 FROM order_deletions d WHERE payment_intents.product_kind='subscription'
         AND d.order_id=payment_intents.business_id)""", (int(time.time()), payment_id)))


def record_creation(db_file, payment_id, result):
    with transaction(db_file) as conn:
        row = conn.execute("SELECT * FROM payment_intents WHERE payment_id=?", (payment_id,)).fetchone()
        if not row or (row["trade_no"] and row["trade_no"] != result["trade_no"]):
            raise PaymentError("payment_trade_mismatch")
        conn.execute("""UPDATE payment_intents SET trade_no=?,gateway_json=?,
            status=CASE WHEN status='paid' THEN 'paid' ELSE 'ready' END,updated_at=?
            WHERE payment_id=?""", (result["trade_no"], json.dumps(result), int(time.time()), payment_id))


def creation_unknown(db_file, payment_id, error_code=''):
    # Persist only fixed diagnostic codes, never raw gateway messages or URLs.
    import re
    code = error_code if re.fullmatch(r'[a-z0-9_]{1,80}', error_code) else None
    db_execute(db_file, "UPDATE payment_intents SET status='unknown',creation_error_code=?,updated_at=? WHERE payment_id=? AND status='creating'",
               (code, int(time.time()), payment_id))


def mark_paid(db_file, payment_id, trade_no):
    with transaction(db_file) as conn:
        row = conn.execute("SELECT * FROM payment_intents WHERE payment_id=?", (payment_id,)).fetchone()
        if not row or (row["trade_no"] and row["trade_no"] != trade_no):
            raise PaymentError("payment_trade_mismatch")
        if row["status"] == "paid":
            return False
        now = int(time.time())
        try:
            conn.execute("UPDATE payment_intents SET status='paid',trade_no=?,paid_at=?,updated_at=? WHERE payment_id=?",
                         (trade_no, now, now, payment_id))
        except sqlite3.IntegrityError:
            raise PaymentError("payment_trade_reused") from None
        conn.execute("INSERT INTO payment_events(payment_id,event,created_at) VALUES (?,'paid',?)", (payment_id, now))
        if row['product_kind'] == 'subscription':
            restored = conn.execute("DELETE FROM order_deletions WHERE order_id=?", (row['business_id'],)).rowcount
            if restored:
                conn.execute("""INSERT INTO order_audit_logs(order_id,action,actor_id,detail,created_at)
                    VALUES (?,'restore_on_payment',0,'verified_payment_restored_deleted_order',?)""", (row['business_id'], now))
        return True


def is_order_deleted(db_file, order_id):
    return bool(db_query(db_file, "SELECT 1 FROM order_deletions WHERE order_id=?", (order_id,), one=True))


def can_delete_unpaid(order, intent):
    """Only unfulfilled online orders whose ledger has no verified receipt."""
    return bool(order and intent and order.get('status') == 'pending'
                and intent.get('status') in {'new', 'creating', 'ready', 'unknown', 'redirected'}
                and not intent.get('paid_at')
                and not order.get('delivered_user_id') and not order.get('delivered_uuid'))


def delete_unpaid_order(db_file, order_id, actor_id):
    """Logical list deletion only. Never erase the ledger or close a gateway order.

    Serialize with callbacks: if paid wins first this refuses deletion; if
    deletion wins first a later verified payment restores the visible order.
    """
    with transaction(db_file) as conn:
        row = conn.execute('SELECT * FROM orders WHERE order_id=?', (order_id,)).fetchone()
        intent = conn.execute("SELECT * FROM payment_intents WHERE product_kind='subscription' AND business_id=?", (order_id,)).fetchone()
        if not can_delete_unpaid(dict(row) if row else None, dict(intent) if intent else None):
            raise PaymentError('order_not_deletable')
        now = int(time.time())
        changed = conn.execute('INSERT OR IGNORE INTO order_deletions(order_id,deleted_by,deleted_at) VALUES (?,?,?)',
                               (order_id, int(actor_id), now)).rowcount
        if changed:
            conn.execute("""INSERT INTO order_audit_logs(order_id,action,actor_id,detail,created_at)
                VALUES (?,'admin_delete_unpaid',?,'list_deletion_only_gateway_remains_active',?)""", (order_id, int(actor_id), now))
        return bool(changed)


def claim_query(db_file, payment_id, *, interval=30):
    now = int(time.time())
    return bool(db_execute(db_file, """UPDATE payment_intents SET last_checked_at=?
        WHERE payment_id=? AND status IN ('creating','ready','unknown','redirected') AND last_checked_at<=?""",
                           (now, payment_id, now - interval)))
