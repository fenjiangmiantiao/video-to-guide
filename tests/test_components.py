# -*- coding: utf-8 -*-
"""可选组件下载与安装的测试。

重点不是"能不能装"，而是"装错了文件必须拒绝、解压不能越界"。
真实 FFmpeg 包 114 MB、走外网，测试里用同一条代码路径 + 小样本。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest

from video_to_guide import components as C
from video_to_guide import download as D


def make_archive(tmp_path: Path, extra: dict | None = None) -> Path:
    """造一个测试归档：正常文件 + 可选的恶意条目。"""
    src = tmp_path / "src"
    src.mkdir()
    (src / "ffmpeg.exe").write_bytes(b"FAKE-FFMPEG")
    (src / "ffprobe.exe").write_bytes(b"FAKE-FFPROBE")
    archive = tmp_path / "pkg.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.write(src / "ffmpeg.exe", "build/bin/ffmpeg.exe")
        zf.write(src / "ffprobe.exe", "build/bin/ffprobe.exe")
        for name, body in (extra or {}).items():
            zf.writestr(name, body)
    return archive


def fake_module(archive: Path, **kw) -> C.Module:
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    base = replace(C.FFMPEG, id="fake", name="测试组件", version="0.0.1",
                  url="https://example.invalid/pkg.zip", sha256=digest,
                  size=archive.stat().st_size, verify=())
    return replace(base, **kw) if kw else base


def test_downloadable_requires_url_and_sha():
    assert C.downloadable(C.FFMPEG) is True
    assert C.downloadable(replace(C.FFMPEG, sha256="short")) is False
    assert C.downloadable(replace(C.FFMPEG, url="")) is False


def test_whisper_is_not_a_downloadable_component():
    """语音识别已经内置，不该再出现在"可选组件"里让用户以为要自己装。"""
    assert C.get("whisper") is None
    assert all(m.id != "whisper" for m in C.MODULES)


def test_real_ffmpeg_entry_is_pinned():
    """清单里的 FFmpeg 必须是完整 SHA-256 和固定版本，否则不许安装。"""
    assert len(C.FFMPEG.sha256) == 64
    assert C.FFMPEG.version
    assert C.FFMPEG.url.startswith("https://")


def test_install_rejects_wrong_hash(tmp_path):
    archive = make_archive(tmp_path)
    m = replace(fake_module(archive), sha256="0" * 64)
    with pytest.raises(RuntimeError, match="校验"):
        D.install_from_file(m, archive, True, tmp_path)
    assert not (tmp_path / "Data" / "components" / "fake").exists()


def test_install_success_writes_manifest_and_active(tmp_path):
    archive = make_archive(tmp_path)
    m = fake_module(archive)
    out = D.install_from_file(m, archive, True, tmp_path)
    assert (out / "ffmpeg.exe").exists()
    assert (out / "ffprobe.exe").exists()
    manifest = json.loads((out / "component.json").read_text(encoding="utf-8"))
    assert manifest["sha256"] == m.sha256
    assert manifest["source_url"] == m.url
    active = json.loads((tmp_path / "Data" / "components" / "fake" / "active.json")
                        .read_text(encoding="utf-8"))
    assert active["version"] == "0.0.1"
    assert D.is_installed(m, True, tmp_path) is True


def test_extract_blocks_path_traversal(tmp_path):
    """归档里带 ../ 或绝对路径的条目必须被丢掉，绝不能写到安装目录之外。"""
    archive = make_archive(tmp_path, extra={
        "../../evil.txt": "nope",
        "/absolute/evil2.txt": "nope",
    })
    m = fake_module(archive)
    D.install_from_file(m, archive, True, tmp_path)
    assert not list(tmp_path.rglob("evil.txt"))
    assert not list(tmp_path.rglob("evil2.txt"))


def test_uninstall_removes_everything(tmp_path):
    archive = make_archive(tmp_path)
    m = fake_module(archive)
    D.install_from_file(m, archive, True, tmp_path)
    assert D.is_installed(m, True, tmp_path)
    D.uninstall(m, True, tmp_path)
    assert D.is_installed(m, True, tmp_path) is False


def test_resolve_ffmpeg_prefers_installed_component(tmp_path, monkeypatch):
    """装了组件就用组件里的，不要再去找 PATH。"""
    archive = make_archive(tmp_path)
    m = fake_module(archive)
    out = D.install_from_file(m, archive, True, tmp_path)

    monkeypatch.setattr(C, "FFMPEG", m)
    monkeypatch.setattr(D, "is_installed", lambda *a, **k: True)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/" + name)
    a, b = C.resolve_ffmpeg(True, tmp_path, "")
    assert a == str(out / "ffmpeg.exe")
    assert b == str(out / "ffprobe.exe")


def test_portable_and_standard_roots_differ(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    standard = C.data_root(False, tmp_path)
    portable = C.data_root(True, tmp_path)
    assert "VideoToGuide" in str(standard)
    assert portable == tmp_path / "Data"
