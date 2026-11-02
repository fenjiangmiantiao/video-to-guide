"""步骤校验：确保每条步骤都有据可查、时间合法。

这是"防止 AI 编造"的最后一道闸门。模型输出永远是不可信的，
这里做四件事：
1. 把引用的证据 id 与真实存在的截图/字幕/画面描述比对，剔除不存在的；
2. 时间越界或倒序的步骤修正到合法范围；
3. 没有任何证据的步骤标记为待核实（并且导出时会被拦下）；
4. 按时间重新排序并编号，保证手册顺序与视频顺序一致。
"""

from __future__ import annotations

import re

from ..models import Frame, Step, Transcript, VisionNote

MIN_STEP_SECONDS = 0.5
# 和评测口径保持一致：低于这个自评置信度就要求人工复核。
# 两个地方用不同的阈值，会出现"评测说该标待核实、手册里却没标"的矛盾。
LOW_CONFIDENCE = 0.7


def has_evidence(step: Step) -> bool:
    """步骤是否至少有一条可追溯的证据。截图本身也算证据。"""
    return bool(step.frame_ids or step.evidence.transcript_ids or step.evidence.vision_ids)


# 复核原因的中文说明（界面和报告共用一套措辞）
REASON_TEXT = {
    "missing_evidence": "没有任何证据支撑",
    "screenshot_only": "只有截图，没有字幕或画面描述",
    "action_not_visible": "只看到结果状态，看不到是谁做的这个动作",
    "ambiguous_target": "步骤提到的界面元素在证据里没有出现",
    "uncertain_time": "时间落在一处整屏切换上，可能只是切了窗口",
    "low_confidence": "模型自评把握较低",
}


def _no_action(note: VisionNote) -> bool:
    """画面描述里没有任何"有人在操作"的迹象。"""
    text = f"{getattr(note, 'activity', '') or ''}".strip()
    if not text:
        return True
    return text in ("无明显操作", "看不出操作", "无", "-")


_TERM = re.compile(r"「([^」]{1,24})」")


def _unmentioned_terms(step: Step, descs: list[VisionNote], transcript) -> list[str]:
    """步骤里用「」点名的元素，是否在它自己引用的证据文字里出现过。

    只查这个步骤引用的那几条证据，不去全片搜——否则第 1 步把整个界面的
    控件名都罗列一遍，后面每一步的术语都能"找到"，检查就失去意义了。
    """
    terms = _TERM.findall(f"{step.action} {step.expected_result}")
    if not terms:
        return []
    hay = " ".join(f"{n.interface} {n.visible_text} {n.activity}" for n in descs)
    seg_by_id = {s.id: s for s in getattr(transcript, "segments", [])}
    for tid in step.evidence.transcript_ids:
        s = seg_by_id.get(tid)
        if s:
            hay += " " + s.text
    return [t for t in terms if t not in hay]


def _is_window_switch(frame_id: str, frames: list[Frame]) -> bool:
    for f in frames:
        if f.id == frame_id:
            return getattr(f, "kind", "") == "window_switch"
    return False


