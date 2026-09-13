"""轻量自检：不联网、不弹窗，只验证核心逻辑与界面能不能正常装配。

运行：
    python scripts/selftest.py           # 含离屏界面冒烟测试
    python scripts/selftest.py --no-ui   # 只测核心逻辑（无 Qt 环境时用）

所有测试都跑在临时目录里（QUARKRELAY_HOME 指向 %TEMP%），不会碰你 %APPDATA% 下的真实数据。
"""

from __future__ import annotations

import compileall
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

_RESULTS: list[tuple[str, bool, str]] = []


def check(name: str):
    """把一个返回说明文字、抛异常即失败的操作包成测试用例。"""

    def decorator(func):
        def runner() -> None:
            try:
                detail = func() or ""
                _RESULTS.append((name, True, str(detail)))
            except Exception as exc:  # noqa: BLE001
                _RESULTS.append((name, False, f"{type(exc).__name__}: {exc}"))

        runner.__name__ = func.__name__
        runner.check_name = name  # type: ignore[attr-defined]
        return runner

    return decorator


@check("源码可编译（compileall）")
def test_compile() -> str:
    ok = compileall.compile_dir(str(ROOT / "quarkrelay"), quiet=1, force=True, legacy=False)
    assert ok, "存在语法错误的文件"
    return "全部通过"


@check("文档与 docs_content.py 同步")
def test_docs_sync() -> str:
    import importlib.util

    from quarkrelay import __version__

    target = ROOT / "quarkrelay" / "docs_content.py"
    assert target.exists(), "docs_content.py 不存在，请先运行 scripts/build-docs.py"
    spec = importlib.util.spec_from_file_location("_docs_content_check", target)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    features = getattr(module, "FEATURES", "")
    notes = getattr(module, "RELEASE_NOTES", "")
    assert len(features) > 500, "FEATURES 内容过短"
    assert len(notes) > 500, "RELEASE_NOTES 内容过短"
    assert __version__ in notes, f"公告里没提到当前版本号 v{__version__}"
    return f"功能清单 {len(features)} 字符 / 公告 {len(notes)} 字符"


@check("链接识别：夸克 / 百度 / 提取码")
def test_links() -> str:
    from quarkrelay.core.links import extract

    text = (
        "【资源】随手分享一份 https://pan.quark.cn/s/7f8a9b0c1d2e?pwd=ab12 提取码：ab12\n"
        "百度：https://pan.baidu.com/s/1AbCdEfGhIjK 密码 3x9k\n"
        "123云盘 https://www.123pan.com/s/abcd-efgH\n"
        "重复一条 https://pan.quark.cn/s/7f8a9b0c1d2e\n"
        "不是链接的 http://example.com/page\n"
    )
    links = extract(text)
    providers = sorted({link.provider for link in links})
    assert any(link.provider == "quark" for link in links), "没识别出夸克链接"
    assert any(link.provider == "baidu" for link in links), "没识别出百度链接"
    assert any(link.provider == "pan123" for link in links), "没识别出 123 云盘链接"
    quark = next(link for link in links if link.provider == "quark")
    assert quark.code == "ab12", f"提取码不对：{quark.code!r}"
    assert not any("example.com" in link.url for link in links), "误识别了普通网址"
    assert len([l for l in links if l.provider == "quark"]) == 2, "同链接不同行应各自保留"
    return f"{len(links)} 条 / 平台 {providers}"


