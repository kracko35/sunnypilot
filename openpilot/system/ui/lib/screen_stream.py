"""UIを停止させず、Wi-Fiへ低遅延の画面映像を送信する。"""

import ipaddress
import logging
import os
import queue
import select
import socket
import subprocess
import threading
import time
from collections.abc import Callable
from typing import BinaryIO
from openpilot.system.ui.lib.screen_stream_config import ScreenStreamConfig

WIDTH, HEIGHT, FPS = 800, 480, 20
FRAME_BYTES = WIDTH * HEIGHT * 4
DEFAULT_CONFIG = ScreenStreamConfig()
MULTICAST_ADDRESS = DEFAULT_CONFIG.address
PORT = DEFAULT_CONFIG.port
NETWORK_INTERVAL = 1.0
RETRY_INTERVAL = 3.0
WRITE_TIMEOUT = 0.25
TS_PACKET_SIZE = 188
UDP_PAYLOAD_SIZE = TS_PACKET_SIZE * 7
logger = logging.getLogger(__name__)


def wifi_address() -> tuple[str, str] | None:
  """接続済みWi-FiのインターフェースとIPv4を取得する。携帯回線やAPモードは対象外。"""
  from jeepney import DBusAddress, new_method_call
  from jeepney.io.blocking import open_dbus_connection
  from jeepney.low_level import MessageType
  from jeepney.wrappers import Properties
  from openpilot.system.ui.lib.networkmanager import (
    NM, NM_PATH, NM_IFACE, NM_DEVICE_IFACE, NM_WIRELESS_IFACE, NM_IP4_CONFIG_IFACE, NM_DEVICE_TYPE_WIFI, NMDeviceState,
  )

  deadline = time.monotonic() + WRITE_TIMEOUT
  with open_dbus_connection(bus="SYSTEM", auth_timeout=WRITE_TIMEOUT) as connection:
    def request(message):
      remaining = deadline - time.monotonic()
      if remaining <= 0:
        raise TimeoutError("Wi-Fi情報の取得がタイムアウトしました")
      reply = connection.send_and_get_reply(message, timeout=remaining)
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
    '-f', 'rawvideo', '-pixel_format', 'rgba', '-video_size', f'{WIDTH}x{HEIGHT}', '-framerate', str(FPS), '-i', 'pipe:0',
    '-an', '-vf', 'vflip,format=yuv420p', '-filter_threads', '1',
    '-c:v', 'libx264', '-threads', '2', '-preset', 'veryfast', '-tune', 'zerolatency', '-profile:v', 'baseline',
    '-bf', '0', '-g', '10', '-keyint_min', '10', '-sc_threshold', '0', '-x264-params', 'repeat-headers=1',
    '-b:v', f'{config.bitrate}k', '-maxrate', f'{config.bitrate}k', '-bufsize', f'{max(1, config.bitrate // 3)}k',
    '-f', 'mpegts', '-mpegts_flags', '+resend_headers', '-muxdelay', '0', '-muxpreload', '0', '-flush_packets', '1',
    'pipe:1',
  ]


