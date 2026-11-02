"""文档生成层：把结构化步骤渲染成 Markdown 和 HTML。"""

from __future__ import annotations

import re


def fmt_time(seconds: float) -> str:
    """把秒数格式化成 mm:ss（超过一小时用 h:mm:ss）。"""
    try:
        s = max(0.0, float(seconds))
    except (TypeError, ValueError):
        s = 0.0
    total = int(round(s))
    h, rem = divmod(total, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{sec:02d}"
    return f"{m:02d}:{sec:02d}"


def short_visible(text: str, max_items: int = 6, max_chars: int = 90) -> str:
    """「可见文字」常常是一整屏按钮清单，全贴进依据里会把手册淹没。

    这里只保留前几项，其余用省略号带过；想核对全文可以看 vision.json。
    """
    text = (text or "").strip()
    if not text:
        return ""
    items = re.findall(r"「[^」]*」", text)
    if len(items) > max_items:
        return "".join(items[:max_items]) + f"…（另 {len(items) - max_items} 项）"
    if len(text) > max_chars:
        return text[:max_chars] + "…"
    return text
