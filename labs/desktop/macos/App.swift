import AppKit
import WebKit
import UserNotifications
import Darwin

private final class BrowserPane: NSObject, WKNavigationDelegate, WKUIDelegate {
    let view: WKWebView
    let blockedOrigin: URL
    var lastError = ""
    init(blockedOrigin: URL) {
        self.blockedOrigin = blockedOrigin
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .nonPersistent()
        configuration.preferences.javaScriptCanOpenWindowsAutomatically = false
        // No scripts, credentials, message handlers or application bridge in this container.
        self.view = WKWebView(frame: .zero, configuration: configuration)
        super.init()
        view.navigationDelegate = self
        view.uiDelegate = self
        view.isHidden = true
    }
    func allowed(_ url: URL) -> Bool {
        guard ["http", "https"].contains(url.scheme ?? ""), url.host != nil,
              url.user == nil, url.password == nil else { return false }
        return !(url.scheme == blockedOrigin.scheme && url.host == blockedOrigin.host && url.port == blockedOrigin.port)
    }
    func state() -> [String: Any] {
        ["url": view.url?.absoluteString ?? "", "title": view.title ?? "网页", "loading": view.isLoading,
         "can_go_back": view.canGoBack, "can_go_forward": view.canGoForward, "error": lastError,
         "model_access": false, "ephemeral": true]
    }
    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = action.request.url, allowed(url) else {
            lastError = "此地址不能在网页工作区打开。"; decisionHandler(.cancel); return
        }
        if action.targetFrame == nil { webView.load(URLRequest(url: url)); decisionHandler(.cancel); return }
        lastError = ""; decisionHandler(.allow)
    }
    func webView(_ webView: WKWebView, decidePolicyFor response: WKNavigationResponse,
                 decisionHandler: @escaping (WKNavigationResponsePolicy) -> Void) {
        let disposition = (response.response as? HTTPURLResponse)?.value(forHTTPHeaderField: "Content-Disposition") ?? ""
        if !response.canShowMIMEType || disposition.lowercased().hasPrefix("attachment") {
            lastError = "此网页请求下载文件；网页工作区暂不接收下载。"; decisionHandler(.cancel)
        } else { decisionHandler(.allow) }
    }
    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        lastError = error.localizedDescription
    }
    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        lastError = error.localizedDescription
    }
    func webView(_ webView: WKWebView, requestMediaCapturePermissionFor origin: WKSecurityOrigin,
                 initiatedByFrame frame: WKFrameInfo, type: WKMediaCaptureType,
                 decisionHandler: @escaping (WKPermissionDecision) -> Void) { decisionHandler(.deny) }
}

private final class WorkbenchToolbar: NSObject, NSToolbarDelegate {
    private let backID = NSToolbarItem.Identifier("workbench.back")
    private let forwardID = NSToolbarItem.Identifier("workbench.forward")
    private let sidebarID = NSToolbarItem.Identifier("workbench.sidebar")
    private let panelID = NSToolbarItem.Identifier("workbench.panel")
    private let onToggle: () -> Void
    private let onTogglePanel: () -> Void
    private let onNavigate: (String) -> Void
    private var buttons: [NSToolbarItem.Identifier: NSButton] = [:]
    let toolbar = NSToolbar(identifier: "workbench.chrome")

    init(window: NSWindow, onNavigate: @escaping (String) -> Void, onToggle: @escaping () -> Void, onTogglePanel: @escaping () -> Void) {
        self.onToggle = onToggle
        self.onTogglePanel = onTogglePanel
        self.onNavigate = onNavigate
        super.init()
        toolbar.delegate = self
        toolbar.displayMode = .iconOnly
        toolbar.allowsUserCustomization = false
        toolbar.autosavesConfiguration = false
        window.titleVisibility = .hidden
        window.titlebarAppearsTransparent = true
        window.titlebarSeparatorStyle = .none
        window.toolbarStyle = .unifiedCompact
        window.toolbar = toolbar
    }

    func toolbarDefaultItemIdentifiers(_ toolbar: NSToolbar) -> [NSToolbarItem.Identifier] {
        [backID, forwardID, sidebarID, .flexibleSpace, panelID]
    }
    func toolbarAllowedItemIdentifiers(_ toolbar: NSToolbar) -> [NSToolbarItem.Identifier] {
        toolbarDefaultItemIdentifiers(toolbar)
    }
    func toolbar(_ toolbar: NSToolbar, itemForItemIdentifier identifier: NSToolbarItem.Identifier,
                 willBeInsertedIntoToolbar flag: Bool) -> NSToolbarItem? {
        let label: String, symbol: String, action: Selector
        switch identifier {
        case backID: label = "后退"; symbol = "arrow.left"; action = #selector(goBack)
        case forwardID: label = "前进"; symbol = "arrow.right"; action = #selector(goForward)
        case sidebarID: label = "显示／隐藏左侧栏"; symbol = "sidebar.left"; action = #selector(toggleSidebar)
        case panelID: label = "显示／隐藏右侧栏"; symbol = "sidebar.right"; action = #selector(togglePanel)
        default: return nil
        }
        let item = NSToolbarItem(itemIdentifier: identifier)
        item.label = label
        item.toolTip = label
        item.isNavigational = identifier != panelID
        item.visibilityPriority = .high
        let button = NSButton(image: NSImage(systemSymbolName: symbol, accessibilityDescription: nil)!,
                              target: self, action: action)
        button.image = button.image?.withSymbolConfiguration(NSImage.SymbolConfiguration(pointSize: 14, weight: .regular))
        button.bezelStyle = .texturedRounded
        button.isBordered = false
        button.contentTintColor = .secondaryLabelColor
        button.toolTip = item.toolTip
        button.setAccessibilityLabel(label)
        button.isEnabled = identifier == sidebarID || identifier == panelID
        button.translatesAutoresizingMaskIntoConstraints = false
        NSLayoutConstraint.activate([button.widthAnchor.constraint(equalToConstant: 30),
                                     button.heightAnchor.constraint(equalToConstant: 28)])
        item.view = button
        buttons[identifier] = button
        return item
    }
    func updateNavigation(canGoBack: Bool, canGoForward: Bool) {
        buttons[backID]?.isEnabled = canGoBack
        buttons[forwardID]?.isEnabled = canGoForward
    }
    @objc private func goBack() { onNavigate("back") }
    @objc private func goForward() { onNavigate("forward") }
    @objc private func toggleSidebar() { onToggle() }
    @objc private func togglePanel() { onTogglePanel() }
}

private final class LocalLookupDelegate: NSObject, URLSessionTaskDelegate {
    func urlSession(_ session: URLSession, task: URLSessionTask,
                    willPerformHTTPRedirection response: HTTPURLResponse, newRequest request: URLRequest,
                    completionHandler: @escaping (URLRequest?) -> Void) { completionHandler(nil) }
}

