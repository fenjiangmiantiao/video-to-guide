@echo off
REM ============================================================
REM  打包成 Windows 桌面程序
REM  产物：dist\VideoToGuide\VideoToGuide.exe（整个文件夹一起拷走）
REM  用 --onedir 而不是 --onefile：启动快，出错时日志也看得见
REM ============================================================
setlocal
chcp 65001 >nul
cd /d "%~dp0"

echo [1/3] 清理旧的打包产物...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

echo [2/4] 开始打包（第一次约 5-8 分钟）...
REM numpy / av / PIL: built-in frame extractor (parse/builtin_frames.py).
REM ctranslate2 / faster_whisper / tokenizers: built-in speech recognition.
REM Everything ships with the app - users must not download anything.
".venv\Scripts\pyinstaller.exe" --noconfirm --clean --name VideoToGuide --windowed --paths src --collect-submodules video_to_guide --collect-binaries av --hidden-import av --hidden-import numpy --hidden-import PIL --hidden-import PIL.Image --collect-all faster_whisper --collect-all ctranslate2 --collect-all tokenizers --hidden-import onnxruntime --exclude-module tkinter --exclude-module matplotlib --exclude-module pandas --exclude-module scipy --exclude-module scenedetect run_gui.py

if errorlevel 1 (
  echo.
  echo [失败] 打包出错，请把上面的报错发给我。
  exit /b 1
)

REM Remove the intermediate build dir: build\VideoToGuide\VideoToGuide.exe cannot run
REM (Failed to load Python DLL ... python312.dll) and only confuses people.
if exist build rmdir /s /q build

REM Generate the portable config from clean defaults. Do NOT copy the dev config.json:
REM it may contain API keys or absolute dev-machine paths.
".venv\Scripts\python.exe" scripts\make_release_config.py "dist\VideoToGuide\config.json"
if errorlevel 1 (
  echo.
  echo [FAILED] release config contains sensitive fields; packaging aborted.
  exit /b 1
)

echo [3/4] 补齐运行时 DLL + 复制语音模型...
REM PyInstaller misses ffi.dll for _ctypes on Anaconda-built Python; without it
REM "import ctypes" fails at runtime and speech recognition becomes unavailable.
".venv\Scripts\python.exe" scripts\stage_runtime_dlls.py
REM 模型是纯数据文件，PyInstaller 不会自动收；不带它的话，有旁白的视频会转录失败。
".venv\Scripts\python.exe" scripts\copy_whisper_model.py
if errorlevel 1 (
  echo [WARN] speech model copy failed; narration videos cannot be transcribed.
)

echo [4/4] done: dist\VideoToGuide\VideoToGuide.exe
echo Everything is bundled: frame extraction, video probing, speech recognition.
echo Only a model service (LM Studio / Ollama / any OpenAI-compatible API) is required.
endlocal