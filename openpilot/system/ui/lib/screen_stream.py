"""UIを停止させず、Wi-Fiへ低遅延の画面映像を送信する。"""

import errno
import ipaddress
import os
import queue
import select
import socket
import subprocess
import threading
import time
import sys
from collections.abc import Callable
from typing import BinaryIO
from openpilot.common.swaglog import cloudlog
from openpilot.system.ui.lib.screen_stream_config import ScreenStreamConfig
from openpilot.system.ui.lib.screen_stream_monitor import CachedQuery, LatencyStats
from openpilot.system.ui.lib.screen_stream_latency import FrameTimingObserver, ProcessUsage

WIDTH, HEIGHT, FPS = 800, 480, 20
FRAME_BYTES = WIDTH * HEIGHT * 4
DEFAULT_CONFIG = ScreenStreamConfig()
MULTICAST_ADDRESS = DEFAULT_CONFIG.address
PORT = DEFAULT_CONFIG.port
CONFIG_INTERVAL = 1.0
NETWORK_INTERVAL_CONNECTED = 5.0
NETWORK_INTERVAL_DISCONNECTED = 1.0
NETWORK_ERROR_LOG_INTERVAL = 10.0
RETRY_INTERVAL = 3.0
# 各D-Bus往復に250msを与え、複数の照会で期限を共有しない。個々の待機は短く制限する。
NETWORK_REQUEST_TIMEOUT = 0.25
FRAME_MAX_AGE = 0.075
PIPE_WRITE_TIMEOUT = 0.10
FIRST_FRAME_WRITE_TIMEOUT = 0.50
TS_PACKET_SIZE = 188
MULTICAST_TS_PACKETS_PER_DATAGRAM = 7
UNICAST_TS_PACKETS_PER_DATAGRAM = 3
UDP_PAYLOAD_SIZE = TS_PACKET_SIZE * MULTICAST_TS_PACKETS_PER_DATAGRAM
OUTQ_SAMPLE_INTERVAL = 0.1
LATENCY_STATS_INTERVAL = 5.0
UDP_CONGESTION_RATIO_PCT = 1.0
UDP_CONGESTION_MIN_ATTEMPTS = 100
ENCODER_PRESET = 'ultrafast'
UDP_DROP_LOG_INTERVAL = 5.0
TRANSIENT_SEND_ERRNOS = {
  getattr(errno, name) for name in ('EAGAIN', 'EWOULDBLOCK', 'ENOBUFS', 'EINTR',
                                   'WSAEWOULDBLOCK', 'WSAENOBUFS', 'WSAEINTR', 'WSAETIMEDOUT') if hasattr(errno, name)
}


class ScreenStreamError(Exception):
  pass


class FrameWriteTimeout(ScreenStreamError):
  pass


class SenderFatalError(ScreenStreamError):
  pass


class StreamStopped(ScreenStreamError):
  """通常の停止操作を障害や書き込み期限超過と区別する。"""


def wifi_address() -> tuple[str, str] | None:
  """接続済みWi-FiのインターフェースとIPv4を取得する。携帯回線やAPモードは対象外。"""
  from jeepney import DBusAddress, new_method_call
  from jeepney.io.blocking import open_dbus_connection
  from jeepney.low_level import MessageType
  from jeepney.wrappers import Properties
  from openpilot.system.ui.lib.networkmanager import (
    NM, NM_PATH, NM_IFACE, NM_DEVICE_IFACE, NM_WIRELESS_IFACE, NM_IP4_CONFIG_IFACE, NM_DEVICE_TYPE_WIFI, NMDeviceState,
  )

  with open_dbus_connection(bus="SYSTEM", auth_timeout=NETWORK_REQUEST_TIMEOUT) as connection:
    def request(message):
      reply = connection.send_and_get_reply(message, timeout=NETWORK_REQUEST_TIMEOUT)
      if reply.header.message_type == MessageType.error:
        raise OSError("NetworkManagerからWi-Fi情報を取得できません")
      return reply.body[0]

    def properties(path, interface):
      return Properties(DBusAddress(path, bus_name=NM, interface=interface))

    devices = request(new_method_call(DBusAddress(NM_PATH, bus_name=NM, interface=NM_IFACE), 'GetDevices'))
    for path in devices:
      device = request(properties(path, NM_DEVICE_IFACE).get_all())
      if device['DeviceType'][1] != NM_DEVICE_TYPE_WIFI or device['State'][1] != NMDeviceState.ACTIVATED:
        continue
      wireless = request(properties(path, NM_WIRELESS_IFACE).get_all())
      if wireless['Mode'][1] != 2 or wireless['ActiveAccessPoint'][1] == '/':
        continue
      ip4_path = device['Ip4Config'][1]
      if ip4_path == '/':
        continue
      for entry in request(properties(ip4_path, NM_IP4_CONFIG_IFACE).get('AddressData'))[1]:
        address = ipaddress.IPv4Address(entry['address'][1])
        if not (address.is_unspecified or address.is_loopback or address.is_multicast or address.is_link_local):
          return device['Interface'][1], str(address)
  return None


