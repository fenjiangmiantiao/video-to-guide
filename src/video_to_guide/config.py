"""运行配置。

优先级：命令行参数 > 环境变量 > 项目根目录的 config.json > 这里的默认值。

为什么要有配置文件而不是写死：本项目依赖几个"本机已装好"的外部程序
（ffmpeg、whisper-ctranslate2、analysis-video、本地模型服务），
不同电脑上的路径不同，写死会导致换台机器就跑不起来。

模型配置分两层，是为了让"视觉分析"和"步骤整理"能用不同来源：
  连接配置 connections  = 每个服务商一套 {base_url, api_key}，只存一份；
  任务角色 vision_role / text_role = {provider, model}，引用上面的连接。
这样切角色不会互相覆盖 Key，也不会重复存同一个 Key。
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, asdict, field, fields
from pathlib import Path


def _app_root() -> Path:
    """程序根目录。

    源码运行时是项目根（src/video_to_guide/config.py 往上两级）；
    被 PyInstaller 打包后 __file__ 在临时解包目录里，必须改用 exe 所在目录，
    否则 config.json 和 runs/ 会被写到解包目录，用户根本找不到。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


PROJECT_ROOT = _app_root()
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.json"
RUNS_DIR = PROJECT_ROOT / "runs"

ROLE_VISION = "vision"
ROLE_TEXT = "text"


