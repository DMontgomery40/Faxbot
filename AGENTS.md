# AGENTS.md - Instructions for AI Assistants

## Project Overview: Faxbot

**Primary Goal:** A fax-sending API with two backend options - Phaxio (cloud) and SIP/Asterisk (self-hosted). Target users are healthcare professionals who need to send occasional faxes without complex setup.

**Current Status:** Phaxio integration needs to be implemented, replacing the previous Twilio integration that was based on a discontinued API. The project needs continued auditing, fixes, and documentation restructuring.

**CRITICAL: Project Name is "Faxbot" (not "OpenFax" or any other name). MCP server is "faxbot-mcp".**

## Critical Context You Must Understand

### 1. **Two Completely Different Backends**
This is NOT a single system - it's two different fax transmission methods:

- **Phaxio Backend**: Uses Phaxio's cloud fax API. No telephony knowledge required.
- **SIP/Asterisk Backend**: Self-hosted using Asterisk PBX with SIP trunks and T.38 protocol.

**⚠️ CRITICAL**: Never mix instructions between these backends. They are mutually exclusive.

### 2. **MCP Server Component**
**MCP = Model Context Protocol** - This is a 2024/2025 standard for AI assistant tool integration.

The MCP server (`mcp_server.js`, `mcp_http_server.js`) is a **separate component** that:
- Runs independently of the main fax API
- Provides tools for AI assistants (Claude, etc.) to send faxes
- Acts as a bridge between AI and the main fax API
- Uses the same API endpoints but wraps them for AI consumption

**Do NOT confuse MCP setup with main API setup.** They are different layers:
```
AI Assistant (Claude) → MCP Server → Fax API → Twilio/Asterisk → Fax Transmission
```

### 3. **File Structure Understanding**
```
/api/                   # Main FastAPI fax service
  /app/                 # Core application
    main.py            # Main API with dual backend support
    phaxio_service.py  # Phaxio integration (NEEDS TO BE IMPLEMENTED)
    ami.py             # Asterisk Manager Interface (SIP only)
    config.py          # Environment configuration
  mcp_server.js        # MCP server for AI integration
  mcp_http_server.js   # HTTP version of MCP server

/asterisk/             # Asterisk configuration (SIP backend only)
  /etc/asterisk/       # Asterisk config files
    extensions.conf    # Dialplan for T.38 fax
    pjsip.conf.template # SIP trunk configuration

/freeswitch/           # EMPTY - legacy/placeholder directory

docker-compose.yml     # Container orchestration
.env.example          # Environment template
```

## Immediate Tasks (Priority Order)

### 1. **Create Missing .env.example File** 
**Status**: CRITICAL - File does not exist but is referenced everywhere

Create `/Users/davidmontgomery/faxbot/.env.example` with:
```env
# API Configuration
FAX_DATA_DIR=/faxdata
MAX_FILE_SIZE_MB=10
FAX_DISABLED=false
API_KEY=your_secure_api_key_here

# Backend Selection - Choose ONE
FAX_BACKEND=sip  # Options: "sip" (self-hosted) or "phaxio" (cloud)

# === PHAXIO BACKEND CONFIGURATION ===
# Only needed if FAX_BACKEND=phaxio
PHAXIO_API_KEY=your_phaxio_api_key_here
PHAXIO_API_SECRET=your_phaxio_api_secret_here
PHAXIO_CALLBACK_URL=https://your-domain.com/phaxio-callback
PUBLIC_API_URL=https://your-domain.com

# === SIP/ASTERISK BACKEND CONFIGURATION ===
# Only needed if FAX_BACKEND=sip
ASTERISK_AMI_HOST=asterisk
ASTERISK_AMI_PORT=5038
ASTERISK_AMI_USERNAME=api
ASTERISK_AMI_PASSWORD=changeme

# SIP Trunk Settings (from your provider)
SIP_USERNAME=your_username
SIP_PASSWORD=your_password
SIP_SERVER=sip.provider.example
SIP_FROM_USER=+15551234567
SIP_FROM_DOMAIN=sip.provider.example

# === COMMON SETTINGS ===
FAX_LOCAL_STATION_ID=+15551234567
FAX_HEADER=Company Name
DATABASE_URL=sqlite:///./faxbot.db
TZ=UTC
```

