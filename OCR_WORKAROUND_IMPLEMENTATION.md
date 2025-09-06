# OCR Workaround Implementation - Detailed Agent Instructions

## Project Context
Faxbot currently has a severe UX limitation: MCP servers require base64-encoded file content, making large PDFs (>1MB) fail due to token consumption. The solution is to implement MCP prompts that automatically extract text from PDFs instead of sending the entire file as base64.

## Current Project Structure (CRITICAL - DO NOT BREAK THIS)
```
/Users/davidmontgomery/faxbot/
├── api/                          # Main FastAPI service + Node MCP servers
│   ├── app/                      # FastAPI Python code
│   ├── mcp_server.js             # Node MCP stdio server
│   ├── mcp_http_server.js        # Node MCP HTTP server  
│   ├── mcp_sse_server.js         # Node MCP SSE+OAuth server
│   ├── package.json              # Node dependencies
│   └── setup-mcp.js              # MCP installer script
├── python_mcp/                   # Python MCP servers (EXISTING - DO NOT TOUCH)
│   ├── stdio_server.py
│   ├── http_server.py
│   ├── server.py
│   └── requirements.txt
├── sdks/                         # Client SDKs
│   ├── node/
│   └── python/
└── docs/                         # Documentation
```

## Proposed Structure Addition (NEW - CREATE THIS)
```
/Users/davidmontgomery/faxbot/
├── node_mcp/                     # NEW: Organized Node MCP servers
│   ├── src/                      # Source code
│   │   ├── servers/              # Individual server implementations
│   │   │   ├── stdio.js          # Stdio transport server
│   │   │   ├── http.js           # HTTP transport server
│   │   │   └── sse.js            # SSE+OAuth transport server
│   │   ├── prompts/              # MCP prompt definitions
│   │   │   ├── faxbot.js         # Faxbot prompts (OCR workflow)
│   │   │   └── index.js          # Prompt registry
│   │   ├── tools/                # MCP tool implementations
│   │   │   ├── fax-tools.js      # send_fax, get_fax_status
│   │   │   └── pdf-tools.js      # extract_pdf_text (internal)
│   │   └── shared/               # Shared utilities
│   │       ├── pdf-extractor.js  # PDF text extraction logic
│   │       └── fax-client.js     # Faxbot API client
│   ├── package.json              # Dependencies (pdf-parse, etc.)
│   ├── README.md                 # Node MCP documentation
│   └── scripts/                  # Build/run scripts
│       ├── start-stdio.sh
│       ├── start-http.sh
│       └── start-sse.sh
```

## Implementation Tasks

### Phase 1: Create New Structure
1. **Create /node_mcp directory structure**
   - All directories and subdirectories as shown above
   - DO NOT modify anything in /api directory yet
   - This is a clean slate implementation

2. **Initialize package.json in /node_mcp**
   ```json
   {
     "name": "faxbot-node-mcp",
     "version": "1.0.0",
     "description": "Node.js MCP servers for Faxbot with OCR workflow support",
     "main": "src/servers/stdio.js",
     "scripts": {
       "stdio": "node src/servers/stdio.js",
       "http": "node src/servers/http.js", 
       "sse": "node src/servers/sse.js"
     },
     "dependencies": {
       "@modelcontextprotocol/sdk": "^1.17.5",
       "axios": "^1.7.0",
       "form-data": "^4.0.0",
       "pdf-parse": "^1.1.1",
       "fs": "^0.0.1-security",
       "path": "^0.12.7"
     }
   }
   ```

### Phase 2: Implement Core Utilities

3. **Create /node_mcp/src/shared/pdf-extractor.js**
   - Import pdf-parse library
   - Function: `extractTextFromPDF(filePath)` 
   - Function: `extractTextFromBuffer(buffer)`
   - Error handling for corrupted PDFs
   - Return cleaned text (remove excessive whitespace, format nicely)

4. **Create /node_mcp/src/shared/fax-client.js**
   - Axios-based client for Faxbot API
   - Functions: `sendFax(to, content, type)`, `getFaxStatus(jobId)`
   - Handle API authentication (X-API-Key header)
   - Base URL from environment variable

### Phase 3: Implement MCP Tools

5. **Create /node_mcp/src/tools/pdf-tools.js**
   - MCP tool: `extract_pdf_text`
   - Input schema: `{ filePath: string }`
   - Uses pdf-extractor.js internally
   - This is INTERNAL tool, not exposed to user

6. **Create /node_mcp/src/tools/fax-tools.js**
   - MCP tools: `send_fax`, `get_fax_status` (existing tools)
   - Move logic from current /api/mcp_server.js
   - Clean up and organize properly

### Phase 4: Implement MCP Prompts (THE KEY FEATURE)

