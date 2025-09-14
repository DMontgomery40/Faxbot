"""Faxbot API Client SDK for Python.

This module provides FaxbotClient, a simple client for interacting with a Faxbot API server.
It allows sending faxes and checking the status of fax jobs via the Faxbot REST API.

Example:
    from faxbot import FaxbotClient

    client = FaxbotClient(base_url="http://localhost:8080", api_key="YOUR_API_KEY")
    try:
        job = client.send_fax("+15551234567", "/path/to/document.pdf")
        print(f"Fax submitted, job ID: {job['id']}, initial status: {job['status']}")
        # Later, check status:
        status_info = client.get_status(job['id'])
        print(f"Current status: {status_info['status']}")
    except Exception as e:
        print(f"Fax operation failed: {e}")
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

import requests
from .plugins import PluginManager
from .plugins import PluginManager


class FaxbotClient:
    """Client for Faxbot API.

    Allows sending faxes and checking fax status using the Faxbot server's REST API.
    Initialize with the base URL of the Faxbot API and an optional API key for authentication.

    Notes:
        - Project is named "Faxbot". There is no Twilio integration in the SDK; the server abstracts backends.
        - If the server requires an API key, provide it via the `api_key` parameter so the client
          sends `X-API-Key` on each request.
    """

    def __init__(self, base_url: str = "http://localhost:8080", api_key: Optional[str] = None) -> None:
        """Initialize the FaxbotClient.

        Args:
            base_url: Base URL of the Faxbot API (e.g. "http://localhost:8080").
                      Defaults to "http://localhost:8080".
            api_key: Optional API key for authentication. If provided, it will be sent in the "X-API-Key" header.
        """
        # Ensure base_url has no trailing slash for consistency
        self.base_url = base_url.rstrip('/')
        self.api_key = api_key
        # Prepare common headers (if API key is set, we'll add it in requests)
        self._headers: Dict[str, str] = {}
        if self.api_key:
            self._headers["X-API-Key"] = self.api_key
        # Lazy plugin manager (initialized on first access)
        self._plugin_manager = None

    def send_fax(self, to: str, file_path: str) -> Dict[str, Any]:
        """Send a fax through the Faxbot API.

        Args:
            to: Destination fax number (in E.164 format like "+15551234567", or a valid dialable number string).
            file_path: Path to the file to fax. Must be a PDF or text file.

        Returns:
            A dictionary with fax job information (includes 'id', 'status', 'to', etc. as keys).

        Raises:
            ValueError: If inputs are missing or invalid (e.g., unsupported file type).
            Exception: If the HTTP request fails or the server returns an error (the exception message will include details).
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

        # Prepare multipart form data for the request
        files = {
            "file": (filename, open(file_path, "rb"), mime_type)
        }
        data = {
            "to": to
        }
        url = f"{self.base_url}/fax"

        try:
            response = requests.post(url, data=data, files=files, headers=self._headers, timeout=30)
        finally:
            # Ensure the file handle is closed
            try:
                files["file"][1].close()
            except Exception:
                pass

        # Check HTTP response
        if response.status_code == 202:
            # Fax job accepted
            return response.json()
        else:
            # An error occurred; try to extract detail message
            error_detail = ""
            try:
                error_json = response.json()
                # The API typically returns {"detail": "..."} on errors
                if isinstance(error_json, dict) and error_json.get("detail"):
                    error_detail = error_json["detail"]
            except ValueError:
                # response body is not JSON or cannot parse
                error_detail = response.text or ""
            status = response.status_code
            if status == 401:
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
        response = requests.get(url, headers=self._headers, timeout=15)
        if response.status_code == 200:
            return response.json()
        else:
            # Error handling similar to send_fax
            status = response.status_code
            error_detail = ""
            try:
                err = response.json()
                if isinstance(err, dict) and err.get("detail"):
                    error_detail = err["detail"]
            except ValueError:
                error_detail = response.text or ""
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
            response = requests.get(url, timeout=5)
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
        if not hasattr(self, "_plugin_manager") or self._plugin_manager is None:
            self._plugin_manager = PluginManager(self)
        return self._plugin_manager

    # NEW: Plugin manager property
    @property
    def plugins(self) -> PluginManager:
        """Access plugin manager (lazy initialization)."""
        if self._plugin_manager is None:
            self._plugin_manager = PluginManager(self)
        return self._plugin_manager
