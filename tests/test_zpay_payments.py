import asyncio
import hashlib
import importlib
import ipaddress
import json
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from aiohttp.test_utils import TestClient, TestServer
from aiohttp import web
import httpx

from services.zpay import (PaymentError, ZPayConfig, ZPayClient, money_fen,
                           sign_params, verify_signature, validate_paid)
from services.http_server import create_app, csrf_value, real_client_ip
from services.payment_processing import reconcile_payment
from storage import payments
from storage.db import init_db, db_query, db_execute


CONFIG = ZPayConfig('merchant1', 'test-secret', 'https://pay.example.test')


class PaymentFixture:
    def seed(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.temp.name) / 'data.db')
        init_db(self.db)

    def intent(self, tg=777, kind='new', target=None):
        return payments.subscription_checkout(self.db, tg, 'p1', kind, target, group_uuid='fixed-group')

    def notification(self, intent, trade='gateway-1', **overrides):
        data = dict(pid=CONFIG.pid, type='alipay', out_trade_no=intent['payment_id'],
                    trade_no=trade, trade_status='TRADE_SUCCESS', money='200.00', sign_type='MD5')
        data.update(overrides)
        data['sign'] = sign_params(data, CONFIG.key)
        return data


class TestPaymentLedger(PaymentFixture, unittest.TestCase):
    def setUp(self):
        self.seed()

    def tearDown(self):
        self.temp.cleanup()

    def test_money_and_signature_contract(self):
        for value in ('200元', '¥200.00', '￥200', 'CNY 200', '200 CNY'):
            self.assertEqual(money_fen(value, display=True), 20000)
        for value in ('NaN', '-1', '0', '1e2', '1.001', '1,000', '200美元'):
            with self.subTest(value=value), self.assertRaises(PaymentError):
                money_fen(value, display=True)
        params = {'z': '中文 + &', 'a': '1', 'empty': '', 'sign': 'ignored', 'sign_type': 'MD5'}
        expected = hashlib.md5('a=1&z=中文 + &secret'.encode()).hexdigest()
        self.assertEqual(sign_params(params, 'secret'), expected)
        params['sign'] = expected
        self.assertTrue(verify_signature(params, 'secret'))
        params['a'] = '2'
        self.assertFalse(verify_signature(params, 'secret'))

    def test_atomic_reuse_and_snapshot_survives_plan_edit(self):
        first = self.intent()
        db_execute(self.db, "UPDATE plans SET price='1元',days=1,gb=1 WHERE key='p1'")
        reused = self.intent()
        self.assertEqual(first['payment_id'], reused['payment_id'])
        self.assertEqual(reused['amount_fen'], 20000)
        self.assertEqual(json.loads(reused['snapshot_json'])['days'], 30)
        self.assertEqual(len(first['payment_id']), 30)
        self.assertTrue(first['payment_id'].isdigit())

    def test_repeat_and_cross_payment_trade_replay(self):
        first = self.intent()
        second = self.intent(tg=778)
        self.assertTrue(payments.mark_paid(self.db, first['payment_id'], 'trade-unique'))
        self.assertFalse(payments.mark_paid(self.db, first['payment_id'], 'trade-unique'))
        with self.assertRaises(PaymentError):
            payments.mark_paid(self.db, second['payment_id'], 'trade-unique')
        self.assertNotEqual(payments.get_intent(self.db, second['payment_id'])['status'], 'paid')
        self.assertEqual(len(db_query(self.db, 'SELECT * FROM payment_events')), 1)

    def test_creation_barrier_and_http_schema_do_not_recover_bot(self):
        intent = self.intent()
        self.assertTrue(payments.claim_creation(self.db, intent['payment_id']))
        self.assertFalse(payments.claim_creation(self.db, intent['payment_id']))
        db_execute(self.db, "UPDATE orders SET status='approved'")
        payments.init_payments(self.db)
        self.assertEqual(db_query(self.db, 'SELECT status FROM orders', one=True)['status'], 'approved')
        init_db(self.db)
        self.assertEqual(db_query(self.db, 'SELECT status FROM orders', one=True)['status'], 'unknown')
        self.assertFalse(payments.claim_creation(self.db, intent['payment_id']))

    def test_identity_and_paid_response_validation(self):
        intent = self.intent()
        for overrides in ({'pid': 'wrong'}, {'type': 'wxpay'}, {'money': '0.01'},
                          {'out_trade_no': 'other'}, {'trade_status': 'WAIT_BUYER_PAY'}):
            with self.subTest(overrides=overrides), self.assertRaises(PaymentError):
                validate_paid(self.notification(intent, **overrides), intent, CONFIG, notification=True)
        self.assertEqual(validate_paid(self.notification(intent), intent, CONFIG, notification=True), 'gateway-1')

    def test_other_product_adapter_and_renewal_ownership(self):
        with payments.transaction(self.db) as conn:
            other = payments.insert_intent(conn, product_kind='digital_book', business_id='book-order-1',
                                            owner_id='customer-1', amount_fen=5000, title='具体电子书商品', snapshot={'isbn': 'demo'})
        payments.mark_paid(self.db, other['payment_id'], 'book-trade')
        self.assertEqual(payments.get_intent(self.db, other['payment_id'])['product_kind'], 'digital_book')
        with self.assertRaises(PaymentError):
            self.intent(kind='renew', target=42)
        with self.assertRaises(PaymentError):
            self.intent(kind='invalid')

    def test_real_ip_trust_boundary(self):
        networks = (ipaddress.ip_network('172.18.0.0/16'),)
        self.assertEqual(real_client_ip('172.18.0.1', '1.1.1.1', networks), '1.1.1.1')
        self.assertEqual(real_client_ip('8.8.8.8', '1.1.1.1', networks), '8.8.8.8')
        self.assertEqual(real_client_ip('172.18.0.1', '2.2.2.2, 1.1.1.1', networks), '1.1.1.1')
        with self.assertRaises(PaymentError):
            real_client_ip('172.18.0.1', '', networks)


