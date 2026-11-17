import AppKit
import UserNotifications

struct WorkspaceViewRecord: Codable {
    var id = UUID().uuidString
    var frame: String?
    var sessionID: String?
    var page: String?
    mutating func update(_ state: [String: Any]) {
        let sid = state["session_id"] as? String ?? ""
        sessionID = sid.range(of: "^[A-Za-z0-9_-]{0,200}$", options: .regularExpression) != nil ? sid : nil
        let value = state["page"] as? String ?? ""
        page = value.range(of: "^[a-z_-]{0,40}$", options: .regularExpression) != nil ? value : nil
    }
}
struct WorkspaceRecord: Codable {
    var path: String
    var views: [WorkspaceViewRecord]
    var closedViews: [WorkspaceViewRecord] = []
}

/// Stores navigation metadata only. Credentials and backend addresses are never persisted here.
final class WorkspaceRegistry {
    let defaults: UserDefaults
    init(defaults: UserDefaults = .standard) { self.defaults = defaults }
    var documentsDirectory: URL {
        FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Documents", isDirectory: true)
    }
    var preferredWorkspace: String? {
        let saved = defaults.string(forKey: "xiaoyu.desktop.preferredWorkspace")
        if saved?.hasPrefix("/") == true { return saved }
        return defaults.bool(forKey: "xiaoyu.desktop.defaultWorkspaceInitialized") ? nil : recent.first
    }
    var chooserDirectory: URL {
        preferredWorkspace.map { URL(fileURLWithPath: $0, isDirectory: true) } ?? documentsDirectory
    }
    func prepareDefaultWorkspace(documents: URL? = nil) throws -> URL {
        if let saved = preferredWorkspace {
            var directory: ObjCBool = false
            guard FileManager.default.fileExists(atPath: saved, isDirectory: &directory), directory.boolValue else {
                throw NSError(domain: "DesktopWorkspace", code: 1, userInfo: [NSLocalizedDescriptionKey: "上次选择的目录已移动或不可访问，请重新选择文件夹。"])
            }
            return URL(fileURLWithPath: saved, isDirectory: true)
        }
        let url = (documents ?? documentsDirectory).appendingPathComponent("羽案/Default", isDirectory: true)
        try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        defaults.set(true, forKey: "xiaoyu.desktop.defaultWorkspaceInitialized")
        return url
    }
    func selectWorkspace(_ path: String) { defaults.set(path, forKey: "xiaoyu.desktop.preferredWorkspace") }
    static func canonical(_ url: URL) -> String { url.standardizedFileURL.resolvingSymlinksInPath().path }
    static func sameDirectory(_ first: String, _ second: String) -> Bool {
        let lhs = canonical(URL(fileURLWithPath: first)), rhs = canonical(URL(fileURLWithPath: second))
        if lhs == rhs { return true }
        let a = try? FileManager.default.attributesOfItem(atPath: lhs)
        let b = try? FileManager.default.attributesOfItem(atPath: rhs)
        guard let an = a?[.systemFileNumber] as? NSNumber, let bn = b?[.systemFileNumber] as? NSNumber,
              let ad = a?[.systemNumber] as? NSNumber, let bd = b?[.systemNumber] as? NSNumber else { return false }
        return an == bn && ad == bd
    }
    var restored: [WorkspaceRecord] { read("xiaoyu.desktop.workspaces") }
    private func read(_ key: String) -> [WorkspaceRecord] {
        guard let data = defaults.data(forKey: key), data.count <= 1_048_576,
              let records = try? JSONDecoder().decode([WorkspaceRecord].self, from: data) else { return [] }
        var accepted: [WorkspaceRecord] = []
        var identifiers = Set<String>()
        for var record in records.prefix(32) {
            guard record.path.hasPrefix("/"), record.path.count < 8192 else { continue }
            record.path = Self.canonical(URL(fileURLWithPath: record.path))
            guard !accepted.contains(where: { Self.sameDirectory($0.path, record.path) }) else { continue }
            func sanitize(_ entries: [WorkspaceViewRecord]) -> [WorkspaceViewRecord] {
                entries.compactMap { view in
                    guard let id = UUID(uuidString: view.id)?.uuidString,
                          identifiers.insert(id).inserted else { return nil }
                    var clean = view; clean.id = id
                    clean.update(["session_id": view.sessionID ?? "", "page": view.page ?? ""])
                    if (clean.frame?.count ?? 0) > 300 { clean.frame = nil }
                    return clean
                }
            }
            record.views = sanitize(Array(record.views.prefix(16)))
            record.closedViews = sanitize(Array(record.closedViews.prefix(100)))
            if record.views.isEmpty { record.views = [WorkspaceViewRecord()] }
            accepted.append(record)
        }
        return accepted
    }
    func save(_ records: [WorkspaceRecord]) {
        if let data = try? JSONEncoder().encode(records) { defaults.set(data, forKey: "xiaoyu.desktop.workspaces") }
    }
    func archive(_ record: WorkspaceRecord) {
        let saved = read("xiaoyu.desktop.workspaceArchive").filter { !Self.sameDirectory($0.path, record.path) }
        if let data = try? JSONEncoder().encode(Array(([record] + saved).prefix(32))) {
            defaults.set(data, forKey: "xiaoyu.desktop.workspaceArchive")
        }
    }
    func archived(_ path: String) -> WorkspaceRecord? {
        read("xiaoyu.desktop.workspaceArchive").first { Self.sameDirectory($0.path, path) }
    }
    var recent: [String] { Array((defaults.stringArray(forKey: "xiaoyu.desktop.recentWorkspaces") ?? []).filter { $0.hasPrefix("/") }.prefix(12)) }
    func remember(_ path: String) {
        defaults.set(Array(([path] + recent.filter { !Self.sameDirectory($0, path) }).prefix(12)), forKey: "xiaoyu.desktop.recentWorkspaces")
    }
}

