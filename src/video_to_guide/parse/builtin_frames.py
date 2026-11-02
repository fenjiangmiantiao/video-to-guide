# -*- coding: utf-8 -*-
"""内置抽帧器：用 PyAV + numpy 自己做画面变化检测，不再依赖外部 analysis-video。

为什么要有它：
- analysis-video 是外部 Python 包（依赖 scipy），PyInstaller 打包后它的 _ctypes
  加载失败，便携版就必须靠用户自己装一份——这违背"独立便携版"的目标。
- 而 av / numpy / Pillow 都是纯 wheel，PyInstaller 能正常打包。

做法（针对屏幕录屏优化，不是通用视频理解）：
1. 按固定间隔采样解码（默认每 0.4 秒一帧），不逐帧处理，速度可控；
2. 把每帧缩成小灰度图，和上一张算"变化面积占比"；
3. 变化占比超过阈值就认为换屏，把这一屏的开始/结束时间记下来；
4. 每屏取中间时刻的一帧，按长边缩放后存成 jpg。

产出结构与 analysis-video 的 frames.json 等价（time / end / screen_index /
change_score），所以下游 timeline、步骤生成、评测都不用改。
"""
from __future__ import annotations

import time
from pathlib import Path

import av
import numpy as np
from PIL import Image

# 采样间隔（秒）。太小会很慢，太大可能漏掉短暂出现的对话框。
SAMPLE_INTERVAL = 0.3
# 判定"换屏"的阈值：变化像素占全图的比例。
# 屏幕录屏里光标移动只改几十个像素；打开对话框会改掉 5% 以上；
# 但"在记事本里打字"这种变化，因为记事本只占屏幕三分之一，
# 全屏占比可能只有 1%~2%——阈值设 0.02 会把整段输入合并成一屏，
# 后面步骤生成就只能看到"打开记事本和WPS"这种模糊结论。
# 实测 0.008 能分辨出打字，同时不会被光标动画骗到。
CHANGE_THRESHOLD = 0.008
# 比较用的缩略尺寸：宽度固定，高度按比例。
COMPARE_WIDTH = 160
# 两次换屏之间至少间隔多少秒（避免动画、滚动条产生一堆碎屏）
MIN_SCENE_SECONDS = 0.5
# 同一屏最长持续多少秒。屏幕录屏里经常出现"画面几秒不变"（用户在思考、
# 在等模型输出），如果整段算一屏，代表帧会落在毫无意义的中间时刻，
# 下游就拿不到"操作发生的瞬间"。超过这个时长就主动切一刀。
MAX_SCENE_SECONDS = 4.0
# "剧烈变化"阈值：一屏里出现/消失整个窗口时，变化占比往往超过 30%。
# 这种时刻即使距离上一刀不到 MIN_SCENE_SECONDS，也必须单独成屏——
# 否则"记事本刚被打开"会和后面的内容合并，下游就看不到这一步。
BIG_CHANGE = 0.25
# 分块口径的阈值：任意一块里超过这个比例的像素变了，就算画面有实质变化。
# 打字、勾选、下拉框展开都落在这个量级；纯光标移动只有 0.1~0.2 的一小块。
# 实测数据（真实记事本录屏，210 个采样点）：
#   光标移动 / 轻微悬停     分块 0.03 ~ 0.06
#   在记事本里打字          分块 0.11 ~ 0.12
#   点开下拉框 / 勾选        分块 0.50 ~ 0.56
#   打开/关闭整个窗口        分块 1.00
# 取 0.10 作为分界：能抓住打字，又不会被光标骗到。
BLOCK_THRESHOLD = 0.10
# 判定"和上一屏是同一幅画面"的阈值。低于它就认为画面没动，
# 定时切分不会单独成屏（避免给下游喂重复截图）。
SAME_SCENE_BLOCK = 0.05


class BuiltinFramesError(RuntimeError):
    """内置抽帧失败。"""


def _iter_samples(video_path: Path, sample_interval: float):
    """逐帧产出 (时间秒, 小灰度图)，一路不解码整帧。

    两个关键点：
    1. **不积累全分辨率帧**：1920×1080 一张 RGB 是 6.2 MB，30 分钟按 0.3 秒
       采样约 6000 张 → 37 GB。这里解码一帧就立刻缩成 160px 灰度，原始帧
       随即可以被回收。
    2. **不积累采样序列本身**：用生成器逐帧交出，调用方只持有"上一帧"，
       所以 10 分钟和 30 分钟的视频峰值内存相同。
    """
    try:
        container = av.open(str(video_path))
    except Exception as e:
        raise BuiltinFramesError(f"打不开视频（{type(e).__name__}: {e}）") from e

    count = 0
    try:
        stream = container.streams.video[0]
        next_at = 0.0
        for frame in container.decode(stream):
            t = (float(frame.pts * stream.time_base)
                 if (frame.pts is not None and stream.time_base) else None)
            if t is None:
                t = float(count) * sample_interval
            if t < next_at:
                continue
            next_at = t + sample_interval
            count += 1
            arr = frame.to_ndarray(format="rgb24")
            yield t, _shrink(arr)
    except BuiltinFramesError:
        raise
    except Exception as e:
        raise BuiltinFramesError(f"解码失败（{type(e).__name__}: {e}）") from e
    finally:
        container.close()


