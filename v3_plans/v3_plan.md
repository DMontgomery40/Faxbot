# Prompt for Agent with Full DMontgomery40/Faxbot Codebase Access

## Critical Mission
You have complete access to the dmontgomery40/faxbot repository. The v3_plan.md attempted to create a unified plugin system serving both Python and Node, but this created unnecessary complexity. Your task is to analyze the actual codebase and create TWO INDEPENDENT plugin systems that don't rely on each other.

## Phase 1: Codebase Reality Check (DO THIS FIRST)

### Analyze Current Provider Implementation
```
EXAMINE these files to understand current architecture:
- api/app/phaxio_service.py 
  → How does Phaxio currently work?
  → What dependencies does it have?
  → How are credentials handled?
  
- api/app/sinch_service.py
  → How does Sinch integration work?
  → What's the auth mechanism?
  
- api/app/ami.py + conversion.py
  → How does SIP/Asterisk work?
  → What conversion utilities are shared?
  → How does AMI authentication work?
  
- api/app/main.py
  → How are providers currently selected?
  → What's the routing logic?
  → How are files passed to providers?
  
- api/app/config.py
  → What environment variables exist?
  → How is configuration loaded?
  → What's the precedence order?
  
- api/app/storage.py
  → How are files stored/retrieved?
  → What's the tokenization mechanism?
  → How do providers access files?
```

### Map MCP Server Reality
```
ANALYZE MCP implementations:
- node_mcp/
  → What tools actually exist?
  → How do they call the REST API?
  → What's the authentication mechanism?
  → How are files passed (base64 vs filepath)?
  
- python_mcp/
  → What tools actually exist?
  → How do they differ from Node MCP?
  → What's the actual implementation?
```

### Understand Admin UI Current State
```
EXAMINE Admin UI:
- api/admin_ui/src/
  → What configuration UI exists today?
  → How are providers currently selected?
  → What API endpoints does it call?
  → How is authentication handled?
```

### Identify HIPAA Controls
```
FIND all HIPAA-related code:
- Where is PHI handled?
- What encryption is in place?
- Where are audit logs?
- What can't be logged?
- What are the secure defaults?
```

## Phase 2: Reality Matrix & Config Store Baseline

Create a reality matrix:

```markdown
| Component | Exists Today | v3 Plan Says | Gap Analysis |
|-----------|--------------|--------------|--------------|
| Plugin Discovery | ??? | Entry points/npm scan | ??? |
| Config Store | ??? | Atomic JSON writes | ??? |
| Provider Adapter | ??? | Plugin instance manager | ??? |
| Dynamic MCP Tools | ??? | Config-based registration | ??? |
| Admin UI Plugins Tab | ??? | Full CRUD UI | ??? |
| Plugin Manifests | ??? | JSON Schema validation | ??? |
| Installation | ??? | pip/npm with allowlist | ??? |
```

## Phase 3: Identify Critical Dependencies

### Questions You MUST Answer:

1. **File Flow**
   - How do files get from MCP → API → Provider → External Service?
   - Where are files stored during processing?
   - How are PDFs tokenized for cloud providers?
   - Can both Node and Python access the same storage mechanism?

2. **Authentication Flow**
   - How do MCP servers authenticate with the API?
   - How does the Admin UI authenticate?
   - How are provider credentials stored/accessed?
   - What's the API key mechanism?

3. **Configuration Precedence**
   - What wins: env vars, config files, or database?
   - How are configs reloaded?
   - What requires a restart vs hot reload?

4. **Provider Selection Logic**
   - How does main.py choose between Phaxio/Sinch/SIP?
   - What's the fallback behavior?
   - How are errors handled?

5. **Shared Utilities**
   - What functions in conversion.py are used by multiple providers?
   - What storage operations are shared?
   - What logging/audit mechanisms are shared?

## Phase 4: Design the Split

### Python Plugin System (Backend-Focused)

```markdown
SCOPE:
- Executes in FastAPI process
- Handles actual fax transmission (Phaxio, Sinch, SIP)
- Manages storage backends (S3, local)
- Handles authentication providers (OIDC)

CRITICAL PATHS TO PRESERVE:
- [List actual file paths from codebase]
- [List actual env vars being used]
- [List actual API endpoints]

NEW COMPONENTS NEEDED:
- api/app/plugins/python_plugin_base.py
- api/app/plugins/python_registry.py
- api/app/services/python_config_store.py
- config/python-plugins.json

MIGRATION STRATEGY:
1. Extract phaxio_service.py → faxbot_plugin_phaxio
2. Extract sinch_service.py → faxbot_plugin_sinch
3. Extract ami.py → faxbot_plugin_sip
4. Keep shared utilities in core
```

### Node Plugin System (MCP/UI-Focused)

```markdown
SCOPE:
- MCP tool enhancements
- UI helper functions
- Client-side validations
- NO direct PHI handling

CRITICAL PATHS TO PRESERVE:
- [List actual MCP tool names]
- [List actual MCP-to-API calls]

NEW COMPONENTS NEEDED:
- node_mcp/src/node_plugin_loader.js
- node_mcp/src/node_tool_registry.js
- config/node-plugins.json

MIGRATION STRATEGY:
1. Keep existing MCP tools as-is
2. Add plugin-based tool registration on top
3. Maintain backward compatibility
```

## Phase 5: Implementation Order (CRITICAL)

### Order of Operations to Avoid Breaking Production:

1. **Feature Flag Everything**
   ```python
   if os.getenv('FEATURE_V3_PLUGINS') == 'true':
       # New plugin path
   else:
       # Existing path (phaxio_service, sinch_service, ami)
   ```

2. **Backend First (Python)**
   - Week 1: Create plugin base classes without changing existing code
   - Week 2: Create adapters that wrap existing services
   - Week 3: Test with feature flag enabled in dev
   - Week 4: Migration script for config

3. **Frontend Updates**
   - Week 5: Add Plugins tab (read-only first)
   - Week 6: Add configuration UI
   - Week 7: Test with real configs

4. **Node Plugins (Lower Priority)**
   - Week 8: Node plugin discovery
   - Week 9: Dynamic tool registration
   - Week 10: Test MCP with plugins

## Phase 6: Security Audit Points

### HIPAA Critical Paths (MUST VERIFY):

```markdown
CHECK these specific items:
1. Are API keys masked in logs? → Check: [specific log files]
2. Is PHI encrypted at rest? → Check: storage.py implementation
3. Are audit logs complete? → Check: where audit() is called
4. Are temp files securely deleted? → Check: cleanup routines
5. Are backups encrypted? → Check: backup mechanisms
```

### Plugin Security (MUST IMPLEMENT):

```markdown
1. Allowlist Location: docs/plugin-allowlist.json
2. Checksum Verification: 
   - Python: Before pip install
   - Node: Before npm install
3. Config Validation:
   - Never trust plugin-provided schemas
   - Validate against our schema first
4. Secrets Management:
   - How are plugin secrets stored?
   - How are they passed to plugins?
   - How are they rotated?
```

## Phase 7: Testing Strategy

### What to Test First (Risk-Based):

