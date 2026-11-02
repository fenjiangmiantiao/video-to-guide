"""设置对话框：模型配置 + 视频处理参数。

界面只有两个一级分类：
  「本地模型」  —— LM Studio / Ollama / 自定义本地服务，免费但要自己先启动；
  「其他模型」  —— 云端服务商，需要 API Key。
服务商是二级选择，模型列表、任务角色在两种来源下完全一致。

模型分两层保存，才能让"看图"和"写步骤"用不同来源：
  connections = {服务商: {base_url, api_key}}  连接配置只存一份；
  vision_role / text_role = {provider, model}  任务角色引用连接。
"""

from __future__ import annotations

import copy
import os
import tempfile
import time

from PySide6.QtCore import Qt, QThread, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QAbstractItemView, QButtonGroup, QCheckBox,
                               QComboBox, QDialog, QDialogButtonBox, QFileDialog,
                               QFrame, QFormLayout, QGroupBox, QHBoxLayout,
                               QHeaderView, QInputDialog, QLabel, QLineEdit,
                               QMessageBox, QProgressBar, QPushButton,
                               QRadioButton, QScrollArea, QSpinBox, QTabWidget,
                               QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from .. import asr_models
from ..config import ROLE_TEXT, ROLE_VISION, Settings
from ..model_catalog import (ModelEntry, ModelListResult, fetch_models,
                             protocol_for)
from ..providers import PROVIDERS, get, sends_thinking_flag
from ..vision.lms_client import ChatClient

# 一级分类：本地 / 其他。本地类里的服务商不需要 API Key。
LOCAL_IDS = tuple(p.id for p in PROVIDERS if p.local)
CLOUD_IDS = tuple(p.id for p in PROVIDERS if not p.local)

ROLE_LABELS = {ROLE_VISION: "视觉分析（看截图）", ROLE_TEXT: "步骤整理（写步骤）"}


def _short(text: str, limit: int = 54) -> str:
    """模型 ID 常常是一长串路径，状态卡里只显示最后一段，认得出是哪个模型就够。"""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    tail = text.split("/")[-1]
    if len(tail) + 2 <= limit:
        return "…/" + tail
    return "…" + text[-(limit - 1):]


class ModelFetchWorker(QThread):
    """在后台线程里请求模型列表。

    界面线程不能直接发 HTTP：本地服务没启动时会卡住十几秒，窗口看起来像死了。
    每次请求带一个序号（gen），对话框只认最新一次的返回值，避免用户快速切换
    服务商时旧请求把新列表覆盖掉。
    """

    done = Signal(object)

    def __init__(self, provider_id: str, base_url: str, api_key: str,
                 gen: int, timeout: float = 15.0, parent=None):
        super().__init__(parent)
        self.provider_id = provider_id
        self.base_url = base_url
        self.api_key = api_key
        self.gen = gen
        self.timeout = timeout

    def run(self) -> None:
        try:
            result = fetch_models(self.provider_id, self.base_url,
                                  self.api_key, self.timeout)
        except Exception as e:                    # 兜底：worker 里绝不让异常逃出去
            result = ModelListResult(ok=False, provider=self.provider_id,
                                     error=f"{type(e).__name__}: {e}")
        result_dict = result.to_dict()
        result_dict["gen"] = self.gen
        self.done.emit(result_dict)


