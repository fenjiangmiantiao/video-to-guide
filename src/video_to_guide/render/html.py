"""生成单文件 HTML 手册。

目标：双击就能在浏览器里打开，不依赖网络、不依赖服务器、不引外部 CDN。
设计取舍：
- 左侧固定目录 + 右侧步骤卡片：一份十几步的手册，读者最需要的是"快速定位到某一步"，
  目录比"从头滚到底"有用得多。窄屏（手机、半屏窗口）时目录自动收起。
- 截图可点击放大：教程截图里经常有很小的按钮和菜单，缩略图看不清，
  但全尺寸图直接铺在正文里又会把正文挤没。用遮罩层放大是折中方案。
- 依据默认折叠：读者关心"怎么做"，核对者才关心"凭什么"。折叠起来既不影响阅读，
  又保证证据随时可查（这是本项目"不编造操作"的底线）。
- 打印样式单独处理：手册常常被打印或另存为 PDF 带去开会。
"""

from __future__ import annotations

import html
import time
from pathlib import Path

from ..models import Frame, Step, Transcript, VideoInfo, VisionNote
from . import fmt_time, short_visible


def render_html(
    steps: list[Step],
    frames: list[Frame],
    notes: list[VisionNote],
    transcript: Transcript,
    video: VideoInfo,
    video_rel: str = "",
    title: str = "",
    meta=None,
) -> str:
    frame_by_id = {f.id: f for f in frames}
    note_by_id = {n.id: n for n in notes}
    seg_by_id = {s.id: s for s in transcript.segments}
    review_count = sum(1 for s in steps if s.needs_review)
    page_title = f"{title or Path(video.path).stem} 操作手册"

    parts: list[str] = [_head(page_title), "<body>"]
    parts.append(_topbar(page_title, video, steps, review_count))
    parts.append('<div class="layout">')
    parts.append(_toc(steps, bool(video_rel)))
    parts.append('<main class="main">')
    parts.append(_player(video_rel, getattr(video, "duration", 0.0),
                           Path(video.path).name if video.path else ""))
    parts.append(_review_banner(review_count))

    if not steps:
        parts.append('<p class="empty">这次没有生成任何步骤，可能是视频里没有可识别的操作。</p>')

    for step in steps:
        parts.append(_render_step(step, frame_by_id, note_by_id, seg_by_id, bool(video_rel)))

    parts.append("</main></div>")
    parts.append(_lightbox())
    parts.append(_script())
    parts.append("</body></html>")
    return "\n".join(parts)


# ---------------------------------------------------------------- 页面骨架

def _meta_card(meta) -> str:
    """现实模式的用料/工具/成品状态卡片。软件模式 meta 为空，直接不渲染。"""
    if meta is None or not (meta.materials or meta.tools or meta.final_state):
        return ""
    import html as _html

    rows = []
    for m in meta.materials:
        qty = f" {m.quantity}" if m.quantity else ""
        rows.append(f"<li>{_html.escape(m.name)}{_html.escape(qty)}</li>")
    body = ""
    if rows:
        body += '<div class="meta-block"><h3>用料</h3><ul class="meta-list">' + "".join(rows) + "</ul></div>"
    if meta.tools:
        tools_txt = "、".join(_html.escape(t) for t in meta.tools)
        body += f'<div class="meta-block"><h3>工具</h3><p>{tools_txt}</p></div>'
    if meta.final_state:
        body += f'<div class="meta-block"><h3>成品状态</h3><p>{_html.escape(meta.final_state)}</p></div>'
    style = ("background:#fff;border:1px solid #d0d7de;border-radius:8px;"
             "padding:14px 16px;margin:0 0 18px;")
    h3 = "margin:0 0 6px;font-size:14px;color:#1f2328;"
    block = "display:inline-block;vertical-align:top;margin-right:32px;"
    body = body.replace('<div class="meta-block">', f'<div style="{block}">')
    body = body.replace("<h3>", f'<h3 style="{h3}">')
    body = body.replace('<ul class="meta-list">', '<ul style="margin:0;padding-left:18px;">')
    return f'<section style="{style}">{body}</section>'


def _topbar(title: str, video: VideoInfo, steps: list[Step], review_count: int) -> str:
    badges = [f'<span class="badge">{len(steps)} 个步骤</span>']
    if review_count:
        badges.append(f'<span class="badge warn">{review_count} 个待核实</span>')
    else:
        badges.append('<span class="badge ok">全部已核实</span>')
    badges.append(f'<span class="badge">时长 {fmt_time(video.duration)}</span>')
    return (
        '<header class="topbar">'
        '<div class="topbar-inner">'
        f'<h1>{_e(title)}</h1>'
        f'<div class="badges">{"".join(badges)}</div>'
        '<div class="meta">'
        f'<span>源视频：{_e(Path(video.path).name)}</span>'
        f'<span>{video.width}×{video.height}</span>'
        f'<span>生成于 {time.strftime("%Y-%m-%d %H:%M")}</span>'
        "</div>"
        "</div>"
        "</header>"
    )