class TestUnpaidDeletion(PaymentFixture, unittest.TestCase):
    def setUp(self):
        self.seed()

    def tearDown(self):
        self.temp.cleanup()

    def test_delete_preserves_ledger_allows_new_order_and_restores_on_paid(self):
        intent = self.intent()
        order_id = intent['business_id']
        self.assertTrue(payments.delete_unpaid_order(self.db, order_id, 123))
        self.assertFalse(payments.delete_unpaid_order(self.db, order_id, 123))
        self.assertTrue(payments.is_order_deleted(self.db, order_id))
        self.assertFalse(payments.claim_creation(self.db, intent['payment_id']))
        self.assertEqual(db_query(self.db, f'SELECT count(*) c FROM orders WHERE {payments.VISIBLE_ORDER_SQL}', one=True)['c'], 0)
        self.assertEqual(db_query(self.db, 'SELECT count(*) c FROM orders', one=True)['c'], 1)
        self.assertIsNotNone(payments.get_intent(self.db, intent['payment_id']))
        replacement = self.intent()
        self.assertNotEqual(replacement['payment_id'], intent['payment_id'])
        self.assertEqual(payments.get_intent(self.db, intent['payment_id'])['status'], 'new')
        payments.mark_paid(self.db, intent['payment_id'], 'late-paid')
        self.assertFalse(payments.is_order_deleted(self.db, order_id))
        self.assertEqual(len(db_query(self.db, "SELECT * FROM order_audit_logs WHERE action='restore_on_payment'")), 1)
        with self.assertRaises(PaymentError):
            payments.delete_unpaid_order(self.db, order_id, 123)

    def test_only_unfulfilled_unpaid_online_orders_can_be_deleted(self):
        for state in ['new', 'creating', 'ready', 'unknown', 'redirected']:
            intent = self.intent(tg=1000 + len(state))
            db_execute(self.db, 'UPDATE payment_intents SET status=? WHERE payment_id=?', (state, intent['payment_id']))
            self.assertTrue(payments.delete_unpaid_order(self.db, intent['business_id'], 123))
        intent = self.intent(tg=3000)
        for state in ['approved', 'delivered', 'extension_applied', 'unknown', 'failed', 'rejected']:
            db_execute(self.db, 'UPDATE orders SET status=? WHERE order_id=?', (state, intent['business_id']))
            with self.assertRaises(PaymentError):
                payments.delete_unpaid_order(self.db, intent['business_id'], 123)
        db_execute(self.db, "UPDATE orders SET status='pending',delivered_user_id=42 WHERE order_id=?", (intent['business_id'],))
        with self.assertRaises(PaymentError):
            payments.delete_unpaid_order(self.db, intent['business_id'], 123)
        with self.assertRaises(PaymentError):
            payments.delete_unpaid_order(self.db, 'missing', 123)

    def test_delete_and_verified_payment_race_never_loses_paid_order(self):
        from concurrent.futures import ThreadPoolExecutor
        import threading
        for index in range(5):
            intent = self.intent(tg=4000 + index)
            barrier = threading.Barrier(2)
            def delete():
                barrier.wait()
                try:
                    payments.delete_unpaid_order(self.db, intent['business_id'], 123)
                except PaymentError:
                    pass
            def paid():
                barrier.wait()
                payments.mark_paid(self.db, intent['payment_id'], f'race-{index}')
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(delete), pool.submit(paid)]
                for future in futures:
                    future.result()
            self.assertEqual(payments.get_intent(self.db, intent['payment_id'])['status'], 'paid')
            self.assertFalse(payments.is_order_deleted(self.db, intent['business_id']))


