import json
from contextlib import closing
import tempfile
from pathlib import Path
import sqlite3
import unittest

from storage.db import init_db, db_execute, db_query
from storage.payments import subscription_checkout
from storage.plan_groups import plan_groups, snapshot_groups, require_available, validate_groups
from services.zpay import PaymentError

A = '11111111-1111-4111-8111-111111111111'
B = '22222222-2222-4222-8222-222222222222'


class TestPlanGroups(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.temp.name) / 'db.sqlite')
        init_db(self.db)

    def tearDown(self):
        self.temp.cleanup()

    def assign(self, groups):
        db_execute(self.db, 'UPDATE plans SET group_uuids=? WHERE key=?', (json.dumps(groups), 'p1'))

    def test_multi_group_snapshot_without_global_default(self):
        self.assign([A, B, A])
        intent = subscription_checkout(self.db, 777, 'p1', 'new', None)
        self.assign([B])
        self.assertEqual(snapshot_groups(json.loads(intent['snapshot_json'])), [A, B])
        self.assertEqual(subscription_checkout(self.db, 777, 'p1', 'new', None)['payment_id'], intent['payment_id'])

    def test_renewal_freezes_explicit_groups(self):
        self.assign([A, B])
        db_execute(self.db, 'INSERT INTO subscriptions(tg_id,user_id) VALUES (777,42)')
        intent = subscription_checkout(self.db, 777, 'p1', 'renew', 42)
        self.assertEqual(json.loads(intent['snapshot_json'])['group_uuids'], [A, B])

    def test_invalid_explicit_never_inherits_default(self):
        for groups in [[], ['bad'], A, None, [A, 42]]:
            if groups is None:
                continue
            self.assign(groups)
            with self.assertRaises(PaymentError):
                subscription_checkout(self.db, 777, 'p1', 'new', None, group_uuid=A)
        db_execute(self.db, "UPDATE plans SET group_uuids='not-json' WHERE key='p1'")
        with self.assertRaises(PaymentError):
            subscription_checkout(self.db, 777, 'p1', 'new', None, group_uuid=A)

    def test_legacy_defaults_and_old_snapshot(self):
        self.assertEqual(plan_groups({'group_uuids': None}, A), [A])
        self.assertEqual(snapshot_groups({'group_uuid': A}), [A])
        self.assertEqual(snapshot_groups({}), [])
        intent = subscription_checkout(self.db, 777, 'p1', 'new', None, group_uuid=A)
        self.assertIsNone(json.loads(intent['snapshot_json'])['group_uuids'])

    def test_missing_inventory_fails_closed(self):
        with self.assertRaises(ValueError):
            require_available([A, B], [{'uuid': A}])
        require_available([A, B], [{'uuid': A}, {'uuid': B}])
        self.assertEqual(validate_groups([A, B, A]), [A, B])

    def test_migrates_existing_schema_without_losing_data(self):
        old = str(Path(self.temp.name) / 'old.sqlite')
        with closing(sqlite3.connect(old)) as conn:
            conn.execute('CREATE TABLE plans (key TEXT PRIMARY KEY,name TEXT,price TEXT,days INTEGER,gb INTEGER)')
            conn.execute("INSERT INTO plans VALUES ('old','保留套餐','1.00',30,10)")
            conn.commit()
        init_db(old)
        row = db_query(old, 'SELECT * FROM plans', one=True)
        self.assertEqual(row['name'], '保留套餐')
        self.assertIsNone(row['group_uuids'])
        init_db(old)
        self.assertEqual(len(db_query(old, 'SELECT * FROM plans')), 1)
