import socket
import struct
import threading
import time
import unittest
import zlib
from unittest.mock import patch

from openpilot.system.ui.lib.vnc_server import HEIGHT, WIDTH, VncServer


def recv_exact(client, size):
  data = bytearray()
  while len(data) < size:
    chunk = client.recv(size - len(data))
    if not chunk:
      raise EOFError
    data.extend(chunk)
  return bytes(data)


class TestVncServer(unittest.TestCase):
  def setUp(self):
    self.enabled = threading.Event()
    self.enabled.set()
    self.server = VncServer(self.enabled.is_set)
    self.server.start()
    self.addCleanup(self.server.close)

  def connect(self):
    deadline = time.monotonic() + 3
    while True:
      try:
        client = socket.create_connection(('127.0.0.1', 5900), timeout=2)
        break
      except ConnectionRefusedError:
        if time.monotonic() > deadline:
          raise
        time.sleep(0.01)
    self.addCleanup(client.close)
    self.assertEqual(recv_exact(client, 12), b'RFB 003.003\n')
    # Fragment the version to exercise TCP stream reassembly.
    client.sendall(b'RFB 003.')
    client.sendall(b'003\n')
    self.assertEqual(recv_exact(client, 4), b'\0\0\0\1')
    client.sendall(b'\1')
    init = recv_exact(client, 24)
    self.assertEqual(struct.unpack('!HH', init[:4]), (800, 480))
    self.assertEqual(struct.unpack('!4B3H3B3x', init[4:20]), (32, 24, 0, 1, 255, 255, 255, 0, 8, 16))
    self.assertEqual(recv_exact(client, struct.unpack('!I', init[20:])[0]), b'sunnypilot')
    return client

  def update(self, client, frame, decoder=None):
    # Exact request used by opengl-render-qnx, including its oversized rectangle.
    client.sendall(b'\x03\x00\x00\x00\x00\x00\xff\xff\xff\xff')
    self.assertTrue(self.server.requested.wait(2))
    self.server.submit(frame)
    header = struct.unpack('!BBHHHHHi', recv_exact(client, 16))
    self.assertEqual(header, (0, 0, 1, 0, 0, WIDTH, HEIGHT, 6 if decoder else 0))
    if decoder:
      size = struct.unpack('!I', recv_exact(client, 4))[0]
      self.assertEqual(decoder.decompress(recv_exact(client, size)), frame)
      self.assertFalse(decoder.eof)
      self.assertFalse(decoder.unused_data)
    else:
      self.assertEqual(recv_exact(client, len(frame)), frame)
    self.assertFalse(self.server.requested.is_set())

  def test_zlib_stream_and_reconnect(self):
    for _ in range(2):
      with self.connect() as client:
        self.assertFalse(self.server.requested.is_set())
        client.sendall(bytes.fromhex('02 00 00 02 00 00 00 06 00 00 00 00'))
        decoder = zlib.decompressobj()
        for color in (b'\xff\0\0\xff', b'\0\xff\0\xff', b'\0\0\xff\xff'):
          self.update(client, color * (WIDTH * HEIGHT), decoder)

  def test_raw_format_and_ignored_input(self):
    with self.connect() as client:
      client.sendall(bytes.fromhex('00 00 00 00 20 18 00 01 00 ff 00 ff 00 ff 00 08 10 00 00 00'))
      client.sendall(bytes.fromhex('02 00 00 01 00 00 00 00'))
      client.sendall(bytes.fromhex('04 01 00 00 00 00 00 41 05 00 00 01 00 01'))
      self.update(client, b'\x12\x34\x56\xff' * (WIDTH * HEIGHT))

  def test_disconnect_while_waiting_for_frame(self):
    with self.connect() as client:
      client.sendall(bytes.fromhex('03 01 00 00 00 00 03 20 01 e0'))
      self.assertTrue(self.server.requested.wait(2))
    with self.connect():
      self.assertFalse(self.server.requested.is_set())

  def test_disable_and_reenable(self):
    with self.connect() as client:
      client.sendall(bytes.fromhex('03 00 00 00 00 00 03 20 01 e0'))
      self.assertTrue(self.server.requested.wait(2))
      self.enabled.clear()
      self.assertEqual(client.recv(1), b'')
    self.assertFalse(self.server.requested.is_set())
    self.enabled.set()
    with self.connect():
      pass

  def test_slow_reader(self):
    serve = self.server._serve

    def small_send_buffer(client):
      client.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
      serve(client)

    with patch.object(self.server, '_serve', small_send_buffer), self.connect() as client:
      client.sendall(bytes.fromhex('03 00 00 00 00 00 03 20 01 e0'))
      self.assertTrue(self.server.requested.wait(2))
      frame = b'\x12\x34\x56\xff' * (WIDTH * HEIGHT)
      self.server.submit(frame)
      time.sleep(0.7)  # Exceed the socket timeout without losing part of the frame.
      self.assertEqual(struct.unpack('!BBHHHHHi', recv_exact(client, 16)), (0, 0, 1, 0, 0, WIDTH, HEIGHT, 0))
      self.assertEqual(recv_exact(client, len(frame)), frame)


if __name__ == '__main__':
  unittest.main()
