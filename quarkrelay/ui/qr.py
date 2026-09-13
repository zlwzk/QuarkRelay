"""登录页二维码：把内置浏览器页面里的二维码抠出来，放大、留够白边之后单独展示。

夸克和百度的登录页都把二维码画在网页里：夸克是矢量 ``<svg>``，百度是 ``<img>``。
页面里的那枚又小又靠边，还可能被自家浮层盖住，抬手扫很费劲。这里统一处理成
「白底卡片 + 静区 + 足够大」的一张图，放在登录窗口左边。

有个坑值得记下来：这两家的二维码本身都是**紧贴边缘**生成的（没有静区），
而静区是二维码规范的一部分 —— 少了它，很多扫码器（包括本项目的截图识链）
都会认不出来。所以卡片必须留白边，不能直接把图案铺满。
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
from collections.abc import Callable

from PySide6.QtCore import QByteArray, QObject, QRectF, QTimer, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPixmap
from PySide6.QtWidgets import QLabel, QPushButton, QVBoxLayout, QWidget

logger = logging.getLogger(__name__)

# 白底卡片的边长，以及四周留白占边长的比例（二维码的「静区」）
# 卡片给得大一点：夸克那枚是 105×105 的超高密度码，屏幕上每格像素越多越好扫
CARD_SIZE = 400
QUIET_RATIO = 0.075
# 矢量图先按这个倍数放大渲染，再缩回目标尺寸，边缘才干净
OVERSAMPLE = 3

# 注入到页面里的取码器。只注入一次，之后每轮用 probe/grab 两个小调用，
# 免得把几十万字节的 SVG 每 1.2 秒搬一次过桥。
JS_HELPERS = r"""
window.__qrrelay = (function () {
  function names(el) {
    var cls = el.className;
    if (cls && typeof cls !== 'string') cls = cls.baseVal || '';
    return String(cls || '') + ' ' + String(el.id || '');
  }
  // 页面里可能有好几枚二维码（比如夸克页底部还有一枚「下载 App」），把这些排掉
  function excluded(el) {
    for (var node = el; node && node !== document.body; node = node.parentElement) {
      var mark = names(node).toLowerCase();
      if (mark.indexOf('download') >= 0 || mark.indexOf('app-qr') >= 0 || mark.indexOf('qr-app') >= 0) {
        return true;
      }
    }
    return false;
  }
  function usable(el) {
    if (!el || excluded(el)) return false;
    var rect = el.getBoundingClientRect();
    if (rect.width < 80 || rect.height < 80) return false;
    var ratio = rect.width / rect.height;
    return ratio > 0.8 && ratio < 1.25;
  }
  function hash(text) {
    var value = 5381;
    for (var i = 0; i < text.length; i++) value = ((value * 33) ^ text.charCodeAt(i)) >>> 0;
    return value.toString(16) + ':' + text.length;
  }
  function pickImage() {
    var img = document.querySelector('img.tang-pass-qrcode-img');
    if (usable(img) && img.src) return img;
    var all = document.querySelectorAll('img');
    for (var i = 0; i < all.length; i++) {
      var mark = (names(all[i]) + ' ' + String(all[i].getAttribute('src') || '')).toLowerCase();
      if (mark.indexOf('qr') < 0 && mark.indexOf('qrcode') < 0) continue;
      if (usable(all[i]) && all[i].src) return all[i];
    }
    return null;
  }
  function pickSvg() {
    var container = document.querySelector('[class*="qr-code-container"]');
    var svg = container ? container.querySelector('svg') : null;
    if (usable(svg)) return svg;
    var all = document.querySelectorAll('svg');
    for (var i = 0; i < all.length; i++) {
      if (names(all[i]).toLowerCase().indexOf('qr') < 0) continue;
      if (usable(all[i])) return all[i];
    }
    return null;
  }
  return {
    // 便宜：只回报「有没有、是哪一枚、变了没」
    probe: function () {
      var img = pickImage();
      if (img) return JSON.stringify({ found: true, kind: 'img', sig: 'img:' + hash(String(img.src)) });
      var svg = pickSvg();
      if (svg) {
        var text = new XMLSerializer().serializeToString(svg);
        return JSON.stringify({ found: true, kind: 'svg', sig: 'svg:' + hash(text) });
      }
      return JSON.stringify({ found: false });
    },
    // 贵：真的把像素取出来。SVG 交回原文，由本地按需要的尺寸清晰渲染
    grab: function () {
      var img = pickImage();
      if (img) {
        try {
          var width = img.naturalWidth || img.width;
          var height = img.naturalHeight || img.height;
          if (!width || !height) return JSON.stringify({ found: false });
          var canvas = document.createElement('canvas');
          canvas.width = width;
          canvas.height = height;
          canvas.getContext('2d').drawImage(img, 0, 0, width, height);
          return JSON.stringify({
            found: true, kind: 'img', value: canvas.toDataURL('image/png'), width: width, height: height
          });
        } catch (e) {
          return JSON.stringify({ found: false, error: String(e) });
        }
      }
      var svg = pickSvg();
      if (svg) {
        return JSON.stringify({ found: true, kind: 'svg', value: new XMLSerializer().serializeToString(svg) });
      }
      return JSON.stringify({ found: false });
    },
    // 二维码失效时，替你在页面里点一下刷新
    refresh: function () {
      var targets = [
        '#TANGRAM__PSP_3__QrcodeMain',
        '.tang-pass-qrcode-imgWrapper',
        'img.tang-pass-qrcode-img',
        '[class*="qr-code-container"]'
      ];
      for (var i = 0; i < targets.length; i++) {
        var node = document.querySelector(targets[i]);
        if (node) {
          node.click();
          return 'click';
        }
      }
      var all = document.querySelectorAll('a, button, div, span, p');
      for (var j = 0; j < all.length; j++) {
        var text = String(all[j].textContent || '').replace(/\s+/g, '');
        if (text.length <= 8 && (text.indexOf('刷新') >= 0 || text.indexOf('重新获取') >= 0)) {
          all[j].click();
          return 'click';
        }
      }
      return '';
    }
  };
})();
'ok';
"""

PROBE_CALL = "window.__qrrelay ? window.__qrrelay.probe() : ''"
GRAB_CALL = "window.__qrrelay ? window.__qrrelay.grab() : ''"
REFRESH_CALL = "window.__qrrelay ? window.__qrrelay.refresh() : ''"


def _loads(raw: object) -> dict | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _image_from_svg(text: str, target: int) -> QImage | None:
    """把矢量二维码渲染成位图。"""
    from PySide6.QtSvg import QSvgRenderer

    renderer = QSvgRenderer(QByteArray(text.encode("utf-8")))
    if not renderer.isValid():
        logger.warning("二维码 SVG 解析失败（%d 字节）", len(text))
        return None
    side = max(160, target) * OVERSAMPLE
    image = QImage(side, side, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.white)
    painter = QPainter(image)
    # 抗锯齿会把相邻的模块糊在一起，尺寸一小就扫不出来了，这里必须关掉
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
    renderer.render(painter, QRectF(0, 0, side, side))
    painter.end()
    return image


def _image_from_data_url(value: str) -> QImage | None:
    if not value.startswith("data:"):
        return None
    _, _, body = value.partition(",")
    if not body:
        return None
    try:
        raw = base64.b64decode(body)
    except (ValueError, binascii.Error):
        logger.warning("二维码图片不是合法的 base64")
        return None
    image = QImage()
    if not image.loadFromData(raw):
        logger.warning("二维码图片解码失败（%d 字节）", len(raw))
        return None
    return image


def image_from_payload(payload: dict, target: int) -> QImage | None:
    """把页面交回来的二维码变成位图。target 是打算显示成多大（像素）。"""
    kind = str(payload.get("kind") or "")
    value = str(payload.get("value") or "")
    if not value:
        return None
    if kind == "svg":
        return _image_from_svg(value, target)
    return _image_from_data_url(value)


def card_pixmap(image: QImage, card: int = CARD_SIZE, dpr: float = 1.0) -> QPixmap:
    """把二维码贴到白底卡片中央，四周留出静区。"""
    scale = max(1.0, float(dpr))
    side = max(96, int(round(card * scale)))
    board = QImage(side, side, QImage.Format.Format_ARGB32_Premultiplied)
    board.fill(Qt.GlobalColor.white)
    inner = int(round(side * (1 - 2 * QUIET_RATIO)))
    offset = (side - inner) // 2
    painter = QPainter(board)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
    painter.drawImage(QRectF(offset, offset, inner, inner), image)
    painter.end()
    pixmap = QPixmap.fromImage(board)
    pixmap.setDevicePixelRatio(scale)
    return pixmap


def placeholder_pixmap(card: int = CARD_SIZE, dpr: float = 1.0, text: str = "正在获取二维码…") -> QPixmap:
    scale = max(1.0, float(dpr))
    side = max(96, int(round(card * scale)))
    board = QImage(side, side, QImage.Format.Format_ARGB32_Premultiplied)
    board.fill(QColor(255, 255, 255, 20))
    painter = QPainter(board)
    painter.setPen(QColor(255, 255, 255, 110))
    painter.drawText(board.rect(), Qt.AlignmentFlag.AlignCenter, text)
    painter.end()
    pixmap = QPixmap.fromImage(board)
    pixmap.setDevicePixelRatio(scale)
    return pixmap


class QrPanel(QWidget):
    """登录窗口左边那块「放大后的二维码」。"""

    def __init__(
        self,
        hint: str,
        parent: QWidget | None = None,
        on_refresh: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("QrPanel")
        self.setFixedWidth(CARD_SIZE + 8)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        self.canvas = QLabel()
        self.canvas.setFixedSize(CARD_SIZE, CARD_SIZE)
        self.canvas.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.canvas.setPixmap(placeholder_pixmap(CARD_SIZE, self.devicePixelRatioF()))
        layout.addWidget(self.canvas, 0, Qt.AlignmentFlag.AlignHCenter)

        self.hint = QLabel(hint)
        self.hint.setObjectName("CardTitle")
        self.hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.hint.setWordWrap(True)
        layout.addWidget(self.hint)

        self.note = QLabel("正在从登录页取二维码…")
        self.note.setObjectName("Muted")
        self.note.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.note.setWordWrap(True)
        layout.addWidget(self.note)

        if on_refresh is not None:
            self.refresh = QPushButton("刷新二维码")
            self.refresh.setObjectName("Ghost")
            self.refresh.setCursor(Qt.CursorShape.PointingHandCursor)
            self.refresh.clicked.connect(on_refresh)
            layout.addWidget(self.refresh, 0, Qt.AlignmentFlag.AlignHCenter)

        layout.addStretch(1)

    def set_waiting(self, text: str = "正在获取二维码…") -> None:
        self.canvas.setPixmap(placeholder_pixmap(CARD_SIZE, self.devicePixelRatioF(), text))

    def set_image(self, image: QImage, note: str = "") -> None:
        self.canvas.setPixmap(card_pixmap(image, CARD_SIZE, self.devicePixelRatioF()))
        self.note.setText(note)

    def set_note(self, text: str) -> None:
        self.note.setText(text)


class QrWatcher(QObject):
    """盯着登录页，把页面里的二维码抠出来喂给面板。"""

    # 连续几轮都没找到（页面还在加载、或者根本没二维码）就换一句更实在的话
    MISS_LIMIT = 6

    def __init__(
        self,
        page,
        panel: QrPanel,
        parent: QObject | None = None,
        interval_ms: int = 1200,
    ) -> None:
        super().__init__(parent)
        self._page = page
        self._panel = panel
        self._sig = ""
        self._misses = 0
        self._busy = False
        self._injected = False
        self._timer = QTimer(self)
        self._timer.setInterval(interval_ms)
        self._timer.timeout.connect(self._tick)

    def start(self) -> None:
        self._timer.start()
        self._tick()

    def stop(self) -> None:
        self._timer.stop()

    def refresh(self) -> None:
        """用户在面板上点了「刷新二维码」：先在页面里点一下，点不到就整页重载。"""
        self._injected = False
        self._sig = ""
        self._panel.set_waiting("正在刷新二维码…")
        self._page.runJavaScript(REFRESH_CALL, self._on_refresh)

    def _on_refresh(self, raw: object) -> None:
        if isinstance(raw, str) and raw.startswith("click"):
            self._panel.set_note("已在登录页里刷新，稍等一秒")
        else:
            from PySide6.QtWebEngineCore import QWebEnginePage

            self._panel.set_note("登录页已重新加载")
            self._page.triggerAction(QWebEnginePage.WebAction.Reload)

    def _tick(self) -> None:
        if self._busy:
            return
        self._busy = True
        if not self._injected:
            # 页面每次跳转都会把注入清掉，这里顺手补回来
            self._injected = True
            self._page.runJavaScript(JS_HELPERS)
            self._busy = False
            return
        self._page.runJavaScript(PROBE_CALL, self._on_probe)

    def _on_probe(self, raw: object) -> None:
        self._busy = False
        info = _loads(raw)
        if info is None:
            self._injected = False
            return
        if not info.get("found"):
            self._misses += 1
            if self._misses >= self.MISS_LIMIT:
                self._panel.set_note("登录页里没找到二维码：多半是已经登录过了，点「显示登录页」看一眼")
            return
        self._misses = 0
        sig = str(info.get("sig") or "")
        if sig == self._sig:
            return
        self._sig = sig
        self._busy = True
        self._page.runJavaScript(GRAB_CALL, self._on_grab)

    def _on_grab(self, raw: object) -> None:
        self._busy = False
        payload = _loads(raw)
        if not payload or not payload.get("found"):
            self._injected = False
            return
        # 矢量图要比卡片本身再大一点渲染，缩回去时边缘才干净；位图则原样读取
        target = max(self._panel.canvas.width(), 160)
        image = image_from_payload(payload, target)
        if image is None:
            self._panel.set_note("二维码取到了但没解析成功，点「显示登录页」直接在页面里扫")
            return
        self._panel.set_image(image, "与登录页里是同一枚二维码，扫这枚更清楚")
