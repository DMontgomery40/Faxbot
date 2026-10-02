# Faxbot configuration values implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace scattered environment parsing and lossy settings serialization with one validated configuration value model, while retaining the approved HTTP/client contracts. This is the first implementation dependency of complete activation; it does not alone claim safe runtime rotation.

**Architecture:** A pure configuration-values module owns field names, environment aliases, defaults, validation, secret classification, and literal persisted-file encoding. It accepts an explicit input mapping and returns a complete immutable value object or a sanitized configuration error. The runtime configuration module owns loading and publishing that object. HTTP handlers, providers and persistence must not independently invent another field map.

**Tech stack:** Python 3.11, Pydantic 2, existing FastAPI/SQLAlchemy application. Existing real database and container test infrastructure.

## Context and interfaces

The whole-product design is approved in `../specs/2026-10-02-faxbot-refresh-design.md`. The user has already authorized implementation. The source compatibility inventory is `.superpowers/sdd/2026-10-02-faxbot-configuration/contracts.md`. Provider path changes are a separate active task; wait for its writer before editing `config.py` or `main.py`.

Tests cross the approved settings HTTP and deployment configuration interfaces. Narrow pure codec tests supplement those interfaces for literal secret preservation; they must use independent literal examples, not round-trip equality as their only oracle. No live provider account calls.

### Required value contract

- All existing `Settings` fields remain available, including inactive and hybrid provider credentials, FreeSWITCH, Documo, SignalWire, features, callback verification, storage and MCP paths. Include persisted-settings bootstrap controls and `SINCH_BASE_URL`, which currently bypasses the model.
- Explicit environment aliases are canonical. Preserve Phaxio/SignalWire callback aliases and the existing Sinch-to-Phaxio credential fallback only when the canonical Sinch variable is absent; an explicitly empty Sinch value clears it.
- Empty outbound/inbound selectors mean inherit the legacy provider. Preserve whether the override was explicit rather than materializing the fallback into an indistinguishable string. Effective selectors still expose the inherited provider to existing clients.
- Environment parsing never changes `os.environ`. Recognized booleans have an explicit accepted true/false vocabulary; typo values fail. Ports are 1–65535, positive byte/TTL/cleanup inputs remain positive, disabled rates/retention/polling allow zero, negative values fail. Unknown provider selection is an error at registry validation, never a fallback to SIP or another provider.
- HTTP patch omission and null preserve a value; empty text/secret clears it; false and zero are not omission. An empty directional selector resets inheritance. Reject unknown patch names and masked-secret placeholders instead of silently storing them as credentials.
- Parse and validate the whole candidate before publication. Invalid candidates leave the previously effective settings and environment unchanged.
- Secret-bearing errors and object representations do not contain raw values. Treat database URLs as secret-bearing. Ordinary settings responses mask credentials and DB userinfo; exports intended for display remain redacted.

### Persisted text contract

- Parse data, never execute or expand it. Shell substitution, backticks and dollar expressions remain literal values. Reject malformed syntax, duplicate keys and unsupported persisted keys with line/key metadata only.
- Preserve conventional comments, optional `export`, unquoted simple values, single-quoted literal strings and escaped double-quoted strings. Generated output must have one canonical parseable form, preserve whitespace/quotes/backslashes/newlines and never write an unescaped additional assignment from a secret.
- Encode every effective configurable field, not only the legacy selected provider. Omit unset directional overrides so reloading retains inheritance. Preserve explicit empty strings where they suppress a fallback.
- Reading a missing optional persisted file uses the environment. An existing enabled but unreadable or invalid persisted file fails clearly; it must not silently boot with another account's environment values.
- File publication uses a private same-directory temporary file, flush/fsync and atomic replace; a failed replacement preserves the old canonical file. No move-old-first sequence. The final containing directory is synchronized where supported. File I/O errors are sanitized. Locking/revision compare and configuration activation ordering belong to the activation task, not an ad-hoc codec lock.

## Task 1: Literal configuration parsing and serialization

**Owner:** Primary after provider-path task is integrated. **Files:** new configuration-values module; focused configuration tests; `config.py` only for integration; deployment docs derived later from metadata.

- [ ] Add a failing settings startup/persistence example with independent hybrid credentials and quote/newline/dollar characters; prove current parsing/export loses data or mutates process environment.
- [ ] Introduce the single field/alias/default/secret definition and immutable values interface; integrate environment construction without duplicating definitions in `config.py`.
- [ ] Add one behavior regression at a time for aliases, explicit empty versus absent, false/zero and sanitized invalid input; implement each before advancing.
- [ ] Replace persisted parsing and generation with literal complete serialization; verify independent expected examples as well as restart round trips.
- [ ] Add invalid-file and atomic replacement failure regressions; implement private publication that retains the old canonical file on failure.
- [ ] Keep runtime activation outside this commit unless the activation design is ready; record the exact distinction in evidence and do not claim provider rotation complete.

## Task 2: HTTP integration and accurate settings projection

**Owner:** Primary. **Files:** configuration module, settings routes, relevant response/input models, existing Settings/Setup client payloads if needed for round-trip proof.

- [ ] Replace manual per-field mutation/export paths with the configuration module. Validate candidate before any environment/global/file mutation.
- [ ] Preserve nested GET, flat PUT and `_meta` compatibility; expose every visible supported field and mask DB credentials.
- [ ] Prove invalid mixed patch cannot apply its valid prefix; omission/null preserve, empty clears, false/zero apply, directional empty resets inheritance.
- [ ] Preserve plugin API shapes while routing values through the same definitions; provider activation/restart behavior follows the separately reviewed activation design.
- [ ] Fix test environment isolation rather than retaining FAX_DISABLED selection overrides or permissive assertions to hide state leakage.

## Task 3: Verification and review

- [ ] Run focused behavior tests, then complete API tests with real SQLite and PostgreSQL using a configuration-owned scratch wrapper/log location.
- [ ] Independent fixed-base/head review, resolve findings, retain corrective evidence.
- [ ] Build actual committed API image and prove enabled persisted-file restart, hybrid credential preservation and invalid-file startup refusal using synthetic configuration and no external fax.
- [ ] Update versioned evidence/progress. Complete provider configuration still requires revisioned account profiles, activation, adapters and in-flight job binding; keep the whole goal active.
