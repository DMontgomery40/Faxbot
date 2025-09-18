import SwiftUI

struct ContentView: View {
    @EnvironmentObject private var client: FaxbotClient
    @State private var activeTab: Int = 0

    var body: some View {
        TabView(selection: $activeTab) {
            SendView()
                .tabItem { Label("Send", systemImage: "paperplane.fill") }
                .tag(0)
            InboxView()
                .tabItem { Label("Inbox", systemImage: "tray.and.arrow.down.fill") }
                .tag(1)
            HistoryView()
                .tabItem { Label("History", systemImage: "clock.fill") }
                .tag(2)
            SettingsView()
                .tabItem { Label("Settings", systemImage: "gearshape.fill") }
                .tag(3)
        }
        .task {
            await client.bootstrapFromKeychain()
        }
    }
}

struct SendView: View {
    @EnvironmentObject private var client: FaxbotClient
    @State private var toNumbersRaw: String = ""
    @State private var parsedNumbers: [String] = []
    @State private var contactMap: [String:String] = [:]
    @State private var showingScanner = false
    @State private var showingText = false
    @State private var textContent = ""
    @State private var sending = false
    @State private var resultMessage: String?
    @State private var showToast = false
    @State private var showingContacts = false

    var body: some View {
        ScrollView {
            VStack(spacing: 16) {
                TextField("To (E.164, comma-separated)", text: $toNumbersRaw)
                    .textFieldStyle(.roundedBorder)
                    .keyboardType(.numbersAndPunctuation)
                    .onChange(of: toNumbersRaw) { _, newValue in
                        parsedNumbers = parseNumbers(newValue)
                    }
                if !parsedNumbers.isEmpty {
                    FlowLayout(parsedNumbers, id: \.self) { num in
                        let label = contactMap[num] != nil ? "\(contactMap[num]!) (\(num))" : num
                        Text(label).font(.caption)
                            .padding(.horizontal, 8).padding(.vertical, 4)
                            .background(Capsule().fill(Color.secondary.opacity(0.15)))
                    }
                }
                HStack {
                    Button {
                        showingScanner = true
                    } label: {
                        Label("Scan Document", systemImage: "doc.viewfinder")
                    }
                    .buttonStyle(.borderedProminent)

                    Button { showingText = true } label: { Label("Type Text", systemImage: "text.alignleft") }
                        .buttonStyle(.bordered)
                    Button { Haptics.lightTap(); showingContacts = true } label: { Label("Contacts", systemImage: "person.crop.circle") }
                        .buttonStyle(.bordered)
                }
                // Recent contacts quick-add
                RecentChips { selected in
                    let existing = Set(parsedNumbers)
                    parsedNumbers = Array(existing.union([selected]))
                    toNumbersRaw = parsedNumbers.joined(separator: ", ")
                }
                if let msg = resultMessage { Text(msg).font(.footnote).foregroundStyle(.secondary) }
            }.padding()
        }
        .scrollBounceBehavior(.basedOnSize)
        .sheet(isPresented: $showingText) {
            NavigationStack {
                VStack {
                    TextEditor(text: $textContent).frame(minHeight: 240)
                        .overlay(RoundedRectangle(cornerRadius: 8).stroke(.quaternary))
                    Button(sending ? "Sending…" : "Send as TXT to \(parsedNumbers.count)") {
                        Task { await sendText() }
                    }.disabled(sending || parsedNumbers.isEmpty || textContent.isEmpty)
                }.padding().navigationTitle("Text to Fax")
            }
        }
        .sheet(isPresented: $showingScanner) {
            DocumentScanner { images in
                Task { await sendImages(images) }
            }
        }
        .sheet(isPresented: $showingContacts) {
            ContactPickerView { selected in
                let nums = selected.map { $0.number }
                let existing = Set(parsedNumbers)
                parsedNumbers = Array(existing.union(nums))
                toNumbersRaw = parsedNumbers.joined(separator: ", ")
            }
        }
        .background(.ultraThinMaterial)
        .overlay(alignment: .bottom) {
            if showToast, let msg = resultMessage {
                ToastView(text: msg)
                    .transition(.move(edge: .bottom).combined(with: .opacity))
                    .padding(.bottom, 20)
            }
        }
        .task { await loadContactsMap() }
    }