```markdown
1. Provider Switching
   - Can you still switch between Phaxio/Sinch/SIP?
   - Do credentials still work?
   - Are errors handled?

2. File Processing
   - Can files still flow through the system?
   - Are PDFs converted correctly?
   - Are tokens generated?

3. MCP Tools
   - Do existing tools still work?
   - Can you send a fax via MCP?
   - Are responses correct?

4. Admin UI
   - Can you still configure providers?
   - Do settings persist?
   - Are validations working?
```

### Integration Test Critical Paths:

```python
# Test these exact flows:
def test_phaxio_via_plugin():
    """Current phaxio_service.py behavior MUST work via plugin"""
    # 1. Upload PDF
    # 2. Send via Phaxio plugin
    # 3. Check status
    # 4. Verify webhook

def test_config_migration():
    """Env vars MUST migrate to plugin config"""
    # 1. Set PHAXIO_API_KEY env
    # 2. Run migration
    # 3. Verify config/python-plugins.json
    # 4. Test sending works
```

## Phase 8: Rollback Plan

### If Something Breaks:

```markdown
IMMEDIATE ROLLBACK:
1. Set FEATURE_V3_PLUGINS=false
2. Restart services
3. Existing code paths take over

PARTIAL ROLLBACK:
1. Disable specific plugin via config
2. Fall back to legacy provider
3. Keep other plugins running

DATA ROLLBACK:
1. Config backup at: config/python-plugins.json.bak
2. Restore with: cp config/python-plugins.json.bak config/python-plugins.json
3. Restart to apply
```

## Critical Warnings for Implementation

### DO NOT:
- Remove ANY existing files until migration is proven
- Change ANY existing env var names
- Modify ANY existing API endpoints
- Break ANY existing MCP tools
- Assume ANYTHING is unused without tracing all code paths
- Enable remote plugin installation without security review
- Log ANY PHI content
- Allow non-HIPAA mode without explicit flag

### MUST DO:
- Keep parallel code paths during migration
- Test with real provider credentials
- Verify audit logs still work
- Check all error paths
- Test with actual PHI-like data (in dev)
- Document every assumption you make
- Get approval before removing legacy code

## Specific Files to Check

```markdown
BEFORE you implement ANYTHING, examine these files:

Configuration:
- .env.example - what vars are documented?
- docker-compose.yml - what's configured there?
- Dockerfile - what's baked in?

Provider-Specific:
- Any file with 'phaxio' in name
- Any file with 'sinch' in name  
- Any file with 'ami' or 'asterisk' in name
- Any file with 'sip' in name

MCP-Specific:
- node_mcp/package.json - dependencies
- node_mcp/src/server.js - tool registration
- python_mcp/server.py - tool registration

Admin UI:
- api/admin_ui/src/App.js - routing
- api/admin_ui/src/api/ - API clients
- api/admin_ui/src/components/ - existing config UI

Testing:
- api/tests/ - what's already tested?
- What would break if you changed X?
```

## Transport Update (WebSocket)

Given new requirements, WebSocket is now an approved transport in addition to stdio/HTTP/SSE (Node MCP). Implementation will be tracked under Phase 5.

## Your Deliverables

### CRITICAL: Living Documentation Process

**You will create and continuously update THREE documents as you work:**

1. **PROGRESS.md** - Your work tracker (START HERE)
2. **v3_python_plan.md** - Python plugin implementation
3. **v3_node_plan.md** - Node plugin implementation

## Document 1: PROGRESS.md (Your Task Tracker)

Create this FIRST and update after EVERY work session:

```markdown
# Faxbot v3 Plugin System - Progress Tracker

## Last Updated: [timestamp]
## Current Phase: [Phase X - Name]
## Hours Spent: X
## Blocked On: [nothing | specific issue]

## Phase Checklist

### Phase 1: Reality Check ✅ COMPLETE (2 hours)
- [x] Analyzed phaxio_service.py
- [x] Analyzed sinch_service.py  
- [x] Analyzed ami.py
- [x] Mapped current provider selection in main.py
- [x] Documented how files flow through system
- [x] Identified HIPAA controls

### Phase 2: Core Transport Implementation 🚧 IN PROGRESS (3/8 hours)
- [x] Implemented webhook manager
- [x] Added webhook routing to main.py
- [ ] Implemented WebSocket transport
- [ ] Added to both Python and Node
- [ ] Created tests for transports
- [ ] Updated OpenAPI spec

### Phase 3: Plugin Base Classes ⏳ NOT STARTED
- [ ] Create Python plugin base
- [ ] Create Node plugin base
- [ ] Add dependency injection
- [ ] Test lifecycle methods

[... continue for all phases ...]

## Critical Discoveries

### What's Different Than v3_plan.md
1. Webhook routing needs to be in core (DONE - added to main.py)
2. WebSocket must be in core transports (IN PROGRESS)
3. [Add findings as you discover them]

### Blockers Encountered
1. [Date] - Issue with X, resolved by Y
2. [Date] - Waiting on clarification for Z

## Next Session Tasks
1. Finish WebSocket implementation
2. Add tests for webhook manager
3. Start plugin base classes

## Confidence Check
Can I tell a developer "build your plugin"? NO - Missing:
- [ ] Working plugin discovery
- [ ] Dependency injection  
- [ ] Published SDK
- [ ] Complete docs
```

## Document 2 & 3: Living Implementation Plans

**Update Process (MANDATORY):**
```markdown
AS YOU COMPLETE EACH PHASE:
1. Update PROGRESS.md FIRST with what you did
2. Update your v3_[language]_plan.md with ACTUAL code
3. Mark each section as:
   - [VERIFIED] - Exists and works as described
   - [MODIFIED] - Exists but works differently than planned  
   - [NOT IMPLEMENTED] - Doesn't exist yet
   - [BLOCKED] - Can't implement due to dependency

CRITICAL ADDITIONS TO INCLUDE:
- Core webhook support (see webhook manager implementation above)
- Core WebSocket transport (see WebSocket implementation above)
- Automatic webhook URL registration for all plugins
- Universal webhook router in main.py

AFTER EACH UPDATE, TEST YOURSELF:
Can I confidently tell a customer:
"Yes, you can create @faxbot/sinch-sms plugin following our SDK docs and it will work"

If NO: Document what's missing in PROGRESS.md
If YES: Your plan is ready for that section
```

## How to Handle This Massive Task

### Work in 2-Hour Focused Sessions

```markdown
SESSION TEMPLATE:
1. Check PROGRESS.md for next tasks (5 min)
2. Work on specific phase (1h 45min)
3. Update all docs with findings (10 min)

EXAMPLE SESSION 1:
- Goal: Implement webhook manager
- Files touched: api/app/transports/webhooks.py, main.py
- Tests written: test_webhook_routing.py
- Docs updated: Added webhook section to v3_python_plan.md

EXAMPLE SESSION 2:  
- Goal: Implement WebSocket transport
- Files touched: api/app/transports/websocket.py
- Tests written: test_websocket.py
- Docs updated: Added WebSocket section to both plans
```

### When You Get Lost

1. Check PROGRESS.md - What phase are you in?
2. Check the checklist - What's the next unchecked item?
3. Check confidence - Can a dev build a plugin yet?
4. If stuck > 30min, document the blocker and move to next item

