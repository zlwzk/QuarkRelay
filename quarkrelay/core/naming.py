"""命名模板：把「名字 + 链接」按用户模板拼成最终可复制的文本。"""

from __future__ import annotations

import re
import time
from collections.abc import Iterable

from ..config import config

DEFAULT_TEMPLATE = "{name} {link}"
FALLBACK_NAME = "未命名分享"

_VAR_RE = re.compile(r"\{([a-zA-Z_]+)(?::(\d+))?\}")


def build_name(
    raw: str,
    *,
    prefix: str = "",
    suffix: str = "",
    index: int | None = None,
    max_len: int = 60,
) -> str:
    """清洗一个文件名/标题，作为分享名字。"""
    name = (raw or "").strip()
    name = re.sub(r"[\r\n\t]+", " ", name)
    name = re.sub(r"[\\/:*?\"<>|]", " ", name)
    name = re.sub(r"\s{2,}", " ", name).strip(" .")
    if prefix:
        name = f"{prefix}{name}"
    if suffix:
        name = f"{name}{suffix}"
    if index is not None:
        name = f"{name}"
    if not name:
        return FALLBACK_NAME
    if len(name) > max_len:
        name = name[:max_len].rstrip(" .")
    return name


def render(
    template: str,
    *,
    name: str,
    link: str = "",
    code: str = "",
    index: int | None = None,
    extra: dict[str, str] | None = None,
) -> str:
    """按模板渲染最终文本；未知变量原样保留会被替换成空串。"""
    values = {
        "name": name or FALLBACK_NAME,
        "link": link or "",
        "code": code or "",
        "url": link or "",
        "index": str(index) if index is not None else "",
        "date": time.strftime("%Y-%m-%d"),
        "time": time.strftime("%H:%M:%S"),
        "provider": (extra or {}).get("provider", ""),
    }
    if extra:
        values.update({k: str(v) for k, v in extra.items()})

    def _sub(match: re.Match[str]) -> str:
        key = match.group(1)
        width = match.group(2)
        value = values.get(key)
        if value is None:
            return ""
        if width:
            return value[: int(width)]
        return value

    text = _VAR_RE.sub(_sub, template or DEFAULT_TEMPLATE)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def render_for(name: str, link: str, code: str = "", index: int | None = None) -> str:
    """用当前配置的模板渲染，并对「名字+链接」这种默认场景做贴心的空行清理。"""
    template = config().get("quark.naming_template") or DEFAULT_TEMPLATE
    return render(template, name=name, link=link, code=code, index=index)


def render_many(
    items: Iterable[tuple[str, str, str]], template: str | None = None
) -> str:
    """批量渲染，返回多行文本。items = [(name, link, code), ...]"""
    lines = []
    for index, (name, link, code) in enumerate(items, start=1):
        if template:
            lines.append(render(template, name=name, link=link, code=code, index=index))
        else:
            lines.append(render_for(name, link, code, index=index))
    return "\n".join(lines)


def code_suffix(code: str) -> str:
    return f" 提取码：{code}" if code else ""
