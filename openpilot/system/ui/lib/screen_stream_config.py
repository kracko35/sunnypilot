"""画面配信設定の既定値、永続化キー、共通の入力検証。"""

from dataclasses import dataclass, replace
import ipaddress
from typing import TYPE_CHECKING

if TYPE_CHECKING:
  from openpilot.common.params import Params

PARAM_KEYS = {
  'address': 'ScreenStreamAddress',
  'port': 'ScreenStreamPort',
  'bitrate': 'ScreenStreamBitrate',
  'ttl': 'ScreenStreamTtl',
}


@dataclass(frozen=True)
class ScreenStreamConfig:
  address: str = '239.255.42.99'
  port: int = 12346
  bitrate: int = 1000
  ttl: int = 1

  def __post_init__(self):
    address = ipaddress.IPv4Address(self.address)
    if (address.is_unspecified or address.is_loopback or address.is_link_local or address.is_reserved or int(address) >> 24 == 0
        or (address.is_multicast and int(address) < int(ipaddress.IPv4Address('224.0.1.0')))):
      raise ValueError('宛先には通常のユニキャストIPv4または224.0.1.0～239.255.255.255のマルチキャストIPv4を指定してください')
    for value, minimum, maximum in [(self.port, 1, 65535), (self.bitrate, 250, 8000), (self.ttl, 1, 255)]:
      if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError('画面配信の数値設定が範囲外です')

  @classmethod
  def from_params(cls, params: 'Params') -> 'ScreenStreamConfig':
    defaults = cls()
    values = {}
    for field, key in PARAM_KEYS.items():
      value = params.get(key)
      values[field] = getattr(defaults, field) if value is None else value
    return cls(**values)


def parse_stream_setting(field: str, text: str) -> str | int:
  """UIと外部設定で同じ検証を行い、URLなどの余分な文字列は受け付けない。"""
  if field not in PARAM_KEYS:
    raise ValueError('不明な画面配信設定です')
  text = text.strip()
  if field == 'address':
    value: str | int = str(ipaddress.IPv4Address(text))
  else:
    if not text.isascii() or not text.isdecimal():
      raise ValueError('整数を入力してください')
    value = int(text)
  replace(ScreenStreamConfig(), **{field: value})
  return value
