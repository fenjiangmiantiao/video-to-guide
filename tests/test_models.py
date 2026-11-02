"""数据结构测试：保证写盘再读回来不丢字段。"""

from video_to_guide.models import (Frame, RunReport, Step, StepEvidence, Transcript,
                                   TranscriptSegment, VideoInfo, VisionNote)


def test_video_info_roundtrip():
    v = VideoInfo(path="a.mp4", duration=12.5, width=1920, height=1080, fps=30.0,
                  has_audio=True, audio_codec="aac", audio_channels=2,
                  audio_sample_rate=44100, size_bytes=1024)
    assert VideoInfo.from_dict(v.to_dict()) == v


def test_transcript_roundtrip_and_lookup():
    t = Transcript(language="zh", backend="whisper", segments=[
        TranscriptSegment(id="t0", start=0.0, end=4.0, text="打开设置"),
        TranscriptSegment(id="t1", start=4.0, end=8.0, text="修改主题"),
    ])
    back = Transcript.from_dict(t.to_dict())
    assert back.segments[1].text == "修改主题"
    assert "打开设置" in back.text_between(0.0, 5.0)
    assert "修改主题" in back.text_between(3.0, 6.0)


def test_step_roundtrip():
    s = Step(index=1, t_start=1.0, t_end=3.0, title="打开设置", action="点击设置按钮",
             expected_result="弹出窗口", frame_ids=["f0"],
             evidence=StepEvidence(transcript_ids=["t0"], vision_ids=["v0"]),
             needs_review=True, confidence=0.8, review_note="依据不足")
    back = Step.from_dict(s.to_dict())
    assert back.to_dict() == s.to_dict()


def test_evidence_empty():
    assert StepEvidence().empty
    assert not StepEvidence(transcript_ids=["t0"]).empty


def test_vision_note_ok_flag():
    assert VisionNote(id="v0", frame_id="f0", interface="窗口").ok
    assert not VisionNote(id="v0", frame_id="f0", error="超时").ok


def test_run_report_stage():
    r = RunReport(run_id="r1", video_path="a.mp4")
    r.add_stage("probe", "ok", "done", 1.234)
    assert r.stages[0]["seconds"] == 1.23
    assert RunReport.from_dict(r.to_dict()).run_id == "r1"
