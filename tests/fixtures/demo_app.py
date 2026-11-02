# -*- coding: utf-8 -*-
"""测试夹具：一个"示例笔记软件"，自动演示【打开设置 -> 修改选项 -> 点击保存 -> 确认生效】四步操作。
用途：配合 record_demo.sh 录屏，生成 Video to Guide 的验收测试视频（测试 A）。
不依赖网络，不写任何文件，运行 150 秒后自动退出。
"""
import tkinter as tk
from tkinter import ttk
import sys

WIN_W, WIN_H = 1200, 800
WIN_X, WIN_Y = 360, 140

LIGHT = dict(bg="#f5f5f5", panel="#ffffff", fg="#1f1f1f", muted="#666666",
             accent="#0a66c2", border="#d0d0d0", btn="#e8e8e8", textbg="#ffffff")
DARK = dict(bg="#1e1e1e", panel="#2a2a2a", fg="#e8e8e8", muted="#a0a0a0",
            accent="#4a9eff", border="#3c3c3c", btn="#333333", textbg="#252525")

FONT = ("Microsoft YaHei UI", 12)
FONT_B = ("Microsoft YaHei UI", 12, "bold")
FONT_T = ("Microsoft YaHei UI", 18, "bold")


class Demo:
    def __init__(self, root):
        self.root = root
        self.c = dict(LIGHT)
        self.dark = False
        self.root.title("示例笔记软件 - VideoToGuideDemo")
        self.root.geometry("%dx%d+%d+%d" % (WIN_W, WIN_H, WIN_X, WIN_Y))
        self.root.resizable(False, False)
        self.build_main()
        self.apply_theme()
        self.root.after(500, self.run_timeline)

    # ---------- 界面构建 ----------
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
        self.btn_open = tk.Button(self.topbar, text="打开", font=FONT, width=8,
                                  relief="raised", bd=1, cursor="hand2")
        self.btn_open.pack(side="left", padx=8, pady=12)
        self.btn_settings = tk.Button(self.topbar, text="设置", font=FONT_B, width=8,
                                      relief="raised", bd=1, cursor="hand2")
        self.btn_settings.pack(side="left", padx=8, pady=12)

        self.body = tk.Frame(r)
        self.body.pack(fill="both", expand=True, padx=20, pady=(0, 10))
        self.text = tk.Text(self.body, font=FONT, wrap="word", relief="flat", bd=1,
                            padx=16, pady=16)
        self.text.pack(fill="both", expand=True)
        self.text.insert("1.0",
            "项目周报\n\n"
            "1. 完成登录模块联调\n"
            "2. 修复导出功能在中文路径下的乱码问题\n"
            "3. 整理下周的测试计划\n\n"
            "备注：界面主题和自动保存可以在「设置」里调整。")
        self.text.config(state="disabled")

        self.statusbar = tk.Frame(r, height=34)
        self.statusbar.pack(fill="x", side="bottom")
        self.statusbar.pack_propagate(False)
        self.lbl_status = tk.Label(self.statusbar, text="状态：就绪", font=("Microsoft YaHei UI", 10))
        self.lbl_status.pack(side="left", padx=20)
        self.lbl_theme = tk.Label(self.statusbar, text="主题：浅色", font=("Microsoft YaHei UI", 10))
        self.lbl_theme.pack(side="left", padx=20)
        self.lbl_autosave = tk.Label(self.statusbar, text="自动保存：关闭", font=("Microsoft YaHei UI", 10))
        self.lbl_autosave.pack(side="left", padx=20)

    def apply_theme(self):
        c = self.c
        self.root.config(bg=c["bg"])
        self.topbar.config(bg=c["panel"])
        self.app_title.config(bg=c["panel"], fg=c["fg"])
        for b in (self.btn_new, self.btn_open, self.btn_settings):
            b.config(bg=c["btn"], fg=c["fg"], activebackground=c["accent"],
                     activeforeground="#ffffff", highlightbackground=c["border"])
        self.body.config(bg=c["bg"])
        self.text.config(bg=c["textbg"], fg=c["fg"], insertbackground=c["fg"])
        self.statusbar.config(bg=c["panel"])
        for l in (self.lbl_status, self.lbl_theme, self.lbl_autosave):
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
        w.geometry("620x420+%d+%d" % (WIN_X + 290, WIN_Y + 190))
        w.resizable(False, False)
        w.transient(self.root)
        c = self.c
        w.config(bg=c["bg"])

        tk.Label(w, text="设置", font=FONT_T).pack(anchor="w", padx=24, pady=(18, 6))

        f1 = tk.Frame(w)
        f1.pack(fill="x", padx=24, pady=8)
        tk.Label(f1, text="外观", font=FONT_B).pack(anchor="w")
        row = tk.Frame(f1)
        row.pack(fill="x", pady=6)
        tk.Label(row, text="主题：", font=FONT).pack(side="left")
        self.cmb = ttk.Combobox(row, values=["浅色", "深色", "跟随系统"], state="readonly",
                                font=FONT, width=14)
        self.cmb.current(0)
        self.cmb.pack(side="left", padx=6)

        f2 = tk.Frame(w)
        f2.pack(fill="x", padx=24, pady=12)
        tk.Label(f2, text="编辑", font=FONT_B).pack(anchor="w")
        self.var_auto = tk.IntVar(value=0)
        self.chk = tk.Checkbutton(f2, text="每次编辑后自动保存", variable=self.var_auto,
                                  font=FONT, onvalue=1, offvalue=0)
        self.chk.pack(anchor="w", pady=6)

        bottom = tk.Frame(w)
        bottom.pack(fill="x", side="bottom", pady=18, padx=24)
        self.btn_cancel = tk.Button(bottom, text="取消", font=FONT, width=9,
                                    command=lambda: self.dlg.destroy())
        self.btn_cancel.pack(side="right", padx=6)
        self.btn_save = tk.Button(bottom, text="保存", font=FONT_B, width=9)
        self.btn_save.pack(side="right", padx=6)

        for widget in (w, f1, f2, bottom, row):
            widget.config(bg=c["bg"])
        for lbl in w.winfo_children():
            if isinstance(lbl, tk.Label):
                lbl.config(bg=c["bg"], fg=c["fg"])
        for lbl in f1.winfo_children() + f2.winfo_children():
            if isinstance(lbl, tk.Label):
                lbl.config(bg=c["bg"], fg=c["fg"])
        self.chk.config(bg=c["bg"], fg=c["fg"], activebackground=c["bg"],
                        selectcolor=c["panel"], activeforeground=c["fg"])
        self.btn_cancel.config(bg=c["btn"], fg=c["fg"])
        self.btn_save.config(bg=c["accent"], fg="#ffffff")

    def pick_dark(self):
        self.cmb.focus_set()
        self.cmb.current(1)

    def check_auto(self):
        # 只把变量置 1，让 Checkbutton 自己画出勾。
        # 早先这里把整行背景刷成主色来"表示勾选"，结果截图里根本看不到勾，
        # 视觉模型只能说"文字被高亮"，步骤生成不敢写"已勾选"——测试素材的锅。
        self.var_auto.set(1)
        self.chk.config(selectcolor=self.c["panel"], bg=self.c["bg"], fg=self.c["fg"])

    def save_settings(self):
        self.flash(self.btn_save, 500)
        self.root.after(600, self.show_saved_toast)

    def show_saved_toast(self):
        """自定义提示框。不用 messagebox：它是模态阻塞的，会卡住自动演示的时间轴。"""
        w = tk.Toplevel(self.root)
        self.toast = w
        w.title("提示")
        w.geometry("360x170+%d+%d" % (WIN_X + 420, WIN_Y + 300))
        w.resizable(False, False)
        w.transient(self.root)
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
        self.dark = True
        self.c = dict(DARK)
        self.apply_theme()
        self.lbl_status.config(text="状态：设置已生效")
        self.lbl_theme.config(text="主题：深色")
        self.lbl_autosave.config(text="自动保存：已开启")

    # ---------- 时间轴（毫秒） ----------
    def run_timeline(self):
        steps = [
            (20000, lambda: self.highlight(self.btn_settings, True)),
            (3000, self.open_settings),
            (17000, self.pick_dark),
            (20000, self.check_auto),
            (15000, lambda: self.highlight(self.btn_save, True)),
            (3000, self.save_settings),
            (15000, self.finish),
            (25000, self.root.destroy),
        ]
        delay = 0
        for d, fn in steps:
            delay += d
            self.root.after(delay, fn)


if __name__ == "__main__":
    root = tk.Tk()
    Demo(root)
    root.mainloop()
    sys.exit(0)

