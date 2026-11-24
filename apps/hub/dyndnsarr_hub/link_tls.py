"""CA propia y certificado del puerto de enlace del Hub (ADR-0013, D3).

CA ECDSA P-256 de 10 años y certificado de servidor de 1 año en el directorio de datos (0600).
El certificado se renueva al arrancar si quedan menos de 30 días o cambian los nombres: el Agent
fija la CA, así que renovar el certificado le es transparente.
"""

import ipaddress
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

CA_DAYS = 3650
CERT_DAYS = 365
RENEW_BEFORE = timedelta(days=30)


@dataclass(frozen=True)
class LinkTls:
    cert_file: Path
    key_file: Path
    ca_pem: str


def _write_private(path: Path, data: bytes) -> None:
    """Escritura atómica con modo 0600 desde el primer byte."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
    os.replace(tmp, path)


def _key_pem(key: ec.EllipticCurvePrivateKey) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def _san(names: list[str]) -> x509.SubjectAlternativeName:
    entries: list[x509.GeneralName] = []
    for name in names:
        try:
            entries.append(x509.IPAddress(ipaddress.ip_address(name)))
        except ValueError:
            entries.append(x509.DNSName(name))
    return x509.SubjectAlternativeName(entries)


def _new_ca(now: datetime) -> tuple[ec.EllipticCurvePrivateKey, x509.Certificate]:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "DynDNSarr Hub link CA")])
    public = key.public_key()
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(public)
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=CA_DAYS))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(public), critical=False)
        .sign(key, hashes.SHA256())
    )
    return key, cert


def _new_server_cert(
    ca_key: ec.EllipticCurvePrivateKey, ca: x509.Certificate, names: list[str], now: datetime
) -> tuple[ec.EllipticCurvePrivateKey, x509.Certificate]:
    key = ec.generate_private_key(ec.SECP256R1())
    public = key.public_key()
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, names[0])]))
        .issuer_name(ca.subject)
        .public_key(public)
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=CERT_DAYS))
        .add_extension(_san(names), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(public), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    return key, cert


def _needs_renewal(cert_file: Path, names: list[str], now: datetime) -> bool:
    if not cert_file.is_file():
        return True
    cert = x509.load_pem_x509_certificate(cert_file.read_bytes())
    if cert.not_valid_after_utc - now < RENEW_BEFORE:
        return True
    try:
        current = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        return True
    return current != _san(names)


def ensure(data_dir: Path, names: list[str], now: datetime) -> LinkTls:
    """Crea la CA si no existe y (re)emite el certificado de servidor si hace falta."""
    data_dir.mkdir(parents=True, exist_ok=True)
    ca_file, ca_key_file = data_dir / "link-ca.pem", data_dir / "link-ca.key"
    cert_file, key_file = data_dir / "link.pem", data_dir / "link.key"
    if ca_file.is_file() and ca_key_file.is_file():
        ca = x509.load_pem_x509_certificate(ca_file.read_bytes())
        ca_key = serialization.load_pem_private_key(ca_key_file.read_bytes(), None)
        if not isinstance(ca_key, ec.EllipticCurvePrivateKey):
            raise ValueError(f"{ca_key_file}: no es una clave ECDSA")
    else:
        ca_key, ca = _new_ca(now)
        _write_private(ca_key_file, _key_pem(ca_key))
        _write_private(ca_file, ca.public_bytes(serialization.Encoding.PEM))
        cert_file.unlink(missing_ok=True)
    if _needs_renewal(cert_file, names, now):
        key, cert = _new_server_cert(ca_key, ca, names, now)
        _write_private(key_file, _key_pem(key))
        _write_private(cert_file, cert.public_bytes(serialization.Encoding.PEM))
    return LinkTls(cert_file, key_file, ca_file.read_text())
