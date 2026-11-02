"""从服务商那里实际查询「这台机器/这个账号现在能用哪些模型」。

为什么单独一个模块：
1. 每家服务商的模型列表接口不一样（OpenAI 兼容的 /models、Ollama 的 /api/tags、
   Anthropic 和 Gemini 各有原生接口），把它们和界面分开，界面就只负责显示。
2. 只用官方文档里确实存在的接口，不猜、不伪造：拿不到就明确报错，让用户手动填。
3. 纯标准库（urllib），不为了列个模型再引一个新依赖。

注意：这里只查列表，不发推理请求，所以不会产生 token 费用。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urlsplit, urlunsplit

from .providers import get as get_provider


# 每家服务商用哪种方式列模型。没有适配的写 "none"，界面会提示手动添加，
# 而不是给一个点了就报「不支持」的假按钮。
PROTOCOLS: dict[str, str] = {
    # 本机
    "lmstudio": "openai",       # LM Studio 的 OpenAI 兼容层 GET /v1/models
    "ollama": "ollama",         # 原生 GET /api/tags
    # 云端
    "openai": "openai",
    "deepseek": "openai",
    "dashscope": "openai",
    "zhipu": "openai",
    "moonshot": "openai",
    "siliconflow": "openai",
    "openrouter": "openai",
    "xai": "openai",
    "mistral": "openai",
    "custom": "openai",         # 用户自己填的地址，按 OpenAI 兼容处理
    "localcustom": "openai",    # 本地自建服务同样按 OpenAI 兼容处理
    "anthropic": "anthropic",   # 原生 GET /v1/models（需要 x-api-key）
    "gemini": "gemini",         # 原生 GET /v1beta/models?key=
    # 已知没有公开、稳定的模型列表接口（或需要特殊鉴权），本轮不做
    "ark": "none",
    "qianfan": "none",
    "hunyuan": "none",
}


def protocol_for(provider_id: str) -> str:
    return PROTOCOLS.get(provider_id, "none")


@dataclass
class ModelEntry:
    """一个模型。id 是真正要填进请求体的那个字符串。"""

    id: str
    owned_by: str = ""
    source: str = "remote"        # remote = 服务商返回；manual = 用户手填
    vision: str = "unknown"       # yes / unknown（没有可靠依据时绝不写 yes）
    note: str = ""

    def to_dict(self) -> dict:
        return {"id": self.id, "owned_by": self.owned_by,
                "source": self.source, "vision": self.vision, "note": self.note}

    @classmethod
    def from_dict(cls, d: dict) -> "ModelEntry":
        d = d or {}
        return cls(id=str(d.get("id") or ""),
                   owned_by=str(d.get("owned_by") or ""),
                   source=str(d.get("source") or "remote"),
                   vision=str(d.get("vision") or "unknown"),
                   note=str(d.get("note") or ""))


@dataclass
class ModelListResult:
    """一次「获取模型」的结果。ok=False 时 error 一定是给人看的中文原因。"""

    ok: bool
    provider: str
    endpoint: str = ""
    models: list[ModelEntry] = field(default_factory=list)
    error: str = ""
    note: str = ""
    elapsed: float = 0.0
    fetched_at: str = ""

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "provider": self.provider,
            "endpoint": self.endpoint,
            "models": [m.to_dict() for m in self.models],
            "error": self.error,
            "note": self.note,
            "elapsed": round(self.elapsed, 2),
            "fetched_at": self.fetched_at,
        }


def _origin(url: str) -> str:
    parts = urlsplit(url.strip())
    if not parts.scheme or not parts.netloc:
        raise ValueError(f"接口地址格式不对（需要 http:// 或 https:// 开头）：{url}")
    return urlunsplit((parts.scheme, parts.netloc, "", "", ""))


def _request_spec(proto: str, base_url: str, api_key: str) -> tuple[str, dict]:
    base = (base_url or "").strip()
    if not base:
        raise ValueError("接口地址为空：请先在「服务商」页填写接口地址。")
    if proto == "openai":
        url = base.rstrip("/") + "/models"
        headers = {"Accept": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        return url, headers
    if proto == "ollama":
        return _origin(base) + "/api/tags", {"Accept": "application/json"}
    if proto == "anthropic":
        headers = {"Accept": "application/json", "anthropic-version": "2023-06-01"}
        if api_key:
            headers["x-api-key"] = api_key
        return base.rstrip("/") + "/models?limit=1000", headers
    if proto == "gemini":
        if not api_key:
            raise ValueError("Gemini 的原生模型列表接口需要 API Key，请先填写。")
        url = (_origin(base) + "/v1beta/models?pageSize=1000&key="
               + urllib.parse.quote(api_key))
        return url, {"Accept": "application/json"}
    raise ValueError("这个服务商没有适配模型列表接口。")


def _http_error_text(err: urllib.error.HTTPError) -> str:
    code = int(getattr(err, "code", 0) or 0)
    if code in (401, 403):
        return f"鉴权失败（HTTP {code}）：API Key 不正确、已过期或没有该接口权限。"
    if code == 404:
        return ("接口不存在（HTTP 404）：这个服务商的模型列表地址可能不同，"
                "请手动填写模型 ID。")
    if code == 429:
        return "请求太频繁（HTTP 429）：稍等一会儿再点「刷新」。"
    if code >= 500:
        return f"服务端出错（HTTP {code}）：不是你的配置问题，稍后再试。"
    body = ""
    try:
        body = err.read().decode("utf-8", errors="replace").strip()[:200]
    except Exception:
        body = ""
    return f"请求失败（HTTP {code}）" + (f"：{body}" if body else "")


def _vision_from_item(it: dict) -> bool:
    """服务商自己声明的"能不能看图"。只有可靠元数据才返回 True。

    llama.cpp / LM Studio 会在列表里给出 capabilities（含 multimodal），
    也会给 architecture.input_modalities（含 image）。这两个都是服务端声明，
    可以当依据；名字里带 vl/vision 不算依据。
    """
    caps = it.get("capabilities")
    if isinstance(caps, list):
        low = {str(c).strip().lower() for c in caps}
        if low & {"multimodal", "vision", "image"}:
            return True
    arch = it.get("architecture")
    if isinstance(arch, dict):
        modalities = arch.get("input_modalities")
        if isinstance(modalities, list):
            low = {str(m).strip().lower() for m in modalities}
            if low & {"image", "vision"}:
                return True
    return False


def _parse_models(proto: str, data) -> list[ModelEntry]:
    """把各家五花八门的响应统一成 ModelEntry 列表。"""
    out: list[ModelEntry] = []
    if not isinstance(data, dict):
        return out
    if proto in ("openai", "anthropic"):
        # OpenAI 官方是 {"data": [...]}；llama.cpp / 部分网关用 {"models": [...]}
        items = data.get("data")
        if not isinstance(items, list):
            alt = data.get("models")
            items = alt if isinstance(alt, list) else []
        for it in items:
            if not isinstance(it, dict):
                continue
            mid = str(it.get("id") or it.get("model") or it.get("name") or "").strip()
            if not mid:
                continue
            entry = ModelEntry(id=mid,
                               owned_by=str(it.get("owned_by") or it.get("type") or ""))
            if _vision_from_item(it):
                # 服务商自己声明支持图像输入，这是可靠元数据，可以标"支持"
                entry.vision = "yes"
                entry.note = "服务商元数据标明支持图像输入"
            out.append(entry)
        return out
    if proto == "ollama":
        for it in data.get("models") or []:
            if not isinstance(it, dict):
                continue
            mid = str(it.get("name") or it.get("model") or "").strip()
            if mid:
                out.append(ModelEntry(id=mid, owned_by="ollama"))
        return out
    if proto == "gemini":
        for it in data.get("models") or []:
            if not isinstance(it, dict):
                continue
            name = str(it.get("name") or "").strip()
            if name.startswith("models/"):
                name = name[len("models/"):]
            if not name:
                continue
            methods = it.get("supportedGenerationMethods") or []
            if methods and "generateContent" not in methods:
                continue          # 只能 embed 的模型对本项目没用，不列
            out.append(ModelEntry(id=name, owned_by="google"))
        return out
    return out


def mark_vision(provider_id: str, models: list[ModelEntry]) -> None:
    """只给「服务商预设里明确列出」的模型打视觉标记。

    名字里带 vl/vision 只能算线索，不能当证据——所以其它一律保持 unknown，
    由界面显示「未验证」，用户要用就连一下测试。
    """
    try:
        preset = set(get_provider(provider_id).vision_models)
    except Exception:
        preset = set()
    for m in models:
        if m.vision == "yes":
            continue                      # 元数据已经确认过，别覆盖更可靠的来源
        if m.id in preset:
            m.vision = "yes"
            m.note = "服务商预设标注为视觉模型"


def fetch_models(provider_id: str, base_url: str, api_key: str = "",
                 timeout: float = 15.0) -> ModelListResult:
    """真实请求一次模型列表。任何失败都返回结构化结果，不抛异常给界面。"""
    started = time.time()
    proto = protocol_for(provider_id)
    if proto == "none":
        try:
            name = get_provider(provider_id).name
        except Exception:
            name = provider_id
        return ModelListResult(
            ok=False, provider=provider_id, elapsed=time.time() - started,
            error=f"{name} 暂时没有适配模型列表接口，请手动添加模型 ID。")
    try:
        url, headers = _request_spec(proto, base_url, api_key)
    except ValueError as e:
        return ModelListResult(ok=False, provider=provider_id,
                               error=str(e), elapsed=time.time() - started)

    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return ModelListResult(ok=False, provider=provider_id, endpoint=url,
                               error=_http_error_text(e),
                               elapsed=time.time() - started)
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", e)
        return ModelListResult(
            ok=False, provider=provider_id, endpoint=url,
            error=f"连不上服务：{reason}（服务没启动、地址填错或被防火墙拦了）",
            elapsed=time.time() - started)
    except TimeoutError:
        return ModelListResult(ok=False, provider=provider_id, endpoint=url,
                               error=f"请求超时（超过 {timeout:.0f} 秒没响应）。",
                               elapsed=time.time() - started)
    except Exception as e:
        return ModelListResult(ok=False, provider=provider_id, endpoint=url,
                               error=f"请求失败：{type(e).__name__}: {e}",
                               elapsed=time.time() - started)

    try:
        data = json.loads(raw)
    except Exception:
        head = raw.strip()[:80].replace("\n", " ")
        return ModelListResult(
            ok=False, provider=provider_id, endpoint=url,
            error="返回的不是 JSON（多半是接口地址填成了网页地址）。"
                  f"返回内容开头：{head}",
            elapsed=time.time() - started)

    models = _parse_models(proto, data)
    mark_vision(provider_id, models)
    result = ModelListResult(
        ok=True, provider=provider_id, endpoint=url, models=models,
        elapsed=time.time() - started,
        fetched_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    if not models:
        result.note = "服务连上了，但没有返回任何模型（本机可能还没加载模型）。"
    return result