"""Кодек сейва: JSON сейчас, msgpack — когда понадобится (SAVEFORMAT.md, §3).

Смысл этого слоя один: переезд на бинарный формат должен стоить одной смены
умолчания, а не переписывания сохранений. Оба кодека реализованы и покрыты
тестами с первого дня, чтобы «переезд когда-нибудь» не оказался переездом на
непроверенный код.

Кодек выбирается по расширению **существующего** файла, поэтому смена умолчания
не ломает уже лежащие на диске партии.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Protocol

__all__ = ["Codec", "JsonCodec", "MsgpackCodec", "codec_for_path", "default_codec"]


class Codec(Protocol):
    """Пара функций сериализации. Работает с ``dict``, не с dataclass-ами:
    миграции обязаны видеть сырые данные (SAVEFORMAT.md, §5, правило 4)."""

    suffix: str

    def dumps(self, data: dict) -> bytes: ...

    def loads(self, raw: bytes) -> dict: ...


class JsonCodec:
    """Читаемый глазами сейв.

    ``sort_keys`` — не косметика: без него порядок ключей зависел бы от порядка
    вставки, и диффы фикстур в git шумели бы на каждом изменении кода.
    ``ensure_ascii=False`` оставляет кириллицу кириллицей — фикстуры читают люди.
    """

    suffix = ".json"

    def dumps(self, data: dict) -> bytes:
        text = json.dumps(data, ensure_ascii=False, sort_keys=True, indent=1)
        return text.encode("utf-8")

    def loads(self, raw: bytes) -> dict:
        return json.loads(raw.decode("utf-8"))


class MsgpackCodec:
    """Компактный бинарный формат. Критерий переезда — SAVEFORMAT.md, §3."""

    suffix = ".msgpack"

    def dumps(self, data: dict) -> bytes:
        import msgpack

        return msgpack.packb(data, use_bin_type=True)

    def loads(self, raw: bytes) -> dict:
        import msgpack

        return msgpack.unpackb(raw, raw=False, strict_map_key=False)


_CODECS: dict[str, Codec] = {
    JsonCodec.suffix: JsonCodec(),
    MsgpackCodec.suffix: MsgpackCodec(),
}


def default_codec() -> Codec:
    """Умолчание для новых партий. Менять здесь и только здесь."""
    return _CODECS[JsonCodec.suffix]


#: Хвост кольца бэкапов: ``save.json.1``. Расширение у такого файла — ``.1``,
#: поэтому перед выбором кодека номер отбрасывается. Иначе бэкапы читались бы
#: только «по счастью», и падение основного файла уносило бы партию целиком.
_BACKUP_INDEX = re.compile(r"\.\d+$")


def codec_for_path(path: Path | str) -> Codec:
    """Кодек по расширению файла. Понимает и имена бэкапов ``save.json.N``."""
    name = _BACKUP_INDEX.sub("", Path(path).name)
    suffix = Path(name).suffix
    codec = _CODECS.get(suffix)
    if codec is None:
        known = ", ".join(sorted(_CODECS))
        raise ValueError(f"неизвестное расширение сейва {suffix!r}; известные: {known}")
    return codec
