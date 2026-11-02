"""运行目录（run）的读写。

一次分析 = 一个 runs/<run_id>/ 目录，里面固定放着这些文件：

    video.json        视频元信息
    transcript.json   带时间戳的字幕
    frames/           截图（原分辨率）
    frames.json       截图索引（时间、路径、所属屏幕）
    vision.json       每张截图的画面描述
    timeline.json     字幕与画面按时间对齐后的时间线
    steps.json        操作步骤（可被用户编辑）
    guide.md          导出的 Markdown 手册
    guide.html        导出的 HTML 手册
    run_report.json   本次处理的状态、警告、错误

约定：所有 JSON 都是 UTF-8，路径字段一律用"/"分隔的相对路径，
这样把整个 run 目录复制到别处、或者路径里有中文，都能正常打开。
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any

from .config import RUNS_DIR

# ---------------------------------------------------------------- 目录布局
# 一个任务目录分成"给人看的成品"和"给程序用的工作数据"两块：
#
#     runs/<run_id>/
#       project.json        任务元信息（schema 版本、源视频、创建时间）
#       output/             ← 成品：用户直接打开这个目录就能看手册
#         guide.html
#         guide.md
#         frames/           ← 手册里引用的截图（相对 output/ 的 frames/xxx.jpg）
#       .work/              ← 工作数据：中间产物，不含"必须人工保存"以外的东西
#         video.json  transcript.json  frames.json  vision.json
#         timeline.json  steps.json  run_report.json
#         analysis/         ← 外部工具的内部产物
#
# 为什么要分开：以前手册 guide.html 和一堆 JSON 平铺在一个目录里，用户想找
# 手册要在十几个文件里翻；拷给别人时也分不清哪些是必需的。现在 output/ 是
# 自包含的成品（拷走就能看），.work/ 是过程数据（可以没有，只是不能随便删——
# steps.json 里有用户人工改过的步骤）。
#
# 旧布局（schema 1）是全部平铺在 run 根目录。读取时用 rp() 自动回退到旧路径，
# 所以老任务不用迁移也能打开；写入一律写新布局。
PROJECT_JSON = "project.json"
WORK_DIR = ".work"
OUTPUT_DIR = "output"
OUTPUT_RES = "output"          # 截图等资源的引用根（HTML 里的 frames/ 是相对它）

FRAMES_DIR = OUTPUT_DIR + "/frames"     # 截图放在成品目录里，手册才能自包含
FRAMES_RES = "frames"                   # 截图在 JSON/HTML 里记录的相对路径前缀
VIDEO_JSON = WORK_DIR + "/video.json"
TRANSCRIPT_JSON = WORK_DIR + "/transcript.json"
FRAMES_JSON = WORK_DIR + "/frames.json"
VISION_JSON = WORK_DIR + "/vision.json"
TIMELINE_JSON = WORK_DIR + "/timeline.json"
STEPS_JSON = WORK_DIR + "/steps.json"
REPORT_JSON = WORK_DIR + "/run_report.json"
GUIDE_MD = OUTPUT_DIR + "/guide.md"
GUIDE_HTML = OUTPUT_DIR + "/guide.html"
SCHEMA = 2

# 历史记录索引：runs/history.json，只记录"用户从列表里移除掉哪些 run"。
# 移除一条记录 = 从索引里划掉，磁盘上的 run 目录、截图、手册、源视频一律不动。
# 这样用户误删了还能把 run_id 从 removed 里去掉恢复回来。
HISTORY_JSON = "history.json"


def video_fingerprint(path: str | Path) -> dict:
    """给视频算一个"内容身份"，用来判断缓存还能不能用。

    为什么不直接对整个文件做 SHA-256：一段 30 分钟 1080p 录屏几百 MB，
    每次分析都全量哈希会明显拖慢启动。这里取"文件大小 + 头 1MB + 尾 1MB"
    的哈希——同路径同名同长度但内容被替换的典型情况（换了另一个录屏、
    重新导出、剪辑后时长没变）都能识别出来。

    不记录 mtime：复制文件会改 mtime 但内容没变，那种情况应该继续复用缓存。
    """
    p = Path(path)
    try:
        st = p.stat()
    except OSError:
        return {"size": 0, "sha256": ""}
    h = hashlib.sha256()
    chunk = 1 << 20
    try:
        with open(p, "rb") as f:
            h.update(f.read(chunk))
            if st.st_size > 2 * chunk:
                f.seek(-chunk, 2)
                h.update(f.read(chunk))
    except OSError:
        return {"size": st.st_size, "sha256": ""}
    return {"size": st.st_size, "sha256": h.hexdigest()[:16]}


def new_run_id(video_path: Path) -> str:
    """用"时间 + 视频名"生成可读的 run id，例如 20261031-105530_我的录屏。"""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    stem = re.sub(r"[^\w\u4e00-\u9fff-]+", "_", video_path.stem).strip("_") or "video"
    return f"{stamp}_{stem[:40]}"


def run_dir(run_id: str, base: Path | None = None) -> Path:
    return (base or RUNS_DIR) / run_id


def prepare(run_id: str, base: Path | None = None) -> Path:
    """建好一个任务目录的骨架（新布局：output/ 成品 + .work/ 工作数据）。"""
    d = run_dir(run_id, base)
    (d / WORK_DIR).mkdir(parents=True, exist_ok=True)
    (d / FRAMES_DIR).mkdir(parents=True, exist_ok=True)
    marker = d / PROJECT_JSON
    if not marker.exists():
        write_json(marker, {
            "schema": SCHEMA,
            "run_id": run_id,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        })
    return d


# ---------------------------------------------------------------- 旧布局兼容
def rp(run_path: Path, rel: str) -> Path:
    """按新布局解析路径；若新路径不存在而旧布局（平铺）里有同名文件，回退到它。

    只用于**读**。写入请直接用 run_path / rel —— 新任务永远写新结构。
    这样 schema 1 的老任务不用先迁移也能打开、也能把手册导出来。
    """
    p = run_path / rel
    if p.exists():
        return p
    legacy = run_path / Path(rel).name
    if legacy.exists():
        return legacy
    return p


def read_json_rel(run_path: Path, rel: str, default: Any = None) -> Any:
    """读一个新布局 JSON（自动兼容旧的平铺布局）。"""
    return read_json(rp(run_path, rel), default)


def is_legacy(run_path: Path) -> bool:
    """这个任务目录还是旧的平铺布局吗（根目录里直接有 steps.json）。"""
    return (run_path / "steps.json").exists() and not (run_path / WORK_DIR).exists()


def frame_root(run_path: Path) -> Path:
    """截图目录（新布局 output/frames/；旧布局 frames/）。"""
    p = run_path / FRAMES_DIR
    if p.exists():
        return p
    legacy = run_path / "frames"
    if legacy.exists():
        return legacy
    return p


def frame_abs(run_path: Path, rel_path: str) -> Path:
    """把 JSON 里记录的截图相对路径（frames/xxx.jpg，相对 output/）还原成磁盘路径。

    旧布局的截图在 run 根下的 frames/，新布局在 output/frames/，这里两种都认。
    """
    p = run_path / OUTPUT_RES / rel_path
    if p.exists():
        return p
    legacy = run_path / rel_path
    if legacy.exists():
        return legacy
    return p


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def write_text(path: Path, text: str, encoding: str = "utf-8") -> Path:
    """写文本文件，自动建父目录（新布局下手册在 output/，目录可能还不存在）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding=encoding)
    return path


