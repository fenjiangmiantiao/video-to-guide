# -*- coding: utf-8 -*-
"""生成便携版专用配置，并做发布前的敏感信息扫描。

为什么不能直接复制开发机的 config.json：
开发配置里可能有 API Key、本机绝对路径（whisper_exe、whisper_cache_home、
analysis_video_exe），这些都不应该跟着发布包走。这里从 dataclass 默认值
重新生成一份干净配置，只覆盖"发布版该有的默认值"。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from video_to_guide.config import Settings, asdict  # noqa: E402

# 发布版允许出现的字段白名单：只放"与用户机器无关"的设置
ALLOWED = {
    "connections", "vision_role", "text_role",
    "frames_backend", "probe_backend", "enable_vision", "reuse_cache",
    "read_long_edge", "max_frames", "max_frames_auto",
    "max_frames_floor", "max_frames_cap", "vision_batch_limit",
    "whisper_model", "whisper_language", "whisper_device", "whisper_compute_type",
    "disable_thinking", "vision_max_tokens", "vision_timeout", "llm_timeout",
    "ffmpeg_exe", "ffprobe_exe",
}
# 一旦在这些字段里发现"像凭据"的内容就中止发布
SUSPICIOUS_KEYS = ("api_key", "token", "secret", "password")

# 发布版默认值：本地模型服务地址留空让用户自己填，不继承开发机的
RELEASE_DEFAULTS = {
    "frames_backend": "builtin",     # 便携版不依赖外部 analysis-video
    "probe_backend": "builtin",      # 元信息用内置 PyAV 读，不要求装 FFmpeg
    "analysis_video_exe": "",        # 清空开发机路径
    "whisper_exe": "",               # 清空开发机路径（用户自己配）
    "whisper_cache_home": "",
    "ffmpeg_exe": "ffmpeg",          # 仅在需要兜底时从 PATH 查找
    "ffprobe_exe": "ffprobe",
    # 便携包内置 small（464 MB），保证开箱即用；要更高中文准确率可在
    # 设置 → 语音识别档位里下载 large-v3（2.9 GB），那里有完整下载与校验流程。
    "whisper_model": "small",
}


# 发布版给一个"开箱能连上本机 LM Studio"的默认，用户改了就在设置里改。
# 不继承开发机的任何连接（那里面可能有中转地址）。
RELEASE_CONNECTIONS = {
    "lmstudio": {"base_url": "http://127.0.0.1:12345/v1", "api_key": ""},
}


def build() -> dict:
    base = asdict(Settings())
    out = {k: v for k, v in base.items() if k in ALLOWED}
    out.update({k: v for k, v in RELEASE_DEFAULTS.items() if k in base})
    # 连接只保留本地 LM Studio 这一条，且 api_key 一律清空
    out["connections"] = {k: {"base_url": v["base_url"], "api_key": ""}
                          for k, v in RELEASE_CONNECTIONS.items()}
    # 角色默认指向 lmstudio；model 留空让用户在自己的机器上选
    out["vision_role"] = {"provider": "lmstudio", "model": ""}
    out["text_role"] = {"provider": "lmstudio", "model": ""}
    return out


def scan(cfg: dict, cfg_path: Path) -> list[str]:
    """发布前扫描：发现凭据或开发机私有路径就报错。"""
    problems: list[str] = []
    text = json.dumps(cfg, ensure_ascii=False).lower()
    for word in SUSPICIOUS_KEYS:
        # 只看"有值"的：api_key 字段本身就是空串是允许的
        for k, v in _walk(cfg):
            if word in k.lower() and isinstance(v, str) and v.strip():
                problems.append(f"{cfg_path.name} 里 {k} 有非空值，疑似凭据")
    for k, v in _walk(cfg):
        if not isinstance(v, str) or not v:
            continue
        if v.startswith(("http://", "https://")):
            continue                      # 服务地址不是文件路径
        if _looks_like_local_path(v):
            problems.append(f"{cfg_path.name} 里 {k} 是开发机绝对路径：{v}")
    return problems


def _looks_like_local_path(text: str) -> bool:
    """判断是不是"这台机器上的绝对路径"。

    只认两种情况：盘符开头（Windows 绝对路径）或 Unix 根目录，
    避免把 http URL 误判成路径。
    """
    if len(text) > 2 and text[1] == ":" and text[0].isalpha():
        return True
    if text.startswith("/") and not text.startswith("//"):
        return True
    return False


def _walk(node, prefix=""):
    if isinstance(node, dict):
        for k, v in node.items():
            key = f"{prefix}.{k}" if prefix else k
            yield from _walk(v, key)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _walk(v, f"{prefix}[{i}]")
    else:
        yield prefix, node


def main() -> int:
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "dist" / "VideoToGuide" / "config.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    cfg = build()
    problems = scan(cfg, target)
    if problems:
        print("[发布中止] 配置里有敏感内容：", file=sys.stderr)
        for p in problems:
            print("  - " + p, file=sys.stderr)
        return 1
    target.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[OK] 已生成发布配置：{target}（{len(cfg)} 个字段，frames_backend={cfg.get('frames_backend')}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())