class MpegTsMulticastSender:
  """FFmpegのstdoutを専用スレッドで読み、TS境界を保ってWi-Fiへ送信する。"""
  def __init__(self, stdout: BinaryIO, local_address: str, config: ScreenStreamConfig):
    self._stdout = stdout
    self._destination = (config.address, config.port)
    self._stop = threading.Event()
    self.done = threading.Event()
    self.error: Exception | None = None
    self._thread = threading.Thread(target=self._run, name="ui-screen-multicast", daemon=True)
    self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
      self._socket.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(local_address))
      self._socket.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, config.ttl)
      self._socket.settimeout(0.1)
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

  def _run(self):
    pending = bytearray()
    try:
      while not self._stop.is_set():
        try:
          chunk = os.read(self._stdout.fileno(), 65536)
        except BlockingIOError:
          self._stop.wait(0.01)
          continue
        if not chunk:
          # EOFの188 bytes未満の端数は不完全なTSパケットなので送信しない。
          return
        pending.extend(chunk)
        while len(pending) >= TS_PACKET_SIZE and not self._stop.is_set():
          size = min(len(pending) // TS_PACKET_SIZE * TS_PACKET_SIZE, UDP_PAYLOAD_SIZE)
          payload = bytes(pending[:size])
          if self._socket.sendto(payload, self._destination) != size:
            raise OSError("画面配信のUDPデータグラムを送信できません")
          del pending[:size]
    except Exception as error:
      if not self._stop.is_set():
        self.error = error
    finally:
      self._socket.close()
      self.done.set()


class ScreenStreamer:
  def __init__(self, enabled: Callable[[], bool], network: Callable[[], tuple[str, str] | None] = wifi_address,
               config: Callable[[], ScreenStreamConfig] = ScreenStreamConfig):
    self._enabled = enabled
    self._network = network
    self._config = config
    self._frames: queue.Queue[tuple[float, bytes]] = queue.Queue(maxsize=1)
    self.ready = threading.Event()
    self.visible = threading.Event()
    self._stop = threading.Event()
    self._thread = threading.Thread(target=self._run, name="ui-screen-stream", daemon=True)
    self._proc: subprocess.Popen | None = None
    self._sender: MpegTsMulticastSender | None = None

  def start(self):
    self._thread.start()

  def submit(self, data: bytes):
    if not self.ready.is_set() or len(data) != FRAME_BYTES:
      return
    # UI側では待機せず、未処理の古いフレームを最新のものに置き換える。
    try:
      self._frames.get_nowait()
    except queue.Empty:
      pass
    self._frames.put_nowait((time.monotonic(), data))

  def close(self):
    self.ready.clear()
    self._stop.set()
    if self._thread.is_alive():
      self._thread.join(timeout=2.0)

  def _close_process(self):
    self.ready.clear()
    proc, self._proc = self._proc, None
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
          if sender is not None:
            sender.close()
        finally:
          try:
            self._frames.get_nowait()
          except queue.Empty:
            pass

  def _check_sender(self):
    if self._sender is not None and self._sender.done.is_set():
      raise BrokenPipeError("画面配信のMPEG-TS送信が停止しました") from self._sender.error

  def _write_frame(self, captured: float, data: bytes):
    assert self._proc is not None and self._proc.stdin is not None
    remaining = memoryview(data)
    deadline = captured + WRITE_TIMEOUT
    while remaining:
      self._check_sender()
      if self._stop.is_set() or not self.visible.is_set() or time.monotonic() >= deadline:
        # rawvideoの途中を破棄すると次フレームの境界が壊れるため、プロセスごと再開する。
        raise TimeoutError("画面配信の書き込み期限を超過しました")
      try:
        written = os.write(self._proc.stdin.fileno(), remaining)
        if written == 0:
          raise BrokenPipeError("画面配信の入力パイプが閉じられました")
        remaining = remaining[written:]
      except BlockingIOError:
        if os.name == 'posix':
          select.select([], [self._proc.stdin.fileno()], [], min(0.05, max(0, deadline - time.monotonic())))
        else:
          self._stop.wait(0.001)

  def _run(self):
    current_network = None
    current_config = ScreenStreamConfig()
    next_check = next_retry = 0.0
    last_error = 0.0
    try:
      if hasattr(os, 'sched_setscheduler'):
        # UIのリアルタイム優先度をエンコーダと送信スレッドへ継承させない。
        os.sched_setscheduler(0, os.SCHED_OTHER, os.sched_param(0))
        os.setpriority(os.PRIO_PROCESS, 0, 10)
      while not self._stop.is_set():
        try:
          now = time.monotonic()
          if not self.visible.is_set() or not self._enabled():
            self._close_process()
            current_network = None
            next_check = 0.0
            self._stop.wait(0.1)
            continue
          if now >= next_check:
            config = self._config()
            network = self._network()
            next_check = time.monotonic() + NETWORK_INTERVAL
            if network != current_network or config != current_config:
              self._close_process()
              current_network = network
              current_config = config
              next_retry = 0.0
          if current_network is None:
            self._stop.wait(0.1)
            continue
          if self._proc is not None and self._proc.poll() is not None:
            raise BrokenPipeError("画面配信のFFmpegが終了しました")
          self._check_sender()
          if self._proc is None:
            if now < next_retry:
              self._stop.wait(0.1)
              continue
            self._proc = subprocess.Popen(ffmpeg_command(current_config), stdin=subprocess.PIPE, stdout=subprocess.PIPE, bufsize=0)
            assert self._proc.stdin is not None and self._proc.stdout is not None
            os.set_blocking(self._proc.stdin.fileno(), False)
            self._sender = MpegTsMulticastSender(self._proc.stdout, current_network[1], current_config)
            self._sender.start()
            self.ready.set()
          try:
            captured, data = self._frames.get(timeout=0.05)
          except queue.Empty:
            continue
          if time.monotonic() - captured < WRITE_TIMEOUT:
            self._write_frame(captured, data)
        except Exception:
          self._close_process()
          current_network = None
          next_check = next_retry = time.monotonic() + RETRY_INTERVAL
          if time.monotonic() - last_error >= 30:
            logger.exception("画面配信を停止しました。自動で再試行します")
            last_error = time.monotonic()
          self._stop.wait(0.1)
    except Exception:
      logger.exception("画面配信ワーカーを開始できません")
    finally:
      self._close_process()
