// Compile with -D PREVIEW alongside Schedulex.swift to render the actual panel.
import AppKit
import SwiftUI

@main
struct Preview {
    @MainActor static func main() throws {
        _ = NSApplication.shared
        let data = try Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[1]))
        let value = try JSONDecoder().decode(Dashboard.self, from: data)
        let model = MenuModel()
        model.dashboard = value
        model.usage = value.usage
        model.error = value.usageError
        // Hosting view rendering includes macOS controls and ScrollView, which
        // ImageRenderer intentionally leaves out. This renders our own view only.
        let view = NSHostingView(rootView: MenuPanel(model: model)
            .environment(\.colorScheme, .light).background(Color(nsColor: .windowBackgroundColor)))
        let window = NSWindow(contentRect: NSRect(x: -2000, y: -2000, width: 410, height: 595),
                              styleMask: .borderless, backing: .buffered, defer: false)
        window.contentView = view
        view.frame = NSRect(x: 0, y: 0, width: 410, height: 595)
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
