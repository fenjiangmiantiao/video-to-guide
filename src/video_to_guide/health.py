# -*- coding: utf-8 -*-
"""AI 模型服务的"就绪状态"判定。

为什么单独一个模块：医生检查（doctor）、设置界面、点「开始分析」之前都要回答
同一个问题——"现在能不能真的调模型"。以前三处各写一套，判定口径不一致，
出现过"检查说在线、一分析就 0 步"的情况。

这里把结论统一成四种状态，界面按状态给不同的动作，而不是笼统打个红叉：

    ready        可以分析
    no_service   服务没启动/地址不对（用户去启动服务或改地址）
    no_model     服务通了但没有可用模型（用户去加载模型）
    bad_key      鉴权失败（用户去改 Key）

注意：**服务未就绪不等于软件缺依赖**。软件本身（ffmpeg、抽帧、目录）是好的，
只是没有模型可用。所以这个检查失败不应该阻止软件启动，只应该阻止"开始分析"。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass

from .providers import get as get_provider
from .vision.lms_client import ChatClient

READY = "ready"
NO_SERVICE = "no_service"
NO_MODEL = "no_model"
BAD_KEY = "bad_key"

# 状态的中文说明，界面与命令行共用，避免两边措辞不一致
STATE_TEXT = {
    READY: "AI 服务就绪",
    NO_SERVICE: "模型服务未启动",
    NO_MODEL: "服务已连通，但没有已加载的模型",
    BAD_KEY: "鉴权失败（API Key 不正确或没有权限）",
}


@dataclass
class ServiceState:
    kind: str                 # vision / text
    state: str
    detail: str
    models: list[str]
    provider_id: str
    provider_name: str
    base_url: str
    model: str

    @property
    def ok(self) -> bool:
        return self.state == READY

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "state": self.state,
            "ok": self.ok,
            "text": STATE_TEXT.get(self.state, self.state),
            "detail": self.detail,
            "models": self.models,
            "provider": self.provider_id,
            "provider_name": self.provider_name,
            "base_url": self.base_url,
            "model": self.model,
        }


def probe(settings, kind: str = "vision", timeout: float = 10.0) -> ServiceState:
    """探测一个任务角色背后的服务是否真的可用。

    只查 /v1/models，不发推理请求：默认检查不能替用户花钱，也不能因为模型
    推理慢就把界面卡住。真正验证"能否看图"要用户主动点"测试"。
    """
    pid, base_url, api_key, model = settings.role_client(kind)
    provider = get_provider(pid)
    client = ChatClient(base_url, model, timeout=timeout, api_key=api_key)
    models = _list_models(client)
    if models is None:
        ok, detail = client.health()
        state = BAD_KEY if "鉴权" in detail else NO_SERVICE
        return ServiceState(kind, state, detail, [], pid, provider.name, base_url, model)

    if not models:
        return ServiceState(
            kind, NO_MODEL,
            f"连上了 {base_url}，但服务没有加载任何模型"
            "（LM Studio 里加载一个带视觉能力的模型，或 Ollama 先 pull 一个）",
            [], pid, provider.name, base_url, model)

    if model and model not in models:
        # 单模型服务（llama.cpp / llama-server / vLLM 单实例）的 /v1/models
        # 通常只返回一个"模型文件的完整路径"，而用户配置里写的是短名字。
        # 实测 llama-server 下配 "gemma-4-12B" 会被判成"没有可用模型"，
        # 但请求写任意名字它都会用那唯一的模型正常回答。只有一个模型时直接采用。
        if len(models) == 1:
            model = models[0]
        else:
            return ServiceState(
                kind, NO_MODEL,
                f"配置里选的模型 {model} 不在服务已加载的列表里；"
                f"当前可用：{', '.join(models[:6])}",
                models, pid, provider.name, base_url, model)

    return ServiceState(kind, READY,
                        "服务在线，可用模型：" + ", ".join(models[:6]),
                        models, pid, provider.name, base_url, model)


def probe_all(settings) -> dict[str, ServiceState]:
    """视觉与步骤两个角色一起探。同一服务只探一次，避免重复请求。"""
    out: dict[str, ServiceState] = {}
    cache: dict[tuple, list | None] = {}
    for kind in ("vision", "text"):
        pid, base_url, api_key, model = settings.role_client(kind)
        key = (base_url, api_key)
        if key not in cache:
            cache[key] = _list_models(ChatClient(base_url, model, timeout=10, api_key=api_key))
        cache[key]  # 共享结果
        out[kind] = probe(settings, kind)
    return out


def _list_models(client: ChatClient) -> list[str] | None:
    """取服务端的模型列表。返回 None 表示"连不上"，[] 表示"连上了但没模型"。"""
    url = client.base_url + "/models"
    try:
        req = urllib.request.Request(url, headers=client._headers())
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return None        # 交给 health() 判鉴权
        return None
    except Exception:
        return None
    ids = [m.get("id") for m in data.get("data", []) if isinstance(m, dict)]
    return [i for i in ids if i]


def block_reason(states: dict[str, ServiceState]) -> str:
    """分析开始前如果不该继续，返回一句给用户看的原因；可以继续就返回空串。"""
    bad = [s for s in states.values() if not s.ok]
    if not bad:
        return ""
    lines = ["当前还不能开始分析："]
    for s in bad:
        role = "视觉（看截图）" if s.kind == "vision" else "步骤整理"
        lines.append(f"· {role}模型：{STATE_TEXT.get(s.state, s.state)}")
        if s.detail:
            lines.append(f"  {s.detail}")
    lines.append("")
    lines.append("已有手册和历史记录不受影响，可以继续查看、编辑、导出。")
    return "\n".join(lines)
