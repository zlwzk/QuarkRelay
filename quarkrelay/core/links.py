"""链接识别引擎：从任意文本里提取网盘分享链接、提取码，并归一化成结构化对象。

覆盖夸克、百度、阿里云盘、123 云盘、迅雷、天翼、115 等常见形态；对提取码做
「上下文就近匹配 + 常见前缀」两路启发式，尽量把复制过来的一坨文字拆干净。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlparse

URL_RE = re.compile(
    r"""(?xi)
    (?:
        https?://[^\s"'<>()\[\]{}，。、；：！？（）【】\u4e00-\u9fff]+   # 常规 URL
        |
        (?:pan|www)\.(?:quark|baidu|aliyundrive|alipan|123pan|cloud\.189|xunlei|115)\.(?:cn|com)
        /[^\s"'<>()\[\]{}，。、；：！？（）【】\u4e00-\u9fff]*
    )
    """
)

_CODE_PATTERNS = [
    re.compile(r"(?:提取码|提取碼|密码|密碼|访问码|訪問碼|pwd|密码|code)\s*[:：=]?\s*([A-Za-z0-9]{4,8})", re.I),
    re.compile(r"(?:[?&]pwd=)([A-Za-z0-9]{4,8})", re.I),
    re.compile(r"(?:[?&]password=)([A-Za-z0-9]{4,8})", re.I),
    re.compile(r"（\s*([A-Za-z0-9]{4})\s*）"),
]

_TRAILING_JUNK = "。，、；：！？）】》\"'\u201d\u2019.,;:!?)]}>"


@dataclass
class ShareLink:
    """一条被识别出来的分享链接。"""

    url: str
    provider: str = "unknown"
    code: str = ""
    raw: str = ""
    title: str = ""
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def provider_name(self) -> str:
        return PROVIDER_NAMES.get(self.provider, self.provider or "未知")

    @property
    def pwd_id(self) -> str:
        return self.extra.get("pwd_id", "")

    @property
    def surl(self) -> str:
        return self.extra.get("surl", "")

    def display(self) -> str:
        code = f"（提取码：{self.code}）" if self.code else ""
        return f"[{self.provider_name}] {self.url}{code}"


PROVIDER_NAMES = {
    "quark": "夸克网盘",
    "baidu": "百度网盘",
    "aliyun": "阿里云盘",
    "pan123": "123云盘",
    "tianyi": "天翼云盘",
    "xunlei": "迅雷云盘",
    "115": "115网盘",
    "uc": "UC网盘",
    "unknown": "未知",
}


def _clean_url(url: str) -> str:
    url = url.strip().strip(_TRAILING_JUNK)
    while url and url[-1] in _TRAILING_JUNK:
        url = url[:-1]
    if not url.lower().startswith(("http://", "https://")):
        url = "https://" + url
    return url


def classify(url: str) -> tuple[str, dict[str, str]]:
    """返回 (provider, extra)。"""
    try:
        parsed = urlparse(url)
    except ValueError:
        return "unknown", {}
    host = (parsed.netloc or "").lower()
    path = parsed.path or ""
    query = parse_qs(parsed.query or "")
    extra: dict[str, str] = {}

    if "quark" in host or "uc.cn" in host:
        provider = "quark" if "quark" in host else "uc"
        match = re.search(r"/s/([A-Za-z0-9_-]+)", path)
        if match:
            extra["pwd_id"] = match.group(1)
        if query.get("pwd"):
            extra["pwd"] = query["pwd"][0]
        return provider, extra

    if "baidu" in host or host.endswith("baidu.com") or "pan.baidu" in host:
        extra["surl"] = ""
        match = re.search(r"/s/(?:1)?([A-Za-z0-9_-]+)", path)
        if match:
            extra["surl"] = match.group(1)
        if not extra["surl"]:
            match = re.search(r"[?&]surl=([A-Za-z0-9_-]+)", parsed.query or "")
            if match:
                extra["surl"] = match.group(1)
        if query.get("pwd"):
            extra["pwd"] = query["pwd"][0]
        return "baidu", extra

    if "aliyundrive" in host or "alipan" in host:
        match = re.search(r"/s/([A-Za-z0-9]+)", path)
        if match:
            extra["share_id"] = match.group(1)
        return "aliyun", extra

    if "123pan" in host or "123684" in host or "123865" in host:
        match = re.search(r"/s/([A-Za-z0-9_-]+)", path)
        if match:
            extra["share_key"] = match.group(1)
        return "pan123", extra

    if "cloud.189" in host:
        match = re.search(r"/t/([A-Za-z0-9]+)", path)
        if match:
            extra["share_code"] = match.group(1)
        if query.get("code") or query.get("pwd"):
            extra["pwd"] = (query.get("code") or query.get("pwd"))[0]
        return "tianyi", extra

    if "xunlei" in host:
        match = re.search(r"/s/([A-Za-z0-9_-]+)", path)
        if match:
            extra["share_id"] = match.group(1)
        return "xunlei", extra

    if "115" in host:
        match = re.search(r"/s/([A-Za-z0-9]+)", path)
        if match:
            extra["share_code"] = match.group(1)
        return "115", extra

    return "unknown", extra


def find_code(text: str, url_span: tuple[int, int] | None = None) -> str:
    """在一段文本里找提取码；优先找 URL 附近的。"""
    if not text:
        return ""
    if url_span is not None:
        start, end = url_span
        window = text[max(0, start - 60) : end + 60]
        for pattern in _CODE_PATTERNS:
            match = pattern.search(window)
            if match:
                return match.group(1)
    for pattern in _CODE_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(1)
    return ""


def extract(text: str) -> list[ShareLink]:
    """从任意文本里提取所有分享链接（去重、保序）。"""
    if not text:
        return []
    results: list[ShareLink] = []
    seen: set[str] = set()
    for match in URL_RE.finditer(text):
        url = _clean_url(match.group(0))
        provider, extra = classify(url)
        if provider == "unknown" and "pan." not in url:
            continue
        key = url.rstrip("/")
        if key in seen:
            continue
        seen.add(key)
        code = extra.get("pwd", "") or find_code(text, match.span())
        results.append(ShareLink(url=url, provider=provider, code=code, raw=text, extra=extra))
    return results


def first(text: str, provider: str | None = None) -> ShareLink | None:
    for link in extract(text):
        if provider is None or link.provider == provider:
            return link
    return None


def detect_provider(text: str) -> str:
    link = first(text)
    return link.provider if link else "unknown"
