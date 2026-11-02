# -*- coding: utf-8 -*-
"""抽帧后清理未被选中的候选图。"""

from video_to_guide.models import Frame
from video_to_guide.parse.frames import _prune_frame_files


def test_prune_removes_unselected(tmp_path):
    d = tmp_path / "output" / "frames"
    d.mkdir(parents=True)
    for n in ("a.jpg", "b.jpg", "c.jpg"):
        (d / n).write_bytes(b"x")
    kept = [Frame(id="f0", time=0, end=1, path="frames/a.jpg"),
            Frame(id="f1", time=1, end=2, path="frames/b.jpg")]
    _prune_frame_files(tmp_path, kept)
    assert sorted(p.name for p in d.glob("*.jpg")) == ["a.jpg", "b.jpg"]


def test_prune_noop_when_dir_missing(tmp_path):
    _prune_frame_files(tmp_path, [])      # 不该抛异常

def _frames(n):
    return [Frame(id=f"f{i}", time=float(i), end=float(i + 1), path=f"frames/{i}.jpg")
            for i in range(n)]


def test_pad_floor_adds_frames_uniformly():
    from video_to_guide.parse.frames import _pad_to_floor
    allf = _frames(100)
    picked = [allf[0], allf[50], allf[99]]
    out = _pad_to_floor(picked, allf, 10)
    assert len(out) == 10
    assert out[0].time == 0.0 and out[-1].time == 99.0
    assert [f.time for f in out] == sorted(f.time for f in out)


def test_pad_floor_noop_when_enough():
    from video_to_guide.parse.frames import _pad_to_floor
    allf = _frames(20)
    picked = allf[:10]
    assert len(_pad_to_floor(picked, allf, 10)) == 10