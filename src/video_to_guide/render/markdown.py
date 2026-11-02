"""生成 Markdown 手册。

为什么先做 Markdown：纯文本、能进 Git、任何编辑器都能改，
而且用户可以自己继续编辑、粘到 wiki 或 issue 里。
"""

from __future__ import annotations

import time
from pathlib import Path

from ..models import Frame, Step, Transcript, VideoInfo, VisionNote
from . import fmt_time, short_visible


def render_markdown(
    steps: list[Step],
    frames: list[Frame],
    notes: list[VisionNote],
    transcript: Transcript,
    video: VideoInfo,
    run_path: Path,
    video_rel: str = "",
    title: str = "",
    meta=None,
) -> str:
    frame_by_id = {f.id: f for f in frames}
    note_by_id = {n.id: n for n in notes}
    seg_by_id = {s.id: s for s in transcript.segments}
    review_count = sum(1 for s in steps if s.needs_review)

    lines: list[str] = []
    lines.append(f"# {title or Path(video.path).stem} 操作手册")
    lines.append("")
    lines.append(f"- 源视频：{Path(video.path).name}")
    lines.append(f"- 时长：{fmt_time(video.duration)}（{video.duration:.1f} 秒）")
    lines.append(f"- 分辨率：{video.width}×{video.height}，帧率 {video.fps:g}")
    lines.append(f"- 步骤数：{len(steps)}" + (f"，其中 {review_count} 步待核实" if review_count else ""))
    lines.append(f"- 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("")
    if review_count:
        lines.append(f"> ⚠️ 有 {review_count} 个步骤标记为「待核实」，说明依据不足，请先核对再使用。")
        lines.append("")
    lines.append("---")
    lines.append("")

    for step in steps:
        lines.append(f"## 步骤 {step.index} · {step.title}")
        lines.append("")
        if video_rel:
            lines.append(f"**时间**：{fmt_time(step.t_start)} – {fmt_time(step.t_end)}"
                         f"（[跳到视频 {fmt_time(step.t_start)}]({video_rel}#t={step.t_start:.1f})）")
        else:
            lines.append(f"**时间**：{fmt_time(step.t_start)} – {fmt_time(step.t_end)}")
        lines.append("")
        if step.needs_review:
            note = step.review_note or "依据不足，需要人工核对"
            lines.append(f"> ⚠️ **待核实**：{note}")
            lines.append("")
        for fid in step.frame_ids:
            f = frame_by_id.get(fid)
            if f:
                lines.append(f"![步骤 {step.index} 截图]({f.path})")
                lines.append("")
        lines.append(f"**操作**：{step.action or '（未填写）'}")
        lines.append("")
        if step.expected_result:
            lines.append(f"**预期结果**：{step.expected_result}")
            lines.append("")
        lines.append(_evidence_line(step, note_by_id, seg_by_id))
        lines.append("")
        lines.append("---")
        lines.append("")

    if not steps:
        lines.append("_没有生成任何步骤。_")
        lines.append("")
    return "\n".join(lines)


def _evidence_line(step: Step, note_by_id: dict, seg_by_id: dict) -> str:
    """把证据渲染成一句话，方便读者核对这条步骤凭什么这么说。"""
    parts = []
    for tid in step.evidence.transcript_ids:
        seg = seg_by_id.get(tid)
        if seg:
            parts.append(f"字幕 {fmt_time(seg.start)}「{seg.text}」")
    for vid in step.evidence.vision_ids:
        n = note_by_id.get(vid)
        if n and n.ok:
            bits = [b for b in (n.interface, short_visible(n.visible_text), n.activity)
                    if b and b != "无明显操作"]
            if bits:
                parts.append("画面：" + "；".join(bits))
    if not parts:
        return "**依据**：（无文字依据，仅截图）"
    return "**依据**：" + " ｜ ".join(parts)


def write_markdown(text: str, path: Path) -> Path:
    path.write_text(text, encoding="utf-8")
    return path
