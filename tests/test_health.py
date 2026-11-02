# -*- coding: utf-8 -*-
"""健康检查：模型名匹配的边界情况。"""

from video_to_guide import health
from video_to_guide.config import Settings


def _settings(monkeypatch, model):
    s = Settings.load()
    monkeypatch.setattr(s, "role_client",
                        lambda kind: ("localcustom", "http://127.0.0.1:8080/v1", "", model))
    return s


def test_single_model_service_adopts_the_only_model(monkeypatch):
    """llama-server 只报一个完整路径，用户配的是短名字——应视为就绪。"""
    monkeypatch.setattr(health, "_list_models", lambda c: ["E:/models/gemma.gguf"])
    st = health.probe(_settings(monkeypatch, "gemma-4-12B"), "vision")
    assert st.state == health.READY
    assert st.model == "E:/models/gemma.gguf"


def test_multi_model_service_with_wrong_name_is_not_ready(monkeypatch):
    monkeypatch.setattr(health, "_list_models", lambda c: ["a-model", "b-model"])
    st = health.probe(_settings(monkeypatch, "不存在的模型"), "vision")
    assert st.state == health.NO_MODEL


def test_matching_name_is_ready(monkeypatch):
    monkeypatch.setattr(health, "_list_models", lambda c: ["gemma-4-12B", "other"])
    st = health.probe(_settings(monkeypatch, "gemma-4-12B"), "vision")
    assert st.state == health.READY