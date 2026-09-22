"""任意実行: 不規則なstdin入力でPTSが実時間の空白を保つことを検証する。"""

import os
import unittest

from openpilot.system.ui.lib.tests.benchmark_screen_stream import decode_pts, feed_frames, stream
from openpilot.system.ui.lib.screen_stream_config import ScreenStreamConfig


@unittest.skipUnless(os.getenv('SCREEN_STREAM_TEST_FFMPEG'), 'FFmpeg統合テストは実行ファイルの指定が必要です')
class TestScreenStreamTimestamps(unittest.TestCase):
  def test_per_frame_pes_observation_matches_decoded_marker_and_input(self):
    for bitrate in [500, 1000, 1500, 3000]:
      with self.subTest(bitrate=bitrate):
        _, result = feed_frames(ScreenStreamConfig(bitrate=bitrate), [0, .05, .10, .30, .35],
                                frame_diagnostics=True, production_observer=True)
        records = result['frame_timings']
        self.assertEqual([record['frame'] for record in records], list(range(5)))
        self.assertGreater(records[3]['pts_90k'] - records[2]['pts_90k'], 9000)
        for record in records:
          self.assertGreaterEqual(record['stdin_complete_s'], record['input_arrival_s'])
          self.assertGreaterEqual(record['stdout_first_observed_s'], record['input_arrival_s'])
          self.assertAlmostEqual(record['stdin_to_stdout_ms'],
                                 (record['stdout_first_observed_s'] - record['stdin_complete_s']) * 1000)
        self.assertEqual(result['transport'], 'pipe-only')
        self.assertEqual(result['production_latency']['encoder_pes_samples'], 5)
        self.assertEqual(result['production_latency']['diag_sync_lost'], 0)
        self.assertLess(result['observer_exact_avg_error_ms'], .001)
        self.assertLess(result['observer_exact_p95_error_ms'], .001)
        self.assertLess(result['observer_exact_max_error_ms'], .001)

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
