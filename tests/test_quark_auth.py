"""认证续期、凭据落盘和读写重试隔离测试；所有网络请求均被替换。"""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import requests
from others.quark_drive import AuthenticationError, QuarkClient, QuarkError, main


def response(data=None, *, status=200, code=0, cookies=None, body=b""):
    r = Mock()
    r.status_code = status
    r.headers = {}
    r.content = body
    r.json.return_value = {"status": status, "code": code, "data": data}
    r.cookies = requests.cookies.RequestsCookieJar()
    for name, value in (cookies or {}).items():
        r.cookies.set(name, value, domain="drive-pc.quark.cn")
    r.iter_content.return_value = iter([body])
    r.__enter__ = Mock(return_value=r)
    r.__exit__ = Mock(return_value=False)
    return r


def listing():
    return response({"list": []})


def refresh_responses():
    return [response({}, cookies={"__puus": "fresh"}), listing()]


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = Path(self.tmp.name) / "local.json"
        self.config.write_text(json.dumps({"cookie": "__pus=long-lived; __puus=old", "custom": "preserved"}))
        self.client = QuarkClient(config_file=self.config)
        self.addCleanup(self.client.close)
        self.api = patch.object(self.client.session, "request").start()
        self.transfer = patch.object(self.client.transfer, "request").start()
        self.addCleanup(patch.stopall)
        patch("others.quark_drive.time.sleep").start()

    def test_first_request_refreshes_verifies_and_saves(self):
        def request(method, url, **kwargs):
            if url.endswith('/config'):
                cookie = self.client._cookie_header()
                self.assertNotIn("__puus", cookie)
                self.assertIn("__pus=long-lived", cookie)
                return response({}, cookies={"__puus": "fresh"})
            return listing()
        self.api.side_effect = request
        self.client.list_files()
        self.assertEqual(self.api.call_count, 3)
        saved = json.loads(self.config.read_text())
        self.assertIn("__puus=fresh", saved["cookie"])
        self.assertNotIn("__puus=old", saved["cookie"])
        self.assertEqual(saved["custom"], "preserved")
        self.assertIn("session_refreshed_at", saved)
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(self.config.parent.iterdir()), [self.config])

    def test_refresh_runs_only_when_due(self):
        self.api.side_effect = refresh_responses() + [listing(), listing()] + refresh_responses() + [listing()]
        self.client.list_files()
        self.client.list_files()
        self.client._last_refresh -= 6001
        self.client.list_files()
        self.assertEqual(sum(c.args[1].endswith('/config') for c in self.api.call_args_list), 2)

    def test_expired_long_lived_auth_restores_original_and_does_not_save(self):
        original = self.config.read_bytes()
        self.api.side_effect = [response({}, cookies={"__puus": "new-but-anonymous"}), response(status=401)]
        with self.assertRaisesRegex(AuthenticationError, "auth-login"):
            self.client.refresh_session()
        self.assertEqual(self.config.read_bytes(), original)
        self.assertEqual(self.client.session.cookies.get("__puus"), "old")
        self.assertIsNone(self.client._last_refresh)
        self.assertEqual(self.api.call_count, 2)

    def test_malformed_account_verification_never_saves(self):
        original = self.config.read_bytes()
        self.api.side_effect = [response({}, cookies={"__puus": "fresh"}), response({})]
        with self.assertRaises(QuarkError):
            self.client.refresh_session()
        self.assertEqual(self.config.read_bytes(), original)
        self.assertEqual(self.client.session.cookies.get("__puus"), "old")

    def test_network_refresh_failure_restores_original(self):
        original = self.config.read_bytes()
        self.api.side_effect = requests.Timeout("sensitive-url")
        with self.assertRaises(QuarkError) as error:
            self.client.refresh_session()
        self.assertNotIn("sensitive", str(error.exception))
        self.assertEqual(self.client.session.cookies.get("__puus"), "old")
        self.assertEqual(self.config.read_bytes(), original)
        self.assertEqual(self.api.call_count, 3)

    def test_no_new_cookie_checks_old_login_without_claiming_refresh(self):
        self.api.side_effect = [response({}), listing()]
        result = self.client.refresh_session()
        self.assertTrue(result["authenticated"])
        self.assertFalse(result["refreshed"])
        self.assertEqual(self.client.session.cookies.get("__puus"), "old")
        self.assertNotIn("session_refreshed_at", json.loads(self.config.read_text()))

    def test_no_new_cookie_and_invalid_old_login_fails(self):
        self.api.side_effect = [response({}), response({}, status=401)]
        with self.assertRaises(AuthenticationError):
            self.client.refresh_session()
        self.assertIsNone(self.client._last_refresh)

    def test_read_auth_failure_refreshes_once_and_replays(self):
        self.client._last_refresh = time.monotonic()
        self.api.side_effect = [response({}, status=200, code=31001)] + refresh_responses() + [listing()]
        self.assertEqual(self.client.list_files(), [])
        self.assertEqual(self.api.call_count, 4)

    def test_repeated_auth_failure_does_not_loop(self):
        self.client._last_refresh = time.monotonic()
        self.api.side_effect = [response(status=401)] + refresh_responses() + [response(status=401)]
        with self.assertRaisesRegex(AuthenticationError, 'auth-login'):
            self.client.list_files()
        self.assertEqual(self.api.call_count, 4)

    def test_write_auth_failure_refreshes_but_does_not_replay(self):
        self.client._last_refresh = time.monotonic()
        self.api.side_effect = [response(status=401)] + refresh_responses()
        with self.assertRaisesRegex(AuthenticationError, "未自动重放"):
            self.client.create_folder("once")
        self.assertEqual(sum(c.args[0] == "POST" for c in self.api.call_args_list), 1)
        self.assertIn("__puus=fresh", json.loads(self.config.read_text())["cookie"])

    def test_network_write_failure_does_not_refresh_or_repeat(self):
        self.client._last_refresh = time.monotonic()
        self.api.side_effect = requests.Timeout()
        with self.assertRaises(QuarkError):
            self.client.delete_files(["file"])
        self.assertEqual(self.api.call_count, 1)

    def test_permission_error_does_not_trigger_auth_refresh(self):
        self.client._last_refresh = time.monotonic()
        self.api.return_value = response(status=403)
        with self.assertRaises(QuarkError):
            self.client.list_files()
        self.assertEqual(self.api.call_count, 1)

    def test_scope_duplicates_replaced_and_cookie_rotation_saved(self):
        self.client._last_refresh = time.monotonic()
        self.client.session.cookies.set('__puus', 'duplicate', domain='drive-pc.quark.cn')
        self.api.return_value = response({"list": []}, cookies={"__puus": "rotated", "__pus": "renewed"})
        self.client.list_files()
        self.assertEqual(self.client._cookie_header().count('__puus='), 1)
        self.assertIn('__puus=rotated', self.client._cookie_header())
        self.assertIn('__pus=renewed', json.loads(self.config.read_text())["cookie"])

    def test_cookie_save_failure_preserves_file_and_remote_success(self):
        before = self.config.read_bytes()
        self.client._last_refresh = time.monotonic()
        self.api.return_value = response({"fid": "created"}, cookies={"__puus": "rotated"})
        with patch('others.quark_drive.os.replace', side_effect=OSError('sensitive')):
            self.assertEqual(self.client.create_folder('created'), {"fid": "created"})
        self.assertEqual(self.config.read_bytes(), before)
        self.assertEqual(list(self.config.parent.iterdir()), [self.config])
        self.assertIsNotNone(self.client.credential_save_error)
        self.assertNotIn('sensitive', self.client.credential_save_error)

    def test_modified_config_not_overwritten(self):
        self.config.write_text('{"cookie":"other-account=secret"}')
        self.api.side_effect = refresh_responses()
        result = self.client.refresh_session()
        self.assertFalse(result['persisted'])
        self.assertTrue(result['warning'])
        self.assertIn('other-account', self.config.read_text())

    def test_persistence_can_be_disabled(self):
        self.client.persist_cookie = False
        original = self.config.read_bytes()
        self.api.side_effect = refresh_responses()
        self.assertFalse(self.client.refresh_session()['persisted'])
        self.assertEqual(self.config.read_bytes(), original)
        self.assertIsNone(self.client.credential_save_error)

    def test_config_preferred_over_stale_environment_and_loaded_by_new_process(self):
        self.api.side_effect = refresh_responses()
        self.client.refresh_session()
        with patch.dict(os.environ, {'QUARK_COOKIE': '__puus=stale'}), patch('others.quark_drive.DEFAULT_CONFIG', self.config):
            with QuarkClient() as c:
                self.assertEqual(c.session.cookies.get('__puus'), 'fresh')
            with QuarkClient(cookie='__puus=explicit') as c:
                self.assertEqual(c.session.cookies.get('__puus'), 'explicit')

    def test_environment_bootstrap_creates_default_file_only_after_verification(self):
        self.config.unlink()
        with patch('others.quark_drive.DEFAULT_CONFIG', self.config), patch.dict(os.environ, {'QUARK_COOKIE': '__pus=long-lived'}):
            with QuarkClient() as c, patch.object(c.session, 'request', side_effect=refresh_responses()):
                self.assertFalse(self.config.exists())
                result = c.refresh_session()
                self.assertTrue(result['persisted'])
                self.assertIn('__puus=fresh', self.config.read_text())

    def test_download_uses_cookie_snapshot_after_refresh_before_response_rotation(self):
        ticket = response([{"fid": "f", "file_name": "a", "size": 3,
                            "download_url": "https://dl.quark.cn/file"}], cookies={'__puus': 'next'})
        self.api.side_effect = refresh_responses() + [ticket]
        self.transfer.return_value = response(body=b'abc')
        self.client.download_file('f', self.config.parent / 'downloads')
        self.assertIn('__puus=fresh', self.api.call_args.kwargs['headers']['Cookie'])
        self.assertIn('__puus=fresh', self.transfer.call_args.kwargs['headers']['Cookie'])
        self.assertNotIn('__puus=next', self.transfer.call_args.kwargs['headers']['Cookie'])
        self.assertIn('__puus=next', self.config.read_text())

    def test_download_403_refreshes_and_gets_new_ticket_once(self):
        self.client._last_refresh = time.monotonic()
        def ticket(url):
            return response([{"fid": "f", "file_name": "a", "size": 3, "download_url": url}])
        self.api.side_effect = [ticket('https://dl.quark.cn/old')] + refresh_responses() + [ticket('https://dl.quark.cn/new')]
        self.transfer.side_effect = [response(status=403), response(body=b'abc')]
        self.client.download_file('f', self.config.parent / 'downloads')
        self.assertEqual(self.transfer.call_args.args[1], 'https://dl.quark.cn/new')
        self.assertIn('__puus=fresh', self.transfer.call_args.kwargs['headers']['Cookie'])

    def test_download_repeated_403_stops(self):
        self.client._last_refresh = time.monotonic()
        ticket = response([{"fid": "f", "file_name": "a", "download_url": "https://dl.quark.cn/file"}])
        self.api.side_effect = [ticket] + refresh_responses() + [ticket]
        self.transfer.return_value = response(status=403)
        with self.assertRaises(QuarkError):
            self.client.download_file('f', self.config.parent / 'downloads')
        self.assertEqual(self.transfer.call_count, 2)

    def test_auth_login_uses_hidden_input_and_returns_no_cookie(self):
        out, err = io.StringIO(), io.StringIO()
        with patch('others.quark_drive.sys.stdin.isatty', return_value=True), \
             patch('others.quark_drive.getpass.getpass', return_value='__pus=private'), \
             patch('requests.sessions.Session.request', side_effect=refresh_responses()), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(['--config', str(self.config), 'auth-login', '--json'])
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out.getvalue())['persisted'])
        self.assertNotIn('private', out.getvalue() + err.getvalue())
        self.assertNotIn('__puus', out.getvalue() + err.getvalue())

    def test_auth_keepalive_refreshes_then_stops_on_login_failure(self):
        out, err = io.StringIO(), io.StringIO()
        with patch('requests.sessions.Session.request', side_effect=refresh_responses() + [response(status=401)]), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(['--config', str(self.config), 'auth-keepalive', '--interval', '1', '--json'])
        self.assertEqual(code, 1)
        self.assertEqual(len(out.getvalue().splitlines()), 1)
        self.assertIn('auth-login', err.getvalue())


if __name__ == '__main__':
    unittest.main()
