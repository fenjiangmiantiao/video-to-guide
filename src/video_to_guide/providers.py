"""常见大模型服务的连接预设。

只预填"接口地址 + 常见视觉模型名"，API Key 由用户自己填。
地址取自各家官方文档（见每条的 doc_url）；厂商可能调整地址或模型名，
所以设置界面允许手动修改，也可以选「自定义」完全自己填。

为什么用 OpenAI 兼容接口统一对接：视觉模型厂商几乎都提供
/v1/chat/completions 兼容层，请求体一致（messages 里放 image_url），
这样一套代码就能换服务商，不必给每家写一个适配器。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Provider:
    """一家服务商的预设。只描述"怎么连"，不含密钥。"""

    id: str
    name: str
    base_url: str
    vision_models: tuple[str, ...] = ()
    text_models: tuple[str, ...] = ()
    key_url: str = ""       # 获取 API Key 的页面
    doc_url: str = ""       # 官方文档
    local: bool = False     # 本机服务，不需要 API Key
    note: str = ""          # 界面上的补充说明


PROVIDERS: tuple[Provider, ...] = (
    # ---------------- 本机 ----------------
    Provider(
        id="lmstudio",
        name="本机服务（免费 · OpenAI 兼容）",
        base_url="http://127.0.0.1:12345/v1",
        # 本机服务的模型由服务端决定，不给假预设：点「获取模型」拿到真实列表，
        # 或者手动填一个 ID。这样下拉框里不会出现点不动的占位名。
        #
        # id 仍叫 lmstudio 是为了兼容已保存的 config.json；显示名通用化，
        # 因为 LM Studio / llama.cpp / vLLM 都可以监听在 12345，接口完全一致。
        vision_models=(),
        text_models=(),
        local=True,
        note="任选一种本机服务：LM Studio（默认 12345 端口）、llama.cpp llama-server、"
             "vLLM。只要兼容 OpenAI 接口，在本机加载一个支持图像输入的模型即可。",
        doc_url="",
    ),
    Provider(
        id="ollama",
        name="本机 Ollama",
        base_url="http://127.0.0.1:11434/v1",
        vision_models=("qwen2.5vl:7b", "llava:latest", "llama3.2-vision:latest"),
        text_models=("qwen2.5:7b", "llama3.1:8b"),
        local=True,
        note="先执行 ollama pull <模型名> 拉取模型，再保持 ollama serve 运行（11434 端口）。",
        doc_url="https://docs.ollama.com/api/openai-compatibility",
    ),
    Provider(
        id="localcustom",
        name="自定义本地服务",
        base_url="http://127.0.0.1:8000/v1",
        vision_models=(),
        text_models=(),
        local=True,
        note="自建的 vLLM / llama.cpp(8080) / one-api / 其他中转，只要兼容 OpenAI 接口就能接。",
    ),
    # ---------------- 云端 ----------------
    Provider(
        id="openai",
        name="OpenAI",
        base_url="https://api.openai.com/v1",
        vision_models=("gpt-4o", "gpt-4o-mini", "gpt-4.1", "gpt-4.1-mini"),
        text_models=("gpt-4o-mini", "gpt-4.1-mini"),
        key_url="https://platform.openai.com/api-keys",
        doc_url="https://platform.openai.com/docs/api-reference/chat",
    ),
    Provider(
        id="anthropic",
        name="Anthropic Claude",
        base_url="https://api.anthropic.com/v1",
        vision_models=("claude-sonnet-4-5", "claude-haiku-4-5", "claude-opus-4-1"),
        text_models=("claude-haiku-4-5", "claude-sonnet-4-5"),
        key_url="https://console.anthropic.com/settings/keys",
        doc_url="https://docs.anthropic.com/en/api/openai-sdk",
        note="走 Anthropic 的 OpenAI 兼容层。",
    ),
    Provider(
        id="gemini",
        name="Google Gemini",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        vision_models=("gemini-2.5-flash", "gemini-2.5-pro", "gemini-2.0-flash"),
        text_models=("gemini-2.5-flash",),
        key_url="https://aistudio.google.com/apikey",
        doc_url="https://ai.google.dev/gemini-api/docs/openai",
        note="走 Gemini 的 OpenAI 兼容层，注意地址结尾保留斜杠。",
    ),
    Provider(
        id="deepseek",
        name="DeepSeek 深度求索",
        base_url="https://api.deepseek.com/v1",
        vision_models=(),          # 目前没有公开的视觉模型
        text_models=("deepseek-chat", "deepseek-reasoner"),
        key_url="https://platform.deepseek.com/api_keys",
        doc_url="https://api-docs.deepseek.com/",
        note="目前没有视觉模型，只能用它的文本模型生成步骤；识图请选别家。",
    ),
    Provider(
        id="dashscope",
        name="阿里云百炼（通义千问）",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        vision_models=("qwen-vl-max-latest", "qwen-vl-plus-latest",
                       "qwen2.5-vl-72b-instruct"),
        text_models=("qwen-plus", "qwen-max"),
        key_url="https://bailian.console.aliyun.com/?apiKey=1",
        doc_url="https://help.aliyun.com/zh/model-studio/compatibility-of-openai-with-dashscope",
        note="国际站地址是 https://dashscope-intl.aliyuncs.com/compatible-mode/v1。",
    ),
    Provider(
        id="zhipu",
        name="智谱 GLM",
        base_url="https://open.bigmodel.cn/api/paas/v4",
        vision_models=("glm-4.5v", "glm-4v-plus", "glm-4v-flash"),
        text_models=("glm-4-plus", "glm-4-flash"),
        key_url="https://open.bigmodel.cn/usercenter/apikeys",
        doc_url="https://docs.bigmodel.cn/cn/guide/develop/openai/introduction",
    ),
    Provider(
        id="moonshot",
        name="月之暗面 Kimi",
        base_url="https://api.moonshot.cn/v1",
        vision_models=("moonshot-v1-8k-vision-preview", "kimi-latest"),
        text_models=("moonshot-v1-8k",),
        key_url="https://platform.moonshot.cn/console/api-keys",
        doc_url="https://platform.moonshot.cn/docs/api/chat",
    ),
    Provider(
        id="siliconflow",
        name="硅基流动 SiliconFlow",
        base_url="https://api.siliconflow.cn/v1",
        vision_models=("Qwen/Qwen2.5-VL-72B-Instruct",
                       "Qwen/Qwen2.5-VL-32B-Instruct"),
        text_models=("Qwen/Qwen2.5-72B-Instruct",),
        key_url="https://cloud.siliconflow.cn/account/ak",
        doc_url="https://docs.siliconflow.cn/cn/api-reference/chat-completions/chat-completions",
        note="国内可直连，视觉模型按 token 计费，价格较低。",
    ),
    Provider(
        id="ark",
        name="火山方舟（豆包）",
        base_url="https://ark.cn-beijing.volces.com/api/v3",
        vision_models=("doubao-1.5-vision-pro-32k", "doubao-vision-pro-32k"),
        text_models=("doubao-1.5-pro-32k",),
        key_url="https://console.volcengine.com/ark/region:ark+cn-beijing/apiKey",
        doc_url="https://www.volcengine.com/docs/82379/1298454",
        note="多数情况要填「推理接入点 ID」（ep-xxxx）而不是模型名，请以控制台为准。",
    ),
    Provider(
        id="openrouter",
        name="OpenRouter（聚合多家）",
        base_url="https://openrouter.ai/api/v1",
        vision_models=("openai/gpt-4o", "google/gemini-2.5-flash",
                       "qwen/qwen2.5-vl-72b-instruct"),
        text_models=("openai/gpt-4o-mini",),
        key_url="https://openrouter.ai/settings/keys",
        doc_url="https://openrouter.ai/docs/quickstart",
        note="一个 Key 调用多家模型，模型名带厂商前缀。",
    ),
    Provider(
        id="xai",
        name="xAI Grok",
        base_url="https://api.x.ai/v1",
        vision_models=("grok-4", "grok-2-vision-1212"),
        text_models=("grok-3-mini",),
        key_url="https://console.x.ai/",
        doc_url="https://docs.x.ai/docs/overview",
    ),
    Provider(
        id="mistral",
        name="Mistral AI",
        base_url="https://api.mistral.ai/v1",
        vision_models=("pixtral-large-latest", "pixtral-12b-2409"),
        text_models=("mistral-large-latest",),
        key_url="https://console.mistral.ai/api-keys",
        doc_url="https://docs.mistral.ai/api/",
    ),
    Provider(
        id="qianfan",
        name="百度智能云千帆",
        base_url="https://qianfan.baidubce.com/v2",
        vision_models=("ernie-4.5-turbo-vl-32k-preview",),
        text_models=("ernie-4.5-turbo-32k",),
        key_url="https://console.bce.baidu.com/iam/#/iam/apikey/list",
        doc_url="https://cloud.baidu.com/doc/WENXINWORKSHOP/s/Fm2vrveyu",
        note="用「API Key」方式鉴权（不是老的 AK/SK）。",
    ),
    Provider(
        id="hunyuan",
        name="腾讯混元",
        base_url="https://api.hunyuan.cloud.tencent.com/v1",
        vision_models=("hunyuan-vision", "hunyuan-t1-vision"),
        text_models=("hunyuan-turbos-latest",),
        key_url="https://console.cloud.tencent.com/hunyuan/api-key",
        doc_url="https://cloud.tencent.com/document/product/1729/111007",
    ),
    # ---------------- 兜底 ----------------
    Provider(
        id="custom",
        name="自定义（任意 OpenAI 兼容服务）",
        base_url="http://127.0.0.1:8000/v1",
        vision_models=(),
        text_models=(),
        note="填写任何兼容 /v1/chat/completions 的服务地址，例如自建的 vLLM、one-api 网关。",
    ),
)

BY_ID: dict[str, Provider] = {p.id: p for p in PROVIDERS}


def get(provider_id: str) -> Provider:
    """按 id 取预设；未知 id 退回自定义，避免旧配置文件打不开。"""
    return BY_ID.get(provider_id) or BY_ID["custom"]


def guess_vision_provider(provider_id: str) -> bool:
    """这家服务是否声称支持视觉模型（用于在界面上给出提示）。"""
    p = get(provider_id)
    return bool(p.vision_models) or p.id == "custom"


def sends_reasoning_effort(provider_id: str) -> bool:
    """要不要在请求里带 reasoning_effort。

    LM Studio 用 chat_template_kwargs 关思考；其它自托管服务和中转服务更常见的
    是 OpenAI 风格的 reasoning_effort。实测对 127.0.0.1:3425 上的
    auto/deepseek-v4.1-flash 有效：同一个步骤生成请求从 13.4 秒（含 4377 字推理，
    max_tokens 被推理吃光、正式 JSON 被截断）降到 4.2 秒且没有推理内容。
    """
    return provider_id != "lmstudio"


def sends_thinking_flag(provider_id: str, user_flag: bool) -> bool:
    """要不要在请求里带 chat_template_kwargs。

    这个参数是 llama.cpp / vLLM 这类自托管推理框架的私有扩展；
    OpenAI、DeepSeek 等云端厂商收到不认识的字段会直接返回 400，
    所以只对本机服务和自定义服务发送。
    """
    if not user_flag:
        return False
    return get(provider_id).local or provider_id == "custom"