@dataclass
class Settings:
    """所有可调项。字段名即 config.json 的键名。"""

    # --- 连接配置：{服务商 id: {"base_url": str, "api_key": str}} ---
    connections: dict = field(default_factory=dict)
    # --- 任务角色：{"provider": 服务商 id, "model": 模型 id} ---
    vision_role: dict = field(default_factory=dict)   # 看截图
    text_role: dict = field(default_factory=dict)     # 整理操作步骤

    # --- 旧版单套配置（保留读写兼容，不再作为唯一来源）---
    llm_provider: str = "lmstudio"
    llm_api_key: str = ""
    llm_base_url: str = "http://127.0.0.1:12345/v1"
    llm_model: str = "local"
    vision_model: str = "local"

    vision_timeout: float = 300.0        # 单张图最长等待秒数
    vision_max_tokens: int = 2048        # 关闭思考后实际只用 100~200，留余量给不支持该参数的服务
    llm_timeout: float = 600.0           # 生成步骤（一次处理多段文本）给更长时间
    disable_thinking: bool = True        # 推理型模型先输出思考过程，会吃光 token 且慢 7 倍

    # --- 语音转录（外部 whisper 工具）---
    # auto = 进程内用 faster-whisper（便携包自带，推荐）；cli = 调外部 whisper-ctranslate2
    whisper_backend: str = "auto"
    whisper_exe: str = ""
    whisper_model: str = "small"
    whisper_model_dir: str = ""      # 空 = 自动找 程序目录/models/ 或 HF 缓存
    whisper_language: str = "zh"
    # 推理设备：auto = 有 CUDA 就用 GPU（快一个数量级），没有退回 CPU
    whisper_device: str = "auto"
    # 计算精度：auto = GPU 用 float16、CPU 用 int8；也可显式写 float16/int8/int8_float16 等
    whisper_compute_type: str = "auto"
    whisper_cache_home: str = ""     # 空 = 自动探测常见位置（HF 缓存 / 程序目录）
    # 术语提示：逗号分隔的专有名词（软件名、菜单名、专业词），帮助识别这类词
    whisper_hotwords: str = ""
    # 开头提示：一段自然中文，用来定住标点风格和话题（只影响开头，长音频靠 hotwords）
    whisper_initial_prompt: str = ""
    # 是否已经问过"要不要换更准的语音模型"。只问一次，问过就不再打扰。
    asr_prompted: bool = False

    # --- 视频解析（analysis-video）---
    analysis_video_exe: str = ""          # 空表示用当前 Python 环境的 analysis-video
    # auto = 有 analysis-video 就用它（更准），没有就用内置抽帧器；
    # builtin = 强制用内置抽帧器（便携版默认，不依赖任何外部 Python 包）；
    # external = 强制用 analysis-video，找不到就报错。
    frames_backend: str = "auto"
    # 视频元信息读取：auto = 内置 PyAV 优先、失败再试 ffprobe；
    # builtin = 只用内置（便携版默认，不要求装 FFmpeg）；ffprobe = 只用外部工具。
    probe_backend: str = "auto"
    ffmpeg_exe: str = "ffmpeg"
    ffprobe_exe: str = "ffprobe"
    # 视频类型决定用哪套提示词和步骤结构：
    #   software = 软件操作录屏（默认，已实测验证）
    #   reality  = 现实场景操作（做饭、手工、装配；Beta，尚未通过验收）
    # 两条路线的提示词、字段、质量门禁彼此独立，改一个不影响另一个。
    video_mode: str = "software"
    read_long_edge: int = 768             # 交给视觉模型的图长边像素
    # 视觉描述并发数。1 = 顺序（本机单卡推荐）；云端路由可以开到 4-6。
    vision_workers: int = 1
    # 抽帧策略：signal = 按"画面变化 + 讲解密度"选帧（推荐）；even = 旧的按时长均匀抽
    frame_pick: str = "signal"
    max_frames_auto: bool = True
    max_frames: int = 80                  # frame_pick=even 且 max_frames_auto=False 时才是硬上限
    max_frames_floor: int = 60
    max_frames_cap: int = 0               # 0 = 不设上限，抽多少由画面和讲解决定
    # 两张截图的最小时间间隔：再密也隔这么久，避免同一秒抽好几张几乎一样的图
    frame_min_gap: float = 1.5
    # 权重低于这个值的帧直接丢（首帧除外）
    frame_weight_min: float = 0.12
    # 权重里"画面变化"和"讲解密度"各占多少（两者都归一到 0-1）
    frame_weight_scene: float = 0.55
    frame_weight_speech: float = 0.45
    # 步骤生成时给每屏附什么上下文：
    #   plain = 只有本屏（默认）
    #   diff  = 额外给一行「相对上一屏的变化」（P2 时序整合实验开关）
    # 用已有视觉文字算差异，不额外调用模型；是否默认开启由实测结果决定。
    frame_context: str = "plain"
    # 生成步骤后做覆盖检查：把「有画面描述动作、却没有任何步骤覆盖」的时段再补一轮。
    # 归因实测（docs/p2-attribution.md）显示漏步骤 100% 丢在这一层，所以默认开。
    fill_gaps: bool = True

    # --- 模型列表（从服务商动态获取 + 用户自己整理）---
    # 三个都是 {服务商 id: ...}，按服务商分别保存，切服务商不会串数据。
    hidden_models: dict = field(default_factory=dict)   # 被隐藏的模型 id：只影响本软件的下拉列表
    manual_models: dict = field(default_factory=dict)   # 用户手动填的模型 id（服务商列不出来时用）
    model_cache: dict = field(default_factory=dict)     # 上次获取到的列表，用于离线显示和"这是缓存"提示

    # --- 导出命名（只影响导出包，不影响任务目录）---
    # 可用占位符：{video} {date} {time} {run_id} {title}
    export_mode_default: str = "light"           # light=不含视频；full=连源视频一起打包
    export_root: str = ""                        # 空 = 用户「文档/Video to Guide/Exports」
    export_layout: str = "by_video_batch"        # by_video_batch=视频/批次两层目录；flat=一层
    export_video_template: str = "{video}"       # 视频分组目录名
    export_batch_template: str = "{date}-{time}" # 批次目录名
    export_name_template: str = "{title}"        # 手册文件名（不含扩展名）

    # --- 可选组件 ---
    portable_data: bool = False           # True = 组件装到程序目录 Data/；False = 用户数据目录

    # --- 行为 ---
    reuse_cache: bool = True              # 有中间结果就复用，避免重复计算
    vision_batch_limit: int = 0           # 0 = 不限制；>0 时最多描述这么多张图
    enable_vision: bool = True             # False = 完全不看画面，只依据字幕出步骤

    # ------------------------------------------------------------ 连接配置
    def connection(self, provider_id: str) -> dict:
        """某服务商的连接配置。用户填过的优先，没填过就用预设地址。"""
        from .providers import get as get_provider

        stored = self.connections.get(provider_id)
        stored = stored if isinstance(stored, dict) else {}
        preset = get_provider(provider_id)
        base_url = str(stored.get("base_url") or preset.base_url or "").strip()
        api_key = str(stored.get("api_key") or "").strip()
        return {"base_url": base_url, "api_key": api_key}

    def set_connection(self, provider_id: str, base_url: str, api_key: str) -> None:
        self.connections[provider_id] = {
            "base_url": str(base_url or "").strip(),
            "api_key": str(api_key or "").strip(),
        }

    # ------------------------------------------------------------ 任务角色
    def role(self, kind: str) -> dict:
        """任务角色：{"provider": 服务商 id, "model": 模型 id}。

        缺省时回落到旧版单套配置，保证老 config.json 和没配置过的电脑都能跑。
        """
        raw = self.vision_role if kind == ROLE_VISION else self.text_role
        raw = raw if isinstance(raw, dict) else {}
        pid = str(raw.get("provider") or self.llm_provider or "lmstudio")
        legacy = self.vision_model if kind == ROLE_VISION else self.llm_model
        model = str(raw.get("model") or legacy or "")
        return {"provider": pid, "model": model}

    def set_role(self, kind: str, provider_id: str, model: str) -> None:
        value = {"provider": str(provider_id or ""), "model": str(model or "")}
        if kind == ROLE_VISION:
            self.vision_role = value
        else:
            self.text_role = value

    def role_client(self, kind: str) -> tuple[str, str, str, str]:
        """给调用方用的四元组：(服务商 id, 接口地址, API Key, 模型 id)。"""
        role = self.role(kind)
        conn = self.connection(role["provider"])
        return role["provider"], conn["base_url"], conn["api_key"], role["model"]

    # ------------------------------------------------------------ 读写
    @classmethod
    def load(cls, path: Path | None = None) -> "Settings":
        path = path or DEFAULT_CONFIG_PATH
        data: dict = {}
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                data = {}
        data = _migrate(data if isinstance(data, dict) else {})
        # 环境变量覆盖：VTG_ 前缀 + 大写字段名
        for f in fields(cls):
            env_key = "VTG_" + f.name.upper()
            if env_key in os.environ:
                raw = os.environ[env_key]
                data[f.name] = _coerce(raw, f.type)
        known = {f.name for f in fields(cls)}
        clean = {k: v for k, v in data.items() if k in known}
        return cls(**clean)

    def save(self, path: Path | None = None) -> Path:
        path = path or DEFAULT_CONFIG_PATH
        data = asdict(self)
        # 同步旧字段：老版本程序和命令行仍读 llm_*，人手看 config.json 时也更直观。
        vision = self.role(ROLE_VISION)
        conn = self.connection(vision["provider"])
        data["llm_provider"] = vision["provider"]
        data["llm_base_url"] = conn["base_url"]
        data["llm_api_key"] = conn["api_key"]
        data["vision_model"] = vision["model"]
        data["llm_model"] = self.role(ROLE_TEXT)["model"]
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return path