def _toc(steps: list[Step], has_video: bool) -> str:
    items = []
    for s in steps:
        flag = '<span class="toc-flag">待核实</span>' if s.needs_review else ""
        items.append(
            f'<li><a href="#step-{s.index}" data-target="step-{s.index}" '
            f'data-search="{_e(_search_text(s))}">'
            f'<span class="toc-num">{s.index}</span>'
            f'<span class="toc-title">{_e(s.title)}</span>{flag}</a></li>'
        )
    if not items:
        items.append('<li class="toc-empty">（没有步骤）</li>')
    return (
        '<aside class="toc">'
        '<div class="toc-head">'
        '<span class="toc-label">步骤目录</span>'
        '<input id="toc-filter" type="search" placeholder="筛选步骤…" aria-label="筛选步骤">'
        "</div>"
        f'<ol class="toc-list">{"".join(items)}</ol>'
        f'<div class="toc-foot">'
        f'{"点目录可跳到对应步骤；点步骤里的时间按钮可跳到视频位置。" if has_video else "点目录可跳到对应步骤。手册没附视频：先在上方「选择本地视频」，时间按钮才能跳转。"}'
        f"</div>"
        "</aside>"
    )


def _player(video_rel: str, duration: float = 0.0, video_name: str = "") -> str:
    """播放器。

    没有视频路径时（轻量手册）不留一块死掉的空白，而是给一个「选择本地视频」入口：
    用户挑一个本地文件后，用 createObjectURL 在当前页面临时播放。这样手册可以单独
    发给别人，对方手上有原视频就能恢复跳转，没有也能照着手册做。
    """
    # 把"期望的视频"写进页面：用户自己挑文件时用它判断选得对不对。
    # 秒数是整数比对（±1.5 秒容差），文件名只作提示，不作为硬条件——
    # 用户完全可能把源视频改名或换个同内容的副本。
    expect = f'data-expect-duration="{duration:.1f}" data-expect-name="{_e(video_name)}"'
    if not video_rel:
        return (
            f'<div class="player missing" id="player-box" {expect}>'
            '<div class="player-missing" id="player-missing">'
            "<p><strong>手册里没有附带视频。</strong>步骤、截图和依据都能正常查看，"
            "只是时间按钮暂时不能跳转播放。</p>"
            '<label class="pick-video" for="video-file">选择本地视频…</label>'
            '<p class="player-note">请选这段录屏<strong>本身</strong>'
            + (f"（约 {_fmt_duration(duration)}）" if duration else "")
            + "。选错了会提示你，并且随时可以换。"
            "只在当前页面临时播放，不会上传，也不会记住路径。</p>"
            "</div>"
            '<video id="player" controls preload="metadata" hidden></video>'
            # 换视频的入口要常驻：加载过一次之后仍然能换，
            # 否则用户选错了就只能刷新页面重来。
            '<div class="player-switch"><label class="pick-video" for="video-file">'
            "换一个视频…</label></div>"
            '<input id="video-file" class="visually-hidden" type="file" accept="video/*">'
            "</div>"
        )
    # 完整包也要留"换一个视频"的入口：手册被单独拷走、源视频被移动、
    # 或者浏览器不支持这个编码时，用户仍然能自己指定视频，而不是只能看黑屏。
    return (
        f'<div class="player" id="player-box" {expect}>'
        f'<video id="player" controls preload="metadata" src="{_e(video_rel)}"></video>'
        '<div class="player-hint">点步骤右上角的时间按钮，视频会跳到那一刻并开始播放</div>'
        '<div class="player-switch"><label class="pick-video" for="video-file">'
        "换一个视频…</label></div>"
        '<input id="video-file" class="visually-hidden" type="file" accept="video/*">'
        "</div>"
    )


def _fmt_duration(seconds: float) -> str:
    """把秒数说成人话：2 分 5 秒 / 46 秒。"""
    s = int(round(seconds or 0))
    if s < 60:
        return f"{s} 秒"
    return f"{s // 60} 分 {s % 60} 秒"


def _review_banner(review_count: int) -> str:
    if not review_count:
        return ""
    return (
        '<div class="warn-banner">'
        f"有 {review_count} 个步骤标记为「待核实」：当时的字幕和画面不足以确认操作，"
        "使用前请先对照视频核对，不要直接照做。"
        "</div>"
    )


# ---------------------------------------------------------------- 单个步骤

