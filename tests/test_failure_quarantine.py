# -*- coding: utf-8 -*-
"""失败时不能留下上一次的手册（否则用户会把旧结果当成本次成功）。"""

from video_to_guide.models import RunReport
from video_to_guide.pipeline import _quarantine_stale_docs


def _report(errors=(), warnings=()):
    r = RunReport(run_id="t", video_path="v.mp4")
    r.errors.extend(errors)
    r.warnings.extend(warnings)
    return r


def test_moves_stale_guide_and_writes_note(tmp_path):
    out = tmp_path / "output"
    out.mkdir()
    (out / "guide.md").write_text("旧手册", encoding="utf-8")
    (out / "guide.html").write_text("<html></html>", encoding="utf-8")
    rep = _report(["不是操作教学"])
    _quarantine_stale_docs(tmp_path, rep)
    assert not (out / "guide.md").exists()
    assert not (out / "guide.html").exists()
    assert list(out.glob("guide.stale-*.md"))
    assert list(out.glob("guide.stale-*.html"))
    note = (out / "分析失败.txt").read_text(encoding="utf-8")
    assert "不是操作教学" in note and "请勿使用" in note
    assert any("旧手册已改名" in w for w in rep.warnings)


def test_no_output_dir_is_noop(tmp_path):
    rep = _report()
    _quarantine_stale_docs(tmp_path, rep)     # 不该抛异常
    assert rep.warnings == []


def test_nothing_to_move_still_writes_note(tmp_path):
    out = tmp_path / "output"
    out.mkdir()
    rep = _report(["失败"])
    _quarantine_stale_docs(tmp_path, rep)
    assert (out / "分析失败.txt").exists()
    assert rep.warnings == []