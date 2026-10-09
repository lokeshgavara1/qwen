"""
Generate Self-Signed SSL Certificate for CUTM AI Gateway
Requires: pip install cryptography
"""

import subprocess
import sys

# Install cryptography if not present
try:
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives import serialization
except ImportError:
    print("Installing cryptography...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "cryptography", "--break-system-packages"])
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives import serialization

from datetime import datetime, timedelta
import ipaddress

print("=" * 70)
print("GENERATING SELF-SIGNED SSL CERTIFICATE")
print("=" * 70)

# Generate private key
print("\n[1/4] Generating private key...")
private_key = rsa.generate_private_key(
    public_exponent=65537,
    key_size=4096,
    backend=default_backend()
)
print("[OK] Private key generated")

# Build certificate subject
print("[2/4] Building certificate subject...")
subject = issuer = x509.Name([
    x509.NameAttribute(NameOID.COUNTRY_NAME, u"IN"),
    x509.NameAttribute(NameOID.STATE_OR_PROVINCE_NAME, u"Andhra Pradesh"),
    x509.NameAttribute(NameOID.LOCALITY_NAME, u"Vizianagaram"),
    x509.NameAttribute(NameOID.ORGANIZATION_NAME, u"CUTM"),
    x509.NameAttribute(NameOID.COMMON_NAME, u"172.16.8.4"),
])
print("[OK] Subject configured")

# Build certificate
print("[3/4] Building certificate...")
cert = x509.CertificateBuilder().subject_name(
    subject
).issuer_name(
    issuer
).public_key(
    private_key.public_key()
).serial_number(
    x509.random_serial_number()
).not_valid_before(
    datetime.utcnow()
).not_valid_after(
    datetime.utcnow() + timedelta(days=3650)  # 10 years
).add_extension(
    x509.SubjectAlternativeName([
        x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
        x509.IPAddress(ipaddress.IPv4Address("172.16.8.170")),
        x509.IPAddress(ipaddress.IPv4Address("172.16.8.4")),
        x509.DNSName(u"localhost"),
        x509.DNSName(u"127.0.0.1"),
        x509.DNSName(u"172.16.8.170"),
        x509.DNSName(u"172.16.8.4"),
    ]),
    critical=False,
).sign(private_key, hashes.SHA256(), default_backend())
print("[OK] Certificate built")

# Save private key
print("[4/4] Saving certificate and key...")
with open("key.pem", "wb") as f:
    f.write(private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption()
    ))
print("[OK] Saved: key.pem")

# Save certificate
with open("cert.pem", "wb") as f:
    f.write(cert.public_bytes(serialization.Encoding.PEM))
print("[OK] Saved: cert.pem")

print("\n" + "=" * 70)
print("[OK] SUCCESS! Certificate generated")
print("=" * 70)
print("\nFiles created:")
print("  - cert.pem (certificate)")
print("  - key.pem (private key)")
print("\nPlace these files in the SAME folder as qwen_lb.py")
print("=" * 70)