### Definition of Done

You're done when PROGRESS.md shows:
- All phases complete ✅
- Zero blockers
- Confidence check = YES
- A developer can build a plugin using ONLY your docs

### Living Document Structure

Your v3_[language]_plan.md should evolve like this:

```markdown
# v3 Python Plugin System - LIVING DOCUMENT

## Status: [IN PROGRESS]
Last Updated: [timestamp]
Ready for External Developers: NO

## Section 1: Plugin Discovery [NOT IMPLEMENTED]
~~Theory: Use importlib.metadata entry points~~
REALITY: [Update after Phase 1 analysis]
- Actual implementation: [code reference]
- Tested with: [test case]
- Known limitations: [list]

## Section 2: Plugin Interface [MODIFIED]
~~Theory: Plugins implement base class~~
REALITY: Found existing provider pattern in providers.py
- Actual implementation: Adapting existing BaseProvider class
- Changes needed: Add manifest() method
- Tested with: Created test plugin inheriting BaseProvider

[Continue for each section...]

## Customer Readiness Checklist
- [ ] Plugin discovery works
- [ ] Plugin lifecycle (start/stop) works  
- [ ] SDK published and documented
- [ ] Admin UI can install plugins
- [ ] Security allowlist enforced
- [ ] Sample plugin works end-to-end
- [ ] Documentation complete

CONFIDENT ANSWER TO CUSTOMER: Not yet - missing [X, Y, Z]
```

### Specific Deliverables

1. **Reality Check Document** (keep updating throughout)
   - What actually exists vs what v3 plan claims
   - Which parts of v3 plan are implementable as-is
   - Which parts need major changes

2. **Living v3_[language]_plan.md** (PRIMARY DELIVERABLE)
   - Start with the split plan from earlier
   - Update EVERY section as you verify against code
   - Replace ALL theoretical code with actual implementations
   - Mark confidence level for each component
   - Include "Customer Ready" assessment after each major section

3. **Dependency Map**
   - Draw the actual flow of data through the system
   - Identify shared components that can't be split
   - Mark critical paths for HIPAA compliance

4. **Migration Script (Actual Code)**
   - Script that reads current config/env
   - Produces new plugin configs
   - Can run in dry-run mode
   - Has rollback capability

5. **Risk Assessment**
   - What could break?
   - What's the blast radius?
   - How do we detect breakage?
   - How do we recover?

### Progress Gates

**After Each Phase, Update Your Plan and Answer:**

1. **Phase 1 Complete:** "Can I load a plugin?"
2. **Phase 2 Complete:** "Can a plugin send a fax?"
3. **Phase 3 Complete:** "Can the Admin UI configure it?"
4. **Phase 4 Complete:** "Can MCP tools use it?"
5. **Phase 5 Complete:** "Can an external developer build a plugin?"

**THE ACTUAL TEST:**
```
Developer: "I have a crazy idea involving MQTT, camera OCR, and fax subjects"
Your Documentation Must Say: "Here's our interface. Here's what you get access to. 
Here's how to publish your plugin. We don't care what you build with it."

The plugin system is ready when developers can build ANYTHING without asking permission
or fitting into our preconceived scenarios.
```

**Critical Addition to Your Plan:**

## Open Plugin Architecture Documentation

Your v3_[language]_plan.md must include COMPLETE interface documentation:

```markdown
# Plugin Developer Reference

## What You Get Access To
- Raw fax data (binary)
- Metadata (to, from, timestamp, etc.)
- Storage interface
- Logger interface  
- Database handle (if needed)
- Event bus (for pub/sub)

## What You Must Implement
interface OutboundPlugin {
  manifest(): object
  validateConfig(config): void
  start(config, deps): void
  stop(): void
  send(to, file): SendResult
  getStatus(id): StatusResult
}

## What You Can Do
- Whatever you want with the data
- Integrate any API
- Transform data however you like
- Emit events
- Subscribe to events
- Add new capabilities we never thought of

## What You Can't Do
- Block the main thread
- Access other plugins' private data
- Bypass audit logs (if HIPAA mode)

## How Data Flows
Request → API → Plugin → [Your Code Here] → Response

## Publishing
1. Implement interface
2. Add manifest
3. Publish to npm/PyPI
4. Done

We don't care if you're sending faxes to space or converting them to interpretive dance.
If it implements the interface, it's a valid plugin.
```

**Stop trying to predict what developers will build. Just give them:**
1. Clean interfaces
2. Clear data flow documentation
3. Access to the primitives they need
4. A way to publish

**Remove from your documentation:**
- "Use cases"
- "Intended for"
- "Best suited for"
- Any limiting language

**Add to your documentation:**
- Raw interface specs
- Every available hook/event
- Every piece of data they can access
- Memory/CPU limits (if any)
- "Build whatever you want"

## MANDATORY: Create Complete Developer Documentation

### THIS MUST BE CREATED AS PART OF THE IMPLEMENTATION

Create `docs/PLUGIN_DEVELOPER_REFERENCE.md` with the following COMPLETE specification:

```markdown
# Faxbot Plugin Developer Reference

## When The Plugin System Isn't Enough

### Reality Check
The plugin system provides basic interfaces. Your provider probably needs more:
- Custom authentication flows (OAuth2, JWT, HMAC)
- Specific webhook validation 
- Rate limiting strategies
- Retry logic specific to your provider
- Custom event types we haven't thought of

You have two paths:

## Core Transport Layer (MUST IMPLEMENT)

### Webhook Support (Built into Core)

**THIS MUST BE IMPLEMENTED IN CORE FOR ALL PLUGINS**

```python
# api/app/transports/webhooks.py (CORE IMPLEMENTATION)
class WebhookManager:
    """Core webhook manager - handles ALL webhook traffic"""
    
    def __init__(self):
        self.routes = {}  # Dynamic routes for each plugin
        self.validators = {}  # Validation functions per plugin
        
    def register_webhook(self, plugin_id: str, path: str = None) -> str:
        """
        Register a webhook endpoint for a plugin
        Returns the full webhook URL
        
        Example: 
        - plugin_id="faxbot-plugin-twilio"
        - Returns: "https://api.faxbot.dev/webhooks/faxbot-plugin-twilio"
        """
        if not path:
            path = f"/webhooks/{plugin_id}"
        
        # Create dynamic route that forwards to plugin
        self.routes[path] = plugin_id
        
        return f"{BASE_URL}{path}"
    
    async def handle_webhook(self, path: str, headers: Dict, body: bytes) -> Dict:
        """
        Central webhook handler - routes to appropriate plugin
        ALL webhooks flow through here
        """
        plugin_id = self.routes.get(path)
        if not plugin_id:
            raise ValueError(f"No plugin registered for {path}")
        
        # Get plugin instance
        plugin = self.get_plugin(plugin_id)
        
        # Plugin's webhook handler is called with raw data
        # Plugin decides how to validate/process
        return await plugin.handle_webhook(headers, body)
    
    def set_validator(self, plugin_id: str, validator: Callable):
        """
        Optional: Set a validation function for security
        """
        self.validators[plugin_id] = validator