class TestPaymentHTTP(PaymentFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.seed()
        self.gateway = SimpleNamespace(create=AsyncMock(return_value={'trade_no': 'gateway-1',
                                        'qrcode': 'https://qr.example.test/pay/1', 'payurl': 'https://pay.example.test/1'}),
                                       query=AsyncMock(return_value={'code': 1, 'status': 0}))
        self.app = create_app(self.db, CONFIG, client=self.gateway, trusted_proxies=('127.0.0.0/8',),
                              extra_routes=[web.get('/another-product/ping', lambda r: web.Response(text='pong'))])
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.temp.cleanup()

    async def test_valid_callback_duplicate_concurrent_and_health(self):
        intent = self.intent()
        data = self.notification(intent)
        responses = await asyncio.gather(*(self.client.get('/payments/zpay/notify', params=data) for _ in range(8)))
        for response in responses:
            self.assertEqual(response.status, 200)
            self.assertEqual(await response.text(), 'success')
        self.assertEqual(len(db_query(self.db, 'SELECT * FROM payment_events')), 1)
        self.assertEqual(db_query(self.db, 'SELECT status FROM orders', one=True)['status'], 'pending')
        self.gateway.create.assert_not_awaited()
        self.assertEqual((await self.client.get('/healthz')).status, 200)
        self.assertEqual(await (await self.client.get('/another-product/ping')).text(), 'pong')

    async def test_tampering_unknown_and_duplicate_fields_rejected(self):
        intent = self.intent()
        for overrides in ({'money': '1.00'}, {'pid': 'attacker'}, {'type': 'wxpay'},
                          {'trade_status': 'WAIT_BUYER_PAY'}, {'out_trade_no': 'missing'}):
            response = await self.client.get('/payments/zpay/notify', params=self.notification(intent, **overrides))
            self.assertEqual(response.status, 400)
        data = self.notification(intent)
        data['money'] = '0.01'  # Without recomputing the signature.
        self.assertEqual((await self.client.get('/payments/zpay/notify', params=data)).status, 400)
        params = list(self.notification(intent).items()) + [('money', '0.01')]
        self.assertEqual((await self.client.get('/payments/zpay/notify', params=params)).status, 400)
        self.assertNotEqual(payments.get_intent(self.db, intent['payment_id'])['status'], 'paid')

    async def test_checkout_get_is_safe_post_redirects_once_and_escapes_html(self):
        from html.parser import HTMLParser
        class Inputs(HTMLParser):
            def __init__(self):
                super().__init__(); self.fields = {}
            def handle_starttag(self, tag, attrs):
                data = dict(attrs)
                if tag == 'input': self.fields[data['name']] = data['value']
        intent = self.intent()
        db_execute(self.db, 'UPDATE payment_intents SET title=?', ('<script>alert(1)</script>',))
        path = '/payments/zpay/checkout/' + intent['checkout_token']
        response = await self.client.get(path)
        text = await response.text()
        self.assertIn('&lt;script&gt;', text)
        self.assertNotIn('<script>', text)
        self.assertNotIn('zpay-submit', text)
        self.assertEqual(payments.get_intent(self.db, intent['payment_id'])['status'], 'new')
        self.assertEqual(response.headers['Referrer-Policy'], 'same-origin')
        self.assertEqual((await self.client.post(path, data={'csrf': 'invalid'})).status, 403)
        data = {'csrf': csrf_value(intent['checkout_token'])}
        self.assertEqual((await self.client.post(path, data=data, headers={'Origin': 'https://evil.test'})).status, 403)
        # No client IP required: browser submits directly to the documented URL.
        responses = await asyncio.gather(*(self.client.post(path, data=data, headers={'Origin': CONFIG.public_url}) for _ in range(4)))
        bodies = [await r.text() for r in responses]
        self.assertEqual(sum('id="zpay-submit"' in body for body in bodies), 1)
        handoff = responses[next(i for i,b in enumerate(bodies) if 'id="zpay-submit"' in b)]
        body = bodies[next(i for i,b in enumerate(bodies) if 'id="zpay-submit"' in b)]
        self.assertIn('action="https://zpayz.cn/submit.php"', body)
        self.assertIn("form-action https://zpayz.cn", handoff.headers['Content-Security-Policy'])
        self.assertIn("script-src 'nonce-", handoff.headers['Content-Security-Policy'])
        self.assertNotIn(CONFIG.key, body)
        parser = Inputs(); parser.feed(body)
        self.assertTrue(verify_signature(parser.fields, CONFIG.key))
        self.assertEqual(parser.fields['return_url'], CONFIG.public_url + '/payments/zpay/return')
        self.assertEqual(parser.fields['money'], '200.00')
        self.assertEqual(parser.fields['name'], '<script>alert(1)</script>')
        self.assertEqual(payments.get_intent(self.db, intent['payment_id'])['status'], 'redirected')
        self.gateway.create.assert_not_awaited()

    async def test_opaque_origin_rejected_without_creating_payment(self):
        intent = self.intent()
        response = await self.client.post('/payments/zpay/checkout/' + intent['checkout_token'],
                                         data={'csrf': csrf_value(intent['checkout_token'])},
                                         headers={'Origin': 'null', 'X-Forwarded-For': '1.1.1.1'})
        self.assertEqual(response.status, 403)
        self.gateway.create.assert_not_awaited()
        self.assertEqual(payments.get_intent(self.db, intent['payment_id'])['status'], 'new')

    async def test_redirect_never_reissues_and_callback_can_arrive_after_handoff(self):
        intent = self.intent()
        path = '/payments/zpay/checkout/' + intent['checkout_token']
        data = {'csrf': csrf_value(intent['checkout_token'])}
        first = await self.client.post(path, data=data)
        self.assertIn('id="zpay-submit"', await first.text())
        for _ in range(2):
            response = await self.client.post(path, data=data)
            self.assertNotIn('id="zpay-submit"', await response.text())
        self.gateway.create.assert_not_awaited()
        self.assertEqual(payments.get_intent(self.db, intent['payment_id'])['status'], 'redirected')
        response = await self.client.get('/payments/zpay/notify', params=self.notification(intent))
        self.assertEqual(await response.text(), 'success')
        self.assertIn('支付成功', await (await self.client.get(path)).text())

    async def test_legacy_unknown_never_switches_to_redirect_or_replays(self):
        intent = self.intent()
        db_execute(self.db, "UPDATE payment_intents SET gateway_mode='api',status='unknown' WHERE payment_id=?", (intent['payment_id'],))
        path = '/payments/zpay/checkout/' + intent['checkout_token']
        self.gateway.query.return_value = {'code': 0, 'msg': '订单编号不存在'}
        response = await self.client.post(path, data={'csrf': csrf_value(intent['checkout_token'])})
        text = await response.text()
        self.assertIn('ZPAY 查单未成功', text)
        self.assertIn('订单编号不存在', text)
        self.assertIn('旧 API 订单', text)
        self.assertNotIn('id="zpay-submit"', text)
        response = await self.client.post(path, data={'csrf': csrf_value(intent['checkout_token'])})
        self.assertIn('30 秒', await response.text())
        self.gateway.create.assert_not_awaited()
        self.gateway.query.assert_awaited_once()
        self.assertEqual(payments.get_intent(self.db, intent['payment_id'])['status'], 'unknown')

    async def test_query_explanation_is_html_escaped(self):
        intent = self.intent()
        payments.claim_redirect(self.db, intent['payment_id'])
        self.gateway.query.side_effect = PaymentError('gateway_query_rejected', gateway_message='<script>alert(1)</script>')
        response = await self.client.post('/payments/zpay/checkout/' + intent['checkout_token'],
                                         data={'csrf': csrf_value(intent['checkout_token'])})
        text = await response.text()
        self.assertNotIn('<script>', text)
        self.assertIn('&lt;script&gt;', text)

    async def test_deleted_checkout_is_disabled_but_verified_callback_restores(self):
        intent = self.intent()
        path = '/payments/zpay/checkout/' + intent['checkout_token']
        payments.delete_unpaid_order(self.db, intent['business_id'], 123)
        for response in [await self.client.get(path), await self.client.post(path, data={'csrf': csrf_value(intent['checkout_token'])})]:
            self.assertEqual(response.status, 410)
            self.assertNotIn('<form', await response.text())
        self.gateway.create.assert_not_awaited()
        self.gateway.query.assert_not_awaited()
        tampered = await self.client.get('/payments/zpay/notify', params=self.notification(intent, money='0.01'))
        self.assertEqual(tampered.status, 400)
        self.assertTrue(payments.is_order_deleted(self.db, intent['business_id']))
        response = await self.client.get('/payments/zpay/notify', params=self.notification(intent))
        self.assertEqual(await response.text(), 'success')
        self.assertFalse(payments.is_order_deleted(self.db, intent['business_id']))
        self.assertEqual((await self.client.get(path)).status, 200)

    async def test_legacy_403_diagnostic_survives_reload_without_create_retry(self):
        intent = self.intent()
        db_execute(self.db, "UPDATE payment_intents SET gateway_mode='api' WHERE payment_id=?", (intent['payment_id'],))
        payments.claim_creation(self.db, intent['payment_id'])
        payments.creation_unknown(self.db, intent['payment_id'], 'gateway_http_error_403')
        path = '/payments/zpay/checkout/' + intent['checkout_token']
        text = await (await self.client.get(path)).text()
        self.assertIn('HTTP 403', text)
        self.gateway.create.assert_not_awaited()

    async def test_query_reconciliation_and_throttling(self):
        intent = self.intent()
        payments.claim_creation(self.db, intent['payment_id'])
        data = self.notification(intent)
        data.update(code=1, status=1)
        self.gateway.query.return_value = data
        result = await reconcile_payment(self.db, intent['payment_id'], CONFIG, self.gateway)
        self.assertEqual(result['status'], 'paid')
        await reconcile_payment(self.db, intent['payment_id'], CONFIG, self.gateway)
        self.gateway.query.assert_awaited_once()


    async def test_return_is_not_payment_proof_and_unsigned_return_is_rejected(self):
        intent = self.intent()
        payments.claim_redirect(self.db, intent['payment_id'])
        response = await self.client.get('/payments/zpay/return', params={'out_trade_no': intent['payment_id']}, allow_redirects=False)
        self.assertEqual(response.status, 400)
        self.gateway.query.assert_not_awaited()
        data = self.notification(intent)
        response = await self.client.get('/payments/zpay/return', params=data, allow_redirects=False)
        self.assertEqual(response.status, 303)
        self.assertEqual(response.headers['Location'], '/payments/zpay/checkout/' + intent['checkout_token'])
        self.assertEqual(payments.get_intent(self.db, intent['payment_id'])['status'], 'redirected')
        self.assertEqual(len(db_query(self.db, 'SELECT * FROM payment_events')), 0)
        self.gateway.create.assert_not_awaited()

    async def test_return_paid_requires_server_query_and_restores_deleted_order(self):
        intent = self.intent()
        payments.claim_redirect(self.db, intent['payment_id'])
        payments.delete_unpaid_order(self.db, intent['business_id'], 123)
        data = self.notification(intent)
        self.gateway.query.return_value = dict(data, code=1, status=1)
        response = await self.client.get('/payments/zpay/return', params=data, allow_redirects=False)
        self.assertEqual(response.status, 303)
        self.assertEqual(payments.get_intent(self.db, intent['payment_id'])['status'], 'paid')
        self.assertFalse(payments.is_order_deleted(self.db, intent['business_id']))
        self.assertEqual(len(db_query(self.db, 'SELECT * FROM payment_events')), 1)

    async def test_return_tampered_amount_duplicate_and_gateway_failure_never_mark_paid(self):
        intent = self.intent()
        payments.claim_redirect(self.db, intent['payment_id'])
        response = await self.client.get('/payments/zpay/return', params=self.notification(intent, money='0.01'), allow_redirects=False)
        self.assertEqual(response.status, 400)
        params = list(self.notification(intent).items()) + [('money', '0.01')]
        self.assertEqual((await self.client.get('/payments/zpay/return', params=params)).status, 400)
        self.gateway.query.return_value = {'code': 0, 'msg': '订单编号不存在'}
        response = await self.client.get('/payments/zpay/return', params=self.notification(intent), allow_redirects=False)
        self.assertEqual(response.status, 303)
        self.assertEqual(payments.get_intent(self.db, intent['payment_id'])['status'], 'redirected')

    async def test_delete_wins_redirect_race_never_issues_signed_form(self):
        intent = self.intent()
        def deleted_before_claim(db, payment_id):
            payments.delete_unpaid_order(db, intent['business_id'], 123)
            return payments.claim_redirect(db, payment_id)
        with patch('services.http_server.claim_redirect', side_effect=deleted_before_claim):
            response = await self.client.post('/payments/zpay/checkout/' + intent['checkout_token'],
                                             data={'csrf': csrf_value(intent['checkout_token'])})
        self.assertEqual(response.status, 410)
        self.assertNotIn('id="zpay-submit"', await response.text())
        self.gateway.create.assert_not_awaited()


class TestZPayTransport(unittest.IsolatedAsyncioTestCase):
    async def test_submit_fields_signature_and_query_no_redirect_or_retry(self):
        calls = []
        def transport(request):
            calls.append(request)
            self.assertEqual(request.method, 'GET')
            self.assertEqual(request.url.path, '/api.php')
            return httpx.Response(302, headers={'location': 'https://evil.test'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
            client = ZPayClient(CONFIG, http)
            fields = client.submit_params(dict(payment_id='123',amount_fen=100,title='具体商品'))
            self.assertEqual(calls, [])
            self.assertEqual(fields['return_url'], CONFIG.public_url + '/payments/zpay/return')
            self.assertTrue(verify_signature(fields, CONFIG.key))
            self.assertNotIn('key', fields)
            self.assertNotIn('clientip', fields)
            self.assertFalse(hasattr(client, 'create'))
            with self.assertRaises(PaymentError):
                await client.query('123')
        self.assertEqual(len(calls), 1)

    async def test_query_message_redacts_secrets(self):
        from services.zpay import gateway_message
        message = gateway_message({'msg': '渠道未开通 ' + CONFIG.key + ' https://example.test?key=secret ' + 'a' * 32}, CONFIG)
        self.assertIn('渠道未开通', message)
        for secret in (CONFIG.key, 'https://', 'a' * 32):
            self.assertNotIn(secret, message)

    async def test_query_http_error_and_timeout_attempted_once(self):
        for mode in ('timeout','503'):
            calls = []
            def transport(request):
                calls.append(request)
                if mode == 'timeout': raise httpx.ReadTimeout('secret must not escape')
                return httpx.Response(503, text='secret must not escape')
            async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
                with self.assertRaises(PaymentError) as error:
                    await ZPayClient(CONFIG, http).query('123')
            self.assertNotIn('secret', str(error.exception))
            self.assertEqual(len(calls), 1)


class TestPaidFulfillment(PaymentFixture, unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.module_temp = tempfile.TemporaryDirectory()
        config_path = Path(cls.module_temp.name) / 'config.json'
        config_path.write_text(json.dumps({'admin_id': '123', 'bot_token': '123:test-token'}))
        with patch.dict(os.environ, {'REMNASHOP_CONFIG': str(config_path),
                                    'REMNASHOP_DB': str(Path(cls.module_temp.name) / 'module.db')}):
            cls.bot = importlib.import_module('bot')

    @classmethod
    def tearDownClass(cls):
        cls.module_temp.cleanup()

    async def asyncSetUp(self):
        self.seed()
        self.db_patch = patch.object(self.bot, 'DB_FILE', self.db)
        self.db_patch.start()
        self.context = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock(), send_photo=AsyncMock()))
        self.ui = AsyncMock()

    async def asyncTearDown(self):
        self.db_patch.stop()
        self.temp.cleanup()

    async def test_worker_dispatches_only_paid_subscription_orders(self):
        intent = self.intent()
        payments.mark_paid(self.db, intent['payment_id'], 'paid-worker')
        with payments.transaction(self.db) as conn:
            other = payments.insert_intent(conn, product_kind='book', business_id='other', owner_id='777',
                                           amount_fen=100, title='电子书', snapshot={})
        payments.mark_paid(self.db, other['payment_id'], 'book-worker')
        fulfill = AsyncMock()
        with patch.object(self.bot, 'payment_config_ready', return_value=False), \
             patch.object(self.bot, 'fulfill_subscription_order', fulfill):
            await self.bot.process_paid_orders_job(self.context)
        fulfill.assert_awaited_once_with(intent['business_id'], self.context)

    async def test_stale_manual_callbacks_and_online_cancellation_are_blocked(self):
        intent = self.intent()
        query = SimpleNamespace(data='client_order_cancel_' + intent['business_id'],
                                 from_user=SimpleNamespace(id=777), answer=AsyncMock())
        update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=777))
        context = SimpleNamespace(user_data={})
        # Reset the UI cooldown for every synthetic callback.
        for data in ('client_order_cancel_' + intent['business_id'], 'manualreview_p1_new_0', 'paymethod_usdt_p1_new_0'):
            query.data = data
            with patch.object(self.bot, 'check_cooldown', return_value=True):
                await self.bot.client_menu_handler(update, context)
        self.assertEqual(len(db_query(self.db, 'SELECT * FROM orders')), 1)
        self.assertEqual(db_query(self.db, 'SELECT status FROM orders', one=True)['status'], 'pending')
        query.data = 'rj_' + intent['business_id']
        query.from_user.id = 123
        context.bot = self.context.bot
        await self.bot.process_order(update, context)
        self.assertEqual(db_query(self.db, 'SELECT status FROM orders', one=True)['status'], 'pending')

    async def test_payment_query_result_is_message_not_second_callback_answer(self):
        intent = self.intent()
        query = SimpleNamespace(data='client_paycheck_' + intent['business_id'],
                                from_user=SimpleNamespace(id=777), answer=AsyncMock())
        update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=777))
        context = SimpleNamespace(user_data={})
        for status in ('new', 'ready', 'unknown', 'redirected', 'paid', 'unavailable'):
            query.answer.reset_mock()
            returned = dict(intent, status=status)
            reconcile = AsyncMock(return_value=returned)
            if status == 'unavailable':
                reconcile.side_effect = PaymentError('gateway_unavailable')
            with patch.object(self.bot, 'check_cooldown', return_value=True), \
                 patch.object(self.bot.payments, 'for_business', return_value=returned), \
                 patch.object(self.bot.ZPayConfig, 'from_env', return_value=CONFIG), \
                 patch.object(self.bot, 'reconcile_payment', reconcile), \
                 patch.object(self.bot, 'send_or_edit_menu', AsyncMock()) as menu:
                await self.bot.client_menu_handler(update, context)
                query.answer.assert_awaited_once_with()
                text = menu.await_args.args[2]
                if status == 'new':
                    self.assertIn('尚未生成支付单', text)
                    reconcile.assert_not_awaited()
                elif status == 'paid':
                    self.assertIn('已付款', text)
                elif status == 'unavailable':
                    self.assertIn('查询暂不可用', text)
                else:
                    self.assertIn('暂未确认付款', text)
                self.assertIn('client_order_', repr(menu.await_args.args[3]))

    async def test_admin_unpaid_delete_requires_role_confirmation_and_current_unpaid_state(self):
        intent = self.intent()
        order_id = intent['business_id']
        query = SimpleNamespace(data='admin_order_delete_' + order_id,
                                from_user=SimpleNamespace(id=777), answer=AsyncMock())
        update = SimpleNamespace(callback_query=query)
        context = SimpleNamespace(user_data={})
        with patch.object(self.bot, 'send_or_edit_menu', AsyncMock()) as menu:
            await self.bot.admin_menu_handler(update, context)
            self.assertFalse(payments.is_order_deleted(self.db, order_id))
            self.assertNotIn('order_delete_confirmation', context.user_data)
            query.from_user.id = self.bot.ADMIN_ID
            await self.bot.admin_menu_handler(update, context)
            token = context.user_data['order_delete_confirmation']['token']
            self.assertIn('不会关闭', menu.await_args.args[2])
            # A payment wins while the confirmation is open: deletion is blocked.
            payments.mark_paid(self.db, intent['payment_id'], 'paid-before-confirm')
            query.data = 'admin_order_delete_apply_' + token
            await self.bot.admin_menu_handler(update, context)
            self.assertFalse(payments.is_order_deleted(self.db, order_id))
            self.assertIn('操作已停止', menu.await_args.args[2])
            # Independent unpaid order; the paid pending order must not be recreated.
            intent = self.intent(tg=778)
            query.data = 'admin_order_delete_' + intent['business_id']
            await self.bot.admin_menu_handler(update, context)
            query.data = 'admin_order_delete_apply_' + context.user_data['order_delete_confirmation']['token']
            await self.bot.admin_menu_handler(update, context)
            self.assertTrue(payments.is_order_deleted(self.db, intent['business_id']))
            await self.bot.admin_menu_handler(update, context)
            self.assertIn('失效', menu.await_args.args[2])
            self.assertEqual(len(db_query(self.db, "SELECT * FROM order_audit_logs WHERE action='admin_delete_unpaid'")), 1)

    async def test_admin_delete_cancel_and_expiry_invalidate_confirmation(self):
        intent = self.intent()
        query = SimpleNamespace(data='admin_order_delete_' + intent['business_id'],
                                from_user=SimpleNamespace(id=self.bot.ADMIN_ID), answer=AsyncMock())
        update = SimpleNamespace(callback_query=query)
        context = SimpleNamespace(user_data={})
        with patch.object(self.bot, 'send_or_edit_menu', AsyncMock()):
            await self.bot.admin_menu_handler(update, context)
            token = context.user_data['order_delete_confirmation']['token']
            context.user_data['order_delete_confirmation']['expires_at'] = 0
            query.data = 'admin_order_delete_apply_' + token
            await self.bot.admin_menu_handler(update, context)
            self.assertFalse(payments.is_order_deleted(self.db, intent['business_id']))
            query.data = 'admin_order_delete_' + intent['business_id']
            await self.bot.admin_menu_handler(update, context)
            token = context.user_data['order_delete_confirmation']['token']
            query.data = 'admin_orders_menu'
            await self.bot.admin_menu_handler(update, context)
            query.data = 'admin_order_delete_apply_' + token
            await self.bot.admin_menu_handler(update, context)
            self.assertFalse(payments.is_order_deleted(self.db, intent['business_id']))

    async def test_paid_renewal_uses_snapshot_and_extends_once(self):
        db_execute(self.db, 'INSERT INTO subscriptions(tg_id,user_id,created_at) VALUES (777,42,1)')
        intent = self.intent(kind='renew', target=42)
        payments.mark_paid(self.db, intent['payment_id'], 'renew-success')
        db_execute(self.db, "UPDATE plans SET days=1,gb=1 WHERE key='p1'")
        user = {'id': 42, 'telegramId': 777, 'trafficLimitBytes': 100}
        extend = AsyncMock(return_value=user)
        patch_user = AsyncMock(return_value=SimpleNamespace(status_code=200))
        with patch.object(self.bot, 'panel_config_ready', return_value=True), \
             patch.object(self.bot, 'get_panel_user', AsyncMock(return_value=user)), \
             patch.object(self.bot, 'extend_subscription', extend), \
             patch.object(self.bot, 'patch_panel_user', patch_user), \
             patch.object(self.bot, 'sync_user_metadata', AsyncMock()), \
             patch.object(self.bot, 'send_subscription_card', AsyncMock()):
            await self.bot.fulfill_subscription_order(intent['business_id'], self.context, update_ui=self.ui)
            await self.bot.fulfill_subscription_order(intent['business_id'], self.context, update_ui=self.ui)
        extend.assert_awaited_once_with(42, 30)
        self.assertEqual(patch_user.await_args.args[0]['trafficLimitBytes'], 100 + 100 * 1024**3)
        self.assertEqual(db_query(self.db, 'SELECT status FROM orders', one=True)['status'], 'delivered')

    async def test_unpaid_cannot_be_fulfilled_even_by_admin(self):
        intent = self.intent()
        create = AsyncMock()
        with patch.object(self.bot, 'create_panel_user', create):
            await self.bot.fulfill_subscription_order(intent['business_id'], self.context, actor_id=123, update_ui=self.ui)
        create.assert_not_awaited()
        self.assertEqual(db_query(self.db, 'SELECT status FROM orders', one=True)['status'], 'pending')

    async def test_paid_new_order_uses_snapshot_deleted_plan_and_only_creates_once(self):
        intent = self.intent()
        payments.mark_paid(self.db, intent['payment_id'], 'paid-new')
        db_execute(self.db, "DELETE FROM plans WHERE key='p1'")
        create = AsyncMock(return_value=SimpleNamespace(status_code=201))
        with patch.object(self.bot, 'panel_config_ready', return_value=True), \
             patch.object(self.bot, 'TARGET_GROUP_UUID', 'changed-group'), \
             patch.object(self.bot, 'get_user_by_username', AsyncMock(return_value=None)), \
             patch.object(self.bot, 'create_panel_user', create), \
             patch.object(self.bot, 'extract_payload', return_value={'id': 42, 'subscriptionUrl': 'https://sub.test/secret'}), \
             patch.object(self.bot, 'sync_user_metadata', AsyncMock()), \
             patch.object(self.bot, 'generate_qr', return_value=b'qr'):
            await asyncio.gather(*(self.bot.fulfill_subscription_order(intent['business_id'], self.context, update_ui=self.ui) for _ in range(3)))
        create.assert_awaited_once()
        self.assertEqual(create.await_args.args[0]['activeInternalSquads'], ['fixed-group'])
        self.assertEqual(create.await_args.args[0]['trafficLimitBytes'], 100 * 1024**3)
        self.assertEqual(db_query(self.db, 'SELECT status FROM orders', one=True)['status'], 'delivered')

    async def test_paid_renewal_timeout_is_never_replayed(self):
        db_execute(self.db, 'INSERT INTO subscriptions(tg_id,user_id,created_at) VALUES (777,42,1)')
        intent = self.intent(kind='renew', target=42)
        payments.mark_paid(self.db, intent['payment_id'], 'paid-renew')
        extend = AsyncMock(side_effect=RuntimeError('timeout'))
        with patch.object(self.bot, 'panel_config_ready', return_value=True), \
             patch.object(self.bot, 'get_panel_user', AsyncMock(return_value={'id': 42, 'telegramId': 777})), \
             patch.object(self.bot, 'extend_subscription', extend):
            for _ in range(2):
                await self.bot.fulfill_subscription_order(intent['business_id'], self.context, update_ui=self.ui)
        extend.assert_awaited_once()
        self.assertEqual(db_query(self.db, 'SELECT status FROM orders', one=True)['status'], 'unknown')

    async def test_multi_groups_create_and_renew_from_frozen_plan(self):
        a = '11111111-1111-4111-8111-111111111111'
        b = '22222222-2222-4222-8222-222222222222'
        inventory = [{'uuid': a, 'name': 'A'}, {'uuid': b, 'name': 'B'}]
        db_execute(self.db, "UPDATE plans SET group_uuids=? WHERE key='p1'", (json.dumps([a, b]),))
        intent = payments.subscription_checkout(self.db, 777, 'p1', 'new', None)
        payments.mark_paid(self.db, intent['payment_id'], 'multi-new')
        create = AsyncMock(return_value=SimpleNamespace(status_code=201))
        with patch.object(self.bot, 'panel_config_ready', return_value=True), \
             patch.object(self.bot, 'get_internal_squads', AsyncMock(return_value=inventory)), \
             patch.object(self.bot, 'get_user_by_username', AsyncMock(return_value=None)), \
             patch.object(self.bot, 'create_panel_user', create), \
             patch.object(self.bot, 'extract_payload', return_value={'id': 42, 'subscriptionUrl': 'https://sub.test/secret'}), \
             patch.object(self.bot, 'sync_user_metadata', AsyncMock()), \
             patch.object(self.bot, 'generate_qr', return_value=b'qr'):
            await self.bot.fulfill_subscription_order(intent['business_id'], self.context, update_ui=self.ui)
        self.assertEqual(create.await_args.args[0]['activeInternalSquads'], [a, b])
        renew = payments.subscription_checkout(self.db, 777, 'p1', 'renew', 42)
        payments.mark_paid(self.db, renew['payment_id'], 'multi-renew')
        db_execute(self.db, "UPDATE plans SET group_uuids=? WHERE key='p1'", (json.dumps([a]),))
        user = {'id': 42, 'telegramId': 777, 'trafficLimitBytes': 100}
        patch_user = AsyncMock(return_value=SimpleNamespace(status_code=200))
        with patch.object(self.bot, 'panel_config_ready', return_value=True), \
             patch.object(self.bot, 'get_internal_squads', AsyncMock(return_value=inventory)), \
             patch.object(self.bot, 'get_panel_user', AsyncMock(return_value=user)), \
             patch.object(self.bot, 'extend_subscription', AsyncMock(return_value=user)), \
             patch.object(self.bot, 'patch_panel_user', patch_user), \
             patch.object(self.bot, 'sync_user_metadata', AsyncMock()), \
             patch.object(self.bot, 'send_subscription_card', AsyncMock()):
            await self.bot.fulfill_subscription_order(renew['business_id'], self.context, update_ui=self.ui)
        self.assertEqual(patch_user.await_args.args[0]['activeInternalSquads'], [a, b])

    async def test_deleted_group_blocks_panel_writes(self):
        a = '11111111-1111-4111-8111-111111111111'
        db_execute(self.db, "UPDATE plans SET group_uuids=? WHERE key='p1'", (json.dumps([a]),))
        intent = payments.subscription_checkout(self.db, 777, 'p1', 'new', None)
        payments.mark_paid(self.db, intent['payment_id'], 'missing-group')
        with patch.object(self.bot, 'panel_config_ready', return_value=True), \
             patch.object(self.bot, 'get_internal_squads', AsyncMock(return_value=[])), \
             patch.object(self.bot, 'create_panel_user', AsyncMock()) as create:
            await self.bot.fulfill_subscription_order(intent['business_id'], self.context, update_ui=self.ui)
        create.assert_not_awaited()
        self.assertEqual(db_query(self.db, 'SELECT status FROM orders', one=True)['status'], 'failed')

    async def test_admin_picker_pagination_toggle_save_and_stale_callback(self):
        inventory = [{'uuid': f'{i:08x}-1111-4111-8111-111111111111', 'name': f'Group {i}'} for i in range(1, 20)]
        self.context.user_data = {}
        update = SimpleNamespace(callback_query=SimpleNamespace(data='', from_user=SimpleNamespace(id=123), answer=AsyncMock()))
        with patch.object(self.bot, 'get_internal_squads', AsyncMock(return_value=inventory)), \
             patch.object(self.bot, 'send_or_edit_menu', AsyncMock()) as menu, \
             patch.object(self.bot, 'ADMIN_ID', 123):
            await self.bot.begin_plan_group_selection(update, self.context, 'p1')
            state = self.context.user_data['plan_group_selection']
            state['selected'] = []
            token = state['token']
            for action in ['toggle_0', 'page_2', 'toggle_18', 'save']:
                update.callback_query.data = f'admin_pg_{token}_{action}'
                await self.bot.admin_menu_handler(update, self.context)
            self.assertEqual(json.loads(db_query(self.db, "SELECT group_uuids FROM plans WHERE key='p1'", one=True)['group_uuids']),
                             [inventory[0]['uuid'], inventory[18]['uuid']])
            await self.bot.admin_menu_handler(update, self.context)
            self.assertIn('失效', menu.await_args.args[2])

    async def test_admin_picker_empty_selection_no_save_and_non_admin_denied(self):
        inventory = [{'uuid': '11111111-1111-4111-8111-111111111111', 'name': 'A'}]
        self.context.user_data = {}
        update = SimpleNamespace(callback_query=SimpleNamespace(data='', from_user=SimpleNamespace(id=123), answer=AsyncMock()))
        with patch.object(self.bot, 'get_internal_squads', AsyncMock(return_value=inventory)) as get, \
             patch.object(self.bot, 'send_or_edit_menu', AsyncMock()), \
             patch.object(self.bot, 'ADMIN_ID', 123):
            await self.bot.begin_plan_group_selection(update, self.context, 'p1')
            state = self.context.user_data['plan_group_selection']
            state['selected'] = []
            update.callback_query.data = f'admin_pg_{state["token"]}_save'
            await self.bot.admin_menu_handler(update, self.context)
            self.assertIsNone(db_query(self.db, "SELECT group_uuids FROM plans WHERE key='p1'", one=True)['group_uuids'])
            update.callback_query.from_user.id = 999
            get.reset_mock()
            await self.bot.admin_menu_handler(update, self.context)
            get.assert_not_awaited()

    async def test_admin_creates_plan_only_after_group_selection(self):
        inventory = [{'uuid': '11111111-1111-4111-8111-111111111111', 'name': 'A'}]
        self.context.user_data = {'add_plan_step': 'strategy', 'new_plan': {'name': '多组套餐', 'price': '1.00', 'days': 30, 'gb': 10}}
        update = SimpleNamespace(callback_query=SimpleNamespace(data='set_strategy_NO_RESET', from_user=SimpleNamespace(id=123), answer=AsyncMock()))
        with patch.object(self.bot, 'get_internal_squads', AsyncMock(return_value=inventory)), \
             patch.object(self.bot, 'send_or_edit_menu', AsyncMock()), \
             patch.object(self.bot, 'ADMIN_ID', 123):
            await self.bot.admin_menu_handler(update, self.context)
            self.assertEqual(len(db_query(self.db, 'SELECT * FROM plans')), 2)
            state = self.context.user_data['plan_group_selection']
            for action in ['toggle_0', 'save', 'save']:
                update.callback_query.data = f'admin_pg_{state["token"]}_{action}'
                await self.bot.admin_menu_handler(update, self.context)
            plans = db_query(self.db, "SELECT * FROM plans WHERE name='多组套餐'")
            self.assertEqual(len(plans), 1)
            self.assertEqual(json.loads(plans[0]['group_uuids']), [inventory[0]['uuid']])
