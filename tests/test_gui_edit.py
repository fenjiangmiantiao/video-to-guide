# -*- coding: utf-8 -*-
"""人工审核闭环：新增步骤、改时间、导出拦截规则。"""
import json
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QDialog

from video_to_guide.models import Step, StepEvidence
from video_to_guide.steps.validate import export_blockers

from conftest import store_helpers, work


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_export_blocks_step_without_evidence():
    s = Step(index=1, t_start=0, t_end=5, title="x", action="", expected_result="",
             frame_ids=[], evidence=StepEvidence(), needs_review=False, confidence=0.9)
    problems = export_blockers([s])
    assert len(problems) == 1
    assert "没有任何证据" in problems[0]


def test_export_allows_review_marked_step():
    """人工补充的步骤没有证据，但标了待核实，应该允许导出。"""
    s = Step(index=1, t_start=0, t_end=5, title="人工补充", action="", expected_result="",
             frame_ids=[], evidence=StepEvidence(), needs_review=True, confidence=0.5)
    assert export_blockers([s]) == []


def test_add_step_appends_and_marks_review(app, tmp_path, monkeypatch):
    import video_to_guide.store as store
    from video_to_guide.gui import main_window as mw

    monkeypatch.setattr(store, "RUNS_DIR", tmp_path)
    run = tmp_path / "run-add"
    store_helpers(run)
    work(run, "steps.json").write_text(json.dumps({"steps": []}), encoding="utf-8")
    work(run, "video.json").write_text(json.dumps({"path": "v.mp4", "duration": 60.0}), encoding="utf-8")

    w = mw.MainWindow()
    try:
        w.run_path = run
        w.steps = [Step(index=1, t_start=0, t_end=10, title="原步骤", action="a",
                        expected_result="b", frame_ids=[], evidence=StepEvidence(),
                        needs_review=False, confidence=0.9)]

        # 替身对话框：直接接受并把内容改成"手工补的一步"
        class FakeDialog:
            def __init__(self, step, parent=None):
                self.step = step
            def setWindowTitle(self, *a): pass
            def exec(self): return QDialog.Accepted
            def apply(self):
                self.step.title = "手工补的一步"
                self.step.action = "点了保存按钮"
                self.step.t_start = 12.0
                self.step.t_end = 18.0
                return self.step

        monkeypatch.setattr(mw, "StepEditDialog", FakeDialog)
        w.add_step()

        assert len(w.steps) == 2
        assert w.steps[1].title == "手工补的一步"
        assert w.steps[1].needs_review is True
        assert "人工补充" in w.steps[1].review_note
        # 写盘且带 edited_at
        doc = json.loads((run / ".work" / "steps.json").read_text(encoding="utf-8"))
        assert len(doc["steps"]) == 2
        assert doc.get("edited_at")
    finally:
        w.close()


def test_add_step_sorts_by_time(app, tmp_path, monkeypatch):
    import video_to_guide.store as store
    from video_to_guide.gui import main_window as mw

    monkeypatch.setattr(store, "RUNS_DIR", tmp_path)
    run = tmp_path / "run-sort"
    store_helpers(run)
    work(run, "steps.json").write_text(json.dumps({"steps": []}), encoding="utf-8")

    w = mw.MainWindow()
    try:
        w.run_path = run
        w.steps = [
            Step(index=1, t_start=0, t_end=10, title="第一步", action="", expected_result="",
                 frame_ids=[], evidence=StepEvidence(), needs_review=False, confidence=0.9),
            Step(index=2, t_start=30, t_end=40, title="第三步", action="", expected_result="",
                 frame_ids=[], evidence=StepEvidence(), needs_review=False, confidence=0.9),
        ]

        class FakeDialog:
            def __init__(self, step, parent=None):
                self.step = step
            def setWindowTitle(self, *a): pass
            def exec(self): return QDialog.Accepted
            def apply(self):
                self.step.title = "中间补的一步"
                self.step.t_start = 15.0
                self.step.t_end = 20.0
                return self.step

        monkeypatch.setattr(mw, "StepEditDialog", FakeDialog)
        w.add_step()
        titles = [s.title for s in w.steps]
        assert titles == ["第一步", "中间补的一步", "第三步"]
        assert [s.index for s in w.steps] == [1, 2, 3]
    finally:
        w.close()


def test_edit_dialog_updates_time(app):
    from video_to_guide.gui.main_window import StepEditDialog

    s = Step(index=1, t_start=5.0, t_end=10.0, title="t", action="", expected_result="",
             frame_ids=[], evidence=StepEvidence(), needs_review=False, confidence=0.9)
    dlg = StepEditDialog(s)
    try:
        assert dlg.sp_start.value() == 5.0
        dlg.sp_start.setValue(7.5)
        dlg.sp_end.setValue(3.0)          # 故意把结束改到开始之前
        dlg.apply()
        assert s.t_start == 7.5
        assert s.t_end == 7.5             # 收敛成零长度，不产生负区间
    finally:
        dlg.close()
