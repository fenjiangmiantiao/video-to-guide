"""让 `python -m video_to_guide` 可用。

不带参数时打开图形界面（和双击图标一样），带参数时走命令行，
所以 `python -m video_to_guide analyze xxx.mp4` 也能用。
"""

from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":
    argv = sys.argv[1:] or ["gui"]
    raise SystemExit(main(argv))
