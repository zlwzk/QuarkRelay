"""把程序图标（代码里画的）导出成 build/app.ico，供 PyInstaller 打包时使用。

图标本身来自 quarkrelay/ui/theme.py 的 app_icon()，这里只是把它渲染成多尺寸
ICO。ICO 容器里直接塞 PNG（Vista 以后都支持），所以不需要额外装 Pillow。

用法：python scripts/make-icon.py
"""

from __future__ import annotations

import os
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SIZES = (16, 24, 32, 48, 64, 128, 256)


def _png_bytes(icon, size: int) -> bytes:
    from PySide6.QtCore import QBuffer, QIODevice

    pixmap = icon.pixmap(size, size)
    if pixmap.isNull():
        raise SystemExit(f"图标渲染失败（{size}px）")
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    if not pixmap.save(buffer, "PNG"):
        raise SystemExit(f"图标编码失败（{size}px）")
    return bytes(buffer.data())


def build_ico(target: Path) -> int:
    from PySide6.QtGui import QIcon

    sys.path.insert(0, str(ROOT))
    from quarkrelay.ui.theme import app_icon

    icon: QIcon = app_icon(256)
    images = [(size, _png_bytes(icon, size)) for size in SIZES]

    directory = struct.pack("<HHH", 0, 1, len(images))
    offset = len(directory) + 16 * len(images)
    entries = b""
    payload = b""
    for size, data in images:
        entries += struct.pack(
            "<BBBBHHII",
            0 if size >= 256 else size,
            0 if size >= 256 else size,
            0,
            0,
            1,
            32,
            len(data),
            offset,
        )
        offset += len(data)
        payload += data

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(directory + entries + payload)
    return len(images)


def main() -> int:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtGui import QGuiApplication

    app = QGuiApplication.instance() or QGuiApplication(sys.argv[:1])
    target = ROOT / "build" / "app.ico"
    count = build_ico(target)
    del app
    print(f"已生成 {target.relative_to(ROOT)}（{count} 个尺寸，{target.stat().st_size} 字节）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
