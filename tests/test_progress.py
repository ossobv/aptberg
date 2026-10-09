from aptberg.progress import (
    BAR_EVERY,
    BAR_WIDTH,
    LOG_EVERY,
    RATE_SMOOTHING,
    Progress,
    human,
)


def test_bar_redraws_often_but_smooths_the_rate(monkeypatch):
    "Bytes/files still redraw at BAR_EVERY; the rate estimate is damped"
    calls = []

    class FakeTqdm:
        def __init__(self, *a, **kw):
            calls.append(kw)

        def update(self, n):
            pass

        def set_postfix_str(self, *a, **kw):
            pass

        def close(self):
            pass

    monkeypatch.setattr('aptberg.progress.tqdm', FakeTqdm)
    Progress(100, 1, 'test', enabled=True).close()
    assert calls[0]['mininterval'] == BAR_EVERY
    assert calls[0]['smoothing'] == RATE_SMOOTHING
    assert calls[0]['ncols'] == BAR_WIDTH


def test_log_without_a_tty(monkeypatch, caplog):
    "No bar: a log line at most once every LOG_EVERY seconds"
    clock = [0.0]
    monkeypatch.setattr('aptberg.progress.time.monotonic', lambda: clock[0])
    progress = Progress(3 * 1024**2, 2, 'pool', enabled=False)
    caplog.set_level('INFO', 'aptberg.progress')
    progress(1024**2)
    clock[0] = LOG_EVERY
    progress(1024**2)
    progress.item_done()
    progress.close()
    assert caplog.messages == ['pool: 0/2 files, 2.0 MiB/3.0 MiB']


def test_human():
    "Bytes as they read best"
    assert human(512) == '512 B'
    assert human(1536) == '1.5 KiB'
    assert human(5 * 1024**4) == '5.0 TiB'
