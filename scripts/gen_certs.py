"""Собственный CA + серверный сертификат на все хосты Яндекса, к которым ходит станция.

    python scripts/gen_certs.py --ip 192.168.1.5

Создаёт certs/ca.crt, certs/ca.key, certs/server.crt, certs/server.key.
Станция примет сертификат только если certs/ca.crt добавлен в её хранилище доверенных
сертификатов (root/adb) — либо используйте свой MITM-прокси и HTTP-порт этого сервера.
"""
from __future__ import annotations

import argparse
import datetime as dt
import ipaddress
import sys
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from quasard.http_api import KNOWN_HOSTS  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "certs"


def _write(path: Path, data: bytes) -> None:
    path.write_bytes(data)
    print("wrote", path)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ip", action="append", default=[], help="IP сервера для SAN (можно несколько раз)")
    ap.add_argument("--host", action="append", default=[], help="доп. имена хостов")
    args = ap.parse_args()
    OUT.mkdir(exist_ok=True)
    now = dt.datetime.now(dt.timezone.utc)

    ca_key_p, ca_crt_p = OUT / "ca.key", OUT / "ca.crt"
    if ca_key_p.exists() and ca_crt_p.exists():
        ca_key = serialization.load_pem_private_key(ca_key_p.read_bytes(), None)
        ca_crt = x509.load_pem_x509_certificate(ca_crt_p.read_bytes())
    else:
        ca_key = ec.generate_private_key(ec.SECP256R1())
        ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "AliceQwenWhisper Local CA")])
        ca_crt = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name)
                  .public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
                  .not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=3650))
                  .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
                  .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True,
                                               content_commitment=False, key_encipherment=False,
                                               data_encipherment=False, key_agreement=False,
                                               encipher_only=False, decipher_only=False), critical=True)
                  .sign(ca_key, hashes.SHA256()))
        _write(ca_key_p, ca_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                              serialization.NoEncryption()))
        _write(ca_crt_p, ca_crt.public_bytes(serialization.Encoding.PEM))

    names = [x509.DNSName(h) for h in (*KNOWN_HOSTS, *args.host)]
    names += [x509.IPAddress(ipaddress.ip_address(ip)) for ip in args.ip]
    key = ec.generate_private_key(ec.SECP256R1())
    crt = (x509.CertificateBuilder()
           .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, KNOWN_HOSTS[0])]))
           .issuer_name(ca_crt.subject).public_key(key.public_key()).serial_number(x509.random_serial_number())
           .not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=825))
           .add_extension(x509.SubjectAlternativeName(names), critical=False)
           .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
           .sign(ca_key, hashes.SHA256()))
    _write(OUT / "server.key", key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                 serialization.NoEncryption()))
    _write(OUT / "server.crt", crt.public_bytes(serialization.Encoding.PEM) +
           ca_crt.public_bytes(serialization.Encoding.PEM))


if __name__ == "__main__":
    main()
