import SwiftUI

struct ContactPickerView: View {
    var onDone: ([Contact]) -> Void
    @State private var contacts: [Contact] = []
    @State private var selected: Set<String> = []
    @State private var showingAdd = false
    @State private var newName = ""
    @State private var newNumber = ""

    var body: some View {
        NavigationStack {
            List {
                ForEach(contacts) { c in
                    HStack {
                        Button(action: {
                            if selected.contains(c.id) { selected.remove(c.id) } else { selected.insert(c.id) }
                            Haptics.lightTap()
                        }) {
                            Image(systemName: selected.contains(c.id) ? "checkmark.circle.fill" : "circle")
                                .foregroundStyle(selected.contains(c.id) ? .tint : .secondary)
                        }.buttonStyle(.plain)
                        VStack(alignment: .leading) {
                            Text(c.name).font(.body)
                            Text(c.number).font(.caption).foregroundStyle(.secondary)
                        }
                        Spacer()
                        Button(role: .destructive) {
                            Task { await ContactsStore.shared.remove(id: c.id); await reload() }
                        } label: { Image(systemName: "trash").foregroundStyle(.red) }
                    }
                }
            }
            .listStyle(.insetGrouped)
            .toolbar {
                ToolbarItem(placement: .navigationBarLeading) { Button("Cancel") { onDone([]) } }
                ToolbarItem(placement: .navigationBarTrailing) { Button("Done") { finish() }.disabled(selected.isEmpty) }
                ToolbarItem(placement: .bottomBar) { Button { showingAdd = true } label: { Label("New Contact", systemImage: "plus.circle.fill") } }
            }
            .navigationTitle("Contacts")
            .onAppear { Task { await reload() } }
            .sheet(isPresented: $showingAdd) { addSheet }
        }
    }

    var addSheet: some View {
        NavigationStack {
            Form {
                TextField("Name", text: $newName)
                TextField("Number", text: $newNumber).keyboardType(.phonePad)
            }
            .toolbar {
                ToolbarItem(placement: .navigationBarLeading) { Button("Close") { showingAdd = false } }
                ToolbarItem(placement: .navigationBarTrailing) {
                    Button("Save") {
                        Task {
                            guard !newName.isEmpty, !newNumber.isEmpty else { return }
                            await ContactsStore.shared.add(name: newName, number: newNumber)
                            newName = ""; newNumber = ""; showingAdd = false
                            await reload()
                            Haptics.success()
                        }
                    }
                }
            }
            .navigationTitle("New Contact")
        }
    }

    func reload() async { contacts = await ContactsStore.shared.list() }
    func finish() {
        let sel = contacts.filter { selected.contains($0.id) }
        onDone(sel)
    }
}

