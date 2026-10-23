"""Private push delivery through operator-approved, DNS-pinned HTTPS gateways.

Core deliveries use an explicit user-to-endpoint mapping. The old global
endpoint environment variable is deliberately ignored. A gateway must accept
JSON title/body and an Idempotency-Key; arbitrary chat-provider APIs are
not interchangeable with this private gateway contract.
"""

from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import os
import re
import socket
import ssl
import time
import uuid
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

MAX_CONFIG_BYTES = 256 * 1024
MAX_ENDPOINTS = 4
MAX_PAYLOAD_BYTES = 16 * 1024
ENDPOINT_SPLIT = re.compile(r"[\s,;]+")
UNRESERVED = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")


def parse_endpoints(raw):
    if not raw:
        return []
    return [item for item in ENDPOINT_SPLIT.split(str(raw).strip()) if item]


def _normalize_endpoint(url):
    # Deferred because sources imports platform, which constructs this gateway.
    from .sources import canonical_url

    normalized = canonical_url(url)
    parsed = urlsplit(normalized)
    if parsed.scheme != "https" or (parsed.port or 443) != 443:
        raise ValueError("HTTPS on port 443 required")
    host = parsed.hostname.rstrip(".")
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        raise ValueError("non-public host")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None:
        effective = getattr(address, "ipv4_mapped", None) or address
        if not effective.is_global or effective == ipaddress.ip_address("100.100.100.200"):
            raise ValueError("non-public address")
    authority = "[" + host + "]" if ":" in host else host

    # Equivalent host/default-port/unreserved escapes and query order share one
    # identity. Keep the transmission URL separate, preserving signed queries.
    def escapes(value):
        return re.sub(
            r"%([0-9a-fA-F]{2})",
            lambda match: chr(int(match[1], 16)) if chr(int(match[1], 16)) in UNRESERVED else "%" + match[1].upper(),
            value,
        )

    path = escapes(parsed.path)
    identity = urlunsplit(
        ("https", authority, path, urlencode(sorted(parse_qsl(parsed.query, keep_blank_values=True))), "")
    )
    transmission = urlunsplit(("https", authority, parsed.path, parsed.query, ""))
    return transmission, identity


def _is_public_https(url):
    """Static configuration check; public DNS is verified again before sending."""
    try:
        _normalize_endpoint(url)
        return True
    except (ValueError, RuntimeError, UnicodeError):
        return False


def _receipt(status, detail=None):
    return {"status": status, "provider_id": None, "detail": detail}


