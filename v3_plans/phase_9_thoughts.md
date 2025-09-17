Phase 9 Thoughts and Notes

1) Repo path adjustments
- Earlier drafts referenced `faxbot/sdks/...`; actual paths are `sdks/...`. Dev kit lives under `plugin-dev-kit/`.

2) SDK plugin manager endpoints
- Both Python and Node managers rely on `/plugins`, `/plugins/{id}/config`, `/plugins/install`.
- If disabled, managers set `enabled=false` and return empty lists gracefully.

3) Python Plugin Dev Kit packaging
- Entry point: `faxbot-plugin`. Depends on `jsonschema`, `click`, `pytest`, `pytest-asyncio`.

4) CLI scaffolding
- Scaffolded plugin uses a minimal structure; core discovery group TBD.

5) HIPAA/PHI heuristics
- Validators use conservative regex heuristics; false positives possible. Keep advisory.

6) Node Plugin Dev Kit
- Chose CommonJS + `.d.ts` to avoid a TS build. CLI uses `yargs`. Ajv validation allows `$ref` in development.
- CLI renamed to `faxbot-plugin-node` to avoid collision with Python.

