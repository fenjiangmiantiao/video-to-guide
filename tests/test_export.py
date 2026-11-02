# -*- coding: utf-8 -*-
"""导出模块测试：轻量/完整两种模式、同名不覆盖、缺图拦截、中文路径。"""
import json
from pathlib import Path

import pytest

from video_to_guide.export import MODE_FULL, MODE_LIGHT, export_manual


def make_run(tmp_path: Path, *, with_video=True, with_frame=True, name="run1") -> Path:
    d = tmp_path / name
    (d / "frames").mkdir(parents=True)
    video = tmp_path / "demo.mp4"
    if with_video:
        video.write_bytes(b"\x00" * 2048)
    frame_file = d / "frames" / "scene_000.jpg"
    if with_frame:
        frame_file.write_bytes(b"\xff\xd8\xff\xd9")     # 最小 JPEG 头尾

    (d / "video.json").write_text(json.dumps({
        "path": str(video), "duration": 60.0, "width": 1280, "height": 720,
        "fps": 15.0, "has_audio": True, "audio_codec": "aac", "audio_channels": 2,
    }, ensure_ascii=False), encoding="utf-8")
    (d / "frames.json").write_text(json.dumps({"frames": [{
        "id": "f1", "time": 10.0, "end": 20.0, "path": "frames/scene_000.jpg",
        "width": 1280, "height": 720, "screen_index": 0, "change_score": 0.5,
        "transcript_ids": ["t1"],
    }]}, ensure_ascii=False), encoding="utf-8")
    (d / "transcript.json").write_text(json.dumps({"language": "zh", "backend": "srt", "segments": [
        {"id": "t1", "start": 10.0, "end": 20.0, "text": "点击设置按钮"},
    ]}, ensure_ascii=False), encoding="utf-8")
    (d / "vision.json").write_text(json.dumps({"notes": [{
        "id": "v1", "frame_id": "f1", "interface": "设置对话框", "visible_text": "设置",
        "activity": "设置对话框已打开", "raw": "", "error": "", "elapsed": 1.0,
    }]}, ensure_ascii=False), encoding="utf-8")
    (d / "steps.json").write_text(json.dumps({"steps": [{
        "index": 1, "t_start": 10.0, "t_end": 20.0, "title": "打开设置",
        "action": "点击「设置」按钮", "expected_result": "弹出设置对话框",
        "frame_ids": ["f1"], "evidence": {"transcript_ids": ["t1"], "vision_ids": ["v1"]},
        "needs_review": False, "confidence": 0.9, "review_note": "",
    }]}, ensure_ascii=False), encoding="utf-8")
    return d


def test_light_export_structure(tmp_path):
    run = make_run(tmp_path)
    out = tmp_path / "out"
    res = export_manual(run, out, MODE_LIGHT)
    assert res.ok, res.error
    names = set(res.files)
    html_name = next(n for n in names if n.endswith(".html"))
    # 默认按"视频/批次/标题.html"摆放，文件名不再是固定的 guide.html
    assert {html_name, "steps.json", "manifest.json", "frames"} <= names
    assert any(n.endswith(".md") for n in names)
    assert "media" not in names                      # 轻量模式不复制视频
    assert (res.target / "frames" / "scene_000.jpg").exists()

    manifest = json.loads((res.target / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["mode"] == "light"
    assert manifest["has_video"] is False
    assert manifest["video_file"] == ""
    assert manifest["frames"] == ["frames/scene_000.jpg"]
    assert "api_key" not in json.dumps(manifest).lower()

    html = (res.target / manifest["entry_html"]).read_text(encoding="utf-8")
    assert "选择本地视频" in html
    assert 'id="video-file"' in html
    assert "createObjectURL" in html


def test_full_export_copies_video(tmp_path):
    run = make_run(tmp_path)
    res = export_manual(run, tmp_path / "out", MODE_FULL)
    assert res.ok, res.error
    assert (res.target / "media" / "demo.mp4").exists()
    manifest = json.loads((res.target / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["has_video"] is True
    assert manifest["video_file"] == "media/demo.mp4"
    html = (res.target / manifest["entry_html"]).read_text(encoding="utf-8")
    assert 'src="media/demo.mp4"' in html
    # 完整包也要留"换一个视频"入口：源视频被移动或编码不支持时，
    # 用户得有条出路，不能只看到黑屏。
    assert 'id="video-file"' in html
    assert "换一个视频" in html


def test_no_overwrite_on_repeat(tmp_path):
    run = make_run(tmp_path)
    out = tmp_path / "out"
    a = export_manual(run, out, MODE_LIGHT)
    b = export_manual(run, out, MODE_LIGHT)
    assert a.ok and b.ok
    assert a.target != b.target
    assert b.target.name.endswith("-2")
    # 第一次导出的内容还在
    assert a.target.is_dir() and any(p.suffix == ".html" for p in a.target.iterdir())


def test_missing_frame_blocks_export(tmp_path):
    run = make_run(tmp_path, with_frame=False)
    res = export_manual(run, tmp_path / "out", MODE_LIGHT)
    assert not res.ok
    assert res.missing
    assert "截图缺失" in res.error
    # 失败时不留半成品目录
    leftovers = [p for p in (tmp_path / "out").glob("guide_*")] if (tmp_path / "out").exists() else []
    assert leftovers == []


def test_full_requires_video(tmp_path):
    run = make_run(tmp_path, with_video=False)
    res = export_manual(run, tmp_path / "out", MODE_FULL)
    assert not res.ok
    assert "源视频" in res.error


def test_chinese_path(tmp_path):
    run = make_run(tmp_path, name="演示任务")
    out = tmp_path / "导出目录" / "子目录"
    res = export_manual(run, out, MODE_FULL)
    assert res.ok, res.error
    # 中文根目录必须能正常写入；分组目录用视频文件名（demo），批次用时间
    assert "导出目录" in str(res.target) and res.target.is_dir()
    assert (res.target / "media" / "demo.mp4").exists()
    manifest = json.loads((res.target / "manifest.json").read_text(encoding="utf-8"))
    html = (res.target / manifest["entry_html"]).read_text(encoding="utf-8")
    assert "media/demo.mp4" in html


def test_missing_run_dir(tmp_path):
    res = export_manual(tmp_path / "nope", tmp_path / "out", MODE_LIGHT)
    assert not res.ok
    assert "运行目录不存在" in res.error
