"""关键帧抽取。

调用 analysis-video（MIT 许可）做两件事：
1. 用场景检测找出"屏幕内容发生变化"的时刻；
2. 为每个稳定屏幕截一张全分辨率图，另存一份缩小的"阅读副本"给视觉模型。

我们不重复实现场景检测：它对录屏的适配（忽略鼠标移动、忽略编码噪声）
比手写阈值可靠，而且已经在本机验证可用。

本模块只负责：跑它 → 读它的 frames.json → 转成我们自己的 Frame 结构 →
把图片复制进 run 目录，保证 run 目录自包含（可以整体拷走）。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

from ..config import Settings
from ..models import Frame
from ..store import (FRAMES_DIR, FRAMES_JSON, OUTPUT_RES, TRANSCRIPT_JSON,
                     VIDEO_JSON, WORK_DIR, frame_abs, read_json, read_json_rel,
                     rel_posix, rp, video_fingerprint, write_json)

# 外部 analysis-video 的原始输出，放在 .work/ 里（属于中间产物，不是成品）
ANALYSIS_DIR = WORK_DIR + "/analysis"


class FramesError(RuntimeError):
    """抽帧失败。"""


def analysis_exe(settings: Settings) -> Path:
    """定位 analysis-video 可执行文件。

    查找顺序：显式配置 > 当前解释器同目录（源码运行时就是 venv/Scripts）
    > 程序目录下的 analysis-video/ 子目录（打包版把独立 exe 放这里）
    > PATH。
    """
    if settings.analysis_video_exe:
        return Path(settings.analysis_video_exe)
    name = "analysis-video.exe" if sys.platform == "win32" else "analysis-video"
    exe_dir = Path(sys.executable).parent
    candidates = [
        exe_dir / name,                      # 和主程序放一起
        exe_dir / "analysis-video" / name,   # 放在子目录里（带 _internal 的版本）
    ]
    # 打包版最容易踩的坑：程序目录里没有它。往上找两级，看是不是从项目目录启动的
    # （项目里的 .venv/Scripts 通常装着 analysis-video）。
    for base in (exe_dir, exe_dir.parent, exe_dir.parent.parent):
        candidates.append(base / ".venv" / "Scripts" / name)
        candidates.append(base / ".venv" / "bin" / name)
    for cand in candidates:
        if cand.exists():
            return cand
    found = shutil.which("analysis-video")
    if found:
        return Path(found)
    return candidates[0]


def frame_budget(settings: Settings, duration: float) -> int:
    """这次最多保留多少张截图。

    固定 80 张的旧策略在 20 分钟的教程上会把后半段抽空：场景检测给出的
    屏数远超 80，均匀稀释之后很多操作只剩一张近似图。改成按时长给预算：
    大约每 3 秒一张，短片保底 max_frames_floor 张，长视频封顶 max_frames_cap。
    """
    if not settings.max_frames_auto:
        return max(1, settings.max_frames)
    cap = int(getattr(settings, "max_frames_cap", 0) or 0)
    floor = max(1, settings.max_frames_floor)
    if duration <= 0:
        return floor if cap <= 0 else min(floor, cap)
    want = int(duration / 3) + 30
    if cap <= 0:                 # 0 = 不设上限
        return max(floor, want)
    floor = min(floor, cap)
    return max(floor, min(cap, want))


def extract_frames(
    video_path: Path,
    run_path: Path,
    settings: Settings,
    srt_path: Path | None = None,
    force: bool = False,
) -> list[Frame]:
    """抽关键帧并复制进 run 目录。已有 frames.json 且非强制时直接复用。"""
    cache_path = run_path / FRAMES_JSON      # 写入一律新布局
    # 抽帧结果和"哪个视频 + 多少张预算"绑在一起：换了视频或者改了截图数量，
    # 旧帧就是另一段画面的截图，不能拿来当这次的证据。
    meta = {
        "video": str(Path(video_path).resolve()),
        "video_id": video_fingerprint(video_path),
        "duration": round(_video_duration(run_path), 2),
        "max_frames": settings.max_frames,
        "max_frames_auto": settings.max_frames_auto,
        "max_frames_floor": settings.max_frames_floor,
        "max_frames_cap": settings.max_frames_cap,
        "read_long_edge": settings.read_long_edge,
    }
    if not force:
        cached = read_json(rp(run_path, FRAMES_JSON))
        if cached and cached.get("frames") and cached.get("meta") == meta:
            return [Frame.from_dict(f) for f in cached["frames"]]

    out_dir = run_path / ANALYSIS_DIR
    backend = (getattr(settings, "frames_backend", "auto") or "auto").lower()
    exe = analysis_exe(settings)

    if backend == "external" and not exe.exists():
        raise FramesError(
            f"配置要求用外部 analysis-video，但找不到：{exe}\n"
            "把 config.json 的 frames_backend 改成 builtin，或把 analysis-video.exe 放进程序目录。")

    use_external = backend == "external" or (backend == "auto" and exe.exists())
    if use_external:
        _run_analysis_video(video_path, out_dir, settings, srt_path,
                            _video_duration(run_path))
        records = _load_records(out_dir)
        if not records:
            raise FramesError("没有检测到任何画面变化，无法抽取截图")
    else:
        records = _builtin_records(video_path, out_dir, settings, run_path)

    frames = _to_frames(records, out_dir, run_path, settings)
    mode = (getattr(settings, "frame_pick", "signal") or "signal").lower()
    if mode == "signal":
        # 按"画面变化 + 讲解密度"选帧：静止又没人说话的段落不占名额，
        # 操作密集的地方多留几张。不设总数上限，抽多少由内容决定。
        segs = _load_segments(run_path)
        frames = _select_by_signal(frames, segs, settings)
    else:
        budget = frame_budget(settings, _video_duration(run_path))
        if len(frames) > budget:
            frames = _thin(frames, budget)

    # _to_frames 会把所有候选帧的图片复制到 output/frames，signal 模式筛完只剩一部分。
    # 不清理的话，目录里会堆一堆永远不会出现在手册里的图：实测 67 秒视频留下 157 张，
    # 实际只用到 27 张，既占空间又让用户以为分析了很多。
    _prune_frame_files(run_path, frames)

    write_json(cache_path, {"meta": meta, "frames": [f.to_dict() for f in frames]})
    return frames


def _video_duration(run_path: Path) -> float:
    """从 run 目录里已写好的 video.json 取时长（拿不到就返回 0）。"""
    data = read_json_rel(run_path, VIDEO_JSON) or {}
    try:
        return float(data.get("duration") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _srt_ts(seconds: float) -> str:
    ms = int(round(max(0.0, seconds) * 1000))
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _write_placeholder_srt(out_dir: Path, duration: float) -> Path:
    """生成一份"占位字幕"，用来让 analysis-video 跳过它自己的语音识别。

    踩过的坑：--no-subtitles 的语义不是"不要字幕、只做画面分析"，而是
    "不看字幕、改用内置 STT 后端转录音频"。那个后端是可选依赖
    （analysis-video[stt]），本机没装（我们用的是外部 whisper），
    于是抽帧阶段会直接以退出码 4 失败，报 stt-backend-missing。
    而且它要求字幕至少 5 条，空文件同样会被拒绝。

    所以这里造 5 条无意义字幕：它只要"有字幕"就跳过 STT，专心做画面分析。
    这份文件只喂给 analysis-video，不会进入我们自己的 transcript.json。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "_placeholder.srt"
    total = max(duration, 5.0)
    step = total / 6.0
    lines: list[str] = []
    for i in range(5):
        t0 = i * step
        t1 = min(t0 + step, total)
        lines += [str(i + 1), f"{_srt_ts(t0)} --> {_srt_ts(t1)}",
                  "（本视频没有可识别的语音）", ""]
    path.write_text(chr(10).join(lines), encoding="utf-8")
    return path


