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
    @State private var showingScanner = false
    @State private var showingText = false
    @State private var textContent = ""
    @State private var sending = false
    @State private var resultMessage: String?

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
                        Text(num).font(.caption)
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
        .background(.ultraThinMaterial)
    }

    func sendText() async {
        sending = true; defer { sending = false }
        guard let data = textContent.data(using: .utf8) else { return }
        do {
            let results = await client.sendFax(toMany: parsedNumbers, file: data, filename: "note.txt")
            let ok = results.filter { if case .success = $0.result { true } else { false } }.count
            resultMessage = "Queued to \(ok)/\(results.count)"
            textContent = ""
            for r in results { await RecentContacts.shared.add(number: r.to) }
        } catch {
            resultMessage = "Failed: \(error.localizedDescription)"
        }
    }

    func sendImages(_ images: [UIImage]) async {
        sending = true; defer { sending = false }
        do {
            let pdf = try PDFComposer.pdfFrom(images: images)
            let results = await client.sendFax(toMany: parsedNumbers, file: pdf, filename: "scan.pdf")
            let ok = results.filter { if case .success = $0.result { true } else { false } }.count
            resultMessage = "Queued to \(ok)/\(results.count)"
            for r in results { await RecentContacts.shared.add(number: r.to) }
        } catch {
            resultMessage = "Failed: \(error.localizedDescription)"
        }
    }

    func parseNumbers(_ raw: String) -> [String] {
        let parts = raw.split(whereSeparator: { ", \n\t".contains($0) })
        return parts.map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }.filter { !$0.isEmpty }
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
        .overlay(loading ? ProgressView() : nil)
        .task { await reload() }
        .sheet(item: $previewURL) { url in
            PDFPreview(url: url)
        }
    }

    func reload() async {
        loading = true; defer { loading = false }
        do { items = try await client.listInbound() } catch { items = [] }
    }

    func openPdf(id: String) async {
        do { previewURL = try await client.downloadInboundPdf(id: id) } catch { }
    }
}

struct HistoryView: View {
    @EnvironmentObject private var client: FaxbotClient
    @State private var items: [[String:String]] = []
    @State private var polling = true
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
                    }
                }
            }
        }
        .onAppear { Task { items = await FaxHistory.shared.list(); await startPolling() } }
        .onDisappear { polling = false }
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
                await FaxHistory.shared.update(jobId: id, status: job.status, error: job.error)
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