    func sendText() async {
        sending = true; defer { sending = false }
        guard let data = textContent.data(using: .utf8) else { return }
        let results = await client.sendFax(toMany: parsedNumbers, file: data, filename: "note.txt")
        let ok = results.filter { if case .success = $0.result { true } else { false } }.count
        resultMessage = ok == results.count ? "Queued to \(ok) recipient(s)" : "Queued \(ok)/\(results.count)"
        Haptics.success()
        withAnimation(.spring(response: 0.35, dampingFraction: 0.8)) { showToast = true }
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.6) { withAnimation { showToast = false } }
        textContent = ""
        for r in results { await RecentContacts.shared.add(number: r.to) }
    }

    func sendImages(_ images: [UIImage]) async {
        sending = true; defer { sending = false }
        do {
            let pdf = try PDFComposer.pdfFrom(images: images)
            let results = await client.sendFax(toMany: parsedNumbers, file: pdf, filename: "scan.pdf")
            let ok = results.filter { if case .success = $0.result { true } else { false } }.count
            resultMessage = ok == results.count ? "Queued to \(ok) recipient(s)" : "Queued \(ok)/\(results.count)"
            Haptics.success()
            withAnimation(.spring(response: 0.35, dampingFraction: 0.8)) { showToast = true }
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.6) { withAnimation { showToast = false } }
            for r in results { await RecentContacts.shared.add(number: r.to) }
        } catch {
            resultMessage = "Failed: \(error.localizedDescription)"
            Haptics.error()
            withAnimation(.spring(response: 0.35, dampingFraction: 0.8)) { showToast = true }
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.8) { withAnimation { showToast = false } }
        }
    }

    func parseNumbers(_ raw: String) -> [String] {
        let parts = raw.split(whereSeparator: { ", \n\t".contains($0) })
        return parts.map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }.filter { !$0.isEmpty }
    }
    func loadContactsMap() async {
        let list = await ContactsStore.shared.list()
        var m: [String:String] = [:]
        for c in list { m[c.number] = c.name }
        contactMap = m
    }
}

struct InboxView: View {
    @EnvironmentObject private var client: FaxbotClient
    @State private var items: [FaxbotClient.InboundItem] = []
    @State private var previewURL: URL?
    @State private var loading = false

    var body: some View {
        List(items) { it in
            Button {
                Task { await openPdf(id: it.id) }
            } label: {
                VStack(alignment: .leading) {
                    HStack {
                        Text("From: \(it.fr ?? "Unknown")")
                        Spacer()
                        if let p = it.pages { Text("\(p) pgs").foregroundStyle(.secondary) }
                    }
                    Text(it.received_at ?? "").font(.caption).foregroundStyle(.secondary)
                }
            }
        }
        .listStyle(.insetGrouped)
        .scrollContentBackground(.hidden)
        .scrollBounceBehavior(.basedOnSize)
        .overlay(loading ? ProgressView() : nil)
        .task { await reload() }
        .refreshable { await reload() }
        .sheet(isPresented: Binding(get: { previewURL != nil }, set: { if !$0 { previewURL = nil } })) {
            if let url = previewURL { PDFPreview(url: url) }
        }
    }

    func reload() async {
        Haptics.lightTap()
        loading = true; defer { loading = false }
        do {
            let prev = Set(items.map { $0.id })
            let latest = try await client.listInbound()
            let latestIDs = Set(latest.map { $0.id })
            let newIDs = latestIDs.subtracting(prev)
            items = latest
            for id in newIDs {
                if let fx = latest.first(where: { $0.id == id }) {
                    NotificationManager.shared.notifyInbound(id: id, from: fx.fr)
                }
            }
        } catch { items = [] }
    }

    func openPdf(id: String) async {
        do { previewURL = try await client.downloadInboundPdf(id: id); Haptics.lightTap() } catch { }
    }
}

