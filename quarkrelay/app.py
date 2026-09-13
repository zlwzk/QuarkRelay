"""程序入口：初始化 Qt、日志、单实例、服务与主窗口。"""

from __future__ import annotations

import ctypes
import logging
import os
import sys
import traceback
from pathlib import Path

from . import __app_name__, __version__
from .logging_setup import setup as setup_logging
from .paths import LOG_DIR, ensure_dirs, is_frozen

logger = logging.getLogger(__name__)


def _safe_print(*parts: object) -> None:
    """窗口化 exe 里 stdout 可能根本不可用，打印失败绝不能影响退出码。"""
    try:
        print(*parts)
    except (OSError, ValueError):
        pass


def _finish(code: int) -> int:
    """结束程序。

    打包成 exe 之后，解释器收尾阶段（销毁 Qt 对象、卸载 DLL）偶尔会直接崩掉
    （退出码 0xC0000005，ACCESS_VIOLATION），用户会看到「程序已停止工作」。
    数据早就落盘了，所以这里把日志刷干净后直接结束进程，绕开那段不可控的收尾。
    """
    if not is_frozen():
        return code
    try:
        logging.shutdown()
    except Exception:  # noqa: BLE001
        pass
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is not None:
                stream.flush()
        except Exception:  # noqa: BLE001
            pass
    os._exit(code)


def _prepare_windows() -> None:
    if not sys.platform.startswith("win"):
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("zlwzk.QuarkRelay.Desktop")
    except Exception:  # noqa: BLE001
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # Per-Monitor DPI Aware
    except Exception:  # noqa: BLE001
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:  # noqa: BLE001
            pass


def _install_excepthook() -> None:
    def _hook(exc_type, exc_value, exc_tb) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            return
        text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        logger.error("未捕获异常：\n%s", text)
        try:
            from PySide6.QtWidgets import QApplication, QMessageBox

            if QApplication.instance() is not None:
                QMessageBox.critical(
                    None,
                    f"{__app_name__} 出错了",
                    f"程序遇到一个未处理的问题：\n\n{exc_value}\n\n详细日志见：\n{LOG_DIR}",
                )
        except Exception:  # noqa: BLE001
            pass

    sys.excepthook = _hook


class SingleInstance:
    """用命名互斥体保证只开一个窗口。"""

    def __init__(self, name: str = "QuarkRelay.SingleInstance") -> None:
        self.handle = None
        if not sys.platform.startswith("win"):
            return
        try:
            self.handle = ctypes.windll.kernel32.CreateMutexW(None, False, name)
            self.already_running = ctypes.windll.kernel32.GetLastError() == 183
        except Exception:  # noqa: BLE001
            self.already_running = False

    def release(self) -> None:
        if self.handle:
            try:
                ctypes.windll.kernel32.CloseHandle(self.handle)
            except Exception:  # noqa: BLE001
                pass


