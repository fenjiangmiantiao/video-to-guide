# -*- coding: utf-8 -*-
"""R7-03b 数据安全补强：缓存身份、编辑保护、取消状态。"""
import json
import time
from pathlib import Path

import pytest

from video_to_guide.config import Settings
from video_to_guide.parse import frames as frames_mod
from video_to_guide.parse import transcribe as transcribe_mod
from video_to_guide.pipeline import EditedStepsError, _backup_edited_steps
from video_to_guide.models import RunReport
from video_to_guide.store import video_fingerprint

from conftest import store_helpers, work


# ---------------------------------------------------------------- 转录缓存

def make_transcript_cache(run: Path, meta: dict | None, segments=True):
    doc = {"language": "zh", "backend": "whisper-ctranslate2",
           "segments": [{"id": "t1", "start": 0.0, "end": 1.0, "text": "你好"}] if segments else []}
    if meta is not None:
        doc["meta"] = meta
    store_helpers(run)
    work(run, "transcript.json").write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")


def test_transcribe_cache_hit_when_meta_matches(tmp_path, monkeypatch):
    s = Settings.load()
    video = tmp_path / "v.mp4"
    video.write_bytes(b"VIDEO-CONTENT")
    meta = {"model": s.whisper_model, "language": s.whisper_language,
            "compute_type": s.whisper_compute_type,
            "video": video_fingerprint(video)}
    make_transcript_cache(tmp_path, meta)

    def boom(*a, **k):
        raise AssertionError("不该真的去跑 whisper")
    monkeypatch.setattr(transcribe_mod, "_run_whisper", boom)
    tr = transcribe_mod.transcribe(video, tmp_path, s)
    assert len(tr.segments) == 1


def test_transcribe_cache_miss_when_video_content_changed(tmp_path, monkeypatch):
    """路径和大小都没变，但文件内容换了 —— 字幕必须重转。"""
    s = Settings.load()
    s.whisper_backend = "cli"          # 这个用例测的是缓存判断，固定走 CLI 路径便于替身
    video = tmp_path / "v.mp4"
    video.write_bytes(b"OLD-CONTENT-12345678")
    meta = {"model": s.whisper_model, "language": s.whisper_language,
            "compute_type": s.whisper_compute_type,
            "video": video_fingerprint(video)}
    make_transcript_cache(tmp_path, meta)
    video.write_bytes(b"NEW-CONTENT-12345678")      # 同样 20 字节，内容不同
    called = {}

    def fake(v, srt, settings, lang):
        called["hit"] = True
        raise transcribe_mod.TranscribeError("停在这里即可")
    monkeypatch.setattr(transcribe_mod, "_run_whisper", fake)
    with pytest.raises(transcribe_mod.TranscribeError):
        transcribe_mod.transcribe(video, tmp_path, s)
    assert called.get("hit") is True


def test_video_fingerprint_distinguishes_content(tmp_path):
    a = tmp_path / "a.mp4"
    b = tmp_path / "b.mp4"
    a.write_bytes(b"A" * 4096)
    b.write_bytes(b"B" * 4096)
    assert video_fingerprint(a)["sha256"] != video_fingerprint(b)["sha256"]
    assert video_fingerprint(a)["size"] == video_fingerprint(b)["size"]


def test_transcribe_cache_miss_when_model_changed(tmp_path, monkeypatch):
    s = Settings.load()
    s.whisper_backend = "cli"
    old = {"model": "tiny", "language": s.whisper_language,
           "compute_type": s.whisper_compute_type}
    make_transcript_cache(tmp_path, old)
    called = {}

    def fake(video, srt, settings, lang):
        called["hit"] = True
        raise transcribe_mod.TranscribeError("停在这里即可")
    monkeypatch.setattr(transcribe_mod, "_run_whisper", fake)
    with pytest.raises(transcribe_mod.TranscribeError):
        transcribe_mod.transcribe(tmp_path / "v.mp4", tmp_path, s)
    assert called.get("hit") is True


def test_transcribe_cache_miss_without_meta(tmp_path, monkeypatch):
    s = Settings.load()
    s.whisper_backend = "cli"
    make_transcript_cache(tmp_path, None)
    called = {}

    def fake(video, srt, settings, lang):
        called["hit"] = True
        raise transcribe_mod.TranscribeError("停在这里即可")
    monkeypatch.setattr(transcribe_mod, "_run_whisper", fake)
    with pytest.raises(transcribe_mod.TranscribeError):
        transcribe_mod.transcribe(tmp_path / "v.mp4", tmp_path, s)
    assert called.get("hit") is True


