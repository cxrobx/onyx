"""Where sealed objects go: a local folder (a dry run that touches no network) or an R2 bucket.

The R2 client is stdlib ``urllib`` plus a SigV4 signer, so the mirror adds no dependency for the upload
(no ``boto3``). Every error it raises is a ``StoreError`` with a fixed message: a urllib or TLS error
can quote the endpoint, which names the account, so the library's text never leaves this module.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Protocol
from urllib.parse import quote

from .errors import MirrorNotConfigured, StoreError
from . import secrets as keychain

ID_RE = re.compile(r"[0-9a-f]{64}")
PREFIX = "o/"
REGION = "auto"
SERVICE = "s3"
TIMEOUT = 120

_ACCOUNT_RE = re.compile(r"[A-Za-z0-9]{8,64}")
_BUCKET_RE = re.compile(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]")


class Store(Protocol):
    # Names the place, without being a credential or a path: publish keeps its per-destination
    # bookkeeping under it, so a dry run into a folder never tells the real bucket "already uploaded".
    destination: str

    def put(self, id: str, data: bytes) -> None: ...
    def delete(self, id: str) -> None: ...
    def list_ids(self) -> list[str]: ...


def _check_id(id: str) -> None:
    if not ID_RE.fullmatch(id):
        raise StoreError("an object id is 64 lowercase hex characters")


def _fingerprint(kind: str, *parts: str) -> str:
    digest = hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()
    return f"{kind}-{digest[:12]}"


class LocalDirStore:
    """Objects as files under ``<dir>/o/<id>``, which is the layout the bucket has."""

    def __init__(self, directory: Path) -> None:
        self.dir = Path(directory).expanduser().resolve()
        self.destination = _fingerprint("local", str(self.dir))

    def __repr__(self) -> str:
        return "LocalDirStore()"

    def _path(self, id: str) -> Path:
        _check_id(id)
        return self.dir / "o" / id

    def put(self, id: str, data: bytes) -> None:
        path = self._path(id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".part")
        tmp.write_bytes(data)
        os.replace(tmp, path)

    def delete(self, id: str) -> None:
        self._path(id).unlink(missing_ok=True)

    def list_ids(self) -> list[str]:
        folder = self.dir / "o"
        if not folder.is_dir():
            return []
        return sorted(p.name for p in folder.iterdir() if ID_RE.fullmatch(p.name))


# -- SigV4 ---------------------------------------------------------------------------------------------

def _hmac(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def canonical_request(method: str, path: str, query: list[tuple[str, str]], headers: dict[str, str],
                      payload_hash: str) -> tuple[str, str]:
    """The canonical request and its signed-headers list, as AWS defines them for S3.

    S3 encodes the path once (not twice, as other services do) and keeps its slashes.
    """
    canonical_path = quote(path, safe="/-_.~")
    canonical_query = "&".join(
        f"{quote(k, safe='-_.~')}={quote(v, safe='-_.~')}" for k, v in sorted(query)
    )
    lowered = {name.lower(): " ".join(value.split()) for name, value in headers.items()}
    names = sorted(lowered)
    canonical_headers = "".join(f"{name}:{lowered[name]}\n" for name in names)
    signed = ";".join(names)
    request = "\n".join([method, canonical_path, canonical_query, canonical_headers, signed, payload_hash])
    return request, signed


def sign_v4(*, method: str, path: str, query: list[tuple[str, str]], headers: dict[str, str],
            payload_hash: str, access_key_id: str, secret_access_key: str, amz_date: str,
            region: str = REGION, service: str = SERVICE) -> str:
    """The ``Authorization`` header value. ``headers`` must already hold ``host`` and ``x-amz-date``."""
    request, signed = canonical_request(method, path, query, headers, payload_hash)
    day = amz_date[:8]
    scope = f"{day}/{region}/{service}/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(request.encode("utf-8")).hexdigest()])
    key = _hmac(_hmac(_hmac(_hmac(b"AWS4" + secret_access_key.encode("utf-8"), day), region), service), "aws4_request")
    signature = hmac.new(key, to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"AWS4-HMAC-SHA256 Credential={access_key_id}/{scope}, SignedHeaders={signed}, Signature={signature}"


class R2Store:
    """A Cloudflare R2 bucket over its S3 API, path-style: ``https://<account>.r2.cloudflarestorage.com/<bucket>/o/<id>``."""

    def __init__(self, account_id: str, bucket: str, access_key_id: str, secret_access_key: str, *,
                 opener: Callable | None = None, clock: Callable[[], datetime] | None = None) -> None:
        # The account id becomes a hostname and the bucket a path segment, so neither may carry
        # anything but what those are made of.
        if not _ACCOUNT_RE.fullmatch(account_id):
            raise MirrorNotConfigured("r2_account_id does not look like a Cloudflare account id")
        if not _BUCKET_RE.fullmatch(bucket):
            raise MirrorNotConfigured("r2_bucket does not look like a bucket name")
        self._host = f"{account_id}.r2.cloudflarestorage.com"
        self._bucket = bucket
        self._access_key_id = access_key_id
        self._secret_access_key = secret_access_key
        self._opener = opener or (lambda request: urllib.request.urlopen(request, timeout=TIMEOUT))
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.destination = _fingerprint("r2", account_id, bucket)

    def __repr__(self) -> str:
        return "R2Store()"

    @classmethod
    def from_secrets(cls, secrets: keychain.Secrets, **kwargs) -> "R2Store":
        values = {a: secrets.get(a) for a in (keychain.R2_ACCOUNT_ID, keychain.R2_BUCKET,
                                              keychain.R2_ACCESS_KEY_ID, keychain.R2_SECRET_ACCESS_KEY)}
        missing = [a for a, v in values.items() if not v]
        if missing:
            raise MirrorNotConfigured("missing in the Keychain: " + ", ".join(missing) + " (run `onyx mirror setup`)")
        return cls(values[keychain.R2_ACCOUNT_ID], values[keychain.R2_BUCKET],
                   values[keychain.R2_ACCESS_KEY_ID], values[keychain.R2_SECRET_ACCESS_KEY], **kwargs)

    def _request(self, method: str, path: str, *, query: list[tuple[str, str]] | None = None,
                 body: bytes = b"") -> bytes:
        query = query or []
        payload_hash = hashlib.sha256(body).hexdigest()
        amz_date = self._clock().astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        headers = {"host": self._host, "x-amz-content-sha256": payload_hash, "x-amz-date": amz_date}
        if body:
            headers["content-type"] = "application/octet-stream"
        authorization = sign_v4(
            method=method, path=path, query=query, headers=headers, payload_hash=payload_hash,
            access_key_id=self._access_key_id, secret_access_key=self._secret_access_key, amz_date=amz_date,
        )
        url = f"https://{self._host}{quote(path, safe='/-_.~')}"
        if query:
            url += "?" + "&".join(f"{quote(k, safe='-_.~')}={quote(v, safe='-_.~')}" for k, v in sorted(query))
        request = urllib.request.Request(url, data=body if body else None, method=method)
        for name, value in {**headers, "authorization": authorization}.items():
            if name != "host":  # urllib sets Host from the URL, which is the same string
                request.add_header(name, value)
        try:
            with self._opener(request) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            if method == "DELETE" and exc.code == 404:
                return b""
            raise StoreError(f"R2 {method} failed: HTTP {exc.code}") from None
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
            raise StoreError(f"R2 {method} failed: {type(reason).__name__}") from None

    def put(self, id: str, data: bytes) -> None:
        _check_id(id)
        self._request("PUT", f"/{self._bucket}/{PREFIX}{id}", body=data)

    def delete(self, id: str) -> None:
        _check_id(id)
        self._request("DELETE", f"/{self._bucket}/{PREFIX}{id}")

    def list_ids(self) -> list[str]:
        ids: list[str] = []
        token: str | None = None
        while True:
            query = [("list-type", "2"), ("prefix", PREFIX)]
            if token:
                query.append(("continuation-token", token))
            body = self._request("GET", f"/{self._bucket}", query=query)
            try:
                root = ET.fromstring(body)
            except ET.ParseError:
                raise StoreError("R2 LIST returned something that is not XML") from None
            for key in root.findall(".//{*}Contents/{*}Key"):
                name = (key.text or "")[len(PREFIX):]
                if (key.text or "").startswith(PREFIX) and ID_RE.fullmatch(name):
                    ids.append(name)
            truncated = (root.findtext("{*}IsTruncated") or "").lower() == "true"
            token = root.findtext("{*}NextContinuationToken")
            if not truncated or not token:
                return ids
