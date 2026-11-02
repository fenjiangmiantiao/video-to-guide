# -*- coding: utf-8 -*-
"""测试夹具（Round 7 基准视频 C）：密集操作的"示例笔记软件"自动演示。

与 demo_app.py 的区别：
- 操作更密集：6 个可辨识动作集中在 50 秒内完成，相邻动作间隔 5 秒左右；
- 多两个控件（字号下拉、启动时恢复会话复选框），用来测"遗漏步骤"和"步骤合并"；
- 窗口置顶，避免录屏时被其他窗口遮挡。

用法：record_dense.sh 会先启动 ffmpeg 录屏，再运行本脚本。
"""
import sys
import tkinter as tk
from tkinter import ttk

WIN_W, WIN_H = 1200, 800
WIN_X, WIN_Y = 360, 140

LIGHT = dict(bg="#f5f5f5", panel="#ffffff", fg="#1f1f1f", muted="#666666",
             accent="#0a66c2", border="#d0d0d0", btn="#e8e8e8", textbg="#ffffff")
DARK = dict(bg="#1e1e1e", panel="#2a2a2a", fg="#e8e8e8", muted="#a0a0a0",
            accent="#4a9eff", border="#3c3c3c", btn="#333333", textbg="#252525")

FONT = ("Microsoft YaHei UI", 12)
FONT_B = ("Microsoft YaHei UI", 12, "bold")
FONT_T = ("Microsoft YaHei UI", 18, "bold")


