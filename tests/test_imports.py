"""冒烟测试：确保每个模块都能被导入。

为什么需要这个：单元测试只导入被测模块，像 frames.py 这种
"没有专门测试但被 pipeline 依赖"的文件，写错一个字符（比如把换行写进字符串里）
单元测试照样全绿，但程序一启动就崩。
这个测试专门防这类"整体不可用"的低级错误。
"""

import importlib

import pytest


MODULES = [
    "video_to_guide",
    "video_to_guide.cli",
    "video_to_guide.config",
    "video_to_guide.models",
    "video_to_guide.pipeline",
    "video_to_guide.store",
    "video_to_guide.parse.align",
    "video_to_guide.parse.frames",
    "video_to_guide.parse.probe",
    "video_to_guide.parse.transcribe",
    "video_to_guide.render",
    "video_to_guide.render.html",
    "video_to_guide.render.markdown",
    "video_to_guide.steps.generate",
    "video_to_guide.steps.validate",
    "video_to_guide.vision.describe",
    "video_to_guide.vision.lms_client",
]


@pytest.mark.parametrize("name", MODULES)
def test_module_imports(name):
    importlib.import_module(name)


def test_gui_modules_import():
    """界面模块单独测：缺 PySide6 时跳过，而不是让整套测试失败。"""
    pytest.importorskip("PySide6")
    importlib.import_module("video_to_guide.gui.worker")
    importlib.import_module("video_to_guide.gui.main_window")
    importlib.import_module("video_to_guide.gui.app")


def test_placeholder_srt_is_valid(tmp_path):
    """占位字幕必须能被 analysis-video 接受（它要求至少 5 条）。"""
    from video_to_guide.parse.frames import _write_placeholder_srt

    path = _write_placeholder_srt(tmp_path, 120.0)
    text = path.read_text(encoding="utf-8")
    assert path.exists()
    assert text.count("-->") == 5
    assert "00:00:00,000" in text


def test_placeholder_srt_handles_short_video(tmp_path):
    from video_to_guide.parse.frames import _write_placeholder_srt

    path = _write_placeholder_srt(tmp_path, 1.5)
    text = path.read_text(encoding="utf-8")
    assert text.count("-->") == 5