def _render_step(step: Step, frame_by_id: dict, note_by_id: dict, seg_by_id: dict,
                 has_video: bool) -> str:
    cls = "step review" if step.needs_review else "step"
    out = [f'<section class="{cls}" id="step-{step.index}" '
           f'data-title="{_e(step.title)}" data-search="{_e(_search_text(step))}">']

    # 标题行：序号 + 标题 + 时间跳转
    out.append('<div class="step-head">')
    out.append('<div class="step-title">')
    out.append(f'<span class="num">{step.index}</span>')
    out.append(f"<h2>{_e(step.title)}</h2>")
    out.append("</div>")
    # 时间按钮**两种情况都要有**：轻量手册里用户可能后来自己选了视频，
    # 那时按钮就能用了。之前只在"包内自带视频"时渲染，导致轻量包里根本没有
    # 跳转入口——用户选了视频也点不到。
    jump_cls = "jump" if has_video else "jump pending"
    out.append(
        f'<button class="{jump_cls}" onclick="seekTo({step.t_start:.2f})" '
        f'title="跳到视频 {fmt_time(step.t_start)}">'
        f'▶ {fmt_time(step.t_start)}</button>'
    )
    out.append("</div>")

    out.append(
        f'<div class="step-meta">'
        f'<span class="time-range">{fmt_time(step.t_start)} – {fmt_time(step.t_end)}</span>'
        f'<span class="duration">持续 {_dur(step.t_start, step.t_end)}</span>'
        + (f'<span class="conf">置信度 {step.confidence:.2f}</span>' if step.confidence is not None else "")
        + ("<span class=\"review-tag\">待核实</span>" if step.needs_review else "")
        + "</div>"
    )

    if step.needs_review:
        note = _e(step.review_note or "依据不足，需要人工核对")
        out.append(f'<div class="review-note">⚠️ 待核实：{note}</div>')

    # 正文：左边文字，右边截图（窄屏自动堆叠）
    out.append('<div class="step-body">')
    out.append('<div class="step-text">')
    out.append(f'<p class="action">{_e(step.action) or "（这一步没有写操作说明）"}</p>')
    if step.expected_result:
        out.append(
            '<p class="expect"><span class="field">预期结果</span>'
            f"{_e(_clip(step.expected_result, 90))}</p>"
        )
    out.append("</div>")

    figures, shot_count = _figures(step, frame_by_id)
    if figures:
        multi = " multi" if shot_count > 1 else ""
        out.append(f'<div class="step-shots{multi}">{figures}</div>')
    out.append("</div>")

    # 依据折叠：读者看操作，核对者看证据
    out.append(
        '<details class="evidence">'
        f"<summary>依据（{len(step.evidence.transcript_ids)} 条字幕 · "
        f"{len(step.evidence.vision_ids)} 条画面）</summary>"
        f'<div class="evidence-body">{_evidence_html(step, note_by_id, seg_by_id, frame_by_id)}</div>'
        "</details>"
    )
    out.append("</section>")
    return "\n".join(out)


def _figures(step: Step, frame_by_id: dict) -> tuple[str, int]:
    shots = []
    for fid in step.frame_ids:
        f = frame_by_id.get(fid)
        if not f:
            continue
        # 用 aspect-ratio 先把位置占住：截图是懒加载的，如果等下载完才撑开，
        # 页面高度会在滚动过程中变化，目录跳转就会停在半路（实测过）。
        ratio = ""
        if getattr(f, "width", 0) and getattr(f, "height", 0):
            ratio = f' style="aspect-ratio:{f.width}/{f.height}"'
        shots.append(
            f'<figure><img src="{_e(f.path)}" alt="步骤 {step.index} 在 {fmt_time(f.time)} 的截图" '
            f'loading="lazy" decoding="async"{ratio} onclick="openLightbox(this)">'
            f"<figcaption>{fmt_time(f.time)}</figcaption></figure>"
        )
    return "".join(shots), len(shots)


def _evidence_html(step: Step, note_by_id: dict, seg_by_id: dict,
                   frame_by_id: dict | None = None) -> str:
    rows = []
    for tid in step.evidence.transcript_ids:
        seg = seg_by_id.get(tid)
        if seg:
            rows.append(
                f'<li><span class="ev-kind">字幕</span>'
                f'<span class="ev-time">{fmt_time(seg.start)}</span>'
                f"{_e(seg.text)}</li>"
            )
    for vid in step.evidence.vision_ids:
        n = note_by_id.get(vid)
        if n and n.ok:
            bits = [b for b in (n.interface, short_visible(n.visible_text), n.activity)
                    if b and b != "无明显操作"]
            if bits:
                frame = (frame_by_id or {}).get(n.frame_id)
                when = fmt_time(frame.time) if frame else ""
                rows.append(
                    f'<li><span class="ev-kind">画面</span>'
                    f'<span class="ev-time">{when}</span>'
                    f"{_e('；'.join(bits))}</li>"
                )
    if not rows:
        return '<p class="ev-empty">这一步没有文字依据，只能靠截图判断。</p>'
    return f'<ul class="ev-list">{"".join(rows)}</ul>'


def _search_text(step: Step) -> str:
    """目录筛选用的搜索文本。

    只放「标题 + 操作 + 预期结果」：这三样是读者真正想找的东西。
    如果把依据、截图说明也算进去，搜「保存」会命中一堆只是顺口提到保存的步骤。
    目录和正文必须用同一份文本，否则会出现"目录筛出 3 条、正文只剩 4 条"的矛盾。
    """
    return " ".join(x for x in (step.title, step.action, step.expected_result) if x)


def _clip(text: str, limit: int) -> str:
    """预期结果只留摘要。

    真实录屏里截图是全屏的，画面描述的"可见文字"会混进大量背景窗口的菜单、
    插件名。原样铺在正文里既读不下去，也会干扰"这一步到底改了什么"的判断。
    完整内容仍然在下面可展开的依据区，随时能查。
    """
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…（完整内容见下方依据）"


