from app.core.security import KEY_PREFIX, generate_api_key, hash_api_key


def test_generated_keys_are_unique_and_prefixed() -> None:
    keys = {generate_api_key()[0] for _ in range(1000)}
    assert len(keys) == 1000
    key, prefix = generate_api_key()
    assert key.startswith(KEY_PREFIX)
    assert len(key) >= 40
    assert key.startswith(prefix)
    assert len(prefix) == 12


def test_hash_is_deterministic_and_depends_on_the_pepper() -> None:
    assert hash_api_key("k", b"p") == hash_api_key("k", b"p")
    assert hash_api_key("k", b"p") != hash_api_key("k", b"q")
    assert len(hash_api_key("k", b"p")) == 64