def write_json(path: Path, data: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)          # 原子替换，避免写一半断电留下坏文件
    return path


def list_runs(base: Path | None = None) -> list[dict]:
    """扫描 runs 目录，列出所有可以重新打开的分析记录（新的在前）。

    只要目录里有 steps.json 就算一条记录；testvideo/ badinput/ logs/ 这类
    辅助目录没有 steps.json，自然被跳过。返回的是"摘要"，够界面列表显示；
    要读全部内容请按 run_id 再读具体文件。
    """
    root = base or RUNS_DIR
    items: list[dict] = []
    if not root.exists():
        return items

    removed = set(load_history_index(root)["removed"])
    for d in root.iterdir():
        if not d.is_dir():
            continue
        steps_path = rp(d, STEPS_JSON)
        if not steps_path.exists():
            continue
        if d.name in removed:
            continue          # 用户从列表里移除过，但目录还在磁盘上
        steps_doc = read_json(steps_path) or {}
        raw_steps = steps_doc.get("steps") or []
        video_doc = read_json(rp(d, VIDEO_JSON)) or {}
        report = read_json(rp(d, REPORT_JSON)) or {}
        video_path = video_doc.get("path") or ""
        try:
            video_exists = bool(video_path) and Path(video_path).exists()
        except OSError:
            video_exists = False
        items.append({
            "run_id": d.name,
            "path": str(d),
            "video_name": Path(video_path).name if video_path else d.name,
            "video_path": video_path,
            "video_exists": video_exists,
            "has_guide": (d / GUIDE_HTML).exists() or (d / GUIDE_MD).exists()
                          or rp(d, GUIDE_HTML).exists() or rp(d, GUIDE_MD).exists(),
            "frames": len((read_json(rp(d, FRAMES_JSON)) or {}).get("frames") or []),
            "duration": float(video_doc.get("duration") or 0),
            "steps": len(raw_steps),
            "needs_review": sum(1 for s in raw_steps if s.get("needs_review")),
            "status": report.get("status") or "?",
            "finished_at": report.get("finished_at") or "",
            "legacy": is_legacy(d),
        })

    items.sort(key=lambda it: (it["finished_at"], it["run_id"]), reverse=True)
    return items


def load_history_index(base: Path | None = None) -> dict:
    """读历史记录索引，永远返回 {"removed": [...]}（文件不存在或坏掉都兜底）。"""
    root = base or RUNS_DIR
    data = read_json(root / HISTORY_JSON) or {}
    if not isinstance(data, dict):
        data = {}
    removed = data.get("removed")
    if not isinstance(removed, list):
        removed = []
    return {"removed": [str(x) for x in removed if isinstance(x, (str, int)) and str(x)]}


def save_history_index(data: dict, base: Path | None = None) -> None:
    root = base or RUNS_DIR
    write_json(root / HISTORY_JSON, {"removed": list(data.get("removed") or [])})


def remove_from_history(run_id: str, base: Path | None = None) -> None:
    """从历史记录列表里移除一条（不删任何文件）。"""
    root = base or RUNS_DIR
    data = load_history_index(root)
    if run_id not in data["removed"]:
        data["removed"].append(run_id)
    save_history_index(data, root)


def restore_to_history(run_id: str, base: Path | None = None) -> None:
    """把之前移除的记录放回列表。"""
    root = base or RUNS_DIR
    data = load_history_index(root)
    data["removed"] = [x for x in data["removed"] if x != run_id]
    save_history_index(data, root)


def rel_posix(path: Path, root: Path) -> str:
    """转成相对 root 的、用 / 分隔的路径，供 JSON 和 HTML 使用。"""
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()
