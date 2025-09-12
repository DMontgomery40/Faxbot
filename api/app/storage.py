import os
from typing import Optional, Tuple, IO

from .config import settings


class Storage:
    def put_pdf(self, local_path: str, object_name: str) -> str:
        raise NotImplementedError

    def get_pdf_stream(self, uri: str) -> Tuple[IO[bytes], str]:
        raise NotImplementedError

    def delete(self, uri: str) -> None:
        raise NotImplementedError

    def is_s3_uri(self, uri: str) -> bool:
        return uri.startswith("s3://")


class LocalStorage(Storage):
    def put_pdf(self, local_path: str, object_name: str) -> str:
        # Keep as local path; caller controls placement
        return local_path

    def get_pdf_stream(self, uri: str) -> Tuple[IO[bytes], str]:
        return open(uri, "rb"), os.path.basename(uri)

    def delete(self, uri: str) -> None:
        try:
            if uri and os.path.exists(uri):
                os.remove(uri)
        except FileNotFoundError:
            pass


class S3Storage(Storage):
    def __init__(self):
        import boto3  # type: ignore

        self._s3 = boto3.client(
            "s3",
            region_name=(settings.s3_region or None),
            endpoint_url=(settings.s3_endpoint_url or None),
        )
        if not settings.s3_bucket:
            raise RuntimeError("S3 storage selected but S3_BUCKET is not set")

    def put_pdf(self, local_path: str, object_name: str) -> str:
        bucket = settings.s3_bucket
        prefix = settings.s3_prefix or ""
        key = f"{prefix}{object_name}" if prefix else object_name
        extra = {}
        if settings.s3_kms_key_id:
            extra["ServerSideEncryption"] = "aws:kms"
            extra["SSEKMSKeyId"] = settings.s3_kms_key_id
        with open(local_path, "rb") as f:
            self._s3.upload_fileobj(f, bucket, key, ExtraArgs=extra)
        # Optionally delete local file – the caller may manage retention; keep file for now
        return f"s3://{bucket}/{key}"

    def get_pdf_stream(self, uri: str):
        import io
        bucket, key = _parse_s3_uri(uri)
        bio = io.BytesIO()
        self._s3.download_fileobj(bucket, key, bio)
        bio.seek(0)
        name = os.path.basename(key)
        return bio, name

    def delete(self, uri: str) -> None:
        bucket, key = _parse_s3_uri(uri)
        self._s3.delete_object(Bucket=bucket, Key=key)


def _parse_s3_uri(uri: str) -> Tuple[str, str]:
    assert uri.startswith("s3://"), f"Not an s3 uri: {uri}"
    path = uri[5:]
    bucket, _, key = path.partition("/")
    return bucket, key


_storage: Optional[Storage] = None
_storage_sig: Optional[tuple] = None

def _signature() -> tuple:
    return (
        (settings.storage_backend or "local").lower(),
        settings.s3_bucket or "",
        settings.s3_region or "",
        settings.s3_endpoint_url or "",
        settings.s3_kms_key_id or "",
        settings.s3_prefix or "",
    )


def get_storage() -> Storage:
    global _storage
    global _storage_sig
    sig = _signature()
    if _storage is not None and _storage_sig == sig:
        return _storage
    if (settings.storage_backend or "local").lower() == "s3":
        try:
            _storage = S3Storage()
        except Exception:
            # Fail closed with explicit error if configured but invalid
            raise
    else:
        _storage = LocalStorage()
    _storage_sig = sig
    return _storage


def reset_storage() -> None:
    """Clear cached storage so next get_storage() reflects updated settings."""
    global _storage, _storage_sig
    _storage = None
    _storage_sig = None
