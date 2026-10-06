import AppKit
import SwiftUI
import Observation
import UserNotifications
import UniformTypeIdentifiers

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
    let cwd: String?
    let note: String
    let window: String?
    let timezone: String?
    let runs: String
    let model: String?
    let effort: String?
    let destination: String?
    let cloudEnv: String?
    let cloudUrl: String?
    let codexUrl: String?
    let canInterrupt: Bool?
    var sandbox: String? = nil
    var idleMinutes: Int? = nil
    var minRemaining: Double? = nil
    var limitId: String? = nil
    var timeout: Int? = nil
    var schedule: String? = nil
    var recurrence: String? = nil
    var resetKnown: Bool? = nil
    var revision: Int? = nil
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
    var completions: [CompletionNotice]? = nil
}

struct CompletionNotice: Codable, Sendable {
    let id: String
    let status: String
    let finished: Double
    let prompt: String
    let url: String?
    let runs: String

    var title: String {
        switch status {
        case "succeeded": "Task completed"
        case "failed": "Task failed"
        case "interrupted": "Task interrupted"
        case "submitted": "Task submitted to Codex Cloud"
        default: "Task finished"
        }
    }

    static func pending(_ notices: [CompletionNotice], since: Double, seen: Set<String>) -> [CompletionNotice] {
        notices.filter { $0.finished > since && !seen.contains($0.id) &&
            ["succeeded", "failed", "interrupted", "submitted"].contains($0.status) }
    }
}

private struct NotificationTarget {
    let fireAt: Date
    let eventAt: Double
    let title: String
    let body: String

    var signature: String {
        "\(Int(eventAt.rounded()))|\(title)|\(body)"
    }
}

@MainActor
private final class NotificationCoordinator: NSObject, UNUserNotificationCenterDelegate {
    private let center = UNUserNotificationCenter.current()
    private let defaults = UserDefaults.standard
    private let storageKey = "schedulex.notificationSignatures"
    private let leadTime: TimeInterval = 10 * 60
    private var isSyncing = false

    override init() {
        super.init()
        center.delegate = self
    }

    nonisolated func userNotificationCenter(_ center: UNUserNotificationCenter,
                                            willPresent notification: UNNotification,
                                            withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void) {
        completionHandler([.banner, .list, .sound])
    }

    nonisolated func userNotificationCenter(_ center: UNUserNotificationCenter,
                                            didReceive response: UNNotificationResponse,
                                            withCompletionHandler completionHandler: @escaping () -> Void) {
        let target = response.notification.request.content.userInfo["target"] as? String
        Task { @MainActor in
            if let target, let url = URL(string: target),
               ["codex", "https", "file"].contains(url.scheme ?? "") {
                NSWorkspace.shared.open(url)
            }
            completionHandler()
        }
    }

    private func syncCompletions(_ dashboard: Dashboard, key: String, since: Double) async {
        let seenKey = "schedulex.completed." + key
        var seen = Set(defaults.stringArray(forKey: seenKey) ?? [])
        for notice in CompletionNotice.pending(dashboard.completions ?? [], since: since, seen: seen) {
            let content = UNMutableNotificationContent()
            content.title = notice.title
            content.body = String((notice.prompt.split(separator: "\n").first.map(String.init) ?? "Scheduled task").prefix(140))
            content.sound = .default
            let folder = FileManager.default.fileExists(atPath: notice.runs) ? notice.runs : dashboard.stateDir
            content.userInfo = ["target": notice.url ?? URL(fileURLWithPath: folder).absoluteString]
            do {
                try await center.add(UNNotificationRequest(identifier: "completion-" + key + "-" + notice.id,
                                                          content: content, trigger: nil))
                seen.insert(notice.id)
                defaults.set(Array(seen), forKey: seenKey)
            } catch {
                // Leave this occurrence unseen so a later refresh can retry delivery.
            }
        }
    }

