"""读取视频元信息：内置实现（PyAV）优先，ffprobe 兜底。

为什么要内置：便携版如果把 ffmpeg/ffprobe 一起打包，两个 exe 加起来 ~450MB，
压缩包直接翻倍；而 PyAV 已经为了抽帧打进包里了，它能读时长、分辨率、
帧率和有没有音轨——这些正是手册需要的全部信息。

ffprobe 仍然保留，因为有些冷门容器 PyAV 打不开，那时用外部工具兜底。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from ..models import VideoInfo


class ProbeError(RuntimeError):
    """视频读不出来（文件损坏、不是视频、没有可用的读取后端）。"""


def probe(video_path: Path, ffprobe: str = "ffprobe", timeout: float = 60.0,
          backend: str = "auto") -> VideoInfo:
    """读取视频元信息。文件不存在或不是合法视频时抛 ProbeError。

    backend: auto = 先用内置 PyAV，失败再试 ffprobe；
             builtin = 只用内置；ffprobe = 只用外部工具。
    """
    video_path = Path(video_path)
    if not video_path.exists():
        raise ProbeError(f"视频文件不存在：{video_path}")
    if video_path.stat().st_size == 0:
        raise ProbeError(f"视频文件是空的（0 字节）：{video_path}")

    backend = (backend or "auto").lower()
    errors: list[str] = []

    if backend in ("auto", "builtin"):
        try:
            return _probe_builtin(video_path)
        except ProbeError as e:
            errors.append(str(e))
            if backend == "builtin":
                raise

    try:
        return _probe_ffprobe(video_path, ffprobe, timeout)
    except ProbeError as e:
        errors.append(str(e))
        raise ProbeError("；".join(errors)) from None


def _probe_builtin(video_path: Path) -> VideoInfo:
    """用 PyAV 直接读容器信息，不调外部程序。"""
    try:
        import av
    except Exception as e:                                    # noqa: BLE001
        raise ProbeError(f"内置读取不可用（PyAV 未安装）：{e}") from e

    try:
        container = av.open(str(video_path))
    except Exception as e:                                    # noqa: BLE001
        raise ProbeError(f"打不开这个文件，可能已损坏或不是视频：{type(e).__name__}: {e}") from e

    try:
        vstreams = list(container.streams.video)
        if not vstreams:
            raise ProbeError("文件里没有视频流，不是可分析的视频")
        v = vstreams[0]
        astreams = list(container.streams.audio)
        a = astreams[0] if astreams else None

        duration = 0.0
        if container.duration:
            duration = float(container.duration) / 1_000_000      # 容器时长是微秒
        if not duration and v.duration is not None and v.time_base:
            duration = float(v.duration * v.time_base)

        fps = 0.0
        try:
            fps = float(v.average_rate) if v.average_rate else 0.0
        except (TypeError, ZeroDivisionError):
            fps = 0.0

        return VideoInfo(
            path=str(video_path.resolve()),
            duration=round(duration, 3),
            width=int(v.codec_context.width or 0),
            height=int(v.codec_context.height or 0),
            fps=round(fps, 3),
            has_audio=a is not None,
            audio_codec=(a.codec_context.name if a else None),
            audio_channels=(a.codec_context.channels if a else None),
            audio_sample_rate=(a.codec_context.sample_rate if a else None),
            size_bytes=video_path.stat().st_size,
        )
    finally:
        try:
            container.close()
        except Exception:                                     # noqa: BLE001
            pass


def _probe_ffprobe(video_path: Path, ffprobe: str, timeout: float) -> VideoInfo:
    """外部 ffprobe 兜底路径（老代码，行为保持不变）。"""
    cmd = [
        ffprobe, "-v", "error",
        "-show_format", "-show_streams",
        "-of", "json",
        str(video_path),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except FileNotFoundError as e:
        raise ProbeError(f"找不到 ffprobe（装 FFmpeg 并加入 PATH，或用内置读取）：{e}") from e
    except subprocess.TimeoutExpired as e:
        raise ProbeError(f"ffprobe 读取超时（{timeout}s）：{video_path}") from e

    if proc.returncode != 0:
        msg = proc.stderr.decode("utf-8", "replace").strip()[:500]
        raise ProbeError(f"ffprobe 无法解析该文件，可能已损坏或不是视频：{msg}")

    try:
        data = json.loads(proc.stdout.decode("utf-8", "replace"))
    except json.JSONDecodeError as e:
        raise ProbeError(f"ffprobe 输出不是合法 JSON：{e}") from e

    streams = data.get("streams", [])
    fmt = data.get("format", {})

    video_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video_stream is None:
        raise ProbeError("文件里没有视频流，不是可分析的视频")

    audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)

    duration = _to_float(fmt.get("duration")) or _to_float(video_stream.get("duration")) or 0.0
    fps = _parse_fraction(video_stream.get("avg_frame_rate")) or _parse_fraction(
        video_stream.get("r_frame_rate")) or 0.0

    return VideoInfo(
        path=str(video_path.resolve()),
        duration=round(duration, 3),
        width=int(video_stream.get("width") or 0),
        height=int(video_stream.get("height") or 0),
        fps=round(fps, 3),
        has_audio=audio_stream is not None,
        audio_codec=(audio_stream or {}).get("codec_name"),
        audio_channels=(audio_stream or {}).get("channels"),
        audio_sample_rate=_to_int((audio_stream or {}).get("sample_rate")),
        size_bytes=int(_to_float(fmt.get("size")) or video_path.stat().st_size),
    )


def _to_float(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _to_int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _parse_fraction(v) -> float | None:
    """ffprobe 的帧率是 "30000/1001" 这种分数形式。"""
    if not v:
        return None
    try:
        if "/" in str(v):
            num, den = str(v).split("/", 1)
            den_f = float(den)
            return float(num) / den_f if den_f else None
        return float(v)
    except (TypeError, ValueError, ZeroDivisionError):
        return None
