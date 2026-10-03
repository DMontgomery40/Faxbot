"""Faxbot API Client SDK for Python.

This module provides FaxbotClient, a simple client for interacting with a Faxbot API server.
It allows sending faxes and checking the status of fax jobs via the Faxbot REST API.

Example:
    from faxbot import FaxbotClient, FaxSubmissionUncertain

    client = FaxbotClient(base_url="http://localhost:8080", api_key="YOUR_API_KEY")
    operation_id = FaxbotClient.new_operation_id()  # save this before sending
    try:
        job = client.send_fax("+15551234567", "/path/to/document.pdf", operation_id=operation_id)
        print(f"Fax submitted, job ID: {job['id']}, initial status: {job['status']}")
        # Later, check status:
        status_info = client.get_status(job['id'])
        print(f"Current status: {status_info['status']}")
    except FaxSubmissionUncertain as e:
        # Faxbot may or may not have accepted it. Sending again with the same
        # operation id finishes this same fax instead of sending a second one.
        job = client.resume_fax(e.operation_id, "+15551234567", "/path/to/document.pdf")
    except Exception as e:
        print(f"Fax operation failed: {e}")
"""

from __future__ import annotations

import os
import re
import time
import uuid
from typing import Any, Dict, Optional

import requests
from .plugins import PluginManager

__all__ = ["FaxbotClient", "FaxSubmissionUncertain", "FaxOperationConflict"]

# Faxbot answers these when it could not confirm the fax; the same operation id is safe to send again.
_RETRYABLE_STATUSES = frozenset({502, 503, 504})
# The request may or may not have reached Faxbot.
_TRANSPORT_ERRORS = (requests.exceptions.ConnectionError, requests.exceptions.Timeout,
                     requests.exceptions.ChunkedEncodingError)
_OPERATION_ID = re.compile(r"[\x21-\x7e]{1,128}")


class FaxSubmissionUncertain(Exception):
    """Faxbot did not confirm a fax after every allowed attempt; it may or may not have accepted it.

    Call ``send_fax`` (or ``resume_fax``) again with ``operation_id`` and the same number and document
    to finish this same fax without sending it twice.
    """

    def __init__(self, operation_id: str, status: Optional[int] = None) -> None:
        self.operation_id = operation_id
        self.status = status
        super().__init__(
            f"Faxbot did not confirm this fax, so call send_fax again with operation_id={operation_id} "
            "to finish the same fax without sending it twice."
        )


class FaxOperationConflict(Exception):
    """The operation id already belongs to a different fax (another number, document or queue setting)."""

    def __init__(self, operation_id: str, detail: str = "") -> None:
        self.operation_id = operation_id
        self.status = 409
        super().__init__(
            f"Conflict (409): {detail or 'Idempotency-Key already belongs to a different fax request.'}"
        )


def _error_detail(response: Any) -> str:
    """The server's {"detail": "..."} message, or the raw body when it is not JSON."""
    try:
        error_json = response.json()
    except ValueError:
        return response.text or ""
    if isinstance(error_json, dict) and error_json.get("detail"):
        return str(error_json["detail"])
    return ""