class PushGateway:
    def __init__(
        self,
        user_endpoints=None,
        *,
        endpoints=None,
        timeout=15,
        resolver=None,
        connector=None,
        max_payload_bytes=MAX_PAYLOAD_BYTES,
    ):
        self.timeout = min(30, max(1, float(timeout)))
        self.resolver = resolver or socket.getaddrinfo
        self.connector = connector
        self.max_payload_bytes = min(MAX_PAYLOAD_BYTES, max(1, int(max_payload_bytes)))
        self._endpoints = {}
        self._reasons = {}
        self._invalid_configuration = False
        raw = os.getenv("CAMPUS_PUSH_USER_ENDPOINTS", "") if user_endpoints is None else user_endpoints
        try:
            if isinstance(raw, str):
                if len(raw.encode("utf-8")) > MAX_CONFIG_BYTES:
                    raise ValueError("configuration too large")
                raw = json.loads(raw) if raw.strip() else {}
            if not isinstance(raw, dict) or len(raw) > 10000:
                raise ValueError("mapping required")
            owners = {}
            for user_id, urls in raw.items():
                if not isinstance(user_id, str) or str(uuid.UUID(user_id)) != user_id:
                    raise ValueError("canonical user UUID required")
                if not isinstance(urls, list) or len(urls) > MAX_ENDPOINTS:
                    self._reasons[user_id] = "invalid_configuration"
                    continue
                if not urls:
                    self._reasons[user_id] = "unconfigured"
                    continue
                normalized = []
                identities = set()
                try:
                    for url in urls:
                        transmission, identity = _normalize_endpoint(url)
                        owners.setdefault(identity, set()).add(user_id)
                        if identity not in identities:
                            normalized.append(transmission)
                            identities.add(identity)
                except (ValueError, RuntimeError, UnicodeError):
                    self._reasons[user_id] = "invalid_configuration"
                    continue
                self._endpoints[user_id] = normalized
                self._reasons[user_id] = "ready"
            for users in owners.values():
                if len(users) > 1:
                    for user_id in users:
                        self._reasons[user_id] = "shared_destination"
        except (ValueError, TypeError, AttributeError, RuntimeError, UnicodeError):
            self._invalid_configuration = True
            self._endpoints.clear()
        # Explicit endpoints support isolated transport tests only. They are
        # never used by send_delivery_for_user and never loaded from global env.
        self._standalone = parse_endpoints(endpoints) if isinstance(endpoints, str) else list(endpoints or [])

    def status(self, user_id):
        reason = "invalid_configuration" if self._invalid_configuration else self._reasons.get(user_id, "unconfigured")
        return {"available": reason == "ready", "reason": reason}

    def send_delivery_for_user(self, user_id, subject, text, idempotency_key):
        status = self.status(user_id)
        if not status["available"]:
            return _receipt("failed", "push_" + status["reason"])
        return self._send(self._endpoints[user_id], subject, text, idempotency_key)

    @property
    def configured(self):
        return bool(self._standalone)

    def send_delivery(self, subject, text, idempotency_key):
        """Explicitly bound transport only; application deliveries use the user API."""
        return self._send(self._standalone, subject, text, idempotency_key)

    def _send(self, endpoints, subject, text, idempotency_key):
        from .sources import FetchError, _PinnedHTTPS, resolve_public

        if not endpoints:
            return _receipt("failed", "push_unconfigured")
        if len(endpoints) > MAX_ENDPOINTS:
            return _receipt("failed", "push_invalid_configuration")
        try:
            payload = json.dumps({"title": str(subject), "body": str(text)}, ensure_ascii=False).encode("utf-8")
        except (TypeError, ValueError, UnicodeError):
            return _receipt("failed", "push_invalid_payload")
        if len(payload) > self.max_payload_bytes:
            return _receipt("failed", "push_payload_too_large")
        deadline = time.monotonic() + self.timeout
        resolved = []
        try:
            # Validate every destination before transmitting any private text.
            for url in endpoints:
                normalized, _ = _normalize_endpoint(url)
                host, ip, port = resolve_public(normalized, self.resolver, trusted_origins=())
                resolved.append((urlsplit(normalized), host, ip, port))
        except (FetchError, ValueError, RuntimeError, UnicodeError):
            return _receipt("failed", "push_destination_rejected")
        receipt_key = hashlib.sha256(str(idempotency_key).encode()).hexdigest()
        accepted = 0
        for parsed, host, ip, port in resolved:
            connection = None
            write_started = False
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return _receipt("unknown" if accepted else "failed", "push_timeout")
                factory = self.connector or _PinnedHTTPS
                connection = factory(host, ip, port, remaining)
                # DNS, TCP and TLS failures here prove no HTTP payload was sent.
                connection.connect()
                write_started = True
                connection.request(
                    "POST",
                    parsed.path + ("?" + parsed.query if parsed.query else ""),
                    body=payload,
                    headers={
                        "Host": parsed.netloc,
                        "Content-Type": "application/json; charset=utf-8",
                        "Content-Length": str(len(payload)),
                        "Idempotency-Key": receipt_key,
                        "Accept-Encoding": "identity",
                    },
                )
                response = connection.getresponse()
                # Never follow redirects with private message bodies or tokens.
                if 300 <= response.status < 400:
                    return _receipt("unknown" if accepted else "failed", "push_redirect_denied")
                if 200 <= response.status < 300:
                    accepted += 1
                # Response bodies are never read, so a gateway cannot make the
                # worker allocate an unbounded reply after accepting a message.
            except (OSError, ssl.SSLError, http.client.HTTPException, ValueError):
                return _receipt("unknown" if write_started or accepted else "failed", "push_transport_error")
            finally:
                if connection is not None:
                    try:
                        connection.close()
                    except OSError:
                        pass
        if accepted == len(resolved):
            return _receipt("sent")
        # Some destinations accepted: a blanket retry could duplicate those sends.
        return _receipt("unknown" if accepted else "failed", "push_not_accepted")