def _migrate(data: dict) -> dict:
    """把旧版"单套 llm_* 配置"补成新版"连接配置 + 两个任务角色"。

    原则：只补空缺，绝不覆盖用户已经写好的新字段，也不丢任何旧值。
    """
    d = dict(data)
    pid = str(d.get("llm_provider") or "lmstudio")
    base = str(d.get("llm_base_url") or "").strip()
    key = str(d.get("llm_api_key") or "").strip()

    conns = d.get("connections")
    conns = dict(conns) if isinstance(conns, dict) else {}
    if pid not in conns and (base or key):
        conns[pid] = {"base_url": base, "api_key": key}
    d["connections"] = conns

    for role_key, model_key in ((("vision_role"), "vision_model"),
                                (("text_role"), "llm_model")):
        role = d.get(role_key)
        if not isinstance(role, dict) or not role.get("provider"):
            d[role_key] = {"provider": pid, "model": str(d.get(model_key) or "")}
    return d


def _coerce(raw: str, ftype) -> object:
    """把环境变量字符串转成字段应有的类型。"""
    t = str(ftype)
    if "bool" in t:
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if "int" in t:
        return int(raw)
    if "float" in t:
        return float(raw)
    return raw


def sanitize_hf_env() -> None:
    r"""把 HuggingFace 相关环境变量里的引号去掉。

    踩过的坑：某台机器上 HF_HOME 被设成 "E:Aihf-cache"（字面带双引号），
    huggingface_hub 拼路径时会得到 '"E:Aihf-cache"	oken'，
    直接抛 OSError: [Errno 22] Invalid argument，连"加载已下载的模型"都会失败。
    这个错误信息完全看不出是环境变量的问题，所以在这里统一清理一次。
    """
    for key in ("HF_HOME", "HF_ENDPOINT", "HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE"):
        raw = os.environ.get(key)
        if not raw:
            continue
        cleaned = raw.strip().strip('"').strip("'").strip()
        if cleaned != raw:
            os.environ[key] = cleaned


def ensure_dirs() -> None:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
