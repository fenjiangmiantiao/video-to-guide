"""画面预览区：显示当前步骤对应的截图，可切换、可放大。

为什么要单独做一个控件：
- 主窗口的职责是「调度」，不该塞进缩放、翻页这些绘图细节；
- 预览区在三种状态下行为不同（没有步骤 / 有步骤无截图 / 有截图），
  这些分支集中在一个类里，比散在 show_step 里更好维护。

图片用自绘画布而不是 QLabel：QLabel 的 pixmap 需要在「布局完成后」重新缩放，
否则会按窗口刚打开时的临时尺寸算一次，之后不再更新，图就被压扁。
paintEvent 里每次按真实尺寸重算，永远不会错，也不用管时序。
"""

from __future__ import annotations

from collections import OrderedDict
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPixmap, QTransform
from PySide6.QtWidgets import (
    QDialog, QFrame, QGraphicsScene, QGraphicsView, QHBoxLayout, QLabel,
    QPushButton, QSizePolicy, QVBoxLayout, QWidget,
)

from . import theme


class _ZoomView(QGraphicsView):
    """支持 Ctrl+滚轮缩放的图片视图。"""

    def __init__(self, scene: QGraphicsScene, parent=None):
        super().__init__(scene, parent)
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.AnchorViewCenter)
        self.setRenderHints(QPainter.SmoothPixmapTransform | QPainter.Antialiasing)
        self.setStyleSheet(f"background:{theme.PANEL};border:none;")
        self._zoom = 1.0

    def wheelEvent(self, event):
        if event.modifiers() & Qt.ControlModifier:
            step = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
            self._zoom = max(0.1, min(8.0, self._zoom * step))
            self.setTransform(QTransform().scale(self._zoom, self._zoom))
            event.accept()
            return
        super().wheelEvent(event)

    def fit(self) -> None:
        self._zoom = 1.0
        self.setTransform(QTransform())
        self.fitInView(self.sceneRect(), Qt.KeepAspectRatio)


class ImageViewer(QDialog):
    """放大查看单张截图。"""

    def __init__(self, path: Path | str, caption: str = "", parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"查看截图 · {caption}" if caption else "查看截图")
        self.resize(1200, 820)
        self.setStyleSheet(f"QDialog{{background:{theme.BG};}}")

        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(6)

        pix = QPixmap(str(path))
        self.scene = QGraphicsScene(self)
        if pix.isNull():
            label = QLabel(f"无法读取这张截图：\n{path}")
            label.setAlignment(Qt.AlignCenter)
            label.setStyleSheet(f"color:{theme.FAIL};font-size:14px;")
            lay.addWidget(label, 1)
            self.view = None
        else:
            self.scene.addPixmap(pix)
            self.view = _ZoomView(self.scene, self)
            self.view.setSceneRect(self.scene.itemsBoundingRect())
            lay.addWidget(self.view, 1)

        bar = QHBoxLayout()
        hint = QLabel("Ctrl + 滚轮缩放　·　按住拖动平移　·　Esc 关闭")
        hint.setStyleSheet(f"color:{theme.TEXT_FAINT};font-size:12px;")
        bar.addWidget(hint, 1)
        btn_fit = QPushButton("适应窗口")
        btn_fit.clicked.connect(lambda: self.view.fit() if self.view else None)
        btn_close = QPushButton("关闭")
        btn_close.clicked.connect(self.accept)
        bar.addWidget(btn_fit)
        bar.addWidget(btn_close)
        lay.addLayout(bar)

    def showEvent(self, event):
        super().showEvent(event)
        if self.view is not None:
            self.view.fit()


# 预览图 LRU 缓存（模块级：多个 PreviewPane 实例共用，切换记录时也能命中）
_PIX_CACHE: "OrderedDict[str, QPixmap]" = OrderedDict()
_PIX_LIMIT = 8


class _Canvas(QWidget):
    """按当前控件尺寸等比绘制图片；没有图片时画一行提示文字。"""

    double_clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pix = QPixmap()
        self._msg = ""
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMinimumSize(160, 120)

    def set_pixmap(self, pix: QPixmap) -> None:
        self._pix = pix
        self._msg = ""
        self.update()

    def set_message(self, text: str) -> None:
        self._pix = QPixmap()
        self._msg = text
        self.update()

    def has_pixmap(self) -> bool:
        return not self._pix.isNull()

    def paintEvent(self, event):
        painter = QPainter(self)
        if self._pix.isNull():
            if self._msg:
                painter.setPen(QColor(theme.TEXT_FAINT))
                font = painter.font()
                font.setPointSizeF(10.5)
                painter.setFont(font)
                painter.drawText(self.rect().adjusted(16, 8, -16, -8),
                                 Qt.AlignCenter | Qt.TextWordWrap, self._msg)
            return
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        area = self.rect().adjusted(2, 2, -2, -2)
        if area.width() < 8 or area.height() < 8:
            return
        scaled = self._pix.scaled(area.size(), Qt.KeepAspectRatio,
                                  Qt.SmoothTransformation)
        painter.drawPixmap(area.x() + (area.width() - scaled.width()) // 2,
                           area.y() + (area.height() - scaled.height()) // 2,
                           scaled)

    def mouseDoubleClickEvent(self, event):
        if self.has_pixmap():
            self.double_clicked.emit()
        super().mouseDoubleClickEvent(event)


