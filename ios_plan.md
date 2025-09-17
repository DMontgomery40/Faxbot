# Faxbot iOS Client - TestFlight Development Plan

## Project Overview

**Purpose**: Create a simple iOS wrapper app that connects to a user's self-hosted Faxbot instance at home. Think HomeAssistant app or Scrypted app - the user manages their own VPN tunnel/connection, the app is just a clean mobile interface.

**Core Functions**:
1. **Photo to Fax**: Take a picture (insurance card, prescription, etc.) and fax it as easily as sending a text
2. **Text to Fax**: Type text, convert to .txt file, and send as fax
3. **Inbox**: View and read received faxes with PDF viewer
4. **Share Sheet Integration**: Fax documents from any iOS app via share sheet

**Design Philosophy**: Dead simple by default, advanced features hidden under "Advanced" settings. All complexity handled by the user's Faxbot server.

## Faxbot API Integration

**Base Endpoints** (user configures their server URL):
- `POST /fax` - Send fax (multipart: to, file)
- `GET /fax/{job_id}` - Check fax status
- `GET /inbound` - List received faxes
- `GET /inbound/{inbound_id}` - Get received fax details
- `GET /inbound/{inbound_id}/pdf` - Download received fax PDF
- `GET /health` - Health check

**Authentication**:
- X-API-Key header (user configures in app settings)
- Optional in dev mode

**File Support**:
- PDF and TXT files only (Faxbot handles conversion)
- Max 10MB default limit

## App Architecture

### Core App Structure
**SwiftUI + iOS 26 Liquid Glass Design**:
- Modern SwiftUI interface with automatic Liquid Glass effects
- System color palette for automatic light/dark mode
- Standard navigation patterns for familiar UX

### Main Views:
1. **Send Fax Tab**
   - Photo capture button (main action)
   - Text input field
   - Recent contacts picker
   - Send button

2. **Inbox Tab**
   - List of received faxes (newest first)
   - Sender, date, page count
   - Tap to view PDF
   - Mark as read/unread
   - Search/filter functionality

3. **History Tab**
   - Recent sent fax jobs with status
   - Tap to retry failed faxes

4. **Settings Tab**
   - **Connection Status** section:
     - Current active connection (Local/Tunnel)
     - Connection health indicators
     - "Test Connections" button
   - **Server Configuration**:
     - Local URL (auto-discovered or manual)
     - Tunnel URL (from Faxbot tunnel setup)
     - Connection preference (Auto/Local Only/Tunnel Only)
   - **Setup Options**:
     - "Scan QR Code" button
     - "Discover on Network" button
     - Manual server entry
   - Advanced settings (collapsed by default)

### Data Models:
```swift
struct FaxJob {
    let id: String
    let to: String
    let status: String // "queued", "in_progress", "SUCCESS", "FAILED"
    let error: String?
    let pages: Int?
    let backend: String
    let createdAt: Date
    let updatedAt: Date
}

struct InboundFax {
    let id: String
    let from: String?
    let to: String?
    let status: String
    let backend: String
    let pages: Int?
    let sizeBytes: Int?
    let createdAt: Date?
    let receivedAt: Date?
    let updatedAt: Date?
    var isRead: Bool = false // local app state
}

struct ServerConfig {
    var localURL: String = ""          // 192.168.1.100:8080
    var tunnelURL: String = ""         // https://fax-abc123.trycloudflare.com
    var apiKey: String = ""
    var tunnelProvider: String = ""    // cloudflare, wireguard, tailscale, none
    var inboundEnabled: Bool = false
    var preferredConnection: ConnectionType = .auto

    var isConfigured: Bool { !localURL.isEmpty || !tunnelURL.isEmpty }
    var hasMultipleConnections: Bool { !localURL.isEmpty && !tunnelURL.isEmpty }
}

enum ConnectionType {
    case auto      // Smart selection based on network
    case local     // Force local network
    case tunnel    // Force tunnel
}

struct ConnectionStatus {
    let type: ConnectionType
    let url: String
    let isReachable: Bool
    let latency: TimeInterval?
    let lastTested: Date
}
```

## Share Extension Implementation

### Share Extension Target
Create iOS Share Extension for "Fax" action in share sheet:
- Support images (JPEG, PNG, HEIC) up to 10 files
- Support PDFs
- Show in share sheet for supported content types

