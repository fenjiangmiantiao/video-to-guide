# -*- coding: utf-8 -*-
"""导出命名：把"视频 / 批次 / 手册名"变成安全的文件系统名字。

为什么单独一个模块：Windows 的文件名限制不是常识，踩过才知道——
末尾句点会被静默丢弃、CON 这类设备名能创建但访问不到、路径超过 260 字符
在部分工具里打不开。这些规则集中在这里，导出和界面校验共用同一套。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# Windows 不允许出现在文件名里的字符（含控制字符）
_ILLEGAL = re.compile('[\\\\/:*?"<>|\\x00-\\x1f]')
# 保留设备名：这些名字不能作为文件名/目录名（带扩展名也不行）
_RESERVED = {"CON", "PRN", "AUX", "NUL",
             *(f"COM{i}" for i in range(1, 10)),
             *(f"LPT{i}" for i in range(1, 10))}
# 单个路径段上限；整条路径留出余量给盘符和后续层级
MAX_SEGMENT = 64
MAX_TOTAL = 240

# 允许的模板字段。刻意不做通用模板引擎：字段少、行为可预测、不会被执行
FIELDS = ("video", "date", "time", "run_id", "title")

LAYOUT_BY_VIDEO_BATCH = "by_video_batch"
LAYOUT_FLAT = "flat"
LAYOUTS = (LAYOUT_BY_VIDEO_BATCH, LAYOUT_FLAT)


class NamingError(ValueError):
    """命名失败（名字清洗后为空等）。"""


def safe_segment(raw: str, *, fallback: str = "未命名") -> str:
    """把一个用户可见的名字变成 Windows 上安全的单层目录/文件名。"""
    text = _ILLEGAL.sub("_", str(raw or ""))
    text = text.strip().strip(".")
    text = re.sub(r"\s+", " ", text).strip()
    if not text or text in (".", ".."):
        text = fallback
    if text.split(".")[0].upper() in _RESERVED:
        text = "_" + text
    if len(text) > MAX_SEGMENT:
        digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:6]
        head = text[: MAX_SEGMENT - 7].rstrip(" .")
        text = f"{head}~{digest}"
    return text


def render_template(template: str, fields: dict) -> str:
    """替换 {video} 这类占位符。

    不用 str.format：模板里出现不认识的占位符时 format 会抛 KeyError，
    这里只想"不认识的就原样保留"，避免一个笔误让整个导出失败。
    """
    out = str(template or "")
    for name in FIELDS:
        out = out.replace("{" + name + "}", str(fields.get(name, "")))
    return out


def build_fields(video_stem: str, run_id: str, title: str,
                 when: datetime | None = None) -> dict:
    """准备模板可用的五个字段。"""
    when = when or datetime.now()
    return {
        "video": video_stem or "视频",
        "date": when.strftime("%Y%m%d"),
        "time": when.strftime("%H%M%S"),
        "run_id": run_id,
        "title": title or video_stem or "操作手册",
    }


@dataclass
class ExportNames:
    """一次导出要用到的名字（已清洗）。"""
    video_dir: str
    batch_dir: str
    html_name: str
    md_name: str

    def to_dict(self) -> dict:
        return {"video_dir": self.video_dir, "batch_dir": self.batch_dir,
                "html": self.html_name, "md": self.md_name}


def plan(settings, video_stem: str, run_id: str, title: str,
         *, video_dir: str = "", batch_dir: str = "", name: str = "",
         when: datetime | None = None) -> ExportNames:
    """算出这次导出的目录名与文件名。

    video_dir / batch_dir / name 非空表示"用户这一次手动填的"，优先于模板。
    """
    fields = build_fields(video_stem, run_id, title, when)
    layout = getattr(settings, "export_layout", LAYOUT_BY_VIDEO_BATCH) or LAYOUT_BY_VIDEO_BATCH

    vdir = video_dir or render_template(
        getattr(settings, "export_video_template", "{video}") or "{video}", fields)
    bdir = batch_dir or render_template(
        getattr(settings, "export_batch_template", "{date}-{time}") or "{date}-{time}", fields)
    fname = name or render_template(
        getattr(settings, "export_name_template", "{title}") or "{title}", fields)

    if layout == LAYOUT_FLAT:
        combined = f"{vdir}_{bdir}" if bdir else vdir
        vdir, bdir = combined, ""

    return ExportNames(
        video_dir=safe_segment(vdir, fallback="视频"),
        batch_dir=safe_segment(bdir, fallback="批次") if bdir else "",
        html_name=safe_segment(fname, fallback="操作手册") + ".html",
        md_name=safe_segment(fname, fallback="操作手册") + ".md",
    )


def check_path_length(root: Path, names: ExportNames) -> str:
    """检查拼出来的完整路径会不会太长。返回提示文本，没问题时返回空串。"""
    parts = [names.video_dir, names.batch_dir, names.html_name]
    total = len(str(root)) + 1 + sum(len(p) + 1 for p in parts if p)
    if total > MAX_TOTAL:
        return (f"导出路径会达到 {total} 个字符，超过 {MAX_TOTAL} 的安全上限。"
                "请把导出根目录换到更短的位置，或把名字改短。")
    return ""
