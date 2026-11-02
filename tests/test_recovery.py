# -*- coding: utf-8 -*-
"""任务恢复与缓存可靠性测试（R7-03）。

重点不是"能不能跑通"，而是"缓存能不能被错误复用"：
换模型、换阅读尺寸、换帧、上次失败，都必须重算；只有同一张图 + 同一模型 +
上次成功，才可以跳过模型调用。
"""
import json
from pathlib import Path

import pytest

from video_to_guide.config import Settings
from video_to_guide.models import Frame, VisionNote
from video_to_guide.vision import describe

from conftest import work


class FakeClient:
    """替身客户端：记录调用次数，可按序号模拟失败。"""

    calls: list[str] = []
    fail_at: set[int] = set()

    def __init__(self, *a, **kw):
        pass

    def describe_image(self, image_path: Path, prompt: str, max_tokens=None, retries=1):
        idx = len(FakeClient.calls)
        FakeClient.calls.append(Path(image_path).name)
        if idx in FakeClient.fail_at:
            raise describe.LLMError("模拟：模型服务不可用")
        class R:
            text = "界面：设置对话框\n可见文字：「主题：」\n操作迹象：下拉框展开"
            elapsed = 0.1
            usage = {}
        return R()


@pytest.fixture(autouse=True)
def patch_client(monkeypatch):
    FakeClient.calls = []
    FakeClient.fail_at = set()
    monkeypatch.setattr(describe, "ChatClient", FakeClient)


def make_frames(tmp_path: Path, n: int = 3) -> list[Frame]:
    frames = []
    for i in range(n):
        img = tmp_path / f"scene_{i}.jpg"
        img.write_bytes(b"\xff\xd8\xff\xd9")
        frames.append(Frame(id=f"f{i}", time=float(i * 5), end=float(i * 5 + 5),
                            path=f"scene_{i}.jpg", width=800, height=600,
                            screen_index=0, change_score=0.5, transcript_ids=[]))
    return frames


def make_settings(tmp_path: Path, model="test/vision-a", edge=768) -> Settings:
    s = Settings.load()
    # 不能依赖本机 config.json 的当前模式：测试跑在哪个模式，
    # 决定了 prompt_for() 返回哪版提示词，也会影响下面改提示词能否让缓存失效。
    s.video_mode = "software"
    s.set_role("vision", "localcustom", model)
    s.read_long_edge = edge
    s.vision_batch_limit = 0
    s.enable_vision = True
    s.reuse_cache = True
    return s


def run(tmp_path, frames, settings, force=False, stats=None):
    return describe.describe_frames(
        frames, tmp_path, settings,
        image_of=lambda f: tmp_path / f.path, force=force, stats=stats)


def test_first_run_calls_model_for_every_frame(tmp_path):
    frames = make_frames(tmp_path, 3)
    s = make_settings(tmp_path)
    notes = run(tmp_path, frames, s)
    assert len(notes) == 3
    assert len(FakeClient.calls) == 3
    data = json.loads(work(tmp_path, "vision.json").read_text(encoding="utf-8"))
    assert data["meta"]["model"] == "test/vision-a"
    assert data["meta"]["read_long_edge"] == 768


def test_second_run_reuses_all(tmp_path):
    frames = make_frames(tmp_path, 3)
    s = make_settings(tmp_path)
    run(tmp_path, frames, s)
    FakeClient.calls = []
    stats: dict = {}
    notes = run(tmp_path, frames, s, stats=stats)
    assert len(notes) == 3
    assert FakeClient.calls == []            # 一张都没有重新请求
    assert stats["reused"] == 3 and stats["described"] == 0


def test_model_change_invalidates_cache(tmp_path):
    frames = make_frames(tmp_path, 2)
    run(tmp_path, frames, make_settings(tmp_path, model="test/vision-a"))
    FakeClient.calls = []
    run(tmp_path, frames, make_settings(tmp_path, model="test/vision-b"))
    assert len(FakeClient.calls) == 2        # 换模型必须重算


def test_read_size_change_invalidates_cache(tmp_path):
    frames = make_frames(tmp_path, 2)
    run(tmp_path, frames, make_settings(tmp_path, edge=768))
    FakeClient.calls = []
    run(tmp_path, frames, make_settings(tmp_path, edge=1280))
    assert len(FakeClient.calls) == 2


def test_new_frame_set_invalidates_only_new(tmp_path):
    frames = make_frames(tmp_path, 2)
    s = make_settings(tmp_path)
    run(tmp_path, frames, s)
    # 视频重抽后多了一张新帧
    frames2 = frames + [Frame(id="f9", time=30.0, end=35.0, path="scene_9.jpg",
                              width=800, height=600, screen_index=0,
                              change_score=0.5, transcript_ids=[])]
    (tmp_path / "scene_9.jpg").write_bytes(b"\xff\xd8\xff\xd9")
    FakeClient.calls = []
    notes = run(tmp_path, frames2, s)
    assert len(notes) == 3
    assert FakeClient.calls == ["scene_9.jpg"]   # 只补新的一张


