"""步骤校验测试：这是"防止 AI 编造"的核心闸门，规则必须稳定。"""

from video_to_guide.models import Frame, Step, StepEvidence, Transcript, TranscriptSegment, VisionNote
from video_to_guide.steps.validate import export_blockers, has_evidence, validate_steps


def _ctx():
    frames = [Frame(id="f0", time=0.0, end=10.0, path="frames/a.jpg"),
              Frame(id="f1", time=10.0, end=20.0, path="frames/b.jpg")]
    transcript = Transcript(segments=[
        TranscriptSegment(id="t0", start=0.0, end=5.0, text="打开设置"),
        TranscriptSegment(id="t1", start=10.0, end=15.0, text="修改主题"),
    ])
    notes = [VisionNote(id="v0", frame_id="f0", interface="主界面"),
             VisionNote(id="v1", frame_id="f1", interface="设置窗口")]
    return frames, transcript, notes, 20.0


def test_drops_nonexistent_evidence():
    frames, tr, notes, dur = _ctx()
    step = Step(index=1, t_start=1.0, t_end=3.0, title="x", action="y",
                frame_ids=["f0", "f99"],
                evidence=StepEvidence(transcript_ids=["t0", "t404"], vision_ids=["v0", "v404"]))
    steps, warns = validate_steps([step], frames, tr, notes, dur)
    assert steps[0].frame_ids == ["f0"]
    assert steps[0].evidence.transcript_ids == ["t0"]
    assert steps[0].evidence.vision_ids == ["v0"]
    assert any("不存在的证据" in w for w in warns)


def test_clamps_time_and_sorts():
    frames, tr, notes, dur = _ctx()
    late = Step(index=1, t_start=50.0, t_end=90.0, title="后", action="a",
                frame_ids=["f1"], evidence=StepEvidence(transcript_ids=["t1"]))
    early = Step(index=2, t_start=-5.0, t_end=2.0, title="前", action="b",
                 frame_ids=["f0"], evidence=StepEvidence(transcript_ids=["t0"]))
    steps, _ = validate_steps([late, early], frames, tr, notes, dur)
    assert [s.title for s in steps] == ["前", "后"]
    assert steps[0].t_start == 0.0
    assert steps[1].t_end == 20.0
    assert [s.index for s in steps] == [1, 2]


def test_zero_length_step_gets_min_duration():
    frames, tr, notes, dur = _ctx()
    step = Step(index=1, t_start=5.0, t_end=5.0, title="x", action="y",
                frame_ids=["f0"], evidence=StepEvidence(transcript_ids=["t0"]))
    steps, warns = validate_steps([step], frames, tr, notes, dur)
    assert steps[0].t_end > steps[0].t_start
    assert any("时间范围不合法" in w for w in warns)


def test_attaches_nearest_frame_when_missing():
    frames, tr, notes, dur = _ctx()
    step = Step(index=1, t_start=11.0, t_end=13.0, title="x", action="y",
                evidence=StepEvidence(transcript_ids=["t1"]))
    steps, _ = validate_steps([step], frames, tr, notes, dur)
    assert steps[0].frame_ids == ["f1"]


def test_auto_attaches_vision_note_by_frame():
    """模型只写了 frame_ids 时，按 frame 反查把画面描述挂上。

    不这么做的话，整份手册都会被判成"只有截图"，可画面描述其实就在
    vision.json 里——证据存在却标"缺证据"，会让用户学会忽略这些标记。
    """
    frames, tr, notes, dur = _ctx()
    step = Step(index=1, t_start=1.0, t_end=3.0, title="凭空", action="编的")
    steps, warns = validate_steps([step], frames, tr, notes, dur)
    assert steps[0].frame_ids                       # 自动挂了最近截图
    assert steps[0].evidence.vision_ids             # 并补上了对应的画面描述
    assert "screenshot_only" not in steps[0].review_reasons


def test_screenshot_only_when_no_note_exists():
    """确实找不到画面描述时，才标"只有截图"。"""
    frames, tr, notes, dur = _ctx()
    step = Step(index=1, t_start=1.0, t_end=3.0, title="凭空", action="编的")
    steps, _ = validate_steps([step], frames, tr, [], dur)   # 不提供任何画面描述
    assert steps[0].evidence.vision_ids == []
    assert "screenshot_only" in steps[0].review_reasons
    assert steps[0].needs_review


def test_low_confidence_alone_is_not_enough():
    """模型自评低只是参考：证据齐全、术语对得上时不该只因为"没把握"就要求人工复核。

    真实录屏里 8 步全标待核实，用户看到一片黄条，等于没有提示。
    现在只有"低置信度 + 具体缺陷"才标。
    """
    frames, tr, notes, dur = _ctx()
    step = Step(index=1, t_start=1.0, t_end=3.0, title="x", action="y", confidence=0.2,
                frame_ids=["f0"], evidence=StepEvidence(transcript_ids=["t0"]))
    steps, _ = validate_steps([step], frames, tr, notes, dur)
    assert steps[0].needs_review is False