class FaxbotClient:
    """Client for Faxbot API.

    Allows sending faxes and checking fax status using the Faxbot server's REST API.
    Initialize with the base URL of the Faxbot API and an optional API key for authentication.

    Notes:
        - Project is named "Faxbot". There is no Twilio integration in the SDK; the server abstracts backends.
        - If the server requires an API key, provide it via the `api_key` parameter so the client
          sends `X-API-Key` on each request.
        - The client keeps no state on disk. To finish a fax after a crash, save the operation id
          before sending and pass it back to ``resume_fax``.
    """

    def __init__(self, base_url: str = "http://localhost:8080", api_key: Optional[str] = None, *,
                 session: Optional[Any] = None, retries: int = 2, retry_backoff: float = 0.5) -> None:
        """Initialize the FaxbotClient.

        Args:
            base_url: Base URL of the Faxbot API (e.g. "http://localhost:8080").
                      Defaults to "http://localhost:8080".
            api_key: Optional API key for authentication. If provided, it will be sent in the "X-API-Key" header.
            session: Optional ``requests.Session`` (or compatible object) used for every request.
                     The API key is sent with each request and never stored on the session.
            retries: How many more times ``send_fax`` sends the same fax, with the same operation id,
                     after a connection error, a timeout or HTTP 502/503/504. Use 0 against a Faxbot
                     server that does not support Idempotency-Key.
            retry_backoff: Seconds to wait before the first retry; each later retry waits twice as long.
        """
        if int(retries) < 0:
            raise ValueError("retries must be 0 or more")
        if float(retry_backoff) < 0:
            raise ValueError("retry_backoff must be 0 or more")
        # Ensure base_url has no trailing slash for consistency
        self.base_url = base_url.rstrip('/')
        self.api_key = api_key
        self.retries = int(retries)
        self.retry_backoff = float(retry_backoff)
        self._session = session if session is not None else requests.Session()
        # Prepare common headers (if API key is set, we'll add it in requests)
        self._headers: Dict[str, str] = {}
        if self.api_key:
            self._headers["X-API-Key"] = self.api_key
        # Lazy plugin manager (initialized on first access)
        self._plugin_manager = None

    @staticmethod
    def new_operation_id() -> str:
        """Return a new operation id for one fax. Save it before sending so an unconfirmed send can be finished."""
        return str(uuid.uuid4())

    def send_fax(self, to: str, file_path: str, *, operation_id: Optional[str] = None) -> Dict[str, Any]:
        """Send a fax through the Faxbot API.

        Every call is one fax operation, identified by ``operation_id`` and sent as the Idempotency-Key
        header. Without an ``operation_id`` the call is a new fax and gets a new id, even for a document
        sent before. After a connection error, a timeout or HTTP 502/503/504 the client sends the same fax
        again with the same id, up to ``retries`` more times; Faxbot returns the original job instead of
        sending it twice.

        Args:
            to: Destination fax number (in E.164 format like "+15551234567", or a valid dialable number string).
            file_path: Path to the file to fax. Must be a PDF or text file.
            operation_id: The id of an earlier send to finish (see ``resume_fax``). Omit it for a new fax.

        Returns:
            The server's job JSON, unchanged (includes 'id', 'status', 'to', etc. as keys).

        Raises:
            ValueError: If inputs are missing or invalid (e.g., unsupported file type).
            FaxSubmissionUncertain: No attempt was confirmed; send again with its ``operation_id``
                to finish the same fax.
            FaxOperationConflict: ``operation_id`` already belongs to a different fax.
            Exception: If the server rejects the fax (the exception message will include details).
        """
        if not to:
            raise ValueError("Destination fax number 'to' must be provided")
        if not file_path:
            raise ValueError("file_path must be provided and point to a PDF or TXT file")
        if not os.path.isfile(file_path):
            raise ValueError(f"File not found: {file_path}")

        # Determine file MIME type based on extension
        filename = os.path.basename(file_path)
        # Only allow .pdf or .txt
        ext = os.path.splitext(filename)[1].lower()
        if ext == '.pdf':
            mime_type = 'application/pdf'
        elif ext == '.txt':
            mime_type = 'text/plain'
        else:
            # Unsupported file type for fax
            raise ValueError(f"Unsupported file type '{ext}'. Only .pdf or .txt files can be faxed.")

        # The id exists before any upload, so an unconfirmed send can always be finished with it.
        if operation_id is None:
            operation_id = self.new_operation_id()
        if not isinstance(operation_id, str) or not _OPERATION_ID.fullmatch(operation_id):
            raise ValueError("operation_id must be 1 to 128 printable ASCII characters without spaces")

        url = f"{self.base_url}/fax"
        headers = {**self._headers, "Idempotency-Key": operation_id}
        for attempt in range(self.retries + 1):
            if attempt:
                time.sleep(self.retry_backoff * 2 ** (attempt - 1))
            try:
                # Reopen the file every attempt: a handle read by a failed upload would send an empty body.
                with open(file_path, "rb") as handle:
                    response = self._session.post(url, data={"to": to},
                                                  files={"file": (filename, handle, mime_type)},
                                                  headers=headers, timeout=30)
            except _TRANSPORT_ERRORS as error:
                if attempt == self.retries:
                    raise FaxSubmissionUncertain(operation_id) from error
                continue
            if response.status_code not in _RETRYABLE_STATUSES:
                break
            if attempt == self.retries:
                raise FaxSubmissionUncertain(operation_id, response.status_code)

        # Check HTTP response
        if response.status_code == 202:
            # Fax job accepted (or the original job for this operation id)
            return response.json()
        # An error occurred; try to extract detail message
        error_detail = _error_detail(response)
        status = response.status_code
        if status == 409:
            raise FaxOperationConflict(operation_id, error_detail)
        elif status == 401:
            raise Exception("Unauthorized (401): invalid API key or missing authentication.")
        elif status == 404:
            raise Exception("Not Found (404): The fax endpoint is unavailable. Check the base_url.")
        elif status == 400:
            raise Exception(f"Bad Request (400): {error_detail or 'Invalid fax request parameters.'}")
        elif status == 415:
            raise Exception(f"Unsupported Media Type (415): {error_detail or 'File type not allowed. Only PDF or TXT are supported.'}")
        elif status == 413:
            raise Exception(f"Payload Too Large (413): {error_detail or 'File size exceeds the allowed limit.'}")
        else:
            # Other errors
            raise Exception(f"Fax API Error (HTTP {status}): {error_detail or response.reason}")

    def resume_fax(self, operation_id: str, to: str, file_path: str) -> Dict[str, Any]:
        """Finish a fax whose send was not confirmed, without sending it twice.

        Pass the ``operation_id`` from ``FaxSubmissionUncertain`` (or the one you saved before sending)
        with the same number and document. Faxbot returns the original job if it already accepted the
        fax, or accepts it now if it never did. A different number or document raises
        ``FaxOperationConflict``.
        """
        if not operation_id:
            raise ValueError("operation_id must be provided to resume a fax")
        return self.send_fax(to, file_path, operation_id=operation_id)

    def get_status(self, job_id: str) -> Dict[str, Any]:
        """Get the status of a previously sent fax.

        Args:
            job_id: The fax job ID returned by send_fax.

        Returns:
            A dictionary with the fax job information, including updated status, error (if any), pages, etc.

        Raises:
            ValueError: If job_id is not provided.
            Exception: If the HTTP request fails or the server returns an error (404 if not found, etc.).
        """
        if not job_id:
            raise ValueError("job_id must be provided to get fax status")
        url = f"{self.base_url}/fax/{job_id}"
        response = self._session.get(url, headers=self._headers, timeout=15)
        if response.status_code == 200:
            return response.json()
        else:
            # Error handling similar to send_fax
            status = response.status_code
            error_detail = _error_detail(response)
            if status == 404:
                # Fax job not found
                raise Exception(f"Fax job not found (404): Job ID {job_id} does not exist.")
            elif status == 401:
                raise Exception("Unauthorized (401): invalid API key for retrieving fax status.")
            else:
                raise Exception(f"Failed to get fax status (HTTP {status}): {error_detail or response.reason}")

    def check_health(self) -> bool:
        """Check the health status of the Faxbot API server.

        Returns:
            True if the server is reachable and healthy (status "ok").

        Raises:
            Exception: If the request fails or the server returns an unexpected response.
        """
        url = f"{self.base_url}/health"
        try:
            response = self._session.get(url, timeout=5)
        except Exception as e:
            raise Exception(f"Health check failed: {e}")
        if response.status_code == 200:
            try:
                data = response.json()
            except ValueError:
                data = None
            if isinstance(data, dict) and data.get("status") == "ok":
                return True
            # If response isn't the expected JSON, treat 200 as healthy
            return True
        else:
            raise Exception(f"Health check returned HTTP {response.status_code}")

    @property
    def plugins(self) -> PluginManager:
        """Access plugin manager (lazy initialization)."""
        if self._plugin_manager is None:
            self._plugin_manager = PluginManager(self)
        return self._plugin_manager
