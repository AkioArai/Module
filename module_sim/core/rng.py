"""Именованные детерминированные потоки случайности.

Инвариант И2 (CLAUDE.md): вся случайность в игре идёт отсюда. Голый ``random``
в ``core/`` запрещён и проверяется тестом.

Устройство: поток — **чистая функция** ``(seed, имя потока, счётчик)``. Никакого
внутреннего состояния генератора не существует, счётчик — просто целое число.
Из этого следуют три важных для игры свойства:

1. В сейв пишутся только счётчики (SAVEFORMAT.md, §2). Формат состояния
   генератора не может измениться между версиями Python и сломать партию.
2. Добавление нового потока в новой версии игры не сдвигает последовательности
   существующих потоков. Старые сейвы продолжают считаться так же — это прямая
   поддержка требования «обновление не теряет прогресс».
3. Значение можно получить, **не сдвигая** счётчик (``at()``), и перепрыгнуть
   через N значений за O(1) (``skip()``). Это то, на чём держится батчевый
   догон (DESIGN.md, §Р2): блок из тысячи часов должен уметь взять ровно те же
   числа, что взял бы потиковый прогон.

Примечание о переносимости: сырые целочисленные draw-ы воспроизводятся
бит-в-бит везде, потому что blake2b — фиксированный алгоритм. Производные
величины (``normal``, ``weibull``) проходят через libm и теоретически могут
разойтись в последнем разряде между платформами. Канонической
последовательностью считается целочисленная.
"""

from __future__ import annotations

import hashlib
import math

__all__ = ["Rng", "RngStream"]

_UINT64 = 1 << 64
_KEY_SIZE = 32


def _stream_key(seed: int, name: str) -> bytes:
    """Ключ потока: свёртка seed и имени. Разные имена — независимые потоки."""
    material = f"{seed}\x00{name}".encode()
    return hashlib.blake2b(material, digest_size=_KEY_SIZE).digest()


class RngStream:
    """Один именованный поток. Дёшево создаётся, состояние — одно целое."""

    __slots__ = ("_key", "counter", "name")

    def __init__(self, seed: int, name: str, counter: int = 0) -> None:
        self.name = name
        self.counter = counter
        self._key = _stream_key(seed, name)

    # -- сырой доступ ----------------------------------------------------

    def raw_at(self, counter: int) -> int:
        """64-битное целое по индексу. Чистая функция, счётчик не двигает."""
        digest = hashlib.blake2b(
            counter.to_bytes(8, "big"),
            digest_size=8,
            key=self._key,
        ).digest()
        return int.from_bytes(digest, "big")

    def at(self, counter: int) -> float:
        """Значение в [0, 1) по индексу. Чистая функция, счётчик не двигает."""
        return self.raw_at(counter) / _UINT64

    def skip(self, n: int) -> None:
        """Промотать n значений за O(1).

        Применимо **только** к потокам с заранее известным расходом на час:
        промотать можно то, число обращений к чему вычисляется, а не
        разыгрывается. Поток, из которого тянет обработчик события, мотать
        нельзя — сколько он возьмёт, зависит от того, сколько событий
        сработало; такие потоки двигаются настоящими розыгрышами и в батчевом
        пути, и в потиковом (core/sim.py).
        """
        if n < 0:
            raise ValueError("skip не принимает отрицательные значения")
        self.counter += n

    # -- распределения ---------------------------------------------------

    def random(self) -> float:
        """Равномерное в [0, 1)."""
        value = self.at(self.counter)
        self.counter += 1
        return value

    def uniform(self, low: float, high: float) -> float:
        return low + (high - low) * self.random()

    def randint(self, low: int, high: int) -> int:
        """Целое в [low, high] включительно, без смещения по модулю."""
        if high < low:
            raise ValueError("high < low")
        span = high - low + 1
        # Отбрасываем «хвост», который не делится на span нацело, иначе младшие
        # значения выпадали бы чаще. Цикл почти всегда завершается на первой
        # итерации, но обязан быть детерминированным — он двигает счётчик.
        #
        # Расход счётчика здесь **переменный** (обычно 1, изредка больше), и
        # потому поток, где вызывается randint, нельзя двигать через ``skip``:
        # сколько он потратит, заранее не вычислить. Ограничение не мешает,
        # пока розыгрыши идут из обработчиков событий, которые в обоих путях
        # срабатывают одинаково.
        limit = _UINT64 - (_UINT64 % span)
        while True:
            value = self.raw_at(self.counter)
            self.counter += 1
            if value < limit:
                return low + value % span

    def choice(self, seq):
        """Элемент последовательности. Только индексируемые — множества и
        словари в горячем пути запрещены инвариантом И2."""
        if not seq:
            raise ValueError("пустая последовательность")
        return seq[self.randint(0, len(seq) - 1)]

    def normal(self, mu: float = 0.0, sigma: float = 1.0) -> float:
        """Бокс–Мюллер. Тратит ровно два значения счётчика — всегда, чтобы
        расход был предсказуем при промотке."""
        u1 = self.random()
        u2 = self.random()
        # u1 == 0 дало бы log(0); сдвигаем на минимальный шаг сетки.
        if u1 <= 0.0:
            u1 = 1.0 / _UINT64
        return mu + sigma * math.sqrt(-2.0 * math.log(u1)) * math.cos(2.0 * math.pi * u2)

    def exponential(self, rate: float) -> float:
        """Показательное с интенсивностью rate (1/ч). Розыгрыш наработки до
        отказа (DESIGN.md, §4.6)."""
        if rate <= 0.0:
            raise ValueError("rate должен быть положительным")
        u = self.random()
        if u >= 1.0:  # недостижимо для [0,1), но пусть код не зависит от этого
            u = 1.0 - 1.0 / _UINT64
        return -math.log(1.0 - u) / rate

    def weibull(self, shape: float, scale: float) -> float:
        """Вейбулл: наработка до отказа узла (DESIGN.md, §4.6, BALANCE.md §2.6).
        shape > 1 — старение, интенсивность отказов растёт со временем."""
        if shape <= 0.0 or scale <= 0.0:
            raise ValueError("shape и scale должны быть положительными")
        u = self.random()
        if u >= 1.0:
            u = 1.0 - 1.0 / _UINT64
        return scale * (-math.log(1.0 - u)) ** (1.0 / shape)

    def __repr__(self) -> str:
        return f"RngStream({self.name!r}, counter={self.counter})"


class Rng:
    """Набор потоков партии. Хранит seed и счётчики, отдаёт потоки по имени."""

    __slots__ = ("_seed", "_streams")

    def __init__(self, seed: int, counters: dict[str, int] | None = None) -> None:
        self._seed = seed
        self._streams: dict[str, RngStream] = {}
        for name, counter in (counters or {}).items():
            self._streams[name] = RngStream(seed, name, counter)

    @property
    def seed(self) -> int:
        return self._seed

    def stream(self, name: str) -> RngStream:
        """Поток по имени. Создаётся при первом обращении со счётчиком 0 —
        поэтому новая подсистема в новой версии игры стартует с нуля и не
        трогает чужие последовательности."""
        stream = self._streams.get(name)
        if stream is None:
            stream = RngStream(self._seed, name, 0)
            self._streams[name] = stream
        return stream

    def counters(self) -> dict[str, int]:
        """Снимок для сейва. Отсортирован: порядок ключей в файле не должен
        зависеть от порядка обращения к потокам."""
        return {name: self._streams[name].counter for name in sorted(self._streams)}

    def __repr__(self) -> str:
        return f"Rng(seed={self._seed}, streams={len(self._streams)})"
