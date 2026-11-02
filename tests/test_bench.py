# -*- coding: utf-8 -*-
"""bench 模块测试：用构造数据验证评测逻辑，不依赖真实视频和模型。"""
import json
from pathlib import Path

import pytest

from video_to_guide import bench


def make_run(tmp_path: Path, steps, segments=None, notes=None, frames=None,
             duration=100.0, frame_files=True) -> Path:
    d = tmp_path / "run"
    d.mkdir(parents=True, exist_ok=True)
    (d / "steps.json").write_text(json.dumps({"steps": steps}, ensure_ascii=False), encoding="utf-8")
    (d / "transcript.json").write_text(
        json.dumps({"segments": segments or []}, ensure_ascii=False), encoding="utf-8")
    (d / "vision.json").write_text(json.dumps({"notes": notes or []}, ensure_ascii=False), encoding="utf-8")
    frame_list = frames or []
    (d / "frames.json").write_text(json.dumps({"frames": frame_list}, ensure_ascii=False), encoding="utf-8")
    (d / "video.json").write_text(json.dumps({"path": "v.mp4", "duration": duration}), encoding="utf-8")
    if frame_files:
        (d / "frames").mkdir(exist_ok=True)
        for f in frame_list:
            (d / f["path"]).parent.mkdir(parents=True, exist_ok=True)
            (d / f["path"]).write_bytes(b"x")
    return d


def labels(actions, video="v.mp4"):
    return {"video": video, "duration": 100.0, "expected_actions": actions}


def test_all_covered(tmp_path):
    steps = [
        {"index": 1, "t_start": 10.0, "t_end": 20.0, "title": "打开设置", "action": "点击设置按钮",
         "expected_result": "设置窗口出现", "frame_ids": ["f1"],
         "evidence": {"transcript_ids": ["t1"], "vision_ids": ["v1"]}, "needs_review": False, "confidence": 0.9},
        {"index": 2, "t_start": 40.0, "t_end": 50.0, "title": "点击保存", "action": "点保存按钮",
         "expected_result": "提示已保存", "frame_ids": ["f2"],
         "evidence": {"transcript_ids": ["t2"], "vision_ids": ["v2"]}, "needs_review": False, "confidence": 0.9},
    ]
    d = make_run(tmp_path, steps,
                 segments=[{"id": "t1"}, {"id": "t2"}],
                 notes=[{"id": "v1"}, {"id": "v2"}],
                 frames=[{"id": "f1", "path": "frames/a.jpg"}, {"id": "f2", "path": "frames/b.jpg"}])
    lab = labels([{"id": "A1", "title": "打开设置", "t": 10.0, "tol": 5, "keywords": ["设置"]},
                  {"id": "A2", "title": "保存", "t": 41.0, "tol": 5, "keywords": ["保存"]}])
    rep = bench.evaluate_run(d, lab)
    assert rep.coverage_rate == 1.0
    assert rep.invalid_refs == []
    assert rep.unsupported == []
    assert rep.extra_steps == []
    # 标注时刻落在步骤区间内 → 区间偏差 0；起点偏差单独记录
    assert rep.max_time_error == 0.0
    assert rep.actions[1].start_offset == pytest.approx(-1.0)


def test_time_miss_when_text_found_far_away(tmp_path):
    steps = [{"index": 1, "t_start": 80.0, "t_end": 90.0, "title": "打开设置",
              "action": "", "expected_result": "", "frame_ids": [],
              "evidence": {"transcript_ids": [], "vision_ids": []}, "needs_review": True, "confidence": 0.6}]
    d = make_run(tmp_path, steps)
    rep = bench.evaluate_run(d, labels([{"id": "A1", "title": "打开设置", "t": 10.0, "tol": 5,
                                         "keywords": ["设置"]}]))
    a = rep.actions[0]
    assert a.status == "time_miss"
    assert a.time_error == 70.0
    assert "时间区间" in a.detail


def test_text_miss_when_time_matches_but_words_differ(tmp_path):
    steps = [{"index": 1, "t_start": 10.0, "t_end": 20.0, "title": "调整外观",
              "action": "", "expected_result": "", "frame_ids": [],
              "evidence": {"transcript_ids": ["t1"], "vision_ids": []}, "needs_review": False, "confidence": 0.9}]
    d = make_run(tmp_path, steps, segments=[{"id": "t1"}])
    rep = bench.evaluate_run(d, labels([{"id": "A1", "title": "改主题", "t": 10.0, "tol": 5,
                                         "keywords": ["主题"]}]))
    assert rep.actions[0].status == "text_miss"
    assert "主题" in rep.actions[0].detail