def _run_analysis_video(video_path: Path, out_dir: Path,
                        settings: Settings, srt_path: Path | None,
                        duration: float = 0.0) -> None:
    exe = analysis_exe(settings)
    if not exe.exists():
        raise FramesError(
            f"找不到 analysis-video：{exe}\n"
            "它负责从视频里抽关键帧，是必须的外部工具。三种解决办法：\n"
            "1) 装到 Python 环境里：pip install analysis-video，然后把它的完整路径填进 config.json 的 "
            'analysis_video_exe；\n'
            "2) 把 analysis-video.exe 放到 VideoToGuide.exe 同目录；\n"
            "3) 如果本机已有项目源码，程序会自动找 .venv/Scripts 里的那份。"
        )
    out_dir.mkdir(parents=True, exist_ok=True)

    cmd = [str(exe), "analyze", str(video_path), "--out", str(out_dir),
           "--read-long-edge", str(settings.read_long_edge)]
    # 注意：转录失败时 whisper 会留下一个 0 字节的 srt，exists() 为真但内容为空，
    # 直接传过去会被 analysis-video 判为"这不是字幕"而整体失败。所以要看大小。
    if srt_path and srt_path.exists() and srt_path.stat().st_size > 0:
        transcript_arg = srt_path
    else:
        transcript_arg = _write_placeholder_srt(out_dir, duration)
    cmd += ["--transcript", str(transcript_arg), "--sub-lang", settings.whisper_language]

    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=3600)
    except subprocess.TimeoutExpired as e:
        raise FramesError("analysis-video 抽帧超时（60 分钟）") from e

    if proc.returncode != 0:
        raise FramesError(_extract_error(proc.stdout, proc.stderr, proc.returncode))


