"""语音转文字。

默认**在进程内直接调用 faster-whisper**（不再 spawn 外部 CLI）：
1. 便携包里自带 Python 与依赖，用户不需要额外安装；
2. 模型只加载一次（旧方案每次分析都要重新加载，冷启动 1~3 秒）；
3. 音频用 PyAV 自己解码成 16k 单声道，绕开 faster-whisper 内部对
   av 版本有要求的解码路径（实测 faster-whisper 1.2.1 与 av 19 不兼容）。

仍然保留 whisper-ctranslate2 CLI 这条老路：配置里填了 whisper_exe 且
whisper_backend=cli 时走它，方便已经装好外部工具的用户。

产出的 transcript.json 里每条字幕都有 id（t0、t1...），供步骤引用。
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
from pathlib import Path

import numpy as np

from ..config import Settings
from ..models import Transcript, TranscriptSegment
from ..store import TRANSCRIPT_JSON, read_json, rp, video_fingerprint, write_json

SAMPLE_RATE = 16000
# 模型加载一次就够了，反复 new WhisperModel 会把界面拖慢好几秒
_MODEL_CACHE: dict = {}
_MODEL_LOCK = threading.Lock()


class TranscribeError(RuntimeError):
    """转录失败（没有音轨、whisper 不可用、模型下载失败等）。"""


def transcribe(
    video_path: Path,
    run_path: Path,
    settings: Settings,
    language: str | None = None,
    force: bool = False,
) -> Transcript:
    """把视频里的语音转成带时间戳的字幕。

    已有 transcript.json 且非强制重跑时，直接读缓存。
    """
    cache_path = run_path / TRANSCRIPT_JSON
    lang = language or settings.whisper_language
    # 缓存要认"用哪个模型、哪种语言、什么计算精度"转的。
    # 只判断"文件存在"会导致：换成 large-v3 之后仍然拿 small 的结果充数。
    meta = {
        "model": settings.whisper_model,
        "language": lang,
        "compute_type": settings.whisper_compute_type,
        # 源视频内容变了，字幕就必须重转——哪怕路径和时长都一样
        "video": video_fingerprint(video_path),
    }
    if not force:
        cached = read_json(rp(run_path, TRANSCRIPT_JSON))
        if cached and cached.get("segments") and cached.get("meta") == meta:
            return Transcript.from_dict(cached)

    backend = (getattr(settings, "whisper_backend", "auto") or "auto").lower()
    # 默认走向内置：便携包自带 faster-whisper，用户不需要装任何外部工具。
    # 只有明确把 whisper_backend 设成 cli 时才用外部 whisper-ctranslate2。
    use_cli = backend == "cli"

    if use_cli:
        srt_path = cache_path.with_suffix(".srt")
        _run_whisper(video_path, srt_path, settings, lang)
        segments = parse_srt(srt_path)
        name = "whisper-ctranslate2"
    else:
        segments = _transcribe_builtin(video_path, settings, lang)
        name = "faster-whisper"

    # 没有字幕不是错误：纯操作录屏、只有背景音乐的视频很常见。
    # 这种情况继续往下走（只靠画面生成步骤），但要在报告里说清楚，
    # 免得用户以为"软件坏了"或者以为"这视频真的没讲任何东西"。
    transcript = Transcript(language=lang, backend=name, segments=segments)
    doc = transcript.to_dict()
    doc["meta"] = meta
    write_json(cache_path, doc)
    return transcript




# ---------------------------------------------------------------- 内置转录
def _load_audio(video_path: Path, sample_rate: int = SAMPLE_RATE) -> np.ndarray:
    """用 PyAV 解出单声道 16k float32 音频。

    为什么自己解码：faster-whisper 1.2.1 内部调用 av.open(metadata_errors=...)
    在 av 19 上会直接 TypeError。自己解码既绕开这个版本冲突，也让我们能控制
    "没有音轨"这种情况——直接返回空数组，不必让模型去猜。
    """
    import av

    container = av.open(str(video_path))
    try:
        if not container.streams.audio:
            return np.zeros(0, dtype=np.float32)
        stream = container.streams.audio[0]
        resampler = av.AudioResampler(format="fltp", layout="mono", rate=sample_rate)
        chunks: list[np.ndarray] = []
        for frame in container.decode(stream):
            for out in resampler.resample(frame):
                chunks.append(out.to_ndarray().reshape(-1))
        for out in resampler.resample(None):          # 冲掉重采样器里的残留
            chunks.append(out.to_ndarray().reshape(-1))
        if not chunks:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(chunks).astype(np.float32)
    finally:
        container.close()


def _model_path(settings: Settings) -> str:
    """找模型目录。

    查找顺序统一由 asr_models.resolve() 决定（用户指定目录 → 程序自带 models/ →
    本软件下载的平铺目录 → HF 官方缓存）。只认"目录里真有 model.bin"的位置，
    否则返回档位名交给 faster-whisper 自己解析（它会当 HF 仓库名去下）。
    """
    from .. import asr_models

    size = (settings.whisper_model or "small").strip()
    found = asr_models.resolve(size, settings)
    if found is not None:
        return str(found)
    # 不是内置档位（比如用户直接写了仓库名或绝对路径），原样交回去
    return size


def _pick_device(settings: Settings) -> tuple[str, str]:
    """决定用哪块硬件和什么精度。

    auto：有可用的 CUDA 就走 GPU（同样模型下快一个数量级），否则退回 CPU。
    GPU 上默认 float16——int8 是 CPU 的省钱选项，在显卡上反而又慢又损精度。
    """
    want = (getattr(settings, "whisper_device", "auto") or "auto").strip().lower()
    ct = (getattr(settings, "whisper_compute_type", "auto") or "auto").strip().lower()
    cpu_default = "int8"
    gpu_default = "float16"
    if want == "cpu":
        return "cpu", (cpu_default if ct in ("auto", "float16", "bfloat16", "int8_float16") else ct)
    if want == "cuda":
        return "cuda", (gpu_default if ct in ("auto", "int8") else ct)
    try:
        import ctranslate2

        if ctranslate2.get_cuda_device_count() > 0:
            return "cuda", (gpu_default if ct in ("auto", "int8") else ct)
    except Exception:                                        # noqa: BLE001
        pass
    return "cpu", (cpu_default if ct in ("auto", "float16", "bfloat16", "int8_float16") else ct)


def _get_model(settings: Settings):
    """加载模型并按 (路径, 设备, 精度) 缓存，避免每段视频都重新读一遍。"""
    from faster_whisper import WhisperModel

    # faster-whisper 加载时也会读 HF 环境变量，HF_HOME 带引号会让它崩在 OSError 22
    from ..config import sanitize_hf_env

    sanitize_hf_env()
    path = _model_path(settings)
    device, compute = _pick_device(settings)
    key = (path, device, compute)
    with _MODEL_LOCK:
        model = _MODEL_CACHE.get(key)
        if model is None:
            try:
                model = WhisperModel(path, device=device, compute_type=compute)
            except Exception as e:                            # noqa: BLE001
                if device == "cuda":
                    # 显卡在但 CUDA 库不全（缺 cuDNN/cuBLAS）时退回 CPU，不让整个分析失败
                    try:
                        model = WhisperModel(path, device="cpu", compute_type="int8")
                        device, compute = "cpu", "int8"
                    except Exception:                         # noqa: BLE001
                        raise TranscribeError(
                            f"加载语音模型失败：{type(e).__name__}: {e}\n"
                            "如果模型还没下载，可在设置里换一个档位，或把模型放进程序目录的 models/ 下。") from e
                else:
                    raise TranscribeError(
                        f"加载语音模型失败：{type(e).__name__}: {e}\n"
                        "如果模型还没下载，可在设置里换一个档位，或把模型放进程序目录的 models/ 下。") from e
            _MODEL_CACHE[key] = model
    return model


def _transcribe_builtin(video_path: Path, settings: Settings, language: str) -> list[TranscriptSegment]:
    """进程内转录，不 spawn 子进程。"""
    audio = _load_audio(video_path)
    if audio.size == 0:
        raise TranscribeError("这个视频没有音轨，无法做语音转录")
    model = _get_model(settings)
    # 术语提示：hotwords 对每个解码窗口都生效，是提升专有名词识别率的主要手段；
    # initial_prompt 只定住开头（标点风格、话题），长音频指望它不现实。
    extra: dict = {}
    hot = (getattr(settings, "whisper_hotwords", "") or "").strip()
    if hot:
        extra["hotwords"] = hot.replace("，", ",")
    prompt = (getattr(settings, "whisper_initial_prompt", "") or "").strip()
    if prompt:
        extra["initial_prompt"] = prompt
    try:
        segments_iter, _info = model.transcribe(
            audio, language=language or None, vad_filter=True,
            beam_size=5, condition_on_previous_text=False, **extra)
        out: list[TranscriptSegment] = []
        for i, s in enumerate(segments_iter):
            text = (s.text or "").strip()
            if not text:
                continue
            # Whisper 在没有真人说话的音频上会"编"出常见字幕。实测一段只有背景
            # 音乐的美食视频，它输出了「请不吝点赞 订阅 转发 打赏支持明镜与点点栏目」
            # ——这是训练数据里的模板，不是视频内容。这类幻觉会污染步骤证据，
            # 必须在进 transcript 之前就丢掉。
            if _is_hallucination(text, float(s.start), float(s.end), len(out)):
                continue
            out.append(TranscriptSegment(id=f"t{i}", start=round(float(s.start), 3),
                                         end=round(float(s.end), 3), text=text))
        return out
    except TranscribeError:
        raise
    except Exception as e:                                    # noqa: BLE001
        raise TranscribeError(f"语音转录失败：{type(e).__name__}: {e}") from e

# Whisper 在无语音音频上的典型幻觉。都是"视频结尾的客套话/字幕组署名"，
# 真实教程里也可能出现，但配合"单段时长异常长"就能可靠区分。
_HALLUCINATION_HINTS = (
    "请不吝点赞", "明镜与点点", "点点栏目", "打赏支持", "订阅", "转发",
    "字幕由", "Amara.org", "字幕志愿者", "谢谢观看", "感谢观看", "感谢收看",
    "感谢您的观看", "中文字幕", "翻译 by",
)


def _is_hallucination(text: str, start: float, end: float, index: int) -> bool:
    """判断这段字幕是不是 Whisper 编出来的。

    只认两种同时成立的证据，避免误删真实内容：
    1. 文本命中已知的客套话/署名模板；
    2. 这一段的时长明显不合理（>= 20 秒，正常口语一句 2-8 秒）。
    另外：如果整段音频只转出 1-2 段且全都命中模板，几乎肯定是幻觉。
    """
    t = (text or "").strip()
    if not t:
        return True
    hit = any(h in t for h in _HALLUCINATION_HINTS)
    if not hit:
        return False
    dur = max(0.0, end - start)
    return dur >= 20.0 or index <= 1


def _run_whisper(video_path: Path, srt_path: Path, settings: Settings, language: str) -> None:
    exe = Path(settings.whisper_exe)
    if not exe.exists():
        raise TranscribeError(f"找不到 whisper 程序：{exe}（可在 config.json 里改 whisper_exe）")

    env = dict(os.environ)
    # 这个环境变量如果带着引号会导致 whisper 报 OSError [Errno 22]，必须显式覆盖成干净的路径
    if settings.whisper_cache_home:
        env["HF_HOME"] = settings.whisper_cache_home
    env.setdefault("HF_HUB_OFFLINE", "1")

    cmd = [
        str(exe), str(video_path),
        "--model", settings.whisper_model,
        "--language", language,
        "--output_format", "srt",
        "--output_dir", str(srt_path.parent),
        "--compute_type", settings.whisper_compute_type,
        "--verbose", "False",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=1800, env=env)
    except FileNotFoundError as e:
        raise TranscribeError(f"无法启动 whisper：{e}") from e
    except subprocess.TimeoutExpired as e:
        raise TranscribeError("whisper 转录超时（30 分钟）") from e

    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip()[-800:]
        raise TranscribeError(f"whisper 执行失败（退出码 {proc.returncode}）：{err}")

    # whisper 用视频文件名作为输出名，可能和我们想要的 srt_path 不一致
    if not srt_path.exists():
        candidates = sorted(srt_path.parent.glob("*.srt"), key=lambda p: p.stat().st_mtime)
        if not candidates:
            raise TranscribeError("whisper 执行完成但没有生成 SRT 文件")
        candidates[-1].replace(srt_path)


_SRT_TIME = re.compile(r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})")


def _parse_time(text: str) -> float:
    m = _SRT_TIME.search(text)
    if not m:
        return 0.0
    h, mi, s, ms = m.groups()
    return int(h) * 3600 + int(mi) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000.0


def parse_srt(path: Path) -> list[TranscriptSegment]:
    """解析 SRT。容忍 BOM、CRLF、多余空行。"""
    raw = path.read_text(encoding="utf-8-sig", errors="replace")
    blocks = re.split(r"\n\s*\n", raw.replace("\r\n", "\n").replace("\r", "\n"))
    segments: list[TranscriptSegment] = []
    for block in blocks:
        lines = [ln for ln in block.split("\n") if ln.strip()]
        if len(lines) < 2:
            continue
        time_line_idx = 0 if "-->" in lines[0] else 1
        if time_line_idx >= len(lines) or "-->" not in lines[time_line_idx]:
            continue
        start_s, _, end_s = lines[time_line_idx].partition("-->")
        text = " ".join(lines[time_line_idx + 1:]).strip()
        if not text:
            continue
        segments.append(TranscriptSegment(
            id=f"t{len(segments)}",
            start=round(_parse_time(start_s), 3),
            end=round(_parse_time(end_s), 3),
            text=text,
        ))
    return segments
