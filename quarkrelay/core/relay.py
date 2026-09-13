"""搬运编排：把「链接 → 转存 → 生成分享」和「百度 → 夸克」两条流水线编起来。

所有耗时逻辑都写成 `worker(task)` 形式，由 TaskCenter 丢进线程池执行；
线程里只通过 task 对象上报进度，不直接碰界面。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from .baidu import BaiduClient, BaiduShare
from .errors import BaiduError, CancelledError, QuarkError
from .naming import build_name, render_for
from .net import SpeedMeter, Throttle, human_size
from .quark import QuarkClient
from .tasks import Task

logger = logging.getLogger(__name__)

DLinkProvider = Callable[[str], str]


# --------------------------------------------------------------------- 工具
def _check_cancel(task: Task) -> None:
    if task.cancelled:
        raise CancelledError()


def _stage(task: Task, text: str, progress: float | None = None) -> None:
    task.set_stage(text)
    if progress is not None:
        task.set_progress(progress)


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
    targets = [f for f in selected]

    _check_cancel(task)
    _stage(task, "转存到我的百度网盘", 8)
    try:
        baidu.transfer(share, [f.fs_id for f in targets], baidu_dir)
    except BaiduError as exc:
        if "已存在同名文件" not in str(exc):
            raise
        logger.info("百度网盘已存在同名文件，直接使用已有文件")

    buffer_dir = temp_buffer_dir()
    quark_fids: list[str] = []
    uploaded_names: list[str] = []
    total_bytes = sum(f.size for f in targets if not f.is_dir) or 1
    done_bytes = 0
    started = time.monotonic()

    for index, item in enumerate(targets, start=1):
        _check_cancel(task)
        if item.is_dir:
            logger.info("跳过目录：%s（暂不支持整目录搬运）", item.name)
            continue

        remote_path = item.path if item.path.startswith("/") else f"{baidu_dir.rstrip('/')}/{item.name}"
        base_progress = 8 + (index - 1) / max(1, len(targets)) * 82

        _stage(task, f"获取直链：{item.name}", base_progress)
        if dlink_provider is None:
            raise BaiduError("未配置内置浏览器下载通道，无法获取百度下载直链")
        dlink = dlink_provider(remote_path)
        if not dlink:
            raise BaiduError(f"没能获取「{item.name}」的下载直链")

        _check_cancel(task)
        buffer_path = buffer_dir / f"{item.fs_id}_{item.name}"
        meter = SpeedMeter()
        throttle = Throttle(speed_kbps)
        last_seen = [0]

        def _on_download(done: int, total: int, _base: float = base_progress, _meter: SpeedMeter = meter) -> None:
            total_now = total or item.size or 1
            fraction = min(1.0, done / total_now) if total_now else 0.0
            _stage(
                task,
                f"下载 {item.name} · {human_size(done)}/{human_size(total_now)}"
                f" · {human_size(_meter.speed)}/s",
                _base + fraction * 41,
            )

        def _wrapped(done: int, total: int) -> None:
            delta = done - last_seen[0]
            last_seen[0] = done
            if delta > 0:
                throttle(delta)
            meter.update(done)
            _on_download(done, total)

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

        _check_cancel(task)
        _stage(task, f"上传到夸克：{item.name}", base_progress + 45)
        quark_fid = quark.ensure_dir(quark_dir)

        def _on_upload(done: int, total: int, _base: float = base_progress + 45) -> None:
            total_now = total or item.size or 1
            fraction = min(1.0, done / total_now) if total_now else 0.0
            _stage(
                task,
                f"上传 {item.name} · {human_size(done)}/{human_size(total_now)}",
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
        uploaded_names.append(item.name)

        if not keep_buffer:
            try:
                buffer_path.unlink(missing_ok=True)
            except OSError as exc:
                logger.debug("删除缓冲文件失败：%s", exc)

    if not quark_fids:
        raise BaiduError("没有成功搬运任何文件")

    elapsed = max(0.001, time.monotonic() - started)
    summary: dict[str, Any] = {
        "files": uploaded_names,
        "fids": quark_fids,
        "target_dir": quark_dir,
        "speed": total_bytes / elapsed,
        "elapsed": elapsed,
        "bytes": total_bytes,
    }

    if make_share:
        _stage(task, "生成夸克分享链接", 95)
        title = share_name or build_name(uploaded_names[0] if uploaded_names else "")
        share_result = quark.share_create(
            [fid for fid in quark_fids if fid],
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


def result_to_record(result: dict[str, Any]) -> tuple[str, str, str]:
    """从 worker 结果里取出 (名字, 链接, 提取码)。"""
    return (
        str(result.get("name") or ""),
        str(result.get("link") or ""),
        str(result.get("code") or ""),
    )