```

```python
# EVERY plugin gets webhook support automatically
class Plugin:
    def __init__(self):
        # Auto-registered webhook endpoint
        self.webhook_url = None
        
    def start(self, config, deps):
        # Webhook URL automatically created
        self.webhook_url = deps.webhooks.register_webhook(self.manifest()['id'])
        # Plugin gets: https://api.faxbot.dev/webhooks/your-plugin-id
        
    async def handle_webhook(self, headers: Dict, body: bytes) -> Dict:
        """
        Override this to handle webhooks
        Gets raw headers and body - you handle validation
        """
        return {"status": "received"}
```

### WebSocket Support (Built into Core)

**WEBSOCKET IS ALSO IN CORE FOR ALL PLUGINS**

```python
# api/app/transports/websocket.py (CORE IMPLEMENTATION)
import asyncio
import websockets
import json
from typing import Callable, Dict, Any

class WebSocketTransport:
    """Core WebSocket transport - available to all plugins"""
    def __init__(self):
        self.connections = {}
        self.handlers = {}
        
    async def connect(self, url: str, auth: Dict[str, Any] = None) -> str:
        """Connect to WebSocket endpoint"""
        # Returns connection_id
        pass
        
    async def disconnect(self, connection_id: str):
        """Close WebSocket connection"""
        pass
        
    async def send(self, connection_id: str, message: Dict[str, Any]):
        """Send message over WebSocket"""
        pass
        
    async def subscribe(self, connection_id: str, event: str, handler: Callable):
        """Subscribe to WebSocket events"""
        pass
        
    async def unsubscribe(self, connection_id: str, event: str):
        """Unsubscribe from events"""
        pass
```

```javascript
// node_mcp/src/transports/websocket.js (CORE IMPLEMENTATION)
const WebSocket = require('ws');

class WebSocketTransport {
    constructor() {
        this.connections = new Map();
        this.handlers = new Map();
    }
    
    async connect(url, auth = null) {
        // Returns connection_id
    }
    
    async disconnect(connectionId) {
        // Close connection
    }
    
    async send(connectionId, message) {
        // Send message
    }
    
    async subscribe(connectionId, event, handler) {
        // Subscribe to events
    }
}

module.exports = WebSocketTransport;
```

### Complete Example: Plugin Using Core Webhook + WebSocket

```python
class MyProviderPlugin(OutboundPlugin):
    def start(self, config, deps):
        # Webhook URL is automatically available
        print(f"Webhook URL: {deps.webhooks.register_webhook(self.manifest()['id'])}")
        # Gives you: https://api.faxbot.dev/webhooks/my-provider-plugin
        
        # Connect WebSocket for real-time events
        self.ws_id = await deps.transports.websocket.connect(
            "wss://api.provider.com/events",
            auth={"token": config["api_key"]}
        )
        
        # Subscribe to WebSocket events
        await deps.transports.websocket.subscribe(
            self.ws_id, 
            "status_update",
            self.handle_status_update
        )
    
    async def handle_webhook(self, headers, body):
        """
        Called automatically when webhook received
        Core handles routing - you just process
        """
        # Validate signature (provider-specific)
        if not self.validate_signature(headers, body):
            return {"error": "invalid signature"}, 401
            
        data = json.loads(body)
        
        # Process webhook
        if data["event"] == "fax.completed":
            self.deps.events.emit("fax.completed", data)
            
        return {"status": "processed"}
    
    async def handle_status_update(self, message):
        """WebSocket message handler"""
        self.deps.events.emit("realtime.status", message)
```

### Core Transports Available to All Plugins

```python
# Every plugin gets these via deps
deps.transports.http      # Standard HTTP/REST
deps.transports.websocket  # WebSocket connections
deps.transports.sse        # Server-Sent Events
deps.webhooks              # Webhook registration and handling
deps.events               # Event bus for pub/sub
```

### How Webhook Routing Works (Core Implementation)

```python
# api/app/main.py - Core webhook router
@app.post("/webhooks/{plugin_id}")
async def webhook_handler(
    plugin_id: str,
    request: Request,
    body: bytes = Body()
):
    """
    Universal webhook endpoint
    ALL webhooks go through here
    Core routes to correct plugin
    """
    headers = dict(request.headers)
    
    # Core webhook manager handles routing
    result = await webhook_manager.handle_webhook(
        f"/webhooks/{plugin_id}",
        headers,
        body
    )
    
    return result
```

This means:
- Twilio plugin gets: `/webhooks/faxbot-plugin-twilio`
- RingCentral gets: `/webhooks/faxbot-plugin-ringcentral`  
- Your custom plugin gets: `/webhooks/your-plugin-id`
- Future plugins that don't exist yet will work automatically

No plugin needs to implement webhook routing. Core handles it.

## Path 1: Request Core Support (Contribute Back)

File an issue at `github.com/dmontgomery40/faxbot/issues` with this template:

```markdown
### Provider Integration Request

**Provider:** [e.g., RingCentral]
**API Docs:** [link to their API documentation]

**Authentication Needed:**
- [ ] Basic Auth (in core)
- [ ] Bearer Token (in core)
- [ ] OAuth2 Client Credentials (in core)
- [ ] OAuth2 PKCE (needs implementation)
- [ ] HMAC webhook validation (you handle in plugin)
- [ ] Custom: [describe]

**Transport Requirements:**
- [ ] REST API (already in core)
- [ ] Webhooks (already in core)
- [ ] WebSocket (already in core)
- [ ] SSE (already in core)
- [ ] GraphQL (needs implementation)
- [ ] gRPC (optional in core)
- [ ] Custom: [describe]

**What's Already Handled by Core:**
- Webhook endpoint registration
- Webhook routing to your plugin
- WebSocket connection management
- HTTP client with retry logic
- Event bus for pub/sub

**What You Handle in Your Plugin:**
- Provider-specific webhook validation
- Provider-specific auth flows
- Provider-specific retry logic
- Business logic

**Events I Need:**
```
# Most events can be emitted by your plugin
# Only request core events if they need special handling
```

```python
# api/app/transports/websocket.py (CORE IMPLEMENTATION)
import asyncio
import websockets
import json
from typing import Callable, Dict, Any

class WebSocketTransport:
    """Core WebSocket transport - available to all plugins"""
    def __init__(self):
        self.connections = {}
        self.handlers = {}
        
    async def connect(self, url: str, auth: Dict[str, Any] = None) -> str:
        """Connect to WebSocket endpoint"""
        # Returns connection_id
        pass
        
    async def disconnect(self, connection_id: str):
        """Close WebSocket connection"""
        pass
        
    async def send(self, connection_id: str, message: Dict[str, Any]):
        """Send message over WebSocket"""
        pass
        
    async def subscribe(self, connection_id: str, event: str, handler: Callable):
        """Subscribe to WebSocket events"""
        pass
        
    async def unsubscribe(self, connection_id: str, event: str):
        """Unsubscribe from events"""
        pass
```

