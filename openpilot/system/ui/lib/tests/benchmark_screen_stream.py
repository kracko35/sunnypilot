"""任意実行: 識別子付き合成RGBAとPESの各frame時刻を照合する。端末間遅延ではない。"""

import argparse
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time

from openpilot.system.ui.lib.tests.screen_stream_test_support import load_screen_stream
from openpilot.system.ui.lib.screen_stream_config import ScreenStreamConfig
from openpilot.system.ui.lib.tests.screen_stream_diagnostics import MARKER_BITS, MARKER_HEIGHT, frame_with_marker, match_frame_timings, video_pes_events

stream = load_screen_stream()


def ffmpeg_path():
  executable = os.getenv('SCREEN_STREAM_TEST_FFMPEG') or shutil.which('ffmpeg')
  if not executable:
    raise RuntimeError('FFmpegのパスをSCREEN_STREAM_TEST_FFMPEGへ指定してください')
  return executable


def feed_frames(config, schedule, *, command=None, frame=None, frame_diagnostics=False, udp_local_address=None):
  command = list(command or stream.ffmpeg_command(config))
  command[0] = ffmpeg_path()
  command[1:1] = ['-protocol_whitelist', 'file,pipe']
  frame = frame if frame is not None else bytes(stream.FRAME_BYTES)
  chunks, errors, writes, arrivals, first_output = [], [], [], [], []
  completions, chunk_ends, chunk_times = [], [], []
  proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
  started = time.perf_counter()

  def observe(chunk):
    observed = time.perf_counter() - started
    if not first_output:
      first_output.append(observed)
    chunks.append(chunk)
    chunk_ends.append((chunk_ends[-1] if chunk_ends else 0) + len(chunk))
    chunk_times.append(observed)

  def receive():
    while chunk := proc.stdout.read(65536):
      observe(chunk)

  def write():
    try:
      for index, offset in enumerate(schedule):
        marked = frame_with_marker(frame, index, stream.WIDTH) if frame_diagnostics else frame
        while (remaining := started + offset - time.perf_counter()) > 0:
          time.sleep(remaining)
        before = time.perf_counter()
        arrivals.append(before - started)
        data = memoryview(marked)
        while data:
          size = proc.stdin.write(data)
          if not size:
            raise BrokenPipeError('ベンチマークの入力が閉じられました')
          data = data[size:]
        completed = time.perf_counter()
        completions.append(completed - started)
        writes.append(completed - before)
    except Exception as error:
      errors.append(repr(error))
    finally:
      proc.stdin.close()

  stderr = []
  sender = None
  threads = [threading.Thread(target=write), threading.Thread(target=lambda: stderr.append(proc.stderr.read()))]
  if udp_local_address is None:
    threads.insert(0, threading.Thread(target=receive))
  try:
    if udp_local_address is not None:
      sender = stream.MpegTsUdpSender(proc.stdout, udp_local_address, config, stdout_observer=observe)
      sender.start()
    for thread in threads:
      thread.start()
    proc.wait(timeout=max(schedule, default=0) + 15)
    if sender is not None and (not sender.done.wait(2) or sender.error is not None):
      errors.append(f'sender: {sender.error!r}')
  finally:
    if proc.poll() is None:
      proc.kill()
      proc.wait(timeout=2)
    for thread in threads:
      if thread.ident is not None:
        thread.join(timeout=2)
    if sender is not None:
      sender.close()
    proc.stdin.close()
    proc.stdout.close()
    proc.stderr.close()
  diagnostic = b''.join(stderr).decode(errors='replace')
  if proc.returncode or errors or any(thread.is_alive() for thread in threads):
    raise RuntimeError(f'FFmpeg異常終了 rc={proc.returncode} errors={errors} stderr={diagnostic}')
  ts = b''.join(chunks)
  elapsed = time.perf_counter() - started
  metrics = {'bitrate': config.bitrate, 'frames': len(writes), 'feed_times': arrivals,
             'transport': 'pipe-only' if sender is None else sender.mode,
             'stdin_write_avg_ms': sum(writes) * 1000 / len(writes), 'stdin_write_max_ms': max(writes) * 1000,
             'first_stdout_ms': first_output[0] * 1000 if first_output else None,
             'throughput_fps': len(writes) / elapsed, 'output_bytes': len(ts), 'stdout_bytes_per_sec': len(ts) / elapsed,
             'stdout_chunks': len(chunk_ends), 'stderr': diagnostic,
             'datagrams_by_payload': {str(size): math.ceil(len(ts) / size) for size in [188, 376, 564, 1316]}}
  if sender is not None:
    metrics['transport_stats'] = sender.stats_snapshot(time.monotonic())
  if frame_diagnostics:
    events = video_pes_events(ts, chunk_ends, chunk_times)
    ids, pts = decode_markers(ts)
    frames = match_frame_timings(arrivals, completions, events, ids, pts)
    metrics['frame_timings'] = frames
    for key in ['stdin_to_stdout_ms', 'arrival_to_stdout_ms']:
      values = sorted(record[key] for record in frames)
      metrics[key + '_avg'] = sum(values) / len(values)
      metrics[key + '_p95'] = values[math.ceil(len(values) * .95) - 1]
      metrics[key + '_max'] = max(values)
    metrics['negative_latency_samples'] = sum(record['stdin_to_stdout_ms'] < 0 for record in frames)
  return ts, metrics


