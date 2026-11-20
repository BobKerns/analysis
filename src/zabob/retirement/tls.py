"""TLS for the tools that download over HTTPS (#40).

They verify certificates with the operating system's own trust store, so a
locally trusted root is honored and the python.org build of Python on macOS
works without a root store of its own.
"""

from __future__ import annotations

import ssl

import truststore

__all__ = ["ssl_context", "verify_reason"]


def ssl_context() -> ssl.SSLContext:
    """An SSL context that verifies certificates with the operating system's
    own trust store (#40): the macOS keychain, the Windows certificate store,
    or the system bundle on Linux, via ``truststore``.

    So a root you've trusted locally (a corporate proxy, a home CA) is
    honored, the root list follows OS updates, and it doesn't matter that
    the python.org build of Python on macOS ships with no root store of its
    own (which made fetching fail with CERTIFICATE_VERIFY_FAILED)."""
    return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)


_UNTRUSTED_CHAIN_CODES = frozenset({2, 18, 19, 20})
"""OpenSSL verify codes that all mean "no trusted root for this chain":
unable to get issuer certificate (2), self-signed certificate (18),
self-signed certificate in chain (19), and unable to get *local* issuer
certificate (20) -- whose wording misleadingly suggests a missing local
certificate rather than a missing trusted root."""


def verify_reason(error: ssl.SSLCertVerificationError) -> str:
    """Why verification failed, in words that match the actual failure (#40).

    A verification error isn't always an untrusted root: it can be an expired
    or not-yet-valid certificate, a hostname mismatch, and more. The OS trust
    store's own reasons (truststore on macOS: "... certificate is expired",
    "... is not trusted") are already accurate and pass through. Only
    OpenSSL's untrusted-chain codes are reworded, since its "unable to get
    local issuer certificate" names the wrong problem."""
    if error.verify_code in _UNTRUSTED_CHAIN_CODES:
        return "its chain doesn't lead to a root certificate this system trusts"
    return str(error.verify_message or error.reason or error)
