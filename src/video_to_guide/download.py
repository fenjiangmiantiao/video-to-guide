# -*- coding: utf-8 -*-
"""可选组件的下载与安装。

安全要求（按重要性排序）：
1. 只装校验通过的文件：URL 和 SHA-256 都来自代码里锁定的清单，
   绝不信任下载过程中拿到的任何"哈希文件"。
2. 解压要防路径穿越：归档里的上级目录跳转或绝对路径一律拒绝，只按白名单取文件。
3. 先暂存再原子就位：下载到 .part、解压到 staging，验证通过才移动到版本目录，
   最后原子替换 active.json——中途失败不破坏已有可用版本。
4. 可取消：所有耗时步骤都检查取消标志，取消后保留 .part 以便续传。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .components import Module, component_dir, downloadable, data_root

CHUNK = 256 * 1024


class DownloadCancelled(RuntimeError):
    """用户点了取消。"""


@dataclass
class Progress:
    phase: str        # download / verify / extract / install / verify-run
    done: int
    total: int
    text: str = ""

    @property
    def percent(self) -> float:
        return (self.done / self.total * 100.0) if self.total else 0.0


def _cancel_check(cancel: Callable[[], bool] | None) -> None:
    if cancel and cancel():
        raise DownloadCancelled("已取消")


def download(url: str, dest: Path, module: Module,
             progress: Callable[[Progress], None] | None = None,
             cancel: Callable[[], bool] | None = None,
             timeout: float = 30.0) -> Path:
    """断点续传下载并校验，返回已校验的文件路径。

    续传靠 HTTP Range：上次留下的 .part 如果比远端短，就从断点接着下。
    服务器不支持 Range 时会返回整个文件，这时清空重下（不能盲目追加）。
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    have = part.stat().st_size if part.exists() else 0

    headers = {"User-Agent": "VideoToGuide/0.1"}
    if have:
        headers["Range"] = f"bytes={have}-"

    req = urllib.request.Request(url, headers=headers)
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        if e.code == 416:                       # 断点已经等于整文件长度
            part.unlink(missing_ok=True)
            return download(url, dest, module, progress, cancel, timeout)
        raise
    except urllib.error.URLError as e:
        raise RuntimeError(f"连不上下载地址：{getattr(e, 'reason', e)}") from e

    total = module.size
    resumed = resp.status == 206
    length = resp.headers.get("Content-Length")
    if length:
        total = int(length) + (have if resumed else 0)
    if not resumed:
        have = 0                                # 服务器给了整包，从头写

    mode = "ab" if resumed else "wb"
    t0 = time.time()
    with part.open(mode) as f:
        while True:
            _cancel_check(cancel)
            block = resp.read(CHUNK)
            if not block:
                break
            f.write(block)
            have += len(block)
            if progress:
                speed = have / max(0.001, time.time() - t0) / 1024 / 1024
                progress(Progress("download", have, total,
                                  f"{have / 1048576:.1f} / {total / 1048576:.1f} MB"
                                  f"（{speed:.1f} MB/s）"))
    resp.close()

    if progress:
        progress(Progress("verify", 0, 1, "正在校验文件完整性…"))
    digest = _sha256(part, cancel)
    if digest.lower() != module.sha256.lower():
        part.unlink(missing_ok=True)
        raise RuntimeError(
            "文件校验不通过，已删除下载内容。\n"
            f"期望 {module.sha256}\n实际 {digest}\n"
            "可能是下载中断或上游发布了新版本；请重新下载。")
    part.replace(dest)
    return dest


def _sha256(path: Path, cancel: Callable[[], bool] | None = None) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            _cancel_check(cancel)
            block = f.read(1024 * 1024)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def _safe_extract(archive: Path, target: Path, members: tuple,
                  cancel: Callable[[], bool] | None = None) -> list[Path]:
    """只取出白名单里的文件，并拒绝任何越界路径。"""
    picked: list[Path] = []
    with zipfile.ZipFile(archive) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            name = info.filename.replace("\\", "/")
            # 路径穿越与绝对路径：直接跳过，不解压也不报错
            if name.startswith("/") or ".." in name.split("/"):
                continue
            base = name.rsplit("/", 1)[-1]
            if members and base not in members:
                continue
            _cancel_check(cancel)
            out = target / base
            out.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, out.open("wb") as dst:
                shutil.copyfileobj(src, dst, CHUNK)
            picked.append(out)
    return picked


def install(module: Module, portable: bool, app_root: Path,
            progress: Callable[[Progress], None] | None = None,
            cancel: Callable[[], bool] | None = None) -> Path:
    """完整安装流程：下载 → 校验 → 解压 → 试运行 → 原子切换。"""
    if not downloadable(module):
        raise RuntimeError(f"{module.name} 暂不支持一键安装：{module.note or '缺少下载信息'}")

    root = data_root(portable, app_root) / "components" / module.id
    downloads = data_root(portable, app_root) / "downloads"
    version_dir = root / "versions" / module.version
    staging = root / "staging"

    archive = downloads / Path(module.url).name
    if not archive.exists():
        download(module.url, archive, module, progress, cancel)
    else:
        # 已有完整包：快速核对一次，避免拿旧包反复安装
        if progress:
            progress(Progress("verify", 0, 1, "已有下载包，正在核对…"))
        if _sha256(archive, cancel).lower() != module.sha256.lower():
            archive.unlink(missing_ok=True)
            download(module.url, archive, module, progress, cancel)

    if progress:
        progress(Progress("extract", 0, 1, "正在解压…"))
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)
    picked = _safe_extract(archive, staging, module.members, cancel)
    if not picked:
        raise RuntimeError("压缩包里没有找到需要的文件，可能上游改了打包结构")

    if progress:
        progress(Progress("install", 0, 1, "正在安装…"))
    if version_dir.exists():
        shutil.rmtree(version_dir, ignore_errors=True)   # 同一版本重装
    version_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(staging), str(version_dir))

    # 试运行：命令能起来、退出码正常，才算装好
    if progress:
        progress(Progress("verify-run", 0, 1, "正在验证组件能否运行…"))
    _verify_run(module, version_dir)

    _write_manifest(module, version_dir, archive)
    _activate(root, module.version)
    shutil.rmtree(root / "staging", ignore_errors=True)
    return version_dir


