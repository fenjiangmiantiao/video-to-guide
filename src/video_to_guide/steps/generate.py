"""用本地语言模型把时间线整理成操作步骤。

核心原则（任务书要求）：**不得把未经视频证实的推测描述成已经发生的操作**。
所以在提示词里明确：
- 只能使用给定的字幕和画面描述；
- 每一步必须挂至少一条证据 id；
- 拿不准就标 needs_review 并写明原因，不要编。

同时准备了一条兜底路径：模型不可用或输出无法解析时，按"每个屏幕一段"生成
保守步骤（标题写"第 N 段画面"，正文用当时的字幕原文），并全部标记待核实。
这样程序不会崩，但也不会伪造"AI 分析成功"。
"""

from __future__ import annotations

import difflib
import json
import re

from ..config import Settings
from ..models import Frame, Step, StepEvidence, Transcript, VisionNote
from ..vision.lms_client import ChatClient, LLMError

# 两套系统提示词，按 settings.video_mode 选。
# 软件版是已经验证过、跑出 21 个步骤的原版，一个字都不要动——它是基线。
SYSTEM_SOFTWARE = "你是一名严谨的技术文档编辑，专门把软件操作录屏整理成操作手册。"

# 现实场景版（Beta）：关注的不是按钮和界面，而是物件、动作和状态变化。
SYSTEM_REALITY = ("你是一名严谨的流程文档编辑，专门把现实中的操作过程"
                  "（做饭、手工制作、装配设备等）整理成能照着复现的图文步骤。"
                  "关注三件事：用了什么物件或食材、做了哪些具体动作、"
                  "物件发生了什么可见的状态变化。")


def system_for(settings) -> str:
    mode = (getattr(settings, "video_mode", "software") or "software").lower()
    return SYSTEM_REALITY if mode == "reality" else SYSTEM_SOFTWARE


def system_strict_for(settings) -> str:
    """重试时用的更硬的要求：有些模型第一遍会先写一段思考草稿就停。"""
    return (system_for(settings) + " 这次必须直接输出 JSON 对象本身，"
            "不要写任何分析、说明、草稿或英文思考过程。")

# 推理型模型会先写一大段思考再给答案。3000 个 token 时思考会把预算吃光，
# 正式 JSON 被截断成半截（实测 deepseek-v4.1-flash 只剩 1157 字全是推理）。
# 提到 8000 之后即使不关思考也能拿到完整 JSON；配合 reasoning_effort=none
# 则完全跳过思考，同一请求 4 秒出结果。
# 实测：136 屏视频切成 4 批后，有 1 批的模型输出（含思考过程）把 8000 token
# 吃光，JSON 在半截被截断，两次都解析失败 → 那一批 45 屏全部降级成占位。
# 提高到 16000 给"思考 + JSON"留足空间。
STEPS_MAX_TOKENS = 16000

# 视频没有人声时追加的说明。实测纯画面（美食制作、静默演示）的视频，
# 模型看到 51 屏「（这段没有语音）」会直接返回 0 个步骤——它不知道在没有
# 台词的情况下该怎么归纳，于是什么都不写。这里明确告诉它：只看画面。
PROMPT_NOSPEECH = """
补充说明：这段视频**没有语音**（字幕为空），所有判断只能来自画面观察。
- 「操作迹象」是唯一线索；描述时用"画面显示…"这种措辞，不要写成"视频里说…"。
- 连续几屏画面几乎一样（只有鼠标位置或指针变化）时，把它们并成一步，或直接跳过。
- 看不清具体点了什么时，仍然写出这一屏发生了什么变化，并标 needs_review=true。
- 没有语音不等于没有内容：画面里的窗口标题、菜单展开、输入框内容变化都可以作为步骤依据。
"""

# 一次最多喂给模型多少屏。
# 实测数据（同一个 11 分钟教程，235 屏）：
#   235 屏不分批 -> 模型返回 0 步（输入太长，抓不住重点）
#   45 屏/批（6 批） -> 21 个真实步骤，质量最好
#   25 屏/批（11 批） -> 150 步，其中 22 条「查看XX界面」+ 100 条占位
# 批越小反而越差：一个操作横跨两批时，两批各自都缺一半上下文，
# 模型就把"画面里出现了某个界面"误判成"做了一次查看操作"。
# 所以取 45：批次要大到能看见操作的上下文。
BATCH_SCREENS = 45
# 补漏时每批屏数：只喂"漏掉的"那几屏。20 屏约 4400 tokens，在 12k 上下文里很安全；
# 再小会让批次数翻倍，本地 gemma 每批几十秒，批次数比单批长度更伤总耗时。
TAILOR_BATCH_SCREENS = 20
# 补漏的屏数上限：再多就说明视频本身大段没有可识别操作，继续补只是烧时间。
TAILOR_MAX_SCREENS = 60
# 相邻两批重叠几屏：避免一个操作正好卡在批次交界处被切成两半
BATCH_OVERLAP = 4

