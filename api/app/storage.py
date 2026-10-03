import os
from typing import Optional, Tuple, IO

from .config import settings
from functools import lru_cache


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
    def __init__(self, *, bucket=None, prefix=None, region=None, endpoint=None, kms_key=None):
        self.bucket = settings.s3_bucket if bucket is None else bucket
        self.prefix = settings.s3_prefix if prefix is None else prefix
        self.kms_key = settings.s3_kms_key_id if kms_key is None else kms_key
        region = settings.s3_region if region is None else region
        endpoint = settings.s3_endpoint_url if endpoint is None else endpoint
        import boto3  # type: ignore

        self._s3 = boto3.client(
            "s3",
            region_name=(region or None),
            endpoint_url=(endpoint or None),
        )
        if not self.bucket:
            raise RuntimeError("S3 storage selected but S3_BUCKET is not set")

    def put_pdf(self, local_path: str, object_name: str) -> str:
        bucket = self.bucket
        prefix = self.prefix or ""
        key = f"{prefix}{object_name}" if prefix else object_name
        extra = {}
        if self.kms_key:
            extra["ServerSideEncryption"] = "aws:kms"
            extra["SSEKMSKeyId"] = self.kms_key
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


def _signature() -> tuple:
    return (
        (settings.storage_backend or "local").lower(),
        settings.s3_bucket or "",
        settings.s3_region or "",
        settings.s3_endpoint_url or "",
        settings.s3_kms_key_id or "",
        settings.s3_prefix or "",
    )


@lru_cache(maxsize=32)
def _storage_for(backend, bucket, region, endpoint, kms_key, prefix):
    if backend == 's3':
        return S3Storage(bucket=bucket, prefix=prefix, region=region, endpoint=endpoint, kms_key=kms_key)
    if backend == 'local':
        return LocalStorage()
    raise RuntimeError('Unknown storage backend.')


def get_storage() -> Storage:
    # Returning a local cache result prevents another request's selection from
    # replacing the shared object between construction and return.
    return _storage_for(*_signature())


def reset_storage() -> None:
    _storage_for.cache_clear()