def install_from_file(module: Module, archive_path: Path, portable: bool,
                      app_root: Path,
                      progress: Callable[[Progress], None] | None = None,
                      cancel: Callable[[], bool] | None = None) -> Path:
    """用本地已下载的文件安装（校验 → 解压 → 试运行 → 切换）。

    为什么需要这条路：官方源在国外，直连可能要几小时。用户完全可以用浏览器
    或下载工具先把文件下好，再让软件做校验和安装。校验用的是代码里锁定的
    SHA-256，所以"从哪下载的"不影响安全性——文件对不上就不装。
    """
    archive_path = Path(archive_path)
    if not archive_path.exists():
        raise RuntimeError(f"找不到文件：{archive_path}")
    if not downloadable(module):
        raise RuntimeError(f"{module.name} 暂不支持安装：{module.note or '缺少校验信息'}")

    if progress:
        progress(Progress("verify", 0, 1, "正在校验本地文件…"))
    digest = _sha256(archive_path, cancel)
    if digest.lower() != module.sha256.lower():
        raise RuntimeError(
            "这个文件的校验值对不上，可能下错了版本或文件损坏。\n"
            f"期望 {module.sha256}\n实际 {digest}")

    root = data_root(portable, app_root) / "components" / module.id
    version_dir = root / "versions" / module.version
    staging = root / "staging"
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)

    if progress:
        progress(Progress("extract", 0, 1, "正在解压…"))
    picked = _safe_extract(archive_path, staging, module.members, cancel)
    if not picked:
        raise RuntimeError("这个压缩包里没有需要的文件，可能选错了文件")

    if progress:
        progress(Progress("install", 0, 1, "正在安装…"))
    if version_dir.exists():
        shutil.rmtree(version_dir, ignore_errors=True)
    version_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(staging), str(version_dir))

    if progress:
        progress(Progress("verify-run", 0, 1, "正在验证组件能否运行…"))
    _verify_run(module, version_dir)
    _write_manifest(module, version_dir, archive_path)
    _activate(root, module.version)
    shutil.rmtree(root / "staging", ignore_errors=True)
    return version_dir


def _verify_run(module: Module, version_dir: Path) -> None:
    for exe, flag in module.verify:
        path = version_dir / exe
        if not path.exists():
            raise RuntimeError(f"安装后找不到 {exe}")
        try:
            proc = subprocess.run([str(path), flag], capture_output=True, timeout=30)
        except Exception as e:                                # noqa: BLE001
            raise RuntimeError(f"{exe} 无法运行：{e}") from e
        if proc.returncode != 0:
            err = proc.stderr.decode("utf-8", "replace").strip()[:300]
            raise RuntimeError(f"{exe} 运行失败（退出码 {proc.returncode}）：{err}")


def _write_manifest(module: Module, version_dir: Path, archive: Path) -> None:
    """记录来源、版本、哈希、时间、许可证——将来排查问题时靠它。"""
    doc = {
        "id": module.id,
        "name": module.name,
        "version": module.version,
        "source_url": module.url,
        "sha256": module.sha256,
        "installed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "license": module.license_name,
        "license_url": module.license_url,
        "archive": archive.name,
        "files": sorted(p.name for p in version_dir.iterdir() if p.is_file()),
    }
    (version_dir / "component.json").write_text(
        json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if module.license_url:
        (version_dir / "LICENSE-SOURCE.txt").write_text(
            f"{module.name} {module.version}\n"
            f"许可证：{module.license_name}\n"
            f"来源：{module.license_url}\n"
            f"下载地址：{module.url}\n", encoding="utf-8")


def _activate(root: Path, version: str) -> None:
    """原子替换 active.json，指向当前生效版本。"""
    doc = {"version": version, "activated_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    tmp = root / "active.json.tmp"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(root / "active.json")


def is_installed(module: Module, portable: bool, app_root: Path) -> bool:
    d = component_dir(module, portable, app_root)
    return d.is_dir() and all((d / exe).exists() for exe, _ in module.verify)


def installed_version(module: Module, portable: bool, app_root: Path) -> str:
    active = data_root(portable, app_root) / "components" / module.id / "active.json"
    try:
        return str(json.loads(active.read_text(encoding="utf-8")).get("version") or "")
    except Exception:                                         # noqa: BLE001
        return ""


def uninstall(module: Module, portable: bool, app_root: Path) -> None:
    """删掉整个组件目录（包含所有历史版本）和下载缓存。"""
    root = data_root(portable, app_root)
    shutil.rmtree(root / "components" / module.id, ignore_errors=True)
    archive = root / "downloads" / Path(module.url).name
    archive.unlink(missing_ok=True)
