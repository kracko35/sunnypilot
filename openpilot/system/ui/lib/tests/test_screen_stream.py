"""配信の排他・復旧・遅延制限を実機なしで検証する。"""

import errno
import os
from pathlib import Path
from contextlib import contextmanager
from dataclasses import replace
import subprocess
import time
import threading
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from openpilot.system.ui.lib.tests.screen_stream_test_support import load_screen_stream
from openpilot.system.ui.lib.screen_stream_config import PARAM_KEYS, ScreenStreamConfig, parse_stream_setting

stream = load_screen_stream()


def wait_until(predicate, timeout=2.0):
  deadline = time.monotonic() + timeout
  while not predicate():
    if time.monotonic() >= deadline:
      raise AssertionError("状態遷移が完了しませんでした")
    time.sleep(0.005)


class TestScreenStreamer(unittest.TestCase):
  def setUp(self):
    self.enabled = False
    self.network = Mock(return_value=('wlan0', '192.168.1.8'))
    self.config = Mock(return_value=ScreenStreamConfig())
    self.streamer = stream.ScreenStreamer(lambda: self.enabled, self.network, self.config)
    self.streamer.visible.set()
    self.processes = []
    self.commands = []
    self.senders = []
    self.sender_class = stream.MpegTsUdpSender
    self.stdout_factory = Mock
    log_patch = patch.object(stream, 'cloudlog')
    self.cloudlog = log_patch.start()
    self.addCleanup(log_patch.stop)
    set_blocking = os.set_blocking

    def sender(*args):
      transport = Mock()
      transport.done.is_set.return_value = False
      transport.error = None
      transport.arguments = args
      transport.mode = 'multicast' if stream.ipaddress.IPv4Address(args[2].address).is_multicast else 'unicast'
      transport.stats_snapshot.return_value = dict.fromkeys([
        'sender_datagrams', 'sender_drops', 'sender_bytes', 'sender_datagrams_delta', 'sender_drops_delta',
        'sender_drop_ratio_window_pct', 'socket_sndbuf', 'socket_outq_current', 'socket_outq_peak',
      ], 0)
      self.senders.append(transport)
      return transport

    def process(*args, **kwargs):
      self.commands.append(args[0])
      proc = Mock()
      proc.poll.return_value = None
      proc.stdin.fileno.return_value = 42
      proc.stdout = self.stdout_factory()
      self.processes.append(proc)
      return proc

    for target, kwargs in [
      ('subprocess.Popen', {'side_effect': process}), ('MpegTsUdpSender', {'side_effect': sender}),
      ('os.set_blocking', {'side_effect': lambda fd, blocking: None if fd == 42 else set_blocking(fd, blocking)}),
      ('select.select', {'return_value': ([], [], [])}),
      ('CONFIG_INTERVAL', {'new': 0.02}), ('NETWORK_INTERVAL_CONNECTED', {'new': 0.02}),
      ('NETWORK_INTERVAL_DISCONNECTED', {'new': 0.02}), ('RETRY_INTERVAL', {'new': 0.03}),
    ]:
      patcher = patch(f'{stream.__name__}.{target}', **kwargs)
      patcher.start()
      self.addCleanup(patcher.stop)
    if hasattr(stream.os, 'sched_setscheduler'):
      for name in ['sched_setscheduler', 'setpriority']:
        patcher = patch.object(stream.os, name)
        patcher.start()
        self.addCleanup(patcher.stop)
    self.addCleanup(self.streamer.close)

  def log_messages(self):
    return '\n'.join(str(call.args[0]) for call in self.cloudlog.mock_calls if call.args)

  def test_disabled_has_no_network_or_encoder(self):
    self.streamer.start()
    time.sleep(0.15)
    self.network.assert_not_called()
    self.config.assert_not_called()
    self.assertEqual(self.processes, [])
    self.assertEqual(self.senders, [])
    self.assertIsNone(self.streamer._latency)
    self.assertIsNone(self.streamer._usage)

  def test_live_settings_restart_once_with_new_destination(self):
    self.enabled = True
    self.streamer.start()
    wait_until(self.streamer.ready.is_set)
    self.config.return_value = ScreenStreamConfig('239.10.20.30', 15000, 3000, 2)
    wait_until(lambda: len(self.processes) == 2 and self.streamer.ready.is_set())
    self.processes[0].terminate.assert_called_once()
    self.assertEqual(self.commands[-1][-1], 'pipe:1')
    self.assertEqual(self.senders[-1].arguments[1:], ('192.168.1.8', self.config.return_value))
    self.senders[0].stop.assert_called_once()
    self.senders[0].close.assert_called_once()
    self.assertEqual(self.commands[-1][self.commands[-1].index('-b:v') + 1], '3000k')
    time.sleep(0.1)
    self.assertEqual(len(self.processes), 2)
    self.assertIn('screen stream state: config changed old=', self.log_messages())
    self.assertEqual(self.streamer._restart_count, 0)

  def test_invalid_saved_settings_stop_until_corrected(self):
    self.enabled = True
    self.streamer.start()
    wait_until(self.streamer.ready.is_set)
    self.config.side_effect = ValueError('設定破損')
    wait_until(lambda: not self.streamer.ready.is_set())
    self.processes[0].terminate.assert_called_once()
    self.config.side_effect = None
    wait_until(lambda: len(self.processes) == 2 and self.streamer.ready.is_set())
    self.assertIn('screen stream restart: invalid config:', self.log_messages())

  def test_switches_between_unicast_and_multicast_with_clean_restart(self):
    self.enabled = True
    self.streamer.start()
    wait_until(self.streamer.ready.is_set)
    for index, address in enumerate(['192.168.4.44', '239.255.42.99'], start=1):
      self.config.return_value = replace(self.config.return_value, address=address)
      wait_until(lambda expected=index + 1: len(self.processes) == expected and self.streamer.ready.is_set())
      self.processes[index - 1].terminate.assert_called_once()
      self.processes[index - 1].stdin.close.assert_called_once()
      self.processes[index - 1].stdout.close.assert_called_once()
      self.senders[index - 1].stop.assert_called_once()
      self.senders[index - 1].close.assert_called_once()
      self.assertEqual(self.senders[index].arguments[2].address, address)
      self.assertEqual(self.commands[index], self.commands[0])
    self.assertIn('mode=unicast', self.log_messages())
    self.assertIn('mode=multicast', self.log_messages())
    self.assertEqual(self.streamer._restart_count, 0)

  def test_settings_saved_while_disabled_apply_on_enable(self):
    self.config.return_value = ScreenStreamConfig(port=23456)
    self.streamer.start()
    self.enabled = True
    wait_until(self.streamer.ready.is_set)
    self.assertEqual(self.senders[0].arguments[2].port, 23456)

  def test_wifi_disconnect_reconnect_address_change_and_toggle(self):
    self.enabled = True
    self.network.return_value = None
    self.streamer.start()
    wait_until(lambda: self.network.called)
    self.assertEqual(self.processes, [])
    self.network.return_value = ('wlan0', '192.168.1.8')
    wait_until(self.streamer.ready.is_set)
    first = self.processes[-1]
    self.network.return_value = None
    wait_until(lambda: not self.streamer.ready.is_set())
    first.terminate.assert_called_once()
    self.senders[0].close.assert_called_once()
    self.assertEqual(self.streamer._restart_count, 0)
    self.assertIn('screen stream state: network unavailable', self.log_messages())
    self.network.return_value = ('wlan0', '192.168.2.8')
    wait_until(lambda: len(self.processes) == 2 and self.streamer.ready.is_set())
    self.assertEqual(self.senders[-1].arguments[1], '192.168.2.8')
    self.network.return_value = ('wlan1', '192.168.2.9')
    wait_until(lambda: len(self.processes) == 3 and self.streamer.ready.is_set())
    self.assertEqual(self.senders[-1].arguments[1], '192.168.2.9')
    self.enabled = False
    wait_until(lambda: not self.streamer.ready.is_set())
    self.processes[-1].terminate.assert_called_once()
    self.senders[-1].close.assert_called_once()
    self.network.reset_mock()
    time.sleep(0.15)
    self.network.assert_not_called()
    self.assertIn('screen stream state: network changed old=', self.log_messages())
    self.assertIn('reason=stream disabled', self.log_messages())

  def test_screen_off_stops_and_wake_restarts(self):
    self.enabled = True
    self.streamer.start()
    wait_until(self.streamer.ready.is_set)
    self.streamer.visible.clear()
    wait_until(lambda: not self.streamer.ready.is_set())
    self.streamer.visible.set()
    wait_until(lambda: len(self.processes) == 2 and self.streamer.ready.is_set())
    self.senders[0].close.assert_called_once()
    self.assertIn('reason=screen invisible', self.log_messages())

  def test_encoder_exit_restarts(self):
    self.enabled = True
    self.streamer.start()
    wait_until(self.streamer.ready.is_set)
    self.processes[-1].poll.return_value = 1
    wait_until(lambda: len(self.processes) == 2 and self.streamer.ready.is_set())
    self.assertIn('screen stream restart: ffmpeg exited rc=1', self.log_messages())
    self.assertEqual(self.streamer._restart_count, 1)

  def test_network_query_errors_preserve_encoder_and_recover(self):
    self.enabled = True
    self.streamer.start()
    wait_until(self.streamer.ready.is_set)
    first = self.processes[0]
    for error in [TimeoutError('D-Bus待機期限'), OSError('D-Bus通信失敗'), EOFError('D-Bus接続終了')]:
      with self.subTest(error=error):
        self.network.side_effect = error
        wait_until(lambda: self.streamer._network_query_failures >= 2)
        self.assertTrue(self.streamer.ready.is_set())
        self.assertEqual(self.processes, [first])
        first.terminate.assert_not_called()
        self.senders[0].close.assert_not_called()
        self.assertEqual(self.streamer._restart_count, 0)
        self.network.side_effect = None
        wait_until(lambda: self.streamer._network_query_failures == 0)
    self.assertIn('screen stream network query transient:', self.log_messages())
    self.assertIn("using=('wlan0', '192.168.1.8')", self.log_messages())
    self.assertIn('screen stream network query recovered:', self.log_messages())
    self.assertNotIn('screen stream restart:', self.log_messages())

  def _check_blocked_query_does_not_block_frames(self, query, result):
    entered, release = threading.Event(), threading.Event()

    def blocked():
      entered.set()
      release.wait(2)
      return result

    self.enabled = True
    with patch.object(stream.os, 'write', return_value=stream.FRAME_BYTES) as write:
      self.streamer.start()
      wait_until(self.streamer.ready.is_set)
      first = self.processes[0]
      query.side_effect = blocked
      try:
        self.assertTrue(entered.wait(1))
        started = time.monotonic()
        for count in range(1, 7):
          self.streamer.submit(bytes(stream.FRAME_BYTES))
          wait_until(lambda count=count: write.call_count >= count, timeout=.2)
          time.sleep(.05)
        self.assertGreater(time.monotonic() - started, .25)
        self.assertFalse(release.is_set())
        self.assertEqual(self.processes, [first])
        first.terminate.assert_not_called()
      finally:
        release.set()

  def test_blocked_network_query_does_not_stop_frame_writes(self):
    self._check_blocked_query_does_not_block_frames(self.network, self.network.return_value)

  def test_blocked_config_query_does_not_stop_frame_writes(self):
    self._check_blocked_query_does_not_block_frames(self.config, self.config.return_value)

  def test_stats_are_periodic_and_include_transport_and_timings(self):
    self.enabled = True
    with patch.object(stream, 'LATENCY_STATS_INTERVAL', .1), patch.object(stream.os, 'write', return_value=stream.FRAME_BYTES):
      self.streamer.start()
      wait_until(self.streamer.ready.is_set)
      self.streamer.stats.record({'capture_count': 1}, capture=.02, readback=.015, gpu_scale=.003, bytes_copy=.002)
      self.streamer.submit(bytes(stream.FRAME_BYTES), captured=time.monotonic() - .03)
      wait_until(lambda: 'screen stream latency stats:' in self.log_messages())
      message = next(call.args[0] for call in self.cloudlog.info.call_args_list if 'screen stream latency stats:' in call.args[0])
      for field in ['capture_count=1', 'submitted_count=1', 'frames_written=1', 'capture_avg_ms=20.0', 'readback_max_ms=15.0',
                    'queue_replaced_count=', 'stale_drop_count=', 'queue_age_avg_ms=', 'stdin_write_max_ms=', 'pid=',
                    'mode=multicast', 'bitrate=1000', 'sender_datagrams=', 'sender_drops=', 'sender_bytes=',
                    'socket_sndbuf=', 'socket_outq_current=', 'socket_outq_peak=', 'first_frame_written=1',
                    'first_frame_write_ms=', 'first_frame_blocked_wait_ms=', 'process_start_to_first_frame_complete_ms=',
                    'first_frame_write_timeout_count=0', 'regular_frame_write_timeout_count=0']:
        self.assertIn(field, message)

  def test_worker_drops_realtime_policy_without_lowering_nice(self):
    self.enabled = True
    with patch.object(stream.os, 'sched_setscheduler', create=True) as scheduler, \
         patch.object(stream.os, 'sched_param', create=True, return_value=0), \
         patch.object(stream.os, 'SCHED_OTHER', create=True, new=0), patch.object(stream.os, 'setpriority', create=True) as priority:
      self.streamer.start()
      wait_until(self.streamer.ready.is_set)
      scheduler.assert_called_once_with(0, 0, 0)
      priority.assert_not_called()

  def test_final_stats_flush_once_before_config_restart(self):
    self.enabled = True
    self.streamer.start()
    wait_until(self.streamer.ready.is_set)
    old_observer = self.streamer._latency
    self.streamer.stats.record({'capture_count': 3}, capture=.004)
    self.config.return_value = ScreenStreamConfig(bitrate=500)
    wait_until(lambda: len(self.processes) == 2 and self.streamer.ready.is_set())
    finals = [call.args[0] for call in self.cloudlog.info.call_args_list if 'screen stream latency final:' in call.args[0]]
    self.assertEqual(len(finals), 1)
    self.assertIn('config changed', finals[0])
    self.assertIn('capture_count=3', finals[0])
    self.assertIn('bitrate=1000', finals[0])
    self.assertIn('encoder_pes_samples=0', finals[0])
    self.assertIn('diag_pending_max=0', finals[0])
    self.assertIn('observer_parse_calls=0', finals[0])
    self.assertIn('observer_parse_total_ms=0.0', finals[0])
    self.assertIn('stdin_write_syscalls=0', finals[0])
    self.assertIn('ffmpeg_cpu_pct=None', finals[0])
    self.assertIsNot(self.streamer._latency, old_observer)
    self.assertEqual(self.streamer.stats.snapshot()['capture_count'], 0)
    messages = [call.args[0] for call in self.cloudlog.info.call_args_list]
    stopped = next(i for i, message in enumerate(messages) if 'screen stream stopped:' in message)
    self.assertLess(messages.index(finals[0]), stopped)
    self.enabled = False
    wait_until(lambda: self.streamer._proc is None)
    time.sleep(.15)
    finals = [call.args[0] for call in self.cloudlog.info.call_args_list if 'screen stream latency final:' in call.args[0]]
    self.assertEqual(len(finals), 2)
    self.assertIn('stream disabled', finals[-1])
    self.assertIsNone(self.streamer._latency)
    self.assertIsNone(self.streamer._usage)

  def test_stdin_blocking_waits_and_partial_syscalls_are_aggregated(self):
    self.streamer._proc = Mock()
    with patch.object(stream.os, 'write', side_effect=[2, BlockingIOError(), 3]), \
         patch.object(stream.time, 'perf_counter', side_effect=[1.0, 1.002]):
      self.streamer._write_frame(time.monotonic(), b'12345')
    result = self.streamer.stats.snapshot(reset=True)
    self.assertEqual(result['stdin_write_syscalls'], 3)
    self.assertEqual(result['stdin_bytes'], 5)
    self.assertEqual(result['stdin_blocked_events'], 1)
    self.assertEqual(result['stdin_blocked_wait_ms'], 2)
    self.assertEqual(result['stdin_blocked_wait_avg_ms'], 2)
    self.assertEqual(result['stdin_blocked_wait_max_ms'], 2)
    self.assertEqual(self.streamer.stats.snapshot()['stdin_write_syscalls'], 0)

  def test_observer_sync_loss_does_not_restart_or_stop_stream(self):
    self.enabled = True
    with patch.object(stream.os, 'write', side_effect=lambda fd, data: len(data)):
      self.streamer.start()
      wait_until(self.streamer.ready.is_set)
      self.streamer._latency.observe(b'X' * 188, time.monotonic())
      self.streamer.submit(bytes(stream.FRAME_BYTES))
      wait_until(lambda: self.streamer.stats.snapshot()['frames_written'] == 1)
      self.assertEqual(len(self.processes), 1)
      self.assertEqual(self.streamer._restart_count, 0)
      self.assertFalse(self.streamer._latency.active)
      self.assertTrue(self.streamer.ready.is_set())

  def test_congestion_warning_does_not_restart_encoder(self):
    self.enabled = True
    with patch.object(stream, 'LATENCY_STATS_INTERVAL', .05):
      self.streamer.start()
      wait_until(self.streamer.ready.is_set)
      self.senders[0].stats_snapshot.return_value.update(sender_datagrams_delta=90, sender_drops_delta=10,
                                                        sender_drop_ratio_window_pct=10.0)
      wait_until(lambda: 'screen stream UDP congestion:' in self.log_messages())
      self.assertEqual(len(self.processes), 1)
      self.assertEqual(self.streamer._restart_count, 0)
      self.assertTrue(self.streamer.ready.is_set())

  def test_final_stats_failure_still_closes_encoder(self):
    self.enabled = True
    self.streamer.start()
    wait_until(self.streamer.ready.is_set)
    self.senders[0].stats_snapshot.side_effect = ValueError('計測失敗')
    self.enabled = False
    wait_until(lambda: self.processes[0].stdin.close.called)
    self.processes[0].terminate.assert_called_once()
    self.senders[0].close.assert_called_once()
    self.assertIn('screen stream latency final failed', self.log_messages())

  def test_repeated_network_timeouts_do_not_restart_or_spam(self):
    self.enabled = True
    with patch.object(stream, 'NETWORK_INTERVAL_CONNECTED', 0.001):
      self.streamer.start()
      wait_until(self.streamer.ready.is_set)
      self.network.side_effect = TimeoutError('D-Bus待機期限')
      wait_until(lambda: self.streamer._network_query_failures >= 100, timeout=5.0)
      self.assertEqual(len(self.processes), 1)
      self.assertEqual(self.streamer._restart_count, 0)
      self.assertTrue(self.streamer.ready.is_set())
      self.assertEqual(self.cloudlog.warning.call_count, 1)
      self.network.side_effect = None
      wait_until(lambda: self.streamer._network_query_failures == 0)
      self.streamer.close()

  def test_network_error_warning_uses_ten_second_window(self):
    error = TimeoutError('D-Bus待機期限')
    with patch.object(stream.time, 'monotonic', return_value=1.0):
      for _ in range(100):
        self.streamer._log_network_query_error(('wlan0', '192.168.1.8'), error)
    self.assertEqual(self.cloudlog.warning.call_count, 1)
    with patch.object(stream.time, 'monotonic', return_value=10.9):
      self.streamer._log_network_query_error(('wlan0', '192.168.1.8'), error)
    self.assertEqual(self.cloudlog.warning.call_count, 1)
    with patch.object(stream.time, 'monotonic', return_value=11.0):
      self.streamer._log_network_query_error(('wlan0', '192.168.1.8'), error)
    self.assertEqual(self.cloudlog.warning.call_count, 2)
    self.assertIn('failures=102', self.cloudlog.warning.call_args.args[0])
    self.cloudlog.exception.assert_not_called()

  def test_startup_network_timeout_waits_without_restart_or_cleanup(self):
    self.enabled = True
    self.network.side_effect = TimeoutError('D-Bus待機期限')
    with patch.object(self.streamer, '_close_process', wraps=self.streamer._close_process) as close:
      self.streamer.start()
      wait_until(lambda: self.streamer._network_query_failures >= 3)
      self.assertEqual(self.processes, [])
      self.assertEqual(self.streamer._restart_count, 0)
      self.assertFalse(self.streamer.ready.is_set())
      close.assert_not_called()
      self.assertIn('screen stream network query failed before start:', self.log_messages())
      self.network.side_effect = None
      wait_until(self.streamer.ready.is_set)
      self.assertEqual(len(self.processes), 1)
      close.assert_not_called()
      self.assertEqual(self.streamer._network_query_failures, 0)
      self.assertNotIn('screen stream restart:', self.log_messages())

  def test_query_timeouts_do_not_hide_config_changes(self):
    self.enabled = True
    self.config.return_value = ScreenStreamConfig(bitrate=500)
    self.streamer.start()
    wait_until(self.streamer.ready.is_set)
    self.network.side_effect = TimeoutError('D-Bus待機期限')
    wait_until(lambda: self.streamer._network_query_failures >= 1)
    self.config.return_value = ScreenStreamConfig(bitrate=1500)
    wait_until(lambda: len(self.processes) == 2 and self.streamer.ready.is_set())
    self.assertEqual(self.senders[-1].arguments[1], '192.168.1.8')
    self.assertEqual(self.senders[-1].arguments[2].bitrate, 1500)
    self.processes[0].terminate.assert_called_once()
    self.assertEqual(self.streamer._restart_count, 0)
    self.assertIn('screen stream state: config changed', self.log_messages())
    self.assertNotIn('screen stream restart:', self.log_messages())

  def test_network_discovery_does_not_start_encoder_with_invalid_config(self):
    self.enabled = True
    self.config.side_effect = ValueError('設定破損')
    # 障害後の100ms待機より長い猶予を設け、設定再試行の前にネットワーク照会を通す。
    with patch.object(stream, 'RETRY_INTERVAL', 0.3):
      self.streamer.start()
      wait_until(lambda: self.network.called)
      time.sleep(0.1)
      self.assertEqual(self.processes, [])
      self.assertFalse(self.streamer.ready.is_set())
      self.config.side_effect = None
      wait_until(self.streamer.ready.is_set)
      self.assertEqual(len(self.processes), 1)

  def test_connected_disconnected_and_config_polling_are_independent(self):
    checks = []

    def network():
      checks.append(time.monotonic())
      return self.network.return_value

    self.network.return_value = None
    self.network.side_effect = network
    self.enabled = True
    with patch.object(stream, 'NETWORK_INTERVAL_CONNECTED', 0.4), \
         patch.object(stream, 'NETWORK_INTERVAL_DISCONNECTED', 0.02):
      self.streamer.start()
      wait_until(lambda: len(checks) >= 2)
      self.assertGreaterEqual(checks[1] - checks[0], 0.02)
      self.assertLess(checks[1] - checks[0], 0.4)
      self.network.return_value = ('wlan0', '192.168.1.8')
      wait_until(self.streamer.ready.is_set)
      count, config_count = len(checks), self.config.call_count
      time.sleep(0.15)
      self.assertEqual(len(checks), count)
      self.assertGreaterEqual(self.config.call_count - config_count, 2)
      wait_until(lambda: len(checks) > count)
      self.assertGreaterEqual(checks[count] - checks[count - 1], 0.4)
      self.streamer.close()

  def test_each_transport_setting_and_interface_change_restarts(self):
    self.enabled = True
    self.streamer.start()
    wait_until(self.streamer.ready.is_set)
    for field, value in [('address', '239.2.3.4'), ('port', 23456), ('ttl', 2), ('bitrate', 2500)]:
      with self.subTest(field=field):
        count = len(self.processes)
        self.config.return_value = replace(self.config.return_value, **{field: value})
        wait_until(lambda count=count: len(self.processes) == count + 1 and self.streamer.ready.is_set())
        self.assertEqual(self.senders[-1].arguments[2], self.config.return_value)
        self.senders[-2].close.assert_called_once()
    self.network.return_value = ('wlan1', '192.168.1.8')
    wait_until(lambda: len(self.processes) == 6 and self.streamer.ready.is_set())
    self.senders[-2].close.assert_called_once()

  @contextmanager
  def real_transport(self):
    writers, transports, sockets = [], [], []

    def stdout():
      read_fd, write_fd = os.pipe()
      writers.append(write_fd)
      self.addCleanup(os.close, write_fd)
      pipe = os.fdopen(read_fd, 'rb', buffering=0)
      self.addCleanup(pipe.close)
      return pipe

    def sender(*args):
      transport = self.sender_class(*args)
      transports.append(transport)
      return transport

    def socket(*args):
      sock = Mock()
      sock.sendto.side_effect = lambda payload, destination: len(payload)
      sockets.append(sock)
      return sock

    self.stdout_factory = stdout
    self.enabled = True
    with patch.object(stream, 'MpegTsUdpSender', side_effect=sender), patch.object(stream.socket, 'socket', side_effect=socket):
      try:
        self.streamer.start()
        yield writers, transports, sockets
      finally:
        self.streamer.close()

  def test_socket_failure_stops_encoder_and_restarts_transport(self):
    with self.real_transport() as (writers, transports, sockets):
      wait_until(self.streamer.ready.is_set)
      sockets[0].sendto.side_effect = OSError(errno.ENETDOWN, 'UDP送信障害')
      os.write(writers[0], b'\x47' * stream.UDP_PAYLOAD_SIZE)
      wait_until(lambda: len(transports) == 2 and self.streamer.ready.is_set())
      self.assertIsInstance(transports[0].error, OSError)
      self.assertFalse(transports[0]._thread.is_alive())
      self.processes[0].terminate.assert_called_once()
      self.assertTrue(self.processes[0].stdout.closed)
      sockets[0].close.assert_called()
      self.assertIn('screen stream restart: sender fatal error:', self.log_messages())
      self.assertIn(f'errno={errno.ENETDOWN}', self.log_messages())
      self.assertIn(repr(transports[0].error), self.log_messages())
    self.assertTrue(all(not transport._thread.is_alive() for transport in transports))
    self.assertTrue(all(proc.stdout.closed for proc in self.processes))

  def test_transient_send_errors_keep_sender_and_encoder_alive(self):
    errors = [BlockingIOError(errno.EAGAIN, '一時的な送信不可'), OSError(errno.EWOULDBLOCK, '送信待ち'),
              OSError(errno.ENOBUFS, '送信バッファ不足'), InterruptedError(errno.EINTR, '割り込み'), TimeoutError('送信待ち期限')]
    with self.real_transport() as (writers, transports, sockets):
      wait_until(self.streamer.ready.is_set)
      transport = transports[0]
      for count, error in enumerate(errors, start=1):
        with self.subTest(error=error):
          sockets[0].sendto.side_effect = [error, 1316]
          dropped = bytes([count]) * 1316
          sent = bytes([count + 10]) * 1316
          os.write(writers[0], dropped)
          wait_until(lambda count=count: transport.datagrams_dropped == count)
          self.assertFalse(transport.done.is_set())
          self.assertIsNone(transport.error)
          self.assertTrue(transport._thread.is_alive())
          os.write(writers[0], sent)
          wait_until(lambda count=count: transport.datagrams_sent == count)
          self.assertEqual(sockets[0].sendto.call_args.args[0], sent)
          self.assertEqual(transport.bytes_sent, count * 1316)
      time.sleep(0.1)
      self.assertEqual(len(self.processes), 1)
      self.assertTrue(self.streamer.ready.is_set())
      self.assertEqual(self.streamer._restart_count, 0)
      self.assertEqual(self.cloudlog.warning.call_count, 1)
      self.assertIn('screen stream started: pid=', self.log_messages())

  def test_stable_network_and_config_do_not_spam_logs(self):
    self.enabled = True
    self.streamer.start()
    wait_until(self.streamer.ready.is_set)
    self.cloudlog.reset_mock()
    time.sleep(0.2)
    self.cloudlog.assert_not_called()
    self.assertEqual(self.cloudlog.mock_calls, [])
    self.assertEqual(self.streamer._restart_count, 0)

  def test_stale_frame_is_dropped_without_encoder_restart(self):
    self.enabled = True
    with patch.object(stream.os, 'write', return_value=stream.FRAME_BYTES) as write:
      self.streamer.start()
      wait_until(self.streamer.ready.is_set)
      self.streamer._frames.put_nowait((time.monotonic() - .080, bytes(stream.FRAME_BYTES), 1))
      wait_until(self.streamer._frames.empty)
      # 次の新鮮なフレームまで処理できれば、古いフレームによる書き込みも再起動もない。
      self.streamer.submit(bytes(stream.FRAME_BYTES))
      wait_until(lambda: write.call_count == 1)
      self.assertEqual(len(self.processes), 1)
      self.assertEqual(self.streamer._restart_count, 0)
      self.assertEqual(self.streamer.stats.snapshot()['stale_drop_count'], 1)

  def test_pipe_deadline_starts_at_write_not_capture(self):
    self.streamer._proc = Mock()
    self.streamer._first_frame_written = True
    with patch.object(stream.time, 'monotonic', side_effect=[10.0, 10.0, 10.09]), \
         patch.object(stream.os, 'write', side_effect=[1, 2]) as write:
      self.streamer._write_frame(9.8, b'abc')
    self.assertEqual(write.call_count, 2)

  def delayed_pipe(self, first_delay, regular_delay=0):
    # 各プロセスの先頭64KiBを受け入れ、consumerが動くまではEAGAINを返す。
    states = {}

    def write(fd, data):
      state = states.setdefault(len(self.processes), {'completed': 0, 'started': None})
      if state['started'] is None:
        state['started'] = time.monotonic()
        return min(65536, len(data))
      delay = first_delay if state['completed'] == 0 else regular_delay
      if time.monotonic() - state['started'] < delay:
        raise BlockingIOError
      state['completed'] += 1
      state['started'] = None
      return len(data)

    return write

  def check_first_frame_delay(self, delay):
    self.enabled = True
    with patch.object(stream.os, 'write', side_effect=self.delayed_pipe(delay)):
      self.streamer.start()
      wait_until(self.streamer.ready.is_set)
      self.streamer.submit(bytes(stream.FRAME_BYTES))
      wait_until(lambda: self.streamer.stats.snapshot()['frames_written'] == 1)
      self.assertTrue(self.streamer._first_frame_written)
      self.assertEqual(self.streamer._restart_count, 0)
      self.assertEqual(len(self.processes), 1)
      metrics = self.streamer._first_frame_metrics.copy()
      self.assertGreaterEqual(metrics['first_frame_write_ms'], delay * 1000)
      self.assertGreater(metrics['first_frame_blocked_wait_ms'], 0)
      self.assertGreater(metrics['first_frame_blocked_events'], 0)
      self.assertGreater(metrics['first_frame_write_syscalls'], 2)
      self.assertEqual(metrics['first_frame_bytes_written'], stream.FRAME_BYTES)
      self.assertGreaterEqual(metrics['process_start_to_first_frame_complete_ms'], metrics['first_frame_write_ms'])
      self.streamer.submit(bytes(stream.FRAME_BYTES))
      wait_until(lambda: self.streamer.stats.snapshot()['frames_written'] == 2)
      self.assertEqual(self.streamer._first_frame_metrics, metrics)
      self.streamer.close()
      final = next(call.args[0] for call in self.cloudlog.info.call_args_list if 'screen stream latency final:' in call.args[0])
      for name, value in metrics.items():
        self.assertIn(f'{name}={value}', final)
      self.assertIn('first_frame_write_timeout_count=0', final)
      self.assertIn('regular_frame_write_timeout_count=0', final)

  def test_first_frame_120ms_does_not_restart(self):
    self.check_first_frame_delay(.120)

  def test_first_frame_300ms_does_not_restart_and_preserves_metrics(self):
    self.check_first_frame_delay(.300)

  def test_first_frame_partial_timeout_restarts_with_clean_startup_and_observer(self):
    self.enabled = True
    with patch.object(stream.os, 'write', side_effect=self.delayed_pipe(stream.FIRST_FRAME_WRITE_TIMEOUT + .1)):
      self.streamer.start()
      wait_until(self.streamer.ready.is_set)
      observer = self.streamer._latency
      self.streamer.submit(bytes(stream.FRAME_BYTES))
      wait_until(lambda: len(self.processes) == 2 and self.streamer.ready.is_set())
      self.processes[0].terminate.assert_called_once()
      self.processes[0].stdin.close.assert_called_once()
      self.assertEqual(self.streamer._first_frame_write_timeout_count, 1)
      self.assertEqual(self.streamer._regular_frame_write_timeout_count, 0)
      self.assertFalse(self.streamer._first_frame_written)
      self.assertTrue(all(value is None for value in self.streamer._first_frame_metrics.values()))
      self.assertIsNot(observer, self.streamer._latency)
      self.assertTrue(self.streamer._latency.active)
      message = self.log_messages()
      for field in ['first_frame=1', 'written_bytes=65536', 'remaining_bytes=1470464', 'blocked_events=',
                    'blocked_wait_ms=', 'process_uptime_ms=', 'first_frame_write_timeout_count=1']:
        self.assertIn(field, message)
    with patch.object(stream.os, 'write', side_effect=self.delayed_pipe(.120)):
      self.streamer.submit(bytes(stream.FRAME_BYTES))
      wait_until(lambda: self.streamer.stats.snapshot()['frames_written'] == 1)
      self.assertEqual(len(self.processes), 2)
      self.assertGreaterEqual(self.streamer._first_frame_metrics['first_frame_write_ms'], 120)

  def test_second_frame_120ms_restarts_at_regular_deadline(self):
    self.enabled = True
    with patch.object(stream.os, 'write', side_effect=self.delayed_pipe(0, .120)):
      self.streamer.start()
      wait_until(self.streamer.ready.is_set)
      self.streamer.submit(bytes(stream.FRAME_BYTES))
      wait_until(lambda: self.streamer.stats.snapshot()['frames_written'] == 1)
      metrics = self.streamer._first_frame_metrics.copy()
      self.streamer.submit(bytes(stream.FRAME_BYTES))
      wait_until(lambda: len(self.processes) == 2 and self.streamer.ready.is_set())
      self.processes[0].terminate.assert_called_once()
      self.assertEqual(self.streamer._regular_frame_write_timeout_count, 1)
      self.assertEqual(self.streamer._first_frame_write_timeout_count, 0)
      self.assertIn('first_frame=0', self.log_messages())
      self.assertIn(f"first_frame_write_ms={metrics['first_frame_write_ms']}", self.log_messages())

  def test_config_and_network_new_process_allow_first_frame_again(self):
    self.enabled = True
    with patch.object(stream.os, 'write', side_effect=self.delayed_pipe(.120)):
      self.streamer.start()
      wait_until(self.streamer.ready.is_set)
      for generation in range(3):
        wait_until(lambda expected=generation + 1: len(self.processes) == expected and self.streamer.ready.is_set())
        self.assertFalse(self.streamer._first_frame_written)
        self.assertIsNone(self.streamer._first_frame_metrics['first_frame_write_ms'])
        self.streamer.submit(bytes(stream.FRAME_BYTES))
        wait_until(lambda: self.streamer.stats.snapshot()['frames_written'] == 1)
        self.assertGreaterEqual(self.streamer._first_frame_metrics['first_frame_write_ms'], 120)
        if generation == 0:
          self.config.return_value = ScreenStreamConfig(bitrate=500)
        elif generation == 1:
          self.network.return_value = ('wlan0', '192.168.1.9')
      self.assertEqual(self.streamer._restart_count, 0)

  def test_steady_20fps_write_accounting_and_observer_timestamps(self):
    self.streamer._proc = Mock()
    self.streamer._latency = observer = Mock()
    self.streamer._process_started_at = 10.0
    with patch.object(stream.os, 'write', return_value=stream.FRAME_BYTES):
      for sequence in range(200):
        captured = 10.0 + sequence * .05
        started, completed = captured + .005, captured + .025
        times = [started, started, completed, completed] if sequence == 0 else [started, started, completed]
        with patch.object(stream.time, 'monotonic', side_effect=times):
          self.streamer._write_frame(captured, bytes(stream.FRAME_BYTES), sequence, captured + .004)
        observer.begin.assert_called_with(sequence, captured, captured + .004, started)
        observer.complete.assert_called_with(sequence, completed, success=True)
    result = self.streamer.stats.snapshot()
    self.assertEqual(result['stdin_write_syscalls'], 200)
    self.assertEqual(result['stdin_bytes'], 200 * stream.FRAME_BYTES)
    self.assertEqual(result['stdin_blocked_events'], 0)
    self.assertEqual(self.streamer._first_frame_metrics['first_frame_write_ms'], 20)
    self.assertEqual(self.streamer._first_frame_metrics['process_start_to_first_frame_complete_ms'], 25)
    self.assertEqual(stream.PIPE_WRITE_TIMEOUT, .1)
    self.assertEqual(stream.FIRST_FRAME_WRITE_TIMEOUT, .5)

  def test_pipe_timeout_reports_age_elapsed_and_remaining_bytes(self):
    self.streamer._proc = Mock()
    self.streamer._first_frame_written = True
    with patch.object(stream.time, 'monotonic', side_effect=[10.0, 10.0, 10.11]), \
         patch.object(stream.os, 'write', return_value=1):
      with self.assertRaises(stream.FrameWriteTimeout) as caught:
        self.streamer._write_frame(9.8, b'abc')
    message = str(caught.exception)
    self.assertIn('age=0.310s', message)
    self.assertIn('write_elapsed=0.110s', message)
    self.assertIn('remaining_bytes=2', message)

  def test_screen_off_during_write_is_not_a_timeout(self):
    self.streamer._proc = Mock()
    self.streamer.visible.clear()
    with self.assertRaisesRegex(stream.StreamStopped, 'screen invisible'):
      self.streamer._write_frame(time.monotonic(), b'abc')

  def test_sender_eof_stops_encoder_and_retries(self):
    self.enabled = True
    self.streamer.start()
    wait_until(self.streamer.ready.is_set)
    self.senders[0].done.is_set.return_value = True
    wait_until(lambda: len(self.senders) == 2 and self.streamer.ready.is_set())
    self.processes[0].terminate.assert_called_once()
    self.senders[0].close.assert_called_once()

  def test_missing_ffmpeg_is_retried(self):
    self.enabled = True
    with patch.object(stream.subprocess, 'Popen', side_effect=FileNotFoundError("ffmpeg")) as launch:
      self.streamer.start()
      wait_until(lambda: launch.call_count >= 2)
      self.assertFalse(self.streamer.ready.is_set())
      self.assertIn('screen stream restart: encoder start failed:', self.log_messages())

  def test_queue_keeps_only_latest_and_rejects_wrong_size(self):
    self.streamer.ready.set()
    for value in range(10):
      self.streamer.submit(bytes([value]) * stream.FRAME_BYTES)
    self.streamer.submit(b'invalid')
    self.assertEqual(self.streamer._frames.qsize(), 1)
    self.assertEqual(self.streamer._frames.get_nowait()[1], bytes([9]) * stream.FRAME_BYTES)
    self.assertEqual(self.streamer.stats.snapshot()['submitted_count'], 10)
    self.assertEqual(self.streamer.stats.snapshot()['queue_replaced_count'], 9)

  def test_partial_writes_preserve_frame(self):
    self.streamer._proc = Mock()
    parts = []

    def write(fd, data):
      parts.append(bytes(data[:3]))
      return len(parts[-1])

    with patch.object(stream.os, 'write', side_effect=write):
      self.streamer._write_frame(time.monotonic(), b'0123456789')
    self.assertEqual(b''.join(parts), b'0123456789')

  def test_blocked_pipe_times_out_and_close_is_bounded(self):
    self.enabled = True
    with patch.object(stream.os, 'write', side_effect=BlockingIOError):
      self.streamer.start()
      wait_until(self.streamer.ready.is_set)
      first = self.processes[0]
      self.streamer.submit(bytes(stream.FRAME_BYTES))
      wait_until(lambda: first.terminate.called)
      wait_until(lambda: len(self.processes) >= 2 and self.streamer.ready.is_set())
      start = time.monotonic()
      self.streamer.close()
      self.assertLess(time.monotonic() - start, 1)
      self.assertFalse(self.streamer._thread.is_alive())
      message = self.log_messages()
      self.assertIn('screen stream restart: frame write timeout age=', message)
      self.assertIn('write_elapsed=', message)
      self.assertIn('remaining_bytes=', message)

  def test_kill_if_encoder_ignores_terminate(self):
    proc = self.streamer._proc = Mock()
    proc.poll.return_value = None
    proc.wait.side_effect = [subprocess.TimeoutExpired('ffmpeg', 0.2), 0]
    self.streamer._close_process()
    proc.kill.assert_called_once()
    proc.stdin.close.assert_called_once()
    proc.stdout.close.assert_called_once()

  def test_cleanup_releases_transport_even_if_kill_wait_fails(self):
    proc = self.streamer._proc = Mock()
    sender = self.streamer._sender = Mock()
    proc.poll.return_value = None
    proc.wait.side_effect = subprocess.TimeoutExpired('ffmpeg', 0.2)
    self.streamer.ready.set()
    self.streamer.submit(bytes(stream.FRAME_BYTES))
    self.assertRaises(subprocess.TimeoutExpired, self.streamer._close_process)
    self.assertFalse(self.streamer.ready.is_set())
    self.assertTrue(self.streamer._frames.empty())
    proc.kill.assert_called_once()
    proc.stdin.close.assert_called_once()
    proc.stdout.close.assert_called_once()
    sender.stop.assert_called_once()
    sender.close.assert_called_once()

  def test_close_releases_an_idle_encoder(self):
    self.enabled = True
    self.streamer.start()
    wait_until(self.streamer.ready.is_set)
    self.streamer.close()
    self.assertFalse(self.streamer._thread.is_alive())
    self.processes[-1].terminate.assert_called_once()


