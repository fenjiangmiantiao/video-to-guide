#!/bin/bash
# 录制测试视频：全屏录屏，同时运行 demo_app.py 自动演示四步操作。
# 容器用 MKV：即使录制被中断，MKV 也不会像 MP4 那样丢失索引导致文件损坏。
set -u
cd "$(dirname "$0")/../.."
OUT="runs/testvideo"
mkdir -p "$OUT"
rm -f "$OUT/raw.mkv"

ffmpeg -y -hide_banner -loglevel error -f gdigrab -framerate 15 -draw_mouse 1 \
  -i desktop -t 165 -c:v libx264 -preset veryfast -crf 20 -pix_fmt yuv420p \
  "$OUT/raw.mkv" &
FFPID=$!
sleep 2
"./.venv/Scripts/python.exe" tests/fixtures/demo_app.py
RC=$?
sleep 4
kill -TERM $FFPID 2>/dev/null || true
for i in $(seq 1 10); do
  kill -0 $FFPID 2>/dev/null || break
  sleep 1
done
kill -9 $FFPID 2>/dev/null || true
wait $FFPID 2>/dev/null || true
echo "demo exit=$RC"
ls -la "$OUT"
