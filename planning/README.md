# Maintained planning sources

This directory contains manually maintained product direction and implementation guidance. It is outside `docs/` and is not generated output. Keep one authoritative source for each plan; documentation pages can point here without duplicating the text.

- [Enterprise correspondence](enterprise-correspondence.md): shared capabilities, current-code gaps, interfaces, optional templates, guided setup and acceptance criteria. Preserve the current Phase 1 and four-fix scope.
- The root [README roadmap](../README.md#roadmap) owns current implementation status. Do not create a separate status roadmap here.
- [AGENTS.md](../AGENTS.md) and [CLAUDE.md](../CLAUDE.md) define maintenance rules and entry points. Local private research remains supporting evidence, not generated product documentation.

## What the generators actually write

Inspected October 3, 2026 against the current checkout. Re-check these boundaries when changing the tooling.

| Mechanism | Write boundary |
| --- | --- |
| [Source-reference generator](../scripts/docs_ai/generate_reference.py) and [API renderer](../scripts/docs_ai/build_api.py) | Default output is `docs/generated/`: OpenAPI, configuration/provider references, callback excerpts, provenance and rendered API assets. These files are disposable outputs; never put implementation requirements there. Explicit output-directory arguments can change that destination. |
| [Docs Autopilot push workflow](../.github/workflows/docs-ai.yml) | Creates a proposed update plan as an artifact; it does not apply prose edits on every push. |
| Optional AI prose proposals | Written by `make docs-propose` (Codex, read-only, on the maintainer's own sign-in) or by the manually dispatched workflow job (OpenRouter with the `OPENROUTER_API_KEY` secret). [Patch validation](../scripts/docs_ai/validate_doc_patch.py) permits ordinary Markdown under `docs/` and excludes `docs/architecture/`, `docs/generated/` and agent/skill instruction filenames. Root README/roadmap, root agent instructions and `planning/` are outside its allowed scope. A proposal is saved only after validation, and the workflow and local `--apply` command both validate again before applying. The workflow opens a pull request only when asked. |
| [MkDocs/Mike publication](../.github/workflows/mkdocs-deploy.yml) | Restores the generated reference overlay, builds/publishes the website on `gh-pages`, and does not regenerate maintained planning sources. The [artifact hook](../scripts/docs_ai/use_built_artifact.py) replaces the built site directory, not source documentation. |

The [old architecture location](../docs/architecture/2026-10-03-enterprise-correspondence.md) is now a navigation pointer. The full enterprise specification lives here so replacement of the public documentation tree does not erase its source.

## Legacy scripts are not routine generation

The current workflows do not invoke the following migration tools:

- [Branch mirror](../scripts/docs_tools/mirror_from_branch.py) overwrites a fixed set of operator guides and can restore historical pages that have since been archived.
- [Markdown cleanup](../scripts/docs_tools/cleanup_mkdocs_content.py) rewrites matching content recursively under its supplied directory. Giving it the repository root includes planning and ignored research/archive files.

Do not use these scripts for ordinary documentation updates. An explicitly requested historical migration needs its own scoped review and preservation of maintained sources. Relocating a file protects it from a `docs/` replacement, not from arbitrary scripts pointed at the whole repository.

## Maintenance rules

Edit the enterprise specification here and update the root README/roadmap and affected guidance in the same capability change. Keep the old documentation pointer, agent entry points and local research cross-references current. Preserve implemented/partial/proposed labels and source limitations. Never infer that documented future work has shipped merely because a generator publishes a page.

Changes to documentation automation must preserve these write boundaries and pass its patch-scope tests. Maintained planning and agent instructions may be read as context but must not become AI patch targets.