class TestWifiAddress(unittest.TestCase):
  def query(self, device_type=2, state=100, mode=2, address='192.168.1.8', access_point='/ap', request_elapsed=0.0):
    from jeepney.low_level import MessageType
    responses = [
      ['/device'],
      {'DeviceType': ('u', device_type), 'State': ('u', state), 'Interface': ('s', 'wlan0'), 'Ip4Config': ('o', '/ip4')},
      {'Mode': ('u', mode), 'ActiveAccessPoint': ('o', access_point)},
      ('aa{sv}', [{'address': ('s', address)}]),
    ]
    connection = Mock()
    replies = iter([
      SimpleNamespace(body=[body], header=SimpleNamespace(message_type=MessageType.method_return)) for body in responses
    ])
    self.request_timeouts = []
    clock = 10.0

    def reply(message, timeout):
      nonlocal clock
      self.request_timeouts.append(timeout)
      clock += request_elapsed
      return next(replies)

    connection.send_and_get_reply.side_effect = reply
    with patch('jeepney.io.blocking.open_dbus_connection') as connect, patch.object(stream.time, 'monotonic', side_effect=lambda: clock):
      connect.return_value.__enter__.return_value = connection
      result = stream.wifi_address()
      connect.assert_called_once_with(bus="SYSTEM", auth_timeout=stream.NETWORK_REQUEST_TIMEOUT)
      return result

  def test_connected_wifi_address(self):
    self.assertEqual(self.query(), ('wlan0', '192.168.1.8'))

  def test_each_request_gets_fresh_timeout_budget(self):
    self.assertEqual(self.query(request_elapsed=0.1), ('wlan0', '192.168.1.8'))
    self.assertEqual(self.request_timeouts, [stream.NETWORK_REQUEST_TIMEOUT] * 4)

  def test_excludes_ethernet_cellular_disconnected_and_hotspot(self):
    for kwargs in [{'device_type': 1}, {'device_type': 8}, {'state': 30}, {'mode': 3}, {'access_point': '/'}]:
      with self.subTest(kwargs=kwargs):
        self.assertIsNone(self.query(**kwargs))

  def test_rejects_unusable_addresses(self):
    for address in ['0.0.0.0', '127.0.0.1', '224.0.0.1', '169.254.1.8']:
      with self.subTest(address=address):
        self.assertIsNone(self.query(address=address))

  def test_dbus_failure_propagates_to_worker(self):
    with patch('jeepney.io.blocking.open_dbus_connection', side_effect=OSError):
      self.assertRaises(OSError, stream.wifi_address)


