# -*- coding: utf-8 -*-
"""把录好的原始屏幕录像加工成正式的测试视频（测试 A 用）。

做三件事：
1. 裁剪：只保留演示窗口区域，去掉桌面背景；
2. 截断：只保留 0-120 秒（演示结束就停，避免结尾是空桌面）；
3. 配音：用 Windows 自带的 Huihui 中文语音合成旁白，按时间点混入音轨。

输出：runs/testvideo/demo_settings.mp4（120 秒，含中文旁白）
"""
import base64
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RAW = ROOT / "runs" / "testvideo" / "raw.mkv"
OUT = ROOT / "runs" / "testvideo" / "demo_settings.mp4"
WORK = ROOT / "runs" / "testvideo" / "_voice"
CROP = "1240:860:350:120"
DURATION = 120

# (视频时间秒, 旁白文本)
LINES = [
    (3, "这是示例笔记软件的主界面，我们先来修改它的设置。"),
    (18, "点击工具栏右侧的设置按钮。"),
    (27, "设置窗口打开了，这里可以调整界面主题和自动保存。"),
    (42, "把主题从浅色改成深色。"),
    (62, "再勾选每次编辑后自动保存。"),
    (79, "点击保存按钮。"),
    (85, "软件提示设置已保存。"),
    (97, "回到主界面，界面已经变成深色主题。"),
    (110, "状态栏也显示自动保存已开启，设置修改完成。"),
]


def speak(text: str, out: Path) -> None:
    """用 PowerShell 的 System.Speech 合成中文语音，写成 WAV。"""
    script = (
        "Add-Type -AssemblyName System.Speech; "
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        "$s.SelectVoice('Microsoft Huihui Desktop'); "
        "$s.SetOutputToWaveFile('" + out.as_posix() + "'); "
        "$s.Speak('" + text + "'); "
        "$s.Dispose()"
    )
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    proc = subprocess.run(["powershell", "-NoProfile", "-EncodedCommand", encoded],
                          capture_output=True, timeout=120)
    if proc.returncode != 0 or not out.exists():
        raise RuntimeError("语音合成失败：" + proc.stderr.decode("utf-8", "replace")[:300])


def main() -> int:
    if not RAW.exists():
        print("缺少原始录像：" + str(RAW), file=sys.stderr)
        return 2
    WORK.mkdir(parents=True, exist_ok=True)

    wavs = []
    for i, (t, text) in enumerate(LINES):
        wav = WORK / ("v%02d.wav" % i)
        if not wav.exists():
            speak(text, wav)
        wavs.append(wav)
        print("  旁白 %d/%d @ %ds：%s" % (i + 1, len(LINES), t, text))

    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(RAW)]
    for w in wavs:
        cmd += ["-i", str(w)]

    filters = ["[0:v]crop=" + CROP + ",trim=0:" + str(DURATION) + ",setpts=PTS-STARTPTS[v]"]
    for i, (t, _) in enumerate(LINES):
        filters.append("[%d:a]adelay=%d:all=1[a%d]" % (i + 1, int(t * 1000), i))
    mix_inputs = "".join("[a%d]" % i for i in range(len(LINES)))
    filters.append(mix_inputs + "amix=inputs=" + str(len(LINES)) + ":normalize=0[a]")

    cmd += ["-filter_complex", ";".join(filters),
            "-map", "[v]", "-map", "[a]",
            "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "128k", "-ar", "44100",
            "-movflags", "+faststart", "-t", str(DURATION), str(OUT)]

    print("混流中…")
    proc = subprocess.run(cmd, capture_output=True, timeout=1800)
    if proc.returncode != 0:
        print("ffmpeg 失败：" + proc.stderr.decode("utf-8", "replace")[-1500:], file=sys.stderr)
        return 1
    print("完成：" + str(OUT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