final class WorkbenchController: NSObject, NSWindowDelegate, WKNavigationDelegate, WKUIDelegate, WKScriptMessageHandlerWithReply {
    private var window: NSWindow!
    private var webView: WKWebView?
    private var process: Process?
    private var input: Pipe?
    private var output: Pipe?
    private var errors: Pipe?
    private var outputBuffer = Data()
    private var errorBuffer = Data()
    private var readyURL: URL?
    private var backendToken: String?
    private var startupTimer: Timer?
    private var stopCompletion: (() -> Void)?
    private var stopping = false
    private var quitting = false
    private(set) var mode: String?
    weak var coordinator: DesktopApp?
    private var views: [NSWindow: WorkspaceViewRecord] = [:]
    private var closing = false
    private var closingViews = Set<NSWindow>()
    private var closedViews: [WorkspaceViewRecord] = []
    var ownsBackend: Bool { process != nil }
    var hasConnectedView: Bool { readyURL != nil && !workWindows.isEmpty }
    var lifecycleBusy: Bool { closing || !closingViews.isEmpty || stopping }
    private var viewStateTimer: Timer?
    var windows: [NSWindow] { Array(views.keys) }
    var displayName: String { mode == nil ? "新工作台" : mode == "" ? "示例" : URL(fileURLWithPath: mode!).lastPathComponent }
    private var workWindows: [NSWindow: WKWebView] = [:]
    private var browserPanes: [ObjectIdentifier: [String: BrowserPane]] = [:]
    private var windowToolbars: [NSWindow: WorkbenchToolbar] = [:]
    private var launchConfig: [String: Any]?
    private var modelSetup: ModelSetupController?
    private var useModelSetup = false