def _dur(t_start: float, t_end: float) -> str:
    try:
        d = max(0.0, float(t_end) - float(t_start))
    except (TypeError, ValueError):
        d = 0.0
    return f"{d:.1f} 秒"


def _e(text: str) -> str:
    """HTML 转义，防止视频标题或字幕里的尖括号破坏页面。"""
    return html.escape(str(text or ""), quote=True)


# ---------------------------------------------------------------- 样式与脚本

_CSS = """
:root {
  --bg:#f6f8fa; --card:#ffffff; --fg:#1f2328; --muted:#57606a; --faint:#8b949e;
  --line:#d0d7de; --accent:#0969da; --accent-soft:#ddf4ff;
  --ok:#1a7f37; --warn:#9a6700; --warn-bg:#fff8c5; --warn-line:#d4a72c;
}
* { box-sizing:border-box; }
/* 播放器是 sticky 的，点目录跳转时必须把它占的高度让出来，
   否则目标步骤会被播放器盖住，看起来像"点第 1 步跳到了第 2 步"。
   这里的 16px 只是兜底，真实高度由脚本按播放器实际尺寸写入。 */
html { scroll-behavior:smooth; scroll-padding-top:16px; }
body { margin:0; background:var(--bg); color:var(--fg); line-height:1.75;
       font-family:"Microsoft YaHei UI","Microsoft YaHei","PingFang SC",system-ui,sans-serif;
       font-size:15px; }

/* 顶部 */
.topbar { background:var(--card); border-bottom:1px solid var(--line); }
.topbar-inner { max-width:1280px; margin:0 auto; padding:20px 28px 16px; }
.topbar h1 { font-size:22px; margin:0 0 10px; font-weight:600; }
.badges { display:flex; flex-wrap:wrap; gap:8px; margin-bottom:8px; }
.badge { background:var(--bg); border:1px solid var(--line); color:var(--muted);
         border-radius:999px; padding:2px 10px; font-size:12px; }
.badge.warn { background:var(--warn-bg); border-color:var(--warn-line); color:#6b4e00; }
.badge.ok { background:#e6f4ea; border-color:#a7d7b5; color:var(--ok); }
.meta { display:flex; flex-wrap:wrap; gap:4px 18px; color:var(--faint); font-size:12px; }

/* 两栏布局 */
.layout { max-width:1280px; margin:0 auto; padding:22px 28px 90px;
          display:grid; grid-template-columns:250px minmax(0,1fr); gap:26px; align-items:start; }

/* 目录 */
.toc { position:sticky; top:16px; background:var(--card); border:1px solid var(--line);
       border-radius:10px; padding:12px; max-height:calc(100vh - 40px);
       display:flex; flex-direction:column; gap:8px; }
.toc-head { display:flex; flex-direction:column; gap:6px; }
.toc-label { font-size:12px; color:var(--faint); letter-spacing:.04em; }
#toc-filter { width:100%; padding:6px 9px; font-size:13px; color:var(--fg);
              border:1px solid var(--line); border-radius:6px; background:var(--card); }
#toc-filter:focus { outline:none; border-color:var(--accent); box-shadow:0 0 0 3px var(--accent-soft); }
.toc-list { list-style:none; margin:0; padding:0; overflow:auto; flex:1; }
.toc-list li { margin:0; }
.toc-list a { display:flex; gap:8px; align-items:baseline; padding:6px 8px; border-radius:6px;
              color:var(--fg); text-decoration:none; font-size:13px; line-height:1.5; }
.toc-list a:hover { background:var(--bg); }
.toc-list a.active { background:var(--accent-soft); box-shadow:inset 0 0 0 1px var(--accent); }
.toc-num { flex:none; width:20px; height:20px; border-radius:50%; background:var(--bg);
           border:1px solid var(--line); color:var(--muted); font-size:11px;
           display:inline-flex; align-items:center; justify-content:center; }
.toc-title { flex:1; }
.toc-flag { flex:none; font-size:11px; color:var(--warn); background:var(--warn-bg);
            border-radius:4px; padding:0 5px; }
.toc-empty { color:var(--faint); font-size:13px; padding:6px 8px; }
.toc-foot { font-size:11px; color:var(--faint); border-top:1px solid var(--line); padding-top:8px; }

/* 播放器 */
.player { position:sticky; top:0; z-index:6; background:var(--bg); padding:0 0 12px; }
.player video { width:100%; max-height:44vh; background:#000; border-radius:10px;
                border:1px solid var(--line); display:block; }
.player-hint { color:var(--faint); font-size:12px; margin-top:6px; }
.player.missing { color:var(--muted); font-size:13px; padding:14px 16px; background:var(--card);
                  border:1px dashed var(--line); border-radius:10px; margin-bottom:16px; }
.player-missing p { margin:0 0 10px; line-height:1.7; }
.player-missing p:last-child { margin-bottom:0; }
.player-note { color:var(--faint); font-size:12px; }
.player-error { color:#b3261e; font-size:12.5px; margin-top:8px; white-space:pre-line; }
.player-ok { color:#1a7f37; font-size:12px; margin-top:8px; }
.jump.pending { opacity:.55; }
.jump.pending::after { content:'（先选视频）'; font-size:11px; margin-left:2px; }
.player-switch { display:none; text-align:right; padding:0 10px 8px; }
.player-switch .pick-video { font-size:12px; padding:4px 10px; }
/* 文件选择框不能真的 display:none —— 那样点 label 也不会弹出选择框。
   用"视觉隐藏但保留在布局里"的标准写法。 */
.visually-hidden { position:absolute; width:1px; height:1px; padding:0; margin:-1px;
                   overflow:hidden; opacity:0; white-space:nowrap; border:0; }
.pick-video { display:inline-block; }
.pick-video { font:inherit; font-size:13px; padding:7px 16px; border-radius:8px; cursor:pointer;
              border:1px solid var(--line); background:var(--accent); color:#fff; }
.pick-video:hover { filter:brightness(1.06); }

.warn-banner { background:var(--warn-bg); border:1px solid var(--warn-line); color:#6b4e00;
               padding:10px 14px; border-radius:8px; margin-bottom:16px; font-size:13px; }

/* 步骤卡片 */
.step { background:var(--card); border:1px solid var(--line); border-radius:10px;
        padding:18px 20px 14px; margin-bottom:16px; }
.step.review { border-left:4px solid var(--warn-line); }
.step-head { display:flex; align-items:flex-start; justify-content:space-between; gap:14px; }
.step-title { display:flex; align-items:center; gap:10px; min-width:0; }
.num { flex:none; width:28px; height:28px; border-radius:50%; background:var(--accent);
       color:#fff; font-size:14px; display:inline-flex; align-items:center; justify-content:center; }
.step h2 { font-size:17px; margin:0; font-weight:600; word-break:break-word; }
.jump { flex:none; border:1px solid var(--accent); background:var(--card); color:var(--accent);
        cursor:pointer; padding:5px 11px; border-radius:6px; font-size:12px; font-family:inherit; }
.jump:hover { background:var(--accent); color:#fff; }
.time-chip { flex:none; color:var(--muted); font-size:12px; border:1px solid var(--line);
             border-radius:6px; padding:4px 9px; }
.step-meta { display:flex; flex-wrap:wrap; gap:10px; align-items:center;
             margin:6px 0 12px; font-size:12px; color:var(--muted); }
.time-range { font-variant-numeric:tabular-nums; }
.duration, .conf { color:var(--faint); }
.review-tag { background:var(--warn-bg); color:#6b4e00; border-radius:4px; padding:1px 7px; }
.review-note { background:var(--warn-bg); color:#6b4e00; padding:8px 12px;
               border-radius:6px; font-size:13px; margin-bottom:12px; }

.step-body { display:grid; grid-template-columns:minmax(0,1fr) minmax(0,42%); gap:18px;
             align-items:start; }
.step-text { min-width:0; }
.action { margin:0 0 10px; font-size:15px; }
.expect { margin:0; font-size:14px; color:var(--muted); }
.field { display:inline-block; font-size:11px; color:var(--accent); background:var(--accent-soft);
         border-radius:4px; padding:1px 7px; margin-right:8px; vertical-align:1px; }
.step-shots { display:grid; grid-template-columns:1fr; gap:8px; }
.step-shots.multi { grid-template-columns:1fr 1fr; }
.step-shots figure { margin:0; }
.step-shots img { width:100%; height:auto; border:1px solid var(--line); border-radius:8px; display:block;
                  cursor:zoom-in; }
.step-shots figcaption { color:var(--faint); font-size:11px; margin-top:4px;
                         font-variant-numeric:tabular-nums; }

/* 依据折叠 */
.evidence { margin-top:12px; border-top:1px dashed var(--line); padding-top:10px; }
.evidence summary { cursor:pointer; color:var(--muted); font-size:12px; list-style:none; }
.evidence summary::-webkit-details-marker { display:none; }
.evidence summary::before { content:"▸ "; color:var(--faint); }
.evidence[open] summary::before { content:"▾ "; }
.evidence-body { margin-top:8px; }
.ev-list { list-style:none; margin:0; padding:0; }
.ev-list li { font-size:13px; color:var(--muted); padding:5px 0; border-bottom:1px solid #eef1f4;
              display:flex; gap:8px; align-items:baseline; }
.ev-kind { flex:none; font-size:11px; color:var(--accent); background:var(--accent-soft);
           border-radius:4px; padding:0 6px; }
.ev-time { flex:none; color:var(--faint); font-size:12px; font-variant-numeric:tabular-nums; }
.ev-empty { color:var(--faint); font-size:13px; margin:0; }
.empty { color:var(--muted); }

/* 放大遮罩 */
.lightbox { display:none; position:fixed; inset:0; background:rgba(16,20,26,.86); z-index:50;
            padding:24px; cursor:zoom-out; }
.lightbox.on { display:flex; align-items:center; justify-content:center; }
.lightbox img { max-width:100%; max-height:100%; border-radius:6px; background:#fff; }
.lightbox-hint { position:fixed; bottom:18px; left:50%; transform:translateX(-50%);
                 color:#d0d7de; font-size:12px; }

/* 窄屏 */
@media (max-width: 980px) {
  .layout { grid-template-columns:1fr; padding:16px 14px 60px; }
  .toc { position:static; max-height:none; order:-1; }
  .toc-list { display:flex; flex-wrap:wrap; gap:6px; }
  .toc-list a { background:var(--bg); }
  .step-body { grid-template-columns:1fr; }
  .player { position:static; }
  .topbar-inner { padding:16px 14px 12px; }
}

/* 打印 */
@media print {
  body { background:#fff; font-size:12px; }
  .toc, .player, .jump, .lightbox, .player-hint { display:none !important; }
  .layout { display:block; padding:0; max-width:none; }
  .topbar { border-bottom:1px solid #ccc; }
  .step { break-inside:avoid; page-break-inside:avoid; border:1px solid #ccc;
          box-shadow:none; margin-bottom:10px; }
  .evidence { display:block; }
  .evidence summary { display:none; }
  .evidence-body { display:block !important; }
  .step-shots img { max-width:100%; }
}
"""

