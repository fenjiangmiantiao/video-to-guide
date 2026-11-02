"""配置读写测试：新旧 config.json 都要能正常加载，模型列表与任务角色要能往返。"""

import json

from video_to_guide.config import Settings


def test_roundtrip_with_model_lists(tmp_path):
    path = tmp_path / "config.json"
    s = Settings.load(path)
    s.set_connection("openai", "https://api.openai.com/v1", "sk-test")
    s.set_role("vision", "openai", "gpt-4o")
    s.set_role("text", "ollama", "qwen2.5:7b")
    s.set_connection("ollama", "http://127.0.0.1:11434/v1", "")
    s.hidden_models = {"openai": ["gpt-4o-mini"], "ollama": ["llava:latest"]}
    s.manual_models = {"openai": ["my-model"]}
    s.model_cache = {"openai": {"fetched_at": "2026-10-31 20:00:00",
                                "base_url": "https://api.openai.com/v1",
                                "models": [{"id": "gpt-4o", "vision": "yes"}]}}
    s.save(path)

    back = Settings.load(path)
    assert back.role("vision") == {"provider": "openai", "model": "gpt-4o"}
    assert back.role("text") == {"provider": "ollama", "model": "qwen2.5:7b"}
    assert back.connection("openai")["api_key"] == "sk-test"
    assert back.hidden_models == {"openai": ["gpt-4o-mini"], "ollama": ["llava:latest"]}
    assert back.manual_models == {"openai": ["my-model"]}
    assert back.model_cache["openai"]["models"][0]["id"] == "gpt-4o"


def test_roles_can_use_different_providers(tmp_path):
    """视觉和步骤可以来自不同服务商，互不覆盖。"""
    path = tmp_path / "config.json"
    s = Settings.load(path)
    s.set_connection("openai", "https://api.openai.com/v1", "sk-a")
    s.set_connection("deepseek", "https://api.deepseek.com/v1", "sk-b")
    s.set_role("vision", "openai", "gpt-4o")
    s.set_role("text", "deepseek", "deepseek-chat")
    s.save(path)

    back = Settings.load(path)
    pid_v, base_v, key_v, model_v = back.role_client("vision")
    pid_t, base_t, key_t, model_t = back.role_client("text")
    assert (pid_v, key_v, model_v) == ("openai", "sk-a", "gpt-4o")
    assert (pid_t, key_t, model_t) == ("deepseek", "sk-b", "deepseek-chat")
    assert base_v == "https://api.openai.com/v1"
    assert base_t == "https://api.deepseek.com/v1"


def test_old_config_is_migrated(tmp_path):
    """旧版只有一套 llm_* 配置：迁移后两个角色都指向它，Key 不丢。"""
    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "llm_provider": "openai",
        "llm_base_url": "https://api.openai.com/v1",
        "llm_api_key": "sk-legacy",
        "llm_model": "gpt-4o-mini",
        "vision_model": "gpt-4o",
        "read_long_edge": 900,
    }), encoding="utf-8")

    s = Settings.load(path)
    assert s.read_long_edge == 900
    assert s.role("vision") == {"provider": "openai", "model": "gpt-4o"}
    assert s.role("text") == {"provider": "openai", "model": "gpt-4o-mini"}
    assert s.connection("openai") == {"base_url": "https://api.openai.com/v1",
                                      "api_key": "sk-legacy"}


def test_new_config_is_not_overwritten_by_legacy_fields(tmp_path):
    """已经有 connections/roles 的新配置，不能被旧字段改回去。"""
    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "llm_provider": "lmstudio",
        "connections": {"openai": {"base_url": "https://x/v1", "api_key": "sk-x"}},
        "vision_role": {"provider": "openai", "model": "gpt-4o"},
        "text_role": {"provider": "openai", "model": "gpt-4o-mini"},
    }), encoding="utf-8")

    s = Settings.load(path)
    assert s.role("vision")["provider"] == "openai"
    assert s.connection("openai")["api_key"] == "sk-x"
    assert "lmstudio" not in s.connections


def test_connection_falls_back_to_preset(tmp_path):
    """没填过地址时用服务商预设，不会变成空地址。"""
    s = Settings.load(tmp_path / "config.json")
    conn = s.connection("openai")
    assert conn["base_url"] == "https://api.openai.com/v1"
    assert conn["api_key"] == ""


def test_unknown_keys_are_ignored(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"llm_provider": "ollama", "nonsense": 1}),
                    encoding="utf-8")
    s = Settings.load(path)
    assert s.role("vision")["provider"] == "ollama"
    assert not hasattr(s, "nonsense")


def test_broken_config_falls_back_to_defaults(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{ not json", encoding="utf-8")
    s = Settings.load(path)
    assert s.role("vision")["provider"] == "lmstudio"


def test_env_override(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"read_long_edge": 640}), encoding="utf-8")
    monkeypatch.setenv("VTG_READ_LONG_EDGE", "1024")
    monkeypatch.setenv("VTG_MAX_FRAMES_AUTO", "false")
    s = Settings.load(path)
    assert s.read_long_edge == 1024
    assert s.max_frames_auto is False


def test_frame_budget_formula():
    from video_to_guide.parse.frames import frame_budget

    s = Settings()
    s.max_frames_auto = True
    s.max_frames_floor = 60
    s.max_frames_cap = 240
    assert frame_budget(s, 30) == 60          # 短片走下限
    assert frame_budget(s, 300) == 130        # 300/3 + 30
    assert frame_budget(s, 3600) == 240       # 长视频走上限
    s.max_frames_auto = False
    s.max_frames = 42
    assert frame_budget(s, 3600) == 42
