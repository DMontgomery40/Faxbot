# Changelog

All notable changes to the Faxbot MCP server will be documented in this file.

## [2.0.0] - 2025-01-XX

### Added
- Complete MCP 2.0 integration with modern standards
- Streamable HTTP transport for cloud deployment
- Enhanced tool output schemas for better AI interaction
- Advanced security controls and permissions
- Real-time progress notifications
- Comprehensive error handling and validation
- Auto-configuration for Claude Desktop, Cursor, and system PATH
- One-click installer with platform detection
- Support for both stdio and HTTP transports
- Priority queuing and metadata support
- Comprehensive logging and monitoring

### Changed
- Renamed from "Open Fax by Codex" to "Faxbot"
- Updated to MCP SDK 2.0 standards
- Enhanced tool definitions with structured schemas
- Improved installation experience
- Better error messages and user feedback

### Technical
- Node.js 18+ requirement
- Modern JavaScript with ES6+ features
- Joi validation for all inputs
- Express.js for HTTP transport
- Helmet.js for security
- Winston for structured logging

## [1.0.0] - Previous

### Added
- Initial MCP integration
- Basic fax sending functionality
- T.38 protocol support via Asterisk
- PDF and TXT file support
- SQLite job tracking
- Docker deployment support