def test_low_confidence_with_defect_is_reported():
    """低置信度 + 证据有缺陷时，原因里要同时出现两条。"""
    frames, tr, notes, dur = _ctx()
    step = Step(index=1, t_start=1.0, t_end=3.0, title="x", action="点击「保存」",
                confidence=0.2, frame_ids=["f0"],
                evidence=StepEvidence(transcript_ids=["t0"]))
    steps, _ = validate_steps([step], frames, tr, notes, dur)
    assert steps[0].needs_review is True
    assert "ambiguous_target" in steps[0].review_reasons
    assert "low_confidence" in steps[0].review_reasons


def test_overlap_warning():
    frames, tr, notes, dur = _ctx()
    a = Step(index=1, t_start=0.0, t_end=10.0, title="A", action="a",
             frame_ids=["f0"], evidence=StepEvidence(transcript_ids=["t0"]))
    b = Step(index=2, t_start=5.0, t_end=12.0, title="B", action="b",
             frame_ids=["f1"], evidence=StepEvidence(transcript_ids=["t1"]))
    _, warns = validate_steps([a, b], frames, tr, notes, dur)
    assert any("时间重叠" in w for w in warns)


def test_has_evidence_and_blockers():
    empty = Step(index=1, t_start=0.0, t_end=1.0, title="x", action="y")
    assert not has_evidence(empty)
    assert export_blockers([empty])

    ok = Step(index=2, t_start=0.0, t_end=1.0, title="x", action="y", frame_ids=["f0"])
    assert has_evidence(ok)
    assert export_blockers([ok]) == []


def test_empty_input():
    frames, tr, notes, dur = _ctx()
    steps, warns = validate_steps([], frames, tr, notes, dur)
    assert steps == []
    assert any("没有剩下任何步骤" in w for w in warns)


# ---------------------------------------------------------------- 视频模式差异
#
# uncertain_time（「整屏切换可能只是切了窗口」）是给软件录屏写的规则。
# 实拍视频里画面大改就是真的发生了动作，套用这条规则会大面积误报：
# 番茄炒蛋实测 27 步里有 12 步被标待核实，全部是这个原因。


def _window_switch_ctx():
    frames = [Frame(id="f0", time=0.0, end=10.0, path="frames/a.jpg",
                    kind="window_switch")]
    transcript = Transcript(segments=[TranscriptSegment(id="t0", start=0.0, end=5.0,
                                                        text="把番茄倒进锅里")])
    notes = [VisionNote(id="v0", frame_id="f0", interface="灶台",
                        activity="把番茄倒进锅里")]
    return frames, transcript, notes, 10.0


def test_window_switch_frame_flags_review_in_software_mode():
    """软件录屏：整屏切换 + 无可信证据 → 仍要人工复核。"""
    frames, tr, notes, dur = _window_switch_ctx()
    step = Step(index=1, t_start=1.0, t_end=3.0, title="切换", action="点开设置",
                frame_ids=["f0"], evidence=StepEvidence(vision_ids=["v0"]))
    steps, _ = validate_steps([step], frames, tr, notes, dur)   # 默认 software
    assert steps[0].needs_review
    assert "uncertain_time" in steps[0].review_reasons


def test_window_switch_frame_ignored_in_reality_mode():
    """实拍视频：同一份数据不该因为「画面整屏变化」被标待核实。"""
    frames, tr, notes, dur = _window_switch_ctx()
    step = Step(index=1, t_start=1.0, t_end=3.0, title="倒番茄", action="把番茄倒进锅里",
                frame_ids=["f0"], evidence=StepEvidence(vision_ids=["v0"]))
    steps, _ = validate_steps([step], frames, tr, notes, dur, video_mode="reality")
    assert not steps[0].needs_review
    assert steps[0].review_reasons == []


def test_reality_mode_still_flags_screenshot_only():
    """放宽的只有窗口切换那一条，其余闸门在现实模式下必须照旧。

    这里故意不给画面描述：没有 vision 可反查，一步只剩一张截图，
    就应该照旧要求人工复核。
    """
    frames, tr, notes, dur = _window_switch_ctx()
    step = Step(index=1, t_start=1.0, t_end=3.0, title="只有截图", action="切菜")
    steps, _ = validate_steps([step], frames, tr, [], dur, video_mode="reality")
    assert steps[0].frame_ids == ["f0"]           # 自动补了最近截图
    assert steps[0].needs_review
    assert "screenshot_only" in steps[0].review_reasons
