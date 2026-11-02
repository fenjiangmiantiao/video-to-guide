# -*- coding: utf-8 -*-
"""操作手册质量评测（Round 7 · R7-01）。

不评价"界面好不好看"，只回答四个能用数据回答的问题：

1. 关键操作有没有被覆盖？（人工标注的每个动作，能不能在 steps.json 里找到对应步骤）
2. 时间定位准不准？（步骤起点与人工标注时间点的偏差）
3. 证据站不站得住？（步骤引用的字幕/截图/画面描述是否真实存在）
4. 有没有"没证据却当成事实"的步骤？（evidence 为空但 needs_review=false）

评测只读运行目录，不改任何数据。它不能判断语义层面的对错——
"这个按钮是保存还是取消"仍然要人看截图。所以报告里把"机器能判定"和
"需要人工复核"分开列。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# 置信度低于这个值、又没有标 needs_review，就算"低置信度未标记"
LOW_CONFIDENCE = 0.7
# 步骤之间超过这个秒数没有任何覆盖，算时间空白
MAX_GAP = 20.0


# ---------------------------------------------------------------- 数据读取

def _load(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def load_labels(path: str | Path) -> dict:
    obj = json.loads(Path(path).read_text(encoding="utf-8"))
    actions = obj.get("expected_actions") or []
    for a in actions:
        a.setdefault("tol", 5.0)
        a.setdefault("keywords", [])
    obj["expected_actions"] = actions
    return obj


def load_run(run_dir: str | Path) -> dict:
    """读一次运行的数据。作业数据在新布局的 .work/ 下，旧任务在根目录。"""
    from .store import (FRAMES_JSON, STEPS_JSON, TRANSCRIPT_JSON, VIDEO_JSON,
                        VISION_JSON, rp)

    d = Path(run_dir)
    steps = _load(rp(d, STEPS_JSON), {"steps": []}).get("steps") or []
    transcript = _load(rp(d, TRANSCRIPT_JSON), {"segments": []}).get("segments") or []
    vision = _load(rp(d, VISION_JSON), {"notes": []}).get("notes") or []
    frames = _load(rp(d, FRAMES_JSON), {"frames": []}).get("frames") or []
    video = _load(rp(d, VIDEO_JSON), {})
    return {
        "dir": d,
        "steps": steps,
        "segments": transcript,
        "notes": vision,
        "frames": frames,
        "video": video,
    }


# ---------------------------------------------------------------- 评测主体

@dataclass
class ActionResult:
    id: str
    title: str
    expected_t: float
    tol: float
    # covered=命中且文字吻合 / merged=被合并进另一个动作的步骤 /
    # text_miss=时间对上但文字不符 / time_miss=找到步骤但时间偏了 / missing=没有对应步骤
    status: str = "missing"
    step_index: int | None = None
    step_t: float | None = None
    # 标注时间点到步骤时间区间的距离，0 表示落在区间内
    time_error: float | None = None
    # 步骤起点与标注时间点的差，用来观察"步骤起点提前/推后了多少"
    start_offset: float | None = None
    detail: str = ""


# ---------------------------------------------------------------- 粒度评测
@dataclass
class GranularityResult:
    """步骤粒度：把"拆碎"和"合并"分开计量。

    为什么要单独测：只记"步骤数"看不出问题。同一个视频，模型可能把 5 个操作
    写成 11 步（拆碎），也可能写成 3 步（合并），两种都让"步骤数"偏离，
    但用户遇到的麻烦完全相反——拆碎要反复翻页，合并会漏掉关键动作。

    做法：人工标注分两层
      events      = 视频里真实发生的原子操作（可独立观察）
      ideal_steps = 为了让人能复现，手册应该怎么组织（可以捆几个 event）
    然后看生成的步骤与 ideal_steps 是不是一对一。
    """

    ideal_count: int = 0
    one_to_one: list[str] = field(default_factory=list)
    over_split: list[dict] = field(default_factory=list)    # 一个理想步骤被拆成多步
    wrongly_merged: list[dict] = field(default_factory=list)  # 一步覆盖多个理想步骤
    missed: list[str] = field(default_factory=list)         # 完全没被表达
    extra: list[dict] = field(default_factory=list)         # 生成步骤不对应任何理想步骤

    @property
    def one_to_one_ratio(self) -> float:
        return len(self.one_to_one) / self.ideal_count if self.ideal_count else 0.0

    @property
    def split_rate(self) -> float:
        return len(self.over_split) / self.ideal_count if self.ideal_count else 0.0

    @property
    def merge_rate(self) -> float:
        return len(self.wrongly_merged) / self.ideal_count if self.ideal_count else 0.0

    def to_dict(self) -> dict:
        return {
            "ideal_count": self.ideal_count,
            "one_to_one": self.one_to_one,
            "one_to_one_ratio": round(self.one_to_one_ratio, 3),
            "over_split": self.over_split,
            "split_rate": round(self.split_rate, 3),
            "wrongly_merged": self.wrongly_merged,
            "merge_rate": round(self.merge_rate, 3),
            "missed": self.missed,
            "extra": self.extra,
        }


@dataclass
class Report:
    video: str = ""
    run_dir: str = ""
    duration: float = 0.0
    step_count: int = 0
    actions: list[ActionResult] = field(default_factory=list)
    extra_steps: list[dict] = field(default_factory=list)
    invalid_refs: list[dict] = field(default_factory=list)
    unsupported: list[dict] = field(default_factory=list)
    unmarked_low_conf: list[dict] = field(default_factory=list)
    time_gaps: list[dict] = field(default_factory=list)
    needs_review_count: int = 0
    # 每个步骤的"证据能不能支持它的说法"：{"index", "step", "missing": [...]}
    weak_evidence: list[dict] = field(default_factory=list)
    # 步骤粒度（拆碎 / 合并 / 漏 / 多余）
    granularity: GranularityResult = field(default_factory=GranularityResult)

    @property
    def covered(self) -> list[ActionResult]:
        return [a for a in self.actions if a.status == "covered"]

    @property
    def coverage_rate(self) -> float:
        return len(self.covered) / len(self.actions) if self.actions else 0.0

    @property
    def merged(self) -> list[ActionResult]:
        return [a for a in self.actions if a.status == "merged"]

    @property
    def independent_recall(self) -> float:
        """独立事件召回率：被单独成步呈现的标注事件占比。

        被合并进别人步骤的事件不算"独立呈现"——用户照着手册做的时候，
        看不到那一步就是漏了。
        """
        return len(self.covered) / len(self.actions) if self.actions else 0.0

    @property
    def step_precision(self) -> float:
        """步骤精确率：生成的步骤里，有多少能对上人工标注的动作。"""
        if not self.step_count:
            return 0.0
        matched = len({a.step_index for a in self.actions if a.step_index is not None})
        return min(1.0, matched / self.step_count)

    @property
    def max_time_error(self) -> float | None:
        errs = [a.time_error for a in self.actions if a.time_error is not None]
        return max(errs) if errs else None

    @property
    def mean_time_error(self) -> float | None:
        errs = [a.time_error for a in self.actions if a.time_error is not None]
        return sum(errs) / len(errs) if errs else None

    def to_dict(self) -> dict:
        return {
            "video": self.video,
            "run_dir": self.run_dir,
            "duration": self.duration,
            "step_count": self.step_count,
            "needs_review_count": self.needs_review_count,
            "coverage_rate": round(self.coverage_rate, 3),
            "independent_recall": round(self.independent_recall, 3),
            "step_precision": round(self.step_precision, 3),
            "merged_count": len(self.merged),
            "weak_evidence_count": len(self.weak_evidence),
            "weak_evidence": self.weak_evidence,
            "max_time_error": None if self.max_time_error is None else round(self.max_time_error, 2),
            "mean_time_error": None if self.mean_time_error is None else round(self.mean_time_error, 2),
            "actions": [vars(a) for a in self.actions],
            "extra_steps": self.extra_steps,
            "invalid_refs": self.invalid_refs,
            "unsupported": self.unsupported,
            "unmarked_low_conf": self.unmarked_low_conf,
            "time_gaps": self.time_gaps,
            "granularity": self.granularity.to_dict(),
        }


_QUOTED = re.compile(r"「([^」]{1,24})」")
_SPLIT = re.compile(r"[：:，,。、\s]+")


def _quoted_names(text: str) -> list[str]:
    """步骤文字里用「」标出的界面元素名——这些必须能在证据里找到出处。

    「状态：设置已生效」这种写法会先按标点拆成词再核对，避免因为多了一个
    冒号就把正常引用判成"编造"。
    """
    names: list[str] = []
    for raw in _QUOTED.findall(text or ""):
        parts = [p for p in _SPLIT.split(raw) if len(p) >= 2]
        names.extend(parts or [raw])
    return names


def _evidence_text(step: dict, seg_by_id: dict, note_by_id: dict,
                   note_by_frame: dict | None = None) -> str:
    """步骤实际能拿到的全部证据文字。

    截图本身就是证据：即使步骤没写 vision_ids，只要挂了 frame_id，
    那张图的画面描述也应该算它的依据。
    """
    ev = step.get("evidence") or {}
    parts: list[str] = []
    for tid in ev.get("transcript_ids") or []:
        s = seg_by_id.get(tid)
        if s:
            parts.append(str(s.get("text") or ""))
    notes = [note_by_id.get(vid) for vid in ev.get("vision_ids") or []]
    if note_by_frame:
        notes += [note_by_frame.get(fid) for fid in step.get("frame_ids") or []]
    for n in notes:
        if n:
            parts.append(str(n.get("visible_text") or ""))
            parts.append(str(n.get("activity") or ""))
    return " ".join(parts)


def _text_of(step: dict) -> str:
    return " ".join(str(step.get(k) or "") for k in ("title", "action", "expected_result"))


def _hit_keywords(step: dict, keywords: list[str]) -> bool:
    if not keywords:
        return True
    text = _text_of(step)
    return any(k and k in text for k in keywords)


def _span(step: dict) -> tuple[float, float]:
    s = float(step.get("t_start") or 0.0)
    e = float(step.get("t_end") or s)
    return (s, e if e >= s else s)


def _strict_covers(step: dict, t: float) -> bool:
    s, e = _span(step)
    return s <= t <= e


def _covers(step: dict, t: float, tol: float) -> bool:
    """标注时间点是否落在步骤的时间区间内（含容差）。

    用区间包含而不是"起点接近"，因为一个步骤经常把"介绍界面"和"第一次操作"
    并在一起，起点会比操作时刻早十几秒——那不算时间定位错误。
    """
    s, e = _span(step)
    return (s - tol) <= t <= (e + tol)


def _dist(step: dict, t: float) -> float:
    s, e = _span(step)
    if s <= t <= e:
        return 0.0
    return min(abs(t - s), abs(t - e))


def _match_actions(steps: list[dict], actions: list[dict]) -> list[ActionResult]:
    results = [ActionResult(id=a["id"], title=a["title"],
                            expected_t=float(a["t"]), tol=float(a["tol"])) for a in actions]
    owners: dict[int, list[str]] = {}      # 步骤下标 -> 已经占用它的标注 id

    def assign(res: ActionResult, idx: int, step: dict, merged: bool, text_ok: bool) -> None:
        res.step_index = int(step.get("index", idx + 1))
        res.step_t = float(step.get("t_start") or 0.0)
        res.time_error = round(_dist(step, res.expected_t), 2)
        res.start_offset = round(res.step_t - res.expected_t, 2)
        owners.setdefault(idx, []).append(res.id)
        if merged and text_ok:
            res.status = "merged"
            res.detail = "与 %s 落在同一个步骤里（模型把两个操作合并了）" % "/".join(
                x for x in owners[idx] if x != res.id)
        elif merged:
            # 落在别人占用的步骤里，但那一步讲的是另一个操作 —— 这是漏了，不是合并
            res.status = "missing"
            res.detail = "该时刻落在步骤 #%d 的区间里，但那一步讲的是别的操作，本动作没有被单独记录" % res.step_index
        elif text_ok:
            res.status = "covered"
        else:
            res.status = "text_miss"
            res.detail = "时间对得上，但步骤文字里没有出现标注关键词：" + "/".join(
                actions[results.index(res)]["keywords"])

    # 第一轮：时间区间包含。优先"未被占用 + 文字吻合 + 区间最窄"
    for idx, res in enumerate(results):
        kws = actions[idx]["keywords"]
        cands = [(i, s) for i, s in enumerate(steps) if _covers(s, res.expected_t, res.tol)]
        if not cands:
            continue
        # 分层挑选：先看"没被占用 + 文字对得上"，再看"严格落在区间内"。
        # 单独用任何一个都会错：只按严格包含，0.4 秒的边界误差会把动作判给前一步；
        # 只按文字命中，"自动保存"里的"保存"会抢走"点击保存"。
        strict = [(i, s) for i, s in cands if _strict_covers(s, res.expected_t)]
        tiers = [
            [(i, s) for i, s in strict if i not in owners and _hit_keywords(s, kws)],
            [(i, s) for i, s in cands if i not in owners and _hit_keywords(s, kws)],
            [(i, s) for i, s in strict if i not in owners],
            [(i, s) for i, s in cands if i not in owners],
            [(i, s) for i, s in cands if _hit_keywords(s, kws)],
            cands,
        ]
        pool = next((t for t in tiers if t), cands)
        i, s = min(pool, key=lambda p: (_span(p[1])[1] - _span(p[1])[0], _dist(p[1], res.expected_t)))
        assign(res, i, s, merged=(i in owners), text_ok=_hit_keywords(s, kws))

    # 第二轮：时间不在区间内，但全文能找到对应步骤 —— 操作识别到了，时间定位偏了
    for idx, res in enumerate(results):
        # 只捞"第一轮完全没碰到任何步骤"的；已经判定为落在别人区间里的不算时间偏，
        # 否则会被一个罗列了所有控件的步骤（"对话框里包含「外观」「主题：」…"）误捞走。
        if res.status != "missing" or res.step_index is not None:
            continue
        cands = [(i, s) for i, s in enumerate(steps) if _hit_keywords(s, actions[idx]["keywords"])]
        if not cands:
            continue
        i, s = min(cands, key=lambda p: _dist(p[1], res.expected_t))
        res.step_index = int(s.get("index", i + 1))
        res.step_t = float(s.get("t_start") or 0.0)
        res.time_error = round(_dist(s, res.expected_t), 2)
        res.start_offset = round(res.step_t - res.expected_t, 2)
        res.status = "time_miss"
        res.detail = "找到了对应步骤，但标注时刻不在它的时间区间内，相差 %.1fs" % res.time_error

    for res in results:
        if res.status == "missing":
            res.detail = "steps.json 里没有任何步骤对应该操作"

    return results




def ideal_steps_of(labels: dict) -> list[dict]:
    """取标注里的理想步骤；旧标注没有这一层时，退化成"一个事件一步"。"""
    ideal = labels.get("ideal_steps")
    if ideal:
        return list(ideal)
    return [{"id": a["id"], "title": a.get("title", ""), "event_ids": [a["id"]]}
            for a in labels.get("expected_actions") or []]


def evaluate_granularity(actions: list[ActionResult], labels: dict,
                         steps: list[dict]) -> GranularityResult:
    """判断每个理想步骤是否被"一对一"地表达出来。

    输入用的 actions 是事件级匹配结果（已经有 step_index）。
    """
    ideal = ideal_steps_of(labels)
    res = GranularityResult(ideal_count=len(ideal))
    if not ideal:
        return res

    by_event = {a.id: a for a in actions}
    # 每个理想步骤命中的生成步骤集合
    hit_map: dict[str, set] = {}
    for iso in ideal:
        got = set()
        for eid in iso.get("event_ids") or []:
            a = by_event.get(eid)
            if a and a.step_index is not None and a.status in ("covered", "merged"):
                got.add(a.step_index)
        hit_map[iso["id"]] = got

    # 反向：每个生成步骤覆盖了几个理想步骤
    step_to_ideals: dict[int, list[str]] = {}
    for iso in ideal:
        for idx in hit_map[iso["id"]]:
            step_to_ideals.setdefault(idx, []).append(iso["id"])

    used_steps: set[int] = set()
    for iso in ideal:
        idxs = hit_map[iso["id"]]
        if not idxs:
            res.missed.append(iso["id"])
        elif len(idxs) == 1:
            res.one_to_one.append(iso["id"])
            used_steps.add(next(iter(idxs)))
        else:
            res.over_split.append({"ideal": iso["id"], "title": iso.get("title", ""),
                                   "steps": sorted(idxs)})

    for idx, iso_ids in step_to_ideals.items():
        if len(iso_ids) > 1:
            res.wrongly_merged.append({"step": idx, "ideals": iso_ids,
                                       "titles": [next((x.get("title", "") for x in ideal
                                                        if x["id"] == i), i)
                                                  for i in iso_ids]})

    # 生成步骤里既不落在任何理想步骤、也不被事件命中的，算多余
    for i, s in enumerate(steps):
        idx = int(s.get("index", i + 1))
        if idx in step_to_ideals:
            continue
        res.extra.append({"step": idx, "title": s.get("title", ""),
                          "t_start": s.get("t_start"), "t_end": s.get("t_end")})
    return res


def evaluate_run(run_dir: str | Path, labels: dict) -> Report:
    run = load_run(run_dir)
    steps = run["steps"]
    video = run["video"]

    rep = Report(
        video=str(video.get("path") or labels.get("video") or ""),
        run_dir=str(run["dir"]),
        duration=float(video.get("duration") or labels.get("duration") or 0.0),
        step_count=len(steps),
        needs_review_count=sum(1 for s in steps if s.get("needs_review")),
    )
    rep.actions = _match_actions(steps, labels.get("expected_actions") or [])
    rep.granularity = evaluate_granularity(rep.actions, labels, steps)

    matched_steps = {a.step_index for a in rep.actions if a.step_index is not None}
    rep.extra_steps = [
        {"index": s.get("index"), "t_start": s.get("t_start"), "title": s.get("title")}
        for s in steps if s.get("index") not in matched_steps
    ]

    seg_ids = {s.get("id") for s in run["segments"]}
    note_ids = {n.get("id") for n in run["notes"]}
    frame_by_id = {f.get("id"): f for f in run["frames"]}
    seg_by_id = {s.get("id"): s for s in run["segments"]}
    note_by_id = {n.get("id"): n for n in run["notes"]}
    note_by_frame = {n.get("frame_id"): n for n in run["notes"]}

    for s in steps:
        idx = s.get("index")
        ev = s.get("evidence") or {}
        t_ids = ev.get("transcript_ids") or []
        v_ids = ev.get("vision_ids") or []
        f_ids = s.get("frame_ids") or []

        for tid in t_ids:
            if tid not in seg_ids:
                rep.invalid_refs.append({"step": idx, "kind": "transcript", "ref": tid})
        for vid in v_ids:
            if vid not in note_ids:
                rep.invalid_refs.append({"step": idx, "kind": "vision", "ref": vid})
        for fid in f_ids:
            f = frame_by_id.get(fid)
            if f is None:
                rep.invalid_refs.append({"step": idx, "kind": "frame", "ref": fid})
                continue
            p = Path(str(f.get("path") or ""))
            if not p.is_absolute():
                p = run["dir"] / p
            if not p.exists():
                rep.invalid_refs.append({"step": idx, "kind": "frame_file", "ref": fid})

        if not t_ids and not v_ids and not s.get("needs_review"):
            rep.unsupported.append({"index": idx, "title": s.get("title")})

        conf = s.get("confidence")
        if isinstance(conf, (int, float)) and conf < LOW_CONFIDENCE and not s.get("needs_review"):
            rep.unmarked_low_conf.append({"index": idx, "title": s.get("title"), "confidence": conf})

        # 步骤里用「」写出的界面元素，必须能在它引用的证据文字里找到
        ev_text = _evidence_text(s, seg_by_id, note_by_id, note_by_frame)
        names = _quoted_names((s.get("title") or "") + " " + (s.get("action") or ""))
        if names and ev_text:
            missing = [n for n in names if n not in ev_text]
            if missing:
                rep.weak_evidence.append({
                    "index": idx, "step": s.get("title"), "missing": missing,
                    "note": "这些界面元素名没有出现在该步骤引用的字幕或画面描述里",
                })

    # 时间空白：相邻步骤之间、开头、结尾
    spans = sorted(((float(s.get("t_start") or 0), float(s.get("t_end") or 0)) for s in steps))
    prev_end = 0.0
    for start, end in spans:
        if start - prev_end > MAX_GAP:
            rep.time_gaps.append({"from": round(prev_end, 1), "to": round(start, 1),
                                  "seconds": round(start - prev_end, 1)})
        prev_end = max(prev_end, end)
    if rep.duration and rep.duration - prev_end > MAX_GAP:
        rep.time_gaps.append({"from": round(prev_end, 1), "to": round(rep.duration, 1),
                              "seconds": round(rep.duration - prev_end, 1)})

    return rep


# ---------------------------------------------------------------- 报告输出

def to_markdown(rep: Report, labels: dict | None = None) -> str:
    labels = labels or {}
    lines: list[str] = []
    lines.append("# 手册质量评测：" + (labels.get("video") or Path(rep.video).name or rep.run_dir))
    lines.append("")
    if labels.get("note"):
        lines.append("> " + labels["note"])
        lines.append("")
    lines.append("| 指标 | 结果 |")
    lines.append("| --- | --- |")
    lines.append("| 视频时长 | %.1f 秒 |" % rep.duration)
    lines.append("| 生成步骤数 | %d（其中待核实 %d） |" % (rep.step_count, rep.needs_review_count))
    lines.append("| 独立事件召回 | %d/%d（%.0f%%） |" % (
        len(rep.covered), len(rep.actions), rep.independent_recall * 100))
    if rep.merged:
        lines.append("| 被合并进同一步骤 | %d（%s） |" % (
            len(rep.merged), "、".join(a.id for a in rep.merged)))
    lines.append("| 步骤精确率 | %.0f%%（%d 个步骤里 %d 个对得上标注） |" % (
        rep.step_precision * 100, rep.step_count,
        len({a.step_index for a in rep.actions if a.step_index is not None})))
    lines.append("| 证据支持不足 | %d 条 |" % len(rep.weak_evidence))
    lines.append("| 动作定位误差（最大/平均） | %s / %s 秒 |" % (
        "-" if rep.max_time_error is None else "%.1f" % rep.max_time_error,
        "-" if rep.mean_time_error is None else "%.1f" % rep.mean_time_error))
    lines.append("| 证据引用失效 | %d 处 |" % len(rep.invalid_refs))
    lines.append("| 无证据却未标记待核实 | %d 条 |" % len(rep.unsupported))
    lines.append("| 低置信度未标记 | %d 条 |" % len(rep.unmarked_low_conf))
    lines.append("| 时间空白（>%.0f 秒） | %d 段 |" % (MAX_GAP, len(rep.time_gaps)))
    g = rep.granularity
    if g.ideal_count:
        lines.append("| **步骤粒度一对一** | **%d/%d（%.0f%%）** |" % (
            len(g.one_to_one), g.ideal_count, g.one_to_one_ratio * 100))
        lines.append("| 过度拆分 | %d 处（拆分率 %.0f%%） |" % (
            len(g.over_split), g.split_rate * 100))
        lines.append("| 错误合并 | %d 处（合并率 %.0f%%） |" % (
            len(g.wrongly_merged), g.merge_rate * 100))
        if g.missed:
            lines.append("| 完全遗漏 | %s |" % "、".join(g.missed))
        lines.append("| 不对应任何标注的步骤 | %d 个 |" % len(g.extra))
    lines.append("")

    lines.append("## 逐项对照")
    lines.append("")
    lines.append("| 标注动作 | 时间点 | 结果 | 对应步骤 | 区间偏差 | 起点偏差 | 说明 |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- |")
    mark = {"covered": "✅ 覆盖", "merged": "🔗 被合并", "time_miss": "⚠️ 时间偏",
            "text_miss": "⚠️ 文字不符", "missing": "❌ 遗漏"}
    for a in rep.actions:
        lines.append("| %s %s | %.1fs | %s | %s | %s | %s | %s |" % (
            a.id, a.title, a.expected_t, mark.get(a.status, a.status),
            "-" if a.step_index is None else "#%d" % a.step_index,
            "-" if a.time_error is None else "%.1fs" % a.time_error,
            "-" if a.start_offset is None else "%+.1fs" % a.start_offset,
            a.detail or "-"))
    lines.append("")

    if rep.extra_steps:
        lines.append("## 额外步骤（未被标注覆盖，需人工判断是否合理）")
        lines.append("")
        for s in rep.extra_steps:
            lines.append("- #%s @%.1fs %s" % (s.get("index"), float(s.get("t_start") or 0), s.get("title")))
        lines.append("")

    if rep.invalid_refs:
        lines.append("## 证据引用失效")
        lines.append("")
        for r in rep.invalid_refs:
            lines.append("- 步骤 #%s 的 %s 引用找不到：%s" % (r["step"], r["kind"], r["ref"]))
        lines.append("")

    if rep.unsupported:
        lines.append("## 无证据却未标记待核实（必须修复）")
        lines.append("")
        for s in rep.unsupported:
            lines.append("- #%s %s" % (s["index"], s["title"]))
        lines.append("")

    if rep.unmarked_low_conf:
        lines.append("## 低置信度未标记待核实")
        lines.append("")
        for s in rep.unmarked_low_conf:
            lines.append("- #%s %s（置信度 %.2f）" % (s["index"], s["title"], s["confidence"]))
        lines.append("")

    if rep.weak_evidence:
        lines.append("## 证据支持不足")
        lines.append("")
        lines.append("步骤里写了具体的界面元素名，但它引用的字幕和画面描述里没有出现这些名字——"
                     "可能模型自己编了按钮名。需要人看截图确认。")
        lines.append("")
        for w in rep.weak_evidence:
            lines.append("- #%s %s：%s" % (w["index"], w["step"], "、".join("「%s」" % x for x in w["missing"])))
        lines.append("")

    if rep.time_gaps:
        lines.append("## 时间空白")
        lines.append("")
        for g in rep.time_gaps:
            lines.append("- %.1fs → %.1fs（%.1f 秒没有任何步骤）" % (g["from"], g["to"], g["seconds"]))
        lines.append("")

    lines.append("## 需要人工复核的部分")
    lines.append("")
    lines.append("机器只能判定「有没有对应步骤」「时间对不对」「引用的数据存不存在」。"
                 "以下内容必须人看截图确认：步骤描述是否与画面一致、按钮名称是否准确、"
                 "有没有把推测写成已发生的操作。")
    lines.append("")
    return "\n".join(lines)


def evaluate_to_files(run_dir: str | Path, labels_path: str | Path, out_dir: str | Path) -> tuple[Path, Path]:
    labels = load_labels(labels_path)
    rep = evaluate_run(run_dir, labels)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stem = Path(str(labels.get("video") or "run")).stem
    jp = out / (stem + "-eval.json")
    mp = out / (stem + "-eval.md")
    jp.write_text(json.dumps(rep.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    mp.write_text(to_markdown(rep, labels), encoding="utf-8")
    return jp, mp
