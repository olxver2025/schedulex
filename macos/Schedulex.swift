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

struct TaskDraft: Sendable {
    let prompt: String
    let workspace: String
    let date: Date
    let afterReset: Bool
    let allowEdits: Bool
    let overnight: Bool
    let idleOnly: Bool
    let model: String
    let effort: String
    let cloud: Bool
    let cloudEnvironment: String

    func arguments(promptFile: String) throws -> [String] {
        guard !prompt.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            throw NSError(domain: "Schedulex", code: 1, userInfo: [NSLocalizedDescriptionKey: "Enter a task prompt."])
        }
        let path = (workspace as NSString).expandingTildeInPath
        if !cloud {
            var isDirectory: ObjCBool = false
            guard FileManager.default.fileExists(atPath: path, isDirectory: &isDirectory), isDirectory.boolValue else {
                throw NSError(domain: "Schedulex", code: 1, userInfo: [NSLocalizedDescriptionKey: "Choose an existing workspace folder."])
            }
        } else if cloudEnvironment.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            throw NSError(domain: "Schedulex", code: 1, userInfo: [NSLocalizedDescriptionKey: "Enter a Codex Cloud environment ID."])
        }
        if !afterReset && date <= Date() {
            throw NSError(domain: "Schedulex", code: 1, userInfo: [NSLocalizedDescriptionKey: "Choose a future date and time."])
        }
        var args = ["add", "--prompt-file", promptFile]
        if cloud {
            args += ["--cloud-env", cloudEnvironment.trimmingCharacters(in: .whitespacesAndNewlines)]
        } else {
            args += ["--cwd", path, "--sandbox", allowEdits ? "workspace-write" : "read-only"]
        }
        args += afterReset ? ["--after-reset"] : ["--at", ISO8601DateFormatter().string(from: date)]
        if overnight { args += ["--window", "23:00-07:00", "--timezone", TimeZone.current.identifier] }
        if idleOnly { args += ["--idle-minutes", "15"] }
        let selectedModel = model.trimmingCharacters(in: .whitespacesAndNewlines)
        let selectedEffort = effort.trimmingCharacters(in: .whitespacesAndNewlines)
        if !selectedModel.isEmpty { args += ["--model", selectedModel] }
        if !selectedEffort.isEmpty { args += ["--effort", selectedEffort] }
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
    var draftAfterReset = false
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

    init() {
        do { config = try Configuration.load() }
        catch { config = nil; self.error = "Run the installer to configure Schedulex." }
    }

    func refresh() async {
        guard !busy, !composing, let config else { return }
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

    func queueDraft() async {
        guard !busy, let config else { return }
        let draft = TaskDraft(prompt: draftPrompt, workspace: draftWorkspace, date: draftDate,
                              afterReset: draftAfterReset, allowEdits: draftAllowEdits,
                              overnight: draftOvernight, idleOnly: draftIdleOnly,
                              model: draftModel, effort: draftEffort, cloud: draftCloud,
                              cloudEnvironment: draftCloudEnvironment)
        busy = true
        draftError = nil
        do {
            try await Task.detached { try Bridge.queue(config, draft) }.value
            draftPrompt = ""
            composing = false
            queuedMessage = "Task queued.\(dashboard?.workerRunning == true ? "" : " Start the worker to run it.")"
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
                    Button("Workspace") { NSWorkspace.shared.open(URL(fileURLWithPath: cwd)) }
                }
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

struct TaskComposer: View {
    @Bindable var model: MenuModel

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Text("Queue a task").font(.headline)
                Spacer()
                Button("Back") { model.composing = false }.disabled(model.busy)
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
                Text("Workspace").font(.subheadline).fontWeight(.medium)
                HStack {
                    TextField("Folder path", text: $model.draftWorkspace)
                        .textFieldStyle(.roundedBorder).accessibilityLabel("Workspace folder")
                    Button("Choose…") {
                        let panel = NSOpenPanel()
                        panel.canChooseDirectories = true
                        panel.canChooseFiles = false
                        panel.allowsMultipleSelection = false
                        panel.prompt = "Choose workspace"
                        panel.begin { response in
                            if response == .OK, let url = panel.url { model.draftWorkspace = url.path }
                        }
                    }
                }
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
            Picker("Schedule", selection: $model.draftAfterReset) {
                Text("At a time").tag(false)
                Text("After 5-hour reset").tag(true)
            }.pickerStyle(.segmented)
            if model.draftAfterReset {
                Text("Uses your next reported reset time and waits for available allowance.")
                    .font(.caption).foregroundStyle(.secondary)
            } else {
                DatePicker("Run at", selection: $model.draftDate, in: Date()...,
                           displayedComponents: [.date, .hourAndMinute])
                Text("Time zone: \(TimeZone.current.identifier)").font(.caption).foregroundStyle(.secondary)
            }
            if !model.draftCloud {
                Toggle("Allow edits in this workspace", isOn: $model.draftAllowEdits)
            }
            Toggle("Only start overnight (23:00–07:00)", isOn: $model.draftOvernight)
            Toggle("Wait for 15 minutes of keyboard/mouse inactivity", isOn: $model.draftIdleOnly)
            if let error = model.draftError {
                Text(error).font(.caption).foregroundStyle(.orange).fixedSize(horizontal: false, vertical: true)
            }
            HStack {
                if model.busy { ProgressView().controlSize(.small) }
                Spacer()
                Button(model.draftCloud ? "Queue Cloud task" : "Queue task") { Task { await model.queueDraft() } }
                    .buttonStyle(.borderedProminent)
                    .disabled(model.busy || model.draftPrompt.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ||
                              (model.draftCloud && model.draftCloudEnvironment.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty))
            }
        }.padding(16).frame(width: 410).disabled(model.busy)
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
