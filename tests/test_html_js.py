# -*- coding: utf-8 -*-
"""生成的 HTML 里那段 JS 必须是合法语法。

为什么要专门测：改 JS 时少写一个 + 号，Python 不报错、pytest 也不报错，
但浏览器整段脚本直接不执行——用户看到的是"点目录没反应"。
这种错误只能靠真解析一次来发现。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest

from video_to_guide.models import (Frame, Step, StepEvidence, Transcript,
                                   TranscriptSegment, VideoInfo)
from video_to_guide.render.html import render_html


def make_run():
    frames = [Frame(id=f"f{i}", time=float(i * 10), end=float(i * 10 + 8),
                    path=f"frames/f{i}.jpg", width=1280, height=800,
                    screen_index=i, change_score=0.5, kind="local")
              for i in range(3)]
    steps = [Step(index=i + 1, t_start=float(i * 10), t_end=float(i * 10 + 8),
                  title=f"步骤{i + 1}", action="点击「保存」", expected_result="弹出提示",
                  frame_ids=[f"f{i}"],
                  evidence=StepEvidence(transcript_ids=[], vision_ids=[]),
                  needs_review=False, confidence=0.9)
             for i in range(3)]
    transcript = Transcript(language="zh", backend="srt",
                            segments=[TranscriptSegment(id="t0", start=1.0, end=5.0, text="你好")])
    video = VideoInfo(path="C:/videos/demo.mp4", duration=40.0, width=1280, height=800,
                      fps=30, has_audio=True)
    return steps, frames, [], transcript, video


def _scripts(html: str) -> list[str]:
    return re.findall(r"<script>(.*?)</script>", html, re.S)


@pytest.mark.parametrize("video_rel", ["", "media/demo.mp4"])
def test_embedded_js_parses(video_rel):
    """完整包与轻量包的 JS 都要能通过语法解析。"""
    node = shutil.which("node")
    if not node:
        pytest.skip("没有 node，跳过 JS 语法检查")
    steps, frames, notes, transcript, video = make_run()
    html = render_html(steps, frames, notes, transcript, video, video_rel, "测试")
    for i, code in enumerate(_scripts(html)):
        proc = subprocess.run([node, "-e", "new Function(process.argv[1])", code],
                              capture_output=True, timeout=60)
        assert proc.returncode == 0, (
            f"第 {i + 1} 段脚本语法错误：{proc.stderr.decode('utf-8', 'replace')[:400]}")


def test_both_layouts_have_switch_and_input():
    """两类手册都要留"换一个视频"的入口——源视频被移走时用户得有条出路。"""
    steps, frames, notes, transcript, video = make_run()
    for rel in ("", "media/demo.mp4"):
        html = render_html(steps, frames, notes, transcript, video, rel, "测试")
        assert 'id="video-file"' in html
        assert "换一个视频" in html
