"""配信のフレーム処理を待たせずに設定・接続状態を照会する。"""

from dataclasses import dataclass
import threading
import time
from collections.abc import Callable


@dataclass(frozen=True)
class QueryState:
  value: object = None
  checked: bool = False
  error: Exception | None = None
  generation: int = 0
  failures: int = 0


class CachedQuery:
  """失敗時も最後の正常値を保持する。停止中に完了した古い照会は採用しない。"""
  def __init__(self, query: Callable, interval: Callable, name: str):
    self._query, self._interval = query, interval
    self._condition = threading.Condition()
    self._active = self._closed = False
    self._epoch = 0
    self._state = QueryState()
    self._thread = threading.Thread(target=self._run, name=name, daemon=True)

  def start(self):
    self._thread.start()

  def set_active(self, active: bool):
    with self._condition:
      if active != self._active:
        self._active = active
        self._epoch += 1
        self._state = QueryState(generation=self._state.generation + 1)
        self._condition.notify_all()

  def snapshot(self) -> QueryState:
    with self._condition:
      return self._state

  def close(self):
    with self._condition:
      self._closed = True
      self._condition.notify_all()
    if self._thread.is_alive():
      self._thread.join(timeout=0.2)

  def _run(self):
    next_query, epoch = 0.0, -1
    while True:
      with self._condition:
        if self._closed:
          return
        if not self._active:
          self._condition.wait()
          continue
        if epoch != self._epoch:
          epoch, next_query = self._epoch, 0.0
        remaining = next_query - time.monotonic()
        if remaining > 0:
          self._condition.wait(remaining)
          continue
      # D-Bus・Paramsを呼ぶ間はロックを持たず、snapshotは常に即時に取得できる。
      try:
        value, error = self._query(), None
      except Exception as caught:
        value, error = None, caught
      with self._condition:
        if self._closed:
          return
        if epoch != self._epoch or not self._active:
          continue
        previous = self._state
        self._state = QueryState(previous.value if error else value, previous.checked or error is None, error,
                                 previous.generation + 1, previous.failures + 1 if error else 0)
        next_query = time.monotonic() + max(0.001, self._interval(self._state.value))


class LatencyStats:
  """短いロックで集計し、ログ整形はフレーム処理の外で行う。時間の入力単位は秒。"""
  COUNTERS = ('capture_count', 'submitted_count', 'queue_replaced_count', 'stale_drop_count', 'frames_written')
  TIMINGS = ('capture', 'gpu_scale', 'readback', 'bytes_copy', 'queue_age', 'stdin_write', 'frame_age_written')

  def __init__(self):
    self._lock = threading.Lock()
    self._reset()

  def _reset(self):
    self._counts = dict.fromkeys(self.COUNTERS, 0)
    self._timings = dict.fromkeys(self.TIMINGS, (0, 0.0, 0.0))

  def record(self, counts: dict | None = None, **timings):
    with self._lock:
      for name, count in (counts or {}).items():
        self._counts[name] += count
      for name, elapsed in timings.items():
        count, total, maximum = self._timings[name]
        self._timings[name] = count + 1, total + elapsed, max(maximum, elapsed)

  def snapshot(self, reset: bool = False):
    with self._lock:
      result = self._counts.copy()
      for name, (count, total, maximum) in self._timings.items():
        result[f'{name}_avg_ms'] = round(total * 1000 / count, 3) if count else 0.0
        result[f'{name}_max_ms'] = round(maximum * 1000, 3)
      if reset:
        self._reset()
      return result
