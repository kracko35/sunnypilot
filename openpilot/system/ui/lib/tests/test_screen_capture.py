"""縮小、フレームレート制限、録画用読み出しの解放を検証する。"""

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from openpilot.system.ui.lib.tests.screen_stream_test_support import load_screen_stream

load_screen_stream()
from openpilot.system.ui.lib import screen_capture as capture


class TestScreenCapture(unittest.TestCase):
  def setUp(self):
    self.streamer = Mock()
    self.streamer.ready.is_set.return_value = True
    self.capture = capture.ScreenStreamCapture(self.streamer)
    self.source = SimpleNamespace(width=2160, height=1080)

  def test_no_readback_when_not_ready(self):
    self.streamer.ready.is_set.return_value = False
    with patch.object(capture.rl, 'load_render_texture') as load:
      self.capture.capture(self.source)
      load.assert_not_called()

  def test_rate_limit_resize_letterbox_and_release(self):
    with patch.object(capture, 'rl') as raylib, patch.object(capture, 'read_rgba', return_value=b'frame'), \
         patch.object(capture.time, 'monotonic') as clock:
      for frame in range(60):
        clock.return_value = 1 + frame / 60 + 0.0001
        self.capture.capture(self.source)
      self.assertEqual(self.streamer.submit.call_count, 20)
      raylib.load_render_texture.assert_called_once_with(800, 480)
      self.assertIn((0, 40, 800, 400), [call.args for call in raylib.Rectangle.call_args_list])
      self.assertIn((0, 0, 2160, -1080), [call.args for call in raylib.Rectangle.call_args_list])
      self.capture.release()
      self.capture.release()
      raylib.unload_render_texture.assert_called_once()

  def test_slow_ui_does_not_replay_missed_frames(self):
    with patch.object(capture, 'rl'), patch.object(capture, 'read_rgba'), patch.object(capture.time, 'monotonic') as clock:
      for now in [1, 3, 3.01, 3.02]:
        clock.return_value = now
        self.capture.capture(self.source)
      self.assertEqual(self.streamer.submit.call_count, 2)

  def test_readback_always_frees_image(self):
    with patch.object(capture, 'rl') as raylib:
      image = SimpleNamespace(width=800, height=480, data=object())
      raylib.load_image_from_texture.return_value = image
      raylib.ffi.buffer.side_effect = ValueError("読み出し失敗")
      self.assertRaises(ValueError, capture.read_rgba, self.source)
      raylib.unload_image.assert_called_once_with(image)

  def test_capture_timestamp_precedes_gpu_work(self):
    with patch.object(capture, 'rl'), patch.object(capture, 'read_rgba', return_value=b'frame'), \
         patch.object(capture.time, 'monotonic', side_effect=[10.0, 10.01, 10.09]):
      self.capture.capture(self.source)
    self.streamer.submit.assert_called_once_with(b'frame', captured=10.0)
    timing = self.streamer.stats.record.call_args
    self.assertAlmostEqual(timing.kwargs['gpu_scale'], .01)
    self.assertAlmostEqual(timing.kwargs['capture'], .09)

  def test_readback_and_copy_have_separate_timings(self):
    stats = Mock()
    with patch.object(capture, 'rl') as raylib, patch.object(capture.time, 'monotonic', side_effect=[1.0, 1.02, 1.025]):
      raylib.ffi.buffer.return_value = b'rgba'
      self.assertEqual(capture.read_rgba(self.source, stats), b'rgba')
      raylib.unload_image.assert_called_once()
    self.assertAlmostEqual(stats.record.call_args.kwargs['readback'], .02)
    self.assertAlmostEqual(stats.record.call_args.kwargs['bytes_copy'], .005)


if __name__ == '__main__':
  unittest.main()
