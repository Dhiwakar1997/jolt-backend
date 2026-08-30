from jolt.auth import tokens


def test_generate_token_has_prefix():
    tok = tokens.generate_token()
    assert tok.startswith("jolt_live_")
    assert len(tok) > len("jolt_live_")


def test_hash_and_verify_roundtrip():
    tok = tokens.generate_token()
    h = tokens.hash_token(tok)
    assert h != tok
    assert tokens.verify_token(tok, h) is True
    assert tokens.verify_token(tok + "x", h) is False


def test_distinct_tokens_do_not_verify():
    a = tokens.generate_token()
    b = tokens.generate_token()
    assert tokens.verify_token(b, tokens.hash_token(a)) is False
