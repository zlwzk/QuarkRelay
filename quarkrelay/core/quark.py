"""夸克网盘客户端（走夸克网盘开放平台 HTTP API）。

鉴权：OAuth 授权码模式 —— 在内置浏览器里完成登录授权，轮询拿到 access_token /
refresh_token，之后所有请求带 SHA256 签名头。

覆盖能力：用户信息 / 容量、目录创建、文件搜索、文件详情、分享解析（走网页客态接口）、
转存、创建分享、下载直链、以及完整的分片上传（含秒传判定与断点续传）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import mimetypes
import os
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import requests

from .errors import QuarkAuthError, QuarkError

logger = logging.getLogger(__name__)

BASE = "https://open-api-drive.quark.cn"
CLIENT_ID = "third_party_agent"
SIGN_KEY = "cf134812e2de4032bd1cb7c3727e84b3"
AGENT_ID = "quarkrelay"
DEVICE_NAME = "QuarkRelay Desktop"

DEFAULT_PART_SIZE = 16 * 1024 * 1024
SHA1_INIT_STATE = [1732584193, 4023233417, 2562383102, 271733878, 3285377520]

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 QuarkRelay/1.0"
)

# 分享「读」的两个接口，开放平台已经不提供了：同一套签名打 /open/v1/user/info、
# /open/v1/share/saveas、/open/v1/task/query 都正常，打 /open/v1/share/detail 与
# /open/v1/share/page_detail 却一律回 errno 10001「签名验证失败，禁止访问」——和随便编一个
# 不存在的路径得到的响应一模一样，所以问题不在签名，是这两条路由被撤销了。
# 于是改走网页版的「客态」接口：不用登录、不用签名，匿名就能拿到 stoken 与文件列表，
# 而且这个 stoken 交给开放平台的 /open/v1/share/saveas 转存也被认（已实测）。
WEB_BASE = "https://drive-pc.quark.cn/1/clouddrive"

# 永久 / 1天 / 7天 / 30天 / 60天 / 100天 / 180天
EXPIRED_TYPES = {
    "永久": 1,
    "1天": 2,
    "7天": 3,
    "30天": 4,
    "60天": 5,
    "100天": 6,
    "180天": 7,
}
EXPIRED_NAMES = {value: key for key, value in EXPIRED_TYPES.items()}

ProgressCb = Callable[[int, int], None]


def _fmt_size(size: int) -> str:
    value = float(size or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f}{unit}" if unit != "B" else f"{int(value)}B"
        value /= 1024
    return f"{value:.1f}TB"


def md5_of(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def _share_error(code: Any, message: str) -> str:
    """把客态分享接口的错误码翻成人话（这套接口给的是 code/message）。"""
    if code == 41006:
        return "分享不存在或已被删除"
    if any(word in message for word in ("提取码", "密码", "passcode")):
        return "提取码不正确，请检查链接里的提取码"
    if any(word in message for word in ("过期", "失效")):
        return "分享已过期或已失效"
    return message or f"分享接口返回错误码 {code}"


def _normalize_share_item(item: dict[str, Any]) -> dict[str, Any]:
    """网页接口用 file_name，开放平台用 filename：统一成 filename，调用方不用改。"""
    if not item.get("filename") and item.get("file_name"):
        item["filename"] = item["file_name"]
    return item


class QuarkAuth:
    """令牌仓库，可独立于客户端存在（登录窗口用）。"""

    def __init__(
        self,
        access_token: str = "",
        refresh_token: str = "",
        device_id: str = "",
        expires_at: float = 0.0,
        user_id: str = "",
        nickname: str = "",
    ) -> None:
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.device_id = device_id
        self.expires_at = expires_at
        self.user_id = user_id
        self.nickname = nickname

    @property
    def signed_in(self) -> bool:
        return bool(self.access_token)

    def to_dict(self) -> dict[str, Any]:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "device_id": self.device_id,
            "expires_at": self.expires_at,
            "user_id": self.user_id,
            "nickname": self.nickname,
        }


class QuarkClient:
    """夸克网盘 API 客户端。"""

    def __init__(self, auth: QuarkAuth | None = None, timeout: int = 30) -> None:
        self.auth = auth or QuarkAuth()
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {"User-Agent": UA, "Accept": "application/json", "Referer": "https://pan.quark.cn/"}
        )
        self._projected_paths = {"0": "0"}

    # ----------------------------------------------------------- 基础请求层
    def _signature(self, method: str, path: str) -> tuple[str, str]:
        """算出 (x-pan-tm, x-pan-token)。

        **签名只覆盖「方法 + 路径 + 时间戳 + 密钥」，查询串一律不参与。**
        把 `?page_code=xxx` 一起签进去会直接拿到 errno 10001「签名验证失败」，
        所以这里统一裁掉 `?` 之后的部分，避免调用方踩坑。
        """
        sign_path = path.split("?", 1)[0]
        tm = str(int(time.time() * 1000))
        raw = f"{method.upper()}&{sign_path}&{tm}&{SIGN_KEY}"
        return tm, hashlib.sha256(raw.encode()).hexdigest()

    def request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        auth: bool = True,
        timeout: int | None = None,
        retries: int = 2,
        _retry_auth: bool = True,
        raw: bool = False,
    ) -> dict[str, Any]:
        """发起一次带签名的请求，返回响应包络的 data（raw=True 时返回整个包络）。"""
        method = method.upper()
        last_error: Exception | None = None
        for attempt in range(retries + 1):
            tm, token = self._signature(method, path)
            params: dict[str, str] = {"req_id": str(uuid.uuid4())}
            if auth and self.auth.access_token:
                params["access_token"] = self.auth.access_token
            if self.auth.device_id:
                params["device_id"] = self.auth.device_id
            headers = {
                "x-pan-client-id": CLIENT_ID,
                "x-pan-tm": tm,
                "x-pan-token": token,
            }
            if body is not None:
                headers["Content-Type"] = "application/json"
            try:
                response = self.session.request(
                    method,
                    BASE + path,
                    params=params,
                    headers=headers,
                    data=json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None,
                    timeout=timeout or self.timeout,
                )
            except requests.RequestException as exc:
                last_error = exc
                logger.warning("夸克请求失败(%s %s) 第%s次: %s", method, path, attempt + 1, exc)
                time.sleep(1.0 + attempt)
                continue

            try:
                payload = response.json()
            except ValueError:
                last_error = QuarkError(f"服务端返回了非 JSON 内容（HTTP {response.status_code}）")
                time.sleep(0.8)
                continue

            if not isinstance(payload, dict):
                raise QuarkError("服务端返回结构异常")

            status = payload.get("status")
            errno = payload.get("errno")
            if status in (0, None) and errno in (0, None):
                return payload if raw else payload.get("data") or {}

            info = payload.get("error_info") or payload.get("agent_msg") or f"错误码 {errno}"

            if errno in (10001,) and attempt == 0:
                # 时间戳漂移或签名抖动，重试一次
                last_error = QuarkError(info, code=errno)
                time.sleep(0.5)
                continue
            if errno in (11001, 31001):
                if _retry_auth and self.refresh_access_token():
                    _retry_auth = False
                    continue
                raise QuarkAuthError(info, code=errno)
            raise QuarkError(info, code=errno, payload=payload)

        raise QuarkError(f"网络请求失败：{last_error}")

    # ------------------------------------------------------------- 授权流程
    def start_authorize(self) -> dict[str, str]:
        """申请授权页地址，返回 {authorize_page_url, page_code, device_id}。"""
        body = {
            "client_device_id": self.auth.device_id or f"qr-{uuid.uuid4().hex[:12]}",
            "device_name": DEVICE_NAME,
            "agent_id": AGENT_ID,
            "client_id": CLIENT_ID,
            "work_dir": str(Path.home()),
        }
        data = self.request("POST", "/agent/v1/get_authorize_page_url", body, auth=False, retries=1)
        url = data.get("authorize_page_url") or ""
        page_code = data.get("page_code") or ""
        if not url or not page_code:
            raise QuarkError("获取夸克授权页失败，请稍后重试")
        if data.get("device_id"):
            self.auth.device_id = str(data["device_id"])
        return {
            "authorize_page_url": url,
            "page_code": str(page_code),
            "device_id": str(data.get("device_id") or self.auth.device_id),
        }

    def poll_authorize_code(
        self,
        page_code: str,
        timeout: float = 300.0,
        interval: float = 2.0,
        on_tick: Callable[[str], None] | None = None,
    ) -> str:
        """轮询授权结果，返回 agent_auth_code。

        授权期间接口可能返回「还没授权」之类的错误码，这属于正常等待状态，
        不能当成失败退出 —— 否则用户刚点完同意就被判失败（之前就是这么挂的）。
        """
        deadline = time.monotonic() + timeout
        last_note = "等待你在授权页上完成登录并点击同意"
        while time.monotonic() < deadline:
            try:
                data = self.request(
                    "GET",
                    f"/agent/v1/oauth/get_aac_by_pagecode?page_code={page_code}",
                    auth=False,
                    retries=0,
                )
                code = data.get("agent_auth_code") or ""
                if code:
                    return str(code)
                # status=1 表示「页面还没确认授权」，继续等
                last_note = "已扫码，等待你在授权页上点击「同意授权」"
            except QuarkAuthError:
                raise
            except QuarkError as exc:
                last_note = f"仍在等待授权（{exc}）"
                logger.debug("轮询授权码：%s", exc)
            if on_tick:
                on_tick(last_note)
            time.sleep(interval)
        raise QuarkError(f"授权超时：{last_note}")

    def exchange_auth_code(self, auth_code: str) -> QuarkAuth:
        data = self.request(
            "GET",
            "/agent/v1/oauth/agent_auth_code?agent_auth_code=" + auth_code,
            auth=False,
            retries=1,
        )
        access_token = data.get("access_token") or ""
        if not access_token:
            raise QuarkError("换取访问令牌失败，请重试")
        self.auth = QuarkAuth(
            access_token=str(access_token),
            refresh_token=str(data.get("refresh_token") or ""),
            device_id=str(data.get("device_id") or self.auth.device_id),
            expires_at=float(data.get("access_token_expires_at") or 0) / 1000.0,
            user_id=str(data.get("user_id") or ""),
        )
        return self.auth

    def refresh_access_token(self) -> bool:
        if not self.auth.refresh_token:
            return False
        try:
            data = self.request(
                "POST",
                "/agent/v1/oauth/access_token/rotate",
                {"refresh_token": self.auth.refresh_token, "device_id": self.auth.device_id},
                auth=False,
                retries=1,
            )
        except QuarkError as exc:
            logger.warning("刷新夸克令牌失败：%s", exc)
            return False
        token = data.get("access_token") or ""
        if not token:
            return False
        self.auth.access_token = str(token)
        if data.get("refresh_token"):
            self.auth.refresh_token = str(data["refresh_token"])
        expires = data.get("access_token_expires_at") or data.get("expires_in") or 0
        self.auth.expires_at = float(expires) / 1000.0 if float(expires) > 1e6 else time.time() + float(expires)
        logger.info("夸克令牌已刷新")
        return True

    # --------------------------------------------------------------- 用户侧
    def user_info(self) -> dict[str, Any]:
        return self.request("GET", "/open/v1/user/info", retries=1)

    def vip_info(self) -> dict[str, Any]:
        return self.request("GET", "/open/v1/user/get_vip_info", retries=1)

    def storage_summary(self) -> str:
        try:
            info = self.vip_info()
        except QuarkError:
            return ""
        used = int(info.get("used") or 0)
        capacity = int(info.get("capacity") or 0)
        vip = info.get("vip_type") or "NORMAL"
        if capacity:
            return f"{vip} · 已用 {_fmt_size(used)} / {_fmt_size(capacity)}"
        return str(vip)

    # --------------------------------------------------------------- 目录侧
    def ensure_dir(self, dir_path: str, pdir_fid: str = "0") -> str:
        """按路径（支持 a/b/c 嵌套）创建目录并返回 fid，同名幂等。"""
        path = (dir_path or "").strip().strip("/")
        if not path or path in ("0", "根目录"):
            return "0"
        cache_key = f"{pdir_fid}:{path}"
        if cache_key in self._projected_paths:
            return self._projected_paths[cache_key]

        # 逐级创建，保证返回的是最后一级的 fid
        current = pdir_fid
        for segment in [part for part in path.split("/") if part and part != "."]:
            data = self.request("POST", "/open/v1/dir", {"dir_path": segment, "pdir_fid": current})
            fid = str(data.get("fid") or "")
            if not fid:
                raise QuarkError(f"创建目录「{segment}」失败")
            current = fid
        self._projected_paths[cache_key] = current
        return current

    def dir_info(self, fid: str) -> dict[str, Any]:
        return self.request("GET", f"/open/v1/file/info?fid={fid}&fetch_full_path=1", retries=1)

    def search(self, keyword: str, size: int = 20, category: int | None = None) -> list[dict[str, Any]]:
        body: dict[str, Any] = {"search_type": "mix", "keyword": keyword, "size": size, "page": 1}
        if category is not None:
            body["category"] = category
        data = self.request("POST", "/agent/v1/file/search", body, retries=1)
        items = data.get("file_list") or []
        return [item for item in items if isinstance(item, dict)]

    def file_info(self, fid: str) -> dict[str, Any]:
        return self.request("GET", f"/open/v1/file/info?fid={fid}&fetch_full_path=1", retries=1)

    def find_fid(self, filename: str, parent_fid: str | None = None) -> str | None:
        """在网盘里按文件名找 fid（转存后定位用，开放平台无目录列举接口）。"""
        try:
            items = self.search(filename, size=50)
        except QuarkError as exc:
            logger.warning("搜索 %s 失败：%s", filename, exc)
            return None
        exact = [i for i in items if str(i.get("filename")) == filename]
        pool = exact or [i for i in items if filename and filename in str(i.get("filename"))]
        if parent_fid and parent_fid != "0":
            for item in pool:
                if str(item.get("parent_fid")) == str(parent_fid):
                    return str(item.get("fid"))
        if pool:
            return str(pool[0].get("fid"))
        return None

    # --------------------------------------------------------------- 分享侧
    def _web_share(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        retries: int = 1,
    ) -> dict[str, Any]:
        """调一次网页版「客态」分享接口，返回整个响应包络。

        这类接口既不用登录也不用签名，但只认网页端那套参数（pr/fr），
        错误码是 `code`/`message`，跟开放平台的 `errno`/`error_info` 不是一套。
        """
        query: dict[str, Any] = {"pr": "ucpro", "fr": "pc"}
        query.update({key: value for key, value in (params or {}).items() if value is not None})
        last_error: Exception | None = None
        for attempt in range(retries + 1):
            try:
                response = self.session.request(
                    method,
                    WEB_BASE + path,
                    params=query,
                    json=body,
                    timeout=self.timeout,
                )
                payload = response.json()
            except (requests.RequestException, ValueError) as exc:
                last_error = exc
                logger.warning("客态分享接口失败(%s %s) 第%s次：%s", method, path, attempt + 1, exc)
                time.sleep(0.8 + attempt)
                continue
            if not isinstance(payload, dict):
                raise QuarkError("分享接口返回结构异常")
            code = payload.get("code")
            if code in (0, None):
                return payload
            raise QuarkError(
                _share_error(code, str(payload.get("message") or "")),
                code=code,
                payload=payload,
            )
        raise QuarkError(f"访问分享页失败：{last_error}")

    def share_detail(self, pwd_id: str, passcode: str = "", page: int = 1, size: int = 200) -> dict[str, Any]:
        """解析分享链接：拿到 stoken 与首层文件列表（走网页客态接口）。

        返回结构和开放平台时期保持一致，调用方不用改：
        `{"token_info": {"stoken", "title", ...}, "list": [{"filename", "fid", "dir"}], "share": {...}}`
        """
        envelope = self._web_share(
            "POST",
            "/share/sharepage/token",
            body={"pwd_id": pwd_id, "passcode": passcode or ""},
        )
        info = envelope.get("data") or {}
        stoken = str(info.get("stoken") or "")
        if not stoken:
            raise QuarkError("分享已失效或提取码不正确")
        listing = self.share_page_detail(pwd_id, stoken, "0", page, size)
        return {
            "token_info": {
                "stoken": stoken,
                "title": str(info.get("title") or ""),
                "expired_at": info.get("expired_at"),
                "expired_type": info.get("expired_type"),
                "share_type": info.get("share_type"),
                "url_type": info.get("url_type"),
            },
            "list": listing.get("list") or [],
            "share": listing.get("share") or {},
            "metadata": listing.get("metadata") or {},
        }

    def share_page_detail(self, pwd_id: str, stoken: str, pdir_fid: str = "0", page: int = 1, size: int = 200) -> dict[str, Any]:
        """列出分享里的文件（走网页客态接口）。"""
        envelope = self._web_share(
            "GET",
            "/share/sharepage/detail",
            params={
                "pwd_id": pwd_id,
                "stoken": stoken,
                "pdir_fid": pdir_fid,
                "force": 0,
                "_page": page,
                "_size": size,
                "_fetch_banner": 0,
                "_fetch_share": 1,
                "_fetch_total": 1,
                "_sort": "file_type:asc,updated_at:desc",
                "ver": 2,
                "fetch_share_full_path": 0,
            },
        )
        data = envelope.get("data") or {}
        return {
            **data,
            "list": [
                _normalize_share_item(dict(item))
                for item in (data.get("list") or [])
                if isinstance(item, dict)
            ],
            "metadata": envelope.get("metadata") or data.get("metadata") or {},
        }

    def share_saveas(
        self,
        pwd_id: str,
        stoken: str,
        to_pdir_fid: str = "0",
        fid_list: list[str] | None = None,
        fid_token_list: list[str] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"pwd_id": pwd_id, "stoken": stoken, "to_pdir_fid": to_pdir_fid}
        if fid_list and fid_token_list:
            body["fid_list"] = fid_list
            body["fid_token_list"] = fid_token_list
        else:
            body["save_all"] = True
            body["fid_list"] = []
            body["fid_token_list"] = []
        return self.request("POST", "/open/v1/share/saveas", body, retries=0)

    def share_create(
        self,
        fid_list: list[str],
        title: str = "",
        url_type: int = 2,
        expired_type: int = 1,
    ) -> dict[str, Any]:
        body = {
            "fid_list": fid_list,
            "title": title or "夸克中转站分享",
            "url_type": url_type,
            "expired_type": expired_type,
        }
        return self.request("POST", "/agent/v1/share/create", body, retries=0)

    # --------------------------------------------------------------- 任务侧
    def query_task(self, task_id: str) -> dict[str, Any]:
        return self.request("POST", "/open/v1/task/query", {"task_id": task_id}, retries=1)

    def wait_task(
        self,
        task_id: str,
        timeout: float = 180.0,
        interval: float = 1.2,
        on_tick: Callable[[str], None] | None = None,
    ) -> str:
        """等待异步任务完成，返回 final status（2 成功 / 3 失败）。"""
        deadline = time.monotonic() + timeout
        status = "0"
        while time.monotonic() < deadline:
            try:
                data = self.query_task(task_id)
            except QuarkError as exc:
                logger.debug("查询任务失败：%s", exc)
                time.sleep(interval)
                continue
            status = str(data.get("status", "0"))
            if on_tick:
                on_tick(status)
            if status in ("2", "3", "4"):
                return status
            time.sleep(interval)
        return status

    # --------------------------------------------------------------- 下载侧
    def download_url(self, fid: str) -> dict[str, Any]:
        return self.request("POST", "/open/v1/file/get_download_url", {"fid": fid}, retries=1)

    def download_to(self, fid: str, dest: Path, progress: ProgressCb | None = None) -> Path:
        info = self.download_url(fid)
        url = info.get("download_url") or ""
        if not url:
            raise QuarkError("获取夸克下载直链失败")
        total = int(info.get("size") or 0)
        with self.session.get(
            url,
            cookies={
                "x_pan_client_id": CLIENT_ID,
                "x_pan_access_token": self.auth.access_token,
            },
            stream=True,
            timeout=60,
        ) as response:
            response.raise_for_status()
            total = total or int(response.headers.get("Content-Length") or 0)
            done = 0
            dest.parent.mkdir(parents=True, exist_ok=True)
            with open(dest, "wb") as handle:
                for chunk in response.iter_content(1 << 20):
                    if not chunk:
                        continue
                    handle.write(chunk)
                    done += len(chunk)
                    if progress:
                        progress(done, total)
        return dest

    # --------------------------------------------------------------- 上传侧
    def upload_file(
        self,
        file_path: str | Path,
        pdir_fid: str = "0",
        *,
        remote_name: str | None = None,
        progress: ProgressCb | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """完整上传流程：预上传(秒传) → 补哈希 → 取分片地址 → 分片直传 → 完成。"""
        path = Path(file_path)
        size = path.stat().st_size
        name = remote_name or path.name
        sha1 = hashlib.sha1()
        md5 = hashlib.md5()
        with open(path, "rb") as handle:
            while True:
                chunk = handle.read(4 << 20)
                if not chunk:
                    break
                sha1.update(chunk)
                md5.update(chunk)
        sha1_hex, md5_hex = sha1.hexdigest(), md5.hexdigest()
        return self.upload_path(
            path,
            name,
            size,
            sha1_hex,
            md5_hex,
            pdir_fid=pdir_fid,
            progress=progress,
            should_cancel=should_cancel,
        )

    def upload_path(
        self,
        path: Path,
        name: str,
        size: int,
        sha1_hex: str,
        md5_hex: str,
        *,
        pdir_fid: str = "0",
        progress: ProgressCb | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        ext = Path(name).suffix.lstrip(".").lower() or (mimetypes.guess_extension(mimetypes.guess_type(name)[0] or "") or "").lstrip(".")
        now_ms = int(time.time() * 1000)
        body: dict[str, Any] = {
            "file_name": name,
            "size": size,
            "sha1": sha1_hex,
            "md5": md5_hex,
            "pdir_fid": pdir_fid,
            "format_type": ext,
            "l_created_at": now_ms,
            "l_updated_at": now_ms,
            "same_path_file_reuse": True,
            "parallel_upload": True,
            "hash_update": False,
            "device_id": self.auth.device_id or "quarkrelay",
            "device_name": DEVICE_NAME,
        }
        pre = self.request("POST", "/open/v1/file/upload_pre", body, retries=1)
        if str(pre.get("finish")) == "1" and pre.get("fid"):
            logger.info("命中秒传：%s", name)
            if progress:
                progress(size, size)
            return {"fid": str(pre["fid"]), "instant": True, "name": name}

        task_id = str(pre.get("task_id") or "")
        if not task_id:
            raise QuarkError("预上传未返回任务号")

        # 补交哈希，再给一次秒传机会
        try:
            hashed = self.request(
                "POST",
                "/open/v1/file/update/hash",
                {"task_id": task_id, "sha1": sha1_hex, "md5": md5_hex},
                retries=1,
            )
            if str(hashed.get("finish")) == "1" and hashed.get("fid"):
                logger.info("命中秒传(补哈希)：%s", name)
                if progress:
                    progress(size, size)
                return {"fid": str(hashed["fid"]), "instant": True, "name": name}
        except QuarkError as exc:
            logger.debug("补交哈希失败（继续分片上传）：%s", exc)

        part_size = int(pre.get("part_size") or 0) or DEFAULT_PART_SIZE
        part_count = max(1, (size + part_size - 1) // part_size)
        parts = []
        offset = 0
        for number in range(1, part_count + 1):
            length = min(part_size, size - offset) if size else 0
            parts.append({"part_number": number, "part_size": max(length, 0)})
            offset += length

        urls = self.request(
            "POST",
            "/open/v1/file/get_upload_urls",
            {"task_id": task_id, "part_info_list": parts},
            retries=1,
        )
        upload_urls = {
            int(item.get("part_number") or 0): item for item in (urls.get("upload_urls") or [])
        }
        if not upload_urls and pre.get("upload_urls"):
            upload_urls = {
                int(item.get("part_number") or 0): item for item in pre["upload_urls"]
            }

        etags: list[dict[str, Any]] = []
        uploaded = 0
        with open(path, "rb") as handle:
            for part in parts:
                if should_cancel and should_cancel():
                    raise QuarkError("任务已取消")
                number = part["part_number"]
                length = part["part_size"]
                chunk = handle.read(length)
                info = upload_urls.get(number) or {}
                url = info.get("upload_url") or info.get("uploadUrl") or info.get("url")
                if not url:
                    raise QuarkError(f"第 {number} 个分片没有拿到上传地址")
                signature = ""
                sig_info = info.get("signature_info") or info.get("signatureInfo") or {}
                if isinstance(sig_info, dict):
                    signature = sig_info.get("signature") or ""
                signature = signature or info.get("signature") or ""
                headers = {"Content-Length": str(len(chunk))}
                if signature:
                    headers["Authorization"] = signature
                response = self.session.put(url, data=chunk, headers=headers, timeout=300)
                if response.status_code >= 400:
                    raise QuarkError(f"分片 {number} 上传失败（HTTP {response.status_code}）")
                etag = (response.headers.get("ETag") or response.headers.get("etag") or "").strip('"')
                if not etag:
                    etag = hashlib.md5(chunk).hexdigest()
                etags.append({"part_number": number, "etag": etag})
                uploaded += len(chunk)
                if progress:
                    progress(uploaded, size)

        done = self.request(
            "POST",
            "/open/v1/file/upload_finish",
            {"task_id": task_id, "part_info_list": etags},
            retries=1,
        )
        fid = str(done.get("fid") or "")
        if not fid:
            found = self.find_fid(name, pdir_fid)
            fid = found or ""
        if not fid:
            raise QuarkError("上传完成但未返回文件 ID")
        return {"fid": fid, "instant": False, "name": name}

    # ------------------------------------------------------------------ 便捷
    def transfer_and_share(
        self,
        pwd_id: str,
        passcode: str,
        save_dir: str,
        title: str,
        url_type: int = 2,
        expired_type: int = 1,
        on_stage: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        """一条龙：解析分享 → 转存到指定目录 → 生成新的分享链接。"""
        stage = on_stage or (lambda _msg: None)

        stage("解析分享链接")
        detail = self.share_detail(pwd_id, passcode)
        token_info = detail.get("token_info") or {}
        stoken = str(token_info.get("stoken") or detail.get("stoken") or "")
        if not stoken:
            raise QuarkError("分享已失效或提取码错误")
        items = detail.get("list") or []
        share_title = str(token_info.get("title") or "")

        stage("定位目标目录")
        target_fid = self.ensure_dir(save_dir)

        stage("转存到我的网盘")
        result = self.share_saveas(pwd_id, stoken, to_pdir_fid=target_fid)
        task_id = str(result.get("task_id") or "")
        if task_id:
            status = self.wait_task(task_id)
            if status == "3":
                raise QuarkError("转存任务失败，可能是空间不足或分享已失效")

        stage("定位转存后的文件")
        names = [str(item.get("filename") or "") for item in items if item.get("filename")]
        fid_list: list[str] = []
        for name in names:
            found = self.find_fid(name, target_fid)
            if found:
                fid_list.append(found)
        if not fid_list:
            raise QuarkError("转存成功，但没能定位到文件（可稍后在夸克中手动分享）")

        stage("生成新的分享链接")
        share = self.share_create(fid_list, title=title, url_type=url_type, expired_type=expired_type)
        return {
            "share_url": str(share.get("share_url") or ""),
            "passcode": str(share.get("passcode") or ""),
            "title": str(share.get("title") or title),
            "share_title": share_title,
            "fid_list": fid_list,
            "items": names,
            "target_fid": target_fid,
        }
