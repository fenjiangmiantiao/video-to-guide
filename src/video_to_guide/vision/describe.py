"""批量给截图生成画面描述。

设计取舍：
1. 顺序处理而不是并发——本地单卡模型并发只会互相抢显存，反而更慢；
2. 每张图的成败单独记录，一张失败不影响其它（run_report 里汇总）；
3. 结果写 vision.json，重复运行直接复用，避免重复计算（模型很贵）；
4. 提示词强制三行固定格式，便于程序解析；解析失败时保留原文，供人工核对。
"""

from __future__ import annotations

import hashlib
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from ..config import Settings
from ..models import Frame, VisionNote
from ..store import VISION_JSON, frame_abs, read_json, rp, write_json
from .lms_client import ChatClient, LLMError

# 两套视觉提示词，按 settings.video_mode 选。
# 软件模式是已经验证过的原版，一个字都不要改——它是基线。
PROMPT_SOFTWARE = """你是软件操作录屏的画面分析器。看这一帧截图，用中文回答，严格按以下三行格式输出：

界面：<这是什么软件或什么窗口，一句话>
可见文字：<按钮、菜单、标签、输入框里的文字，用「」逐个列出，照抄原文；看不清就不写>
操作迹象：<画面上正在发生或刚发生什么操作，例如某按钮被高亮、某下拉框展开、某数值刚改变；没有明显操作就写"无明显操作">

只输出这三行，不要解释，不要客套，不要编号。"""

# 现实场景模式（Beta）。关注点是"东西在哪、手里拿什么、放到哪里、状态怎么变"，
# 而不是按钮和菜单：现实操作没有 UI，只有物体和状态。
PROMPT_REALITY = """你是现实操作过程视频的画面分析器。看这一帧截图，用中文回答，严格按以下三行格式输出：

场景：<在哪、在做什么，一句话>
画面物件与文字：<出现的关键物件、食材、工具、包装上的文字，用「」逐个列出；看不清就不写>
动作与状态：<手里拿什么、往哪里放、用什么工具、物件发生了什么可见变化（颜色/形状/位置/数量）；没有明显变化就写"无">

只输出这三行，不要解释，不要客套，不要编号。"""


def prompt_for(settings) -> str:
    """按视频类型选提示词。默认软件模式。"""
    mode = (getattr(settings, "video_mode", "software") or "software").lower()
    return PROMPT_REALITY if mode == "reality" else PROMPT_SOFTWARE


