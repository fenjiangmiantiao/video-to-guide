# -*- coding: utf-8 -*-
"""语音识别模型的登记表、本地探测与下载。

为什么要单独一个模块：
1. 模型档位是用户要能换的东西（small 快但中文容易错，large-v3 准但慢三倍），
   界面只负责显示，判断和下载都放在这里。
2. 下载走 HuggingFace 协议，但允许换镜像端点（国内直连 huggingface.co 常常不通）。
3. 不做静默下载：每个文件都要过大小和摘要校验，失败保留 .part 以便续传。

模型来源都是 Systran 的 CTranslate2 转换版本（faster-whisper 官方推荐格式）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


@dataclass(frozen=True)
class AsrModel:
    """一个可选的语音识别模型档位。"""

    id: str
    name: str
    repo: str
    size_mb: int
    note: str


# 只登记 Systran 官方转换的 CTranslate2 版本——别的仓库格式不一定能直接被
# faster-whisper 加载，登记了却装不上比不登记更糟。
CATALOG: tuple[AsrModel, ...] = (
    AsrModel("tiny", "tiny（最快，准确率最低）", "Systran/faster-whisper-tiny", 75,
             "只适合确认流程能跑通，中文基本不可用。"),
    AsrModel("base", "base（快，中文勉强）", "Systran/faster-whisper-base", 145,
             "比 tiny 好一点，中文仍经常出错。"),
    AsrModel("small", "small（快，中文一般）", "Systran/faster-whisper-small", 484,
             "默认档。合成语音约 4.7% 字错率，软件名和菜单名容易错。"),
    AsrModel("medium", "medium（较慢，中文较好）", "Systran/faster-whisper-medium", 1530,
             "准确率和速度的中间档，显存约 2-4 GB。"),
    AsrModel("large-v3", "large-v3（最准，最慢）", "Systran/faster-whisper-large-v3", 3090,
             "中文准确率最高。实测同一视频字错率接近 0，120 秒音频约 8 秒（GPU）。"),
    AsrModel("large-v3-turbo", "large-v3-turbo（准且快）", "deepdml/faster-whisper-large-v3-turbo-ct2",
             1620, "large-v3 的精简解码器版本，速度快一倍多，个别语言略降。"),
)

BY_ID = {m.id: m for m in CATALOG}

# 这些文件缺一个都加载不了；下载完必须逐个核对存在
REQUIRED_FILES = ("model.bin", "config.json", "tokenizer.json", "vocabulary.txt")


def get(model_id: str) -> AsrModel | None:
    return BY_ID.get((model_id or "").strip())


def catalog() -> tuple[AsrModel, ...]:
    return CATALOG


# ------------------------------------------------------------------ 本地探测

def cache_root(settings) -> Path:
    """模型缓存的根目录。用户没指定就用 HF 默认缓存位置。"""
    explicit = (getattr(settings, "whisper_cache_home", "") or "").strip()
    if explicit:
        return Path(explicit)
    env = (os.environ.get("HF_HOME") or "").strip().strip('"')
    if env:
        return Path(env)
    return Path.home() / ".cache" / "huggingface"


def _hub_dir(repo: str, settings) -> Path:
    return cache_root(settings) / "hub" / ("models--" + repo.replace("/", "--"))


def _snapshot_dir(repo: str, settings) -> Path | None:
    """HF 缓存里的 snapshots/<commit>/ 目录，有就返回。"""
    snaps = _hub_dir(repo, settings) / "snapshots"
    if not snaps.is_dir():
        return None
    for cand in sorted(snaps.glob("*")):
        if (cand / "model.bin").exists():
            return cand
    return None


def resolve(model_id: str, settings) -> Path | None:
    """找出这个模型在本机的实际目录；找不到返回 None。

    查找顺序：用户手动指定的目录 → 程序目录 models/ → HF 缓存。
    """
    model = get(model_id)
    if model is None:
        return None
    # 1) 用户手动加载的目录（设置里选过）
    manual = (getattr(settings, "whisper_model_dir", "") or "").strip()
    if manual:
        p = Path(manual)
        if (p / "model.bin").exists():
            return p
    # 2) 程序目录自带（便携包）
    try:
        from .config import PROJECT_ROOT

        packaged = PROJECT_ROOT / "models" / ("faster-whisper-" + model_id)
        if (packaged / "model.bin").exists():
            return packaged
    except Exception:                                        # noqa: BLE001
        pass
    # 3) 本软件自己下载的目录（平铺布局，不走 HF 的 snapshots 软链）
    flat = cache_root(settings) / ("faster-whisper-" + model_id)
    if (flat / "model.bin").exists():
        return flat
    # 4) HF 官方缓存布局
    return _snapshot_dir(model.repo, settings)


def status(model_id: str, settings) -> dict:
    """给界面用的状态：装了没有、多大、在哪。"""
    model = get(model_id)
    if model is None:
        return {"id": model_id, "known": False, "installed": False, "size_mb": 0, "path": ""}
    path = resolve(model_id, settings)
    size = 0
    if path is not None:
        for name in REQUIRED_FILES:
            f = path / name
            if f.exists():
                size += f.stat().st_size
    return {
        "id": model_id,
        "known": True,
        "installed": path is not None,
        "size_mb": round(size / 1048576),
        "path": str(path) if path else "",
        "expected_mb": model.size_mb,
    }


def installed_models(settings) -> list[dict]:
    """本机已有的模型，用于下拉框。"""
    out = []
    for m in CATALOG:
        st = status(m.id, settings)
        if st["installed"]:
            out.append(st)
    return out


# -------------------------------------------------------------------- 下载

def endpoint() -> str:
    """下载端点。国内直连 huggingface.co 常常不通，允许用镜像。"""
    ep = (os.environ.get("VTG_HF_ENDPOINT") or "").strip()
    if ep:
        return ep.rstrip("/")
    # 没有显式配置时，优先用环境里已有的 HF_ENDPOINT（用户可能已经配好）
    env = (os.environ.get("HF_ENDPOINT") or "").strip()
    return env.rstrip("/") if env else "https://huggingface.co"


def download(
    model_id: str,
    settings,
    on_progress: Callable[[str, int, int], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> Path:
    """下载模型到缓存目录，返回模型目录。

    on_progress(文件名, 已下载字节, 总字节) 用来更新进度条；
    should_cancel() 返回 True 时中止并保留已下载部分。
    """
    model = get(model_id)
    if model is None:
        raise ValueError(f"不认识的模型：{model_id}")

    from .config import sanitize_hf_env

    sanitize_hf_env()
    # 离线标志会让下载直接失败，这里临时放开——用户明确点了"下载"
    old_offline = os.environ.get("HF_HUB_OFFLINE")
    old_endpoint = os.environ.get("HF_ENDPOINT")
    old_home = os.environ.get("HF_HOME")
    os.environ["HF_HUB_OFFLINE"] = "0"
    os.environ["HF_ENDPOINT"] = endpoint()
    # HF_HOME 的环境变量值如果带了引号（例如被写成带双引号的路径），
    # huggingface_hub 会拼出「<带引号的目录>\token」这种非法路径直接崩。
    # 这里统一写回干净的绝对路径。
    os.environ["HF_HOME"] = str(cache_root(settings))
    try:
        from huggingface_hub import hf_hub_download

        target = cache_root(settings) / "hub"
        target.mkdir(parents=True, exist_ok=True)
        last = None
        for name in REQUIRED_FILES:
            if should_cancel and should_cancel():
                raise RuntimeError("已取消")
            # local_dir 会把文件平铺到同一目录，正好是 faster-whisper 要的布局
            dest = cache_root(settings) / ("faster-whisper-" + model_id)
            dest.mkdir(parents=True, exist_ok=True)
            last = hf_hub_download(
                repo_id=model.repo, filename=name, cache_dir=str(target),
                local_dir=str(dest),
            )
            if on_progress:
                f = Path(last)
                on_progress(name, f.stat().st_size if f.exists() else 0, 0)
        return Path(last).parent
    finally:
        if old_offline is None:
            os.environ.pop("HF_HUB_OFFLINE", None)
        else:
            os.environ["HF_HUB_OFFLINE"] = old_offline
        if old_endpoint is None:
            os.environ.pop("HF_ENDPOINT", None)
        else:
            os.environ["HF_ENDPOINT"] = old_endpoint
        if old_home is None:
            os.environ.pop("HF_HOME", None)
        else:
            os.environ["HF_HOME"] = old_home