def _extract_error(stdout: bytes, stderr: bytes, code: int) -> str:
    """把 analysis-video 的失败原因翻译成一句能看懂的话。

    它的分工很反直觉：真正的错误以 JSON 写到 **stdout**，进度信息写到 stderr。
    所以截 stderr 的尾部只会看到 "[split] 비디오/자막 리소스 분리 중..." 这种进度行，
    根本原因反而丢了。这里优先解析 stdout 里的 JSON。
    """
    out = (stdout or b"").decode("utf-8", "replace").strip()
    err = (stderr or b"").decode("utf-8", "replace").strip()
    start = out.find("{")
    if start >= 0:
        try:
            data = json.loads(out[start:])
            info = data.get("error") or {}
            message = str(info.get("message") or "").strip()
            hint = str(info.get("hint") or "").strip()
            if message:
                text = f"analysis-video 执行失败（退出码 {code}）：{message}"
                if hint:
                    text += chr(10) + "提示：" + hint
                return text
        except (json.JSONDecodeError, ValueError):
            pass
    return f"analysis-video 执行失败（退出码 {code}）：{(err or out)[-600:]}"


def _builtin_records(video_path: Path, out_dir: Path, settings: Settings,
                     run_path: Path) -> list[dict]:
    """用内置抽帧器（PyAV + numpy），不依赖外部 analysis-video。

    便携版必须走这条：PyInstaller 打包 analysis-video 时它的 scipy/_ctypes
    加载不起来，让用户自己装 Python 包又违背"独立运行"。
    av / numpy / Pillow 都是纯 wheel，能正常打包。
    """
    from . import builtin_frames

    try:
        records = builtin_frames.extract(
            video_path, out_dir, settings.read_long_edge,
            duration=_video_duration(run_path))
    except builtin_frames.BuiltinFramesError as e:
        raise FramesError(f"内置抽帧失败：{e}") from e
    if not records:
        raise FramesError("没有检测到任何画面变化，无法抽取截图")
    return records


def _load_records(out_dir: Path) -> list[dict]:
    """analysis-video 把结果放在 <out>/runs/<unit>/frames.json，unit 通常是 full。"""
    candidates = list(out_dir.glob("runs/*/frames.json"))
    if not candidates:
        return []
    data = read_json(candidates[0], {}) or {}
    return list(data.get("records", []))


def _prune_frame_files(run_path: Path, frames: list[Frame]) -> None:
    """删掉 output/frames 里没被最终选中的图。"""
    dest = run_path / FRAMES_DIR
    if not dest.is_dir():
        return
    keep = {Path(f.path).name for f in frames if f.path}
    for p in dest.glob("*.jpg"):
        if p.name in keep:
            continue
        try:
            p.unlink()
        except OSError:
            pass


