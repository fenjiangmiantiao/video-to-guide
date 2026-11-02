"""本地模型客户端（OpenAI 兼容 /v1/chat/completions）。

兼容 llama.cpp 的 llama-server、LM Studio 的本地服务、Ollama 的 OpenAI 接口。
只用标准库 urllib，不引入额外 HTTP 依赖。

错误分类很重要：界面需要区分"服务没启动"和"模型太慢"和"模型返回了垃圾"，
因为用户的处理方式完全不同。
"""

from __future__ import annotations

import base64
import json
import mimetypes
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path


class LLMError(RuntimeError):
    """模型服务调用失败。message 会直接显示给用户，所以要写成人话。"""


@dataclass
class LLMResult:
    text: str
    elapsed: float
    usage: dict


class ChatClient:
    def __init__(self, base_url: str, model: str, timeout: float = 300.0,
                 max_tokens: int = 600, temperature: float = 0.1,
                 disable_thinking: bool = True, api_key: str = "",
                 reasoning_effort: str = ""):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.max_tokens = max_tokens
        self.temperature = temperature
        # 云端服务靠 Bearer Token 鉴权；本机服务留空即可。
        self.api_key = (api_key or "").strip()
        # 有些本地模型（如 gemma-4、Qwen3）默认会先输出一大段"思考过程"，
        # 把 max_tokens 全吃光，正式答案反而是空的。关掉思考后同一张图
        # 从 37 秒降到 5 秒，质量不变。不支持的实现会忽略这个参数。
        self.disable_thinking = disable_thinking
        # 推理型模型（如 deepseek-v4.1-flash）默认会先写一大段思考，把
        # max_tokens 吃光，正式 JSON 反而被截断。发 reasoning_effort="none"
        # 可以跳过思考，实测同一请求从 13.4 秒降到 4.2 秒。
        self.reasoning_effort = (reasoning_effort or "").strip()

    # ---------------------------------------------------------------- 鉴权头
    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    # ---------------------------------------------------------------- 健康检查
    def health(self) -> tuple[bool, str]:
        """探测服务是否在线、密钥是否可用。返回 (是否可用, 说明)。"""
        url = self.base_url + "/models"
        try:
            req = urllib.request.Request(url, headers=self._headers())
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
            models = [m.get("id") for m in data.get("data", []) if isinstance(m, dict)]
            return True, "服务在线，可用模型：" + (", ".join(m for m in models if m) or "(未列出)")
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                return False, f"鉴权失败（HTTP {e.code}）：API Key 不正确或没有权限"
            return False, f"服务返回 HTTP {e.code}：{e.reason}"
        except urllib.error.URLError as e:
            return False, (f"连不上 {self.base_url}：{getattr(e, 'reason', e)}\n"
                           "本机服务请先启动（llama-server / LM Studio / Ollama）；"
                           "云端请检查网络和地址")
        except Exception as e:
            return False, f"模型服务探测失败：{type(e).__name__}: {e}"

    # ---------------------------------------------------------------- 文本对话
    def chat(self, messages: list[dict], max_tokens: int | None = None,
             retries: int = 1) -> LLMResult:
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": max_tokens or self.max_tokens,
        }
        if self.disable_thinking:
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        if self.reasoning_effort:
            payload["reasoning_effort"] = self.reasoning_effort
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.base_url + "/chat/completions", data=body, headers=self._headers(),
        )

        last_err: Exception | None = None
        for attempt in range(retries + 1):
            t0 = time.time()
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    data = json.loads(r.read().decode("utf-8", "replace"))
                elapsed = time.time() - t0
                text = _extract_text(data)
                if not text.strip():
                    raise LLMError("模型返回了空内容")
                return LLMResult(text=text, elapsed=elapsed, usage=data.get("usage", {}))
            except urllib.error.HTTPError as e:
                detail = ""
                try:
                    detail = e.read().decode("utf-8", "replace")[:400]
                except Exception:
                    pass
                last_err = LLMError(f"模型服务返回 HTTP {e.code}：{detail}")
                if e.code < 500:      # 4xx 是请求本身有问题，重试没意义
                    break
            except urllib.error.URLError as e:
                last_err = LLMError(f"连不上模型服务：{getattr(e, 'reason', e)}")
            except TimeoutError:
                last_err = LLMError(f"模型响应超时（{self.timeout:.0f} 秒）")
            except json.JSONDecodeError as e:
                last_err = LLMError(f"模型返回的不是合法 JSON：{e}")
            except LLMError as e:
                last_err = e
            if attempt < retries:
                time.sleep(1.5)
        raise last_err or LLMError("未知错误")

    # ---------------------------------------------------------------- 看图
    def describe_image(self, image_path: Path, prompt: str,
                       max_tokens: int | None = None, retries: int = 1) -> LLMResult:
        image_path = Path(image_path)
        if not image_path.exists():
            raise LLMError(f"截图文件不存在：{image_path}")
        mime = mimetypes.guess_type(image_path.name)[0] or "image/jpeg"
        b64 = base64.b64encode(image_path.read_bytes()).decode("ascii")
        messages = [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
            ],
        }]
        return self.chat(messages, max_tokens=max_tokens, retries=retries)


def _extract_text(data: dict) -> str:
    """兼容几种常见的返回结构。

    最后一层兜底：某些"推理型"模型把答案写进 reasoning_content 而 content 为空。
    这种情况下从推理内容里取最后一段当正文，总比直接判定失败要好，
    但要意识到它可能只是模型的草稿，所以上层会把它标成质量较低。
    """
    choices = data.get("choices") or []
    if not choices:
        return ""
    msg = choices[0].get("message") or {}
    content = msg.get("content")
    if isinstance(content, str) and content.strip():
        return content
    if isinstance(content, list):      # 少数实现返回分段内容
        joined = "".join(part.get("text", "") for part in content if isinstance(part, dict))
        if joined.strip():
            return joined
    for key in ("reasoning_content", "reasoning"):
        extra = msg.get(key)
        if isinstance(extra, str) and extra.strip():
            return _tail_block(extra)
    return str(content or "")


def _tail_block(text: str) -> str:
    """取推理文本里最后一段像答案的内容（跳过纯列表式的思考）。"""
    blocks = [b.strip() for b in text.strip().split("\n\n") if b.strip()]
    for block in reversed(blocks):
        if not block.startswith(("*", "-", "#")) and "：" in block:
            return block
    return blocks[-1] if blocks else ""
