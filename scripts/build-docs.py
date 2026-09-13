"""把仓库根目录下的 FEATURES.md / RELEASE_NOTES.md 生成到 quarkrelay/docs_content.py。

「关于」页要显示完整功能清单和本版更新公告，但打包成单文件 exe 之后读不到 md 文件，
所以这里在打包前把两份文档内联成一个 Python 模块。

用法：
    python scripts/build-docs.py          # 生成（内容没变就不写文件）
    python scripts/build-docs.py --check  # 只校验是否同步，未同步时退出码 1
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "quarkrelay" / "docs_content.py"

SOURCES = {
    "FEATURES": ROOT / "FEATURES.md",
    "RELEASE_NOTES": ROOT / "RELEASE_NOTES.md",
}

HEADER = '''"""自动生成，请勿手改 —— 由 scripts/build-docs.py 从 FEATURES.md / RELEASE_NOTES.md 生成。

改文案请改仓库根目录的那两个 .md 文件，然后重新运行：
    python scripts/build-docs.py
"""

'''

FOOTER = '''

__all__ = ["FEATURES", "RELEASE_NOTES"]
'''


def _literal(text: str) -> str:
    """把一个字符串转成安全的 Python 字面量（统一为 \\n，避免三引号冲突）。"""
    return repr(text.rstrip("\n") + "\n")


def render() -> str:
    parts = [HEADER]
    for name, path in SOURCES.items():
        if not path.exists():
            raise SystemExit(f"缺少文档文件：{path}")
        text = path.read_text(encoding="utf-8")
        if not text.strip():
            raise SystemExit(f"文档是空的：{path}")
        # 去掉每行行尾空白，避免 md 与生成物之间产生无意义的 diff
        cleaned = "\n".join(line.rstrip() for line in text.replace("\r\n", "\n").split("\n"))
        parts.append(f"{name} = {_literal(cleaned)}\n\n")
    parts.append(FOOTER.lstrip("\n"))
    return "".join(parts)


def main(argv: list[str]) -> int:
    check_only = "--check" in argv
    content = render()

    if TARGET.exists():
        current = TARGET.read_text(encoding="utf-8")
        if current == content:
            print(f"已是最新：{TARGET.relative_to(ROOT)}")
            return 0
        if check_only:
            print(f"[不同步] {TARGET.relative_to(ROOT)} 与 md 文件不一致，请运行 scripts/build-docs.py")
            return 1
    elif check_only:
        print(f"[缺失] {TARGET.relative_to(ROOT)} 不存在")
        return 1

    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(content, encoding="utf-8")
    print(f"已生成：{TARGET.relative_to(ROOT)}（{len(content)} 字节）")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
