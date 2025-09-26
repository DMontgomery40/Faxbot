from __future__ import annotations

from typing import Any, Tuple, IO

from api.app.storage import S3Storage


class Plugin:
    plugin_type = "storage"
    plugin_id = "s3"

    def __init__(self) -> None:
        # Initialize S3 client from settings
        self._impl = S3Storage()

    async def initialize(self, config: dict[str, Any]) -> None:
        return None

    def put_pdf(self, local_path: str, object_name: str) -> str:
        return self._impl.put_pdf(local_path, object_name)

    def get_pdf_stream(self, uri: str) -> Tuple[IO[bytes], str]:
        return self._impl.get_pdf_stream(uri)

    def delete(self, uri: str) -> None:
        return self._impl.delete(uri)

