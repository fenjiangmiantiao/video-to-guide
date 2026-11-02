# -*- coding: utf-8 -*-
"""把手册导出成"可以离开项目目录"的成品（R7-02）。

两种模式：

- 轻量（默认）：guide.html + guide.md + steps.json + frames/ + manifest.json。
  不复制视频。HTML 在找不到视频时给出「选择本地视频」入口，用户挑一个文件后
  用 createObjectURL 在当前页面临时播放——不上传、不写路径、关掉页面就失效。
- 完整：在轻量基础上把源视频复制到 media/source.mp4，HTML 用相对路径引用它，
  整个文件夹搬到别处也能直接播。

可靠性约定：
- 先在临时目录里生成，全部成功才改名成正式目录；中途失败只清理临时目录。
- 目标目录已存在时自动加 -2 / -3，绝不静默覆盖别人已有的成果。
- 任何引用的截图找不到都算失败，不把缺文件的半成品报成成功。
"""
from __future__ import annotations

import json
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .models import Frame, Step, Transcript, VideoInfo, VisionNote
from .render.html import render_html
from .render.markdown import render_markdown
from .steps.validate import export_blockers
from .store import (
    FRAMES_JSON,
    GUIDE_HTML,
    GUIDE_MD,
    STEPS_JSON,
    TRANSCRIPT_JSON,
    VIDEO_JSON,
    VISION_JSON,
    frame_abs,
    frame_root,
    read_json,
    rp,
)

MANIFEST = "manifest.json"
MEDIA_DIR = "media"
FORMAT = "video-to-guide/manual@1"

MODE_LIGHT = "light"
MODE_FULL = "full"


@dataclass
class ExportResult:
    ok: bool
    mode: str = MODE_LIGHT
    target: Path | None = None
    files: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    error: str = ""
    video_bytes: int = 0

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "mode": self.mode,
            "target": str(self.target or ""),
            "files": self.files,
            "warnings": self.warnings,
            "missing": self.missing,
            "error": self.error,
            "video_bytes": self.video_bytes,
        }


def _load_run(run_dir: Path):
    steps_d = read_json(rp(run_dir, STEPS_JSON)) or {}
    steps = [Step.from_dict(s) for s in steps_d.get("steps", [])]
    video_d = read_json(rp(run_dir, VIDEO_JSON)) or {}
    video = VideoInfo.from_dict(video_d) if video_d else VideoInfo(path="")
    frames_d = read_json(rp(run_dir, FRAMES_JSON)) or {}
    frames = [Frame.from_dict(f) for f in frames_d.get("frames", [])]
    notes_d = read_json(rp(run_dir, VISION_JSON)) or {}
    notes = [VisionNote.from_dict(n) for n in notes_d.get("notes", [])]
    tr_d = read_json(rp(run_dir, TRANSCRIPT_JSON)) or {}
    transcript = Transcript.from_dict(tr_d) if tr_d else Transcript()
    return steps, video, frames, notes, transcript


def _unique_dir(parent: Path, base: str) -> Path:
    """同名时加 -2 / -3，绝不覆盖已有目录。"""
    target = parent / base
    if not target.exists():
        return target
    for i in range(2, 100):
        cand = parent / f"{base}-{i}"
        if not cand.exists():
            return cand
    raise RuntimeError(f"同名导出目录太多：{base}")


def _missing_frames(run_dir: Path, steps: list[Step], frames: list[Frame]) -> list[str]:
    """步骤引用到的截图，必须真实存在于 frames/ 下。"""
    frame_by_id = {f.id: f for f in frames}
    missing: list[str] = []
    for s in steps:
        for fid in s.frame_ids:
            f = frame_by_id.get(fid)
            if f is None:
                missing.append(f"步骤 {s.index} 引用了不存在的截图 id：{fid}")
                continue
            p = Path(f.path)
            if not p.is_absolute():
                p = frame_abs(run_dir, f.path)
            if not p.exists():
                missing.append(f"步骤 {s.index} 的截图文件丢失：{f.path}")
    return missing


