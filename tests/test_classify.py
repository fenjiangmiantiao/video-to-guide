# -*- coding: utf-8 -*-
"""视频教学分类器：只测解析逻辑（不调模型）。"""

from video_to_guide.steps.classify import _parse, _sample_shots, _sample_transcript
from video_to_guide.models import Transcript, TranscriptSegment, VisionNote


def _tr(texts):
    return Transcript(segments=[TranscriptSegment(id=f"t{i}", start=i, end=i + 1, text=t)
                                for i, t in enumerate(texts)])


def test_parse_yes():
    assert _parse("结论：教学\n理由：按步骤演示") is True


def test_parse_no():
    assert _parse("结论：不是教学\n理由：吃播") is False


def test_parse_no_is_not_confused_by_word_jiaoxue():
    # 「不是教学」里也含「教学」，必须优先判「不是」
    assert _parse("结论：不是教学") is False
    assert _parse("这不是教学视频") is False


def test_parse_unknown():
    assert _parse("") is None
    assert _parse("我觉得还可以吧") is None


def test_sample_transcript_handles_empty():
    assert "没有语音" in _sample_transcript(_tr([]))


def test_sample_shots_handles_empty():
    assert "没有画面描述" in _sample_shots([])


def test_sample_shots_skips_failed_notes():
    good = VisionNote(id="v1", frame_id="f1", interface="厨房", activity="切菜")
    bad = VisionNote(id="v2", frame_id="f2", error="超时")
    out = _sample_shots([bad, good])
    assert "厨房" in out and "超时" not in out