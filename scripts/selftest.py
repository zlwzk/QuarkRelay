"""轻量自检：不弹窗，只验证核心逻辑与界面能不能正常装配。

运行：
    python scripts/selftest.py           # 含离屏界面冒烟测试
    python scripts/selftest.py --no-ui   # 只测核心逻辑（无 Qt 环境时用）

唯一会摸网络的是「检查更新」，而且只是尽力而为：连不上就按「离线降级」判通过。

所有测试都跑在临时目录里（QUARKRELAY_HOME 指向 %TEMP%），不会碰你 %APPDATA% 下的真实数据。
"""

from __future__ import annotations

import compileall
import os
import re
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
    assert len(pages) >= 7, f"页面数量不对：{pages}"
    for key in ("transfer", "baidu", "tasks", "history", "accounts", "settings", "about"):
        assert key in pages, f"缺少页面 {key}"
    assert "screenshot" not in pages, "截图识链不该再占一个独立页面"

    # 截图小按钮应该长在需要输入链接的两个页面上
    for key in ("transfer", "baidu"):
        assert getattr(window._pages[key], "shot", None) is not None, f"{key} 页没有截图按钮"

    # 跨盘搬运页的方向切换
    relay_page = window._pages["baidu"]
    relay_page.set_direction(1)
    assert "夸克分享链接" in relay_page.source_card.title_label.text(), "切到夸克方向后标题没变"
    assert relay_page.make_share.text().endswith("百度分享链接"), relay_page.make_share.text()
    relay_page.set_direction(0)
    assert "百度分享链接" in relay_page.source_card.title_label.text(), "切回百度方向后标题没变"

    window.show()
    window.navigate("about")
    app.processEvents()
    window.close()
    services.shutdown()
    return f"{len(pages)} 个页面已装配，双向搬运与截图按钮就位"


