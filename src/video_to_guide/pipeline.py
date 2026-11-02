"""处理流水线：把各个模块串成一次完整分析。

这是整个程序的骨架。设计要点：

1. **每一步都能单独失败**：某一步失败不会让整个任务崩掉，而是记录到 run_report 并降级继续。
   例如视觉模型没启动，仍然能用字幕生成步骤（只是证据变弱、更多步骤会被标记待核实）。
2. **中间结果落盘**：每步产物都写进 run 目录，重跑时自动复用，不重复消耗模型算力。
3. **进度可观察**：通过 progress 回调把"现在在干什么"抛给界面，CLI 和 GUI 共用同一套逻辑。
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .config import Settings, ensure_dirs
from .models import Frame, RunReport, Step, Transcript, VideoInfo, VisionNote
from .parse.align import build_timeline
from .parse.frames import FramesError, extract_frames
from .parse.probe import ProbeError, probe
from .parse.transcribe import TranscribeError, transcribe
from .render.html import render_html
from .render.markdown import render_markdown
from .steps.generate import generate_steps
from .steps.validate import validate_steps
from .store import (GUIDE_HTML, GUIDE_MD, OUTPUT_DIR, REPORT_JSON, STEPS_JSON,
                    TRANSCRIPT_JSON, VIDEO_JSON, frame_abs, new_run_id, prepare,
                    read_json, rp, write_json, write_text)
from .vision.describe import describe_frames

# 七个处理阶段。界面用 STAGES 画阶段指示器，用 stage 字段知道现在走到哪一步。
# 这些名字是稳定的对外契约，改名字要同步改 GUI。
STAGES = ("probe", "transcribe", "frames", "vision", "align", "steps", "render")
STAGE_LABELS = {
    "probe": "读取视频",
    "transcribe": "语音转录",
    "frames": "提取关键帧",
    "vision": "画面理解",
    "align": "时间对齐",
    "steps": "生成步骤",
    "render": "生成手册",
}

# (阶段说明, 状态: running/ok/warn/fail, 百分比 0-100 或 None, 阶段 id 或 None)
# 百分比按实测耗时加权：转录 18%、抽帧 12%、画面理解 48%、步骤生成 12%，
# 剩下的留给读元信息和渲染。它是按阶段推进估算出来的，不是逐像素的真实进度，
# 所以界面上要同时显示"现在在哪个阶段"，避免用户只盯着一个数字。
ProgressFn = Callable[[str, str, "int | None", "str | None"], None]


@dataclass
class AnalyzeResult:
    run_id: str
    run_path: Path
    report: RunReport
    steps: list[Step] = field(default_factory=list)
    video: VideoInfo | None = None
    frames: list[Frame] = field(default_factory=list)
    notes: list[VisionNote] = field(default_factory=list)
    transcript: Transcript | None = None

    @property
    def ok(self) -> bool:
        return self.report.status in ("ok", "partial")


def _backup_edited_steps(run_path: Path, report: RunReport) -> None:
    """重新分析前，如果发现 steps.json 被人改过，先备份再覆盖。

    GUI 手工编辑会写 edited_at 标记。没有这个标记（纯机器生成）就直接覆盖，
    免得每次重跑都在目录里堆备份。
    """
    path = run_path / STEPS_JSON
    old = read_json(rp(run_path, STEPS_JSON)) or {}
    if not old.get("edited_at"):
        return
    stamp = time.strftime("%Y%m%d-%H%M%S")
    # 备份和 steps.json 放同一个目录（新布局下是 .work/）
    backup = path.parent / f"steps.edited-{stamp}.json"
    try:
        write_json(backup, old)
        report.warnings.append(
            f"这份 steps.json 在 {old['edited_at']} 被手工修改过；"
            f"重新分析会覆盖它，原文件已备份为 {backup.name}")
    except OSError as e:
        report.warnings.append(f"手工修改的步骤备份失败：{e}")


class EditedStepsError(RuntimeError):
    """目标运行目录里的 steps.json 被手工改过，未经确认不能覆盖。"""


def analyze(
    video_path: Path,
    settings: Settings | None = None,
    run_id: str | None = None,
    force: bool = False,
    progress: ProgressFn | None = None,
    allow_overwrite_edits: bool = False,
) -> AnalyzeResult:
    """完整分析一段视频。任何阶段失败都会记录并尽量降级继续。

    allow_overwrite_edits：目标目录里的 steps.json 有手工修改标记时，
    默认直接拒绝（抛 EditedStepsError），必须由调用方明确确认后才覆盖。
    """
    settings = settings or Settings.load()
    ensure_dirs()
    video_path = Path(video_path)

    def emit(msg: str, status: str = "running", pct: int | None = None,
             stage: str | None = None) -> None:
        if progress:
            progress(msg, status, pct, stage)

    def emit_fail(msg: str) -> None:
        emit(msg, "fail")

    run_id = run_id or new_run_id(video_path)
    run_path = prepare(run_id)
    if run_id and not allow_overwrite_edits:
        old_steps = read_json(rp(run_path, STEPS_JSON)) or {}
        if old_steps.get("edited_at"):
            raise EditedStepsError(
                f"运行目录 {run_id} 里的 steps.json 在 {old_steps['edited_at']} 被手工修改过。"
                "继续分析会覆盖这些修改：请在界面上确认，或改用新的运行目录（不要指定 --run-id）。")
    report = RunReport(run_id=run_id, video_path=str(video_path.resolve()),
                       started_at=time.strftime("%Y-%m-%d %H:%M:%S"))
    result = AnalyzeResult(run_id=run_id, run_path=run_path, report=report)

    # ---------- 1. 元信息 ----------
    emit("读取视频信息…", pct=0, stage="probe")
    t0 = time.time()
    try:
        # ffprobe 路径优先用"已安装的可选组件"，其次是设置里的路径和 PATH
        from .components import resolve_ffmpeg
        from .config import PROJECT_ROOT
        _ff, ffprobe_exe = resolve_ffmpeg(
            bool(getattr(settings, "portable_data", False)),
            PROJECT_ROOT, settings.ffprobe_exe)
        video = probe(video_path, ffprobe_exe or settings.ffprobe_exe,
                      backend=getattr(settings, "probe_backend", "auto"))
    except ProbeError as e:
        report.errors.append(f"视频读取失败：{e}")
        report.add_stage("probe", "fail", str(e), time.time() - t0)
        _finish(report, run_path, [], status="failed")
        emit_fail(f"视频读取失败：{e}")
        return result
    result.video = video
    write_json(run_path / VIDEO_JSON, video.to_dict())
    audio_flag = "有" if video.has_audio else "无"
    report.add_stage("probe", "ok",
                     f"{video.width}x{video.height}, {video.duration:.1f}s, 音轨={audio_flag}",
                     time.time() - t0)
    emit(f"视频信息：{video.width}×{video.height}，{video.duration:.1f} 秒", "ok", pct=2,
         stage="probe")

    # ---------- 2. 语音转录 ----------
    emit("语音转录中…（首次运行可能需要几分钟）", pct=2, stage="transcribe")
    t0 = time.time()
    if not video.has_audio:
        result.transcript = Transcript(language=settings.whisper_language, backend="none", segments=[])
        report.warnings.append("视频没有音轨，将只依据画面生成步骤")
        report.add_stage("transcribe", "warn", "无音轨，跳过语音转录", time.time() - t0)
        emit("视频没有音轨，跳过转录", "warn", pct=20, stage="transcribe")
    else:
        try:
            result.transcript = transcribe(video_path, run_path, settings, force=force)
            n = len(result.transcript.segments)
            report.add_stage("transcribe", "ok", f"{n} 条字幕", time.time() - t0)
            emit(f"转录完成：{n} 条字幕", "ok", pct=20, stage="transcribe")
        except TranscribeError as e:
            result.transcript = Transcript(language=settings.whisper_language, backend="failed", segments=[])
            report.warnings.append(f"语音转录失败：{e}（将只依据画面生成步骤）")
            report.add_stage("transcribe", "warn", str(e), time.time() - t0)
            emit(f"转录失败，降级为仅画面分析：{e}", "warn", pct=20,
                 stage="transcribe")

    # ---------- 3. 抽关键帧 ----------
    emit("提取关键截图…", pct=20, stage="frames")
    t0 = time.time()
    srt = (run_path / TRANSCRIPT_JSON).with_suffix(".srt")
    # 只有真的转出字幕才把 srt 交给抽帧工具；否则让它用占位字幕跳过内置 STT。
    has_subtitles = bool(result.transcript and result.transcript.segments)
    try:
        result.frames = extract_frames(video_path, run_path, settings,
                                       srt_path=srt if has_subtitles else None, force=force)
        report.add_stage("frames", "ok", f"{len(result.frames)} 张截图", time.time() - t0)
        # 这一步之后才开始真正耗时的视觉理解。帧数已经确定，把预计耗时说出来，
        # 用户能判断"要不要现在等"，而不是盯着进度条猜还有多久。
        est = estimate_vision_seconds(len(result.frames), settings)
        emit(f"提取到 {len(result.frames)} 张截图（画面理解预计 {human_seconds(est)}）",
             "ok", pct=32, stage="frames")
    except FramesError as e:
        detail = str(e)
        # 抽帧失败最常见的原因是文件本身坏了（录制中断、拷贝不完整）。
        # 用户看到韩文/英文的解码错误没有意义，这里补一句人话提示。
        if "退出码" in detail or "decode" in detail.lower() or "invalid" in detail.lower():
            detail += ("\n提示：多半是视频文件损坏或不完整（比如录制中途被强制结束、"
                       "网盘只下了一半）。请先用播放器从头到尾播一遍确认。")
        report.errors.append(f"抽取截图失败：{detail}")
        report.add_stage("frames", "fail", str(e), time.time() - t0)
        emit_fail("抽取截图失败：" + detail.splitlines()[0])
        _finish(report, run_path, [], status="failed")
        return result

    if not result.frames:
        report.errors.append("没有检测到任何画面变化，无法生成步骤")
        _finish(report, run_path, [], status="failed")
        emit_fail("没有检测到画面变化")
        return result

    # ---------- 4. 画面理解 ----------
    emit(f"用本地视觉模型理解 {len(result.frames)} 张截图…"
         f"（预计 {human_seconds(estimate_vision_seconds(len(result.frames), settings))}）",
         pct=32, stage="vision")
    t0 = time.time()

    def vision_progress(done: int, total: int, note) -> None:
        state = "ok" if note.ok else "warn"
        pct = 32 + int(48 * done / total) if total else 32
        emit(f"画面理解 {done}/{total}", state, pct=pct, stage="vision")

    if not settings.enable_vision:
        # 明确的"不看画面"模式：一个视觉请求都不发，也不假装看过画面
        result.notes = []
        report.warnings.append("已跳过画面理解（--no-vision），只依据字幕生成步骤")
        report.add_stage("vision", "warn", "已跳过", time.time() - t0)
        emit("已跳过画面理解（--no-vision）", "warn", pct=80, stage="vision")
    else:
        vision_stats: dict = {}
        result.notes = describe_frames(
            result.frames, run_path, settings,
            image_of=lambda f: frame_abs(run_path, f.path),
            force=force, progress=vision_progress, stats=vision_stats,
        )
        failed = [n for n in result.notes if not n.ok]
        if failed and len(failed) == len(result.notes):
            report.warnings.append(
                f"所有截图都没能识别（{failed[0].error}），将只依据字幕生成步骤")
            report.add_stage("vision", "warn", f"全部失败：{failed[0].error}",
                             time.time() - t0)
            emit("视觉模型不可用，降级为仅字幕分析", "warn", pct=80, stage="vision")
        elif failed:
            report.warnings.append(f"{len(failed)}/{len(result.notes)} 张截图识别失败")
            report.add_stage("vision", "warn", f"{len(failed)} 张失败", time.time() - t0)
            emit(f"画面理解完成，{len(failed)} 张失败", "warn", pct=80,
                 stage="vision")
        else:
            _reuse = vision_stats.get("reused", 0)
            _detail = f"{len(result.notes)} 张全部成功"
            if _reuse:
                _detail += (f"（复用上次 {_reuse} 张，"
                            f"新处理 {vision_stats.get('described', 0)} 张）")
            report.add_stage("vision", "ok", _detail, time.time() - t0)
            emit(f"画面理解完成：{len(result.notes)} 张", "ok", pct=80, stage="vision")

    # ---------- 5. 时间对齐 ----------
    emit("对齐字幕与画面…", pct=80, stage="align")
    t0 = time.time()
    timeline = build_timeline(result.frames, result.transcript, result.notes, run_path)
    screen_count = timeline["screen_count"]
    report.add_stage("align", "ok", f"{screen_count} 屏", time.time() - t0)
    emit(f"对齐完成：{screen_count} 屏画面", "ok", pct=84, stage="align")

    # ---------- 6. 生成步骤 ----------
    # 现实模式先判断"这是不是操作教学"：吃播、探店、开箱这类视频里动作很多，
    # 但观众照着做不出来。不先拦掉，就会生成一份看似完整、实际没用的手册。
    if (getattr(settings, "video_mode", "software") or "software").lower() == "reality":
        from .steps.classify import classify_video
        emit("判断这个视频是不是操作教学…", pct=84, stage="steps")
        verdict, why = classify_video(result.transcript, result.notes, settings)
        if verdict is False:
            msg = (f"这个视频不像操作教学，没有生成手册：{why}" if why
                   else "这个视频不像操作教学，没有生成手册。")
            report.errors.append(msg)
            report.add_stage("steps", "fail", "非操作教学视频", 0.0)
            _finish(report, run_path, [], status="failed")
            emit_fail("没有生成手册：这段视频不像操作教学（吃播/展示/记录类）")
            return result
        if verdict is None:
            report.warnings.append(f"无法判断是否为操作教学，按教学继续：{why}")

    emit("生成操作步骤…", pct=84, stage="steps")
    t0 = time.time()
    steps, gen_warnings = generate_steps(
        result.frames, result.transcript, result.notes, timeline,
        settings, video.duration,
        progress=lambda msg, status="running": emit(msg, status, pct=88, stage="steps"),
    )
    report.warnings.extend(gen_warnings)

    # ---------- 7. 证据校验 ----------
    steps, val_warnings = validate_steps(
        steps, result.frames, result.transcript, result.notes, video.duration,
        video_mode=getattr(settings, "video_mode", "software"))
    report.warnings.extend(val_warnings)
    result.steps = steps
    report.add_stage("steps", "ok" if steps else "warn",
                     f"{len(steps)} 个步骤", time.time() - t0)
    emit(f"生成 {len(steps)} 个步骤", "ok" if steps else "warn", pct=96, stage="steps")

    _backup_edited_steps(run_path, report)
    write_json(run_path / STEPS_JSON, {"steps": [s.to_dict() for s in steps]})

    # ---------- 8. 生成文档 ----------
    # 占位步骤（"第 N 段画面"）不进手册：它不是整理出来的操作，留在正文里只会
    # 让手册看起来像写完了。数量记进报告，让用户知道有多少屏没整理出来。
    doc_steps = [s for s in steps if not s.placeholder]
    dropped = len(steps) - len(doc_steps)
    if dropped:
        report.warnings.append(f"有 {dropped} 屏没有整理出操作，未写入手册")
    emit("生成手册…", pct=96, stage="render")
    t0 = time.time()
    video_rel = _rel_video(video_path, run_path)
    try:
        md = render_markdown(doc_steps, result.frames, result.notes, result.transcript,
                             video, run_path, video_rel)
        write_text(run_path / GUIDE_MD, md)
        html_text = render_html(doc_steps, result.frames, result.notes, result.transcript,
                                video, video_rel)
        write_text(run_path / GUIDE_HTML, html_text)
        report.add_stage("render", "ok", "guide.md + guide.html", time.time() - t0)
        emit("手册已生成", "ok", pct=100, stage="render")
    except Exception as e:
        report.errors.append(f"生成文档失败：{e}")
        report.add_stage("render", "fail", str(e), time.time() - t0)
        emit_fail(f"生成文档失败：{e}")

    # 全是占位步骤 = 这次分析失败了。以前这种结果会被报成 "partial" 并照常导出
    # 一份「第 N 段画面」的手册，用户以为成功了 —— 这是假成功，必须显式失败。
    real = [s for s in steps if not s.placeholder]
    # 注意用 not real 而不是 (steps and not real)：
    # 一条步骤都没生成时，前者同样成立，不该被漏掉。
    if not real:
        if steps:
            report.errors.append(
                f"没有整理出操作步骤：{len(steps)} 条内容全部是按画面分段的占位结果。"
                "多半是这段视频里没有可识别的操作，或画面/语音证据不足。")
            report.add_stage("steps", "fail", "全部为占位步骤", 0.0)
        else:
            report.errors.append(
                "没有生成任何操作步骤：模型未能从画面和语音里提取出可复现的操作。")
            report.add_stage("steps", "fail", "0 个步骤", 0.0)

    status = "ok"
    if report.errors or report.warnings:
        status = "partial"
    if not real:
        status = "failed"
    _finish(report, run_path, steps, status=status)
    if status == "failed":
        emit_fail("没有整理出操作步骤（结果不可用，详见运行日志）")
    else:
        emit(f"完成：{len(steps)} 个步骤", "ok" if status == "ok" else "warn", pct=100,
             stage="render")
    return result


def _rel_video(video_path: Path, run_path: Path) -> str:
    """算出从**手册所在目录**指向源视频的相对路径，供 HTML 播放器使用。

    手册现在放在 run_path/output/ 下，所以要相对 output/ 算。
    以前相对 run 根目录算，生成出来是 ../testvideo/xxx.mp4，从 output/ 打开
    就变成 runs/<id>/testvideo/xxx.mp4 —— 路径不存在，播放器一片黑。
    """
    from .store import OUTPUT_DIR
    try:
        base = run_path / OUTPUT_DIR
        if not base.is_dir():
            base = run_path
        rel = os.path.relpath(str(video_path.resolve()), str(base.resolve()))
        return Path(rel).as_posix()
    except Exception:
        return ""


def _finish(report: RunReport, run_path: Path, steps: list[Step], status: str) -> None:
    report.status = status
    report.finished_at = time.strftime("%Y-%m-%d %H:%M:%S")
    report.steps_count = len(steps)
    report.needs_review_count = sum(1 for s in steps if s.needs_review)
    if status == "failed":
        _quarantine_stale_docs(run_path, report)
    write_json(run_path / REPORT_JSON, report.to_dict())


def _quarantine_stale_docs(run_path: Path, report: RunReport) -> None:
    """本次分析失败时，把上一次留下的手册改名挪开。

    实测踩到的坑：同一个 run 目录先成功过一次（留下了 guide.md），再跑一次
    因为「不是操作教学」失败——旧 guide.md 还躺在 output/ 里，用户打开一看
    是一份完整手册，完全不知道这次其实失败了。失败就必须让旧产物不能再被
    当成本次结果。
    """
    out = run_path / OUTPUT_DIR
    if not out.is_dir():
        return
    stamp = time.strftime("%Y%m%d-%H%M%S")
    moved: list[str] = []
    for name in ("guide.md", "guide.html"):
        src = out / name
        if not src.exists():
            continue
        dst = out / f"{src.stem}.stale-{stamp}{src.suffix}"
        try:
            src.replace(dst)
            moved.append(dst.name)
        except OSError:
            pass
    try:
        text = ["本次分析失败，没有生成新的操作手册。", ""]
        for e in report.errors:
            text.append(f"原因：{e}")
        if moved:
            text.append("")
            text.append("上一次分析留下的手册已改名为：" + "、".join(moved))
            text.append("它不是本次分析的结果，请勿使用。")
        (out / "分析失败.txt").write_text("\n".join(text), encoding="utf-8")
    except OSError:
        pass
    if moved:
        report.warnings.append(
            "本次分析失败，旧手册已改名（" + "、".join(moved) + "），避免误当成本次结果")


# ------------------------------------------------------------------ 耗时预估

# 单帧视觉调用的实测耗时（RTX 4060 Ti + 本地视觉模型 + 768px 长边）。
# 这是给用户看数量级的，不是精确计时：模型、显卡、图片尺寸都会影响。
_SEC_PER_FRAME = 5.0


def estimate_vision_seconds(n_frames: int, settings) -> float:
    """估算 N 张截图全部过一遍视觉模型要多久。

    图片长边越大，视觉模型看得越细也越慢；超过 1024 时按 7 秒/帧算。
    另外加 20 秒固定开销（加载模型、建连接）。
    """
    per = 7.0 if int(getattr(settings, "read_long_edge", 768) or 768) > 1024 else _SEC_PER_FRAME
    return max(0, n_frames) * per + 20.0


def human_seconds(sec: float) -> str:
    """把秒数说成人话：90 秒 -> 约 1.5 分钟；4000 秒 -> 约 1 小时 7 分。"""
    sec = max(0.0, float(sec))
    if sec < 60:
        return f"约 {int(round(sec))} 秒"
    minutes = sec / 60.0
    if minutes < 60:
        return f"约 {minutes:.0f} 分钟" if minutes >= 10 else f"约 {minutes:.1f} 分钟"
    h = int(minutes // 60)
    m = int(round(minutes - h * 60))
    return f"约 {h} 小时 {m} 分" if m else f"约 {h} 小时"

