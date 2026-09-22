"""設定UIの保存・表示と、本家のPOローダーによる言語切替を検証する。"""

import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from openpilot.selfdrive.ui.translations.potools import extract_strings, parse_po
from openpilot.system.ui.lib.screen_stream_config import ScreenStreamConfig

ROOT = Path(__file__).resolve().parents[5]
UI_SOURCE = 'openpilot/system/ui/widgets/screen_stream_settings.py'


def load_module(name, path):
  spec = importlib.util.spec_from_file_location(name, ROOT / path)
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  return module


class FakeParams:
  def __init__(self):
    self.values = {'ScreenStreamEnabled': False}

  def get(self, key):
    return self.values.get(key)

  def get_bool(self, key):
    return bool(self.values.get(key, False))

  def put(self, key, value, block=False):
    self.values[key] = value


class FakeItem:
  def __init__(self, title, *args, callback=None, **kwargs):
    self.title = title
    self.callback = callback
    self.action_item = self

  def set_visible(self, visible):
    self.visible = visible

  def set_enabled(self, enabled):
    self.enabled = enabled

  def set_value(self, value):
    self.value = value

  def set_text(self, text):
    self.title = text

  def set_click_callback(self, callback):
    self.callback = callback


class TestScreenStreamSettings(unittest.TestCase):
  def setUp(self):
    # ネイティブParamsと描画のみ差し替え、翻訳ローダーと設定UIの実コードを使う。
    self.gui = Mock()
    self.gui.sunnypilot_ui.return_value = False
    stubs = {
      'openpilot.common.params': SimpleNamespace(Params=None),
      'openpilot.common.swaglog': SimpleNamespace(cloudlog=Mock()),
    }
    with patch.dict(sys.modules, stubs):
      self.multilang = load_module('stream_test_multilang', 'openpilot/system/ui/lib/multilang.py')
    self.params = FakeParams()
    stubs.update({
      'openpilot.common.params': SimpleNamespace(Params=FakeParams),
      'openpilot.system.ui.lib.application': SimpleNamespace(RECORD=False, gui_app=self.gui),
      'openpilot.system.ui.lib.multilang': self.multilang,
      'openpilot.system.ui.widgets': SimpleNamespace(DialogResult=SimpleNamespace(CONFIRM=1, CANCEL=0)),
      'openpilot.system.ui.widgets.list_view': SimpleNamespace(button_item=FakeItem),
      'openpilot.system.ui.sunnypilot.widgets.list_view': SimpleNamespace(button_item_sp=FakeItem),
      'openpilot.selfdrive.ui.mici.widgets.button': SimpleNamespace(BigButton=FakeItem),
      'openpilot.system.ui.widgets.keyboard': SimpleNamespace(Keyboard=Mock()),
      'openpilot.selfdrive.ui.mici.widgets.dialog': SimpleNamespace(BigInputDialog=Mock()),
    })
    patcher = patch.dict(sys.modules, stubs)
    patcher.start()
    self.addCleanup(patcher.stop)
    self.ui = load_module('stream_test_settings', UI_SOURCE)

  def test_fields_follow_toggle_and_recording_on_both_layouts(self):
    for compact in [False, True]:
      with self.subTest(compact=compact):
        self.params.values['ScreenStreamEnabled'] = False
        self.ui.RECORD = False
        settings = self.ui.ScreenStreamSettings(self.params, compact=compact)
        self.assertTrue(all(not item.visible for item in settings.items.values()))
        self.params.values['ScreenStreamEnabled'] = True
        settings.refresh(force=True)
        self.assertTrue(all(item.visible and item.enabled for item in settings.items.values()))
        self.ui.RECORD = True
        settings.refresh(force=True)
        self.assertTrue(all(not item.enabled for item in settings.items.values()))
        self.params.values['ScreenStreamEnabled'] = False
        settings.refresh(force=True)
        self.assertTrue(all(not item.visible for item in settings.items.values()))

  def test_valid_save_updates_persistent_value_and_display(self):
    self.params.values['ScreenStreamEnabled'] = True
    settings = self.ui.ScreenStreamSettings(self.params)
    settings._save('address', '239.20.30.40')
    settings._save('port', '23456')
    settings._save('bitrate', '3000')
    settings._save('ttl', '2')
    self.assertEqual(ScreenStreamConfig.from_params(self.params), ScreenStreamConfig('239.20.30.40', 23456, 3000, 2))
    self.assertEqual(settings.items['port'].value, '23456')

  def test_bitrate_display_uses_default_or_preserves_saved_value(self):
    for compact in [False, True]:
      for saved, expected in [(None, '1000'), (1500, '1500')]:
        with self.subTest(compact=compact, saved=saved):
          self.params.values = {'ScreenStreamEnabled': True}
          if saved is not None:
            self.params.values['ScreenStreamBitrate'] = saved
          before = self.params.values.copy()
          settings = self.ui.ScreenStreamSettings(self.params, compact=compact)
          self.assertEqual(settings.items['bitrate'].value, expected)
          self.assertEqual(self.params.values, before)

  def test_invalid_save_preserves_previous_value_and_reopens_editor(self):
    self.params.values.update(ScreenStreamEnabled=True, ScreenStreamPort=12346)
    settings = self.ui.ScreenStreamSettings(self.params)
    with patch.object(settings, '_edit') as edit:
      settings._save('port', '65536')
      edit.assert_called_once_with('port', invalid=True)
    self.assertEqual(self.params.values['ScreenStreamPort'], 12346)

  def test_both_layouts_save_unicast_and_multicast_destinations(self):
    self.params.values['ScreenStreamEnabled'] = True
    for compact in [False, True]:
      settings = self.ui.ScreenStreamSettings(self.params, compact=compact)
      for address in ['192.168.4.44', '10.0.0.20', '239.255.42.99']:
        with self.subTest(compact=compact, address=address):
          settings._save('address', address)
          self.assertEqual(ScreenStreamConfig.from_params(self.params).address, address)
          self.assertEqual(settings.items['address'].value, address)

  def test_edit_cancel_and_disabled_save_do_not_change_params(self):
    self.params.values['ScreenStreamEnabled'] = True
    settings = self.ui.ScreenStreamSettings(self.params)
    settings._edit('port')
    keyboard = self.gui.push_widget.call_args.args[0]
    callback = keyboard.set_callback.call_args.args[0]
    callback(self.ui.DialogResult.CANCEL)
    self.assertNotIn('ScreenStreamPort', self.params.values)
    self.params.values['ScreenStreamEnabled'] = False
    settings._save('port', '23456')
    self.assertNotIn('ScreenStreamPort', self.params.values)

  def test_language_switch_uses_standard_catalogs(self):
    self.assertEqual(self.ui.STREAM_TITLE, 'Screen Streaming')
    self.assertEqual(self.multilang.tr(self.ui.STREAM_TITLE), 'Screen Streaming')
    settings = self.ui.ScreenStreamSettings(self.params)
    compact = self.ui.ScreenStreamSettings(self.params, compact=True)
    self.assertEqual(settings.items['address'].title(), 'Destination Address')
    self.multilang.multilang._language = 'ja'
    self.multilang.multilang.setup()
    compact.refresh(force=True)
    self.assertEqual(settings.items['address'].title(), '送信先アドレス')
    self.assertEqual(compact.items['address'].title, '送信先アドレス')
    self.assertEqual(self.multilang.tr(self.ui.STREAM_TITLE), '画面ストリーミング')

  def test_saved_settings_survive_display_name_change_on_both_layouts(self):
    saved = {'ScreenStreamEnabled': True, 'ScreenStreamAddress': '192.168.4.44', 'ScreenStreamPort': 23456,
             'ScreenStreamBitrate': 1000, 'ScreenStreamTtl': 2}
    self.params.values = saved.copy()
    for compact in [False, True]:
      settings = self.ui.ScreenStreamSettings(self.params, compact=compact)
      self.assertTrue(all(item.visible and item.enabled for item in settings.items.values()))
      self.assertEqual(settings.items['address'].value, saved['ScreenStreamAddress'])
      self.assertEqual(settings.items['port'].value, '23456')
      self.assertEqual(settings.items['bitrate'].value, '1000')
      self.assertEqual(ScreenStreamConfig.from_params(self.params), ScreenStreamConfig('192.168.4.44', 23456, 1000, 2))
      self.assertEqual(self.params.values, saved)

  def test_all_supported_languages_include_translated_stream_strings(self):
    entries = extract_strings([UI_SOURCE], str(ROOT))
    catalog_dir = ROOT / 'openpilot/selfdrive/ui/translations'
    template = {entry.msgid for entry in parse_po(catalog_dir / 'app.pot')[1]}
    for language in self.multilang.multilang.languages.values():
      translations, _ = self.multilang.load_translations(catalog_dir / f'app_{language}.po')
      for entry in entries:
        with self.subTest(language=language, text=entry.msgid):
          self.assertTrue(entry.msgid.isascii())
          self.assertIn(entry.msgid, template)
          self.assertTrue(translations.get(entry.msgid))


if __name__ == '__main__':
  unittest.main()