def _tiny_png(size: int = 48, color: tuple = (220, 40, 40)) -> bytes:
    """生成一张纯色 PNG 作为视觉测试图。

    自己生成而不是拿用户的截图：测试图不含任何私人内容，也不依赖本地文件。
    """
    import struct
    import zlib

    width = height = size
    raw = b"".join(b"\x00" + bytes(color) * width for _ in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return (struct.pack(">I", len(data)) + body
                + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


class HealthWorker(QThread):
    """只探测接口是否可达、鉴权是否正确，不发推理请求、不花钱。"""

    done = Signal(bool, str)

    def __init__(self, base_url: str, model: str, api_key: str = "",
                 timeout: float = 15.0, parent=None):
        super().__init__(parent)
        self.base_url = base_url
        self.model = model
        self.api_key = api_key
        self.timeout = timeout

    def run(self) -> None:
        try:
            client = ChatClient(self.base_url, self.model, timeout=self.timeout,
                                api_key=self.api_key)
            ok, detail = client.health()
        except Exception as e:                     # worker 里绝不让异常逃出去
            ok, detail = False, f"{type(e).__name__}: {e}"
        self.done.emit(bool(ok), str(detail))


class VisionTestWorker(QThread):
    """真的发一张图，验证选中的模型能不能处理图像输入。

    「接口连得上」不等于「模型能看图」——这一层就是用来区分这两件事的。
    """

    done = Signal(bool, str, float)

    def __init__(self, base_url: str, model: str, api_key: str = "",
                 disable_thinking: bool = True, timeout: float = 120.0,
                 parent=None):
        super().__init__(parent)
        self.base_url = base_url
        self.model = model
        self.api_key = api_key
        self.disable_thinking = disable_thinking
        self.timeout = timeout

    def run(self) -> None:
        started = time.time()
        tmp_path = ""
        try:
            fd, tmp_path = tempfile.mkstemp(suffix=".png", prefix="vtg_vision_test_")
            os.close(fd)
            with open(tmp_path, "wb") as fh:
                fh.write(_tiny_png())
            client = ChatClient(self.base_url, self.model, timeout=self.timeout,
                                max_tokens=128, api_key=self.api_key,
                                disable_thinking=self.disable_thinking)
            result = client.describe_image(
                tmp_path, "这张图片是什么颜色？只回答颜色名称，不要解释。")
            text = str(getattr(result, "text", "") or "").strip()
            if text:
                ok, detail = True, "模型返回：" + text[:120]
            else:
                ok, detail = False, "模型返回了空内容（可能不支持图像输入）"
        except Exception as e:
            ok, detail = False, f"{type(e).__name__}: {e}"
        finally:
            if tmp_path:
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
        self.done.emit(ok, detail, time.time() - started)


class SettingsDialog(QDialog):
    """改完点「保存」会写回传入的 Settings 对象，由调用方决定何时落盘。"""

    def __init__(self, settings: Settings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("设置")
        self.resize(940, 730)
        self.settings = settings
        self._pid = settings.role(ROLE_VISION)["provider"]
        # 每次"获取模型"递增，回调只认最新一次，防止快速切换服务商时旧结果覆盖新列表
        self._fetch_gen = 0
        self._fetch_worker = None
        self._health_worker = None
        self._vision_worker = None
        # 点"取消"要把内存里改过的配置恢复原样，否则用户以为没保存其实已经改了
        self._orig = {k: copy.deepcopy(getattr(settings, k))
                      for k in ("hidden_models", "manual_models", "model_cache",
                                "connections", "vision_role", "text_role")}
        self._build()
        self._sync_from_settings()

    # ---------------------------------------------------------------- 取消/关闭
    def reject(self) -> None:
        for key, value in self._orig.items():
            setattr(self.settings, key, value)
        self._stop_workers()
        super().reject()

    def _stop_workers(self) -> None:
        for attr in ("_fetch_worker", "_health_worker", "_vision_worker"):
            w = getattr(self, attr, None)
            if w is None:
                continue
            try:
                if w.isRunning():
                    w.requestInterruption()
                    w.wait(3000)
            except RuntimeError:
                pass          # worker 已经跑完并被 deleteLater 销毁，不用再管
            setattr(self, attr, None)

    def closeEvent(self, event) -> None:      # noqa: N802 (Qt 命名)
        self._stop_workers()
        super().closeEvent(event)

    # ---------------------------------------------------------------- 构建界面
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 12)
        root.setSpacing(10)

        title = QLabel("设置")
        title.setStyleSheet("font-size:17px;font-weight:700;color:#1f2328;")
        sub = QLabel("「看图」和「写步骤」可以分别用不同来源的模型，互不影响。"
                     "本机服务免费，但要先自己启动；云端服务需要 API Key。")
        sub.setWordWrap(True)
        sub.setStyleSheet("color:#57606a;font-size:12px;")
        root.addWidget(title)
        root.addWidget(sub)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._wrap_scroll(self._page_models()), "模型配置")
        self.tabs.addTab(self._wrap_scroll(self._page_video()), "视频处理")
        self.tabs.addTab(self._wrap_scroll(self._page_export()), "导出")
        self.tabs.addTab(self._wrap_scroll(self._page_components()), "额外内容")
        root.addWidget(self.tabs, 1)

        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Save).setText("保存")
        bb.button(QDialogButtonBox.Cancel).setText("取消")
        bb.accepted.connect(self._save)
        bb.rejected.connect(self.reject)
        root.addWidget(bb)

    @staticmethod
    def _wrap_scroll(page: QWidget) -> QScrollArea:
        """设置页包一层滚动区：展开「连接与密钥」后内容会变长，
        不能让对话框一直长到屏幕外面去。"""
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.NoFrame)
        area.setWidget(page)
        area.setStyleSheet("QScrollArea{background:transparent;border:none;}")
        return area



    # ---------------------------------------------------------------- 页 4：可选组件
    def _page_components(self) -> QWidget:
        """可选依赖的下载/卸载。

        为什么放设置里而不是塞进 doctor 每一行：doctor 的职责是"诊断并解释"，
        装东西属于另一种动作（要进度、要取消、要选版本），混在一张检查表里
        会让"检查"变成"改系统"，边界就模糊了。
        """
        from .. import components as comp

        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(10, 14, 10, 10)
        v.setSpacing(12)

        tip = QLabel("语音识别随软件自带，换档位只影响准确率和速度，不装也能用。"
                     "下面的可选组件是锦上添花，普通录屏一个都不需要。")
        tip.setWordWrap(True)
        tip.setStyleSheet("color:#57606a;font-size:12px;")
        v.addWidget(tip)

        # 语音模型是"额外内容"里最该被看见的一项：它直接决定字幕对不对
        v.addWidget(self._build_asr_box())

        self._comp_rows = {}
        for m in comp.MODULES:
            v.addWidget(self._component_card(m))
        v.addStretch(1)
        return page

    def _component_card(self, m) -> QWidget:
        from .. import components as comp, download as dl

        card = QGroupBox(m.name)
        box = QVBoxLayout(card)
        box.setContentsMargins(12, 14, 12, 10)
        box.setSpacing(6)

        desc = QLabel(m.summary)
        desc.setWordWrap(True)
        desc.setStyleSheet("color:#57606a;font-size:12px;")
        box.addWidget(desc)

        state = QLabel("")
        state.setWordWrap(True)
        box.addWidget(state)

        bar = QProgressBar()
        bar.setRange(0, 100)
        bar.setVisible(False)
        bar.setFixedHeight(6)
        bar.setTextVisible(False)
        box.addWidget(bar)

        row = QHBoxLayout()
        btn_install = QPushButton("下载并安装")
        btn_local = QPushButton("从本地文件安装…")
        btn_remove = QPushButton("卸载")
        btn_cancel = QPushButton("取消")
        btn_cancel.setVisible(False)
        for b in (btn_install, btn_local, btn_remove, btn_cancel):
            row.addWidget(b)
        row.addStretch(1)
        box.addLayout(row)

        row_ref = {}
        self._comp_rows[m.id] = (state, bar, btn_install, btn_local, btn_remove,
                                 btn_cancel, row_ref)

        def refresh() -> None:
            installed = dl.is_installed(m, self._portable(), self._app_root())
            ver = dl.installed_version(m, self._portable(), self._app_root())
            can_dl = comp.downloadable(m)
            btn_install.setVisible(can_dl)
            btn_local.setVisible(can_dl)
            btn_remove.setVisible(installed)
            if installed:
                state.setText(f"已安装（版本 {ver or m.version}）")
                state.setStyleSheet("color:#1a7f37;font-size:12px;")
            elif can_dl:
                state.setText(f"未安装 · 下载约 {m.size // 1048576} MB。{m.note}")
                state.setStyleSheet("color:#9a6700;font-size:12px;")
            else:
                state.setText(f"暂不可下载：{m.note}")
                state.setStyleSheet("color:#57606a;font-size:12px;")

        row_ref["refresh"] = refresh
        refresh()

        def start(archive=None) -> None:
            from .. import components as comp_mod
            from .worker import ComponentWorker

            if getattr(self, "_comp_worker", None) and self._comp_worker.isRunning():
                return
            state.setText("准备下载…")
            state.setStyleSheet("color:#0969da;font-size:12px;")
            bar.setVisible(True)
            bar.setValue(0)
            btn_install.setEnabled(False)
            btn_local.setEnabled(False)
            btn_remove.setVisible(False)
            btn_cancel.setVisible(True)

            w = ComponentWorker(m, self._portable(), self._app_root(), archive=archive, parent=self)
            self._comp_worker = w

            def on_progress(phase: str, pct: float, text: str) -> None:
                bar.setValue(int(pct))
                names = {"download": "下载中", "verify": "校验中", "extract": "解压中",
                         "install": "安装中", "verify-run": "试运行"}
                state.setText(f"{names.get(phase, phase)}　{text}")

            def done(path: str) -> None:
                bar.setVisible(False)
                btn_install.setEnabled(True)
                btn_local.setEnabled(True)
                btn_cancel.setVisible(False)
                refresh()
                QMessageBox.information(self, "安装完成",
                                        f"{m.name} 已安装到：\n{path}\n\n保存设置后生效。")

            def fail(msg: str) -> None:
                bar.setVisible(False)
                btn_install.setEnabled(True)
                btn_local.setEnabled(True)
                btn_cancel.setVisible(False)
                refresh()
                QMessageBox.critical(self, "安装失败", msg)

            def cancelled() -> None:
                bar.setVisible(False)
                btn_install.setEnabled(True)
                btn_local.setEnabled(True)
                btn_cancel.setVisible(False)
                refresh()
                state.setText("已取消（已下载的部分会保留，下次继续）")

            w.progress.connect(on_progress)
            w.finished_ok.connect(done)
            w.failed.connect(fail)
            w.cancelled.connect(cancelled)
            w.start()

        def pick_local() -> None:
            path, _ = QFileDialog.getOpenFileName(
                self, f"选择 {m.name} 的安装包", "", "压缩包 (*.zip)")
            if path:
                start(archive=Path(path))

        def do_remove() -> None:
            if QMessageBox.question(self, "卸载组件",
                                    f"确定删除 {m.name} 吗？\n\n"
                                    "已有的手册和分析记录不受影响。",
                                    QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
                return
            dl.uninstall(m, self._portable(), self._app_root())
            refresh()

        btn_install.clicked.connect(lambda: start())
        btn_local.clicked.connect(pick_local)
        btn_remove.clicked.connect(do_remove)
        btn_cancel.clicked.connect(
            lambda: getattr(self, "_comp_worker", None) and self._comp_worker.cancel())
        return card

    def _portable(self) -> bool:
        return bool(getattr(self.settings, "portable_data", False))

    def _app_root(self) -> Path:
        from ..config import PROJECT_ROOT
        return PROJECT_ROOT

    # ---------------------------------------------------------------- 页 3：导出
    def _page_export(self) -> QWidget:
        """导出的默认行为与命名规则。

        用户反馈"每次导出都要重新选模式、重新起名字"，所以把高频的三件事
        （默认模式、默认位置、名字规则）放到设置里，一次配好。
        """
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(10, 14, 10, 10)
        v.setSpacing(12)

        g1 = QGroupBox("默认导出方式")
        f1 = QFormLayout(g1)
        f1.setContentsMargins(12, 14, 12, 10)
        self.cb_export_mode = QComboBox()
        self.cb_export_mode.addItems(["轻量手册（推荐）", "完整手册包（含源视频）"])
        self.cb_export_mode.setFixedWidth(240)
        f1.addRow("方式", self.cb_export_mode)

        row = QHBoxLayout()
        self.ed_export_root = QLineEdit()
        self.ed_export_root.setPlaceholderText("留空 = 文档目录下的 Video to Guide/Exports")
        btn_pick = QPushButton("浏览…")
        row.addWidget(self.ed_export_root, 1)
        row.addWidget(btn_pick)
        f1.addRow("默认位置", row)
        v.addWidget(g1)

        g2 = QGroupBox("命名规则")
        f2 = QFormLayout(g2)
        f2.setContentsMargins(12, 14, 12, 10)
        self.cb_export_layout = QComboBox()
        self.cb_export_layout.addItems(["按视频 / 批次分文件夹（推荐）", "全部平铺在同一层"])
        self.cb_export_layout.setFixedWidth(280)
        f2.addRow("目录结构", self.cb_export_layout)

        self.ed_tpl_video = QLineEdit()
        self.ed_tpl_batch = QLineEdit()
        self.ed_tpl_name = QLineEdit()
        f2.addRow("视频文件夹", self.ed_tpl_video)
        f2.addRow("批次文件夹", self.ed_tpl_batch)
        f2.addRow("手册文件名", self.ed_tpl_name)

        self.lbl_tpl_preview = QLabel("")
        self.lbl_tpl_preview.setWordWrap(True)
        self.lbl_tpl_preview.setStyleSheet("color:#57606a;font-size:12px;")
        f2.addRow("预览", self.lbl_tpl_preview)

        tip = QLabel("可用占位符：{video} 视频文件名　{date} 日期　{time} 时间　"
                     "{run_id} 任务编号　{title} 手册标题。\n"
                     "导出前还会弹一个对话框，那一次可以临时改名字。")
        tip.setWordWrap(True)
        tip.setStyleSheet("color:#57606a;font-size:12px;")
        f2.addRow("", tip)
        v.addWidget(g2)
        v.addStretch(1)

        btn_pick.clicked.connect(self._pick_export_root)
        for w in (self.ed_tpl_video, self.ed_tpl_batch, self.ed_tpl_name):
            w.textChanged.connect(self._refresh_tpl_preview)
        self.cb_export_layout.currentIndexChanged.connect(self._refresh_tpl_preview)
        return page

    def _pick_export_root(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "选择默认导出位置",
                                             self.ed_export_root.text() or str(Path.home()))
        if d:
            self.ed_export_root.setText(d)

    def _refresh_tpl_preview(self) -> None:
        """把当前输入框里的模板渲染成一条示例路径。

        注意这里要**先渲染模板**再清洗：直接把 "{video}" 当成用户手填的名字传进去，
        plan() 会跳过渲染，预览里就会原样显示 {video}。
        """
        from ..naming import build_fields, render_template, safe_segment
        fields = build_fields("示例视频", "20261101-120000_示例视频", "示例视频")
        parts = []
        for tpl in (self.ed_tpl_video.text(), self.ed_tpl_batch.text()):
            text = render_template(tpl, fields) if tpl.strip() else ""
            if text:
                parts.append(safe_segment(text, fallback="批次"))
        name = render_template(self.ed_tpl_name.text() or "{title}", fields)
        if self.cb_export_layout.currentIndex() == 1 and len(parts) > 1:
            parts = ["_".join(parts)]
        parts.append(safe_segment(name, fallback="操作手册") + ".html")
        self.lbl_tpl_preview.setText("<根目录>/" + "/".join(parts))

    # ---------------------------------------------------------------- 页 1：模型配置
    def _page_models(self) -> QWidget:
        """模型配置页按使用频率排序：状态 → 任务角色 → 连接（收起）→ 模型列表。

        用户打开设置最想知道的是"现在用的是什么模型、到底能不能用"，
        其次是"给看图和写步骤分别换模型"；改接口地址和 API Key 是低频操作，
        收进折叠区，需要时点「连接设置」展开。
        """
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(10, 10, 10, 10)
        v.setSpacing(10)
        v.addWidget(self._status_card())
        v.addWidget(self._group_roles())
        self._grp_conn = self._group_conn()
        self._grp_conn.setVisible(False)
        v.addWidget(self._grp_conn)
        v.addWidget(self._group_models(), 1)
        return page

    def _status_card(self) -> QFrame:
        """一眼看到"当前用哪个模型、连接通不通"。"""
        card = QFrame()
        card.setObjectName("statusCard")
        card.setStyleSheet(
            "#statusCard{background:#ffffff;border:1px solid #d0d7de;border-radius:8px;}")
        v = QVBoxLayout(card)
        v.setContentsMargins(12, 10, 12, 10)
        v.setSpacing(6)

        head = QHBoxLayout()
        title = QLabel("当前配置")
        title.setStyleSheet("font-weight:600;font-size:13px;color:#1f2328;")
        head.addWidget(title)
        head.addStretch(1)
        self.btn_recheck = QPushButton("重新检查")
        self.btn_recheck.setToolTip("只验证地址能不能访问、Key 对不对（不发推理请求、不花钱）")
        self.btn_recheck.clicked.connect(self._test)
        self.btn_conn = QPushButton("连接设置")
        self.btn_conn.setCheckable(True)
        self.btn_conn.setToolTip("展开或收起服务商、接口地址、API Key")
        self.btn_conn.toggled.connect(self._toggle_conn)
        head.addWidget(self.btn_recheck)
        head.addWidget(self.btn_conn)
        v.addLayout(head)

        self.lbl_st_vision = QLabel("")
        self.lbl_st_text = QLabel("")
        for lbl in (self.lbl_st_vision, self.lbl_st_text):
            lbl.setWordWrap(True)
            lbl.setStyleSheet("font-size:12px;color:#1f2328;")
            v.addWidget(lbl)

        self.lbl_st_conn = QLabel("尚未检查连接。点「重新检查」确认服务是否可用。")
        self.lbl_st_conn.setWordWrap(True)
        self.lbl_st_conn.setStyleSheet("font-size:12px;color:#57606a;")
        v.addWidget(self.lbl_st_conn)
        return card

    def _toggle_conn(self, on: bool) -> None:
        self._grp_conn.setVisible(on)
        self.btn_conn.setText("收起设置" if on else "连接设置")

    def _update_status_card(self) -> None:
        def line(kind: str, label: str) -> str:
            role = self.settings.role(kind)
            provider = get(role["provider"])
            model = _short(role["model"] or "（未选择模型）")
            return f"{label}：<b>{model}</b>　·　{provider.name}"

        if hasattr(self, "lbl_st_vision"):
            self.lbl_st_vision.setText(line(ROLE_VISION, "视觉分析"))
            self.lbl_st_text.setText(line(ROLE_TEXT, "步骤整理"))

    def _group_conn(self) -> QGroupBox:
        """连接与密钥：服务商、接口地址、API Key、两层测试。默认收起。"""
        box = QGroupBox("连接与密钥")
        box.setStyleSheet(
            "QGroupBox{font-weight:600;color:#1f2328;margin-top:22px;"
            "border:1px solid #d0d7de;border-radius:8px;}"
            "QGroupBox::title{subcontrol-origin:margin;subcontrol-position:top left;"
            "left:10px;padding:0 4px;}")
        v = QVBoxLayout(box)
        v.setContentsMargins(12, 14, 12, 10)
        v.setSpacing(8)

        # 一级分类：只有两个
        row_src = QHBoxLayout()
        row_src.addWidget(QLabel("模型来源"))
        self.rb_local = QRadioButton("本地模型")
        self.rb_cloud = QRadioButton("其他模型（API）")
        self._grp_source = QButtonGroup(self)
        self._grp_source.addButton(self.rb_local)
        self._grp_source.addButton(self.rb_cloud)
        self.rb_local.toggled.connect(self._on_source_toggled)
        row_src.addWidget(self.rb_local)
        row_src.addWidget(self.rb_cloud)
        row_src.addSpacing(16)
        row_src.addWidget(QLabel("服务商"))
        self.cb_provider = QComboBox()
        self.cb_provider.setMinimumWidth(260)
        self.cb_provider.currentIndexChanged.connect(self._on_provider_changed)
        row_src.addWidget(self.cb_provider, 1)
        v.addLayout(row_src)

        self.lbl_note = QLabel("")
        self.lbl_note.setWordWrap(True)
        self.lbl_note.setStyleSheet("color:#57606a;font-size:12px;")
        v.addWidget(self.lbl_note)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight)
        form.setSpacing(8)
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)

        self.ed_base = QLineEdit()
        self.ed_base.setPlaceholderText("https://api.example.com/v1")
        btn_reset = QPushButton("恢复默认")
        btn_reset.clicked.connect(self._reset_base)
        row_base = QHBoxLayout()
        row_base.addWidget(self.ed_base, 1)
        row_base.addWidget(btn_reset)
        form.addRow("接口地址", row_base)

        self.ed_key = QLineEdit()
        self.ed_key.setEchoMode(QLineEdit.Password)
        self.ed_key.setPlaceholderText("粘贴 API Key（本机服务留空）")
        self.cb_show = QCheckBox("显示")
        self.cb_show.toggled.connect(
            lambda on: self.ed_key.setEchoMode(QLineEdit.Normal if on else QLineEdit.Password))
        self.btn_getkey = QPushButton("获取 Key")
        self.btn_getkey.clicked.connect(self._open_key_url)
        row_key = QHBoxLayout()
        row_key.addWidget(self.ed_key, 1)
        row_key.addWidget(self.cb_show)
        row_key.addWidget(self.btn_getkey)
        form.addRow("API Key", row_key)
        v.addLayout(form)

        row_test = QHBoxLayout()
        self.btn_test = QPushButton("测试连接")
        self.btn_test.setToolTip("只验证地址能不能访问、API Key 对不对（不发推理请求、不花钱）")
        self.btn_test.clicked.connect(self._test)
        self.btn_test_vision = QPushButton("测试视觉调用")
        self.btn_test_vision.setToolTip(
            "发一张本地生成的纯色测试图，验证模型真的能看图（会消耗少量 token）")
        self.btn_test_vision.clicked.connect(self._test_vision)
        self.btn_doc = QPushButton("官方文档")
        self.btn_doc.clicked.connect(self._open_doc)
        row_test.addWidget(self.btn_test)
        row_test.addWidget(self.btn_test_vision)
        row_test.addWidget(self.btn_doc)
        row_test.addStretch(1)
        v.addLayout(row_test)

        self.lbl_test = QLabel("")
        self.lbl_test.setWordWrap(True)
        self.lbl_test.setMinimumHeight(32)
        self.lbl_test.setStyleSheet("font-size:12px;color:#57606a;")
        v.addWidget(self.lbl_test)
        return box

    def _group_models(self) -> QGroupBox:
        box = QGroupBox("模型列表")
        # margin-top 要大于标题高度，否则第一行内容会压在「模型列表」标题上
        box.setStyleSheet(
            "QGroupBox{font-weight:600;color:#1f2328;margin-top:22px;"
            "border:1px solid #d0d7de;border-radius:6px;}"
            "QGroupBox::title{subcontrol-origin:margin;subcontrol-position:top left;"
            "left:8px;padding:0 4px;}")
        v = QVBoxLayout(box)
        v.setContentsMargins(10, 12, 10, 10)
        v.setSpacing(6)

        self.lbl_models_state = QLabel("还没有获取过模型列表。点「获取模型」向服务商查询一次。")
        self.lbl_models_state.setWordWrap(True)
        self.lbl_models_state.setStyleSheet("color:#57606a;font-size:12px;")
        v.addWidget(self.lbl_models_state)

        row = QHBoxLayout()
        self.ed_model_search = QLineEdit()
        self.ed_model_search.setPlaceholderText("搜索模型名称…")
        self.ed_model_search.setClearButtonEnabled(True)
        self.ed_model_search.textChanged.connect(self._reload_model_tree)
        row.addWidget(self.ed_model_search, 1)
        self.btn_fetch = QPushButton("获取模型")
        self.btn_fetch.setToolTip("向当前服务商查询实际可用的模型（只查列表，不发推理请求，不产生费用）")
        self.btn_fetch.clicked.connect(self._fetch_models)
        row.addWidget(self.btn_fetch)
        self.btn_manual = QPushButton("手动添加…")
        self.btn_manual.setToolTip("服务商列不出模型时，自己填模型 ID")
        self.btn_manual.clicked.connect(self._add_manual_model)
        row.addWidget(self.btn_manual)
        v.addLayout(row)

        self.tree_models = QTreeWidget()
        self.tree_models.setHeaderLabels(["模型", "来源", "用途"])
        self.tree_models.setRootIsDecorated(False)
        self.tree_models.setAlternatingRowColors(True)
        self.tree_models.setMinimumHeight(120)
        # 列表再高也只是"选模型"用的，不该把页面下半屏全占掉
        self.tree_models.setMaximumHeight(210)
        self.tree_models.setSelectionMode(QAbstractItemView.SingleSelection)
        self.tree_models.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.tree_models.header().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.tree_models.header().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.tree_models.itemDoubleClicked.connect(lambda *_: self._use_selected(ROLE_VISION))
        v.addWidget(self.tree_models, 1)

        row2 = QHBoxLayout()
        self.cb_show_hidden = QCheckBox("显示已隐藏")
        self.cb_show_hidden.toggled.connect(self._reload_model_tree)
        row2.addWidget(self.cb_show_hidden)
        row2.addStretch(1)
        for text, slot, tip in (
                ("设为视觉模型", lambda: self._use_selected(ROLE_VISION), "用选中的模型理解截图"),
                ("设为步骤模型", lambda: self._use_selected(ROLE_TEXT), "用选中的模型整理操作步骤"),
                ("隐藏 / 恢复", self._toggle_hidden, "只影响本软件的列表显示，不删除任何模型文件"),
                ("移除手动条目", self._remove_manual, "只删掉你自己手填的那一条记录"),
        ):
            btn = QPushButton(text)
            btn.setToolTip(tip)
            btn.clicked.connect(slot)
            row2.addWidget(btn)
        v.addLayout(row2)

        self.lbl_models_hint = QLabel("")
        self.lbl_models_hint.setWordWrap(True)
        self.lbl_models_hint.setStyleSheet("color:#57606a;font-size:12px;")
        v.addWidget(self.lbl_models_hint)
        return box

    def _group_roles(self) -> QGroupBox:
        box = QGroupBox("任务角色")
        box.setStyleSheet("QGroupBox{font-weight:600;color:#1f2328;margin-top:8px;}")
        form = QFormLayout(box)
        form.setLabelAlignment(Qt.AlignRight)
        form.setSpacing(6)

        self.cb_role_vision_prov = QComboBox()
        self.cb_role_vision = QComboBox()
        self.cb_role_vision.setEditable(True)
        self.cb_role_vision_prov.currentIndexChanged.connect(
            lambda *_: self._on_role_provider_changed(ROLE_VISION))
        self.cb_role_text_prov = QComboBox()
        self.cb_role_text = QComboBox()
        self.cb_role_text.setEditable(True)
        self.cb_role_text_prov.currentIndexChanged.connect(
            lambda *_: self._on_role_provider_changed(ROLE_TEXT))

        row_v = QHBoxLayout()
        row_v.addWidget(QLabel("来源"))
        row_v.addWidget(self.cb_role_vision_prov, 1)
        row_v.addWidget(QLabel("模型"))
        row_v.addWidget(self.cb_role_vision, 2)
        form.addRow(ROLE_LABELS[ROLE_VISION], row_v)

        row_t = QHBoxLayout()
        row_t.addWidget(QLabel("来源"))
        row_t.addWidget(self.cb_role_text_prov, 1)
        row_t.addWidget(QLabel("模型"))
        row_t.addWidget(self.cb_role_text, 2)
        form.addRow(ROLE_LABELS[ROLE_TEXT], row_t)

        self.lbl_role_hint = QLabel("两个角色可以来自不同服务商，例如本地看图 + 云端写步骤。"
                                    "改完记得点「保存」。")
        self.lbl_role_hint.setWordWrap(True)
        self.lbl_role_hint.setStyleSheet("color:#57606a;font-size:12px;")
        form.addRow("", self.lbl_role_hint)
        return box

    # ---------------------------------------------------------------- 页 2：视频处理
    def _page_video(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(10, 14, 10, 10)
        v.setSpacing(10)

        box = QGroupBox("视频处理")
        box.setStyleSheet(
            "QGroupBox{font-weight:600;color:#1f2328;margin-top:22px;"
            "border:1px solid #d0d7de;border-radius:8px;}"
            "QGroupBox::title{subcontrol-origin:margin;subcontrol-position:top left;"
            "left:10px;padding:0 4px;}")
        form = QFormLayout(box)
        form.setContentsMargins(12, 14, 12, 10)
        form.setLabelAlignment(Qt.AlignRight)
        form.setSpacing(8)

        # 视频类型直接决定用哪套视觉/步骤提示词：软件录屏看「界面元素变化」，
        # 现实操作看「动作与物体状态」。两套提示词不共用，缓存也按各自提示词分开算。
        self.cb_video_mode = QComboBox()
        self.cb_video_mode.addItems(["软件操作录屏（默认）", "现实操作过程（Beta）"])
        self.cb_video_mode.setMaximumWidth(220)
        self.cb_video_mode.currentIndexChanged.connect(self._sync_mode_tip)
        row_mode = QHBoxLayout()
        row_mode.addWidget(self.cb_video_mode)
        row_mode.addStretch(1)
        form.addRow("视频类型", row_mode)

        self.sp_edge = QSpinBox()
        self.sp_edge.setRange(512, 1600)
        self.sp_edge.setSingleStep(64)
        self.sp_edge.setSuffix(" px")
        self.sp_edge.setMaximumWidth(180)
        form.addRow("截图长边像素", self.sp_edge)

        self.cb_frames_mode = QComboBox()
        self.cb_frames_mode.addItems(["按时长自动", "固定上限"])
        self.cb_frames_mode.currentIndexChanged.connect(self._sync_frames_enabled)
        self.sp_frames = QSpinBox()
        self.sp_frames.setRange(10, 600)
        self.sp_frames.setMaximumWidth(120)
        self.cb_frames_mode.setMaximumWidth(180)
        row_fr = QHBoxLayout()
        row_fr.addWidget(self.cb_frames_mode)
        row_fr.addWidget(self.sp_frames)
        row_fr.addStretch(1)
        form.addRow("截图数量", row_fr)

        self.cb_think = QCheckBox("关闭模型思考过程（仅本机 / 自定义服务有效）")
        form.addRow("视觉模型处理方式", self.cb_think)
        v.addWidget(box)

        self.lbl_video_tip = QLabel()
        self.lbl_video_tip.setWordWrap(True)
        self.lbl_video_tip.setStyleSheet("color:#57606a;font-size:12px;")
        v.addWidget(self.lbl_video_tip)
        v.addStretch(1)
        return page

    # ------------------------------------------------------------ 语音识别
    def _build_asr_box(self) -> QGroupBox:
        """语音识别档位：选模型、下载、换设备、填术语。

        语音模型对中文准确率的影响比设备大得多（small 换 CUDA 结果一字不差，
        换 large-v3 才把字错率压到接近 0），所以这一块要能自己选、自己下。
        """
        box = QGroupBox("语音识别")
        box.setStyleSheet(
            "QGroupBox{font-weight:600;color:#1f2328;margin-top:22px;"
            "border:1px solid #d0d7de;border-radius:8px;}"
            "QGroupBox::title{subcontrol-origin:margin;subcontrol-position:top left;"
            "left:10px;padding:0 4px;}")
        form = QFormLayout(box)
        form.setContentsMargins(12, 14, 12, 10)
        form.setLabelAlignment(Qt.AlignRight)
        form.setSpacing(8)

        self.cb_whisper = QComboBox()
        for m in asr_models.catalog():
            self.cb_whisper.addItem(m.id, m.id)
        self.cb_whisper.setMinimumWidth(200)
        self.cb_whisper.currentIndexChanged.connect(lambda _=0: self._refresh_asr_status())

        self.btn_dl_model = QPushButton("下载此模型")
        self.btn_dl_model.setToolTip("下载到本机缓存，之后可离线使用")
        self.btn_dl_model.clicked.connect(self._download_whisper)
        self.btn_pick_model = QPushButton("选择本地目录…")
        self.btn_pick_model.setToolTip("如果你已经手动下载过模型，可以直接指定它的目录")
        self.btn_pick_model.clicked.connect(self._pick_whisper_dir)

        row_m = QHBoxLayout()
        row_m.setSpacing(6)
        row_m.addWidget(self.cb_whisper)
        row_m.addWidget(self.btn_dl_model)
        row_m.addWidget(self.btn_pick_model)
        row_m.addStretch(1)
        form.addRow("模型档位", row_m)

        self.lbl_asr_status = QLabel("")
        self.lbl_asr_status.setWordWrap(True)
        self.lbl_asr_status.setStyleSheet("color:#57606a;font-size:12px;")
        form.addRow("", self.lbl_asr_status)

        self.pb_asr = QProgressBar()
        self.pb_asr.setRange(0, 100)
        self.pb_asr.setMaximumHeight(6)
        self.pb_asr.setVisible(False)
        form.addRow("", self.pb_asr)

        self.cb_device = QComboBox()
        self.cb_device.addItem("自动（有显卡就用显卡）", "auto")
        self.cb_device.addItem("显卡（CUDA）", "cuda")
        self.cb_device.addItem("处理器（CPU）", "cpu")
        self.cb_device.setMaximumWidth(220)
        form.addRow("推理设备", self.cb_device)

        self.ed_hotwords = QLineEdit()
        self.ed_hotwords.setPlaceholderText("例如：工具栏, 主界面, 自动保存（逗号分隔）")
        form.addRow("术语提示", self.ed_hotwords)

        self.ed_prompt = QLineEdit()
        self.ed_prompt.setPlaceholderText("例如：打开示例笔记软件的主界面，点击工具栏中的设置按钮。")
        form.addRow("开头提示", self.ed_prompt)

        hint = QLabel("模型越大越准也越慢：120 秒音频在显卡上 small 约 3 秒、large-v3 约 8 秒。"
                      "术语提示对每个识别窗口都生效，填软件名和菜单名能明显减少同音错字；"
                      "开头提示只影响开头，用来定住标点风格。")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#57606a;font-size:12px;")
        form.addRow("", hint)
        return box

    def _refresh_asr_status(self) -> None:
        """更新当前所选模型的本机状态和按钮可用性。"""
        mid = self.cb_whisper.currentData() or self.cb_whisper.currentText()
        model = asr_models.get(mid)
        if model is None:
            self.lbl_asr_status.setText("这个档位不在内置清单里，将按名字交给 faster-whisper 解析。")
            self.btn_dl_model.setEnabled(False)
            return
        st = asr_models.status(mid, self.settings)
        if st["installed"]:
            from pathlib import Path as _P

            p = _P(st["path"])
            # HF 缓存布局是 snapshots/<40位commit>，直接显示 hash 对用户没意义，
            # 统一显示成模型名；完整路径放悬停提示。
            short = p.name if p.name.startswith("faster-whisper") else f"faster-whisper-{model.id}"
            self.lbl_asr_status.setText(f"已就绪：{st['size_mb']} MB　{short}\n{model.note}")
            self.lbl_asr_status.setToolTip(st["path"])
            self.btn_dl_model.setEnabled(False)
            self.btn_dl_model.setText("已下载")
        else:
            self.lbl_asr_status.setText(
                f"本机还没有这个模型（约 {model.size_mb} MB）。点「下载此模型」或指定已有目录。\n{model.note}")
            self.btn_dl_model.setEnabled(True)
            self.btn_dl_model.setText("下载此模型")

    def _pick_whisper_dir(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "选择 faster-whisper 模型目录")
        if not d:
            return
        from pathlib import Path as _P

        if not (_P(d) / "model.bin").exists():
            QMessageBox.warning(self, "目录不对",
                                "这个目录里没有 model.bin。\n"
                                "请选择 faster-whisper 格式的模型目录"
                                "（里面有 model.bin、config.json、tokenizer.json、vocabulary.txt）。")
            return
        self.settings.whisper_model_dir = d
        self._refresh_asr_status()
        QMessageBox.information(self, "已指定", f"将使用这个目录的模型：\n{d}")

    def _download_whisper(self) -> None:
        """在后台线程下载模型，不卡界面。"""
        mid = self.cb_whisper.currentData() or self.cb_whisper.currentText()
        model = asr_models.get(mid)
        if model is None:
            return
        if not QMessageBox.question(
                self, "下载语音模型",
                f"将从 HuggingFace 下载 {model.id}（约 {model.size_mb} MB）。\n"
                "网络不通时会失败，可在环境变量 VTG_HF_ENDPOINT 指定镜像地址。\n\n继续吗？",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) == QMessageBox.Yes:
            return
        self.btn_dl_model.setEnabled(False)
        self.pb_asr.setVisible(True)
        self.pb_asr.setRange(0, 0)          # 不确定进度，用滚动条表示在跑
        self.lbl_asr_status.setText(f"正在下载 {model.id}…（约 {model.size_mb} MB，请保持网络）")

        class _Dl(QThread):
            ok = Signal(str)
            bad = Signal(str)

            def __init__(self, mid, settings):
                super().__init__()
                self._mid = mid
                self._settings = settings

            def run(self):
                try:
                    path = asr_models.download(self._mid, self._settings)
                    self.ok.emit(str(path))
                except Exception as e:                        # noqa: BLE001
                    self.bad.emit(f"{type(e).__name__}: {e}")

        self._dl = _Dl(mid, self.settings)

        def done(path):
            self.pb_asr.setVisible(False)
            self.pb_asr.setRange(0, 100)
            self._refresh_asr_status()
            QMessageBox.information(self, "下载完成", f"模型已就绪：\n{path}")

        def bad(msg):
            self.pb_asr.setVisible(False)
            self.pb_asr.setRange(0, 100)
            self.btn_dl_model.setEnabled(True)
            self.lbl_asr_status.setText("下载失败：" + msg)
            QMessageBox.warning(self, "下载失败",
                                msg + "\n\n可以换网络重试，或手动下载后点「选择本地目录…」。")

        self._dl.ok.connect(done)
        self._dl.bad.connect(bad)
        self._dl.start()

    # ---------------------------------------------------------------- 初始化
    def _sync_from_settings(self) -> None:
        pid = self.settings.role(ROLE_VISION)["provider"]
        local = get(pid).local
        self.rb_local.setChecked(local)
        self.rb_cloud.setChecked(not local)
        self._fill_provider_combo(local, prefer=pid)
        self._fill_role_providers()

        self.sp_edge.setValue(int(self.settings.read_long_edge))
        # 导出页
        self.cb_export_mode.setCurrentIndex(
            1 if getattr(self.settings, "export_mode_default", "light") == "full" else 0)
        self.ed_export_root.setText(getattr(self.settings, "export_root", ""))
        self.cb_export_layout.setCurrentIndex(
            0 if getattr(self.settings, "export_layout", "by_video_batch") == "by_video_batch" else 1)
        self.ed_tpl_video.setText(getattr(self.settings, "export_video_template", "{video}"))
        self.ed_tpl_batch.setText(getattr(self.settings, "export_batch_template", "{date}-{time}"))
        self.ed_tpl_name.setText(getattr(self.settings, "export_name_template", "{title}"))
        self._refresh_tpl_preview()
        self.cb_video_mode.setCurrentIndex(
            1 if getattr(self.settings, "video_mode", "software") == "reality" else 0)
        self._sync_mode_tip()
        self.cb_frames_mode.setCurrentIndex(0 if self.settings.max_frames_auto else 1)
        self.sp_frames.setValue(int(self.settings.max_frames))
        idx = self.cb_whisper.findData(getattr(self.settings, "whisper_model", "small"))
        self.cb_whisper.setCurrentIndex(idx if idx >= 0 else 0)
        dev = getattr(self.settings, "whisper_device", "auto")
        self.cb_device.setCurrentIndex(max(0, self.cb_device.findData(dev)))
        self.ed_hotwords.setText(getattr(self.settings, "whisper_hotwords", "") or "")
        self.ed_prompt.setText(getattr(self.settings, "whisper_initial_prompt", "") or "")
        self._refresh_asr_status()
        self.cb_think.setChecked(bool(self.settings.disable_thinking))
        self._sync_frames_enabled()
        self._update_status_card()

    def _sync_mode_tip(self) -> None:
        real = self.cb_video_mode.currentIndex() == 1
        base = ("截图长边越大，小字越清楚，但视觉模型更慢、更费 token；"
                "1080p 教学录屏用 760 左右比较平衡。"
                "「按时长自动」会按视频长度决定截图预算，长教程也不会只抽到几帧。")
        if real:
            base = ("现实操作模式（Beta）：适合做饭、手工、简单装配等「有明确动作和结果变化」"
                    "的实拍视频，不适合纯展示、Vlog 或缺少过程画面的剪辑视频。"
                    "两种模式用不同的提示词，切换后重新分析才会生效。")
        self.lbl_video_tip.setText(base)

    def _on_source_toggled(self, local: bool) -> None:
        """切换「本地 / 其他」时重建服务商下拉。

        toggled 会连发两次（一个开一个关），只处理被选中的那一个。
        """
        if not local and not self.rb_cloud.isChecked():
            return
        if local and not self.rb_local.isChecked():
            return
        self._fill_provider_combo(local)

    def _fill_provider_combo(self, local: bool, prefer: str = "") -> None:
        ids = LOCAL_IDS if local else CLOUD_IDS
        want = prefer or self._pid
        if want not in ids:
            want = ids[0]
        self.cb_provider.blockSignals(True)
        self.cb_provider.clear()
        for pid in ids:
            self.cb_provider.addItem(get(pid).name, pid)
        idx = max(0, self.cb_provider.findData(want))
        self.cb_provider.setCurrentIndex(idx)
        self.cb_provider.blockSignals(False)
        self._select_provider(want)

    def _on_provider_changed(self, index: int) -> None:
        pid = self.cb_provider.itemData(index)
        if pid:
            self._select_provider(str(pid))

    def _select_provider(self, pid: str, initial: bool = False) -> None:
        p = get(pid)
        self._pid = p.id
        notes = [p.note] if p.note else []
        if not p.vision_models and p.id != "custom":
            notes.append("这家没有内置视觉模型名，识图请确认模型确实支持图像输入（用「测试视觉调用」验证）。")
        self.lbl_note.setText("\n".join(notes))

        conn = self.settings.connection(p.id)
        self.ed_base.setText(conn["base_url"] or p.base_url)
        self.ed_key.setText("" if p.local else conn["api_key"])
        self.ed_key.setEnabled(not p.local)
        self.cb_show.setEnabled(not p.local)
        self.btn_getkey.setEnabled(bool(p.key_url) and not p.local)
        self.btn_doc.setEnabled(bool(p.doc_url))
        self.cb_think.setEnabled(p.local or p.id == "custom")
        self.lbl_test.setText("")

        # 切服务商：作废进行中的"获取模型"请求（旧结果不能覆盖新列表）
        self._fetch_gen += 1
        self.ed_model_search.clear()
        self.btn_fetch.setEnabled(protocol_for(p.id) != "none")
        self.lbl_models_hint.setText("")      # 先清空，再让 _reload_model_tree 填新的提示
        self._reload_model_tree()

    # ---------------------------------------------------------------- 任务角色
    def _fill_role_providers(self) -> None:
        for kind, combo in ((ROLE_VISION, self.cb_role_vision_prov),
                            (ROLE_TEXT, self.cb_role_text_prov)):
            role = self.settings.role(kind)
            combo.blockSignals(True)
            combo.clear()
            for p in PROVIDERS:
                combo.addItem(("本地 · " if p.local else "云端 · ") + p.name, p.id)
            idx = combo.findData(role["provider"])
            combo.setCurrentIndex(max(0, idx))
            combo.blockSignals(False)
        self._fill_role_models(ROLE_VISION)
        self._fill_role_models(ROLE_TEXT)

    def _role_provider(self, kind: str) -> str:
        combo = self.cb_role_vision_prov if kind == ROLE_VISION else self.cb_role_text_prov
        return str(combo.currentData() or self._pid)

    def _role_model_combo(self, kind: str) -> QComboBox:
        return self.cb_role_vision if kind == ROLE_VISION else self.cb_role_text

    def _fill_role_models(self, kind: str, reset: bool = False,
                          preferred: str = "") -> None:
        """填该角色的模型下拉：已获取的 + 手动添加的 + 该服务商预设，去掉隐藏的。"""
        combo = self._role_model_combo(kind)
        pid = self._role_provider(kind)
        p = get(pid)
        hidden = set(self._hidden_for(pid))
        ids: list[str] = []
        for entry in self._all_models(pid):
            if entry.id and entry.id not in hidden and entry.id not in ids:
                ids.append(entry.id)
        for mid in (p.vision_models if kind == ROLE_VISION else p.text_models):
            if mid and mid not in hidden and mid not in ids:
                ids.append(mid)
        cur = "" if reset else (preferred or combo.currentText().strip())
        if not cur:
            cur = self.settings.role(kind)["model"]
            if self.settings.role(kind)["provider"] != pid:
                cur = ""
        # "local" 是旧配置的占位值，不是真实模型 ID：有真实模型时替换掉它，
        # 免得用户在下拉框里看到一个点不动的假名字。
        if cur == "local" and "local" not in ids and ids:
            cur = ids[0]
        if cur and cur not in ids:
            ids.insert(0, cur)
        combo.blockSignals(True)
        combo.clear()
        combo.addItems(ids)
        combo.setCurrentText(cur or (ids[0] if ids else ""))
        combo.blockSignals(False)

    def _on_role_provider_changed(self, kind: str) -> None:
        """换来源后模型列表跟着换，并默认选该来源的第一个模型。"""
        self._fill_role_models(kind, reset=True)
        self._sync_role_setting(kind)

    def _sync_role_setting(self, kind: str) -> None:
        """把界面上的角色选择写进内存配置（点取消会恢复），并刷新状态卡。

        不刷新的话，用户换了模型、状态卡还显示旧的，会以为没生效。
        """
        model = self._role_model_combo(kind).currentText().strip()
        if model:
            self.settings.set_role(kind, self._role_provider(kind), model)
        self._update_status_card()

    # ---------------------------------------------------------------- 模型数据
    def _cache_for(self, pid: str) -> dict:
        cache = self.settings.model_cache.get(pid)
        return dict(cache) if isinstance(cache, dict) else {}

    def _hidden_for(self, pid: str) -> list[str]:
        raw = self.settings.hidden_models.get(pid)
        return [str(x) for x in raw] if isinstance(raw, list) else []

    def _manual_for(self, pid: str) -> list[str]:
        raw = self.settings.manual_models.get(pid)
        return [str(x) for x in raw] if isinstance(raw, list) else []

    def _all_models(self, pid: str) -> list[ModelEntry]:
        """缓存 + 手动 + 当前角色正在用的，按 id 去重。"""
        out: dict[str, ModelEntry] = {}
        for raw in (self._cache_for(pid).get("models") or []):
            entry = ModelEntry.from_dict(raw if isinstance(raw, dict) else {})
            if entry.id:
                out[entry.id] = entry
        for mid in self._manual_for(pid):
            if mid and mid not in out:
                out[mid] = ModelEntry(id=mid, source="manual")
        for kind in (ROLE_VISION, ROLE_TEXT):
            role = self.settings.role(kind)
            mid = role["model"]
            if role["provider"] == pid and mid and mid not in ("local", "") and mid not in out:
                note = "当前视觉模型" if kind == ROLE_VISION else "当前步骤模型"
                out[mid] = ModelEntry(id=mid, source="manual", note=note)
        return list(out.values())

    def _selected_model_id(self) -> str:
        item = self.tree_models.currentItem()
        return str(item.data(0, Qt.UserRole)) if item is not None else ""

    def _reload_model_tree(self, *_args) -> None:
        pid = self._pid
        keyword = self.ed_model_search.text().strip().lower()
        hidden = set(self._hidden_for(pid))
        show_hidden = self.cb_show_hidden.isChecked()
        models = self._all_models(pid)
        prev = self._selected_model_id()
        vision_role = self.settings.role(ROLE_VISION)
        text_role = self.settings.role(ROLE_TEXT)

        self.tree_models.clear()
        shown = 0
        for entry in models:
            is_hidden = entry.id in hidden
            if is_hidden and not show_hidden:
                continue
            if keyword and keyword not in entry.id.lower():
                continue
            source = "手动" if entry.source == "manual" else "服务商"
            marks = []
            if entry.id == vision_role["model"] and pid == vision_role["provider"]:
                marks.append("视觉")
            if entry.id == text_role["model"] and pid == text_role["provider"]:
                marks.append("步骤")
            if entry.vision == "yes":
                marks.append("支持视觉（服务商声明）" if "元数据" in entry.note
                             else "支持视觉（预设）")
            use = "、".join(marks) if marks else "未验证"
            if is_hidden:
                use = "已隐藏"
            item = QTreeWidgetItem([entry.id, source, use])
            item.setData(0, Qt.UserRole, entry.id)
            if is_hidden:
                item.setForeground(0, Qt.gray)
            self.tree_models.addTopLevelItem(item)
            if prev and entry.id == prev:
                self.tree_models.setCurrentItem(item)
            shown += 1

        cache = self._cache_for(pid)
        if cache.get("fetched_at"):
            state = (f"上次获取：{cache['fetched_at']}"
                     f"（服务商返回 {len(cache.get('models') or [])} 个）"
                     f" · 可选 {len(models)} 个 · 已隐藏 {len(hidden)} 个")
        else:
            state = f"还没获取过服务商列表 · 可选 {len(models)} 个（含手动添加）"
        self.lbl_models_state.setText(state)
        self.lbl_models_state.setStyleSheet("color:#57606a;font-size:12px;")

        provider = get(pid)
        if protocol_for(pid) == "none":
            self.lbl_models_hint.setText(
                f"{provider.name} 没有适配模型列表接口，请点「手动添加…」填写模型 ID。")
        elif shown == 0:
            self.lbl_models_hint.setText("当前没有可显示的模型：点「获取模型」，或「手动添加…」。")
        else:
            self.lbl_models_hint.setText(
                "「未验证」表示没有可靠依据判断它能不能看图：用「测试视觉调用」实际验证。")

    # ---------------------------------------------------------------- 获取模型
    def _fetch_models(self) -> None:
        pid = self._pid
        provider = get(pid)
        if protocol_for(pid) == "none":
            QMessageBox.information(
                self, "不支持自动获取",
                f"{provider.name} 暂时没有适配模型列表接口。\n"
                "请点「手动添加…」填写模型 ID。")
            return
        base_url = self.ed_base.text().strip()
        if not base_url:
            QMessageBox.warning(self, "缺少接口地址",
                                "请先填写接口地址。")
            return
        api_key = "" if provider.local else self.ed_key.text().strip()
        # 自定义地址可能指向本机或内网服务，允许不填 Key；其余云端服务商必须填
        if not provider.local and not api_key and provider.id not in ("custom", "localcustom"):
            QMessageBox.information(self, "还没有 API Key",
                                    f"{provider.name} 需要 API Key 才能列出模型。")
            return
        self._fetch_gen += 1
        gen = self._fetch_gen
        self.btn_fetch.setEnabled(False)
        self.btn_fetch.setText("获取中…")
        self.lbl_models_state.setStyleSheet("color:#0969da;font-size:12px;")
        self.lbl_models_state.setText(f"正在向 {provider.name} 获取模型列表…")
        worker = ModelFetchWorker(pid, base_url, api_key, gen, parent=self)
        worker.done.connect(self._on_models_fetched)
        worker.finished.connect(worker.deleteLater)
        self._fetch_worker = worker
        worker.start()

    def _on_models_fetched(self, data: dict) -> None:
        self.btn_fetch.setEnabled(True)
        self.btn_fetch.setText("获取模型")
        if int(data.get("gen") or 0) != self._fetch_gen:
            return                      # 用户已经切到别的服务商，旧结果直接丢掉
        pid = str(data.get("provider") or self._pid)
        if not data.get("ok"):
            error = str(data.get("error") or "未知错误")
            cache = self._cache_for(pid)
            self.lbl_models_state.setStyleSheet("color:#cf222e;font-size:12px;")
            if cache.get("fetched_at"):
                self.lbl_models_state.setText(
                    f"获取失败：{error}\n"
                    f"下面显示的是 {cache['fetched_at']} 的缓存结果，不是最新数据。")
            else:
                self.lbl_models_state.setText(f"获取失败：{error}")
            return
        models = [ModelEntry.from_dict(x if isinstance(x, dict) else {})
                  for x in (data.get("models") or [])]
        self.settings.model_cache[pid] = {
            "fetched_at": str(data.get("fetched_at") or ""),
            "base_url": self.ed_base.text().strip(),
            "models": [m.to_dict() for m in models],
        }
        self._reload_model_tree()
        self._fill_role_models(ROLE_VISION)
        self._fill_role_models(ROLE_TEXT)
        note = str(data.get("note") or "")
        self.lbl_models_state.setStyleSheet("color:#1a7f37;font-size:12px;")
        self.lbl_models_state.setText(
            f"✓ 获取成功：{len(models)} 个模型（{data.get('elapsed', 0)} 秒）"
            + (f"　{note}" if note else ""))

    # ---------------------------------------------------------------- 隐藏 / 手动 / 用途
    def _toggle_hidden(self) -> None:
        mid = self._selected_model_id()
        if not mid:
            QMessageBox.information(self, "先选一个模型", "请在列表里选中一个模型。")
            return
        pid = self._pid
        hidden = self._hidden_for(pid)
        if mid in hidden:
            hidden.remove(mid)
            msg = f"已恢复显示：{mid}"
        else:
            in_use = []
            for kind in (ROLE_VISION, ROLE_TEXT):
                role = self.settings.role(kind)
                if role["provider"] == pid and role["model"] == mid:
                    in_use.append(ROLE_LABELS[kind].split("（")[0])
            if in_use:
                ret = QMessageBox.question(
                    self, "这个模型正在使用",
                    f"{mid} 正被用作{'、'.join(in_use)}。\n\n"
                    "隐藏只会让它从列表里消失，不会自动换成别的模型，\n"
                    "也不会删除任何模型文件。\n\n仍然隐藏吗？")
                if ret != QMessageBox.Yes:
                    return
            hidden.append(mid)
            msg = f"已隐藏：{mid}（只影响本软件列表，不删除模型文件）"
        self.settings.hidden_models[pid] = hidden
        self._reload_model_tree()
        self._fill_role_models(ROLE_VISION)
        self._fill_role_models(ROLE_TEXT)
        self.lbl_models_hint.setText(msg)

    def _add_manual_model(self) -> None:
        pid = self._pid
        provider = get(pid)
        mid, ok = QInputDialog.getText(
            self, "手动添加模型",
            f"输入 {provider.name} 上的模型 ID（要和接口里写的完全一致）：")
        if not ok:
            return
        mid = (mid or "").strip()
        if not mid:
            QMessageBox.warning(self, "没填内容", "模型 ID 不能为空。")
            return
        if mid in {m.id for m in self._all_models(pid)}:
            QMessageBox.information(self, "已经有了", f"{mid} 已经在列表里，不用重复添加。")
            return
        manual = self._manual_for(pid)
        manual.append(mid)
        self.settings.manual_models[pid] = manual
        self._reload_model_tree()
        self._fill_role_models(ROLE_VISION)
        self._fill_role_models(ROLE_TEXT)
        self.lbl_models_hint.setText(
            f"已添加手动条目：{mid}（手动添加不代表一定能调用，请用「测试视觉调用」验证）")

    def _remove_manual(self) -> None:
        mid = self._selected_model_id()
        if not mid:
            QMessageBox.information(self, "先选一个模型", "请在列表里选中一个模型。")
            return
        pid = self._pid
        manual = self._manual_for(pid)
        if mid not in manual:
            QMessageBox.information(
                self, "不是手动条目",
                f"{mid} 是从服务商获取的，不能移除；如果不想看到它，可以「隐藏 / 恢复」。")
            return
        manual.remove(mid)
        self.settings.manual_models[pid] = manual
        self._reload_model_tree()
        self._fill_role_models(ROLE_VISION)
        self._fill_role_models(ROLE_TEXT)
        self.lbl_models_hint.setText(f"已移除手动条目：{mid}（只删这条记录，不影响服务商）")

    def _use_selected(self, kind: str) -> None:
        mid = self._selected_model_id()
        if not mid:
            QMessageBox.information(self, "先选一个模型", "请在列表里选中一个模型。")
            return
        pid = self._pid
        # 角色来源跟着当前编辑的服务商走，模型用选中的那个
        combo = self.cb_role_vision_prov if kind == ROLE_VISION else self.cb_role_text_prov
        combo.blockSignals(True)
        combo.setCurrentIndex(max(0, combo.findData(pid)))
        combo.blockSignals(False)
        self.settings.set_role(kind, pid, mid)
        self._fill_role_models(kind, preferred=mid)
        self._reload_model_tree()
        self.lbl_models_hint.setText(
            f"{ROLE_LABELS[kind]} 已设为 {mid}（{get(pid).name}）。"
            + ("请点「测试视觉调用」确认它真的能看图。" if kind == ROLE_VISION else ""))

    # ---------------------------------------------------------------- 其他按钮
    def _reset_base(self) -> None:
        self.ed_base.setText(get(self._pid).base_url)

    def _open_key_url(self) -> None:
        url = get(self._pid).key_url
        if url:
            QDesktopServices.openUrl(QUrl(url))

    def _open_doc(self) -> None:
        url = get(self._pid).doc_url
        if url:
            QDesktopServices.openUrl(QUrl(url))

    def _sync_frames_enabled(self) -> None:
        self.sp_frames.setEnabled(self.cb_frames_mode.currentIndex() == 1)

    # ---------------------------------------------------------------- 测试连接
    def _test(self) -> None:
        """第一层：只验证「连得上、鉴权对」，不发推理请求。"""
        base_url = self.ed_base.text().strip()
        if not base_url:
            self.lbl_test.setText("请先填接口地址")
            return
        model = self._role_model_combo(ROLE_VISION).currentText().strip() or "local"
        api_key = "" if get(self._pid).local else self.ed_key.text().strip()
        self.btn_test.setEnabled(False)
        self.btn_recheck.setEnabled(False)
        self.lbl_st_conn.setStyleSheet("font-size:12px;color:#57606a;")
        self.lbl_st_conn.setText("● 正在检查连接…")
        self.lbl_test.setStyleSheet("font-size:12px;color:#57606a;")
        self.lbl_test.setText("正在连接…（最多等 15 秒）")
        worker = HealthWorker(base_url, model, api_key, 15.0, parent=self)
        worker.done.connect(self._on_health)
        worker.finished.connect(worker.deleteLater)
        self._health_worker = worker
        worker.start()

    def _on_health(self, ok: bool, detail: str) -> None:
        self.btn_test.setEnabled(True)
        self.btn_recheck.setEnabled(True)
        if ok:
            self.lbl_st_conn.setStyleSheet("font-size:12px;color:#1a7f37;")
            self.lbl_st_conn.setText("● 连接正常。" + detail)
        else:
            self.lbl_st_conn.setStyleSheet("font-size:12px;color:#cf222e;")
            self.lbl_st_conn.setText("● 连接失败：" + detail)
        if ok:
            self.lbl_test.setStyleSheet("font-size:12px;color:#1a7f37;")
            self.lbl_test.setText("✓ 服务连接成功。" + detail
                                  + "\n（这只说明地址通、Key 对；能不能看图请点「测试视觉调用」。）")
        else:
            self.lbl_test.setStyleSheet("font-size:12px;color:#cf222e;")
            self.lbl_test.setText("✗ " + detail)

    def _test_vision(self) -> None:
        """第二层：真的发一张图，验证这个模型能处理图像输入。"""
        base_url = self.ed_base.text().strip()
        model = self._role_model_combo(ROLE_VISION).currentText().strip()
        if not base_url or not model:
            self.lbl_test.setText("请先填接口地址，并在「任务角色」里选一个视觉模型")
            return
        provider = get(self._pid)
        api_key = "" if provider.local else self.ed_key.text().strip()
        ret = QMessageBox.question(
            self, "测试视觉调用",
            f"会用「{model}」处理一张本地生成的 48×48 纯色测试图\n"
            "（不涉及你的任何视频或截图，不会上传本地文件）。\n\n"
            "云端服务会消耗少量 token；本机服务免费。继续吗？")
        if ret != QMessageBox.Yes:
            return
        self.btn_test_vision.setEnabled(False)
        self.lbl_test.setStyleSheet("font-size:12px;color:#57606a;")
        self.lbl_test.setText(f"正在用 {model} 处理测试图…（推理可能要十几秒）")
        worker = VisionTestWorker(base_url, model, api_key,
                                  disable_thinking=sends_thinking_flag(
                                      self._pid, self.cb_think.isChecked()),
                                  timeout=max(60.0, float(self.settings.vision_timeout)),
                                  parent=self)
        worker.done.connect(self._on_vision_test)
        worker.finished.connect(worker.deleteLater)
        self._vision_worker = worker
        worker.start()

    def _on_vision_test(self, ok: bool, detail: str, elapsed: float) -> None:
        self.btn_test_vision.setEnabled(True)
        if ok:
            self.lbl_test.setStyleSheet("font-size:12px;color:#1a7f37;")
            self.lbl_test.setText(f"✓ 视觉调用成功（{elapsed:.1f} 秒）。{detail}")
        else:
            self.lbl_test.setStyleSheet("font-size:12px;color:#cf222e;")
            self.lbl_test.setText(
                f"✗ 视觉调用失败（{elapsed:.1f} 秒）：{detail}\n"
                "这个模型可能不支持图像输入，换一个视觉模型再试。")

    # ---------------------------------------------------------------- 保存
    def collect(self) -> dict:
        """把表单内容变成一组配置值（单独抽出来便于测试）。"""
        pid = self._pid
        p = get(pid)
        base = self.ed_base.text().strip() or p.base_url
        key = "" if p.local else self.ed_key.text().strip()

        conns = {str(k): dict(v) for k, v in (self.settings.connections or {}).items()
                 if isinstance(v, dict)}
        conns[pid] = {"base_url": base, "api_key": key}

        vpid = self._role_provider(ROLE_VISION)
        tpid = self._role_provider(ROLE_TEXT)
        vmodel = self._role_model_combo(ROLE_VISION).currentText().strip()
        tmodel = self._role_model_combo(ROLE_TEXT).currentText().strip()
        return {
            "connections": conns,
            "vision_role": {"provider": vpid, "model": vmodel},
            "text_role": {"provider": tpid, "model": tmodel},
            "video_mode": ("reality" if self.cb_video_mode.currentIndex() == 1
                           else "software"),
            "read_long_edge": int(self.sp_edge.value()),
            "max_frames_auto": self.cb_frames_mode.currentIndex() == 0,
            "max_frames": int(self.sp_frames.value()),
            "disable_thinking": bool(self.cb_think.isChecked()),
            "whisper_model": (self.cb_whisper.currentData()
                              or self.cb_whisper.currentText().strip() or "small"),
            "whisper_device": self.cb_device.currentData() or "auto",
            "whisper_hotwords": self.ed_hotwords.text().strip(),
            "whisper_initial_prompt": self.ed_prompt.text().strip(),
            "hidden_models": {str(k): list(v) for k, v in
                              (self.settings.hidden_models or {}).items()},
            "manual_models": {str(k): list(v) for k, v in
                              (self.settings.manual_models or {}).items()},
            "model_cache": copy.deepcopy(self.settings.model_cache or {}),
            # 导出默认行为与命名规则
            "export_mode_default": ("full" if self.cb_export_mode.currentIndex() == 1
                                    else "light"),
            "export_root": self.ed_export_root.text().strip(),
            "export_layout": ("by_video_batch" if self.cb_export_layout.currentIndex() == 0
                              else "flat"),
            "export_video_template": self.ed_tpl_video.text().strip() or "{video}",
            "export_batch_template": self.ed_tpl_batch.text().strip() or "{date}-{time}",
            "export_name_template": self.ed_tpl_name.text().strip() or "{title}",
        }

    def _save(self) -> None:
        values = self.collect()
        for pid, conn in values["connections"].items():
            url = str(conn.get("base_url") or "")
            if url and not url.startswith(("http://", "https://")):
                QMessageBox.warning(
                    self, "地址不对",
                    f"{get(pid).name} 的接口地址要以 http:// 或 https:// 开头。")
                return
        for kind in (ROLE_VISION, ROLE_TEXT):
            role = values[kind + "_role"]
            if not role["model"]:
                QMessageBox.warning(
                    self, "模型没选",
                    f"请为「{ROLE_LABELS[kind]}」选一个模型，或先「获取模型」。")
                return
        for kind in (ROLE_VISION, ROLE_TEXT):
            role = values[kind + "_role"]
            provider = get(role["provider"])
            if provider.local:
                continue
            conn = values["connections"].get(role["provider"]) or {}
            if not str(conn.get("api_key") or "").strip():
                ret = QMessageBox.question(
                    self, "还没填 API Key",
                    f"「{ROLE_LABELS[kind]}」用的是云端服务 {provider.name}，"
                    "通常需要 API Key 才能用。\n仍要保存吗？")
                if ret != QMessageBox.Yes:
                    return
        for k, v in values.items():
            setattr(self.settings, k, v)
        self.accept()
