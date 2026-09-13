"""截图识别：Windows 系统自带 OCR + OpenCV 二维码解码，双通道提取链接。

* OCR：调用系统 WinRT 的 Windows.Media.Ocr（Win10/11 内置，无需联网、无需额外模型）。
* 二维码：cv2.QRCodeDetector，多尺度尝试，能识别分享图里的二维码。
* 最后统一交给 links 引擎，把「链接 + 提取码」配对成结构化结果。
"""

from __future__ import annotations

import logging
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .links import ShareLink, extract

logger = logging.getLogger(__name__)

_PS_SCRIPT = r"""
param([string]$Path, [string]$Out)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
})[0]
function Await($op, $type) {
    $task = $asTaskGeneric.MakeGenericMethod($type).Invoke($null, @($op))
    $task.Wait(-1) | Out-Null
    $task.Result
}
[Windows.Storage.StorageFile,Windows.Storage,ContentType=WindowsRuntime] | Out-Null
[Windows.Storage.FileAccessMode,Windows.Storage,ContentType=WindowsRuntime] | Out-Null
[Windows.Storage.Streams.IRandomAccessStream,Windows.Storage.Streams,ContentType=WindowsRuntime] | Out-Null
[Windows.Graphics.Imaging.BitmapDecoder,Windows.Graphics,ContentType=WindowsRuntime] | Out-Null
[Windows.Graphics.Imaging.SoftwareBitmap,Windows.Graphics.Imaging,ContentType=WindowsRuntime] | Out-Null
[Windows.Globalization.Language,Windows.Globalization,ContentType=WindowsRuntime] | Out-Null
[Windows.Media.Ocr.OcrEngine,Windows.Foundation,ContentType=WindowsRuntime] | Out-Null
[Windows.Media.Ocr.OcrResult,Windows.Foundation,ContentType=WindowsRuntime] | Out-Null

$engine = $null
foreach ($tag in @('zh-Hans-CN','zh-Hans','zh-CN','en-US')) {
    try {
        $lang = New-Object Windows.Globalization.Language $tag
        $candidate = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($lang)
        if ($candidate -ne $null) { $engine = $candidate; break }
    } catch { }
}
if ($engine -eq $null) { $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages() }
if ($engine -eq $null) {
    [System.IO.File]::WriteAllText($Out, '', (New-Object System.Text.UTF8Encoding($false)))
    exit 3
}

$file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($Path)) ([Windows.Storage.StorageFile])
$stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
$decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
$bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
$result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
$lines = @()
foreach ($line in $result.Lines) { $lines += $line.Text }
[System.IO.File]::WriteAllText($Out, ($lines -join "`n"), (New-Object System.Text.UTF8Encoding($false)))
exit 0
"""


@dataclass
class RecognizeResult:
    """一次识别的完整结果。"""

    text: str = ""
    links: list[ShareLink] = field(default_factory=list)
    qr_links: list[ShareLink] = field(default_factory=list)
    engine: str = ""
    error: str = ""

    @property
    def all_links(self) -> list[ShareLink]:
        seen: set[str] = set()
        out: list[ShareLink] = []
        for link in self.qr_links + self.links:
            key = link.url.rstrip("/")
            if key in seen:
                continue
            seen.add(key)
            out.append(link)
        return out

    @property
    def ok(self) -> bool:
        return bool(self.all_links) or bool(self.text)


_ocr_checked: bool | None = None


def ocr_available() -> bool:
    """检测当前系统是否支持 WinRT OCR。"""
    global _ocr_checked
    if _ocr_checked is not None:
        return _ocr_checked
    if not sys.platform.startswith("win"):
        _ocr_checked = False
        return False
    try:
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "[Windows.Media.Ocr.OcrEngine,Windows.Foundation,ContentType=WindowsRuntime] | Out-Null; "
                "$e=[Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages(); "
                "if ($e) { '1' } else { '0' }",
            ],
            capture_output=True,
            timeout=25,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        _ocr_checked = "1" in result.stdout.decode("utf-8", "ignore")
    except (OSError, subprocess.SubprocessError):
        _ocr_checked = False
    return _ocr_checked


def windows_ocr(image_path: str | Path, timeout: int = 60) -> tuple[str, str]:
    """调用系统 OCR，返回 (文本, 错误信息)。"""
    if not sys.platform.startswith("win"):
        return "", "当前系统不支持 Windows 内置 OCR"
    image_path = Path(image_path)
    if not image_path.exists():
        return "", "图片文件不存在"
    script_path = Path(tempfile.gettempdir()) / "quarkrelay_ocr.ps1"
    out_path = Path(tempfile.gettempdir()) / "quarkrelay_ocr.txt"
    try:
        script_path.write_text(_PS_SCRIPT, encoding="utf-8-sig")
        out_path.write_text("", encoding="utf-8")
        subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script_path),
                "-Path",
                str(image_path),
                "-Out",
                str(out_path),
            ],
            capture_output=True,
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        text = out_path.read_text(encoding="utf-8", errors="ignore")
        return text, ""
    except subprocess.TimeoutExpired:
        return "", "OCR 超时，请换一张更小的截图"
    except OSError as exc:
        return "", f"OCR 调用失败：{exc}"
    finally:
        for path in (script_path, out_path):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass


def decode_qr(image_path: str | Path) -> list[str]:
    """解码图片里的二维码，返回文本列表。"""
    try:
        import cv2  # 延迟导入，加快启动
    except ImportError:
        return []
    try:
        import numpy as np
    except ImportError:
        np = None

    try:
        image = cv2.imread(str(image_path))
    except Exception as exc:  # pragma: no cover
        logger.debug("读取图片失败：%s", exc)
        return []
    if image is None:
        return []

    detector = cv2.QRCodeDetector()
    found: list[str] = []

    def _scan(mat) -> None:
        try:
            text, _, _ = detector.detectAndDecode(mat)
        except Exception:
            return
        if text and text not in found:
            found.append(text)

    _scan(image)
    if not found and np is not None:
        # 小二维码放大后再试
        for scale in (2.0, 3.0, 0.5):
            resized = cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
            _scan(resized)
            if found:
                break
        if not found:
            gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
            for param in (
                cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1],
                cv2.adaptiveThreshold(
                    gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 5
                ),
            ):
                _scan(param)
                if found:
                    break
    return found


def recognize(image_path: str | Path, *, use_ocr: bool = True, use_qr: bool = True) -> RecognizeResult:
    """对一张图片做完整识别。"""
    result = RecognizeResult()
    if use_qr:
        for text in decode_qr(image_path):
            qr_links = extract(text)
            if qr_links:
                result.qr_links.extend(qr_links)
                if not result.engine:
                    result.engine = "二维码"
            elif text.strip():
                result.text = (result.text + "\n" + text).strip()

    if use_ocr:
        text, error = windows_ocr(image_path)
        if error:
            result.error = error
        elif text.strip():
            result.text = (result.text + "\n" + text).strip()
            result.links = extract(text)
            if not result.engine:
                result.engine = "系统 OCR"
    elif not result.engine:
        result.error = result.error or "已关闭 OCR"
    return result


def recognize_text(text: str) -> RecognizeResult:
    """直接对一段文本做链接识别（剪贴板通道）。"""
    return RecognizeResult(text=text, links=extract(text), engine="剪贴板")
