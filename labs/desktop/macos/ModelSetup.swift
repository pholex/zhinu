import AppKit
import Security

struct ModelSetupProvider: Decodable {
    let id: String
    let label: String
    let keyEnvs: [String]
    let models: [String]
}

enum ModelCredentialPresence {
    case missing
    case available
    case authorizationRequired
}

protocol ModelSetupCredentialStore {
    func presence(service: String, account: String) -> ModelCredentialPresence
    func read(service: String, account: String, allowInteraction: Bool) throws -> String?
    func save(_ value: String, service: String, account: String) throws
}

enum ModelSetupError: LocalizedError {
    case noSelection
    case unknownProvider
    case emptyKey
    case invalidKey
    case missingCredential
    case authorizationDeclined
    case credentialReadFailed
    case credentialSaveFailed

    var errorDescription: String? {
        switch self {
        case .noSelection: return "请先启用至少一家模型服务。"
        case .unknownProvider: return "无法识别此模型服务，请重新打开配置界面。"
        case .emptyKey: return "请粘贴 API Key 后再保存。"
        case .invalidKey: return "密钥格式不正确：请使用不含换行的单行密钥，长度不超过 16 KB。"
        case .missingCredential: return "没有读取到密钥，请粘贴 API Key 或检查钥匙串中的条目。"
        case .authorizationDeclined: return "未获钥匙串读取授权，此服务尚未启用。你可以再次点击启用。"
        case .credentialReadFailed: return "暂时无法读取此密钥。请检查钥匙串后再次启用，或重新保存密钥。"
        case .credentialSaveFailed: return "密钥未保存成功，此次没有启用服务。请检查钥匙串权限后重试。"
        }
    }
}

final class SystemModelCredentialStore: ModelSetupCredentialStore {
    // All callers are synchronous. Restore the process-wide legacy setting immediately.
    private func withInteraction<T>(_ allowed: Bool, action: () throws -> T) throws -> T {
        if allowed { return try action() }
        var previous: DarwinBoolean = true
        guard SecKeychainGetUserInteractionAllowed(&previous) == errSecSuccess,
              SecKeychainSetUserInteractionAllowed(false) == errSecSuccess else {
            throw ModelSetupError.authorizationDeclined
        }
        defer { SecKeychainSetUserInteractionAllowed(previous.boolValue) }
        return try action()
    }

    private func identity(service: String, account: String) -> [String: Any] {
        [kSecClass as String: kSecClassGenericPassword,
         kSecAttrService as String: service,
         kSecAttrAccount as String: account]
    }

    func presence(service: String, account: String) -> ModelCredentialPresence {
        var query = identity(service: service, account: account)
        query[kSecReturnAttributes as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne
        query[kSecUseAuthenticationUI as String] = kSecUseAuthenticationUIFail
        var attributes: CFTypeRef?
        let status = (try? withInteraction(false) {
            SecItemCopyMatching(query as CFDictionary, &attributes)
        }) ?? errSecInteractionNotAllowed
        switch status {
        case errSecSuccess: return .available
        case errSecItemNotFound: return .missing
        default: return .authorizationRequired
        }
    }

    func read(service: String, account: String, allowInteraction: Bool) throws -> String? {
        var query = identity(service: service, account: account)
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne
        query[kSecUseAuthenticationUI as String] = allowInteraction ? kSecUseAuthenticationUIAllow : kSecUseAuthenticationUIFail
        if allowInteraction {
            query[kSecUseOperationPrompt as String] = "允许羽案启用你选择的模型服务"
        }
        var result: CFTypeRef?
        let status = try withInteraction(allowInteraction) {
            SecItemCopyMatching(query as CFDictionary, &result)
        }
        if status == errSecItemNotFound { return nil }
        if [errSecUserCanceled, errSecAuthFailed, errSecInteractionNotAllowed].contains(status) {
            throw ModelSetupError.authorizationDeclined
        }
        guard status == errSecSuccess, let data = result as? Data,
              let value = String(data: data, encoding: .utf8) else {
            throw ModelSetupError.credentialReadFailed
        }
        return value
    }

    func save(_ value: String, service: String, account: String) throws {
        var query = identity(service: service, account: account)
        query[kSecUseAuthenticationUI as String] = kSecUseAuthenticationUIAllow
        let values = [kSecValueData as String: Data(value.utf8)]
        let updated = SecItemUpdate(query as CFDictionary, values as CFDictionary)
        if updated == errSecSuccess { return }
        guard updated == errSecItemNotFound else { throw ModelSetupError.credentialSaveFailed }
        query[kSecValueData as String] = Data(value.utf8)
        guard SecItemAdd(query as CFDictionary, nil) == errSecSuccess else {
            throw ModelSetupError.credentialSaveFailed
        }
    }
}

private final class ModelSetupDocument: NSView {
    override var isFlipped: Bool { true }
}

private final class ModelSetupRow: NSView {
    override func viewDidChangeEffectiveAppearance() {
        super.viewDidChangeEffectiveAppearance()
        updateColors()
    }
    func updateColors() {
        effectiveAppearance.performAsCurrentDrawingAppearance {
            layer?.backgroundColor = NSColor.controlBackgroundColor.cgColor
            layer?.borderColor = NSColor.separatorColor.withAlphaComponent(0.25).cgColor
        }
    }
}

final class ModelSetupController: NSObject {
    static let selectedProvidersKey = "xiaoyu.desktop.modelSetup.providers"
    static let disabledProvidersKey = "xiaoyu.desktop.modelSetup.disabledProviders"
    static let explicitSelectionKey = "xiaoyu.desktop.modelSetup.explicitSelection"