def test_missing_action(tmp_path):
    steps = [{"index": 1, "t_start": 10.0, "t_end": 20.0, "title": "打开设置",
              "action": "", "expected_result": "", "frame_ids": [],
              "evidence": {"transcript_ids": [], "vision_ids": []}, "needs_review": True, "confidence": 0.5}]
    d = make_run(tmp_path, steps)
    rep = bench.evaluate_run(d, labels([{"id": "A1", "title": "打开设置", "t": 10.0, "tol": 5,
                                         "keywords": ["设置"]},
                                        {"id": "A2", "title": "点击保存", "t": 60.0, "tol": 5,
                                         "keywords": ["保存"]}]))
    assert rep.actions[1].status == "missing"
    assert rep.coverage_rate == 0.5


def test_unsupported_and_invalid_refs(tmp_path):
    steps = [
        {"index": 1, "t_start": 10.0, "t_end": 20.0, "title": "打开设置", "action": "",
         "expected_result": "", "frame_ids": ["fX"],
         "evidence": {"transcript_ids": [], "vision_ids": []}, "needs_review": False, "confidence": 0.9},
        {"index": 2, "t_start": 40.0, "t_end": 50.0, "title": "点击保存", "action": "",
         "expected_result": "", "frame_ids": [],
         "evidence": {"transcript_ids": ["t9"], "vision_ids": ["v9"]}, "needs_review": False, "confidence": 0.5},
    ]
    d = make_run(tmp_path, steps, segments=[{"id": "t1"}], notes=[{"id": "v1"}])
    rep = bench.evaluate_run(d, labels([]))
    assert len(rep.unsupported) == 1 and rep.unsupported[0]["index"] == 1
    kinds = {r["kind"] for r in rep.invalid_refs}
    assert "frame" in kinds and "transcript" in kinds and "vision" in kinds
    assert len(rep.unmarked_low_conf) == 1 and rep.unmarked_low_conf[0]["index"] == 2


def test_missing_frame_file(tmp_path):
    steps = [{"index": 1, "t_start": 10.0, "t_end": 20.0, "title": "x", "action": "",
              "expected_result": "", "frame_ids": ["f1"],
              "evidence": {"transcript_ids": [], "vision_ids": []}, "needs_review": True, "confidence": 0.5}]
    d = make_run(tmp_path, steps, frames=[{"id": "f1", "path": "frames/gone.jpg"}], frame_files=False)
    rep = bench.evaluate_run(d, labels([]))
    assert any(r["kind"] == "frame_file" for r in rep.invalid_refs)


def test_time_gaps(tmp_path):
    steps = [{"index": 1, "t_start": 5.0, "t_end": 15.0, "title": "a", "action": "", "expected_result": "",
              "frame_ids": [], "evidence": {"transcript_ids": [], "vision_ids": []},
              "needs_review": True, "confidence": 0.5},
             {"index": 2, "t_start": 70.0, "t_end": 80.0, "title": "b", "action": "", "expected_result": "",
              "frame_ids": [], "evidence": {"transcript_ids": [], "vision_ids": []},
              "needs_review": True, "confidence": 0.5}]
    d = make_run(tmp_path, steps, duration=110.0)
    rep = bench.evaluate_run(d, labels([]))
    spans = [(g["from"], g["to"]) for g in rep.time_gaps]
    assert (15.0, 70.0) in spans
    assert (80.0, 110.0) in spans


def test_markdown_and_files(tmp_path):
    steps = [{"index": 1, "t_start": 10.0, "t_end": 20.0, "title": "打开设置", "action": "",
              "expected_result": "", "frame_ids": [], "evidence": {"transcript_ids": [], "vision_ids": []},
              "needs_review": False, "confidence": 0.9}]
    d = make_run(tmp_path, steps)
    lab = labels([{"id": "A1", "title": "打开设置", "t": 10.0, "tol": 5, "keywords": ["设置"]}])
    rep = bench.evaluate_run(d, lab)
    md = bench.to_markdown(rep, lab)
    assert "# 手册质量评测" in md
    assert "独立事件召回" in md
    assert "步骤精确率" in md
    assert "需要人工复核" in md

    lp = tmp_path / "labels.json"
    lp.write_text(json.dumps(lab, ensure_ascii=False), encoding="utf-8")
    jp, mp = bench.evaluate_to_files(d, lp, tmp_path / "out")
    assert jp.exists() and mp.exists()
    assert json.loads(jp.read_text(encoding="utf-8"))["coverage_rate"] == 1.0
