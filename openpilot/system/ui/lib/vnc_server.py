"""Single-client, read-only RFB 3.3 server. Frames are supplied by the UI thread."""

import os
import select
import socket
import struct
import threading
import zlib
from collections.abc import Callable

WIDTH, HEIGHT = 800, 480
# Little-endian RGB in 32 bits: the fourth byte is unused by RFB (opaque alpha for GL_RGBA).
PIXEL_FORMAT = struct.pack('!4B3H3B3x', 32, 24, 0, 1, 255, 255, 255, 0, 8, 16)
VERSION = b'RFB 003.003\n'


class VncServer:
  def __init__(self, enabled: Callable[[], bool]):
    self._enabled = enabled
    self.connected = threading.Event()
    self.requested = threading.Event()
    self._ready = threading.Event()
    self._stop = threading.Event()
    self._frame: bytes | None = None
    self._thread = threading.Thread(target=self._run, daemon=True)

  def start(self):
    self._thread.start()

  def close(self):
    self._stop.set()
    self._thread.join()

  def submit(self, frame: bytes):
    if self.requested.is_set():
      self._frame = frame
      self.requested.clear()
      self._ready.set()

  def _active(self):
    return not self._stop.is_set() and self._enabled()

  def _recv(self, client: socket.socket, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
      if not self._active():
        raise EOFError
      try:
        chunk = client.recv(size - len(data))
      except TimeoutError:
        continue
      if not chunk:
        raise EOFError
      data.extend(chunk)
    return bytes(data)

  def _run(self):
    if hasattr(os, 'sched_setscheduler'):
      try:
        os.sched_setscheduler(0, os.SCHED_OTHER, os.sched_param(0))
      except OSError:
        pass
    while not self._stop.is_set():
      try:
        if self._active():
          with socket.socket() as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(('0.0.0.0', 5900))
            listener.listen(1)
            listener.settimeout(0.5)
            while self._active():
              try:
                client, _ = listener.accept()
              except TimeoutError:
                continue
              with client:
                client.settimeout(0.5)
                client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                try:
                  self._serve(client)
                except (OSError, EOFError, ValueError):
                  pass
                finally:
                  self.connected.clear()
                  self.requested.clear()
                  self._frame = None
      except OSError:
        pass
      self._stop.wait(0.5)

  def _serve(self, client: socket.socket):
    client.sendall(VERSION)
    if self._recv(client, 12) != VERSION:
      return
    client.sendall(struct.pack('!I', 1))  # RFB 3.3: None security, no SecurityResult.
    self._recv(client, 1)  # ClientInit (shared flag)
    name = b'sunnypilot'
    client.sendall(struct.pack('!HH', WIDTH, HEIGHT) + PIXEL_FORMAT + struct.pack('!I', len(name)) + name)
    self.connected.set()
    compressor = zlib.compressobj(1)
    encoding = 0
    while self._active():
      message = self._recv(client, 1)[0]
      if message == 0:  # SetPixelFormat: only the advertised RGBA byte order is supported.
        if self._recv(client, 19)[3:16] != PIXEL_FORMAT[:13]:
          return
      elif message == 2:  # SetEncodings
        count = struct.unpack('!xH', self._recv(client, 3))[0]
        encodings = struct.unpack(f'!{count}i', self._recv(client, count * 4))
        encoding = 6 if 6 in encodings else 0
      elif message == 3:  # FramebufferUpdateRequest: always return the entire current screen.
        self._recv(client, 9)
        self._ready.clear()
        self.requested.set()
        while not self._ready.wait(0.5):
          if not self._active():
            return
          if select.select([client], [], [], 0)[0] and not client.recv(1, socket.MSG_PEEK):
            return
        if not self._active():
          return
        data = self._frame
        assert data is not None
        if encoding == 6:
          data = compressor.compress(data) + compressor.flush(zlib.Z_SYNC_FLUSH)
          data = struct.pack('!I', len(data)) + data
        data = memoryview(struct.pack('!BBHHHHHi', 0, 0, 1, 0, 0, WIDTH, HEIGHT, encoding) + data)
        while data:
          if not self._active():
            return
          try:
            sent = client.send(data)
          except TimeoutError:
            continue
          if not sent:
            return
          data = data[sent:]
        self._frame = None
      elif message in (4, 5):  # Ignore KeyEvent / PointerEvent.
        self._recv(client, 7 if message == 4 else 5)
      else:
        return