def ffmpeg_command(config: ScreenStreamConfig = DEFAULT_CONFIG) -> list[str]:
  """エンコードとMPEG-TSのパイプ出力だけを行い、ネットワーク設定には依存しない。"""
  return [
    'ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'warning', '-nostats',
    '-f', 'rawvideo', '-pixel_format', 'rgba', '-video_size', f'{WIDTH}x{HEIGHT}', '-framerate', str(FPS),
    '-use_wallclock_as_timestamps', '1', '-i', 'pipe:0',
    '-an', '-vf', 'vflip,format=yuv420p', '-filter_threads', '1',
    '-c:v', 'libx264', '-threads', '2', '-preset', ENCODER_PRESET, '-tune', 'zerolatency', '-profile:v', 'baseline',
    '-bf', '0', '-g', '10', '-keyint_min', '10', '-sc_threshold', '0',
    '-x264-params', 'sync-lookahead=0:rc-lookahead=0:sliced-threads=1:repeat-headers=1',
    '-b:v', f'{config.bitrate}k', '-maxrate', f'{config.bitrate}k', '-bufsize', f'{max(1, config.bitrate // 10)}k',
    '-f', 'mpegts', '-mpegts_flags', '+resend_headers', '-muxdelay', '0', '-muxpreload', '0', '-flush_packets', '1',
    'pipe:1',
  ]


