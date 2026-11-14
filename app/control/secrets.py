from __future__ import annotations

import base64
import ipaddress
import json
import os
from datetime import datetime, timedelta, timezone

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.x509.oid import NameOID
from fastapi import HTTPException

from .. import config


def _key(create=False):
    path = config.SECRETS_DIR / 'control.key'
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() and create:
        from ..db import db
        with db() as conn:
            if any(conn.execute(f'SELECT 1 FROM {table} LIMIT 1').fetchone() for table in ('proxy_nodes','proxy_users','proxy_grants')):
                raise HTTPException(503, '代理加密密钥缺失，请恢复原始 /data/secrets/control.key')
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as handle:
                handle.write(os.urandom(32))
        except FileExistsError:
            pass
    value = path.read_bytes()
    if len(value) != 32:
        raise ValueError('control_key_invalid')
    return value


def seal(value):
    nonce = os.urandom(12)
    raw = json.dumps(value, ensure_ascii=False).encode()
    return base64.b64encode(nonce + AESGCM(_key(True)).encrypt(nonce, raw, b'sub-manager-control-v1')).decode()


def unseal(value):
    try:
        raw = base64.b64decode(value, validate=True)
        return json.loads(AESGCM(_key()).decrypt(raw[:12], raw[12:], b'sub-manager-control-v1'))
    except Exception:
        raise HTTPException(503, '代理秘密无法解密，请恢复原始 /data/secrets/control.key') from None


def certificate(server_name, certificate_pem='', private_key_pem=''):
    now = datetime.now(timezone.utc)
    if bool(certificate_pem) != bool(private_key_pem):
        raise ValueError('证书和私钥必须同时填写')
    if not certificate_pem:
        key = ec.generate_private_key(ec.SECP256R1())
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, server_name)])
        try:
            san = x509.IPAddress(ipaddress.ip_address(server_name))
        except ValueError:
            san = x509.DNSName(server_name)
        cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
                .public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now-timedelta(minutes=5)).not_valid_after(now+timedelta(days=365))
                .add_extension(x509.SubjectAlternativeName([san]), critical=False).sign(key, hashes.SHA256()))
        certificate_pem = cert.public_bytes(serialization.Encoding.PEM).decode()
        private_key_pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                          serialization.NoEncryption()).decode()
    cert = x509.load_pem_x509_certificate(certificate_pem.encode())
    key = serialization.load_pem_private_key(private_key_pem.encode(), password=None)
    encode = lambda public: public.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    if encode(cert.public_key()) != encode(key.public_key()):
        raise ValueError('证书与私钥不匹配')
    if cert.not_valid_after_utc <= now or cert.not_valid_before_utc > now:
        raise ValueError('证书不在有效期内')
    names = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    try:
        target_ip = ipaddress.ip_address(server_name)
        matches = target_ip in names.get_values_for_type(x509.IPAddress)
    except ValueError:
        def matches_dns(name):
            name = name.lower().rstrip('.')
            if name.startswith('*.'):
                return server_name.count('.') == name.count('.') and server_name.endswith(name[1:])
            return server_name.lower() == name
        matches = any(matches_dns(name) for name in names.get_values_for_type(x509.DNSName))
    if not matches:
        raise ValueError('证书未覆盖 TLS 名称')
    return certificate_pem, private_key_pem, cert.fingerprint(hashes.SHA256()).hex(), cert.not_valid_after_utc.isoformat()
