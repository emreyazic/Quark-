from types import SimpleNamespace

from ui.main_window import MainWindow


class _FakeWorker:
    def __init__(self, running=True, stops_on_wait=True):
        self.running = running
        self.stops_on_wait = stops_on_wait
        self.cancelled = False
        self.signals_blocked = False
        self.waited = False

    def blockSignals(self, blocked):
        self.signals_blocked = blocked

    def cancel(self):
        self.cancelled = True

    def isRunning(self):
        return self.running

    def wait(self, timeout_ms):
        self.waited = True
        if self.stops_on_wait:
            self.running = False
            return True
        return False


def test_shutdown_cancels_blocks_and_waits_for_all_workers():
    first = _FakeWorker()
    second = _FakeWorker()
    state = SimpleNamespace(_background_workers=lambda: [first, second])

    assert MainWindow._stop_background_workers(state, timeout_ms=1000) is True
    assert all(worker.cancelled for worker in (first, second))
    assert all(worker.signals_blocked for worker in (first, second))
    assert all(worker.waited for worker in (first, second))


def test_shutdown_refuses_completion_while_worker_is_still_running():
    worker = _FakeWorker(stops_on_wait=False)
    state = SimpleNamespace(_background_workers=lambda: [worker])

    assert MainWindow._stop_background_workers(state, timeout_ms=1000) is False
