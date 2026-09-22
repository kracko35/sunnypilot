"""配信の排他・復旧・遅延制限を実機なしで検証する。"""

import subprocess
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from openpilot.system.ui.lib import screen_stream as stream
from openpilot.system.ui.lib.screen_stream_config import PARAM_KEYS, ScreenStreamConfig, parse_stream_setting


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

    def process(*args, **kwargs):
      self.commands.append(args[0])
      proc = Mock()
      proc.poll.return_value = None
      proc.stdin.fileno.return_value = 42
      self.processes.append(proc)
      return proc

    for target, kwargs in [
      ('subprocess.Popen', {'side_effect': process}), ('os.set_blocking', {}),
      ('select.select', {'return_value': ([], [], [])}),
      ('NETWORK_INTERVAL', {'new': 0.02}), ('RETRY_INTERVAL', {'new': 0.03}),
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

  def test_disabled_has_no_network_or_encoder(self):
    self.streamer.start()
    time.sleep(0.15)
    self.network.assert_not_called()
    self.config.assert_not_called()
    self.assertEqual(self.processes, [])

  def test_live_settings_restart_once_with_new_destination(self):
    self.enabled = True
    self.streamer.start()
    wait_until(self.streamer.ready.is_set)
    self.config.return_value = ScreenStreamConfig('239.10.20.30', 15000, 3000, 2)
    wait_until(lambda: len(self.processes) == 2 and self.streamer.ready.is_set())
    self.processes[0].terminate.assert_called_once()
    self.assertEqual(self.commands[-1][-1], 'udp://239.10.20.30:15000?pkt_size=1316&ttl=2&localaddr=192.168.1.8')
    self.assertEqual(self.commands[-1][self.commands[-1].index('-b:v') + 1], '3000k')
    time.sleep(0.1)
    self.assertEqual(len(self.processes), 2)

  def test_invalid_saved_settings_stop_until_corrected(self):
    self.enabled = True
    self.streamer.start()
    wait_until(self.streamer.ready.is_set)
    self.config.side_effect = ValueError('設定破損')
    wait_until(lambda: not self.streamer.ready.is_set())
    self.processes[0].terminate.assert_called_once()
    self.config.side_effect = None
    wait_until(lambda: len(self.processes) == 2 and self.streamer.ready.is_set())

  def test_settings_saved_while_disabled_apply_on_enable(self):
    self.config.return_value = ScreenStreamConfig(port=23456)
    self.streamer.start()
    self.enabled = True
    wait_until(self.streamer.ready.is_set)
    self.assertIn(':23456?', self.commands[0][-1])

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
    self.network.return_value = ('wlan0', '192.168.2.8')
    wait_until(lambda: len(self.processes) == 2 and self.streamer.ready.is_set())
    self.network.return_value = ('wlan1', '192.168.2.9')
    wait_until(lambda: len(self.processes) == 3)
    self.enabled = False
    wait_until(lambda: not self.streamer.ready.is_set())
    self.processes[-1].terminate.assert_called_once()

  def test_screen_off_stops_and_wake_restarts(self):
    self.enabled = True
    self.streamer.start()
    wait_until(self.streamer.ready.is_set)
    self.streamer.visible.clear()
    wait_until(lambda: not self.streamer.ready.is_set())
    self.streamer.visible.set()
    wait_until(lambda: len(self.processes) == 2 and self.streamer.ready.is_set())

  def test_encoder_exit_restarts(self):
    self.enabled = True
    self.streamer.start()
    wait_until(self.streamer.ready.is_set)
    self.processes[-1].poll.return_value = 1
    wait_until(lambda: len(self.processes) == 2 and self.streamer.ready.is_set())

  def test_network_error_stops_encoder(self):
    self.enabled = True
    self.streamer.start()
    wait_until(self.streamer.ready.is_set)
    self.network.side_effect = OSError("D-Bus切断")
    wait_until(lambda: not self.streamer.ready.is_set())
    self.processes[0].terminate.assert_called_once()
    self.network.side_effect = None
    wait_until(lambda: len(self.processes) == 2 and self.streamer.ready.is_set())

  def test_missing_ffmpeg_is_retried(self):
    self.enabled = True
    with patch.object(stream.subprocess, 'Popen', side_effect=FileNotFoundError("ffmpeg")) as launch:
      self.streamer.start()
      wait_until(lambda: launch.call_count >= 2)
      self.assertFalse(self.streamer.ready.is_set())

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

  def test_kill_if_encoder_ignores_terminate(self):
    proc = self.streamer._proc = Mock()
    proc.poll.return_value = None
    proc.wait.side_effect = [subprocess.TimeoutExpired('ffmpeg', 0.2), 0]
    self.streamer._close_process()
    proc.kill.assert_called_once()
    proc.stdin.close.assert_called_once()

  def test_close_releases_an_idle_encoder(self):
    self.enabled = True
    self.streamer.start()
    wait_until(self.streamer.ready.is_set)
    self.streamer.close()
    self.assertFalse(self.streamer._thread.is_alive())
    self.processes[-1].terminate.assert_called_once()


class TestWifiAddress(unittest.TestCase):
  def query(self, device_type=2, state=100, mode=2, address='192.168.1.8', access_point='/ap'):
    from jeepney.low_level import MessageType
    responses = [
      ['/device'],
      {'DeviceType': ('u', device_type), 'State': ('u', state), 'Interface': ('s', 'wlan0'), 'Ip4Config': ('o', '/ip4')},
      {'Mode': ('u', mode), 'ActiveAccessPoint': ('o', access_point)},
      ('aa{sv}', [{'address': ('s', address)}]),
    ]
    connection = Mock()
    connection.send_and_get_reply.side_effect = [
      SimpleNamespace(body=[body], header=SimpleNamespace(message_type=MessageType.method_return)) for body in responses
    ]
    with patch('jeepney.io.blocking.open_dbus_connection') as connect:
      connect.return_value.__enter__.return_value = connection
      return stream.wifi_address()

  def test_connected_wifi_address(self):
    self.assertEqual(self.query(), ('wlan0', '192.168.1.8'))

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
  def test_binds_to_wifi_and_limits_multicast(self):
    command = stream.ffmpeg_command('192.168.1.8')
    self.assertEqual(command[-1], 'udp://239.255.42.99:12346?pkt_size=1316&ttl=1&localaddr=192.168.1.8')
    self.assertRaises(ValueError, stream.ffmpeg_command, 'host?ttl=255')


class TestStreamConfig(unittest.TestCase):
  def test_missing_keys_use_defaults_and_saved_values_are_loaded(self):
    params = Mock()
    params.get.return_value = None
    self.assertEqual(ScreenStreamConfig.from_params(params), ScreenStreamConfig())
    values = dict(zip(PARAM_KEYS.values(), ['239.10.20.30', 1234, 2500, 2], strict=True))
    params.get.side_effect = values.get
    self.assertEqual(ScreenStreamConfig.from_params(params), ScreenStreamConfig('239.10.20.30', 1234, 2500, 2))

  def test_accepts_boundaries_and_normalizes_whitespace(self):
    for field, values in {'address': ['224.0.1.0', '239.255.255.255'], 'port': ['1', '65535'],
                          'bitrate': ['250', '8000'], 'ttl': ['1', '255']}.items():
      for value in values:
        with self.subTest(field=field, value=value):
          self.assertEqual(str(parse_stream_setting(field, ' ' + value + ' ')), value)

  def test_rejects_invalid_addresses_and_url_injection(self):
    for address in ['127.0.0.1', '192.168.1.20', '224.0.0.1', '240.0.0.1', '255.255.255.255', 'ff02::1',
                    '239.1.2.3:1234', '239.1.2.3?ttl=255', 'example.com', '']:
      with self.subTest(address=address):
        self.assertRaises(ValueError, parse_stream_setting, 'address', address)

  def test_rejects_invalid_numbers(self):
    for field, values in {'port': ['0', '65536'], 'bitrate': ['249', '8001'], 'ttl': ['0', '256']}.items():
      for value in values + ['-1', '1.5', '１', '1&ttl=8', '']:
        with self.subTest(field=field, value=value):
          self.assertRaises(ValueError, parse_stream_setting, field, value)

  def test_saved_invalid_settings_are_not_silently_sent(self):
    for values in [{'port': True}, {'ttl': 0}, {'bitrate': '3000'}, {'address': '192.168.1.10'}]:
      with self.subTest(values=values):
        self.assertRaises(ValueError, ScreenStreamConfig, **values)


if __name__ == '__main__':
  unittest.main()