@check("登录二维码：抠图 → 静区 → 放大面板")
def test_qr_panel() -> str:
    import base64
    import json

    from PySide6.QtCore import QBuffer, QIODevice, QObject
    from PySide6.QtGui import QColor, QImage

    from quarkrelay.ui import qr

    # 造一枚「二维码」：黑白棋盘格，而且像真二维码一样紧贴边缘、不留静区。
    # 夸克和百度两家的二维码都是这么生成的，也正是直接截图扫不出来的原因。
    side = 25
    source = QImage(side, side, QImage.Format.Format_RGB32)
    for y in range(side):
        for x in range(side):
            tone = 255 if (x + y) % 2 == 0 else 0
            source.setPixelColor(x, y, QColor(tone, tone, tone))

    def data_url(image: QImage) -> str:
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        assert image.save(buffer, "PNG"), "测试用 PNG 生成失败"
        return "data:image/png;base64," + base64.b64encode(bytes(buffer.data())).decode("ascii")

    # 1) 位图：百度是 <img>，页面里经 canvas 转成 dataURL 交回来
    raster = qr.image_from_payload({"kind": "img", "value": data_url(source)}, 400)
    assert raster is not None, "dataURL 解码失败"
    assert raster.width() == side, raster.width()

    # 2) 矢量：夸克是 <svg>，由本地按目标尺寸光栅化（QtSvg 不能少）
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 21 21">'
        '<rect width="21" height="21" fill="#ffffff"/>'
        '<rect width="7" height="7" fill="#000000"/>'
        "</svg>"
    )
    vector = qr.image_from_payload({"kind": "svg", "value": svg}, 200)
    assert vector is not None, "SVG 渲染失败（QtSvg 没装上？）"
    assert vector.width() == 200 * qr.OVERSAMPLE, vector.width()

    # 3) 卡片：白底 + 四周静区。少了静区，正规扫码器会认不出来
    board = qr.card_pixmap(raster, 200, 1.0).toImage()
    assert (board.width(), board.height()) == (200, 200), board.size()
    margin = int(200 * qr.QUIET_RATIO) - 1
    for point in ((0, 0), (199, 0), (0, 199), (199, 199), (margin, margin), (100, 0)):
        assert board.pixelColor(*point).name() == "#ffffff", f"{point} 不是白的，静区不够"
    dark = sum(
        1
        for y in range(60, 140)
        for x in range(60, 140)
        if board.pixelColor(x, y).red() < 128
    )
    assert dark > 100, f"卡片里压根没有二维码图案（黑点只有 {dark} 个）"

    # 4) 面板 + 监视器：注入脚本 → 探测 → 取图 → 上屏，整条链路走一遍
    class StubPage(QObject):
        def __init__(self) -> None:
            super().__init__()
            self.grabs = 0

        def runJavaScript(self, script, callback=None):  # noqa: ANN001
            if callback is None:
                return
            if script == qr.PROBE_CALL:
                callback(json.dumps({"found": True, "kind": "img", "sig": "img:stub"}))
            elif script == qr.GRAB_CALL:
                self.grabs += 1
                callback(json.dumps({"found": True, "kind": "img", "value": data_url(source)}))
            elif script == qr.REFRESH_CALL:
                callback("click")

    panel = qr.QrPanel("用测试 App 扫码", on_refresh=lambda: None)
    page = StubPage()
    watcher = qr.QrWatcher(page, panel)
    watcher._tick()  # noqa: SLF001 - 第一轮：注入取码器
    watcher._tick()  # noqa: SLF001 - 第二轮：探测到二维码 → 取图 → 上屏
    assert page.grabs == 1, f"取图次数不对：{page.grabs}"
    drawn = panel.canvas.pixmap()
    assert drawn is not None and not drawn.isNull(), "面板上没有二维码"
    assert panel.canvas.width() == qr.CARD_SIZE, panel.canvas.width()
    watcher._tick()  # noqa: SLF001 - 同一枚二维码不该反复取图
    assert page.grabs == 1, "签名没变却又取了一次图"
    assert qr._loads("这不是 JSON") is None  # noqa: SLF001 - 回调可能拿到空串/脏数据

    # 5) 窗口尺寸：只看二维码时收窄到刚装得下卡片，展开登录页时才变宽
    from quarkrelay.ui.browser import WebLoginDialog

    narrow = WebLoginDialog._fit_size((1100, 720), "扫码", False)[0]  # noqa: SLF001
    wide = WebLoginDialog._fit_size((1100, 720), "扫码", True)[0]  # noqa: SLF001
    assert narrow < wide, (narrow, wide)
    assert narrow >= qr.CARD_SIZE + 100, narrow

    return (
        f"位图 {side}px / 矢量 {vector.width()}px / 卡片 {qr.CARD_SIZE}px（含静区）"
        f" / 窗口 {narrow}→{wide}px"
    )