class DenseDemo:
    def __init__(self, root):
        self.root = root
        self.c = dict(LIGHT)
        self.root.title("示例笔记软件 - DenseDemo")
        self.root.geometry("%dx%d+%d+%d" % (WIN_W, WIN_H, WIN_X, WIN_Y))
        self.root.resizable(False, False)
        self.root.attributes("-topmost", True)
        self.build_main()
        self.apply_theme()
        self.keep_front()
        self.root.after(300, self.run_timeline)

    def keep_front(self):
        """录制期间可能有其他窗口抢焦点，每 300ms 把自己压回最前。"""
        try:
            self.root.attributes("-topmost", True)
            self.root.lift()
        except Exception:
            pass
        self.root.after(300, self.keep_front)

    def build_main(self):
        r = self.root
        self.topbar = tk.Frame(r, height=64)
        self.topbar.pack(fill="x", side="top")
        self.topbar.pack_propagate(False)

        self.app_title = tk.Label(self.topbar, text="示例笔记软件", font=FONT_T)
        self.app_title.pack(side="left", padx=20)

        self.btn_new = tk.Button(self.topbar, text="新建笔记", font=FONT, width=10,
                                 relief="raised", bd=1, cursor="hand2")
        self.btn_new.pack(side="left", padx=(30, 8), pady=12)
        self.btn_settings = tk.Button(self.topbar, text="设置", font=FONT_B, width=8,
                                      relief="raised", bd=1, cursor="hand2")
        self.btn_settings.pack(side="left", padx=8, pady=12)

        self.body = tk.Frame(r)
        self.body.pack(fill="both", expand=True, padx=20, pady=(0, 10))
        self.text = tk.Text(self.body, font=FONT, wrap="word", relief="flat", bd=1,
                            padx=16, pady=16)
        self.text.pack(fill="both", expand=True)
        self.text.insert("1.0",
            "会议纪要\n\n"
            "1. 确认本周交付范围\n"
            "2. 排查导出乱码问题\n"
            "3. 下周一开始灰度\n\n"
            "提示：编辑器字号、主题和自动保存都在「设置」里。")
        self.text.config(state="disabled")

        self.statusbar = tk.Frame(r, height=34)
        self.statusbar.pack(fill="x", side="bottom")
        self.statusbar.pack_propagate(False)
        self.lbl_status = tk.Label(self.statusbar, text="状态：就绪", font=("Microsoft YaHei UI", 10))
        self.lbl_status.pack(side="left", padx=20)
        self.lbl_font = tk.Label(self.statusbar, text="字号：12", font=("Microsoft YaHei UI", 10))
        self.lbl_font.pack(side="left", padx=20)
        self.lbl_theme = tk.Label(self.statusbar, text="主题：浅色", font=("Microsoft YaHei UI", 10))
        self.lbl_theme.pack(side="left", padx=20)

    def apply_theme(self):
        c = self.c
        self.root.config(bg=c["bg"])
        self.topbar.config(bg=c["panel"])
        self.app_title.config(bg=c["panel"], fg=c["fg"])
        for b in (self.btn_new, self.btn_settings):
            b.config(bg=c["btn"], fg=c["fg"], activebackground=c["accent"],
                     activeforeground="#ffffff", highlightbackground=c["border"])
        self.body.config(bg=c["bg"])
        self.text.config(bg=c["textbg"], fg=c["fg"], insertbackground=c["fg"])
        self.statusbar.config(bg=c["panel"])
        for l in (self.lbl_status, self.lbl_font, self.lbl_theme):
            l.config(bg=c["panel"], fg=c["muted"])

    # ---------- 动作 ----------
    def flash(self, widget, ms=400):
        widget.config(relief="sunken", bg=self.c["accent"], fg="#ffffff")
        self.root.after(ms, lambda: widget.config(relief="raised", bg=self.c["btn"], fg=self.c["fg"]))

    def highlight(self, widget, on=True):
        widget.config(bg=self.c["accent"] if on else self.c["btn"],
                      fg="#ffffff" if on else self.c["fg"])

    def open_settings(self):
        self.flash(self.btn_settings)
        self.lbl_status.config(text="状态：设置窗口已打开")
        w = tk.Toplevel(self.root)
        self.dlg = w
        w.title("设置")
        w.geometry("640x470+%d+%d" % (WIN_X + 280, WIN_Y + 160))
        w.resizable(False, False)
        w.transient(self.root)
        w.attributes("-topmost", True)
        c = self.c
        w.config(bg=c["bg"])

        tk.Label(w, text="设置", font=FONT_T).pack(anchor="w", padx=24, pady=(18, 6))

        f1 = tk.Frame(w)
        f1.pack(fill="x", padx=24, pady=6)
        tk.Label(f1, text="编辑器", font=FONT_B).pack(anchor="w")
        row1 = tk.Frame(f1)
        row1.pack(fill="x", pady=4)
        tk.Label(row1, text="字号：", font=FONT).pack(side="left")
        self.cmb_font = ttk.Combobox(row1, values=["12", "14", "16", "18", "20"],
                                     state="readonly", font=FONT, width=10)
        self.cmb_font.current(0)
        self.cmb_font.pack(side="left", padx=6)

        row2 = tk.Frame(f1)
        row2.pack(fill="x", pady=4)
        tk.Label(row2, text="主题：", font=FONT).pack(side="left")
        self.cmb_theme = ttk.Combobox(row2, values=["浅色", "深色", "跟随系统"],
                                      state="readonly", font=FONT, width=10)
        self.cmb_theme.current(0)
        self.cmb_theme.pack(side="left", padx=6)

        f2 = tk.Frame(w)
        f2.pack(fill="x", padx=24, pady=10)
        tk.Label(f2, text="行为", font=FONT_B).pack(anchor="w")
        self.var_auto = tk.IntVar(value=0)
        self.chk_auto = tk.Checkbutton(f2, text="每次编辑后自动保存", variable=self.var_auto,
                                       font=FONT, onvalue=1, offvalue=0)
        self.chk_auto.pack(anchor="w", pady=3)
        self.var_resume = tk.IntVar(value=0)
        self.chk_resume = tk.Checkbutton(f2, text="启动时恢复上次会话", variable=self.var_resume,
                                         font=FONT, onvalue=1, offvalue=0)
        self.chk_resume.pack(anchor="w", pady=3)

        bottom = tk.Frame(w)
        bottom.pack(fill="x", side="bottom", pady=16, padx=24)
        self.btn_cancel = tk.Button(bottom, text="取消", font=FONT, width=9,
                                    command=lambda: self.dlg.destroy())
        self.btn_cancel.pack(side="right", padx=6)
        self.btn_save = tk.Button(bottom, text="保存", font=FONT_B, width=9)
        self.btn_save.pack(side="right", padx=6)

        for widget in (w, f1, f2, bottom, row1, row2):
            widget.config(bg=c["bg"])
        for parent in (w, f1, f2):
            for child in parent.winfo_children():
                if isinstance(child, tk.Label):
                    child.config(bg=c["bg"], fg=c["fg"])
        for chk in (self.chk_auto, self.chk_resume):
            chk.config(bg=c["bg"], fg=c["fg"], activebackground=c["bg"],
                       selectcolor=c["panel"], activeforeground=c["fg"])
        self.btn_cancel.config(bg=c["btn"], fg=c["fg"])
        self.btn_save.config(bg=c["accent"], fg="#ffffff")

    def pick_font(self):
        self.cmb_font.focus_set()
        self.cmb_font.current(2)          # 12 -> 16
        self.lbl_status.config(text="状态：字号已改为 16")

    def pick_dark(self):
        self.cmb_theme.focus_set()
        self.cmb_theme.current(1)
        self.lbl_status.config(text="状态：主题已改为深色")

    def check_auto(self):
        # 让复选框自己画勾，不要用整行背景色冒充勾选状态
        self.var_auto.set(1)
        self.chk_auto.config(selectcolor=self.c["panel"], bg=self.c["bg"], fg=self.c["fg"])
        self.lbl_status.config(text="状态：已勾选自动保存")

    def check_resume(self):
        self.var_resume.set(1)
        self.chk_resume.config(selectcolor=self.c["panel"], bg=self.c["bg"], fg=self.c["fg"])
        self.lbl_status.config(text="状态：已勾选恢复会话")

    def save_settings(self):
        self.flash(self.btn_save, 500)
        self.root.after(700, self.show_saved_toast)

    def show_saved_toast(self):
        w = tk.Toplevel(self.root)
        self.toast = w
        w.title("提示")
        w.geometry("360x170+%d+%d" % (WIN_X + 420, WIN_Y + 320))
        w.resizable(False, False)
        w.transient(self.root)
        w.attributes("-topmost", True)
        c = self.c
        w.config(bg=c["panel"])
        tk.Label(w, text="设置已保存", font=FONT_T, bg=c["panel"], fg=c["fg"]).pack(pady=30)
        tk.Button(w, text="确定", font=FONT, width=8, command=w.destroy).pack(pady=(0, 14))

    def finish(self):
        for attr in ("toast", "dlg"):
            win = getattr(self, attr, None)
            try:
                if win is not None:
                    win.destroy()
            except Exception:
                pass
        self.c = dict(DARK)
        self.apply_theme()
        self.lbl_status.config(text="状态：设置已生效")
        self.lbl_font.config(text="字号：16")
        self.lbl_theme.config(text="主题：深色")

    # ---------- 时间轴（毫秒，累计） ----------
    def run_timeline(self):
        steps = [
            (6000, lambda: self.highlight(self.btn_settings, True)),
            (2000, self.open_settings),      # t=8   打开设置
            (6000, self.pick_font),          # t=14  改字号
            (5000, self.pick_dark),          # t=19  改主题
            (5000, self.check_auto),         # t=24  勾选自动保存
            (5000, self.check_resume),       # t=29  勾选恢复会话
            (5000, lambda: self.highlight(self.btn_save, True)),
            (2000, self.save_settings),      # t=36  点保存
            (5000, self.finish),             # t=41  确认生效
            (12000, self.root.destroy),      # t=53  结束
        ]
        delay = 0
        for d, fn in steps:
            delay += d
            self.root.after(delay, fn)


if __name__ == "__main__":
    root = tk.Tk()
    DenseDemo(root)
    root.mainloop()
    sys.exit(0)