def self_check() -> int:
    """离屏自检：装配一次界面与内置浏览器，把结果写成 JSON。

    打包出来的 exe 是 GUI 程序，控制台输出不可靠，所以报告写文件。
    主要用来确认「QtWebEngine 在打包后还能不能用」这件事 —— 内置登录全靠它。
    """
    import json
    import tempfile
    import time

    from .paths import is_frozen

    report_path = Path(tempfile.gettempdir()) / "quarkrelay-selfcheck.json"
    report: dict[str, object] = {
        "app": __app_name__,
        "version": __version__,
        "python": sys.version.split()[0],
        "frozen": is_frozen(),
        "ok": False,
        "checks": {},
        "problems": [],
    }
    checks: dict[str, object] = report["checks"]  # type: ignore[assignment]
    problems: list[str] = report["problems"]  # type: ignore[assignment]

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("QT_LOGGING_RULES", "qt.qpa.*=false")
    # 自检不该联网查版本，否则报告要等几十秒
    os.environ["QUARKRELAY_SELFCHECK"] = "1"

    qt_messages: list[str] = []

    def _collect(_mode, _context, message) -> None:  # noqa: ANN001
        qt_messages.append(str(message))

    from PySide6.QtCore import qInstallMessageHandler
    from PySide6.QtWidgets import QApplication

    qInstallMessageHandler(_collect)
    app = QApplication(sys.argv[:1])

    try:
        from .ui.theme import PALETTES, apply_theme
        from .ui.widgets import set_palette

        palette = apply_theme(app, "dark")
        set_palette(PALETTES.get("dark", palette))
        checks["qss"] = len(app.styleSheet())
    except Exception as exc:  # noqa: BLE001
        problems.append(f"主题装配失败：{exc}")

    try:
        from .ui.main_window import MainWindow
        from .ui.services import AppServices

        services = AppServices()
        window = MainWindow(services, theme="dark")
        window.show()
        app.processEvents()
        checks["pages"] = window.stack.count()
        window.close()
        services.shutdown()
    except Exception as exc:  # noqa: BLE001
        problems.append(f"界面装配失败：{exc}")

    # 内置浏览器：真正渲染一页 HTML，能拿到 loadFinished 才说明进程起来了
    try:
        from PySide6.QtWebEngineCore import QWebEngineProfile
        from PySide6.QtWebEngineWidgets import QWebEngineView

        checks["profile"] = QWebEngineProfile.defaultProfile().persistentStoragePath() != ""
        view = QWebEngineView()
        loaded: list[bool] = []
        view.loadFinished.connect(lambda ok: loaded.append(bool(ok)))
        view.setHtml("<h1>QuarkRelay self-check</h1>")
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not loaded:
            app.processEvents()
            time.sleep(0.05)
        if not loaded:
            problems.append("内置浏览器没有在 20 秒内完成渲染（WebEngine 可能没打包完整）")
        elif not loaded[0]:
            problems.append("内置浏览器渲染失败")
        else:
            checks["webengine"] = "render-ok"
        view.deleteLater()
        app.processEvents()
    except Exception as exc:  # noqa: BLE001
        problems.append(f"内置浏览器初始化失败：{exc}")

    # 登录二维码面板：渲染一枚 SVG 再贴到白底卡片上。
    # 顺带守住 PySide6.QtSvg —— 夸克的二维码是矢量的，这个模块没被 PyInstaller
    # 收进来的话，用户只会看到「二维码取到了但没解析成功」，很难查。
    try:
        from .ui.qr import CARD_SIZE, card_pixmap, image_from_payload

        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 21 21">'
            '<rect width="21" height="21" fill="#ffffff"/>'
            '<rect width="7" height="7" fill="#000000"/>'
            "</svg>"
        )
        painted = image_from_payload({"kind": "svg", "value": svg}, CARD_SIZE)
        if painted is None:
            problems.append("二维码 SVG 渲染失败（PySide6.QtSvg 可能没打包进来）")
        elif card_pixmap(painted, CARD_SIZE, 1.0).width() != CARD_SIZE:
            problems.append("二维码卡片尺寸不对")
        else:
            checks["qr_panel"] = f"svg {painted.width()}px → 卡片 {CARD_SIZE}px"
    except Exception as exc:  # noqa: BLE001
        problems.append(f"二维码面板初始化失败：{exc}")

    noisy = [m for m in qt_messages if "WebEngine" in m or "webengine" in m]
    if noisy:
        checks["qt_messages"] = noisy[:5]
    report["ok"] = not problems
    text = json.dumps(report, ensure_ascii=False, indent=2)
    try:
        report_path.write_text(text, encoding="utf-8")
    except OSError as exc:
        _safe_print(f"写自检报告失败：{exc}")
        return 1

    _safe_print(f"自检报告：{report_path}")
    _safe_print(text)
    return _finish(0 if not problems else 1)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--version" in argv or "-v" in argv:
        _safe_print(f"{__app_name__} v{__version__}")
        return 0
    if "--check" in argv:
        return self_check()
    if "--help" in argv or "-h" in argv:
        _safe_print(
            f"{__app_name__} v{__version__}\n"
            "用法：QuarkRelay.exe [--help] [--version] [--check]\n"
            "  --check    离屏自检（装配界面与内置浏览器），结果写入 %TEMP%\\quarkrelay-selfcheck.json\n"
            "所有功能都在图形界面里，平时直接双击运行即可。"
        )
        return 0

    os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")
    _prepare_windows()
    ensure_dirs()

    # 更新后的收尾：上一版留下的 exe 备份、下载下来的安装包都在这里清掉。
    # 替换脚本正常已经清过一遍，这里是「脚本没跑完（被强杀 / 替换失败）」时的兜底。
    from .core import updater

    cleanup = updater.CleanupReport()
    try:
        cleanup = updater.cleanup_after_update()
    except Exception:  # noqa: BLE001 - 清理失败绝不能挡住启动
        logger.exception("清理更新残留失败")

    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtWidgets import QApplication

    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts, True)

    instance = SingleInstance()
    if instance.already_running:
        logger.info("已经有一个 %s 在运行", __app_name__)

    app = QApplication(sys.argv)
    app.setApplicationName(__app_name__)
    app.setApplicationDisplayName(__app_name__)
    app.setOrganizationName("zlwzk")
    app.setApplicationVersion(__version__)
    app.setQuitOnLastWindowClosed(False)

    from .config import config
    from .ui.theme import PALETTES, app_icon, apply_theme
    from .ui.widgets import set_palette

    cfg = config()
    setup_logging(str(cfg.get("app.log_level", "INFO")))
    _install_excepthook()

    theme_name = str(cfg.get("app.theme", "dark") or "dark")
    palette = apply_theme(app, theme_name)
    set_palette(PALETTES.get(theme_name, palette))
    app.setWindowIcon(app_icon(256))

    if cfg.get("app.first_run", True):
        cfg.set("app.first_run", False)
    cfg.set("app.last_version", __version__)

    from .ui.main_window import MainWindow
    from .ui.services import AppServices

    services = AppServices()
    window = MainWindow(services, theme=theme_name)
    window.show()

    if cleanup:
        logger.info("%s", cleanup.summary)
        QTimer.singleShot(1200, lambda: services.toast.emit(cleanup.summary, "info"))

    code = app.exec()
    services.shutdown()
    instance.release()
    return _finish(code)