PROMPT_HEAD_SOFTWARE = """下面是一段软件操作录屏的时间线。每一"屏"包含：时间段、当时的字幕、视觉模型对画面的观察。

请整理成操作步骤，只输出 JSON，不要任何其它文字。

要求：
1. 只用给定证据，禁止推测。看不到点击动作时，只描述"画面从 A 变成 B"，不要倒推成点了某按钮。
2. 每步必须给 frame_ids，以及 transcript_ids 或 vision_ids 中至少一项。
3. 时间按先后排列，不重叠，落在视频时长范围内。
4. **只写真正发生了操作的步骤**。「画面里出现了某个软件」不算操作；连续几屏是同一界面、
   只是播放头位置或讲解内容在变，也不算操作，不要写成"查看XX界面""观察XX轨道"。
   判断依据：同一界面内有没有出现新元素（新窗口、菜单展开、数值改变、文字被输入）。
5. title 不超过 10 字、动词开头；action 说清点了哪里/改了什么；expected_result 说画面发生了什么变化。
6. 证据不足时 needs_review=true，并在 review_note 里说明缺什么。

输出格式：
{"steps": [{"title": "打开设置", "t_start": 10.0, "t_end": 15.0, "action": "点击工具栏右侧的设置按钮", "expected_result": "设置窗口弹出", "frame_ids": ["f3"], "transcript_ids": [], "vision_ids": ["v3"], "needs_review": false, "review_note": "", "confidence": 0.8}]}

视频时长：{duration} 秒。

时间线：
"""

# 现实场景版（Beta）。结构不变（events + steps），但要求换成了物件、动作、状态：
# 现实操作里没有"点了哪个按钮"，只有"拿了什么、放到哪里、变成什么样"。
PROMPT_HEAD_REALITY = """下面是一段现实操作过程的时间线。每一"屏"包含：时间段、当时的讲解、视觉模型对画面的观察。

请整理成操作步骤，只输出 JSON，不要任何其它文字。

要求：
1. 只用给定证据，禁止推测。不要编造用量、温度、时长、品牌、型号；看不清就写看不清。
2. 每步必须给 frame_ids，以及 transcript_ids 或 vision_ids 中至少一项。
3. 时间按先后排列，不重叠，落在视频时长范围内。
4. **只写真正发生了动作的步骤**。镜头停留、画面没变化、讲解在闲聊，都不是步骤。
5. title 不超过 12 字、动词开头（如"切番茄""打鸡蛋""倒入油"）；
   action 说清用了什么、做了什么、放到哪里；expected_result 说画面里能看到什么变化。
6. 证据不足时 needs_review=true，并在 review_note 里说明缺什么。

输出格式：
{"steps": [{"title": "切番茄", "t_start": 30.0, "t_end": 40.0, "action": "把番茄切成块，放进碗里", "expected_result": "番茄变成块状，碗里堆起来", "frame_ids": ["f8"], "transcript_ids": [], "vision_ids": ["v8"], "needs_review": false, "review_note": "", "confidence": 0.8}]}

视频时长：{duration} 秒。

时间线：
"""


def _has_speech(transcript) -> bool:
    """判断转录里是否真的有人说话。

    只看"有没有 segment"不够：实测一条纯演示视频（67 秒，只有画面声）转录出
    1 段「....」（1.4-2.4 秒），段数不为 0，于是走"有语音"的提示词，
    模型却拿不到任何讲解内容，只能硬编。改成看有效字数。
    """
    segs = getattr(transcript, "segments", None) or []
    if not segs:
        return False
    chars = 0
    for s in segs:
        # 去掉标点、空格、以及 Whisper 在静音处常吐出的省略号
        chars += len(re.sub(r"[^\w\u4e00-\u9fff]", "", getattr(s, "text", "") or ""))
    return chars >= 8


def prompt_head_for(settings) -> str:
    mode = (getattr(settings, "video_mode", "software") or "software").lower()
    return PROMPT_HEAD_REALITY if mode == "reality" else PROMPT_HEAD_SOFTWARE


