"""Provider plugin management for the Faxbot Python SDK."""

from typing import Any, Dict, List, Optional


class PluginManager:
    """List, configure and install Faxbot provider plugins."""

    def __init__(self, client):
        self.client = client  # Reference to FaxbotClient
        self.enabled = False
        self._check_plugin_support()

    def _check_plugin_support(self) -> None:
        try:
            resp = self.client._session.get(
                f"{self.client.base_url}/plugins",
                headers=self.client._headers,
                timeout=5,
            )
            if resp.status_code == 200:
                self.enabled = True
        except Exception:
            self.enabled = False

    def list_plugins(self) -> List[Dict[str, Any]]:
        """Return installed plugins (GET /plugins answers {"items": [...]})."""
        if not self.enabled:
            return []
        resp = self.client._session.get(
            f"{self.client.base_url}/plugins",
            headers=self.client._headers,
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("items", []) if isinstance(data, dict) else data

    def get_plugin_config(self, plugin_id: str) -> Dict[str, Any]:
        """Return {"enabled", "settings", "role", "_meta"}; secrets are masked."""
        resp = self.client._session.get(
            f"{self.client.base_url}/plugins/{plugin_id}/config",
            headers=self.client._headers,
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()

    def update_plugin_config(
        self,
        plugin_id: str,
        settings: Optional[Dict[str, Any]] = None,
        *,
        enabled: Optional[bool] = None,
        role: Optional[str] = None,
        expected_revision_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Change only the given provider fields (PUT /plugins/{id}/config).

        Pass ``expected_revision_id`` from ``get_plugin_config(...)["_meta"]["desired_revision_id"]``
        to refuse the write if someone else changed the configuration first.
        Requires the providers:write permission.
        """
        patch = {
            "settings": settings,
            "enabled": enabled,
            "role": role,
            "expected_revision_id": expected_revision_id,
        }
        resp = self.client._session.put(
            f"{self.client.base_url}/plugins/{plugin_id}/config",
            json={key: value for key, value in patch.items() if value is not None},
            headers=self.client._headers,
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()

    def install_plugin(self, manifest: Dict[str, Any]) -> Dict[str, Any]:
        """Install an HTTP provider manifest (POST /admin/plugins/http/install).

        Requires the providers:install permission. Returns {"ok", "id", "path"}.
        """
        if not isinstance(manifest, dict):
            raise TypeError("install_plugin takes an HTTP provider manifest (a dict with an 'id').")
        resp = self.client._session.post(
            f"{self.client.base_url}/admin/plugins/http/install",
            json={"manifest": manifest},
            headers=self.client._headers,
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()
