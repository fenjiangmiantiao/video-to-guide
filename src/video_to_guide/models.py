"""核心数据结构。

设计原则：
1. 模块之间只传这里的 dataclass，不传裸 dict，避免字段名写错。
2. 每个结构都能 to_dict() / from_dict()，直接对应磁盘上的 JSON 文件。
3. 每个"证据来源"都有稳定 id（字幕 t0/t1、截图 f0/f1、画面描述 v0/v1），
   步骤通过 id 引用证据，保证"这句话有据可查"。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


def _get(d: dict, key: str, default: Any = None) -> Any:
    return d[key] if key in d else default


# ---------------------------------------------------------------- 视频元信息
@dataclass
class VideoInfo:
    """ffprobe 读出来的视频事实。"""

    path: str
    duration: float = 0.0
    width: int = 0
    height: int = 0
    fps: float = 0.0
    has_audio: bool = False
    audio_codec: str | None = None
    audio_channels: int | None = None
    audio_sample_rate: int | None = None
    size_bytes: int = 0

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "duration": self.duration,
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "has_audio": self.has_audio,
            "audio_codec": self.audio_codec,
            "audio_channels": self.audio_channels,
            "audio_sample_rate": self.audio_sample_rate,
            "size_bytes": self.size_bytes,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "VideoInfo":
        # 历史记录可能是旧版本写的、或者文件被手工改过，这里逐字段兜底，
        # 避免 duration=None 之类的问题一路传下去把界面搞崩。
        d = d or {}
        return cls(
            path=d.get("path") or "",
            duration=float(d.get("duration") or 0.0),
            width=int(d.get("width") or 0),
            height=int(d.get("height") or 0),
            fps=float(d.get("fps") or 0.0),
            has_audio=bool(d.get("has_audio")),
            audio_codec=d.get("audio_codec"),
            audio_channels=d.get("audio_channels"),
            audio_sample_rate=d.get("audio_sample_rate"),
            size_bytes=int(d.get("size_bytes") or 0),
        )


# ------------------------------------------------------------------- 字幕
@dataclass
class TranscriptSegment:
    """一句字幕：说话的时间段和内容。"""

    id: str
    start: float
    end: float
    text: str

    def to_dict(self) -> dict:
        return {"id": self.id, "start": self.start, "end": self.end, "text": self.text}

    @classmethod
    def from_dict(cls, d: dict) -> "TranscriptSegment":
        return cls(id=d["id"], start=float(d["start"]), end=float(d["end"]), text=d["text"])


@dataclass
class Transcript:
    """整段语音转录结果。backend 记录它是怎么来的（whisper / 用户提供的 srt）。"""

    language: str = "zh"
    backend: str = "unknown"
    segments: list[TranscriptSegment] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "language": self.language,
            "backend": self.backend,
            "segments": [s.to_dict() for s in self.segments],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Transcript":
        d = d or {}
        return cls(
            language=d.get("language", "zh"),
            backend=d.get("backend", "unknown"),
            segments=[TranscriptSegment.from_dict(s) for s in d.get("segments", [])],
        )

    def text_between(self, start: float, end: float) -> str:
        """取出与 [start, end] 有重叠的所有字幕，拼成一段话。"""
        parts = []
        for s in self.segments:
            if s.end >= start and s.start <= end:
                parts.append(s.text.strip())
        return " ".join(p for p in parts if p)


# ------------------------------------------------------------------- 截图
@dataclass
class Frame:
    """一张关键截图。time 是它在视频里出现的时间点，end 是这一屏持续到什么时候。"""

    id: str
    time: float
    end: float
    path: str          # 相对 run 目录的路径，如 frames/scene_000_t0000.50.jpg
    width: int = 0
    height: int = 0
    screen_index: int = 0
    change_score: float = 0.0   # 场景检测给出的画面变化强度（0 表示首帧）
    transcript_ids: list[str] = field(default_factory=list)
    # 这一屏是"目标窗口内的局部变化"还是"整屏切换窗口"。
    # 整屏切换既可能是真操作（打开目标软件），也可能只是切到别的窗口看资料，
    # 单看像素分不出来，所以把它作为线索传给步骤生成，而不是直接丢弃。
    kind: str = ""              # local / window_switch / change / ""

    def to_dict(self) -> dict:
        return {
            "id": self.id, "time": self.time, "end": self.end, "path": self.path,
            "width": self.width, "height": self.height,
            "screen_index": self.screen_index, "change_score": self.change_score,
            "transcript_ids": self.transcript_ids, "kind": self.kind,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Frame":
        return cls(
            id=d["id"], time=float(d["time"]), end=float(d["end"]), path=d["path"],
            width=int(d.get("width", 0)), height=int(d.get("height", 0)),
            screen_index=int(d.get("screen_index", 0)),
            change_score=float(d.get("change_score", 0.0)),
            transcript_ids=list(d.get("transcript_ids", [])),
            kind=str(d.get("kind") or ""),
        )


# --------------------------------------------------------------- 画面描述
@dataclass
class VisionNote:
    """视觉模型对一张截图的观察。只记录"看见了什么"，不记录"推测做了什么"。"""

    id: str
    frame_id: str
    interface: str = ""        # 界面是什么
    visible_text: str = ""     # 可见文字
    activity: str = ""         # 操作迹象
    raw: str = ""              # 模型原始输出，便于复核
    error: str = ""            # 调用失败时的原因
    elapsed: float = 0.0

    def to_dict(self) -> dict:
        return {
            "id": self.id, "frame_id": self.frame_id, "interface": self.interface,
            "visible_text": self.visible_text, "activity": self.activity,
            "raw": self.raw, "error": self.error, "elapsed": self.elapsed,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "VisionNote":
        return cls(
            id=d["id"], frame_id=d["frame_id"], interface=d.get("interface", ""),
            visible_text=d.get("visible_text", ""), activity=d.get("activity", ""),
            raw=d.get("raw", ""), error=d.get("error", ""),
            elapsed=float(d.get("elapsed", 0.0)),
        )

    @property
    def ok(self) -> bool:
        return not self.error


# ------------------------------------------------------------------- 步骤
@dataclass
class StepEvidence:
    """一个步骤凭什么成立：字幕 id + 画面描述 id + 截图 id。"""

    transcript_ids: list[str] = field(default_factory=list)
    vision_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"transcript_ids": self.transcript_ids, "vision_ids": self.vision_ids}

    @classmethod
    def from_dict(cls, d: dict) -> "StepEvidence":
        return cls(transcript_ids=list(d.get("transcript_ids", [])),
                   vision_ids=list(d.get("vision_ids", [])))

    @property
    def empty(self) -> bool:
        return not self.transcript_ids and not self.vision_ids


@dataclass
class Step:
    """手册里的一个操作步骤。

    字段含义与任务书第六节一致。needs_review=True 表示"AI 不确定，需要人看一眼"，
    这类步骤在 HTML 里会被显著标出，导出时也会统计。
    """

    index: int
    t_start: float
    t_end: float
    title: str
    action: str
    expected_result: str = ""
    frame_ids: list[str] = field(default_factory=list)
    evidence: StepEvidence = field(default_factory=StepEvidence)
    needs_review: bool = False
    confidence: float = 0.0
    review_note: str = ""      # 为什么需要复核（给人看的一句话）
    # 要求复核的机器可读原因。比单个 confidence 有用得多：
    # "8 个步骤全标待核实"和"只有这 2 条因为动作不可见才标"是两种完全不同的处境。
    review_reasons: list[str] = field(default_factory=list)
    # 这条步骤是"模型没成功、按画面分段顶上的占位内容"，不是真整理出来的操作。
    # 手册里必须能识别出来，否则用户会以为分析成功了。
    placeholder: bool = False

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "t_start": self.t_start,
            "t_end": self.t_end,
            "title": self.title,
            "action": self.action,
            "expected_result": self.expected_result,
            "frame_ids": self.frame_ids,
            "evidence": self.evidence.to_dict(),
            "needs_review": self.needs_review,
            "confidence": self.confidence,
            "review_note": self.review_note,
            "review_reasons": self.review_reasons,
            "placeholder": self.placeholder,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Step":
        return cls(
            index=int(d["index"]),
            t_start=float(d["t_start"]),
            t_end=float(d["t_end"]),
            title=d.get("title", ""),
            action=d.get("action", ""),
            expected_result=d.get("expected_result", ""),
            frame_ids=list(d.get("frame_ids", [])),
            evidence=StepEvidence.from_dict(d.get("evidence", {})),
            needs_review=bool(d.get("needs_review", False)),
            confidence=float(d.get("confidence", 0.0)),
            review_note=d.get("review_note", ""),
            review_reasons=list(d.get("review_reasons", [])),
            placeholder=bool(d.get("placeholder", False)),
        )


# --------------------------------------------------------------- 运行报告
@dataclass
class RunReport:
    """一次处理的体检报告：哪些成功、哪些降级、哪些失败。"""

    run_id: str
    video_path: str
    started_at: str = ""
    finished_at: str = ""
    status: str = "running"       # running / ok / partial / failed
    steps_count: int = 0
    needs_review_count: int = 0
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    stages: list[dict] = field(default_factory=list)   # [{name, status, detail, seconds}]

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "video_path": self.video_path,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "status": self.status,
            "steps_count": self.steps_count,
            "needs_review_count": self.needs_review_count,
            "warnings": self.warnings,
            "errors": self.errors,
            "stages": self.stages,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "RunReport":
        return cls(
            run_id=d.get("run_id", ""),
            video_path=d.get("video_path", ""),
            started_at=d.get("started_at", ""),
            finished_at=d.get("finished_at", ""),
            status=d.get("status", "running"),
            steps_count=int(d.get("steps_count", 0)),
            needs_review_count=int(d.get("needs_review_count", 0)),
            warnings=list(d.get("warnings", [])),
            errors=list(d.get("errors", [])),
            stages=list(d.get("stages", [])),
        )

    def add_stage(self, name: str, status: str, detail: str = "", seconds: float = 0.0) -> None:
        self.stages.append({"name": name, "status": status, "detail": detail,
                            "seconds": round(seconds, 2)})
