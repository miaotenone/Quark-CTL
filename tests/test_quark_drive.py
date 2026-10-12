"""离线协议与文件安全测试；绝不连接真实云盘。"""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from xml.etree import ElementTree as ET

import requests
from others.quark_drive import (
    AuthenticationError, QuarkClient, QuarkError, TaskTimeoutError, main,
)


def response(data=None, status=200, headers=None, content=b""):
    r = Mock()
    r.status_code = status
    r.headers = headers or {}
    r.cookies = requests.cookies.RequestsCookieJar()
    r.content = content
    r.json.return_value = data
    r.__enter__ = Mock(return_value=r)
    r.__exit__ = Mock(return_value=False)
    r.iter_content.return_value = iter([content])
    return r


def ok(data=None, **extra):
    return response({"status": 200, "code": 0, "data": data, **extra})


def listing(items):
    return ok({"list": items}, metadata={"_total": len(items)})


class ClientTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        default_config = patch("others.quark_drive.DEFAULT_CONFIG", Path(temporary.name) / "local.json")
        default_config.start()
        self.addCleanup(default_config.stop)
        self.client = QuarkClient("__puus=test-cookie", poll_interval=0.001, auto_refresh=False, persist_cookie=False)
        self.addCleanup(self.client.close)
        self.api = patch.object(self.client.session, "request").start()
        self.transfer = patch.object(self.client.transfer, "request").start()
        self.addCleanup(patch.stopall)
        patch("others.quark_drive.time.sleep").start()

    def test_cookie_precedence_and_config(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"QUARK_COOKIE": "a=env"}):
            config = Path(tmp) / "config.json"
            config.write_text('{"cookie":"a=file"}')
            with QuarkClient("a=argument", config_file=config) as c:
                self.assertEqual(c.session.cookies.get("a"), "argument")
            with QuarkClient(config_file=config) as c:
                self.assertEqual(c.session.cookies.get("a"), "file")
            with patch.dict(os.environ, {}, clear=True), QuarkClient(config_file=config) as c:
                self.assertEqual(c.session.cookies.get("a"), "file")
            config.write_text('invalid-secret-value')
            with patch.dict(os.environ, {}, clear=True), self.assertRaises(AuthenticationError) as error:
                QuarkClient(config_file=config)
            self.assertNotIn('invalid-secret-value', str(error.exception))

    def test_missing_cookie(self):
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(AuthenticationError):
            QuarkClient()
        self.api.assert_not_called()

    def test_pagination_uses_metadata_total(self):
        self.api.side_effect = [ok({"list": [{"fid": "1"}]}, metadata={"_total": 2}),
                                ok({"list": [{"fid": "2"}]}, metadata={"_total": 2})]
        self.assertEqual([x["fid"] for x in self.client.list_files()], ["1", "2"])
        self.assertEqual(self.api.call_args.kwargs["params"]["_page"], 2)

    def test_repeated_page_is_rejected(self):
        self.api.return_value = ok({"list": [{"fid": "1"}]}, metadata={"_total": 2})
        with self.assertRaises(QuarkError):
            self.client.list_files()
        self.assertEqual(self.api.call_count, 2)

    def test_business_error_and_authentication(self):
        for status, code, kind in [(200, 32003, QuarkError), (401, 31001, AuthenticationError)]:
            with self.subTest(status=status):
                self.api.return_value = response({"status": status, "code": code, "message": "secret-cookie"})
                with self.assertRaises(kind) as error:
                    self.client.list_files()
                self.assertNotIn("secret-cookie", str(error.exception))

    def test_invalid_json_and_missing_fields(self):
        bad_json = response()
        bad_json.json.side_effect = ValueError("raw-secret")
        for r in [bad_json, response({}), ok({})]:
            self.api.return_value = r
            with self.assertRaises(QuarkError):
                self.client.list_files()

    def test_read_retries_but_writes_do_not(self):
        self.api.side_effect = [requests.Timeout("secret"), response(status=503), listing([])]
        self.assertEqual(self.client.list_files(), [])
        self.assertEqual(self.api.call_count, 3)
        self.api.reset_mock()
        self.api.side_effect = requests.Timeout("secret")
        with self.assertRaises(QuarkError) as error:
            self.client.create_folder("folder")
        self.assertEqual(self.api.call_count, 1)
        self.assertNotIn("secret", str(error.exception))
        self.assertFalse(self.api.call_args.kwargs["allow_redirects"])

    def test_delete_uses_recycle_bin(self):
        self.api.return_value = ok({})
        self.client.delete_files(["a"])
        self.assertEqual(self.api.call_args.kwargs["json"]["action_type"], 1)
        with self.assertRaises(ValueError):
            self.client.delete_files(["0"])

    def test_file_management_payloads(self):
        self.api.return_value = ok({})
        self.client.create_folder("中文目录", "parent")
        self.assertEqual(self.api.call_args.kwargs["json"]["pdir_fid"], "parent")
        self.client.move_files(["a", "a", "b"], "target")
        self.assertEqual(self.api.call_args.kwargs["json"]["filelist"], ["a", "b"])
        self.client.rename_file("a", "中文.txt")
        self.assertEqual(self.api.call_args.kwargs["json"], {"fid": "a", "file_name": "中文.txt"})

    def test_task_waits_for_success_and_rejects_failure(self):
        self.api.side_effect = [ok({"status": 0}), ok({"status": 1}), ok({"status": 2, "share_id": "s"})]
        self.assertEqual(self.client.wait_task("task")["share_id"], "s")
        self.api.side_effect = None
        self.api.return_value = ok({"status": 3})
        with self.assertRaises(QuarkError):
            self.client.wait_task("task")

    def test_task_timeout_is_not_success(self):
        with patch("others.quark_drive.time.monotonic", side_effect=[0, 121]):
            with self.assertRaises(TaskTimeoutError):
                self.client.wait_task("pending")
        self.api.assert_not_called()

    def test_create_share_waits_before_fetching_url(self):
        self.api.side_effect = [ok({"task_id": "t"}), ok({"status": 1}),
                                ok({"status": 2, "share_id": "s"}),
                                ok({"share_url": "https://pan.quark.cn/s/abc", "passcode": "1234"})]
        self.assertEqual(self.client.create_share(["f"], password=True)["passcode"], "1234")
        self.assertEqual(self.api.call_args_list[0].kwargs["json"]["url_type"], 2)
        self.assertTrue(self.api.call_args_list[-1].args[1].endswith("/share/password"))

    def test_share_selection_and_passcode(self):
        self.api.side_effect = [ok({"stoken": "token"}), listing([
            {"fid": "a", "share_fid_token": "ta"}, {"fid": "b", "share_fid_token": "tb"}]),
            ok({"task_id": "t"}), ok({"status": 2, "save_as": {"save_as_top_fids": ["saved"]}})]
        self.client.save_share("https://pan.quark.cn/s/abc?pwd=1234", "dest", file_ids=["b"])
        self.assertEqual(self.api.call_args_list[0].kwargs["json"]["passcode"], "1234")
        save = self.api.call_args_list[2].kwargs["json"]
        self.assertEqual(save["fid_list"], ["b"])
        self.assertEqual(save["fid_token_list"], ["tb"])
        self.assertEqual(save["to_pdir_fid"], "dest")

    def test_save_all_and_reject_unknown_selection(self):
        for selection in (None, ["missing"]):
            self.api.side_effect = [ok({"stoken": "t"}), listing([{"fid": "a", "share_fid_token": "ta"}]),
                                    ok({"status": 2})]
            if selection:
                with self.assertRaises(ValueError):
                    self.client.save_share("https://pan.quark.cn/s/abc", file_ids=selection)
            else:
                self.client.save_share("https://pan.quark.cn/s/abc")
                self.assertEqual(self.api.call_args.kwargs["json"]["fid_list"], ["a"])

    def test_share_passcode_failure_never_saves(self):
        self.api.return_value = response({"status": 400, "code": 41004})
        with self.assertRaises(QuarkError):
            self.client.save_share("https://pan.quark.cn/s/abc", passcode="wrong")
        self.assertEqual(self.api.call_count, 1)

    def download_info(self, **updates):
        return ok([{"fid": "f", "file_name": "文件.txt", "size": 3,
                    "download_url": "https://dl.quark.cn/file?signature=secret", **updates}])

    def test_download_is_complete_and_cookie_is_scoped(self):
        self.api.return_value = self.download_info(md5="900150983cd24fb0d6963f7d28e17f72")
        self.transfer.return_value = response(content=b"abc")
        with tempfile.TemporaryDirectory() as tmp:
            result = self.client.download_file("f", tmp)
            self.assertEqual(Path(result["path"]).read_bytes(), b"abc")
            self.assertEqual(len(list(Path(tmp).iterdir())), 1)
        self.assertEqual(self.transfer.call_args.kwargs["headers"]["Cookie"], "__puus=test-cookie")
        self.assertFalse(self.transfer.call_args.kwargs["allow_redirects"])

    def test_download_refuses_external_cookie_destination(self):
        self.api.return_value = self.download_info(download_url="https://quark.cn.example.org/x")
        with self.assertRaises(QuarkError):
            self.client.download_file("f")
        self.transfer.assert_not_called()

    def test_download_refuses_path_escape(self):
        for name in ("../outside", "/outside", "..\\outside", "..", "C:outside"):
            self.api.return_value = self.download_info(file_name=name)
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.client.download_file("f")
        self.transfer.assert_not_called()

    def test_interrupted_or_short_download_preserves_existing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "文件.txt"
            target.write_bytes(b"old")
            self.api.return_value = self.download_info()
            with self.assertRaises(FileExistsError):
                self.client.download_file("f", tmp)
            self.transfer.assert_not_called()
            self.transfer.return_value = response(content=b"ab")
            with self.assertRaises(QuarkError):
                self.client.download_file("f", tmp, overwrite=True)
            self.assertEqual(target.read_bytes(), b"old")
            broken = response()
            broken.iter_content.side_effect = requests.ConnectionError("signed-secret-url")
            self.transfer.return_value = broken
            with self.assertRaises(QuarkError) as error:
                self.client.download_file("f", tmp, overwrite=True)
            self.assertNotIn("signed-secret", str(error.exception))
            self.assertEqual(list(Path(tmp).iterdir()), [target])

    def test_download_overwrite_and_symlink(self):
        self.api.return_value = self.download_info()
        self.transfer.return_value = response(content=b"abc")
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "文件.txt"
            target.write_bytes(b"old")
            self.client.download_file("f", tmp, overwrite=True)
            self.assertEqual(target.read_bytes(), b"abc")
            target.unlink()
            target.symlink_to(Path(tmp) / "missing")
            with self.assertRaises(FileExistsError):
                self.client.download_file("f", tmp, overwrite=True)

    def test_upload_conflict_has_no_write_request(self):
        self.api.return_value = listing([{"fid": "old", "file_name": "a"}])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a"
            path.write_bytes(b"abc")
            with self.assertRaises(FileExistsError):
                self.client.upload_file(path)
        self.assertEqual(self.api.call_count, 1)
        self.assertEqual(self.api.call_args.args[0], "GET")

    def upload_responses(self, *, instant=False):
        pre = {"task_id": "t", "bucket": "bucket", "obj_key": "key", "upload_id": "upload",
               "auth_info": "auth", "callback": {"callbackUrl": "https://drive.quark.cn/callback"},
               "upload_url": "http://oss.example.org"}
        initial = [listing([]), ok(pre, metadata={"part_size": 2}), ok({"finish": instant})]
        end = listing([{"fid": "new", "file_name": "a", "size": 3}])
        return initial + ([end] if instant else [ok({"auth_key": "a"})] * 3 + [ok({}), end])

    def test_instant_upload_still_confirms_visibility(self):
        self.api.side_effect = self.upload_responses(instant=True)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a"
            path.write_bytes(b"abc")
            result = self.client.upload_file(path)
        self.assertTrue(result["instant"])
        self.assertEqual(result["fid"], "new")
        self.transfer.assert_not_called()

    def test_multipart_upload_commits_then_finishes(self):
        self.api.side_effect = self.upload_responses()
        self.transfer.side_effect = [response(headers={"ETag": '"part1"'}),
                                    response(headers={"ETag": '"part2"'}), response(content=b"<CompleteMultipartUploadResult/>")]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a"
            path.write_bytes(b"abc")
            result = self.client.upload_file(path)
        self.assertFalse(result["instant"])
        calls = self.transfer.call_args_list
        self.assertEqual([c.args[0] for c in calls], ["PUT", "PUT", "POST"])
        self.assertEqual([c.kwargs["data"] for c in calls[:2]], [b"ab", b"c"])
        parts = ET.fromstring(calls[2].kwargs["data"]).findall("Part")
        self.assertEqual([p.findtext("ETag") for p in parts], ['"part1"', '"part2"'])
        for call in calls:
            self.assertNotIn("Cookie", call.kwargs["headers"])
        self.assertTrue(self.api.call_args_list[-2].args[1].endswith("/file/upload/finish"))
        auth_meta = self.api.call_args_list[3].kwargs["json"]["auth_meta"]
        self.assertTrue(auth_meta.startswith("PUT\n\napplication/octet-stream\n"))

    def test_failed_upload_commit_never_reports_success(self):
        self.api.side_effect = self.upload_responses()
        self.transfer.side_effect = [response(headers={"ETag": "1"}), response(headers={"ETag": "2"}),
                                    response(content=b"<Error><Code>BadDigest</Code></Error>")]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a"
            path.write_bytes(b"abc")
            with self.assertRaises(QuarkError):
                self.client.upload_file(path)
        self.assertFalse(any(c.args[1].endswith("/file/upload/finish") for c in self.api.call_args_list))

    def test_batch_continues_after_failure(self):
        with patch.object(self.client, "upload_file", side_effect=[QuarkError("failure"), {"fid": "ok"}]):
            result = self.client.upload_files(["a", "b"])
        self.assertEqual([r["ok"] for r in result], [False, True])


class CliTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        default_config = patch("others.quark_drive.DEFAULT_CONFIG", Path(temporary.name) / "local.json")
        default_config.start()
        self.addCleanup(default_config.stop)

    def test_import_has_no_side_effects(self):
        code = "from unittest.mock import patch;\nwith patch('requests.sessions.Session.request', side_effect=AssertionError('network')):\n import others.quark_drive\n"
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))

    def test_both_help_entrypoints(self):
        for args in (["-m", "others.quark_drive"], ["others/quark_drive.py"]):
            result = subprocess.run([sys.executable, *args, "--help"], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("upload", result.stdout)

    def test_json_output_and_partial_failure_exit_code(self):
        results = [{"input": "a", "ok": False, "error": "failed"}, {"input": "b", "ok": True, "data": {}}]
        with patch("others.quark_drive.QuarkClient") as factory:
            factory.return_value.__enter__.return_value.upload_files.return_value = results
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = main(["upload", "a", "b", "--json"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out.getvalue()), results)
        self.assertTrue(err.getvalue())

    def test_missing_cookie_error_goes_to_stderr(self):
        out, err = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, {}, clear=True), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["--json", "ls"])
        self.assertEqual(code, 1)
        self.assertEqual(out.getvalue(), "")
        self.assertFalse(json.loads(err.getvalue())["ok"])


if __name__ == "__main__":
    unittest.main()
