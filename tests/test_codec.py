"""Кодек сейва (SAVEFORMAT.md, §3).

msgpack проверяется наравне с JSON, хотя умолчание — JSON. Смысл слоя в том,
чтобы переезд на бинарный формат когда-нибудь стоил одной строки; непроверенный
второй путь этой гарантии не даёт.
"""

from __future__ import annotations

import json

import pytest

from module_sim.core.state import GameState
from module_sim.persistence import save as save_mod
from module_sim.persistence.codec import (
    JsonCodec,
    MsgpackCodec,
    codec_for_path,
    default_codec,
)

SAMPLE = {
    "schema_version": 2,
    "saved_at": 1767484800.0,
    "game": {
        "seed": 5,
        "tick": 100,
        "company": {"name": "Третий блок", "cash_cents": 50_000_000_000},
        "rng": {"counters": {"sim.heartbeat": 100}},
    },
}


@pytest.mark.parametrize("codec", [JsonCodec(), MsgpackCodec()], ids=["json", "msgpack"])
def test_round_trip(codec):
    assert codec.loads(codec.dumps(SAMPLE)) == SAMPLE


@pytest.mark.parametrize("codec", [JsonCodec(), MsgpackCodec()], ids=["json", "msgpack"])
def test_integers_stay_integers(codec):
    """Деньги обязаны пережить сериализацию целыми."""
    restored = codec.loads(codec.dumps(SAMPLE))
    assert isinstance(restored["game"]["company"]["cash_cents"], int)


def test_codec_selected_by_extension():
    assert isinstance(codec_for_path("save.json"), JsonCodec)
    assert isinstance(codec_for_path("save.msgpack"), MsgpackCodec)


def test_unknown_extension_is_refused():
    with pytest.raises(ValueError, match="неизвестное расширение"):
        codec_for_path("save.sav")


def test_default_is_json():
    assert isinstance(default_codec(), JsonCodec)


def test_json_keys_are_sorted():
    """Порядок ключей не должен зависеть от порядка вставки — иначе диффы
    фикстур в git шумят на каждом изменении кода."""
    shuffled = {"zulu": 1, "alpha": 2, "mike": 3}
    text = JsonCodec().dumps(shuffled).decode("utf-8")
    assert list(json.loads(text)) == ["alpha", "mike", "zulu"]
    assert text.index("alpha") < text.index("mike") < text.index("zulu")


def test_json_keeps_cyrillic_readable():
    """Фикстуры читают люди."""
    text = JsonCodec().dumps({"name": "Третий блок"}).decode("utf-8")
    assert "Третий блок" in text


def test_save_and_load_through_msgpack(tmp_path):
    """Полный путь сохранения работает и на бинарном кодеке."""
    state = GameState(seed=3, tick=77, epoch=0.0)
    state.company.cash_cents = 4_242
    target = tmp_path / "save.msgpack"

    save_mod.save_game(state, now=1.0, path=target)
    result = save_mod.load_game(target)

    assert result.state.tick == 77
    assert result.state.company.cash_cents == 4_242
