# -*- coding: utf-8 -*-
"""覆盖检查：找出没被步骤覆盖的时间段。"""

from video_to_guide.models import Step
from video_to_guide.steps.generate import _uncovered_ranges


def _s(i, a, b, placeholder=False):
    return Step(index=i, t_start=a, t_end=b, title=f"s{i}", action="x",
                placeholder=placeholder)


def test_finds_leading_and_trailing_gaps():
    steps = [_s(1, 10, 20), _s(2, 30, 40)]
    gaps = _uncovered_ranges(steps, 60, min_gap=4)
    assert (0, 10) in gaps
    assert (20, 30) in gaps
    assert (40, 60) in gaps


def test_ignores_placeholder_steps():
    steps = [_s(1, 10, 20), _s(2, 20, 50, placeholder=True)]
    gaps = _uncovered_ranges(steps, 60, min_gap=4)
    assert (20, 60) in gaps        # 占位步骤不算覆盖


def test_small_gaps_are_skipped():
    steps = [_s(1, 0, 18), _s(2, 20, 60)]
    gaps = _uncovered_ranges(steps, 60, min_gap=4)
    assert gaps == []              # 只差 2 秒，不值得补


def test_fully_covered_returns_empty():
    assert _uncovered_ranges([_s(1, 0, 60)], 60) == []