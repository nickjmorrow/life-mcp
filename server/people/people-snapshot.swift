// people-snapshot: copy the Messages and Contacts databases into a private folder so the
// Life connector can read them without Full Disk Access. This binary is the only thing
// that gets Full Disk Access; it only reads those databases and writes the copies.
//
// Default: ~/Library/Messages/chat.db -> chat.db, each AddressBook source -> contacts-<n>.abcddb,
// into ~/Library/Application Support/life-mcp/people (700; files 600).
// Tests: --dest DIR and one or more --src PATH NAME.
import Foundation
import SQLite3

let fm = FileManager.default
let home = fm.homeDirectoryForCurrentUser.path
var dest = home + "/Library/Application Support/life-mcp/people"
var sources: [(String, String)] = []

var args = Array(CommandLine.arguments.dropFirst())
while !args.isEmpty {
    let a = args.removeFirst()
    if a == "--dest", !args.isEmpty { dest = args.removeFirst() }
    else if a == "--src", args.count >= 2 { sources.append((args.removeFirst(), args.removeFirst())) }
}
if sources.isEmpty {
    sources.append((home + "/Library/Messages/chat.db", "chat.db"))
    let ab = home + "/Library/Application Support/AddressBook/Sources"
    let dirs = ((try? fm.contentsOfDirectory(atPath: ab)) ?? []).sorted()
    for (i, d) in dirs.enumerated() {
        sources.append(("\(ab)/\(d)/AddressBook-v22.abcddb", "contacts-\(i + 1).abcddb"))
    }
}

func copy(_ src: String, to dst: String) -> String? {
    guard fm.fileExists(atPath: src) else { return "\(src): not found (or no Full Disk Access)" }
    var s: OpaquePointer?
    guard sqlite3_open_v2(src, &s, SQLITE_OPEN_READONLY, nil) == SQLITE_OK else {
        sqlite3_close(s); return "\(src): can't open (Full Disk Access?)"
    }
    defer { sqlite3_close(s) }
    guard sqlite3_exec(s, "select count(*) from sqlite_master", nil, nil, nil) == SQLITE_OK else {
        return "\(src): can't read: \(String(cString: sqlite3_errmsg(s)))"
    }
    let tmp = dst + ".tmp"
    try? fm.removeItem(atPath: tmp)
    var d: OpaquePointer?
    guard sqlite3_open(tmp, &d) == SQLITE_OK else { sqlite3_close(d); return "\(tmp): can't create" }
    guard let b = sqlite3_backup_init(d, "main", s, "main") else { sqlite3_close(d); return "\(src): backup failed" }
    let rc = sqlite3_backup_step(b, -1)
    sqlite3_backup_finish(b)
    sqlite3_close(d)
    guard rc == SQLITE_DONE else { try? fm.removeItem(atPath: tmp); return "\(src): backup failed (\(rc))" }
    chmod(tmp, 0o600)
    if rename(tmp, dst) != 0 { return "\(dst): rename failed" }
    return nil
}

try? fm.createDirectory(atPath: dest, withIntermediateDirectories: true)
chmod(dest, 0o700)
var failed = 0
for (src, name) in sources {
    if let err = copy(src, to: dest + "/" + name) {
        FileHandle.standardError.write((err + "\n").data(using: .utf8)!)
        failed += 1
    }
}
print("\(ISO8601DateFormatter().string(from: Date())) people-snapshot: \(sources.count - failed)/\(sources.count) copied")
exit(failed == 0 ? 0 : 1)
