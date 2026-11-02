# -*- coding: utf-8 -*-
"""步骤生成里「相邻屏变化」的渲染（P2 时序整合层）。

只比较已有视觉文字，不额外调用模型。这层要保证两件事：
1. 有变化时给出可读的一行，让模型看出"这一屏跟前一屏不一样"；
2. 没有变化时不要硬凑一行，否则时间线变长、模型反而更难抓重点。
"""
from video_to_guide.models import Frame, Transcript, VisionNote
from video_to_guide.steps.generate import _delta_line, _render_timeline


def _note(i, scene, act, text=""):
    return VisionNote(id=f"v{i}", frame_id=f"f{i}", interface=scene,
                      activity=act, visible_text=text)


def test_delta_line_empty_when_no_prev():
    assert _delta_line(None, _note(1, "厨房台面", "切菜")) == ""


def test_delta_line_reports_scene_and_action_change():
    line = _delta_line(_note(0, "厨房台面", "切番茄"), _note(1, "灶台", "倒油"))
    assert "场景" in line and "→" in line
    assert "动作" in line


def test_delta_line_empty_when_identical():
    a = _note(0, "厨房台面", "切番茄")
    b = _note(1, "厨房台面", "切番茄")
    assert _delta_line(a, b) == ""


def test_render_timeline_diff_only_when_enabled():
    frames = [Frame(id="f0", time=0.0, end=5.0, path="a.jpg"),
              Frame(id="f1", time=5.0, end=10.0, path="b.jpg")]
    notes = [_note(0, "厨房台面", "切番茄"), _note(1, "灶台", "倒油")]
    timeline = {"screens": [
        {"frame_id": "f0", "start": 0.0, "end": 5.0,
         "transcript_ids": [], "vision_ids": ["v0"]},
        {"frame_id": "f1", "start": 5.0, "end": 10.0,
         "transcript_ids": [], "vision_ids": ["v1"]},
    ]}
    tr = Transcript(segments=[])
    plain = _render_timeline(timeline, tr, notes, frames)
    diff = _render_timeline(timeline, tr, notes, frames, with_diff=True)
    assert "变化：" not in plain
    assert diff.count("变化：") == 1        # 只有第二屏有"上一屏"可比
    assert len(diff.splitlines()) == len(plain.splitlines()) + 1


# --- 批次合并去重（同一段操作被两批各写一次） ---

from video_to_guide.models import Step
from video_to_guide.steps.generate import _merge_batches


def _step(i, title, t0, t1):
    return Step(index=i, t_start=t0, t_end=t1, title=title, action="x" * 20)


def test_merge_dedups_synonym_titles_at_same_time():
    """实测踩过的坑：同一秒出现「加入番茄酱」和「加番茄酱」两条。

    后者不是前者的连续子串（中间隔了"入"），旧的子串判断漏掉了。
    """
    out = _merge_batches([_step(1, "加入番茄酱", 209.3, 214.9),
                          _step(2, "加番茄酱", 209.3, 214.9)], 300)
    assert len(out) == 1


def test_merge_keeps_different_actions_at_same_time():
    out = _merge_batches([_step(1, "倒入蛋液", 10, 20),
                          _step(2, "倒入食用油", 10, 20)], 300)
    assert len(out) == 2


def test_merge_keeps_similar_titles_at_different_time():
    """时间不重叠就是两件先后发生的事，哪怕用词很像也不能合并。"""
    out = _merge_batches([_step(1, "打开设置", 10, 20),
                          _step(2, "打开导出设置", 22, 30)], 300)
    assert len(out) == 2


def test_merge_dedups_substring_titles():
    out = _merge_batches([_step(1, "点击保存", 10, 20),
                          _step(2, "点击保存按钮", 10, 20)], 300)
    assert len(out) == 1


def test_merge_renumbers_after_dedup():
    out = _merge_batches([_step(1, "加入番茄酱", 10, 20),
                          _step(2, "加番茄酱", 10, 20),
                          _step(3, "切番茄", 30, 40)], 300)
    assert [s.index for s in out] == [1, 2]


# --- 无语音判定（纯画面演示视频） ---

from video_to_guide.models import TranscriptSegment
from video_to_guide.steps.generate import _has_speech


def _tr(texts):
    return Transcript(segments=[TranscriptSegment(id=f"t{i}", start=i, end=i + 1, text=t)
                                for i, t in enumerate(texts)])


def test_no_speech_when_only_punctuation():
    """实测：纯演示视频转录出 1 段「....」，段数不为 0 但等于没语音。"""
    assert not _has_speech(_tr(["...."]))
    assert not _has_speech(_tr([]))


def test_has_speech_when_real_words():
    assert _has_speech(_tr(["先把番茄划十字", "然后烫一下"]))


def test_no_speech_when_too_few_chars():
    assert not _has_speech(_tr(["嗯", "啊"]))
