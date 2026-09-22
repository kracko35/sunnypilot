"""映像を保存せず、限定したTSヘッダーと入力順序から配信の区間時間を観測する。"""

from collections import deque
from dataclasses import dataclass
import math
import os
from pathlib import Path
import sys
import threading
import time

TS_SIZE = 188
MAX_PENDING = 32
MAX_SAMPLES = 200
MAX_PENDING_AGE = 2.0
PTS_GAP_LIMIT = 2.0
PTS_INPUT_TOLERANCE = .100
STAGES = ('capture_to_pes', 'stdin_start_to_pes', 'stdin_complete_to_pes', 'pes_to_send_attempt', 'pes_to_send', 'capture_to_udp_send')


@dataclass(frozen=True)
class PesEvent:
  packet_offset: int
  pts: int
  observed_at: float


class TsPesParser:
  """先頭から整列した単一映像TS専用。残すbytesは未完成TSの最大187 bytesだけ。"""
  def __init__(self):
    self.tail = b''
    self.tail_times = []
    self.offset = 0
    self.video_pid = self.cc = None

  def feed(self, chunk: bytes, observed_at: float) -> list[PesEvent]:
    old_size = len(self.tail)
    data = self.tail + chunk if old_size else chunk
    view = memoryview(data)
    complete = len(data) // TS_SIZE * TS_SIZE
    events = []
    for offset in range(0, complete, TS_SIZE):
      p = view[offset:offset + TS_SIZE]
      if p[0] != 0x47 or p[1] & 0x80 or p[3] & 0xC0 or not p[3] & 0x30:
        raise ValueError('invalid_ts')
      pid, cc = ((p[1] & 31) << 8) | p[2], p[3] & 15
      adaptation = bool(p[3] & 0x20)
      start = 5 + p[4] if adaptation else 4
      if start > TS_SIZE:
        raise ValueError('invalid_adaptation')
      if pid == self.video_pid and adaptation and p[4] and p[5] & 0x80:
        raise ValueError('ts_discontinuity')
      if not p[3] & 0x10:
        continue
      if pid == self.video_pid:
        if cc != (self.cc + 1) % 16:
          raise ValueError('ts_continuity')
        self.cc = cc
      if not p[1] & 0x40 or start + 4 > TS_SIZE:
        continue
      if p[start:start + 3] != b'\0\0\1' or not 0xE0 <= p[start + 3] <= 0xEF:
        continue
      if self.video_pid is not None and pid != self.video_pid:
        raise ValueError('multiple_video_pids')
      self.video_pid, self.cc = pid, cc
      if start + 14 > TS_SIZE or not p[start + 7] & 0x80 or p[start + 8] < 5:
        raise ValueError('unsupported_pes_header')
      pts = p[start + 9:start + 14]
      if pts[0] >> 4 not in (2, 3) or not (pts[0] & pts[2] & pts[4] & 1):
        raise ValueError('invalid_pts')
      value = ((pts[0] >> 1 & 7) << 30) | (pts[1] << 22) | ((pts[2] >> 1) << 15) | (pts[3] << 7) | (pts[4] >> 1)
      byte = offset + start
      events.append(PesEvent(self.offset + offset, value, self.tail_times[byte] if byte < old_size else observed_at))
    self.tail = bytes(view[complete:])
    self.tail_times = [self.tail_times[i] if i < old_size else observed_at for i in range(complete, len(data))]
    self.offset += complete
    return events


@dataclass
class FrameTimingRecord:
  sequence: int
  captured_at: float
  dequeued_at: float
  stdin_started_at: float
  stdin_completed_at: float | None = None
  pes: PesEvent | None = None
  send_started_at: float | None = None
  sent_at: float | None = None
  dropped: bool = False