    private let providers: [ModelSetupProvider]
    private let environment: [String: String]
    private let defaults: UserDefaults
    private let credentialStore: ModelSetupCredentialStore
    private let account: String
    private var detected: [String: ModelCredentialPresence] = [:]
    private var activeKeys: [String: String] = [:]
    private var toggles: [String: NSButton] = [:]
    private var fields: [String: NSSecureTextField] = [:]
    private var statusLabels: [String: NSTextField] = [:]
    private var saveButtons: [String: NSButton] = [:]
    private var editors: [String: NSStackView] = [:]
    private var replacements: [String: NSButton] = [:]
    private var spacers: [String: NSView] = [:]
    private var editingKeys = Set<String>()
    private var attemptedAutomaticAuthorization = Set<String>()
    private var globalStatus: NSTextField?
    private var continueButton: NSButton?
    private var onContinue: (() -> Void)?

    init(providers: [ModelSetupProvider], environment: [String: String] = ProcessInfo.processInfo.environment,
         defaults: UserDefaults = .standard,
         credentialStore: ModelSetupCredentialStore = SystemModelCredentialStore()) {
        self.providers = providers
        self.environment = environment
        self.defaults = defaults
        self.credentialStore = credentialStore
        let inheritedUser = environment["USER"]?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        self.account = inheritedUser.isEmpty ? NSUserName() : inheritedUser
        super.init()
        let explicit = defaults.bool(forKey: Self.explicitSelectionKey)
        let remembered = Set(defaults.stringArray(forKey: Self.selectedProvidersKey) ?? [])
        for provider in providers {
            let shouldEnable = !explicit || remembered.contains(provider.id)
            if let value = inheritedKey(for: provider) {
                detected[provider.id] = .available
                if shouldEnable { activeKeys[provider.id] = value }
                continue
            }
            let probes = provider.keyEnvs.map { credentialStore.presence(service: $0, account: account) }
            detected[provider.id] = probes.contains(where: { $0 == .available }) ? .available
                : probes.contains(where: { $0 == .authorizationRequired }) ? .authorizationRequired : .missing
            if shouldEnable && (detected[provider.id] == .available || explicit) {
                // Enable readable local credentials by default, preserving explicit exclusions.
                // Protected entries still require the user's system authorization.
                if let value = try? storedKey(for: provider, allowInteraction: false) {
                    activeKeys[provider.id] = value
                }
            }
        }
    }

