import Foundation
import Darwin

@_silgen_name("launch_activate_socket")
private func launchActivateSocket(_ name: UnsafePointer<CChar>,
                                  _ descriptors: UnsafeMutablePointer<UnsafeMutablePointer<Int32>?>,
                                  _ count: UnsafeMutablePointer<Int>) -> Int32

private struct Request: Decodable {
    let action: String
    let job: String
    let due: Double?
}

private struct Response: Encodable {
    let ok: Bool
    let error: String?
}

private let pmset = "/usr/bin/pmset"
private let jobPattern = try! NSRegularExpression(pattern: "^[0-9a-f]{12}$")

private func fail(_ message: String) -> Never {
    FileHandle.standardError.write(Data(("Schedulex power helper: " + message + "\n").utf8))
    exit(1)
}

private func validJob(_ value: String) -> Bool {
    let range = NSRange(value.startIndex..., in: value)
    return jobPattern.firstMatch(in: value, range: range) != nil
}

private func runPMSet(_ arguments: [String], allowFailure: Bool = false) throws {
    let process = Process()
    process.executableURL = URL(fileURLWithPath: pmset)
    process.arguments = arguments
    let errors = Pipe()
    process.standardError = errors
    try process.run()
    process.waitUntilExit()
    let message = String(data: errors.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8)?
        .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
    if process.terminationStatus != 0 && !allowFailure {
        throw NSError(domain: "SchedulexPower", code: Int(process.terminationStatus),
                      userInfo: [NSLocalizedDescriptionKey: message.isEmpty ? "pmset failed" : message])
    }
}

private func eventDate(_ value: Double) -> String {
    let formatter = DateFormatter()
    formatter.locale = Locale(identifier: "en_US_POSIX")
    formatter.timeZone = TimeZone.current
    formatter.dateFormat = "MM/dd/yy HH:mm:ss"
    return formatter.string(from: Date(timeIntervalSince1970: value))
}

private func owner(_ uid: uid_t, _ job: String) -> String {
    "local.schedulex.\(uid).\(job)"
}

private func registryURL(_ uid: uid_t) -> URL {
    URL(fileURLWithPath: "/Library/Application Support/Schedulex/Power", isDirectory: true)
        .appendingPathComponent("\(uid).plist")
}

private func loadRegistry(_ url: URL) -> [String: Double] {
    guard let data = try? Data(contentsOf: url),
          let values = try? PropertyListDecoder().decode([String: Double].self, from: data) else { return [:] }
    return values.filter { validJob($0.key) }
}

private func saveRegistry(_ values: [String: Double], at url: URL) throws {
    let directory = url.deletingLastPathComponent()
    try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true,
                                            attributes: [.posixPermissions: 0o700])
    let data = try PropertyListEncoder().encode(values)
    try data.write(to: url, options: .atomic)
    try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: url.path)
}

private func handle(_ request: Request, uid: uid_t) throws {
    let url = registryURL(uid)
    var registry = loadRegistry(url)
    switch request.action {
    case "schedule":
        guard validJob(request.job) else {
            throw NSError(domain: "SchedulexPower", code: 1,
                          userInfo: [NSLocalizedDescriptionKey: "Invalid job identifier"])
        }
        guard let due = request.due, due.isFinite, due > Date().timeIntervalSince1970 + 10 else {
            throw NSError(domain: "SchedulexPower", code: 2,
                          userInfo: [NSLocalizedDescriptionKey: "Wake time must be at least ten seconds in the future"])
        }
        let eventOwner = owner(uid, request.job)
        let previous = registry[request.job]
        if let previous {
            try runPMSet(["schedule", "cancel", "wake", eventDate(previous), eventOwner], allowFailure: true)
        }
        do {
            try runPMSet(["schedule", "wake", eventDate(due), eventOwner])
            registry[request.job] = due
            try saveRegistry(registry, at: url)
        } catch {
            try? runPMSet(["schedule", "cancel", "wake", eventDate(due), eventOwner], allowFailure: true)
            if let previous {
                try? runPMSet(["schedule", "wake", eventDate(previous), eventOwner], allowFailure: true)
                registry[request.job] = previous
            } else {
                registry.removeValue(forKey: request.job)
            }
            try? saveRegistry(registry, at: url)
            throw error
        }
    case "cancel":
        guard validJob(request.job) else {
            throw NSError(domain: "SchedulexPower", code: 1,
                          userInfo: [NSLocalizedDescriptionKey: "Invalid job identifier"])
        }
        let eventOwner = owner(uid, request.job)
        if let previous = registry.removeValue(forKey: request.job) {
            try runPMSet(["schedule", "cancel", "wake", eventDate(previous), eventOwner], allowFailure: true)
            try saveRegistry(registry, at: url)
        }
    case "cancel-all":
        for (job, due) in registry {
            try runPMSet(["schedule", "cancel", "wake", eventDate(due), owner(uid, job)], allowFailure: true)
        }
        try saveRegistry([:], at: url)
    default:
        throw NSError(domain: "SchedulexPower", code: 3,
                      userInfo: [NSLocalizedDescriptionKey: "Unsupported action"])
    }
}

private func send(_ response: Response, to descriptor: Int32) {
    let data = (try? JSONEncoder().encode(response)) ?? Data("{\"ok\":false}".utf8)
    var output = data
    output.append(0x0a)
    output.withUnsafeBytes { bytes in
        guard let base = bytes.baseAddress else { return }
        _ = Darwin.write(descriptor, base, bytes.count)
    }
}

private func serve(_ descriptor: Int32, authorizedUID: uid_t) {
    var peerUID: uid_t = 0
    var peerGID: gid_t = 0
    guard getpeereid(descriptor, &peerUID, &peerGID) == 0, peerUID == authorizedUID else {
        send(Response(ok: false, error: "Caller is not the configured Schedulex user"), to: descriptor)
        close(descriptor)
        return
    }

    var input = Data()
    var byte: UInt8 = 0
    while input.count < 4096 {
        let count = Darwin.read(descriptor, &byte, 1)
        if count <= 0 || byte == 0x0a { break }
        input.append(byte)
    }
    do {
        let request = try JSONDecoder().decode(Request.self, from: input)
        try handle(request, uid: authorizedUID)
        send(Response(ok: true, error: nil), to: descriptor)
    } catch {
        send(Response(ok: false, error: error.localizedDescription), to: descriptor)
    }
    close(descriptor)
}

guard CommandLine.arguments.count == 2, let rawUID = UInt32(CommandLine.arguments[1]) else {
    fail("expected the configured user ID")
}
let authorizedUID = uid_t(rawUID)
var descriptors: UnsafeMutablePointer<Int32>?
var descriptorCount = 0
let activation = "Listener".withCString {
    launchActivateSocket($0, &descriptors, &descriptorCount)
}
guard activation == 0, let descriptors, descriptorCount > 0 else {
    fail("launchd did not provide the Listener socket (\(activation))")
}
defer { free(descriptors) }

for index in 0..<descriptorCount {
    let listener = descriptors[index]
    while true {
        let client = accept(listener, nil, nil)
        if client < 0 {
            if errno == EINTR { continue }
            fail("accept failed: \(String(cString: strerror(errno)))")
        }
        serve(client, authorizedUID: authorizedUID)
    }
}
