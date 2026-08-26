from app.security import masked_url, redact


def test_redact_removes_urls_proxy_credentials_uuid_and_tokens() -> None:
    secret = "public-secret-token"
    raw = "failed https://host.invalid/sub?token=abc vless://uuid@host:443 11111111-1111-4111-8111-111111111111 public-secret-token"
    clean = redact(raw, known_tokens=(secret,))
    assert "host.invalid" not in clean
    assert "vless://" not in clean
    assert "11111111" not in clean
    assert secret not in clean


def test_masked_url_only_exposes_origin() -> None:
    result = masked_url("https://provider.invalid:8443/private/path?token=secret")
    assert result == "https://provider.invalid:8443/•••"
    assert "secret" not in result
