"""搬运编排：把「链接 → 转存 → 生成分享」和「百度 → 夸克」两条流水线编起来。

所有耗时逻辑都写成 `worker(task)` 形式，由 TaskCenter 丢进线程池执行；
线程里只通过 task 对象上报进度，不直接碰界面。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .baidu import BaiduClient, BaiduFile, BaiduShare
from .errors import BaiduError, CancelledError, QuarkError
from .naming import build_name, render_for
from .net import SpeedMeter, Throttle, human_size
from .quark import QuarkClient
from .tasks import Task

logger = logging.getLogger(__name__)

DLinkProvider = Callable[[str], str]

# 分享里的目录最多展开这么多层，纯粹是防呆：真遇到异常结构不至于把线程卡死
MAX_DIR_DEPTH = 8

# 转存时百度说「已经转存过」的各种说法。这不算失败：
# 内容早就在我的网盘里了，拿现有文件接着搬就行。
ALREADY_TRANSFERRED = ("已转存", "已存在", "文件已存在", "同名文件", "已收藏")


# --------------------------------------------------------------------- 工具
def _check_cancel(task: Task) -> None:
    if task.cancelled:
        raise CancelledError()


def _stage(task: Task, text: str, progress: float | None = None) -> None:
    task.set_stage(text)
    if progress is not None:
        task.set_progress(progress)


# ----------------------------------------------------------- 搬运：分享展开
@dataclass(frozen=True)
class RelayedFile:
    """展开后的一个待搬运文件。

    `relative` 是相对**分享根目录**的路径，例如 `花儿与少年/S1E1.mp4`。
    它决定了文件转存后在我的网盘里的位置，也决定了上传到对方网盘时要建哪些目录。
    """

    relative: str
    item: BaiduFile

    @property
    def name(self) -> str:
        return self.item.name


def _already_transferred(message: str) -> bool:
    return any(word in message for word in ALREADY_TRANSFERRED)


def expand_share_files(
    baidu: BaiduClient,
    share: BaiduShare,
    entries: list[BaiduFile],
    prefix: str = "",
    depth: int = 0,
) -> list[RelayedFile]:
    """把分享里选中的条目展开成文件清单：目录递归展开，层级原样保留。

    分享里常常只有一个大目录（一部剧、一个合集），以前的实现见到目录就直接跳过，
    整条任务最后只剩一句「没有成功搬运任何文件」——用户看到的就是这个。
    """
    if depth > MAX_DIR_DEPTH:
        raise BaiduError(f"目录嵌套超过 {MAX_DIR_DEPTH} 层，已停止展开（分享结构可能有异常）")
    found: list[RelayedFile] = []
    for item in entries:
        relative = f"{prefix}/{item.name}" if prefix else item.name
        if not item.is_dir:
            found.append(RelayedFile(relative, item))
            continue
        children = baidu.list_share(share, f"/{relative}")
        logger.info("展开目录：%s（%d 个条目）", relative, len(children))
        found.extend(expand_share_files(baidu, share, children, relative, depth + 1))
    return found


def remote_path_of(baidu_dir: str, relative: str) -> str:
    """文件转存到我的百度网盘后的真实路径（取直链就是按这个路径去取的）。"""
    base = "/" + (baidu_dir or "").strip().strip("/")
    clean = relative.lstrip("/")
    return f"{base.rstrip('/')}/{clean}" if clean else base


def _verify_download(path: Path, item: BaiduFile, label: str) -> None:
    """下载完立刻核对字节数。

    直链是靠内置浏览器点「下载」截获的，万一截到的是别的文件，宁可在这里报错、
    也不能把别人的文件当成目标文件传上去；顺带也能发现下载被截断的情况。
    """
    if not item.size:
        return
    try:
        actual = path.stat().st_size
    except OSError:
        actual = -1
    if actual != item.size:
        path.unlink(missing_ok=True)
        raise BaiduError(
            f"「{label}」下载后大小不对（拿到 {human_size(max(0, actual))}，"
            f"应为 {human_size(item.size)}），已丢弃这份缓冲，请重试"
        )


class BaiduIndex:
    """查「转存后文件到底落在哪」。

    转存是幂等的：同一份分享再搬一次，百度会回「文件已转存／已存在同名文件」，
    意思是内容早就在我的网盘里了；同名冲突时还可能被改名成「名字 (1)」。
    所以取直链之前先看一眼目标目录，免得拿着一个猜的路径去取。
    """

    def __init__(self, baidu: BaiduClient) -> None:
        self.baidu = baidu
        self._dirs: dict[str, dict[str, BaiduFile]] = {}

    def _listing(self, directory: str) -> dict[str, BaiduFile]:
        if directory not in self._dirs:
            self._dirs[directory] = {item.name: item for item in self.baidu.list_dir(directory)}
        return self._dirs[directory]

    def locate(self, path: str) -> BaiduFile | None:
        directory, _, name = path.rpartition("/")
        listing = self._listing(directory or "/")
        found = listing.get(name)
        if found is not None:
            return found
        # 重名时百度会给新文件加序号，按「名字 (1)」「名字 (2)」… 找回去
        stem, dot, suffix = name.rpartition(".")
        base = stem if dot else name
        ext = f".{suffix}" if dot else ""
        for index in range(1, 100):
            candidate = f"{base} ({index}){ext}"
            if candidate in listing:
                logger.info("转存后文件被改名：%s → %s", name, candidate)
                return listing[candidate]
        return None


def _share_title(names: list[str], share_title: str) -> str:
    """给生成的分享取名字：整目录搬运就用那层目录名，否则用分享标题／第一个文件名。"""
    tops = {name.split("/")[0] for name in names}
    if len(tops) == 1:
        return next(iter(tops))
    return share_title or (names[0] if names else "")


def _share_targets(quark: QuarkClient, quark_dir: str, names: list[str], fids: list[str]) -> list[str]:
    """整棵目录搬过来时优先分享那层目录：一个链接对一个文件夹，比逐个分享文件好用。"""
    tops = {name.split("/")[0] for name in names}
    if len(tops) != 1 or not any("/" in name for name in names):
        return [fid for fid in fids if fid]
    top = next(iter(tops))
    try:
        return [quark.ensure_dir(f"{quark_dir.rstrip('/')}/{top}")]
    except QuarkError as exc:
        logger.warning("没能定位要分享的目录「%s」：%s，改为分享文件", top, exc)
        return [fid for fid in fids if fid]


# ---------------------------------------------------- 流水线一：夸克中转站
def quark_transfer_worker(
    task: Task,
    client: QuarkClient,
    *,
    pwd_id: str,
    passcode: str,
    name: str,
    target_dir: str,
    url_type: int = 2,
    expired_type: int = 1,
) -> dict[str, Any]:
    """解析别人的夸克分享 → 转存到我的指定目录 → 生成我自己的分享链接。"""
    _stage(task, "解析分享链接", 5)
    detail = client.share_detail(pwd_id, passcode)
    token_info = detail.get("token_info") or {}
    stoken = str(token_info.get("stoken") or "")
    if not stoken:
        raise QuarkError("分享已失效或提取码不正确")
    items = [item for item in (detail.get("list") or []) if item.get("filename")]
    if not items:
        raise QuarkError("这个分享里没有可转存的文件")

    _check_cancel(task)
    _stage(task, "创建/定位目标目录", 15)
    target_fid = client.ensure_dir(target_dir)

    _check_cancel(task)
    _stage(task, "转存到我的网盘", 30)
    result = client.share_saveas(pwd_id, stoken, to_pdir_fid=target_fid)
    task_id = str(result.get("task_id") or "")
    if task_id:
        status = client.wait_task(task_id, on_tick=lambda s: _stage(task, f"转存中（状态 {s}）", 45))
        if status == "3":
            raise QuarkError("转存失败：可能是网盘空间不足，或分享已失效")
        if status == "4":
            raise QuarkError("转存任务被暂停，请稍后重试")

    _check_cancel(task)
    _stage(task, "定位转存后的文件", 65)
    fid_list: list[str] = []
    names: list[str] = []
    for item in items:
        filename = str(item.get("filename"))
        fid = client.find_fid(filename, target_fid)
        if fid:
            fid_list.append(fid)
            names.append(filename)
        else:
            names.append(filename)

    if not fid_list:
        raise QuarkError("转存已完成，但没能定位到文件；可到夸克网盘里手动分享")

    _check_cancel(task)
    _stage(task, "生成新的分享链接", 85)
    title = name or build_name(names[0] if names else "")
    share = client.share_create(fid_list, title=title, url_type=url_type, expired_type=expired_type)
    share_url = str(share.get("share_url") or "")
    share_code = str(share.get("passcode") or "")
    final_name = str(share.get("title") or title)

    _stage(task, "完成", 100)
    return {
        "name": final_name,
        "link": share_url,
        "code": share_code,
        "combined": render_for(final_name, share_url, share_code),
        "files": names,
        "target_dir": target_dir,
        "expired_type": expired_type,
    }


# ------------------------------------------------- 流水线二：百度 → 夸克
def baidu_to_quark_worker(
    task: Task,
    baidu: BaiduClient,
    quark: QuarkClient,
    *,
    share_url: str,
    password: str = "",
    fs_ids: list[int] | None = None,
    baidu_dir: str = "/夸克中转站",
    quark_dir: str = "夸克中转站",
    dlink_provider: DLinkProvider | None = None,
    speed_kbps: int = 0,
    keep_buffer: bool = False,
    share_name: str = "",
    make_share: bool = False,
    url_type: int = 2,
    expired_type: int = 1,
) -> dict[str, Any]:
    """百度分享 → 转存到我的百度盘 → 内置浏览器取直链 → 流式下载 → 上传夸克。"""
    from ..paths import temp_buffer_dir  # 避免顶层循环导入

    _stage(task, "解析百度分享", 3)
    share: BaiduShare = baidu.resolve_share(share_url, password)
    files = baidu.list_share(share)
    if not files:
        raise BaiduError("这个分享里没有文件")
    selected = [f for f in files if not fs_ids or f.fs_id in set(fs_ids)]
    if not selected:
        selected = files
    targets = list(selected)

    _check_cancel(task)
    _stage(task, "转存到我的百度网盘", 7)
    try:
        baidu.transfer(share, [f.fs_id for f in targets], baidu_dir)
    except BaiduError as exc:
        # 转存是幂等的：同一份分享再搬一次，百度会回「文件已转存／已存在同名文件」，
        # 意思是内容早就在我的网盘里了 —— 这不是失败，拿已有文件接着搬就行。
        if not _already_transferred(str(exc)):
            raise
        logger.info("百度网盘里已有这些内容（%s），改用已有文件继续", exc)

    _check_cancel(task)
    _stage(task, "展开目录", 9)
    relay_files = expand_share_files(baidu, share, targets)
    if not relay_files:
        raise BaiduError("这个分享里没有可搬运的文件（选中的目录是空的）")

    index_of = BaiduIndex(baidu)
    buffer_dir = temp_buffer_dir()
    quark_fids: list[str] = []
    uploaded: list[str] = []
    total_bytes = sum(relayed.item.size for relayed in relay_files) or 1
    total = len(relay_files)
    started = time.monotonic()
    logger.info("本次要搬 %d 个文件，共 %s", total, human_size(total_bytes))

    for position, relayed in enumerate(relay_files, start=1):
        _check_cancel(task)
        item = relayed.item
        label = relayed.relative
        parent = label.rsplit("/", 1)[0] if "/" in label else ""
        # 夸克那边按原层级建目录，不把整棵树摊平到一个目录里
        target_dir = f"{quark_dir.rstrip('/')}/{parent}" if parent else quark_dir
        base_progress = 9 + (position - 1) / max(1, total) * 81

        _stage(task, f"确认转存位置：{label}", base_progress)
        located = index_of.locate(remote_path_of(baidu_dir, label))
        if located is None:
            raise BaiduError(
                f"转存后在我的百度网盘里没找到「{label}」，请确认中转目录「{baidu_dir}」"
                f"里有这个文件（也可能转存还没完成），稍后重试"
            )
        remote_path = located.path or remote_path_of(baidu_dir, label)

        _check_cancel(task)
        _stage(task, f"获取直链：{label}", base_progress + 2)
        if dlink_provider is None:
            raise BaiduError("未配置内置浏览器下载通道，无法获取百度下载直链")
        dlink = dlink_provider(remote_path)
        if not dlink:
            raise BaiduError(f"没能获取「{label}」的下载直链")

        _check_cancel(task)
        buffer_path = buffer_dir / f"{item.fs_id}_{item.name}"
        meter = SpeedMeter()
        throttle = Throttle(speed_kbps)
        last_seen = [0]

        def _on_download(
            done: int,
            total_now: int,
            _base: float = base_progress + 2,
            _meter: SpeedMeter = meter,
            _label: str = label,
            _size: int = item.size,
        ) -> None:
            size = total_now or _size or 1
            fraction = min(1.0, done / size) if size else 0.0
            _stage(
                task,
                f"下载 {_label} · {human_size(done)}/{human_size(size)}"
                f" · {human_size(_meter.speed)}/s",
                _base + fraction * 41,
            )

        def _wrapped(done: int, total_now: int) -> None:
            delta = done - last_seen[0]
            last_seen[0] = done
            if delta > 0:
                throttle(delta)
            meter.update(done)
            _on_download(done, total_now)

        try:
            baidu.download(
                dlink,
                buffer_path,
                referer=share.url,
                progress=_wrapped,
                should_cancel=task.should_cancel,
                expect_size=item.size,
            )
        except BaiduError as exc:
            if "取消" in str(exc):
                raise CancelledError() from exc
            raise
        _verify_download(buffer_path, item, label)

        _check_cancel(task)
        _stage(task, f"上传到夸克：{label}", base_progress + 45)
        quark_fid = quark.ensure_dir(target_dir)

        def _on_upload(
            done: int,
            total_now: int,
            _base: float = base_progress + 45,
            _label: str = label,
            _size: int = item.size,
        ) -> None:
            size = total_now or _size or 1
            fraction = min(1.0, done / size) if size else 0.0
            _stage(
                task,
                f"上传 {_label} · {human_size(done)}/{human_size(size)}",
                _base + fraction * 45,
            )

        result = quark.upload_file(
            buffer_path,
            quark_fid,
            remote_name=item.name,
            progress=_on_upload,
            should_cancel=task.should_cancel,
        )
        quark_fids.append(str(result.get("fid") or ""))
        uploaded.append(label)

        if not keep_buffer:
            try:
                buffer_path.unlink(missing_ok=True)
            except OSError as exc:
                logger.debug("删除缓冲文件失败：%s", exc)

    if not quark_fids:
        raise BaiduError("没有成功搬运任何文件")

    elapsed = max(0.001, time.monotonic() - started)
    summary: dict[str, Any] = {
        "files": uploaded,
        "fids": quark_fids,
        "target_dir": quark_dir,
        "speed": total_bytes / elapsed,
        "elapsed": elapsed,
        "bytes": total_bytes,
    }

    if make_share:
        _stage(task, "生成夸克分享链接", 95)
        title = share_name or build_name(_share_title(uploaded, share.title))
        share_result = quark.share_create(
            _share_targets(quark, quark_dir, uploaded, quark_fids),
            title=title,
            url_type=url_type,
            expired_type=expired_type,
        )
        link = str(share_result.get("share_url") or "")
        code = str(share_result.get("passcode") or "")
        summary.update(
            {
                "name": str(share_result.get("title") or title),
                "link": link,
                "code": code,
                "combined": render_for(str(share_result.get("title") or title), link, code),
            }
        )

    _stage(task, "完成", 100)
    return summary


# --------------------------------------------------- 流水线三：夸克内互转
def quark_export_share_worker(
    task: Task,
    client: QuarkClient,
    *,
    names: list[str],
    target_dir: str,
    share_name: str,
    url_type: int = 2,
    expired_type: int = 1,
) -> dict[str, Any]:
    """把我在夸克里已有的文件批量生成分享链接。"""
    _stage(task, "定位文件", 10)
    fid_list: list[str] = []
    missing: list[str] = []
    for name in names:
        fid = client.find_fid(name)
        if fid:
            fid_list.append(fid)
        else:
            missing.append(name)
    if not fid_list:
        raise QuarkError("没有在夸克网盘里找到这些文件：" + "、".join(missing))
    _stage(task, "创建分享", 60)
    share = client.share_create(
        fid_list,
        title=share_name or build_name(names[0] if names else ""),
        url_type=url_type,
        expired_type=expired_type,
    )
    link = str(share.get("share_url") or "")
    code = str(share.get("passcode") or "")
    title = str(share.get("title") or share_name)
    _stage(task, "完成", 100)
    return {
        "name": title,
        "link": link,
        "code": code,
        "combined": render_for(title, link, code),
        "files": names,
        "missing": missing,
        "target_dir": target_dir,
    }


# ------------------------------------------------- 流水线四：夸克 → 百度
def quark_to_baidu_worker(
    task: Task,
    quark: QuarkClient,
    baidu: BaiduClient,
    *,
    pwd_id: str,
    passcode: str = "",
    quark_dir: str = "夸克中转站",
    baidu_dir: str = "/夸克中转站",
    speed_kbps: int = 0,
    keep_buffer: bool = False,
    share_name: str = "",
    make_share: bool = False,
    period: int = 0,
    on_exists: str = "rename",
) -> dict[str, Any]:
    """夸克分享 → 转存到我的夸克 → 下载到本地缓冲 → 上传百度网盘。

    和「百度 → 夸克」正好反过来：同样不落桌面，缓冲区随用随删。
    """
    from ..paths import temp_buffer_dir  # 避免顶层循环导入

    _stage(task, "解析夸克分享", 3)
    detail = quark.share_detail(pwd_id, passcode)
    stoken = str((detail.get("token_info") or {}).get("stoken") or "")
    if not stoken:
        raise QuarkError("分享已失效或提取码不正确")
    items = [item for item in (detail.get("list") or []) if item.get("filename")]
    if not items:
        raise QuarkError("这个分享里没有可搬运的文件")

    # 这个方向是整包转存，但下载要靠「按文件名搜回 fid」，而目录没法这么搜
    # （开放平台没有目录列举接口），所以文件夹只能明确说清楚，别装作搬过了。
    folders = [str(item.get("filename")) for item in items if item.get("dir")]
    files_only = [item for item in items if not item.get("dir")]
    if folders:
        logger.info("分享里这些是目录，本方向暂时跳过：%s", "、".join(folders))
    if not files_only:
        raise QuarkError(
            "这个分享里只有文件夹（" + "、".join(folders[:3]) + "），"
            "「夸克 → 百度」暂时搬不了整目录；把里面的文件单独分享一下再搬"
        )
    items = files_only

    _check_cancel(task)
    _stage(task, "转存到我的夸克网盘", 8)
    quark_pdir = quark.ensure_dir(quark_dir)
    saved = quark.share_saveas(pwd_id, stoken, to_pdir_fid=quark_pdir)
    task_id = str(saved.get("task_id") or "")
    if task_id:
        status = quark.wait_task(
            task_id, on_tick=lambda state: _stage(task, f"夸克转存中（状态 {state}）", 14)
        )
        if status == "3":
            raise QuarkError("夸克转存失败：可能是网盘空间不足，或分享已失效")
        if status == "4":
            raise QuarkError("夸克转存任务被暂停，请稍后重试")

    _check_cancel(task)
    _stage(task, "定位转存后的文件", 18)
    targets: list[tuple[str, str]] = []
    for item in items:
        filename = str(item.get("filename"))
        fid = quark.find_fid(filename, quark_pdir)
        if fid:
            targets.append((filename, fid))
    if not targets:
        raise QuarkError("转存已完成，但没能定位到文件；可到夸克网盘里手动搬运")

    _stage(task, "准备百度目标目录", 22)
    baidu.mkdir(baidu_dir)

    buffer_dir = temp_buffer_dir()
    names: list[str] = []
    remote_paths: list[str] = []
    total_bytes = 0
    started = time.monotonic()

    for index, (filename, fid) in enumerate(targets, start=1):
        _check_cancel(task)
        base = 22 + (index - 1) / max(1, len(targets)) * 70
        buffer_path = buffer_dir / f"quark_{fid}_{filename}"
        meter = SpeedMeter()
        throttle = Throttle(speed_kbps)
        last_seen = [0]

        def _on_download(
            done: int,
            total: int,
            _base: float = base,
            _meter: SpeedMeter = meter,
            _name: str = filename,
        ) -> None:
            total_now = total or 1
            fraction = min(1.0, done / total_now) if total_now else 0.0
            _stage(
                task,
                f"下载 {_name} · {human_size(done)}/{human_size(total_now)}"
                f" · {human_size(_meter.speed)}/s",
                _base + fraction * 33,
            )

        def _wrapped(done: int, total: int) -> None:
            delta = done - last_seen[0]
            last_seen[0] = done
            if delta > 0:
                throttle(delta)
            meter.update(done)
            _on_download(done, total)

        _stage(task, f"从夸克下载：{filename}", base)
        quark.download_to(fid, buffer_path, progress=_wrapped)
        size = buffer_path.stat().st_size
        total_bytes += size

        _check_cancel(task)
        _stage(task, f"上传到百度网盘：{filename}", base + 36)

        def _on_upload(
            done: int,
            total: int,
            _base: float = base + 36,
            _name: str = filename,
            _size: int = size,
        ) -> None:
            total_now = total or _size or 1
            fraction = min(1.0, done / total_now) if total_now else 0.0
            _stage(
                task,
                f"上传 {_name} · {human_size(done)}/{human_size(total_now)}",
                _base + fraction * 32,
            )

        uploaded = baidu.upload(
            buffer_path,
            baidu_dir,
            on_exists=on_exists,
            progress=_on_upload,
            should_cancel=task.should_cancel,
        )
        names.append(uploaded.name)
        remote_paths.append(uploaded.path)

        if not keep_buffer:
            try:
                buffer_path.unlink(missing_ok=True)
            except OSError as exc:
                logger.debug("删除缓冲文件失败：%s", exc)

    if not names:
        raise QuarkError("没有成功搬运任何文件")

    elapsed = max(0.001, time.monotonic() - started)
    summary: dict[str, Any] = {
        "files": names,
        "remote_paths": remote_paths,
        "target_dir": baidu_dir,
        "speed": total_bytes / elapsed if total_bytes else 0.0,
        "elapsed": elapsed,
        "bytes": total_bytes,
    }

    if make_share:
        _stage(task, "生成百度分享链接", 95)
        created = baidu.create_share(remote_paths, period=period)
        title = share_name or build_name(names[0] if names else "")
        summary.update(
            {
                "name": title,
                "link": created["link"],
                "code": created["pwd"],
                "combined": render_for(title, created["link"], created["pwd"]),
            }
        )

    _stage(task, "完成", 100)
    return summary


def result_to_record(result: dict[str, Any]) -> tuple[str, str, str]:
    """从 worker 结果里取出 (名字, 链接, 提取码)。"""
    return (
        str(result.get("name") or ""),
        str(result.get("link") or ""),
        str(result.get("code") or ""),
    )
