from datetime import date
import unittest
from unittest.mock import patch, MagicMock, AsyncMock, ANY

import httpx

from scripts.check_package_age import check_age, release_date
from scripts.invoke_hypothesis_agent import _invoke, _parser, request_body, wait_for_phase
from scripts.register_hypothesis_toolboxes import main as register


class PackageAgeTests(unittest.TestCase):
    def test_inclusive_age_and_fixed_cutoff(self):
        check_age(date(2026, 10, 2), date(2026, 10, 9), 7, date(2026, 10, 2))
        for today, published in [(date(2026, 10, 9), date(2026, 10, 3)),
                                 (date(2026, 10, 7), date(2026, 10, 1)),
                                 (date(2026, 11, 1), date(2026, 10, 3))]:
            with self.assertRaises(ValueError):
                check_age(published, today, 7, date(2026, 10, 2))

    def test_yanks_prereleases_and_missing_dates_fail_closed(self):
        metadata = {'releases': {'1.0.0': [
            {'yanked': True, 'upload_time_iso_8601': '2026-09-01T00:00:00Z'},
            {'yanked': False, 'upload_time_iso_8601': '2026-10-03T00:00:00Z'}]}}
        self.assertEqual(release_date(metadata, '1.0.0', 'python'), date(2026, 10, 3))
        for version in ['1.0.0rc1', '2.0.0']:
            with self.assertRaises(ValueError): release_date(metadata, version, 'python')
        with self.assertRaises(ValueError): release_date({'versions': {'1.0.0': {}}}, '1.0.0', 'npm')
        with self.assertRaises(ValueError): release_date({'time': {'1.0.0': '2026-01-01T00:00:00Z'}}, '1.0.0', 'npm')


class InvocationTests(unittest.TestCase):
    def test_phase_contract_and_explicit_retry_id(self):
        args = _parser().parse_args(['approve', 'wf', 'wf-p001', '2', 'sha256:test', 'approved', '--request-id', 'same'])
        body = request_body(args)
        self.assertEqual((body['phase_id'], body['plan_revision'], body['client_request_id']), ('wf-p001', 2, 'same'))
        args = _parser().parse_args(['revise', 'wf', 'wf-p001', 'feedback'])
        self.assertEqual(request_body(args)['scenario'], 'feedback')
        args = _parser().parse_args(['events', 'wf', '--after-seq', '8', '--follow'])
        self.assertEqual(request_body(args)['after_seq'], 8)
        self.assertTrue(request_body(args)['stream'])


class InvocationTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_retries_transient_transport_error(self):
        response = MagicMock()
        response.json.return_value = {"status": "accepted"}
        post = AsyncMock(side_effect=[httpx.ConnectError("transient"), response])
        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)
        client.post = post
        with patch("scripts.invoke_hypothesis_agent.httpx.AsyncClient", return_value=client), \
             patch("scripts.invoke_hypothesis_agent.EntraTokenAuth"), \
             patch("scripts.invoke_hypothesis_agent._endpoint", return_value="https://example.test"), \
             patch("scripts.invoke_hypothesis_agent.asyncio.sleep", new_callable=AsyncMock) as sleep:
            result = await _invoke({"action": "status"})
        self.assertEqual(result, {"status": "accepted"})
        self.assertEqual(post.await_count, 2)
        sleep.assert_awaited_once_with(1)


class RegistrationTests(unittest.TestCase):
    def test_failed_creation_does_not_delete_working_toolbox_or_save_endpoints(self):
        client = MagicMock()
        client.toolboxes.create_version.side_effect = RuntimeError('create failed')
        with patch('scripts.register_hypothesis_toolboxes.load_env'), \
             patch('scripts.register_hypothesis_toolboxes.env', side_effect=lambda key, default=None, **kw: default or 'https://example.test'), \
             patch('scripts.register_hypothesis_toolboxes.project_client') as project, \
             patch('scripts.register_hypothesis_toolboxes.save_env') as save:
            project.return_value.__enter__.return_value = client
            with self.assertRaises(RuntimeError): register()
            client.toolboxes.delete.assert_not_called()
            save.assert_not_called()


class PollingTests(unittest.IsolatedAsyncioTestCase):
    async def test_polls_phase_not_workflow_status_until_approval(self):
        payloads = [
            {"status": "active", "phase": {"phase_id": "p", "stage": "planning"}},
            {"status": "active", "phase": {"phase_id": "p", "stage": "awaiting_approval"}},
        ]
        with patch("scripts.invoke_hypothesis_agent._invoke", new_callable=AsyncMock, side_effect=payloads) as invoke:
            result = await wait_for_phase("wf", "p", {"awaiting_approval"}, interval=0)
        self.assertEqual(result["phase"]["stage"], "awaiting_approval")
        self.assertEqual(
            invoke.await_args.args[0],
            {"action": "status", "workflow_id": "wf", "phase_id": "p", "client_request_id": ANY},
        )

    async def test_terminal_failure_does_not_wait_until_timeout(self):
        with patch("scripts.invoke_hypothesis_agent._invoke", new_callable=AsyncMock,
                   return_value={"phase": {"stage": "failed", "error": {"message": "model failed"}}}):
            with self.assertRaisesRegex(RuntimeError, "model failed"):
                await wait_for_phase("wf", "p", {"completed"})
