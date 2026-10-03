# Documentation and GUI verification checkpoint

## Clean hosted documentation

Commit `4f3effef803b2017d4ea2c72b38af1fe9f1ef7de` was pushed to `feat/faxbot-refresh`.

- [Docs Autopilot run 37082230477](https://github.com/DMontgomery40/Faxbot/actions/runs/37082230477): success, generates a plan from the actual previous push SHA.
- [MkDocs preview run 37082230561](https://github.com/DMontgomery40/Faxbot/actions/runs/37082230561): success, installs locked documentation dependencies, runs11 documentation tests, generates references with `--require-clean`, builds Redocly and strict MkDocs, uploads the source-versioned artifact.
- Downloaded that artifact, rather than rebuilding it locally, into `/tmp/faxbot-docs-ci-4f3effef`. Its provenance declares the exact commit above, source_ref `feat/faxbot-refresh`, and `source_tree_dirty:false`.
- Served unmodified artifact at localhost8848. Primary Browser GUI opened the overview and verified the exact commit/no dirty warning/no invalid generated-page edit action. Real individual keystrokes in MkDocs search for `MAX_FILE_SIZE_MB` returned the generated configuration page; clicking it opened the table and highlighted the field.

The workflow publishes with Mike on main pushes; refresh-branch/PR events build previews. The actual main publication has not occurred because the whole-product PR remains a draft. A real local bare-remote/fresh-clone publication test verifies preservation of gh-pages history/content/CNAME, exact prebuilt artifact reuse, source versions, and delayed older publication retaining newer latest. This is publication-mechanism evidence, not evidence of a production docs deploy.

Generated references cover OpenAPI route/model declarations, typed configuration fields, provider traits and source provenance. They do not invent live instance state or fax delivery. Maintained instructional pages and the separately hosted legacy faxbot.net/api compatibility surface require final reconciliation with the finished product.

- Independent Sol Browser replay on the actual hosted artifact in Brave: overview -> API reference -> type Send Fax -> click result visibly shows multipart file/to fields and202 response schema/example, with no console warnings/errors. Screenshots are retained in the scratch evidence directory. The original hydration errors were fixed with the supported client-rendering template and a local pinned Redoc bundle. The in-app browser still paints the long-page anchor view white, while Brave renders it correctly.

## GUI-driven corrections

All user-facing acceptance in this checkpoint used real Browser clicks/keystrokes in an isolated synthetic local instance. No external fax was sent.

- `60de89b8`: explicit login submission, repaired Dashboard Security/SystemStatus destinations, AllStatuses filter display, destination accessibility, factual Setup provider description, and surfaced job action failures.
- `0ade455e` / `223b430a`: Settings controls retain actual false/zero and distinguish current from unsaved edits. Primary GUI verified Audit/Persisted/Inbound Disabled, draft Enabled and0 edits, then LoadSettings restoring actual state.
- Primary GUI verified Logout -> type key stays on Login -> Enter reaches Dashboard; stored-key restoration; Security -> Keys; SystemStatus -> Diagnostics; Jobs Success -> AllStatuses restores the synthetic queued job.
- PDF download acceptance is unresolved: real DownloadPDF clicks in both the in-app browser and independent Brave tab yielded no download event or visible completion. Both consoles were empty. Source tracing found the standard authenticated Blob/anchor path and no additional demonstrated cause. No download success is inferred from HTTP200.
- `28c7c1bb` fixes the inherited Setup provider display, provider/security control names, field-specific secret toggles and duplicate Tunnel heading. Independent Sol GUI replay passed; primary replay confirms inheritance and one heading in the combined build.
- `24f28678` makes the disabled Inbox state explicit, disables inapplicable actions, and provides Go to Settings without adding any privileged configuration request. Independent Sol and primary GUI replay both confirm the disabled message and correct main/subtab Settings destination. Enabled-inbound and scoped-key flows remain part of the broader runtime/RBAC acceptance.

The scratchpad is `.superpowers/sdd/2026-10-02-faxbot-configuration/gui-bug-scratchpad.md`; observations stay open until replay establishes the result.

## Hosted implementation checks

[CI run 37082234724](https://github.com/DMontgomery40/Faxbot/actions/runs/37082234724) succeeds at 4f3effef: **691 passed, 14 dialect skips** on Linux/PostgreSQL16, plus the Admin UI build. Backend tests supplement GUI acceptance; neither these checks nor documentation generation establishes whole-product completion.

## Independent documentation correction review

Independent review found two defects after the initial clean hosted run: Git rename patches could hide deletion of backend source behind a docs destination, and prebuilt artifact canonical/sitemap links omitted the Mike source version. Both were reproduced before repair.

`e59247f0` validates proposals in a temporary Git index, inspects both rename sides and regular-file modes, rejects protected/control Markdown targets, and checks the actual staged delta before creating the optional proposal PR. Authoritative artifacts now build with `MIKE_DOCS_VERSION` set to the source SHA. Real MkDocs regression verifies canonical/sitemap version URLs. Full focused docs suite: **24 passed**. Independent correction review: **13 focused checks passed**, no additional concrete defect, correction approved. A new hosted clean run is required for the corrected commit.

### Shared tunnel controls

`5f9e1a6f` repairs shared select label/description associations and password toggle names without replacing the tunnel controls or changing tunnel behavior. Independent real GUI replay selected a Tailscale draft, verified the visible helper association, and used Space to toggle Auth Key password/text/password with matching Show/Hide names and pressed state. It restored None without Apply, credential entry or network changes. The final build passed; console warnings/errors were empty. Primary GUI replay of the combined build independently confirms the named Tunnel Provider selection.
