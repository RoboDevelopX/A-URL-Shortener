import pytest

from app.services.codes import (
    BASE62_ALPHABET,
    InvalidAliasError,
    ShortCodeGenerator,
    base62_decode,
    base62_encode,
    validate_alias,
)


@pytest.mark.parametrize("value", [0, 1, 61, 62, 3843, 62**7 - 1])
def test_base62_round_trip(value: int) -> None:
    assert base62_decode(base62_encode(value, 7)) == value


def test_base62_pads_to_width() -> None:
    assert base62_encode(0, 4) == "0000"
    assert base62_encode(61, 3) == "00z"


def test_base62_rejects_values_that_do_not_fit() -> None:
    with pytest.raises(ValueError, match="does not fit"):
        base62_encode(62**3, 3)
    with pytest.raises(ValueError, match="non-negative"):
        base62_encode(-1, 3)


def test_base62_rejects_invalid_characters() -> None:
    with pytest.raises(ValueError, match="invalid base62"):
        base62_decode("ab-c")


def test_generator_is_a_bijection_on_the_whole_code_space() -> None:
    # Exhaustive check on a small space (62**2 = 3844 codes): every id maps to a distinct
    # code, every code is reached, and decode inverts encode.
    generator = ShortCodeGenerator(b"secret", length=2)
    codes = [generator.encode(i) for i in range(generator.capacity)]
    assert len(set(codes)) == generator.capacity
    assert all(len(code) == 2 and set(code) <= set(BASE62_ALPHABET) for code in codes)
    assert [generator.decode(code) for code in codes] == list(range(generator.capacity))


def test_generator_production_length_has_no_collisions_in_a_large_sample() -> None:
    generator = ShortCodeGenerator(b"secret", length=7)
    ids = range(1, 100_001)
    codes = {generator.encode(i) for i in ids}
    assert len(codes) == len(ids)
    assert all(generator.decode(generator.encode(i)) == i for i in range(1, 1000))


def test_consecutive_ids_do_not_give_guessable_codes() -> None:
    generator = ShortCodeGenerator(b"secret", length=7)
    first, second = generator.encode(1000), generator.encode(1001)
    differing = sum(a != b for a, b in zip(first, second, strict=True))
    assert differing >= 4


def test_different_secrets_give_different_codes() -> None:
    assert ShortCodeGenerator(b"a", 7).encode(42) != ShortCodeGenerator(b"b", 7).encode(42)


def test_generator_rejects_ids_outside_the_code_space() -> None:
    generator = ShortCodeGenerator(b"secret", length=2)
    with pytest.raises(ValueError, match="outside the code space"):
        generator.encode(62**2)
    with pytest.raises(ValueError, match="not a code"):
        generator.decode("abc")
    with pytest.raises(ValueError, match="positive"):
        ShortCodeGenerator(b"secret", length=0)


@pytest.mark.parametrize("alias", ["abc", "spring-sale", "Promo_2026", "a" * 32])
def test_valid_aliases(alias: str) -> None:
    assert validate_alias(alias) == alias


@pytest.mark.parametrize("alias", ["ab", "a" * 33, "has space", "emoji🙂", "dot.ted", "sl/ash"])
def test_malformed_aliases_are_rejected(alias: str) -> None:
    with pytest.raises(InvalidAliasError, match="3-32 characters"):
        validate_alias(alias)


@pytest.mark.parametrize("alias", ["api", "admin", "docs", "healthz", "ADMIN", "Docs"])
def test_reserved_aliases_are_rejected_case_insensitively(alias: str) -> None:
    with pytest.raises(InvalidAliasError, match="reserved"):
        validate_alias(alias)