class StepError(RuntimeError):
    """步骤生成失败。"""


class ContextOverflow(RuntimeError):
    """请求超过模型上下文长度。

    单独一个类型是为了让上层能"把这一批切两半重试"，而不是直接降级成占位。
    实测：本地 gemma 的上下文设 8192 时，45 屏一批（约 8386 tokens）直接 400，
    整批 45 屏全变占位——但那不是"模型不会做"，只是喂太长。
    """


def generate_steps(
    frames: list[Frame],
    transcript: Transcript,
    notes: list[VisionNote],
    timeline: dict,
    settings: Settings,
    duration: float,
    progress=None,
) -> tuple[list[Step], list[str]]:
    """返回 (步骤列表, 警告列表)。

    帧数超过 BATCH_SCREENS 时切成多批分别生成再合并。
    为什么必须分批：实测把 235 屏一次性喂进去，模型会直接返回 0 个步骤
    （输入太长，它抓不住重点），代码随后降级成"每屏一个步骤"，
    结果比只有 80 帧时还差。分批是让"帧越多越细"这件事真正成立的前提。
    """
    screens = timeline.get("screens") or []
    if len(screens) <= BATCH_SCREENS:
        warnings: list[str] = []
        steps = _gen_with_split(frames, transcript, notes, timeline, screens,
                                settings, duration, warnings, progress)
        # 单批路径同样要做覆盖检查，否则短片段（≤45 屏）永远不会补漏。
        extra, gap_warnings = _fill_gaps(frames, transcript, notes, timeline, steps,
                                         settings, duration, progress)
        warnings.extend(gap_warnings)
        if extra:
            steps = _merge_batches(steps + extra, duration)
        return steps, warnings

    warnings: list[str] = []
    batches = _split_screens(screens, BATCH_SCREENS, BATCH_OVERLAP)
    collected: list[Step] = []
    for bi, part in enumerate(batches):
        if progress:
            progress(f"分批整理步骤 {bi + 1}/{len(batches)}（{len(part)} 屏）…")
        try:
            steps, w = _gen_with_split(frames, transcript, notes, timeline, part,
                                       settings, duration, warnings)
        except Exception as e:                                # noqa: BLE001
            warnings.append(f"第 {bi + 1} 批步骤生成失败：{type(e).__name__}: {e}")
            continue
        warnings.extend(w)
        collected.extend(steps)
    if not collected:
        warnings.append("分批生成后仍然没有得到任何步骤，已降级为保守步骤")
        return _fallback_steps(frames, transcript, notes, timeline, duration, warnings), warnings
    merged = _merge_batches(collected, duration)
    warnings.append(f"步骤由 {len(batches)} 批分别生成后合并（共 {len(screens)} 屏）")
    # 覆盖检查：有画面描述、却没有任何步骤覆盖的时段，补一轮。
    # 归因数据（docs/p2-attribution.md）：4 条正例的 23 个空洞全部是"素材都在、
    # 就是没整理成步骤"，说明瓶颈在这一层，补漏比加抽帧/换视觉模型都直接。
    extra, gap_warnings = _fill_gaps(frames, transcript, notes, timeline, merged,
                                     settings, duration, progress)
    warnings.extend(gap_warnings)
    if extra:
        merged = _merge_batches(merged + extra, duration)
    return merged, warnings


def _gen_with_split(frames: list[Frame], transcript: Transcript, notes: list[VisionNote],
                    timeline: dict, part: list, settings: Settings, duration: float,
                    warnings: list[str], progress=None, depth: int = 0) -> list[Step]:
    """生成一批步骤；碰到上下文超限就把这批切两半分别生成，而不是整批降级。

    本地模型上下文有限（gemma 默认 8k 上下），45 屏一批在长视频上会溢出。
    切半比换更大上下文更稳：不占额外显存，也不会因为一批失手丢掉整段。
    """
    sub = dict(timeline)
    sub["screens"] = part
    ids = {sc.get("frame_id") for sc in part}
    sub_frames = [f for f in frames if f.id in ids] or frames
    try:
        steps, sub_warnings = _generate_once(sub_frames, transcript, notes, sub, settings,
                                             duration, progress)
        warnings.extend(sub_warnings)
        return steps
    except ContextOverflow:
        if depth >= 3 or len(part) <= 8:
            warnings.append(f"批（{len(part)} 屏）超出模型上下文，且已无法再切，跳过")
            return []
        mid = len(part) // 2
        warnings.append(f"批（{len(part)} 屏）超出模型上下文，切成 {mid}+{len(part)-mid} 重试")
        out: list[Step] = []
        for half in (part[:mid], part[mid:]):
            out.extend(_gen_with_split(frames, transcript, notes, timeline, half,
                                       settings, duration, warnings, progress, depth + 1))
        return out


