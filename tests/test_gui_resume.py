# -*- coding: utf-8 -*-
"""断点续跑的界面逻辑：同一个视频 + 没跑完的任务 → 复用；否则新建。"""
import json
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from video_to_guide.config import Settings
from video_to_guide.gui.main_window import MainWindow


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(app, tmp_path, monkeypatch):
    import video_to_guide.store as store
    monkeypatch.setattr(store, "RUNS_DIR", tmp_path)
    w = MainWindow()
    yield w
    w.close()


def make_run(tmp_path: Path, name: str, video: Path, status: str, ok_notes=2, bad_notes=1):
    d = tmp_path / name
    d.mkdir(parents=True)
    (d / "video.json").write_text(json.dumps({"path": str(video)}), encoding="utf-8")
    (d / "run_report.json").write_text(json.dumps({"status": status}), encoding="utf-8")
    notes = [{"id": f"v{i}", "frame_id": f"f{i}", "interface": "x", "visible_text": "", "activity": ""}
             for i in range(ok_notes)]
    notes += [{"id": f"v{ok_notes + i}", "frame_id": f"f{ok_notes + i}", "error": "失败"}
              for i in range(bad_notes)]
    (d / "vision.json").write_text(json.dumps({"notes": notes}, ensure_ascii=False), encoding="utf-8")
    return d


def test_resume_when_partial_and_same_video(window, tmp_path):
    video = tmp_path / "a.mp4"
    video.write_bytes(b"x")
    d = make_run(tmp_path, "run-a", video, "partial")
    window.run_path = d
    window.video_path = video
    assert window._resume_run_id() == "run-a"
    assert "已完成的 2 张截图描述会被复用" in window.txt_log.toPlainText()


def test_no_resume_when_finished(window, tmp_path):
    video = tmp_path / "b.mp4"
    video.write_bytes(b"x")
    d = make_run(tmp_path, "run-b", video, "ok")
    window.run_path = d
    window.video_path = video
    assert window._resume_run_id() is None


def test_no_resume_when_video_changed(window, tmp_path):
    old = tmp_path / "old.mp4"
    new = tmp_path / "new.mp4"
    old.write_bytes(b"x")
    new.write_bytes(b"y")
    d = make_run(tmp_path, "run-c", old, "failed")
    window.run_path = d
    window.video_path = new
    assert window._resume_run_id() is None


def test_no_resume_without_run(window, tmp_path):
    window.run_path = None
    window.video_path = tmp_path / "whatever.mp4"
    assert window._resume_run_id() is None
