"""解析层测试：SRT 解析、时间对齐、ffprobe 容错。"""

from pathlib import Path

import pytest

from video_to_guide.models import Frame, Transcript, TranscriptSegment, VisionNote
from video_to_guide.parse.align import build_timeline
from video_to_guide.parse.probe import ProbeError, probe
from video_to_guide.parse.transcribe import parse_srt


def test_parse_srt_basic(tmp_path: Path):
    srt = tmp_path / "a.srt"
    srt.write_text(
        "1\n00:00:00,000 --> 00:00:04,500\n打开设置\n\n"
        "2\n00:00:04,500 --> 00:00:08,000\n修改主题\n",
        encoding="utf-8",
    )
    segs = parse_srt(srt)
    assert len(segs) == 2
    assert segs[0].id == "t0" and segs[0].start == 0.0 and segs[0].end == 4.5
    assert segs[1].text == "修改主题"


def test_parse_srt_handles_bom_and_crlf(tmp_path: Path):
    srt = tmp_path / "b.srt"
    srt.write_bytes(b"\xef\xbb\xbf1\r\n00:00:01,000 --> 00:00:02,000\r\n\xe4\xbd\xa0\xe5\xa5\xbd\r\n")
    segs = parse_srt(srt)
    assert len(segs) == 1
    assert segs[0].text == "你好"


def test_parse_srt_skips_garbage(tmp_path: Path):
    srt = tmp_path / "c.srt"
    srt.write_text("这不是字幕\n\n1\n00:00:01,000 --> 00:00:02,000\n有效\n", encoding="utf-8")
    assert [s.text for s in parse_srt(srt)] == ["有效"]


def _frames():
    return [
        Frame(id="f0", time=0.0, end=10.0, path="frames/a.jpg"),
        Frame(id="f1", time=10.0, end=20.0, path="frames/b.jpg"),
    ]


def test_align_assigns_segments_to_screens():
    frames = _frames()
    tr = Transcript(segments=[
        TranscriptSegment(id="t0", start=1.0, end=3.0, text="第一屏的话"),
        TranscriptSegment(id="t1", start=9.0, end=12.0, text="跨屏的话"),
    ])
    notes = [VisionNote(id="v0", frame_id="f0", interface="主界面"),
             VisionNote(id="v1", frame_id="f1", interface="设置窗口")]
    tl = build_timeline(frames, tr, notes)

    assert tl["screen_count"] == 2
    assert tl["screens"][0]["transcript_ids"] == ["t0", "t1"]   # 跨屏字幕两边都挂
    assert tl["screens"][1]["transcript_ids"] == ["t1"]
    assert tl["screens"][0]["vision_ids"] == ["v0"]
    assert tl["screens"][1]["interface"] == "设置窗口"
    assert tl["screens"][0]["end"] == 10.0


def test_align_drops_failed_notes():
    frames = _frames()
    notes = [VisionNote(id="v0", frame_id="f0", error="超时")]
    tl = build_timeline(frames, Transcript(), notes)
    assert tl["screens"][0]["vision_ids"] == []


def test_probe_missing_file(tmp_path: Path):
    with pytest.raises(ProbeError):
        probe(tmp_path / "不存在.mp4")


def test_probe_empty_file(tmp_path: Path):
    f = tmp_path / "empty.mp4"
    f.write_bytes(b"")
    with pytest.raises(ProbeError):
        probe(f)


def test_probe_garbage_file(tmp_path: Path):
    f = tmp_path / "bad.mp4"
    f.write_bytes(b"not a video at all" * 100)
    with pytest.raises(ProbeError):
        probe(f)
