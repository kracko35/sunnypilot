"""診断専用のMPEG-TS/PES解析。送信の通常経路には組み込まない。"""

from bisect import bisect_right

TS_SIZE = 188
MARKER_BITS = 16
MARKER_HEIGHT = 48


def frame_with_marker(frame: bytes, index: int, width: int) -> bytes:
  if not 0 <= index < 2 ** MARKER_BITS or width % MARKER_BITS:
    raise ValueError('フレーム識別子または画像幅が範囲外です')
  row = b''.join((b'\xff\xff\xff\xff' if index & (1 << bit) else b'\0\0\0\xff') * (width // MARKER_BITS)
                 for bit in range(MARKER_BITS))
  return row * MARKER_HEIGHT + frame[width * 4 * MARKER_HEIGHT:]


def video_pes_events(ts: bytes, chunk_ends: list[int], chunk_times: list[float]):
  """本番FFmpegの単一映像出力を検査し、PES先頭byteを最初に観測したread時刻へ対応付ける。"""
  if len(ts) % TS_SIZE or not chunk_ends or chunk_ends[-1] != len(ts) or len(chunk_ends) != len(chunk_times):
    raise ValueError('TS長またはstdout観測記録が不正です')
  if any(a >= b for a, b in zip([0, *chunk_ends[:-1]], chunk_ends, strict=True)):
    raise ValueError('stdout境界は昇順でなければなりません')
  events, pids = [], set()
  for offset in range(0, len(ts), TS_SIZE):
    packet = ts[offset:offset + TS_SIZE]
    if packet[0] != 0x47 or packet[1] & 0x80 or packet[3] & 0xC0:
      raise ValueError('TS同期・エラー・暗号化フラグが不正です')
    if not packet[3] & 0x10 or not packet[1] & 0x40:
      continue
    start = 5 + packet[4] if packet[3] & 0x20 else 4
    payload = packet[start:]
    if len(payload) < 4 or payload[:3] != b'\0\0\1' or not 0xE0 <= payload[3] <= 0xEF:
      continue
    # この診断はFFmpegの映像PESヘッダーが先頭TSに収まる出力を対象とする。
    if len(payload) < 14 or not payload[7] & 0x80 or payload[8] < 5:
      raise ValueError('映像PESのPTSを取得できません')
    p = payload[9:14]
    if p[0] >> 4 not in (2, 3) or not (p[0] & p[2] & p[4] & 1):
      raise ValueError('PES timestampのマーカービットが不正です')
    pts = ((p[0] >> 1 & 7) << 30) | (p[1] << 22) | ((p[2] >> 1) << 15) | (p[3] << 7) | (p[4] >> 1)
    pids.add(((packet[1] & 31) << 8) | packet[2])
    events.append({'pts_90k': pts, 'stdout_first_observed_s': chunk_times[bisect_right(chunk_ends, offset + start)]})
  if len(pids) != 1:
    raise ValueError('診断には単一の映像PIDが必要です')
  return events


def match_frame_timings(arrivals, completions, events, decoded_ids, decoded_pts):
  """数だけで対応を推測せず、復号した識別子・PTSの一致を確認してから計測値を返す。"""
  count = len(arrivals)
  if not count or not (count == len(completions) == len(events) == len(decoded_ids) == len(decoded_pts)):
    raise ValueError('入力・PES・復号frame数が一致しません。対応付けを中止します')
  if decoded_ids != list(range(count)):
    raise ValueError('復号frame識別子に欠落・重複・順序変更があります')
  results = []
  for index, event in enumerate(events):
    relative_pts = ((event['pts_90k'] - events[0]['pts_90k']) % (1 << 33)) / 90000
    if abs(relative_pts - (decoded_pts[index] - decoded_pts[0])) > .001:
      raise ValueError('PESと復号frameのPTSが一致しません')
    results.append({'frame': index, 'input_arrival_s': arrivals[index], 'stdin_complete_s': completions[index], **event,
                    'stdin_to_stdout_ms': (event['stdout_first_observed_s'] - completions[index]) * 1000,
                    'arrival_to_stdout_ms': (event['stdout_first_observed_s'] - arrivals[index]) * 1000})
  return results