    func start(coordinator: DesktopApp, restored: WorkspaceRecord? = nil, autoStart: Bool = false, showSetup: Bool = false) {
        self.coordinator = coordinator
        viewStateTimer = Timer.scheduledTimer(withTimeInterval: 3, repeats: true) { [weak self] _ in self?.captureViewStates() }
        mode = restored?.path
        closedViews = restored?.closedViews ?? []
        useModelSetup = true
        let records = restored?.views.isEmpty == false ? restored!.views : [WorkspaceViewRecord()]
        for record in records {
            window = makeWindow(record)
            if showSetup {
                welcome(autoStart: false)
            } else if restored != nil {
                panel("正在打开工作台…", "")
            } else if UserDefaults.standard.string(forKey: "xiaoyu.desktop.modelMode") != nil {
                launcher()
                if autoStart {
                    DispatchQueue.main.async { [weak self] in
                        guard let self = self, self.mode == nil, !self.quitting else { return }
                        self.startDefaultWorkspace()
                    }
                }
            } else { welcome(autoStart: autoStart) }
            window.makeKeyAndOrderFront(nil)
        }
        if restored != nil && autoStart {
            DispatchQueue.main.async { [weak self] in
                guard let self = self, !self.quitting, !self.views.isEmpty else { return }
                self.startDefaultWorkspace()
            }
        }
    }
    private func makeWindow(_ record: WorkspaceViewRecord = WorkspaceViewRecord()) -> NSWindow {
        let next = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1280, height: 860),
            styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        next.title = "羽案 · \(displayName)"
        next.titlebarAppearsTransparent = true; next.titlebarSeparatorStyle = .none
        next.minSize = NSSize(width: 820, height: 600); next.isReleasedWhenClosed = false
        next.delegate = self; next.center()
        if let frame = record.frame { next.setFrame(from: frame) }
        if !NSScreen.screens.contains(where: { $0.visibleFrame.intersects(next.frame) }) { next.center() }
        views[next] = record
        return next
    }
    private func launcher() {
        panel("羽案，从一件工作开始。", "工作目录为 \(coordinator?.defaultWorkspaceDescription ?? "~/Documents/羽案/Default")。你也可以选择其他文件夹。",
              buttons: [("开始使用", #selector(startDefaultWorkspace)), ("选择其他文件夹…", #selector(chooseWorkspace))])
    }
    var restoration: WorkspaceRecord? {
        guard let path = mode, !path.isEmpty else { return nil }
        return WorkspaceRecord(path: path, views: views.map { target, value in
            var record = value; record.frame = target.frameDescriptor; return record
        }.sorted { $0.id < $1.id }, closedViews: closedViews)
    }
    private func captureViewStates() {
        guard !quitting, !stopping else { return }
        for (target, web) in workWindows {
            web.evaluateJavaScript("window.__XIAOYU_VIEW_STATE__?.()") { [weak self, weak target, weak web] result, _ in
                guard let self = self, let target = target, let web = web, self.workWindows[target] === web,
                      var record = self.views[target], let state = result as? [String: Any] else { return }
                record.update(state); self.views[target] = record; self.coordinator?.persist()
            }
        }
    }
    func windowDidMove(_ notification: Notification) { coordinator?.persist() }
    func windowDidResize(_ notification: Notification) { coordinator?.persist() }

    private func panel(_ title: String, _ detail: String, buttons: [(String, Selector)] = []) {
        webView?.stopLoading()
        windowToolbars.removeValue(forKey: window)
        window.toolbar = nil
        window.titleVisibility = .visible
        webView = nil
        let view = NSView()
        view.wantsLayer = true
        view.layer?.backgroundColor = NSColor.windowBackgroundColor.cgColor
        let heading = NSTextField(labelWithString: title)
        heading.font = NSFont.systemFont(ofSize: 28, weight: .medium)
        let description = NSTextField(wrappingLabelWithString: detail)
        description.font = NSFont.systemFont(ofSize: 14)
        description.textColor = .secondaryLabelColor
        let logo = NSImageView()
        if let resources = Bundle.main.resourceURL {
            logo.image = NSImage(contentsOf: resources.appendingPathComponent("runtime/labs/desktop/web/logo.png"))
        }
        logo.translatesAutoresizingMaskIntoConstraints = false
        logo.widthAnchor.constraint(equalToConstant: 56).isActive = true
        logo.heightAnchor.constraint(equalToConstant: 56).isActive = true
        let stack = NSStackView(views: [logo, heading, description])
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 20
        for (title, action) in buttons {
            stack.addArrangedSubview(NSButton(title: title, target: self, action: action))
        }
        stack.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.centerXAnchor.constraint(equalTo: view.centerXAnchor),
            stack.centerYAnchor.constraint(equalTo: view.centerYAnchor),
            stack.widthAnchor.constraint(equalToConstant: 560),
        ])
        window.contentView = view
    }

    private func welcome(autoStart: Bool = false) {
        guard process == nil, !stopping, !quitting else { return }
        guard let resources = Bundle.main.resourceURL,
              let data = try? Data(contentsOf: resources.appendingPathComponent("model-providers.json")),
              let providers = try? JSONDecoder().decode([ModelSetupProvider].self, from: data),
              providers.count == 8 else {
            panel("欢迎使用羽案", "模型配置页面未能加载，请重新构建应用，或使用已有核心配置打开工作目录。",
                  buttons: [("开始使用", #selector(startWithCoreConfiguration))])
            return
        }
        if modelSetup == nil { modelSetup = ModelSetupController(providers: providers) }
        windowToolbars.removeValue(forKey: window)
        window.toolbar = nil
        window.titleVisibility = .visible
        window.title = "羽案"
        webView?.stopLoading(); webView = nil
        let logo = NSImage(contentsOf: resources.appendingPathComponent("runtime/labs/desktop/web/logo.png"))
        window.contentView = modelSetup!.makeView(logo: logo, autoStart: autoStart, workspaceDescription: coordinator?.defaultWorkspaceDescription ?? "~/Documents/羽案/Default", onContinue: { [weak self] in
            guard let self = self else { return }
            self.useModelSetup = true
            UserDefaults.standard.set("selected", forKey: "xiaoyu.desktop.modelMode")
            self.startDefaultWorkspace()
        })
    }
    @objc private func configureModels() { welcome() }
    @objc private func startWithCoreConfiguration() {
        useModelSetup = false
        UserDefaults.standard.set("existing", forKey: "xiaoyu.desktop.modelMode")
        startDefaultWorkspace()
    }
    @objc private func startDefaultWorkspace() {
        guard process == nil, !stopping, !quitting else { return }
        // Re-entering setup after a failed launch keeps the chosen workspace.
        // Resume its owner directly; directory deduplication would only focus it.
        if let workspace = mode { launch(workspace) }
        else { coordinator?.openDefault(from: self) }
    }

    @objc func about() {
        let alert = NSAlert()
        let version = Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "未知版本"
        alert.messageText = "羽案 · \(version)"
        alert.informativeText = "原生 macOS 窗口 · 本机服务生命周期 · 任务工作台\n\n这是使用本机 Python 环境的开发构建。尚未签名公证分发。任务、审批和问答操作使用本机服务。各工作台服务独立；关闭工作台会停止其服务，退出应用会停止全部服务。"
        alert.accessoryView = NSView(frame: NSRect(x: 0, y: 0, width: 460, height: 0))
        alert.runModal()
    }

    @objc func openSettings() {
        let active = NSApp.keyWindow.flatMap { workWindows[$0] } ?? webView
        active?.evaluateJavaScript("document.getElementById('settings')?.click()", completionHandler: nil)
    }

    @objc func newWindow() { openWorkbenchWindow(config: launchConfig) }
    var hasClosedViews: Bool { !closedViews.isEmpty }
    func restoreClosedView() {
        guard let ready = readyURL, let config = launchConfig, !stopping, !quitting, !closing,
              let record = closedViews.popLast() else { return }
        let next = makeWindow(record); installWorkbench(next, ready: ready, config: config)
        coordinator?.persist()
    }
    private func openWorkbenchWindow(config: [String: Any]?) {
        guard let ready = readyURL, let config = config, !stopping, !quitting, !closing else { return }
        var record = WorkspaceViewRecord()
        record.sessionID = config["session_id"] as? String
        let next = makeWindow(record)
        installWorkbench(next, ready: ready, config: config)
        coordinator?.persist()
    }
    func windowDidBecomeKey(_ notification: Notification) {
        if let next = notification.object as? NSWindow, views[next] != nil {
            window = next; webView = workWindows[next]
        }
    }
    @objc func showWindow() { window.makeKeyAndOrderFront(nil) }
    @objc func reload() {
        if !stopping && !quitting, let target = webView {
            flushWeb(target) { [weak self, weak target] saved in
                guard let self = self, let target = target, saved, !self.stopping, !self.quitting,
                      self.workWindows.values.contains(where: { $0 === target }) else { return }
                target.reload()
            }
        }
    }
    @objc func openFixture() { launch("") }
    @objc private func retry() {
        guard let mode = mode, process == nil, !stopping, !quitting else { return }
        if workWindows.isEmpty { launch(mode) }
        else { saveApplicationState { [weak self] saved in if saved { self?.launch(mode) } } }
    }
    func openWorkspace(_ path: String) { launch(path) }

    @objc func chooseWorkspace() {
        let chooser = NSOpenPanel()
        chooser.canChooseFiles = false
        chooser.canChooseDirectories = true
        chooser.allowsMultipleSelection = false
        chooser.directoryURL = coordinator?.chooserDirectory
        chooser.prompt = "打开工作台"
        chooser.message = "选择项目目录或包含多个项目的父目录。桌面服务仅管理此范围，终端会话不会自动接管。"
        if chooser.runModal() == .OK, let url = chooser.url { coordinator?.open(url, from: self) }
    }

    @objc func restart() {
        guard mode != nil, !stopping, !quitting else { return }
        let alert = NSAlert()
        alert.messageText = "重新启动本机服务？"
        alert.informativeText = "正在执行的任务将被打断。真实工作目录的会话清单保留；示例会重新生成。"
        alert.addButton(withTitle: "重新启动")
        alert.addButton(withTitle: "取消")
        if alert.runModal() == .alertFirstButtonReturn {
            saveApplicationState { [weak self] saved in
                guard let self = self, saved else { return }
                self.panel("正在停止服务…", "停止后会重新连接工作台。")
                self.stop { [weak self] in self?.retry() }
            }
        }
    }

    func saveApplicationState(_ completion: @escaping (Bool) -> Void) {
        var pending = Array(workWindows.values)
        func next() {
            guard !pending.isEmpty else { completion(true); return }
            let web = pending.removeFirst()
            flushWeb(web) { saved in if saved { next() } else { completion(false) } }
        }
        next()
    }

    private func flushWeb(_ web: WKWebView, _ completion: @escaping (Bool) -> Void) {
        var finished = false
        let finish: (Bool) -> Void = { success in
            guard !finished else { return }; finished = true
            if success { completion(true); return }
            let alert = NSAlert()
            alert.messageText = "草稿或提交回执尚未保存"
            alert.informativeText = "请返回工作台，复制保留输入并检查本机存储。仍然继续可能丢失最后的修改。"
            alert.addButton(withTitle: "返回工作台"); alert.addButton(withTitle: "仍然继续")
            completion(alert.runModal() == .alertSecondButtonReturn)
        }
        web.callAsyncJavaScript("if (typeof window.__XIAOYU_FLUSH__ !== 'function') throw new Error('not ready'); await window.__XIAOYU_FLUSH__(); return window.__XIAOYU_VIEW_STATE__?.() || {};",
            arguments: [:], in: nil, in: .page) { [weak self] result in
                guard !finished else { return }
                switch result {
                case .success(let value):
                    if let self = self, let target = self.workWindows.first(where: { $0.value === web })?.key,
                       let state = value as? [String: Any], var record = self.views[target] {
                        record.update(state); self.views[target] = record; self.coordinator?.persist()
                    }
                    finish(true)
                case .failure: finish(false)
                }
            }
        DispatchQueue.main.asyncAfter(deadline: .now() + 5) { finish(false) }
    }

    private func launch(_ workspace: String) {
        guard process == nil, !stopping, !quitting else { return }
        mode = workspace
        if useModelSetup && modelSetup == nil,
           let resources = Bundle.main.resourceURL,
           let data = try? Data(contentsOf: resources.appendingPathComponent("model-providers.json")),
           let providers = try? JSONDecoder().decode([ModelSetupProvider].self, from: data) {
            modelSetup = ModelSetupController(providers: providers)
        }
        coordinator?.persist()
        readyURL = nil
        errorBuffer.removeAll()
        outputBuffer.removeAll()
        panel("正在打开工作台…", workspace.isEmpty ? "正在准备隔离的脚本模型示例，不调用真实模型。" : "正在为所选工作目录启动本机服务。")
        do {
            guard let resources = Bundle.main.resourceURL,
                  let config = try JSONSerialization.jsonObject(with: Data(contentsOf: resources.appendingPathComponent("runtime.json"))) as? [String: String] else {
                throw NSError(domain: "DesktopLab", code: 1, userInfo: [NSLocalizedDescriptionKey: "应用运行环境不完整，请重新安装羽案。"])
            }
            let python = config["bundledPython"].map { resources.appendingPathComponent($0).path } ?? config["python"] ?? ""
            guard FileManager.default.isExecutableFile(atPath: python) else {
                throw NSError(domain: "DesktopLab", code: 1, userInfo: [NSLocalizedDescriptionKey: "找不到应用运行环境，请重新安装羽案。"])
            }
            let child = Process()
            child.executableURL = URL(fileURLWithPath: python)
            // Bytecode caches inside the signed resources would invalidate the bundle.
            child.arguments = ["-B", "-P", "-u", "-m", "labs.desktop.macos.backend"] +
                (workspace.isEmpty ? ["--fixture"] : ["--workspace", workspace])
            var env = ProcessInfo.processInfo.environment
            if !workspace.isEmpty && useModelSetup {
                guard let setup = modelSetup else {
                    throw NSError(domain: "DesktopLab", code: 2, userInfo: [NSLocalizedDescriptionKey: "请先启用模型服务。"])
                }
                if !setup.selectedProviderIDs.isEmpty {
                    var overlay = try setup.launchEnvironment()
                    if let fallback = overlay.removeValue(forKey: "XIAOYU_MODEL") {
                        child.arguments! += ["--fallback-model", fallback]
                    }
                    overlay.removeValue(forKey: "XIAOYU_PROVIDERS")
                    env.merge(overlay) { _, selected in selected }
                }
                for provider in setup.disabledProviderIDs {
                    child.arguments! += ["--disabled-provider", provider]
                }
            }
            env["PYTHONPATH"] = resources.appendingPathComponent("runtime").path
            env.removeValue(forKey: "PYTHONHOME")
            env.removeValue(forKey: "PYTHONUSERBASE")
            env.removeValue(forKey: "PYTHONSTARTUP")
            env.removeValue(forKey: "PYTHONPYCACHEPREFIX")
            env["PYTHONNOUSERSITE"] = "1"
            env["PATH"] = desktopExecutablePath(python: python, inherited: env["PATH"] ?? "", home: NSHomeDirectory())
            child.environment = env
            child.currentDirectoryURL = resources
            let stdinPipe = Pipe(), stdoutPipe = Pipe(), stderrPipe = Pipe()
            child.standardInput = stdinPipe
            child.standardOutput = stdoutPipe
            child.standardError = stderrPipe
            input = stdinPipe; output = stdoutPipe; errors = stderrPipe
            process = child
            stdoutPipe.fileHandleForReading.readabilityHandler = { [weak self, weak child] handle in
                let data = handle.availableData
                DispatchQueue.main.async {
                    guard let self = self, let child = child, self.process === child, !self.stopping else { return }
                    self.readReady(data)
                }
            }
            stderrPipe.fileHandleForReading.readabilityHandler = { [weak self, weak child] handle in
                let data = handle.availableData
                DispatchQueue.main.async {
                    guard let self = self, let child = child, self.process === child else { return }
                    self.errorBuffer.append(data)
                    self.errorBuffer = Data(self.errorBuffer.suffix(4096))
                }
            }
            child.terminationHandler = { [weak self] child in
                DispatchQueue.main.async { self?.exited(child) }
            }
            try child.run()
            // Parent retains only the write end: an app crash must deliver EOF.
            stdinPipe.fileHandleForReading.closeFile()
            stdoutPipe.fileHandleForWriting.closeFile()
            stderrPipe.fileHandleForWriting.closeFile()
            startupTimer = Timer.scheduledTimer(withTimeInterval: 30, repeats: false) { [weak self] _ in
                guard let self = self, self.readyURL == nil else { return }
                self.fail("服务启动超过 30 秒。请检查 Python 环境与工作目录后重试。")
            }
        } catch {
            releaseProcess()
            failurePanel(error.localizedDescription)
        }
    }

    private func readReady(_ data: Data) {
        guard readyURL == nil, !data.isEmpty else { return }
        outputBuffer.append(data)
        guard outputBuffer.count < 16384 else { fail("服务启动响应过大。"); return }
        guard let newline = outputBuffer.firstIndex(of: 10) else { return }
        do {
            guard let result = try JSONSerialization.jsonObject(with: outputBuffer[..<newline]) as? [String: Any],
                  let rawURL = result["url"] as? String, let url = URL(string: rawURL),
                  url.scheme == "http", url.host == "127.0.0.1", url.port != nil,
                  URLComponents(url: url, resolvingAgainstBaseURL: false)?.path == "/workbench/",
                  url.user == nil, url.password == nil, url.query == nil, url.fragment == nil,
                  let token = result["token"] as? String, token.count >= 32 else {
                fail("服务启动响应无效。"); return
            }
            startupTimer?.invalidate(); startupTimer = nil
            readyURL = url
            backendToken = token
            outputBuffer.removeAll()
            let origin = "http://127.0.0.1:\(url.port!)"
            let config: [String: Any] = ["origin": origin, "token": token, "fixture": result["fixture"] as? Bool ?? false, "app_version": Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "0.0.0"]
            launchConfig = config
            let targets = Array(views.keys)
            for target in targets { installWorkbench(target, ready: url, config: config) }
        } catch { fail("无法解析服务启动响应。") }
    }

    private func installWorkbench(_ target: NSWindow, ready: URL, config: [String: Any]) {
        disposeWeb(target)
        var pageConfig = config
        if let record = views[target] {
            pageConfig["view_id"] = record.id
            if let sid = record.sessionID { pageConfig["session_id"] = sid }
            if let page = record.page { pageConfig["page"] = page }
        }
        pageConfig["titlebar_sidebar"] = true
        pageConfig["titlebar_panel"] = true
        guard let data = try? JSONSerialization.data(withJSONObject: pageConfig) else { return }
        let encoded = String(decoding: data, as: UTF8.self)
        let origin = "http://127.0.0.1:\(ready.port!)"
        let script = "if (location.origin === \"\(origin)\" && location.pathname === '/workbench/') { window.__XIAOYU_DESKTOP__ = \(encoded); }"
        let settings = WKWebViewConfiguration()
        settings.websiteDataStore = .nonPersistent()
        settings.preferences.javaScriptCanOpenWindowsAutomatically = false
        settings.userContentController.addUserScript(WKUserScript(source: script, injectionTime: .atDocumentStart, forMainFrameOnly: true))
        for name in ["revealArtifact", "openConnectionAuthorization", "workspace", "navigationState"] {
            settings.userContentController.addScriptMessageHandler(self, contentWorld: .page, name: name)
        }
        let web = WKWebView(frame: target.contentView!.bounds, configuration: settings)
        web.autoresizingMask = [.width, .height]; web.navigationDelegate = self; web.uiDelegate = self
        web.allowsBackForwardNavigationGestures = false
        target.contentView = web; workWindows[target] = web
        windowToolbars[target] = WorkbenchToolbar(window: target, onNavigate: { [weak self, weak target] direction in
            guard let self = self, let target = target, !self.stopping, !self.quitting,
                  let active = self.workWindows[target] else { return }
            let event = direction == "back" ? "xiaoyu-navigate-back" : "xiaoyu-navigate-forward"
            active.evaluateJavaScript("window.dispatchEvent(new Event('\(event)'))", completionHandler: nil)
        }, onToggle: { [weak self, weak target] in
            guard let self = self, let target = target, !self.stopping, !self.quitting,
                  let active = self.workWindows[target] else { return }
            active.evaluateJavaScript("window.dispatchEvent(new Event('xiaoyu-toggle-sidebar'))", completionHandler: nil)
        }, onTogglePanel: { [weak self, weak target] in
            guard let self = self, let target = target, !self.stopping, !self.quitting,
                  let active = self.workWindows[target] else { return }
            active.evaluateJavaScript("window.dispatchEvent(new Event('xiaoyu-toggle-panel'))", completionHandler: nil)
        })
        target.title = mode == "" ? "羽案 · 示例" : "羽案 · \(URL(fileURLWithPath: mode ?? "").lastPathComponent)"
        window = target; webView = web
        target.makeFirstResponder(web); target.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        web.load(URLRequest(url: ready))
    }

    private func releaseProcess() {
        startupTimer?.invalidate(); startupTimer = nil
        output?.fileHandleForReading.readabilityHandler = nil
        errors?.fileHandleForReading.readabilityHandler = nil
        input?.fileHandleForWriting.closeFile()
        output?.fileHandleForReading.closeFile()
        errors?.fileHandleForReading.closeFile()
        input = nil; output = nil; errors = nil; process = nil; readyURL = nil; backendToken = nil
    }

    private func exited(_ child: Process) {
        guard process === child else { return }
        let expected = stopping
        releaseProcess()
        stopping = false
        let completion = stopCompletion
        stopCompletion = nil
        if expected { completion?() }
        else { failurePanel("本机服务已退出（状态 \(child.terminationStatus)）。请核对模型配置与工作目录；该目录也可能已由另一个羽案窗口实例打开。") }
    }

    func stop(_ completion: @escaping () -> Void) {
        if stopping {
            let previous = stopCompletion
            stopCompletion = { previous?(); completion() }
            return
        }
        guard let child = process, child.isRunning else {
            releaseProcess(); completion(); return
        }
        stopping = true
        stopCompletion = completion
        startupTimer?.invalidate(); startupTimer = nil
        for web in workWindows.values { web.stopLoading() }
        for panes in browserPanes.values { for pane in panes.values { pane.view.stopLoading(); pane.view.isHidden = true } }
        input?.fileHandleForWriting.closeFile()
        // Only signal the Process instance this app owns, never a port's occupant.
        DispatchQueue.main.asyncAfter(deadline: .now() + 4) { [weak child] in
            if let child = child, child.isRunning { child.terminate() }
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 6) { [weak child] in
            if let child = child, child.isRunning { kill(child.processIdentifier, SIGKILL) }
        }
    }

    private func fail(_ message: String) { stop { [weak self] in self?.failurePanel(message) } }
    private func failurePanel(_ message: String) {
        if !workWindows.isEmpty {
            let alert = NSAlert(); alert.messageText = "工作台服务暂时不可用"; alert.informativeText = message + "\n窗口中的输入会保留，请先复制未保存内容。"
            alert.addButton(withTitle: "保留界面"); alert.addButton(withTitle: "重新启动服务")
            if alert.runModal() == .alertSecondButtonReturn { retry() }
            return
        }
        panel("工作台暂时无法连接", message, buttons: [("重试", #selector(retry)), ("选择其他工作目录…", #selector(chooseWorkspace)), ("模型配置…", #selector(configureModels))])
    }

    private func disposeWeb(_ target: NSWindow) {
        guard let web = workWindows.removeValue(forKey: target) else { return }
        browserPanes.removeValue(forKey: ObjectIdentifier(web))?.values.forEach { $0.view.stopLoading(); $0.view.removeFromSuperview() }
        for name in ["revealArtifact", "openConnectionAuthorization", "workspace", "navigationState"] {
            web.configuration.userContentController.removeScriptMessageHandler(forName: name, contentWorld: .page)
        }
        web.stopLoading(); web.navigationDelegate = nil; web.uiDelegate = nil
        windowToolbars.removeValue(forKey: target)
    }
    func setQuitting(_ value: Bool) { quitting = value }
    func confirmStop(_ completion: @escaping (Bool) -> Void) {
        guard process != nil else { completion(true); return }
        var resolved = false
        let finish: ([String: Any]?) -> Void = { state in
            guard !resolved else { return }; resolved = true
            let running = state?["running"] as? Int
            let pending = state?["pending"] as? Int
            if running == 0 && pending == 0 { completion(true); return }
            let alert = NSAlert()
            alert.messageText = "关闭工作台“\(self.displayName)”？"
            alert.informativeText = running != nil && pending != nil
                ? "此工作台有 \(running!) 个执行中的会话、\(pending!) 项待处理交互。关闭将停止这些任务；其他工作台不受影响。"
                : "暂时无法确认此工作台的任务状态。关闭将停止其服务和任务；其他工作台不受影响。"
            alert.addButton(withTitle: "停止并关闭"); alert.addButton(withTitle: "取消")
            completion(alert.runModal() == .alertFirstButtonReturn)
        }
        if let web = workWindows.values.first {
            web.evaluateJavaScript("window.__XIAOYU_WORK_STATUS__?.()") { value, _ in finish(value as? [String: Any]) }
            DispatchQueue.main.asyncAfter(deadline: .now() + 2) { finish(nil) }
        } else { finish(nil) }
    }
    func closeWorkspace() {
        guard !closing, !quitting, closingViews.isEmpty, coordinator?.isQuitting != true else { return }
        closing = true
        confirmStop { [weak self] allowed in
            guard let self = self else { return }
            guard allowed else { self.closing = false; return }
            self.saveApplicationState { saved in
                guard saved else { self.closing = false; return }
                self.quitting = true
                self.coordinator?.archive(self)
                self.closeInterfaceAndStop { self.coordinator?.remove(self) }
            }
        }
    }
    // 仅在关闭确认、保存及恢复记录归档完成后调用。
    // 先移除界面，再停止服务，正常退出不会被仍在轮询的页面显示为连接故障。
    func closeInterfaceAndStop(_ completion: @escaping () -> Void) {
        closeAllWindows()
        stop(completion)
    }
    func closeAllWindows() {
        viewStateTimer?.invalidate(); viewStateTimer = nil
        for target in windows { disposeWeb(target); target.delegate = nil; target.close() }
        views.removeAll(); webView = nil
    }
    func windowShouldClose(_ sender: NSWindow) -> Bool {
        guard !closing, !quitting, coordinator?.isQuitting != true, closingViews.isEmpty else { return false }
        guard views.count > 1 else { closeWorkspace(); return false }
        closingViews.insert(sender)
        let close = { [weak self, weak sender] (saved: Bool) in
            guard let self = self, let sender = sender else { return }
            self.closingViews.remove(sender)
            guard saved else { return }
            if var record = self.views[sender] {
                record.frame = sender.frameDescriptor; self.closedViews.append(record)
            }
            self.disposeWeb(sender); self.views.removeValue(forKey: sender)
            sender.delegate = nil; sender.close()
            if let next = self.views.keys.first { self.window = next; self.webView = self.workWindows[next] }
            self.coordinator?.persist()
        }
        if let web = workWindows[sender] { flushWeb(web, close) } else { close(true) }
        return false
    }
    func openSession(_ sid: String) {
        guard sid.range(of: "^[A-Za-z0-9_-]{1,100}$", options: .regularExpression) != nil else { return }
        guard var config = launchConfig, readyURL != nil else { showWindow(); return }
        config["session_id"] = sid; openWorkbenchWindow(config: config)
    }

    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = action.request.url, let ready = readyURL,
              action.targetFrame?.isMainFrame == true,
              url.scheme == ready.scheme, url.host == ready.host, url.port == ready.port,
              url.path == ready.path, url.query == nil else { decisionHandler(.cancel); return }
        decisionHandler(.allow)
    }

    func userContentController(_ userContentController: WKUserContentController,
                               didReceive message: WKScriptMessage,
                               replyHandler: @escaping (Any?, String?) -> Void) {
        if message.name == "navigationState" {
            guard !stopping, !quitting, message.frameInfo.isMainFrame,
                  let web = message.webView,
                  let target = workWindows.first(where: { $0.value === web })?.key,
                  let ready = readyURL, let origin = message.frameInfo.request.url,
                  origin.scheme == ready.scheme, origin.host == ready.host,
                  origin.port == ready.port, origin.path == ready.path,
                  let body = message.body as? [String: Any],
                  Set(body.keys) == Set(["canGoBack", "canGoForward"]),
                  let back = body["canGoBack"] as? NSNumber, CFGetTypeID(back) == CFBooleanGetTypeID(),
                  let forward = body["canGoForward"] as? NSNumber, CFGetTypeID(forward) == CFBooleanGetTypeID()
            else { replyHandler(nil, "无法识别导航状态。"); return }
            windowToolbars[target]?.updateNavigation(canGoBack: back.boolValue, canGoForward: forward.boolValue)
            replyHandler(["updated": true], nil)
            return
        }
        if message.name == "workspace" {
            workspaceMessage(message, replyHandler: replyHandler); return
        }
        if message.name == "openConnectionAuthorization" {
            openConnectionAuthorization(message, replyHandler: replyHandler)
            return
        }
        guard !stopping, !quitting, message.name == "revealArtifact", message.frameInfo.isMainFrame,
              message.webView.map({ candidate in self.workWindows.values.contains(where: { $0 === candidate }) }) == true, let ready = readyURL, let token = backendToken,
              let origin = message.frameInfo.request.url,
              origin.scheme == ready.scheme, origin.host == ready.host,
              origin.port == ready.port, origin.path == ready.path,
              let body = message.body as? [String: Any],
              let sid = body["session_id"] as? String, let aid = body["artifact_id"] as? String,
              sid.range(of: "^[A-Za-z0-9_-]{1,100}$", options: .regularExpression) != nil,
              aid.range(of: "^[A-Za-z0-9_-]{1,100}$", options: .regularExpression) != nil,
              Set(body.keys).isSubset(of: ["session_id", "artifact_id", "run_id", "batch_id", "item_id", "contest_id", "candidate_id"])
        else { replyHandler(nil, "无法识别成果定位请求。"); return }
        var components = URLComponents(url: ready, resolvingAgainstBaseURL: false)!
        components.path = "/session/\(sid)/artifacts/\(aid)/location"
        components.queryItems = []
        components.fragment = nil
        for key in ["run_id", "batch_id", "item_id", "contest_id", "candidate_id"] {
            if let value = body[key] {
                guard let text = value as? String, text.count <= 200 else {
                    replyHandler(nil, "成果来源无效。"); return
                }
                components.queryItems!.append(URLQueryItem(name: key, value: text))
            }
        }
        guard let url = components.url else { replyHandler(nil, "成果地址无效。"); return }
        var request = URLRequest(url: url)
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        let configuration = URLSessionConfiguration.ephemeral
        configuration.timeoutIntervalForRequest = 5
        configuration.httpCookieStorage = nil
        configuration.urlCache = nil
        let lookup = URLSession(configuration: configuration, delegate: LocalLookupDelegate(), delegateQueue: nil)
        lookup.dataTask(with: request) { [weak self] data, response, _ in
            defer { lookup.finishTasksAndInvalidate() }
            var target: URL?
            if (response as? HTTPURLResponse)?.statusCode == 200, let data = data,
               let record = (try? JSONSerialization.jsonObject(with: data)) as? [String: String],
               let path = record["path"], let workspace = record["workspace"],
               path.hasPrefix("/"), workspace.hasPrefix("/") {
                let file = URL(fileURLWithPath: path).standardizedFileURL
                let root = URL(fileURLWithPath: workspace).standardizedFileURL
                if file.path.hasPrefix(root.path + "/"), file.resolvingSymlinksInPath().path == file.path,
                   FileManager.default.fileExists(atPath: file.path) { target = file }
            }
            DispatchQueue.main.async {
                guard let self = self, self.readyURL == ready, self.backendToken == token,
                      let target = target else {
                    replyHandler(nil, "成果已缺失、路径已变化或服务不可用，请刷新后重试。"); return
                }
                NSWorkspace.shared.activateFileViewerSelecting([target])
                replyHandler(["revealed": true], nil)
            }
        }.resume()
    }
    func webView(_ webView: WKWebView, runJavaScriptTextInputPanelWithPrompt prompt: String,
                 defaultText: String?, initiatedByFrame frame: WKFrameInfo,
                 completionHandler: @escaping (String?) -> Void) {
        guard workWindows.values.contains(where: { $0 === webView }), frame.isMainFrame,
              let target = webView.window else { completionHandler(nil); return }
        let alert = NSAlert(); alert.messageText = String(prompt.prefix(1000))
        alert.addButton(withTitle: "确定"); alert.addButton(withTitle: "取消")
        let input = NSTextField(string: defaultText ?? ""); input.frame = NSRect(x: 0, y: 0, width: 400, height: 24)
        alert.accessoryView = input
        alert.beginSheetModal(for: target) { result in completionHandler(result == .alertFirstButtonReturn ? input.stringValue : nil) }
    }
    func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
        guard workWindows.values.contains(where: { $0 === webView }), frame.isMainFrame,
              let target = webView.window else { completionHandler(false); return }
        let alert = NSAlert(); alert.messageText = String(message.prefix(2000))
        alert.addButton(withTitle: "确定"); alert.addButton(withTitle: "取消")
        alert.beginSheetModal(for: target) { result in completionHandler(result == .alertFirstButtonReturn) }
    }

    private func workspaceMessage(_ message: WKScriptMessage, replyHandler: @escaping (Any?, String?) -> Void) {
        guard !stopping, !quitting, message.frameInfo.isMainFrame, let web = message.webView,
              workWindows.values.contains(where: { $0 === web }),
              let ready = readyURL, let source = message.frameInfo.request.url,
              source.scheme == ready.scheme, source.host == ready.host, source.port == ready.port,
              source.path == ready.path, source.query == nil,
              let body = message.body as? [String: Any], let operation = body["operation"] as? String else {
            replyHandler(nil, "仅工作台主界面可操作工作区。"); return
        }
        if operation == "open_catalog_workspace" {
            // 页面只给目录条目的标识；要打开哪个文件夹由本机服务按该条目记录的工作目录回答。
            guard Set(body.keys) == Set(["operation", "catalog_id"]), let token = backendToken,
                  let id = body["catalog_id"] as? String,
                  id.range(of: "^cli-[0-9a-f]{32}$", options: .regularExpression) != nil,
                  var components = URLComponents(url: ready, resolvingAgainstBaseURL: false) else {
                replyHandler(nil, "无法识别要打开的终端会话。"); return
            }
            components.path = "/catalog/sessions/\(id)/history"
            components.queryItems = [URLQueryItem(name: "cursor", value: "0"), URLQueryItem(name: "limit", value: "1")]
            components.fragment = nil
            guard let url = components.url else { replyHandler(nil, "会话地址无效。"); return }
            var request = URLRequest(url: url)
            request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
            let configuration = URLSessionConfiguration.ephemeral
            configuration.timeoutIntervalForRequest = 15
            configuration.httpCookieStorage = nil
            configuration.urlCache = nil
            let lookup = URLSession(configuration: configuration, delegate: LocalLookupDelegate(), delegateQueue: nil)
            lookup.dataTask(with: request) { [weak self] data, response, _ in
                defer { lookup.finishTasksAndInvalidate() }
                var folder: String?
                if (response as? HTTPURLResponse)?.statusCode == 200, let data = data,
                   let record = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
                   let workspace = record["workspace"] as? String, workspace.hasPrefix("/") { folder = workspace }
                DispatchQueue.main.async {
                    guard let self = self, self.readyURL == ready, self.backendToken == token, let folder = folder else {
                        replyHandler(nil, "这场会话已变化或其工作目录不可用，请刷新后重试。"); return
                    }
                    self.coordinator?.open(URL(fileURLWithPath: folder), from: self)
                    replyHandler(["opened": true], nil)
                }
            }.resume()
            return
        }
        if operation == "notification_permission" {
            UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound]) { allowed, error in
                DispatchQueue.main.async { if let error = error { replyHandler(nil, error.localizedDescription) } else { replyHandler(["enabled": allowed], nil) } }
            }
            return
        }
        if operation == "notify" {
            guard let title = body["title"] as? String, let text = body["body"] as? String,
                  let sid = body["session_id"] as? String,
                  sid.range(of: "^[A-Za-z0-9_-]{1,100}$", options: .regularExpression) != nil else {
                replyHandler(nil, "通知内容无效。"); return
            }
            let center = UNUserNotificationCenter.current()
            center.getNotificationSettings { settings in
                guard settings.authorizationStatus == .authorized || settings.authorizationStatus == .provisional else {
                    DispatchQueue.main.async { replyHandler(["delivered": false, "reason": "permission_required"], nil) }; return
                }
                let content = UNMutableNotificationContent()
                content.title = String(title.prefix(150)); content.body = String(text.prefix(1000))
                content.userInfo = ["session_id": sid, "workspace": self.mode ?? ""]; content.sound = .default
                center.add(UNNotificationRequest(identifier: UUID().uuidString, content: content, trigger: nil)) { error in
                    DispatchQueue.main.async { if let error = error { replyHandler(nil, error.localizedDescription) } else { replyHandler(["delivered": true], nil) } }
                }
            }
            return
        }
        if operation == "choose_project_folders" {
            guard Set(body.keys) == Set(["operation"]), let target = web.window else {
                replyHandler(nil, "当前窗口无法选择文件夹。"); return
            }
            let chooser = NSOpenPanel()
            chooser.canChooseFiles = false; chooser.canChooseDirectories = true
            chooser.allowsMultipleSelection = true
            chooser.prompt = "添加到项目"
            chooser.message = "项目中的新对话可以使用这些文件夹。"
            chooser.beginSheetModal(for: target) { result in
                guard result == .OK else { replyHandler(["cancelled": true], nil); return }
                replyHandler(["folders": chooser.urls.map { $0.resolvingSymlinksInPath().path }], nil)
            }
            return
        }
        if operation == "choose_references" {
            guard Set(body.keys) == Set(["operation"]), let target = web.window else {
                replyHandler(nil, "当前窗口无法选择文件。"); return
            }
            let chooser = NSOpenPanel()
            chooser.canChooseFiles = true; chooser.canChooseDirectories = true
            chooser.allowsMultipleSelection = true
            chooser.prompt = "添加到任务"
            chooser.message = "选择文件或文件夹，发送任务后按需读取。"
            chooser.directoryURL = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask).first
            chooser.beginSheetModal(for: target) { result in
                guard result == .OK else { replyHandler(["cancelled": true], nil); return }
                guard chooser.urls.count <= 20 else { replyHandler(nil, "一次最多添加 20 项，请分批选择。"); return }
                let items = chooser.urls.map { url -> [String: String] in
                    let directory = (try? url.resourceValues(forKeys: [.isDirectoryKey]))?.isDirectory == true
                    return ["path": url.path, "kind": directory ? "directory" : "file"]
                }
                replyHandler(["items": items], nil)
            }
            return
        }
        if operation == "export_text" {
            guard let text = body["text"] as? String, text.utf8.count <= 2 * 1024 * 1024,
                  let filename = body["filename"] as? String, let target = web.window else {
                replyHandler(nil, "导出文本过大或窗口已关闭。"); return
            }
            let clean = filename.components(separatedBy: CharacterSet.controlCharacters.union(CharacterSet(charactersIn: "/\\:"))).joined(separator: "_")
            let save = NSSavePanel()
            save.nameFieldStringValue = String(clean.prefix(180)).isEmpty ? "导出.txt" : String(clean.prefix(180))
            save.canCreateDirectories = true
            save.beginSheetModal(for: target) { result in
                guard result == .OK, let url = save.url else { replyHandler(["saved": false, "cancelled": true], nil); return }
                do { try text.write(to: url, atomically: true, encoding: .utf8); replyHandler(["saved": true, "filename": url.lastPathComponent], nil) }
                catch { replyHandler(nil, "无法保存文件：\(error.localizedDescription)") }
            }
            return
        }
        if operation == "new_window" {
            var config = launchConfig ?? [:]
            if let sid = body["session_id"] as? String,
               sid.range(of: "^[A-Za-z0-9_-]{1,100}$", options: .regularExpression) != nil { config["session_id"] = sid }
            openWorkbenchWindow(config: config)
            replyHandler(["opened": true], nil); return
        }
        let owner = ObjectIdentifier(web)
        if operation == "hide" {
            browserPanes[owner]?.values.forEach { $0.view.isHidden = true }
            replyHandler(["hidden": true], nil); return
        }
        guard let id = body["id"] as? String,
              id.range(of: "^[A-Za-z0-9_-]{1,100}$", options: .regularExpression) != nil else {
            replyHandler(nil, "网页标签标识无效。"); return
        }
        if operation == "browser_open" {
            guard browserPanes[owner]?[id] == nil, (browserPanes[owner]?.count ?? 0) < 16,
                  let raw = body["url"] as? String, raw.count <= 8192, let url = URL(string: raw) else {
                replyHandler(nil, "网页地址或标签数量不正确。"); return
            }
            let pane = BrowserPane(blockedOrigin: ready)
            guard pane.allowed(url) else { replyHandler(nil, "仅支持外部 HTTP / HTTPS 网页。"); return }
            browserPanes[owner, default: [:]][id] = pane
            web.addSubview(pane.view); pane.view.load(URLRequest(url: url))
            replyHandler(pane.state(), nil); return
        }
        guard let pane = browserPanes[owner]?[id] else { replyHandler(nil, "网页标签已关闭。"); return }
        switch operation {
        case "browser_navigate":
            guard let raw = body["url"] as? String, raw.count <= 8192, let url = URL(string: raw), pane.allowed(url) else {
                replyHandler(nil, "网页地址无效。"); return
            }
            pane.view.load(URLRequest(url: url))
        case "browser_back": pane.view.goBack()
        case "browser_forward": pane.view.goForward()
        case "browser_reload": pane.view.reload()
        case "browser_close": pane.view.stopLoading(); pane.view.removeFromSuperview(); browserPanes[owner]?.removeValue(forKey: id)
        case "browser_layout":
            browserPanes[owner]?.values.forEach { $0.view.isHidden = true }
            guard let x = body["x"] as? Double, let y = body["y"] as? Double,
                  let width = body["width"] as? Double, let height = body["height"] as? Double,
                  [x, y, width, height].allSatisfy({ $0.isFinite }), x >= 0, y >= 0, width >= 0, height >= 0,
                  x + width <= web.bounds.width + 2, y + height <= web.bounds.height + 2 else {
                replyHandler(nil, "网页区域无效。"); return
            }
            let frameY = web.isFlipped ? y : web.bounds.height - y - height
            pane.view.frame = NSRect(x: x, y: frameY, width: width, height: height)
            pane.view.isHidden = width < 10 || height < 10
        case "browser_selection":
            let selectedURL = pane.view.url
            pane.view.evaluateJavaScript("String(window.getSelection ? window.getSelection().toString() : '').slice(0, 16000)") { result, error in
                guard error == nil, pane.view.url == selectedURL, let text = result as? String else { replyHandler(nil, "网页已变化或无法读取选中内容。"); return }
                replyHandler(["text": String(text.prefix(16000)), "url": pane.view.url?.absoluteString ?? "",
                    "title": pane.view.title ?? "网页", "captured_at": ISO8601DateFormatter().string(from: Date()), "source": "user_selection"], nil)
            }
            return
        case "browser_state": break
        default: replyHandler(nil, "工作区操作未实现。"); return
        }
        replyHandler(pane.state(), nil)
    }

    private func openConnectionAuthorization(_ message: WKScriptMessage,
                                             replyHandler: @escaping (Any?, String?) -> Void) {
        guard !stopping, !quitting, message.frameInfo.isMainFrame, message.webView.map({ candidate in self.workWindows.values.contains(where: { $0 === candidate }) }) == true,
              let ready = readyURL, let token = backendToken,
              let origin = message.frameInfo.request.url,
              origin.scheme == ready.scheme, origin.host == ready.host,
              origin.port == ready.port, origin.path == ready.path,
              let body = message.body as? [String: Any],
              Set(body.keys) == Set(["connection_id", "authorization_id"]),
              let cid = body["connection_id"] as? String,
              let aid = body["authorization_id"] as? String,
              cid.range(of: "^[A-Za-z0-9_-]{1,100}$", options: .regularExpression) != nil,
              aid.range(of: "^[A-Za-z0-9_-]{1,100}$", options: .regularExpression) != nil else {
            replyHandler(nil, "无法识别授权请求。"); return
        }
        var components = URLComponents(url: ready, resolvingAgainstBaseURL: false)!
        components.path = "/desktop/connections/\(cid)/authorizations/\(aid)/open-url"
        components.query = nil; components.fragment = nil
        guard let url = components.url else { replyHandler(nil, "授权入口无效。"); return }
        var request = URLRequest(url: url)
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        let configuration = URLSessionConfiguration.ephemeral
        configuration.timeoutIntervalForRequest = 5
        configuration.httpCookieStorage = nil; configuration.urlCache = nil
        let lookup = URLSession(configuration: configuration, delegate: LocalLookupDelegate(), delegateQueue: nil)
        lookup.dataTask(with: request) { [weak self] data, response, _ in
            defer { lookup.finishTasksAndInvalidate() }
            var target: URL?
            if (response as? HTTPURLResponse)?.statusCode == 200, let data = data, data.count <= 32768,
               let record = (try? JSONSerialization.jsonObject(with: data)) as? [String: String],
               let raw = record["url"], raw.count <= 16384, let candidate = URL(string: raw),
               candidate.user == nil, candidate.password == nil, candidate.fragment == nil,
               let host = candidate.host, !host.isEmpty,
               candidate.scheme == "https" || candidate.scheme == "http" && ["127.0.0.1", "localhost", "::1", "[::1]"].contains(host) {
                target = candidate
            }
            DispatchQueue.main.async {
                guard let self = self, self.readyURL == ready, self.backendToken == token,
                      let target = target, NSWorkspace.shared.open(target) else {
                    replyHandler(nil, "授权已失效或浏览器无法打开，请刷新授权状态。"); return
                }
                replyHandler(["opened": true], nil)
            }
        }.resume()
    }
    private func showViewFailure(_ web: WKWebView, message: String) {
        guard let target = workWindows.first(where: { $0.value === web })?.key else { return }
        let alert = NSAlert(); alert.messageText = "界面暂时不可用"; alert.informativeText = message
        alert.addButton(withTitle: "重新加载界面"); alert.addButton(withTitle: "稍后")
        alert.beginSheetModal(for: target) { result in if result == .alertFirstButtonReturn { web.reload() } }
    }
    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        if !stopping && !quitting && (error as NSError).code != NSURLErrorCancelled { showViewFailure(webView, message: error.localizedDescription) }
    }
    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        if !stopping && !quitting && (error as NSError).code != NSURLErrorCancelled { showViewFailure(webView, message: error.localizedDescription) }
    }
    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
        if !stopping && !quitting { showViewFailure(webView, message: "界面进程已退出。后台任务仍继续运行。") }
    }
}

@main
private enum DesktopMain {
    static func main() {
        let application = NSApplication.shared
        let delegate = DesktopApp()
        application.delegate = delegate
        withExtendedLifetime(delegate) { application.run() }
    }
}