def _grab_frames(video_path: Path, sample_interval: float) -> list[tuple[float, np.ndarray]]:
    """把采样结果收集成列表（测试与小文件用；正式流程走 _iter_samples）。"""
    return list(_iter_samples(video_path, sample_interval))


def _shrink(arr: np.ndarray) -> np.ndarray:
    """把一帧缩成小灰度图，用来快速比较。"""
    img = Image.fromarray(arr).convert("L")
    h = max(1, int(img.height * COMPARE_WIDTH / img.width))
    small = img.resize((COMPARE_WIDTH, h), Image.BILINEAR)
    return np.asarray(small, dtype=np.float32)


def _change_ratio(prev: np.ndarray, cur: np.ndarray, threshold: int = 12) -> float:
    """两张小图里"明显变了"的像素占比（整屏口径，用于判断剧烈变化）。"""
    if prev.shape != cur.shape:
        return 1.0
    diff = np.abs(cur - prev)
    return float((diff > threshold).mean())


def _block_change(prev: np.ndarray, cur: np.ndarray, threshold: int = 12,
                  grid: int = 4, block_ratio: float = 0.35) -> float:
    """按块判断变化：返回"变化最剧烈的那一块"的像素占比。

    为什么不用整屏占比：屏幕录屏里绝大多数操作都是局部的——
    在记事本里打字、点开一个下拉框、勾一个复选框，可能只占全屏 1%，
    但**在那一块里**变化超过三成。整屏口径会把它们全部忽略，
    结果就是一整段操作被合成一屏，下游只能看到"打开文件对话框"这种粗结论。
    分块后既能抓住局部变化，又不会被光标移动（只影响一两个块的一小块）骗到。
    """
    if prev.shape != cur.shape:
        return 1.0
    diff = (np.abs(cur - prev) > threshold).astype(np.float32)
    h, w = diff.shape
    bh, bw = max(1, h // grid), max(1, w // grid)
    worst = 0.0
    for by in range(grid):
        for bx in range(grid):
            block = diff[by * bh:(by + 1) * bh, bx * bw:(bx + 1) * bw]
            if block.size:
                worst = max(worst, float(block.mean()))
    return worst


def _concentration(prev: np.ndarray, cur: np.ndarray, threshold: int = 12) -> float:
    """变化是否集中：最大的一块占"所有活跃块之和"的比例。

    1.0 表示只有一小片在动（打字、点按钮）；0.1 左右表示整屏都在换（切换窗口）。
    """
    diff = np.abs(cur - prev)
    mask = diff > threshold
    h, w = mask.shape
    bh, bw = h // 4, w // 4
    blocks = []
    for by in range(4):
        for bx in range(4):
            b = mask[by * bh:(by + 1) * bh, bx * bw:(bx + 1) * bw]
            if b.size:
                blocks.append(float(b.mean()))
    if not blocks:
        return 0.0
    total = sum(blocks)
    return (max(blocks) / total) if total > 0 else 0.0


def _scene_kind(ratio: float, concentration: float) -> str:
    """给一屏分类：整屏切换窗口 / 明显变化 / 局部变化。

    这个结论只是"线索"，不是判断：切到别的窗口和打开目标软件在像素上一样。
    它的用处是让步骤生成模型知道"这一屏是从别处切过来的"，避免把
    "切回记事本"当成一次操作。
    """
    if ratio >= 0.30 and concentration < 0.45:
        return "window_switch"
    if ratio >= 0.08:
        return "change"
    return "local"


def detect_scenes(video_path: Path, duration: float,
                  sample_interval: float = SAMPLE_INTERVAL,
                  threshold: float = CHANGE_THRESHOLD) -> list[dict]:
    """返回 [{"start", "end", "score", "peak", "trigger"}], peak 是该屏代表帧的时间点。

    全程只持有"上一帧"和"当前屏"的少量变量，内存占用与视频长度无关：
    10 分钟和 30 分钟的视频跑出来峰值一样。
    """
    scenes: list[dict] = []
    start_time = 0.0
    start_idx = 0
    best_ratio = 0.0
    prev: np.ndarray | None = None
    prev_time = 0.0
    last_time = 0.0
    idx = 0

    for t, small in _iter_samples(video_path, sample_interval):
        if prev is None:                 # 第一帧
            start_time = t
            prev, prev_time = small, t
            last_time = t
            idx = 1
            continue

        ratio = _change_ratio(prev, small)
        block = _block_change(prev, small)
        elapsed = t - start_time
        long_enough = elapsed >= MAX_SCENE_SECONDS
        # 三个触发条件：局部明显变化 / 整屏剧烈变化 / 这一屏已经太长
        changed = ((block >= BLOCK_THRESHOLD or ratio >= threshold)
                   and elapsed >= MIN_SCENE_SECONDS) or ratio >= BIG_CHANGE
        # 定时切分的这一屏如果和上一屏几乎一模一样，就不单独成屏——
        # "画面静止但时间在走"并不代表发生了新操作，硬切只会给下游喂重复截图。
        # 但允许它一直延长下去也不行，所以用 2 倍上限兜底。
        if long_enough and not changed:
            if (_block_change(prev, small) < SAME_SCENE_BLOCK
                    and scenes and elapsed < MAX_SCENE_SECONDS * 2):
                if ratio > best_ratio:
                    best_ratio = ratio
                prev, prev_time = small, t
                last_time = t
                idx += 1
                continue

        if changed or long_enough:
            scenes.append({
                "start": start_time,
                "end": t,
                "score": round(best_ratio, 4),
                # 代表帧取"这一屏稳定之后"：变化刚发生的那一帧往往还没画完
                # （对话框半透明、文字还在逐字出现），读者想看的是稳定态。
                "peak": prev_time,
                "trigger": "change" if changed else "timeout",
                # 整屏都在动 = 切换窗口；只有一小片在动 = 目标窗口内的操作。
                # 实测：切窗口时 12~16 个分块同时变（集中度 0.06~0.15），
                # 记事本里打字/点击只有 1 块在变（集中度 0.8~1.0）。
                "kind": _scene_kind(ratio, _concentration(prev, small)),
            })
            start_time = t
            start_idx = idx
            best_ratio = 0.0
        elif ratio > best_ratio:
            best_ratio = ratio

        prev, prev_time = small, t
        last_time = t
        idx += 1

    if prev is None:
        raise BuiltinFramesError("这个视频里没有任何可解码的画面")

    end_time = duration if duration > 0 else (last_time + sample_interval)
    scenes.append({
        "start": start_time,
        "end": end_time,
        "score": round(best_ratio, 4),
        "peak": last_time,
        "trigger": "tail",
    })
    return scenes


def extract(video_path: Path, out_dir: Path, read_long_edge: int,
            duration: float = 0.0, progress=None) -> list[dict]:
    """抽帧并写图片，返回与 analysis-video 兼容的 records。"""
    t0 = time.time()
    scenes = detect_scenes(video_path, duration)
    if progress:
        progress(f"检测到 {len(scenes)} 屏，开始截取代表帧…")

    out_dir.mkdir(parents=True, exist_ok=True)
    targets = [s["peak"] for s in scenes]

    records: list[dict] = []
    try:
        container = av.open(str(video_path))
    except Exception as e:
        raise BuiltinFramesError(f"打不开视频（{type(e).__name__}: {e}）") from e

    try:
        stream = container.streams.video[0]
        idx = 0
        for frame in container.decode(stream):
            if frame.pts is None or not stream.time_base:
                continue
            t = float(frame.pts * stream.time_base)
            while idx < len(targets) and t >= targets[idx]:
                arr = frame.to_ndarray(format="rgb24")
                img = Image.fromarray(arr)
                if img.width > read_long_edge:
                    h = int(img.height * read_long_edge / img.width)
                    img = img.resize((read_long_edge, h), Image.LANCZOS)
                name = f"scene_{idx:03d}_t{t:04.2f}.jpg"
                img.save(out_dir / name, quality=92)
                scene = scenes[idx]
                records.append({
                    "image": name,
                    "time": round(scene["peak"], 3),
                    "interval": [round(scene["start"], 3), round(scene["end"], 3)],
                    "screen": idx,
                    "content_area": scene["score"],
                    "kind": scene.get("kind", ""),
                })
                idx += 1
            if idx >= len(targets):
                break
    except Exception as e:
        raise BuiltinFramesError(f"截取代表帧失败（{type(e).__name__}: {e}）") from e
    finally:
        container.close()

    if not records:
        raise BuiltinFramesError("没有截到任何画面")

    elapsed = time.time() - t0
    if progress:
        progress(f"抽帧完成：{len(records)} 张，用时 {elapsed:.1f} 秒")
    return records