    func enableDetectedKeychainProviders() {
        let disabled = Set(defaults.stringArray(forKey: Self.disabledProvidersKey) ?? [])
        for provider in providers {
            guard !isEnabled(provider.id), inheritedKey(for: provider) == nil,
                  detected[provider.id] != .missing, !disabled.contains(provider.id),
                  attemptedAutomaticAuthorization.insert(provider.id).inserted else { continue }
            do {
                guard let value = try storedKey(for: provider, allowInteraction: true) else {
                    throw ModelSetupError.missingCredential
                }
                activeKeys[provider.id] = value
                detected[provider.id] = .available
                persistSelection()
                refreshRows()
            } catch {
                showError((error as? ModelSetupError) ?? ModelSetupError.credentialReadFailed, provider: provider.id)
            }
        }
    }
    private func rememberDisabled(_ id: String, _ disabled: Bool) {
        var ids = Set(defaults.stringArray(forKey: Self.disabledProvidersKey) ?? [])
        if disabled { ids.insert(id) } else { ids.remove(id) }
        defaults.set(ids.sorted(), forKey: Self.disabledProvidersKey)
    }

    var disabledProviderIDs: [String] { defaults.stringArray(forKey: Self.disabledProvidersKey) ?? [] }
    var hasExistingConfiguration: Bool {
        if ["XIAOYU_MODEL", "XIAOYU_API_KEY", "XIAOYU_BASE_URL", "XIAOYU_PROVIDERS"].contains(where: { nonempty(environment[$0]) != nil }) { return true }
        let base = environment["XDG_CONFIG_HOME"] ?? NSHomeDirectory() + "/.config"
        let path = environment["XIAOYU_ENV_FILE"] ?? base + "/xiaoyu/.env"
        return FileManager.default.fileExists(atPath: (path as NSString).expandingTildeInPath)
    }
    var selectedProviderIDs: [String] { providers.filter { activeKeys[$0.id] != nil }.map(\.id) }
    func isEnabled(_ id: String) -> Bool { activeKeys[id] != nil }
    func presence(for id: String) -> ModelCredentialPresence { detected[id] ?? .missing }

    private func provider(_ id: String) throws -> ModelSetupProvider {
        guard let provider = providers.first(where: { $0.id == id }), !provider.keyEnvs.isEmpty else {
            throw ModelSetupError.unknownProvider
        }
        return provider
    }
    private func nonempty(_ value: String?) -> String? {
        guard let result = value?.trimmingCharacters(in: .whitespacesAndNewlines), !result.isEmpty else { return nil }
        return result
    }
    private func inheritedKey(for provider: ModelSetupProvider) -> String? {
        provider.keyEnvs.lazy.compactMap { self.nonempty(self.environment[$0]) }.first
    }
    private func storedKey(for provider: ModelSetupProvider, allowInteraction: Bool) throws -> String? {
        for variable in provider.keyEnvs {
            if let value = nonempty(try credentialStore.read(service: variable, account: account, allowInteraction: allowInteraction)) {
                return value
            }
        }
        return nil
    }
    private func persistSelection() {
        defaults.set(selectedProviderIDs, forKey: Self.selectedProvidersKey)
        defaults.set(true, forKey: Self.explicitSelectionKey)
    }

    func enableProvider(_ id: String) throws {
        let entry = try provider(id)
        do {
            guard let value = try inheritedKey(for: entry) ?? storedKey(for: entry, allowInteraction: true) else {
                throw ModelSetupError.missingCredential
            }
            activeKeys[id] = value
            rememberDisabled(id, false)
            detected[id] = .available
            persistSelection()
            refreshRows()
        } catch {
            activeKeys.removeValue(forKey: id)
            persistSelection()
            refreshRows()
            throw (error as? ModelSetupError) ?? ModelSetupError.credentialReadFailed
        }
    }
    func disableProvider(_ id: String) {
        rememberDisabled(id, true)
        activeKeys.removeValue(forKey: id)
        persistSelection()
        refreshRows()
    }
    func saveKey(_ value: String, for id: String) throws {
        let entry = try provider(id)
        guard let secret = nonempty(value), let primary = entry.keyEnvs.first else { throw ModelSetupError.emptyKey }
        guard secret.utf8.count <= 16_384,
              !secret.contains("\0"), secret.rangeOfCharacter(from: .newlines) == nil else {
            throw ModelSetupError.invalidKey
        }
        do {
            try credentialStore.save(secret, service: primary, account: account)
        } catch {
            activeKeys.removeValue(forKey: id)
            persistSelection()
            refreshRows()
            throw ModelSetupError.credentialSaveFailed
        }
        // An explicitly inherited credential keeps the same priority as the core.
        activeKeys[id] = inheritedKey(for: entry) ?? secret
        rememberDisabled(id, false)
        detected[id] = .available
        fields[id]?.stringValue = ""
        editingKeys.remove(id)
        persistSelection()
        refreshRows()
    }