### Extension Flow:
1. User shares image/PDF from any app
2. Extension shows contact picker + optional note field
3. Convert images to PDF if needed (iOS APIs handle this)
4. Upload to user's Faxbot server
5. Show success/failure notification

### Key Implementation Details:
```swift
// Info.plist configuration
NSExtensionActivationRule = {
    NSExtensionActivationSupportsImageWithMaxCount = 10;
    NSExtensionActivationSupportsFileWithMaxCount = 5;
    NSExtensionActivationSupportsText = NO;
}
```

## Core Features Implementation

### 1. Photo to Fax Feature
**UI Flow**:
- Large camera button (primary action)
- Live camera preview
- Crop/adjust interface
- Contact selection
- Send confirmation

**Technical**:
- Use `AVCaptureSession` for camera
- `VNDocumentCameraViewController` for document scanning
- Convert images to PDF using `PDFKit`
- Compress for upload efficiency

### 2. Text to Fax Feature
**UI Flow**:
- Simple text editor interface
- Character count indicator
- Contact selection
- Preview formatted text

**Technical**:
- Convert text to PDF using `NSAttributedString` + `PDFKit`
- Standard formatting (readable font, margins)
- Save as .txt file for Faxbot API

### 3. Contact Management
**Simple Contact Storage**:
- UserDefaults for small contact list
- Name + Phone number only
- Recent contacts at top
- Manual entry fallback

```swift
struct FaxContact {
    let id: UUID
    let name: String
    let phoneNumber: String
    var lastUsed: Date
}
```

## Network Layer & API Client

### Faxbot API Client
```swift
class FaxbotClient: ObservableObject {
    @Published var serverConfig = ServerConfig()
    @Published var isConnected = false

    private let session = URLSession.shared

    func sendFax(to: String, file: Data, filename: String) async throws -> FaxJob {
        let url = URL(string: "\(serverConfig.url)/fax")!
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue(serverConfig.apiKey, forHTTPHeaderField: "X-API-Key")

        // Multipart form data
        let boundary = UUID().uuidString
        request.setValue("multipart/form-data; boundary=\(boundary)",
                        forHTTPHeaderField: "Content-Type")

        let body = createMultipartBody(to: to, file: file,
                                     filename: filename, boundary: boundary)
        request.httpBody = body

        let (data, response) = try await session.data(for: request)
        // Handle response...
    }

    func checkStatus(jobId: String) async throws -> FaxJob {
        let url = URL(string: "\(serverConfig.url)/fax/\(jobId)")!
        var request = URLRequest(url: url)
        request.setValue(serverConfig.apiKey, forHTTPHeaderField: "X-API-Key")

        let (data, _) = try await session.data(for: request)
        return try JSONDecoder().decode(FaxJob.self, from: data)
    }

    func getInboundFaxes() async throws -> [InboundFax] {
        let url = URL(string: "\(serverConfig.url)/inbound")!
        var request = URLRequest(url: url)
        request.setValue(serverConfig.apiKey, forHTTPHeaderField: "X-API-Key")

        let (data, _) = try await session.data(for: request)
        return try JSONDecoder().decode([InboundFax].self, from: data)
    }

    func getInboundFax(id: String) async throws -> InboundFax {
        let url = URL(string: "\(serverConfig.url)/inbound/\(id)")!
        var request = URLRequest(url: url)
        request.setValue(serverConfig.apiKey, forHTTPHeaderField: "X-API-Key")

        let (data, _) = try await session.data(for: request)
        return try JSONDecoder().decode(InboundFax.self, from: data)
    }

    func downloadInboundPDF(id: String) async throws -> Data {
        let url = URL(string: "\(serverConfig.url)/inbound/\(id)/pdf")!
        var request = URLRequest(url: url)
        request.setValue(serverConfig.apiKey, forHTTPHeaderField: "X-API-Key")

        let (data, _) = try await session.data(for: request)
        return data
    }

    func testConnection() async -> Bool {
        // Test /health endpoint and check inbound availability
    }

    func discoverServers() async -> [ServerInfo] {
        // Scan local network for Faxbot instances
    }

    func getTunnelInfo() async throws -> TunnelInfo {
        let url = URL(string: "\(currentURL)/admin/tunnel/status")!
        var request = URLRequest(url: url)
        request.setValue(serverConfig.apiKey, forHTTPHeaderField: "X-API-Key")

        let (data, _) = try await session.data(for: request)
        return try JSONDecoder().decode(TunnelInfo.self, from: data)
    }

    func selectBestConnection() async -> String {
        // Test both local and tunnel URLs, return fastest available
        if isOnLocalNetwork() && !serverConfig.localURL.isEmpty {
            return serverConfig.localURL
        } else if !serverConfig.tunnelURL.isEmpty {
            return serverConfig.tunnelURL
        }
        return serverConfig.localURL.isEmpty ? serverConfig.tunnelURL : serverConfig.localURL
    }

    private func isOnLocalNetwork() -> Bool {
        // Check if device is on same network as Faxbot server
        // Compare network interfaces, ping local URL, etc.
        return true // Simplified for now
    }
}

struct ServerInfo {
    let localURL: String
    let tunnelURL: String?
    let tunnelProvider: String?
    let version: String
    let serverName: String
}

struct TunnelInfo {
    let enabled: Bool
    let provider: String  // cloudflare, wireguard, tailscale, none
    let status: String    // connected, connecting, error, disabled
    let publicURL: String?
    let lastConnected: Date?
    let error: String?
}
}
```

