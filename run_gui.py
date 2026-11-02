"""桌面程序打包入口（PyInstaller 用）。

不带参数启动图形界面；带参数时当作命令行程序用。
打包成 --windowed 后没有控制台，命令行模式的输出会写进 runs/logs/exe_cli.log，
这样"打包版能不能真的跑通分析"可以用命令验证，而不是只能靠肉眼点界面。
"""

import sys


def _log_stream():
    from video_to_guide.config import PROJECT_ROOT

    log_dir = PROJECT_ROOT / "runs" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return open(log_dir / "exe_cli.log", "w", encoding="utf-8", buffering=1)


def main() -> int:
    if len(sys.argv) > 1:
        if sys.stdout is None or sys.stderr is None:
            stream = _log_stream()
            sys.stdout = sys.stdout or stream
            sys.stderr = sys.stderr or stream
        from video_to_guide.cli import main as cli_main

        return cli_main(sys.argv[1:])

    from video_to_guide.gui.app import run

    return run()


if __name__ == "__main__":
    sys.exit(main())
