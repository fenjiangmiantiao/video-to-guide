# -*- coding: utf-8 -*-
"""把 dense_raw.mkv 加工成基准视频 C：裁剪 + 截断 + Huihui 中文旁白。"""
import base64
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RAW = ROOT / "runs" / "testvideo" / "dense_raw.mkv"
OUT = ROOT / "runs" / "testvideo" / "dense_settings.mp4"
WORK = ROOT / "runs" / "testvideo" / "_voice_dense"
CROP = "1240:860:350:120"
DURATION = 55

LINES = [
    (8.5, "打开设置窗口。"),
    (15.5, "把编辑器字号从十二改成十六。"),
    (20.5, "把主题从浅色改成深色。"),
    (25.5, "勾选每次编辑后自动保存。"),
    (30.5, "再勾选启动时恢复上次会话。"),
    (37.0, "点击保存按钮。"),
    (40.0, "软件提示设置已保存。"),
    (44.0, "回到主界面，主题已经变成深色，设置全部生效。"),
]


def speak(text: str, out: Path) -> None:
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
        wav = WORK / ("d%02d.wav" % i)
        if not wav.exists():
            speak(text, wav)
        wavs.append(wav)
        print("  旁白 %d/%d @ %.1fs：%s" % (i + 1, len(LINES), t, text))

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
    proc = subprocess.run(cmd, capture_output=True, timeout=900)
    if proc.returncode != 0:
        print("ffmpeg 失败：" + proc.stderr.decode("utf-8", "replace")[-1500:], file=sys.stderr)
        return 1
    print("完成：" + str(OUT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