    func sync(_ dashboard: Dashboard) async {
        guard !isSyncing else { return }
        isSyncing = true
        defer { isSyncing = false }

        let queueKey = Data(dashboard.stateDir.utf8).base64EncodedString()
        let startKey = "schedulex.notificationStart." + queueKey
        // Establish a first-launch baseline without replaying the existing task history.
        if defaults.object(forKey: startKey) == nil {
            defaults.set(dashboard.checkedAt, forKey: startKey)
        }
        let since = defaults.double(forKey: startKey)
        var settings = await center.notificationSettings()
        if settings.authorizationStatus == .notDetermined {
            do {
                _ = try await center.requestAuthorization(options: [.alert, .sound])
            } catch {
                return
            }
            settings = await center.notificationSettings()
        }
        guard settings.authorizationStatus == .authorized ||
                settings.authorizationStatus == .provisional else { return }

        await syncCompletions(dashboard, key: queueKey, since: since)
        let now = Date()
        var targets: [String: NotificationTarget] = [:]
        for job in dashboard.jobs where job.status == "pending" && job.resetKnown != false && job.due > now.timeIntervalSince1970 {
            let due = Date(timeIntervalSince1970: job.due)
            let fireAt = max(now.addingTimeInterval(1), due.addingTimeInterval(-leadTime))
            targets["task-\(job.id)"] = NotificationTarget(
                fireAt: fireAt,
                eventAt: job.due,
                title: "Scheduled task coming up",
                body: "Schedulex is scheduled to start a task at \(due.formatted(date: .omitted, time: .shortened))."
            )
        }

        if let usage = dashboard.usage {
            for window in usage.windows {
                guard let reset = window.resetsAt, reset > now.timeIntervalSince1970 else { continue }
                let resetDate = Date(timeIntervalSince1970: reset)
                let fireAt = max(now.addingTimeInterval(1), resetDate.addingTimeInterval(-leadTime))
                let bucket = window.bucket == "codex" ? "Codex" : window.bucket
                targets["reset-\(window.id)"] = NotificationTarget(
                    fireAt: fireAt,
                    eventAt: reset,
                    title: "\(bucket) \(window.title) reset coming up",
                    body: "Expected usage reset at \(resetDate.formatted(date: .omitted, time: .shortened))."
                )
            }
        }

        var signatures = defaults.dictionary(forKey: storageKey) as? [String: String] ?? [:]
        let identifiers = Set(signatures.keys).union(targets.keys)
        for identifier in identifiers {
            guard let target = targets[identifier] else {
                // Keep reset reminders while Codex usage is temporarily unavailable.
                if identifier.hasPrefix("reset-"), dashboard.usage == nil { continue }
                center.removePendingNotificationRequests(withIdentifiers: [identifier])
                center.removeDeliveredNotifications(withIdentifiers: [identifier])
                signatures.removeValue(forKey: identifier)
                continue
            }
            guard signatures[identifier] != target.signature else { continue }

            center.removePendingNotificationRequests(withIdentifiers: [identifier])
            center.removeDeliveredNotifications(withIdentifiers: [identifier])
            let content = UNMutableNotificationContent()
            content.title = target.title
            content.body = target.body
            content.sound = .default
            let interval = max(1, target.fireAt.timeIntervalSinceNow)
            let request = UNNotificationRequest(
                identifier: identifier,
                content: content,
                trigger: UNTimeIntervalNotificationTrigger(timeInterval: interval, repeats: false)
            )
            do {
                try await center.add(request)
                signatures[identifier] = target.signature
            } catch {
                signatures.removeValue(forKey: identifier)
            }
        }
        defaults.set(signatures, forKey: storageKey)
    }
}

enum TaskSchedule: Hashable, Sendable {
    case at
    case fiveHourReset
    case weeklyReset
}