struct HistoryView: View {
    @EnvironmentObject private var client: FaxbotClient
    @State private var items: [[String:String]] = []
    @State private var polling = true
    @State private var showResendSheet = false
    @State private var resendTo: String = ""
    @State private var showResendScanner = false
    @State private var showResendText = false
    @State private var resendText = ""
    @State private var showDocPicker = false
    var body: some View {
        List(items, id: \.["id"]) { it in
            VStack(alignment: .leading) {
                HStack {
                    Text(it["file"] ?? "").font(.subheadline)
                    Spacer()
                    StatusBadge(status: it["status"] ?? "")
                }
                Text("To: \(it["to"] ?? "") • \(it["ts"] ?? "")").font(.caption).foregroundStyle(.secondary)
                if let err = it["err"], !(err.isEmpty) {
                    VStack(alignment: .leading, spacing: 4) {
                        Text("We couldn't complete this fax.").font(.caption)
                        Text(friendlyError(err)).font(.caption2).foregroundStyle(.secondary)
                        Link("Learn more", destination: URL(string: "https://faxbot.net/docs/troubleshooting")!)
                        HStack {
                            Spacer()
                            Button { prepareResend(to: it["to"] ?? "") } label: { Label("Resend", systemImage: "arrow.triangle.2.circlepath") }
                                .buttonStyle(.borderedProminent)
                        }
                    }
                }
            }
        }
        .listStyle(.insetGrouped)
        .scrollContentBackground(.hidden)
        .scrollBounceBehavior(.basedOnSize)
        .onAppear { Task { items = await FaxHistory.shared.list(); await startPolling() } }
        .onDisappear { polling = false }
        .confirmationDialog("Resend to \(resendTo)", isPresented: $showResendSheet, titleVisibility: .visible) {
            Button("Pick PDF") { showDocPicker = true }
            Button("Scan Document") { showResendScanner = true }
            Button("Type Text") { showResendText = true }
        }
        .sheet(isPresented: $showDocPicker) {
            DocumentPicker { url, data in
                if let data, let url {
                    Task { try? await client.sendFax(to: resendTo, file: data, filename: url.lastPathComponent); Haptics.success() }
                }
            }
        }
        .sheet(isPresented: $showResendScanner) {
            DocumentScanner { images in
                Task {
                    if let pdf = try? PDFComposer.pdfFrom(images: images) {
                        try? await client.sendFax(to: resendTo, file: pdf, filename: "scan.pdf"); Haptics.success()
                    }
                }
            }
        }
        .sheet(isPresented: $showResendText) {
            NavigationStack {
                VStack(spacing: 12) {
                    Text("Resend to \(resendTo)").font(.caption).foregroundStyle(.secondary)
                    TextEditor(text: $resendText).frame(minHeight: 200).overlay(RoundedRectangle(cornerRadius: 8).stroke(.quaternary))
                    Button("Send") {
                        Task {
                            if let data = resendText.data(using: .utf8) {
                                try? await client.sendFax(to: resendTo, file: data, filename: "note.txt"); Haptics.success(); resendText = ""; showResendText = false
                            }
                        }
                    }.buttonStyle(.borderedProminent)
                }.padding().navigationTitle("Type Text")
            }
        }
    }
    private func prepareResend(to: String) {
        resendTo = to
        showResendSheet = true
    }
    func startPolling() async {
        polling = true
        while polling {
            await updateStatuses()
            try? await Task.sleep(nanoseconds: 3_000_000_000)
        }
    }
    func updateStatuses() async {
        var updated: [[String:String]] = []
        for it in items {
            guard let id = it["id"] else { continue }
            do {
                let job = try await client.getFaxStatus(jobId: id)
                let oldStatus = it["status"] ?? ""
                await FaxHistory.shared.update(jobId: id, status: job.status, error: job.error)
                let newStatus = job.status
                if newStatus.uppercased() != oldStatus.uppercased() {
                    NotificationManager.shared.notifyOutbound(jobId: id, status: newStatus, to: it["to"])
                    if newStatus.uppercased().contains("SUCCESS") { Haptics.success() }
                    if newStatus.uppercased().contains("FAIL") { Haptics.error() }
                }
            } catch { /* ignore transient */ }
        }
        updated = await FaxHistory.shared.list()
        items = updated
    }
    func friendlyError(_ raw: String) -> String {
        // Keep it human and polite
        let lower = raw.lowercased()
        if lower.contains("busy") { return "The receiving fax line was busy. Please try again later." }
        if lower.contains("no answer") { return "The other side didn't answer. Confirm the number and try again." }
        if lower.contains("unsupported") { return "The receiving system couldn't accept the fax format." }
        return "A network or line issue occurred. Please retry."
    }
}

// Simple flow layout for tag-like display
struct FlowLayout<Data: RandomAccessCollection, Content: View>: View where Data.Element: Hashable {
    private let data: Data
    private let content: (Data.Element) -> Content
    @State private var totalHeight = CGFloat.zero
    init(_ data: Data, id: KeyPath<Data.Element, Data.Element>, @ViewBuilder content: @escaping (Data.Element) -> Content) {
        self.data = data
        self.content = content
    }
    var body: some View {
        GeometryReader { geo in
            self.generate(in: geo)
        }
        .frame(height: totalHeight)
    }
    func generate(in geo: GeometryProxy) -> some View {
        var width = CGFloat.zero
        var height = CGFloat.zero
        return ZStack(alignment: .topLeading) {
            ForEach(Array(data), id: \.self) { item in
                content(item)
                    .padding(.trailing, 6)
                    .alignmentGuide(.leading) { d in
                        if (width + d.width) > geo.size.width { width = 0; height -= d.height }
                        let res = width
                        width += d.width
                        return res
                    }
                    .alignmentGuide(.top) { _ in height }
            }
        }.background(viewHeightReader($totalHeight))
    }
    func viewHeightReader(_ binding: Binding<CGFloat>) -> some View { GeometryReader { geo -> Color in DispatchQueue.main.async { binding.wrappedValue = geo.size.height }; return .clear } }
}
