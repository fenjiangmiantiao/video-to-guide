# -*- coding: utf-8 -*-
"""补齐 PyInstaller 漏收的原生运行时 DLL。

踩过的坑：Anaconda 的 Python 3.12 里，_ctypes.pyd 依赖的是 Library/bin/ffi.dll，
而 PyInstaller 扫不到那个目录，于是打包后 import ctypes 直接
"ImportError: DLL load failed while importing _ctypes"。
表现在软件上就是"语音转录不可用"——因为 ctranslate2 会用到 ctypes。

这里按"读 PE 导入表 → 找同名 DLL → 复制进 _internal"的方式补齐，
不写死具体路径，换台机器也能用。
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INTERNAL = ROOT / "dist" / "VideoToGuide" / "_internal"


def python_roots() -> list[Path]:
    """可能放着原生 DLL 的几个目录（按可靠性排序）。"""
    import sysconfig
    roots = []
    for key in ("base_prefix", "prefix", "exec_prefix"):
        p = Path(getattr(sys, key, "") or "")
        if p and p.exists():
            roots.extend([p / "DLLs", p / "Library" / "bin", p])
    plat = Path(sysconfig.get_path("platstdlib") or "") / ".."
    roots.append(plat.resolve())
    seen, out = set(), []
    for r in roots:
        if r.exists() and r not in seen:
            seen.add(r)
            out.append(r)
    return out


def imports_of(pyd: Path) -> list[str]:
    """读一个 .pyd/.dll 的 PE 导入表，返回它依赖的 DLL 名。"""
    try:
        import pefile
    except Exception:                                        # noqa: BLE001
        return []
    try:
        pe = pefile.PE(str(pyd), fast_load=True)
        pe.parse_data_directories(
            directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"]])
        return [e.dll.decode("utf-8", "ignore") for e in getattr(pe, "DIRECTORY_ENTRY_IMPORT", [])]
    except Exception:                                        # noqa: BLE001
        return []
    finally:
        try:
            pe.close()
        except Exception:                                    # noqa: BLE001
            pass


def main() -> int:
    if not INTERNAL.is_dir():
        print(f"[FAILED] 找不到打包目录：{INTERNAL}", file=sys.stderr)
        return 1

    have = {p.name.lower() for p in INTERNAL.rglob("*.dll")}
    # 只检查最容易被漏掉的那几个扩展模块
    watch = ["_ctypes.pyd", "_ssl.pyd", "_hashlib.pyd", "_socket.pyd", "_lzma.pyd",
             "_decimal.pyd", "_bz2.pyd", "_sqlite3.pyd", "_multiprocessing.pyd",
             "_asyncio.pyd", "_overlapped.pyd", "_queue.pyd", "_elementtree.pyd"]
    roots = python_roots()
    copied: list[str] = []

    for name in watch:
        target = next(iter(INTERNAL.rglob(name)), None)
        if target is None:
            continue
        for dep in imports_of(target):
            low = dep.lower()
            if low.startswith(("api-ms-", "kernel32", "ole32", "oleaut32",
                               "vcruntime", "msvcp", "python3", "user32",
                               "advapi32", "ws2_32", "shell32", "crypt32",
                               "rpcrt4", "gdi32", "imm32", "winmm", "oleacc",
                               "uxtheme", "dwmapi", "comctl32", "comdlg32",
                               "version", "setupapi", "ntdll", "secur32",
                               "winhttp", "dnsapi", "iphlpapi", "netapi32")):
                continue
            if low in have:
                continue
            src = None
            for r in roots:
                cand = r / dep
                if cand.exists():
                    src = cand
                    break
            if src is None:
                print(f"[WARN] {name} 依赖 {dep}，但在 Python 安装目录里没找到", file=sys.stderr)
                continue
            shutil.copy2(src, INTERNAL / dep)
            have.add(low)
            copied.append(f"{dep} （来自 {src.parent}）")

    if copied:
        print("[OK] 补齐了 PyInstaller 漏收的运行时 DLL：")
        for c in copied:
            print("     " + c)
    else:
        print("[OK] 没有需要补齐的运行时 DLL")
    return 0


if __name__ == "__main__":
    sys.exit(main())
