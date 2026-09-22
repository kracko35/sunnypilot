"""任意実行: 不規則なstdin入力でPTSが実時間の空白を保つことを検証する。"""

import os
import unittest

from openpilot.system.ui.lib.tests.benchmark_screen_stream import decode_pts, feed_frames, stream
from openpilot.system.ui.lib.screen_stream_config import ScreenStreamConfig


@unittest.skipUnless(os.getenv('SCREEN_STREAM_TEST_FFMPEG'), 'FFmpeg統合テストは実行ファイルの指定が必要です')
class TestScreenStreamTimestamps(unittest.TestCase):
  def test_irregular_feed_preserves_wallclock_gap_at_all_bitrates(self):
    for bitrate in [500, 1500, 3000]:
      with self.subTest(bitrate=bitrate):
        config = ScreenStreamConfig(bitrate=bitrate)
        ts, result = feed_frames(config, [0, .05, .10, .30, .35])
        pts, _ = decode_pts(ts)
        self.assertEqual(len(pts), 5)
        relative = [value - pts[0] for value in pts]
        feed = [value - result['feed_times'][0] for value in result['feed_times']]
        self.assertGreater(relative[3] - relative[2], .10)
        for actual, expected in zip(relative, feed, strict=True):
          self.assertAlmostEqual(actual, expected, delta=.075)
        self.assertNotIn('Error', result['stderr'])
        self.assertNotIn('VBV buffer size cannot be smaller', result['stderr'])
        print(f'bitrate={bitrate} feed={feed} pts={relative}', flush=True)

  def test_frame_count_timestamps_compress_the_gap(self):
    config = ScreenStreamConfig()
    command = stream.ffmpeg_command(config)
    index = command.index('-use_wallclock_as_timestamps')
    del command[index:index + 2]
    ts, _ = feed_frames(config, [0, .05, .10, .30, .35], command=command)
    pts, _ = decode_pts(ts)
    self.assertEqual(len(pts), 5)
    for index, value in enumerate(pts):
      self.assertAlmostEqual(value - pts[0], index / 20, delta=.002)


if __name__ == '__main__':
  unittest.main()