class TestCommand(unittest.TestCase):
  def test_experimental_encoder_and_timestamp_options(self):
    for bitrate in [500, 1500, 3000]:
      command = stream.ffmpeg_command(ScreenStreamConfig(bitrate=bitrate))
      for flag, value in [('-preset', 'ultrafast'), ('-tune', 'zerolatency'), ('-bf', '0'),
                          ('-bufsize', f'{bitrate // 10}k'), ('-use_wallclock_as_timestamps', '1')]:
        self.assertEqual(command[command.index(flag) + 1], value)
      self.assertLess(command.index('-use_wallclock_as_timestamps'), command.index('-i'))
      self.assertNotIn('-copyts', command)
      self.assertNotIn('-start_at_zero', command)
      self.assertIn('sync-lookahead=0:rc-lookahead=0:sliced-threads=1:repeat-headers=1', command)

  def test_uses_only_pipes_and_ignores_network_settings(self):
    command = stream.ffmpeg_command()
    self.assertEqual(command[-1], 'pipe:1')
    self.assertEqual(command[command.index('-i') + 1], 'pipe:0')
    self.assertFalse(any(value in ' '.join(command) for value in ['udp://', 'localaddr', 'ttl', 'pkt_size', '239.255', '12346']))
    self.assertEqual(command, stream.ffmpeg_command(ScreenStreamConfig('239.2.3.4', 23456, 1000, 2)))

  def test_bitrate_controls_encoder(self):
    command = stream.ffmpeg_command(ScreenStreamConfig(bitrate=3000))
    for flag, value in [('-b:v', '3000k'), ('-maxrate', '3000k'), ('-bufsize', '300k')]:
      self.assertEqual(command[command.index(flag) + 1], value)


