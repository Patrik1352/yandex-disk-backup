"""Progress callbacks run synchronously; batch callbacks are serialized."""
from dataclasses import dataclass
import time
from typing import Callable


@dataclass(frozen=True)
class Progress:
    path: str
    direction: str
    transferred: int
    total: int
    elapsed: float
    attempt: int = 1
    done: bool = False
    files_completed: int = 0
    files_total: int = 1

    @property
    def percent(self) -> float:
        return min(100.0, self.transferred / self.total * 100) if self.total else (100.0 if self.done else 0.0)

    @property
    def bytes_per_second(self) -> float:
        return self.transferred / self.elapsed if self.elapsed > 0 else 0.0

    @property
    def eta_seconds(self) -> float | None:
        speed = self.bytes_per_second
        return max(0, self.total - self.transferred) / speed if speed else (0.0 if self.done else None)


ProgressCallback = Callable[[Progress], None]


class Reporter:
    def __init__(self, callback, path, direction, total):
        self.callback, self.path, self.direction, self.total = callback, path, direction, total
        self.started = time.monotonic()
        self.attempt = 1

    def emit(self, transferred, *, done=False):
        if self.callback:
            self.callback(Progress(self.path, self.direction, min(transferred, self.total), self.total,
                                   time.monotonic() - self.started, self.attempt, done, int(done)))

    def reset(self, attempt):
        self.attempt = attempt + 1
        self.started = time.monotonic()
        self.emit(0)
