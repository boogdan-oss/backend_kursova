import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Literal

LimiterState = Literal["ok", "paused", "cooldown"]

MIN_RPS = 0.1
RPS_STEP = 0.5  # адитивне зростання
SUCCESSES_PER_STEP = 20
# Після блокування стеля нижча за швидкість, на якій воно сталося; перевіряти її можна лише
# зрідка: блокує WAF за кількістю запитів у вікні, і кожна спроба — ризик довшого бану IP
CEILING_FACTOR = 0.7
SUCCESSES_PER_PROBE = 5000
BACKOFF_FACTOR = 0.5  # мультиплікативне падіння на блокуванні
BLOCK_PAUSE_SEC = 5.0  # якщо біржа не надіслала Retry-After
BLOCKS_BEFORE_COOLDOWN = 3


class AdaptiveRateLimiter:
    """Token bucket + AIMD: сам знаходить максимальну безпечну швидкість біржі.

    Офіційних лімітів для P2P біржі не публікують (CLAUDE.md, розділ 5.1).
    Місткість bucket — один запит: запити йдуть рівномірно, без сплесків.
    Блокування знижує не лише поточну швидкість, а й стелю: limiter пам'ятає, де ліміт біржі.
    """

    def __init__(
        self,
        initial_rps: float,
        max_rps: float,
        cooldown_sec: float,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._rps = min(initial_rps, max_rps)
        self._ceiling = max_rps
        self._max_rps = max_rps
        self._cooldown_sec = cooldown_sec
        self._clock = clock
        self._sleep = sleep
        self._tokens = 1.0
        self._refilled_at = clock()
        self._paused_until = 0.0
        self._in_cooldown = False
        self._successes = 0
        self._blocks_in_row = 0

    @property
    def rps(self) -> float:
        return self._rps

    @property
    def ceiling(self) -> float:
        return self._ceiling

    @property
    def state(self) -> LimiterState:
        if self._clock() >= self._paused_until:
            return "ok"
        return "cooldown" if self._in_cooldown else "paused"

    @property
    def paused_for_sec(self) -> float:
        return max(self._paused_until - self._clock(), 0.0)

    async def acquire(self) -> None:
        """Чекає дозволу на один запит."""
        while True:
            now = self._clock()
            elapsed = now - self._refilled_at
            self._tokens = min(1.0, self._tokens + elapsed * self._rps)
            self._refilled_at = now
            if now < self._paused_until:
                wait = self._paused_until - now
            elif self._tokens >= 1.0:
                self._tokens -= 1.0
                return
            else:
                wait = (1.0 - self._tokens) / self._rps
            await self._sleep(wait)

    def on_success(self) -> None:
        self._blocks_in_row = 0
        self._successes += 1
        at_ceiling = self._rps >= self._ceiling
        if self._successes < (SUCCESSES_PER_PROBE if at_ceiling else SUCCESSES_PER_STEP):
            return
        self._successes = 0
        if at_ceiling:
            self._ceiling = min(self._max_rps, self._ceiling + RPS_STEP)
        self._rps = min(self._ceiling, self._rps + RPS_STEP)

    def on_error(self) -> None:
        """Збій мережі або сервера — не блокування: швидкість не падає, але й не зростає."""
        self._successes = 0

    def on_blocked(self, retry_after: float | None = None) -> None:
        now = self._clock()
        self._successes = 0
        if now < self._paused_until:
            # Відповіді запитів, що вже були в дорозі, — це те саме блокування
            self._paused_until = max(self._paused_until, now + (retry_after or 0.0))
            return

        # Лише перше блокування серії каже про швидкість: наступні — це ще той самий бан,
        # і від зниженої вже швидкості стеля впала б майже до нуля
        if self._blocks_in_row == 0:
            self._ceiling = max(MIN_RPS, self._rps * CEILING_FACTOR)
        self._rps = max(MIN_RPS, self._rps * BACKOFF_FACTOR)
        self._blocks_in_row += 1
        self._in_cooldown = self._blocks_in_row >= BLOCKS_BEFORE_COOLDOWN
        if self._in_cooldown:
            pause = max(self._cooldown_sec, retry_after or 0.0)
        else:
            pause = BLOCK_PAUSE_SEC if retry_after is None else retry_after
        self._paused_until = now + pause
