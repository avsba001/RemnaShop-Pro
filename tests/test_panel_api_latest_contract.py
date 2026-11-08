"""Offline release lock and real HTTP transport regressions for Panel 3.4.8."""

import hashlib
import json
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from services import panel_api
from utils.constants import PANEL_CONTRACT_VERSION, PANEL_TARGET_VERSION

URL = 'https://panel.example/api'
HEADERS = {'Authorization': 'Bearer test-token'}
ROOT = Path(__file__).resolve().parents[1]


def canonical_sha256(value):
    data = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    return hashlib.sha256(data.encode('utf-8')).hexdigest()


class TestLatestContractLock(unittest.TestCase):
    def test_release_version_and_official_contract_are_locked(self):
        lock = json.loads((ROOT / 'docs/remnawave-contract-lock.json').read_text(encoding='utf-8'))
        # read_text normalizes CRLF so this check also works after a Windows checkout.
        text = (ROOT / 'docs/remnawave-openapi.json').read_text(encoding='utf-8')
        spec = json.loads(text)
        self.assertEqual(PANEL_TARGET_VERSION, '3.4.8')
        self.assertEqual(PANEL_CONTRACT_VERSION, '3.4.15')
        self.assertEqual(lock['panel_version'], PANEL_TARGET_VERSION)
        self.assertEqual(lock['contract_version'], PANEL_CONTRACT_VERSION)
        self.assertEqual(spec['info']['version'], PANEL_TARGET_VERSION)
        self.assertEqual(lock['vendored_lf_sha256'], hashlib.sha256(text.encode('utf-8')).hexdigest())
        self.assertEqual(lock['paths_canonical_sha256'], canonical_sha256(spec['paths']))
        self.assertEqual(lock['components_canonical_sha256'], canonical_sha256(spec['components']))
        self.assertEqual(lock['path_count'], len(spec['paths']))
        self.assertEqual(lock['schema_count'], len(spec['components']['schemas']))
        verbs = {'get', 'post', 'put', 'patch', 'delete', 'head', 'options', 'trace'}
        self.assertEqual(lock['operation_count'], sum(
            method in verbs for path in spec['paths'].values() for method in path))
        self.assertEqual(lock['release_url'],
                         f'https://github.com/remnawave/backend/releases/tag/{PANEL_TARGET_VERSION}')
        self.assertEqual(lock['contract_package_url'],
                         f'https://raw.githubusercontent.com/remnawave/backend/{PANEL_TARGET_VERSION}'
                         '/libs/contract/package.json')


class TestLatestPanelTransport(unittest.IsolatedAsyncioTestCase):
    async def test_mutating_methods_never_replay_retryable_responses(self):
        for method in ('POST', 'PUT', 'PATCH', 'DELETE'):
            for status in (429, 500, 502, 503, 504):
                with self.subTest(method=method, status=status):
                    calls = []

                    def handler(request):
                        calls.append(request)
                        return httpx.Response(status, headers={'Retry-After': '0'})

                    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                        with patch.object(panel_api, '_get_client', return_value=client):
                            response = await panel_api.safe_api_request(
                                method, '/users', URL, HEADERS, json_data={'id': 42}, retry=True)
                    self.assertEqual(response.status_code, status)
                    self.assertEqual(len(calls), 1)
                    self.assertEqual(json.loads(calls[0].content), {'id': 42})

    async def test_lost_write_response_is_not_replayed(self):
        for method in ('POST', 'PUT', 'PATCH', 'DELETE'):
            with self.subTest(method=method):
                calls = []

                def handler(request):
                    calls.append(request)
                    raise httpx.ReadTimeout('Response lost after Panel may have accepted the write',
                                            request=request)

                async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                    with patch.object(panel_api, '_get_client', return_value=client):
                        with self.assertRaises(panel_api.PanelApiError):
                            await panel_api.safe_api_request(method, '/users', URL, HEADERS)
                self.assertEqual(len(calls), 1)

    async def test_read_retries_and_retry_false_are_preserved(self):
        for failure in ('status', 'timeout'):
            for retry, expected in ((True, 3), (False, 1)):
                with self.subTest(failure=failure, retry=retry):
                    calls = []

                    def handler(request):
                        calls.append(request)
                        if failure == 'timeout':
                            raise httpx.ReadTimeout('timeout', request=request)
                        return httpx.Response(503)

                    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                        with patch.object(panel_api, '_get_client', return_value=client), \
                                patch.object(panel_api.asyncio, 'sleep', new_callable=AsyncMock):
                            if failure == 'timeout':
                                with self.assertRaises(panel_api.PanelApiError):
                                    await panel_api.safe_api_request('GET', '/nodes', URL, HEADERS,
                                                                     retry=retry)
                            else:
                                response = await panel_api.safe_api_request('GET', '/nodes', URL,
                                                                            HEADERS, retry=retry)
                                self.assertEqual(response.status_code, 503)
                    self.assertEqual(len(calls), expected)

    async def test_get_recovers_from_transient_failure_without_losing_query_or_auth(self):
        calls = []

        def handler(request):
            calls.append(request)
            if len(calls) == 1:
                return httpx.Response(429, headers={'Retry-After': '0'})
            return httpx.Response(200, json={'response': {'users': [], 'hasMore': False}})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with patch.object(panel_api, '_get_client', return_value=client), \
                    patch.object(panel_api.asyncio, 'sleep', new_callable=AsyncMock) as sleep:
                users = await panel_api.get_users_by_telegram_id(123, URL, HEADERS)
        self.assertEqual(users, [])
        self.assertEqual(len(calls), 2)
        sleep.assert_awaited_once_with(0.0)
        for request in calls:
            self.assertEqual(request.url.path, '/api/users/stream')
            self.assertEqual(request.url.params['telegramId'], '123')
            self.assertEqual(request.headers['Authorization'], HEADERS['Authorization'])

    async def test_existing_create_and_reset_wrappers_are_protected_at_transport(self):
        for operation in ('create', 'reset', 'bulk_reset', 'bulk_delete'):
            with self.subTest(operation=operation):
                calls = []

                def handler(request):
                    calls.append(request)
                    raise httpx.ReadTimeout('lost response', request=request)

                async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                    with patch.object(panel_api, '_get_client', return_value=client):
                        with self.assertRaises(panel_api.PanelApiError):
                            if operation == 'create':
                                await panel_api.create_user(
                                    {'username': 'alice', 'expireAt': '2026-12-01T00:00:00Z'}, URL, HEADERS)
                            elif operation == 'reset':
                                await panel_api.reset_user_traffic(42, URL, HEADERS)
                            elif operation == 'bulk_reset':
                                await panel_api.bulk_reset_traffic_users([42], URL, HEADERS)
                            else:
                                await panel_api.bulk_delete_users([42], URL, HEADERS)
                self.assertEqual(len(calls), 1)
