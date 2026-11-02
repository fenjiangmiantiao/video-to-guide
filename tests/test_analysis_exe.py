# -*- coding: utf-8 -*-
"""打包版找 analysis-video 的路径探测。"""
import sys
from pathlib import Path

from video_to_guide.config import Settings
from video_to_guide.parse.frames import analysis_exe


def make_exe(base: Path, name="analysis-video.exe") -> Path:
    p = base / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"stub")
    return p


def test_prefers_config(tmp_path, monkeypatch):
    s = Settings.load()
    target = make_exe(tmp_path / "custom")
    s.analysis_video_exe = str(target)
    assert analysis_exe(s) == target


def test_finds_sibling_of_executable(tmp_path, monkeypatch):
    s = Settings.load()
    s.analysis_video_exe = ""
    exe = make_exe(tmp_path)
    monkeypatch.setattr(sys, "executable", str(exe))
    assert analysis_exe(s) == exe


def test_finds_venv_two_levels_up(tmp_path, monkeypatch):
    """打包版放在 dist/VideoToGuide/，venv 在项目根 —— 往上两级要能找到。"""
    s = Settings.load()
    s.analysis_video_exe = ""
    proj = tmp_path / "proj"
    packaged = proj / "dist" / "VideoToGuide"
    packaged.mkdir(parents=True)
    monkeypatch.setattr(sys, "executable", str(packaged / "VideoToGuide.exe"))
    venv_exe = make_exe(proj / ".venv" / "Scripts")
    assert analysis_exe(s) == venv_exe


def test_returns_candidate_when_nothing_found(tmp_path, monkeypatch):
    s = Settings.load()
    s.analysis_video_exe = ""
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setattr(sys, "executable", str(empty / "VideoToGuide.exe"))
    monkeypatch.setattr("shutil.which", lambda *a, **k: None)
    result = analysis_exe(s)
    assert result.name == "analysis-video.exe"
    assert not result.exists()