class PreviewPane(QFrame):
    """当前步骤的截图预览：标题行 + 图片 + 多张切换。"""

    zoom_requested = Signal(str, str)   # 图片绝对路径, 说明文字

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("previewPane")
        self.setStyleSheet(f"#previewPane{{background:{theme.PANEL};"
                           f"border:1px solid {theme.BORDER};border-radius:8px;}}")
        self.setMinimumHeight(180)

        self._items: list[tuple[Path, str, str]] = []   # (路径, 时间文字, 说明)
        self._index = 0

        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 10)
        lay.setSpacing(6)

        head = QHBoxLayout()
        head.setSpacing(6)
        self.lbl_title = QLabel("画面预览")
        self.lbl_title.setStyleSheet(f"color:{theme.TEXT};font-weight:600;font-size:13px;")
        self.lbl_time = QLabel("")
        self.lbl_time.setStyleSheet(f"color:{theme.TEXT_DIM};font-size:12px;")
        head.addWidget(self.lbl_title)
        head.addWidget(self.lbl_time)
        head.addStretch(1)

        self.btn_prev = QPushButton("◀")
        self.btn_next = QPushButton("▶")
        for b in (self.btn_prev, self.btn_next):
            b.setFixedWidth(34)
            b.setToolTip("切换本步骤的其他截图")
        self.btn_prev.clicked.connect(lambda: self._step(-1))
        self.btn_next.clicked.connect(lambda: self._step(1))
        self.btn_zoom = QPushButton("放大")
        self.btn_zoom.setToolTip("放大查看细节（也可双击图片）")
        self.btn_zoom.clicked.connect(self._zoom)
        head.addWidget(self.btn_prev)
        head.addWidget(self.btn_next)
        head.addWidget(self.btn_zoom)
        lay.addLayout(head)

        self.canvas = _Canvas()
        self.canvas.double_clicked.connect(self._zoom)
        lay.addWidget(self.canvas, 1)

        self._sync_buttons()

    # ------------------------------------------------------------ 对外接口
    def clear(self, message: str = "选中左侧步骤后，这里显示对应的截图。") -> None:
        self._items = []
        self._index = 0
        self.canvas.set_message(message)
        self.lbl_time.setText("")
        self._sync_buttons()

    def set_images(self, items: list[tuple[Path, str, str]]) -> None:
        """items: [(图片路径, 时间文字, 说明)]，按步骤顺序传入。"""
        self._items = list(items)
        self._index = 0
        if not self._items:
            self.clear("这一步没有可用的截图（可能只依据字幕生成）。")
            return
        self._render()

    def current_path(self) -> Path | None:
        if not self._items:
            return None
        return self._items[self._index][0]

    # ------------------------------------------------------------ 内部
    def _step(self, delta: int) -> None:
        if len(self._items) < 2:
            return
        self._index = (self._index + delta) % len(self._items)
        self._render()

    def _zoom(self) -> None:
        path = self.current_path()
        if not path:
            return
        caption = self._items[self._index][1]
        self.zoom_requested.emit(str(path), caption)

    def _render(self) -> None:
        path, time_text, caption = self._items[self._index]
        pix = self._cached(str(path))
        total = len(self._items)
        head = f"{time_text}"
        if total > 1:
            head += f"　·　第 {self._index + 1}/{total} 张"
        self.lbl_time.setText(head)
        self.lbl_time.setToolTip(caption)
        if pix.isNull():
            self.canvas.set_message(f"截图文件缺失：{path.name}")
        else:
            self.canvas.set_pixmap(pix)
        self._sync_buttons()

    def _cached(self, key: str) -> QPixmap:
        """按路径缓存已解码的图。

        来回翻步骤时同一张图会被反复请求；QPixmap 解码 1200×860 的 JPEG 约 6–9ms，
        缓存命中后降到接近 0。只保留最近 8 张（一张约 4MB，共约 32MB）。
        """
        hit = _PIX_CACHE.get(key)
        if hit is not None and not hit.isNull():
            _PIX_CACHE.move_to_end(key)
            return hit
        pix = QPixmap(key)
        if not pix.isNull():
            _PIX_CACHE[key] = pix
            while len(_PIX_CACHE) > _PIX_LIMIT:
                _PIX_CACHE.popitem(last=False)
        return pix

    def _sync_buttons(self) -> None:
        multi = len(self._items) > 1
        self.btn_prev.setEnabled(multi)
        self.btn_next.setEnabled(multi)
        self.btn_zoom.setEnabled(bool(self._items) and self.canvas.has_pixmap())