def validate_steps(
    steps: list[Step],
    frames: list[Frame],
    transcript: Transcript,
    notes: list[VisionNote],
    duration: float,
    video_mode: str = "software",
) -> tuple[list[Step], list[str]]:
    """返回 (修正后的步骤, 警告列表)。不抛异常：坏数据被修正并记录原因。

    video_mode 只影响一条规则：uncertain_time（「画面整屏切换可能只是切窗口」）。
    这条规则是给软件录屏写的——录屏里整屏换掉通常意味着换了窗口或软件，
    不一定是一次操作。但实拍视频里画面大幅变化恰恰说明真的发生了动作，
    沿用这条规则会把 44% 的炒菜步骤误标成待核实（实测 27 步里 12 步）。
    """
    warnings: list[str] = []
    frame_ids = {f.id for f in frames}
    seg_ids = {s.id for s in transcript.segments}
    note_ids = {n.id for n in notes if n.ok}

    cleaned: list[Step] = []
    for step in steps:
        # --- 证据 id 有效性 ---
        bad_frames = [x for x in step.frame_ids if x not in frame_ids]
        bad_segs = [x for x in step.evidence.transcript_ids if x not in seg_ids]
        bad_notes = [x for x in step.evidence.vision_ids if x not in note_ids]
        if bad_frames or bad_segs or bad_notes:
            warnings.append(
                f"步骤「{step.title}」引用了不存在的证据，已剔除："
                f"截图={bad_frames} 字幕={bad_segs} 画面={bad_notes}"
            )
        step.frame_ids = [x for x in step.frame_ids if x in frame_ids]
        step.evidence.transcript_ids = [x for x in step.evidence.transcript_ids if x in seg_ids]
        step.evidence.vision_ids = [x for x in step.evidence.vision_ids if x in note_ids]

        # --- 时间修正 ---
        d = max(duration, 0.0)
        t0 = max(0.0, min(float(step.t_start), d))
        t1 = max(0.0, min(float(step.t_end), d))
        if t1 - t0 < MIN_STEP_SECONDS:
            # 先试着往后补到最小时长；若已经贴着视频末尾，则把起点往前让
            t1 = min(t0 + MIN_STEP_SECONDS, d)
            if t1 - t0 < MIN_STEP_SECONDS:
                t0 = max(0.0, t1 - MIN_STEP_SECONDS)
            warnings.append(f"步骤「{step.title}」时间范围不合法，已修正为 {t0:.1f}-{t1:.1f}s")
        step.t_start, step.t_end = round(t0, 3), round(t1, 3)

        # --- 没截图时，自动挂上时间范围内最近的一张，保证手册里有图 ---
        if not step.frame_ids and frames:
            nearest = _nearest_frame(frames, step.t_start, step.t_end)
            if nearest is not None:
                step.frame_ids = [nearest.id]

        # --- 有截图但没写画面描述时，按 frame_id 反查补上 ---
        #
        # 模型经常只写 frame_ids、不写 vision_ids，于是一整份手册全被判成
        # "只有截图"而要求人工复核——可画面描述其实就在 vision.json 里。
        # 证据存在却标"缺证据"，是误报，会训练用户忽略这些标记。
        if step.frame_ids and not step.evidence.vision_ids:
            note_by_frame = {n.frame_id: n for n in notes if n.ok}
            for fid in step.frame_ids:
                n = note_by_frame.get(fid)
                if n is not None:
                    step.evidence.vision_ids.append(n.id)

        # --- 待核实判定：按"缺什么"给原因，而不是只看一个 confidence 数字 ---
        #
        # 为什么改：以前 confidence<0.7 就标，结果真实录屏 8 个步骤全被标成待核实，
        # 用户看到的是一片黄条，等于没有提示。现在每个原因都对应一种可以检查的
        # 具体缺陷，用户能按原因分别处理。
        reasons: list[str] = []

        if not has_evidence(step):
            reasons.append("missing_evidence")
        elif not step.evidence.transcript_ids and not step.evidence.vision_ids:
            reasons.append("screenshot_only")
        else:
            notes_by_id = {n.id: n for n in notes}
            descs = [notes_by_id[i] for i in step.evidence.vision_ids
                     if i in notes_by_id and notes_by_id[i].ok]
            # 只看到"结果状态"、没有任何"动作迹象"：说得通，但无法确认是谁做的
            if descs and not step.evidence.transcript_ids:
                if all(_no_action(n) for n in descs):
                    reasons.append("action_not_visible")
            # 步骤里用「」点名的界面元素，必须在证据文字里出现过
            missing = _unmentioned_terms(step, descs, transcript)
            if missing:
                reasons.append("ambiguous_target")
                step.review_note = (step.review_note + "；" if step.review_note else "") + \
                    "证据里没出现这些界面元素：" + "、".join(missing[:4])
            # 落在"整屏切换"帧上的步骤，很可能只是切了个窗口。
            # 只对软件录屏成立；实拍视频里的整屏变化就是真的动作（见函数说明）。
            if video_mode != "reality" and any(
                    _is_window_switch(f, frames) for f in step.frame_ids):
                reasons.append("uncertain_time")

        # 模型自评低只是辅助原因：单独出现时不足以要求人工复核
        low_conf = bool(step.confidence and step.confidence < LOW_CONFIDENCE)
        if low_conf and reasons:
            reasons.append("low_confidence")

        step.review_reasons = reasons
        if reasons:
            step.needs_review = True
            text = "；".join(REASON_TEXT.get(r, r) for r in reasons)
            step.review_note = (step.review_note + "；" if step.review_note else "") + text
        else:
            step.needs_review = False

        cleaned.append(step)

    # --- 按时间排序并重新编号 ---
    cleaned.sort(key=lambda s: (s.t_start, s.t_end))
    for i, s in enumerate(cleaned):
        s.index = i + 1

    # --- 重叠检测（只警告，不自动改，因为可能是模型把一步拆成了两步）---
    for prev, cur in zip(cleaned, cleaned[1:]):
        if cur.t_start < prev.t_end - 0.05:
            warnings.append(
                f"步骤 {prev.index}「{prev.title}」与步骤 {cur.index}「{cur.title}」时间重叠"
                f"（{prev.t_end:.1f}s > {cur.t_start:.1f}s）"
            )

    if not cleaned:
        warnings.append("校验后没有剩下任何步骤")
    return cleaned, warnings


def _nearest_frame(frames: list[Frame], t_start: float, t_end: float) -> Frame | None:
    """找与 [t_start, t_end] 重叠、或时间最近的一张截图。"""
    overlapping = [f for f in frames if f.time <= t_end and f.end >= t_start]
    if overlapping:
        return overlapping[0]
    if not frames:
        return None
    return min(frames, key=lambda f: abs(f.time - t_start))


def export_blockers(steps: list[Step]) -> list[str]:
    """导出前检查：哪些步骤还不允许进入正式手册。

    规则：没有证据的步骤不能悄悄混进手册。但已经明确标了「待核实」的除外——
    那可能是用户手工补充的一步（AI 没识别出来，自然也没有对应证据），
    手册里会带着待核实标记显示，读者知道要自己核对。这比"禁止导出"更实用。
    """
    problems = []
    for s in steps:
        if not has_evidence(s) and not s.needs_review:
            problems.append(
                f"步骤 {s.index}「{s.title}」没有任何证据，也没有标待核实，不能导出")
    return problems