@check("登录会话：cookie 库读取与同名冲突")
def test_login_session() -> str:
    import sqlite3

    from quarkrelay.core.baidu import BaiduClient
    from quarkrelay.ui.browser import cookie_string, read_persisted_cookies

    # 1) 同名 cookie 挂到多个域时，requests 读 `cookies.get("BDUSS")` 会抛
    #    CookieConflictError —— 表现就是「扫完码却说未登录」。必须只挂一个域。
    client = BaiduClient(cookies="BDUSS=fake-bduss; STOKEN=fake-stoken; BAIDUID=x")
    assert client.logged_in, "带 BDUSS 的会话应当算作已登录"
    assert client.export_cookies().count("BDUSS=") == 1
    assert {c.domain for c in client.session.cookies} == {".baidu.com"}
    assert client._cookie("BDUSS") == "fake-bduss"  # noqa: SLF001

    # 2) cookie 库要读得出来：`loadAllCookies()` 在新版 Qt 上一条都不回，
    #    冷启动时就靠这一手把已经躺在库里的会话捡回来。
    root = Path(tempfile.mkdtemp(prefix="qrck-"))
    try:
        now_us = int((time.time() + 11644473600) * 1_000_000)  # Chromium 纪元
        con = sqlite3.connect(root / "Cookies")
        con.execute(
            "create table cookies"
            " (host_key text, name text, value text, has_expires int, expires_utc int)"
        )
        con.executemany(
            "insert into cookies values (?,?,?,?,?)",
            [
                (".baidu.com", "BDUSS", "bduss-root", 0, 0),
                ("pan.baidu.com", "STOKEN", "stoken-pan", 1, now_us + 3600 * 10**6),
                (".baidu.com", "STOKEN", "stoken-root", 0, 0),
                (".baidu.com", "DEAD", "gone", 1, now_us - 3600 * 10**6),
                (".baidu.com", "EMPTY", "", 0, 0),
            ],
        )
        con.commit()
        con.close()

        class StubProfile:
            def persistentStoragePath(self) -> str:  # noqa: N802
                return str(root)

        got = read_persisted_cookies(StubProfile())  # type: ignore[arg-type]
        assert got["baidu.com"]["BDUSS"] == "bduss-root", got
        assert got["pan.baidu.com"]["STOKEN"] == "stoken-pan", got
        assert "DEAD" not in got["baidu.com"], "过期 cookie 不该被读出来"
        assert "EMPTY" not in got["baidu.com"], "空值 cookie 不该被读出来"

        # 3) 读不出来时要安静地退化，不能把登录流程带崩
        assert read_persisted_cookies(StubProfile()) is not None  # type: ignore[arg-type]
        assert cookie_string({"BDUSS": "x"}, ("BDUSS",)) == "BDUSS=x"
    finally:
        shutil.rmtree(root, ignore_errors=True)

    return f"域名 {sorted(got)} · 同名 cookie 只挂一个域"