struct TaskDraft: Sendable {
    let prompt: String
    let workspace: String
    let date: Date
    let schedule: TaskSchedule
    let allowEdits: Bool
    let overnight: Bool
    let idleOnly: Bool
    let model: String
    let effort: String
    let cloud: Bool
    let cloudEnvironment: String
    var repeatReset: String? = nil
    var editingID: String? = nil
    var revision: Int? = nil
    var allowedWindow: String = "23:00-07:00"
    var timezone: String = TimeZone.current.identifier
    var idleMinutes: Int = 15
    var limitId: String = "codex"
    var minRemaining: Double = 1
    var timeout: Int = 7200
    var originalSchedule: TaskSchedule? = nil
    var originalDate: Date? = nil
    var originalRecurrence: String? = nil

    func arguments(promptFile: String) throws -> [String] {
        guard !prompt.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            throw NSError(domain: "Schedulex", code: 1, userInfo: [NSLocalizedDescriptionKey: "Enter a task prompt."])
        }
        let path = (workspace as NSString).expandingTildeInPath
        if !cloud {
            var isDirectory: ObjCBool = false
            guard FileManager.default.fileExists(atPath: path, isDirectory: &isDirectory), isDirectory.boolValue else {
                throw NSError(domain: "Schedulex", code: 1, userInfo: [NSLocalizedDescriptionKey: "Choose an existing Codex project folder."])
            }
        } else if cloudEnvironment.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            throw NSError(domain: "Schedulex", code: 1, userInfo: [NSLocalizedDescriptionKey: "Enter a Codex Cloud environment ID."])
        }
        let keepSchedule = editingID != nil && originalRecurrence == nil && originalSchedule == schedule &&
            (schedule != .at || date == originalDate)
        if schedule == .at && repeatReset == nil && !keepSchedule && date <= Date() {
            throw NSError(domain: "Schedulex", code: 1, userInfo: [NSLocalizedDescriptionKey: "Choose a future date and time."])
        }
        var args = editingID.map { ["edit", $0] } ?? ["add"]
        args += ["--prompt-file", promptFile, "--limit-id", limitId,
                 "--min-remaining", String(minRemaining), "--timeout", String(timeout)]
        if let revision, editingID != nil { args += ["--revision", String(revision)] }
        if cloud {
            args += ["--cloud-env", cloudEnvironment.trimmingCharacters(in: .whitespacesAndNewlines)]
        } else {
            args += ["--project", path]
            if editingID != nil { args += ["--local"] }
        }
        args += ["--sandbox", allowEdits ? "workspace-write" : "read-only"]
        if let repeatReset {
            args += ["--repeat-reset", repeatReset]
        } else if !keepSchedule {
            switch schedule {
            case .at: args += ["--at", ISO8601DateFormatter().string(from: date)]
            case .fiveHourReset: args += ["--after-reset"]
            case .weeklyReset: args += ["--after-weekly-reset"]
            }
        }
        args += ["--timezone", timezone]
        if overnight { args += ["--window", allowedWindow] }
        else if editingID != nil { args += ["--clear-window"] }
        if idleOnly { args += ["--idle-minutes", String(idleMinutes)] }
        else if editingID != nil { args += ["--clear-idle"] }
        let selectedModel = model.trimmingCharacters(in: .whitespacesAndNewlines)
        let selectedEffort = effort.trimmingCharacters(in: .whitespacesAndNewlines)
        if !selectedModel.isEmpty { args += ["--model", selectedModel] }
        else if editingID != nil { args += ["--clear-model"] }
        if !selectedEffort.isEmpty { args += ["--effort", selectedEffort] }
        else if editingID != nil { args += ["--clear-effort"] }
        return args
    }
}

struct ModelOption: Codable, Identifiable, Sendable {
    let id: String
    let name: String
    let efforts: [String]
    let defaultEffort: String?
}

struct ModelCatalog: Codable, Sendable {
    let models: [ModelOption]
    let defaultModel: String?
    let defaultEffort: String?
}

