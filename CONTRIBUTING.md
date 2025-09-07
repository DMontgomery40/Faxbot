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

Faxbot supports multiple backends and configurations. When contributing code:

- **Test Mode**: Use `FAX_DISABLED=true` for development—no actual faxes are sent
- **Backend-Specific**: If your change affects a specific backend, test with that backend
- **MCP Changes**: Test the relevant MCP transport (stdio/HTTP/SSE)
- **SDK Changes**: Test both Node.js and Python SDKs if applicable

## Code Style

- **Python**: Follow PEP 8, use `black` for formatting
- **JavaScript/Node.js**: Use ESLint configuration in the project
- **Documentation**: Update relevant docs in `docs/` directory

## Security Considerations

Faxbot handles sensitive healthcare data. When contributing:

- Never commit API keys, secrets, or test PHI
- Consider HIPAA implications for new features
- Use secure defaults
- Document security requirements clearly

## Questions?

Open an issue with the "question" label. We're happy to help you understand the codebase, architecture decisions, or how to implement your use case.

The maintainers are friendly and want Faxbot to succeed. Don't be shy!
