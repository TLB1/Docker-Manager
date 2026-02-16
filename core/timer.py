import threading
from typing import Callable, Dict



class RunnableTimer:



    def __init__(self):
        self._timers: Dict[str, threading.Timer] = {}
        self._lock = threading.Lock()



    def startOrRenew(self, key: str, timeout_seconds: int, callback: Callable[[], None]):
        def _wrapped():
            try:
                callback()
            finally:
                with self._lock:
                    self._timers.pop(key, None)

        timer = threading.Timer(timeout_seconds, _wrapped)
        timer.daemon = True

        with self._lock:
            old = self._timers.get(key)
            if old:
                old.cancel()
            self._timers[key] = timer

        timer.start()



    def cancel(self, key: str):
        """Cancel timeout for key."""
        with self._lock:
            timer = self._timers.pop(key, None)
            if timer:
                timer.cancel()
