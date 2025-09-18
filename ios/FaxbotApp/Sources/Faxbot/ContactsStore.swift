import Foundation

struct Contact: Identifiable, Codable, Hashable {
    var id: String = UUID().uuidString
    var name: String
    var number: String
    var lastUsed: Date?
}

actor ContactsStore {
    static let shared = ContactsStore()
    private let key = "fax_contacts"
    private init() {}

    func list() -> [Contact] {
        guard let data = UserDefaults.standard.data(forKey: key),
              let arr = try? JSONDecoder().decode([Contact].self, from: data) else { return [] }
        return arr
    }
    func save(_ arr: [Contact]) {
        if let data = try? JSONEncoder().encode(arr) { UserDefaults.standard.set(data, forKey: key) }
    }
    func add(name: String, number: String) {
        var arr = list()
        arr.insert(Contact(name: name, number: number, lastUsed: Date()), at: 0)
        save(arr)
    }
    func remove(id: String) {
        var arr = list()
        arr.removeAll { $0.id == id }
        save(arr)
    }
    func update(contact: Contact) {
        var arr = list()
        if let idx = arr.firstIndex(where: { $0.id == contact.id }) { arr[idx] = contact; save(arr) }
    }
}

