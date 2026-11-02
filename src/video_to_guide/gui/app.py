"""桌面界面启动入口。"""

from __future__ import annotations

import sys


def run() -> int:
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError:
        print(
            "缺少 PySide6，桌面界面无法启动。\n"
            "请先安装：pip install \"PySide6>=6.6\"\n"
            "（命令行模式不受影响，仍可用 vtg analyze 分析视频）",
            file=sys.stderr,
        )
        return 2

    from .main_window import MainWindow
    from .theme import app_qss

    app = QApplication(sys.argv)
    app.setApplicationName("Video to Guide")
    app.setApplicationDisplayName("Video to Guide")
    # 全局样式表在这里设一次，所有窗口和对话框都继承，
    # 组件代码里就不必到处写 setStyleSheet。
    app.setStyleSheet(app_qss())
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(run())
