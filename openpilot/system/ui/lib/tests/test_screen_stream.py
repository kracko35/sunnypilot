"""配信の排他・復旧・遅延制限を実機なしで検証する。"""

import errno
import os
import queue
from contextlib import contextmanager
from dataclasses import replace
import subprocess
import time
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

  def test_repeated_network_timeouts_do_not_restart_or_spam(self):
    self.enabled = True
    with patch.object(stream, 'NETWORK_INTERVAL_CONNECTED', 0.0), \
         patch.object(self.streamer._frames, 'get', side_effect=queue.Empty):
      self.streamer.start()
      wait_until(self.streamer.ready.is_set)
      self.network.side_effect = TimeoutError('D-Bus待機期限')
      wait_until(lambda: self.streamer._network_query_failures >= 100)
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
      self.streamer._frames.put_nowait((time.monotonic() - stream.FRAME_MAX_AGE - 1, bytes(stream.FRAME_BYTES)))
      wait_until(self.streamer._frames.empty)
      # 次の新鮮なフレームまで処理できれば、古いフレームによる書き込みも再起動もない。
      self.streamer.submit(bytes(stream.FRAME_BYTES))
      wait_until(lambda: write.call_count == 1)
      self.assertEqual(len(self.processes), 1)
      self.assertEqual(self.streamer._restart_count, 0)

  def test_pipe_deadline_starts_at_write_not_capture(self):
    self.streamer._proc = Mock()
    with patch.object(stream.time, 'monotonic', side_effect=[10.0, 10.0, 10.4]), \
         patch.object(stream.os, 'write', side_effect=[1, 2]) as write:
      self.streamer._write_frame(9.8, b'abc')
    self.assertEqual(write.call_count, 2)

  def test_pipe_timeout_reports_age_elapsed_and_remaining_bytes(self):
    self.streamer._proc = Mock()
    with patch.object(stream.time, 'monotonic', side_effect=[10.0, 10.0, 10.51]), \
         patch.object(stream.os, 'write', return_value=1):
      with self.assertRaises(stream.FrameWriteTimeout) as caught:
        self.streamer._write_frame(9.8, b'abc')
    message = str(caught.exception)
    self.assertIn('age=0.710s', message)
    self.assertIn('write_elapsed=0.510s', message)
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
  def test_uses_only_pipes_and_ignores_network_settings(self):
    command = stream.ffmpeg_command()
    self.assertEqual(command[-1], 'pipe:1')
    self.assertEqual(command[command.index('-i') + 1], 'pipe:0')
    self.assertFalse(any(value in ' '.join(command) for value in ['udp://', 'localaddr', 'ttl', 'pkt_size', '239.255', '12346']))
    self.assertEqual(command, stream.ffmpeg_command(ScreenStreamConfig('239.2.3.4', 23456, 1500, 2)))

  def test_bitrate_controls_encoder(self):
    command = stream.ffmpeg_command(ScreenStreamConfig(bitrate=3000))
    for flag, value in [('-b:v', '3000k'), ('-maxrate', '3000k'), ('-bufsize', '1000k')]:
      self.assertEqual(command[command.index(flag) + 1], value)


class TestMpegTsUdpSender(unittest.TestCase):
  def setUp(self):
    log_patch = patch.object(stream, 'cloudlog')
    self.cloudlog = log_patch.start()
    self.addCleanup(log_patch.stop)

  def run_sender(self, chunks, config=stream.DEFAULT_CONFIG):
    sock = Mock()
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
    sock.connect.assert_not_called()
    sock.setblocking.assert_called_once_with(False)
    sock.settimeout.assert_not_called()
    sock.close.assert_called()
    calls = sock.sendto.call_args_list
    self.assertTrue(all(call.args[1] == (config.address, config.port) for call in calls))
    packets = [call.args[0] for call in calls]
    self.assertTrue(all(0 < len(packet) <= 1316 and len(packet) % 188 == 0 for packet in packets))
    self.assertTrue(all(len(packet) == 1316 for packet in packets[:-1]))
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

  def test_unicast_ignores_multicast_ttl(self):
    for ttl in [1, 255]:
      with self.subTest(ttl=ttl):
        config = ScreenStreamConfig('10.0.0.20', 12346, 1500, ttl)
        self.assertEqual(self.run_sender([b'A' * 1316], config), b'A' * 1316)
        self.assertEqual(stream.ffmpeg_command(config), stream.ffmpeg_command())

  def test_unicast_bind_failure_closes_socket(self):
    with patch.object(stream.socket, 'socket') as create:
      create.return_value.bind.side_effect = OSError(errno.EADDRNOTAVAIL, '送信元アドレス消失')
      self.assertRaises(OSError, stream.MpegTsUdpSender, Mock(), '192.168.4.38', ScreenStreamConfig('192.168.4.44'))
      create.return_value.close.assert_called_once()
      create.return_value.setsockopt.assert_not_called()

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
        payloads = [bytes([i]) * 1316 for i in range(3)]
        with patch.object(stream.socket, 'socket') as create, patch.object(stream.os, 'set_blocking'), \
             patch.object(stream.os, 'read', side_effect=[b''.join(payloads), b'']):
          create.return_value.sendto.side_effect = [1316, OSError(errno.ENOBUFS, '送信バッファ不足'), 1316]
          sender = stream.MpegTsUdpSender(Mock(), '127.0.0.1', config)
          sender.start()
          try:
            self.assertTrue(sender.done.wait(2))
          finally:
            sender.close()
          self.assertIsNone(sender.error)
          self.assertEqual([call.args[0] for call in create.return_value.sendto.call_args_list], payloads)
          self.assertEqual((sender.datagrams_sent, sender.datagrams_dropped, sender.bytes_sent), (2, 1, 2632))

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


class TestStreamConfig(unittest.TestCase):
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