def _uncovered_ranges(steps: list[Step], duration: float, min_gap: float = 4.0) -> list[tuple[float, float]]:
    """找出没被任何真实步骤覆盖的时间段。"""
    covered = sorted((s.t_start, s.t_end) for s in steps if not s.placeholder)
    gaps: list[tuple[float, float]] = []
    cur = 0.0
    for a, b in covered:
        if a - cur >= min_gap:
            gaps.append((cur, a))
        cur = max(cur, b)
    if duration - cur >= min_gap:
        gaps.append((cur, duration))
    return gaps


def _fill_gaps(frames: list[Frame], transcript: Transcript, notes: list[VisionNote],
               timeline: dict, steps: list[Step], settings: Settings, duration: float,
               progress=None) -> tuple[list[Step], list[str]]:
    """对"有动作描述但没进步骤"的时段单独补一轮。"""
    warnings: list[str] = []
    if not getattr(settings, "fill_gaps", True):
        return [], warnings
    if not steps:
        return [], warnings

    gap_ranges = _uncovered_ranges(steps, duration)
    if not gap_ranges:
        return [], warnings

    def _in_gap(t: float) -> bool:
        return any(a <= t < b for a, b in gap_ranges)

    screens = [sc for sc in (timeline.get("screens") or []) if _in_gap(float(sc.get("start") or 0))]
    if not screens:
        return [], warnings

    # 只补"描述里确实写了动作"的屏：片头闲聊、成品展示本来就不该出步骤。
    note_by_id = {n.id: n for n in notes}

    def _has_action(sc) -> bool:
        for vid in sc.get("vision_ids", []):
            n = note_by_id.get(vid)
            if n and n.ok and n.activity and n.activity.strip() not in ("", "无", "无明显操作", "无明显变化", "无。"):
                return True
        return False

    screens = [sc for sc in screens if _has_action(sc)]
    if not screens:
        return [], warnings
    # 成本闸门：本地模型补漏很慢——每批 10-20 屏要几十秒到几分钟，长视频的空洞
    # 动辄几十屏。实测 277 秒的视频补一轮跑了 15 分钟以上还没结束，收益（多几条
    # 步骤）远小于代价。所以只对短视频补，长视频明确告知"跳过"而不是悄悄拖慢。
    if duration > 90 or len(screens) > 40:
        warnings.append(
            f"有 {len(screens)} 屏画面写了动作却没进步骤，视频较长（{duration:.0f}s），"
            "已跳过自动补漏（本地补漏成本高）；可人工补步骤，或确认后重新分析")
        return [], warnings
    if len(screens) > TAILOR_MAX_SCREENS:
        warnings.append(f"有动作但未进步骤的屏幕 {len(screens)} 屏，超过补漏上限 "
                        f"{TAILOR_MAX_SCREENS} 屏，只补前 {TAILOR_MAX_SCREENS} 屏")
        screens = screens[:TAILOR_MAX_SCREENS]

    if progress:
        progress(f"有 {len(gap_ranges)} 段时间没有步骤，正在补（{len(screens)} 屏）…")

    new_steps: list[Step] = []
    for part in _split_screens(screens, TAILOR_BATCH_SCREENS, 0):
        sub = dict(timeline)
        sub["screens"] = part
        try:
            got = _gen_with_split(frames, transcript, notes, timeline, part,
                                  settings, duration, warnings)
            new_steps.extend(got)
        except Exception as e:                                    # noqa: BLE001
            warnings.append(f"补漏一批失败：{type(e).__name__}: {e}")
    if new_steps:
        warnings.append(f"覆盖检查：为 {len(gap_ranges)} 段没有步骤的时间补了 {len(new_steps)} 条")
    return new_steps, warnings


