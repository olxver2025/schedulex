// Integration checks for the same bridge and draft used by the menu bar composer.
import Foundation

@main
struct ComposerChecks {
    static func require(_ condition: Bool, _ message: String) throws {
        if !condition { throw NSError(domain: "ComposerChecks", code: 1, userInfo: [NSLocalizedDescriptionKey: message]) }
    }

    @MainActor static func main() throws {
        let config = try JSONDecoder().decode(Configuration.self,
                    from: Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[1])))
        let prompt = "Multiline café 🦆\n" + String(repeating: "full prompt content\n", count: 10000)
        let draft = TaskDraft(prompt: prompt, workspace: config.source,
                              date: Date().addingTimeInterval(7200), schedule: .at,
                              allowEdits: true, overnight: true, idleOnly: true,
                              model: "example-model", effort: "high", cloud: false, cloudEnvironment: "")
        try Bridge.queue(config, draft)
        let dashboard = try JSONDecoder().decode(Dashboard.self, from: Bridge.run(config, ["dashboard"]))
        try require(dashboard.jobs.count == 1, "Expected one queued job")
        let job = dashboard.jobs[0]
        try require(job.prompt == prompt && job.status == "pending", "Complete prompt must be saved without executing")
        try require(job.model == "example-model" && job.effort == "high", "Model and effort must survive transport")
        try require(job.window == "23:00-07:00", "Overnight gate must be saved")
        let data = try JSONSerialization.jsonObject(with: Bridge.run(config, ["show", job.id])) as! [String: Any]
        let spec = data["spec"] as! [String: Any]
        try require(spec["sandbox"] as? String == "workspace-write" && spec["idle_minutes"] as? Int == 15,
                    "Write and idle options must be saved")
        let editDraft = TaskDraft(prompt: "edited café 🦆\ncomplete prompt", workspace: config.source,
                              date: Date().addingTimeInterval(10800), schedule: .at,
                              allowEdits: false, overnight: false, idleOnly: false,
                              model: "", effort: "", cloud: false, cloudEnvironment: "",
                              editingID: job.id, revision: job.revision)
        try Bridge.queue(config, editDraft)
        let editedDashboard = try JSONDecoder().decode(Dashboard.self, from: Bridge.run(config, ["dashboard"]))
        let edited = editedDashboard.jobs.first { $0.id == job.id }!
        try require(edited.prompt == editDraft.prompt && edited.model == nil && edited.effort == nil,
                    "Editing must save the prompt and clear model/effort overrides")
        try require(edited.window == nil && edited.idleMinutes == 0 && edited.sandbox == "read-only",
                    "Editing must clear gates and revoke write permission")
        try require(edited.due > job.due + 3000 && editedDashboard.jobs.count == 1,
                    "Rescheduling must update the same job")
        _ = try Bridge.run(config, ["cancel", job.id])
        let resetDraft = TaskDraft(prompt: "reset task", workspace: config.source, date: .distantPast,
                         schedule: .fiveHourReset, allowEdits: false, overnight: false, idleOnly: false, model: "", effort: "",
                         cloud: false, cloudEnvironment: "")
        try Bridge.queue(config, resetDraft)
        let after = try JSONDecoder().decode(Dashboard.self, from: Bridge.run(config, ["dashboard"]))
        let resetJob = after.jobs.first { $0.status == "pending" }!
        try require(resetJob.model == nil && resetJob.effort == nil, "Unset overrides must preserve Codex defaults")
        try require(resetJob.due > Date().addingTimeInterval(3500).timeIntervalSince1970, "Reset scheduling must read account reset time")
        _ = try Bridge.run(config, ["cancel", resetJob.id])
        let weeklyDraft = TaskDraft(prompt: "weekly reset task", workspace: config.source, date: .distantPast,
                         schedule: .weeklyReset, allowEdits: false, overnight: false, idleOnly: false, model: "", effort: "",
                         cloud: false, cloudEnvironment: "")
        try Bridge.queue(config, weeklyDraft)
        let weekly = try JSONDecoder().decode(Dashboard.self, from: Bridge.run(config, ["dashboard"]))
        let weeklyJob = weekly.jobs.first { $0.status == "pending" }!
        try require(weeklyJob.due > Date().addingTimeInterval(86400).timeIntervalSince1970,
                    "Weekly scheduling must use the weekly reset, not the five-hour reset")
        let weeklyData = try JSONSerialization.jsonObject(with: Bridge.run(config, ["show", weeklyJob.id])) as! [String: Any]
        let weeklySpec = weeklyData["spec"] as! [String: Any]
        try require(weeklySpec["schedule"] as? String == "after-weekly-reset", "Weekly schedule must be saved")
        _ = try Bridge.run(config, ["cancel", weeklyJob.id])
        for period in ["five-hour", "weekly"] {
            let repeatDraft = TaskDraft(prompt: "repeat " + period, workspace: config.source, date: .distantPast,
                            schedule: .at, allowEdits: false, overnight: false, idleOnly: false,
                            model: "", effort: "", cloud: false, cloudEnvironment: "", repeatReset: period)
            try Bridge.queue(config, repeatDraft)
            let repeatDashboard = try JSONDecoder().decode(Dashboard.self, from: Bridge.run(config, ["dashboard"]))
            let recurring = repeatDashboard.jobs.first { $0.status == "pending" }!
            try require(recurring.recurrence == period && recurring.resetKnown == true,
                        "Recurring tasks must retain the matching reset period")
            let model = MenuModel()
            model.beginEditing(recurring)
            try require(model.editingID == recurring.id && model.draftRecurrence == period,
                        "Edit form must restore recurring settings")
            let editRepeat = TaskDraft(prompt: "updated recurring prompt", workspace: config.source, date: .distantPast,
                            schedule: .at, allowEdits: false, overnight: false, idleOnly: false,
                            model: "", effort: "", cloud: false, cloudEnvironment: "", repeatReset: period,
                            editingID: recurring.id, revision: recurring.revision)
            try Bridge.queue(config, editRepeat)
            let repeatEditedDashboard = try JSONDecoder().decode(Dashboard.self, from: Bridge.run(config, ["dashboard"]))
            let repeatEdited = repeatEditedDashboard.jobs.first { $0.id == recurring.id }!
            try require(repeatEdited.due == recurring.due, "Editing a recurring prompt must preserve its captured reset")
            _ = try Bridge.run(config, ["cancel", recurring.id])
        }
        let historical = CompletionNotice(id: "old", status: "succeeded", finished: 100, prompt: "old", url: nil, runs: "/tmp")
        let completed = CompletionNotice(id: "new", status: "succeeded", finished: 201, prompt: "new", url: nil, runs: "/tmp")
        let failed = CompletionNotice(id: "failed", status: "failed", finished: 202, prompt: "failed", url: nil, runs: "/tmp")
        let interrupted = CompletionNotice(id: "interrupted", status: "interrupted", finished: 203, prompt: "stopped", url: nil, runs: "/tmp")
        let submitted = CompletionNotice(id: "submitted", status: "submitted", finished: 204, prompt: "cloud", url: nil, runs: "/tmp")
        let notices = [historical, completed, failed, interrupted, submitted]
        let pending = CompletionNotice.pending(notices, since: 200, seen: ["new"])
        try require(pending.map(\.id) == ["failed", "interrupted", "submitted"],
                    "Notifications must exclude historical and already delivered runs")
        try require(submitted.title == "Task submitted to Codex Cloud" && completed.title == "Task completed",
                    "Cloud submission must not be presented as completed execution")
        let invalid = TaskDraft(prompt: "  \n", workspace: config.source, date: .distantFuture,
                          schedule: .at, allowEdits: false, overnight: false, idleOnly: false, model: "", effort: "",
                          cloud: false, cloudEnvironment: "")
        do {
            try Bridge.queue(config, invalid)
            throw NSError(domain: "ComposerChecks", code: 2)
        } catch let error as NSError { try require(error.domain == "Schedulex", "Empty prompts must be rejected") }
        print("PASS: native composer edits, reschedules, both reset recurrences, notification filtering, and input validation")
    }
}