@check("命名模板渲染")
def test_naming() -> str:
    from quarkrelay.core.naming import DEFAULT_TEMPLATE, build_name, render, render_many

    out = render(
        "{index}.{name} | {link} | {code} | {date} | {size:3}",
        name="电影合集",
        link="https://pan.quark.cn/s/xyz",
        code="ab12",
        index=2,
        extra={"size": "1234567"},
    )
    expect = f"2.电影合集 | https://pan.quark.cn/s/xyz | ab12 | {time.strftime('%Y-%m-%d')} | 123"
    assert out == expect, out
    assert render("{name}", name="只有名字") == "只有名字"
    assert render("x{不合法}", name="n") == "x{不合法}", "未知变量应原样保留"

    many = render_many([("A", "L1", ""), ("B", "L2", "c2")], template="{name} {link}")
    assert many == "A L1\nB L2", repr(many)

    cleaned = build_name('  坏/名 字*  ?.  ')
    assert "/" not in cleaned and "*" not in cleaned and cleaned.strip() == cleaned, cleaned
    assert build_name("") != ""
    assert DEFAULT_TEMPLATE.count("{link}") == 1
    return f"模板渲染正确，非法字符已清洗：{cleaned!r}"


@check("体积 / 速度 / 时长格式化")
def test_net() -> str:
    from quarkrelay.core.net import human_duration, human_size, human_speed

    assert human_size(1024 ** 3).endswith("GB"), human_size(1024 ** 3)
    assert human_size(0) == "0 B", human_size(0)
    assert human_speed(1536).endswith("/s")
    assert human_duration(3725) == "1 时 2 分", human_duration(3725)
    return f"{human_size(1536)} · {human_speed(1536)} · {human_duration(3725)}"


@check("配置读写")
def test_config() -> str:
    from quarkrelay.config import config
    from quarkrelay.paths import CONFIG_FILE

    cfg = config()
    cfg.set("selftest.marker", "hello")
    assert cfg.get("selftest.marker") == "hello", "配置写入后读不到"
    cfg.update({"selftest.b": 1})
    assert cfg.get("selftest.b") == 1
    assert cfg.get("根本不存在的键", "默认值") == "默认值"
    return f"读写正常（{CONFIG_FILE.name}）"


@check("历史记录（SQLite）")
def test_store() -> str:
    from quarkrelay.store import history

    store = history()
    store.add(kind="quark", name="自检条目", link="https://pan.quark.cn/s/selftest", source="quark")
    rows = store.list(keyword="自检条目", limit=10)
    assert rows, "写进去的历史读不回来"
    assert rows[0].name == "自检条目", rows[0].name
    assert rows[0].combined.startswith("自检条目 https://"), rows[0].combined
    csv_path = ROOT / "build" / "selftest-history.csv"
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    count = store.export_csv(csv_path)
    assert csv_path.exists() and csv_path.stat().st_size > 0, "导出 CSV 失败"
    csv_path.unlink(missing_ok=True)
    return f"{count} 条已导出 CSV"


@check("任务中心：线程池调度与状态流转")
def test_tasks() -> str:
    from PySide6.QtCore import QCoreApplication

    from quarkrelay.core.tasks import TaskCenter, TaskStatus

    app = QCoreApplication.instance()
    center = TaskCenter(max_workers=2, parent=None)
    seen: list[str] = []

    def worker(task) -> dict:
        seen.append(task.title)
        return {"ok": True}

    task = center.submit("selftest", "自检任务", worker)
    deadline = time.time() + 15
    while task.status.active and time.time() < deadline:
        if app is not None:
            app.processEvents()
        time.sleep(0.05)
    assert not task.status.active, "任务没有在 15 秒内结束"
    assert task.status == TaskStatus.SUCCESS, f"{task.status} / {task.error}"
    assert seen == ["自检任务"], seen
    assert task.progress == 100.0, task.progress
    assert center.active_count() == 0
    assert center.clear_finished() == 1
    center.shutdown()
    return "提交 / 执行 / 完成 / 清理 全通"