### 2. **Implement Phaxio Integration**
**Status**: CRITICAL - Replace discontinued Twilio integration

Create complete Phaxio integration:
- `phaxio_service.py` - Phaxio API wrapper service
- Update `main.py` to use Phaxio instead of Twilio
- Update `config.py` with Phaxio settings
- Add Phaxio webhook endpoint `/phaxio-callback`
- Update database models to track Phaxio fax IDs
- Add comprehensive error handling and retry logic

**Phaxio API Documentation**:
- Main API: https://www.phaxio.com/docs/api/v2
- Send Fax: https://www.phaxio.com/docs/api/v2/faxes/create_and_send_fax
- HIPAA Compliance: https://www.phaxio.com/docs/security/hipaa

### 3. **Documentation Restructuring**
**Current Problem**: Single massive README.md (314 lines) is overwhelming

**Required Structure**:
```
README.md              # Main overview with links
docs/
  PHAXIO_SETUP.md      # Complete Phaxio setup guide (include HIPAA link)
  SIP_SETUP.md         # Complete SIP/Asterisk setup guide
  MCP_INTEGRATION.md   # AI assistant integration
  API_REFERENCE.md     # API endpoints and examples
  TROUBLESHOOTING.md   # Common issues and solutions
```

**New README.md should be ~50 lines max** with clear navigation:
```markdown
# Faxbot

Simple fax-sending API with AI integration. Choose your backend:

## 🚀 Quick Start Options

### Option 1: Phaxio (Recommended for Most Users)
- ✅ 5-minute setup
- ✅ No telephony knowledge required  
- ✅ Pay per fax (~$0.07/page)
- ✅ HIPAA compliant with BAA

**[→ Phaxio Setup Guide](docs/PHAXIO_SETUP.md)**

### Option 2: Self-Hosted SIP/Asterisk
- ✅ Full control
- ✅ No per-fax charges
- ⚠️ Requires SIP trunk and T.38 knowledge

**[→ SIP Setup Guide](docs/SIP_SETUP.md)**

## 🤖 AI Assistant Integration
**[→ MCP Integration Guide](docs/MCP_INTEGRATION.md)**

## 📚 Documentation
- **[API Reference](docs/API_REFERENCE.md)** - Endpoints and examples
- **[Troubleshooting](docs/TROUBLESHOOTING.md)** - Common issues
```

### 4. **Code Audit Priorities**

#### A. **Security Issues to Fix**
- AMI password defaults to "changeme" - add warning in docs
- API key is optional (blank disables auth) - document security implications
- No rate limiting on main API - add note about reverse proxy
- Twilio PDF URLs use basic token validation - acceptable for now

#### B. **Reliability Issues to Investigate**
- SQLite database stored in container (data loss on rebuild) - document volume mounting
- No retry logic for failed Phaxio API calls - add basic retry with exponential backoff
- AMI client infinite reconnection without backoff - add proper backoff
- Ghostscript dependency not validated - add startup check

#### C. **Empty/Placeholder Code to Clean**
- `/freeswitch/` directory is completely empty - remove or document purpose
- Some test coverage is minimal - expand Phaxio tests
- Error handling could be more specific - improve error messages

### 5. **Phaxio Integration Implementation & Testing**
**Status: NEEDS TO BE IMPLEMENTED**

Implement and verify these components:
- `phaxio_service.py` - Complete Phaxio API integration
- PDF serving endpoint `/fax/{job_id}/pdf` - Security validation
- Webhook handling `/phaxio-callback` - Test status updates
- Backend switching logic in `main.py` - Update for Phaxio
- Database models - Update for Phaxio fax IDs
- Error handling - Phaxio-specific error codes
- Tests - Comprehensive Phaxio test coverage

## Documentation Writing Guidelines

### For Phaxio Documentation
**Target User**: Non-technical healthcare worker who needs to fax occasionally