class TestMpegTsUdpSender(unittest.TestCase):
  def test_sendto_syscall_duration_and_eagain_window(self):
    with patch.object(stream.socket, 'socket') as create, patch.object(stream.os, 'set_blocking'), \
         patch.object(stream, 'cloudlog'), patch.object(stream.time, 'perf_counter', side_effect=[1, 1.00004, 2, 2.00002]):
      create.return_value.sendto.side_effect = [564, BlockingIOError(errno.EAGAIN, 'busy')]
      sender = stream.MpegTsUdpSender(Mock(), '127.0.0.1', ScreenStreamConfig())
      try:
        self.assertTrue(sender._send_datagram(bytes(564)))
        self.assertFalse(sender._send_datagram(bytes(564)))
        result = sender.stats_snapshot(time.monotonic())
        self.assertEqual(result['sendto_calls'], 2)
        self.assertEqual(result['sendto_eagain_count'], 1)
        self.assertEqual(result['sendto_avg_us'], 30)
        self.assertEqual(result['sendto_max_us'], 40)
        result = sender.stats_snapshot(time.monotonic())
        self.assertEqual(result['sendto_calls'], 0)
        self.assertEqual(result['sendto_max_us'], 0)
      finally:
        sender.close()

  def test_stdout_read_eagain_wait_window(self):
    with patch.object(stream.socket, 'socket') as create, patch.object(stream.os, 'set_blocking'), \
         patch.object(stream.os, 'read', side_effect=[BlockingIOError(), b'A' * 564, b'']), \
         patch.object(stream.MpegTsUdpSender, '_wait_readable'), patch.object(stream.MpegTsUdpSender, 'sample_outq'), \
         patch.object(stream.time, 'perf_counter', side_effect=[1, 1.003, 2, 2.00002]):
      create.return_value.sendto.return_value = 564
      sender = stream.MpegTsUdpSender(Mock(), '127.0.0.1', ScreenStreamConfig('192.168.1.1'))
      sender._run()
      self.assertIsNone(sender.error)
      result = sender.stats_snapshot(time.monotonic())
      self.assertEqual(result['stdout_read_calls'], 3)
      self.assertEqual(result['stdout_eagain_count'], 1)
      self.assertEqual(result['stdout_wait_ms'], 3)
      self.assertEqual(result['stdout_wait_max_ms'], 3)
      self.assertEqual(result['stdout_bytes'], 564)
      self.assertEqual(result['stdout_chunks'], 1)

  def test_pipe_backlog_sampling_reset_and_failure_are_nonfatal(self):
    fcntl = Mock()
    count = 0

    def ioctl(fd, request, values, mutate):
      nonlocal count
      if request == 2:
        count += 1
        if count == 3:
          raise OSError('unsupported')
        values[0] = 1000 if count == 1 else 200
      else:
        values[0] = 10

    fcntl.ioctl.side_effect = ioctl
    with patch.object(stream.socket, 'socket'), patch.object(stream.os, 'set_blocking'), \
         patch.object(stream.sys, 'platform', 'linux'), patch.dict(sys.modules, {'fcntl': fcntl, 'termios': SimpleNamespace(TIOCOUTQ=1, FIONREAD=2)}):
      sender = stream.MpegTsUdpSender(Mock(), '127.0.0.1', ScreenStreamConfig())
      try:
        with patch.object(stream.time, 'monotonic', return_value=1):
          sender.sample_outq()
          sender.sample_outq()
        self.assertEqual(fcntl.ioctl.call_count, 2)
        result = sender.stats_snapshot(1.05)
        self.assertEqual(result['stdout_pipe_available_window_peak'], 1000)
        with patch.object(stream.time, 'monotonic', return_value=1.2):
          sender.sample_outq()
        result = sender.stats_snapshot(1.25)
        self.assertEqual(result['stdout_pipe_available_current'], 200)
        self.assertEqual(result['stdout_pipe_available_window_peak'], 200)
        with patch.object(stream.time, 'monotonic', return_value=1.4):
          sender.sample_outq()
        result = sender.stats_snapshot(1.45)
        self.assertIsNone(result['stdout_pipe_available_current'])
        self.assertIsNone(result['stdout_pipe_available_window_peak'])
        self.assertIsNone(sender.error)
      finally:
        sender.close()

  def setUp(self):
    log_patch = patch.object(stream, 'cloudlog')
    self.cloudlog = log_patch.start()
    self.addCleanup(log_patch.stop)

  def run_sender(self, chunks, config=stream.DEFAULT_CONFIG):
    sock = Mock()
    sock.getsockopt.return_value = 32768
    sock.sendto.side_effect = lambda payload, destination: len(payload)
    with patch.object(stream.socket, 'socket', return_value=sock) as create, \
         patch.object(stream.os, 'set_blocking'), patch.object(stream.os, 'read', side_effect=[*chunks, b'']):
      sender = stream.MpegTsUdpSender(Mock(), '192.168.4.38', config)
      sender.start()
      try:
        self.assertTrue(sender.done.wait(2))
      finally:
        sender.close()
      self.assertIsNone(sender.error)
      self.assertFalse(sender._thread.is_alive())
    create.assert_called_once_with(stream.socket.AF_INET, stream.socket.SOCK_DGRAM, stream.socket.IPPROTO_UDP)
    if stream.ipaddress.IPv4Address(config.address).is_multicast:
      self.assertEqual(sender.mode, 'multicast')
      sock.setsockopt.assert_any_call(stream.socket.IPPROTO_IP, stream.socket.IP_MULTICAST_IF, stream.socket.inet_aton('192.168.4.38'))
      sock.setsockopt.assert_any_call(stream.socket.IPPROTO_IP, stream.socket.IP_MULTICAST_TTL, config.ttl)
      self.assertEqual(sock.setsockopt.call_count, 2)
      sock.bind.assert_not_called()
    else:
      self.assertEqual(sender.mode, 'unicast')
      sock.bind.assert_called_once_with(('192.168.4.38', 0))
      sock.setsockopt.assert_not_called()
    sock.getsockopt.assert_called_once_with(stream.socket.SOL_SOCKET, stream.socket.SO_SNDBUF)
    self.assertEqual(sender.socket_sndbuf, 32768)
    sock.connect.assert_not_called()
    sock.setblocking.assert_called_once_with(False)
    sock.settimeout.assert_not_called()
    sock.close.assert_called()
    calls = sock.sendto.call_args_list
    self.assertTrue(all(call.args[1] == (config.address, config.port) for call in calls))
    packets = [call.args[0] for call in calls]
    self.assertTrue(all(0 < len(packet) <= 1316 and len(packet) % 188 == 0 for packet in packets))
    self.assertTrue(all(len(packet) == sender.payload_size for packet in packets[:-1]))
    self.last_datagram_count = len(packets)
    self.assertEqual(sender.datagrams_sent, len(packets))
    self.assertEqual(sender.datagrams_dropped, 0)
    self.assertEqual(sender.bytes_sent, sum(map(len, packets)))
    return b''.join(packets)

  def test_irregular_reads_preserve_all_ts_bytes(self):
    ts = b''.join(b'\x47' + bytes([i % 256]) * 187 for i in range(200))
    chunks, offset, sizes = [], 0, [1, 100, 187, 188, 500, 4096, 2]
    while offset < len(ts):
      size = sizes[len(chunks) % len(sizes)]
      chunks.append(ts[offset:offset + size])
      offset += size
    for address in ['239.10.20.30', '192.168.4.44']:
      with self.subTest(address=address):
        self.assertEqual(self.run_sender(chunks, ScreenStreamConfig(address, 15000, 3000, 2)), ts)

  def test_all_candidate_payloads_preserve_sequence_and_reduce_send_calls(self):
    ts = b''.join(b'\x47' + bytes([index % 256]) * 187 for index in range(211))
    chunks = [ts[offset:offset + 997] for offset in range(0, len(ts), 997)]
    self.assertEqual(stream.UNICAST_TS_PACKETS_PER_DATAGRAM, 3)
    for packets in [1, 2, 3, 7]:
      with self.subTest(packets=packets), patch.object(stream, 'UNICAST_TS_PACKETS_PER_DATAGRAM', packets):
        self.assertEqual(self.run_sender(chunks, ScreenStreamConfig('192.168.4.44')), ts)
        self.assertEqual(self.last_datagram_count, (211 + packets - 1) // packets)
    self.assertEqual(self.run_sender(chunks), ts)
    self.assertEqual(self.last_datagram_count, 31)

  def test_stdout_observer_is_optional_and_preserves_chunks(self):
    observed = []
    chunks = [b'A' * 200, b'B' * 364]
    with patch.object(stream.socket, 'socket') as create, patch.object(stream.os, 'set_blocking'), \
         patch.object(stream.os, 'read', side_effect=[*chunks, b'']):
      create.return_value.sendto.return_value = 564
      sender = stream.MpegTsUdpSender(Mock(), '192.168.4.38', ScreenStreamConfig('192.168.4.44'), observed.append)
      sender.start()
      try:
        self.assertTrue(sender.done.wait(2))
      finally:
        sender.close()
      self.assertIsNone(sender.error)
      self.assertEqual(observed, chunks)
      self.assertEqual((sender.stdout_bytes, sender.stdout_chunks), (564, 2))

  def test_sender_window_statistics_ratios_rates_and_reset(self):
    with patch.object(stream.socket, 'socket'), patch.object(stream.os, 'set_blocking'):
      sender = stream.MpegTsUdpSender(Mock(), '127.0.0.1', ScreenStreamConfig())
      try:
        sender._stats_started = 0
        sender.datagrams_sent, sender.datagrams_dropped = 90, 10
        sender.bytes_sent, sender.stdout_bytes, sender.stdout_chunks = 9000, 10000, 20
        sender.socket_outq_current, sender.socket_outq_peak, sender.socket_outq_window_peak = 100, 1000, 500
        first = sender.stats_snapshot(5)
        for key, value in {'sender_drop_ratio_pct': 10, 'sender_drop_ratio_window_pct': 10, 'stdout_bytes_per_sec': 2000,
                            'stdout_chunks_delta': 20, 'datagrams_per_sec': 18, 'sender_drops_per_sec': 2,
                            'socket_outq_window_peak': 500}.items():
          self.assertEqual(first[key], value)
        sender.datagrams_sent += 10
        sender.socket_outq_window_peak = 200
        second = sender.stats_snapshot(10)
        self.assertEqual(second['sender_datagrams_delta'], 10)
        self.assertEqual(second['sender_drops_delta'], 0)
        self.assertEqual(second['sender_drop_ratio_pct'], round(1000 / 110, 4))
        self.assertEqual(second['sender_drop_ratio_window_pct'], 0)
        self.assertEqual(second['socket_outq_peak'], 1000)
        self.assertEqual(second['socket_outq_window_peak'], 200)
        empty = sender.stats_snapshot(15)
        self.assertEqual(empty['datagrams_per_sec'], 0)
        self.assertEqual(empty['sender_drop_ratio_window_pct'], 0)
        self.assertIsNone(empty['socket_outq_window_peak'])
      finally:
        sender.close()

  def test_outq_ioctl_is_rate_limited_even_when_stats_sample(self):
    fcntl = Mock()
    with patch.object(stream.socket, 'socket'), patch.object(stream.os, 'set_blocking'), \
         patch.object(stream.sys, 'platform', 'linux'), patch.dict(sys.modules, {'fcntl': fcntl, 'termios': SimpleNamespace(TIOCOUTQ=0x5411)}):
      sender = stream.MpegTsUdpSender(Mock(), '127.0.0.1', ScreenStreamConfig())
      try:
        with patch.object(stream.time, 'monotonic', return_value=1.0):
          for _ in range(100):
            sender.sample_outq()
        self.assertEqual(fcntl.ioctl.call_count, 1)
        with patch.object(stream.time, 'monotonic', return_value=1.099):
          sender.sample_outq()
        self.assertEqual(fcntl.ioctl.call_count, 1)
        with patch.object(stream.time, 'monotonic', return_value=1.101):
          sender.sample_outq()
        self.assertEqual(fcntl.ioctl.call_count, 2)
      finally:
        sender.close()

  def test_unicast_ignores_multicast_ttl(self):
    for ttl in [1, 255]:
      with self.subTest(ttl=ttl):
        config = ScreenStreamConfig('10.0.0.20', 12346, 1000, ttl)
        self.assertEqual(self.run_sender([b'A' * 1316], config), b'A' * 1316)
        self.assertEqual(stream.ffmpeg_command(config), stream.ffmpeg_command())

  def test_unicast_bind_failure_closes_socket(self):
    with patch.object(stream.socket, 'socket') as create:
      create.return_value.bind.side_effect = OSError(errno.EADDRNOTAVAIL, '送信元アドレス消失')
      self.assertRaises(OSError, stream.MpegTsUdpSender, Mock(), '192.168.4.38', ScreenStreamConfig('192.168.4.44'))
      create.return_value.close.assert_called_once()
      create.return_value.setsockopt.assert_not_called()

  def test_unicast_sends_three_ts_packets_without_waiting_for_seven(self):
    read_fd, write_fd = os.pipe()
    self.addCleanup(os.close, write_fd)
    with os.fdopen(read_fd, 'rb', buffering=0) as stdout, patch.object(stream.socket, 'socket') as create:
      create.return_value.sendto.side_effect = lambda payload, destination: len(payload)
      sender = stream.MpegTsUdpSender(stdout, '127.0.0.1', ScreenStreamConfig('192.168.4.44'))
      sender.start()
      try:
        os.write(write_fd, b'A' * 188)
        time.sleep(.02)
        create.return_value.sendto.assert_not_called()
        os.write(write_fd, b'B' * 376)
        wait_until(lambda: sender.datagrams_sent == 1)
        create.return_value.sendto.assert_called_once_with(b'A' * 188 + b'B' * 376, ('192.168.4.44', 12346))
      finally:
        sender.close()

  def test_posix_pipe_wait_is_readiness_driven(self):
    with patch.object(stream.socket, 'socket'), patch.object(stream.os, 'set_blocking'):
      stdout = Mock()
      stdout.fileno.return_value = 12
      sender = stream.MpegTsUdpSender(stdout, '127.0.0.1', ScreenStreamConfig())
      try:
        with patch.object(stream.os, 'name', 'posix'), patch.object(stream.select, 'select', return_value=([12], [], [])) as wait:
          sender._wait_readable()
          wait.assert_called_once_with([12], [], [], .05)
      finally:
        sender.close()

  def test_linux_outq_sampling_and_failure_are_nonfatal(self):
    fcntl = Mock()
    values = iter([1000, 100, OSError(errno.ENOTTY, '未対応')])

    def ioctl(fd, request, output, mutate):
      value = next(values)
      if isinstance(value, Exception):
        raise value
      output[0] = value

    fcntl.ioctl.side_effect = ioctl
    with patch.object(stream.socket, 'socket'), patch.object(stream.os, 'set_blocking'), \
         patch.object(stream.sys, 'platform', 'linux'), patch.dict(sys.modules, {'fcntl': fcntl, 'termios': SimpleNamespace(TIOCOUTQ=0x5411)}):
      sender = stream.MpegTsUdpSender(Mock(), '127.0.0.1', ScreenStreamConfig())
      try:
        with patch.object(stream.time, 'monotonic', side_effect=[1.0, 1.2, 1.4]):
          sender.sample_outq()
          sender.sample_outq()
          self.assertEqual(sender.socket_outq_current, 100)
          self.assertEqual(sender.socket_outq_peak, 1000)
          sender.sample_outq()
        self.assertIsNone(sender.socket_outq_current)
        self.assertEqual(sender.socket_outq_peak, 1000)
        self.assertIsNone(sender.error)
      finally:
        sender.close()

  def test_eof_drops_only_incomplete_packet(self):
    for address in ['239.255.42.99', '192.168.4.44']:
      with self.subTest(address=address):
        config = ScreenStreamConfig(address)
        ts = b'\x47' * (188 * 9)
        for tail in [b'', b'X', b'X' * 187]:
          with self.subTest(tail=len(tail)):
            self.assertEqual(self.run_sender([ts + tail], config), ts)
        self.assertEqual(self.run_sender([b'X' * 187], config), b'')

  def test_idle_stdout_close_is_bounded(self):
    read_fd, write_fd = os.pipe()
    self.addCleanup(os.close, write_fd)
    with os.fdopen(read_fd, 'rb', buffering=0) as stdout, patch.object(stream.socket, 'socket') as create:
      sender = stream.MpegTsUdpSender(stdout, '127.0.0.1', ScreenStreamConfig())
      sender.start()
      start = time.monotonic()
      sender.close()
      self.assertLess(time.monotonic() - start, 0.5)
      self.assertFalse(sender._thread.is_alive())
      self.assertIsNone(sender.error)
      create.return_value.close.assert_called()

  def test_socket_setup_failure_closes_socket(self):
    with patch.object(stream.socket, 'socket') as create:
      create.return_value.setsockopt.side_effect = OSError('インターフェース消失')
      self.assertRaises(OSError, stream.MpegTsUdpSender, Mock(), '192.168.4.38', ScreenStreamConfig())
      create.return_value.close.assert_called_once()

  def test_coalesces_until_full_and_does_not_flush_on_stop(self):
    read_fd, write_fd = os.pipe()
    self.addCleanup(os.close, write_fd)
    with os.fdopen(read_fd, 'rb', buffering=0) as stdout, patch.object(stream.socket, 'socket') as create:
      create.return_value.sendto.side_effect = lambda payload, destination: len(payload)
      sender = stream.MpegTsUdpSender(stdout, '127.0.0.1', ScreenStreamConfig())
      sender.start()
      try:
        os.write(write_fd, b'A' * 188)
        time.sleep(0.03)
        create.return_value.sendto.assert_not_called()
        os.write(write_fd, b'B' * (1316 - 188))
        wait_until(lambda: sender.datagrams_sent == 1)
        self.assertEqual(create.return_value.sendto.call_args.args[0], b'A' * 188 + b'B' * (1316 - 188))
        os.write(write_fd, b'C' * 188)
        time.sleep(0.03)
      finally:
        sender.close()
      self.assertEqual(sender.datagrams_sent, 1)

  def test_transient_drop_removes_exactly_one_datagram(self):
    for address in ['239.255.42.99', '192.168.4.44']:
      with self.subTest(address=address):
        config = ScreenStreamConfig(address)
        size = 1316 if address.startswith('239.') else 564
        payloads = [bytes([i]) * size for i in range(3)]
        with patch.object(stream.socket, 'socket') as create, patch.object(stream.os, 'set_blocking'), \
             patch.object(stream.os, 'read', side_effect=[b''.join(payloads), b'']):
          create.return_value.sendto.side_effect = [size, OSError(errno.ENOBUFS, '送信バッファ不足'), size]
          sender = stream.MpegTsUdpSender(Mock(), '127.0.0.1', config)
          sender.start()
          try:
            self.assertTrue(sender.done.wait(2))
          finally:
            sender.close()
          self.assertIsNone(sender.error)
          self.assertEqual([call.args[0] for call in create.return_value.sendto.call_args_list], payloads)
          self.assertEqual((sender.datagrams_sent, sender.datagrams_dropped, sender.bytes_sent), (2, 1, size * 2))

  def test_drop_warning_is_aggregated_and_rate_limited(self):
    with patch.object(stream.socket, 'socket') as create, patch.object(stream.os, 'set_blocking'):
      create.return_value.sendto.side_effect = OSError(errno.ENOBUFS, '送信バッファ不足')
      sender = stream.MpegTsUdpSender(Mock(), '127.0.0.1', ScreenStreamConfig())
      try:
        with patch.object(stream.time, 'monotonic', return_value=1.0):
          for _ in range(100):
            self.assertFalse(sender._send_datagram(bytes(1316)))
        self.assertEqual(sender.datagrams_dropped, 100)
        self.assertEqual(self.cloudlog.warning.call_count, 1)
        with patch.object(stream.time, 'monotonic', return_value=6.0):
          sender._send_datagram(bytes(1316))
        self.assertEqual(self.cloudlog.warning.call_count, 2)
        self.assertIn('dropped=101 sent=0', self.cloudlog.warning.call_args.args[0])
        self.assertIn(f'last_errno={errno.ENOBUFS}', self.cloudlog.warning.call_args.args[0])
      finally:
        sender.close()

  def test_platform_transient_errnos_and_fatal_errors(self):
    for address in ['239.255.42.99', '192.168.4.44']:
      with self.subTest(address=address):
        config = ScreenStreamConfig(address)
        with patch.object(stream.socket, 'socket') as create, patch.object(stream.os, 'set_blocking'):
          sender = stream.MpegTsUdpSender(Mock(), '127.0.0.1', config)
          try:
            for code in stream.TRANSIENT_SEND_ERRNOS:
              create.return_value.sendto.side_effect = OSError(code, '一時障害')
              self.assertFalse(sender._send_datagram(bytes(1316)))
            for code in [errno.ENETDOWN, errno.ENODEV, errno.EADDRNOTAVAIL]:
              error = OSError(code, '致命的な障害')
              create.return_value.sendto.side_effect = error
              with self.assertRaises(OSError) as caught:
                sender._send_datagram(bytes(1316))
              self.assertIs(caught.exception, error)
          finally:
            sender.close()

  def test_fatal_socket_and_read_error_are_reported(self):
    for address in ['239.255.42.99', '192.168.4.44']:
      with self.subTest(address=address):
        config = ScreenStreamConfig(address)
        for read_error in [False, True]:
          with self.subTest(read_error=read_error), patch.object(stream.socket, 'socket') as create, \
               patch.object(stream.os, 'set_blocking'), patch.object(stream.os, 'read') as read:
            read.side_effect = OSError('パイプ障害') if read_error else [b'\x47' * 1316]
            create.return_value.sendto.side_effect = OSError(errno.ENETDOWN, 'ネットワーク停止')
            sender = stream.MpegTsUdpSender(Mock(), '192.168.4.38', config)
            sender.start()
            try:
              self.assertTrue(sender.done.wait(2))
            finally:
              sender.close()
            self.assertIsInstance(sender.error, OSError)
            create.return_value.close.assert_called()


class TestCachedQueriesAndStats(unittest.TestCase):
  def test_disabled_query_discards_inflight_result_and_resumes_fresh(self):
    entered, release = threading.Event(), threading.Event()
    calls = []

    def query():
      calls.append(1)
      if len(calls) == 1:
        entered.set()
        release.wait(2)
        return '古い接続'
      return '新しい接続'

    monitor = stream.CachedQuery(query, lambda value: .01, 'test-cached-query')
    monitor.start()
    try:
      time.sleep(.02)
      self.assertEqual(calls, [])
      monitor.set_active(True)
      self.assertTrue(entered.wait(1))
      started = time.monotonic()
      self.assertFalse(monitor.snapshot().checked)
      self.assertLess(time.monotonic() - started, .1)
      monitor.set_active(False)
      release.set()
      time.sleep(.03)
      self.assertFalse(monitor.snapshot().checked)
      self.assertEqual(len(calls), 1)
      monitor.set_active(True)
      wait_until(lambda: monitor.snapshot().checked)
      self.assertEqual(monitor.snapshot().value, '新しい接続')
    finally:
      release.set()
      monitor.close()
    self.assertFalse(monitor._thread.is_alive())

  def test_monitor_retains_successful_none_and_tuple_across_errors(self):
    query = Mock(return_value=('wlan0', '192.168.4.38'))
    monitor = stream.CachedQuery(query, lambda value: .005, 'test-cached-query')
    monitor.start()
    monitor.set_active(True)
    try:
      wait_until(lambda: monitor.snapshot().checked)
      query.side_effect = TimeoutError('待機期限')
      wait_until(lambda: monitor.snapshot().failures >= 2)
      self.assertEqual(monitor.snapshot().value, query.return_value)
      query.side_effect = None
      query.return_value = None
      wait_until(lambda: monitor.snapshot().value is None)
      generation = monitor.snapshot().generation
      query.side_effect = TimeoutError('待機期限')
      wait_until(lambda: monitor.snapshot().failures >= 1)
      state = monitor.snapshot()
      self.assertTrue(state.checked)
      self.assertIsNone(state.value)
      self.assertGreater(state.generation, generation)
    finally:
      monitor.close()

  def test_stats_average_maximum_and_window_reset(self):
    stats = stream.LatencyStats()
    stats.record({'capture_count': 1}, capture=.01)
    stats.record({'capture_count': 1}, capture=.03)
    window = stats.snapshot(reset=True)
    self.assertEqual(window['capture_count'], 2)
    self.assertEqual(window['capture_avg_ms'], 20)
    self.assertEqual(window['capture_max_ms'], 30)
    self.assertEqual(stats.snapshot()['capture_count'], 0)
    self.assertEqual(stats.snapshot()['capture_avg_ms'], 0)


class TestStreamConfig(unittest.TestCase):
  def test_native_parameter_defaults_match_config(self):
    header = (Path(__file__).resolve().parents[5] / 'openpilot/common/params_keys.h').read_text(encoding='utf-8')
    defaults = ScreenStreamConfig()
    for field, key in PARAM_KEYS.items():
      declaration = next(line for line in header.splitlines() if f'"{key}"' in line)
      self.assertIn(f'"{getattr(defaults, field)}"', declaration)

  def test_default_bitrate_only_applies_when_not_saved(self):
    self.assertEqual(ScreenStreamConfig().bitrate, 1000)
    params = Mock()
    params.get.return_value = None
    self.assertEqual(ScreenStreamConfig.from_params(params).bitrate, 1000)
    params.get.side_effect = {'ScreenStreamBitrate': 1500}.get
    self.assertEqual(ScreenStreamConfig.from_params(params).bitrate, 1500)
    params.put.assert_not_called()

  def test_missing_keys_use_defaults_and_saved_values_are_loaded(self):
    params = Mock()
    params.get.return_value = None
    self.assertEqual(ScreenStreamConfig.from_params(params), ScreenStreamConfig())
    values = dict(zip(PARAM_KEYS.values(), ['239.10.20.30', 1234, 2500, 2], strict=True))
    params.get.side_effect = values.get
    self.assertEqual(ScreenStreamConfig.from_params(params), ScreenStreamConfig('239.10.20.30', 1234, 2500, 2))

  def test_accepts_boundaries_and_normalizes_whitespace(self):
    for field, values in {'address': ['192.168.4.44', '10.0.0.20', '224.0.1.0', '239.255.255.255'], 'port': ['1', '65535'],
                          'bitrate': ['250', '8000'], 'ttl': ['1', '255']}.items():
      for value in values:
        with self.subTest(field=field, value=value):
          self.assertEqual(str(parse_stream_setting(field, ' ' + value + ' ')), value)

  def test_rejects_invalid_addresses_and_url_injection(self):
    for address in ['0.0.0.0', '0.1.2.3', '127.0.0.1', '127.255.255.254', '169.254.1.2', '224.0.0.1',
                    '240.0.0.1', '255.255.255.255', 'ff02::1', '::1', 'udp://192.168.4.44', '192.168.4.44:12346',
                    '192.168.4.44?ttl=255', '239.1.2.3:1234', '239.1.2.3?ttl=255', 'example.com', '']:
      with self.subTest(address=address):
        self.assertRaises(ValueError, parse_stream_setting, 'address', address)

  def test_rejects_invalid_numbers(self):
    for field, values in {'port': ['0', '65536'], 'bitrate': ['249', '8001'], 'ttl': ['0', '256']}.items():
      for value in values + ['-1', '1.5', '１', '1&ttl=8', '']:
        with self.subTest(field=field, value=value):
          self.assertRaises(ValueError, parse_stream_setting, field, value)

  def test_saved_invalid_settings_are_not_silently_sent(self):
    for values in [{'port': True}, {'ttl': 0}, {'bitrate': '3000'}, {'address': '0.0.0.0'}]:
      with self.subTest(values=values):
        self.assertRaises(ValueError, ScreenStreamConfig, **values)


if __name__ == '__main__':
  unittest.main()
