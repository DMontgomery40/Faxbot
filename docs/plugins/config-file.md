# Plugin Bootstrap JSON and Canonical Settings

`FAXBOT_CONFIG_PATH` (default `config/faxbot.config.json`) identifies the legacy plugin JSON bootstrap input. Its version 1 construction format remains accepted when canonical installation state is absent. Conflicting environment/JSON selections are rejected for explicit reconciliation; startup does not silently pick one.

Structure

```json
{
  "version": 1,
  "providers": {
    "outbound": { "plugin": "phaxio", "enabled": true, "settings": {} },
    "inbound": { "plugin": null, "enabled": false, "settings": {} },
    "auth":    { "plugin": null, "enabled": false, "settings": {} },
    "storage": { "plugin": "local", "enabled": true, "settings": {} }
  }
}
```

## Existing installations

Once initialized, canonical desired/active revisions in the database are authoritative. Editing this JSON file or `.env` and restarting does not import a new runtime selection. Use Settings or Tools → Plugins to edit the desired revision and inspect active/pending status. Pending changes require every API worker to stop and the installation restart successfully.

- `GET /plugins/{id}/config` returns sanitized desired `enabled`, `settings`, `role` and `_meta` identity/apply state.
- `PUT /plugins/{id}/config` accepts intended `enabled`, `settings`, `role` and the editor's loaded `expected_revision_id`. Use a role of outbound, inbound or storage as supported by that provider.
- Omit unchanged fields/masked secrets. Enter replacements or explicit clears deliberately; do not write display masks as credentials.
- On 409, retain the draft and explicitly reload/review the desired revision before another write.
- Inspect returned `_meta.apply_state` and `pending_fields`; a successful write does not necessarily mean all desired fields are active.

The compatibility `path` returned by a plugin write identifies the configured legacy path; it does not mean that file is the authoritative runtime store. A backup of that JSON alone cannot recover the database, encrypted provider profiles, original installation key or document artifacts.

Installed manifest JSON remains a provider definition. It is captured with accepted work; changing a file does not rewrite an already accepted job's provider frame. See [manifest installation](registry.md#manifest-installation) and [Settings](../admin-console/settings.md).
