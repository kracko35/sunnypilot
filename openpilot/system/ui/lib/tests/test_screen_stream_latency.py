"""本番観測の限定条件・同期喪失・保持量とLinux計測の失敗時動作を検証する。"""

import unittest
from unittest.mock import patch

from openpilot.system.ui.lib import screen_stream_latency as latency


def packet(pts=0, cc=0):
  p = bytes([0x21 | ((pts >> 29) & 14), (pts >> 22) & 255, ((pts >> 14) & 254) | 1,
             (pts >> 7) & 255, ((pts << 1) & 254) | 1])
  data = bytes([0x47, 0x41, 0, 0x10 | cc]) + b'\0\0\1\xe0\0\0\x80\x80\x05' + p
  return data + bytes(188 - len(data))


class TestTsPesParser(unittest.TestCase):
  def test_chunk_boundaries_and_payload_sizes(self):
    data = b''.join(packet(i * 4500, i % 16) for i in range(20))
    for size in (1, 17, 188, 564, 1316, 65536):
      with self.subTest(size=size):
        parser, events = latency.TsPesParser(), []
        for start in range(0, len(data), size):
          events.extend(parser.feed(data[start:start + size], start / 10000))
          self.assertLess(len(parser.tail), 188)
          self.assertEqual(len(parser.tail), len(parser.tail_times))
        self.assertEqual([event.pts for event in events], [i * 4500 for i in range(20)])
        self.assertEqual([event.packet_offset for event in events], [i * 188 for i in range(20)])
        for event in events:
          self.assertEqual(event.observed_at, ((event.packet_offset + 4) // size * size) / 10000)

  def test_adaptation_and_pts_wrap(self):
    raw = packet((1 << 33) - 1)
    data = raw[:3] + b'\x30\x01\x00' + raw[4:-2]
    event = latency.TsPesParser().feed(data, 1)[0]
    self.assertEqual(event.pts, (1 << 33) - 1)

  def test_ts_loss_and_duplicate_are_rejected(self):
    for cc in (0, 2):
      parser = latency.TsPesParser()
      parser.feed(packet(), 1)
      self.assertRaisesRegex(ValueError, 'ts_continuity', parser.feed, packet(4500, cc), 1.05)

  def test_invalid_headers_and_discontinuity_are_rejected(self):
    for index, value in ((0, 0), (1, 0xC1), (3, 0xD0), (13, 0x20)):
      data = bytearray(packet())
      data[index] = value
      self.assertRaises(ValueError, latency.TsPesParser().feed, bytes(data), 0)
    parser = latency.TsPesParser()
    parser.feed(packet(), 0)
    original = packet(4500, 1)
    self.assertRaisesRegex(ValueError, 'ts_discontinuity', parser.feed,
                           original[:3] + b'\x31\x01\x80' + original[4:-2], .05)


class TestFrameTimingObserver(unittest.TestCase):
  def setUp(self):
    self.observer = latency.FrameTimingObserver()

  def frame(self, index, now, *, pts=None, sent=True):
    self.observer.begin(index, now - .004, now - .001, now)
    self.observer.complete(index, now + .005)
    self.observer.observe(packet(index * 4500 if pts is None else pts, index % 16), now + .010)
    self.observer.datagram(index * 188, (index + 1) * 188, now + .012, sent)

  def test_order_pairing_window_summary_and_reset(self):
    for index in range(20):
      self.frame(index, 1 + index * .05)
    result = self.observer.snapshot(2, 1)
    self.assertEqual(result['encoder_pes_samples'], 20)
    self.assertEqual(result['frames_paired_per_sec'], 20)
    self.assertEqual(result['capture_to_pes_avg_ms'], 14)
    self.assertEqual(result['stdin_start_to_pes_p95_ms'], 10)
    self.assertEqual(result['stdin_complete_to_pes_max_ms'], 5)
    self.assertEqual(result['pes_to_send_avg_ms'], 2)
    self.assertEqual(result['capture_to_udp_send_avg_ms'], 16)
    self.assertEqual(result['diag_pending'], 0)
    result = self.observer.snapshot(2.1, .1)
    self.assertEqual(result['encoder_pes_samples'], 0)
    self.assertIsNone(result['capture_to_pes_avg_ms'])
    self.assertIsNone(result['capture_to_pes_p95_ms'])

  def test_stdout_before_stdin_completion_preserves_negative_value(self):
    self.observer.begin(0, 0, .001, .002)
    self.observer.observe(packet(), .010)
    self.observer.datagram(0, 188, .011, True)
    self.assertEqual(self.observer.snapshot(.012, .012)['encoder_pes_samples'], 0)
    self.observer.complete(0, .015)
    result = self.observer.snapshot(.02, .008)
    self.assertEqual(result['encoder_pes_samples'], 1)
    self.assertEqual(result['stdin_complete_to_pes_avg_ms'], -5)
    self.assertEqual(result['stdin_start_to_pes_avg_ms'], 8)

  def test_initial_constant_shift_is_not_proven_by_order_and_gap_checks(self):
    self.observer.begin(0, 0, 0, 0)
    self.observer.complete(0, .004)
    self.observer.begin(1, .05, .05, .05)
    self.observer.complete(1, .054)
    # frame 0のPESを欠落させる。初回PTSだけではframe 1との違いを判定できない。
    self.observer.observe(packet(4500), .06)
    self.observer.datagram(0, 188, .061, True)
    result = self.observer.snapshot(.1, .1)
    # 初回からの一定shiftは検出不能。この順序前提の限界を成功条件として隠さない。
    self.assertEqual(result['encoder_pes_samples'], 1)
    self.assertEqual(result['diag_pending'], 1)
    self.assertEqual(result['capture_to_pes_avg_ms'], 60)
    result = self.observer.snapshot(2.1, 2)
    self.assertEqual(result['diag_sync_lost'], 1)
    self.assertEqual(result['diag_reason'], 'pending_timeout')
    self.assertIsNone(result['stdin_start_to_pes_avg_ms'])

  def pipeline(self, delay, *, frames=240, fault=None, wrap=False, close_with_tail=False):
    events = []
    for i in range(frames):
      at = i * .05
      events.extend([(at, 0, i), (at + .004, 1, i), (at + delay, 2, i), (at + delay + .002, 3, i)])
    events.extend((at, 4, 0) for at in (5, 10))
    windows, peak, last_window = [], 0, 0
    stop_at = (frames - 1) * .05 + .004 if close_with_tail else float('inf')
    for at, kind, i in sorted(events):
      if at > stop_at:
        break
      if kind == 0:
        self.observer.begin(i, at, at, at)
      elif kind == 1:
        self.observer.complete(i, at)
      elif kind == 2:
        pts = ((1 << 33) - 45000 if wrap else 0) + i * 4500
        data = packet(pts % (1 << 33), i % 16)
        if fault and i == 120:
          if fault == 'continuity':
            data = packet(pts, (i + 2) % 16)
          elif fault in ('duplicate', 'backward'):
            data = packet(pts - (4500 if fault == 'duplicate' else 9000), i % 16)
          elif fault == 'unexpected':
            data = b''.join(packet(pts + j * 4500, (i + j) % 16) for j in range(33))
        self.observer.observe(data, at)
        if not self.observer.active:
          return windows, self.observer.snapshot(at, at - last_window), peak
      elif kind == 3:
        self.observer.datagram(i * 188, (i + 1) * 188, at, True, attempted_at=at - .001)
      else:
        windows.append(self.observer.snapshot(at, at - last_window))
        last_window = at
      peak = max(peak, len(self.observer._records))
      self.assertTrue(self.observer.active)
      self.assertLess(len(self.observer._records), latency.MAX_PENDING)
    end = stop_at if close_with_tail else (frames - 1) * .05 + delay + .003
    return windows, self.observer.snapshot(end, end - last_window, final=True), peak

  def check_pipeline(self, delay):
    windows, final, peak = self.pipeline(delay)
    self.assertEqual(sum(w['encoder_pes_samples'] for w in [*windows, final]), 240)
    self.assertGreater(windows[0]['encoder_pes_samples'], 80)
    self.assertEqual(windows[1]['encoder_pes_samples'], 100)
    self.assertEqual(windows[1]['frames_paired_per_sec'], 20)
    self.assertLessEqual(peak, int(delay / .05 + 1.01))
    self.assertEqual(final['diag_pending'], 0)
    for w in [*windows, final]:
      self.assertEqual(w['diag_active'], 1)
      self.assertEqual(w['diag_sync_lost'], 0)
      self.assertLess(w['diag_pending_max'], latency.MAX_PENDING)
      for stage, expected in (('capture_to_pes', delay * 1000), ('stdin_start_to_pes', delay * 1000),
                              ('stdin_complete_to_pes', delay * 1000 - 4), ('pes_to_send_attempt', 1),
                              ('pes_to_send', 2), ('capture_to_udp_send', delay * 1000 + 2)):
        for stat in ('avg', 'p95', 'max'):
          self.assertAlmostEqual(w[f'{stage}_{stat}_ms'], expected, places=3)

  def test_steady_pipeline_20ms_240_frames(self):
    self.check_pipeline(.020)

  def test_steady_pipeline_80ms_240_frames(self):
    self.check_pipeline(.080)

  def test_steady_pipeline_100ms_240_frames(self):
    self.check_pipeline(.100)

  def test_steady_pipeline_300ms_240_frames(self):
    self.check_pipeline(.300)

  def test_steady_pipeline_800ms_240_frames(self):
    self.check_pipeline(.800)

  def test_steady_pipeline_faults_stop_only_diagnostics(self):
    for fault, reason in [('continuity', 'ts_continuity'), ('unexpected', 'unexpected_pes'),
                          ('duplicate', 'pts_discontinuity'), ('backward', 'pts_discontinuity')]:
      with self.subTest(fault=fault):
        self.observer = latency.FrameTimingObserver()
        windows, result, _ = self.pipeline(.8, fault=fault)
        self.assertGreater(windows[0]['encoder_pes_samples'], 0)
        self.assertEqual(result['diag_active'], 0)
        self.assertEqual(result['diag_sync_lost'], 1)
        self.assertEqual(result['diag_reason'], reason)
        self.observer.observe(packet(), 20)
        self.assertEqual(self.observer.snapshot(20, 1)['encoder_pes_samples'], 0)

  def test_steady_pipeline_wrap_and_final_unfinished_tail(self):
    windows, final, _ = self.pipeline(.8, wrap=True)
    self.assertEqual(sum(w['encoder_pes_samples'] for w in [*windows, final]), 240)
    self.assertEqual(final['diag_sync_lost'], 0)
    self.observer = latency.FrameTimingObserver()
    windows, final, _ = self.pipeline(.8, close_with_tail=True)
    self.assertGreater(final['encoder_pes_samples'], 0)
    self.assertEqual(final['capture_to_pes_avg_ms'], 800)
    self.assertEqual(final['diag_reason'], 'unfinished_at_close')
    self.assertEqual(sum(w['encoder_pes_samples'] for w in [*windows, final]), 224)

  def test_cumulative_pts_drift_rejects_small_adjacent_drift(self):
    for i in range(5):
      self.frame(i, 1 + i * .05, pts=i * 7200)
    result = self.observer.snapshot(1.3, .3)
    self.assertEqual(result['diag_reason'], 'pts_input_drift')
    self.assertEqual(result['diag_sync_lost'], 1)

  def test_constant_shift_with_contiguous_ts_can_remain_undetectable(self):
    self.observer.begin(0, 0, 0, 0)
    self.observer.complete(0, .004)
    for i in range(1, 201):
      at = i * .05
      self.observer.begin(i, at, at, at)
      self.observer.complete(i, at + .004)
      # 元frame 0がmux前で消えた場合を模擬し、TS連続性は正常のままにする。
      self.observer.observe(packet(i * 4500, (i - 1) % 16), at + .010)
      self.observer.datagram((i - 1) * 188, i * 188, at + .012, True)
    result = self.observer.snapshot(10.02, 10.02)
    self.assertEqual(result['diag_active'], 1)
    self.assertEqual(result['encoder_pes_samples'], 200)
    self.assertEqual(result['diag_pending'], 1)
    self.assertEqual(result['capture_to_pes_avg_ms'], 60)

  def test_expired_record_is_not_committed_by_late_send_or_completion(self):
    for late in ('send', 'completion'):
      self.observer = latency.FrameTimingObserver()
      self.observer.begin(0, 0, 0, 0)
      self.observer.observe(packet(), .01)
      if late == 'send':
        self.observer.complete(0, .005)
        self.observer.datagram(0, 188, 2.1, True)
      else:
        self.observer.datagram(0, 188, .012, True)
        self.observer.complete(0, 2.1)
      result = self.observer.snapshot(2.2, 2.2)
      self.assertEqual(result['encoder_pes_samples'], 0)
      self.assertEqual(result['diag_reason'], 'pending_timeout')

  def test_parse_cost_is_per_chunk_reset_and_inactive_has_no_timer(self):
    self.observer.begin(0, 0, 0, 0)
    with patch.object(latency.time, 'perf_counter', side_effect=[1, 1.00002, 2, 2.00004]) as clock:
      self.observer.observe(packet()[:100], .01)
      self.observer.observe(packet()[100:], .02)
      self.assertEqual(clock.call_count, 4)
    result = self.observer.snapshot(.03, .03)
    self.assertEqual(result['observer_parse_calls'], 2)
    self.assertEqual(result['observer_parse_total_ms'], .06)
    self.assertEqual(result['observer_parse_avg_us'], 30)
    self.assertEqual(result['observer_parse_max_us'], 40)
    self.assertEqual(result['diag_pending_max'], 1)
    result = self.observer.snapshot(.04, .01)
    self.assertEqual(result['observer_parse_calls'], 0)
    self.assertEqual(result['observer_parse_max_us'], 0)
    self.assertEqual(result['diag_pending_max'], 1)
    self.observer.complete(0, .05, success=False)
    with patch.object(latency.time, 'perf_counter') as clock:
      self.observer.observe(packet(), .06)
      clock.assert_not_called()

  def test_pts_duplicate_backwards_and_large_jump_disable_pairing(self):
    for pts in (4500, 0, 900000):
      self.observer = latency.FrameTimingObserver()
      self.frame(0, 1, pts=4500)
      self.frame(1, 1.05, pts=pts)
      result = self.observer.snapshot(1.1, .1)
      self.assertEqual(result['diag_sync_lost'], 1)
      self.assertEqual(result['diag_active'], 0)
      self.assertEqual(result['encoder_pes_samples'], 1)

  def test_pts_wrap_remains_valid(self):
    self.frame(0, 1, pts=(1 << 33) - 4500)
    self.frame(1, 1.05, pts=0)
    self.assertEqual(self.observer.snapshot(1.1, .1)['encoder_pes_samples'], 2)

  def test_unexpected_pes_and_duplicate_sequence_disable_pairing(self):
    self.observer.observe(packet(), 0)
    self.assertEqual(self.observer.snapshot(.1, .1)['diag_reason'], 'unexpected_pes')
    self.observer = latency.FrameTimingObserver()
    self.observer.begin(1, 0, 0, 0)
    self.observer.begin(1, .05, .05, .05)
    self.assertEqual(self.observer.snapshot(.1, .1)['diag_reason'], 'sequence_or_capacity')

  def test_queue_is_bounded_and_does_not_silently_evict(self):
    for i in range(latency.MAX_PENDING + 1):
      self.observer.begin(i, i * .01, i * .01, i * .01)
      self.assertLessEqual(len(self.observer._records), latency.MAX_PENDING)
    result = self.observer.snapshot(.5, .5)
    self.assertEqual(result['diag_active'], 0)
    self.assertEqual(result['diag_sync_lost'], 1)
    self.assertEqual(result['diag_pending'], 0)

  def test_samples_bounded_and_p95_uses_nearest_rank(self):
    for i in range(250):
      self.frame(i, 1 + i * .05)
    result = self.observer.snapshot(14, 13)
    self.assertEqual(result['encoder_pes_samples'], 250)
    self.assertEqual(result['capture_to_pes_samples'], 200)
    self.assertEqual(result['capture_to_pes_p95_ms'], 14)
    self.observer._samples['capture_to_pes'].extend(i / 1000 for i in range(1, 21))
    result = self.observer.snapshot(15, 1)
    self.assertEqual(result['capture_to_pes_avg_ms'], 10.5)
    self.assertEqual(result['capture_to_pes_p95_ms'], 19)
    self.assertEqual(result['capture_to_pes_max_ms'], 20)

  def test_datagram_mapping_for_all_sizes(self):
    for size in (188, 564, 1316):
      self.observer = latency.FrameTimingObserver()
      for i in range(7):
        self.observer.begin(i, i * .05, i * .05, i * .05)
        self.observer.complete(i, i * .05 + .005)
      self.observer.observe(b''.join(packet(i * 4500, i) for i in range(7)), .4)
      for offset in range(0, 1316, size):
        self.observer.datagram(offset, offset + size, .41, True)
      result = self.observer.snapshot(.5, .5)
      self.assertEqual(result['encoder_pes_samples'], 7)
      self.assertEqual(result['pes_to_send_avg_ms'], 10)

  def test_first_datagram_drop_has_no_success_latency(self):
    self.frame(0, 1, sent=False)
    result = self.observer.snapshot(1.1, .1)
    self.assertEqual(result['encoder_pes_samples'], 1)
    self.assertEqual(result['frame_first_datagram_drop_count'], 1)
    self.assertEqual(result['capture_to_udp_send_samples'], 0)
    self.assertIsNone(result['capture_to_udp_send_avg_ms'])

  def test_close_resets_unfinished_and_new_process_starts_clean(self):
    self.observer.begin(3, 0, 0, 0)
    self.observer.complete(3, .01)
    result = self.observer.snapshot(.1, .1, final=True)
    self.assertEqual(result['diag_reason'], 'unfinished_at_close')
    self.assertEqual(result['diag_sync_lost'], 1)
    self.assertEqual(self.observer.snapshot(.2, .1, final=True)['diag_sync_lost'], 0)
    self.observer = latency.FrameTimingObserver()
    self.frame(0, 1)
    self.assertEqual(self.observer.snapshot(1.1, .1)['encoder_pes_samples'], 1)

  def test_failed_write_and_inactive_observer_do_no_more_parsing(self):
    self.observer.begin(0, 0, 0, 0)
    self.observer.complete(0, .01, success=False)
    with patch.object(self.observer.parser, 'feed') as parse:
      self.observer.observe(packet(), .02)
      self.observer.begin(1, .05, .05, .05)
      parse.assert_not_called()
    self.assertEqual(self.observer.snapshot(.1, .1)['diag_sync_lost'], 1)

  def test_invalid_input_and_send_clock_order_is_rejected(self):
    self.observer.begin(0, 1, .5, .6)
    self.assertEqual(self.observer.snapshot(1, 1)['diag_reason'], 'input_timestamps')
    self.observer = latency.FrameTimingObserver()
    self.observer.begin(0, 0, 0, 0)
    self.observer.complete(0, .005)
    self.observer.observe(packet(), .010)
    self.observer.datagram(0, 188, .012, True, attempted_at=.009)
    result = self.observer.snapshot(.1, .1)
    self.assertEqual(result['diag_reason'], 'send_timestamps')
    self.assertEqual(result['encoder_pes_samples'], 0)


class TestProcessUsage(unittest.TestCase):
  def stat(self, ticks, identity=123):
    fields = ['0'] * 22
    fields[0], fields[11], fields[12], fields[19], fields[21] = 'S', str(ticks), '5', str(identity), '2000'
    return '123 (ffmpeg ) worker) ' + ' '.join(fields)

  def test_cpu_rss_first_sample_and_pid_reuse(self):
    values = [self.stat(10), self.stat(510), self.stat(20, 456)]
    with patch.object(latency.sys, 'platform', 'linux'), patch.object(latency.Path, 'read_text', side_effect=values), \
         patch.object(latency.os, 'sysconf', side_effect=lambda key: 100 if key == 'SC_CLK_TCK' else 4096, create=True):
      usage = latency.ProcessUsage(123)
      self.assertIsNone(usage.sample(1)['ffmpeg_cpu_pct'])
      self.assertEqual(usage.sample(6), {'ffmpeg_cpu_pct': 100, 'ffmpeg_rss_kb': 8000})
      self.assertIsNone(usage.sample(11)['ffmpeg_cpu_pct'])

  def test_disappeared_pid_or_invalid_stat_is_nonfatal(self):
    for error in (FileNotFoundError(), PermissionError(), 'broken'):
      with patch.object(latency.sys, 'platform', 'linux'), patch.object(latency.Path, 'read_text') as read:
        if isinstance(error, Exception):
          read.side_effect = error
        else:
          read.return_value = error
        self.assertEqual(latency.ProcessUsage(123).sample(0), {'ffmpeg_cpu_pct': None, 'ffmpeg_rss_kb': None})

  def test_unsupported_platform_does_not_read_proc(self):
    with patch.object(latency.sys, 'platform', 'win32'), patch.object(latency.Path, 'read_text') as read:
      self.assertIsNone(latency.ProcessUsage(123).sample(1)['ffmpeg_cpu_pct'])
      read.assert_not_called()


if __name__ == '__main__':
  unittest.main()