# ---------------------------------------------------------------- 抽帧缓存

def test_frames_cache_miss_when_video_changed(tmp_path, monkeypatch):
    s = Settings.load()
    s.frames_backend = "external"      # 这个用例专门测外部抽帧的缓存
    (tmp_path / "video.json").write_text(json.dumps({"path": "old.mp4", "duration": 10.0}),
                                         encoding="utf-8")
    (tmp_path / "frames.json").write_text(json.dumps({
        "meta": {"video": str((tmp_path / "old.mp4").resolve()), "duration": 10.0,
                 "max_frames": s.max_frames, "max_frames_auto": s.max_frames_auto,
                 "max_frames_floor": s.max_frames_floor, "max_frames_cap": s.max_frames_cap,
                 "read_long_edge": s.read_long_edge},
        "frames": [{"id": "f0", "time": 1.0, "end": 2.0, "path": "frames/a.jpg",
                    "width": 1, "height": 1, "screen_index": 0, "change_score": 0.1,
                    "transcript_ids": []}],
    }, ensure_ascii=False), encoding="utf-8")
    called = {}

    def fake(*a, **k):
        called["hit"] = True
        raise frames_mod.FramesError("停在这里即可")
    monkeypatch.setattr(frames_mod, "_run_analysis_video", fake)
    with pytest.raises(frames_mod.FramesError):
        frames_mod.extract_frames(tmp_path / "new.mp4", tmp_path, s)
    assert called.get("hit") is True


# ---------------------------------------------------------------- 编辑保护

def test_backup_edited_steps(tmp_path):
    doc = {"steps": [{"index": 1, "title": "人工改过"}], "edited_at": "2026-11-01 00:40:00"}
    store_helpers(tmp_path)
    work(tmp_path, "steps.json").write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    report = RunReport(run_id="x", video_path="v.mp4")
    _backup_edited_steps(tmp_path, report)
    backups = list((tmp_path / ".work").glob("steps.edited-*.json"))
    assert len(backups) == 1
    assert json.loads(backups[0].read_text(encoding="utf-8"))["steps"][0]["title"] == "人工改过"
    assert any("手工修改过" in w for w in report.warnings)


def test_no_backup_when_not_edited(tmp_path):
    store_helpers(tmp_path)
    work(tmp_path, "steps.json").write_text(json.dumps({"steps": []}), encoding="utf-8")
    report = RunReport(run_id="x", video_path="v.mp4")
    _backup_edited_steps(tmp_path, report)
    assert list((tmp_path / ".work").glob("steps.edited-*.json")) == []
    assert report.warnings == []


def test_analyze_refuses_to_overwrite_edited(tmp_path, monkeypatch):
    """analyze 在碰到手工修改过的 steps.json 时，默认直接拒绝。"""
    import video_to_guide.store as store
    from video_to_guide import pipeline

    run = tmp_path / "run-x"
    store_helpers(run)
    work(run, "steps.json").write_text(json.dumps({"steps": [], "edited_at": "2026-11-01 00:40:00"}),
                                       encoding="utf-8")
    monkeypatch.setattr(store, "RUNS_DIR", tmp_path)
    monkeypatch.setattr(pipeline, "prepare", lambda run_id: run)

    with pytest.raises(EditedStepsError):
        pipeline.analyze(tmp_path / "v.mp4", Settings.load(), run_id="run-x")


# ---------------------------------------------------------------- 取消状态

def test_worker_marks_cancelled(tmp_path, monkeypatch):
    import video_to_guide.store as store
    from video_to_guide.gui import worker as worker_mod

    run = tmp_path / "run-c"
    store_helpers(run)
    work(run, "run_report.json").write_text(json.dumps({"run_id": "run-c", "status": "running"}),
                                            encoding="utf-8")
    monkeypatch.setattr(store, "RUNS_DIR", tmp_path)
    w = worker_mod.AnalyzeWorker(tmp_path / "v.mp4", Settings.load(), run_id="run-c")
    w._mark_cancelled()
    data = json.loads(work(run, "run_report.json").read_text(encoding="utf-8"))
    assert data["status"] == "cancelled"
    assert data.get("cancelled_at")