def test_failed_note_is_retried(tmp_path):
    frames = make_frames(tmp_path, 3)
    s = make_settings(tmp_path)
    FakeClient.fail_at = {1}                 # 第 2 张失败
    notes = run(tmp_path, frames, s)
    assert notes[1].ok is False
    FakeClient.calls = []
    FakeClient.fail_at = set()
    notes2 = run(tmp_path, frames, s)
    assert FakeClient.calls == ["scene_1.jpg"]   # 只重试失败的那张
    assert notes2[1].ok is True


def test_interrupt_then_resume(tmp_path):
    """模拟"处理到一半模型挂了"：已完成的描述必须保住，恢复后只补剩下的。"""
    frames = make_frames(tmp_path, 4)
    s = make_settings(tmp_path)
    FakeClient.fail_at = {2, 3}              # 后两张失败
    run(tmp_path, frames, s)
    data = json.loads(work(tmp_path, "vision.json").read_text(encoding="utf-8"))
    assert len(data["notes"]) == 4
    ok = [n for n in data["notes"] if not n.get("error")]
    assert len(ok) == 2

    FakeClient.calls = []
    FakeClient.fail_at = set()
    stats: dict = {}
    notes = run(tmp_path, frames, s, stats=stats)
    assert FakeClient.calls == ["scene_2.jpg", "scene_3.jpg"]
    assert stats["reused"] == 2
    assert all(n.ok for n in notes)


def test_legacy_cache_without_meta_is_not_reused(tmp_path):
    """早期版本的 vision.json 没有 meta：无法确认当时用的模型和提示词，必须重算。

    旧文件不删，重新算完会覆盖成带 meta 的新格式。
    """
    frames = make_frames(tmp_path, 2)
    legacy = {"notes": [
        VisionNote(id="v0", frame_id="f0", interface="旧", visible_text="旧", activity="旧").to_dict(),
        VisionNote(id="v1", frame_id="f1", interface="旧", visible_text="旧", activity="旧").to_dict(),
    ]}
    work(tmp_path, "vision.json").parent.mkdir(exist_ok=True)
    work(tmp_path, "vision.json").write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")
    notes = run(tmp_path, frames, make_settings(tmp_path))
    assert len(FakeClient.calls) == 2
    assert len(notes) == 2
    data = json.loads(work(tmp_path, "vision.json").read_text(encoding="utf-8"))
    assert data["meta"]["prompt_hash"]


def test_same_frame_id_but_changed_image_is_recomputed(tmp_path):
    """frame_id 还是 f0，但那张图已经被换成另一张 —— 旧描述不能拿来用。"""
    frames = make_frames(tmp_path, 2)
    s = make_settings(tmp_path)
    run(tmp_path, frames, s)
    # 模拟重新抽帧：文件名和 id 没变，图片内容变了
    (tmp_path / "scene_0.jpg").write_bytes(b"CHANGED-IMAGE-DATA")
    FakeClient.calls = []
    stats: dict = {}
    run(tmp_path, frames, s, stats=stats)
    assert FakeClient.calls == ["scene_0.jpg"]
    assert stats["reused"] == 1


def test_prompt_change_invalidates_cache(tmp_path, monkeypatch):
    frames = make_frames(tmp_path, 2)
    s = make_settings(tmp_path)
    run(tmp_path, frames, s)
    monkeypatch.setattr(describe, "PROMPT_SOFTWARE",
                        describe.PROMPT_SOFTWARE + " 另外多问一句。")
    FakeClient.calls = []
    run(tmp_path, frames, s)
    assert len(FakeClient.calls) == 2


def test_max_tokens_change_invalidates_cache(tmp_path):
    frames = make_frames(tmp_path, 2)
    s = make_settings(tmp_path)
    run(tmp_path, frames, s)
    s.vision_max_tokens = s.vision_max_tokens + 512
    FakeClient.calls = []
    run(tmp_path, frames, s)
    assert len(FakeClient.calls) == 2


def test_corrupt_cache_falls_back_to_recompute(tmp_path):
    frames = make_frames(tmp_path, 2)
    work(tmp_path, "vision.json").parent.mkdir(exist_ok=True)
    work(tmp_path, "vision.json").write_text("{ 这不是 JSON", encoding="utf-8")
    notes = run(tmp_path, frames, make_settings(tmp_path))
    assert len(notes) == 2
    assert len(FakeClient.calls) == 2


def test_force_ignores_cache(tmp_path):
    frames = make_frames(tmp_path, 2)
    s = make_settings(tmp_path)
    run(tmp_path, frames, s)
    FakeClient.calls = []
    run(tmp_path, frames, s, force=True)
    assert len(FakeClient.calls) == 2
