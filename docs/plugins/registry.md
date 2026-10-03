
# Curated Plugin Registry

The Plugins tab uses these discovery endpoints when `FEATURE_V3_PLUGINS=true`.

[:material-puzzle-outline: Plugins Overview](index.md){ .md-button }
[:material-http: HTTP Manifest Docs](manifest-http.md){ .md-button }
[:material-file-cog: Plugin Config File](config-file.md){ .md-button }
[:material-puzzle: Plugin Builder](../admin-console/plugin-builder.md){ .md-button }

---

## Discovery and configuration endpoints

:material-puzzle: `GET /plugins`
: List installed provider metadata under `items`, with desired selection and manifest precedence. Read a provider’s config separately for editor revision/settings.

:material-cog: `GET /plugins/{id}/config`
: Return sanitized desired `enabled`, `settings`, `role` and `_meta` active/desired identity.

:material-content-save-cog: `PUT /plugins/{id}/config`
: Validate and save intended changes with the loaded `expected_revision_id` into canonical desired state. Inspect returned `_meta.apply_state` and pending fields.

:material-database-search: `GET /plugin-registry`
: Serve the curated registry JSON for UI search

---

## Canonical configuration and permissions

Environment and `FAXBOT_CONFIG_PATH` JSON are first-bootstrap inputs. Existing installations use the canonical database revisions; subsequent file edits and restart do not import provider changes. See [the bootstrap format](config-file.md).

Configuration endpoints require admin authentication; database keys need the permissions accepted by `require_admin` (including `keys:manage`). The curated `/plugin-registry` catalog is discovery data, not a credential or runtime settings store.

Load a provider configuration before editing, omit unchanged masks and carry its desired revision into the write. A 409 requires explicit reload/review. Applied changes become active; pending changes require every API worker to stop and the installation restart. Accepted fax attempts keep their original provider frame.

---

## Manifest installation

Install/import adds provider definitions; configuration selection and credentials remain canonical desired edits. Existing accepted work keeps its captured definition. Validate a proposed manifest before installing it, and inspect the server's actual permissions and validation response; a discovery catalog or declared feature flag is not an installation sandbox.

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

- `/plugins` returns 404 → enable v3 plugins in canonical Settings, inspect active/pending status and complete any required coordinated restart.
- Configuration conflicts → reload the desired revision explicitly before reviewing/retrying the edit; file permissions on a legacy JSON path are not a substitute for canonical write validation.

---

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
