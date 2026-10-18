"""Config (price.md numbers) and crypto tests."""

from cryptography.fernet import Fernet

from turso import config, crypto


def test_free_plan_limits_match_prices_md():
    free = config.plan_limits("free")
    assert free.databases == 1
    assert free.storage_mb == 60
    assert free.reads_per_month == 5_000_000
    assert free.writes_per_month == 75_000
    assert free.storage_bytes == 60 * 1024 * 1024


def test_pro_plan_limits_match_prices_md():
    pro = config.plan_limits("pro")
    assert pro.databases == 1
    assert pro.storage_mb == 200
    assert pro.reads_per_month == 10_000_000
    assert pro.writes_per_month == 400_000


def test_unknown_plan_falls_back_to_free():
    assert config.plan_limits("enterprise") == config.plan_limits("free")


def test_fernet_key_is_valid():
    assert Fernet(config.TURSO_ENC_KEY.encode()) is not None


def test_secret_roundtrip():
    secret = "turso-jwt.value.here"
    encrypted = crypto.encrypt_secret(secret)
    assert encrypted != secret
    assert crypto.decrypt_secret(encrypted) == secret


def test_generated_token_format():
    raw = crypto.generate_api_token()
    assert raw.startswith("dbc_")
    assert len(raw) == 4 + 43
    assert crypto.token_prefix(raw) == raw[:8]


def test_hash_token_is_sha256_hex():
    digest = crypto.hash_token("dbc_abc")
    assert len(digest) == 64
    assert digest == crypto.hash_token("dbc_abc")
    assert digest != crypto.hash_token("dbc_abd")


def test_tokens_equal_is_boolean():
    assert crypto.tokens_equal("abc", "abc")
    assert not crypto.tokens_equal("abc", "abd")
