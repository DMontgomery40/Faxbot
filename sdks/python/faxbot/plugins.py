"""
Plugin management extension for existing Faxbot Python SDK
Backward compatible addition to v1.0.x / v1.1.x
"""

from typing import Dict, List, Any
import requests


class PluginManager:
    """Optional plugin manager for v3 architecture."""

    def __init__(self, client):
        self.client = client  # Reference to FaxbotClient
        self.enabled = False
        self._check_plugin_support()

    def _check_plugin_support(self) -> None:
        try:
            resp = requests.get(
                f"{self.client.base_url}/plugins",
                headers=self.client._headers,
                timeout=5,
            )
            if resp.status_code == 200:
                self.enabled = True
        except Exception:
            self.enabled = False

    def list_plugins(self) -> List[Dict[str, Any]]:
        if not self.enabled:
            return []
        resp = requests.get(
            f"{self.client.base_url}/plugins",
            headers=self.client._headers,
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()

    def get_plugin_config(self, plugin_id: str) -> Dict[str, Any]:
        resp = requests.get(
            f"{self.client.base_url}/plugins/{plugin_id}/config",
            headers=self.client._headers,
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()

    def update_plugin_config(self, plugin_id: str, config: Dict[str, Any]) -> Dict[str, Any]:
        resp = requests.put(
            f"{self.client.base_url}/plugins/{plugin_id}/config",
            json=config,
            headers=self.client._headers,
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()

    def install_plugin(self, plugin_id: str) -> Dict[str, Any]:
        resp = requests.post(
            f"{self.client.base_url}/plugins/install",
            json={"plugin_id": plugin_id},
            headers=self.client._headers,
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()

