# -*- coding: utf-8 -*-
"""启动步骤补齐规则：只在证据充分、画面变化明显、且没被覆盖时补一步。"""
from video_to_guide.models import Frame, Step, StepEvidence, VisionNote
from video_to_guide.steps.generate import _ensure_launch_step


def frame(fid, t, end, score=0.9):
    return Frame(id=fid, time=t, end=end, path=f"frames/{fid}.jpg", width=800, height=600,
                 screen_index=0, change_score=score, transcript_ids=[])


def note(fid, interface="", activity="", visible="", err=""):
    return VisionNote(id=f"v_{fid}", frame_id=fid, interface=interface,
                      visible_text=visible, activity=activity, raw="", error=err, elapsed=1.0)


def step(t_start, t_end, title="已有步骤"):
    return Step(index=1, t_start=t_start, t_end=t_end, title=title, action="", expected_result="",
                frame_ids=[], evidence=StepEvidence(), needs_review=False, confidence=0.9)


def test_adds_launch_step_when_evidence_is_clear():
    frames = [frame("f0", 0.5, 2.0), frame("f1", 3.8, 7.5)]
    notes = [
        note("f0", interface="开发工具窗口", activity="鼠标悬停，无明显操作"),
        note("f1", interface="记事本", activity="屏幕上打开了空的记事本窗口，光标停在编辑区等待输入",
             visible=""),
    ]
    steps = [step(17.0, 19.0, "输入三行文字")]
    got = _ensure_launch_step(steps, frames, notes)
    assert got is not None
    assert got.title == "打开记事本"
    assert got.t_start == 3.8
    assert got.frame_ids == ["f1"]
    assert got.evidence.vision_ids == ["v_f1"]
    assert got.needs_review is False


def test_skips_when_frame_already_covered():
    frames = [frame("f1", 3.8, 7.5)]
    notes = [note("f1", activity="屏幕上打开了空的记事本窗口")]
    steps = [step(2.0, 9.0, "打开并输入")]
    assert _ensure_launch_step(steps, frames, notes) is None


def test_skips_when_change_score_too_low():
    frames = [frame("f1", 3.8, 7.5, score=0.2)]
    notes = [note("f1", activity="屏幕上打开了空的记事本窗口")]
    steps = [step(17.0, 19.0)]
    assert _ensure_launch_step(steps, frames, notes) is None


def test_skips_when_no_launch_wording():
    frames = [frame("f1", 3.8, 7.5)]
    notes = [note("f1", interface="示例笔记软件主窗口", activity="无明显操作，画面停留")]
    steps = [step(17.0, 19.0)]
    assert _ensure_launch_step(steps, frames, notes) is None


def test_skips_when_note_failed():
    frames = [frame("f1", 3.8, 7.5)]
    notes = [note("f1", activity="屏幕上打开了空的记事本窗口", err="模型服务不可用")]
    steps = [step(17.0, 19.0)]
    assert _ensure_launch_step(steps, frames, notes) is None


def test_ignores_frames_after_first_four():
    frames = [frame(f"f{i}", i * 10.0, i * 10.0 + 2) for i in range(6)]
    notes = [note(f"f{i}", activity="无") for i in range(5)]
    notes.append(note("f5", activity="屏幕上打开了空的记事本窗口"))
    steps = [step(0.0, 1.0)]
    assert _ensure_launch_step(steps, frames, notes) is None