def _generate_once(
    frames: list[Frame],
    transcript: Transcript,
    notes: list[VisionNote],
    timeline: dict,
    settings: Settings,
    duration: float,
    progress=None,
) -> tuple[list[Step], list[str]]:
    """对一条（可能是分片后的）时间线调用一次模型。"""
    warnings: list[str] = []
    # 注意：这里不能用 str.format——提示词里的 JSON 示例含有 { }，会被当成占位符。
    head = prompt_head_for(settings)
    if not _has_speech(transcript):
        head += PROMPT_NOSPEECH
    prompt = head.replace("{duration}", f"{duration:.1f}") + _render_timeline(
        timeline, transcript, notes, frames,
        with_diff=getattr(settings, "frame_context", "plain") == "diff")

    from ..providers import sends_reasoning_effort, sends_thinking_flag
    # 文本角色独立于视觉角色：可以用另一家的模型整理步骤
    pid, base_url, api_key, model = settings.role_client("text")
    client = ChatClient(base_url=base_url, model=model,
                        timeout=settings.llm_timeout, max_tokens=STEPS_MAX_TOKENS, temperature=0.1,
                        disable_thinking=sends_thinking_flag(pid, settings.disable_thinking),
                        reasoning_effort="none" if sends_reasoning_effort(pid) else "",
                        api_key=api_key)

    raw = ""
    data = None
    try:
        if progress:
            progress("正在请求本地模型整理步骤…")
        result = client.chat(
            [{"role": "system", "content": system_for(settings)},
             {"role": "user", "content": prompt}],
            max_tokens=STEPS_MAX_TOKENS,
        )
        raw = result.text
        data = _extract_json(raw)
        if data is None:
            # 实测同一份输入跑三次会有一次只吐一段思考草稿、没有 JSON
            # （输出 200 字就停）。这种情况不该直接降级成"第 N 段画面"，
            # 再问一次、并明确要求直接给 JSON，成功率明显更高。
            if progress:
                progress("模型没有给出 JSON，重试一次…", "warn")
            retry = client.chat(
                [{"role": "system", "content": system_strict_for(settings)},
                 {"role": "user", "content": prompt}],
                max_tokens=STEPS_MAX_TOKENS,
            )
            raw = retry.text
            data = _extract_json(raw)
            if data is not None:
                warnings.append("步骤生成：首次输出不是合法 JSON，重试一次后成功")
    except LLMError as e:
        msg = str(e).lower()
        if "context" in msg or "exceeds" in msg or ("token" in msg and "400" in msg):
            raise ContextOverflow(str(e)) from e
        warnings.append(f"步骤生成：本地模型不可用（{e}），已降级为保守步骤")
        return _fallback_steps(frames, transcript, notes, timeline, duration, warnings), warnings

    if data is None:
        warnings.append("步骤生成：模型输出不是合法 JSON（已重试一次），已降级为保守步骤")
        return _fallback_steps(frames, transcript, notes, timeline, duration, warnings), warnings

    items = data.get("steps") if isinstance(data, dict) else data
    raw_events = data.get("events") if isinstance(data, dict) else None
    events: dict[str, dict] = {}
    if isinstance(raw_events, list):
        for e in raw_events:
            if isinstance(e, dict) and e.get("id"):
                events[str(e["id"])] = e
        # 事件层面的统计进警告，方便对比"模型看到几次操作 vs 最后合并成几步"
        warnings.append(f"步骤生成：模型列出 {len(events)} 个候选事件")

    if not isinstance(items, list) or not items:
        if events:
            # 模型经常只填 events、把 steps 留空（实测 6 批里有 3 批这样）。
            # 这时用事件本身合成步骤：事件是模型真看过的操作，比"每屏一步"准确得多。
            ordered = sorted(events.values(),
                             key=lambda e: float(e.get("time") or 0))
            items = [{
                "title": (e.get("action") or f"操作 {eid}").strip()[:40],
                "t_start": e.get("time"),
                "t_end": float(e.get("time") or 0) + 3.0,
                "action": e.get("action") or "",
                "expected_result": "",
                "event_ids": [eid],
            } for eid, e in ((str(x["id"]), x) for x in ordered)]
            warnings.append(
                f"步骤生成：模型只给出 {len(items)} 个事件、未给步骤，已用事件合成步骤")
        else:
            warnings.append("步骤生成：模型没有给出任何步骤，已降级为保守步骤")
            return _fallback_steps(frames, transcript, notes, timeline, duration, warnings), warnings

    steps: list[Step] = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        try:
            # 引用了不存在的事件：这是硬错误，说明模型在编来源
            if events:
                bad = [str(x) for x in (item.get("event_ids") or []) if str(x) not in events]
                if bad:
                    warnings.append(
                        f"步骤「{item.get('title', i + 1)}」引用了不存在的事件 {bad}，已忽略这些引用")
                if not item.get("event_ids"):
                    warnings.append(
                        f"步骤「{item.get('title', i + 1)}」没有关联任何事件，"
                        "证据只能靠它自己写的 id")
            steps.append(_to_step(item, i, events or None))
        except Exception as e:
            warnings.append(f"步骤生成：第 {i + 1} 条结构不完整，已跳过（{e}）")
    if not steps:
        warnings.append("步骤生成：所有步骤结构都不合法，已降级为保守步骤")
        return _fallback_steps(frames, transcript, notes, timeline, duration, warnings), warnings

    launch = _ensure_launch_step(steps, frames, notes)
    if launch is not None:
        steps.insert(0, launch)
        for i, s in enumerate(steps, 1):
            s.index = i
    return steps, warnings


