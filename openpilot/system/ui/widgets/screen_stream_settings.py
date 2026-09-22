"""本家の入力ウィジェットと翻訳カタログを使う画面配信設定。"""

import time

from openpilot.common.params import Params
from openpilot.system.ui.lib.application import RECORD, gui_app
from openpilot.system.ui.lib.multilang import tr, tr_noop
from openpilot.system.ui.lib.screen_stream_config import PARAM_KEYS, ScreenStreamConfig, parse_stream_setting
from openpilot.system.ui.widgets import DialogResult

STREAM_TITLE = tr_noop("UDP Screen Streaming")
STREAM_DESCRIPTION = tr_noop(
  "Stream the UI over Wi-Fi. Recording or display sleep pauses streaming. Anyone on the same network can view the stream."
)
SETTINGS = {
  'address': (tr_noop("Multicast Address"), tr_noop("IPv4 multicast address (224.0.1.0 - 239.255.255.255).")),
  'port': (tr_noop("UDP Port"), tr_noop("Port: 1 - 65535.")),
  'bitrate': (tr_noop("Bitrate (kbit/s)"), tr_noop("Bitrate: 250 - 8000 kbit/s.")),
  'ttl': (tr_noop("Multicast TTL"), tr_noop("TTL: 1 - 255. Use 1 for the local network.")),
}


class ScreenStreamSettings:
  def __init__(self, params: Params, compact: bool = False):
    self._params = params
    self._compact = compact
    self._next_refresh = 0.0
    self.items = {}
    for field, (title, description) in SETTINGS.items():
      if compact:
        from openpilot.selfdrive.ui.mici.widgets.button import BigButton
        item = BigButton(tr(title))
        item.set_click_callback(lambda f=field: self._edit(f))
      else:
        from openpilot.system.ui.widgets.list_view import button_item
        if gui_app.sunnypilot_ui():
          from openpilot.system.ui.sunnypilot.widgets.list_view import button_item_sp as button_item
        item = button_item(lambda t=title: tr(t), lambda: tr("CHANGE"), lambda d=description: tr(d),
                           callback=lambda f=field: self._edit(f))
      self.items[field] = item
    self.refresh(force=True)

  def _can_edit(self):
    return self._params.get_bool('ScreenStreamEnabled') and not RECORD

  def _value(self, field):
    try:
      value = self._params.get(PARAM_KEYS[field])
    except (TypeError, ValueError):
      value = None
    return str(getattr(ScreenStreamConfig(), field) if value is None else value)

  def refresh(self, force: bool = False):
    now = time.monotonic()
    if not force and now < self._next_refresh:
      return
    self._next_refresh = now + 0.2
    enabled = self._params.get_bool('ScreenStreamEnabled')
    for field, item in self.items.items():
      item.set_visible(enabled)
      if self._compact:
        item.set_text(tr(SETTINGS[field][0]))
        item.set_value(self._value(field))
        item.set_enabled(not RECORD)
      else:
        item.action_item.set_value(self._value(field))
        item.action_item.set_enabled(not RECORD)

  def _save(self, field: str, text: str):
    # 入力中に設定がOFFになった場合も保存を行わない。
    if not self._can_edit():
      return
    try:
      value = parse_stream_setting(field, text)
    except ValueError:
      self._edit(field, invalid=True)
      return
    self._params.put(PARAM_KEYS[field], value, block=True)
    self.refresh(force=True)

  def _edit(self, field: str, invalid: bool = False):
    if not self._can_edit():
      return
    title, description = SETTINGS[field]
    hint = (tr("Invalid value") + "\n" if invalid else "") + tr(description)
    if self._compact:
      from openpilot.selfdrive.ui.mici.widgets.dialog import BigInputDialog
      dialog = BigInputDialog(hint, "" if invalid else self._value(field),
                              confirm_callback=lambda text: self._save(field, text))
    else:
      from openpilot.system.ui.widgets.keyboard import Keyboard
      dialog = Keyboard(max_text_size=15, min_text_size=1)
      dialog.set_title(tr(title), hint)
      dialog.set_text(self._value(field))
      dialog.set_callback(lambda result: self._save(field, dialog.text) if result == DialogResult.CONFIRM else None)
    gui_app.push_widget(dialog)