7. **Create /node_mcp/src/prompts/faxbot.js**
   ```javascript
   const FAXBOT_PROMPTS = {
     "faxbot_pdf": {
       name: "faxbot_pdf",
       description: "Extract text from PDF and send as fax (avoids base64 token limits)",
       arguments: [
         {
           name: "pdf_path",
           description: "Absolute path to PDF file",
           required: true
         },
         {
           name: "to", 
           description: "Fax number (E.164 format preferred)",
           required: true
         },
         {
           name: "header_text",
           description: "Optional header text to add",
           required: false
         }
       ]
     }
   };
   ```

8. **Create /node_mcp/src/prompts/index.js**
   - Export all prompt definitions
   - Registry pattern for easy expansion

### Phase 5: Implement MCP Servers

9. **Create /node_mcp/src/servers/stdio.js**
   - Copy structure from /api/mcp_server.js
   - Add ListPromptsRequestSchema, GetPromptRequestSchema handlers
   - Add prompt execution logic for faxbot_pdf
   - Import tools and prompts from organized modules

10. **Create /node_mcp/src/servers/http.js**
    - Copy structure from /api/mcp_http_server.js
    - Add same prompt support as stdio server
    - Maintain HTTP transport functionality

11. **Create /node_mcp/src/servers/sse.js**
    - Copy structure from /api/mcp_sse_server.js
    - Add same prompt support as stdio server  
    - Maintain OAuth2/JWT functionality

### Phase 6: Prompt Execution Logic

12. **Implement faxbot_pdf workflow in each server**
    ```javascript
    async function executeSmartFaxPdf(args) {
      // 1. Validate PDF file exists
      // 2. Extract text using pdf-extractor
      // 3. Format text nicely (add headers if provided)
      // 4. Send as TXT fax using fax-client
      // 5. Return job ID and confirmation
      // 6. Handle errors gracefully (file not found, extraction failed, etc.)
    }
    ```

### Phase 7: Testing & Integration

13. **Create test scripts in /node_mcp/scripts/**
    - start-stdio.sh, start-http.sh, start-sse.sh
    - Test with small PDF, large PDF, corrupted PDF
    - Verify text extraction quality
    - Confirm fax transmission works

14. **Update documentation**
    - Create /node_mcp/README.md with usage examples
    - Update main project docs to reference new structure
    - Add migration guide from /api servers to /node_mcp servers

### Phase 8: Migration Path (CAREFUL)

15. **DO NOT DELETE /api MCP servers yet**
    - Keep them as fallback
    - Add deprecation notices
    - Update setup scripts to point to /node_mcp by default
    - Test extensively before considering removal

## Key Implementation Details

### MCP Prompt Handler Structure
```javascript
this.server.setRequestHandler(GetPromptRequestSchema, async (request) => {
  const { name, arguments: args } = request.params;
  
  switch (name) {
    case 'faxbot_pdf':
      // Extract text from PDF
      const text = await extractTextFromPDF(args.pdf_path);
      // Send as text fax
      const result = await sendFax(args.to, text, 'txt');
      // Return formatted message for LLM
      return {
        messages: [
          {
            role: 'user',
            content: {
              type: 'text', 
              text: `Faxbot workflow initiated. PDF "${args.pdf_path}" extracted to ${text.length} characters. Fax job ID: ${result.jobId}`
            }
          }
        ]
      };
  }
});
```

### Error Handling Requirements
- File not found: Clear error message with file path
- PDF extraction failed: Graceful fallback message
- Fax API errors: Pass through original error
- Large text extraction: Warn if >100KB of text

### Environment Variables
- `FAX_API_URL`: Faxbot API endpoint (default: http://localhost:8080)
- `API_KEY`: Faxbot API authentication key
- `MAX_TEXT_SIZE`: Maximum extracted text size in bytes (default: 100000)

## Expected User Experience After Implementation

### Before (Broken):
```
User: "Fax report.pdf to +1234567890"
Claude: "I need to read the file first and encode it as base64..."
Result: Token limit exceeded, fails
```

### After (Working):
```  
User: "Faxbot report.pdf to +1234567890"  
Claude: "I'll use the faxbot_pdf workflow to extract text and send it."
Result: PDF text extracted, sent as text fax, succeeds
```

## Critical Success Criteria
1. **File size handling**: 10MB PDF → ~100KB text (99% reduction)
2. **Token efficiency**: No base64 encoding in conversation
3. **Text fidelity**: Extracted text is readable and formatted
4. **Error resilience**: Graceful failures with helpful messages
5. **Backward compatibility**: Existing tools still work
6. **Project structure**: Clean, organized, maintainable code

## What NOT To Do
- DO NOT modify /api directory during initial implementation
- DO NOT delete existing MCP servers until new ones are proven
- DO NOT break existing functionality
- DO NOT create files in random locations
- DO NOT ignore error handling
- DO NOT hardcode file paths or API endpoints
- DO NOT add unnecessary dependencies

## Deliverables
1. Complete /node_mcp directory structure
2. Working MCP servers with prompt support
3. PDF text extraction functionality  
4. Documentation and examples
5. Test scripts and validation
6. Migration guide

This implementation will solve the base64 limitation while maintaining clean project structure and providing a foundation for future MCP prompt workflows.
