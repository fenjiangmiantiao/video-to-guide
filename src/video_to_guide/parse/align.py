"""时间对齐：把字幕、截图、画面描述按时间轴串起来。

为什么要单独做这一步：
字幕说的是"做什么"，截图显示的是"当时屏幕长什么样"，两者时间并不严格同步
（人说话会比操作慢半拍）。先按时间把三者对齐成一个时间线，
后面的步骤生成才能"每一步都知道自己有哪些证据"。

对齐规则：
- 每个屏幕有一个显示区间 [start, end)；
- 一条字幕与哪些屏幕有重叠，就挂到哪些屏幕下（跨屏的字幕会出现在两个屏幕上）；
- 画面描述本来就属于某张截图，直接跟随。
"""

from __future__ import annotations

from ..models import Frame, Transcript, VisionNote
from ..store import TIMELINE_JSON, read_json, rp, write_json


def build_timeline(frames: list[Frame], transcript: Transcript,
                   notes: list[VisionNote], run_path=None) -> dict:
    """生成 timeline：按屏幕组织的时间线。"""
    note_by_frame = {n.frame_id: n for n in notes}

    screens = []
    for i, frame in enumerate(frames):
        start = frame.time
        # 一个屏幕持续到下一个屏幕开始；最后一屏持续到它自己的 end
        end = frames[i + 1].time if i + 1 < len(frames) else frame.end
        if end <= start:
            end = max(frame.end, start + 0.1)

        seg_ids = [s.id for s in transcript.segments if s.end >= start and s.start <= end]
        frame.transcript_ids = seg_ids
        note = note_by_frame.get(frame.id)
        screens.append({
            "frame_id": frame.id,
            "start": round(start, 3),
            "end": round(end, 3),
            "transcript_ids": seg_ids,
            "vision_ids": [note.id] if note and note.ok else [],
            "text": transcript.text_between(start, end),
            "interface": note.interface if note and note.ok else "",
            "activity": note.activity if note and note.ok else "",
        })

    timeline = {
        "duration": round(frames[-1].end if frames else 0.0, 3),
        "screen_count": len(screens),
        "screens": screens,
    }
    if run_path is not None:
        write_json(run_path / TIMELINE_JSON, timeline)
    return timeline


def load_timeline(run_path) -> dict | None:
    return read_json(rp(run_path, TIMELINE_JSON))
