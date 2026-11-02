# -*- coding: utf-8 -*-
"""可选组件登记表：算什么、从哪下、多大、怎么验证。

只登记"用户真的可能需要、来源可信、体积可控"的组件。模型（几个 GB）和
LM Studio 这类整包应用不在这里——它们不随软件分发，也不该由软件悄悄下载。

每个组件都锁定版本和 SHA-256：网络上的文件随时会变，只认固定摘要，
否则"下载安装"就成了一个执行任意程序的后门。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Module:
    """一个可选组件。"""

    id: str
    name: str
    summary: str                  # 一句话说清"装了能得到什么"
    version: str
    url: str
    sha256: str                   # 锁定摘要；为空表示还没核实，禁止下载
    size: int                     # 预计字节数，仅用于进度显示
    archive: str = "zip"
    # 归档里需要的文件（相对路径的尾部匹配）；空表示整个解压
    members: tuple = field(default_factory=tuple)
    # 安装后用来验证"真的能用"的命令（相对安装目录）
    verify: tuple = field(default_factory=tuple)
    license_name: str = ""
    license_url: str = ""
    note: str = ""                # 界面上补充说明


# ---------------------------------------------------------------- 组件清单
FFMPEG = Module(
    id="ffmpeg",
    name="FFmpeg / FFprobe",
    summary="冷门视频格式的兼容兜底。普通 MP4/MKV 录屏不需要它。",
    version="9.0.2",
    url="https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip",
    # 官方 .sha256 文件的固定值（2026-11 核实）。上游发新版后这里要重新核对，
    # 不匹配就不许安装——宁可装不上，也不要装来路不明的可执行文件。
    sha256="60f467265b1e312373dbcd92200c2618a74850f98d3d078e94296bb3fa2047ba",
    size=109 * 1024 * 1024,
    members=("ffmpeg.exe", "ffprobe.exe"),
    verify=(("ffmpeg.exe", "-version"), ("ffprobe.exe", "-version")),
    license_name="GPLv3（Gyan.dev 构建）",
    license_url="https://www.gyan.dev/ffmpeg/builds/",
    note="也可以自己装 FFmpeg 到 PATH，二选一即可。",
)

# 语音识别已经是内置能力（faster-whisper + small 模型随包分发），
# 不再是一个"需要用户安装的可选组件"——把它列在这里只会让用户以为自己还得装点什么。
BUILTIN = Module(
    id="whisper",
    name="本地语音识别（已内置）",
    summary="随软件提供，无需安装。有旁白的视频会自动转录。",
    version="",
    url="",
    sha256="",
    size=0,
    note="已内置 faster-whisper 与 small 模型",
)

MODULES: tuple[Module, ...] = (FFMPEG,)


def get(module_id: str) -> Module | None:
    for m in MODULES:
        if m.id == module_id:
            return m
    return None


def downloadable(m: Module) -> bool:
    """能不能一键安装：必须有固定 URL 和已核实的 SHA-256。"""
    return bool(m.url) and bool(m.sha256) and len(m.sha256) == 64


# ---------------------------------------------------------------- 安装位置
def data_root(portable: bool, app_root: Path) -> Path:
    """组件安装根目录。

    默认放用户数据目录：程序可能被解压到 Program Files 这类没有写权限的地方。
    便携模式放程序旁边的 Data/，整个文件夹拷走时组件一起带走。
    """
    if portable:
        return app_root / "Data"
    import os
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "VideoToGuide"


def resolve_ffmpeg(portable: bool, app_root: Path,
                   configured: str = "") -> tuple[str, str]:
    """找 ffmpeg / ffprobe 的实际可执行路径，返回 (ffmpeg, ffprobe)。

    优先级：已安装的可选组件 → 设置里手填的路径 → PATH 里的名字。
    返回空串表示"没找到"。内置 PyAV 能处理绝大多数视频，这里只是兜底。
    """
    import shutil as _shutil
    from .download import is_installed

    ffmpeg_c = component_dir(FFMPEG, portable, app_root)
    if is_installed(FFMPEG, portable, app_root):
        a, b = ffmpeg_c / "ffmpeg.exe", ffmpeg_c / "ffprobe.exe"
        if a.exists() and b.exists():
            return str(a), str(b)

    if configured:
        cand = Path(configured)
        if cand.is_file():
            return str(cand), str(cand.parent / "ffprobe.exe")
        found = _shutil.which(configured)
        if found:
            return found, str(Path(found).parent / "ffprobe.exe")

    a = _shutil.which("ffmpeg") or ""
    b = _shutil.which("ffprobe") or ""
    return a, b


def components_present(portable: bool, app_root: Path) -> bool:
    """是否装了任意一个可选组件（用于 doctor 说明来源）。"""
    from .download import is_installed
    return any(is_installed(m, portable, app_root) for m in MODULES if downloadable(m))


def component_dir(m: Module, portable: bool, app_root: Path) -> Path:
    """某个组件当前的生效目录（读 active.json 指向的版本目录）。"""
    root = data_root(portable, app_root) / "components" / m.id
    active = root / "active.json"
    if active.exists():
        try:
            import json
            data = json.loads(active.read_text(encoding="utf-8"))
            ver = str(data.get("version") or "")
            if ver:
                cand = root / "versions" / ver
                if cand.is_dir():
                    return cand
        except Exception:                                     # noqa: BLE001
            pass
    return root / "versions" / (m.version or "unknown")
