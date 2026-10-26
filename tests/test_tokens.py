from jolt.auth import tokens
from jolt.config import Settings
from jolt.domain.models import TokenKind

SETTINGS = Settings(_env_file=None)
PEPPER = "test-pepper"


def test_generated_tokens_carry_their_kind_prefix():
    app = tokens.generate_token(TokenKind.APP, SETTINGS)
    mcp = tokens.generate_token(TokenKind.MCP, SETTINGS)
    assert app.startswith("jolt_app_") and len(app) > 40
    assert mcp.startswith("jolt_live_") and len(mcp) > 40


def test_hash_and_verify_roundtrip():
    tok = tokens.generate_token(TokenKind.MCP, SETTINGS)
    h = tokens.hash_token(tok, PEPPER)
    assert tok not in h
    assert tokens.verify_token(tok, h, PEPPER) is True
    assert tokens.verify_token(tok + "x", h, PEPPER) is False
    # The pepper is part of the hash: the same token with another pepper fails.
    assert tokens.verify_token(tok, h, "other-pepper") is False


def test_verify_tolerates_a_garbage_hash():
    assert tokens.verify_token("jolt_app_x", "not-an-argon2-hash", PEPPER) is False


def test_distinct_tokens_do_not_verify():
    a = tokens.generate_token(TokenKind.APP, SETTINGS)
    b = tokens.generate_token(TokenKind.APP, SETTINGS)
    assert tokens.verify_token(b, tokens.hash_token(a, PEPPER), PEPPER) is False


def test_lookup_key_is_a_stable_truncated_hmac_keyed_by_the_pepper():
    tok = tokens.generate_token(TokenKind.MCP, SETTINGS)
    key = tokens.lookup_key(tok, PEPPER)
    assert key == tokens.lookup_key(tok, PEPPER)
    assert len(key) == tokens.LOOKUP_HEX_CHARS
    int(key, 16)  # hex
    assert key != tokens.lookup_key(tok, "other-pepper")
    assert key != tokens.lookup_key(tok + "x", PEPPER)
    assert tok not in key


def test_last4():
    assert tokens.last4("jolt_live_abcd1234") == "1234"