# "打开了空的记事本窗口" 这类描述，说明画面里刚出现一个应用
_LAUNCH_PAT = re.compile(
    r"(打开了?|刚被?打开|首次打开)[^，。；]{0,6}?(空的?|空白的?|新的?)?[^，。；]{0,8}?窗口")
_APP_NAME_PAT = re.compile(r"打开了?空的?(.{2,12}?)窗口")


def _ensure_launch_step(steps: list[Step], frames: list[Frame],
                        notes: list[VisionNote]) -> Step | None:
    """AI 常常把"应用刚被打开"并进第一次输入，导致手册缺了起点。

    这里用一条纯规则补：只有同时满足三个条件才补一步——
    1. 画面描述里明确写了"打开了…窗口"；
    2. 那一帧的画面变化幅度够大（不是鼠标动了一下）；
    3. 这一帧的时间还没有被任何步骤覆盖（避免重复）。

    三条都满足时，补出来的步骤引用那一帧和它的画面描述，所以不是凭空编的。
    """
    if not steps or not frames:
        return None
    note_by_frame = {n.frame_id: n for n in notes}
    for frame in frames[:4]:
        note = note_by_frame.get(frame.id)
        if note is None or not note.ok:
            continue
        text = f"{note.interface} {note.activity}"
        if not _LAUNCH_PAT.search(text):
            continue
        if float(getattr(frame, "change_score", 0.0) or 0.0) < 0.5:
            continue
        if any(s.t_start <= frame.time <= s.t_end for s in steps):
            continue
        m = _APP_NAME_PAT.search(note.activity or "")
        app = (m.group(1).strip() if m else "").strip("的 ")
        title = f"打开{app}" if app else "打开应用窗口"
        return Step(
            index=0,
            t_start=float(frame.time),
            t_end=float(frame.end or frame.time + 2),
            title=title[:16],
            action=(note.activity or "").strip()[:120] or f"画面里出现了「{note.interface}」",
            expected_result=(note.visible_text or "").strip()[:80] or "出现该应用的窗口",
            frame_ids=[frame.id],
            evidence=StepEvidence(transcript_ids=[], vision_ids=[note.id]),
            needs_review=False,
            confidence=0.6,
            review_note="",
        )
    return None


def _to_step(item: dict, i: int, events: dict | None = None) -> Step:
    """把一条模型输出变成 Step。

    events 非空时走"事件 → 步骤"契约：步骤只声明它覆盖哪几个 event_id，
    截图/字幕/画面证据都从事件里汇总。这样"这一步凭什么成立"由事件决定，
    而不是模型在写步骤时顺手编一串 id。
    """
    frame_ids = [str(x) for x in (item.get("frame_ids") or [])]
    t_ids = [str(x) for x in (item.get("transcript_ids") or [])]
    v_ids = [str(x) for x in (item.get("vision_ids") or [])]

    if events:
        for eid in (item.get("event_ids") or []):
            ev = events.get(str(eid))
            if not ev:
                continue
            for f in (ev.get("frame_ids") or []):
                if str(f) not in frame_ids:
                    frame_ids.append(str(f))
            for t in (ev.get("transcript_ids") or []):
                if str(t) not in t_ids:
                    t_ids.append(str(t))
            for v in (ev.get("vision_ids") or []):
                if str(v) not in v_ids:
                    v_ids.append(str(v))
        # 步骤自己没写时间区间时，用事件的时刻兜底
        if not item.get("t_start") and (item.get("event_ids") or []):
            times = [float(events[str(e)].get("time") or 0.0)
                     for e in item["event_ids"] if str(e) in events]
            if times:
                item = {**item, "t_start": min(times),
                        "t_end": max(item.get("t_end") or (min(times) + 1.0), min(times) + 0.5)}

    return Step(
        index=i + 1,
        t_start=float(item.get("t_start", 0.0)),
        t_end=float(item.get("t_end", 0.0)),
        title=str(item.get("title", "")).strip() or f"步骤 {i + 1}",
        action=str(item.get("action", "")).strip(),
        expected_result=str(item.get("expected_result", "")).strip(),
        frame_ids=frame_ids,
        evidence=StepEvidence(transcript_ids=t_ids, vision_ids=v_ids),
        needs_review=bool(item.get("needs_review", False)),
        confidence=_clamp(item.get("confidence", 0.5)),
        review_note=str(item.get("review_note", "") or "").strip(),
    )