@check("关窗即退出（进程不能赖在后台）")
def test_close_quits() -> str:
    import subprocess
    import textwrap

    # 子进程里真跑一次事件循环：关掉主窗口后，app.exec() 必须返回。
    # 程序设了 setQuitOnLastWindowClosed(False) 又建了托盘图标，很容易出现
    # 「窗口没了、进程还在」，所以这条要单独守着。
    script = textwrap.dedent(
        """
        import os, sys
        os.environ['QT_QPA_PLATFORM'] = 'offscreen'
        sys.path.insert(0, os.getcwd())
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QApplication

        from quarkrelay.config import config
        config().set('app.close_to_tray', False)

        app = QApplication([])
        from quarkrelay.ui.theme import PALETTES, apply_theme
        from quarkrelay.ui.widgets import set_palette
        apply_theme(app, 'dark')
        set_palette(PALETTES['dark'])

        from quarkrelay.ui.main_window import MainWindow
        from quarkrelay.ui.services import AppServices
        services = AppServices()
        window = MainWindow(services, theme='dark')
        window.show()
        QTimer.singleShot(800, window.close)
        QTimer.singleShot(15000, lambda: os._exit(97))
        code = app.exec()
        services.shutdown()
        os._exit(code)
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(ROOT),
        capture_output=True,
        timeout=90,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if proc.returncode == 97:
        raise AssertionError("关窗后事件循环没有退出（进程会一直赖在后台）")
    assert proc.returncode == 0, (
        f"退出码 {proc.returncode}：{proc.stderr.decode('utf-8', 'ignore')[-400:]}"
    )
    return "事件循环正常退出"


@check("界面装配（离屏）")
def test_ui() -> str:
    from PySide6.QtWidgets import QApplication

    from quarkrelay.ui.main_window import MainWindow
    from quarkrelay.ui.pages.settings import TEMPLATE_PRESETS
    from quarkrelay.ui.services import AppServices
    from quarkrelay.ui.theme import PALETTES, apply_theme

    app = QApplication.instance()
    assert isinstance(app, QApplication), "没有可用的 QApplication"
    palette = apply_theme(app, "dark")
    assert palette is not None

    from quarkrelay.ui.widgets import set_palette

    set_palette(PALETTES.get("dark", palette))
    assert len(TEMPLATE_PRESETS) >= 5, "命名预设不足五种"

    services = AppServices()
    window = MainWindow(services, theme="dark")
    pages = list(window._pages)  # noqa: SLF001 - 自检里直接看内部结构
    assert len(pages) >= 8, f"页面数量不对：{pages}"
    for key in ("transfer", "screenshot", "baidu", "tasks", "history", "accounts", "settings", "about"):
        assert key in pages, f"缺少页面 {key}"
    window.show()
    window.navigate("about")
    app.processEvents()
    window.close()
    services.shutdown()
    return f"{len(pages)} 个页面已装配"


def main(argv: list[str]) -> int:
    tmp = Path(tempfile.mkdtemp(prefix="quarkrelay-selftest-"))
    os.environ["QUARKRELAY_HOME"] = str(tmp)
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("QT_LOGGING_RULES", "qt.qpa.*=false")
    sys.path.insert(0, str(ROOT))

    argv = list(argv)
    if "--no-ui" not in argv:
        from PySide6.QtWidgets import QApplication

        QApplication.instance() or QApplication(sys.argv[:1])

    tests = [test_compile, test_docs_sync, test_links, test_naming, test_net, test_config, test_store, test_tasks]
    if "--no-ui" not in argv:
        tests.append(test_close_quits)
        tests.append(test_ui)

    print(f"夸克中转站 自检 · 共 {len(tests)} 项\n" + "-" * 58)
    for test in tests:
        test()
        name, ok, detail = _RESULTS[-1]
        print(f"{'[通过]' if ok else '[失败]'} {name}" + (f" —— {detail}" if detail else ""))

    failed = [item for item in _RESULTS if not item[1]]
    print("-" * 58)
    print(f"{len(_RESULTS) - len(failed)}/{len(_RESULTS)} 项通过")
    for name, _, detail in failed:
        print(f"  未通过：{name} —— {detail}")

    shutil.rmtree(tmp, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
