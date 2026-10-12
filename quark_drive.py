#!/usr/bin/env python3
"""夸克云盘客户端；支持 import 和命令行，详见同目录 README.md。"""
from __future__ import annotations

import argparse
import base64
import hashlib
import getpass
import json
import mimetypes
import os
import re
import sys
import tempfile
import time
from email.utils import formatdate
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import parse_qs, quote, urlparse
from xml.etree import ElementTree as ET

import requests

API = "https://drive-pc.quark.cn/1/clouddrive"
REFERER = "https://pan.quark.cn/"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
OSS_AGENT = "aliyun-sdk-js/6.6.1"
DEFAULT_CONFIG = Path(__file__).resolve().with_name("quark.local.json")
REFRESH_INTERVAL = 100 * 60
RELOGIN_MESSAGE = "长期登录凭据已失效，请运行 auth-login 在本机重新录入浏览器 Cookie"
READ_ONLY_POSTS = {"/file/download", "/share/sharepage/token", "/share/password", "/file/upload/auth"}


class QuarkError(Exception):
    """网络、协议、业务或任务失败；不会包含 Cookie 和签名下载地址。"""


class AuthenticationError(QuarkError):
    """Cookie 缺失或已失效。"""


class HttpError(QuarkError):
    """保留 HTTP 状态码，错误文本不包含请求地址或凭据。"""

    def __init__(self, status: int):
        self.status = status
        super().__init__(f"HTTP 请求失败（{status}），请检查权限、网络或接口变化")


class TaskTimeoutError(QuarkError):
    """任务结果尚未确认；不要直接重复提交写操作。"""


