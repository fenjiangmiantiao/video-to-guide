"""桌面主窗口。

结构：
    菜单栏：文件 / 编辑 / 帮助
    工具栏：主操作（选择视频 / 开始分析 / 取消）… 右侧「设置」；
    结果操作（编辑 / 删除 / 导出 / 打开手册 / 重新生成 / 输出目录）放在右栏内容上方；
    历史记录只有左栏标签一个入口。
    中央：顶部阶段面板（只在分析时出现）+ 左右分栏
        左侧：视频信息卡 + 标签页（操作步骤 / 历史记录）
        右侧：欢迎页（无数据时）或 标签页（步骤详情 / 全部字幕 / 运行日志）
    状态栏：状态文字

设计原则：
1. 界面只管显示和转发，真正的处理在 pipeline 里，所以命令行和界面结果完全一致。
2. 长任务全部在 QThread 里跑，界面线程只接收信号，不做阻塞调用。
3. 没数据的时候显示明确的下一步，而不是一块空白。
"""

from __future__ import annotations

import html as _html
import os
import shutil
import time
from pathlib import Path

from PySide6.QtCore import QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QDesktopServices, QKeySequence
from PySide6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
                               QFileDialog, QFormLayout, QFrame, QHBoxLayout,
                               QInputDialog, QLabel,
                               QLineEdit, QListWidget, QListWidgetItem,
                               QMainWindow, QMenu, QMessageBox, QPlainTextEdit,
                               QProgressBar, QPushButton, QSizePolicy, QSplitter,
                               QStackedWidget, QStyle, QTabWidget, QTextBrowser,
                               QTextEdit, QToolButton, QVBoxLayout, QWidget)

from ..config import Settings
from ..export import MODE_FULL, MODE_LIGHT, export_manual
from ..models import (Frame, RunReport, Step, StepEvidence, Transcript, VideoInfo,
                      VisionNote)
from ..pipeline import STAGE_LABELS, STAGES
from ..render import short_visible
from ..render.html import render_html
from ..render.markdown import render_markdown
from ..steps.validate import export_blockers
from ..store import (FRAMES_JSON, GUIDE_HTML, GUIDE_MD, OUTPUT_RES, REPORT_JSON,
                     RUNS_DIR, STEPS_JSON, TRANSCRIPT_JSON, VIDEO_JSON, VISION_JSON,
                     frame_abs, list_runs, read_json, remove_from_history, rp,
                     write_json, write_text)
from . import theme
from .preview import ImageViewer, PreviewPane
from .settings_dialog import SettingsDialog
from .worker import AnalyzeWorker


def _mmss(x: float) -> str:
    x = max(0, int(round(x or 0)))
    return f"{x // 60:02d}:{x % 60:02d}"


def _esc(text: str) -> str:
    return _html.escape(str(text or ""), quote=True)


def _short_model(model_id: str) -> str:
    """模型 ID 可能是很长的 .gguf 路径，信息卡里只显示文件名部分。"""
    raw = str(model_id or "").strip()
    if not raw:
        return "未设置"
    name = raw.replace("\\", "/").rsplit("/", 1)[-1]
    if name.lower().endswith(".gguf"):
        name = name[:-5]
    return name


class StageBar(QFrame):
    """分析时显示在顶部的进度区：进度条 + 现在在干什么 + 七个阶段指示器。

    进度百分比是按阶段权重估算的（不是逐像素的真实进度），所以这里同时把
    "现在处于哪个阶段"画出来，用户看到的不是一个孤零零的数字在跳。
    """

    cancel_clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("stageBar")
        self.setStyleSheet(
            "#stageBar{background:#f6f8fa;border:1px solid #d0d7de;border-radius:8px;}")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 8, 12, 8)
        lay.setSpacing(6)

        top = QHBoxLayout()
        top.setSpacing(10)
        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        self.bar.setFormat("%p%")
        self.bar.setMinimumWidth(240)
        self.lbl_now = QLabel("准备中…")
        self.lbl_now.setStyleSheet("color:#1f2328;font-size:13px;")
        self.btn_cancel = QPushButton("取消分析")
        self.btn_cancel.clicked.connect(self.cancel_clicked.emit)
        top.addWidget(self.bar, 1)
        top.addWidget(self.btn_cancel)
        lay.addLayout(top)

        self.lbl_stages = QLabel()
        self.lbl_stages.setTextFormat(Qt.RichText)
        self.lbl_stages.setStyleSheet("font-size:12px;")
        lay.addWidget(self.lbl_stages)
        self._render_stages(None)

    def _render_stages(self, current) -> None:
        idx = STAGES.index(current) if current in STAGES else -1
        parts = []
        for i, key in enumerate(STAGES):
            label = STAGE_LABELS[key]
            if i < idx:
                parts.append(f'<span style="color:#1a7f37">✓ {label}</span>')
            elif i == idx:
                parts.append(f'<b style="color:#0969da">{label}</b>')
            else:
                parts.append(f'<span style="color:#8b949e">{label}</span>')
        self.lbl_stages.setText("  →  ".join(parts))

    def reset(self) -> None:
        self.bar.setValue(0)
        self.bar.setVisible(True)
        self.lbl_now.setText("准备中…")
        self.lbl_now.setStyleSheet("color:#1f2328;font-size:13px;")
        self.btn_cancel.setEnabled(True)
        self._render_stages(None)

    def set_progress(self, msg: str, status: str, pct, stage) -> None:
        self.lbl_now.setText(msg)
        if isinstance(pct, int):
            self.bar.setValue(max(0, min(100, pct)))
        self._render_stages(stage or None)

    def finish(self, text: str, ok: bool = True) -> None:
        self.lbl_now.setText(text)
        color = "#1a7f37" if ok else "#cf222e"
        self.lbl_now.setStyleSheet(f"color:{color};font-size:13px;font-weight:600;")
        if ok:
            self.bar.setValue(100)
        self.btn_cancel.setEnabled(False)


