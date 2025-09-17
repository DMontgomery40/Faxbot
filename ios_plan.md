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
   - Server URL configuration
   - API key setup
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
    var url: String = ""
    var apiKey: String = ""
    var inboundEnabled: Bool = false
    var isConfigured: Bool { !url.isEmpty }
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
- AVFoundation (Camera)
- PDFKit (PDF generation and viewing)
- Vision (Document scanning)
- Combine (Data flow)
- Foundation (Networking)
- QuickLook (PDF preview)

### App Capabilities:
- Camera usage
- Photo library access (share extension)
- Network requests
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
// Info.plist configuration for self-hosted exceptions
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
    <key>NSAllowsArbitraryLoadsInWebContent</key>
    <false/>
</dict>
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
2. Server URL configuration (e.g., `https://fax.myhouse.local:8080`)
3. API key setup (copy from Faxbot admin console)
4. Connection test
5. Tutorial overlay showing main features

### Daily Usage:
1. **Quick Fax**: Open app → Camera → Take photo → Select contact → Send
2. **Share Fax**: In Photos → Share → Fax → Select contact → Send
3. **Text Fax**: Open app → Text tab → Type message → Select contact → Send
4. **Read Fax**: Open app → Inbox tab → Tap received fax → View PDF

### Advanced Features (Hidden by Default):
- Custom server settings
- Detailed fax job history with metadata
- Re-send failed faxes
- Export/share received fax PDFs
- Bulk operations on inbox
- Connection diagnostics
- Inbound fax notifications settings

## Success Metrics

### Core Functionality:
- [ ] Successfully connects to Faxbot server
- [ ] Sends photo faxes reliably
- [ ] Sends text faxes reliably
- [ ] Displays received faxes in inbox
- [ ] PDF viewer for received faxes works
- [ ] Share extension works from Photos/Files
- [ ] Status tracking shows job progress

### User Experience:
- [ ] Setup takes <5 minutes for technical users
- [ ] Sending a fax takes <30 seconds
- [ ] App feels native on iOS 26
- [ ] Error messages are helpful and actionable

### Technical:
- [ ] Handles network interruptions gracefully
- [ ] Maintains connection state across app launches
- [ ] Respects file size limits
- [ ] Share extension completes quickly

This plan creates a focused, practical iOS app that serves as a clean mobile interface to Faxbot, prioritizing simplicity and reliability over feature complexity.