```javascript
// node_mcp/src/transports/websocket.js (CORE IMPLEMENTATION)
const WebSocket = require('ws');

class WebSocketTransport {
    constructor() {
        this.connections = new Map();
        this.handlers = new Map();
    }
    
    async connect(url, auth = null) {
        // Returns connection_id
    }
    
    async disconnect(connectionId) {
        // Close connection
    }
    
    async send(connectionId, message) {
        // Send message
    }
    
    async subscribe(connectionId, event, handler) {
        // Subscribe to events
    }
}

module.exports = WebSocketTransport;
```

### Core Transports Available to All Plugins

```python
# Every plugin gets these via deps.transports
deps.transports.http      # Standard HTTP/REST
deps.transports.websocket  # WebSocket connections
deps.transports.sse        # Server-Sent Events
deps.transports.grpc       # gRPC (if enabled)
```

## Path 1: Request Core Support (Contribute Back)

File an issue at `github.com/dmontgomery40/faxbot/issues` with this template:

```markdown
### Provider Integration Request

**Provider:** [e.g., RingCentral]
**API Docs:** [link to their API documentation]

**Authentication Needed:**
- [ ] Basic Auth
- [ ] Bearer Token
- [ ] OAuth2 (specify flow)
- [ ] HMAC signature validation
- [ ] Custom: [describe]

**Transport Requirements:**
- [ ] REST API (already in core)
- [ ] WebSocket (already in core)
- [ ] SSE (already in core)
- [ ] GraphQL (needs implementation)
- [ ] gRPC (optional in core)
- [ ] Custom: [describe]

**Events I Need:**
```
# Example: RingCentral needs these events
webhook.ringcentral.subscription_renewed
webhook.ringcentral.presence_updated
auth.oauth2.token_refreshed
transport.websocket.connected
transport.websocket.message_received
```

**Why Current Events Don't Work:**
[Explain why generic events aren't sufficient]

**I'm Willing To:**
- [ ] Implement and PR this myself with guidance
- [ ] Test beta implementation
- [ ] Maintain the provider plugin
```

We'll either:
1. Add the events to core (most common transports are already there)
2. Guide you on implementation
3. Explain why it should be a fork

## Path 2: Fork and Extend (Full Control)

### What's Already in Core

**Transports (no fork needed):**
- HTTP/REST ✓
- WebSocket ✓
- Server-Sent Events ✓
- gRPC (optional, enabled by flag)

**Auth Methods (in core):**
- Basic Auth ✓
- API Key ✓
- Bearer Token ✓
- OAuth2 Client Credentials ✓

**Still Need a Fork For:**
- GraphQL transport
- OAuth2 PKCE flow
- SAML
- Custom binary protocols
- Provider-specific webhook validation (beyond HMAC)

**Events I Need:**
```
# Example: RingCentral needs these events
webhook.ringcentral.subscription_renewed
webhook.ringcentral.presence_updated
auth.oauth2.token_refreshed
transport.websocket.connected
transport.websocket.message_received
```