def _clamp(v) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.5
    return max(0.0, min(1.0, f))


def _delta_line(prev: VisionNote | None, cur: VisionNote) -> str:
    """用两屏已有文字算出「相对上一屏发生了什么变化」。

    只比较已经拿到的场景名和动作描述，不再调视觉模型——P2 的取舍是先看
    文本层面的时序够不够用，不够再上双图复核（那样每次都要多一次模型调用）。
    跨批的第一屏没有上一屏可比，直接不给这一行。
    """
    if prev is None:
        return ""
    cut = lambda s: (s or "").strip()[:24]                       # noqa: E731
    parts: list[str] = []
    pi, ci = cut(prev.interface), cut(cur.interface)
    if pi and ci and pi != ci:
        parts.append(f"场景「{pi}」→「{ci}」")
    pa, ca = cut(prev.activity), cut(cur.activity)
    if pa and ca and pa != ca:
        parts.append(f"动作「{pa}」→「{ca}」")
    return "；".join(parts)


def _render_timeline(timeline: dict, transcript: Transcript, notes: list[VisionNote],
                     frames: list | None = None, screens: list | None = None,
                     with_diff: bool = False) -> str:
    """把时间线渲染成紧凑文本，控制 token 数量。

    会带上每一屏的"类型"（整屏切换窗口 / 局部变化），因为录屏里经常有
    "切到别处看一眼再切回来"——像素上跟打开目标软件一模一样，不告诉模型
    它就会把来回切换写成操作步骤。
    """
    seg = {s.id: s for s in transcript.segments}
    note = {n.id: n for n in notes}
    kind_of = {f.id: getattr(f, "kind", "") for f in (frames or [])}
    lines: list[str] = []
    prev_note: VisionNote | None = None
    # screens 不为空时只渲染这几屏（分批生成用），否则渲染整条时间线
    for sc in (screens if screens is not None else timeline.get("screens", [])):
        kind = kind_of.get(sc.get("frame_id"), "")
        tag = {
            "window_switch": "  ←整屏切换（可能是切到别的窗口，不一定是操作）",
            "change": "  ←画面明显变化",
            "local": "  ←局部变化",
        }.get(kind, "")
        lines.append(f"[屏幕 {sc['frame_id']}] {sc['start']:.1f}-{sc['end']:.1f}s{tag}")
        texts = []
        for tid in sc.get("transcript_ids", []):
            s = seg.get(tid)
            if s:
                texts.append(f"{tid}「{s.text}」")
        lines.append("  字幕：" + ("；".join(texts) if texts else "（这段没有语音）"))
        desc = []
        cur_note: VisionNote | None = None
        for vid in sc.get("vision_ids", []):
            n = note.get(vid)
            if n and n.ok:
                if cur_note is None:
                    cur_note = n
                desc.append(f"场景={n.interface or '未知'}")
                if n.visible_text:
                    desc.append(f"可见文字={n.visible_text}")
                desc.append(f"动作={n.activity or '无'}")
        lines.append("  画面：" + ("；".join(desc) if desc else "（无画面描述）"))
        if with_diff:
            delta = _delta_line(prev_note, cur_note)
            if delta:
                lines.append("  变化：" + delta)
        prev_note = cur_note
    return "\n".join(lines)


def _extract_json(text: str):
    """从模型输出里抠出 JSON。容忍代码块包裹和前后多余文字。"""
    if not text:
        return None
    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    candidates = [fence.group(1)] if fence else []
    candidates.append(text)
    for cand in candidates:
        starts = [i for i in (cand.find("{"), cand.find("[")) if i >= 0]
        if not starts:
            continue
        start = min(starts)
        end = max(cand.rfind("}"), cand.rfind("]"))
        if end <= start:
            continue
        snippet = cand[start:end + 1]
        try:
            return json.loads(snippet)
        except json.JSONDecodeError:
            fixed = re.sub(r",\s*([}\]])", r"\1", snippet)
            try:
                return json.loads(fixed)
            except json.JSONDecodeError:
                continue
    return None