_JS = """
// 轻量手册里视频是用户临时选的，用 objectURL 播放；换文件时要释放旧的，否则会泄漏。
var _videoUrl = null;

function pickVideo() {
  var input = document.getElementById('video-file');
  if (input) { input.click(); }
}

function showVideoBox() {
  var box = document.getElementById('player-box');
  var v = document.getElementById('player');
  var missing = document.getElementById('player-missing');
  if (box) { box.classList.remove('missing'); }
  if (missing) { missing.style.display = 'none'; }
  if (v) { v.hidden = false; }
  var sw = box ? box.querySelector('.player-switch') : null;
  if (sw) { sw.style.display = 'block'; }   // 加载成功后仍然能换一个
  // 视频就绪后，所有时间按钮都恢复正常外观
  Array.prototype.forEach.call(document.querySelectorAll('.jump.pending'), function (b) {
    b.classList.remove('pending');
  });
}

// 显示一条播放器提示。
// 完整包（自带 video 标签）里没有 player-missing 这一块，
// 所以不能只在 missing 里找位置——那样提示会"静默消失"。
function showVideoNote(msg) {
  var missing = document.getElementById('player-missing');
  var box = _playerBox();
  if (!box) { return; }
  if (missing) {
    box.classList.add('missing');
    missing.style.display = '';
  }
  // 不管提示长什么样，都要留一条"能换视频"的路
  var sw0 = box.querySelector('.player-switch');
  if (sw0) { sw0.style.display = 'block'; }
  var tip = box.querySelector('.player-error');
  if (!tip) {
    tip = document.createElement('p');
    tip.className = 'player-error';
    box.appendChild(tip);
  }
  tip.textContent = msg;
}

// 当前视频是否和手册匹配。选错视频时置 false，跳转一律不执行——
// 时间戳对应的是原录屏，换了视频跳过去也是错的，那种"看起来能用"更糟糕。
var _videoOk = null;

function _playerBox() { return document.getElementById('player-box'); }

function _expectDuration() {
  var box = _playerBox();
  if (!box) { return 0; }
  var d = parseFloat(box.getAttribute('data-expect-duration') || '0');
  return isNaN(d) ? 0 : d;
}

function _expectName() {
  var box = _playerBox();
  return box ? (box.getAttribute('data-expect-name') || '') : '';
}

function _fmtDur(s) {
  s = Math.round(s || 0);
  if (s < 60) { return s + ' 秒'; }
  return Math.floor(s / 60) + ' 分 ' + (s % 60) + ' 秒';
}

// 用"时长"判断是不是同一段视频：改名不影响，换素材一定会变。
// 容差 1.5 秒，照顾不同播放器/封装对时长的计算差异。
function checkVideo() {
  var v = document.getElementById('player');
  if (!v || !v.duration || !isFinite(v.duration)) { return; }
  var expect = _expectDuration();
  if (!expect) { _videoOk = true; return; }
  var diff = Math.abs(v.duration - expect);
  _videoOk = diff <= 1.5;
  if (_videoOk) {
    var note = document.querySelector('.player-ok');
    if (!note) {
      note = document.createElement('p');
      note.className = 'player-ok';
      var box = _playerBox();
      if (box) { box.appendChild(note); }
    }
    note.textContent = '✓ 视频和手册匹配（' + _fmtDur(v.duration) + '）。点步骤里的时间按钮可以跳转。';
    return;
  }
  var name = _expectName();
  showVideoNote(
    '这个视频和手册对不上：手册对应的是' + (name ? '「' + name + '」，' : '')
    + '约 ' + _fmtDur(expect) + '，你选的是 ' + _fmtDur(v.duration) + '。 '
    + '时间戳只对原录屏才准，所以跳转已停用，请换成手册对应的那段视频。'
  );
}

function seekTo(t) {
  var v = document.getElementById('player');
  if (!v) { return; }
  // 轻量手册：还没选视频时给一句明确的下一步，而不是毫无反应
  if (!v.src && v.readyState === 0) {
    showVideoNote('还没有加载视频。请在上方点「选择本地视频…」选这段录屏，之后时间按钮才能跳转。');
    return;
  }
  if (_videoOk === false) {
    // 已经有一条更具体的说明（时长对比）时不要覆盖它
    if (!document.querySelector('.player-error')) {
      showVideoNote('当前加载的视频不是手册对应的那段，跳转已停用。请换成原录屏。');
    }
    return;
  }
  var go = function () {
    try {
      v.currentTime = t;
      var p = v.play();
      if (p && p.catch) { p.catch(function () {}); }
    } catch (e) {
      showVideoNote('这个浏览器没法跳到指定时间（可能不支持该视频格式）。');
    }
  };
  // 浏览器拿到元数据之前设 currentTime 会被忽略，所以要等 loadedmetadata
  if (v.readyState >= 1) { go(); }
  else { v.addEventListener('loadedmetadata', go, { once: true }); }
  // 让播放器回到视野里（用和目录跳转一样的算法，别被 sticky 挡住）
  var p = document.querySelector('.player');
  var top = (p ? p.getBoundingClientRect().top : 0) + window.pageYOffset - 12;
  try { window.scrollTo({ top: Math.max(0, top), behavior: 'smooth' }); }
  catch (e) { window.scrollTo(0, Math.max(0, top)); }
}

document.addEventListener('DOMContentLoaded', function () {
  var input = document.getElementById('video-file');
  var v = document.getElementById('player');
  if (input && v) {
    input.addEventListener('change', function (e) {
      var f = e.target.files && e.target.files[0];
      if (!f) { return; }
      if (_videoUrl) { URL.revokeObjectURL(_videoUrl); }
      _videoUrl = URL.createObjectURL(f);
      v.onloadedmetadata = function () {
        showVideoBox();
        checkVideo();          // 选完立刻判断是不是这段视频
      };
      v.onerror = function () {
        showVideoNote('这个文件无法在浏览器里播放（编码可能不受支持），换一个试试。');
      };
      v.src = _videoUrl;
      v.load();
    });
  }
  if (v) {
    v.addEventListener('error', function () {
      // 加载失败时一定要给一条出路：告诉用户为什么、并露出「换一个视频」
      showVideoNote('这个视频打不开：可能源视频已被移动，或浏览器不支持它的编码。'
                    + '可以点下面的「换一个视频…」手动指定源文件。');
      var sw = document.querySelector('.player-switch');
      if (sw) { sw.style.display = 'block'; }
      var hint = document.querySelector('.player-hint');
      if (hint) { hint.style.display = 'none'; }
    });
    // 完整包里的视频直接就是源文件，加载完也校验一次（防止用户替换了 media/ 里的文件）
    v.addEventListener('loadedmetadata', checkVideo, { once: true });
    if (v.readyState >= 1) { checkVideo(); }
  }
});

window.addEventListener('beforeunload', function () {
  if (_videoUrl) { URL.revokeObjectURL(_videoUrl); }
});

function openLightbox(img) {
  var lb = document.getElementById('lightbox');
  var big = document.getElementById('lightbox-img');
  if (!lb || !big) { return; }
  big.src = img.getAttribute('src');
  big.alt = img.getAttribute('alt') || '';
  lb.classList.add('on');
}

function closeLightbox() {
  var lb = document.getElementById('lightbox');
  if (lb) { lb.classList.remove('on'); }
}

document.addEventListener('keydown', function (e) {
  if (e.key === 'Escape') { closeLightbox(); }
});

// 打印时把折叠的依据全部展开：纸质手册上没有"点击展开"这回事，
// 不展开就等于把证据整段丢掉。打印结束后再恢复原来的折叠状态。
window.addEventListener('beforeprint', function () {
  Array.prototype.forEach.call(document.querySelectorAll('details'), function (d) {
    d.setAttribute('data-was-open', d.open ? '1' : '0');
    d.open = true;
  });
});
window.addEventListener('afterprint', function () {
  Array.prototype.forEach.call(document.querySelectorAll('details'), function (d) {
    if (d.getAttribute('data-was-open') === '0') { d.open = false; }
    d.removeAttribute('data-was-open');
  });
});

// 让锚点跳转让开 sticky 播放器的高度。
// 视频加载前后播放器高度会变（有视频 vs 没有视频、控制条出现），所以要多次同步。
(function () {
  function syncScrollPadding() {
    var p = document.querySelector('.player');
    var h = p ? Math.round(p.getBoundingClientRect().height) : 0;
    document.documentElement.style.scrollPaddingTop = (h + 12) + 'px';
  }
  if (document.readyState !== 'loading') { syncScrollPadding(); }
  else { document.addEventListener('DOMContentLoaded', syncScrollPadding); }
  window.addEventListener('resize', syncScrollPadding);
  window.addEventListener('load', syncScrollPadding);
  document.addEventListener('loadedmetadata', syncScrollPadding, true);
  var v = document.getElementById('player');
  if (v) { v.addEventListener('loadedmetadata', syncScrollPadding); }
})();

// 目录跳转 + 高亮
//
// 这里踩过两个坑，所以不用浏览器原生锚点：
// 1. 原生 href="#step-4" 的定位由浏览器算，它不知道顶部有个 sticky 播放器，
//    结果会被挡住一截；配合 scroll-padding-top 又容易在页面高度变化时算错。
// 2. 原生跳转和我们自己的平滑滚动叠加，出现过"点了第 4 步，滚到一半又弹回顶部"。
// 现在统一由 JS 接管：preventDefault + 自己算 top + 平滑滚动。
(function () {
  var links = Array.prototype.slice.call(document.querySelectorAll('.toc-list a'));
  if (!links.length) { return; }

  var lockUntil = 0;          // 手动跳转后的锁定窗口，期间不让观察器改高亮

  function playerHeight() {
    var p = document.querySelector('.player');
    return p ? p.getBoundingClientRect().height : 0;
  }

  function setActive(a) {
    links.forEach(function (x) { x.classList.remove('active'); });
    if (a) { a.classList.add('active'); }
  }

  function goTo(a) {
    var id = a.getAttribute('data-target');
    var sec = id ? document.getElementById(id) : null;
    if (!sec) { return; }
    var top = sec.getBoundingClientRect().top + window.pageYOffset - playerHeight() - 12;
    if (top < 0) { top = 0; }
    lockUntil = Date.now() + 900;
    setActive(a);
    try {
      window.scrollTo({ top: top, behavior: 'smooth' });
    } catch (e) {
      window.scrollTo(0, top);
    }
    // 截图是懒加载的，滚动过程中图片撑开会让目标位置往下跑，
    // 所以动画结束后再校正一次，否则用户点第 4 步可能停在半路。
    clearTimeout(a._fixTimer);
    a._fixTimer = setTimeout(function () {
      var now = sec.getBoundingClientRect().top + window.pageYOffset - playerHeight() - 12;
      if (Math.abs(window.pageYOffset - now) > 12) {
        window.scrollTo(0, Math.max(0, now));   // 瞬时校正，不再来一次动画
      }
      lockUntil = Date.now() + 400;
      setActive(a);
    }, 520);
    if (history.replaceState) {
      history.replaceState(null, '', '#' + id);   // 地址栏保留锚点，刷新能回到这一步
    }
  }

  links.forEach(function (a) {
    a.addEventListener('click', function (e) {
      e.preventDefault();          // 关键：不让浏览器再做一次原生锚点跳转
      goTo(a);
    });
  });

  // 高亮跟随滚动；但手动跳转后的 900ms 内保持不动，
  // 否则平滑滚动经过中间步骤时会把高亮抢回上一步，看起来像"点了 4 却停在 3"。
  if ('IntersectionObserver' in window) {
    var sections = links.map(function (a) {
      return document.getElementById(a.getAttribute('data-target'));
    }).filter(Boolean);
    if (sections.length) {
      var obs = new IntersectionObserver(function (entries) {
        if (Date.now() < lockUntil) { return; }
        entries.forEach(function (en) {
          if (!en.isIntersecting) { return; }
          links.forEach(function (a) {
            a.classList.toggle('active', a.getAttribute('data-target') === en.target.id);
          });
        });
      }, { rootMargin: '-10% 0px -70% 0px' });
      sections.forEach(function (s) { obs.observe(s); });
    }
  }

  // 从地址栏带锚点打开时，也走我们自己的定位逻辑
  window.addEventListener('load', function () {
    var id = (location.hash || '').replace('#', '');
    if (!id) { return; }
    var a = links.filter(function (x) { return x.getAttribute('data-target') === id; })[0];
    if (a) { goTo(a); }
  });
})();

// 目录筛选：只隐藏不匹配的步骤，不做跳转
(function () {
  var box = document.getElementById('toc-filter');
  if (!box) { return; }
  box.addEventListener('input', function () {
    var q = box.value.trim().toLowerCase();
    Array.prototype.forEach.call(document.querySelectorAll('.toc-list li'), function (li) {
      var a = li.querySelector('a');
      if (!a) { return; }
      var hay = (a.getAttribute('data-search') || a.textContent).toLowerCase();
      li.style.display = (!q || hay.indexOf(q) >= 0) ? '' : 'none';
    });
    // 只匹配「标题 + 操作 + 预期结果」，不匹配证据和截图说明，
    // 否则搜"保存"会把仅仅提到保存两个字的无关步骤也留下。
    Array.prototype.forEach.call(document.querySelectorAll('section.step'), function (sec) {
      var hay = (sec.getAttribute('data-search') || '').toLowerCase();
      sec.style.display = (!q || hay.indexOf(q) >= 0) ? '' : 'none';
    });
  });
})();
"""


def _head(title: str) -> str:
    return (
        '<!DOCTYPE html>\n<html lang="zh-CN">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{_e(title)}</title>\n"
        f"<style>{_CSS}</style>\n"
        "</head>"
    )


def _lightbox() -> str:
    return (
        '<div class="lightbox" id="lightbox" onclick="closeLightbox()">'
        '<img id="lightbox-img" alt="">'
        '<div class="lightbox-hint">点击任意处或按 Esc 关闭</div>'
        "</div>"
    )


def _script() -> str:
    return f"<script>{_JS}</script>"