enum Bridge {
    static func queue(_ config: Configuration, _ draft: TaskDraft) throws {
        let file = FileManager.default.temporaryDirectory.appendingPathComponent("schedulex-prompt-\(UUID().uuidString).txt")
        let args = try draft.arguments(promptFile: file.path)
        guard FileManager.default.createFile(atPath: file.path, contents: Data(draft.prompt.utf8),
                                            attributes: [.posixPermissions: 0o600]) else {
            throw NSError(domain: "Schedulex", code: 1, userInfo: [NSLocalizedDescriptionKey: "Could not save the prompt."])
        }
        defer { try? FileManager.default.removeItem(at: file) }
        _ = try run(config, args)
    }

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
    var composing = false
    var draftPrompt = ""
    var draftCloud = false
    var draftCloudEnvironment = ""
    var draftWorkspace = FileManager.default.homeDirectoryForCurrentUser.path
    var draftDate = Date().addingTimeInterval(3600)
    var draftSchedule: TaskSchedule = .at
    var draftRecurrence = ""
    var editingID: String?
    var editingRevision: Int?
    var editingSchedule: TaskSchedule?
    var editingDate: Date?
    var editingRecurrence: String?
    var draftWindow = "23:00-07:00"
    var draftTimezone = TimeZone.current.identifier
    var draftIdleMinutes = 15
    var draftLimitId = "codex"
    var draftMinRemaining: Double = 1
    var draftTimeout = 7200
    var draftAllowEdits = false
    var draftOvernight = false
    var draftIdleOnly = false
    var draftModel = ""
    var draftEffort = ""
    var catalog: ModelCatalog?
    var catalogError: String?
    var loadingModels = false
    var draftError: String?
    var queuedMessage: String?
    private let config: Configuration?
    private let notifications = NotificationCoordinator()

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
            error = value.usageError
            if let current = value.usage {
                usage = current
                usageUpdated = Date(timeIntervalSince1970: value.checkedAt)
            }
            // Local diagnostics let the CLI verify the app's own refresh pipeline.
            try? JSONEncoder().encode(value).write(
                to: URL(fileURLWithPath: config.stateDir).appendingPathComponent("menubar-status.json"), options: .atomic)
            Task { await notifications.sync(value) }
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

    func beginEditing(_ job: ScheduledJob) {
        guard !busy, job.status == "pending" else { return }
        editingID = job.id
        editingRevision = job.revision
        draftPrompt = job.prompt
        draftCloud = job.destination == "cloud"
        draftCloudEnvironment = job.cloudEnv ?? ""
        draftWorkspace = job.cwd ?? FileManager.default.homeDirectoryForCurrentUser.path
        draftDate = max(Date(timeIntervalSince1970: job.due), Date().addingTimeInterval(60))
        draftSchedule = job.schedule == "after-reset" ? .fiveHourReset : (job.schedule == "after-weekly-reset" ? .weeklyReset : .at)
        draftRecurrence = job.recurrence ?? ""
        editingSchedule = draftSchedule
        editingDate = Date(timeIntervalSince1970: job.due)
        editingRecurrence = job.recurrence
        draftAllowEdits = job.sandbox == "workspace-write"
        draftOvernight = job.window != nil
        draftWindow = job.window ?? "23:00-07:00"
        draftTimezone = job.timezone ?? TimeZone.current.identifier
        let idleMinutes = job.idleMinutes ?? 0
        draftIdleOnly = idleMinutes > 0
        draftIdleMinutes = idleMinutes > 0 ? idleMinutes : 15
        draftLimitId = job.limitId ?? "codex"
        draftMinRemaining = job.minRemaining ?? 1
        draftTimeout = job.timeout ?? 7200
        draftModel = job.model ?? ""
        draftEffort = job.effort ?? ""
        draftError = nil
        queuedMessage = nil
        composing = true
    }

    func leaveComposer() {
        composing = false
        if editingID != nil { clearDraft() }
    }