def _fallback_steps(frames: list[Frame], transcript: Transcript, notes: list[VisionNote],
                    timeline: dict, duration: float, warnings: list[str]) -> list[Step]:
    """模型不可用时的保守输出：一屏一步，正文只引用原文，全部标记待核实。

    但如果整个视频里根本没有"操作迹象"（实测一个美食制作视频，51 屏的
    activity 全是「无明显操作」），一屏一步只会产出 55 条「第 N 段画面」，
    对用户毫无用处。这种情况明确返回空，让上层报"没检测到软件操作"。
    """
    def _has_action(n) -> bool:
        a = (getattr(n, "activity", "") or "").strip()
        return bool(a) and a not in ("无明显操作", "无", "无操作", "none", "None")

    if not any(n.ok and _has_action(n) for n in notes):
        warnings.append(
            "画面里没有检测到任何操作迹象（可能不是软件操作视频），已放弃生成步骤")
        return []

    note_by_frame = {n.frame_id: n for n in notes}
    steps: list[Step] = []
    for i, sc in enumerate(timeline.get("screens", [])):
        text = (sc.get("text") or "").strip()
        n = note_by_frame.get(sc["frame_id"])
        parts = []
        if text:
            parts.append(f"视频里这段的说法是：「{text}」")
        if n and n.ok:
            if n.interface:
                parts.append(f"画面显示：{n.interface}")
            if n.activity and n.activity != "无明显操作":
                parts.append(f"画面上的迹象：{n.activity}")
        if not parts:
            continue
        steps.append(Step(
            index=len(steps) + 1,
            t_start=float(sc["start"]),
            t_end=float(sc["end"]),
            title=f"第 {len(steps) + 1} 段画面",
            action=" ".join(parts),
            expected_result="",
            frame_ids=[sc["frame_id"]],
            evidence=StepEvidence(transcript_ids=list(sc.get("transcript_ids", [])),
                                  vision_ids=list(sc.get("vision_ids", []))),
            needs_review=True,
            confidence=0.0,
            review_note="自动分析未成功，这是按画面分段生成的占位内容，需要人工补充操作说明",
            placeholder=True,
        ))
    return steps


def _split_screens(screens: list, size: int, overlap: int) -> list[list]:
    """把屏列表切成有重叠的若干批。"""
    if not screens:
        return []
    size = max(1, size)
    overlap = max(0, min(overlap, size - 1))
    out: list[list] = []
    i = 0
    while i < len(screens):
        out.append(screens[i:i + size])
        if i + size >= len(screens):
            break
        i += size - overlap
    return out


def _merge_batches(steps: list[Step], duration: float) -> list[Step]:
    """合并多批结果：按时间排序、去掉重叠批次产生的重复步骤、重新编号。

    重叠区里同一段操作会被两批各写一次。判定"重复"要同时满足：时间有交叠，
    且标题够像。只比"是否是子串"不够——实测同一件事两批写成「加入番茄酱」
    和「加番茄酱」，后者并不是前者的连续子串（中间隔了个"入"），
    于是同一秒出现两个步骤。改用相似度兜住这类中文同义写法。
    """
    def norm(t: str) -> str:
        return "".join((t or "").split())

    def alike(a: str, b: str) -> bool:
        if not a or not b:
            return False
        if a == b or a in b or b in a:
            return True
        # 0.72 是实测挑的线：「翻炒鸡蛋」/「翻动鸡蛋」算同一件事（0.75），
        # 「倒入蛋液」/「倒入食用油」不算（0.67）——动作对象不同就该是两步。
        return difflib.SequenceMatcher(None, a, b).ratio() >= 0.72

    ordered = sorted(steps, key=lambda s: (s.t_start, s.t_end))
    kept: list[Step] = []
    for s in ordered:
        dup = False
        for k in kept:
            overlap = min(s.t_end, k.t_end) - max(s.t_start, k.t_start)
            if overlap <= 0:
                continue
            a, b = norm(s.title), norm(k.title)
            if alike(a, b):
                dup = True
                # 保留信息更全的那条
                if len(s.action or "") > len(k.action or ""):
                    kept[kept.index(k)] = s
                break
        if not dup:
            kept.append(s)
    for i, s in enumerate(kept):
        s.index = i + 1
    return kept

