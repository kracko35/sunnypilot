"""任意実行: 実際のGPU読み出しとFFmpegで上下方向・解像度・MPEG-TSを検証する。"""

import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from openpilot.system.ui.lib.tests.screen_stream_test_support import load_screen_stream
from openpilot.system.ui.lib.screen_stream_config import ScreenStreamConfig
from openpilot.system.ui.lib.tests.benchmark_screen_stream import feed_frames


stream = load_screen_stream()


@unittest.skipUnless(os.getenv('SCREEN_STREAM_TEST_GPU') == '1', 'GPU統合テストは明示的に有効化してください')
class TestScreenStreamVideo(unittest.TestCase):
  def test_diagnostic_benchmark_through_production_udp_sender(self):
    packets = []
    stopped = threading.Event()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as receiver:
      receiver.bind(('127.0.0.1', 0))
      receiver.settimeout(.1)
      receiver.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024 * 1024)

      def receive():
        while True:
          try:
            packets.append(receiver.recv(65536))
          except TimeoutError:
            if stopped.is_set():
              return

      reader = threading.Thread(target=receive)
      reader.start()
      try:
        # 本番設定のloopback拒否は維持し、実socketの試験用だけ置換する。
        config = SimpleNamespace(**(vars(ScreenStreamConfig()) | {'address': '127.0.0.1', 'port': receiver.getsockname()[1]}))
        ts, result = feed_frames(config, [0, .05, .10, .30, .35], frame_diagnostics=True, udp_local_address='127.0.0.1')
      finally:
        stopped.set()
        reader.join(timeout=2)
      self.assertFalse(reader.is_alive())
      self.assertEqual(b''.join(packets), ts)
      self.assertEqual(len(result['frame_timings']), 5)
      self.assertEqual(result['transport_stats']['sender_drops'], 0)
      self.assertEqual(result['transport_stats']['sender_datagrams'], len(packets))
      self.assertTrue(all(len(packet) == 564 for packet in packets[:-1]))

  def test_gpu_capture_and_h264_transport_stream(self):
    self._check_stream(ScreenStreamConfig())

  def test_custom_destination_and_bitrate(self):
    self._check_stream(ScreenStreamConfig('239.255.42.100', 12347, 2600, 2))

  def test_unicast_loopback_at_500_kbit(self):
    self._check_stream(ScreenStreamConfig('192.168.4.44', 12348, 500, 1), loopback_unicast=True)

  def _check_stream(self, config, loopback_unicast=False):
    import pyray as rl
    from openpilot.system.ui.lib.screen_capture import ScreenStreamCapture

    ffmpeg = os.getenv('SCREEN_STREAM_TEST_FFMPEG') or shutil.which('ffmpeg')
    self.assertIsNotNone(ffmpeg, 'FFmpegが必要です')
    rl.set_config_flags(rl.ConfigFlags.FLAG_WINDOW_HIDDEN)
    rl.init_window(100, 50, '画面配信の統合テスト')
    if not rl.is_window_ready():
      self.skipTest('画面／OpenGLを初期化できない実行環境です')
    self.addCleanup(rl.close_window)
    source = rl.load_render_texture(100, 50)
    self.addCleanup(rl.unload_render_texture, source)
    rl.set_texture_filter(source.texture, rl.TextureFilter.TEXTURE_FILTER_BILINEAR)
    rl.begin_texture_mode(source)
    rl.clear_background(rl.BLACK)
    rl.draw_rectangle(0, 0, 100, 25, rl.RED)
    rl.draw_rectangle(0, 25, 100, 25, rl.BLUE)
    rl.end_texture_mode()
    streamer = Mock()
    streamer.ready.is_set.return_value = True
    capture = ScreenStreamCapture(streamer)
    self.addCleanup(capture.release)
    capture.capture(source.texture)
    data = streamer.submit.call_args.args[0]
    self.assertEqual(len(data), stream.FRAME_BYTES)

    with tempfile.TemporaryDirectory() as directory:
      output = str(Path(directory) / 'screen.ts')
      command = stream.ffmpeg_command(config)
      command[0] = ffmpeg
      # 開発PCにUDP対応FFmpegがあっても、エンコードにはfile/pipe以外を許可しない。
      command[1:1] = ['-protocol_whitelist', 'file,pipe']
      self.assertEqual(command[-1], 'pipe:1')
      self.assertNotIn('udp://', ' '.join(command))
      ts, _ = feed_frames(config, [i / stream.FPS for i in range(20)], frame=data)
      Path(output).write_bytes(ts)
      self.assertEqual(len(ts) % 188, 0)
      self.assertTrue(all(ts[offset] == 0x47 for offset in range(0, len(ts), 188)))
      decoded = subprocess.run([ffmpeg, '-i', output, '-frames:v', '1', '-f', 'rawvideo', '-pix_fmt', 'rgb24', 'pipe:1'],
                               capture_output=True, timeout=20)
      self.assertEqual(decoded.returncode, 0, decoded.stderr.decode(errors='replace'))
      self.assertEqual(len(decoded.stdout), stream.WIDTH * stream.HEIGHT * 3)
      self.assertIn(b'Constrained Baseline', decoded.stderr)
      self.assertIn(b'20 fps', decoded.stderr)
      self.assertIn(b'yuv420p', decoded.stderr)
      self.assertEqual(command[command.index('-bf') + 1], '0')

      def pixel(x, y):
        offset = (y * stream.WIDTH + x) * 3
        return decoded.stdout[offset:offset + 3]

      self.assertLess(max(pixel(400, 10)), 10)
      self.assertGreater(pixel(400, 100)[0], 200)
      self.assertLess(pixel(400, 100)[2], 80)
      self.assertGreater(pixel(400, 350)[2], 200)
      self.assertLess(pixel(400, 350)[0], 80)
      self.assertLess(max(pixel(400, 470)), 10)

      # 本番のPython送信クラスを使い、FFmpegのstdoutからループバックへ送信する。
      packets = []
      stopped = threading.Event()
      # ループバック宛先は送信クラスの統合テスト専用。本番の設定検証では引き続き拒否する。
      transport_config = SimpleNamespace(**(vars(config) | {'address': '127.0.0.1'})) if loopback_unicast else config
      with socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP) as receiver:
        receiver.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        receiver.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024 * 1024)
        receiver.bind(('127.0.0.1' if loopback_unicast else '', config.port))
        if not loopback_unicast:
          receiver.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                              socket.inet_aton(config.address) + socket.inet_aton('127.0.0.1'))
        receiver.settimeout(0.1)

        def receive():
          while True:
            try:
              packets.append(receiver.recv(65536))
            except TimeoutError:
              if stopped.is_set():
                break

        reader = threading.Thread(target=receive)
        reader.start()
        try:
          with tempfile.TemporaryFile() as stderr:
            proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr, bufsize=0)
            sender = stream.MpegTsUdpSender(proc.stdout, '127.0.0.1', transport_config)
            self.assertEqual(sender.mode, 'unicast' if loopback_unicast else 'multicast')
            writer_errors = []

            def write_frames():
              try:
                started = time.monotonic()
                for index in range(20):
                  while (delay := started + index / stream.FPS - time.monotonic()) > 0:
                    time.sleep(delay)
                  remaining = memoryview(data)
                  while remaining:
                    written = proc.stdin.write(remaining)
                    if not written:
                      raise BrokenPipeError('統合テストの入力パイプが閉じられました')
                    remaining = remaining[written:]
              except Exception as error:
                writer_errors.append(error)
              finally:
                proc.stdin.close()

            writer = threading.Thread(target=write_frames)
            try:
              sender.start()
              writer.start()
              proc.wait(timeout=20)
              writer.join(timeout=1)
              self.assertFalse(writer.is_alive())
              self.assertEqual(writer_errors, [])
              self.assertTrue(sender.done.wait(2))
              self.assertIsNone(sender.error)
              stderr.seek(0)
              self.assertEqual(proc.returncode, 0, stderr.read().decode(errors='replace'))
            finally:
              sender.stop()
              if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=2)
              if writer.ident is not None:
                writer.join(timeout=2)
              proc.stdin.close()
              proc.stdout.close()
              sender.close()
            self.assertFalse(sender._thread.is_alive())
        finally:
          stopped.set()
          reader.join(timeout=2)
        self.assertFalse(reader.is_alive())
      self.assertTrue(packets, 'UDPを受信できませんでした')
      self.assertTrue(all(len(packet) <= 1316 and len(packet) % 188 == 0 for packet in packets))
      self.assertTrue(all(len(packet) == sender.payload_size for packet in packets[:-1]))
      self.assertEqual(sender.datagrams_dropped, 0)
      self.assertEqual(sender.datagrams_sent, len(packets))
      self.assertEqual(sender.bytes_sent, sum(map(len, packets)))
      self.assertTrue(all(packet[offset] == 0x47 for packet in packets for offset in range(0, len(packet), 188)))
      inspected = subprocess.run([ffmpeg, '-protocol_whitelist', 'file,pipe', '-f', 'mpegts', '-i', 'pipe:0',
                                  '-vf', 'showinfo', '-f', 'null', '-'], input=b''.join(packets), capture_output=True, timeout=20)
      self.assertEqual(inspected.returncode, 0, inspected.stderr.decode(errors='replace'))
      frame_info = [line for line in inspected.stderr.splitlines() if b'Parsed_showinfo' in line and b' type:' in line]
      self.assertEqual(len(frame_info), 20)
      self.assertFalse(any(b'type:B' in line for line in frame_info))
      received = subprocess.run([ffmpeg, '-f', 'mpegts', '-i', 'pipe:0', '-frames:v', '1', '-f', 'rawvideo', '-pix_fmt', 'rgb24', 'pipe:1'],
                                input=b''.join(packets), capture_output=True, timeout=20)
      self.assertEqual(received.returncode, 0, received.stderr.decode(errors='replace'))
      self.assertEqual(len(received.stdout), len(decoded.stdout))
      for y in [10, 100, 350, 470]:
        offset = (y * stream.WIDTH + 400) * 3
        for actual, expected in zip(received.stdout[offset:offset + 3], pixel(400, y), strict=True):
          self.assertLessEqual(abs(actual - expected), 10)


if __name__ == '__main__':
  unittest.main()
