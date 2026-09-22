"""Windowsの限定テスト環境ではswaglogのネイティブ依存だけを差し替える。"""

import importlib
import sys
from types import ModuleType
from unittest.mock import Mock


def load_screen_stream() -> ModuleType:
  name = 'openpilot.common.swaglog'
  stub = sys.platform == 'win32' and name not in sys.modules
  if stub:
    module = ModuleType(name)
    module.cloudlog = Mock()
    sys.modules[name] = module
  try:
    return importlib.import_module('openpilot.system.ui.lib.screen_stream')
  finally:
    if stub:
      del sys.modules[name]
