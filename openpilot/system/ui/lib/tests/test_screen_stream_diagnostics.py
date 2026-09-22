"""診断のPES時刻対応が欠落・順序変更・分割読み取りで誤った遅延を報告しないことを検証する。"""

import unittest

from openpilot.system.ui.lib.tests.screen_stream_diagnostics import match_frame_timings, video_pes_events


def packet(pts=b'\x21\x00\x01\x00\x01'):
  header = b'\x47\x41\x00\x10\x00\x00\x01\xe0\x00\x00\x80\x80\x05' + pts
  return header + bytes(188 - len(header))


class TestFrameDiagnostics(unittest.TestCase):
  def test_split_pes_header_uses_first_byte_observation_time(self):
    events = video_pes_events(packet(), [2, 7, 188], [.1, .2, .3])
    self.assertEqual(events, [{'pts_90k': 0, 'stdout_first_observed_s': .2}])

  def test_known_pts_and_adaptation_field(self):
    original = packet(b'\x21\x00\x05\xbf\x21')
    adapted = original[:3] + b'\x30\x01\x00' + original[4:-2]
    events = video_pes_events(adapted, [188], [.1])
    self.assertEqual(events[0]['pts_90k'], 90000)

  def test_invalid_ts_or_pts_does_not_produce_measurements(self):
    for ts in [packet()[:-1], b'X' + packet()[1:], packet(b'\x20\x00\x01\x00\x01')]:
      with self.subTest(ts=ts[:20]):
        self.assertRaises(ValueError, video_pes_events, ts, [len(ts)], [.1])

  def test_mapping_checks_ids_counts_and_pts(self):
    events = video_pes_events(packet() + packet(b'\x21\x00\x05\xbf\x21'), [188, 376], [.01, 1.01])
    for ids, pts in [([0], [0]), ([1, 0], [0, 1]), ([0, 0], [0, 1]), ([0, 1], [0, .5])]:
      with self.subTest(ids=ids, pts=pts):
        self.assertRaises(ValueError, match_frame_timings, [0, 1], [.011, 1.002], events, ids, pts)
    frames = match_frame_timings([0, 1], [.011, 1.002], events, [0, 1], [0, 1])
    self.assertAlmostEqual(frames[0]['stdin_to_stdout_ms'], -1)
    self.assertAlmostEqual(frames[1]['stdin_to_stdout_ms'], 8)


if __name__ == '__main__':
  unittest.main()
