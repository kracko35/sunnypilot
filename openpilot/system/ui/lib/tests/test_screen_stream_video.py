"""任意実行: 実際のGPU読み出しとFFmpegで上下方向・解像度・MPEG-TSを検証する。"""

import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import Mock

from openpilot.system.ui.lib.screen_stream import WIDTH, HEIGHT, FRAME_BYTES, ffmpeg_command
from openpilot.system.ui.lib.screen_stream_config import ScreenStreamConfig


@unittest.skipUnless(os.getenv('SCREEN_STREAM_TEST_GPU') == '1', 'GPU統合テストは明示的に有効化してください')
class TestScreenStreamVideo(unittest.TestCase):
  def test_gpu_capture_and_h264_transport_stream(self):
    self._check_stream(ScreenStreamConfig())

  def test_custom_destination_and_bitrate(self):
    self._check_stream(ScreenStreamConfig('239.255.42.100', 12347, 2600, 2))

  def _check_stream(self, config):
    import pyray as rl
    from openpilot.system.ui.lib.screen_capture import ScreenStreamCapture

    ffmpeg = os.getenv('SCREEN_STREAM_TEST_FFMPEG') or shutil.which('ffmpeg')
    self.assertIsNotNone(ffmpeg, 'FFmpegが必要です')
    rl.set_config_flags(rl.ConfigFlags.FLAG_WINDOW_HIDDEN)
    rl.init_window(100, 50, '画面配信の統合テスト')
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
    self.assertEqual(len(data), FRAME_BYTES)

    with tempfile.TemporaryDirectory() as directory:
      output = str(Path(directory) / 'screen.ts')
      command = ffmpeg_command('127.0.0.1', config)
      command[0], command[-1] = ffmpeg, output
      encoded = subprocess.run(command, input=data * 20, capture_output=True, timeout=20)
      self.assertEqual(encoded.returncode, 0, encoded.stderr.decode(errors='replace'))
      ts = Path(output).read_bytes()
      self.assertTrue(all(ts[offset] == 0x47 for offset in range(0, len(ts), 188)))
      decoded = subprocess.run([ffmpeg, '-i', output, '-frames:v', '1', '-f', 'rawvideo', '-pix_fmt', 'rgb24', 'pipe:1'],
                               capture_output=True, timeout=20)
      self.assertEqual(decoded.returncode, 0, decoded.stderr.decode(errors='replace'))
      self.assertEqual(len(decoded.stdout), WIDTH * HEIGHT * 3)
      self.assertIn(b'Constrained Baseline', decoded.stderr)
      self.assertIn(b'20 fps', decoded.stderr)

      def pixel(x, y):
        offset = (y * WIDTH + x) * 3
        return decoded.stdout[offset:offset + 3]

      self.assertLess(max(pixel(400, 10)), 10)
      self.assertGreater(pixel(400, 100)[0], 200)
      self.assertLess(pixel(400, 100)[2], 80)
      self.assertGreater(pixel(400, 350)[2], 200)
      self.assertLess(pixel(400, 350)[0], 80)
      self.assertLess(max(pixel(400, 470)), 10)

      # 本番と同じUDP URLを使い、ループバック上のマルチキャストだけで送受信する。
      packets = []
      stopped = threading.Event()
      with socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP) as receiver:
        receiver.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        receiver.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024 * 1024)
        receiver.bind(('', config.port))
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
          command = ffmpeg_command('127.0.0.1', config)
          command[0] = ffmpeg
          sent = subprocess.run(command, input=data * 20, capture_output=True, timeout=20)
          self.assertEqual(sent.returncode, 0, sent.stderr.decode(errors='replace'))
        finally:
          stopped.set()
          reader.join(timeout=2)
        self.assertFalse(reader.is_alive())
      self.assertTrue(packets, 'UDPマルチキャストを受信できませんでした')
      self.assertTrue(all(len(packet) <= 1316 and len(packet) % 188 == 0 for packet in packets))
      received = subprocess.run([ffmpeg, '-f', 'mpegts', '-i', 'pipe:0', '-frames:v', '1', '-f', 'rawvideo', '-pix_fmt', 'rgb24', 'pipe:1'],
                                input=b''.join(packets), capture_output=True, timeout=20)
      self.assertEqual(received.returncode, 0, received.stderr.decode(errors='replace'))
      self.assertEqual(len(received.stdout), len(decoded.stdout))
      for y in [10, 100, 350, 470]:
        offset = (y * WIDTH + 400) * 3
        for actual, expected in zip(received.stdout[offset:offset + 3], pixel(400, y), strict=True):
          self.assertLessEqual(abs(actual - expected), 10)


if __name__ == '__main__':
  unittest.main()