final class DesktopApp: NSObject, NSApplicationDelegate, UNUserNotificationCenterDelegate, NSMenuDelegate, NSMenuItemValidation {
    private(set) var workspaces: [WorkbenchController] = []
    private(set) var isQuitting = false
    private var restoring = false
    private let registry = WorkspaceRegistry()
    private let recentMenu = NSMenu(title: "打开最近的工作台")
    private let windowMenu = NSMenu(title: "窗口")
    private var active: WorkbenchController? {
        if let key = NSApp.keyWindow, let owner = workspaces.first(where: { $0.windows.contains(key) }) { return owner }
        if let main = NSApp.mainWindow, let owner = workspaces.first(where: { $0.windows.contains(main) }) { return owner }
        return workspaces.last
    }
    func applicationDidFinishLaunching(_ notification: Notification) {
        // Preserve navigation and model choices when moving from the local bundle identity.
        if Bundle.main.bundleIdentifier == "click.openapi.xiaoyu" {
            let defaults = UserDefaults.standard
            let marker = "xiaoyu.desktop.legacyPreferencesImported"
            if !defaults.bool(forKey: marker) {
                for (key, value) in defaults.persistentDomain(forName: "local.xiaoyu.desktop-lab") ?? [:] {
                    if key.hasPrefix("xiaoyu.desktop."), defaults.object(forKey: key) == nil {
                        defaults.set(value, forKey: key)
                    }
                }
                defaults.set(true, forKey: marker)
            }
        }
        NSApp.setActivationPolicy(.regular)
        buildMenu(); UNUserNotificationCenter.current().delegate = self
        let showSetup = ProcessInfo.processInfo.arguments.contains("--setup")
        restoring = true
        for record in showSetup ? [] : registry.restored {
            let controller = WorkbenchController(); workspaces.append(controller)
            controller.start(coordinator: self, restored: record, autoStart: true)
        }
        restoring = false
        if workspaces.isEmpty { createWorkspace(autoStart: !showSetup, showSetup: showSetup) }
        NSApp.activate(ignoringOtherApps: true)
    }
    func persist() {
        guard !restoring, !isQuitting else { return }
        registry.save(workspaces.compactMap(\.restoration))
    }
    func archive(_ controller: WorkbenchController) { if let record = controller.restoration { registry.archive(record) } }
    func remove(_ controller: WorkbenchController) {
        workspaces.removeAll { $0 === controller }; persist()
    }
    @objc func newWorkspace() { createWorkspace(autoStart: false) }
    private func createWorkspace(autoStart: Bool, showSetup: Bool = false) {
        guard !isQuitting else { return }
        let controller = WorkbenchController(); workspaces.append(controller)
        controller.start(coordinator: self, autoStart: autoStart, showSetup: showSetup)
    }
    var chooserDirectory: URL { registry.chooserDirectory }
    var defaultWorkspaceDescription: String { registry.preferredWorkspace ?? "~/Documents/羽案/Default" }
    func openDefault(from source: WorkbenchController) {
        guard !isQuitting else { return }
        do { open(try registry.prepareDefaultWorkspace(), from: source, rememberSelection: false) }
        catch {
            let alert = NSAlert(); alert.messageText = "暂时无法打开工作目录"
            alert.informativeText = error.localizedDescription
            alert.addButton(withTitle: "选择其他目录"); alert.addButton(withTitle: "取消")
            if alert.runModal() == .alertFirstButtonReturn { source.chooseWorkspace() }
        }
    }
    @objc private func openFolder() { if active == nil { newWorkspace() }; active?.chooseWorkspace() }
    func open(_ url: URL, from source: WorkbenchController?, rememberSelection: Bool = true) {
        guard !isQuitting else { return }
        let path = WorkspaceRegistry.canonical(url)
        var isDirectory: ObjCBool = false
        guard FileManager.default.fileExists(atPath: path, isDirectory: &isDirectory), isDirectory.boolValue else {
            let alert = NSAlert(); alert.messageText = "文件夹已移动或不可访问"; alert.informativeText = path; alert.runModal(); return
        }
        if let existing = workspaces.first(where: { $0.mode.map { WorkspaceRegistry.sameDirectory($0, path) } == true }) {
            if rememberSelection { registry.selectWorkspace(path) }
            if let source = source, source !== existing, source.mode == nil && !source.ownsBackend { source.closeAllWindows(); remove(source) }
            existing.showWindow(); return
        }
        if let overlap = workspaces.first(where: { controller in
            guard let other = controller.mode, !other.isEmpty else { return false }
            return path.hasPrefix(other == "/" ? "/" : other + "/") || other.hasPrefix(path == "/" ? "/" : path + "/")
        }) {
            let alert = NSAlert(); alert.messageText = "两个工作台的文件范围有重叠"
            alert.informativeText = "此文件夹与“\(overlap.displayName)”包含相同文件。两个工作台的任务仍然独立，但可能同时修改这些文件。"
            alert.addButton(withTitle: "仍然打开"); alert.addButton(withTitle: "取消")
            guard alert.runModal() == .alertFirstButtonReturn else { return }
        }
        let controller: WorkbenchController
        if let source = source, source.mode == nil && !source.ownsBackend && registry.archived(path) == nil { controller = source }
        else {
            controller = WorkbenchController(); workspaces.append(controller)
            controller.start(coordinator: self, restored: registry.archived(path))
            if let source = source, source.mode == nil && !source.ownsBackend { source.closeAllWindows(); remove(source) }
        }
        if rememberSelection { registry.selectWorkspace(path) }
        registry.remember(path); controller.openWorkspace(path); persist()
    }
    @objc private func openRecent(_ item: NSMenuItem) {
        if let path = item.representedObject as? String { open(URL(fileURLWithPath: path), from: active) }
    }
    @objc private func closeWorkspace() { active?.closeWorkspace() }
    @objc private func restoreView() { active?.restoreClosedView() }
    @objc private func newView() { active?.newWindow() }
    @objc private func showWindow() { active?.showWindow() }
    @objc private func settings() { active?.openSettings() }
    @objc private func about() { if active == nil { newWorkspace() }; active?.about() }
    @objc private func reload() { active?.reload() }
    @objc private func restart() { active?.restart() }
    @objc private func fixture() {
        guard !isQuitting else { return }
        if let existing = workspaces.first(where: { $0.mode == "" }) { existing.showWindow(); return }
        newWorkspace(); workspaces.last?.openFixture()
    }
    @objc private func selectWindow(_ item: NSMenuItem) { (item.representedObject as? NSWindow)?.makeKeyAndOrderFront(nil) }
    private func add(_ menu: NSMenu, _ title: String, _ action: Selector, _ key: String = "", _ symbol: String? = nil) {
        let item = menu.addItem(withTitle: title, action: action, keyEquivalent: key)
        item.target = self
        if let symbol = symbol { item.image = NSImage(systemSymbolName: symbol, accessibilityDescription: nil) }
    }
    private func buildMenu() {
        let main = NSMenu()
        func section(_ title: String) -> NSMenu {
            let item = NSMenuItem(); let menu = NSMenu(title: title); item.submenu = menu; main.addItem(item); return menu
        }
        let app = section("羽案")
        add(app, "关于羽案", #selector(about), "", "info.circle")
        add(app, "设置…", #selector(settings), ",", "slider.horizontal.3")
        app.addItem(.separator()); app.addItem(withTitle: "退出羽案", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        let file = section("文件")
        add(file, "新建工作台", #selector(newWorkspace), "N", "plus.rectangle")
        add(file, "打开文件夹…", #selector(openFolder), "o", "folder")
        let recent = file.addItem(withTitle: "打开最近的工作台", action: nil, keyEquivalent: "")
        recent.submenu = recentMenu; recent.image = NSImage(systemSymbolName: "clock", accessibilityDescription: nil); recentMenu.delegate = self
        file.addItem(.separator()); add(file, "关闭工作台…", #selector(closeWorkspace), "", "xmark.rectangle")
        let edit = section("编辑")
        for (title, selector, key) in [("撤销", "undo:", "z"), ("重做", "redo:", "Z"), ("剪切", "cut:", "x"), ("复制", "copy:", "c"), ("粘贴", "paste:", "v"), ("全选", "selectAll:", "a")] {
            let item = edit.addItem(withTitle: title, action: Selector(selector), keyEquivalent: key)
            let icons = ["撤销": "arrow.uturn.backward", "重做": "arrow.uturn.forward", "剪切": "scissors", "复制": "doc.on.doc", "粘贴": "doc.on.clipboard", "全选": "checkmark.rectangle"]
            item.image = NSImage(systemSymbolName: icons[title]!, accessibilityDescription: nil)
        }
        let work = section("工作台")
        add(work, "显示工作台", #selector(showWindow), "0", "macwindow")
        add(work, "重新连接界面", #selector(reload), "r", "arrow.clockwise")
        add(work, "重新启动此工作台服务…", #selector(restart), "", "arrow.triangle.2.circlepath")
        work.addItem(.separator()); add(work, "打开示例工作台", #selector(fixture), "", "play.rectangle")
        let windows = NSMenuItem(); windows.submenu = windowMenu; main.addItem(windows); windowMenu.delegate = self
        menuNeedsUpdate(windowMenu)
        NSApp.mainMenu = main
    }
    func validateMenuItem(_ item: NSMenuItem) -> Bool {
        guard !isQuitting else { return false }
        switch item.action {
        case #selector(newView), #selector(reload), #selector(settings): return active?.hasConnectedView == true && active?.lifecycleBusy == false
        case #selector(restoreView): return active?.hasConnectedView == true && active?.hasClosedViews == true && active?.lifecycleBusy == false
        case #selector(restart): return active?.mode != nil && active?.lifecycleBusy == false
        case #selector(closeWorkspace), #selector(showWindow): return active != nil && active?.lifecycleBusy == false
        default: return true
        }
    }
    func menuNeedsUpdate(_ menu: NSMenu) {
        menu.removeAllItems()
        if menu === recentMenu {
            for path in registry.recent {
                let item = menu.addItem(withTitle: path, action: #selector(openRecent(_:)), keyEquivalent: "")
                item.target = self; item.representedObject = path; item.image = NSImage(systemSymbolName: "folder", accessibilityDescription: nil)
            }
            if menu.items.isEmpty { menu.addItem(withTitle: "暂无最近的工作台", action: nil, keyEquivalent: "") }
        } else {
            add(menu, "此工作台的新视图", #selector(newView), "", "macwindow.badge.plus")
            add(menu, "恢复关闭的视图", #selector(restoreView), "T", "arrow.uturn.backward")
            menu.items.last?.isEnabled = active?.hasClosedViews == true
            let minimize = menu.addItem(withTitle: "最小化", action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m")
            minimize.image = NSImage(systemSymbolName: "minus.rectangle", accessibilityDescription: nil)
            let close = menu.addItem(withTitle: "关闭窗口", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
            close.image = NSImage(systemSymbolName: "xmark.rectangle", accessibilityDescription: nil)
            menu.addItem(.separator())
            for controller in workspaces {
                for (index, window) in controller.windows.enumerated() {
                    let item = menu.addItem(withTitle: "\(controller.displayName) · \(index + 1)", action: #selector(selectWindow(_:)), keyEquivalent: "")
                    item.target = self; item.representedObject = window
                    item.state = window.isKeyWindow ? .on : .off
                    item.image = NSImage(systemSymbolName: "macwindow", accessibilityDescription: nil)
                }
            }
        }
    }
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        if !flag && !isQuitting { if workspaces.isEmpty { newWorkspace() } else { active?.showWindow() } }; return true
    }
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard !isQuitting else { return .terminateLater }
        guard !workspaces.isEmpty else { return .terminateNow }
        if workspaces.contains(where: { $0.lifecycleBusy }) {
            let alert = NSAlert(); alert.messageText = "正在保存或关闭工作台"
            alert.informativeText = "请等待当前操作完成后再退出。"; alert.runModal(); return .terminateCancel
        }
        isQuitting = true
        let targets = workspaces
        targets.forEach { $0.setQuitting(true) }
        var checking = targets
        func cancel() { self.isQuitting = false; targets.forEach { $0.setQuitting(false) }; sender.reply(toApplicationShouldTerminate: false) }
        func saveNext(_ pending: [WorkbenchController]) {
            guard let first = pending.first else {
                self.registry.save(targets.compactMap(\.restoration))
                targets.forEach { self.archive($0) }
                targets.forEach { $0.setQuitting(true) }
                var remaining = targets.count
                for controller in targets { controller.closeInterfaceAndStop { remaining -= 1; if remaining == 0 { sender.reply(toApplicationShouldTerminate: true) } } }
                return
            }
            first.saveApplicationState { saved in if saved { saveNext(Array(pending.dropFirst())) } else { cancel() } }
        }
        func checkNext() {
            guard !checking.isEmpty else { saveNext(targets); return }
            checking.removeFirst().confirmStop { allowed in if allowed { checkNext() } else { cancel() } }
        }
        // Return terminateLater before a synchronous no-backend check can reply.
        DispatchQueue.main.async { checkNext() }
        return .terminateLater
    }
    func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse, withCompletionHandler completion: @escaping () -> Void) {
        DispatchQueue.main.async {
            let info = response.notification.request.content.userInfo
            if let path = info["workspace"] as? String, let sid = info["session_id"] as? String,
               let controller = self.workspaces.first(where: { $0.mode == path }) { controller.openSession(sid) }
            completion()
        }
    }
    func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification, withCompletionHandler completion: @escaping (UNNotificationPresentationOptions) -> Void) { completion([.banner, .sound]) }
}
