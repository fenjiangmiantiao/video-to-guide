"""渲染测试：Markdown 和 HTML 必须包含步骤、截图、时间、待核实标记。"""

from pathlib import Path

from video_to_guide.models import (Frame, Step, StepEvidence, Transcript,
                                   TranscriptSegment, VideoInfo, VisionNote)
from video_to_guide.render import fmt_time
from video_to_guide.render.html import render_html
from video_to_guide.render.markdown import render_markdown


def _data():
    video = VideoInfo(path="C:/v/演示.mp4", duration=125.0, width=1240, height=860, fps=15.0)
    frames = [Frame(id="f0", time=20.0, end=40.0, path="frames/s0.jpg")]
    notes = [VisionNote(id="v0", frame_id="f0", interface="设置窗口",
                        visible_text="「保存」", activity="按钮被点击")]
    tr = Transcript(segments=[TranscriptSegment(id="t0", start=20.0, end=25.0, text="点击保存")])
    steps = [
        Step(index=1, t_start=20.0, t_end=40.0, title="点击保存", action="点击「保存」按钮",
             expected_result="提示设置已保存", frame_ids=["f0"],
             evidence=StepEvidence(transcript_ids=["t0"], vision_ids=["v0"]),
             confidence=0.9),
        Step(index=2, t_start=95.0, t_end=110.0, title="确认生效", action="查看状态栏",
             frame_ids=["f0"], evidence=StepEvidence(transcript_ids=["t0"]),
             needs_review=True, review_note="画面依据不足"),
    ]
    return steps, frames, notes, tr, video


def test_fmt_time():
    assert fmt_time(0) == "00:00"
    assert fmt_time(65) == "01:05"
    assert fmt_time(3725) == "1:02:05"
    assert fmt_time(-3) == "00:00"


def test_markdown_contains_key_parts():
    steps, frames, notes, tr, video = _data()
    md = render_markdown(steps, frames, notes, tr, video, Path("."), video_rel="../../v.mp4")
    assert "# " in md and "操作手册" in md
    assert "## 步骤 1 · 点击保存" in md
    assert "![步骤 1 截图](frames/s0.jpg)" in md
    assert "跳到视频 00:20" in md
    assert "⚠️ **待核实**" in md
    assert "字幕 00:20「点击保存」" in md
    assert "画面：设置窗口" in md


def test_markdown_without_video():
    steps, frames, notes, tr, video = _data()
    md = render_markdown(steps, frames, notes, tr, video, Path("."), video_rel="")
    assert "跳到视频" not in md


def test_html_structure():
    steps, frames, notes, tr, video = _data()
    html = render_html(steps, frames, notes, tr, video, video_rel="../../v.mp4")
    assert '<meta charset="utf-8">' in html
    assert '<video id="player"' in html
    assert "function seekTo" in html
    assert 'onclick="seekTo(20.00)"' in html
    assert 'src="frames/s0.jpg"' in html
    assert 'class="step review"' in html
    assert "待核实" in html
    assert html.count("</section>") == 2


def test_html_escapes_dangerous_text():
    steps, frames, notes, tr, video = _data()
    steps[0].title = '<script>alert(1)</script>'
    steps[0].action = '点 "确定" & 保存'
    html = render_html(steps, frames, notes, tr, video, video_rel="")
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html
    assert "&amp;" in html


def test_html_no_video_message():
    steps, frames, notes, tr, video = _data()
    html = render_html(steps, frames, notes, tr, video, video_rel="")
    assert 'class="player missing"' in html
    # 新手册结构：目录 + 卡片 + 依据折叠 + 放大遮罩
    assert 'class="toc"' in html
    assert 'class="evidence"' in html or "<details" in html
    assert 'id="lightbox"' in html
    assert "@media print" in html