class MpegTsUdpSender:
  """FFmpegのstdoutを専用スレッドで読み、TS境界を保ってWi-Fiへ送信する。"""
  def __init__(self, stdout: BinaryIO, local_address: str, config: ScreenStreamConfig,
               stdout_observer: Callable[[bytes], None] | None = None):
    self._stdout = stdout
    self._stdout_observer = stdout_observer
    self.latency: FrameTimingObserver | None = None
    self._stream_offset = 0
    self._destination = (config.address, config.port)
    self.mode = 'multicast' if ipaddress.IPv4Address(config.address).is_multicast else 'unicast'
    self.payload_size = TS_PACKET_SIZE * (MULTICAST_TS_PACKETS_PER_DATAGRAM if self.mode == 'multicast' else UNICAST_TS_PACKETS_PER_DATAGRAM)
    self.socket_sndbuf = None
    self.socket_outq_current = self.socket_outq_peak = self.socket_outq_window_peak = None
    self._next_outq_sample = 0.0
    self.stdout_pipe_available_current = self.stdout_pipe_available_window_peak = None
    self._outq_lock = threading.Lock()
    self._stop = threading.Event()
    self.done = threading.Event()
    self.error: Exception | None = None
    self.datagrams_sent = 0
    self.datagrams_dropped = 0
    self.bytes_sent = 0
    self.stdout_bytes = self.stdout_chunks = 0
    self._stats_previous = (0, 0, 0, 0, 0)
    self._stats_started = time.monotonic()
    self._last_drop_log: float | None = None
    self._io = dict.fromkeys(('sendto_calls', 'sendto_eagain_count', 'stdout_read_calls', 'stdout_eagain_count', 'sendto_seconds',
                              'stdout_wait_seconds'), 0)
    self._io_previous = self._io.copy()
    self._sendto_max = self._stdout_wait_max = 0.0
    self._thread = threading.Thread(target=self._run, name="ui-screen-udp", daemon=True)
    self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
      if self.mode == 'multicast':
        self._socket.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(local_address))
        self._socket.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, config.ttl)
      else:
        # ユニキャストも送信元をWi-Fiへ固定する。TTL設定はマルチキャストだけに適用する。
        self._socket.bind((local_address, 0))
      self.socket_sndbuf = self._socket.getsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF)
      self._socket.setblocking(False)
      os.set_blocking(stdout.fileno(), False)
    except Exception:
      self._socket.close()
      raise

  def start(self):
    self._thread.start()

  def stop(self):
    self._stop.set()

  def close(self):
    self.stop()
    if self._thread.is_alive():
      self._thread.join(timeout=0.5)
    self._socket.close()
    if self._thread.is_alive():
      raise TimeoutError("画面配信の送信スレッドを停止できません")

  def _send_datagram(self, payload: bytes) -> bool:
    sent = False
    self._io['sendto_calls'] += 1
    attempted_at = self.latency.clock() if self.latency is not None else 0.0
    started = time.perf_counter()
    try:
      try:
        written = self._socket.sendto(payload, self._destination)
      finally:
        duration = time.perf_counter() - started
        returned_at = self.latency.clock() if self.latency is not None else 0.0
      if written != len(payload):
        raise OSError("画面配信のUDPデータグラムを送信できません")
    except OSError as error:
      if not isinstance(error, (BlockingIOError, InterruptedError, TimeoutError)) and error.errno not in TRANSIENT_SEND_ERRNOS:
        raise
      # 一時的な輻輳では古い映像を再送せず、エンコーダと次のデータグラムを維持する。
      self.datagrams_dropped += 1
      if isinstance(error, BlockingIOError) or error.errno in (errno.EAGAIN, errno.EWOULDBLOCK, getattr(errno, 'WSAEWOULDBLOCK', 10035)):
        self._io['sendto_eagain_count'] += 1
      now = time.monotonic()
      if self._last_drop_log is None or now - self._last_drop_log >= UDP_DROP_LOG_INTERVAL:
        cloudlog.warning(f"screen stream UDP drops: dropped={self.datagrams_dropped} sent={self.datagrams_sent} last_errno={error.errno}")
        self._last_drop_log = now
      return False
    else:
      self.datagrams_sent += 1
      self.bytes_sent += len(payload)
      sent = True
      return True
    finally:
      self._io['sendto_seconds'] += duration
      self._sendto_max = max(self._sendto_max, duration)
      if self.latency is not None:
        self.latency.datagram(self._stream_offset, self._stream_offset + len(payload), returned_at, sent, attempted_at)
      self._stream_offset += len(payload)

  def sample_outq(self):
    # Linuxのsocketキューをサンプリングする。NIC・無線・受信側のキューは含まない。
    if not sys.platform.startswith('linux'):
      return
    now = time.monotonic()
    with self._outq_lock:
      if now < self._next_outq_sample:
        return
      self._next_outq_sample = now + OUTQ_SAMPLE_INTERVAL
    try:
      import array
      import fcntl
      import termios
      pending = array.array('i', [0])
      fcntl.ioctl(self._socket.fileno(), termios.TIOCOUTQ, pending, True)
      with self._outq_lock:
        self.socket_outq_current = pending[0]
        self.socket_outq_peak = max(self.socket_outq_peak or 0, pending[0])
        self.socket_outq_window_peak = max(self.socket_outq_window_peak or 0, pending[0])
    except (OSError, ValueError, TypeError, AttributeError, ImportError):
      with self._outq_lock:
        self.socket_outq_current = None
    try:
      import array
      import fcntl
      import termios
      available = array.array('i', [0])
      fcntl.ioctl(self._stdout.fileno(), termios.FIONREAD, available, True)
      with self._outq_lock:
        self.stdout_pipe_available_current = available[0]
        self.stdout_pipe_available_window_peak = max(self.stdout_pipe_available_window_peak or 0, available[0])
    except (OSError, ValueError, TypeError, AttributeError, ImportError):
      with self._outq_lock:
        self.stdout_pipe_available_current = None

  def stats_snapshot(self, now: float):
    # 累積カウンターの差を取り、送信ループで毎packetの統計整形をしない。
    current = (self.datagrams_sent, self.datagrams_dropped, self.bytes_sent, self.stdout_bytes, self.stdout_chunks)
    sent, dropped, sent_bytes, stdout_bytes, stdout_chunks = (value - old for value, old in zip(current, self._stats_previous, strict=True))
    elapsed = max(now - self._stats_started, 1e-6)
    self._stats_previous, self._stats_started = current, now
    with self._outq_lock:
      outq = (self.socket_outq_current, self.socket_outq_peak, self.socket_outq_window_peak)
      self.socket_outq_window_peak = None
      pipe = self.stdout_pipe_available_current, self.stdout_pipe_available_window_peak
      self.stdout_pipe_available_window_peak = None
    io = self._io.copy()
    delta = {key: value - self._io_previous[key] for key, value in io.items()}
    self._io_previous = io
    send_max, wait_max = self._sendto_max, self._stdout_wait_max
    self._sendto_max = self._stdout_wait_max = 0.0
    return {
      **{key: value for key, value in delta.items() if not key.endswith('_seconds')},
      'sendto_avg_us': round(delta['sendto_seconds'] * 1e6 / max(delta['sendto_calls'], 1), 3),
      'sendto_max_us': round(send_max * 1e6, 3), 'stdout_wait_ms': round(delta['stdout_wait_seconds'] * 1000, 3),
      'stdout_wait_max_ms': round(wait_max * 1000, 3),
      'stdout_bytes': stdout_bytes, 'stdout_chunks': stdout_chunks,
      'stdout_pipe_available_current': pipe[0], 'stdout_pipe_available_window_peak': pipe[1],
      'sender_datagrams': current[0], 'sender_drops': current[1], 'sender_bytes': current[2],
      'sender_drop_ratio_pct': round(100 * current[1] / max(current[0] + current[1], 1), 4),
      'sender_datagrams_delta': sent, 'sender_drops_delta': dropped, 'sender_bytes_delta': sent_bytes,
      'sender_drop_ratio_window_pct': round(100 * dropped / max(sent + dropped, 1), 4),
      'stdout_bytes_delta': stdout_bytes, 'stdout_chunks_delta': stdout_chunks,
      'stdout_bytes_per_sec': round(stdout_bytes / elapsed, 3), 'datagrams_per_sec': round(sent / elapsed, 3),
      'sender_drops_per_sec': round(dropped / elapsed, 3), 'sender_bytes_per_sec': round(sent_bytes / elapsed, 3),
      'socket_sndbuf': self.socket_sndbuf, 'socket_outq_current': outq[0], 'socket_outq_peak': outq[1], 'socket_outq_window_peak': outq[2],
    }

  def _wait_readable(self):
    if os.name == 'posix':
      select.select([self._stdout.fileno()], [], [], 0.05)
    else:
      # Windowsの匿名パイプはselect非対応。開発PCでの検証のみ短い待機を使う。
      self._stop.wait(0.001)

  def _run(self):
    pending = bytearray()
    try:
      while not self._stop.is_set():
        try:
          self._io['stdout_read_calls'] += 1
          chunk = os.read(self._stdout.fileno(), 65536)
          observed_at = self.latency.clock() if self.latency is not None else 0.0
        except BlockingIOError:
          self._io['stdout_eagain_count'] += 1
          before = time.perf_counter()
          self._wait_readable()
          elapsed = time.perf_counter() - before
          self._io['stdout_wait_seconds'] += elapsed
          self._stdout_wait_max = max(self._stdout_wait_max, elapsed)
          continue
        if not chunk:
          # 正常EOFだけ端数の完全なTSパケットを送り、停止時は古いstreamの末尾を送らない。
          size = len(pending) // TS_PACKET_SIZE * TS_PACKET_SIZE
          if size and not self._stop.is_set():
            self._send_datagram(bytes(pending[:size]))
          return
        # 診断時だけ観測時刻とbytesを保存する。通常配信にはPES解析を入れない。
        if self._stdout_observer is not None:
          self._stdout_observer(chunk)
        if self.latency is not None:
          self.latency.observe(chunk, observed_at)
        pending.extend(chunk)
        self.stdout_bytes += len(chunk)
        self.stdout_chunks += 1
        offset = 0
        while len(pending) - offset >= self.payload_size and not self._stop.is_set():
          self._send_datagram(bytes(pending[offset:offset + self.payload_size]))
          offset += self.payload_size
        if offset:
          del pending[:offset]
        self.sample_outq()
    except Exception as error:
      if not self._stop.is_set():
        self.error = error
    finally:
      self._socket.close()
      self.done.set()
      cloudlog.info(f"screen stream sender stopped: sent={self.datagrams_sent} dropped={self.datagrams_dropped} bytes_sent={self.bytes_sent}")