    private func clearDraft() {
        editingID = nil
        editingRevision = nil
        editingSchedule = nil
        editingDate = nil
        editingRecurrence = nil
        draftPrompt = ""
        draftRecurrence = ""
        draftSchedule = .at
        draftCloud = false
        draftCloudEnvironment = ""
        draftWorkspace = FileManager.default.homeDirectoryForCurrentUser.path
        draftDate = Date().addingTimeInterval(3600)
        draftAllowEdits = false
        draftOvernight = false
        draftIdleOnly = false
        draftModel = ""
        draftEffort = ""
        draftWindow = "23:00-07:00"
        draftTimezone = TimeZone.current.identifier
        draftIdleMinutes = 15
        draftLimitId = "codex"
        draftMinRemaining = 1
        draftTimeout = 7200
    }

    func queueDraft() async {
        guard !busy, let config else { return }
        let draft = TaskDraft(prompt: draftPrompt, workspace: draftWorkspace, date: draftDate,
                              schedule: draftSchedule, allowEdits: draftAllowEdits,
                              overnight: draftOvernight, idleOnly: draftIdleOnly,
                              model: draftModel, effort: draftEffort, cloud: draftCloud,
                              cloudEnvironment: draftCloudEnvironment,
                              repeatReset: draftRecurrence.isEmpty ? nil : draftRecurrence,
                              editingID: editingID, revision: editingRevision,
                              allowedWindow: draftWindow, timezone: draftTimezone,
                              idleMinutes: draftIdleMinutes, limitId: draftLimitId,
                              minRemaining: draftMinRemaining, timeout: draftTimeout,
                              originalSchedule: editingSchedule, originalDate: editingDate,
                              originalRecurrence: editingRecurrence)
        busy = true
        draftError = nil
        do {
            try await Task.detached { try Bridge.queue(config, draft) }.value
            let edited = editingID != nil
            clearDraft()
            composing = false
            queuedMessage = "Task \(edited ? "updated" : "queued").\(dashboard?.workerRunning == true ? "" : " Start the worker to run it.")"
            busy = false
            await refresh()
        } catch {
            draftError = error.localizedDescription
            busy = false
        }
    }

    func loadModels() async {
        guard !loadingModels, let config else { return }
        loadingModels = true
        defer { loadingModels = false }
        do {
            catalog = try await Task.detached {
                try JSONDecoder().decode(ModelCatalog.self, from: Bridge.run(config, ["models"]))
            }.value
            catalogError = nil
        } catch { catalogError = "Could not load model choices. You can enter a model and effort manually." }
    }

    var selectedModel: ModelOption? {
        let name = draftModel.isEmpty ? catalog?.defaultModel : draftModel
        return catalog?.models.first { $0.id == name }
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
    var edit: (() -> Void)? = nil
    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            HStack(alignment: .top) {
                Text(job.prompt.split(separator: "\n").first.map(String.init) ?? "Scheduled task")
                    .fontWeight(.medium).lineLimit(2).help(job.prompt)
                Spacer()
                Text(job.status.capitalized).font(.caption)
                    .foregroundStyle(job.status == "failed" || job.status == "interrupted" ? .orange : .secondary)
            }
            Text(job.resetKnown == false ? "Waiting for the next reported reset" : dateLabel(job.due))
                .font(.caption).foregroundStyle(.secondary)
            if let recurrence = job.recurrence {
                Label(recurrence == "weekly" ? "Every weekly reset" : "Every five-hour reset", systemImage: "repeat")
                    .font(.caption).foregroundStyle(.secondary)
            }
            if let environment = job.cloudEnv {
                Text("Codex Cloud · \(environment)").font(.caption).foregroundStyle(.secondary)
            }
            if job.model != nil || job.effort != nil {
                Text("\(job.model ?? "Codex default model") · \(job.effort ?? "default") effort")
                    .font(.caption).foregroundStyle(.secondary)
            }
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
                if let cloudUrl = job.cloudUrl, let url = URL(string: cloudUrl) {
                    Link("Open Cloud task", destination: url)
                } else if let cwd = job.cwd, !cwd.isEmpty {
                    Button("Project folder") { NSWorkspace.shared.open(URL(fileURLWithPath: cwd)) }
                }
                if let codexUrl = job.codexUrl, let url = URL(string: codexUrl) {
                    Link("Open in Codex", destination: url)
                }
                if FileManager.default.fileExists(atPath: job.runs) {
                    Button("Results") { NSWorkspace.shared.open(URL(fileURLWithPath: job.runs)) }
                }
                Spacer()
                if job.status == "pending" {
                    if let edit { Button("Edit", action: edit).disabled(busy) }
                    Button("Cancel", role: .destructive, action: cancel).disabled(busy)
                } else if job.canInterrupt == true {
                    Button("Interrupt", role: .destructive, action: cancel).disabled(busy)
                }
            }.font(.caption).buttonStyle(.borderless)
        }
        .padding(10).background(.quaternary.opacity(0.4), in: RoundedRectangle(cornerRadius: 8))
    }
}