def _name(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or value in {".", ".."} or any(
        c in value for c in '/\\\x00\r\n:'
    ):
        raise ValueError("文件名必须是单个有效名称，不能包含路径分隔符")
    return value


def _ids(values: Iterable[str]) -> list[str]:
    if isinstance(values, (str, bytes)):
        raise ValueError("文件 ID 请使用列表，例如 ['file-id']")
    result = list(dict.fromkeys(str(v) for v in values))
    if not result or any(not v.strip() or v == "0" for v in result):
        raise ValueError("请提供非空文件 ID 列表，不能操作根目录本身")
    return result


class QuarkClient:
    """同步客户端；默认自动刷新并保存到本地配置，一个配置文件只用一个写入进程。"""

    def __init__(self, cookie: str | None = None, *, config_file: str | Path | None = None,
                 timeout: float = 30, task_timeout: float = 120, poll_interval: float = 1,
                 auto_refresh: bool = True, persist_cookie: bool = True,
                 refresh_interval: float = REFRESH_INTERVAL):
        if min(timeout, task_timeout, poll_interval, refresh_interval) <= 0:
            raise ValueError("超时、轮询及刷新间隔必须大于零")
        self.config_file = Path(config_file).expanduser().absolute() if config_file is not None else DEFAULT_CONFIG
        self.persist_cookie = persist_cookie
        self.auto_refresh = auto_refresh
        self.refresh_interval = refresh_interval
        self._last_refresh: float | None = None
        self._refreshed_at: float | None = None
        self.credential_save_error: str | None = None
        self._config_contents: bytes | None = None
        self._config_data: dict = {}
        # 已保存的新会话优先于遗留环境变量；显式 cookie 可用于更换账号。
        try:
            if self.config_file.exists():
                self._config_contents = self.config_file.read_bytes()
                self._config_data = json.loads(self._config_contents)
                if not isinstance(self._config_data, dict):
                    raise ValueError()
            elif config_file is not None and cookie is None:
                raise FileNotFoundError()
        except (OSError, ValueError):
            raise AuthenticationError("无法读取 Cookie 配置，应为 JSON 对象；请检查路径或修复文件") from None
        selected = cookie
        if selected is None:
            selected = self._config_data.get("cookie")
        if selected is None and config_file is None:
            selected = os.environ.get("QUARK_COOKIE")
        if not isinstance(selected, str) or not selected.strip():
            raise AuthenticationError("请运行 auth-login 录入 Cookie，或配置 QUARK_COOKIE")
        pairs = [item.strip().split("=", 1) for item in selected.split(";") if item.strip()]
        if not pairs or any(len(pair) != 2 or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", pair[0])
                            or any(ord(c) < 32 or ord(c) > 126 for c in pair[1]) for pair in pairs):
            raise AuthenticationError("Cookie 格式无效，应为 name=value; name=value")
        self.timeout, self.task_timeout, self.poll_interval = timeout, task_timeout, poll_interval
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "Referer": REFERER,
                                     "Origin": REFERER.rstrip("/"), "Accept": "application/json"})
        for key, value in pairs:
            self.session.cookies.set(key, value, domain=".quark.cn", secure=True)
        # 对象存储请求必须与携带登录会话的 API 请求分离。
        self.transfer = requests.Session()

    def close(self) -> None:
        self.session.close()
        self.transfer.close()

    def __enter__(self) -> QuarkClient:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def _cookie_header(self) -> str:
        prepared = self.session.prepare_request(requests.Request("GET", API + "/config"))
        return prepared.headers.get("Cookie", "")

    def _remove_cookie(self, name: str) -> None:
        for cookie in list(self.session.cookies):
            if cookie.name == name:
                self.session.cookies.clear(cookie.domain, cookie.path, cookie.name)

    def _merge_cookies(self, response: requests.Response) -> None:
        # requests 会自动合并响应 Cookie；这里统一作用域，避免旧 __puus 与新值同时发送。
        for cookie in response.cookies:
            domain = cookie.domain.lstrip(".")
            if domain and domain not in {"quark.cn", "drive-pc.quark.cn"}:
                continue
            self._remove_cookie(cookie.name)
            if cookie.value and not cookie.is_expired():
                self.session.cookies.set(cookie.name, cookie.value, domain=".quark.cn", secure=True)

    def _save_cookie(self) -> bool:
        if not self.persist_cookie:
            return False
        temporary: Path | None = None
        try:
            # 乐观冲突检测防止覆盖另一实例已更新的配置；仍要求单写入进程。
            current = self.config_file.read_bytes() if self.config_file.exists() else None
            if self.config_file.is_symlink() or current != self._config_contents:
                raise OSError("config changed")
            updated = {**self._config_data, "cookie": self._cookie_header(),
                       "cookie_updated_at": time.time()}
            if self._refreshed_at is not None:
                updated["session_refreshed_at"] = self._refreshed_at
            encoded = (json.dumps(updated, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
            self.config_file.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=self.config_file.parent, prefix=".quark-cookie-",
                                             suffix=".tmp", delete=False) as target:
                temporary = Path(target.name)
                os.chmod(temporary, 0o600)
                target.write(encoded)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, self.config_file)
            self._config_contents, self._config_data = encoded, updated
            self.credential_save_error = None
            return True
        except OSError:
            # 保存错误不能将已完成的远程写操作伪装成失败并诱发重复提交。
            self.credential_save_error = "Cookie 未保存：配置不可写、为符号链接或已被其他进程修改；当前会话仍可使用"
            return False
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    self.credential_save_error = "Cookie 临时文件清理失败，请检查配置目录权限"

    def _ensure_session(self) -> None:
        if self.auto_refresh and (self._last_refresh is None or
                                  time.monotonic() - self._last_refresh >= self.refresh_interval):
            self.refresh_session()

    def refresh_session(self) -> dict:
        """移除短期 __puus 后请求新值，验证登录，再原子保存；失败保留原凭据。"""
        original = self.session.cookies.copy()
        self._remove_cookie("__puus")
        try:
            self._request_once("GET", "/config", persist=False)
            refreshed = any(c.name == "__puus" and c.value and not c.is_expired() for c in self.session.cookies)
            if not refreshed:
                self.session.cookies = original.copy()
            # /config 可能是匿名可访问的配置接口，必须再验证私人目录访问权限。
            account = self._data(self._request_once(
                "GET", "/file/sort", params={"pdir_fid": "0", "_page": 1, "_size": 1}, persist=False))
            if not isinstance(account, dict) or not isinstance(account.get("list"), list):
                raise QuarkError("登录验证未返回私人目录列表，未保存新凭据")
        except AuthenticationError:
            self.session.cookies = original
            raise AuthenticationError(RELOGIN_MESSAGE) from None
        except BaseException:
            self.session.cookies = original
            raise
        self._last_refresh = time.monotonic()
        if refreshed:
            self._refreshed_at = time.time()
        else:
            # 服务端没有更新会话时，不谎报续期成功；仍可使用验证有效的原会话。
            self._last_refresh -= max(0, self.refresh_interval - 300)
        saved = self._save_cookie()
        return {"authenticated": True, "refreshed": refreshed, "persisted": saved,
                "message": "会话已刷新" if refreshed else "服务端未下发新会话；原登录仍有效，将稍后重试",
                "config_file": str(self.config_file) if self.persist_cookie else None,
                "warning": self.credential_save_error}

    def auth_status(self) -> dict:
        self._request("GET", "/file/sort", params={"pdir_fid": "0", "_page": 1, "_size": 1})
        return {"authenticated": True, "auto_refresh": self.auto_refresh,
                "refresh_interval": self.refresh_interval,
                "config_file": str(self.config_file) if self.persist_cookie else None,
                "warning": self.credential_save_error}

    def _http(self, session: requests.Session, method: str, url: str, *, retry: bool = False,
              **kwargs: Any) -> requests.Response:
        for attempt in range(3 if retry else 1):
            try:
                response = session.request(method, url, timeout=self.timeout,
                                           allow_redirects=False, **kwargs)
            except requests.RequestException:
                if retry and attempt < 2:
                    time.sleep(0.5 * (2 ** attempt))
                    continue
                raise QuarkError("网络请求失败或超时；写操作结果可能未确认，请先检查云盘") from None
            if response.status_code in {429, 500, 502, 503, 504} and retry and attempt < 2:
                response.close()
                time.sleep(0.5 * (2 ** attempt))
                continue
            if not 200 <= response.status_code < 300:
                status = response.status_code
                response.close()
                if status == 401:
                    raise AuthenticationError("登录已失效，请更新 Cookie")
                raise HttpError(status)
            return response
        raise QuarkError("请求重试失败")

    def _request(self, method: str, path: str, *, params: dict | None = None,
                 data: dict | None = None, headers: dict | None = None) -> dict:
        self._ensure_session()
        try:
            return self._request_once(method, path, params=params, data=data, headers=headers)
        except AuthenticationError:
            if not self.auto_refresh:
                raise
        self.refresh_session()
        if method != "GET" and path not in READ_ONLY_POSTS:
            raise AuthenticationError("会话已刷新；原写操作未自动重放，请确认远程结果后重新执行")
        # 此处不递归；刷新后再失败即返回重新登录提示。
        try:
            return self._request_once(method, path, params=params, data=data, headers=headers)
        except AuthenticationError:
            raise AuthenticationError(RELOGIN_MESSAGE) from None

    def _request_once(self, method: str, path: str, *, params: dict | None = None,
                      data: dict | None = None, headers: dict | None = None, persist: bool = True) -> dict:
        query = {"pr": "ucpro", "fr": "pc", **(params or {})}
        before = self._cookie_header()
        with self._http(self.session, method, API + path, retry=method == "GET",
                        params=query, json=data, headers=headers) as response:
            try:
                payload = response.json()
            except ValueError:
                raise QuarkError("接口未返回 JSON，可能需要重新登录或适配接口") from None
            if not isinstance(payload, dict) or "status" not in payload:
                raise QuarkError("接口响应缺少 status 字段")
            status, code = str(payload["status"]), str(payload.get("code", 0))
            if status == "401" or code in {"31001", "31002", "31003"}:
                raise AuthenticationError("登录已失效，请更新 Cookie")
            if status != "200" or code != "0":
                raise QuarkError(f"接口 {path} 失败（status={status}, code={code}）")
            self._merge_cookies(response)
        if persist and before != self._cookie_header():
            self._save_cookie()
        return payload

    @staticmethod
    def _data(payload: dict) -> Any:
        if "data" not in payload:
            raise QuarkError("接口响应缺少 data 字段")
        return payload["data"]

    def _pages(self, path: str, params: dict) -> list[dict]:
        result, seen = [], set()
        for page in range(1, 10001):
            payload = self._request("GET", path, params={**params, "_page": page,
                                    "_size": 100, "_fetch_total": 1})
            data = self._data(payload)
            if not isinstance(data, dict) or not isinstance(data.get("list"), list):
                raise QuarkError("目录响应缺少 list")
            items = data["list"]
            for item in items:
                if not isinstance(item, dict) or not item.get("fid"):
                    raise QuarkError("目录条目缺少文件 ID")
                if item["fid"] in seen:
                    raise QuarkError("目录分页重复，目录可能在读取期间发生变化，请重试")
                seen.add(item["fid"])
                result.append(item)
            total = payload.get("metadata", {}).get("_total", data.get("total"))
            if not items or (total is not None and len(result) >= int(total)):
                return result
            if total is None and len(items) < 100:
                return result
        raise QuarkError("目录分页超过限制")

    def list_files(self, folder_id: str = "0") -> list[dict]:
        return self._pages("/file/sort", {"pdir_fid": str(folder_id),
                           "_sort": "file_type:asc,file_name:asc", "fetch_all_file": 1})

    def search_files(self, keyword: str) -> list[dict]:
        if not keyword.strip():
            raise ValueError("搜索关键词不能为空")
        return self._pages("/file/search", {"q": keyword, "_sort": "file_name:asc"})

    def wait_task(self, task_id: str) -> dict:
        deadline, index = time.monotonic() + self.task_timeout, 0
        while time.monotonic() < deadline:
            data = self._data(self._request("GET", "/task", params={
                "task_id": task_id, "retry_index": index}))
            if not isinstance(data, dict) or "status" not in data:
                raise QuarkError("任务响应缺少状态")
            status = str(data["status"])
            if status == "2":
                return data
            if status not in {"0", "1"}:
                raise QuarkError(f"任务失败或状态不支持（status={status}）")
            index += 1
            time.sleep(min(self.poll_interval, max(0, deadline - time.monotonic())))
        raise TaskTimeoutError(f"任务 {task_id} 等待超时，可调用 wait_task 查询，勿直接重复提交")

    def _mutation(self, path: str, data: dict) -> dict:
        result = self._data(self._request("POST", path, data=data))
        if result is None:
            return {}
        if not isinstance(result, dict):
            raise QuarkError("写操作返回格式异常")
        return self.wait_task(str(result["task_id"])) if result.get("task_id") else result

    def create_folder(self, folder_name: str, parent_id: str = "0") -> dict:
        return self._mutation("/file", {"file_name": _name(folder_name), "pdir_fid": str(parent_id),
                                       "dir_init_lock": False, "dir_path": ""})

    def move_files(self, file_ids: Iterable[str], target_folder_id: str) -> dict:
        return self._mutation("/file/move", {"filelist": _ids(file_ids), "action_type": 1,
                              "exclude_fids": [], "to_pdir_fid": str(target_folder_id)})

    def rename_file(self, file_id: str, new_name: str) -> dict:
        return self._mutation("/file/rename", {"fid": _ids([file_id])[0], "file_name": _name(new_name)})

    def delete_files(self, file_ids: Iterable[str]) -> dict:
        """移入回收站，不提供永久删除。"""
        return self._mutation("/file/delete", {"filelist": _ids(file_ids),
                                               "action_type": 1, "exclude_fids": []})

    def create_share(self, file_ids: Iterable[str], title: str = "文件分享", *,
                     expires: str = "7d", password: bool = False) -> dict:
        expiry = {"forever": 1, "7d": 2, "1d": 3, "30d": 4}
        if expires not in expiry:
            raise ValueError("分享有效期应为 forever、1d、7d 或 30d")
        result = self._mutation("/share", {"fid_list": _ids(file_ids), "title": title,
                               "url_type": 2 if password else 1, "expired_type": expiry[expires]})
        if not result.get("share_id"):
            raise QuarkError("分享任务未返回 share_id")
        info = self._data(self._request("POST", "/share/password", data={"share_id": result["share_id"]}))
        if not isinstance(info, dict) or not info.get("share_url"):
            raise QuarkError("分享任务未返回分享链接")
        return info

    @staticmethod
    def _share_link(share_url: str, passcode: str) -> tuple[str, str]:
        parsed = urlparse(share_url.strip())
        match = re.fullmatch(r"/s/([A-Za-z0-9_-]+)/?", parsed.path)
        if parsed.scheme != "https" or parsed.hostname != "pan.quark.cn" or not match:
            raise ValueError("请提供 https://pan.quark.cn/s/... 格式的分享链接")
        return match[1], passcode or parse_qs(parsed.query).get("pwd", [""])[0]

    def _share_items(self, share_url: str, passcode: str, folder_id: str) -> tuple[str, str, list[dict]]:
        share_id, passcode = self._share_link(share_url, passcode)
        token = self._data(self._request("POST", "/share/sharepage/token",
                          data={"pwd_id": share_id, "passcode": passcode}))
        if not isinstance(token, dict) or not token.get("stoken"):
            raise QuarkError("分享不可访问或提取码错误")
        items = self._pages("/share/sharepage/detail", {"pwd_id": share_id, "stoken": token["stoken"],
                            "pdir_fid": str(folder_id), "force": 0, "_sort": "file_type:asc,file_name:asc"})
        return share_id, token["stoken"], items

    def list_share_files(self, share_url: str, passcode: str = "", folder_id: str = "0") -> list[dict]:
        return self._share_items(share_url, passcode, folder_id)[2]

    def save_share(self, share_url: str, parent_id: str = "0", *, passcode: str = "",
                   file_ids: Iterable[str] | None = None, share_folder_id: str = "0") -> dict:
        share_id, token, items = self._share_items(share_url, passcode, share_folder_id)
        selected = items
        if file_ids is not None:
            wanted = set(_ids(file_ids))
            selected = [item for item in items if str(item["fid"]) in wanted]
            if len(selected) != len(wanted):
                raise ValueError("指定文件 ID 不在当前分享目录中")
        if not selected:
            raise ValueError("分享目录为空，没有可转存的文件")
        if any(not item.get("share_fid_token") for item in selected):
            raise QuarkError("分享条目缺少转存令牌")
        return self._mutation("/share/sharepage/save", {
            "fid_list": [item["fid"] for item in selected],
            "fid_token_list": [item["share_fid_token"] for item in selected],
            "to_pdir_fid": str(parent_id), "pwd_id": share_id, "stoken": token,
            "pdir_fid": str(share_folder_id), "scene": "link"})

    def download_file(self, file_id: str, output_dir: str | Path = ".", *,
                      filename: str | None = None, overwrite: bool = False) -> dict:
        for attempt in range(2):
            try:
                return self._download_file_once(file_id, output_dir, filename=filename, overwrite=overwrite)
            except HttpError as exc:
                # CDN 的 403 可能源于短期会话/签名过期；只更新链接再下载一次。
                if exc.status != 403 or attempt or not self.auto_refresh:
                    raise
            except AuthenticationError:
                if attempt or not self.auto_refresh:
                    raise
            self.refresh_session()
        raise AuthenticationError(RELOGIN_MESSAGE)

    def _download_file_once(self, file_id: str, output_dir: str | Path, *,
                            filename: str | None, overwrite: bool) -> dict:
        self._ensure_session()
        # 必须在刷新之后截取签名使用的 Cookie，响应里的新值不能替换此快照。
        cookie = self._cookie_header()
        data = self._data(self._request_once("POST", "/file/download", data={"fids": _ids([file_id])},
                                            headers={"Cookie": cookie}))
        if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
            raise QuarkError("未获得文件下载信息")
        info = data[0]
        url = info.get("download_url", "")
        parsed = urlparse(url)
        host = parsed.hostname or ""
        if parsed.scheme != "https" or not any(host == d or host.endswith('.' + d)
                                                for d in ("quark.cn", "quark.com", "uc.cn", "ucweb.com")):
            raise QuarkError("下载地址不是受支持的夸克 HTTPS 域名，未发送 Cookie")
        name = _name(filename if filename is not None else info.get("file_name", str(file_id)))
        directory = Path(output_dir).expanduser().resolve()
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / name
        if target.is_symlink() or (target.exists() and not overwrite):
            raise FileExistsError(f"本地文件已存在：{target}")
        temporary: Path | None = None
        try:
            with self._http(self.transfer, "GET", url, retry=True, stream=True, headers={
                "Cookie": cookie, "User-Agent": USER_AGENT, "Referer": REFERER,
                "Accept-Encoding": "identity",
            }) as response:
                digest, count = hashlib.md5(), 0
                with tempfile.NamedTemporaryFile(dir=directory, prefix=".quark-", suffix=".part", delete=False) as output:
                    temporary = Path(output.name)
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        output.write(chunk)
                        digest.update(chunk)
                        count += len(chunk)
                expected = info.get("size", response.headers.get("Content-Length"))
                if expected is not None and count != int(expected):
                    raise QuarkError("下载长度不符，未发布不完整文件")
                if info.get("md5") and digest.hexdigest().lower() != str(info["md5"]).lower():
                    raise QuarkError("下载 MD5 校验失败")
            if overwrite:
                os.replace(temporary, target)
            else:
                # 原子创建且不覆盖，避免检查之后出现的同名文件被覆盖。
                os.link(temporary, target)
            return {"fid": str(file_id), "path": str(target), "size": count, "md5": digest.hexdigest()}
        except requests.RequestException:
            raise QuarkError("下载中断，未发布不完整文件；可重新下载") from None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _oss_request(self, info: dict, method: str, mime: str, body: bytes,
                     query: str, *, callback: bool = False) -> requests.Response:
        stamp = formatdate(usegmt=True)
        headers = {"Content-Type": mime, "Referer": REFERER,
                   "x-oss-date": stamp, "x-oss-user-agent": OSS_AGENT}
        content_md5 = ""
        if callback:
            content_md5 = base64.b64encode(hashlib.md5(body).digest()).decode()
            headers["Content-MD5"] = content_md5
            headers["x-oss-callback"] = base64.b64encode(json.dumps(info["callback"],
                separators=(",", ":")).encode()).decode()
        canonical = "".join(f"{key}:{headers[key]}\n" for key in sorted(headers) if key.startswith("x-oss-"))
        auth_meta = f"{method}\n{content_md5}\n{mime}\n{stamp}\n{canonical}/{info['bucket']}/{info['obj_key']}?{query}"
        auth = self._data(self._request("POST", "/file/upload/auth", data={
            "auth_info": info["auth_info"], "auth_meta": auth_meta, "task_id": info["task_id"]}))
        if not isinstance(auth, dict) or not auth.get("auth_key"):
            raise QuarkError("上传授权响应不完整")
        headers["Authorization"] = auth["auth_key"]
        endpoint = urlparse(info["upload_url"])
        if endpoint.scheme not in {"http", "https"} or not endpoint.hostname or endpoint.path not in {"", "/"}:
            raise QuarkError("对象存储地址格式不支持")
        bucket = str(info["bucket"])
        if not re.fullmatch(r"[a-z0-9-]+", bucket):
            raise QuarkError("对象存储 bucket 无效")
        host = endpoint.hostname
        if not host.startswith(bucket + "."):
            host = bucket + "." + host
        url = f"https://{host}/{quote(info['obj_key'], safe='/')}?{query}"
        return self._http(self.transfer, method, url, retry=method == "PUT", headers=headers, data=body)

    def upload_file(self, local_path: str | Path, parent_id: str = "0", *,
                    remote_name: str | None = None) -> dict:
        """上传一个文件；同名拒绝，不自动删除或覆盖云盘原文件。"""
        path = Path(local_path).expanduser()
        if not path.is_file():
            raise FileNotFoundError(f"本地文件不存在或不是普通文件：{path}")
        name = _name(remote_name if remote_name is not None else path.name)
        if any(item.get("file_name") == name for item in self.list_files(parent_id)):
            raise FileExistsError(f"云盘目录已有同名文件：{name}")
        before = path.stat()
        md5, sha1 = hashlib.md5(), hashlib.sha1()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                md5.update(chunk)
                sha1.update(chunk)
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        pre = self._request("POST", "/file/upload/pre", data={
            "ccp_hash_update": True, "dir_name": "", "file_name": name, "format_type": mime,
            "l_created_at": int(before.st_ctime * 1000), "l_updated_at": int(before.st_mtime * 1000),
            "pdir_fid": str(parent_id), "size": before.st_size})
        info = self._data(pre)
        if not isinstance(info, dict) or not info.get("task_id"):
            raise QuarkError("上传预处理未返回 task_id")
        hashed = self._data(self._request("POST", "/file/update/hash", data={
            "md5": md5.hexdigest(), "sha1": sha1.hexdigest(), "task_id": info["task_id"]}))
        if not isinstance(hashed, dict):
            raise QuarkError("上传哈希响应格式异常")
        instant = hashed.get("finish") is True
        if not instant:
            if any(key not in info for key in ("bucket", "obj_key", "upload_id", "auth_info", "callback", "upload_url")):
                raise QuarkError("上传预处理缺少分片信息")
            part_size = int(pre.get("metadata", {}).get("part_size", 4 * 1024 * 1024))
            if not 0 < part_size <= 64 * 1024 * 1024:
                raise QuarkError("分片大小不支持（须在 1 字节至 64 MiB 之间）")
            root = ET.Element("CompleteMultipartUpload")
            with path.open("rb") as source:
                number = 0
                while True:
                    block = source.read(part_size)
                    if not block and (number or before.st_size):
                        break
                    number += 1
                    query = f"partNumber={number}&uploadId={quote(str(info['upload_id']), safe='')}"
                    with self._oss_request(info, "PUT", mime, block, query) as response:
                        etag = response.headers.get("ETag")
                        if not etag:
                            raise QuarkError("上传分片未返回 ETag")
                    part = ET.SubElement(root, "Part")
                    ET.SubElement(part, "PartNumber").text = str(number)
                    ET.SubElement(part, "ETag").text = etag
                    if not block:
                        break
            after = path.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise QuarkError("本地文件在上传期间变化，未合并分片")
            xml = ET.tostring(root, encoding="utf-8", xml_declaration=True)
            with self._oss_request(info, "POST", "application/xml", xml,
                                   f"uploadId={quote(str(info['upload_id']), safe='')}", callback=True) as response:
                # OSS 可能在 HTTP 200 中返回 XML 错误，不能仅检查状态码。
                if response.content.lstrip().startswith(b"<"):
                    try:
                        result_xml = ET.fromstring(response.content)
                    except ET.ParseError:
                        raise QuarkError("分片合并响应 XML 无效") from None
                    if result_xml.tag.rsplit("}", 1)[-1] == "Error":
                        raise QuarkError("对象存储分片合并失败")
                elif response.content:
                    try:
                        callback_result = response.json()
                    except ValueError:
                        raise QuarkError("分片合并响应格式异常") from None
                    if isinstance(callback_result, dict) and (
                        str(callback_result.get("code", 0)) != "0" or
                        str(callback_result.get("status", 200)) != "200"
                    ):
                        raise QuarkError("分片合并回调失败")
            self._request("POST", "/file/upload/finish", data={"obj_key": info["obj_key"], "task_id": info["task_id"]})
        # 无论秒传还是分片上传，核对目标目录中的名称、大小和可用哈希。
        deadline = time.monotonic() + self.task_timeout
        while time.monotonic() < deadline:
            for item in self.list_files(parent_id):
                if item.get("file_name") == name and int(item.get("size", -1)) == before.st_size:
                    if item.get("md5") and str(item["md5"]).lower() != md5.hexdigest():
                        raise QuarkError("上传后文件 MD5 不一致")
                    return {"fid": item["fid"], "file_name": name, "size": before.st_size,
                            "md5": md5.hexdigest(), "instant": instant}
            time.sleep(min(self.poll_interval, max(0, deadline - time.monotonic())))
        raise TaskTimeoutError("上传已提交，但未确认文件可见；请检查目标目录后再决定是否重试")

    @staticmethod
    def _batch(values: Iterable[str | Path], operation: Callable) -> list[dict]:
        if isinstance(values, (str, bytes, Path)):
            raise ValueError("批量操作请传入列表")
        results = []
        for value in values:
            try:
                results.append({"input": str(value), "ok": True, "data": operation(value)})
            except (QuarkError, OSError, ValueError) as exc:
                results.append({"input": str(value), "ok": False, "error": str(exc)})
        if not results:
            raise ValueError("批量操作列表不能为空")
        return results

    def upload_files(self, local_paths: Iterable[str | Path], parent_id: str = "0") -> list[dict]:
        return self._batch(local_paths, lambda path: self.upload_file(path, parent_id))

    def download_files(self, file_ids: Iterable[str], output_dir: str | Path = ".", *,
                       overwrite: bool = False) -> list[dict]:
        return self._batch(file_ids, lambda fid: self.download_file(fid, output_dir, overwrite=overwrite))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="夸克云盘文件管理工具（Cookie 登录）")
    parser.add_argument("--config", help="包含 cookie 字段的本地 JSON 配置")
    parser.add_argument("--timeout", type=float, default=30, help="每次网络请求超时秒数")
    parser.add_argument("--task-timeout", type=float, default=120, help="异步任务等待秒数")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    parser.add_argument("--no-auto-refresh", action="store_true", help="禁用请求前刷新及失效恢复")
    parser.add_argument("--no-save-cookie", action="store_true", help="仅在内存中更新 Cookie")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("ls", "search", "mkdir", "move", "rename", "delete", "upload", "download", "share", "save", "share-ls", "task", "auth-login", "auth-refresh", "auth-status", "auth-keepalive"):
        p = commands.add_parser(name)
        p.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
        if name == "auth-keepalive":
            p.add_argument("--interval", type=float, default=REFRESH_INTERVAL, help="续期间隔秒数，默认 6000")
        elif name == "ls":
            p.add_argument("--folder-id", default="0")
        elif name == "search":
            p.add_argument("keyword")
        elif name == "mkdir":
            p.add_argument("name")
            p.add_argument("--parent-id", default="0")
        elif name in {"move", "delete", "download", "share"}:
            p.add_argument("file_ids", nargs="+")
            if name == "move":
                p.add_argument("--parent-id", required=True)
            elif name == "download":
                p.add_argument("--output-dir", default=".")
                p.add_argument("--overwrite", action="store_true")
            elif name == "share":
                p.add_argument("--title", default="文件分享")
                p.add_argument("--expires", choices=["forever", "1d", "7d", "30d"], default="7d")
                p.add_argument("--password", action="store_true", help="由服务器生成提取码")
        elif name == "rename":
            p.add_argument("file_id")
            p.add_argument("name")
        elif name == "upload":
            p.add_argument("paths", nargs="+")
            p.add_argument("--parent-id", default="0")
        elif name in {"save", "share-ls"}:
            p.add_argument("url")
            p.add_argument("--passcode", default="")
            p.add_argument("--share-folder-id", default="0")
            if name == "save":
                p.add_argument("--parent-id", default="0")
                p.add_argument("--file-ids", nargs="+")
        elif name == "task":
            p.add_argument("task_id")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cookie = None
        if args.command == "auth-login":
            if not sys.stdin.isatty():
                raise ValueError("auth-login 需要本机交互终端；自动化请用环境变量或配置文件配合 auth-refresh")
            cookie = getpass.getpass("粘贴浏览器完整 Cookie（输入不回显）：")
        if args.command == "auth-keepalive" and not 0 < args.interval <= REFRESH_INTERVAL:
            raise ValueError("续期间隔必须大于零且不超过 6000 秒")
        with QuarkClient(cookie=cookie, config_file=args.config, timeout=args.timeout,
                         task_timeout=args.task_timeout, auto_refresh=not args.no_auto_refresh,
                         persist_cookie=not args.no_save_cookie) as client:
            actions = {
                "auth-login": client.refresh_session,
                "auth-refresh": client.refresh_session,
                "auth-status": client.auth_status,
                "ls": lambda: client.list_files(args.folder_id),
                "search": lambda: client.search_files(args.keyword),
                "mkdir": lambda: client.create_folder(args.name, args.parent_id),
                "move": lambda: client.move_files(args.file_ids, args.parent_id),
                "rename": lambda: client.rename_file(args.file_id, args.name),
                "delete": lambda: client.delete_files(args.file_ids),
                "upload": lambda: client.upload_files(args.paths, args.parent_id),
                "download": lambda: client.download_files(args.file_ids, args.output_dir, overwrite=args.overwrite),
                "share": lambda: client.create_share(args.file_ids, args.title, expires=args.expires, password=args.password),
                "save": lambda: client.save_share(args.url, args.parent_id, passcode=args.passcode,
                                                  file_ids=args.file_ids, share_folder_id=args.share_folder_id),
                "share-ls": lambda: client.list_share_files(args.url, args.passcode, args.share_folder_id),
                "task": lambda: client.wait_task(args.task_id),
            }
            if args.command == "auth-keepalive":
                while True:
                    result = client.refresh_session()
                    print(json.dumps(result, ensure_ascii=False), flush=True)
                    if client.credential_save_error:
                        print(client.credential_save_error, file=sys.stderr)
                        return 1
                    time.sleep(args.interval if result["refreshed"] else min(args.interval, 300))
            result = actions[args.command]()
            if client.credential_save_error:
                print(client.credential_save_error, file=sys.stderr)
        failed = args.command in {"upload", "download"} and any(not item["ok"] for item in result)
        print(json.dumps(result, ensure_ascii=False, indent=None if args.json else 2))
        if failed:
            print("部分文件处理失败，详情见逐项结果", file=sys.stderr)
        auth_save_failed = args.command in {"auth-login", "auth-refresh"} and bool(result.get("warning"))
        return 1 if failed or auth_save_failed else 0
    except (QuarkError, OSError, ValueError) as exc:
        error = {"ok": False, "error": str(exc)}
        print(json.dumps(error, ensure_ascii=False) if args.json else f"错误：{exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("操作已中断；已提交的远程操作可能仍在执行", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
