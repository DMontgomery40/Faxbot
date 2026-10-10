
# Curated Plugin Registry

The Plugins tab is a legacy feature. Faxbot plans to remove provider plugins in the next release. Set up built-in providers in **Administration → Setup**.

[:material-puzzle-outline: Provider Setup](../setup/index.md){ .md-button }
[:material-http: Manifest Installation](#manifest-installation){ .md-button }
[:material-file-cog: Plugin Config File](config-file.md){ .md-button }
[:material-cog: Settings](../admin-console/settings.md){ .md-button }

---

## Discovery and configuration endpoints

:material-puzzle: `GET /plugins`
: List installed providers under `items`, including which one is selected. An installed manifest replaces the built-in entry with the same name.

:material-cog: `GET /plugins/{id}/config`
: Return the provider's saved `enabled`, `settings` and `role`, with secrets hidden, plus a `_meta` block to send back when saving.

:material-content-save-cog: `PUT /plugins/{id}/config`
: Save only the fields you changed, with the `expected_revision_id` from the last read. `_meta.apply_state` in the reply says whether the change is already in use or waits for a restart.

---

## Configuration and permissions

Provider configuration is stored in Faxbot's installation settings. The `FAXBOT_CONFIG_PATH` JSON file is a legacy bootstrap input; see [the file format](config-file.md).

Faxes already accepted keep using the provider settings they were accepted with.

---

## Manifest installation

Installing or importing adds provider definitions; choosing a provider and entering its credentials are separate settings changes. Faxes already accepted keep the definition they were accepted with. Validate a proposed manifest before installing it, and inspect the server's actual permissions and validation response; a discovery catalog or declared feature flag is not an installation sandbox.

---

## Admin Console behavior

:material-view-grid-plus: Plugins tab
: Reads `/plugins` and renders schema‑driven forms

:material-filter-variant: Backend isolation
: Installed cards retain their provider identity. Custom manifests take precedence over a builtin with the same ID; curated metadata must not overwrite the manifest’s description.

---

## Notes

- Provider panels should describe their own configuration; a catalog card is not a delivery result.
- Outbound callbacks authenticate the captured provider/account and issued attempt. Inbound ingestion is a separate implementation; a configured provider or catalog card is not proof of receiving a usable document.

---

## Troubleshooting

- `/plugins` returns 404 → turn on v3 plugins in Settings and restart if Faxbot asks for it.
- `/plugins` returns 403 → your account needs `providers:read`; see [Access Control](../security/access-control.md).
- A save is refused with 409 → someone else saved first. Read the configuration again and review it before retrying.

## Quick examples

=== "Registry JSON"

    Curated catalog data is separate from installed manifests and runtime credentials:

    ```json
    {
      "items": [
        {
          "id": "phaxio",
          "name": "Phaxio Cloud Fax",
          "version": "1.0.0",
          "categories": ["outbound"],
          "capabilities": ["send", "get_status", "webhook"],
          "description": "Cloud fax sending with account credentials and a separate callback token."
        }
      ]
    }
    ```

=== "/plugins response"

    ```json
    {
      "items": [
        {
          "id": "phaxio",
          "enabled": true,
          "name": "Phaxio Cloud Fax",
          "categories": ["outbound"],
          "capabilities": ["send", "get_status", "webhook"],
          "configurable": true
        }
      ]
    }
    ```

=== "Enable plugin (curl)"

    First read `/plugins/phaxio/config` and retain its `_meta.desired_revision_id` while reviewing the edit. The placeholder below is that loaded revision, not a fresh read immediately before mutation.

    ```sh
    BASE="http://localhost:8080"
    API_KEY="your_admin_api_key"
    curl -sS -X PUT "$BASE/plugins/phaxio/config" \
      -H "X-API-Key: $API_KEY" -H 'content-type: application/json' \
      -d '{"expected_revision_id":"LOADED_DESIRED_REVISION", "enabled":true, "role":"outbound", "settings": {"api_key":"...","api_secret":"..."}}'
    ```
