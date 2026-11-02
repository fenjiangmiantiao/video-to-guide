# -*- coding: utf-8 -*-
"""待核实状态一致性：数据、界面、手册三处口径必须一致。"""
from video_to_guide.models import Frame, Step, StepEvidence, Transcript, VisionNote
from video_to_guide.steps.validate import LOW_CONFIDENCE, validate_steps


def frame(fid="f1", t=5.0, end=8.0):
    return Frame(id=fid, time=t, end=end, path=f"frames/{fid}.jpg", width=800, height=600,
                 screen_index=0, change_score=0.9, transcript_ids=[])


def note(fid="f1"):
    return VisionNote(id="v1", frame_id=fid, interface="记事本", visible_text="文件",
                      activity="打开了空的记事本窗口", raw="", error="", elapsed=1.0)


def step(conf, needs=False, t_ids=(), v_ids=("v1",), frames=("f1",)):
    return Step(index=1, t_start=5.0, t_end=8.0, title="打开记事本", action="a",
                expected_result="b", frame_ids=list(frames),
                evidence=StepEvidence(transcript_ids=list(t_ids), vision_ids=list(v_ids)),
                needs_review=needs, confidence=conf, review_note="")


def test_low_confidence_gets_flagged_when_evidence_weak():
    """低置信度单独不算；配上"动作不可见"这类具体缺陷才要求复核。"""
    quiet = VisionNote(id="v1", frame_id="f1", interface="记事本",
                       visible_text="文件", activity="无明显操作",
                       raw="", error="", elapsed=1.0)
    steps, warns = validate_steps([step(0.60)], [frame()], Transcript(), [quiet], 20.0)
    assert steps[0].needs_review is True
    assert "action_not_visible" in steps[0].review_reasons


def test_confidence_above_threshold_not_flagged():
    steps, _ = validate_steps([step(0.85)], [frame()], Transcript(), [note()], 20.0)
    assert steps[0].needs_review is False


def test_threshold_matches_bench():
    from video_to_guide import bench
    assert LOW_CONFIDENCE == bench.LOW_CONFIDENCE


def test_no_vision_no_transcript_flagged():
    steps, _ = validate_steps([step(0.9, v_ids=())], [frame()], Transcript(), [], 20.0)
    assert steps[0].needs_review is True
    assert "只有截图" in steps[0].review_note
