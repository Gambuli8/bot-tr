import time

from bot.scanner import Scanner
from bot.strategy import MS_1H, MS_5M, Bar5, H1Bar
from tests.conftest import make_settings


class FakeExecutor:
    def __init__(self):
        self.signals = []

    def handle(self, sig):
        self.signals.append(sig)
        return {"status": "narrated"}


def zone_bar(index, new_h1=True):
    t = index * MS_5M
    h1 = H1Bar(time=t - MS_1H, high=103, low=100.5, close=101.5, last_ph=105.0, last_pl=None, atr=2.0)
    return Bar5(index=index, time=t, high=102, low=100.6, close=101.8, prev_close=101.5, ph5=None, pl5=None,
                h1=h1, new_h1=new_h1, d_sup=100.0, d_res=None, d_tol=1.0)


def make_scanner(tmp_path, bars_by_call):
    from bot.store import Store
    settings = make_settings(tmp_path, symbols=["BTC-USDT"], strategy_source="internal")
    store = Store(settings.data_dir)
    notes, executor = [], FakeExecutor()
    scanner = Scanner(settings, client=None, store=store, executor=executor, notify=notes.append)
    calls = iter(bars_by_call)
    scanner._context = lambda symbol, n: next(calls)
    return scanner, store, executor, notes


def test_warmup_is_silent_and_rebuilds_setups(tmp_path):
    now_index = int(time.time() * 1000) // MS_5M
    old = [zone_bar(now_index - 500)]                 # evento viejo: no se narra ni opera
    scanner, store, executor, notes = make_scanner(tmp_path, [old])
    scanner.warmup()
    assert executor.signals == []
    assert "Motor de análisis listo" in notes[-1] and "BTC" in notes[-1]
    assert store.state["setups"]["BTC-USDT:LONG"]["stage"].startswith("en zona diaria")
    assert store.state["scanner"]["last_index"]["BTC-USDT"] == now_index - 500


def test_live_scan_dispatches_only_new_recent_bars(tmp_path):
    now_index = int(time.time() * 1000) // MS_5M
    warm = [Bar5(index=now_index - 3, time=(now_index - 3) * MS_5M, high=1, low=1, close=1, prev_close=1,
                 ph5=None, pl5=None, h1=None, new_h1=False, d_sup=None, d_res=None, d_tol=None)]
    live = warm + [zone_bar(now_index - 1)]
    scanner, store, executor, notes = make_scanner(tmp_path, [warm, live, live])
    scanner.warmup()
    scanner.scan_all()
    assert [s.event for s in executor.signals] == ["zone"]
    scanner.scan_all()                                # misma vela: no se repite
    assert len(executor.signals) == 1
    assert scanner.last_scan_ok is not None


def test_seconds_to_next_close(tmp_path):
    scanner, *_ = make_scanner(tmp_path, [])
    base = 1_800_000_000.0  # múltiplo de 5 min
    assert scanner.seconds_to_next_close(base + 60) == 240 + scanner.delay_s