class ScreenStreamer:
  def __init__(self, enabled: Callable[[], bool], network: Callable[[], tuple[str, str] | None] = wifi_address,
               config: Callable[[], ScreenStreamConfig] = ScreenStreamConfig):
    self._enabled = enabled
    self._network = network
    self._config = config
    self._frames: queue.Queue[tuple[float, bytes, int]] = queue.Queue(maxsize=1)
    self._frame_sequence = 0
    self._latency: FrameTimingObserver | None = None
    self._usage: ProcessUsage | None = None
    self._stats_window = 0
    self.ready = threading.Event()
    self.visible = threading.Event()
    self._stop = threading.Event()
    self._thread = threading.Thread(target=self._run, name="ui-screen-stream", daemon=True)
    self._proc: subprocess.Popen | None = None
    self._sender: MpegTsUdpSender | None = None
    self._restart_count = 0
    self._first_frame_written = False
    self._process_started_at: float | None = None
    self._first_frame_metrics: dict[str, float | int | None] = {}
    self._first_frame_write_timeout_count = 0
    self._regular_frame_write_timeout_count = 0
    self._network_query_failures = 0
    self._last_network_error_log: float | None = None
    self.stats = LatencyStats()
    self._network_monitor = CachedQuery(network, lambda value: NETWORK_INTERVAL_CONNECTED if value is not None else NETWORK_INTERVAL_DISCONNECTED,
                                        'ui-stream-network')
    self._config_monitor = CachedQuery(config, lambda value: CONFIG_INTERVAL, 'ui-stream-config')
    self._next_stats = 0.0
    self._last_stats_time = 0.0
    self._active_config: ScreenStreamConfig | None = None

  def start(self):
    self._thread.start()

  def submit(self, data: bytes, captured: float | None = None):
    if not self.ready.is_set() or len(data) != FRAME_BYTES:
      return
    # UI側では待機せず、未処理の古いフレームを最新のものに置き換える。
    replaced = 0
    try:
      self._frames.get_nowait()
      replaced = 1
    except queue.Empty:
      pass
    self._frame_sequence += 1
    self._frames.put_nowait((time.monotonic() if captured is None else captured, data, self._frame_sequence))
    self.stats.record({'submitted_count': 1, 'queue_replaced_count': replaced})

  def close(self):
    self.ready.clear()
    self._stop.set()
    if self._thread.is_alive():
      self._thread.join(timeout=2.0)

  def _log_latency_stats(self, config, final_reason: str | None = None):
    now = time.monotonic()
    if self._proc is None or self._sender is None or (final_reason is None and now < self._next_stats):
      return
    self._next_stats = now + LATENCY_STATS_INTERVAL
    sender = self._sender
    sender.sample_outq()
    values = self.stats.snapshot(reset=True)
    elapsed = max(now - self._last_stats_time, 1e-6)
    self._last_stats_time = now
    values.update(stats_window_s=round(elapsed, 6), capture_fps=round(values['capture_count'] / elapsed, 3),
                  submitted_fps=round(values['submitted_count'] / elapsed, 3), frames_written_fps=round(values['frames_written'] / elapsed, 3))
    values.update(sender.stats_snapshot(now))
    values.update(self._first_frame_metrics)
    values.update(first_frame_written=int(self._first_frame_written),
                  first_frame_write_timeout_count=self._first_frame_write_timeout_count,
                  regular_frame_write_timeout_count=self._regular_frame_write_timeout_count)
    if self._latency is not None:
      values.update(self._latency.snapshot(now, elapsed, final=final_reason is not None))
    if self._usage is not None:
      values.update(self._usage.sample(now))
    self._stats_window += 1
    values.update(pid=self._proc.pid, bitrate=config.bitrate, mode=sender.mode, window_id=self._stats_window)
    prefix = 'screen stream latency stats: ' if final_reason is None else f'screen stream latency final: reason={final_reason!r} '
    cloudlog.info(prefix + ' '.join(f'{key}={value}' for key, value in values.items()))
    if (values['sender_datagrams_delta'] + values['sender_drops_delta'] >= UDP_CONGESTION_MIN_ATTEMPTS
        and values['sender_drop_ratio_window_pct'] >= UDP_CONGESTION_RATIO_PCT):
      cloudlog.warning(f"screen stream UDP congestion: pid={self._proc.pid} bitrate={config.bitrate} mode={sender.mode} " +
                       f"window_s={elapsed:.3f} sent={values['sender_datagrams_delta']} dropped={values['sender_drops_delta']} " +
                       f"drop_ratio_pct={values['sender_drop_ratio_window_pct']}")

  def _close_process(self, reason: str = "shutdown"):
    self.ready.clear()
    if self._active_config is not None:
      try:
        self._log_latency_stats(self._active_config, final_reason=reason)
      except Exception:
        cloudlog.exception('screen stream latency final failed')
      self._active_config = None
    proc, self._proc = self._proc, None
    self._latency = self._usage = None
    sender, self._sender = self._sender, None
    if sender is not None:
      sender.stop()
    try:
      if proc is not None:
        if proc.poll() is None:
          proc.terminate()
        try:
          proc.wait(timeout=0.2)
        except subprocess.TimeoutExpired:
          proc.kill()
          proc.wait(timeout=0.2)
    finally:
      try:
        if proc is not None:
          try:
            if proc.stdin is not None:
              proc.stdin.close()
          finally:
            if proc.stdout is not None:
              proc.stdout.close()
      finally:
        try:
          if proc is not None:
            cloudlog.info(f"screen stream stopped: pid={proc.pid} rc={proc.poll()} reason={reason}")
          if sender is not None:
            sender.close()
        finally:
          try:
            self._frames.get_nowait()
          except queue.Empty:
            pass

  def _check_sender(self):
    if self._sender is not None and self._sender.done.is_set():
      error = self._sender.error
      if error is not None:
        raise SenderFatalError(f"sender fatal error: errno={getattr(error, 'errno', None)} error={error!r}") from error
      raise ScreenStreamError("sender EOF")

  def _log_restart(self, reason: str, exception: bool = False):
    self._restart_count += 1
    message = f"screen stream restart: {reason} count={self._restart_count}"
    if exception:
      cloudlog.exception(message)
    else:
      cloudlog.warning(message)

  def _log_network_query_error(self, network: tuple[str, str] | None, error: Exception):
    self._network_query_failures += 1
    now = time.monotonic()
    if self._last_network_error_log is None or now - self._last_network_error_log >= NETWORK_ERROR_LOG_INTERVAL:
      state = "transient" if network is not None else "failed before start"
      cloudlog.warning(f"screen stream network query {state}: failures={self._network_query_failures} using={network!r} error={error!r}")
      self._last_network_error_log = now

  def _write_frame(self, captured: float, data: bytes, sequence: int = 0, dequeued: float | None = None):
    assert self._proc is not None and self._proc.stdin is not None
    remaining = memoryview(data)
    started = time.monotonic()
    first_frame = not self._first_frame_written
    deadline = started + (FIRST_FRAME_WRITE_TIMEOUT if first_frame else PIPE_WRITE_TIMEOUT)
    observer = self._latency
    if observer is not None:
      observer.begin(sequence, captured, started if dequeued is None else dequeued, started)
    calls = blocked = size = 0
    wait = maximum = 0.0
    try:
      while remaining:
        self._check_sender()
        if self._stop.is_set():
          raise StreamStopped("shutdown")
        if not self.visible.is_set():
          raise StreamStopped("screen invisible")
        now = time.monotonic()
        if now >= deadline:
          # rawvideoの途中を破棄すると次フレームの境界が壊れるため、プロセスごと再開する。
          if first_frame:
            self._first_frame_write_timeout_count += 1
          else:
            self._regular_frame_write_timeout_count += 1
          uptime = (now - self._process_started_at) * 1000 if self._process_started_at is not None else None
          raise FrameWriteTimeout(f"frame write timeout age={now - captured:.3f}s write_elapsed={now - started:.3f}s " +
                                  f"first_frame={int(first_frame)} written_bytes={size} remaining_bytes={len(remaining)} " +
                                  f"blocked_events={blocked} blocked_wait_ms={wait * 1000:.3f} " +
                                  f"process_uptime_ms={round(uptime, 3) if uptime is not None else None}")
        try:
          calls += 1
          written = os.write(self._proc.stdin.fileno(), remaining)
          if written == 0:
            raise BrokenPipeError("画面配信の入力パイプが閉じられました")
          size += written
          remaining = remaining[written:]
        except BlockingIOError:
          blocked += 1
          before = time.perf_counter()
          if os.name == 'posix':
            select.select([], [self._proc.stdin.fileno()], [], min(0.05, max(0, deadline - time.monotonic())))
          else:
            self._stop.wait(0.001)
          elapsed = time.perf_counter() - before
          wait += elapsed
          maximum = max(maximum, elapsed)
        except OSError as error:
          raise ScreenStreamError(f"frame pipe broken: rc={self._proc.poll()} errno={error.errno} error={error!r}") from error
      if first_frame:
        # 最初の完全writeだけ起動猶予を使う。値は次のプロセス生成まで保持する。
        completed = time.monotonic()
        self._first_frame_metrics = {
          'first_frame_write_ms': round((completed - started) * 1000, 3),
          'first_frame_blocked_wait_ms': round(wait * 1000, 3),
          'first_frame_write_syscalls': calls,
          'first_frame_blocked_events': blocked,
          'first_frame_bytes_written': size,
          'process_start_to_first_frame_complete_ms': (round((completed - self._process_started_at) * 1000, 3)
                                                      if self._process_started_at is not None else None),
        }
        self._first_frame_written = True
    finally:
      self.stats.record_stdin(calls, blocked, size, wait, maximum)
      if observer is not None:
        observer.complete(sequence, time.monotonic(), success=not remaining)

  def _run(self):
    current_network = None
    network_checked = False
    current_config = ScreenStreamConfig()
    config_valid = False
    config_generation = network_generation = -1
    next_retry = 0.0
    try:
      if hasattr(os, 'sched_setscheduler'):
        # UIのリアルタイム優先度をエンコーダと送信スレッドへ継承させない。
        os.sched_setscheduler(0, os.SCHED_OTHER, os.sched_param(0))
      self._network_monitor.start()
      self._config_monitor.start()
      while not self._stop.is_set():
        try:
          now = time.monotonic()
          enabled = self._enabled()
          if not self.visible.is_set() or not enabled:
            self._close_process("screen invisible" if enabled else "stream disabled")
            current_network = None
            network_checked = False
            config_valid = False
            self._network_query_failures = 0
            self._last_network_error_log = None
            self._network_monitor.set_active(False)
            self._config_monitor.set_active(False)
            self._stop.wait(0.1)
            continue
          self._network_monitor.set_active(True)
          self._config_monitor.set_active(True)
          settings = self._config_monitor.snapshot()
          if settings.generation != config_generation:
            config_generation = settings.generation
            if settings.error is not None:
              config_valid = False
              raise ScreenStreamError(f"invalid config: {settings.error!r}") from settings.error
            config_valid = settings.checked
            if settings.checked and settings.value != current_config:
              reason = f"config changed old={current_config!r} new={settings.value!r}"
              cloudlog.info(f"screen stream state: {reason}")
              if self._proc is not None:
                self._close_process(reason)
              current_config = settings.value
              next_retry = 0.0

          connection = self._network_monitor.snapshot()
          if connection.generation != network_generation:
            network_generation = connection.generation
            if connection.error is not None:
              self._network_query_failures = connection.failures - 1
              self._log_network_query_error(current_network, connection.error)
            else:
              if connection.checked and self._network_query_failures:
                cloudlog.info(f"screen stream network query recovered: failures={self._network_query_failures}")
                self._network_query_failures = 0
                self._last_network_error_log = None
            if connection.checked:
              network = connection.value
              if network != current_network or not network_checked:
                reason = (f"network changed old={current_network!r} new={network!r}" if network is not None
                          else f"network unavailable old={current_network!r}")
                cloudlog.info(f"screen stream state: {reason}")
                if self._proc is not None:
                  self._close_process(reason)
                current_network = network
                next_retry = 0.0
              network_checked = True

          if self._stop.is_set() or not self.visible.is_set() or not self._enabled():
            continue
          if current_network is None or not config_valid:
            self._stop.wait(0.1)
            continue
          returncode = self._proc.poll() if self._proc is not None else None
          if returncode is not None:
            raise ScreenStreamError(f"ffmpeg exited rc={returncode}")
          self._check_sender()
          if self._proc is None:
            if now < next_retry:
              self._stop.wait(0.1)
              continue
            try:
              # Popenの開始を基準とし、設定変更・障害再起動も必ず新しい起動状態にする。
              self._process_started_at = time.monotonic()
              self._first_frame_written = False
              self._first_frame_metrics = dict.fromkeys((
                'first_frame_write_ms', 'first_frame_blocked_wait_ms', 'first_frame_write_syscalls',
                'first_frame_blocked_events', 'first_frame_bytes_written', 'process_start_to_first_frame_complete_ms',
              ))
              self._proc = subprocess.Popen(ffmpeg_command(current_config), stdin=subprocess.PIPE, stdout=subprocess.PIPE, bufsize=0)
            except OSError as error:
              raise ScreenStreamError(f"encoder start failed: errno={error.errno} error={error!r}") from error
            assert self._proc.stdin is not None and self._proc.stdout is not None
            os.set_blocking(self._proc.stdin.fileno(), False)
            try:
              self._sender = MpegTsUdpSender(self._proc.stdout, current_network[1], current_config)
            except OSError as error:
              raise SenderFatalError(f"sender fatal error during setup: errno={error.errno} error={error!r}") from error
            self._latency = FrameTimingObserver()
            self._usage = ProcessUsage(self._proc.pid)
            self._stats_window = 0
            self._sender.latency = self._latency
            self._sender.start()
            self._active_config = current_config
            cloudlog.info(f"screen stream started: pid={self._proc.pid} mode={self._sender.mode} network={current_network!r} " +
                          f"destination={current_config.address}:{current_config.port} bitrate={current_config.bitrate} ttl={current_config.ttl} " +
                          f"socket_sndbuf={self._sender.socket_sndbuf} payload_size={self._sender.payload_size} preset={ENCODER_PRESET}")
            self.stats.snapshot(reset=True)
            self._last_stats_time = time.monotonic()
            self._next_stats = self._last_stats_time + LATENCY_STATS_INTERVAL
            self.ready.set()
          self._log_latency_stats(current_config)
          try:
            captured, data, sequence = self._frames.get(timeout=0.05)
          except queue.Empty:
            continue
          dequeued = time.monotonic()
          age = dequeued - captured
          self.stats.record(queue_age=age)
          if age < FRAME_MAX_AGE:
            started = time.monotonic()
            try:
              self._write_frame(captured, data, sequence, dequeued)
            finally:
              self.stats.record(stdin_write=time.monotonic() - started)
            self.stats.record({'frames_written': 1}, frame_age_written=time.monotonic() - captured)
          else:
            self.stats.record({'stale_drop_count': 1})
        except StreamStopped as error:
          self._close_process(str(error))
          self._network_monitor.set_active(False)
          self._config_monitor.set_active(False)
        except Exception as error:
          reason = str(error) if isinstance(error, ScreenStreamError) else f"unexpected error: {error!r}"
          self._log_restart(reason, exception=True)
          self._close_process(reason)
          next_retry = time.monotonic() + RETRY_INTERVAL
          self._stop.wait(0.1)
    except Exception:
      cloudlog.exception("screen stream worker failed")
    finally:
      self._network_monitor.close()
      self._config_monitor.close()
      self._close_process()
