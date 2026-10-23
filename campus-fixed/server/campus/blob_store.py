"""Immutable bounded snapshot storage; cloud SDK is optional and lazily loaded."""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

from fastapi.responses import FileResponse, Response

MAX_BLOB_BYTES = 8 * 1024 * 1024
KEY = re.compile(r"snapshots/[0-9a-f-]{36}/[0-9a-f]{64}\.bin\Z")


class BlobError(RuntimeError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _key(key):
    if not isinstance(key, str) or not KEY.fullmatch(key):
        raise BlobError("snapshot_file_missing")
    return key


def _verify(body, digest):
    if len(body) > MAX_BLOB_BYTES:
        raise BlobError("snapshot_too_large")
    if hashlib.sha256(body).hexdigest() != digest:
        raise BlobError("snapshot_storage_corrupt")
    return body


class LocalBlobStore:
    def __init__(self, data_dir):
        self.root = Path(data_dir).resolve()

    def path(self, key):
        target = (self.root / _key(key)).resolve()
        if self.root not in target.parents:
            raise BlobError("snapshot_file_missing")
        return target

    def put(self, key, body, digest):
        _verify(body, digest)
        target = self.path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with target.open("xb") as stream:
                stream.write(body)
        except FileExistsError:
            self.read(key, digest)

    def read(self, key, digest):
        try:
            with self.path(key).open("rb") as stream:
                body = stream.read(MAX_BLOB_BYTES + 1)
        except FileNotFoundError:
            raise BlobError("snapshot_file_missing") from None
        return _verify(body, digest)

    def response(self, snapshot, headers):
        self.read(snapshot["object_key"], snapshot["content_hash"])
        return FileResponse(
            self.path(snapshot["object_key"]),
            media_type="application/octet-stream",
            filename=f"{snapshot['snapshot_id']}.bin",
            headers=headers,
        )


def validate_bucket_name(bucket_name):
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{1,220}[a-z0-9]", bucket_name or ""):
        raise ValueError("CAMPUS_GCS_BUCKET must be a bucket name, without gs:// or paths")
    return bucket_name


class GCSBlobStore:
    def __init__(self, bucket_name, client=None):
        validate_bucket_name(bucket_name)
        if client is None:
            from google.cloud import storage

            client = storage.Client()  # Cloud Run attached identity / ADC; no key files.
        self.bucket = client.bucket(bucket_name)

    def put(self, key, body, digest):
        _key(key)
        _verify(body, digest)
        from google.api_core.exceptions import GoogleAPICallError, PreconditionFailed

        blob = self.bucket.blob(key)
        try:
            blob.upload_from_string(
                body, content_type="application/octet-stream", if_generation_match=0, checksum="auto", timeout=30
            )
        except PreconditionFailed:
            self.read(key, digest)  # Immutable object already present: verify, never overwrite.
        except GoogleAPICallError:
            raise BlobError("snapshot_storage_unavailable") from None

    def read(self, key, digest):
        _key(key)
        from google.api_core.exceptions import GoogleAPICallError, NotFound

        blob = self.bucket.blob(key)
        try:
            blob.reload(timeout=30)
            if blob.size is None or blob.size > MAX_BLOB_BYTES:
                raise BlobError("snapshot_too_large")
            generation = blob.generation
            if generation is None:
                raise BlobError("snapshot_storage_unavailable")
            # Explicit range bounds even if metadata is wrong; raw bytes avoid gzip expansion.
            body = blob.download_as_bytes(
                start=0,
                end=MAX_BLOB_BYTES,
                if_generation_match=generation,
                raw_download=True,
                checksum="auto",
                timeout=30,
            )
        except NotFound:
            raise BlobError("snapshot_file_missing") from None
        except GoogleAPICallError:
            raise BlobError("snapshot_storage_unavailable") from None
        return _verify(body, digest)

    def response(self, snapshot, headers):
        return Response(
            self.read(snapshot["object_key"], snapshot["content_hash"]),
            media_type="application/octet-stream",
            headers={**headers, "Content-Disposition": f'attachment; filename="{snapshot["snapshot_id"]}.bin"'},
        )


def blob_store(core):
    value = getattr(core, "_snapshot_blobs", None)
    if value is None:
        mode = os.getenv("CAMPUS_BLOB_STORE", "local").strip().lower()
        if mode not in {"local", "gcs"}:
            raise ValueError("CAMPUS_BLOB_STORE must be local or gcs")
        value = GCSBlobStore(os.getenv("CAMPUS_GCS_BUCKET", "")) if mode == "gcs" else LocalBlobStore(core.data_dir)
        core._snapshot_blobs = value
    return value


def public_origin(request=None):
    value = os.getenv("CAMPUS_PUBLIC_ORIGIN", "").strip().rstrip("/")
    if value:
        parsed = urlsplit(value)
        try:
            port = parsed.port
        except ValueError:
            raise ValueError("CAMPUS_PUBLIC_ORIGIN must be an exact HTTPS origin") from None
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path
            or parsed.query
            or parsed.fragment
            or port not in {None, 443}
            or re.search(r"[\s\\]", value)
        ):
            raise ValueError("CAMPUS_PUBLIC_ORIGIN must be an exact HTTPS origin")
        return value
    if request is None:
        raise ValueError("CAMPUS_PUBLIC_ORIGIN is required for Cloud Run")
    return str(request.base_url).rstrip("/")