def describe_frames(
    frames: list[Frame],
    run_path: Path,
    settings: Settings,
    image_of,
    force: bool = False,
    progress=None,
    stats: dict | None = None,
) -> list[VisionNote]:
    """为每张截图生成 VisionNote。

    image_of: 传入 Frame 返回图片绝对路径的函数（由调用方决定用原图还是阅读副本）。
    progress: 可选回调 progress(done, total, note)。
    stats: 传入一个 dict 时，会填上 reused / described / total，供上层报告"复用了几张"。
    """
    cache_path = run_path / VISION_JSON
    limit = settings.vision_batch_limit if settings.vision_batch_limit > 0 else 0

    from ..providers import sends_reasoning_effort, sends_thinking_flag
    # 视觉角色可以和服务商跟"步骤整理"完全不同（例如本地看截图 + 云端写步骤）
    pid, base_url, api_key, model = settings.role_client("vision")
    client = ChatClient(
        base_url=base_url,
        model=model,
        timeout=settings.vision_timeout,
        max_tokens=settings.vision_max_tokens,
        disable_thinking=sends_thinking_flag(pid, settings.disable_thinking),
        reasoning_effort="none" if sends_reasoning_effort(pid) else "",
        api_key=api_key,
    )

    targets = frames
    if limit and limit < len(frames):
        # 均匀抽取，保证描述覆盖整段视频，而不是只描述开头 N 屏
        step = len(frames) / limit
        targets = [frames[min(len(frames) - 1, int(i * step))] for i in range(limit)]

    # 缓存按"这张图本身 + 用哪个模型、哪版提示词、多大尺寸算的"来认。
    # 只认 frame_id 是不够的：重抽帧后同一 id 可能已经是另一张图。
    sig = {
        "provider": pid,
        "model": model,
        "read_long_edge": settings.read_long_edge,
        "prompt_hash": _text_hash(prompt_for(settings)),
        "max_tokens": settings.vision_max_tokens,
        "frames": {f.id: _file_hash(image_of(f)) for f in targets},
    }
    reusable: dict[str, VisionNote] = {}
    if not force:
        reusable = _load_reusable(cache_path, targets, sig)

    # 上一版存档里的全部描述：写盘时要合并回去，避免中断导致数据缩水
    prev_notes = [VisionNote.from_dict(n) for n in
                  ((read_json(cache_path) or {}).get("notes") or [])]

    # 先按 targets 顺序把能复用的填好，剩下的才真正去调模型。
    by_frame: dict[str, VisionNote] = {}
    used_ids: set[str] = set()
    reused = 0
    todo: list[tuple[int, Frame]] = []
    done = 0
    for i, frame in enumerate(targets):
        old = reusable.get(frame.id)
        if old is not None:
            by_frame[frame.id] = old
            used_ids.add(old.id)
            reused += 1
            done += 1
            if progress:
                progress(done, len(targets), old)
            continue
        todo.append((i, frame))

    # 并发数由配置决定，默认 1（顺序）。云端路由下并发明显更快；
    # 本机单卡跑时保持 1，避免互相抢显存（那只会更慢）。
    workers = max(1, int(getattr(settings, "vision_workers", 1) or 1))
    lock = threading.Lock()

    def _run(item: tuple[int, Frame]) -> VisionNote:
        idx, frame = item
        note = _describe_one(client, frame, idx, image_of, settings)
        with lock:
            if note.id in used_ids:             # 复用旧 id 时避免撞号
                note.id = f"{note.id}_{idx}"
            used_ids.add(note.id)
            by_frame[frame.id] = note
            # 每张图存一次盘：中途模型挂了，已完成的描述不会白跑。
            # 存盘要合并旧存档里"本次还没轮到"的帧——只写当前 notes 会把这些帧
            # 的描述丢掉（实测中断一次，235 条缓存被覆盖成 9 条）。
            ordered = [by_frame[f.id] for f in targets if f.id in by_frame]
            _save_progress(cache_path, sig, ordered, prev_notes)
        return note

    if todo:
        if workers > 1 and len(todo) > 1:
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futures = [ex.submit(_run, item) for item in todo]
                for fut in as_completed(futures):
                    note = fut.result()          # _describe_one 内部已兜住异常
                    done += 1
                    if progress:
                        progress(done, len(targets), note)
        else:
            for item in todo:
                note = _run(item)
                done += 1
                if progress:
                    progress(done, len(targets), note)

    notes = [by_frame[f.id] for f in targets if f.id in by_frame]
    _save_progress(cache_path, sig, notes, prev_notes)
    if stats is not None:
        stats.update({"reused": reused, "described": len(notes) - reused, "total": len(notes)})
    return notes


def _save_progress(cache_path: Path, sig: dict, notes: list[VisionNote],
                   prev_notes: list[VisionNote]) -> None:
    """把已完成的结果和旧存档合并后写盘。

    只写当前 notes 会把本次没处理到的帧丢掉：中断一次就等于把缓存清空了一半。
    合并时以本次结果为准，其余帧沿用旧描述。
    """
    merged: dict[str, VisionNote] = {n.frame_id: n for n in prev_notes}
    for n in notes:
        merged[n.frame_id] = n
    write_json(cache_path, {"meta": sig, "notes": [n.to_dict() for n in merged.values()]})