class StepEditDialog(QDialog):
    """改一个步骤的文字内容。改完立刻回写 steps.json 并重新出手册。"""

    def __init__(self, step: Step, parent=None):
        super().__init__(parent)
        self.step = step
        self.setWindowTitle(f"编辑步骤 {step.index}")
        self.setMinimumWidth(560)

        form = QFormLayout()
        self.ed_title = QLineEdit(step.title)
        form.addRow("标题", self.ed_title)

        self.ed_action = QTextEdit(step.action)
        self.ed_action.setMinimumHeight(90)
        form.addRow("具体操作", self.ed_action)

        self.ed_expect = QTextEdit(step.expected_result)
        self.ed_expect.setMinimumHeight(70)
        form.addRow("预期结果", self.ed_expect)

        # 时间范围可改：AI 偶尔把一步的时间框得太窄或太宽，用户要能自己校正
        row_time = QHBoxLayout()
        self.sp_start = QDoubleSpinBox()
        self.sp_start.setRange(0.0, 86400.0)
        self.sp_start.setDecimals(1)
        self.sp_start.setSingleStep(0.5)
        self.sp_start.setSuffix(" 秒")
        self.sp_start.setValue(float(step.t_start))
        self.sp_end = QDoubleSpinBox()
        self.sp_end.setRange(0.0, 86400.0)
        self.sp_end.setDecimals(1)
        self.sp_end.setSingleStep(0.5)
        self.sp_end.setSuffix(" 秒")
        self.sp_end.setValue(float(step.t_end))
        row_time.addWidget(QLabel("开始"))
        row_time.addWidget(self.sp_start)
        row_time.addWidget(QLabel("结束"))
        row_time.addWidget(self.sp_end)
        row_time.addStretch(1)
        form.addRow("时间范围", row_time)

        self.cb_review = QCheckBox("仍需人工复核")
        self.cb_review.setChecked(step.needs_review)
        note = f"置信度 {step.confidence:.2f}"
        form.addRow(QLabel(note), self.cb_review)

        self.ed_note = QLineEdit(step.review_note)
        form.addRow("复核备注", self.ed_note)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout()
        layout.addLayout(form)
        layout.addWidget(buttons)
        self.setLayout(layout)

    def apply(self) -> Step:
        self.step.title = self.ed_title.text().strip() or self.step.title
        self.step.action = self.ed_action.toPlainText().strip()
        self.step.expected_result = self.ed_expect.toPlainText().strip()
        start = float(self.sp_start.value())
        end = float(self.sp_end.value())
        if end < start:                       # 用户把结束改到开始之前，按"零长度"处理
            end = start
        self.step.t_start = start
        self.step.t_end = end
        self.step.needs_review = self.cb_review.isChecked()
        self.step.review_note = self.ed_note.text().strip()
        return self.step


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Video to Guide · 视频转操作手册")
        self.resize(1320, 860)
        self.setMinimumSize(1024, 720)

        self.settings = Settings.load()
        self.video_path: Path | None = None
        self.worker: AnalyzeWorker | None = None
        self.result = None
        self.steps: list[Step] = []
        self.frames: list[Frame] = []
        self.notes: list[VisionNote] = []
        self.transcript = Transcript()
        self.video = VideoInfo(path="")
        self.run_path: Path | None = None

        # 进度节流：攒 150ms 刷一次界面（见 on_progress 的说明）
        self._pending_progress: list[tuple] = []
        self._progress_timer = QTimer(self)
        self._progress_timer.setSingleShot(True)
        self._progress_timer.setInterval(150)
        self._progress_timer.timeout.connect(self._flush_progress)

        self._build_actions()
        self._build_menu()
        self._build_toolbar()
        self._build_central()
        self._build_statusbar()
        self._show_info()
        self.refresh_history()
        self._update_actions()

    # ------------------------------------------------------------ 动作与菜单
    def _make_action(self, text, slot, icon=None, shortcut=None, tip="") -> QAction:
        act = QAction(text, self)
        if icon is not None:
            act.setIcon(self.style().standardIcon(icon))
        if shortcut:
            act.setShortcut(QKeySequence(shortcut))
        if tip:
            act.setToolTip(tip)
            act.setStatusTip(tip)
        act.triggered.connect(slot)
        return act

    def _build_actions(self) -> None:
        S = QStyle
        self.act_open = self._make_action(
            "选择视频…", self.choose_video, S.SP_DialogOpenButton, "Ctrl+O",
            "选择要分析的录屏文件")
        self.act_start = self._make_action(
            "开始分析", self.start_analysis, S.SP_MediaPlay, "F5",
            "转录语音、提取截图并生成操作步骤")
        self.act_cancel = self._make_action(
            "取消", self.cancel_analysis, S.SP_MediaStop, None,
            "停止当前分析（在当前阶段结束时退出）")
        self.act_add = self._make_action(
            "新增步骤", self.add_step, S.SP_FileDialogNewFolder,
            "AI 漏掉的操作用这个补一步（会标成待核实）",
        )
        self.act_edit = self._make_action(
            "编辑步骤", self.edit_step, S.SP_FileDialogDetailedView, "F2",
            "修改选中步骤的标题、操作说明和待核实状态")
        self.act_delete = self._make_action(
            "删除步骤", self.delete_step, S.SP_TrashIcon, "Delete",
            "删除选中的步骤，并立即重新生成手册")
        self.act_export = self._make_action(
            "导出手册…", self.export_guide, S.SP_DialogSaveButton, "Ctrl+E",
            "把 guide.md / guide.html 和截图复制到你选的目录")
        self.act_open_html = self._make_action(
            "打开手册", self.open_guide_html, S.SP_FileDialogContentsView, None,
            "用浏览器打开本次生成的 guide.html")
        self.act_reopen = self._make_action(
            "重新生成手册", self.rerender, S.SP_BrowserReload, "Ctrl+G",
            "用当前步骤数据重新生成 markdown 和 html")
        self.act_outdir = self._make_action(
            "输出目录", self.open_output_dir, S.SP_DirOpenIcon, None,
            "打开这次分析的输出目录")
        self.act_settings = self._make_action(
            "设置…", self.open_settings, S.SP_FileDialogInfoView, "Ctrl+,",
            "选择模型服务商、填 API Key，调整截图与转录参数")
        self.act_quit = self._make_action("退出", self.close, None, "Ctrl+Q")
        self.act_usage = self._make_action("使用说明", self.show_usage)
        self.act_local_model = self._make_action("如何配置本地模型…", self.show_local_model_help)
        self.act_about = self._make_action("关于", self.show_about)

    def _build_menu(self) -> None:
        bar = self.menuBar()
        m_file = bar.addMenu("文件(&F)")
        m_file.addAction(self.act_open)
        m_file.addSeparator()
        m_file.addAction(self.act_export)
        m_file.addAction(self.act_open_html)
        m_file.addAction(self.act_outdir)
        m_file.addSeparator()
        m_file.addAction(self.act_quit)

        m_edit = bar.addMenu("编辑(&E)")
        m_edit.addAction(self.act_start)
        m_edit.addAction(self.act_cancel)
        m_edit.addSeparator()
        m_edit.addAction(self.act_edit)
        m_edit.addAction(self.act_delete)
        m_edit.addSeparator()
        m_edit.addAction(self.act_reopen)

        m_tool = bar.addMenu("工具(&T)")
        m_tool.addAction(self.act_settings)
        m_tool.addSeparator()
        m_tool.addAction(self.act_reopen)
        m_tool.addAction(self.act_outdir)

        m_help = bar.addMenu("帮助(&H)")
        m_help.addAction(self.act_local_model)
        m_help.addAction(self.act_usage)
        m_help.addAction(self.act_about)

    def _build_toolbar(self) -> None:
        """顶部只保留"开始一轮工作"的动作。

        步骤编辑放到步骤列表上方、手册导出放到详情上方，
        因为它们和左右两栏的内容直接相关，放顶部反而要来回找。
        """
        self.toolbar = bar = self.addToolBar("主工具栏")
        bar.setMovable(False)
        bar.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        bar.setIconSize(QSize(18, 18))
        for a in (self.act_open, self.act_start, self.act_cancel):
            bar.addAction(a)
        bar.addSeparator()
        bar.addAction(self.act_export)

        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        bar.addWidget(spacer)
        bar.addAction(self.act_settings)

    # ------------------------------------------------------------ 中央区域
    def _build_central(self) -> None:
        central = QWidget()
        cv = QVBoxLayout(central)
        cv.setContentsMargins(8, 8, 8, 8)
        cv.setSpacing(8)

        self.stagebar = StageBar()
        self.stagebar.setVisible(False)
        self.stagebar.cancel_clicked.connect(self.cancel_analysis)
        cv.addWidget(self.stagebar)

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self._build_left())
        splitter.addWidget(self._build_right())
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([390, 930])
        cv.addWidget(splitter, 1)
        self.setCentralWidget(central)

    def _build_left(self) -> QWidget:
        left = QWidget()
        left.setMinimumWidth(300)
        left.setMaximumWidth(560)
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 4, 0)
        lv.setSpacing(8)

        card = QFrame()
        card.setObjectName("infoCard")
        card.setStyleSheet(
            "#infoCard{background:#f6f8fa;border:1px solid #d0d7de;border-radius:8px;}")
        cl = QVBoxLayout(card)
        cl.setContentsMargins(12, 10, 12, 10)
        cl.setSpacing(4)
        self.lbl_video = QLabel("尚未选择视频")
        self.lbl_video.setWordWrap(True)
        self.lbl_video.setStyleSheet("font-size:15px;font-weight:600;color:#1f2328;")
        self.lbl_meta = QLabel("选择一段录屏后点「开始分析」。")
        self.lbl_meta.setWordWrap(True)
        self.lbl_meta.setStyleSheet("color:#57606a;font-size:12px;")
        self.lbl_badge = QLabel("")
        self.lbl_badge.setWordWrap(True)
        self.lbl_badge.setStyleSheet("color:#57606a;font-size:12px;")
        self.lbl_service = QLabel("")
        self.lbl_service.setWordWrap(True)
        self.lbl_service.setStyleSheet("color:#8b949e;font-size:12px;")
        cl.addWidget(self.lbl_video)
        cl.addWidget(self.lbl_meta)
        cl.addWidget(self.lbl_badge)
        cl.addWidget(self.lbl_service)
        lv.addWidget(card)

        self.tabs_left = QTabWidget()

        # ---- 操作步骤页：搜索框 + 编辑/删除小按钮（操作离内容越近越好找）----
        steps_page = QWidget()
        sv = QVBoxLayout(steps_page)
        sv.setContentsMargins(0, 6, 0, 0)
        sv.setSpacing(6)
        row_filter = QHBoxLayout()
        row_filter.setSpacing(4)
        self.edit_filter = QLineEdit()
        self.edit_filter.setPlaceholderText("搜索步骤标题或内容…")
        self.edit_filter.setClearButtonEnabled(True)
        self.edit_filter.textChanged.connect(self._reload_step_list)
        row_filter.addWidget(self.edit_filter, 1)
        sv.addLayout(row_filter)
        self.list_steps = QListWidget()
        self.list_steps.currentItemChanged.connect(self._on_step_selected)
        self.list_steps.itemDoubleClicked.connect(lambda _: self.edit_step())
        sv.addWidget(self.list_steps, 1)
        self.tabs_left.addTab(steps_page, "操作步骤")
        # 记住"页"对象本身。QTabWidget 的页是外层 QWidget（steps_page/hist_page），
        # 不是里面的列表控件——setCurrentWidget 必须传页，传列表控件是无效操作。
        self.page_steps = steps_page

        # ---- 历史记录页：计数 + 刷新按钮 + 右键菜单 ----
        hist_page = QWidget()
        hv = QVBoxLayout(hist_page)
        hv.setContentsMargins(0, 6, 0, 0)
        hv.setSpacing(6)
        row_hist = QHBoxLayout()
        self.lbl_hist_count = QLabel("")
        self.lbl_hist_count.setStyleSheet("color:#57606a;font-size:12px;")
        btn_refresh = QPushButton("刷新")
        btn_refresh.setToolTip("重新扫描输出目录，看看有没有新的分析记录")
        btn_refresh.clicked.connect(self.refresh_history)
        row_hist.addWidget(self.lbl_hist_count, 1)
        row_hist.addWidget(btn_refresh)
        hv.addLayout(row_hist)
        self.list_history = QListWidget()
        self.list_history.itemDoubleClicked.connect(self.open_history_item)
        self.list_history.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list_history.customContextMenuRequested.connect(self._history_menu)
        hv.addWidget(self.list_history, 1)
        self.tabs_left.addTab(hist_page, "历史记录")
        self.page_history = hist_page

        self.tabs_left.currentChanged.connect(self._on_left_tab)
        lv.addWidget(self.tabs_left, 1)
        return left

    def _build_right(self) -> QWidget:
        """右栏：上半画面预览，下半步骤详情/字幕/日志。"""
        self.stack_right = QStackedWidget()
        self.welcome = self._build_welcome()
        self.stack_right.addWidget(self.welcome)          # 0 = 欢迎页

        work_box = QWidget()
        wv = QVBoxLayout(work_box)
        wv.setContentsMargins(0, 0, 0, 0)
        wv.setSpacing(6)

        # 结果工具行：只跟「这一份手册」有关的动作。
        # 「导出手册」在顶部工具栏（那是每次分析结束都要做的事），这里不重复。
        row_res = QHBoxLayout()
        row_res.setSpacing(4)
        for act in (self.act_open_html, self.act_reopen, self.act_outdir):
            tb = QToolButton()
            tb.setDefaultAction(act)
            tb.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
            tb.setToolTip(act.toolTip())
            row_res.addWidget(tb)
        row_res.addStretch(1)
        # 耗时预估单独一行，常驻显示：分析可能要跑十几分钟，用户需要知道还要等多久，
        # 而不是盯着进度条猜。抽帧一完成就换成按真实帧数算的值。
        self.lbl_eta = QLabel("")
        self.lbl_eta.setStyleSheet(
            f"color:{theme.ACCENT};font-size:12px;font-weight:600;")
        self.lbl_eta.setVisible(False)
        row_res.addWidget(self.lbl_eta)
        self.lbl_res_hint = QLabel("")
        self.lbl_res_hint.setStyleSheet(f"color:{theme.TEXT_FAINT};font-size:12px;")
        row_res.addWidget(self.lbl_res_hint)
        wv.addLayout(row_res)

        # 上下两栏可拖动：看画面时把预览拉高，读文字时把详情拉高。
        self.preview = PreviewPane()
        self.preview.zoom_requested.connect(self._open_image)
        vsplit = QSplitter(Qt.Vertical)
        vsplit.addWidget(self.preview)
        vsplit.addWidget(self._build_work_tabs())
        vsplit.setStretchFactor(0, 3)
        vsplit.setStretchFactor(1, 2)
        vsplit.setSizes([420, 300])
        vsplit.setChildrenCollapsible(False)
        self.vsplit_right = vsplit
        wv.addWidget(vsplit, 1)
        self.stack_right.addWidget(work_box)              # 1 = 工作区
        return self.stack_right

    def _build_work_tabs(self) -> QWidget:
        """详情 / 字幕 / 日志。详情是默认页，日志只是排查时用。"""
        self.work = QTabWidget()

        detail_page = QWidget()
        dv = QVBoxLayout(detail_page)
        dv.setContentsMargins(8, 8, 8, 8)
        dv.setSpacing(6)
        row_detail = QHBoxLayout()
        row_detail.setSpacing(4)
        for act in (self.act_add, self.act_edit, self.act_delete):
            tb = QToolButton()
            tb.setDefaultAction(act)
            tb.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
            tb.setToolTip(act.toolTip())
            row_detail.addWidget(tb)
        row_detail.addStretch(1)
        self.lbl_detail_hint = QLabel("双击左侧步骤也可以修改")
        self.lbl_detail_hint.setStyleSheet(f"color:{theme.TEXT_FAINT};font-size:12px;")
        row_detail.addWidget(self.lbl_detail_hint)
        dv.addLayout(row_detail)
        self.detail = QTextBrowser()
        self.detail.setOpenExternalLinks(True)
        self.detail.setStyleSheet(
            f"QTextBrowser{{background:{theme.PANEL};border:none;}}")
        dv.addWidget(self.detail, 1)
        self.work.addTab(detail_page, "步骤详情")
        self.page_detail = detail_page

        self.txt_transcript = QPlainTextEdit()
        self.txt_transcript.setReadOnly(True)
        self.txt_transcript.setStyleSheet(
            f"font-family:{theme.FONT_MONO};font-size:13px;")
        self.work.addTab(self.txt_transcript, "全部字幕")

        self.txt_log = QPlainTextEdit()
        self.txt_log.setReadOnly(True)
        self.txt_log.setStyleSheet(
            f"font-family:{theme.FONT_MONO};font-size:12px;")
        self.work.addTab(self.txt_log, "运行日志")
        return self.work

    def _build_welcome(self) -> QWidget:
        box = QWidget()
        lay = QVBoxLayout(box)
        lay.setContentsMargins(56, 40, 56, 40)
        lay.setSpacing(12)
        lay.addStretch(1)

        title = QLabel("把录屏变成可执行的操作手册")
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet("font-size:24px;font-weight:700;color:#1f2328;")
        sub = QLabel("导入一段软件操作视频，自动生成带截图、时间戳和操作说明的图文手册。\n"
                     "全部在本机处理：内置抽帧 · 内置语音识别 · 本地视觉模型看画面。")
        sub.setAlignment(Qt.AlignCenter)
        sub.setWordWrap(True)
        sub.setStyleSheet("color:#57606a;font-size:14px;line-height:1.6;")
        lay.addWidget(title)
        lay.addWidget(sub)
        lay.addSpacing(10)

        row = QHBoxLayout()
        row.addStretch(1)
        self.btn_welcome_open = QPushButton("选择视频…")
        self.btn_welcome_open.setMinimumHeight(40)
        self.btn_welcome_open.setMinimumWidth(150)
        self.btn_welcome_open.setCursor(Qt.PointingHandCursor)
        self.btn_welcome_open.setStyleSheet(
            "QPushButton{background:#0969da;color:#ffffff;border:none;border-radius:6px;"
            "font-size:15px;}QPushButton:hover{background:#0860c4;}"
            "QPushButton:disabled{background:#c6cbd1;color:#6e7781;}")
        self.btn_welcome_open.clicked.connect(self.choose_video)
        row.addWidget(self.btn_welcome_open)
        row.addStretch(1)
        lay.addLayout(row)
        lay.addSpacing(6)

        fmt = QLabel("支持 MP4 / MKV / MOV / AVI / WebM / FLV。"
                     "有旁白更好（会自动转录），没有音轨也能只靠画面分析。")
        fmt.setAlignment(Qt.AlignCenter)
        fmt.setWordWrap(True)
        fmt.setStyleSheet("color:#8b949e;font-size:12px;")
        flow = QLabel("① 选择视频　→　② 开始分析　→　③ 核对并修改步骤　→　④ 导出手册")
        flow.setAlignment(Qt.AlignCenter)
        flow.setWordWrap(True)
        flow.setStyleSheet("color:#57606a;font-size:13px;")
        lay.addWidget(fmt)
        lay.addWidget(flow)
        lay.addStretch(2)
        return box

    def _build_statusbar(self) -> None:
        self.lbl_status = QLabel("就绪。请先选择一段视频，或从「历史记录」里打开一条。")
        self.statusBar().addWidget(self.lbl_status, 1)


    # ------------------------------------------------------------ 状态刷新
    def _update_actions(self) -> None:
        running = self.worker is not None and self.worker.isRunning()
        has_steps = bool(self.steps)
        self.act_start.setEnabled(self.video_path is not None and not running)
        self.act_cancel.setEnabled(running)
        self.act_open.setEnabled(not running)
        self.act_add.setEnabled(bool(self.run_path) and not running)
        self.act_edit.setEnabled(has_steps and not running)
        self.act_delete.setEnabled(has_steps and not running)
        self.act_export.setEnabled(has_steps and not running)
        self.act_reopen.setEnabled(has_steps and not running)
        self.act_open_html.setEnabled(self._guide_html_path() is not None)
        self.act_outdir.setEnabled(self.run_path is not None)
        self.act_settings.setEnabled(not running)
        self.btn_welcome_open.setEnabled(not running)
        if hasattr(self, "lbl_res_hint"):
            self.lbl_res_hint.setText(f"输出目录：{self.run_path.name}" if self.run_path else "")

    def _status(self, text: str, kind: str = "info") -> None:
        color = {"ok": "#1a7f37", "warn": "#9a6700", "fail": "#cf222e"}.get(kind, "#1f2328")
        self.lbl_status.setText(text)
        self.lbl_status.setStyleSheet(f"color:{color};")

    def _show_info(self) -> None:
        v = self.video
        name = Path(v.path).name if v.path else (self.video_path.name if self.video_path else "")
        self.lbl_video.setText(name or "尚未选择视频")
        bits = []
        if v.duration:
            bits.append(_mmss(v.duration))
        if v.width and v.height:
            bits.append(f"{v.width}×{v.height}")
        # 只有真读到视频信息（有 duration）才谈音轨：分析刚开始时 video 对象是空的，
        # 直接按 has_audio=False 显示会写成"无音轨"，把有语音的视频说成没声音。
        if v.duration and (v.path or self.video_path):
            bits.append("有音轨" if v.has_audio else "无音轨")
        if self.transcript.segments:
            bits.append(f"{len(self.transcript.segments)} 条字幕")
        if self.frames:
            bits.append(f"{len(self.frames)} 张截图")
        self.lbl_meta.setText(" · ".join(bits) if bits else "选择一段录屏后点「开始分析」。")

        review = sum(1 for s in self.steps if s.needs_review)
        if self.steps and review:
            self.lbl_badge.setText(f"⚠ {review} 个步骤待人工核实（双击步骤可修改）")
            self.lbl_badge.setStyleSheet("color:#9a6700;font-size:12px;")
        elif self.steps:
            self.lbl_badge.setText(f"✓ {len(self.steps)} 个步骤，都带证据")
            self.lbl_badge.setStyleSheet("color:#1a7f37;font-size:12px;")
        else:
            self.lbl_badge.setText("")
            self.lbl_badge.setStyleSheet("color:#57606a;font-size:12px;")

        # 用「视觉角色」的真实来源，而不是旧的单套 llm_* 字段：
        # 用户完全可能把识图放在本机、写步骤放在云端。
        pid, _base, _key, model = self.settings.role_client("vision")
        from ..providers import get as _get_provider
        prov = _get_provider(pid)
        self.lbl_service.setText(f"识图：{prov.name} · {_short_model(model)}")
        self.lbl_service.setToolTip(
            f"服务商：{prov.name}\n接口地址：{_base}\n模型：{model or '（未选择，点「设置」选一个）'}")

    # ------------------------------------------------------------ 选视频 / 分析
    def choose_video(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "选择要分析的视频", "",
            "视频文件 (*.mp4 *.mkv *.mov *.avi *.webm *.flv);;所有文件 (*.*)")
        if not path:
            return
        p = Path(path)
        if not p.exists():
            QMessageBox.warning(self, "文件不可用", f"找不到这个文件：\n{p}")
            return
        if p.stat().st_size == 0:
            QMessageBox.warning(self, "文件不可用", f"这是一个 0 字节的空文件：\n{p.name}")
            return
        self.video_path = p
        self.stack_right.setCurrentIndex(1)
        self.work.setCurrentIndex(0)
        self.preview.clear("已选择视频。点「开始分析」后，这里会显示每一步对应的截图。")
        self.detail.setHtml(
            f"<style>{theme.detail_html_css()}</style>"
            f"<h2>{_esc(p.name)}</h2>"
            f'<p class="time">{_esc(str(p))}</p>'
            '<p class="field"><span class="label">下一步</span><br>'
            "点顶部「开始分析」（F5）：先读视频信息，再转录语音、提取截图、"
            "用本地模型看画面，最后生成带证据的操作步骤。</p>"
            '<p class="ev">全部在本机处理，视频不会上传。</p>')
        self._status(f"已选择：{p.name}（点「开始分析」）")
        self._update_actions()

    def _resume_run_id(self) -> str | None:
        """当前加载的任务如果没跑完、而且视频没变，就复用它，接着上次的进度跑。

        为什么要这么做：视觉分析是最贵的一步（每张截图十几秒）。上次跑到一半
        模型服务断了，用户再点一次「开始分析」，不该让已经分析完的截图从头再来。
        换了视频就一定新建任务，绝不复用别人的中间结果。
        """
        if not self.run_path or not self.run_path.exists() or not self.video_path:
            return None
        old_video = read_json(rp(self.run_path, VIDEO_JSON)) or {}
        try:
            same = Path(old_video.get("path") or "").resolve() == Path(self.video_path).resolve()
        except OSError:
            same = False
        if not same:
            return None
        report = read_json(rp(self.run_path, REPORT_JSON)) or {}
        if report.get("status") not in ("partial", "failed", "cancelled", "running"):
            return None
        notes = read_json(rp(self.run_path, VISION_JSON)) or {}
        done = len([n for n in notes.get("notes", []) if not n.get("error")])
        self.txt_log.appendPlainText(
            f"继续上次未完成的任务（{self.run_path.name}）："
            f"已完成的 {done} 张截图描述会被复用，只重做没做完的部分。")
        return self.run_path.name

    def _ask_edited_steps(self) -> str:
        """重新分析前先问清楚：已有的结果怎么办。

        返回 "new"（另开新任务）/ "overwrite"（备份后覆盖）/ "cancel"。

        为什么只要有结果就问，而不只在"被手工改过"时才问：
        步骤生成本身不是完全可复现的，同一份视频两次跑出的步骤数可能差一倍
        （实测 27 → 35）。用户点"重新分析"时如果不提示，就会出现
        "昨天导出的手册 27 步、今天 35 步"却不知道发生了什么。
        默认按钮放在"保留现在的结果"上——已有成果比重新跑一次更值钱。
        """
        if not self.run_path or not self.run_path.exists():
            return "new"
        old = read_json(rp(self.run_path, STEPS_JSON)) or {}
        has_steps = bool(old.get("steps"))
        stamp = old.get("edited_at")
        if not has_steps:
            return "new"
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Question)
        if stamp:
            box.setWindowTitle("这份手册被手工修改过")
            box.setText(f"这个任务的手册在 {stamp} 被修改过。重新分析会重新调用模型，"
                        "生成的步骤数量和内容都可能和现在不同。")
        else:
            box.setWindowTitle("重新分析会生成新的步骤")
            box.setText("这个任务已经分析过了。重新分析会重新调用模型，"
                        "步骤数量和内容都可能和现在不一样（同一视频两次结果并不完全相同）。")
        box.setInformativeText("选择怎么处理：")
        keep = box.addButton("保留现在的结果，另存为新任务", QMessageBox.AcceptRole)
        over = box.addButton("备份现在的结果后重新分析", QMessageBox.DestructiveRole)
        box.addButton("取消", QMessageBox.RejectRole)
        box.setDefaultButton(keep)
        box.exec()
        clicked = box.clickedButton()
        if clicked is keep:
            return "new"
        if clicked is over:
            return "overwrite"
        return "cancel"


    def _ensure_ai_ready(self) -> bool:
        """分析前的门禁：AI 服务没就绪就拦下来，说明原因并给修复入口。"""
        from .. import health

        self._status("正在检查模型服务…")
        states = health.probe_all(self.settings)
        reason = health.block_reason(states)
        if not reason:
            return True

        box = QMessageBox(self)
        box.setWindowTitle("还不能开始分析")
        box.setIcon(QMessageBox.Warning)
        box.setText(reason)
        btn_settings = box.addButton("打开设置…", QMessageBox.ActionRole)
        btn_recheck = box.addButton("重新检测", QMessageBox.ActionRole)
        box.addButton("取消", QMessageBox.RejectRole)
        box.exec()

        if box.clickedButton() is btn_settings:
            self.open_settings()
        elif box.clickedButton() is btn_recheck:
            if self._ensure_ai_ready():
                self.start_analysis()
        self._status("AI 服务未就绪，分析已取消", "fail")
        return False

    def _ensure_asr_model(self) -> None:
        """首次分析前提醒换更准的语音模型。

        为什么放在分析前而不是启动时：启动时用户还没进入状态，弹窗只会被关掉；
        分析前提示，他能立刻明白"等会儿出来的文字会不会错"。只问一次。
        """
        from .. import asr_models

        if getattr(self.settings, "asr_prompted", False):
            return
        cur = (getattr(self.settings, "whisper_model", "small") or "small").strip()
        # 已经在用好模型、或者已经下好了 large-v3，就不用劝了
        if cur not in ("tiny", "base", "small"):
            return
        if asr_models.status("large-v3", self.settings)["installed"]:
            return

        box = QMessageBox(self)
        box.setWindowTitle("要不要换个更准的语音模型")
        box.setIcon(QMessageBox.Information)
        box.setText(
            "现在用的是 small 语音模型，中文识别会有同音字错误，"
            "比如把「工具栏」听成「工具蓝」、「示例」听成「视力」。\n\n"
            "换成 large-v3 后，同一段测试视频的字错率从 4.7% 降到 0，"
            "代价是要下载约 3 GB，识别也慢一些（120 秒音频约 8 秒）。\n\n"
            "下载一次就行，之后可以离线使用。")
        btn_dl = box.addButton("下载 large-v3（推荐）", QMessageBox.ActionRole)
        btn_small = box.addButton("先用 small", QMessageBox.AcceptRole)
        box.addButton("以后再说", QMessageBox.RejectRole)
        box.exec()

        self.settings.asr_prompted = True
        self.settings.save()
        if box.clickedButton() is btn_dl:
            # 3 = 「额外内容」页，语音模型在那里
            self.open_settings(tab=3)
        elif box.clickedButton() is btn_small:
            self._status("将使用 small 模型；随时可以在设置里换档位。")

    def start_analysis(self) -> None:
        if not self.video_path:
            return
        if self.worker and self.worker.isRunning():
            return

        # 先确认模型服务真的可用，再开始花钱花时间跑一遍。
        # 没有这一步的后果是：跑几分钟、降级、最后给 0 个步骤，
        # 用户以为软件坏了。现在直接把原因说清楚，并给"去设置里改"的入口。
        if not self._ensure_ai_ready():
            return

        self._ensure_asr_model()

        edited_choice = self._ask_edited_steps()
        if edited_choice == "cancel":
            self._status("已取消：保留原有手工修改")
            return
        resume_id = None if edited_choice == "new" else self._resume_run_id()
        self.steps = []
        self.frames = []
        self.notes = []
        self.transcript = Transcript()
        self.video = VideoInfo(path="")
        self.run_path = None
        self.list_steps.clear()
        self.detail.clear()
        self.txt_transcript.clear()
        self.edit_filter.clear()

        self.stagebar.reset()
        self.stagebar.setVisible(True)
        self.txt_log.clear()
        self.txt_log.appendPlainText(f"开始分析：{self.video_path}")
        # 分析中停在「步骤详情」而不是日志页：日志是排查时才看的次级内容，
        # 日常等待时看顶部进度条就够了。
        self.work.setCurrentIndex(0)
        self.stack_right.setCurrentIndex(1)
        self.preview.clear("正在分析…截图生成后会显示在这里。")
        if hasattr(self, "lbl_eta"):
            hint = self._estimate_before_start()
            if hint:
                self.lbl_eta.setText(hint)
                self.lbl_eta.setVisible(True)
            else:
                self.lbl_eta.setVisible(False)
        self.detail.setHtml(
            f"<style>{theme.detail_html_css()}</style>"
            "<h2>正在分析</h2>"
            '<p class="field"><span class="label">处理顺序</span><br>'
            "读取视频信息 → 语音转录 → 提取关键帧 → 画面理解 → 生成步骤 → 生成手册</p>"
            '<p class="ev">进度见顶部进度条；详细日志在下方「运行日志」页，'
            "排查问题时再切换过去。</p>")
        self._show_info()
        self._status("开始分析…")

        self.worker = AnalyzeWorker(self.video_path, self.settings, run_id=resume_id,
                                    allow_overwrite_edits=(edited_choice == "overwrite"))
        self.worker.progress.connect(self.on_progress)
        self.worker.finished_ok.connect(self.on_finished)
        self.worker.failed.connect(self.on_failed)
        self.worker.cancelled.connect(self.on_cancelled)
        self.worker.start()
        self._update_actions()

    def cancel_analysis(self) -> None:
        if self.worker and self.worker.isRunning():
            self.worker.cancel()
            self.stagebar.btn_cancel.setEnabled(False)
            self.stagebar.finish("正在取消…（当前阶段结束后停止）", ok=False)
            self._status("正在取消…（当前阶段结束后停止）", "warn")

    def on_progress(self, msg: str, status: str, pct, stage: str) -> None:
        """进度来了先攒着，由定时器统一刷新界面。

        为什么不直接更新：视觉阶段是"每张图一条进度"，长视频几十上百条连着到达，
        每条都触发状态栏重排 + 日志追加 + 滚动到底，主线程会被这些小事占满，
        用户体感就是"分析时点不动"。攒 150ms 再刷一次，用户看不出差别，CPU 省下来。
        失败/取消/完成这类关键状态不受节流影响，立刻显示。
        """
        self._pending_progress.append((msg, status, pct, stage))
        if status in ("fail", "warn") or msg.endswith("完成"):
            self._flush_progress()          # 异常与收尾必须立刻可见
            return
        if not self._progress_timer.isActive():
            self._progress_timer.start()

    def _flush_progress(self) -> None:
        """把攒下的进度一次性刷到界面上（只显示最后一条，日志全部追加）。"""
        if not self._pending_progress:
            return
        items = self._pending_progress
        self._pending_progress = []
        lines = []
        for msg, status, pct, stage in items:
            tag = f"[{pct:3d}%] " if isinstance(pct, int) else ""
            lines.append(f"{tag}{msg}")
        last = items[-1]
        self._status(last[0], last[1])
        self.stagebar.set_progress(last[0], last[1], last[2], last[3])
        if hasattr(self, "lbl_eta"):
            self._update_eta(last[0])
        # 批量追加：一次 appendPlainText 比 N 次各自的布局/重绘便宜得多
        self.txt_log.appendPlainText("\n".join(lines))
        self.txt_log.verticalScrollBar().setValue(self.txt_log.verticalScrollBar().maximum())

    def _update_eta(self, msg: str) -> None:
        """从进度消息里抓出"预计 X 分钟"并常驻显示。"""
        import re

        m = re.search(r"预计\s*([^）)]+)", msg or "")
        if m:
            self.lbl_eta.setText("原本预计 " + m.group(1).strip())
            self.lbl_eta.setVisible(True)
        elif msg.startswith("画面理解") or "张截图" in msg:
            self.lbl_eta.setVisible(bool(self.lbl_eta.text()))

    def _estimate_before_start(self) -> str:
        """开始分析前给一个量级预估。

        此时还没抽帧，帧数只能按经验密度猜：讲解型教程大约每分钟 10-20 帧，
        纯静默演示低得多。所以给区间而不是单个数，并说明这只是"量级"。
        """
        from ..pipeline import estimate_vision_seconds, human_seconds

        dur = float(getattr(getattr(self, "video", None), "duration", 0) or 0)
        if dur <= 0:
            return ""
        lo = estimate_vision_seconds(dur / 60 * 5, self.settings)
        hi = estimate_vision_seconds(dur / 60 * 22, self.settings)
        return f"画面理解预计 {human_seconds(lo)} — {human_seconds(hi)}（抽帧后会给准确值）"

    def on_finished(self, result) -> None:
        self._flush_progress()
        self._progress_timer.stop()
        self.result = result
        self.run_path = result.run_path
        self.steps = result.steps
        self.frames = result.frames
        self.notes = result.notes
        self.transcript = result.transcript or Transcript()
        self.video = result.video or VideoInfo(path="")
        if self.video.path:
            self.video_path = Path(self.video.path)
        self.worker = None

        report = result.report
        self._reload_step_list()
        self._load_transcript_pane()
        self._load_report_pane(report)
        self._show_info()
        self._update_actions()
        self.refresh_history()
        self.stack_right.setCurrentIndex(1)

        if report.status == "failed":
            self.stagebar.finish("分析失败：" + ("；".join(report.errors) or "未知原因"), ok=False)
            QTimer.singleShot(6000, self.stagebar.hide)
            self.work.setCurrentWidget(self.txt_log)
            self._status("分析失败：" + ("；".join(report.errors) or "未知原因"), "fail")
            QMessageBox.critical(
                self, "分析失败",
                ("\n".join(report.errors) or "未知原因")
                + "\n\n详细信息见右侧「运行日志」，或点「输出目录」查看 run_report.json。")
        else:
            self.stagebar.finish(f"完成：{len(self.steps)} 个步骤")
            QTimer.singleShot(6000, self.stagebar.hide)
            self.work.setCurrentIndex(0)
            if report.warnings:
                self._status(f"完成（有 {len(report.warnings)} 条警告，详见「运行日志」）", "warn")
            else:
                self._status(f"完成：{len(self.steps)} 个步骤", "ok")

    def on_failed(self, message: str) -> None:
        self.stagebar.finish("出错：" + message, ok=False)
        QTimer.singleShot(6000, self.stagebar.hide)
        self._status(f"出错：{message}", "fail")
        self.preview.clear("分析出错，没有可显示的截图。")
        QMessageBox.critical(self, "出错了", message + "\n\n详细信息见右侧「运行日志」。")
        self.worker = None
        self._update_actions()

    def on_cancelled(self) -> None:
        self.stagebar.finish("已取消", ok=False)
        QTimer.singleShot(4000, self.stagebar.hide)
        self._status("已取消。可以调整后重新开始分析。", "warn")
        self.preview.clear("已取消。重新点「开始分析」即可继续。")
        self.worker = None
        self._update_actions()

    # ------------------------------------------------------------ 历史记录
    def _on_left_tab(self, index: int) -> None:
        """左栏切到「历史记录」时刷新列表并给出可见反馈。

        历史记录只有这一个入口（左栏标签），所以反馈必须做足：
        刷新列表 + 自动选中第一行 + 状态栏说明条数与操作方式。
        """
        if self.tabs_left.widget(index) is not self.page_history:
            return
        self.refresh_history()
        n = self._history_count()
        if n:
            if self.list_history.currentRow() < 0:
                self.list_history.setCurrentRow(0)
            self._status(
                f"历史记录：共 {n} 条。双击打开一条；右键可打开目录或从列表移除。",
                "info")
        else:
            self._status("历史记录还是空的：分析完一段视频后会自动出现在这里。", "warn")

    def _history_count(self) -> int:
        """真正可打开的记录条数（空状态提示项不算）。"""
        n = 0
        for i in range(self.list_history.count()):
            if self.list_history.item(i).data(Qt.UserRole):
                n += 1
        return n

    def refresh_history(self) -> None:
        current = self.list_history.currentItem()
        keep = current.data(Qt.UserRole) if current else None
        self.list_history.clear()
        runs = list_runs()
        status_text = {"ok": "已完成", "partial": "部分完成", "failed": "失败"}
        if hasattr(self, "lbl_hist_count"):
            self.lbl_hist_count.setText(f"共 {len(runs)} 条记录" if runs else "还没有记录")
        if not runs:
            item = QListWidgetItem("（还没有分析记录）")
            item.setFlags(Qt.NoItemFlags)
            self.list_history.addItem(item)
            hint = QListWidgetItem("做完一次分析就会出现在这里，双击可重新打开")
            hint.setFlags(Qt.NoItemFlags)
            self.list_history.addItem(hint)
            return
        for r in runs:
            title = r["video_name"] or r["run_id"]
            when = (r["finished_at"] or "")[5:16]
            bits = []
            if r["duration"]:
                bits.append(_mmss(r["duration"]))
            bits.append(f"{r['steps']} 步")
            if r["needs_review"]:
                bits.append(f"⚠ 待核实 {r['needs_review']}")
            bits.append(status_text.get(r["status"], r["status"]))
            lost = not r.get("video_exists")
            if lost:
                bits.append("源视频找不到")
            meta = " · ".join(bits)
            item = QListWidgetItem(f"{title}\n{meta}{('　' + when) if when else ''}")
            item.setData(Qt.UserRole, r["run_id"])
            item.setSizeHint(QSize(0, 52))
            tip = (f"记录目录：{r['path']}\n"
                   f"源视频：{r['video_path'] or '（未记录）'}\n"
                   f"截图：{r.get('frames', 0)} 张　手册：{'已生成' if r.get('has_guide') else '未生成'}\n")
            if lost:
                tip += "⚠ 源视频已不在原来的位置：截图、手册仍可查看和导出，\n"
                tip += "   但 HTML 里的「跳到视频」会失效，重新分析需要先找回源视频。\n"
            tip += "双击重新打开；右键可查看信息、移除或删除"
            item.setToolTip(tip)
            if r["status"] == "failed":
                item.setForeground(Qt.red)
            elif lost:
                item.setForeground(Qt.darkGray)
            elif r["needs_review"]:
                item.setForeground(Qt.darkYellow)
            self.list_history.addItem(item)
            if keep and r["run_id"] == keep:
                self.list_history.setCurrentItem(item)

    def _history_menu(self, pos) -> None:
        """历史记录右键菜单。

        刻意把"从列表移除"和"删除文件"分成两个动作：
        前者只改 runs/history.json 里的一条索引，磁盘上一个字节都不动；
        后者才是真的 rmtree，必须再确认一次。
        """
        item = self.list_history.itemAt(pos)
        if item is None:
            return
        run_id = item.data(Qt.UserRole)
        if not run_id:
            return
        run_id = str(run_id)
        menu = QMenu(self)
        menu.addAction("打开这条记录", lambda: self.load_run(run_id))
        menu.addAction("查看记录信息…", lambda: self.show_history_info(run_id))
        menu.addAction("在文件夹中显示", lambda: self.reveal_run_dir(run_id))
        menu.addSeparator()
        menu.addAction("从历史记录中移除（不删文件）",
                       lambda: self.remove_history_entry(run_id))
        menu.addAction("删除记录目录及生成的文件…",
                       lambda: self.delete_run(run_id))
        menu.exec(self.list_history.mapToGlobal(pos))

    def show_history_info(self, run_id: str) -> None:
        """查看一条记录的基本信息；旧记录缺字段也要能显示，不能崩。"""
        run_path = RUNS_DIR / run_id
        if not run_path.is_dir():
            QMessageBox.warning(
                self, "记录不存在",
                f"找不到记录目录：\n{run_path}\n\n"
                "可能已被手动删除。点左栏「刷新」更新列表。")
            self.refresh_history()
            return
        video = read_json(rp(run_path, VIDEO_JSON)) or {}
        report = read_json(rp(run_path, REPORT_JSON)) or {}
        steps_doc = read_json(rp(run_path, STEPS_JSON)) or {}
        frames_doc = read_json(rp(run_path, FRAMES_JSON)) or {}
        vpath = video.get("path") or ""
        exists = bool(vpath) and Path(vpath).exists()
        w, h = video.get("width") or "?", video.get("height") or "?"
        lines = [
            f"记录 ID：{run_id}",
            f"记录目录：{run_path}",
            f"源视频：{vpath or '（未记录）'}",
            f"源视频状态：{'仍在原位置' if exists else '已找不到（截图和手册仍可查看）'}",
            f"时长：{_mmss(float(video.get('duration') or 0))}　画面：{w}×{h}",
            f"步骤：{len(steps_doc.get('steps') or [])} 个　"
            f"截图：{len(frames_doc.get('frames') or [])} 张　"
            f"手册：{'已生成' if rp(run_path, GUIDE_HTML).exists() else '未生成'}",
            f"状态：{report.get('status') or '?'}",
            f"开始：{report.get('started_at') or '?'}",
            f"结束：{report.get('finished_at') or '?'}",
        ]
        warns = report.get("warnings") or []
        errs = report.get("errors") or []
        if warns:
            lines.append("")
            lines.append(f"警告 {len(warns)} 条：")
            lines.extend(f"  · {w_}" for w_ in warns[:6])
            if len(warns) > 6:
                lines.append(f"  … 还有 {len(warns) - 6} 条")
        if errs:
            lines.append("")
            lines.append(f"错误 {len(errs)} 条：")
            lines.extend(f"  · {e_}" for e_ in errs[:6])
        QMessageBox.information(self, "记录信息", "\n".join(lines))

    def reveal_run_dir(self, run_id: str) -> None:
        run_path = RUNS_DIR / run_id
        if not run_path.is_dir():
            QMessageBox.warning(
                self, "目录不存在",
                f"找不到记录目录：\n{run_path}\n\n点左栏「刷新」更新列表。")
            self.refresh_history()
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(run_path))):
            QMessageBox.warning(self, "打不开文件夹", f"系统没能打开：\n{run_path}")

    def _close_current_run(self) -> None:
        """把当前打开的记录从界面上卸掉（不碰磁盘）。"""
        self.steps, self.frames, self.notes = [], [], []
        self.run_path = None
        self.result = None
        self.list_steps.clear()
        self.detail.clear()
        self.txt_transcript.clear()
        self.txt_log.clear()
        self.stack_right.setCurrentIndex(0)
        self._show_info()
        self._update_actions()

    def remove_history_entry(self, run_id: str) -> None:
        """默认的"删除"：只从列表里移除索引，不删任何文件。"""
        run_path = RUNS_DIR / run_id
        ret = QMessageBox.question(
            self, "从历史记录中移除",
            f"要把这条记录从列表里移除吗？\n\n{run_id}\n\n"
            "只移除列表里的这一条，不会删除磁盘上的任何文件：\n"
            "截图、手册、中间结果、源视频都会原样保留在：\n"
            f"{run_path}\n\n"
            "以后想找回来，可以在这个目录里直接打开 guide.html。",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if ret != QMessageBox.Yes:
            return
        try:
            remove_from_history(run_id)
        except Exception as e:
            QMessageBox.warning(self, "移除失败",
                                f"写历史记录索引时出错：{type(e).__name__}: {e}")
            return
        if self.run_path and self.run_path.name == run_id:
            self._close_current_run()
        self.refresh_history()
        self._status(f"已从列表移除 {run_id}；文件仍保留在 {run_path}", "ok")

    def delete_run(self, run_id: str) -> None:
        """真正删除：rmtree 整个记录目录（截图、手册、中间结果）。源视频不动。"""
        run_path = RUNS_DIR / run_id
        if not run_path.is_dir():
            self.refresh_history()
            return
        ret = QMessageBox.question(
            self, "删除记录目录",
            f"要永久删除这条记录的全部文件吗？\n\n{run_id}\n\n"
            "会删掉：截图、手册、字幕、中间结果（整个记录目录）\n"
            "不会删：源视频文件\n\n"
            "这个操作无法撤销。如果只想让列表干净一点，"
            "请改用「从历史记录中移除（不删文件）」。",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if ret != QMessageBox.Yes:
            return
        try:
            shutil.rmtree(run_path)
        except Exception as e:
            QMessageBox.warning(self, "删不掉", f"删除失败：{type(e).__name__}: {e}")
            return
        if self.run_path and self.run_path.name == run_id:
            self._close_current_run()
        self.refresh_history()
        self._status(f"已删除记录目录 {run_id}", "ok")

    def open_settings(self, tab: int = 0) -> None:
        if self.worker and self.worker.isRunning():
            QMessageBox.information(self, "正在分析", "请等当前分析结束再改设置。")
            return
        dlg = SettingsDialog(self.settings, self)
        if 0 <= tab < dlg.tabs.count():
            dlg.tabs.setCurrentIndex(tab)
        if dlg.exec() == QDialog.Accepted:
            self.settings.save()
            self._show_info()
            self._status("设置已保存，下一次分析生效。", "ok")

    def open_history_item(self, item: QListWidgetItem) -> None:
        run_id = item.data(Qt.UserRole)
        if run_id:
            self.load_run(str(run_id))

    def load_run(self, run_id: str) -> None:
        if self.worker and self.worker.isRunning():
            QMessageBox.information(self, "正在分析", "请等当前分析结束再打开其它记录。")
            return
        run_path = RUNS_DIR / run_id
        if not run_path.is_dir():
            QMessageBox.warning(self, "打不开", f"找不到记录目录：\n{run_path}")
            self.refresh_history()
            return
        steps_doc = read_json(rp(run_path, STEPS_JSON)) or {}
        frames_doc = read_json(rp(run_path, FRAMES_JSON)) or {}
        notes_doc = read_json(rp(run_path, VISION_JSON)) or {}
        self.steps = [Step.from_dict(x) for x in (steps_doc.get("steps") or [])]
        self.frames = [Frame.from_dict(x) for x in (frames_doc.get("frames") or [])]
        self.notes = [VisionNote.from_dict(x) for x in (notes_doc.get("notes") or [])]
        self.transcript = Transcript.from_dict(read_json(rp(run_path, TRANSCRIPT_JSON)) or {})
        self.video = VideoInfo.from_dict(read_json(rp(run_path, VIDEO_JSON)) or {})
        self.run_path = run_path
        self.result = None
        self.video_path = Path(self.video.path) if self.video.path else None
        self.edit_filter.clear()
        self._reload_step_list()
        self._load_transcript_pane()
        self._load_report_pane()
        self._show_info()
        self.tabs_left.setCurrentWidget(self.page_steps)
        self.stack_right.setCurrentIndex(1)
        self.work.setCurrentWidget(self.detail)
        self.stagebar.setVisible(False)
        self._update_actions()
        lost = not (self.video.path and Path(self.video.path).exists())
        if not self.steps:
            self._status(f"已打开 {run_id}，但这条记录里没有步骤数据"
                         "（可能是旧版本，或那次分析失败了）。", "warn")
        elif lost:
            self._status(f"已打开历史记录 {run_id}（{len(self.steps)} 个步骤）—— "
                         "源视频已不在原位置：截图、编辑、导出都正常，"
                         "但 HTML 里的「跳到视频」会失效。", "warn")
        else:
            self._status(f"已打开历史记录 {run_id}（{len(self.steps)} 个步骤）", "ok")

    # ------------------------------------------------------------ 步骤列表
    def _current_step(self):
        item = self.list_steps.currentItem()
        return item.data(Qt.UserRole) if item else None

    def _on_step_selected(self, current, _previous=None) -> None:
        step = current.data(Qt.UserRole) if current else None
        self.show_step(step)

    def _reload_step_list(self) -> None:
        keyword = self.edit_filter.text().strip().lower()
        keep = self._current_step()
        self.list_steps.clear()
        shown = 0
        for s in self.steps:
            hay = f"{s.title} {s.action} {s.expected_result}".lower()
            if keyword and keyword not in hay:
                continue
            shown += 1
            label = f"{s.index}. {s.title}\n{_mmss(s.t_start)}–{_mmss(s.t_end)}"
            if s.needs_review:
                label += "   ⚠ 待核实"
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, s)
            item.setSizeHint(QSize(0, 50))
            if s.needs_review:
                item.setForeground(Qt.darkYellow)
            self.list_steps.addItem(item)
            if keep is not None and s is keep:
                self.list_steps.setCurrentItem(item)
        if shown == 0 and self.steps:
            info = QListWidgetItem("没有匹配的步骤")
            info.setFlags(Qt.NoItemFlags)
            self.list_steps.addItem(info)
        elif self.list_steps.currentRow() < 0 and self.list_steps.count():
            self.list_steps.setCurrentRow(0)

    def show_step(self, step) -> None:
        """详情区只写「做什么 / 结果 / 依据」，截图交给上方的预览区。"""
        if step is None:
            self.detail.clear()
            self.preview.clear()
            return
        s = step
        note_by_id = {n.id: n for n in self.notes}
        seg_by_id = {t.id: t for t in self.transcript.segments}

        html = [f"<style>{theme.detail_html_css()}</style>"]
        html.append(f"<h2>{s.index}. {_esc(s.title)}</h2>")
        html.append(f'<div class="time">{_mmss(s.t_start)} – {_mmss(s.t_end)}'
                    f"（{s.t_end - s.t_start:.1f} 秒）· 置信度 {s.confidence:.2f}</div>")
        if s.needs_review:
            html.append(f'<div class="warn">⚠️ 待核实：{_esc(s.review_note or "依据不足")}</div>')
        html.append('<p class="field"><span class="label">操作</span><br>'
                    + (_esc(s.action) or "（未填写）") + "</p>")
        if s.expected_result:
            html.append('<p class="field"><span class="label">预期结果</span><br>'
                        + _esc(s.expected_result) + "</p>")

        ev = []
        for tid in s.evidence.transcript_ids:
            seg = seg_by_id.get(tid)
            if seg:
                ev.append(f"字幕 {_mmss(seg.start)}「{_esc(seg.text)}」")
        for vid in s.evidence.vision_ids:
            n = note_by_id.get(vid)
            if n and n.ok:
                bits = [b for b in (n.interface, n.visible_text, n.activity)
                        if b and b != "无明显操作"]
                if bits:
                    text = short_visible("；".join(bits), max_items=8, max_chars=240)
                    ev.append("画面：" + _esc(text))
        html.append('<p class="ev"><span class="label">依据</span><br>'
                    + ("<br>".join(ev) if ev else "（无文字依据，仅截图）") + "</p>")
        self.detail.setHtml("".join(html))
        self._show_preview_for(s)

    def _show_preview_for(self, step: Step) -> None:
        """把这一步的截图交给预览区；同一屏的近似帧只留一张。"""
        frame_by_id = {f.id: f for f in self.frames}
        items: list[tuple[Path, str, str]] = []
        seen: list[float] = []
        for fid in step.frame_ids:
            f = frame_by_id.get(fid)
            if not f or not self.run_path:
                continue
            img = frame_abs(self.run_path, f.path)
            if not img.exists():
                continue
            if any(abs(f.time - t) < 1.5 for t in seen):
                continue
            seen.append(f.time)
            items.append((img, _mmss(f.time), f"步骤 {step.index} · {step.title}"))
        self.preview.set_images(items)

    def _open_image(self, path: str, caption: str) -> None:
        dlg = ImageViewer(path, caption, self)
        dlg.exec()
    def _load_transcript_pane(self) -> None:
        segs = self.transcript.segments
        if not segs:
            self.txt_transcript.setPlainText("（这段视频没有可用的字幕）")
            return
        lines = [f"[{_mmss(s.start)} → {_mmss(s.end)}] {s.text.strip()}" for s in segs]
        self.txt_transcript.setPlainText("\n".join(lines))

    def _load_report_pane(self, report=None) -> None:
        if not self.run_path:
            self.txt_log.clear()
            return
        if report is None:
            data = read_json(rp(self.run_path, REPORT_JSON))
            if not data:
                self.txt_log.setPlainText("（这次运行没有生成 run_report.json）")
                return
            report = RunReport.from_dict(data)
        lines = [f"状态：{report.status}",
                 f"开始：{report.started_at}",
                 f"结束：{report.finished_at}",
                 f"步骤：{report.steps_count}（待核实 {report.needs_review_count}）",
                 ""]
        lines.append("阶段：")
        for st in report.stages:
            lines.append(f"  [{st.get('status', '?')}] {st.get('name', '?')} "
                         f"{st.get('seconds', 0)}s  {st.get('detail', '')}")
        if report.warnings:
            lines.append("")
            lines.append(f"警告（{len(report.warnings)}）：")
            lines += ["  ! " + w for w in report.warnings]
        if report.errors:
            lines.append("")
            lines.append(f"错误（{len(report.errors)}）：")
            lines += ["  x " + e for e in report.errors]
        self.txt_log.setPlainText("\n".join(lines))

    # ------------------------------------------------------------ 编辑 / 删除
    def edit_step(self) -> None:
        step = self._current_step()
        if step is None or step not in self.steps:
            return
        dlg = StepEditDialog(step, self)
        if dlg.exec() != QDialog.Accepted:
            return
        dlg.apply()
        self._persist_steps()
        self._reload_step_list()
        self._show_info()
        self._status(f"步骤 {step.index} 已更新，手册已重新生成", "ok")

    def add_step(self) -> None:
        """手工补一个 AI 漏掉的步骤。

        新增的步骤默认标「待核实」并注明是人工补充：AI 没识别出来的操作，
        通常也没有对应的画面描述可引用，不能假装它是自动识别出来的。
        """
        if not self.run_path:
            return
        # 默认接在最后一步之后；没有步骤就从 0 开始
        if self.steps:
            last = max(self.steps, key=lambda s: s.t_end)
            start = float(last.t_end)
        else:
            start = 0.0
        step = Step(
            index=len(self.steps) + 1,
            t_start=start,
            t_end=start + 5.0,
            title="新步骤",
            action="",
            expected_result="",
            frame_ids=[],
            evidence=StepEvidence(),
            needs_review=True,
            confidence=0.5,
            review_note="人工补充：AI 没有识别出这一步，请核对后填写内容",
        )
        dlg = StepEditDialog(step, self)
        dlg.setWindowTitle("新增步骤")
        if dlg.exec() != QDialog.Accepted:
            return
        dlg.apply()
        self.steps.append(step)
        self.steps.sort(key=lambda s: (s.t_start, s.index))
        for i, st in enumerate(self.steps, 1):
            st.index = i
        self._persist_steps()
        self._reload_step_list()
        self._show_info()
        self._update_actions()
        self._status(f"已新增步骤，共 {len(self.steps)} 步；手册已重新生成", "ok")

    def delete_step(self) -> None:
        step = self._current_step()
        if step is None or step not in self.steps:
            return
        answer = QMessageBox.question(
            self, "删除步骤",
            f"确定删除「{step.index}. {step.title}」吗？\n删除后手册会立即重新生成。",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer != QMessageBox.Yes:
            return
        row = self.steps.index(step)
        del self.steps[row]
        for i, st in enumerate(self.steps, 1):
            st.index = i
        self._persist_steps()
        self._reload_step_list()
        if self.list_steps.count() and not self.list_steps.currentItem():
            self.list_steps.setCurrentRow(0)
        self._show_info()
        self._update_actions()
        self._status(f"已删除，剩余 {len(self.steps)} 个步骤；手册已重新生成", "ok")

    def _persist_steps(self) -> None:
        """把当前步骤写回 steps.json，并同步重新生成 md / html。

        写盘时打一个 edited_at 标记：以后重新分析这段视频时，程序能据此判断
        "这份 steps.json 是人改过的"，先备份再覆盖，不会静默丢掉手工修改。
        """
        if not self.run_path:
            return
        write_json(self.run_path / STEPS_JSON, {
            "steps": [s.to_dict() for s in self.steps],
            "edited_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        })
        self._rerender_files()

    def rerender(self) -> None:
        if not self.run_path or not self.steps:
            return
        self._persist_steps()
        self._update_actions()
        self._status("手册已重新生成", "ok")

    def _rerender_files(self) -> None:
        if not self.run_path:
            return
        try:
            from ..store import OUTPUT_DIR
            base = self.run_path / OUTPUT_DIR
            if not base.is_dir():
                base = self.run_path
            rel = os.path.relpath(self.video.path, str(base)) if self.video.path else ""
            rel = Path(rel).as_posix() if rel else ""
        except Exception:
            rel = ""
        write_text((self.run_path / GUIDE_MD), 
            render_markdown(self.steps, self.frames, self.notes, self.transcript,
                            self.video, self.run_path, rel), encoding="utf-8")
        write_text((self.run_path / GUIDE_HTML), 
            render_html(self.steps, self.frames, self.notes, self.transcript,
                        self.video, rel), encoding="utf-8")

    # ------------------------------------------------------------ 导出
    def _guide_html_path(self) -> Path | None:
        if not self.run_path:
            return None
        p = rp(self.run_path, GUIDE_HTML)          # 兼容旧的平铺布局
        return p if p.exists() else None

    def open_guide_html(self) -> None:
        p = self._guide_html_path()
        if not p:
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(p.resolve())))

    def export_guide(self) -> None:
        if not self.run_path or not self.steps:
            return
        blockers = export_blockers(self.steps)
        if blockers:
            QMessageBox.warning(self, "还不能导出",
                                "以下步骤没有任何证据，请先补充或删除：\n\n"
                                + "\n".join(blockers))
            return
        video = read_json(rp(self.run_path, VIDEO_JSON)) or {}
        video_path = Path(video.get("path") or "")
        size_hint = ""
        if video_path.exists():
            mb = video_path.stat().st_size / (1024 * 1024)
            size_hint = f"（源视频 {mb:.0f} MB）"
        options = [
            "轻量手册（推荐）：只有 HTML、Markdown 和截图，不含视频",
            f"完整手册包：额外复制源视频{size_hint}，换电脑也能直接播放",
        ]
        default_index = 1 if getattr(self.settings, "export_mode_default", "light") == "full" else 0
        choice, ok = QInputDialog.getItem(
            self, "导出手册", "选择导出方式：", options, default_index, False)
        if not ok:
            return
        mode = MODE_FULL if choice.startswith("完整") else MODE_LIGHT

        names = self._ask_export_names(mode)
        if names is None:
            return
        target, video_dir, batch_dir, name = names
        self._rerender_files()
        res = export_manual(self.run_path, target, mode, settings=self.settings,
                            video_dir=video_dir, batch_dir=batch_dir, name=name)
        if not res.ok:
            detail = res.error
            if res.missing:
                detail += "\n\n" + "\n".join(res.missing[:10])
            QMessageBox.critical(self, "导出失败", detail)
            self._status(f"导出失败：{res.error.splitlines()[0]}", "fail")
            return
        self._status(f"已导出到 {res.target}", "ok")
        notes = ""
        if res.warnings:
            notes = "\n\n" + "\n".join(res.warnings)
        entry = next((f for f in res.files if f.endswith(".html")), "")
        answer = QMessageBox.question(
            self, "导出完成",
            f"手册已导出到：\n{res.target}\n\n要现在打开 {entry or "手册"} 看看吗？{notes}",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
        if answer == QMessageBox.Yes and entry:
            QDesktopServices.openUrl(
                QUrl.fromLocalFile(str((res.target / entry).resolve())))

    def _ask_export_names(self, mode: str):
        """导出前的命名对话框：选根目录 + 视频文件夹 / 批次 / 手册名。

        普通用户不需要懂占位符：输入框里预填的是按设置模板算好的名字，
        直接改字就行，改的只对这一次生效。
        """
        from ..naming import plan as plan_names

        video = read_json(rp(self.run_path, VIDEO_JSON)) or {}
        stem = Path(video.get("path") or "").stem or self.run_path.name
        default_title = stem
        names = plan_names(self.settings, stem, self.run_path.name, default_title)

        dlg = QDialog(self)
        dlg.setWindowTitle("导出手册")
        form = QFormLayout(dlg)
        form.setContentsMargins(16, 16, 16, 12)

        configured = (getattr(self.settings, "export_root", "") or "").strip()
        default_root = (Path(configured) if configured
                        else Path.home() / "Documents" / "Video to Guide" / "Exports")
        edit_root = QLineEdit(str(default_root))
        btn_browse = QPushButton("浏览…")
        row_root = QHBoxLayout()
        row_root.addWidget(edit_root, 1)
        row_root.addWidget(btn_browse)

        def browse():
            d = QFileDialog.getExistingDirectory(dlg, "选择导出根目录", edit_root.text())
            if d:
                edit_root.setText(d)

        btn_browse.clicked.connect(browse)
        form.addRow("导出根目录", row_root)

        edit_video = QLineEdit(names.video_dir)
        edit_batch = QLineEdit(names.batch_dir)
        edit_name = QLineEdit(names.html_name[:-5])
        form.addRow("视频文件夹", edit_video)
        form.addRow("批次名称", edit_batch)
        form.addRow("手册名称", edit_name)

        lbl_preview = QLabel("")
        lbl_preview.setWordWrap(True)
        lbl_preview.setStyleSheet(f"color:{theme.TEXT_DIM};font-size:12px;")
        form.addRow("预览", lbl_preview)

        def refresh(*_):
            from ..naming import plan as p2
            n = p2(self.settings, stem, self.run_path.name, default_title,
                   video_dir=edit_video.text(), batch_dir=edit_batch.text(),
                   name=edit_name.text())
            root = Path(edit_root.text() or ".")
            parts = [n.video_dir, n.batch_dir, n.html_name]
            shown = root / Path(*[p for p in parts if p])
            lbl_preview.setText(str(shown))

        for w in (edit_video, edit_batch, edit_name, edit_root):
            w.textChanged.connect(refresh)
        refresh()

        box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        box.button(QDialogButtonBox.Ok).setText("导出")
        box.button(QDialogButtonBox.Cancel).setText("取消")
        box.accepted.connect(dlg.accept)
        box.rejected.connect(dlg.reject)
        form.addRow(box)

        if dlg.exec() != QDialog.Accepted:
            return None
        return (edit_root.text().strip() or str(default_root),
                edit_video.text().strip(), edit_batch.text().strip(),
                edit_name.text().strip())

    def open_output_dir(self) -> None:
        if self.run_path and self.run_path.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.run_path.resolve())))


    # ------------------------------------------------------------ 本地模型指引
    def show_local_model_help(self) -> None:
        """一页以内的本地模型配置说明。

        刻意不写具体模型名：模型版本几个月就换代，写死了很快变成误导。
        这里只讲"通用要求 + 三种常见服务的启动方式 + 在本软件里怎么选"。
        """
        html = (
            f"<style>{theme.detail_html_css()}</style>"
            "<h2>如何配置本地模型（免费）</h2>"
            '<p class="ev">软件本身不带模型。模型由你在本机运行的服务提供，'
            "任何兼容 OpenAI 接口的服务都可以。</p>"
            '<p class="field"><span class="label">第一步：装一个本机服务（任选其一）</span><br>'
            "· <b>LM Studio</b>：装好后在「Developer」页点 Start Server，默认端口 12345<br>"
            "· <b>Ollama</b>：装好后执行 <code>ollama serve</code>，默认端口 11434<br>"
            "· <b>llama.cpp</b>：<code>llama-server -m 模型文件 --port 8080</code><br>"
            "· <b>vLLM / 其他</b>：只要是 OpenAI 兼容接口，端口随意</p>"
            '<p class="field"><span class="label">第二步：加载一个能看图的模型</span><br>'
            "做识图的那个模型必须<b>支持图像输入</b>（模型名里常带 vl / vision / multimodal）。"
            "纯文本模型能用来整理步骤，但看不了截图。<br>"
            "显存参考：7B 级 4-bit 量化约 6–8 GB；13B 约 10–12 GB；"
            "更大的模型需要更多显存，也可以用 CPU 跑但会很慢。</p>"
            '<p class="field"><span class="label">第三步：在本软件里选模型</span><br>'
            "打开「工具 → 设置 → 模型配置」，服务商选「本机服务（免费 · OpenAI 兼容）」，"
            "地址填上一步的端口，点「获取模型」，把列表里的模型分别设为"
            "「视觉模型」和「步骤模型」。</p>"
            '<p class="ev">说明：以上模型名与显存数字只是量级参考，'
            "请按你机器上实际加载的模型选择。</p>"
        )
        dlg = QDialog(self)
        dlg.setWindowTitle("如何配置本地模型")
        dlg.resize(720, 620)
        lay = QVBoxLayout(dlg)
        view = QTextBrowser()
        view.setHtml(html)
        lay.addWidget(view)
        row = QHBoxLayout()
        btn_settings = QPushButton("打开设置…")
        btn_settings.clicked.connect(lambda: (dlg.accept(), self.open_settings()))
        btn_close = QPushButton("知道了")
        btn_close.clicked.connect(dlg.accept)
        row.addStretch(1)
        row.addWidget(btn_settings)
        row.addWidget(btn_close)
        lay.addLayout(row)
        dlg.exec()

    # ------------------------------------------------------------ 帮助
    def show_usage(self) -> None:
        QMessageBox.information(
            self, "使用说明",
            "1. 点「选择视频…」挑一段软件操作录屏（mp4 / mkv / mov 等）。\n"
            "2. 点「开始分析」，等进度条走完。全程在本机处理，不上传视频。\n"
            "3. 左栏看步骤；带 ⚠ 的是 AI 不确定的，双击可修改，按 Delete 可删除。\n"
            "4. 点「导出手册…」把 guide.md / guide.html 和截图复制到你选的目录。\n"
            "5. 之前的分析都在「历史记录」页，双击就能重新打开。\n\n"
            "需要本地模型服务在运行（llama-server --port 12345），详见 docs/usage.md。")

    def show_about(self) -> None:
        QMessageBox.about(
            self, "关于 Video to Guide",
            "Video to Guide 0.1.0\n\n"
            "把软件操作录屏转换成带截图、时间戳和操作说明的图文手册。\n"
            "全部处理在本机完成：内置抽帧 + 内置语音识别 + 本地视觉模型。\n"
            "唯一的外部依赖是一个 OpenAI 兼容的模型服务。")

    def closeEvent(self, event) -> None:
        """关闭窗口时确保后台线程停掉，不留下孤儿进程。"""
        if self.worker and self.worker.isRunning():
            self.worker.cancel()
            # 正常情况下取消标志会在阶段边界生效、线程很快返回；
            # 但如果正卡在一次模型请求里（最长要等 vision_timeout），wait 会超时。
            # 超时后必须强制结束线程，否则进程会带着一个还在跑的后台线程退不干净。
            if not self.worker.wait(3000):
                self.worker.terminate()
                self.worker.wait(2000)
        self.worker = None
        event.accept()
