"""百度网盘网页端客户端（基于内置浏览器登录拿到的会话 Cookie）。

只使用百度网盘网页版自身使用的接口，不依赖任何需要申请 AppKey 的第三方服务：
  * 会话校验 / 取 bdstoken：/api/gettemplatevariable
  * 分享解析：/s/{surl} 页面 + /share/verify 校验提取码
  * 分享列表：/share/list
  * 转存：/share/transfer
  * 我的网盘列表 / 建目录：/api/list、/api/create

下载直链（dlink）由百度前端用私有 sign 算法生成，本程序不走逆向，而是通过内置
浏览器让百度页面自己去请求下载，应用侧拦截真实下载请求拿到直链，再用会话 Cookie
流式读取数据。这样既不需要破解签名，也能保证长期可用。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

import requests

from .errors import BaiduAuthError, BaiduError

logger = logging.getLogger(__name__)

PAN = "https://pan.baidu.com"
PCS = "https://d.pcs.baidu.com"
APP_ID = "250528"
SLICE_SIZE = 4 * 1024 * 1024  # 百度网页端就是按 4MB 分片上传的
SHARE_PWD_CHARS = "abcdefghijkmnpqrstuvwxyz23456789"
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

ERRNO_HINTS = {
    0: "成功",
    -6: "登录态失效，请重新登录百度网盘",
    2: "参数错误",
    12: "目标目录下已存在同名文件",
    -7: "文件名非法",
    -10: "网盘空间不足",
    -9: "目标目录不存在",
    105: "分享链接已失效",
    112: "分享已过期或不存在",
    118: "该分享已被取消",
    -33: "转存失败，可能是空间不足",
    -62: "请求过于频繁，请稍后再试",
}

ProgressCb = Callable[[int, int], None]


def _errno_message(payload: dict[str, Any]) -> str:
    errno = payload.get("errno")
    if errno in ERRNO_HINTS:
        return ERRNO_HINTS[errno]
    show_msg = payload.get("show_msg") or payload.get("errmsg") or payload.get("error_msg")
    if show_msg:
        return str(show_msg)
    return f"百度网盘返回错误码 {errno}"


@dataclass
class BaiduFile:
    fs_id: int
    name: str
    path: str
    size: int
    is_dir: bool
    md5: str = ""
    server_mtime: int = 0
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def isdir(self) -> bool:
        return self.is_dir

    @classmethod
    def from_payload(cls, item: dict[str, Any]) -> "BaiduFile":
        return cls(
            fs_id=int(item.get("fs_id") or 0),
            name=str(item.get("server_filename") or item.get("filename") or ""),
            path=str(item.get("path") or ""),
            size=int(item.get("size") or 0),
            is_dir=bool(int(item.get("isdir") or 0)),
            md5=str(item.get("md5") or ""),
            server_mtime=int(item.get("server_mtime") or 0),
            raw=item,
        )


class BaiduClient:
    """百度网盘网页接口客户端。"""

    def __init__(self, cookies: str = "", user_agent: str = "", timeout: int = 30) -> None:
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": user_agent or DEFAULT_UA,
                "Referer": f"{PAN}/disk/home",
                "Accept": "application/json, text/plain, */*",
            }
        )
        self.bdstoken = ""
        self.uk = ""
        self.nickname = ""
        if cookies:
            self.set_cookies(cookies)

    # ------------------------------------------------------------- 会话管理
    def set_cookies(self, cookies: str) -> None:
        self.session.cookies.clear()
        for part in re.split(r"[;\n]", cookies or ""):
            part = part.strip()
            if not part or "=" not in part:
                continue
            key, _, value = part.partition("=")
            key = key.strip()
            value = value.strip()
            if not key:
                continue
            self.session.cookies.set(key, value, domain=".baidu.com")
            self.session.cookies.set(key, value, domain="pan.baidu.com")
            self.session.cookies.set(key, value, domain=".pan.baidu.com")

    def export_cookies(self) -> str:
        seen: dict[str, str] = {}
        for cookie in self.session.cookies:
            if cookie.name and cookie.value:
                seen[cookie.name] = cookie.value
        return "; ".join(f"{k}={v}" for k, v in seen.items())

    @property
    def logged_in(self) -> bool:
        return bool(self.session.cookies.get("BDUSS")) or bool(self.bdstoken)

    def verify(self, *, deep: bool = True) -> dict[str, Any]:
        """校验会话并拉取 bdstoken / uk。"""
        if not self.session.cookies.get("BDUSS"):
            raise BaiduAuthError("尚未登录百度网盘")
        fields = '["bdstoken","token","uk","isdocuser","servertime"]'
        try:
            response = self.session.get(
                f"{PAN}/api/gettemplatevariable",
                params={"clienttype": "0", "app_id": APP_ID, "web": "1", "fields": fields},
                timeout=self.timeout,
            )
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise BaiduAuthError(f"校验百度会话失败：{exc}") from exc
        if payload.get("errno") not in (0, None):
            raise BaiduAuthError(_errno_message(payload))
        result = payload.get("result") or {}
        self.bdstoken = str(result.get("bdstoken") or "")
        self.uk = str(result.get("uk") or "")
        if deep and not self.nickname:
            self.nickname = self._fetch_nickname() or (f"UID {self.uk}" if self.uk else "百度网盘用户")
        return result

    def _fetch_nickname(self) -> str:
        for url in (f"{PAN}/disk/main", f"{PAN}/api/user/getinfo"):
            try:
                text = self.session.get(url, timeout=self.timeout).text
            except requests.RequestException:
                continue
            for pattern in (
                r'"username"\s*:\s*"([^"]{1,40})"',
                r'NETDISK_USERNAME"\s*:\s*"([^"]{1,40})"',
                r'"bdstoken":"[0-9a-f]+","username":"([^"]{1,40})"',
                r'"loginstate"[^}]{0,200}?"username":"([^"]{1,40})"',
            ):
                match = re.search(pattern, text)
                if match:
                    return match.group(1)
        return ""

    def _check(self, payload: dict[str, Any], *, context: str) -> dict[str, Any]:
        errno = payload.get("errno")
        if errno in (0, None):
            return payload
        if errno in (-6, 2, 400002) or "登录" in str(payload.get("show_msg") or ""):
            raise BaiduAuthError(_errno_message(payload))
        raise BaiduError(f"{context}失败：{_errno_message(payload)}", code=errno, payload=payload)

    # --------------------------------------------------------------- 分享侧
    def resolve_share(self, url: str, password: str = "") -> "BaiduShare":
        """解析分享链接，必要时校验提取码，返回分享对象。"""
        parsed = urlparse(url)
        match = re.search(r"/s/1?([A-Za-z0-9_-]+)", parsed.path or "")
        surl = match.group(1) if match else ""
        if not surl:
            match = re.search(r"[?&]surl=([A-Za-z0-9_-]+)", parsed.query or "")
            surl = match.group(1) if match else ""
        if not surl:
            raise BaiduError("无法从链接中解析出分享标识，请确认是百度网盘分享链接")

        if password:
            self.verify_share_password(surl, password)

        text = self.session.get(f"{PAN}/s/1{surl}" if not surl.startswith("1") else f"{PAN}/s/{surl}",
                                timeout=self.timeout).text
        if password and ("请输入提取码" in text or "verify" in text[:4000] and "shareid" not in text):
            self.verify_share_password(surl, password)
            text = self.session.get(f"{PAN}/s/{surl}", timeout=self.timeout).text

        share = BaiduShare(surl=surl, raw_html=text)
        share.share_id = self._grab(text, r'"shareid"\s*:\s*"?(\d+)"?') or self._grab(
            text, r'"SHAREID"\s*:\s*"?(\d+)"?'
        )
        share.share_uk = (
            self._grab(text, r'"share_uk"\s*:\s*"?(\d+)"?')
            or self._grab(text, r'"SHARE_UK"\s*:\s*"?(\d+)"?')
            or self._grab(text, r'"uk"\s*:\s*"?(\d+)"?')
        )
        share.title = self._grab(text, r'"share_title"\s*:\s*"([^"]*)"') or self._grab(
            text, r"<title>([^<]*)</title>"
        )
        share.require_password = not share.share_id

        if "分享的文件已经被取消" in text or "你访问的页面不存在" in text:
            raise BaiduError("分享链接已失效或被取消")
        return share

    @staticmethod
    def _grab(text: str, pattern: str) -> str:
        match = re.search(pattern, text)
        return match.group(1) if match else ""

    def verify_share_password(self, surl: str, password: str) -> bool:
        if not password:
            return True
        timestamp = int(time.time() * 1000)
        try:
            response = self.session.post(
                f"{PAN}/share/verify",
                params={
                    "surl": surl,
                    "t": timestamp,
                    "channel": "chunlei",
                    "web": "1",
                    "app_id": APP_ID,
                    "clienttype": "0",
                },
                data={"pwd": password, "vcode": "", "vcode_str": ""},
                timeout=self.timeout,
            )
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise BaiduError(f"校验提取码失败：{exc}") from exc
        if payload.get("errno") == 0:
            return True
        code = payload.get("errno")
        if code in (-9, -12, 2):
            raise BaiduError("提取码错误，请重新输入")
        if code == 105:
            raise BaiduError("分享链接已失效")
        raise BaiduError(f"校验提取码失败：{_errno_message(payload)}")

    def list_share(self, share: "BaiduShare", directory: str = "/") -> list[BaiduFile]:
        if not share.share_id or not share.share_uk:
            raise BaiduError("分享信息不完整，无法读取文件列表")
        payload = self.session.get(
            f"{PAN}/share/list",
            params={
                "uk": share.share_uk,
                "shareid": share.share_id,
                "order": "other",
                "desc": "1",
                "showempty": "0",
                "web": "1",
                "page": "1",
                "num": "500",
                "dir": directory,
                "t": int(time.time() * 1000),
                "channel": "chunlei",
                "app_id": APP_ID,
                "bdstoken": self.bdstoken,
                "clienttype": "0",
            },
            headers={"Referer": f"{PAN}/s/1{share.surl}"},
            timeout=self.timeout,
        ).json()
        self._check(payload, context="读取分享列表")
        return [BaiduFile.from_payload(item) for item in payload.get("list") or []]

    def transfer(
        self,
        share: "BaiduShare",
        fs_ids: list[int],
        target_path: str,
        *,
        on_exists: str = "rename",
    ) -> dict[str, Any]:
        """把分享中的文件转存到自己的网盘目录（目录不存在会自动创建）。"""
        self.mkdir(target_path)
        payload = self.session.post(
            f"{PAN}/share/transfer",
            params={
                "shareid": share.share_id,
                "from": share.share_uk,
                "bdstoken": self.bdstoken,
                "channel": "chunlei",
                "web": "1",
                "app_id": APP_ID,
                "clienttype": "0",
                "ondup": on_exists,
            },
            data={"fsidlist": json.dumps(fs_ids), "path": target_path},
            headers={"Referer": f"{PAN}/s/1{share.surl}"},
            timeout=self.timeout,
        ).json()
        if payload.get("errno") == 12:
            return {"errno": 12, "already": True}
        return self._check(payload, context="转存")

    # --------------------------------------------------------------- 我的盘
    def list_dir(self, path: str = "/") -> list[BaiduFile]:
        payload = self.session.get(
            f"{PAN}/api/list",
            params={
                "dir": path or "/",
                "order": "time",
                "desc": "1",
                "showempty": "0",
                "web": "1",
                "page": "1",
                "num": "500",
                "channel": "chunlei",
                "app_id": APP_ID,
                "bdstoken": self.bdstoken,
                "clienttype": "0",
            },
            timeout=self.timeout,
        ).json()
        self._check(payload, context="读取网盘列表")
        return [BaiduFile.from_payload(item) for item in payload.get("list") or []]

    def mkdir(self, path: str) -> bool:
        path = self._normalize(path)
        if path in ("/", ""):
            return True
        parts = [p for p in path.split("/") if p]
        current = ""
        for part in parts:
            parent = current or "/"
            current = f"{current}/{part}"
            existing = {item.name for item in self.list_dir(parent) if item.is_dir}
            if part in existing:
                continue
            payload = self.session.post(
                f"{PAN}/api/create",
                params={
                    "a": "commit",
                    "bdstoken": self.bdstoken,
                    "channel": "chunlei",
                    "web": "1",
                    "app_id": APP_ID,
                    "clienttype": "0",
                },
                data={"path": current, "isdir": "1", "block_list": "[]"},
                timeout=self.timeout,
            ).json()
            if payload.get("errno") not in (0, -8):
                self._check(payload, context=f"创建目录 {current}")
        return True

    @staticmethod
    def _normalize(path: str) -> str:
        path = (path or "/").strip()
        if not path.startswith("/"):
            path = "/" + path
        return re.sub(r"/{2,}", "/", path).rstrip("/") or "/"

    def find_file(self, path: str) -> BaiduFile | None:
        parent = path.rsplit("/", 1)[0] or "/"
        name = path.rsplit("/", 1)[-1]
        for item in self.list_dir(parent):
            if item.name == name:
                return item
        return None

    def delete(self, paths: list[str]) -> None:
        payload = self.session.post(
            f"{PAN}/api/filemanager",
            params={
                "opera": "delete",
                "bdstoken": self.bdstoken,
                "channel": "chunlei",
                "web": "1",
                "app_id": APP_ID,
                "clienttype": "0",
            },
            data={"filelist": json.dumps(paths)},
            timeout=self.timeout,
        ).json()
        self._check(payload, context="删除")

    # --------------------------------------------------------------- 上传侧
    def _api_params(self, method: str = "") -> dict[str, str]:
        params = {
            "bdstoken": self.bdstoken,
            "channel": "chunlei",
            "web": "1",
            "app_id": APP_ID,
            "clienttype": "0",
        }
        if method:
            params["method"] = method
        return params

    @staticmethod
    def _slice_md5s(path: Path, slice_size: int = SLICE_SIZE) -> list[str]:
        """按网页端规则算出每一片的 MD5（末片是剩下的全部）。"""
        digests: list[str] = []
        with open(path, "rb") as handle:
            while True:
                chunk = handle.read(slice_size)
                if not chunk:
                    break
                digests.append(hashlib.md5(chunk).hexdigest())
        return digests or [hashlib.md5(b"").hexdigest()]

    def _unique_name(self, directory: str, name: str) -> str:
        stem, dot, suffix = name.rpartition(".")
        base = stem if dot else name
        ext = f".{suffix}" if dot else ""
        taken = {item.name for item in self.list_dir(directory)}
        for index in range(1, 200):
            candidate = f"{base} ({index}){ext}"
            if candidate not in taken:
                return candidate
        return f"{base} ({int(time.time())}){ext}"

    def upload(
        self,
        local: str | Path,
        remote_dir: str = "/",
        *,
        on_exists: str = "rename",
        progress: ProgressCb | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> BaiduFile:
        """把本地文件传到百度网盘：预上传（支持秒传）→ 分片上传 → 收尾。

        on_exists: rename=同名自动加序号（默认）· skip=已有就跳过 · overwrite=先删再传。
        """
        path = Path(local)
        if not path.is_file():
            raise BaiduError(f"待上传的文件不存在：{path.name}")
        size = path.stat().st_size
        directory = self._normalize(remote_dir)
        self.mkdir(directory)

        name = path.name
        existing = next(
            (item for item in self.list_dir(directory) if item.name == name and not item.is_dir), None
        )
        if existing is not None:
            if on_exists == "skip":
                if progress and size:
                    progress(size, size)
                return existing
            if on_exists == "overwrite":
                self.delete([existing.path])
            else:
                name = self._unique_name(directory, name)

        remote = self._normalize(f"{directory}/{name}")
        slices = self._slice_md5s(path)
        pre = self.session.post(
            f"{PAN}/api/precreate",
            params=self._api_params(),
            data={
                "path": remote,
                "autoinit": "1",
                "size": str(size),
                "isdir": "0",
                "block_list": json.dumps(slices),
                "rtype": "3",
            },
            timeout=self.timeout,
        ).json()
        self._check(pre, context="预上传")
        upload_id = str(pre.get("uploadid") or "")
        if int(pre.get("return_type") or 0) != 2 or not upload_id:
            # return_type != 2 说明服务端已有这个文件（秒传命中），不用再传数据
            logger.info("百度秒传命中：%s", name)
            if progress and size:
                progress(size, size)
            return self.find_file(remote) or BaiduFile(
                fs_id=0, name=name, path=remote, size=size, is_dir=False
            )

        waiting = pre.get("block_list")
        pending = (
            [int(index) for index in waiting]
            if isinstance(waiting, list) and waiting
            else list(range(len(slices)))
        )
        uploaded = 0
        with open(path, "rb") as handle:
            for index in pending:
                if should_cancel and should_cancel():
                    raise BaiduError("任务已取消")
                handle.seek(index * SLICE_SIZE)
                chunk = handle.read(SLICE_SIZE)
                result = self.session.post(
                    f"{PCS}/rest/2.0/pcs/superfile2",
                    params={
                        "method": "upload",
                        "type": "tmpfile",
                        "path": remote,
                        "uploadid": upload_id,
                        "partseq": str(index),
                        "app_id": APP_ID,
                        "channel": "chunlei",
                        "web": "1",
                        "clienttype": "0",
                        "bdstoken": self.bdstoken,
                    },
                    files={"file": (f"part{index}", chunk)},
                    timeout=600,
                ).json()
                if result.get("md5") is None and not result.get("fs_id"):
                    raise BaiduError(
                        f"上传分片 {index} 失败：{result.get('error_msg') or result.get('error_code') or result}"
                    )
                uploaded += len(chunk)
                if progress:
                    progress(min(uploaded, size), size)

        done = self.session.post(
            f"{PAN}/api/create",
            params=self._api_params(),
            data={
                "path": remote,
                "size": str(size),
                "isdir": "0",
                "block_list": json.dumps(slices),
                "uploadid": upload_id,
                "rtype": "3",
            },
            timeout=self.timeout,
        ).json()
        self._check(done, context="上传收尾")
        if progress and size:
            progress(size, size)
        return self.find_file(remote) or BaiduFile(
            fs_id=0, name=name, path=remote, size=size, is_dir=False
        )

    # --------------------------------------------------------------- 分享创建
    def create_share(
        self,
        paths: list[str],
        *,
        period: int = 0,
        password: str = "",
    ) -> dict[str, str]:
        """给一批文件/目录建分享，返回 {"link", "pwd", "period"}。period 0 表示永久。"""
        fs_ids: list[int] = []
        for path in paths:
            item = self.find_file(self._normalize(path))
            if item is None:
                raise BaiduError(f"找不到要分享的内容：{path}")
            fs_ids.append(item.fs_id)
        if not fs_ids:
            raise BaiduError("没有可分享的内容")
        pwd = password or "".join(secrets.choice(SHARE_PWD_CHARS) for _ in range(4))
        payload = self.session.post(
            f"{PAN}/share/set",
            params=self._api_params(),
            data={
                "fid_list": json.dumps(fs_ids),
                "schannel": "4",
                "channel_list": json.dumps([4]),
                "period": str(period),
                "pwd": pwd,
                "pc_modify_permission": "0",
            },
            timeout=self.timeout,
        ).json()
        self._check(payload, context="创建分享")
        link = str(payload.get("link") or "")
        if link.startswith("http://"):
            link = "https://" + link[len("http://") :]
        if not link:
            raise BaiduError("百度没有返回分享链接，请稍后重试")
        return {"link": link, "pwd": str(payload.get("pwd") or pwd), "period": str(period)}

    # --------------------------------------------------------------- 直链下载
    def dlink_headers(self, referer: str = "") -> dict[str, str]:
        cookies = self.export_cookies()
        headers = {
            "User-Agent": str(self.session.headers.get("User-Agent") or DEFAULT_UA),
            "Referer": referer or f"{PAN}/disk/home",
            "Accept": "*/*",
            "Accept-Encoding": "identity",
        }
        if cookies:
            headers["Cookie"] = cookies
        return headers

    def open_stream(self, url: str, referer: str = "", offset: int = 0) -> requests.Response:
        """按给定直链打开下载流，支持 Range 续传。"""
        headers = self.dlink_headers(referer)
        if offset:
            headers["Range"] = f"bytes={offset}-"
        response = requests.get(url, headers=headers, stream=True, timeout=60, allow_redirects=True)
        if response.status_code >= 400:
            response.close()
            raise BaiduError(
                f"直链下载被拒绝（HTTP {response.status_code}），可能链接已过期，请重试一次"
            )
        return response

    def download(
        self,
        url: str,
        dest: Path,
        *,
        referer: str = "",
        progress: ProgressCb | None = None,
        should_cancel: Callable[[], bool] | None = None,
        expect_size: int = 0,
    ) -> Path:
        """流式下载到临时缓冲文件，支持断点续传。"""
        dest.parent.mkdir(parents=True, exist_ok=True)
        offset = dest.stat().st_size if dest.exists() else 0
        if expect_size and offset >= expect_size:
            if progress:
                progress(offset, expect_size)
            return dest

        response = self.open_stream(url, referer=referer, offset=offset)
        try:
            total = expect_size
            if offset and response.status_code == 200:
                offset = 0
                dest.unlink(missing_ok=True)
            if not total:
                length = int(response.headers.get("Content-Length") or 0)
                total = length + offset
            mode = "ab" if offset else "wb"
            done = offset
            with open(dest, mode) as handle:
                for chunk in response.iter_content(1 << 20):
                    if should_cancel and should_cancel():
                        raise BaiduError("任务已取消")
                    if not chunk:
                        continue
                    handle.write(chunk)
                    done += len(chunk)
                    if progress:
                        progress(done, total)
        finally:
            response.close()
        return dest


@dataclass
class BaiduShare:
    """一个已解析的百度分享。"""

    surl: str
    share_id: str = ""
    share_uk: str = ""
    title: str = ""
    require_password: bool = False
    raw_html: str = ""

    @property
    def url(self) -> str:
        return f"{PAN}/s/1{self.surl}" if not self.surl.startswith("1") else f"{PAN}/s/{self.surl}"

    def raw_dir_url(self, directory: str = "/") -> str:
        return f"{self.url}?pwd=#list/path={quote(directory)}"
