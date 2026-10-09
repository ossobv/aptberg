"""Thread-safe byte progress: a bar on a tty, log lines otherwise"""

import logging
import sys
import time
from threading import Lock

from tqdm import tqdm

log = logging.getLogger(__name__)

LOG_EVERY = 60.0
# Bytes and files done should still move at least this often; it is the
# rate and ETA that need smoothing, not the redraw itself.
BAR_EVERY = 0.2
# Many threads each report small chunks, so the raw interval between
# redraws is noisy; weight the rate estimate towards its running
# average (0) rather than the latest interval (1, tqdm's default 0.3)
# so it does not swing with that noise.
RATE_SMOOTHING = 0.05
# Fixed, not the terminal's actual width: a bar copied out of a wide
# terminal into a narrower place (chat, a ticket) should not wrap.
BAR_WIDTH = 99


class Progress:
    """Counts bytes and finished items from many threads

    tqdm.update is not thread-safe, hence the lock. Without a tty (cron)
    there is no bar, just a log line at most once a minute: a scrolling
    bar in a mail spool helps nobody.
    """

    def __init__(
        self,
        total_bytes: int,
        total_items: int,
        desc: str,
        enabled: bool | None = None,
    ) -> None:
        if enabled is None:
            enabled = sys.stderr.isatty()
        self.desc = desc
        self.total_items = total_items
        self.total_bytes = total_bytes
        self.items = 0
        self.bytes = 0
        self._lock = Lock()
        self._last = time.monotonic()
        self._bar = tqdm(
            total=total_bytes,
            unit='B',
            unit_scale=True,
            unit_divisor=1024,
            desc=desc,
            ncols=BAR_WIDTH,
            mininterval=BAR_EVERY,
            smoothing=RATE_SMOOTHING,
            disable=not enabled,
        )
        self._enabled = enabled

    def __call__(self, n: int) -> None:
        with self._lock:
            self.bytes += n
            self._bar.update(n)
            self._maybe_log()

    def item_done(self) -> None:
        "Count one finished item"
        with self._lock:
            self.items += 1
            self._bar.set_postfix_str(
                f'{self.items}/{self.total_items} files', refresh=False
            )
            self._maybe_log()

    def close(self) -> None:
        "Close the bar"
        self._bar.close()

    def _maybe_log(self) -> None:
        now = time.monotonic()
        if self._enabled or now - self._last < LOG_EVERY:
            return
        self._last = now
        log.info(
            '%s: %d/%d files, %s/%s',
            self.desc,
            self.items,
            self.total_items,
            human(self.bytes),
            human(self.total_bytes),
        )


def human(n: int) -> str:
    "A byte count for humans"
    for unit in ('B', 'KiB', 'MiB', 'GiB'):
        if abs(n) < 1024:
            return f'{n:.1f} {unit}' if unit != 'B' else f'{n} B'
        n /= 1024
    return f'{n:.1f} TiB'
