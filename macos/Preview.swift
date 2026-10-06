// Compile with -D PREVIEW alongside Schedulex.swift to render the actual panel.
import AppKit
import SwiftUI

@main
struct Preview {
    @MainActor static func main() throws {
        _ = NSApplication.shared
        NSApplication.shared.appearance = NSAppearance(named: .aqua)
        let data = try Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[1]))
        let value = try JSONDecoder().decode(Dashboard.self, from: data)
        let model = MenuModel()
        model.dashboard = value
        model.usage = value.usage
        model.error = value.usageError
        if CommandLine.arguments.contains("--edit"), let job = value.jobs.first {
            model.beginEditing(job)
        }
        if CommandLine.arguments.contains("--composer") {
            model.composing = true
            if CommandLine.arguments.contains("--weekly-reset") { model.draftSchedule = .weeklyReset }
            if CommandLine.arguments.contains("--repeat-weekly") { model.draftRecurrence = "weekly" }
            if CommandLine.arguments.contains("--repeat-five-hour") { model.draftRecurrence = "five-hour" }
            model.draftPrompt = "Review this project and fix the failing tests.\nExplain what changed and how you verified it."
            model.draftWorkspace = FileManager.default.homeDirectoryForCurrentUser.path
            model.draftModel = "example-model"
            model.draftEffort = "high"
            model.catalog = ModelCatalog(models: [ModelOption(id: "example-model", name: "Example model",
                        efforts: ["low", "medium", "high"], defaultEffort: "medium")], defaultModel: "example-model", defaultEffort: "medium")
        }
        // Hosting view rendering includes macOS controls and ScrollView, which
        // ImageRenderer intentionally leaves out. This renders our own view only.
        let view = NSHostingView(rootView: MenuPanel(model: model)
            .environment(\.colorScheme, .light).background(Color(nsColor: .windowBackgroundColor)))
        let height: CGFloat = model.composing ? 740 : 635
        let window = NSWindow(contentRect: NSRect(x: -2000, y: -2000, width: 410, height: height),
                              styleMask: .borderless, backing: .buffered, defer: false)
        window.contentView = view
        view.frame = NSRect(x: 0, y: 0, width: 410, height: height)
        window.orderFront(nil)
        RunLoop.main.run(until: Date().addingTimeInterval(0.5))
        view.layoutSubtreeIfNeeded()
        guard let bitmap = view.bitmapImageRepForCachingDisplay(in: view.bounds) else {
            throw NSError(domain: "SchedulexPreview", code: 1)
        }
        view.cacheDisplay(in: view.bounds, to: bitmap)
        guard let png = bitmap.representation(using: .png, properties: [:]) else {
            throw NSError(domain: "SchedulexPreview", code: 1)
        }
        try png.write(to: URL(fileURLWithPath: CommandLine.arguments[2]))
    }
}
