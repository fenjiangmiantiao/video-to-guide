# 开发说明

面向想改代码或自己打包的人。只想用软件请看 [usage.md](usage.md)。

## 技术选型

| 层 | 选型 | 为什么 |
|---|---|---|
| 语言 | Python 3.12 | 现有依赖（ctranslate2、scikit-image、PySide6）的 wheel 覆盖最全的版本 |
| 视频信息 / 抽帧 | 内置 PyAV + numpy | 不依赖外部 FFmpeg，PyInstaller 能直接打进去 |
| 语音转文字 | faster-whisper（进程内） | 不额外起服务；small 随包分发，large-v3 可选下载 |
| 画面理解 / 步骤整理 | 任一 OpenAI 兼容服务 | 本机 LM Studio / Ollama / llama.cpp，或云端；视觉与文本可分开配 |
| 界面 | PySide6 6.12 | 与处理逻辑解耦，CLI 先行、GUI 后加 |
| 测试 | pytest | 单元测试覆盖解析、校验、渲染、恢复逻辑 |

## 源码结构

```
src/video_to_guide/
  cli.py           命令行入口（doctor / analyze / list / export / gui）
  pipeline.py      一次分析的主流程与状态机
  config.py        设置项与持久化
  store.py         运行目录布局、读写、迁移
  health.py        AI 服务与软件环境体检
  providers.py     服务商预设与连接配置
  parse/           视频信息、转录、抽帧、时间线对齐
  vision/          截图描述（分批、并发、缓存）
  steps/           步骤生成、覆盖检查、证据校验、教学分类
  render/          Markdown / HTML 手册
  gui/             PySide6 界面
tests/             pytest；fixtures 里是脱敏后的真实数据
scripts/           发布相关：配置清理、DLL 补齐、语音模型拷贝
```

## 运行目录布局

```
runs/<run_id>/
  project.json        任务元信息
  output/             成品：guide.html、guide.md、frames/
  .work/              中间数据：video/transcript/frames/vision/timeline/steps/run_report
                      steps.json 里有你的人工修改，删 .work 会丢
```

## 开发环境

```bash
python -m venv .venv
.venv/Scripts/pip install -e .
.venv/Scripts/python -m pytest tests -q
.venv/Scripts/python -m video_to_guide.cli gui
```

## 打包

```bash
pyinstaller VideoToGuide.spec --noconfirm
python scripts/stage_runtime_dlls.py     # 补 PyInstaller 漏收的原生 DLL
python scripts/copy_whisper_model.py     # 内置语音模型（默认 small）
python scripts/make_release_config.py    # 生成干净的默认配置并扫描敏感信息
```

产物在 `dist/VideoToGuide/`，整目录拷走即可独立运行。

## 几个设计约束

- **不得把未经证实的推测写成已发生的操作**：每步必须挂字幕或截图证据，无证据的步骤不写进手册。
- **失败不得伪装成功**：占位步骤不进手册；分析失败时旧手册改名 `guide.stale-*` 并写 `分析失败.txt`。
- **现实模式先判断是否为教学视频**：吃播、探店、纯展示直接失败，不产出空壳手册。
- **缓存按输入指纹认**：换模型、改提示词、重新抽帧都会让对应缓存失效，不会误用旧结果。

## 许可证

MIT，见 [LICENSE](../LICENSE)。