## Development Timeline

### Phase 1: Core App (2-3 weeks)
**Week 1**:
- Xcode 26 project setup with iOS 26 deployment target
- Basic SwiftUI interface with TabView
- Server configuration screen
- API client implementation

**Week 2**:
- Camera integration for photo capture
- Text editor for text-to-fax
- Contact management
- Basic fax sending functionality

**Week 3**:
- Inbox implementation with PDF viewer
- Status checking and outbound history
- Error handling and user feedback
- Testing and bug fixes

### Phase 2: Share Extension (1-2 weeks)
**Week 4**:
- Share extension target setup
- Extension UI implementation
- File handling and conversion
- Integration with main app via App Groups

**Week 5** (if needed):
- Extension testing and refinement
- Background upload implementation
- Notification integration

### Phase 3: Polish & TestFlight (1 week)
**Week 6**:
- iOS 26 design polish
- App icon and assets
- TestFlight preparation
- Beta testing

## Technical Requirements

### iOS 26 & Xcode 26 Features
- **Minimum iOS version**: iOS 26.0
- **Xcode version**: Xcode 26 beta
- **SwiftUI**: Latest version with Liquid Glass effects
- **Automatic design**: Let system handle translucency and materials

### Key Frameworks:
- SwiftUI (UI)
- AVFoundation (Camera, QR code scanning)
- PDFKit (PDF generation and viewing)
- Vision (Document scanning, QR detection)
- Combine (Data flow)
- Foundation (Networking)
- Network (Local network discovery)
- QuickLook (PDF preview)

### App Capabilities:
- Camera usage (document scanning, QR codes)
- Photo library access (share extension)
- Network requests (both local and internet)
- Local network access (for auto-discovery)
- Background app refresh (for status updates)
- Push notifications (optional)

### Enhanced Security Requirements
Following Apple's enhanced security guidelines: https://developer.apple.com/documentation/xcode/enabling-enhanced-security-for-your-app

**App Transport Security (ATS)**:
- Require HTTPS for all network connections to Faxbot server
- Allow user to add localhost/private IP exceptions for self-hosted setups
- Implement certificate pinning for production deployments

**Data Protection**:
- Store API keys and server config in Keychain (not UserDefaults)
- Use `kSecAttrAccessibleWhenUnlockedThisDeviceOnly` for sensitive data
- Enable data protection for app sandbox files

**Network Security**:
```swift
// Info.plist configuration for self-hosted and local network access
<key>NSAppTransportSecurity</key>
<dict>
    <key>NSExceptionDomains</key>
    <dict>
        <key>localhost</key>
        <dict>
            <key>NSExceptionAllowsInsecureHTTPLoads</key>
            <true/>
        </dict>
    </dict>
    <key>NSAllowsLocalNetworking</key>
    <true/>
    <key>NSAllowsArbitraryLoadsInWebContent</key>
    <false/>
</dict>

// Local Network Usage Description
<key>NSLocalNetworkUsageDescription</key>
<string>This app discovers Faxbot servers on your local network for easy setup and connection.</string>

// Camera Usage for QR Codes
<key>NSCameraUsageDescription</key>
<string>Camera access is used to scan documents for faxing and QR codes for server setup.</string>
```

**Privacy Permissions**:
- Camera usage description for document scanning
- Photo library access for share extension
- Clear privacy policy explaining data handling