struct TaskComposer: View {
    @Bindable var model: MenuModel
    @State private var showingProjectPicker = false

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Text(model.editingID == nil ? "Queue a task" : "Edit task").font(.headline)
                Spacer()
                Button("Back") { model.leaveComposer() }.disabled(model.busy)
            }
            Text("Prompt").font(.subheadline).fontWeight(.medium)
            TextEditor(text: $model.draftPrompt)
                .font(.body).frame(height: 165).padding(4)
                .background(Color(nsColor: .textBackgroundColor), in: RoundedRectangle(cornerRadius: 6))
                .overlay(RoundedRectangle(cornerRadius: 6).stroke(.quaternary))
                .accessibilityLabel("Complete task prompt")
            Picker("Run in", selection: $model.draftCloud) {
                Text("This Mac").tag(false)
                Text("Codex Cloud").tag(true)
            }.pickerStyle(.segmented)
            if model.draftCloud {
                TextField("Cloud environment ID", text: $model.draftCloudEnvironment)
                    .textFieldStyle(.roundedBorder).accessibilityLabel("Codex Cloud environment ID")
                Text("Find configured environments with `codex cloud`. Tasks run in the selected environment’s GitHub repositories.")
                    .font(.caption).foregroundStyle(.secondary)
            } else {
                Text("Codex project").font(.subheadline).fontWeight(.medium)
                HStack {
                    TextField("Project folder path", text: $model.draftWorkspace)
                        .textFieldStyle(.roundedBorder).accessibilityLabel("Codex project folder")
                    Button("Choose…") { showingProjectPicker = true }
                }
                Text("The selected folder is the task’s project context and working directory.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            HStack {
                Text("Model").frame(width: 50, alignment: .leading)
                TextField(model.catalog?.defaultModel ?? "Codex default", text: $model.draftModel)
                    .textFieldStyle(.roundedBorder)
                    .accessibilityLabel("Model override")
                    .onChange(of: model.draftModel) { _, _ in model.draftEffort = "" }
                Menu("Choose") {
                    Button("Codex default") { model.draftModel = "" }
                    ForEach(model.catalog?.models ?? []) { choice in
                        Button(choice.name) { model.draftModel = choice.id }
                    }
                }.disabled(model.loadingModels)
            }
            HStack {
                Text("Effort").frame(width: 50, alignment: .leading)
                if let choice = model.selectedModel {
                    Picker("Reasoning effort", selection: $model.draftEffort) {
                        Text("Codex default").tag("")
                        ForEach(choice.efforts, id: \.self) { effort in Text(effort.capitalized).tag(effort) }
                    }.labelsHidden().frame(maxWidth: .infinity)
                } else {
                    TextField("Codex default (e.g. high)", text: $model.draftEffort)
                        .textFieldStyle(.roundedBorder).accessibilityLabel("Reasoning effort override")
                }
                if model.loadingModels { ProgressView().controlSize(.small) }
            }
            if let message = model.catalogError {
                Text(message).font(.caption).foregroundStyle(.secondary)
            }
            Picker("Repeat", selection: $model.draftRecurrence) {
                Text("Once").tag("")
                Text("Every 5-hour reset").tag("five-hour")
                Text("Every weekly reset").tag("weekly")
            }
            if !model.draftRecurrence.isEmpty {
                Text("Starts after each reported usage reset. Failed or interrupted runs stop recurrence for review.")
                    .font(.caption).foregroundStyle(.secondary)
            } else {
            Picker("Schedule", selection: $model.draftSchedule) {
                Text("At a time").tag(TaskSchedule.at)
                Text("After 5-hour reset").tag(TaskSchedule.fiveHourReset)
                Text("After weekly reset").tag(TaskSchedule.weeklyReset)
            }.pickerStyle(.menu)
            if model.draftSchedule != .at {
                Text(model.draftSchedule == .weeklyReset
                     ? "Uses your next reported weekly reset time and waits for available allowance."
                     : "Uses your next reported five-hour reset time and waits for available allowance.")
                    .font(.caption).foregroundStyle(.secondary)
            } else {
                DatePicker("Run at", selection: $model.draftDate, in: Date()...,
                           displayedComponents: [.date, .hourAndMinute])
                Text("Time zone: \(TimeZone.current.identifier)").font(.caption).foregroundStyle(.secondary)
            }
            }
            if !model.draftCloud {
                Toggle("Allow edits in this project", isOn: $model.draftAllowEdits)
            }
            Toggle("Only start during \(model.draftWindow) (\(model.draftTimezone))", isOn: $model.draftOvernight)
            Toggle("Wait for \(model.draftIdleMinutes) minute\(model.draftIdleMinutes == 1 ? "" : "s") of keyboard/mouse inactivity", isOn: $model.draftIdleOnly)
            if let error = model.draftError {
                Text(error).font(.caption).foregroundStyle(.orange).fixedSize(horizontal: false, vertical: true)
            }
            HStack {
                if model.busy { ProgressView().controlSize(.small) }
                Spacer()
                Button(model.editingID != nil ? "Save changes" : (model.draftCloud ? "Queue Cloud task" : "Queue task")) { Task { await model.queueDraft() } }
                    .buttonStyle(.borderedProminent)
                    .disabled(model.busy || model.draftPrompt.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ||
                              (model.draftCloud && model.draftCloudEnvironment.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty))
            }
        }.padding(16).frame(width: 410).disabled(model.busy)
            .fileImporter(isPresented: $showingProjectPicker,
                          allowedContentTypes: [.folder],
                          allowsMultipleSelection: false) { result in
                switch result {
                case .success(let urls):
                    if let url = urls.first { model.draftWorkspace = url.path }
                case .failure(let error):
                    if (error as? CocoaError)?.code != .userCancelled {
                        model.draftError = error.localizedDescription
                    }
                }
            }
            .task { await model.loadModels() }
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
        if model.composing {
            TaskComposer(model: model)
        } else {
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
            Button { model.draftError = nil; model.queuedMessage = nil; model.composing = true } label: {
                Label("Queue a task", systemImage: "plus")
                    .frame(maxWidth: .infinity)
            }.buttonStyle(.bordered).disabled(model.busy)
            if let message = model.queuedMessage {
                Text(message).font(.caption).foregroundStyle(.secondary)
            }
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
                        JobView(job: job, busy: model.busy,
                                cancel: { Task { await model.perform([job.status == "running" ? "interrupt" : "cancel", job.id]) } },
                                edit: { model.beginEditing(job) })
                    }
                    if !recent.isEmpty {
                        Text("RECENT TASKS").font(.caption).foregroundStyle(.secondary)
                        ForEach(recent) { job in
                            JobView(job: job, busy: model.busy, cancel: {})
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
        }
        .padding(16).frame(width: 410)
        .task { await model.refresh() }
        }
    }
}

#if !PREVIEW
@main
struct SchedulexApp: App {
    private let model = MenuModel()
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
