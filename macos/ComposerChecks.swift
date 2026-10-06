// Integration checks for the same bridge and draft used by the menu bar composer.
import Foundation

@main
struct ComposerChecks {
    static func require(_ condition: Bool, _ message: String) throws {
        if !condition { throw NSError(domain: "ComposerChecks", code: 1, userInfo: [NSLocalizedDescriptionKey: message]) }
    }

    static func main() throws {
        let config = try JSONDecoder().decode(Configuration.self,
                    from: Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[1])))
        let prompt = "Multiline café 🦆\n" + String(repeating: "full prompt content\n", count: 10000)
        let draft = TaskDraft(prompt: prompt, workspace: config.source,
                              date: Date().addingTimeInterval(7200), afterReset: false,
                              allowEdits: true, overnight: true, idleOnly: true,
                              model: "example-model", effort: "high")
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
        _ = try Bridge.run(config, ["cancel", job.id])
        let resetDraft = TaskDraft(prompt: "reset task", workspace: config.source, date: .distantPast,
                         afterReset: true, allowEdits: false, overnight: false, idleOnly: false, model: "", effort: "")
        try Bridge.queue(config, resetDraft)
        let after = try JSONDecoder().decode(Dashboard.self, from: Bridge.run(config, ["dashboard"]))
        let resetJob = after.jobs.first { $0.status == "pending" }!
        try require(resetJob.model == nil && resetJob.effort == nil, "Unset overrides must preserve Codex defaults")
        try require(resetJob.due > Date().addingTimeInterval(3500).timeIntervalSince1970, "Reset scheduling must read account reset time")
        _ = try Bridge.run(config, ["cancel", resetJob.id])
        let invalid = TaskDraft(prompt: "  \n", workspace: config.source, date: .distantFuture,
                          afterReset: false, allowEdits: false, overnight: false, idleOnly: false, model: "", effort: "")
        do {
            try Bridge.queue(config, invalid)
            throw NSError(domain: "ComposerChecks", code: 2)
        } catch let error as NSError { try require(error.domain == "Schedulex", "Empty prompts must be rejected") }
        print("PASS: native composer saved full prompts, model/effort, gates, and reset schedules; rejected empty input")
    }
}