def decode_pts(ts):
  decoded = subprocess.run([ffmpeg_path(), '-i', 'pipe:0', '-vf', 'showinfo', '-f', 'null', '-'],
                           input=ts, capture_output=True, timeout=20)
  if decoded.returncode:
    raise RuntimeError(decoded.stderr.decode(errors='replace'))
  pts = [float(value) for value in re.findall(rb'pts_time:([\d.\-]+)', decoded.stderr)]
  return pts, decoded.stderr.decode(errors='replace')


def decode_markers(ts):
  # 本番のvflipで下端へ移った識別子を復号後に読み、PTSと同じ順序で照合する。
  filters = f'showinfo,crop={stream.WIDTH}:{MARKER_HEIGHT}:0:{stream.HEIGHT - MARKER_HEIGHT},scale={MARKER_BITS}:1:flags=area,format=gray'
  result = subprocess.run([ffmpeg_path(), '-i', 'pipe:0', '-vf', filters, '-fps_mode', 'passthrough',
                           '-f', 'rawvideo', '-pix_fmt', 'gray', 'pipe:1'], input=ts, capture_output=True, timeout=20)
  if result.returncode or len(result.stdout) % MARKER_BITS:
    raise RuntimeError(result.stderr.decode(errors='replace'))
  ids = [sum((value > 128) << bit for bit, value in enumerate(result.stdout[offset:offset + MARKER_BITS]))
         for offset in range(0, len(result.stdout), MARKER_BITS)]
  pts = [float(value) for value in re.findall(rb'pts_time:([\d.\-]+)', result.stderr)]
  return ids, pts


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument('--frames', type=int, default=100)
  parser.add_argument('--output', type=Path, help='全frameの対応時刻を含むJSON保存先')
  parser.add_argument('--udp-address', help='指定時だけ本番のPython UDP senderで合成映像を送信する宛先IPv4')
  parser.add_argument('--local-address', help='UDP試験の送信元Wi-Fi IPv4')
  parser.add_argument('--port', type=int, default=12346)
  args = parser.parse_args()
  if not 2 <= args.frames <= 65536:
    parser.error('--framesは2～65536を指定してください')
  if bool(args.udp_address) != bool(args.local_address):
    parser.error('--udp-addressと--local-addressは両方指定してください')
  # 静止黒画面より負荷のある合成模様。同じ画像を全ビットレートで使用する。
  frame = bytes((i * 37 + i // 3200) % 256 for i in range(stream.FRAME_BYTES))
  results = []
  for bitrate in [500, 1000, 1500, 3000]:
    config = ScreenStreamConfig(address=args.udp_address or stream.DEFAULT_CONFIG.address, port=args.port, bitrate=bitrate)
    ts, result = feed_frames(config, [i / stream.FPS for i in range(args.frames)], frame=frame, frame_diagnostics=True,
                             udp_local_address=args.local_address)
    pts, _ = decode_pts(ts)
    result['pts_span_s'] = pts[-1] - pts[0]
    result['feed_span_s'] = result['feed_times'][-1] - result['feed_times'][0]
    results.append(result)
    print(json.dumps({key: value for key, value in result.items() if key not in ('feed_times', 'frame_timings')}, ensure_ascii=False))
  if args.output:
    args.output.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
  main()
