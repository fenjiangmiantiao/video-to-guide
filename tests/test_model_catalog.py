"""模型列表查询的单元测试：用本地假服务模拟各家接口，不需要联网、不花钱。"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from video_to_guide.model_catalog import (ModelEntry, fetch_models, mark_vision,
                                          protocol_for)


class _Handler(BaseHTTPRequestHandler):
    responses = {}
    last_headers = {}

    def do_GET(self):
        _Handler.last_headers = {k.lower(): v for k, v in self.headers.items()}
        path = self.path.split("?")[0]
        code, body = _Handler.responses.get(path, (404, '{"error":"not found"}'))
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


@pytest.fixture
def fake_server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def test_protocol_map_covers_every_provider():
    assert protocol_for("lmstudio") == "openai"
    assert protocol_for("ollama") == "ollama"
    assert protocol_for("anthropic") == "anthropic"
    assert protocol_for("gemini") == "gemini"
    assert protocol_for("ark") == "none"
    assert protocol_for("no-such-provider") == "none"


def test_openai_style_list(fake_server):
    _Handler.responses = {"/v1/models": (200, json.dumps(
        {"data": [{"id": "gpt-4o", "owned_by": "openai"},
                  {"id": "gpt-4o-mini", "owned_by": "openai"}]}))}
    r = fetch_models("openai", fake_server + "/v1", "sk-test")
    assert r.ok
    assert [m.id for m in r.models] == ["gpt-4o", "gpt-4o-mini"]
    assert _Handler.last_headers.get("authorization") == "Bearer sk-test"
    assert r.fetched_at and r.elapsed >= 0


def test_openai_without_key_sends_no_auth_header(fake_server):
    _Handler.responses = {"/v1/models": (200, '{"data":[{"id":"local"}]}')}
    r = fetch_models("lmstudio", fake_server + "/v1")
    assert r.ok and r.models[0].id == "local"
    assert "authorization" not in _Handler.last_headers


def test_ollama_uses_api_tags(fake_server):
    _Handler.responses = {"/api/tags": (200, json.dumps(
        {"models": [{"name": "qwen2.5vl:7b"}, {"name": "llama3.1:8b"}]}))}
    r = fetch_models("ollama", fake_server + "/v1")
    assert r.ok
    assert [m.id for m in r.models] == ["qwen2.5vl:7b", "llama3.1:8b"]
    assert r.endpoint.endswith("/api/tags")


def test_anthropic_uses_native_endpoint_and_header(fake_server):
    _Handler.responses = {"/v1/models": (200, json.dumps(
        {"data": [{"id": "claude-sonnet-4-5", "type": "model"}]}))}
    r = fetch_models("anthropic", fake_server + "/v1", "sk-ant-1")
    assert r.ok and r.models[0].id == "claude-sonnet-4-5"
    assert _Handler.last_headers.get("x-api-key") == "sk-ant-1"
    assert _Handler.last_headers.get("anthropic-version") == "2023-06-01"


def test_gemini_filters_non_generate_models(fake_server):
    _Handler.responses = {"/v1beta/models": (200, json.dumps({"models": [
        {"name": "models/gemini-2.5-flash", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/text-embedding-004", "supportedGenerationMethods": ["embedContent"]}]}))}
    r = fetch_models("gemini", fake_server + "/v1beta/openai/", "gk-1")
    assert r.ok
    assert [m.id for m in r.models] == ["gemini-2.5-flash"]


def test_gemini_without_key_reports_clearly(fake_server):
    r = fetch_models("gemini", fake_server + "/v1beta/openai/", "")
    assert not r.ok and "API Key" in r.error


def test_unsupported_provider_says_manual(fake_server):
    r = fetch_models("ark", fake_server + "/api/v3")
    assert not r.ok and "手动添加" in r.error


def test_401_is_reported_as_auth_failure(fake_server):
    _Handler.responses = {"/v1/models": (401, '{"error":"bad key"}')}
    r = fetch_models("openai", fake_server + "/v1", "sk-wrong")
    assert not r.ok
    assert "鉴权失败" in r.error and "401" in r.error
    assert "sk-wrong" not in r.error          # 错误信息里不能回显 Key


def test_404_is_reported_as_missing_endpoint(fake_server):
    r = fetch_models("openai", fake_server + "/v9", "sk-1")
    assert not r.ok and "接口不存在" in r.error


def test_non_json_response(fake_server):
    _Handler.responses = {"/v1/models": (200, "<html>hello</html>")}
    r = fetch_models("openai", fake_server + "/v1", "sk-1")
    assert not r.ok and "不是 JSON" in r.error


def test_empty_list_is_ok_with_note(fake_server):
    _Handler.responses = {"/v1/models": (200, '{"data": []}')}
    r = fetch_models("lmstudio", fake_server + "/v1")
    assert r.ok and r.models == [] and "没有返回任何模型" in r.note


def test_connection_refused_is_explained():
    r = fetch_models("lmstudio", "http://127.0.0.1:9/v1", timeout=3)
    assert not r.ok and "连不上服务" in r.error


def test_bad_base_url():
    r = fetch_models("ollama", "不是地址")
    assert not r.ok and "格式不对" in r.error


def test_mark_vision_only_trusts_preset():
    models = [ModelEntry(id="gpt-4o"), ModelEntry(id="qwen2.5-vl-7b"),
              ModelEntry(id="some-unknown")]
    mark_vision("openai", models)
    assert models[0].vision == "yes"          # 服务商预设里确实有 gpt-4o
    assert models[1].vision == "unknown"      # 名字像视觉模型，但没有依据就不标
    assert models[2].vision == "unknown"


def test_mark_vision_for_provider_without_vision_models():
    models = [ModelEntry(id="deepseek-chat")]
    mark_vision("deepseek", models)
    assert models[0].vision == "unknown"


def test_model_entry_roundtrip():
    m = ModelEntry(id="x", owned_by="y", source="manual", vision="yes", note="n")
    assert ModelEntry.from_dict(m.to_dict()).to_dict() == m.to_dict()

def test_llamacpp_style_models_key_and_capabilities(fake_server):
    """llama.cpp / LM Studio 用 {"models": [...]}，并用 capabilities 声明多模态。"""
    _Handler.responses = {"/v1/models": (200, json.dumps({"models": [
        {"name": "gemma", "model": "gemma", "capabilities": ["completion", "multimodal"]},
        {"name": "qwen-text", "model": "qwen-text", "capabilities": ["completion"]},
    ]}))}
    result = fetch_models("lmstudio", fake_server + "/v1")
    assert result.ok is True
    ids = [m.id for m in result.models]
    assert ids == ["gemma", "qwen-text"]
    assert result.models[0].vision == "yes"          # 元数据标明支持多模态
    assert result.models[1].vision == "unknown"      # 没有依据就保持未验证

def test_llamacpp_data_with_architecture_modalities(fake_server):
    """真实 llama.cpp 会同时给 models 和 data；data 里用 architecture 声明输入模态。"""
    _Handler.responses = {"/v1/models": (200, json.dumps({
        "object": "list",
        "models": [{"model": "vision-llm", "capabilities": ["completion", "multimodal"]}],
        "data": [
            {"id": "vision-llm", "object": "model",
             "architecture": {"input_modalities": ["text", "image"]}},
            {"id": "text-only", "object": "model",
             "architecture": {"input_modalities": ["text"]}},
        ],
    }))}
    result = fetch_models("lmstudio", fake_server + "/v1")
    assert result.ok is True
    by_id = {m.id: m for m in result.models}
    assert by_id["vision-llm"].vision == "yes"
    assert by_id["text-only"].vision == "unknown"