    func launchEnvironment() throws -> [String: String] {
        let selected = providers.filter { activeKeys[$0.id] != nil }
        guard !selected.isEmpty else { throw ModelSetupError.noSelection }
        guard let model = selected.first?.models.first, !model.isEmpty else {
            throw ModelSetupError.unknownProvider
        }
        var result: [String: String] = ["XIAOYU_PROVIDERS": selected.map(\.id).joined(separator: ",")]
        result["XIAOYU_MODEL"] = model
        for provider in selected {
            guard let variable = provider.keyEnvs.first, let value = activeKeys[provider.id] else {
                throw ModelSetupError.missingCredential
            }
            result[variable] = value
        }
        return result
    }

    func makeView(logo: NSImage?, autoStart: Bool = false, workspaceDescription: String = "~/Documents/羽案/Default", onContinue: @escaping () -> Void) -> NSView {
        self.onContinue = onContinue
        toggles.removeAll(); fields.removeAll(); statusLabels.removeAll(); saveButtons.removeAll(); editors.removeAll(); replacements.removeAll(); spacers.removeAll(); editingKeys.removeAll()
        let root = NSView()
        root.identifier = NSUserInterfaceItemIdentifier("model-setup")
        let header = NSStackView()
        header.orientation = .horizontal; header.alignment = .centerY; header.spacing = 16
        let image = NSImageView()
        image.identifier = NSUserInterfaceItemIdentifier("welcome-logo")
        image.image = logo ?? bundledLogo()
        image.imageScaling = .scaleProportionallyUpOrDown
        image.setAccessibilityLabel("羽案")
        image.translatesAutoresizingMaskIntoConstraints = false
        NSLayoutConstraint.activate([image.widthAnchor.constraint(equalToConstant: 56), image.heightAnchor.constraint(equalToConstant: 56)])
        let heading = NSStackView()
        heading.orientation = .vertical; heading.alignment = .leading; heading.spacing = 6
        heading.addArrangedSubview(label("欢迎使用羽案", size: 24, weight: .semibold))
        heading.addArrangedSubview(label("默认沿用 xiaoyu CLI 配置；本机可读取的密钥会自动启用，也可以补充新密钥。启用只表示配置已就绪，不代表已通过联网验证。", size: 13, color: .secondaryLabelColor))
        header.addArrangedSubview(image); header.addArrangedSubview(heading)
        let scroll = NSScrollView()
        scroll.identifier = NSUserInterfaceItemIdentifier("provider-scroll")
        scroll.hasVerticalScroller = true; scroll.autohidesScrollers = true
        scroll.drawsBackground = false; scroll.borderType = .noBorder
        let document = ModelSetupDocument()
        document.translatesAutoresizingMaskIntoConstraints = false
        let rows = NSStackView()
        rows.orientation = .vertical; rows.alignment = .leading; rows.spacing = 4
        rows.translatesAutoresizingMaskIntoConstraints = false
        document.addSubview(rows)
        for (index, provider) in providers.enumerated() {
            let row = makeRow(provider, index: index)
            row.translatesAutoresizingMaskIntoConstraints = false
            rows.addArrangedSubview(row)
            row.widthAnchor.constraint(equalTo: rows.widthAnchor).isActive = true
        }
        scroll.documentView = document
        NSLayoutConstraint.activate([
            document.widthAnchor.constraint(equalTo: scroll.contentView.widthAnchor),
            rows.leadingAnchor.constraint(equalTo: document.leadingAnchor), rows.trailingAnchor.constraint(equalTo: document.trailingAnchor),
            rows.topAnchor.constraint(equalTo: document.topAnchor, constant: 4), rows.bottomAnchor.constraint(equalTo: document.bottomAnchor, constant: -8)
        ])
        let footer = NSView()
        let feedback = label("", size: 12, color: .secondaryLabelColor)
        feedback.identifier = NSUserInterfaceItemIdentifier("setup-status")
        globalStatus = feedback
        let actions = NSStackView()
        actions.orientation = .horizontal; actions.alignment = .centerY; actions.spacing = 12
        let next = NSButton(title: "开始使用", target: self, action: #selector(continueSetup))
        next.identifier = NSUserInterfaceItemIdentifier("setup-continue")
        next.bezelStyle = .rounded; next.keyEquivalent = "\r"
        next.image = NSImage(systemSymbolName: "folder", accessibilityDescription: nil)
        next.imagePosition = .imageLeading
        next.bezelColor = NSColor(calibratedRed: 0.275, green: 0.463, blue: 0.710, alpha: 1)
        continueButton = next
        actions.addArrangedSubview(next)
        let information = NSStackView()
        information.orientation = .vertical; information.alignment = .leading; information.spacing = 8
        information.setContentHuggingPriority(.defaultLow, for: .horizontal)
        let directory = label("默认工作目录：\(workspaceDescription)。以后可通过“文件 → 打开文件夹”更改。", size: 12, color: .secondaryLabelColor)
        for text in [directory, feedback] {
            text.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
            information.addArrangedSubview(text)
            text.widthAnchor.constraint(equalTo: information.widthAnchor).isActive = true
        }
        for button in [next] {
            button.controlSize = .large
            button.font = NSFont.systemFont(ofSize: 14, weight: .medium)
            button.heightAnchor.constraint(equalToConstant: 36).isActive = true
            button.setContentCompressionResistancePriority(.required, for: .horizontal)
        }
        next.widthAnchor.constraint(greaterThanOrEqualToConstant: 128).isActive = true
        actions.setContentHuggingPriority(.required, for: .horizontal)
        information.translatesAutoresizingMaskIntoConstraints = false
        actions.translatesAutoresizingMaskIntoConstraints = false
        footer.addSubview(information); footer.addSubview(actions)
        NSLayoutConstraint.activate([
            information.leadingAnchor.constraint(equalTo: footer.leadingAnchor),
            information.topAnchor.constraint(equalTo: footer.topAnchor),
            information.bottomAnchor.constraint(equalTo: footer.bottomAnchor),
            information.trailingAnchor.constraint(equalTo: actions.leadingAnchor, constant: -24),
            actions.trailingAnchor.constraint(equalTo: footer.trailingAnchor),
            actions.bottomAnchor.constraint(equalTo: footer.bottomAnchor),
            actions.topAnchor.constraint(greaterThanOrEqualTo: footer.topAnchor),
            actions.widthAnchor.constraint(equalToConstant: 128)
        ])
        for view in [header, scroll, footer] { view.translatesAutoresizingMaskIntoConstraints = false; root.addSubview(view) }
        NSLayoutConstraint.activate([
            header.topAnchor.constraint(equalTo: root.topAnchor, constant: 28), header.leadingAnchor.constraint(equalTo: root.leadingAnchor, constant: 36), header.trailingAnchor.constraint(lessThanOrEqualTo: root.trailingAnchor, constant: -36),
            scroll.topAnchor.constraint(equalTo: header.bottomAnchor, constant: 24), scroll.leadingAnchor.constraint(equalTo: root.leadingAnchor, constant: 36), scroll.trailingAnchor.constraint(equalTo: root.trailingAnchor, constant: -36),
            footer.topAnchor.constraint(equalTo: scroll.bottomAnchor, constant: 16), footer.leadingAnchor.constraint(equalTo: root.leadingAnchor, constant: 36), footer.trailingAnchor.constraint(equalTo: root.trailingAnchor, constant: -36), footer.bottomAnchor.constraint(equalTo: root.bottomAnchor, constant: -24),
            scroll.heightAnchor.constraint(greaterThanOrEqualToConstant: 120)
        ])
        refreshRows()
        DispatchQueue.main.async { [weak self, weak root] in
            guard root?.window != nil else { return }
            self?.enableDetectedKeychainProviders()
            if autoStart, self?.selectedProviderIDs.isEmpty == false || self?.hasExistingConfiguration == true { self?.continueSetup() }
        }
        return root
    }

    private func makeRow(_ provider: ModelSetupProvider, index: Int) -> NSView {
        let row = ModelSetupRow()
        row.identifier = NSUserInterfaceItemIdentifier("provider.\(provider.id)")
        row.wantsLayer = true
        row.layer?.cornerRadius = 10
        row.layer?.borderWidth = 0.5
        row.updateColors()
        let title = label(provider.label, size: 14, weight: .semibold)
        title.identifier = NSUserInterfaceItemIdentifier("provider.\(provider.id).title")
        let variable = NSTextField(labelWithString: provider.keyEnvs.first ?? "")
        variable.font = NSFont.systemFont(ofSize: 10)
        variable.textColor = .tertiaryLabelColor
        variable.lineBreakMode = .byTruncatingMiddle
        variable.toolTip = provider.keyEnvs.joined(separator: ", ")
        let status = label("", size: 12, color: .secondaryLabelColor)
        status.alignment = .left
        status.identifier = NSUserInterfaceItemIdentifier("provider.\(provider.id).status")
        statusLabels[provider.id] = status
        let toggle = NSButton(checkboxWithTitle: "启用", target: self, action: #selector(toggleProvider(_:)))
        toggle.tag = index
        toggle.identifier = NSUserInterfaceItemIdentifier("provider.\(provider.id).toggle")
        toggle.setAccessibilityLabel("启用\(provider.label)")
        toggles[provider.id] = toggle
        let editor = NSStackView()
        editor.orientation = .horizontal; editor.alignment = .centerY; editor.spacing = 8
        let field = NSSecureTextField()
        field.identifier = NSUserInterfaceItemIdentifier("provider.\(provider.id).secure-key")
        field.placeholderString = "粘贴 API Key"
        field.setAccessibilityLabel("\(provider.label) API Key")
        field.font = NSFont.systemFont(ofSize: 13)
        field.translatesAutoresizingMaskIntoConstraints = false
        field.heightAnchor.constraint(equalToConstant: 30).isActive = true
        field.setContentHuggingPriority(.defaultLow, for: .horizontal)
        fields[provider.id] = field
        let save = NSButton(title: "保存并启用", target: self, action: #selector(saveProvider(_:)))
        save.tag = index
        save.identifier = NSUserInterfaceItemIdentifier("provider.\(provider.id).save")
        save.bezelStyle = .rounded
        saveButtons[provider.id] = save
        editor.addArrangedSubview(field); editor.addArrangedSubview(save)
        editors[provider.id] = editor
        let replace = NSButton(title: "更换密钥", target: self, action: #selector(replaceKey(_:)))
        replace.tag = index; replace.bezelStyle = .rounded
        replace.identifier = NSUserInterfaceItemIdentifier("provider.\(provider.id).replace")
        replacements[provider.id] = replace
        let content = NSStackView()
        content.orientation = .vertical; content.alignment = .leading; content.spacing = 4
        let heading = NSStackView()
        heading.orientation = .horizontal; heading.alignment = .centerY; heading.spacing = 8
        let names = NSStackView()
        names.orientation = .vertical; names.alignment = .leading; names.spacing = 2
        names.widthAnchor.constraint(equalToConstant: 150).isActive = true
        names.addArrangedSubview(title); names.addArrangedSubview(variable)
        variable.widthAnchor.constraint(equalTo: names.widthAnchor).isActive = true
        let spacer = NSView()
        spacer.setContentHuggingPriority(.defaultLow, for: .horizontal)
        spacers[provider.id] = spacer
        heading.addArrangedSubview(names); heading.addArrangedSubview(editor); heading.addArrangedSubview(spacer)
        heading.addArrangedSubview(replace); heading.addArrangedSubview(toggle)
        editor.setContentHuggingPriority(.defaultLow, for: .horizontal)
        for button in [save, replace, toggle] { button.setContentCompressionResistancePriority(.required, for: .horizontal) }
        content.addArrangedSubview(heading); content.addArrangedSubview(status)
        content.translatesAutoresizingMaskIntoConstraints = false
        row.addSubview(content)
        NSLayoutConstraint.activate([
            heading.widthAnchor.constraint(equalTo: content.widthAnchor),
            status.widthAnchor.constraint(equalTo: content.widthAnchor),
            content.topAnchor.constraint(equalTo: row.topAnchor, constant: 7), content.bottomAnchor.constraint(equalTo: row.bottomAnchor, constant: -7),
            content.leadingAnchor.constraint(equalTo: row.leadingAnchor, constant: 16), content.trailingAnchor.constraint(equalTo: row.trailingAnchor, constant: -16)
        ])
        return row
    }

    private func refreshRows() {
        for entry in providers {
            let enabled = isEnabled(entry.id)
            toggles[entry.id]?.state = enabled ? .on : .off
            statusLabels[entry.id]?.textColor = .secondaryLabelColor
            let inherited = inheritedKey(for: entry) != nil
            toggles[entry.id]?.isEnabled = inherited || presence(for: entry.id) != .missing
            statusLabels[entry.id]?.stringValue = enabled ? (inherited ? "已启用 · 来自环境变量" : "已启用 · 来自钥匙串")
                : inherited ? "已检测到应用环境中的密钥，勾选即可启用。"
                : presence(for: entry.id) == .missing ? "可在此补充密钥，已有 CLI 配置会自动读取。"
                : presence(for: entry.id) == .available ? "已检测到钥匙串条目，点击启用并完成系统授权；无需重新填写。"
                : "钥匙串需要授权，点击启用后按系统提示操作；无需重新填写。"
            let known = enabled || inherited || presence(for: entry.id) != .missing
            let editing = editingKeys.contains(entry.id)
            let showEditor = !inherited && (!known || editing)
            editors[entry.id]?.isHidden = !showEditor
            spacers[entry.id]?.isHidden = showEditor
            replacements[entry.id]?.isHidden = inherited || !known
            replacements[entry.id]?.title = editing ? "取消更换" : "更换密钥"
        }
        continueButton?.isEnabled = true
        globalStatus?.stringValue = selectedProviderIDs.isEmpty ? "默认沿用 xiaoyu CLI 配置，也可以在上方补充密钥。" : "已启用 \(selectedProviderIDs.count) 家服务；优先沿用已有默认模型，进入工作台后可切换。"
        globalStatus?.textColor = .secondaryLabelColor
    }
    private func showError(_ error: Error, provider id: String? = nil) {
        let safe = (error as? ModelSetupError)?.errorDescription ?? ModelSetupError.credentialReadFailed.errorDescription!
        if let id { statusLabels[id]?.stringValue = safe; statusLabels[id]?.textColor = .systemRed }
        globalStatus?.stringValue = safe; globalStatus?.textColor = .systemRed
    }
    @objc private func toggleProvider(_ sender: NSButton) {
        guard providers.indices.contains(sender.tag) else { return }
        let id = providers[sender.tag].id
        if sender.state == .off { disableProvider(id); return }
        do { try enableProvider(id) } catch { showError(error, provider: id) }
    }
    @objc private func replaceKey(_ sender: NSButton) {
        guard providers.indices.contains(sender.tag) else { return }
        let id = providers[sender.tag].id
        if editingKeys.contains(id) { editingKeys.remove(id); fields[id]?.stringValue = "" }
        else { editingKeys.insert(id) }
        refreshRows()
        if editingKeys.contains(id), let field = fields[id] { field.window?.makeFirstResponder(field) }
    }
    @objc private func saveProvider(_ sender: NSButton) {
        guard providers.indices.contains(sender.tag) else { return }
        let id = providers[sender.tag].id
        do { try saveKey(fields[id]?.stringValue ?? "", for: id) } catch { showError(error, provider: id) }
    }
    @objc private func continueSetup() {
        persistSelection()
        onContinue?()
    }
    private func label(_ value: String, size: CGFloat, weight: NSFont.Weight = .regular, color: NSColor = .labelColor) -> NSTextField {
        let text = NSTextField(wrappingLabelWithString: value)
        text.font = NSFont.systemFont(ofSize: size, weight: weight)
        text.textColor = color
        text.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        return text
    }
    private func bundledLogo() -> NSImage? {
        if let icon = Bundle.main.url(forResource: "AppIcon", withExtension: "icns"), let image = NSImage(contentsOf: icon) { return image }
        guard let resources = Bundle.main.resourceURL else { return nil }
        return NSImage(contentsOf: resources.appendingPathComponent("runtime/labs/desktop/web/logo.png"))
    }
}
