#!/bin/bash
# 录制基准视频 C（密集操作）：全屏录屏 + demo_dense.py 自动演示六个动作。
set -u
cd "$(dirname "$0")/../.."
OUT="runs/testvideo"
mkdir -p "$OUT"
rm -f "$OUT/dense_raw.mkv"

ffmpeg -y -hide_banner -loglevel error -f gdigrab -framerate 15 -draw_mouse 1 \
  -i desktop -t 62 -c:v libx264 -preset veryfast -crf 20 -pix_fmt yuv420p \
  "$OUT/dense_raw.mkv" &
FFPID=$!
sleep 2
"./.venv/Scripts/python.exe" tests/fixtures/demo_dense.py
RC=$?
sleep 3
kill -TERM $FFPID 2>/dev/null || true
for i in $(seq 1 10); do
  kill -0 $FFPID 2>/dev/null || break
  sleep 1
done
kill -9 $FFPID 2>/dev/null || true
wait $FFPID 2>/dev/null || true
echo "dense demo exit=$RC"
ls -la "$OUT/dense_raw.mkv" 2>/dev/null

# 录完立即合成（裁剪 + 旁白），一次跑完减少中间窗口干扰
"./.venv/Scripts/python.exe" tests/fixtures/make_dense_video.py