def export_manual(
    run_dir: str | Path,
    parent_dir: str | Path,
    mode: str = MODE_LIGHT,
    title: str = "",
    *,
    settings=None,
    video_dir: str = "",
    batch_dir: str = "",
    name: str = "",
    keep_title: bool = True,
) -> ExportResult:
    """导出一份可移植手册。

    video_dir / batch_dir / name 是"这一次手动填的名字"，为空就用设置里的模板。
    settings 传 None 时用默认设置（命令行场景）。
    """
    from .config import Settings
    from .naming import check_path_length, plan as plan_names

    run_dir = Path(run_dir)
    parent = Path(parent_dir)
    mode = MODE_FULL if mode == MODE_FULL else MODE_LIGHT
    res = ExportResult(ok=False, mode=mode)

    if not run_dir.exists():
        res.error = f"运行目录不存在：{run_dir}"
        return res
    if not rp(run_dir, STEPS_JSON).exists():
        res.error = "这个任务还没有生成 steps.json，无法导出"
        return res

    steps, video, frames, notes, transcript = _load_run(run_dir)
    if not steps:
        res.error = "没有步骤可以导出"
        return res

    blockers = export_blockers(steps)
    if blockers:
        res.error = "以下步骤没有任何证据，请先补充或删除：\n" + "\n".join(blockers)
        return res

    missing = _missing_frames(run_dir, steps, frames)
    if missing:
        res.missing = missing
        res.error = "截图缺失，导出中止（不生成缺图的手册）"
        return res

    video_src = Path(video.path) if video.path else None
    if mode == MODE_FULL:
        if not video_src or not video_src.exists():
            res.error = "完整手册包需要源视频，但源视频不存在或已被移动"
            return res
        res.video_bytes = video_src.stat().st_size

    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        res.error = f"导出目录不可用：{e}"
        return res

    # 目录层级与文件名：默认 <根>/<视频>/<批次>/<标题>.html
    settings = settings or Settings.load()
    stem = Path(video.path).stem if video.path else run_dir.name
    names = plan_names(settings, stem, run_dir.name, title or stem,
                       video_dir=video_dir, batch_dir=batch_dir, name=name)
    too_long = check_path_length(parent, names)
    if too_long:
        res.error = too_long
        return res
    if keep_title and not title:
        title = names.html_name[:-5]        # 手册标题跟文件名保持一致

    tmp = parent / f".vtg-export-{uuid.uuid4().hex[:8]}"
    try:
        tmp.mkdir(parents=True)
        # 轻量模式不写视频路径：HTML 会显示"选择本地视频"入口
        video_rel = f"{MEDIA_DIR}/{video_src.name}" if mode == MODE_FULL else ""
        # 导出包本身就是成品目录，不再套一层 output/：guide.html、guide.md、
        # frames/ 直接放在包根，拿到就能双击打开。
        (tmp / names.md_name).write_text(
            render_markdown(steps, frames, notes, transcript, video, run_dir, video_rel or ""),
            encoding="utf-8")
        (tmp / names.html_name).write_text(
            render_html(steps, frames, notes, transcript, video, video_rel, title),
            encoding="utf-8")

        # steps.json 一并带上：别人拿到手册后仍能重新生成、继续编辑
        shutil.copy2(rp(run_dir, STEPS_JSON), tmp / Path(STEPS_JSON).name)

        frames_src = frame_root(run_dir)
        copied: list[str] = []
        if frames_src.exists():
            shutil.copytree(frames_src, tmp / "frames")
            copied = sorted(p.name for p in (tmp / "frames").iterdir() if p.is_file())
            if not copied:
                (tmp / "frames").rmdir()

        if mode == MODE_FULL and video_src:
            media = tmp / MEDIA_DIR
            media.mkdir(exist_ok=True)
            shutil.copy2(video_src, media / video_src.name)

        manifest = {
            "format": FORMAT,
            "schema_version": 2,
            "title": title or Path(video.path).stem or run_dir.name,
            "exported_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "mode": mode,
            "has_video": mode == MODE_FULL,
            "video_file": video_rel,
            # 只放文件名，不放绝对路径：导出包是要分享给别人的
            "source_video": Path(video.path).name if video.path else "",
            "source_run": run_dir.name,
            "steps": len(steps),
            "frames": [f"frames/{n}" for n in copied],
            # 入口文件名（用户改了命名后，别人靠这两个字段找手册）
            "entry_html": names.html_name,
            "entry_md": names.md_name,
            "naming": {"video_dir": names.video_dir, "batch_dir": names.batch_dir},
        }
        (tmp / MANIFEST).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        # 目录层级：<根>/<视频>/<批次>/，批次目录本身就是成品包（不再套一层）。
        # 批次重名（同一秒导出两次）自动 -2/-3，绝不复用旧目录。
        try:
            if names.batch_dir:
                video_home = parent / names.video_dir
                video_home.mkdir(parents=True, exist_ok=True)
                target = _unique_dir(video_home, names.batch_dir)
            else:
                parent.mkdir(parents=True, exist_ok=True)
                target = _unique_dir(parent, names.video_dir)
        except OSError as e:
            raise RuntimeError(f"建目录失败：{e}") from e
        tmp.rename(target)
    except Exception as e:                       # 任何一步失败都不留半成品
        shutil.rmtree(tmp, ignore_errors=True)
        res.error = f"{type(e).__name__}: {e}"
        return res

    res.ok = True
    res.target = target
    res.files = sorted(p.name for p in target.iterdir())
    if mode == MODE_LIGHT and video_src and video_src.exists():
        res.warnings.append(
            "轻量手册不含视频。打开 guide.html 后点「选择本地视频」即可临时播放，"
            "或改用完整手册包。")
    return res