def _file_hash(path: Path) -> str:
    """图片内容哈希。截图只有几百 KB，逐张算一次可以接受；
    源视频那种大文件不在这一步算。"""
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()[:16]
    except OSError:
        return ""


def _text_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


def _load_reusable(cache_path: Path, targets: list[Frame], sig: dict) -> dict[str, VisionNote]:
    """挑出可以安全复用的旧描述。

    必须同时满足：同一张图（内容哈希一致）、同一个模型、同一版提示词、
    同样的阅读尺寸和输出上限、上次是成功的。
    任何一项对不上就重算——宁可多花一次模型调用，也不拿旧结果冒充新结果。
    """
    cached = read_json(cache_path) or {}
    raw = cached.get("notes") or []
    if not raw:
        return {}
    meta = cached.get("meta") or {}
    # 早期版本没有 meta：无法确认当时用的什么模型和提示词，按"不可信"处理，
    # 重新算一遍；旧文件不删，留着可追溯。
    if not meta:
        return {}
    for key in ("model", "read_long_edge", "prompt_hash", "max_tokens"):
        if meta.get(key) != sig.get(key):
            return {}
    old_frames = meta.get("frames") or {}
    new_frames = sig.get("frames") or {}
    wanted = {f.id for f in targets}
    out: dict[str, VisionNote] = {}
    for item in raw:
        try:
            note = VisionNote.from_dict(item)
        except Exception:
            continue
        if note.frame_id not in wanted or not note.ok:
            continue
        # 同一 id 的图片内容必须没变，否则旧描述说的是另一张图
        if old_frames.get(note.frame_id) != new_frames.get(note.frame_id):
            continue
        out.setdefault(note.frame_id, note)
    return out


def _describe_one(client: ChatClient, frame: Frame, index: int, image_of,
                  settings=None) -> VisionNote:
    note = VisionNote(id=f"v{index}", frame_id=frame.id)
    img = Path(image_of(frame))
    t0 = time.time()
    try:
        result = client.describe_image(img, prompt_for(settings) if settings else PROMPT_SOFTWARE)
        note.elapsed = round(time.time() - t0, 2)
        note.raw = result.text.strip()
        parsed = parse_answer(note.raw)
        # 现实模式用「场景/画面物件与文字/动作与状态」，软件模式用「界面/可见文字/操作迹象」。
        # 两种都认，旧缓存也能读。
        note.interface = parsed.get("场景") or parsed.get("界面", "")
        note.visible_text = parsed.get("画面物件与文字") or parsed.get("可见文字", "")
        note.activity = parsed.get("动作与状态") or parsed.get("动作迹象") or parsed.get("操作迹象", "")
        if not note.interface and not note.visible_text:
            # 模型没按格式回答：保留原文，但标记为需要复核
            note.error = "模型输出不符合三行格式，已保留原文"
    except LLMError as e:
        note.elapsed = round(time.time() - t0, 2)
        note.error = str(e)
    except Exception as e:
        note.elapsed = round(time.time() - t0, 2)
        note.error = f"{type(e).__name__}: {e}"
    return note


_KEY = re.compile(r"^\s*(场景|界面|可见文字|画面物件与文字|动作迹象|操作迹象|动作与状态)\s*[：:]\s*(.*)$")


def parse_answer(text: str) -> dict[str, str]:
    """把"界面：xxx / 可见文字：xxx / 操作迹象：xxx"解析成 dict。容忍多余前缀和换行。"""
    out: dict[str, str] = {}
    current = None
    for line in text.splitlines():
        m = _KEY.match(line)
        if m:
            current = m.group(1)
            out[current] = m.group(2).strip()
        elif current and line.strip():
            out[current] = (out[current] + " " + line.strip()).strip()
    return out


def load_notes(run_path: Path) -> list[VisionNote]:
    data = read_json(rp(run_path, VISION_JSON)) or {}
    return [VisionNote.from_dict(n) for n in data.get("notes", [])]
