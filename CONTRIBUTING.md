# Contributing to Faxbot

Thanks for your interest in Faxbot! Whether you're reporting a bug, requesting a feature, or contributing code, we're here to help.

## Getting Help

**Don't hesitate to open an issue.** Seriously. Faxbot has many moving parts, and we'd rather help you get unstuck than have you struggle alone.

## When Opening an Issue

To help us assist you quickly, please include:

### Your Configuration
- **Backend**: Which backend are you using? (`phaxio`, `sinch`, `sip`, or `FAX_DISABLED=true` for testing)
- **Compliance**: Are you bound by HIPAA requirements, or are you a non-healthcare user?
- **MCP Integration** (if applicable):
  - Which MCP server? (Node.js stdio/HTTP/SSE, or Python stdio/SSE)
  - Which transport? (stdio, HTTP, or SSE+OAuth)

### What Happened
- **Expected behavior**: What did you think would happen?
- **Actual behavior**: What actually happened?
- **Steps to reproduce**: How can we recreate the issue?

### Supporting Information
- **Logs**: Relevant log output (see note about PHI/PII below)
- **Screenshots**: If applicable, especially for UI-related issues
- **Environment**: Docker, local development, cloud deployment?

### ⚠️ Important: Protect PHI/PII
**Never include protected health information (PHI) or personally identifiable information (PII) in issues, logs, or screenshots.** This includes:
- Phone numbers
- Patient names or identifiers  
- Document contents
- API keys or secrets

Redact sensitive information with `[REDACTED]` or `***` before sharing.

## Types of Contributions

### Bug Reports
Use the issue template and include the configuration details above.

### Feature Requests  
Describe your use case and why the feature would be valuable. Consider which backends it would apply to.

### Code Contributions
1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Make your changes
4. Test across relevant backends (see [Testing](#testing))
5. Commit with clear messages
6. Push to your branch
7. Open a Pull Request

## Testing

These commands run the same checks as CI (`.github/workflows/ci.yml`). You need [uv](https://docs.astral.sh/uv/), Node 24, and Ghostscript (CI installs `ghostscript` with apt).

| Command | What it does |
| --- | --- |
| `make venv` | Creates `.venv` with Python 3.11 and installs `api/requirements.txt` and `python_mcp/requirements.txt` |
| `make test-local` | Runs the backend tests from `api/` with the same command and environment as the `test-api` job |
| `make ui-build` | Runs `npm ci` and `npm run build` (typecheck and build) in `api/admin_ui` |
| `npm ci --prefix node_mcp && npm --prefix node_mcp run check` | Checks that every Node MCP module parses and imports |

`make test-local` sets the CI environment:

- `FAX_DISABLED=true` (no faxes are sent)
- `FAX_DATA_DIR=./faxdata`
- `DATABASE_URL=sqlite:///./test_faxbot_ci.db`

Options:

- `PYTEST_ARGS` passes extra pytest arguments, for example `make test-local PYTEST_ARGS="tests/test_api.py -x"`.
- `FAXBOT_SCHEMA_TEST_POSTGRES_URL`, if set in your environment, points the PostgreSQL schema tests at a disposable database. Without it they are skipped.
- `VENV` uses a virtualenv other than `.venv`, for example `make test-local VENV=/path/to/Faxbot/.venv` from a git worktree.

If your change affects a specific fax backend or MCP transport, also test it against that backend or transport.

### Enterprise testing boundary

Enterprise features are verified with synthetic fixtures, unit tests, mocked connector contracts and local integration tests, including local end-to-end flows where useful. These checks must still pass. We do not have real customer organizations, enterprise identity tenants, case systems or regulatory portals available for live acceptance testing.

Missing live enterprise validation, credentials, customer access or production data must **never block CI, merge, release or completion of an otherwise implemented generic capability**. Mark that capability implemented when its software checks pass; separately label external integrations or templates that have not been validated. Customer configuration and activation requirements apply to that deployment, not repository build gates.

Any future live customer acceptance is separate, explicitly requested work and opt-in only. Never add it to required checks, automatically run it because credentials happen to exist, or fail ordinary tests because those credentials are absent. Record “not live-validated” honestly without treating it as a failing software test. This rule overrides broader live-verification language in older plans for enterprise work.

## Code Style

- **Python**: Follow PEP 8, use `black` for formatting
- **JavaScript/Node.js**: Use ESLint configuration in the project
- **Documentation**: Whenever a capability is added, changed, or removed, update `README.md`, its bottom-of-file roadmap, and the relevant docs in the same implementation change. Do not defer this to a later release. Mark roadmap items implemented only when usable and verified; keep experiments and partial work unfinished. Agents should also read [AGENTS.md](AGENTS.md).

## Security Considerations

Faxbot handles sensitive healthcare data. When contributing:

- Never commit API keys, secrets, or test PHI
- Consider HIPAA implications for new features
- Use secure defaults
- Document security requirements clearly

## Questions?

Open an issue with the "question" label. We're happy to help you understand the codebase, architecture decisions, or how to implement your use case.

The maintainers are friendly and want Faxbot to succeed. Don't be shy!
