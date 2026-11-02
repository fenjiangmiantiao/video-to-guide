"""后台工作线程。

为什么必须用线程：分析过程要跑几分钟（转录、抽帧、模型推理），
如果在界面线程里做，窗口会"未响应"，用户以为卡死了。
Qt 的规矩是：耗时任务放 QThread，通过信号把进度送回界面线程。
"""

from __future__ import annotations

import time
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from ..config import Settings
from ..pipeline import analyze


class CancelRequested(Exception):
    """用户点了取消。"""


class AnalyzeWorker(QThread):
    """在后台跑完整分析流程。"""

    progress = Signal(str, str, object, str)   # (消息, 状态, 百分比或 None, 阶段 id 或 "")
    finished_ok = Signal(object)     # AnalyzeResult
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, video_path: Path, settings: Settings,
                 run_id: str | None = None, force: bool = False,
                 allow_overwrite_edits: bool = False, parent=None):
        super().__init__(parent)
        self.video_path = Path(video_path)
        self.settings = settings
        self.run_id = run_id
        self.force = force
        self.allow_overwrite_edits = allow_overwrite_edits
        self._stop = False

    def cancel(self) -> None:
        self._stop = True

    def _mark_cancelled(self) -> None:
        """把当前任务的报告状态改成 cancelled，已完成的检查点原样保留。"""
        if not self.run_id:
            return
        try:
            from ..store import REPORT_JSON, STEPS_JSON, read_json, rp, run_dir, write_json

            d = run_dir(self.run_id)
            report = read_json(rp(d, REPORT_JSON)) or {}
            if not report:
                report = {"run_id": self.run_id, "video_path": str(self.video_path)}
            report["status"] = "cancelled"
            report["cancelled_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            write_json(d / REPORT_JSON, report)
        except Exception:
            pass

    def run(self) -> None:
        def progress(msg: str, status: str, pct=None, stage=None) -> None:
            if self._stop:
                raise CancelRequested()
            self.progress.emit(msg, status, pct, stage or "")

        try:
            result = analyze(self.video_path, self.settings, run_id=self.run_id,
                             force=self.force, progress=progress,
                             allow_overwrite_edits=self.allow_overwrite_edits)
            if self._stop:
                self.cancelled.emit()
                return
            self.finished_ok.emit(result)
        except CancelRequested:
            # 取消也要留下痕迹：否则下次打开这个任务，报告里还是上一次的状态，
            # 恢复逻辑就分不清"跑完了"还是"被用户中途掐了"。
            self._mark_cancelled()
            self.cancelled.emit()
        except Exception as e:      # 兜底：任何未预期异常都要变成界面上的提示，而不是崩溃
            self.failed.emit(f"{type(e).__name__}: {e}")

class ComponentWorker(QThread):
    """在后台下载/安装可选组件。

    为什么必须独立线程：一个 100 MB 的下载加上解压、算哈希，几十秒到几分钟，
    放主线程界面就冻住了。用户还要能随时取消，所以这里用共享的取消标志，
    由工作线程在每个分块读取时检查。
    """

    progress = Signal(str, float, str)      # 阶段, 百分比(0-100), 说明
    finished_ok = Signal(str)               # 安装目录
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, module, portable: bool, app_root, archive=None, parent=None):
        super().__init__(parent)
        self.module = module
        self.portable = portable
        self.app_root = app_root
        self.archive = archive               # 非空 = 从本地文件安装
        self._stop = False

    def cancel(self) -> None:
        self._stop = True

    def run(self) -> None:
        from .. import download as dl

        def on_progress(p) -> None:
            self.progress.emit(p.phase, p.percent, p.text)

        def cancelled() -> bool:
            return self._stop

        try:
            if self.archive:
                out = dl.install_from_file(self.module, self.archive, self.portable,
                                           self.app_root, on_progress, cancelled)
            else:
                out = dl.install(self.module, self.portable, self.app_root,
                                 on_progress, cancelled)
            if self._stop:
                self.cancelled.emit()
                return
            self.finished_ok.emit(str(out))
        except dl.DownloadCancelled:
            self.cancelled.emit()
        except Exception as e:                # noqa: BLE001
            self.failed.emit(f"{type(e).__name__}: {e}")

