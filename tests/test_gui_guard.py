# -*- coding: utf-8 -*-
"""重新分析前的三选一逻辑。

规则改过一次：以前只有"手册被手工改过"才弹窗；现在只要这个任务已经分析出过步骤
就弹窗——因为步骤生成本身不完全可复现，同一视频两次可能差一个数量级，
不提示的话用户会莫名其妙发现手册变了。
"""
import json
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from video_to_guide.gui import main_window as mw


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


class FakeBox:
    """替身对话框：addButton 返回带文字的假按钮，clickedButton 按测试设定返回。"""
    pick_text = ""
    last = None
    Question = 4
    AcceptRole = 0
    DestructiveRole = 2
    RejectRole = 1

    def __init__(self, *a, **k):
        self.buttons = []
        FakeBox.last = self

    def setIcon(self, *a): pass
    def setWindowTitle(self, *a): pass
    def setText(self, *a): pass
    def setInformativeText(self, *a): pass
    def setDefaultButton(self, *a): pass
    def exec(self): return 0

    def addButton(self, text, role):
        b = type("B", (), {"text": text})()
        self.buttons.append(b)
        return b

    def clickedButton(self):
        for b in self.buttons:
            if FakeBox.pick_text and FakeBox.pick_text in b.text:
                return b
        return None


@pytest.fixture
def window(app, monkeypatch):
    monkeypatch.setattr(mw, "QMessageBox", FakeBox)
    FakeBox.pick_text = ""
    FakeBox.last = None
    w = mw.MainWindow()
    yield w
    w.close()


def make_run(tmp_path: Path, edited: bool, steps=True) -> Path:
    d = tmp_path / "run-e"
    d.mkdir(exist_ok=True)
    doc = {"steps": ([{"index": 1, "title": "x"}] if steps else [])}
    if edited:
        doc["edited_at"] = "2026-11-01 00:40:00"
    (d / "steps.json").write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return d


def test_no_steps_returns_new_without_asking(window, tmp_path):
    """还没分析出步骤（新任务/空结果）→ 不用问。"""
    window.run_path = make_run(tmp_path, edited=False, steps=False)
    assert window._ask_edited_steps() == "new"
    assert FakeBox.last is None


def test_analyzed_run_asks_even_without_edit(window, tmp_path):
    """只要分析出过步骤，重新分析就要提示，哪怕没手改过。"""
    window.run_path = make_run(tmp_path, edited=False)
    FakeBox.pick_text = "保留现在的结果"
    assert window._ask_edited_steps() == "new"
    assert FakeBox.last is not None


def test_keep_result_returns_new(window, tmp_path):
    window.run_path = make_run(tmp_path, edited=True)
    FakeBox.pick_text = "保留现在的结果"
    assert window._ask_edited_steps() == "new"


def test_overwrite_returns_overwrite(window, tmp_path):
    window.run_path = make_run(tmp_path, edited=True)
    FakeBox.pick_text = "备份现在的结果后重新分析"
    assert window._ask_edited_steps() == "overwrite"


def test_cancel_returns_cancel(window, tmp_path):
    window.run_path = make_run(tmp_path, edited=True)
    FakeBox.pick_text = "取消"
    assert window._ask_edited_steps() == "cancel"