**Tone**: Simple, step-by-step, assume zero telephony knowledge
**Include**:
- Exact Phaxio console screenshots
- Copy-paste environment variables
- Troubleshooting for common Phaxio errors
- Cost estimates and billing info
- **MANDATORY**: HIPAA compliance section with link to https://www.phaxio.com/docs/security/hipaa
- BAA signing process for healthcare users

### For SIP Documentation  
**Target User**: Technical person with some Linux/Docker experience

**Tone**: Technical but clear, assume some networking knowledge
**Include**:
- SIP provider recommendations
- T.38 requirement explanations
- Port forwarding instructions
- NAT traversal considerations
- Asterisk log interpretation

### For MCP Documentation
**Target User**: AI enthusiast who wants voice-controlled faxing

**Tone**: Explain MCP concept from scratch (assume pre-2024 training data)
**Include**:
- What MCP is and why it exists
- Difference between stdio and HTTP transport
- Claude Desktop vs Cursor setup
- Voice command examples
- Base64 encoding explanation (current limitation)

## Critical Don'ts

1. **Never mix backend instructions** - Phaxio users don't need Asterisk info
2. **Don't assume MCP knowledge** - Explain it's for AI tool integration
3. **Don't oversimplify SIP setup** - It genuinely requires technical knowledge
4. **Don't promise features that don't exist** - No voice integration yet, just MCP tools
5. **Don't ignore the empty freeswitch directory** - Address it or remove it
6. **NEVER call the project "OpenFax"** - It's "Faxbot" (main) and "faxbot-mcp" (MCP server)

## Testing Strategy

### Manual Testing Required
1. **Phaxio Backend**:
   - Set FAX_BACKEND=phaxio
   - Test PDF upload and conversion
   - Verify Phaxio API calls work
   - Test webhook callbacks
   - Verify HIPAA compliance features

2. **SIP Backend**:
   - Set FAX_BACKEND=sip  
   - Ensure Asterisk dependency works
   - Test AMI communication
   - Verify T.38 configuration

3. **MCP Integration**:
   - Test both stdio and HTTP servers
   - Verify base64 encoding/decoding
   - Test error handling

### Automated Testing Gaps
- No integration tests between backends
- Limited Phaxio API mocking
- No end-to-end MCP testing

## Current Project Health Assessment

**Strengths**:
- Clean FastAPI architecture
- Proper separation between backends
- Good Pydantic models
- Comprehensive MCP implementation

**Major Issues**:
- Phaxio integration not implemented (Twilio was discontinued)
- Monolithic documentation
- Empty directories causing confusion
- Some pseudo-documentation (promises features that need work)

**Overall**: Solid foundation but needs Phaxio integration implemented and better documentation structure for different user types.

## Success Criteria

When you're done, a user should be able to:
1. **Phaxio User**: Click a link, follow 5-10 clear steps, and send their first fax in under 10 minutes
2. **SIP User**: Understand exactly what they need (SIP trunk, T.38 support) and follow technical setup
3. **AI User**: Understand what MCP is and get voice-controlled faxing working with their assistant

The documentation should be so clear that each user type never sees irrelevant information for the other backends.

## File Naming Conventions
- Use UPPERCASE.md for main documentation files
- Use snake_case for code files
- Be explicit: `PHAXIO_SETUP.md` not `cloud_setup.md`
- Keep URLs short and memorable for linking
- **Project name is "Faxbot"** - update all references from "OpenFax"

## Final Notes
This project serves a real need (healthcare fax requirements) but bridges between highly technical (SIP/T.38) and non-technical (Phaxio API) solutions. The documentation must reflect this reality - don't dumb down the SIP setup (it IS complex) but make the Phaxio path genuinely simple.

**For Healthcare Users**: Phaxio offers HIPAA compliance with BAA signing - this MUST be prominently featured in PHAXIO_SETUP.md with direct link to https://www.phaxio.com/docs/security/hipaa

The MCP integration is forward-thinking (2024/2025 AI tool standard) but currently requires manual base64 encoding. Document current limitations clearly while showing the vision.

**REMEMBER**: This is "Faxbot" not "OpenFax" - update ALL references throughout the codebase and documentation.
