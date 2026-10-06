import AppKit
import SwiftUI
import Observation

struct Configuration: Codable, Sendable {
    let python: String
    let source: String
    let codex: String
    let stateDir: String
    let path: String
    let codexHome: String?

    static func load() throws -> Configuration {
        guard let url = Bundle.main.url(forResource: "configuration", withExtension: "json") else {
            throw NSError(domain: "Schedulex", code: 1)
        }
        return try JSONDecoder().decode(Configuration.self, from: Data(contentsOf: url))
    }
}

struct ScheduledJob: Codable, Identifiable, Sendable {
    let id: String
    let prompt: String
    let status: String
    let due: Double
    let cwd: String
    let note: String
    let window: String?
    let timezone: String?
    let runs: String
}

struct UsageWindow: Codable, Identifiable, Sendable {
    let id: String
    let bucket: String
    let title: String
    let remaining: Double?
    let resetsAt: Double?
}

struct ResetCredit: Codable, Identifiable, Sendable {
    let id: String
    let title: String
    let expiresAt: Double?
    let description: String
}

struct Usage: Codable, Sendable {
    let plan: String
    let windows: [UsageWindow]
    let bankedResets: Int?
    let resetDetails: [ResetCredit]
}

struct Dashboard: Codable, Sendable {
    let jobs: [ScheduledJob]
    let workerRunning: Bool
    let stateDir: String
    let checkedAt: Double
    let usage: Usage?
    let usageError: String?
}

enum Bridge {
    static func run(_ config: Configuration, _ args: [String]) throws -> Data {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: config.python)
        process.arguments = ["-P", "-m", "schedulex", "--state-dir", config.stateDir, "--codex", config.codex] + args
        var environment = ProcessInfo.processInfo.environment
        environment["PYTHONPATH"] = config.source
        environment["PATH"] = config.path
        if let home = config.codexHome { environment["CODEX_HOME"] = home }
        process.environment = environment
        let output = Pipe()
        process.standardOutput = output
        // Errors are small; one pipe avoids blocking on a separate unread stderr pipe.
        process.standardError = output
        try process.run()
        let data = output.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        if process.terminationStatus != 0 {
            throw NSError(domain: "Schedulex", code: Int(process.terminationStatus), userInfo: [
                NSLocalizedDescriptionKey: String(data: data, encoding: .utf8) ?? "Command failed"
            ])
        }
        return data
    }
}

@MainActor @Observable
final class MenuModel {
    var dashboard: Dashboard?
    var usage: Usage?
    var usageUpdated: Date?
    var error: String?
    var busy = false
    private let config: Configuration?

    init() {
        do { config = try Configuration.load() }
        catch { config = nil; self.error = "Run the installer to configure Schedulex." }
    }

    func refresh() async {
        guard !busy, let config else { return }
        busy = true
        defer { busy = false }
        do {
            let value = try await Task.detached {
                try JSONDecoder().decode(Dashboard.self, from: Bridge.run(config, ["dashboard"]))
            }.value
            dashboard = value
            // Local diagnostics let the CLI verify the app's own refresh pipeline.
            try? JSONEncoder().encode(value).write(
                to: URL(fileURLWithPath: config.stateDir).appendingPathComponent("menubar-status.json"), options: .atomic)
            error = value.usageError
            if let current = value.usage {
                usage = current
                usageUpdated = Date(timeIntervalSince1970: value.checkedAt)
            }
        } catch { self.error = error.localizedDescription }
    }

    func perform(_ args: [String]) async {
        guard !busy, let config else { return }
        busy = true
        do {
            _ = try await Task.detached { try Bridge.run(config, args) }.value
            error = nil
            busy = false
            await refresh()
        } catch {
            self.error = error.localizedDescription
            busy = false
        }
    }
}

func dateLabel(_ timestamp: Double) -> String {
    Date(timeIntervalSince1970: timestamp).formatted(date: .abbreviated, time: .shortened)
}

struct AllowanceView: View {
    let window: UsageWindow
    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            HStack {
                Text(window.title).fontWeight(.medium)
                Spacer()
                Text(window.remaining.map { "\(Int($0.rounded()))% remaining" } ?? "Unavailable")
                    .monospacedDigit().foregroundStyle(.secondary)
            }
            if let percent = window.remaining {
                ProgressView(value: percent, total: 100)
                    .tint(percent <= 10 ? .orange : .accentColor)
                    .accessibilityLabel("\(window.title): \(Int(percent)) percent remaining")
            }
            if let reset = window.resetsAt {
                HStack {
                    Text("Resets \(dateLabel(reset))")
                    Spacer()
                    if reset > Date().timeIntervalSince1970 {
                        Text(Date(timeIntervalSince1970: reset), style: .relative)
                    } else { Text("Refreshing…") }
                }
                .font(.caption).foregroundStyle(.secondary)
            } else {
                Text("Reset time unavailable").font(.caption).foregroundStyle(.secondary)
            }
        }
    }
}

