# -*- coding: utf-8 -*-
"""测试公共工具。

任务目录现在是"output/ 成品 + .work/ 工作数据"两段结构，测试里经常要手工
造一个任务目录，这里统一提供构造函数，免得每个测试都自己写路径。
"""
from __future__ import annotations

from pathlib import Path


def store_helpers(run: Path) -> Path:
    """建好任务目录骨架：.work/（数据）+ output/frames/（成品）。"""
    (run / ".work").mkdir(parents=True, exist_ok=True)
    (run / "output" / "frames").mkdir(parents=True, exist_ok=True)
    return run


def work(run: Path, name: str) -> Path:
    """任务数据文件路径（写在 .work/ 下）。"""
    return run / ".work" / name