def _to_frames(records: list[dict], out_dir: Path, run_path: Path,
               settings: Settings) -> list[Frame]:
    frames: list[Frame] = []
    dest_dir = run_path / FRAMES_DIR
    dest_dir.mkdir(parents=True, exist_ok=True)
    run_root = out_dir.parent if out_dir.parent != out_dir else out_dir
    analysis_root = out_dir

    for i, rec in enumerate(records):
        rel = rec.get("image")
        if not rel:
            continue
        src = analysis_root / rel
        if not src.exists():
            # 有些版本把图片放在 runs/<unit>/ 下，兜底再找一次
            hits = list(analysis_root.glob(f"**/{Path(rel).name}"))
            if not hits:
                continue
            src = hits[0]

        dest = dest_dir / Path(rel).name
        if not dest.exists() or dest.stat().st_size != src.stat().st_size:
            shutil.copy2(src, dest)

        interval = rec.get("interval") or [rec.get("time", 0.0), rec.get("time", 0.0)]
        t = float(rec.get("time") or interval[0] or 0.0)
        w, h = _image_size(dest)
        frames.append(Frame(
            id=f"f{i}",
            time=round(t, 3),
            end=round(float(interval[1] if len(interval) > 1 else t), 3),
            width=w,
            height=h,
            # 记录成相对 output/ 的路径（frames/xxx.jpg）。手册就在 output/ 里，
            # 这样 HTML 的 <img src="frames/..."> 不用改，整个 output/ 拷走就能看。
            path=rel_posix(dest, run_path / OUTPUT_RES),
            screen_index=int(rec.get("screen", i)),
            change_score=round(float(rec.get("content_area") or 0.0), 4),
            kind=str(rec.get("kind") or ""),
        ))

    # 按时间排序后重新编号，保证 id 顺序 = 播放顺序
    frames.sort(key=lambda f: (f.time, f.screen_index))
    for i, f in enumerate(frames):
        f.id = f"f{i}"
    return frames


def _image_size(path: Path) -> tuple[int, int]:
    """读图片像素尺寸。

    为什么要记下来：手册里的截图是懒加载的，没有宽高浏览器就占不住位置，
    图片下载完会把页面撑开，目录跳转就会停在半路（实测过这个问题）。
    只读文件头，开销可以忽略。
    """
    try:
        from PIL import Image
        with Image.open(path) as im:
            return int(im.width), int(im.height)
    except Exception:                                        # noqa: BLE001
        return 0, 0


def _load_segments(run_path: Path) -> list[dict]:
    """读已转好的字幕时间轴，用来算"这一刻有没有人在讲操作"。"""
    data = read_json_rel(run_path, TRANSCRIPT_JSON) or {}
    segs = data.get("segments") if isinstance(data, dict) else data
    return segs or []


def _speech_near(segs: list[dict], t: float, win: float = 4.0) -> float:
    """t 前后 win 秒内被语音覆盖的比例（0-1）。

    为什么不用"有没有字幕"这个二值：一句 8 秒的讲解和一句 0.5 秒的"嗯"，
    对"这里在操作"的指示强度完全不同。
    """
    if not segs:
        return 0.0
    lo, hi = t - win, t + win
    covered = 0.0
    for s in segs:
        a = float(s.get("start", 0.0))
        b = float(s.get("end", 0.0))
        if b <= lo or a >= hi:
            continue
        covered += max(0.0, min(b, hi) - max(a, lo))
    return min(1.0, covered / (2 * win))


