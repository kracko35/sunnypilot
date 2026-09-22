"""任意実行: 合成RGBAのパイプ入力・初回出力・PTSを計測する。実機の端末間遅延ではない。"""

import argparse
import json
import os
import re
import shutil
import subprocess
import threading
import time

from openpilot.system.ui.lib.tests.screen_stream_test_support import load_screen_stream
from openpilot.system.ui.lib.screen_stream_config import ScreenStreamConfig

stream = load_screen_stream()


def ffmpeg_path():
  executable = os.getenv('SCREEN_STREAM_TEST_FFMPEG') or shutil.which('ffmpeg')
  if not executable:
    raise RuntimeError('FFmpegのパスをSCREEN_STREAM_TEST_FFMPEGへ指定してください')
  return executable


def feed_frames(config, schedule, *, command=None, frame=None):
  command = list(command or stream.ffmpeg_command(config))
  command[0] = ffmpeg_path()
  command[1:1] = ['-protocol_whitelist', 'file,pipe']
  frame = frame if frame is not None else bytes(stream.FRAME_BYTES)
  chunks, errors, writes, arrivals, first_output = [], [], [], [], []
  proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
  started = time.perf_counter()

  def receive():
    while chunk := proc.stdout.read(65536):
      if not first_output:
        first_output.append(time.perf_counter() - started)
      chunks.append(chunk)

  def write():
    try:
      for offset in schedule:
        while (remaining := started + offset - time.perf_counter()) > 0:
          time.sleep(remaining)
        before = time.perf_counter()
        arrivals.append(before - started)
        data = memoryview(frame)
        while data:
          size = proc.stdin.write(data)
          if not size:
            raise BrokenPipeError('ベンチマークの入力が閉じられました')
          data = data[size:]
        writes.append(time.perf_counter() - before)
    except Exception as error:
      errors.append(repr(error))
    finally:
      proc.stdin.close()

  stderr = []
  threads = [threading.Thread(target=receive), threading.Thread(target=write),
             threading.Thread(target=lambda: stderr.append(proc.stderr.read()))]
  try:
    for thread in threads:
      thread.start()
    proc.wait(timeout=max(schedule, default=0) + 15)
  finally:
    if proc.poll() is None:
      proc.kill()
      proc.wait(timeout=2)
    for thread in threads:
      if thread.ident is not None:
        thread.join(timeout=2)
    proc.stdout.close()
    proc.stderr.close()
  diagnostic = b''.join(stderr).decode(errors='replace')
  if proc.returncode or errors or any(thread.is_alive() for thread in threads):
    raise RuntimeError(f'FFmpeg異常終了 rc={proc.returncode} errors={errors} stderr={diagnostic}')
  ts = b''.join(chunks)
  elapsed = time.perf_counter() - started
  metrics = {'bitrate': config.bitrate, 'frames': len(writes), 'feed_times': arrivals,
             'stdin_write_avg_ms': sum(writes) * 1000 / len(writes), 'stdin_write_max_ms': max(writes) * 1000,
             'first_stdout_ms': first_output[0] * 1000 if first_output else None,
             'throughput_fps': len(writes) / elapsed, 'output_bytes': len(ts), 'stderr': diagnostic}
  return ts, metrics


def decode_pts(ts):
  decoded = subprocess.run([ffmpeg_path(), '-i', 'pipe:0', '-vf', 'showinfo', '-f', 'null', '-'],
                           input=ts, capture_output=True, timeout=20)
  if decoded.returncode:
    raise RuntimeError(decoded.stderr.decode(errors='replace'))
  pts = [float(value) for value in re.findall(rb'pts_time:([\d.\-]+)', decoded.stderr)]
  return pts, decoded.stderr.decode(errors='replace')


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument('--frames', type=int, default=100)
  args = parser.parse_args()
  if args.frames < 2:
    parser.error('--framesは2以上を指定してください')
  # 静止黒画面より負荷のある合成模様。同じ画像を全ビットレートで使用する。
  frame = bytes((i * 37 + i // 3200) % 256 for i in range(stream.FRAME_BYTES))
  for bitrate in [500, 1500, 3000]:
    ts, result = feed_frames(ScreenStreamConfig(bitrate=bitrate), [i / stream.FPS for i in range(args.frames)], frame=frame)
    pts, _ = decode_pts(ts)
    result['pts_span_s'] = pts[-1] - pts[0]
    result['feed_span_s'] = result['feed_times'][-1] - result['feed_times'][0]
    del result['feed_times']
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
  main()
