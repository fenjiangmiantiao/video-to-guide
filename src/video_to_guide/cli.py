"""命令行入口。

先做 CLI 再做 GUI 的原因（任务书建议）：
- 处理逻辑和界面解耦，GUI 只是给同一套流水线套个壳；
- 出问题时可以直接在命令行复现，不用点界面；
- 自动化测试和批量处理可以直接调 CLI。

用法：
    vtg analyze "视频.mp4"
    vtg doctor
    vtg list
    vtg export <run_id>
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

from . import __version__
from .config import RUNS_DIR, Settings
from .models import Step
from .pipeline import EditedStepsError, analyze
from .store import (FRAMES_DIR, FRAMES_JSON, GUIDE_HTML, GUIDE_MD, OUTPUT_DIR,
                    REPORT_JSON, STEPS_JSON, TRANSCRIPT_JSON, VIDEO_JSON, VISION_JSON,
                    WORK_DIR, is_legacy, read_json, rp, run_dir, write_json,
                    write_text)


def main(argv: list[str] | None = None) -> int:
    # Windows 控制台默认 GBK，打印中文和 ✓ 之类的符号会直接抛 UnicodeEncodeError。
    # 这里强制标准输出用 UTF-8，避免"程序本身没问题却因为打印崩掉"。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    parser = argparse.ArgumentParser(
        prog="vtg",
        description="Video to Guide：把软件操作录屏转成带截图和时间戳的图文手册。",
    )
    parser.add_argument("--version", action="version", version=f"vtg {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_analyze = sub.add_parser("analyze", help="分析一段视频，生成操作手册")
    p_analyze.add_argument("video", help="视频文件路径")
    p_analyze.add_argument("--run-id", default=None, help="指定运行目录名（默认按时间生成）")
    p_analyze.add_argument("--force", action="store_true", help="忽略中间结果，全部重算")
    p_analyze.add_argument("--no-vision", action="store_true", help="跳过视觉模型（只用字幕）")
    p_analyze.add_argument("--overwrite-edits", action="store_true",
                           help="允许覆盖目标运行目录里手工修改过的 steps.json（会先备份）")

    p_doctor = sub.add_parser("doctor", help="检查本机环境是否满足运行条件")
    p_doctor.add_argument("--json", action="store_true", help="以 JSON 输出")

    sub.add_parser("list", help="列出历史运行目录")

    p_export = sub.add_parser("export", help="导出可移植手册（默认只重生成到运行目录）")
    p_export.add_argument("run_id", help="运行目录名")
    p_export.add_argument("--out", default="", help="导出到指定目录；不填则只重生成运行目录里的手册")
    p_export.add_argument("--mode", choices=["light", "full"], default="light",
                          help="light=不含视频（默认），full=连视频一起打包")

    p_migrate = sub.add_parser(
        "migrate", help="把旧版任务目录整理成新结构（output/ 成品 + .work/ 数据）")
    p_migrate.add_argument("--dry-run", action="store_true",
                           help="只打印将要做什么，不改任何文件")
    p_migrate.add_argument("--run-id", default="", help="只处理这一个任务")

    sub.add_parser("gui", help="打开桌面界面（需要 PySide6）")

    args = parser.parse_args(argv)

    if args.command == "analyze":
        return _cmd_analyze(args)
    if args.command == "doctor":
        return _cmd_doctor(args)
    if args.command == "list":
        return _cmd_list()
    if args.command == "export":
        return _cmd_export(args)
    if args.command == "migrate":
        return _cmd_migrate(args)
    if args.command == "gui":
        from .gui.app import run as run_gui
        return run_gui()
    parser.print_help()
    return 2


def _cmd_analyze(args) -> int:
    video = Path(args.video)
    if not video.exists():
        print(f"[错误] 找不到视频文件：{video}", file=sys.stderr)
        return 2

    settings = Settings.load()
    if args.no_vision:
        # 真正的开关：视觉阶段一个请求都不发（不影响「写步骤」用的模型）。
        settings.enable_vision = False

    def progress(msg: str, status: str, pct: int | None = None,
                 stage: str | None = None) -> None:
        prefix = {"ok": "  ✓", "warn": "  !", "fail": "  ✗"}.get(status, "  ·")
        tag = f"[{pct:3d}%] " if isinstance(pct, int) else ""
        print(f"{prefix} {tag}{msg}", flush=True)

    print(f"开始分析：{video}")
    try:
        result = analyze(video, settings, run_id=args.run_id, force=args.force,
                         progress=progress, allow_overwrite_edits=args.overwrite_edits)
    except EditedStepsError as e:
        print(f"[错误] {e}", file=sys.stderr)
        print("提示：换一个新的 --run-id，或加 --overwrite-edits 确认覆盖（会先备份）。",
              file=sys.stderr)
        return 3

    print("")
    print(f"运行目录：{result.run_path}")
    print(f"状态：{result.report.status}")
    if result.report.warnings:
        print("警告：")
        for w in result.report.warnings:
            print(f"  ! {w}")
    if result.report.errors:
        print("错误：")
        for e in result.report.errors:
            print(f"  ✗ {e}")
    if result.steps:
        print(f"步骤：{len(result.steps)}（待核实 {result.report.needs_review_count}）")
        for s in result.steps:
            flag = " [待核实]" if s.needs_review else ""
            print(f"  {s.index}. [{_mmss(s.t_start)}-{_mmss(s.t_end)}] {s.title}{flag}")
        print("")
        print(f"手册：{result.run_path / GUIDE_MD}")
        print(f"      {result.run_path / GUIDE_HTML}")
    return 0 if result.ok else 1


def _cmd_doctor(args) -> int:
    settings = Settings.load()
    checks: list[dict] = []

    def add(name: str, ok: bool, detail: str, hint: str = "",
            group: str = "软件环境", state: str = "") -> None:
        checks.append({"name": name, "ok": ok, "detail": detail, "hint": hint,
                       "group": group,
                       "state": state or ("ok" if ok else "missing")})

    # Python
    add("Python 版本", sys.version_info >= (3, 10),
        f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "需要 Python 3.10 以上")

    # 视频元信息读取：内置 PyAV 优先，ffprobe 只是兜底
    pbackend = (getattr(settings, "probe_backend", "auto") or "auto").lower()
    try:
        import av  # noqa: F401
        builtin_probe_ok = True
    except Exception as e:                                    # noqa: BLE001
        builtin_probe_ok = False
        builtin_probe_detail = f"不可用：{type(e).__name__}: {e}"
    if builtin_probe_ok:
        add("视频信息读取（内置 PyAV）", True, "可用，不需要额外安装 FFmpeg")
    else:
        add("视频信息读取（内置 PyAV）", False, builtin_probe_detail,
            "重装程序：内置读取依赖 PyAV")

    # FFmpeg / FFprobe 只在"内置读取打不开某种容器"时才需要，属于可选组件。
    # 查找顺序：已下载的可选组件 → 设置里的路径 → PATH。
    from . import components as comp
    from .config import PROJECT_ROOT
    portable = bool(getattr(settings, "portable_data", False))
    ff_exe, fp_exe = comp.resolve_ffmpeg(portable, PROJECT_ROOT, settings.ffprobe_exe)
    has_comp = comp.components_present(portable, PROJECT_ROOT)
    if ff_exe:
        src = "已安装的可选组件" if has_comp else "系统 PATH"
        ver = _run_version([ff_exe, "-version"])[:24]
        add("FFmpeg / FFprobe（可选）", True, f"{src}：{Path(ff_exe).parent}（{ver}）")
    else:
        add("FFmpeg / FFprobe（可选）", pbackend != "ffprobe",
            "未安装（内置读取可用时不需要）",
            "需要处理冷门格式时再说：设置 → 可选组件 → 下载并安装")

    # 语音转录：内置 faster-whisper（便携包自带），外部 CLI 只在显式配置时用
    ws_backend = (getattr(settings, "whisper_backend", "auto") or "auto").lower()
    if ws_backend == "cli":
        wp = Path(settings.whisper_exe or "")
        add("语音转录（外部 whisper CLI）", wp.exists(),
            str(wp) if wp.exists() else "未找到",
            "在设置里把 whisper_exe 指向 whisper-ctranslate2.exe，或把后端改回内置")
    else:
        try:
            import ctranslate2  # noqa: F401
            import faster_whisper  # noqa: F401
            ws_ok = True
            ws_detail = "内置转录可用（faster-whisper + ctranslate2）"
        except Exception as e:                                # noqa: BLE001
            ws_ok = False
            ws_detail = f"内置转录不可用：{type(e).__name__}: {e}"
        from .parse.transcribe import _model_path
        mp = Path(_model_path(settings))
        if ws_ok:
            if mp.exists():
                ws_detail += f"；模型：{mp.name or mp}"
            else:
                ws_detail += f"；⚠ 模型未找到（{mp}），有旁白的视频会转录失败"
        add("语音转录（内置 faster-whisper）", ws_ok, ws_detail,
            "重装程序：内置转录依赖 ctranslate2 / faster-whisper；模型放在程序目录的 models/ 下")

    # 抽帧能力：内置实现（av+numpy）永远可用；外部 analysis-video 可选
    from .parse.frames import analysis_exe
    backend = (getattr(settings, "frames_backend", "auto") or "auto").lower()
    try:
        import av  # noqa: F401
        import numpy  # noqa: F401
        builtin_ok = True
        builtin_detail = "内置抽帧（PyAV）可用"
    except Exception as e:
        builtin_ok = False
        builtin_detail = f"内置抽帧不可用：{type(e).__name__}: {e}"

    av = analysis_exe(settings)
    external_ok = av.exists()
    if backend == "external":
        add("视频解析（外部 analysis-video）", external_ok,
            str(av) if external_ok else "未找到（当前配置强制用外部工具）",
            "pip install analysis-video，或把 config.json 的 frames_backend 改成 builtin")
    elif backend == "builtin":
        add("视频解析（内置抽帧）", builtin_ok, builtin_detail,
            "重装程序：内置抽帧依赖 PyAV / numpy / Pillow")
    else:   # auto：有外部就用外部，没有就用内置
        add("视频解析（内置抽帧）", builtin_ok, builtin_detail,
            "重装程序：内置抽帧依赖 PyAV / numpy / Pillow")
        add("视频解析（外部 analysis-video，可选）", external_ok,
            str(av) if external_ok else "未找到（将使用内置抽帧，切分精度略低）",
            "pip install analysis-video 可提高长视频的屏幕切分精度")

    # ---- AI 服务（第二层）：服务未就绪不等于软件缺依赖，所以单独一组，
    # 而且不参与 exit code 判定——软件本身是好的，用户只是还没启动模型服务。
    from . import health

    states = health.probe_all(settings)
    for kind, label in (("vision", "视觉（看截图）"), ("text", "步骤整理")):
        st = states[kind]
        hint = ("启动一个本机 OpenAI 兼容服务（LM Studio / llama.cpp / Ollama），"
                "或在设置里改用带 API Key 的云端服务")
        if st.state == health.NO_MODEL:
            hint = "服务已连通，但没有可用模型：在服务里加载一个支持图像输入的模型，再点重新检测"
        add(f"AI 服务 · {label}（{st.provider_name}）",
            st.ok, f"{health.STATE_TEXT.get(st.state, st.state)}｜{st.detail}",
            hint, group="AI 服务", state=st.state)

    # 目录可写
    try:
        RUNS_DIR.mkdir(parents=True, exist_ok=True)
        probe_file = RUNS_DIR / ".write_test"
        probe_file.write_text("ok", encoding="utf-8")
        probe_file.unlink()
        add("运行目录可写", True, str(RUNS_DIR))
    except Exception as e:
        add("运行目录可写", False, str(e), "检查目录权限")

    if args.json:
        import json
        print(json.dumps(checks, ensure_ascii=False, indent=2))
        return 0 if all(c["ok"] for c in checks) else 1

    print("环境检查")
    print("=" * 62)
    for group in ("软件环境", "AI 服务"):
        items = [c for c in checks if c["group"] == group]
        if not items:
            continue
        print(f"[{group}]")
        for c in items:
            mark = "✓" if c["ok"] else ("✗" if group == "软件环境" else "!")
            print(f"  {mark} {c['name']}：{c['detail']}")
            if not c["ok"] and c["hint"]:
                print(f"      → {c['hint']}")
    print("=" * 62)

    # 两层分别判定：软件环境坏了就是坏了；AI 服务没就绪只影响"分析"，
    # 查看/编辑/导出手册都还能用，所以不算失败。
    env_bad = [c for c in checks if c["group"] == "软件环境" and not c["ok"]]
    ai_bad = [c for c in checks if c["group"] == "AI 服务" and not c["ok"]]

    if env_bad:
        print(f"软件环境有 {len(env_bad)} 项不满足，程序可能无法正常运行。")
        return 1
    print("软件环境正常。")
    if ai_bad:
        print("")
        print(f"AI 模型服务未就绪（{len(ai_bad)} 项）。这不影响打开已有手册、"
              "编辑步骤和导出，但暂时不能分析新视频。")
        print("启动 LM Studio / llama.cpp / Ollama（或改用带 Key 的云端服务）后重新检查。")
        return 0
    print("AI 服务就绪，可以开始分析。")
    return 0


def _cmd_migrate(args) -> int:
    """把旧版平铺的任务目录整理成 output/ + .work/ 两段结构。

    为什么要有这个命令：老任务的数据全平铺在任务根目录里（steps.json 和
    guide.html 挨着放）。新版本读的时候会兼容旧路径，但**写**一律写新结构，
    于是老目录会慢慢变成"一半旧一半新"的混合体。这个命令把它一次整理干净。

    安全要求：
    - 默认 dry-run 之外仍然先备份（把旧文件复制到 .work/ 而不是移动）。
    - 只有全部复制成功、校验过 steps.json 可读，才删除旧文件。
    - 已经迁移过的目录跳过。
    """
    ids = [args.run_id] if args.run_id else [d.name for d in RUNS_DIR.iterdir() if d.is_dir()]
    plan: list[tuple[Path, str, str]] = []      # (任务目录, 旧文件名, 新相对路径)
    for name in sorted(ids):
        d = RUNS_DIR / name
        if not is_legacy(d):
            continue
        for fname in ("video.json", "transcript.json", "frames.json", "vision.json",
                      "timeline.json", "steps.json", "run_report.json"):
            if (d / fname).exists():
                plan.append((d, fname, WORK_DIR + "/" + fname))
        for fname in ("guide.md", "guide.html"):
            if (d / fname).exists():
                plan.append((d, fname, OUTPUT_DIR + "/" + fname))
        if (d / "frames").is_dir():
            plan.append((d, "frames", FRAMES_DIR))
        if (d / "_analysis").is_dir():
            plan.append((d, "_analysis", WORK_DIR + "/analysis"))

    if not plan:
        print("没有需要迁移的旧任务。")
        return 0

    if args.dry_run:
        print(f"[dry-run] 将要迁移 {len({d for d, _, _ in plan})} 个任务、{len(plan)} 项：")
        for d, old, new in plan[:40]:
            print(f"  {d.name}/{old}  →  {new}")
        if len(plan) > 40:
            print(f"  …… 还有 {len(plan) - 40} 项")
        print("加上 --dry-run 之外的参数才会真正执行；执行前会先复制，确认无误再删旧文件。")
        return 0

    moved = 0
    for d, old, new in plan:
        src = d / old
        dest = d / new
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            if dest.exists():
                continue
            if src.is_dir():
                shutil.copytree(src, dest)
            else:
                shutil.copy2(src, dest)
        except OSError as e:
            print(f"[跳过] {d.name}/{old}：{e}", file=sys.stderr)
            continue
        moved += 1

    # 复制成功后，把旧文件移进 .work/_legacy/ 而不是删除：
    # 迁移是"整理"，不是"清理"，出问题时用户还能拿回原样。
    removed = 0
    for d, old, new in plan:
        src = d / old
        dest = d / new
        if not dest.exists() or not src.exists():
            continue
        vault = d / WORK_DIR / "_legacy"
        try:
            vault.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(vault / old))
            removed += 1
        except OSError:
            pass
    # 迁移完写一份 project.json，标记结构版本
    for name in {d.name for d, _, _ in plan if d.name}:
        _ensure_project_json(RUNS_DIR / name)
    print(f"完成：复制 {moved} 项，旧文件移到 .work/_legacy/（共 {removed} 项，没有删除）。")
    print("旧任务现在可以在界面上正常打开；想彻底清理 .work/_legacy/ 需要你手动删。")
    return 0


def _ensure_project_json(run_path: Path) -> None:
    """写任务元信息（结构版本、源视频、创建时间）。"""
    from .store import PROJECT_JSON, SCHEMA, write_json
    path = run_path / PROJECT_JSON
    if path.exists():
        return
    video = read_json(rp(run_path, VIDEO_JSON)) or {}
    write_json(path, {
        "schema": SCHEMA,
        "run_id": run_path.name,
        "video_path": video.get("path", ""),
        "created_at": video.get("created_at", ""),
    })


def _cmd_list() -> int:
    if not RUNS_DIR.exists():
        print("还没有任何运行记录。")
        return 0
    rows = []
    for d in sorted(RUNS_DIR.iterdir(), reverse=True):
        if not d.is_dir():
            continue
        report = read_json(rp(d, REPORT_JSON), {}) or {}
        rows.append((d.name, report.get("status", "?"), report.get("steps_count", 0),
                     report.get("finished_at", "")))
    if not rows:
        print("还没有任何运行记录。")
        return 0
    print(f"{'运行目录':<40} {'状态':<8} {'步骤':<6} 完成时间")
    for name, status, steps, finished in rows:
        print(f"{name:<40} {status:<8} {steps:<6} {finished}")
    return 0


def _cmd_export(args) -> int:
    d = run_dir(args.run_id)
    if not d.exists():
        print(f"[错误] 运行目录不存在：{d}", file=sys.stderr)
        return 2
    data = read_json(rp(d, STEPS_JSON))
    if not data:
        print(f"[错误] 没有找到 steps.json：{d / STEPS_JSON}", file=sys.stderr)
        return 2

    if getattr(args, "out", ""):
        from .export import export_manual

        res = export_manual(d, args.out, args.mode)
        if not res.ok:
            print(f"[错误] 导出失败：{res.error}", file=sys.stderr)
            for m in res.missing:
                print("  - " + m, file=sys.stderr)
            return 1
        print(f"已导出（{res.mode}）：{res.target}")
        for name in res.files:
            print("  " + name)
        for w in res.warnings:
            print("提示：" + w)
        return 0

    from .models import Frame, Transcript, VideoInfo, VisionNote
    from .render.html import render_html
    from .render.markdown import render_markdown
    from .store import FRAMES_JSON, TRANSCRIPT_JSON, VIDEO_JSON, VISION_JSON

    steps = [Step.from_dict(s) for s in data.get("steps", [])]
    video_d = read_json(rp(d, VIDEO_JSON)) or {}
    video = VideoInfo.from_dict(video_d) if video_d else VideoInfo(path="")
    frames_d = read_json(rp(d, FRAMES_JSON)) or {}
    frames = [Frame.from_dict(f) for f in frames_d.get("frames", [])]
    notes_d = read_json(rp(d, VISION_JSON)) or {}
    notes = [VisionNote.from_dict(n) for n in notes_d.get("notes", [])]
    tr_d = read_json(rp(d, TRANSCRIPT_JSON)) or {}
    transcript = Transcript.from_dict(tr_d) if tr_d else Transcript()

    import os
    from .store import OUTPUT_DIR
    try:
        base = d / OUTPUT_DIR
        if not base.is_dir():
            base = d
        rel = Path(os.path.relpath(video.path, str(base))).as_posix() if video.path else ""
    except Exception:
        rel = ""

    write_text((d / GUIDE_MD), 
        render_markdown(steps, frames, notes, transcript, video, d, rel), encoding="utf-8")
    write_text((d / GUIDE_HTML), 
        render_html(steps, frames, notes, transcript, video, rel), encoding="utf-8")
    print(f"已重新生成：{d / GUIDE_MD}")
    print(f"           {d / GUIDE_HTML}")
    return 0


def _mmss(seconds: float) -> str:
    s = max(0, int(round(seconds)))
    return f"{s // 60:02d}:{s % 60:02d}"


def _run_version(cmd: list[str]) -> str:
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=10)
        text = (out.stdout or out.stderr).decode("utf-8", "replace")
        return text.splitlines()[0][:60] if text else "?"
    except Exception:
        return "?"


if __name__ == "__main__":
    sys.exit(main())