def _frame_weight(frame: Frame, segs: list[dict], settings: Settings,
                  scene_max: float = 0.0) -> float:
    """这一帧值不值得花一次视觉调用。

    两项信号：画面变化强度（场景检测给的 change_score）+ 附近讲解密度。
    都归一到 0-1 再加权，权重低的帧直接丢掉。

    scene_max 是本视频里 change_score 的最大值。必须按它归一，不能假设
    change_score 天然落在 0-1：实测某教程视频里所有帧的 change_score 都在
    0.05-0.15 之间，直接当 0-1 用会让"画面变化"这一项等于常数，
    权重就退化成只看有没有人说话，选帧也就失去了意义。
    """
    raw_scene = max(0.0, float(frame.change_score or 0.0))
    scene = min(1.0, raw_scene / scene_max) if scene_max > 0 else 0.0
    speech = _speech_near(segs, frame.time)
    ws = float(getattr(settings, "frame_weight_scene", 0.55))
    wp = float(getattr(settings, "frame_weight_speech", 0.45))
    total = ws + wp or 1.0
    return (scene * ws + speech * wp) / total


def _select_by_signal(frames: list[Frame], segs: list[dict],
                      settings: Settings) -> list[Frame]:
    """按权重选帧，不设总数上限。

    规则：
    - 首帧必留（片头本身就是"开始"的画面）；
    - 权重低于 frame_weight_min 的丢掉；
    - 两张之间至少隔 frame_min_gap 秒，太近时留权重更高的那张；
    - 不限制总帧数——操作密集的长视频就该有更多截图。
    """
    if not frames:
        return frames
    min_gap = max(0.0, float(getattr(settings, "frame_min_gap", 1.5)))
    wmin = float(getattr(settings, "frame_weight_min", 0.12))

    scene_max = max((float(f.change_score or 0.0) for f in frames), default=0.0)
    picked: list[Frame] = []
    weights: list[float] = []
    for idx, f in enumerate(frames):
        w = _frame_weight(f, segs, settings, scene_max)
        if idx > 0 and w < wmin:
            continue
        if picked and (f.time - picked[-1].time) < min_gap:
            # 挨得太近：只保留更有信息量的那张，避免同一屏浪费两次调用
            if w > weights[-1]:
                picked[-1] = f
                weights[-1] = w
            continue
        picked.append(f)
        weights.append(w)

    # 尾帧也留一张，手册最后一屏通常就是"完成"的样子
    if frames[-1] is not picked[-1]:
        if picked and (frames[-1].time - picked[-1].time) < min_gap:
            picked[-1] = frames[-1]
        else:
            picked.append(frames[-1])

    # 保底帧数：软件录屏经常整屏不动只有一个小按钮被点，画面变化分很低，
    # 按权重筛完可能只剩十几帧——时间分辨率不够，步骤时间就会挤成一坨
    # （实测 120 秒的设置教程只剩 12 帧，两步时间完全重叠）。低于保底时按时间均匀补回。
    floor = int(getattr(settings, "max_frames_floor", 0) or 0)
    if floor and len(picked) < floor < len(frames):
        picked = _pad_to_floor(picked, frames, floor)

    for i, f in enumerate(picked):
        f.id = f"f{i}"
    return picked


def _pad_to_floor(picked: list[Frame], frames: list[Frame], floor: int) -> list[Frame]:
    """把帧数补到 floor：从被丢掉的帧里按时间均匀取，和已选的合并后按时间排序。"""
    chosen = {id(f) for f in picked}
    rest = [f for f in frames if id(f) not in chosen]
    need = min(floor - len(picked), len(rest))
    if need <= 0:
        return picked
    step = len(rest) / need
    extra = [rest[min(len(rest) - 1, int(i * step))] for i in range(need)]
    out = sorted(picked + extra, key=lambda f: (f.time, f.screen_index))
    return out


def _thin(frames: list[Frame], limit: int) -> list[Frame]:
    """帧太多时按时间均匀抽样，保留首尾（frame_pick=even 时的旧路径）。"""
    if len(frames) <= limit:
        return frames
    step = len(frames) / limit
    picked = [frames[int(i * step)] for i in range(limit)]
    picked[-1] = frames[-1]
    for i, f in enumerate(picked):
        f.id = f"f{i}"
    return picked


def read_image_path(run_path: Path, frame: Frame) -> Path:
    """Frame.path 是相对路径，这里还原成绝对路径。"""
    return frame_abs(run_path, frame.path)
