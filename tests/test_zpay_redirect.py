"""Redirect migration and signing contract (no real gateway traffic)."""
import unittest
from storage import payments
from storage.db import db_execute, db_query
from services.zpay import ZPayClient, ZPayConfig, verify_signature
from test_zpay_payments import PaymentFixture


class TestRedirectMigration(PaymentFixture, unittest.TestCase):
    def setUp(self):
        self.seed()

    def tearDown(self):
        self.temp.cleanup()

    def test_old_attempts_are_preserved_new_unattempted_adopts_redirect(self):
        fresh = self.intent()
        attempted = self.intent(tg=778)
        payments.claim_creation(self.db, attempted['payment_id'])
        payments.creation_unknown(self.db, attempted['payment_id'], 'gateway_http_error_403')
        # Simulate the actual schema deployed before this migration.
        db_execute(self.db, 'ALTER TABLE payment_intents DROP COLUMN gateway_mode')
        payments.init_payments(self.db)
        self.assertEqual(payments.get_intent(self.db, fresh['payment_id'])['gateway_mode'], 'redirect')
        old = payments.get_intent(self.db, attempted['payment_id'])
        self.assertEqual(old['gateway_mode'], 'api')
        self.assertEqual(old['status'], 'unknown')
        self.assertEqual(old['creation_error_code'], 'gateway_http_error_403')
        self.assertFalse(payments.claim_redirect(self.db, old['payment_id']))
        self.assertTrue(payments.claim_redirect(self.db, fresh['payment_id']))
        payments.init_payments(self.db)
        self.assertEqual(payments.get_intent(self.db, fresh['payment_id'])['status'], 'redirected')
        self.assertEqual(db_query(self.db, 'SELECT count(*) c FROM orders', one=True)['c'], 2)

    def test_signed_redirect_uses_frozen_amount_title_and_optional_channel(self):
        intent = self.intent()
        db_execute(self.db, "UPDATE plans SET price='0.01',name='changed'")
        config = ZPayConfig('merchant', 'private-secret', 'https://pay.example.test', '1,2')
        fields = ZPayClient(config, None).submit_params(intent)
        self.assertEqual(set(fields), {'pid', 'type', 'out_trade_no', 'notify_url', 'return_url', 'name', 'money', 'cid', 'sign', 'sign_type'})
        self.assertEqual(fields['money'], '200.00')
        self.assertEqual(fields['name'], intent['title'])
        self.assertEqual(fields['cid'], '1,2')
        self.assertEqual(fields['return_url'], 'https://pay.example.test/payments/zpay/return')
        self.assertTrue(verify_signature(fields, config.key))
        self.assertNotIn(config.key, fields.values())

    def test_redirect_deleted_order_retains_audit_and_cannot_issue_again(self):
        intent = self.intent()
        self.assertTrue(payments.claim_redirect(self.db, intent['payment_id']))
        self.assertFalse(payments.claim_redirect(self.db, intent['payment_id']))
        self.assertTrue(payments.delete_unpaid_order(self.db, intent['business_id'], 123))
        self.assertFalse(payments.claim_redirect(self.db, intent['payment_id']))
        payments.mark_paid(self.db, intent['payment_id'], 'verified-late-payment')
        self.assertFalse(payments.is_order_deleted(self.db, intent['business_id']))
        self.assertEqual(payments.get_intent(self.db, intent['payment_id'])['status'], 'paid')