struct JobView: View {
    let job: ScheduledJob
    let busy: Bool
    let cancel: () -> Void
    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            HStack(alignment: .top) {
                Text(job.prompt.split(separator: "\n").first.map(String.init) ?? "Scheduled task")
                    .fontWeight(.medium).lineLimit(2).help(job.prompt)
                Spacer()
                Text(job.status.capitalized).font(.caption)
                    .foregroundStyle(job.status == "failed" || job.status == "interrupted" ? .orange : .secondary)
            }
            Text(dateLabel(job.due)).font(.caption).foregroundStyle(.secondary)
            if let window = job.window {
                Text("Allowed hours \(window) · \(job.timezone ?? "")")
                    .font(.caption).foregroundStyle(.secondary)
            }
            if !job.note.isEmpty {
                Text(job.note).font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            }
            HStack {
                Button("Copy prompt") {
                    NSPasteboard.general.clearContents()
                    NSPasteboard.general.setString(job.prompt, forType: .string)
                }
                Button("Workspace") { NSWorkspace.shared.open(URL(fileURLWithPath: job.cwd)) }
                if FileManager.default.fileExists(atPath: job.runs) {
                    Button("Results") { NSWorkspace.shared.open(URL(fileURLWithPath: job.runs)) }
                }
                Spacer()
                if job.status == "pending" {
                    Button("Cancel", role: .destructive, action: cancel).disabled(busy)
                }
            }.font(.caption).buttonStyle(.borderless)
        }
        .padding(10).background(.quaternary.opacity(0.4), in: RoundedRectangle(cornerRadius: 8))
    }
}

struct MenuPanel: View {
    let model: MenuModel
    private var active: [ScheduledJob] {
        model.dashboard?.jobs.filter { $0.status == "pending" || $0.status == "running" } ?? []
    }
    private var recent: [ScheduledJob] {
        model.dashboard?.jobs.filter { $0.status != "pending" && $0.status != "running" } ?? []
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Label("Schedulex", systemImage: "clock.badge.checkmark").font(.headline)
                Spacer()
                if model.busy { ProgressView().controlSize(.small) }
                Button { Task { await model.refresh() } } label: { Image(systemName: "arrow.clockwise") }
                    .buttonStyle(.borderless).disabled(model.busy).help("Refresh tasks and usage")
                    .accessibilityLabel("Refresh")
            }
            HStack {
                Circle().fill(model.dashboard?.workerRunning == true ? Color.green : Color.orange)
                    .frame(width: 7, height: 7)
                Text(model.dashboard?.workerRunning == true ? "Worker running" : "Worker stopped")
                Spacer()
                Button(model.dashboard?.workerRunning == true ? "Stop worker" : "Start worker") {
                    Task { await model.perform(["service", model.dashboard?.workerRunning == true ? "uninstall" : "install"]) }
                }.disabled(model.busy).buttonStyle(.borderless)
            }.font(.caption)
            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    if let usage = model.usage {
                        Text("USAGE · \(usage.plan.uppercased())").font(.caption).foregroundStyle(.secondary)
                        ForEach(usage.windows) { window in
                            if window.bucket != "codex" {
                                Text(window.bucket).font(.caption).foregroundStyle(.secondary)
                            }
                            AllowanceView(window: window)
                        }
                        Divider()
                        HStack {
                            Label("Banked resets", systemImage: "arrow.counterclockwise.circle")
                            Spacer()
                            Text(usage.bankedResets.map(String.init) ?? "Unavailable").fontWeight(.semibold)
                        }
                        ForEach(usage.resetDetails) { credit in
                            VStack(alignment: .leading, spacing: 3) {
                                Text(credit.title).font(.caption)
                                if let expiry = credit.expiresAt {
                                    Text("Expires \(dateLabel(expiry))").font(.caption).foregroundStyle(.secondary)
                                }
                            }.help(credit.description)
                        }
                        Text("Resets are displayed only. Shared subscription limits apply.")
                            .font(.caption).foregroundStyle(.secondary)
                    } else if !model.busy {
                        Text("Usage unavailable. Sign in using codex login.").font(.caption)
                    }
                    if let error = model.error {
                        Text(error).font(.caption).foregroundStyle(.orange)
                            .fixedSize(horizontal: false, vertical: true)
                        if let updated = model.usageUpdated {
                            Text("Showing last usage check: \(updated.formatted(date: .abbreviated, time: .shortened))")
                                .font(.caption).foregroundStyle(.secondary)
                        }
                    }
                    Divider()
                    Text("SCHEDULED TASKS · \(active.count)").font(.caption).foregroundStyle(.secondary)
                    if active.isEmpty { Text("No scheduled tasks").foregroundStyle(.secondary) }
                    ForEach(active) { job in
                        JobView(job: job, busy: model.busy) { Task { await model.perform(["cancel", job.id]) } }
                    }
                    if !recent.isEmpty {
                        Text("RECENT TASKS").font(.caption).foregroundStyle(.secondary)
                        ForEach(recent) { job in
                            JobView(job: job, busy: model.busy) { }
                        }
                    }
                }.frame(maxWidth: .infinity, alignment: .leading)
            }.frame(maxHeight: 510)
            Divider()
            HStack {
                Button("Open queue folder") {
                    if let path = model.dashboard?.stateDir { NSWorkspace.shared.open(URL(fileURLWithPath: path)) }
                }.disabled(model.dashboard == nil)
                Spacer()
                Button("Quit") { NSApplication.shared.terminate(nil) }
            }.buttonStyle(.borderless).font(.caption)
            Text("Schedule in Terminal with schx add or schedulex add.")
                .font(.caption).foregroundStyle(.secondary)
        }
        .padding(16).frame(width: 410)
        .task { await model.refresh() }
    }
}

#if !PREVIEW
@main
struct SchedulexApp: App {
    @State private var model = MenuModel()
    var body: some Scene {
        MenuBarExtra("Schedulex", systemImage: "clock.badge.checkmark") {
            MenuPanel(model: model)
        }.menuBarExtraStyle(.window)
    }

    init() {
        // Refresh in the background even while the panel is closed.
        let observer = model
        Task { @MainActor in
            await observer.refresh()
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(60))
                await observer.refresh()
            }
        }
    }
}
#endif