class FrameTimingObserver:
  """整合性検査を通った先頭recordから確定する。順序対応は映像identityの証明ではない。"""
  def __init__(self, clock=time.monotonic):
    self.clock = clock
    self.parser = TsPesParser()
    self._lock = threading.Lock()
    self._records = deque()
    self._unpaired = deque()
    self._datagrams = deque()
    self._last_sequence = -1
    self._last_pts = self._last_start = None
    self._anchor_start = None
    self._pts_elapsed = 0.0
    self._pending_max = 0
    self._parse_calls = 0
    self._parse_total = self._parse_max = 0.0
    self.active = True
    self.reason = 'none'
    self._counts = dict.fromkeys(('encoder_pes_samples', 'pes_events', 'diag_sync_lost', 'frame_first_datagram_drop_count'), 0)
    self._samples = {key: deque(maxlen=MAX_SAMPLES) for key in STAGES}

  def _lose(self, reason):
    if self.active:
      self._counts['diag_sync_lost'] += 1
    self.active, self.reason = False, reason
    self._records.clear()
    self._unpaired.clear()
    self._datagrams.clear()
    self.parser = TsPesParser()

  def _expire(self, now):
    if self._records and now - self._records[0].stdin_started_at > MAX_PENDING_AGE:
      self._lose('pending_timeout')

  def begin(self, sequence, captured_at, dequeued_at, started_at):
    with self._lock:
      self._expire(started_at)
      if not self.active:
        return
      if not captured_at <= dequeued_at <= started_at:
        self._lose('input_timestamps')
        return
      if sequence <= self._last_sequence or len(self._records) >= MAX_PENDING:
        self._lose('sequence_or_capacity')
        return
      self._last_sequence = sequence
      record = FrameTimingRecord(sequence, captured_at, dequeued_at, started_at)
      self._records.append(record)
      self._unpaired.append(record)
      self._pending_max = max(self._pending_max, len(self._records))

  def complete(self, sequence, completed_at, success=True):
    with self._lock:
      self._expire(completed_at)
      if not self.active:
        return
      if (not success or not self._records or self._records[-1].sequence != sequence
          or completed_at < self._records[-1].stdin_started_at):
        self._lose('incomplete_input')
        return
      self._records[-1].stdin_completed_at = completed_at
      self._commit_ready_prefix()

  def observe(self, chunk, observed_at):
    if not self.active:
      return
    # chunk単位で解析・対応検査を計測する。TS packet単位の時計読み取りは追加しない。
    started = time.perf_counter()
    try:
      self._observe(chunk, observed_at)
    finally:
      elapsed = time.perf_counter() - started
      with self._lock:
        self._parse_calls += 1
        self._parse_total += elapsed
        self._parse_max = max(self._parse_max, elapsed)

  def _observe(self, chunk, observed_at):
    # 解析はsenderだけで行い、入力側が待つロックの外に置く。
    try:
      events = self.parser.feed(chunk, observed_at)
    except (ValueError, IndexError) as error:
      with self._lock:
        self._lose(str(error))
      return
    if not events:
      return
    with self._lock:
      self._expire(observed_at)
      if not self.active:
        return
      for event in events:
        self._counts['pes_events'] += 1
        if not self._unpaired:
          self._lose('unexpected_pes')
          return
        record = self._unpaired.popleft()
        if event.observed_at < record.stdin_started_at:
          self._lose('pes_before_input')
          return
        if self._last_pts is not None:
          gap = ((event.pts - self._last_pts) % (1 << 33)) / 90000
          input_gap = record.stdin_started_at - self._last_start
          if not 0 < gap <= PTS_GAP_LIMIT or abs(gap - input_gap) > PTS_INPUT_TOLERANCE:
            self._lose('pts_discontinuity')
            return
          self._pts_elapsed += gap
          # 隣接gapだけでなく初回対応からの累積ずれも検査する。wrapは上の差分で展開済み。
          if abs(self._pts_elapsed - (record.stdin_started_at - self._anchor_start)) > PTS_INPUT_TOLERANCE:
            self._lose('pts_input_drift')
            return
        else:
          self._anchor_start = record.stdin_started_at
        self._last_pts, self._last_start = event.pts, record.stdin_started_at
        record.pes = event
        self._datagrams.append(record)

  def datagram(self, start, end, returned_at, sent, attempted_at=None):
    # PESを含まないdatagramはロックを取らない。追加は同じsenderスレッドだけで行う。
    if not self.active:
      return
    try:
      first = self._datagrams[0]
    except IndexError:
      return
    if first.pes.packet_offset >= end:
      return
    with self._lock:
      self._expire(returned_at)
      while self._datagrams and self._datagrams[0].pes.packet_offset < end:
        record = self._datagrams.popleft()
        if record.pes.packet_offset < start:
          self._lose('datagram_mapping')
          return
        record.send_started_at = returned_at if attempted_at is None else attempted_at
        if not record.pes.observed_at <= record.send_started_at <= returned_at:
          self._lose('send_timestamps')
          return
        record.sent_at, record.dropped = returned_at, not sent
      self._commit_ready_prefix()

  def _commit_ready_prefix(self):
    # 後続の未対応入力は正常なpipeline depthとして許容する。全体drainは待たない。
    # TS/PTS・入力時刻・datagram検査済みでも、初回からの一定frame shiftは完全検出できない。
    # 固定FFmpeg構成のmarker復号試験で順序前提を検証し、本番のidentity証明とは区別する。
    while self.active and self._records:
      record = self._records[0]
      if record.pes is None or record.stdin_completed_at is None or record.sent_at is None:
        break
      self._records.popleft()
      pes = record.pes.observed_at
      self._counts['encoder_pes_samples'] += 1
      self._samples['capture_to_pes'].append(pes - record.captured_at)
      self._samples['stdin_start_to_pes'].append(pes - record.stdin_started_at)
      self._samples['stdin_complete_to_pes'].append(pes - record.stdin_completed_at)
      self._samples['pes_to_send_attempt'].append(record.send_started_at - pes)
      if record.dropped:
        self._counts['frame_first_datagram_drop_count'] += 1
      else:
        self._samples['pes_to_send'].append(record.sent_at - pes)
        self._samples['capture_to_udp_send'].append(record.sent_at - record.captured_at)

  def snapshot(self, now, elapsed, final=False):
    with self._lock:
      self._expire(now)
      if final and self._records:
        self._lose('unfinished_at_close')
      result = self._counts.copy()
      self._counts = dict.fromkeys(self._counts, 0)
      samples, self._samples = self._samples, {key: deque(maxlen=MAX_SAMPLES) for key in STAGES}
      result.update(diag_active=int(self.active), diag_reason=self.reason, diag_pending=len(self._records), diag_pending_max=self._pending_max,
                    observer_parse_calls=self._parse_calls, observer_parse_total_ms=round(self._parse_total * 1000, 3),
                    observer_parse_avg_us=round(self._parse_total * 1e6 / self._parse_calls, 3) if self._parse_calls else 0.0,
                    observer_parse_max_us=round(self._parse_max * 1e6, 3))
      self._pending_max = len(self._records)
      self._parse_calls = 0
      self._parse_total = self._parse_max = 0.0
    for key, values in samples.items():
      values = sorted(values)
      result[key + '_samples'] = len(values)
      result[key + '_avg_ms'] = round(sum(values) * 1000 / len(values), 3) if values else None
      result[key + '_p95_ms'] = round(values[math.ceil(len(values) * .95) - 1] * 1000, 3) if values else None
      result[key + '_max_ms'] = round(values[-1] * 1000, 3) if values else None
    result['pes_events_per_sec'] = round(result['pes_events'] / max(elapsed, 1e-6), 3)
    result['frames_paired_per_sec'] = round(result['encoder_pes_samples'] / max(elapsed, 1e-6), 3)
    return result


class ProcessUsage:
  """stats出力時だけ/procを読む。CPUは1コアを100%とし、初回は基準値だけ取得する。"""
  def __init__(self, pid):
    self.pid = pid
    self.previous = None

  def sample(self, now):
    result = {'ffmpeg_cpu_pct': None, 'ffmpeg_rss_kb': None}
    if not sys.platform.startswith('linux'):
      return result
    try:
      fields = Path(f'/proc/{self.pid}/stat').read_text().rsplit(')', 1)[1].split()
      ticks, identity, pages = int(fields[11]) + int(fields[12]), int(fields[19]), int(fields[21])
      hz, page_size = os.sysconf('SC_CLK_TCK'), os.sysconf('SC_PAGE_SIZE')
      result['ffmpeg_rss_kb'] = pages * page_size // 1024
      if self.previous is not None:
        old_time, old_ticks, old_identity = self.previous
        if identity == old_identity and ticks >= old_ticks and now > old_time:
          result['ffmpeg_cpu_pct'] = round((ticks - old_ticks) / hz / (now - old_time) * 100, 3)
      self.previous = now, ticks, identity
    except (OSError, ValueError, IndexError, AttributeError):
      self.previous = None
    return result
