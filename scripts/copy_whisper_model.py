# -*- coding: utf-8 -*-
"""把 faster-whisper 模型复制进打包产物。

模型是纯数据（model.bin 等），PyInstaller 不会自动收。不带它，包在别人机器上
只能分析无音轨的视频——这正是"自带依赖"要解决的问题。
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODEL_NAME = os.environ.get("VTG_WHISPER_MODEL", "small").strip() or "small"
TARGET = ROOT / "dist" / "VideoToGuide" / "models" / ("faster-whisper-" + MODEL_NAME)


def candidates():
    """找模型源的几个可能位置（开发机 / 便携环境）。"""
    hub = Path(os.environ.get("HF_HOME") or (Path.home() / ".cache" / "huggingface"))
    yield from [
        Path(os.environ.get("VTG_WHISPER_SRC", "")),
        Path.home() / ".dsh/video-analyzer/cache/whisper/hub",
        hub / "hub",
        ROOT / "models" / ("faster-whisper-" + MODEL_NAME),
    ]


def find_model() -> Path | None:
    for base in candidates():
        if not base or not str(base):
            continue
        if base.name == ("faster-whisper-" + MODEL_NAME) and (base / "model.bin").exists():
            return base
        snap = base / ("models--Systran--faster-whisper-" + MODEL_NAME) / "snapshots"
        if snap.is_dir():
            for d in sorted(snap.iterdir()):
                if (d / "model.bin").exists():
                    return d
    return None


def main() -> int:
    src = find_model()
    if src is None:
        print(f"[WARN] 没找到 faster-whisper-{MODEL_NAME} 模型；包里将不含语音模型", file=sys.stderr)
        return 1
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    if TARGET.exists():
        shutil.rmtree(TARGET)
    shutil.copytree(src, TARGET)
    size = sum(p.stat().st_size for p in TARGET.rglob("*") if p.is_file())
    print(f"[OK] 已复制语音模型：{src} -> {TARGET.relative_to(ROOT)}（{size / 1048576:.0f} MB）")
    return 0


if __name__ == "__main__":
    sys.exit(main())