@check("检查更新：版本比较与降级路径")
def test_updater() -> str:
    from pathlib import Path

    from quarkrelay import __version__
    from quarkrelay.core import updater

    assert updater.is_newer("1.10.0", "1.9.9"), "版本要按数字段比较，不能按字符串"
    assert not updater.is_newer("1.0.0", "1.0.0"), "同版本不该算有更新"
    assert not updater.is_newer("1.0.0", "1.1.0"), "更旧的版本不该算有更新"
    assert updater.is_newer("v2.0.0"), "带 v 前缀的 tag 也要认"

    assert updater.UpdateInfo().has_asset is False
    assert updater.latest_download_url().endswith("/releases/latest")

    # 自检跑在源码模式：必须优雅降级，而不是抛未知异常或真去替换自己
    assert updater.current_exe() is None, "自检不该在打包环境里跑"
    assert updater.can_self_update() is False
    for call in (
        lambda: updater.apply_update(Path("does-not-exist.exe")),
        lambda: updater.download(updater.UpdateInfo(version="9.9.9")),
    ):
        try:
            call()
        except updater.UpdateError as exc:
            assert str(exc), "错误信息不能为空"
        else:
            raise AssertionError("这些路径都应该抛 UpdateError")

    # 真跑一遍替换脚本：等进程退出 → 挪走旧文件 → 挪进新文件 → 脚本自清理。
    # 自动更新最容易出问题的就是这段批处理，所以不靠「看着像对」通过。
    import subprocess

    with tempfile.TemporaryDirectory(prefix="qr-update-") as work:
        root = Path(work)
        app_dir = root / "app"  # 程序目录与更新目录分开，跟真机上的布局一致
        app_dir.mkdir()
        target = app_dir / "app.exe"
        source = root / "new.exe"
        target.write_bytes(b"OLD-BUILD")
        source.write_bytes(b"NEW-BUILD")
        # 上一版留下的安装包、下到一半的半成品：替换脚本收尾时应该一并清掉
        stale_package = root / "QuarkRelay.exe"
        stale_part = root / "QuarkRelay.exe.part"
        stale_package.write_bytes(b"OLD-PACKAGE" * 100)
        stale_part.write_bytes(b"HALF")

        # 起一个两三秒后自己退出的进程，脚本里的「等 PID 结束」等的就是它
        sleeper = subprocess.Popen(
            ["cmd", "/c", "ping -n 3 127.0.0.1 >NUL"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        script = root / "apply.bat"
        updater._write_script(  # noqa: SLF001 - 这段批处理是自检的重点，必须直接验
            updater._render_script(target, source, sleeper.pid, restart=False, workdir=root),
            script,
        )
        # 必须按真实启动方式来跑（updater.STARTUP_FLAGS：隐藏控制台的 cmd）。
        # 脚本里刻意不用管道（a | b）：没有控制台的 cmd 里管道会永久挂住，
        # 曾经就是这么静悄悄卡住的。所以这里也不等它返回，而是轮询结果。
        subprocess.Popen(
            ["cmd", "/c", str(script)],
            cwd=str(script.parent),
            creationflags=updater.STARTUP_FLAGS,
            close_fds=True,
        )
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if not script.exists() and not source.exists():
                break
            time.sleep(0.3)
        sleeper.wait(timeout=30)
        assert target.read_bytes() == b"NEW-BUILD", (
            f"替换脚本没把新版本放到位，见 {root / 'update.log'}"
        )
        assert not source.exists(), "新版本文件应该已经挪走"
        assert not (app_dir / "app.exe.old").exists(), "旧版本备份应该被清掉"
        assert not script.exists(), "脚本应该删掉自己"
        assert not stale_package.exists(), "更新目录里留下的旧安装包应该被清掉"
        assert not stale_part.exists(), "下到一半的安装包应该被清掉"
        record = (root / updater.UPDATE_LOG_NAME).read_bytes().decode("mbcs", errors="replace")
        assert "替换完成" in record, f"替换脚本要留一条成功记录，实际是：{record!r}"

        # 替换脚本是模板生成的，静态校验一遍：别把流程跳到不存在的标签上，
        # 也别把「打开新版本」这一段弄丢（真启动会弹窗，自检里不实际跑这一步）
        boot = updater._render_script(target, source, 1, restart=True, workdir=root)
        labels = set(re.findall(r"^:(\w+)", boot, re.M))
        jumps = set(re.findall(r"goto (\w+)", boot))
        assert jumps <= labels, f"跳到了不存在的标签：{sorted(jumps - labels)}"
        assert 'start "" /d "%TARGETDIR%" "%TARGET%"' in boot, "要打开新版本"
        assert "IMAGENAME eq %TARGETNAME%" in boot, "打开之后要确认进程真的起来了"
        assert "没能自动打开" in boot, "起不来要留下一条记录"
        quiet = updater._render_script(target, source, 1, restart=False, workdir=root)
        assert "goto bye" in quiet and '\nstart ""' not in quiet, "不重启时不该打开程序"

    # 启动时的兜底清理：替换脚本没跑完（被强杀 / 替换失败）时留下的东西由它收拾
    with tempfile.TemporaryDirectory(prefix="qr-clean-") as work:
        root = Path(work)
        app = root / "QuarkRelay.exe"
        app.write_bytes(b"CURRENT")
        backup = root / "QuarkRelay.exe.old"  # 上一版替换完没删掉的旧文件
        backup.write_bytes(b"OLD" * 400)
        update_dir = root / "update"
        update_dir.mkdir()
        package = update_dir / "QuarkRelay.exe"  # 下载好却没装上的安装包
        package.write_bytes(b"NEW" * 400)
        part = update_dir / "QuarkRelay.exe.part"  # 下到一半的
        part.write_bytes(b"HALF")
        log = update_dir / updater.UPDATE_LOG_NAME
        log.write_text("留个记录", encoding="utf-8")

        report = updater.cleanup_after_update(exe=app, workdir=update_dir)
        assert not backup.exists(), "旧版本文件应该被删掉"
        assert not package.exists() and not part.exists(), "安装包与半成品应该被清掉"
        assert app.exists(), "当前版本不能被误删"
        assert log.exists(), "update.log 是记录，不能一起删"
        assert len(report.files) == 3 and report.freed > 0, report.files
        assert "项" in report.summary, "清理结果要能直接显示给用户"

        # 替换脚本还在收尾时（刚生成的）不能去动它的文件
        (update_dir / "apply-update.bat").write_text("@echo off\n", encoding="mbcs")
        pending = update_dir / "QuarkRelay.exe"
        pending.write_bytes(b"NEW")
        assert not updater.cleanup_after_update(exe=app, workdir=update_dir), (
            "替换脚本还在跑的时候不该清理"
        )
        assert pending.exists(), "进行中的更新包不能被删"

    # 真连一次 GitHub；断网就当作「离线降级」通过，自检不该因为没网而失败
    info = updater.check(timeout=8)
    if info.error:
        assert info.message.startswith("检查更新失败"), info.message
        return f"替换脚本与收尾清理跑通，离线降级正常（{info.error[:40]}）"
    return (
        f"替换脚本与收尾清理跑通 · GitHub 最新版 {info.version or '未知'} / "
        f"本地 v{__version__}：{info.message}"
    )


@check("自动更新：替换过程不弹控制台黑框")
def test_update_no_console_window() -> str:
    """更新脚本必须「有控制台，但窗口藏起来」。

    用 DETACHED_PROCESS（完全没有控制台）时，脚本里跑的 tasklist / find / ping
    会被系统各配一个新控制台窗口，更新时满屏黑框 —— v1.1.5 之前的实测问题。
    所以这里不只检查启动常量，还按真实方式跑一遍那几条命令，
    数一数屏幕上新冒出来几个可见的控制台窗口（应当一个都没有）。
    """
    import ctypes
    import subprocess
    from ctypes import wintypes

    from quarkrelay.core import updater

    assert updater.STARTUP_FLAGS & getattr(subprocess, "CREATE_NO_WINDOW", 0), (
        "更新脚本要用 CREATE_NO_WINDOW 拉起，否则更新时会弹出一堆控制台窗口"
    )
    assert not updater.STARTUP_FLAGS & getattr(subprocess, "DETACHED_PROCESS", 0), (
        "别用 DETACHED_PROCESS：它完全没有控制台，子命令会被系统各配一个新控制台窗口"
    )

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    callback = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

    def visible_consoles() -> set[int]:
        """屏幕上当前可见的控制台窗口（按句柄去重）。"""
        found: set[int] = set()

        def visit(hwnd, _) -> bool:
            if user32.IsWindowVisible(hwnd):
                name = ctypes.create_unicode_buffer(256)
                user32.GetClassNameW(hwnd, name, 256)
                if "consolewindow" in name.value.lower():
                    found.add(int(hwnd))
            return True

        user32.EnumWindows(callback(visit), 0)
        return found

    with tempfile.TemporaryDirectory(prefix="qr-console-") as work:
        root = Path(work)
        marker = root / "done.txt"
        script = root / "probe.bat"
        # 脚本里这几条正是替换脚本真正会跑的命令，也是当初弹黑框的元凶。
        # 换行交给 newline 参数处理：这里写 \n，落盘成 \r\n，别自己写 \r\n（会变成 \r\r\n）。
        script.write_text(
            "@echo off\n"
            f'tasklist /FI "PID eq 888888" /NH > "{root / "t.txt"}" 2>&1\n'
            f'find "888888" "{root / "t.txt"}" >NUL\n'
            "ping -n 2 127.0.0.1 >NUL\n"
            f'echo ok > "{marker}"\n',
            encoding="mbcs",
            newline="\r\n",
        )
        before = visible_consoles()
        seen: set[int] = set()
        subprocess.Popen(
            ["cmd", "/c", str(script)],
            cwd=str(root),
            creationflags=updater.STARTUP_FLAGS,
            close_fds=True,
        )
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            seen |= visible_consoles() - before
            if marker.is_file():
                break
            time.sleep(0.02)
        time.sleep(0.3)  # 窗口可能要冒一下才画出来，落定后再数一次
        seen |= visible_consoles() - before
        # 要在临时目录还在的时候判定，出去就被删掉了
        ran = marker.is_file()

    assert ran, "自检用的批处理没跑起来（命令一条都没执行）"
    assert not seen, f"替换脚本弹出了 {len(seen)} 个控制台窗口"
    return "tasklist / find / ping 都在隐藏控制台里跑，一个窗口都没冒"


@check("跨盘搬运：分享里的目录整棵搬过来")
def test_relay_folder() -> str:
    """用假客户端把「百度 → 夸克」整条链路跑一遍（不联网、不碰真账号）。

    真实场景里分享常常只有一个大目录（一部剧、一个合集），老实现见到目录就直接跳过，
    整条任务最后只剩一句「没有成功搬运任何文件」——v1.1.6 的实测反馈就是这个。
    这条守住四件事：目录递归展开、下载路径按「中转目录 + 相对路径」拼、
    夸克那边照原样建出子目录、百度回「文件已转存」不能当成失败。
    """
    from quarkrelay.core.baidu import BaiduError, BaiduFile, BaiduShare
    from quarkrelay.core.relay import baidu_to_quark_worker
    from quarkrelay.core.tasks import Task

    next_id = [0]

    def entry(name: str, path: str, size: int, is_dir: bool) -> BaiduFile:
        next_id[0] += 1
        return BaiduFile(fs_id=next_id[0], name=name, path=path, size=size, is_dir=is_dir)

    # 分享里的样子：花儿与少年/{S1E1, S1E2, 花絮/预告}，根目录还放了个说明.txt
    share_tree = {
        "/": [
            entry("花儿与少年", "/花儿与少年", 0, True),
            entry("说明.txt", "/说明.txt", 10, False),
        ],
        "/花儿与少年": [
            entry("S1E1.mp4", "/花儿与少年/S1E1.mp4", 1000, False),
            entry("S1E2.mp4", "/花儿与少年/S1E2.mp4", 2000, False),
            entry("花絮", "/花儿与少年/花絮", 0, True),
        ],
        "/花儿与少年/花絮": [entry("预告.mp4", "/花儿与少年/花絮/预告.mp4", 500, False)],
    }
    # 转存后我的百度网盘里的样子：故意把 S1E2 写成「(1)」，模拟同名被百度改名
    disk_tree = {
        "/夸克中转站": [
            entry("花儿与少年", "/夸克中转站/花儿与少年", 0, True),
            entry("说明.txt", "/夸克中转站/说明.txt", 10, False),
        ],
        "/夸克中转站/花儿与少年": [
            entry("S1E1.mp4", "/夸克中转站/花儿与少年/S1E1.mp4", 1000, False),
            entry("S1E2 (1).mp4", "/夸克中转站/花儿与少年/S1E2 (1).mp4", 2000, False),
            entry("花絮", "/夸克中转站/花儿与少年/花絮", 0, True),
        ],
        "/夸克中转站/花儿与少年/花絮": [
            entry("预告.mp4", "/夸克中转站/花儿与少年/花絮/预告.mp4", 500, False),
        ],
    }

    class FakeBaidu:
        def __init__(self) -> None:
            self.opened: list[str] = []      # 取直链时用的路径
            self.transfer_args: tuple | None = None

        def resolve_share(self, url: str, password: str = "") -> BaiduShare:
            return BaiduShare(surl="1selftest", title="花儿与少年")

        def list_share(self, share: BaiduShare, directory: str = "/") -> list[BaiduFile]:
            return share_tree[directory]

        def list_dir(self, path: str = "/") -> list[BaiduFile]:
            return disk_tree.get(path, [])

        def transfer(self, share, fs_ids, target_path, *, on_exists: str = "rename"):
            self.transfer_args = (len(fs_ids), target_path)
            # 百度就是这么答的：同一份分享再搬一次会说「文件已转存」
            raise BaiduError("转存失败：文件已转存")

        def download(self, url, dest, *, referer="", progress=None, should_cancel=None, expect_size=0):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"x" * expect_size)
            if progress:
                progress(expect_size, expect_size)
            return dest

    class FakeQuark:
        def __init__(self) -> None:
            self.dirs: list[str] = []
            self.uploads: list[tuple[str, str, int]] = []
            self.shared: list[str] = []

        def ensure_dir(self, path: str, pdir_fid: str = "0") -> str:
            self.dirs.append(path)
            return f"fid:{path}"

        def upload_file(self, path, pdir_fid, *, remote_name="", progress=None, should_cancel=None):
            size = path.stat().st_size
            if progress:
                progress(size, size)
            self.uploads.append((pdir_fid, remote_name, size))
            return {"fid": f"f{len(self.uploads)}"}

        def share_create(self, fid_list, *, title="", url_type=2, expired_type=1):
            self.shared = list(fid_list)
            self.share_title = title
            return {"share_url": "https://pan.quark.cn/s/selftest", "passcode": "", "title": title}

    def run(fs_ids: list[int] | None):
        baidu, quark = FakeBaidu(), FakeQuark()

        def provide_dlink(path: str) -> str:
            baidu.opened.append(path)
            return "https://dlink.example/selftest"

        task = Task(id="relay-selftest", kind="relay", title="自检搬运")
        result = baidu_to_quark_worker(
            task,
            baidu,
            quark,
            share_url="https://pan.baidu.com/s/1selftest",
            fs_ids=fs_ids,
            baidu_dir="/夸克中转站",
            quark_dir="夸克中转站",
            dlink_provider=provide_dlink,
            make_share=True,
        )
        return baidu, quark, task, result

    # 一、勾选根目录里的全部内容：目录要递归展开，说明.txt 也不能漏
    baidu, quark, task, result = run(None)
    # worker 只负责干活，「成功 / 失败」由任务中心落定；能一路跑到「完成」就说明链路是通的
    assert task.stage == "完成", f"{task.stage} / {task.error}"
    assert baidu.transfer_args == (2, "/夸克中转站"), baidu.transfer_args
    assert [name for _, name, _ in quark.uploads] == [
        "S1E1.mp4",
        "S1E2.mp4",
        "预告.mp4",
        "说明.txt",
    ], quark.uploads
    assert baidu.opened == [
        "/夸克中转站/花儿与少年/S1E1.mp4",
        "/夸克中转站/花儿与少年/S1E2 (1).mp4",  # 被百度改过名，也要找回来
        "/夸克中转站/花儿与少年/花絮/预告.mp4",
        "/夸克中转站/说明.txt",
    ], baidu.opened
    assert [fid for fid, _, _ in quark.uploads] == [
        "fid:夸克中转站/花儿与少年",
        "fid:夸克中转站/花儿与少年",
        "fid:夸克中转站/花儿与少年/花絮",
        "fid:夸克中转站",
    ], quark.uploads
    assert [size for _, _, size in quark.uploads] == [1000, 2000, 500, 10], quark.uploads
    assert len(quark.shared) == 4, f"根目录还混着文件时应逐个分享：{quark.shared}"
    assert task.progress == 100.0, task.progress
    assert result["bytes"] == 3510, result["bytes"]

    # 二、只勾中那个目录：夸克那边按原层级建目录，分享也直接给这个目录
    folder_fs_id = share_tree["/"][0].fs_id
    baidu, quark, task, _ = run([folder_fs_id])
    assert baidu.transfer_args == (1, "/夸克中转站"), baidu.transfer_args
    assert [name for _, name, _ in quark.uploads] == ["S1E1.mp4", "S1E2.mp4", "预告.mp4"], quark.uploads
    assert set(quark.dirs) == {"夸克中转站/花儿与少年", "夸克中转站/花儿与少年/花絮"}, quark.dirs
    assert quark.shared == ["fid:夸克中转站/花儿与少年"], quark.shared
    assert quark.share_title == "花儿与少年", quark.share_title

    return "目录递归展开、层级落位、转存幂等、改名找回 全部正确"


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

    tests = [
        test_compile,
        test_docs_sync,
        test_links,
        test_naming,
        test_net,
        test_config,
        test_store,
        test_tasks,
        test_login_session,
        test_updater,
        test_update_no_console_window,
        test_relay_folder,
    ]
    if "--no-ui" not in argv:
        tests.append(test_close_quits)
        tests.append(test_ui)
        tests.append(test_qr_panel)

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