**Code Signing & Provisioning**:
- Enable automatic app signing in Xcode 26
- Use App Store distribution certificates for TestFlight
- Configure proper bundle identifiers and team settings

## User Experience Flow

### First Launch Setup:
1. Welcome screen explaining the app purpose
2. **Auto-Discovery & Setup Options**:
   - **Option A: Scan QR Code** from Faxbot Admin Console (instant setup)
   - **Option B: Auto-Discover** on local network (finds `192.168.x.x:8080`)
   - **Option C: Manual Entry** for custom configurations
3. Connection test (tests both local and tunnel URLs)
4. Tutorial overlay showing main features

### Smart Connection Discovery
The app intelligently finds and connects to Faxbot:

**Local Network Discovery**:
- Scans common IP ranges (192.168.x.x, 10.x.x.x) for Faxbot instances
- Tests `/health` endpoint on port 8080
- Shows "Found Faxbot at 192.168.1.100" with automatic setup

**QR Code Setup** (Preferred Method):
- Admin Console generates QR code containing:
  - Local URL (`192.168.1.100:8080`)
  - Tunnel URL (`https://fax-abc123.trycloudflare.com`)
  - API key
  - Tunnel provider type
- iOS app scans and auto-configures everything
- Instant connection with zero typing

**Manual Configuration**:
- Enter server URL manually
- App detects if it's local IP or tunnel URL
- Prompts for API key

### Daily Usage:
1. **Quick Fax**: Open app → Camera → Take photo → Select contact → Send
2. **Share Fax**: In Photos → Share → Fax → Select contact → Send
3. **Text Fax**: Open app → Text tab → Type message → Select contact → Send
4. **Read Fax**: Open app → Inbox tab → Tap received fax → View PDF

### Advanced Setup Scenarios:

**Scenario 1: Home User with Router VPN (Firewalla)**
1. User has WireGuard running on Firewalla router
2. Sets up Faxbot with WireGuard client mode via Admin Console
3. iOS app discovers local IP when home, uses tunnel when away
4. Seamless experience everywhere via user's own VPN

**Scenario 2: Non-Technical User**
1. Starts Faxbot Docker container (Cloudflare auto-enabled)
2. Scans QR code from Admin Console in iOS app
3. App auto-configures both local and tunnel URLs
4. Works immediately from anywhere

**Scenario 3: Enterprise with Tailscale**
1. Admin configures Tailscale in Faxbot Admin Console
2. Users add Faxbot's Tailnet address to iOS app
3. Secure access through corporate network
4. Full audit logging and access controls

### Advanced Features (Hidden by Default):
- **Connection Management**:
  - Manual connection preference override
  - Connection latency monitoring
  - Network diagnostics and troubleshooting
  - Custom timeout settings
- **Server Management**:
  - Multiple server profiles
  - Server health monitoring
  - API key rotation
  - Tunnel status and logs
- **Fax Operations**:
  - Detailed fax job history with metadata
  - Re-send failed faxes
  - Export/share received fax PDFs
  - Bulk operations on inbox
  - Custom retry policies
- **Notifications & Alerts**:
  - Inbound fax notifications settings
  - Connection failure alerts
  - Server health notifications

## Success Metrics

### Core Functionality:
- [ ] Successfully connects to Faxbot server (local and tunnel)
- [ ] Auto-discovers Faxbot on local network
- [ ] QR code setup works from Admin Console
- [ ] Smart connection selection (local when home, tunnel when remote)
- [ ] Sends photo faxes reliably via both connection types
- [ ] Sends text faxes reliably
- [ ] Displays received faxes in inbox
- [ ] PDF viewer for received faxes works
- [ ] Share extension works from Photos/Files
- [ ] Status tracking shows job progress
- [ ] Connection health monitoring works

### User Experience:
- [ ] QR code setup takes <30 seconds
- [ ] Auto-discovery setup takes <2 minutes
- [ ] Manual setup takes <5 minutes for technical users
- [ ] Sending a fax takes <30 seconds
- [ ] Connection switching is seamless (home ↔ remote)
- [ ] App feels native on iOS 26
- [ ] Error messages are helpful and actionable
- [ ] Network issues are handled gracefully

### Technical:
- [ ] Handles network interruptions gracefully
- [ ] Maintains connection state across app launches
- [ ] Respects file size limits
- [ ] Share extension completes quickly

This plan creates a focused, practical iOS app that serves as a clean mobile interface to Faxbot, prioritizing simplicity and reliability over feature complexity.

