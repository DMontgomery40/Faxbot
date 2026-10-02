# Provider resource path verification

The bounded resource-path repair is complete and independently approved. This is not completion of provider configuration, activation, account rotation or the whole refresh.

## Source and review

- BASE: `dcd4c442c42690874fbdc837fbea47fb14a9ef62`.
- Approved source: `48c9ff96f6a2959cdd04a465c2b1d6ce6749ef71`.
- Four changed files: `api/app/config_paths.py`, `api/app/config.py`, `api/app/main.py`, `api/tests/test_config_paths.py`.
- One physical-resource resolver now supplies traits, configured provider manifests, default plugin settings, registry and packaged examples in source and flattened image layouts. Explicit relative overrides retain process-working-directory semantics. Installation, import, discovery, diagnostics, send preparation, dispatch and historical refresh use the same provider manifest resolver.
- IDs must be one safe component. Both resolved provider directory and final manifest must remain beneath the configured root; operator-configured root symlinks remain supported. This checks existing filesystem state, not hostile concurrent filesystem mutation.
- Initial review/self-review found two corner cases: an escaped directory whose manifest linked back inside, and a stale `api/config/providers` directory hiding the real source bundle. Both have reproduced failing tests and corrective checks. Bundle candidates must contain the actual traits resource.
- Independent GPT-6.1 Sol xhigh review of the exact final range reports no remaining Critical, Important or Minor findings. Scratch review: `.superpowers/sdd/2026-10-02-faxbot-configuration/paths-review.md`.

## Tests

- Final focused suite: **32 passed**, zero warnings, 5.09 seconds.
- Independent focused replay: **32 passed**, zero warnings, 5.34 seconds.
- Full API suite with actual PostgreSQL16.15 and SQLite: **258 passed, 13 skipped**, zero warnings, 31.14 seconds. The skips are PostgreSQL-only cases under the SQLite parameter; their PostgreSQL cases execute.
- Tests cover both import spellings (`app` and `api.app`), repository/API/unrelated working directories, a copied flattened layout, explicit roots and relative overrides, actual installation/discovery/configuration/traits, real database/document preparation, path rejection and provider dispatch/status URLs with only external HTTP transport replaced.

## Actual committed image

- Source: `48c9ff96f6a2959cdd04a465c2b1d6ce6749ef71`.
- Tag: `faxbot-refresh:paths-48c9ff96f6a2`.
- Image: `sha256:a6a6974b5e7887f51bda7caac25c9929a95a7a757ebd8b871aa0639605287106`, Linux/arm64.
- Built using the production API Dockerfile from a clean `git archive` of that commit. The earlier e3a38f35 image is retained only as superseded evidence and is not the approved artifact.
- In the actual image, with working directory `/tmp`, traits resolve to `/app/config/provider_traits.json`; explicit provider/config/registry paths resolve to the configured private test volume. Real application lifespan/ASGI HTTP calls, migration/database, document conversion and manifest parsing execute through TestClient.
- Synthetic installation writes `/faxdata/operator-providers/image-path-provider/manifest.json`; discovery and traits see that file. A real text document is accepted and the dispatch/status functions use the installed manifest's expected URLs. The container has `--network none`; only provider HTTP responses are substituted. No external fax transmission is claimed.
- Traversal, escaped directory and backlink symlinks are refused in the actual image. Owned proof container/volume are removed after each run.
- Scratch build/runtime evidence: `docker-build-metadata.json`, `docker-api-build.txt`, `docker-paths-proof.json`, `docker-paths-proof.txt`, `build-paths-image.py`, `verify-paths-image.py` under the configuration scratch directory.

## Related required work carried forward

1. Readiness uses import-time `VALID_BACKENDS`. The image begins ready with a bundled provider, but after installing/selecting a new manifest, readiness incorrectly returns503 although dynamic traits, discovery and dispatch find it. This is a reproduced pre-existing registry/activation defect, not a passing readiness claim for installed plugins. Replace the stale set in the canonical configuration integration.
2. Bundled plugin examples are plain JSON while the scrape parser only reads Markdown fences; loading the correct resource still returns400. Complete provider installation must repair this.
3. Installed traits still require the existing reload to refresh the nonempty cache. Manifest dispatch still selects the legacy backend/current credentials. These are explicit activation/profile tasks.
4. Initial configuration migration must consider genuine old cwd-based provider/config locations and report conflicting operator data rather than silently inventing a new selection. Do not confuse a missing legacy file's fabricated default with an operator configuration.

No production deployment, live provider acceptance, complete configuration activation or finished-product claim is made by this subsystem evidence.