**Why Current Events Don't Work:**
[Explain why generic events aren't sufficient]

**I'm Willing To:**
- [ ] Implement and PR this myself with guidance
- [ ] Test beta implementation
- [ ] Maintain the provider plugin
```

We'll either:
1. Add the events/transport to core
2. Guide you on implementation
3. Explain why it should be a fork

## Path 2: Fork and Extend (Full Control)

### Forking Strategy

```bash
# Fork the repo
git clone https://github.com/yourusername/faxbot-fork
cd faxbot-fork

# Create your provider branch
git checkout -b provider/ringcentral

# Key files you'll modify:
```

### Core Files to Extend

**1. Add Your Transport (if needed)**
```python
# api/app/transports/websocket.py (NEW FILE)
import asyncio
import websockets

class WebSocketTransport:
    """Your custom transport implementation"""
    def __init__(self, url, auth_handler):
        self.url = url
        self.auth_handler = auth_handler
        
    async def connect(self):
        # Your connection logic
        pass
        
    async def subscribe(self, events):
        # Your subscription logic
        pass
```

**2. Add Your Auth Method**
```python
# api/app/auth/oauth2_pkce.py (NEW FILE)
class OAuth2PKCEAuth:
    """OAuth2 with PKCE flow for your provider"""
    def __init__(self, client_id, redirect_uri):
        self.client_id = client_id
        self.redirect_uri = redirect_uri
        
    def get_auth_url(self, state, code_challenge):
        # Build authorization URL
        pass
        
    def exchange_code(self, code, code_verifier):
        # Exchange for tokens
        pass
```

**3. Register Your Events**
```python
# api/app/events/registry.py (MODIFY)
PROVIDER_EVENTS = {
    'ringcentral': [
        'ringcentral.webhook.validated',
        'ringcentral.oauth.refreshed',
        'ringcentral.subscription.created',
        'ringcentral.presence.updated',
        # Add ALL events your provider needs
    ]
}

# Register them on startup
def register_provider_events(provider_name):
    for event in PROVIDER_EVENTS.get(provider_name, []):
        event_bus.register(event)
```

**4. Add Provider-Specific Plugin Base**
```python
# api/app/plugins/providers/ringcentral_base.py (NEW)
from ..base import OutboundPlugin

class RingCentralPlugin(OutboundPlugin):
    """Base class for RingCentral plugins with all custom logic"""
    
    def __init__(self):
        super().__init__()
        self.websocket = None
        self.oauth_client = None
        
    async def setup_webhook_subscription(self):
        """RingCentral-specific webhook setup"""
        # Their specific subscription flow
        pass
        
    async def validate_webhook(self, headers, body):
        """RingCentral-specific webhook validation"""
        # Their specific validation
        pass
        
    async def refresh_token(self):
        """RingCentral-specific token refresh"""
        # Their specific OAuth2 refresh
        pass
```

### Fork Maintenance Strategy

```markdown
# .github/FORK_SYNC.md

## Keeping Your Fork Updated

This fork adds [RingCentral/YourProvider] support.

### Modified Core Files:
- api/app/events/registry.py (added events)
- api/app/main.py (added provider route)

### New Files (safe from upstream conflicts):
- api/app/transports/websocket.py
- api/app/auth/oauth2_pkce.py  
- api/app/plugins/providers/ringcentral_base.py

### Sync Strategy:
```bash
# Add upstream
git remote add upstream https://github.com/dmontgomery40/faxbot

# Fetch upstream changes
git fetch upstream

# Merge carefully
git checkout main
git merge upstream/main --no-commit

# Check for conflicts in modified files
git status

# Your custom files are safe, only check core modifications
# Resolve conflicts, keeping your provider additions
git commit -m "Sync with upstream, maintain RingCentral support"
```

### Publishing Your Fork

```markdown
# README.md addition

## RingCentral Fork

This fork adds native RingCentral support with:
- WebSocket transport for real-time events
- OAuth2 PKCE authentication
- Presence monitoring
- Custom webhook validation

### Additional Events:
- `ringcentral.webhook.validated`
- `ringcentral.oauth.refreshed`
- `ringcentral.subscription.created`

### Installation:
```bash
git clone https://github.com/youruser/faxbot-ringcentral
# Follow standard installation
# Set FAXBOT_PROVIDER=ringcentral
```

### Why Fork Instead of Plugin:
- Requires WebSocket transport (not in core)
- Requires OAuth2 PKCE (not in core)  
- Requires 15+ custom events (not in core)
```

## Complete Event System Documentation

### Core Events (Always Available)

```python
# Transport Layer
transport.http.request_started
transport.http.request_completed  
transport.http.request_failed
transport.http.retry_attempted

# Storage Layer  
storage.file_uploaded
storage.file_downloaded
storage.file_deleted
storage.url_generated

# Plugin Lifecycle
plugin.loaded
plugin.started
plugin.stopped
plugin.error
plugin.config_changed

# Job Management
job.created
job.queued
job.processing
job.completed
job.failed
job.cancelled
```

### How to Request New Events

If you need events like:
- `twilio.signature.validated`
- `messagebird.balance.low`
- `custom.provider.specific_thing`

1. Check if a generic event works first
2. If not, request via issue template above
3. If declined, fork and add them yourself

### Creating Custom Events in Forks

```python
# In your fork's api/app/events/custom.py
from .base import Event, EventBus

class CustomEvent(Event):
    """Define your custom event structure"""
    def __init__(self, name, payload_schema):
        self.name = name
        self.payload_schema = payload_schema
        
# Register in your provider
event_bus.register_event(CustomEvent(
    'my_provider.custom_event',
    schema={
        'type': 'object',
        'properties': {
            'provider_specific_field': {'type': 'string'}
        }
    }
))

# Emit from your plugin
self.deps.events.emit('my_provider.custom_event', {
    'provider_specific_field': 'value'
})
```

## Why Some Things Can't Be Plugins

Be realistic about what needs core changes:

**Can be a plugin:**
- Uses HTTP/REST
- Uses basic auth or API keys
- Needs 0-5 custom events
- Standard request/response flow

**Needs core support or fork:**
- WebSocket/gRPC/GraphQL transport
- OAuth2/SAML/custom auth flows  
- 10+ custom events
- Modifies request pipeline
- Needs custom middleware
- Requires background workers
- Has complex retry/circuit breaker needs

## Getting Help

- **Discord:** [invite link] #plugin-development
- **GitHub Discussions:** For architecture questions
- **Issues:** For feature requests with the template above
- **Fork Network:** See what others have built

Remember: If the plugin system doesn't support your use case, that's not a failure. Fork it and make it work for you. That's why it's open source.
```

### THIS DOCUMENTATION MUST BE:
1. Created as `docs/PLUGIN_DEVELOPER_REFERENCE.md`
2. Include CLEAR paths for contribution vs forking
3. Acknowledge the plugin system's limitations
4. Show EXACTLY which files to modify for common needs
5. Include fork maintenance strategies

```markdown
# Faxbot Plugin Developer Reference

## Core Plugin Interface (Python)

```python
from typing import Dict, Any, Optional, List
from dataclasses import dataclass
from abc import ABC, abstractmethod

@dataclass
class SendResult:
    """Result of a send operation"""
    job_id: str          # Unique identifier for this job
    backend: str         # Name of backend that processed it
    provider_sid: Optional[str]  # Provider's ID if available
    
@dataclass
class StatusResult:
    """Status of a fax transmission"""
    job_id: str
    status: str  # Must be: queued|in_progress|SUCCESS|FAILED
    pages: Optional[int]
    error: Optional[str]
    raw_response: Optional[Dict[str, Any]]  # Provider's raw response

@dataclass
class PluginDeps:
    """Dependencies injected into every plugin"""
    logger: Logger           # Standard Python logger
    storage: StorageInterface  # File storage access
    db: DatabaseHandle      # Database connection
    events: EventBus        # Pub/sub event system
    http: HttpClient        # Pre-configured HTTP client
    config_dir: str         # Plugin's private config directory
    cache: CacheInterface   # Key-value cache
    metrics: MetricsCollector  # Metrics/telemetry

class Plugin(ABC):
    """Base interface ALL plugins must implement"""
    
    @abstractmethod
    def manifest(self) -> Dict[str, Any]:
        """Return plugin manifest"""
        pass
    
    @abstractmethod
    def validate_config(self, config: Dict[str, Any]) -> None:
        """Validate configuration, raise ValueError if invalid"""
        pass
    
    @abstractmethod
    def start(self, config: Dict[str, Any], deps: PluginDeps) -> None:
        """Initialize plugin. Called once on load."""
        pass
    
    @abstractmethod
    def stop(self) -> None:
        """Cleanup. Called on shutdown or reload."""
        pass
```

## Core Plugin Interface (Node)

```typescript
interface SendResult {
  job_id: string;
  backend: string;
  provider_sid?: string;
}

interface StatusResult {
  job_id: string;
  status: 'queued' | 'in_progress' | 'SUCCESS' | 'FAILED';
  pages?: number;
  error?: string;
  raw_response?: any;  // Provider's raw response
}

interface PluginDeps {
  logger: Logger;
  storage: StorageInterface;
  db: DatabaseHandle;
  events: EventBus;
  http: HttpClient;
  configDir: string;
  cache: CacheInterface;
  metrics: MetricsCollector;
}

abstract class Plugin {
  abstract manifest(): PluginManifest;
  abstract validateConfig(config: any): void;
  abstract start(config: any, deps: PluginDeps): Promise<void>;
  abstract stop(): Promise<void>;
}
```

## Available Events

Plugins can subscribe to and emit these events:

```python
# Subscribe
deps.events.on('fax.received', lambda data: handle_inbound(data))
deps.events.on('config.changed', lambda data: reload_config(data))
deps.events.on('health.check', lambda: report_health())

# Emit
deps.events.emit('fax.sent', {'job_id': '123', 'to': '+1234567890'})
deps.events.emit('fax.failed', {'job_id': '123', 'error': 'No answer'})
deps.events.emit('custom.event', any_json_serializable_data)
```

Event Catalog:
- `fax.sent` - Emitted after successful send
- `fax.failed` - Emitted on send failure
- `fax.received` - Emitted when inbound fax arrives
- `fax.status_changed` - Status update
- `config.changed` - Configuration updated
- `health.check` - Health check requested
- `plugin.started` - Plugin initialized
- `plugin.stopped` - Plugin shutdown
- `storage.file_created` - File stored
- `storage.file_deleted` - File removed
- Your custom events - Emit whatever you want

## Storage Interface

```python
# Store a file
url = await deps.storage.put('path/to/file.pdf', file_bytes)

# Get a file
file_bytes = await deps.storage.get('path/to/file.pdf')

# Get temporary signed URL (for cloud providers)
signed_url = await deps.storage.get_signed_url('path/to/file.pdf', expires_in=3600)

# Delete a file
await deps.storage.delete('path/to/file.pdf')

# List files
files = await deps.storage.list('path/prefix/')
```

## Database Access

```python
# Direct SQL (be careful with injection)
result = await deps.db.query('SELECT * FROM faxes WHERE job_id = ?', [job_id])

# Transaction support
async with deps.db.transaction() as tx:
    await tx.execute('INSERT INTO faxes ...')
    await tx.execute('UPDATE stats ...')
    # Auto-commit on success, rollback on exception
```

## HTTP Client

```python
# Pre-configured with retry logic and timeouts
response = await deps.http.post('https://api.provider.com/send', 
    json={'to': number},
    headers={'Authorization': f'Bearer {token}'}
)

# Automatic retry on 5xx errors
# Automatic timeout after 30s
# Automatic logging of requests/responses
```

## Cache Interface

```python
# Set with TTL
await deps.cache.set('key', value, ttl=3600)

# Get
value = await deps.cache.get('key')

# Delete
await deps.cache.delete('key')

# Atomic increment
count = await deps.cache.incr('counter')
```

## Metrics Collection

```python
# Count events
deps.metrics.increment('fax.sent')
deps.metrics.increment('fax.failed', tags={'reason': 'busy'})

# Record values
deps.metrics.gauge('queue.size', 42)
deps.metrics.histogram('processing.time', 1.23)

# Time operations
with deps.metrics.timer('api.call'):
    response = await call_api()
```

## Plugin Categories and Required Methods

### Outbound Plugins
```python
class OutboundPlugin(Plugin):
    @abstractmethod
    async def send(self, to_number: str, file_path: str, options: Dict = {}) -> SendResult:
        """Send a fax"""
        pass
    
    @abstractmethod
    async def get_status(self, job_id: str) -> StatusResult:
        """Get transmission status"""
        pass
    
    # Optional
    async def cancel(self, job_id: str) -> bool:
        """Cancel a queued fax"""
        return False
```

### Inbound Plugins
```python
class InboundPlugin(Plugin):
    @abstractmethod
    async def list_inbound(self, limit: int = 10, offset: int = 0) -> List[InboundFax]:
        """List received faxes"""
        pass
    
    @abstractmethod
    async def get_inbound_pdf(self, inbound_id: str) -> bytes:
        """Get PDF content of received fax"""
        pass
    
    @abstractmethod
    async def handle_webhook(self, payload: Dict) -> None:
        """Process provider webhook"""
        pass
```

### Storage Plugins
```python
class StoragePlugin(Plugin):
    @abstractmethod
    async def put(self, path: str, data: bytes, metadata: Dict = {}) -> str:
        """Store data, return identifier"""
        pass
    
    @abstractmethod
    async def get(self, path: str) -> bytes:
        """Retrieve data"""
        pass
    
    @abstractmethod
    async def delete(self, path: str) -> bool:
        """Delete data"""
        pass
    
    @abstractmethod
    async def get_signed_url(self, path: str, expires_in: int = 3600) -> str:
        """Get temporary access URL"""
        pass
```

## Manifest Structure (COMPLETE)

```json
{
  "id": "unique-plugin-id",
  "name": "Human Readable Name",
  "version": "1.0.0",
  "description": "What this plugin does",
  "author": "Your Name",
  "homepage": "https://github.com/you/plugin",
  "license": "Apache-2.0",
  "platform": "python|node",
  "categories": ["outbound"],  // outbound|inbound|storage|auth|custom
  "capabilities": ["send", "get_status"],  // What methods you implement
  
  "config_schema": {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "type": "object",
    "properties": {
      "api_key": {
        "type": "string",
        "description": "API key for provider"
      },
      "endpoint": {
        "type": "string",
        "format": "uri",
        "default": "https://api.provider.com"
      }
    },
    "required": ["api_key"]
  },
  
  "defaults": {
    "endpoint": "https://api.provider.com"
  },
  
  "requirements": {
    "memory": "256MB",  // Optional resource hints
    "cpu": "0.5",
    "network": ["https://api.provider.com"]  // Firewall rules
  },
  
  "ui": {
    "icon": "https://example.com/icon.png",
    "color": "#FF5733",
    "configFields": {
      "api_key": {
        "ui:widget": "password",
        "ui:help": "Get from provider dashboard"
      }
    }
  }
}
```

## Publishing Your Plugin

### Python
```bash
# Structure
faxbot-plugin-yourname/
├── pyproject.toml
├── README.md
├── LICENSE
├── src/
│   └── your_plugin/
│       ├── __init__.py
│       └── plugin.py

# pyproject.toml
[project]
name = "faxbot-plugin-yourname"
version = "1.0.0"

[project.entry-points."faxbot.plugins"]
yourname = "your_plugin:YourPlugin"

# Publish
python -m build
python -m twine upload dist/*
```

### Node
```bash
# Structure
faxbot-plugin-yourname/
├── package.json
├── README.md
├── LICENSE
├── index.js
└── lib/

# package.json
{
  "name": "faxbot-plugin-yourname",
  "version": "1.0.0",
  "keywords": ["faxbot-plugin"],
  "main": "index.js",
  "faxbot": {
    "manifest": { ... }
  }
}

# Publish
npm publish
```

## Complete Working Example

```python
# Real plugin that actually works
import httpx
from typing import Dict, Any
from faxbot.plugin import OutboundPlugin, SendResult, StatusResult, PluginDeps

class TwilioFaxPlugin(OutboundPlugin):
    """Send faxes via Twilio API"""
    
    def manifest(self) -> Dict[str, Any]:
        return {
            "id": "faxbot-plugin-twilio",
            "name": "Twilio Fax",
            "version": "1.0.0",
            "platform": "python",
            "categories": ["outbound"],
            "capabilities": ["send", "get_status"],
            "config_schema": {
                "type": "object",
                "properties": {
                    "account_sid": {"type": "string"},
                    "auth_token": {"type": "string"},
                    "from_number": {"type": "string"}
                },
                "required": ["account_sid", "auth_token", "from_number"]
            }
        }
    
    def validate_config(self, config: Dict[str, Any]) -> None:
        if not config.get('account_sid'):
            raise ValueError("account_sid required")
        if not config.get('auth_token'):
            raise ValueError("auth_token required")
    
    def start(self, config: Dict[str, Any], deps: PluginDeps) -> None:
        self.config = config
        self.deps = deps
        self.client = httpx.Client(
            base_url=f"https://api.twilio.com/2010-04-01/Accounts/{config['account_sid']}",
            auth=(config['account_sid'], config['auth_token'])
        )
        deps.logger.info(f"Twilio plugin started")
    
    def stop(self) -> None:
        if self.client:
            self.client.close()
    
    async def send(self, to_number: str, file_path: str, options: Dict = {}) -> SendResult:
        # Get signed URL for the file
        file_url = await self.deps.storage.get_signed_url(file_path)
        
        # Send via Twilio
        response = self.client.post('/Faxes.json', data={
            'To': to_number,
            'From': self.config['from_number'],
            'MediaUrl': file_url
        })
        
        data = response.json()
        
        # Emit event
        self.deps.events.emit('fax.sent', {
            'job_id': data['sid'],
            'to': to_number
        })
        
        # Track metric
        self.deps.metrics.increment('twilio.fax.sent')
        
        return SendResult(
            job_id=data['sid'],
            backend='twilio',
            provider_sid=data['sid']
        )
    
    async def get_status(self, job_id: str) -> StatusResult:
        response = self.client.get(f'/Faxes/{job_id}.json')
        data = response.json()
        
        status_map = {
            'queued': 'queued',
            'processing': 'in_progress',
            'delivered': 'SUCCESS',
            'failed': 'FAILED'
        }
        
        return StatusResult(
            job_id=job_id,
            status=status_map.get(data['status'], 'queued'),
            pages=data.get('num_pages'),
            error=data.get('error_message'),
            raw_response=data
        )
```

## Testing Your Plugin Locally

```bash
# Install in development mode
pip install -e ./your-plugin

# Test with Faxbot
export FAXBOT_PLUGIN_PATH=/path/to/your-plugin
faxbot test-plugin your-plugin-id

# Watch logs
tail -f /var/log/faxbot/plugins.log
```

## What You Can Build

- Traditional fax providers (Twilio, RingCentral, InterFAX)
- SMS/MMS bridges 
- Email-to-fax gateways
- OCR processors
- Translation services
- Webhook forwarders
- MQTT publishers
- Database loggers
- Analytics collectors
- Archive systems
- Compliance validators
- ML processors
- Literally anything that can process documents

The system doesn't care what you build. If it implements the interface, it's valid.
```

### THIS DOCUMENTATION MUST BE:
1. Created as `docs/PLUGIN_DEVELOPER_REFERENCE.md`
2. Linked from the main README
3. Published to the docs site
4. Updated whenever interfaces change
5. Tested with actual working examples

## CRITICAL: Non-Fax Plugin Support (MUST ADD TO DOCS)

### The SMS/MMS Problem

The current plugin system assumes everything is a fax. SMS plugins need:

```python
# MUST ADD TO PLUGIN INTERFACES
class MessagingPlugin(Plugin):
    """Base for non-fax messaging (SMS, MMS, WhatsApp, etc)"""
    
    async def send_message(self, to: str, content: Dict) -> MessageResult:
        """
        Send any message type
        content = {
            'text': 'message body',
            'media': ['url1', 'url2'],  # for MMS
            'type': 'sms|mms|whatsapp|telegram'
        }
        """
        pass
    
    async def get_message_status(self, message_id: str) -> MessageStatus:
        pass
    
    async def list_conversations(self) -> List[Conversation]:
        """SMS has conversations, not just one-way faxes"""
        pass
    
    async def send_reply(self, conversation_id: str, content: Dict):
        """Reply in a thread"""
        pass

@dataclass
class MessageResult:
    message_id: str
    provider: str
    type: str  # sms|mms|whatsapp
    segments: int  # SMS segments
    cost: Optional[float]
    
@dataclass  
class MessageStatus:
    message_id: str
    status: str  # sent|delivered|read|failed
    delivered_at: Optional[datetime]
    read_at: Optional[datetime]  # SMS read receipts
```

### MCP Tool Mapping for Non-Fax Plugins

```python
# docs/mcp_tools.schema.json MUST INCLUDE:
{
  "send_message": {
    "description": "Send SMS/MMS/messaging",
    "parameters": {
      "to": "string",
      "text": "string",
      "media": "array of URLs (optional)"
    }
  },
  "get_conversations": {
    "description": "List SMS conversations",
    "parameters": {
      "limit": "number"
    }
  },
  "reply_to_conversation": {
    "description": "Reply in SMS thread",
    "parameters": {
      "conversation_id": "string",
      "text": "string"
    }
  }
}
```

### Extended Plugin Categories

```python
# Current docs only have: outbound|inbound|storage|auth
# MUST ADD:
PLUGIN_CATEGORIES = [
    'outbound',      # Fax sending
    'inbound',       # Fax receiving
    'messaging',     # SMS/MMS/WhatsApp (NEW)
    'voice',         # Voice calls (NEW)
    'email',         # Email gateway (NEW)
    'storage',       # File storage
    'auth',          # Authentication
    'transform',     # OCR, translation (NEW)
    'analytics',     # Metrics, reporting (NEW)
    'custom'         # Anything else
]
```

### Admin UI Must Support Dynamic Capabilities

```typescript
// The UI can't hardcode fax assumptions
// Must dynamically render based on capabilities

function PluginCard({ plugin }) {
  // Don't assume send/get_status
  // Render whatever capabilities the plugin declares
  
  return (
    <div>
      <h3>{plugin.manifest.name}</h3>
      <p>Capabilities:</p>
      <ul>
        {plugin.manifest.capabilities.map(cap => (
          <li key={cap}>
            {cap} {/* Could be send_sms, transcribe_audio, etc */}
          </li>
        ))}
      </ul>
    </div>
  );
}
```

### Complete SMS Plugin Example (What Client Needs)

```python
# THIS EXACT EXAMPLE MUST BE IN DOCS
from faxbot.plugin import MessagingPlugin, MessageResult, MessageStatus

class SinchSMSPlugin(MessagingPlugin):
    def manifest(self):
        return {
            "id": "faxbot-sinch-sms",
            "name": "Sinch SMS Gateway",
            "categories": ["messaging"],  # NOT outbound
            "capabilities": [
                "send_message",
                "get_message_status", 
                "list_conversations",
                "send_reply",
                "receive_webhook"
            ],
            "config_schema": {
                "type": "object",
                "properties": {
                    "service_plan_id": {"type": "string"},
                    "api_token": {"type": "string"},
                    "from_number": {"type": "string"},
                    "enable_delivery_reports": {"type": "boolean", "default": true}
                }
            }
        }
    
    async def send_message(self, to: str, content: Dict) -> MessageResult:
        response = await self.deps.http.post(
            f"https://sms.api.sinch.com/xms/v1/{self.config['service_plan_id']}/batches",
            headers={"Authorization": f"Bearer {self.config['api_token']}"},
            json={
                "from": self.config["from_number"],
                "to": [to],
                "body": content["text"],
                "delivery_report": "full" if self.config["enable_delivery_reports"] else "none"
            }
        )
        
        return MessageResult(
            message_id=response["id"],
            provider="sinch",
            type="sms",
            segments=response.get("number_of_parts", 1),
            cost=response.get("cost")
        )
    
    async def handle_webhook(self, headers, body):
        """Sinch delivery reports and inbound SMS"""
        data = json.loads(body)
        
        if data["type"] == "delivery_report_sms":
            self.deps.events.emit("sms.delivered", {
                "message_id": data["batch_id"],
                "status": data["status"],
                "code": data["code"]
            })
        elif data["type"] == "mo_text":
            self.deps.events.emit("sms.received", {
                "from": data["from"],
                "text": data["body"],
                "received_at": data["received_at"]
            })
        
        return {"status": "ok"}
```

### MCP Tools for SMS (Auto-Generated)

When SinchSMSPlugin is active, these MCP tools appear:
- `send_message` - Send SMS/MMS
- `get_message_status` - Check delivery
- `list_conversations` - Show threads
- `reply_to_conversation` - Reply in thread

The AI assistant can then:
"Send an SMS to +1234567890 saying their appointment is confirmed"
"Show me recent SMS conversations"
"Reply to the last message from John"

Before proposing ANY change, ask yourself:
1. Will this break existing Phaxio users?
2. Will this break existing Sinch users?
3. Will this break existing SIP users?
4. Will this break MCP integrations?
5. Will this expose PHI?
6. Can we roll back if it fails?
7. Have I verified this against the ACTUAL code, not the plan?
8. **Can a developer build a plugin using ONLY this documentation?**

Remember: The v3_plan.md is aspirational. The codebase is reality. When they conflict, reality wins.
