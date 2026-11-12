import shutil
from collections.abc import Callable

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.repositories.market import drop_oldest_offer_chunk, storage_bytes
from app.db.session import SessionLocal

GIB = 1024**3

log = structlog.get_logger()


def disk_free_bytes() -> int:
    """Вільне місце файлової системи, на якій лежать Docker і база (корінь контейнера)."""
    return shutil.disk_usage("/").free


class StorageGuard:
    """Бюджет місця для даних збору: щоб колектори не з'їли весь диск сервера.

    Понад бюджет або коли на диску лишається менше min_free_bytes, видаляються найстаріші
    дні offer_snapshots — сирих оголошень, що займають майже все місце. market_stats для ML
    не чіпаються. Якщо видаляти вже нічого, окрім поточного дня, збір стає на паузу, доки
    місце не звільниться. Перевірка вільного місця рятує, коли бюджет більший за диск.
    """

    def __init__(
        self,
        budget_bytes: int,
        session_factory: async_sessionmaker[AsyncSession] = SessionLocal,
        min_free_bytes: int = 0,
        disk_free: Callable[[], int] = disk_free_bytes,
    ) -> None:
        self.budget_bytes = budget_bytes
        self.min_free_bytes = min_free_bytes
        self.used_bytes: int | None = None
        self.free_bytes: int | None = None
        self.full = False
        self._session_factory = session_factory
        self._disk_free = disk_free

    def _over(self, used: int) -> bool:
        self.free_bytes = self._disk_free()
        return used > self.budget_bytes or self.free_bytes < self.min_free_bytes

    async def enforce(self) -> None:
        async with self._session_factory() as session, session.begin():
            used = await storage_bytes(session)
            while self._over(used):
                dropped_until = await drop_oldest_offer_chunk(session)
                if dropped_until is None:
                    break
                previous, used = used, await storage_bytes(session)
                log.warning(
                    "collector.storage_trimmed",
                    dropped_until=dropped_until.isoformat(),
                    used_gb=round(used / GIB, 2),
                    budget_gb=round(self.budget_bytes / GIB, 2),
                    disk_free_gb=round((self.free_bytes or 0) / GIB, 2),
                )
                if used >= previous:
                    break

        was_full, self.used_bytes = self.full, used
        self.full = self._over(used)
        if self.full:
            log.error(
                "collector.storage_full",
                used_gb=round(used / GIB, 2),
                budget_gb=round(self.budget_bytes / GIB, 2),
                disk_free_gb=round((self.free_bytes or 0) / GIB, 2),
                min_free_gb=round(self.min_free_bytes / GIB, 2),
            )
        elif was_full:
            log.info("collector.storage_ok", used_gb=round(used / GIB, 2))
