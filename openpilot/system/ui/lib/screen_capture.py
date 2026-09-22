"""録画と配信で共有するRenderTextureの読み出し。GPU操作はUIスレッドで行う。"""

import time
import pyray as rl

from openpilot.system.ui.lib.screen_stream import WIDTH, HEIGHT, FPS, ScreenStreamer


def read_rgba(texture: rl.Texture) -> bytes:
  image = rl.load_image_from_texture(texture)
  try:
    return bytes(rl.ffi.buffer(image.data, image.width * image.height * 4))
  finally:
    rl.unload_image(image)


class ScreenStreamCapture:
  def __init__(self, streamer: ScreenStreamer):
    self.streamer = streamer
    self._texture = None
    self._next_frame = 0.0

  def capture(self, source: rl.Texture):
    now = time.monotonic()
    if not self.streamer.ready.is_set() or now < self._next_frame:
      return
    self._next_frame += (int((now - self._next_frame) * FPS) + 1) / FPS
    if self._texture is None:
      self._texture = rl.load_render_texture(WIDTH, HEIGHT)
    # 元画面の縦横比を保持し、余白を黒で埋める。
    scale = min(WIDTH / source.width, HEIGHT / source.height)
    width, height = source.width * scale, source.height * scale
    rl.begin_texture_mode(self._texture)
    rl.clear_background(rl.BLACK)
    rl.draw_texture_pro(source, rl.Rectangle(0, 0, source.width, -source.height),
                        rl.Rectangle((WIDTH - width) / 2, (HEIGHT - height) / 2, width, height),
                        rl.Vector2(0, 0), 0.0, rl.WHITE)
    rl.end_texture_mode()
    self.streamer.submit(read_rgba(self._texture.texture))

  def release(self):
    if self._texture is not None:
      rl.unload_render_texture(self._texture)
      self._texture = None

  def close(self):
    self.streamer.close()
    self.release()
