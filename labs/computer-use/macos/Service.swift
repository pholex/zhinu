// Xiaoyu Computer Use：macOS 原生辅助服务。
//
// 小羽本体是单进程 Python，不能直接碰 Accessibility / ScreenCaptureKit；这些能力
// 只能住在一个带 bundle id 的 .app 里（TCC 授权按 bundle 记）。这个进程就是那个
// 载体：它不做任何决策，只把"读一个应用窗口的无障碍树 + 截图"和"对某个元素或坐标
// 做一个动作"暴露成本地 socket 上的 JSON-RPC，审批、节奏、与模型的对话都在 Python 侧。
//
// 协议（见 labs/computer-use/README.md）：unix socket，每帧 = 4 字节小端长度 + UTF-8
// JSON-RPC 2.0；方法 ping / list_apps / get_state / perform。同一个可执行文件带参数启动时
// 则是该协议的命令行客户端（`XiaoyuComputerUse state 计算器`），不经小羽也能用。
//
// 设计取向：
// - 元素动作优先走无障碍动作（AXPress / AXSetValue / AXShowMenu …），不动鼠标、不抢
//   焦点；只有坐标点击、键入、滚动、拖拽这类没有无障碍等价物的操作才合成输入事件，
//   合成前把目标应用激活到前台——用的全是公开 API，代价是会打断用户正在用的窗口。
// - 元素索引在同一应用内尽量稳定：按 (父路径, 角色, 同角色序号, 标识/标题) 记身份，
//   值变了索引不变，新元素拿新号。Python 侧据此做增量 diff。
// - 坐标统一用"窗口坐标单位"：窗口左上角为原点，截图像素与之一一对应（大窗口按
//   比例缩小，截图与坐标一起缩），模型看图报的点直接能用。

import AppKit
import ApplicationServices
import Carbon.HIToolbox
import Darwin
import Foundation
import ImageIO
import ScreenCaptureKit
import UniformTypeIdentifiers

let apiVersion = "xiaoyu-computer-use/1"
let maxFrameBytes = 16 * 1024 * 1024
let maxNodes = 1500
let maxDepth = 40
let maxEdge: CGFloat = 1600
let valueCap = 240

// MARK: - 错误

struct ServiceError: Error {
    let code: Int
    let message: String
    static func permissions(_ what: String) -> ServiceError {
        ServiceError(code: -32001, message: "未授权：\(what)。请到「系统设置 → 隐私与安全性」里为「Xiaoyu Computer Use」打开对应权限后重试。")
    }
    static func appNotFound(_ q: String) -> ServiceError {
        ServiceError(code: -32002, message: "找不到应用「\(q)」：既没在运行，也没在 /Applications、/System/Applications、~/Applications 里找到同名 .app。用 list_apps 看可用名字，或改用 bundle id / 完整路径。")
    }
    static func ambiguous(_ q: String, _ names: [String]) -> ServiceError {
        ServiceError(code: -32003, message: "「\(q)」匹配到多个应用：\(names.joined(separator: "、"))。请用 bundle id 指名。")
    }
    static func stale(_ index: Int) -> ServiceError {
        ServiceError(code: -32004, message: "元素 [\(index)] 不在最近一次 get_state 的树里（界面可能已变化）。先 get_state 再用新索引。")
    }
    static func noWindow(_ app: String) -> ServiceError {
        ServiceError(code: -32005, message: "应用「\(app)」当前没有可操作的窗口（可能还在启动，或只有菜单栏）。稍后再 get_state，或先用 key / click 让它开一个窗口。")
    }
    static func unsupported(_ what: String) -> ServiceError {
        ServiceError(code: -32006, message: what)
    }
    static func locked() -> ServiceError {
        ServiceError(code: -32007, message: "屏幕已锁定：锁屏期间读不到任何窗口、也不会合成输入。请用户解锁后再试。")
    }
    static func invalid(_ what: String) -> ServiceError {
        ServiceError(code: -32602, message: what)
    }
}

// MARK: - 权限

enum Permissions {
    static func ensureAccessibility() throws {
        if !AXIsProcessTrusted() {
            let options = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary
            _ = AXIsProcessTrustedWithOptions(options)
            throw ServiceError.permissions("辅助功能（Accessibility）")
        }
    }
    static func ensureScreenCapture() throws {
        if !CGPreflightScreenCaptureAccess() {
            _ = CGRequestScreenCaptureAccess()
            throw ServiceError.permissions("屏幕录制（Screen Recording，用于窗口截图）")
        }
    }
    static func ensureUnlocked() throws {
        //  拿不到会话信息按锁定处理；快速切换用户后我们这个会话不在控制台上，也等同锁定
        guard let session = CGSessionCopyCurrentDictionary() as? [String: Any] else { throw ServiceError.locked() }
        if (session["CGSSessionScreenIsLocked"] as? Bool) == true { throw ServiceError.locked() }
        if let onConsole = session[kCGSessionOnConsoleKey as String] as? Bool, !onConsole { throw ServiceError.locked() }
    }
}

// MARK: - 应用解析

struct AppRef {
    let app: NSRunningApplication
    var id: String { app.bundleIdentifier ?? app.executableURL?.path ?? "pid:\(app.processIdentifier)" }
    var name: String { app.localizedName ?? id }
    var pid: pid_t { app.processIdentifier }
    func json() -> [String: Any] {
        ["id": id, "name": name, "pid": Int(pid), "active": app.isActive]
    }
}

enum Apps {
    static func running() -> [AppRef] {
        NSWorkspace.shared.runningApplications
            .filter { $0.activationPolicy == .regular && !$0.isTerminated }
            .map(AppRef.init)
    }

    static func resolve(_ query: String) throws -> AppRef {
        let q = query.trimmingCharacters(in: .whitespacesAndNewlines)
        if q.isEmpty { throw ServiceError.invalid("app 不能为空") }
        let lower = q.lowercased()
        let all = running()
        if let hit = all.first(where: { $0.app.bundleIdentifier?.lowercased() == lower }) { return hit }
        if q.hasPrefix("/"), let hit = all.first(where: { $0.app.bundleURL?.path == q || $0.app.executableURL?.path == q }) { return hit }
        let bare = lower.hasSuffix(".app") ? String(lower.dropLast(4)) : lower
        let byName = all.filter {
            $0.name.lowercased() == lower || $0.name.lowercased() == bare
                //  显示名是本地化的（「访达」），英文名只在 .app 文件名里
                || $0.app.bundleURL?.deletingPathExtension().lastPathComponent.lowercased() == bare
        }
        if byName.count == 1 { return byName[0] }
        if byName.count > 1 { throw ServiceError.ambiguous(q, byName.map { "\($0.name)（\($0.id)）" }) }
        //  没在跑：找得到 .app 就后台启动，启动是「让它有个窗口可读」的前置，不算动作
        guard let url = locate(q) else { throw ServiceError.appNotFound(q) }
        return try launch(url)
    }

    static func locate(_ q: String) -> URL? {
        if q.hasPrefix("/") {
            return FileManager.default.fileExists(atPath: q) ? URL(fileURLWithPath: q) : nil
        }
        if q.contains("."), let url = NSWorkspace.shared.urlForApplication(withBundleIdentifier: q) { return url }
        let name = q.hasSuffix(".app") ? q : q + ".app"
        let dirs = ["/Applications", "/System/Applications", "/System/Applications/Utilities",
                    NSHomeDirectory() + "/Applications"]
        for dir in dirs {
            let candidate = URL(fileURLWithPath: dir).appendingPathComponent(name)
            if FileManager.default.fileExists(atPath: candidate.path) { return candidate }
            //  大小写不敏感地再扫一遍目录
            if let items = try? FileManager.default.contentsOfDirectory(atPath: dir),
               let hit = items.first(where: { $0.lowercased() == name.lowercased() }) {
                return URL(fileURLWithPath: dir).appendingPathComponent(hit)
            }
        }
        return nil
    }

    static func launch(_ url: URL) throws -> AppRef {
        let config = NSWorkspace.OpenConfiguration()
        config.activates = false
        config.addsToRecentItems = false
        let semaphore = DispatchSemaphore(value: 0)
        var launched: NSRunningApplication?
        var failure: Error?
        NSWorkspace.shared.openApplication(at: url, configuration: config) { app, error in
            launched = app
            failure = error
            semaphore.signal()
        }
        if semaphore.wait(timeout: .now() + 15) == .timedOut {
            throw ServiceError(code: -32008, message: "启动 \(url.lastPathComponent) 超时")
        }
        if let failure { throw ServiceError(code: -32008, message: "启动 \(url.lastPathComponent) 失败：\(failure.localizedDescription)") }
        guard let app = launched else { throw ServiceError(code: -32008, message: "启动 \(url.lastPathComponent) 失败") }
        //  等它把第一个窗口摆出来，最多 8 秒；没等到也返回，让调用方看到"没有窗口"
        let deadline = Date().addingTimeInterval(8)
        let element = AXUIElementCreateApplication(app.processIdentifier)
        while Date() < deadline {
            if app.isFinishedLaunching, let windows = attribute(element, kAXWindowsAttribute) as? [AXUIElement], !windows.isEmpty { break }
            Thread.sleep(forTimeInterval: 0.2)
        }
        return AppRef(app: app)
    }
}

// MARK: - AX 小工具

func attribute(_ element: AXUIElement, _ name: String) -> AnyObject? {
    var value: AnyObject?
    let result = AXUIElementCopyAttributeValue(element, name as CFString, &value)
    return result == .success ? value : nil
}

/// 应用自己实现无障碍，标准属性也可能回异类值；强转会让整个服务崩掉，这里先验类型
func axElement(_ raw: AnyObject?) -> AXUIElement? {
    guard let raw, CFGetTypeID(raw) == AXUIElementGetTypeID() else { return nil }
    return (raw as! AXUIElement)
}

/// 一次取多个属性：每个属性单独取都是一次进程间往返，一棵树上千节点时这是耗时的大头。
/// 没有的属性回 AXError 型的值，这里统一成 nil；整个调用失败就退回逐个取。
func axMany(_ element: AXUIElement, _ names: [String]) -> [AnyObject?] {
    var values: CFArray?
    let result = AXUIElementCopyMultipleAttributeValues(element, names as CFArray, AXCopyMultipleAttributeOptions(rawValue: 0), &values)
    guard result == .success, let list = values as? [AnyObject], list.count == names.count else {
        return names.map { attribute(element, $0) }
    }
    return list.map { raw in
        if raw is NSNull { return nil }
        if CFGetTypeID(raw) == AXValueGetTypeID(), AXValueGetType(raw as! AXValue) == .axError { return nil }
        return raw
    }
}

func stringValue(_ raw: AnyObject?) -> String? {
    guard let raw else { return nil }
    if let s = raw as? String { return s }
    if let n = raw as? NSNumber { return n.stringValue }
    if let a = raw as? NSAttributedString { return a.string }
    if let url = raw as? URL { return url.absoluteString }
    return nil
}

func boolValue(_ raw: AnyObject?) -> Bool? { (raw as? NSNumber)?.boolValue }

func pointValue(_ raw: AnyObject?) -> CGPoint? {
    guard let raw, CFGetTypeID(raw) == AXValueGetTypeID() else { return nil }
    var point = CGPoint.zero
    return AXValueGetValue(raw as! AXValue, .cgPoint, &point) ? point : nil
}

func sizeValue(_ raw: AnyObject?) -> CGSize? {
    guard let raw, CFGetTypeID(raw) == AXValueGetTypeID() else { return nil }
    var size = CGSize.zero
    return AXValueGetValue(raw as! AXValue, .cgSize, &size) ? size : nil
}

func rectValue(_ position: AnyObject?, _ size: AnyObject?) -> CGRect? {
    guard let origin = pointValue(position), let size = sizeValue(size) else { return nil }
    return CGRect(origin: origin, size: size)
}

func axString(_ element: AXUIElement, _ name: String) -> String? { stringValue(attribute(element, name)) }
func axBool(_ element: AXUIElement, _ name: String) -> Bool? { boolValue(attribute(element, name)) }
func axFrame(_ element: AXUIElement) -> CGRect? {
    rectValue(attribute(element, kAXPositionAttribute), attribute(element, kAXSizeAttribute))
}

func axActions(_ element: AXUIElement) -> [String] {
    var names: CFArray?
    guard AXUIElementCopyActionNames(element, &names) == .success, let list = names as? [String] else { return [] }
    return list
}

func axSettable(_ element: AXUIElement, _ name: String) -> Bool {
    var settable = DarwinBoolean(false)
    return AXUIElementIsAttributeSettable(element, name as CFString, &settable) == .success && settable.boolValue
}

func clip(_ text: String, _ cap: Int = valueCap) -> String {
    let flat = text.replacingOccurrences(of: "\n", with: "⏎").replacingOccurrences(of: "\r", with: "")
    if flat.count <= cap { return flat }
    return String(flat.prefix(cap - 1)) + "…"
}

// MARK: - 窗口几何与坐标空间

struct Space {
    let frame: CGRect   // 窗口在屏幕坐标系的框（点，左上角原点，和 AX 一致）
    let scale: CGFloat  // 窗口点 → 坐标单位（≤ 1）
    var width: Int { Int((frame.width * scale).rounded()) }
    var height: Int { Int((frame.height * scale).rounded()) }
    func toUnits(_ p: CGPoint) -> CGPoint { CGPoint(x: (p.x - frame.minX) * scale, y: (p.y - frame.minY) * scale) }
    func toScreen(_ p: CGPoint) -> CGPoint { CGPoint(x: frame.minX + p.x / scale, y: frame.minY + p.y / scale) }
    static func of(_ frame: CGRect) -> Space {
        let longest = max(frame.width, frame.height, 1)
        return Space(frame: frame, scale: min(1, maxEdge / longest))
    }
}

// MARK: - 元素索引簿（每个应用一本）

final class Ledger {
    private var byKey: [String: Int] = [:]
    private var lastSeen: [String: Int] = [:]
    private(set) var elements: [Int: AXUIElement] = [:]
    private var next = 1
    private var walk = 0

    func begin() { elements = [:]; walk += 1 }
    func index(for key: String, element: AXUIElement) -> Int {
        let n: Int
        if let known = byKey[key] { n = known } else { n = next; next += 1; byKey[key] = n }
        elements[n] = element
        lastSeen[key] = walk
        return n
    }
    /// 网页换页、列表翻页会不停造新键：50 次遍历没再出现的忘掉，再出现拿新号。每 10 次清一回。
    func finish() {
        guard walk % 10 == 0 else { return }
        for (key, seen) in lastSeen where seen < walk - 50 { lastSeen[key] = nil; byKey[key] = nil }
    }
    func element(_ index: Int) throws -> AXUIElement {
        guard let element = elements[index] else { throw ServiceError.stale(index) }
        return element
    }
}

// MARK: - 树遍历与渲染

let actionRoles: Set<String> = [
    "AXButton", "AXPopUpButton", "AXMenuButton", "AXCheckBox", "AXRadioButton", "AXLink", "AXTextField",
    "AXTextArea", "AXSearchField", "AXComboBox", "AXSlider", "AXIncrementor", "AXMenuItem", "AXMenuBarItem",
    "AXTab", "AXTabGroup", "AXDisclosureTriangle", "AXRow", "AXCell", "AXColorWell", "AXSwitch", "AXToggle",
    "AXImage", "AXStaticText", "AXOutline", "AXTable", "AXList", "AXScrollBar", "AXWebArea", "AXSecureTextField",
]
let interestingActions: Set<String> = ["AXPress", "AXConfirm", "AXCancel", "AXShowMenu", "AXIncrement", "AXDecrement", "AXPick"]
//  每个节点一次取齐的属性；下标与 Walker.walk 里的取用一一对应
let walkAttributes: [String] = [
    kAXRoleAttribute, kAXSubroleAttribute, kAXTitleAttribute, kAXDescriptionAttribute, kAXIdentifierAttribute,
    kAXHelpAttribute, kAXValueAttribute, kAXPlaceholderValueAttribute, kAXEnabledAttribute, kAXFocusedAttribute,
    kAXPositionAttribute, kAXSizeAttribute, kAXSelectedAttribute, kAXChildrenAttribute,
]
let skipRoles: Set<String> = ["AXUnknown"]

struct Walker {
    let ledger: Ledger
    let space: Space
    var lines: [String] = []
    var count = 0
    var truncated = false
    //  整棵树的时间预算：目标应用卡住时每个属性调用都要等到消息超时，没有预算一次 get_state 能等几分钟
    let deadline = Date().addingTimeInterval(8)

    mutating func walk(_ element: AXUIElement, depth: Int, path: String) {
        if count >= maxNodes { truncated = true; return }
        if depth > maxDepth { truncated = true; return }
        if Date() > deadline { truncated = true; return }
        count += 1
        let attrs = axMany(element, walkAttributes)
        let role = stringValue(attrs[0]) ?? "AXUnknown"
        let frame = rectValue(attrs[10], attrs[11])
        //  收起的菜单在无障碍树里仍挂着全部菜单项，只是尺寸为零：整棵跳过
        if role == "AXMenu" || role == "AXMenuItem", let f = frame, f.width < 1 || f.height < 1 { return }
        let subrole = stringValue(attrs[1])
        let title = stringValue(attrs[2])
        let desc = stringValue(attrs[3])
        let identifier = stringValue(attrs[4])
        let help = stringValue(attrs[5])
        let value = stringValue(attrs[6])
        let placeholder = stringValue(attrs[7])
        let enabled = boolValue(attrs[8]) ?? true
        let focused = boolValue(attrs[9]) ?? false
        let selected = boolValue(attrs[12]) ?? false
        let actions = axActions(element)
        let settable = axSettable(element, kAXValueAttribute)

        let label = title ?? desc ?? identifier ?? help
        let interesting = !actions.filter(interestingActions.contains).isEmpty || settable
            || (actionRoles.contains(role) && (label != nil || value != nil || role == "AXImage"))
        let visible = frame.map { $0.width >= 1 && $0.height >= 1 } ?? true
        let shortRole = role.hasPrefix("AX") ? String(role.dropFirst(2)) : role
        var pieces: [String] = []
        let key = "\(path)/\(role):\(identifier ?? title ?? desc ?? "")"

        if interesting && visible && !skipRoles.contains(role) {
            let n = ledger.index(for: key, element: element)
            pieces.append("[\(n)]")
        }
        var roleText = lowerFirst(shortRole)
        if let subrole, subrole.hasPrefix("AX"), subrole != role { roleText += "/" + lowerFirst(String(subrole.dropFirst(2))) }
        pieces.append(roleText)
        if let title, !title.isEmpty { pieces.append("\"\(clip(title, 120))\"") }
        else if let desc, !desc.isEmpty { pieces.append("\"\(clip(desc, 120))\"") }
        else if let identifier, !identifier.isEmpty, role != "AXStaticText" { pieces.append("#\(clip(identifier, 60))") }
        if let value, !value.isEmpty, role != "AXStaticText" || title != nil || desc != nil {
            pieces.append("value=\"\(clip(value))\"")
        } else if role == "AXStaticText", let value, !value.isEmpty {
            pieces.append("\"\(clip(value))\"")
        }
        if let placeholder, !placeholder.isEmpty, (value ?? "").isEmpty { pieces.append("placeholder=\"\(clip(placeholder, 80))\"") }
        var flags: [String] = []
        if !enabled { flags.append("disabled") }
        if focused { flags.append("focused") }
        if selected { flags.append("selected") }
        if settable { flags.append("editable") }
        if !flags.isEmpty { pieces.append("(" + flags.joined(separator: ",") + ")") }
        let extra = actions.filter { interestingActions.contains($0) && $0 != "AXPress" }.map { lowerFirst(String($0.dropFirst(2))) }
        if !extra.isEmpty { pieces.append("actions:" + extra.joined(separator: ",")) }
        if interesting, let frame, visible {
            let center = space.toUnits(CGPoint(x: frame.midX, y: frame.midY))
            pieces.append("@\(Int(center.x.rounded())),\(Int(center.y.rounded()))")
        }
        //  纯结构节点（group/splitGroup 之类，没标题没值）不占一行，但孩子照走、缩进不加
        let structural = !interesting && (label == nil || label!.isEmpty) && (value == nil || value!.isEmpty)
            && !["AXWindow", "AXSheet", "AXDrawer", "AXPopover", "AXMenu", "AXToolbar", "AXTabGroup", "AXOutline", "AXTable", "AXList", "AXWebArea"].contains(role)
        var childDepth = depth
        if !structural {
            lines.append(String(repeating: "  ", count: depth) + pieces.joined(separator: " "))
            childDepth += 1
        }
        //  表格 / 列表 / 滚动区只走可见的那部分：几千行的列表全走一遍既慢又把节点预算耗光，
        //  滚出去的行点不着也没意义（点击前另有可视区核对）
        var children: [AXUIElement]? = nil
        if role == "AXTable" || role == "AXOutline" {
            children = attribute(element, kAXVisibleRowsAttribute) as? [AXUIElement]
        } else if role == "AXList" || role == "AXScrollArea" {
            children = attribute(element, kAXVisibleChildrenAttribute) as? [AXUIElement]
        }
        if children == nil || children!.isEmpty { children = attrs[13] as? [AXUIElement] }
        guard let children else { return }
        var ordinals: [String: Int] = [:]
        for child in children {
            let childRole = axString(child, kAXRoleAttribute) ?? "AXUnknown"
            let ordinal = ordinals[childRole, default: 0]
            ordinals[childRole] = ordinal + 1
            walk(child, depth: childDepth, path: "\(key)[\(ordinal)]")
            if count >= maxNodes { truncated = true; return }
        }
    }
}

/// 调用方给的数值先夹到合理范围再转整数：Double → Int 超范围会直接让进程崩溃
func clampDouble(_ v: Double, _ lo: Double, _ hi: Double) -> Double {
    guard v.isFinite else { return lo }
    return min(hi, max(lo, v))
}

func lowerFirst(_ s: String) -> String {
    guard let first = s.first else { return s }
    return first.lowercased() + s.dropFirst()
}

// MARK: - 窗口选择与截图

struct WindowInfo {
    let element: AXUIElement
    let frame: CGRect
    let title: String
    let number: CGWindowID?
}

func pickWindow(_ app: AppRef) throws -> WindowInfo {
    let root = AXUIElementCreateApplication(app.pid)
    var candidate = axElement(attribute(root, kAXFocusedWindowAttribute))
    if candidate == nil, let windows = attribute(root, kAXWindowsAttribute) as? [AXUIElement] {
        candidate = windows.first(where: { (axBool($0, kAXMinimizedAttribute) ?? false) == false }) ?? windows.first
    }
    guard let element = candidate, let frame = axFrame(element), frame.width > 0, frame.height > 0 else {
        throw ServiceError.noWindow(app.name)
    }
    let title = axString(element, kAXTitleAttribute) ?? ""
    return WindowInfo(element: element, frame: frame, title: title, number: windowNumber(pid: app.pid, frame: frame, title: title))
}

/// AX 不公开窗口号；用 pid + 边框（+ 标题）到窗口服务器那边对一下。
func windowNumber(pid: pid_t, frame: CGRect, title: String) -> CGWindowID? {
    guard let list = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID) as? [[String: Any]] else { return nil }
    var best: (CGWindowID, CGFloat)?
    for info in list {
        guard (info[kCGWindowOwnerPID as String] as? Int32) == pid,
              (info[kCGWindowLayer as String] as? Int) == 0,
              let number = info[kCGWindowNumber as String] as? UInt32,
              let bounds = info[kCGWindowBounds as String] as? [String: CGFloat] else { continue }
        let rect = CGRect(x: bounds["X"] ?? 0, y: bounds["Y"] ?? 0, width: bounds["Width"] ?? 0, height: bounds["Height"] ?? 0)
        var score = abs(rect.minX - frame.minX) + abs(rect.minY - frame.minY) + abs(rect.width - frame.width) + abs(rect.height - frame.height)
        if let name = info[kCGWindowName as String] as? String, !title.isEmpty, name == title { score -= 10 }
        if best == nil || score < best!.1 { best = (number, score) }
    }
    //  边框对不上就是别的窗口（焦点窗口在另一个空间、被隐藏）：宁可不截图，也不能截一张和树不符的
    guard let best, best.1 < 24 else { return nil }
    return best.0
}

func captureWindow(_ number: CGWindowID, space: Space) throws -> Data {
    try Permissions.ensureScreenCapture()
    let semaphore = DispatchSemaphore(value: 0)
    var image: CGImage?
    var failure: Error?
    SCShareableContent.getExcludingDesktopWindows(false, onScreenWindowsOnly: true) { content, error in
        guard let window = content?.windows.first(where: { $0.windowID == number }) else {
            failure = error ?? ServiceError(code: -32009, message: "窗口不在可截图清单里（可能被最小化或在别的空间）")
            semaphore.signal()
            return
        }
        let filter = SCContentFilter(desktopIndependentWindow: window)
        let config = SCStreamConfiguration()
        config.width = max(1, space.width)
        config.height = max(1, space.height)
        config.showsCursor = false
        config.captureResolution = .best
        SCScreenshotManager.captureImage(contentFilter: filter, configuration: config) { result, error in
            image = result
            failure = error
            semaphore.signal()
        }
    }
    if semaphore.wait(timeout: .now() + 10) == .timedOut { throw ServiceError(code: -32009, message: "截图超时") }
    if let failure { throw ServiceError(code: -32009, message: "截图失败：\(failure.localizedDescription)") }
    guard let image else { throw ServiceError(code: -32009, message: "截图失败") }
    let data = NSMutableData()
    guard let dest = CGImageDestinationCreateWithData(data, UTType.png.identifier as CFString, 1, nil) else {
        throw ServiceError(code: -32009, message: "PNG 编码失败")
    }
    CGImageDestinationAddImage(dest, image, nil)
    guard CGImageDestinationFinalize(dest) else { throw ServiceError(code: -32009, message: "PNG 编码失败") }
    return data as Data
}

// MARK: - 用户介入侦测

/// 用户随时可能在动自己的鼠标键盘。合成输入前先看最近有没有"不是我们发的"输入事件；
/// 多步动作（键入分块、拖拽分步）之间再看光标有没有被挪开。靠两件事区分人与我们：
/// 我们投递的每个事件都记时刻，落在这些时刻附近的系统输入算我们的；我们把光标放在哪
/// 也记着，光标跑了就是人动的。不装事件监听（那要额外的输入监控权限），只读系统计时。
enum Activity {
    static let idleWindow: TimeInterval = 1.2
    static let cursorTolerance: CGFloat = 4
    private static let lock = NSLock()
    private static var posts: [Date] = []
    private static var expected: CGPoint?
    private static let watched: [CGEventType] = [.mouseMoved, .leftMouseDown, .rightMouseDown, .otherMouseDown,
                                                 .leftMouseDragged, .scrollWheel, .keyDown, .flagsChanged]

    static func notePost() {
        lock.lock(); defer { lock.unlock() }
        posts.append(Date())
        if posts.count > 400 { posts.removeFirst(200) }
    }

    private static func isOurs(_ at: Date) -> Bool {
        lock.lock(); defer { lock.unlock() }
        return posts.contains { abs($0.timeIntervalSince(at)) < 0.3 }
    }

    /// 最近一次用户（非我们）输入距今秒数；没有或太久远返回 nil
    static func secondsSinceUserInput() -> Double? {
        var best: Double?
        let now = Date()
        for type in watched {
            let since = CGEventSource.secondsSinceLastEventType(.hidSystemState, eventType: type)
            if since > 30 { continue }
            if isOurs(now.addingTimeInterval(-since)) { continue }
            best = min(best ?? since, since)
        }
        return best
    }

    static func assertUserIdle() throws {
        if let since = secondsSinceUserInput(), since < idleWindow {
            VirtualCursor.shared.hide()
            throw ServiceError(code: -32012, message: String(format: "用户正在操作电脑（%.1f 秒前有键鼠活动），已暂停合成输入，没有执行这个动作。请停下来问用户是否继续，或等用户停手后重试。", since))
        }
    }

    static func cursor() -> CGPoint { CGEvent(source: nil)?.location ?? .zero }
    static func placed(_ point: CGPoint) { lock.lock(); expected = point; lock.unlock() }
    static func forget() { lock.lock(); expected = nil; lock.unlock() }
    /// 这次动作里我们动过系统光标吗（无障碍动作不动它，就没有"还回去"一说）
    static func moved() -> Bool { lock.lock(); defer { lock.unlock() }; return expected != nil }

    /// 多步动作中途：光标不在我们放的位置 = 用户把它挪走了
    static func assertCursorUntouched() throws {
        if Input.backgroundPid != nil { return }
        lock.lock(); let want = expected; lock.unlock()
        guard let want else { return }
        let now = cursor()
        if abs(now.x - want.x) > cursorTolerance || abs(now.y - want.y) > cursorTolerance {
            forget()
            throw ServiceError(code: -32012, message: "用户在动作进行中移动了鼠标，已中止剩余步骤（可能只做了一部分）。请 get_state 看现状，并问用户是否继续。")
        }
    }
}

// MARK: - 输入法

/// 中文等输入法开着时，合成的按键会被当成拼码吞进候选框（键入没反应、Return 只是提交候选）。
/// 合成键盘输入前临时切到 ASCII 键盘布局，敲完切回；本来就是键盘布局的不动。
enum Keyboard {
    private static let lock = NSLock()
    //  临时切到 ASCII 布局之前的输入源；非 nil 表示此刻处于"切过去"的状态，退出清理时也要切回
    private static var switchedFrom: TISInputSource?

    static func withASCIILayout<T>(_ body: () throws -> T) rethrows -> T {
        guard let current = TISCopyCurrentKeyboardInputSource()?.takeRetainedValue() else { return try body() }
        var kind = ""
        if let raw = TISGetInputSourceProperty(current, kTISPropertyInputSourceType) {
            kind = Unmanaged<CFString>.fromOpaque(raw).takeUnretainedValue() as String
        }
        if kind == (kTISTypeKeyboardLayout as String) { return try body() }
        guard let ascii = TISCopyCurrentASCIICapableKeyboardLayoutInputSource()?.takeRetainedValue() else { return try body() }
        TISSelectInputSource(ascii)
        lock.lock(); switchedFrom = current; lock.unlock()
        Thread.sleep(forTimeInterval: 0.08)
        defer { restore() }
        return try body()
    }

    //  当前键盘布局的"字符 → (键码, 要不要 shift)"表。美式之外的布局（德法、Dvorak）键位不同，
    //  写死的美式表会打出错字；这里按布局数据用 UCKeyTranslate 反查，按布局 id 缓存。
    private static var maps: [String: [Character: (CGKeyCode, Bool)]] = [:]

    static func layoutMap() -> [Character: (CGKeyCode, Bool)] {
        guard let layout = TISCopyCurrentKeyboardLayoutInputSource()?.takeRetainedValue() else { return [:] }
        var id = ""
        if let raw = TISGetInputSourceProperty(layout, kTISPropertyInputSourceID) {
            id = Unmanaged<CFString>.fromOpaque(raw).takeUnretainedValue() as String
        }
        lock.lock(); let cached = maps[id]; lock.unlock()
        if let cached { return cached }
        var map: [Character: (CGKeyCode, Bool)] = [:]
        if let raw = TISGetInputSourceProperty(layout, kTISPropertyUnicodeKeyLayoutData) {
            let data = Unmanaged<CFData>.fromOpaque(raw).takeUnretainedValue() as Data
            data.withUnsafeBytes { (buffer: UnsafeRawBufferPointer) in
                guard let base = buffer.baseAddress else { return }
                let keyboard = base.assumingMemoryBound(to: UCKeyboardLayout.self)
                for code in 0..<128 {
                    for shift in [false, true] {
                        var dead: UInt32 = 0
                        var chars = [UniChar](repeating: 0, count: 4)
                        var length: Int = 0
                        let modifiers: UInt32 = shift ? (UInt32(shiftKey) >> 8) & 0xFF : 0
                        let status = UCKeyTranslate(keyboard, UInt16(code), UInt16(kUCKeyActionDown), modifiers,
                                                    UInt32(LMGetKbdType()), UInt32(1 << kUCKeyTranslateNoDeadKeysBit),
                                                    &dead, chars.count, &length, &chars)
                        guard status == noErr, length == 1, let scalar = UnicodeScalar(chars[0]),
                              scalar.value >= 0x20, scalar.value != 0x7f else { continue }
                        let ch = Character(scalar)
                        if map[ch] == nil { map[ch] = (CGKeyCode(code), shift) }
                    }
                }
            }
        }
        lock.lock(); maps[id] = map; lock.unlock()
        return map
    }

    /// 切回原输入源。先等一小会儿：最后几个按键事件还在系统队列里，立刻切回会让它们落进输入法的候选框。
    static func restore() {
        lock.lock(); let source = switchedFrom; switchedFrom = nil; lock.unlock()
        guard let source else { return }
        Thread.sleep(forTimeInterval: 0.1)
        TISSelectInputSource(source)
    }
}

// MARK: - Markdown → HTML

/// 粘贴 markdown 用的最小转换。系统自带的 markdown 解析把标题、列表这些块结构全压成一行，
/// 粘进富文本编辑器等于没转；这里只认常用语法：标题、有序 / 无序列表、引用、分隔线、
/// 围栏代码块、粗体、斜体、行内代码、链接，其余按段落。
enum Markdown {
    static func html(_ markdown: String) -> String {
        var out: [String] = []
        var paragraph: [String] = []
        var list: String?
        var inCode = false
        func flushParagraph() {
            if !paragraph.isEmpty { out.append("<p>" + inline(paragraph.joined(separator: " ")) + "</p>"); paragraph = [] }
        }
        func closeList() { if let open = list { out.append("</\(open)>"); list = nil } }
        for piece in markdown.replacingOccurrences(of: "\r\n", with: "\n").split(separator: "\n", omittingEmptySubsequences: false) {
            let line = String(piece)
            if line.hasPrefix("```") {
                flushParagraph(); closeList()
                out.append(inCode ? "</pre>" : "<pre>")
                inCode.toggle()
                continue
            }
            if inCode { out.append(escape(line)); continue }
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            if trimmed.isEmpty { flushParagraph(); closeList(); continue }
            if let (level, rest) = heading(trimmed) {
                flushParagraph(); closeList()
                out.append("<h\(level)>" + inline(rest) + "</h\(level)>")
                continue
            }
            if let item = listItem(trimmed, ordered: false) {
                flushParagraph()
                if list != "ul" { closeList(); out.append("<ul>"); list = "ul" }
                out.append("<li>" + inline(item) + "</li>")
                continue
            }
            if let item = listItem(trimmed, ordered: true) {
                flushParagraph()
                if list != "ol" { closeList(); out.append("<ol>"); list = "ol" }
                out.append("<li>" + inline(item) + "</li>")
                continue
            }
            if trimmed.hasPrefix("> ") {
                flushParagraph(); closeList()
                out.append("<blockquote>" + inline(String(trimmed.dropFirst(2))) + "</blockquote>")
                continue
            }
            if trimmed == "---" || trimmed == "***" { flushParagraph(); closeList(); out.append("<hr>"); continue }
            closeList()
            paragraph.append(trimmed)
        }
        flushParagraph(); closeList()
        if inCode { out.append("</pre>") }
        //  不声明编码的话接收方会猜，中文十有八九猜错
        return "<html><head><meta charset=\"utf-8\"></head><body>\n" + out.joined(separator: "\n") + "\n</body></html>"
    }

    private static func heading(_ s: String) -> (Int, String)? {
        var level = 0
        var index = s.startIndex
        while index < s.endIndex, s[index] == "#", level < 6 { level += 1; index = s.index(after: index) }
        guard level > 0, index < s.endIndex, s[index] == " " else { return nil }
        return (level, String(s[s.index(after: index)...]))
    }

    private static func listItem(_ s: String, ordered: Bool) -> String? {
        if !ordered {
            for marker in ["- ", "* ", "+ "] where s.hasPrefix(marker) { return String(s.dropFirst(2)) }
            return nil
        }
        guard let dot = s.firstIndex(of: "."), dot > s.startIndex, s[s.startIndex..<dot].allSatisfy({ $0.isNumber }) else { return nil }
        let after = s.index(after: dot)
        guard after < s.endIndex, s[after] == " " else { return nil }
        return String(s[s.index(after: after)...])
    }

    static func escape(_ s: String) -> String {
        s.replacingOccurrences(of: "&", with: "&amp;").replacingOccurrences(of: "<", with: "&lt;")
            .replacingOccurrences(of: ">", with: "&gt;").replacingOccurrences(of: "\"", with: "&quot;")
    }

    private static func inline(_ s: String) -> String {
        var text = escape(s)
        text = replace(text, "`([^`]+)`", "<code>$1</code>")
        text = replace(text, "\\*\\*(.+?)\\*\\*", "<b>$1</b>")
        text = replace(text, "(?<![\\w*])\\*(?!\\s)(.+?)(?<!\\s)\\*(?![\\w*])", "<i>$1</i>")
        text = replace(text, "\\[([^\\]]+)\\]\\(([^)\\s]+)\\)", "<a href=\"$2\">$1</a>")
        return text
    }

    private static func replace(_ s: String, _ pattern: String, _ template: String) -> String {
        guard let regex = try? NSRegularExpression(pattern: pattern) else { return s }
        return regex.stringByReplacingMatches(in: s, range: NSRange(location: 0, length: (s as NSString).length), withTemplate: template)
    }
}

// MARK: - 剪贴板粘贴

/// 经系统剪贴板粘贴：长文本、富文本（markdown / html）比逐字符键入快得多也不受输入法影响。
/// 先把用户的剪贴板整份快照，粘完恢复；中途用户自己复制了东西（changeCount 变了）就不覆盖。
enum Clipboard {
    typealias Snapshot = [[NSPasteboard.PasteboardType: Data]]

    static func paste(_ text: String, format: String) throws {
        guard ["text", "markdown", "html"].contains(format) else { throw ServiceError.invalid("format 只能是 text / markdown / html") }
        let board = NSPasteboard.general
        var saved: Snapshot = []
        var mine = 0
        DispatchQueue.main.sync {
            saved = snapshot(board)
            board.clearContents()
            switch format {
            case "html":
                let html = text.lowercased().contains("charset") ? text : "<meta charset=\"utf-8\">" + text
                board.setString(html, forType: .html)
                board.setString(plain(fromHTML: html) ?? text, forType: .string)
            case "markdown":
                board.setString(Markdown.html(text), forType: .html)
                board.setString(text, forType: .string)
            default:
                board.setString(text, forType: .string)
            }
            mine = board.changeCount
        }
        defer {
            //  让应用把剪贴板读完再还回去；恢复时若内容已不是我们写的，说明用户复制了别的，别动
            Thread.sleep(forTimeInterval: 0.6)
            DispatchQueue.main.sync { if board.changeCount == mine { restore(board, saved) } }
        }
        try Input.chord("cmd+v")
    }

    private static func snapshot(_ board: NSPasteboard) -> Snapshot {
        (board.pasteboardItems ?? []).map { item in
            var copy: [NSPasteboard.PasteboardType: Data] = [:]
            for type in item.types { if let data = item.data(forType: type) { copy[type] = data } }
            return copy
        }
    }

    private static func restore(_ board: NSPasteboard, _ items: Snapshot) {
        board.clearContents()
        guard !items.isEmpty else { return }
        board.writeObjects(items.map { dict in
            let item = NSPasteboardItem()
            for (type, data) in dict { item.setData(data, forType: type) }
            return item
        })
    }

    static func plain(fromHTML html: String) -> String? {
        guard let data = html.data(using: .utf8) else { return nil }
        return NSAttributedString(html: data, options: [.documentType: NSAttributedString.DocumentType.html,
                                                        .characterEncoding: String.Encoding.utf8.rawValue],
                                  documentAttributes: nil)?.string
    }
}

// MARK: - 输入合成

enum Input {
    static func activate(_ app: AppRef) {
        if app.app.isActive { return }
        //  后台进程没有"让出前台"的资格，普通 activate 常被系统忽略；无障碍的 AXFrontmost
        //  是给辅助技术留的口子，能把别的应用拉到前台。再用 activateIgnoringOtherApps 兜一下。
        let element = AXUIElementCreateApplication(app.pid)
        AXUIElementSetAttributeValue(element, kAXFrontmostAttribute as CFString, kCFBooleanTrue)
        if let window = axElement(attribute(element, kAXFocusedWindowAttribute)) {
            AXUIElementPerformAction(window, kAXRaiseAction as CFString)
        }
        DispatchQueue.main.sync { _ = app.app.activate(options: [.activateIgnoringOtherApps]) }
        //  激活是异步的：给窗口服务器一点时间换前台，否则第一个事件会落到旧前台
        var deadline = Date().addingTimeInterval(1.0)
        while !app.app.isActive && Date() < deadline { Thread.sleep(forTimeInterval: 0.05) }
        if !app.app.isActive, let url = app.app.bundleURL {
            //  窗口全在最小化状态时先还原一个：下面的"打开应用"对没有可见窗口的文档类应用等于新建文档
            if let windows = attribute(element, kAXWindowsAttribute) as? [AXUIElement], let first = windows.first,
               windows.allSatisfy({ axBool($0, kAXMinimizedAttribute) ?? false }) {
                AXUIElementSetAttributeValue(first, kAXMinimizedAttribute as CFString, kCFBooleanFalse)
                Thread.sleep(forTimeInterval: 0.3)
            }
            //  再兜一层：以"打开应用"的方式请求前台，这是后台进程最可靠的一条路
            let config = NSWorkspace.OpenConfiguration()
            config.activates = true
            let semaphore = DispatchSemaphore(value: 0)
            NSWorkspace.shared.openApplication(at: url, configuration: config) { _, _ in semaphore.signal() }
            _ = semaphore.wait(timeout: .now() + 3)
            deadline = Date().addingTimeInterval(1.5)
            while !app.app.isActive && Date() < deadline { Thread.sleep(forTimeInterval: 0.05) }
        }
        Thread.sleep(forTimeInterval: 0.15)
    }

    /// 合成输入前必须确认目标真在前台：否则按键会落进用户正在用的别的窗口
    static func requireActive(_ app: AppRef) throws {
        if backgroundPid != nil { return }
        try Activity.assertUserIdle()
        //  刚被我们拉起的应用还没启动完就激活，激活会被吞掉；最多等 5 秒
        let launched = Date().addingTimeInterval(5)
        while !app.app.isFinishedLaunching && Date() < launched { Thread.sleep(forTimeInterval: 0.1) }
        activate(app)
        guard app.app.isActive else {
            throw ServiceError(code: -32011, message: "无法把「\(app.name)」切到前台，已放弃合成输入（避免按键落进别的窗口）。请用户手动点一下该应用的窗口后重试，或改用元素动作（click 元素 / set_value）。")
        }
    }

    /// 后台投递：设了目标 pid 就把事件直接投给那个进程（公开 API，不经系统事件流），
    /// 不切前台、不动用户的真实光标；应用收不收由它自己决定。
    static var backgroundPid: pid_t?
    static var backgroundWindow: CGWindowID?

    static func post(_ event: CGEvent?) throws {
        guard let event else { throw ServiceError(code: -32010, message: "无法构造输入事件") }
        if let pid = backgroundPid {
            //  鼠标事件告诉 AppKit 它落在哪个窗口：不然后台 / 被遮挡的窗口不会接
            if let window = backgroundWindow, [.mouseMoved, .leftMouseDown, .leftMouseUp, .rightMouseDown, .rightMouseUp,
                                               .otherMouseDown, .otherMouseUp, .leftMouseDragged, .scrollWheel].contains(event.type) {
                event.setIntegerValueField(.mouseEventWindowUnderMousePointer, value: Int64(window))
                event.setIntegerValueField(.mouseEventWindowUnderMousePointerThatCanHandleThisEvent, value: Int64(window))
            }
            event.postToPid(pid)
            return
        }
        Activity.notePost()
        event.post(tap: .cghidEventTap)
    }

    static func move(to point: CGPoint) throws {
        try post(CGEvent(mouseEventSource: nil, mouseType: .mouseMoved, mouseCursorPosition: point, mouseButton: .left))
        Activity.placed(point)
        Thread.sleep(forTimeInterval: 0.03)
    }

    static func click(at point: CGPoint, button: String, count: Int, flags: CGEventFlags) throws {
        let (down, up, cgButton): (CGEventType, CGEventType, CGMouseButton) = switch button {
        case "right": (.rightMouseDown, .rightMouseUp, .right)
        case "middle": (.otherMouseDown, .otherMouseUp, .center)
        default: (.leftMouseDown, .leftMouseUp, .left)
        }
        try move(to: point)
        for n in 1...max(1, count) {
            let press = CGEvent(mouseEventSource: nil, mouseType: down, mouseCursorPosition: point, mouseButton: cgButton)
            let release = CGEvent(mouseEventSource: nil, mouseType: up, mouseCursorPosition: point, mouseButton: cgButton)
            press?.setIntegerValueField(.mouseEventClickState, value: Int64(n))
            release?.setIntegerValueField(.mouseEventClickState, value: Int64(n))
            press?.flags = flags
            release?.flags = flags
            try Activity.assertCursorUntouched()
            try post(press)
            Thread.sleep(forTimeInterval: 0.04)
            try post(release)
            if n < count { Thread.sleep(forTimeInterval: 0.08) }
        }
    }

    //  拖拽进行中左键是按着的；中途出错（用户介入、事件构造失败、进程被终止）都必须抬起来，
    //  否则用户之后的每次移动都成了拖拽
    private static let buttonLock = NSLock()
    private static var leftButtonHeldAt: CGPoint?

    static func releaseLeftButtonIfHeld() {
        buttonLock.lock(); let at = leftButtonHeldAt; leftButtonHeldAt = nil; buttonLock.unlock()
        guard let at else { return }
        let up = CGEvent(mouseEventSource: nil, mouseType: .leftMouseUp, mouseCursorPosition: at, mouseButton: .left)
        try? post(up)
    }

    static func drag(from: CGPoint, to: CGPoint) throws {
        try move(to: from)
        try post(CGEvent(mouseEventSource: nil, mouseType: .leftMouseDown, mouseCursorPosition: from, mouseButton: .left))
        buttonLock.lock(); leftButtonHeldAt = from; buttonLock.unlock()
        defer { releaseLeftButtonIfHeld() }
        Thread.sleep(forTimeInterval: 0.1)
        let steps = 12
        for i in 1...steps {
            let t = CGFloat(i) / CGFloat(steps)
            let p = CGPoint(x: from.x + (to.x - from.x) * t, y: from.y + (to.y - from.y) * t)
            try Activity.assertCursorUntouched()
            try post(CGEvent(mouseEventSource: nil, mouseType: .leftMouseDragged, mouseCursorPosition: p, mouseButton: .left))
            Activity.placed(p)
            buttonLock.lock(); leftButtonHeldAt = p; buttonLock.unlock()
            Thread.sleep(forTimeInterval: 0.02)
        }
        Thread.sleep(forTimeInterval: 0.1)
        buttonLock.lock(); leftButtonHeldAt = nil; buttonLock.unlock()
        try post(CGEvent(mouseEventSource: nil, mouseType: .leftMouseUp, mouseCursorPosition: to, mouseButton: .left))
    }

    static func scroll(at point: CGPoint, direction: String, pixels rawPixels: Int) throws {
        try move(to: point)
        let pixels = max(1, min(rawPixels, 20_000))
        var vertical: Int32 = 0
        var horizontal: Int32 = 0
        switch direction {
        case "up": vertical = Int32(pixels)
        case "down": vertical = -Int32(pixels)
        case "left": horizontal = Int32(pixels)
        case "right": horizontal = -Int32(pixels)
        default: throw ServiceError.invalid("direction 只能是 up/down/left/right")
        }
        //  分几步滚，很多视图对一次性大位移会丢量
        let steps = max(1, min(10, abs(pixels) / 120))
        for _ in 0..<steps {
            try Activity.assertCursorUntouched()
            try post(CGEvent(scrollWheelEvent2Source: nil, units: .pixel, wheelCount: 2,
                             wheel1: vertical / Int32(steps), wheel2: horizontal / Int32(steps), wheel3: 0))
            Thread.sleep(forTimeInterval: 0.02)
        }
    }

    static func type(_ text: String) throws {
        try Keyboard.withASCIILayout { try typeRaw(text) }
    }

    //  US 布局上需要按住 shift 才能打出的字符 → 不按 shift 时的那个键
    static let shifted: [Character: String] = [
        "!": "1", "@": "2", "#": "3", "$": "4", "%": "5", "^": "6", "&": "7", "*": "8", "(": "9", ")": "0",
        "_": "-", "+": "=", "{": "[", "}": "]", "|": "\\", ":": ";", "\"": "'", "<": ",", ">": ".", "?": "/", "~": "`",
    ]

    private static func typeRaw(_ raw: String) throws {
        //  逐字符发：一个事件塞多个字符时，按键驱动的应用（计算器、游戏、快捷键）只取第一个。
        //  键盘上有的字符按当前布局查真实键码（含 shift），应用按键码判也能认，同时带上字符本身，
        //  按字符读的应用与布局无关；其余走 unicode 串。
        //  "\r\n" 在 Swift 里是一个字符，先归一成换行，否则会被当作普通字符原样插进去
        let text = raw.replacingOccurrences(of: "\r\n", with: "\n")
        let layout = Keyboard.layoutMap()
        var count = 0
        for ch in text {
            if ch == "\n" || ch == "\r" { try chordRaw("Return"); continue }
            if ch == "\t" { try chordRaw("Tab"); continue }
            if count % 8 == 0 { try Activity.assertCursorUntouched() }
            count += 1
            var flags: CGEventFlags = []
            var code: CGKeyCode = 0
            var unicode: [UniChar]? = nil
            let lower = String(ch).lowercased()
            if ch != " ", let hit = layout[ch] {
                code = hit.0; flags = hit.1 ? .maskShift : []; unicode = Array(String(ch).utf16)
            } else if ch.isASCII, let base = shifted[ch], let k = keyCodes[base] {
                code = k; flags = .maskShift
            } else if ch.isASCII, ch.isLetter, let k = keyCodes[lower] {
                code = k; flags = ch.isUppercase ? .maskShift : []
            } else if ch == " " {
                code = 49
            } else if ch.isASCII, let k = keyCodes[String(ch)] {
                code = k
            } else {
                unicode = Array(String(ch).utf16)
            }
            let down = CGEvent(keyboardEventSource: nil, virtualKey: code, keyDown: true)
            let up = CGEvent(keyboardEventSource: nil, virtualKey: code, keyDown: false)
            if let unicode {
                down?.keyboardSetUnicodeString(stringLength: unicode.count, unicodeString: unicode)
                up?.keyboardSetUnicodeString(stringLength: unicode.count, unicodeString: unicode)
            }
            down?.flags = flags
            up?.flags = flags
            try post(down)
            Thread.sleep(forTimeInterval: 0.012)
            try post(up)
            Thread.sleep(forTimeInterval: 0.012)
        }
    }

    static let keyCodes: [String: CGKeyCode] = [
        "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9, "b": 11, "q": 12, "w": 13,
        "e": 14, "r": 15, "y": 16, "t": 17, "1": 18, "2": 19, "3": 20, "4": 21, "6": 22, "5": 23, "=": 24, "9": 25,
        "7": 26, "-": 27, "8": 28, "0": 29, "]": 30, "o": 31, "u": 32, "[": 33, "i": 34, "p": 35, "l": 37, "j": 38,
        "'": 39, "k": 40, ";": 41, "\\": 42, ",": 43, "/": 44, "n": 45, "m": 46, ".": 47, "`": 50,
        "return": 36, "enter": 36, "kp_enter": 76, "tab": 48, "space": 49, "backspace": 51, "delete": 51,
        "forwarddelete": 117, "escape": 53, "esc": 53, "left": 123, "right": 124, "down": 125, "up": 126,
        "home": 115, "end": 119, "pageup": 116, "page_up": 116, "pagedown": 121, "page_down": 121,
        "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97, "f7": 98, "f8": 100, "f9": 101, "f10": 109,
        "f11": 103, "f12": 111, "capslock": 57,
        "kp_0": 82, "kp_1": 83, "kp_2": 84, "kp_3": 85, "kp_4": 86, "kp_5": 87, "kp_6": 88, "kp_7": 89, "kp_8": 91,
        "kp_9": 92, "kp_add": 69, "kp_subtract": 78, "kp_multiply": 67, "kp_divide": 75, "kp_decimal": 65,
    ]
    static let modifierNames: [String: CGEventFlags] = [
        "cmd": .maskCommand, "command": .maskCommand, "super": .maskCommand, "meta": .maskCommand, "win": .maskCommand,
        "ctrl": .maskControl, "control": .maskControl, "control_l": .maskControl,
        "alt": .maskAlternate, "option": .maskAlternate, "opt": .maskAlternate, "alt_l": .maskAlternate,
        "shift": .maskShift, "shift_l": .maskShift, "fn": .maskSecondaryFn,
    ]

    /// `cmd+shift+s` / `Return` / `ctrl+a` 这类写法 → (修饰键, 主键码)；主键可省（只按修饰键没有意义，报错）。
    static func parse(_ chord: String) throws -> (CGEventFlags, CGKeyCode) {
        let trimmed = chord.trimmingCharacters(in: .whitespaces)
        var parts = trimmed.split(separator: "+").map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty }
        //  "+" 自己也是个键（"cmd++" / "+"）：按 "+" 切开后它就没了，补回来
        if trimmed.hasSuffix("+") { parts.append("+") }
        var flags: CGEventFlags = []
        var key: CGKeyCode?
        let layout = Keyboard.layoutMap()
        for part in parts {
            let name = part.lowercased()
            if let flag = modifierNames[name] { flags.insert(flag); continue }
            if key != nil { throw ServiceError.invalid("key 组合里只能有一个主键：\(chord)") }
            if name.count > 1, let code = keyCodes[name] { key = code; continue }
            //  单个字符：先按当前布局找（Dvorak 的 s 不在美式的位置），大写与需要 shift 的符号自带 shift
            if part.count == 1, let hit = layout[Character(part)] {
                key = hit.0; if hit.1 { flags.insert(.maskShift) }; continue
            }
            if let code = keyCodes[name] { key = code; continue }
            if name.count == 1, let base = shifted[Character(name)], let code = keyCodes[base] {
                key = code; flags.insert(.maskShift); continue
            }
            throw ServiceError.invalid("不认识的键名「\(part)」。可用：字母数字、Return、Tab、space、Delete、Escape、方向键 Up/Down/Left/Right、Home/End/PageUp/PageDown、F1-F12，修饰键 cmd/ctrl/alt/shift。")
        }
        guard let key else { throw ServiceError.invalid("key 缺少主键（只有修饰键）：\(chord)") }
        //  大写字母本身就是 shift+字母
        if parts.count == 1, parts[0].count == 1, parts[0] != parts[0].lowercased() { flags.insert(.maskShift) }
        return (flags, key)
    }

    static func chord(_ text: String) throws {
        try Keyboard.withASCIILayout { try chordRaw(text) }
    }

    private static func chordRaw(_ text: String) throws {
        let (flags, key) = try parse(text)
        let down = CGEvent(keyboardEventSource: nil, virtualKey: key, keyDown: true)
        let up = CGEvent(keyboardEventSource: nil, virtualKey: key, keyDown: false)
        down?.flags = flags
        up?.flags = flags
        try post(down)
        Thread.sleep(forTimeInterval: 0.03)
        try post(up)
    }

    static func flags(_ chord: String?) throws -> CGEventFlags {
        guard let chord, !chord.trimmingCharacters(in: .whitespaces).isEmpty else { return [] }
        var flags: CGEventFlags = []
        for part in chord.split(separator: "+") {
            let name = part.trimmingCharacters(in: .whitespaces).lowercased()
            guard let flag = modifierNames[name] else { throw ServiceError.invalid("modifiers 只接受修饰键：\(chord)") }
            flags.insert(flag)
        }
        return flags
    }
}

// MARK: - 虚拟光标覆盖层

/// 小羽自己的指针：一个不接鼠标事件、浮在所有窗口之上的透明面板，画一枚羽毛缺口的箭头
/// 和一圈光环。每个动作前先把它挪到要操作的位置（元素动作不动系统光标，它是唯一的
/// 视觉线索），按下时缩一下并荡开一圈涟漪，空闲 1.5 秒淡出。用户的真实光标照常可用、
/// 与它一眼可分。全部公开 API（AppKit + CoreAnimation），窗口层级用辅助技术专用层。
final class VirtualCursor {
    static let shared = VirtualCursor()
    static var enabled: Bool { (UserDefaults.standard.object(forKey: "showCursor") as? Bool) ?? true }

    private let size: CGFloat = 96
    //  指针尖在面板里的位置。图层用 AppKit 默认的左下角原点（视图会按自己的 isFlipped 覆盖图层的翻转
    //  标志，手动设 isGeometryFlipped 不可靠），所以距顶 36 点就是距底 size - 36 点。
    private let hotspot = CGPoint(x: 36, y: 96 - 36)
    private var panel: NSPanel?
    private var pointer = CAShapeLayer()
    private var halo = CAShapeLayer()
    private var lastPoint: CGPoint?
    private var hideTimer: Timer?

    private func ensurePanel() -> NSPanel {
        if let panel { return panel }
        let panel = NSPanel(contentRect: NSRect(x: 0, y: 0, width: size, height: size),
                            styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = false
        panel.ignoresMouseEvents = true
        panel.hidesOnDeactivate = false
        panel.isReleasedWhenClosed = false
        panel.level = NSWindow.Level(rawValue: Int(CGWindowLevelForKey(.assistiveTechHighWindow)))
        panel.collectionBehavior = [.canJoinAllSpaces, .stationary, .fullScreenAuxiliary, .ignoresCycle]
        let view = NSView(frame: panel.contentRect(forFrameRect: panel.frame))
        view.wantsLayer = true
        //  光环：以指针尖为圆心
        halo.path = CGPath(ellipseIn: CGRect(x: -22, y: -22, width: 44, height: 44), transform: nil)
        halo.position = hotspot
        halo.fillColor = CGColor(red: 0.45, green: 0.40, blue: 1.0, alpha: 0.18)
        halo.strokeColor = CGColor(red: 0.45, green: 0.40, blue: 1.0, alpha: 0.9)
        halo.lineWidth = 2
        halo.opacity = 0
        view.layer?.addSublayer(halo)
        //  指针：经典箭头，尾部带一个羽毛缺口；尖在 (0,0)，身体向右下延伸（y 向上为正，所以取负）
        let path = CGMutablePath()
        let pts: [(CGFloat, CGFloat)] = [(0, 0), (0, 30), (8, 23), (14, 36), (19, 34), (13, 21), (23, 21), (17, 15), (21, 10), (12, 11)]
        path.move(to: CGPoint(x: pts[0].0, y: -pts[0].1))
        for (x, y) in pts.dropFirst() { path.addLine(to: CGPoint(x: x, y: -y)) }
        path.closeSubpath()
        pointer.path = path
        pointer.position = hotspot
        pointer.fillColor = CGColor(red: 1, green: 1, blue: 1, alpha: 0.97)
        pointer.strokeColor = CGColor(red: 0.36, green: 0.40, blue: 0.96, alpha: 1)
        pointer.lineWidth = 1.6
        pointer.lineJoin = .round
        pointer.shadowColor = CGColor(red: 0, green: 0, blue: 0, alpha: 0.45)
        pointer.shadowOpacity = 1
        pointer.shadowRadius = 3
        pointer.shadowOffset = CGSize(width: 0, height: 1.5)
        view.layer?.addSublayer(pointer)
        panel.contentView = view
        self.panel = panel
        return panel
    }

    /// AX / CG 的屏幕坐标（主屏左上为原点）→ 面板左下角在 AppKit 坐标（主屏左下为原点），使指针尖落在该点
    private func frameOrigin(for point: CGPoint) -> NSPoint {
        let primaryHeight = NSScreen.screens.first?.frame.height ?? 0
        return NSPoint(x: point.x - hotspot.x, y: primaryHeight - point.y - hotspot.y)
    }

    func show(at point: CGPoint, press: Bool = false) {
        guard VirtualCursor.enabled else { return }
        DispatchQueue.main.async { [self] in
            let panel = ensurePanel()
            hideTimer?.invalidate()
            let origin = frameOrigin(for: point)
            let target = NSRect(origin: origin, size: NSSize(width: size, height: size))
            if !panel.isVisible || panel.alphaValue < 0.5 {
                panel.setFrame(target, display: false)
                panel.alphaValue = 0
                panel.orderFrontRegardless()
                NSAnimationContext.runAnimationGroup { ctx in ctx.duration = 0.12; panel.animator().alphaValue = 1 }
            } else if let last = lastPoint {
                //  挪动：沿运动方向轻微拉伸再弹回（看得出是"飞过去"而不是瞬移）
                let dx = point.x - last.x, dy = point.y - last.y
                let dist = max(1, hypot(dx, dy))
                let angle = atan2(-dy, dx)   // 图层 y 轴向上，屏幕坐标 y 轴向下
                let stretch = min(1.35, 1 + dist / 600)
                var t = CATransform3DMakeRotation(angle, 0, 0, 1)
                t = CATransform3DScale(t, stretch, 1 / stretch, 1)
                t = CATransform3DRotate(t, -angle, 0, 0, 1)
                pointer.transform = t
                NSAnimationContext.runAnimationGroup({ ctx in
                    ctx.duration = min(0.35, 0.12 + dist / 2500)
                    ctx.timingFunction = CAMediaTimingFunction(name: .easeInEaseOut)
                    panel.animator().setFrame(target, display: true)
                }, completionHandler: { [self] in pointer.transform = CATransform3DIdentity })
            }
            lastPoint = point
            if press { self.press() }
            scheduleHide()
        }
    }

    func press() {
        DispatchQueue.main.async { [self] in
            let squeeze = CABasicAnimation(keyPath: "transform.scale")
            squeeze.fromValue = 1; squeeze.toValue = 0.82
            squeeze.duration = 0.09; squeeze.autoreverses = true
            pointer.add(squeeze, forKey: "press")
            let ring = CAAnimationGroup()
            let grow = CABasicAnimation(keyPath: "transform.scale"); grow.fromValue = 0.4; grow.toValue = 1.6
            let fade = CABasicAnimation(keyPath: "opacity"); fade.fromValue = 1; fade.toValue = 0
            ring.animations = [grow, fade]; ring.duration = 0.45
            ring.timingFunction = CAMediaTimingFunction(name: .easeOut)
            halo.add(ring, forKey: "ripple")
        }
    }

    private func scheduleHide() {
        hideTimer?.invalidate()
        hideTimer = Timer.scheduledTimer(withTimeInterval: 1.5, repeats: false) { [self] _ in hide() }
    }

    func hide() {
        DispatchQueue.main.async { [self] in
            guard let panel, panel.isVisible else { return }
            hideTimer?.invalidate()
            NSAnimationContext.runAnimationGroup({ ctx in ctx.duration = 0.25; panel.animator().alphaValue = 0 },
                                                completionHandler: { panel.orderOut(nil) })
            lastPoint = nil
            halo.removeAllAnimations()
        }
    }
}

// MARK: - 请求处理

final class Service {
    private var ledgers: [pid_t: Ledger] = [:]
    private var spaces: [pid_t: Space] = [:]
    private let lock = NSLock()

    func handle(method: String, params: [String: Any]) throws -> Any {
        //  ping 不进锁：别的连接正卡在一个慢动作里时，新客户端的握手也要能立刻得到应答
        if method == "ping" {
            return ["apiVersion": apiVersion, "pid": Int(getpid()),
                    "accessibility": AXIsProcessTrusted(), "screenCapture": CGPreflightScreenCaptureAccess()]
        }
        lock.lock(); defer { lock.unlock() }
        switch method {
        case "list_apps":
            return ["apps": Apps.running().map { $0.json() }]
        case "probe_activity":
            //  诊断用：每类事件距今秒数（系统计时）与我们据此判断的"用户最近一次输入"
            var per: [String: Double] = [:]
            for (name, type) in [("mouseMoved", CGEventType.mouseMoved), ("leftMouseDown", .leftMouseDown),
                                 ("rightMouseDown", .rightMouseDown), ("otherMouseDown", .otherMouseDown),
                                 ("leftMouseDragged", .leftMouseDragged), ("scrollWheel", .scrollWheel),
                                 ("keyDown", .keyDown), ("flagsChanged", .flagsChanged)] {
                per[name] = CGEventSource.secondsSinceLastEventType(.hidSystemState, eventType: type)
            }
            return ["perType": per, "userSecondsAgo": Activity.secondsSinceUserInput() as Any]
        case "probe_input":
            //  诊断用：把鼠标挪到指定屏幕坐标再读回，判断合成事件有没有真的生效
            try Permissions.ensureAccessibility()
            try Activity.assertUserIdle()
            let before = CGEvent(source: nil)?.location ?? .zero
            let x = clampDouble((params["x"] as? NSNumber)?.doubleValue ?? before.x + 1, 0, 1e5)
            let y = clampDouble((params["y"] as? NSNumber)?.doubleValue ?? before.y + 1, 0, 1e5)
            try Input.move(to: CGPoint(x: x, y: y))
            Thread.sleep(forTimeInterval: 0.1)
            let after = CGEvent(source: nil)?.location ?? .zero
            try? Input.move(to: before)
            Activity.forget()
            return ["before": [before.x, before.y], "after": [after.x, after.y], "wanted": [x, y]]
        case "get_state":
            return try getState(params)
        case "perform":
            return try perform(params)
        default:
            throw ServiceError(code: -32601, message: "未知方法 \(method)")
        }
    }

    private func app(_ params: [String: Any]) throws -> AppRef {
        guard let query = params["app"] as? String else { throw ServiceError.invalid("缺少 app") }
        return try Apps.resolve(query)
    }

    private func getState(_ params: [String: Any]) throws -> Any {
        try Permissions.ensureAccessibility()
        //  锁屏下系统不向无障碍暴露任何窗口，不先判会误报成"应用没有窗口"
        try Permissions.ensureUnlocked()
        let app = try self.app(params)
        let wantShot = (params["screenshot"] as? Bool) ?? true
        let window = try pickWindow(app)
        let space = Space.of(window.frame)
        //  已退出的应用的簿子不再留着（里面全是失效的元素引用）
        for pid in ledgers.keys where NSRunningApplication(processIdentifier: pid) == nil {
            ledgers[pid] = nil; spaces[pid] = nil
        }
        spaces[app.pid] = space
        let ledger = ledgers[app.pid] ?? Ledger()
        ledgers[app.pid] = ledger
        ledger.begin()
        var walker = Walker(ledger: ledger, space: space)
        walker.walk(window.element, depth: 0, path: "")
        //  弹出菜单 / 浮窗 / 对话框是应用级子元素，不挂在主窗口下；打开后不走这一步模型就看不见
        let appElement = AXUIElementCreateApplication(app.pid)
        if let siblings = attribute(appElement, kAXChildrenAttribute) as? [AXUIElement] {
            var ordinal = 0
            for sibling in siblings where !CFEqual(sibling, window.element) {
                let role = axString(sibling, kAXRoleAttribute) ?? ""
                let subrole = axString(sibling, kAXSubroleAttribute) ?? ""
                let floating = ["AXMenu", "AXPopover", "AXSheet"].contains(role)
                    || (role == "AXWindow" && ["AXDialog", "AXSystemDialog", "AXFloatingWindow"].contains(subrole))
                    || (role == "AXWindow" && (axBool(sibling, kAXModalAttribute) ?? false))
                guard floating, (axBool(sibling, kAXMinimizedAttribute) ?? false) == false else { continue }
                walker.walk(sibling, depth: 0, path: "floating[\(ordinal)]")
                ordinal += 1
            }
        }
        //  菜单栏单独放末尾：它属于应用不属于窗口，但点菜单是最常用的"找功能"路径
        if let menubar = axElement(attribute(appElement, kAXMenuBarAttribute)) {
            walker.lines.append("menuBar")
            if let items = attribute(menubar, kAXChildrenAttribute) as? [AXUIElement] {
                var ordinals: [String: Int] = [:]
                for item in items {
                    let ordinal = ordinals["AXMenuBarItem", default: 0]
                    ordinals["AXMenuBarItem"] = ordinal + 1
                    walker.walkShallow(item, depth: 1, path: "menubar[\(ordinal)]")
                }
            }
        }
        ledger.finish()
        var result: [String: Any] = [
            "app": app.json(),
            "window": ["title": window.title, "x": Int(window.frame.minX), "y": Int(window.frame.minY),
                       "width": Int(window.frame.width), "height": Int(window.frame.height)],
            "space": ["width": space.width, "height": space.height, "scale": Double(space.scale)],
            "tree": walker.lines.joined(separator: "\n"),
            "elements": ledger.elements.count,
            "truncated": walker.truncated,
        ]
        if wantShot {
            if let number = window.number {
                do {
                    result["screenshot"] = ["png_base64": try captureWindow(number, space: space).base64EncodedString(),
                                            "width": space.width, "height": space.height]
                } catch let error as ServiceError where error.code == -32001 {
                    result["screenshot_error"] = error.message
                } catch {
                    result["screenshot_error"] = (error as? ServiceError)?.message ?? error.localizedDescription
                }
            } else {
                result["screenshot_error"] = "没找到这个窗口对应的窗口号，截不了图（无障碍树仍可用）"
            }
        }
        return result
    }

    //  每种动作一个函数；共享的解析与核对在 ActionContext 里。新加动作：Actions 里写函数、表里登记一行。
    private static let actions: [String: (ActionContext) throws -> String] = [
        "click": Actions.click, "double_click": Actions.doubleClick, "activate": Actions.activate,
        "type": Actions.type, "key": Actions.key, "scroll": Actions.scroll, "drag": Actions.drag,
        "paste": Actions.paste, "select_text": Actions.selectText, "set_value": Actions.setValue, "action": Actions.action,
    ]
    //  做完把系统光标还给用户的动作（拖拽不还：终点就是意图；元素动作本来就不动光标）
    private static let restoresCursor: Set<String> = ["click", "double_click", "scroll"]

    private func perform(_ params: [String: Any]) throws -> Any {
        try Permissions.ensureAccessibility()
        try Permissions.ensureUnlocked()
        let app = try self.app(params)
        guard let action = params["action"] as? [String: Any], let kind = action["kind"] as? String else {
            throw ServiceError.invalid("缺少 action.kind")
        }
        guard let run = Service.actions[kind] else { throw ServiceError.invalid("未知 action.kind：\(kind)") }
        let background = (action["background"] as? Bool) ?? false
        let context = ActionContext(app: app, action: action, ledger: ledgers[app.pid], space: spaces[app.pid],
                                    live: try? pickWindow(app), background: background)
        Activity.forget()
        Input.backgroundPid = background ? app.pid : nil
        Input.backgroundWindow = background ? context.live?.number : nil
        let cursorBefore = Activity.cursor()
        defer {
            Input.backgroundPid = nil
            Input.backgroundWindow = nil
            //  光标已被用户挪走、或用户刚有活动，就不碰
            if !background, Service.restoresCursor.contains(kind), Activity.moved(),
               (try? Activity.assertCursorUntouched()) != nil, Activity.secondsSinceUserInput().map({ $0 > 0.5 }) ?? true {
                let restore = CGEvent(mouseEventSource: nil, mouseType: .mouseMoved, mouseCursorPosition: cursorBefore, mouseButton: .left)
                Activity.notePost()
                restore?.post(tap: .cghidEventTap)
            }
            Activity.forget()
        }
        let note = try run(context)
        //  给界面一点反应时间再回，调用方紧接着的 get_state 就不会拍到半成品
        Thread.sleep(forTimeInterval: 0.25)
        return ["ok": true, "note": note]
    }
}

// MARK: - 动作上下文与各动作

/// 一次 perform 里各动作共用的东西：目标应用、参数、索引簿、坐标空间、刚读到的窗口。
/// 参数取用、元素解析、坐标换算、可视区核对、虚拟光标提示都在这里，动作函数只写自己的事。
struct ActionContext {
    let app: AppRef
    let action: [String: Any]
    let ledger: Ledger?
    let space: Space?
    let live: WindowInfo?
    let background: Bool

    // -- 参数 --

    var hasElement: Bool { action["element"] != nil }
    func string(_ key: String) -> String? { action[key] as? String }
    func number(_ key: String) -> Double? { (action[key] as? NSNumber)?.doubleValue }
    func integer(_ key: String, default value: Int) -> Int { (action[key] as? NSNumber)?.intValue ?? value }
    func requireString(_ key: String, _ hint: String = "") throws -> String {
        guard let value = action[key] as? String else { throw ServiceError.invalid("缺少 \(key)\(hint)") }
        return value
    }

    // -- 元素与坐标 --

    func element() throws -> AXUIElement {
        guard let raw = action["element"], let index = (raw as? NSNumber)?.intValue ?? Int("\(raw)") else {
            throw ServiceError.invalid("缺少 element（元素索引）")
        }
        guard let ledger else { throw ServiceError.stale(index) }
        return try ledger.element(index)
    }

    /// 给了 element 就用它，否则取应用当前的焦点元素
    func elementOrFocused() throws -> AXUIElement {
        if hasElement { return try element() }
        let root = AXUIElementCreateApplication(app.pid)
        guard let focus = axElement(attribute(root, kAXFocusedUIElementAttribute)) else {
            throw ServiceError.unsupported("应用当前没有焦点元素；用 element 指名")
        }
        return focus
    }

    /// 坐标单位 → 屏幕点。按最近一次 get_state 的窗口框换算：窗口此后被挪动就跟着挪；尺寸变了坐标系就作废。
    /// 窗口外的点落在别的应用上：拒绝，不然"按键不落进别的窗口"的承诺就破了。
    func point(_ xKey: String = "x", _ yKey: String = "y") throws -> CGPoint {
        guard let x = number(xKey), let y = number(yKey) else { throw ServiceError.invalid("缺少坐标 \(xKey)/\(yKey)") }
        guard x.isFinite, y.isFinite, abs(x) < 1e6, abs(y) < 1e6 else { throw ServiceError.invalid("坐标不是有效数值：\(x), \(y)") }
        guard let space else { throw ServiceError.invalid("还没对这个应用 get_state，坐标没有参照；先 get_state") }
        var frame = space.frame
        if let live {
            if abs(live.frame.width - frame.width) > 2 || abs(live.frame.height - frame.height) > 2 {
                throw ServiceError.invalid("窗口尺寸自上次 get_state 后变了（\(Int(frame.width))×\(Int(frame.height)) → \(Int(live.frame.width))×\(Int(live.frame.height))），坐标已不可信；先 get_state")
            }
            frame = live.frame
        }
        let current = Space(frame: frame, scale: space.scale)
        guard x >= 0, y >= 0, x <= Double(current.width), y <= Double(current.height) else {
            throw ServiceError.invalid("坐标 (\(Int(x)), \(Int(y))) 超出窗口范围 \(current.width)×\(current.height)，没有执行")
        }
        return current.toScreen(CGPoint(x: x, y: y))
    }

    /// 元素中心（屏幕点）。元素可能被滚出可视区或在窗口之外，那个点上显示的是别的东西：
    /// 沿父链找最近的滚动区与窗口，中心点必须落在它们里面。
    func center(of el: AXUIElement) throws -> CGPoint {
        guard let frame = axFrame(el), frame.width >= 1, frame.height >= 1 else { throw ServiceError.unsupported("该元素没有位置信息，改用坐标") }
        let c = CGPoint(x: frame.midX, y: frame.midY)
        var parent = axElement(attribute(el, kAXParentAttribute))
        var hops = 0
        while let p = parent, hops < 16 {
            let role = axString(p, kAXRoleAttribute) ?? ""
            if ["AXScrollArea", "AXWindow", "AXSheet", "AXDrawer", "AXPopover"].contains(role), let pf = axFrame(p) {
                if !pf.insetBy(dx: -1, dy: -1).contains(c) {
                    throw ServiceError.unsupported("元素的中心不在可视区域内（被滚出或在窗口外），没有执行。先 scroll 让它露出来，或改用别的元素")
                }
                if role != "AXScrollArea" { break }
            }
            parent = axElement(attribute(p, kAXParentAttribute))
            hops += 1
        }
        return c
    }

    func target() throws -> CGPoint {
        if hasElement { return try center(of: try element()) }
        return try point()
    }

    // -- 虚拟光标 --

    /// 合成点击前：飞到落点、停一拍、按下（用户看得出"要点这里了"）
    func aimCursor(at p: CGPoint) {
        VirtualCursor.shared.show(at: p)
        Thread.sleep(forTimeInterval: 0.15)
        VirtualCursor.shared.press()
    }

    /// 元素动作不动系统光标，虚拟光标是唯一的视觉线索：在元素中心闪一下
    func flashCursor(at el: AXUIElement) {
        if let frame = axFrame(el) { VirtualCursor.shared.show(at: CGPoint(x: frame.midX, y: frame.midY), press: true) }
    }

    /// 点进元素让它拿到焦点（键入 / 粘贴前）
    func clickInto(_ el: AXUIElement) throws {
        let p = try center(of: el)
        try Input.requireActive(app)
        aimCursor(at: p)
        try Input.click(at: p, button: "left", count: 1, flags: [])
        Thread.sleep(forTimeInterval: 0.1)
    }
}

enum Actions {
    static func click(_ c: ActionContext) throws -> String {
        let button = (c.string("button") ?? "left").lowercased()
        let count = max(1, min(3, c.integer("count", default: 1)))
        let flags = try Input.flags(c.string("modifiers"))
        //  普通左键单击元素优先走无障碍动作：不动鼠标、不抢焦点
        if c.hasElement, button == "left", count == 1, flags.isEmpty {
            let el = try c.element()
            if axActions(el).contains("AXPress") {
                if let frame = axFrame(el) { c.aimCursor(at: CGPoint(x: frame.midX, y: frame.midY)) }
                if AXUIElementPerformAction(el, "AXPress" as CFString) == .success { return "已通过无障碍动作按下（未移动鼠标）" }
            }
            //  没有 AXPress（图标、网页里的 div）：退回坐标点击
        }
        let p = try c.target()
        try Input.requireActive(c.app)
        c.aimCursor(at: p)
        try Input.click(at: p, button: button, count: count, flags: flags)
        return "已在 \(button) 键点击 \(count) 次"
    }

    static func doubleClick(_ c: ActionContext) throws -> String {
        let p = try c.target()
        try Input.requireActive(c.app)
        c.aimCursor(at: p)
        try Input.click(at: p, button: "left", count: 2, flags: [])
        return "已双击"
    }

    static func activate(_ c: ActionContext) throws -> String {
        try Input.requireActive(c.app)
        return "已切到前台"
    }

    static func type(_ c: ActionContext) throws -> String {
        let text = try c.requireString("text")
        if c.hasElement {
            let el = try c.element()
            c.flashCursor(at: el)
            //  能直接设值的编辑框就不敲键盘：不抢焦点、不受输入法影响
            if axSettable(el, kAXValueAttribute), (c.action["append"] as? Bool) != true,
               AXUIElementSetAttributeValue(el, kAXValueAttribute as CFString, text as CFTypeRef) == .success {
                return "已直接设置元素的值（整体替换）"
            }
            try c.clickInto(el)
        } else {
            //  应用不在前台时先试无障碍插入：焦点元素接受 AXSelectedText 就直接插（确定能进、
            //  不经键盘、不抢前台）；不行再退回键盘——background 模式投给进程，否则切前台
            if c.background || !c.app.app.isActive, let focus = try? c.elementOrFocused(),
               axSettable(focus, kAXSelectedTextAttribute),
               AXUIElementSetAttributeValue(focus, kAXSelectedTextAttribute as CFString, text as CFTypeRef) == .success {
                return "已经无障碍插入到焦点输入框（未经键盘、未切前台）"
            }
            if !c.background { try Input.requireActive(c.app) }
        }
        try Input.type(text)
        return c.background ? "已把键盘事件投给后台进程（应用是否接收取决于它自己）" : "已按键盘方式键入"
    }

    static func key(_ c: ActionContext) throws -> String {
        let chord = try c.requireString("key")
        try Input.requireActive(c.app)
        try Input.chord(chord)
        return "已按下 \(chord)"
    }

    static func scroll(_ c: ActionContext) throws -> String {
        let direction = (c.string("direction") ?? "down").lowercased()
        let pages = clampDouble(c.number("pages") ?? 1, 0.1, 20)
        let vertical = direction == "up" || direction == "down"
        var p: CGPoint
        var span: CGFloat = 400
        if c.hasElement {
            let el = try c.element()
            p = try c.center(of: el)
            let frame = axFrame(el) ?? CGRect(origin: p, size: CGSize(width: 400, height: 400))
            span = vertical ? frame.height : frame.width
        } else if c.action["x"] != nil {
            p = try c.point()
        } else {
            guard let space = c.space else { throw ServiceError.invalid("先 get_state") }
            p = CGPoint(x: space.frame.midX, y: space.frame.midY)
            span = vertical ? space.frame.height : space.frame.width
        }
        try Input.requireActive(c.app)
        VirtualCursor.shared.show(at: p)
        Thread.sleep(forTimeInterval: 0.1)
        try Input.scroll(at: p, direction: direction, pixels: Int(clampDouble(max(60, span * 0.8) * pages, 1, 20_000)))
        return "已向 \(direction) 滚动 \(pages) 页"
    }

    static func drag(_ c: ActionContext) throws -> String {
        let from = try c.point("from_x", "from_y")
        let to = try c.point("to_x", "to_y")
        try Input.requireActive(c.app)
        VirtualCursor.shared.show(at: from, press: true)
        Thread.sleep(forTimeInterval: 0.15)
        VirtualCursor.shared.show(at: to)
        try Input.drag(from: from, to: to)
        return "已拖拽"
    }

    static func paste(_ c: ActionContext) throws -> String {
        let text = try c.requireString("text")
        let format = (c.string("format") ?? "text").lowercased()
        if c.hasElement { try c.clickInto(try c.element()) } else { try Input.requireActive(c.app) }
        try Clipboard.paste(text, format: format)
        return "已经剪贴板粘贴（\(format)），原剪贴板内容已恢复"
    }

    static func selectText(_ c: ActionContext) throws -> String {
        let el = try c.elementOrFocused()
        guard let content = axString(el, kAXValueAttribute) else { throw ServiceError.unsupported("该元素没有文本值，无法选区") }
        let whole = content as NSString
        var range = NSRange(location: 0, length: whole.length)
        var label = clip(content, 40)
        if let needle = c.string("text"), !needle.isEmpty {
            let occurrence = max(1, min(1000, c.integer("occurrence", default: 1)))
            var found = NSRange(location: NSNotFound, length: 0)
            var from = 0
            for _ in 0..<occurrence {
                found = whole.range(of: needle, options: [], range: NSRange(location: from, length: whole.length - from))
                if found.location == NSNotFound { break }
                from = found.location + found.length
            }
            guard found.location != NSNotFound else {
                throw ServiceError.unsupported("元素的文本里没有第 \(occurrence) 处「\(clip(needle, 60))」（文本开头：「\(clip(content, 80))」）")
            }
            range = found
            label = clip(needle, 40)
        }
        let mode = (c.string("mode") ?? "select").lowercased()
        switch mode {
        case "select": break
        case "start": range = NSRange(location: range.location, length: 0)
        case "end": range = NSRange(location: range.location + range.length, length: 0)
        default: throw ServiceError.invalid("mode 只能是 select / start / end")
        }
        guard axSettable(el, kAXSelectedTextRangeAttribute) else {
            throw ServiceError.unsupported("该元素不支持直接设置选区；改用 click 定位后 key shift+方向键")
        }
        //  先让它拿到键盘焦点，之后的 type / paste / Delete 才落在这里
        AXUIElementSetAttributeValue(el, kAXFocusedAttribute as CFString, kCFBooleanTrue)
        var cfRange = CFRange(location: range.location, length: range.length)
        guard let value = AXValueCreate(.cfRange, &cfRange) else { throw ServiceError.unsupported("无法构造选区") }
        let result = AXUIElementSetAttributeValue(el, kAXSelectedTextRangeAttribute as CFString, value)
        guard result == .success else { throw ServiceError.unsupported("设置选区失败（AX 错误码 \(result.rawValue)）；改用 click 定位后 key shift+方向键") }
        c.flashCursor(at: el)
        switch mode {
        case "select":
            let selected = axString(el, kAXSelectedTextAttribute) ?? ""
            return "已选中 \(range.length) 个字符：「\(clip(selected, 80))」；接着 type / paste 会替换选中内容，key Delete 删除"
        case "start": return "光标已放到「\(label)」前面（未选中文本）"
        default: return "光标已放到「\(label)」后面（未选中文本）"
        }
    }

    static func setValue(_ c: ActionContext) throws -> String {
        let el = try c.element()
        c.flashCursor(at: el)
        let value = try c.requireString("value")
        guard axSettable(el, kAXValueAttribute) else { throw ServiceError.unsupported("元素的值不可直接设置；改用 click 聚焦后 type，或 key cmd+a 再 type") }
        let result = AXUIElementSetAttributeValue(el, kAXValueAttribute as CFString, value as CFTypeRef)
        if result != .success { throw ServiceError.unsupported("设置值失败（AX 错误码 \(result.rawValue)）；改用 click + type") }
        return "已设置元素的值"
    }

    static func action(_ c: ActionContext) throws -> String {
        let el = try c.element()
        c.flashCursor(at: el)
        let name = try c.requireString("name", "（动作名，见树里 actions: 列表）")
        guard !name.isEmpty else { throw ServiceError.invalid("缺少 name（动作名，见树里 actions: 列表）") }
        let available = axActions(el)
        let wanted = name.lowercased().replacingOccurrences(of: " ", with: "")
        guard let match = available.first(where: {
            let bare = $0.lowercased()
            return bare == wanted || bare == "ax" + wanted || bare.dropFirst(2) == wanted
        }) else {
            throw ServiceError.unsupported("元素没有动作「\(name)」。它有的：\(available.map { String($0.dropFirst(2)) }.joined(separator: ", "))")
        }
        let result = AXUIElementPerformAction(el, match as CFString)
        if result != .success { throw ServiceError.unsupported("执行动作 \(match) 失败（AX 错误码 \(result.rawValue)）") }
        return "已执行 \(String(match.dropFirst(2)))"
    }
}

extension Walker {
    /// 菜单栏只走一层：菜单项的子菜单在打开前通常是空的，整棵走没有意义
    mutating func walkShallow(_ element: AXUIElement, depth: Int, path: String) {
        let role = axString(element, kAXRoleAttribute) ?? "AXMenuBarItem"
        let title = axString(element, kAXTitleAttribute) ?? ""
        let key = "\(path)/\(role):\(title)"
        let n = ledger.index(for: key, element: element)
        //  菜单栏在窗口之外，坐标对它没意义：点它一律走无障碍动作
        let pieces = ["[\(n)]", "menuBarItem", "\"\(clip(title, 60))\""]
        lines.append(String(repeating: "  ", count: depth) + pieces.joined(separator: " "))
        count += 1
        //  只有展开着的菜单（菜单栏项处于选中态）才往下走：没展开的菜单项也在树里、
        //  但尺寸为零、点不着，列出来只会误导模型
        if axBool(element, kAXSelectedAttribute) == true,
           let children = attribute(element, kAXChildrenAttribute) as? [AXUIElement], !children.isEmpty {
            var ordinal = 0
            for child in children { walk(child, depth: depth + 1, path: "\(key)[\(ordinal)]"); ordinal += 1 }
        }
    }
}

// MARK: - 传输层：unix socket + 长度前缀帧

final class Server {
    let path: String
    let service = Service()
    private var fd: Int32 = -1
    private let stateLock = NSLock()
    private var connections = 0
    private var lastActivity = Date()

    init(path: String) { self.path = path }

    /// 没有客户端连着、且这么久没有请求：可以退出了（减少持有授权的空闲进程，下次按需再拉起）
    func idle(longerThan seconds: TimeInterval) -> Bool {
        stateLock.lock(); defer { stateLock.unlock() }
        return connections == 0 && Date().timeIntervalSince(lastActivity) > seconds
    }

    /// 退出前把可能留在系统里的状态收干净：输入源切回、按着的鼠标键抬起、socket 文件删掉
    func shutdown() {
        Keyboard.restore()
        Input.releaseLeftButtonIfHeld()
        if fd >= 0 { close(fd); fd = -1 }
        unlink(path)
    }

    func start() throws {
        try FileManager.default.createDirectory(atPath: (path as NSString).deletingLastPathComponent, withIntermediateDirectories: true)
        //  上一次没清干净的 socket 文件：先看有没有人在听，有就说明已经有一份在跑
        if FileManager.default.fileExists(atPath: path) {
            if probe() { throw ServiceError(code: -1, message: "已有一个 Xiaoyu Computer Use 在 \(path) 上服务") }
            unlink(path)
        }
        fd = socket(AF_UNIX, SOCK_STREAM, 0)
        guard fd >= 0 else { throw ServiceError(code: -1, message: "socket() 失败") }
        var addr = sockaddr_un()
        addr.sun_family = sa_family_t(AF_UNIX)
        let bytes = Array(path.utf8)
        guard bytes.count < MemoryLayout.size(ofValue: addr.sun_path) else { throw ServiceError(code: -1, message: "socket 路径过长") }
        withUnsafeMutableBytes(of: &addr.sun_path) { raw in
            raw.copyBytes(from: bytes)
            raw[bytes.count] = 0
        }
        let size = socklen_t(MemoryLayout<sockaddr_un>.size)
        //  bind 创建文件的那一刻就只给自己：先 chmod 的话中间有个任何人都能连的窗口
        let previousMask = umask(0o077)
        let bound = withUnsafePointer(to: &addr) { $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { bind(fd, $0, size) } }
        umask(previousMask)
        guard bound == 0 else { throw ServiceError(code: -1, message: "bind() 失败：\(String(cString: strerror(errno)))") }
        chmod(path, 0o600)
        guard listen(fd, 8) == 0 else { throw ServiceError(code: -1, message: "listen() 失败") }
        Thread.detachNewThread { [self] in acceptLoop() }
    }

    private func probe() -> Bool {
        let probe = socket(AF_UNIX, SOCK_STREAM, 0)
        defer { close(probe) }
        var addr = sockaddr_un()
        addr.sun_family = sa_family_t(AF_UNIX)
        let bytes = Array(path.utf8)
        withUnsafeMutableBytes(of: &addr.sun_path) { raw in raw.copyBytes(from: bytes); raw[bytes.count] = 0 }
        let size = socklen_t(MemoryLayout<sockaddr_un>.size)
        return withUnsafePointer(to: &addr) { $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { connect(probe, $0, size) } } == 0
    }

    private func acceptLoop() {
        while true {
            let client = accept(fd, nil, nil)
            if client < 0 {
                //  被打断、对端中途放弃、fd 暂时用光：都是瞬时的，歇一下接着收；别的错误才算监听坏了
                if errno == EINTR { continue }
                if errno == ECONNABORTED || errno == EMFILE || errno == ENFILE { Thread.sleep(forTimeInterval: 0.2); continue }
                FileHandle.standardError.write(Data("xiaoyu computer use: accept() 失败：\(String(cString: strerror(errno)))，退出\n".utf8))
                shutdown()
                exit(1)
            }
            Thread.detachNewThread { [self] in serve(client) }
        }
    }

    private func readExactly(_ client: Int32, _ n: Int) -> Data? {
        var data = Data(count: n)
        var got = 0
        while got < n {
            let r = data.withUnsafeMutableBytes { raw in read(client, raw.baseAddress! + got, n - got) }
            if r <= 0 { return nil }
            got += r
        }
        return data
    }

    private func serve(_ client: Int32) {
        defer { close(client) }
        //  只服务同一用户的进程。socket 文件本身是 0600，这里再按对端凭据核一次，路径被指到别处时也守得住
        var uid: uid_t = 0
        var gid: gid_t = 0
        guard getpeereid(client, &uid, &gid) == 0, uid == getuid() else { return }
        stateLock.lock(); connections += 1; lastActivity = Date(); stateLock.unlock()
        defer { stateLock.lock(); connections -= 1; lastActivity = Date(); stateLock.unlock() }
        while true {
            guard let header = readExactly(client, 4) else { return }
            stateLock.lock(); lastActivity = Date(); stateLock.unlock()
            let length = Int(header.withUnsafeBytes { $0.load(as: UInt32.self).littleEndian })
            if length <= 0 || length > maxFrameBytes { return }
            guard let body = readExactly(client, length) else { return }
            let reply = respond(body)
            var size = UInt32(reply.count).littleEndian
            var frame = Data(bytes: &size, count: 4)
            frame.append(reply)
            var sent = 0
            while sent < frame.count {
                let w = frame.withUnsafeBytes { raw in write(client, raw.baseAddress! + sent, frame.count - sent) }
                if w <= 0 { return }
                sent += w
            }
        }
    }

    private func respond(_ body: Data) -> Data {
        var id: Any = NSNull()
        func encode(_ object: [String: Any]) -> Data {
            (try? JSONSerialization.data(withJSONObject: object)) ?? Data("{\"jsonrpc\":\"2.0\",\"id\":null,\"error\":{\"code\":-32603,\"message\":\"响应编码失败\"}}".utf8)
        }
        guard let request = (try? JSONSerialization.jsonObject(with: body)) as? [String: Any],
              let method = request["method"] as? String else {
            return encode(["jsonrpc": "2.0", "id": id, "error": ["code": -32700, "message": "请求不是合法的 JSON-RPC"]])
        }
        id = request["id"] ?? NSNull()
        let params = request["params"] as? [String: Any] ?? [:]
        do {
            let result = try service.handle(method: method, params: params)
            return encode(["jsonrpc": "2.0", "id": id, "result": result])
        } catch let error as ServiceError {
            return encode(["jsonrpc": "2.0", "id": id, "error": ["code": error.code, "message": error.message]])
        } catch {
            return encode(["jsonrpc": "2.0", "id": id, "error": ["code": -32603, "message": error.localizedDescription]])
        }
    }
}

// MARK: - 命令行客户端

/// 带参数启动时不当服务，而当客户端：连上（必要时先拉起）服务，把结果打到标准输出。
/// 这让辅助 app 脱离小羽也能用——脚本、别的程序、或人在终端里直接敲；它只是
/// socket 协议的一层薄壳，不持有任何授权（授权在经 `open` 起来的那份服务里）。
enum CLI {
    struct Failure: Error {
        let message: String
        let status: Int32
    }

    static let usage = """
    用法：\(programName) <命令> [参数]（不带参数启动即为服务模式）

      ping                                  服务是否在、授权是否齐
      apps                                  运行中的应用
      state <应用> [--screenshot 文件.png]   窗口的无障碍树（元素带索引）；给文件名就同时存截图
      activate <应用>                       切到前台
      click <应用> <索引|x,y> [--right] [--middle] [--count N] [--mod cmd,shift]
      double-click <应用> <索引|x,y>
      type <应用> <文本|-> [--element N] [--append]      文本为 - 时读标准输入
      key <应用> <键>                       如 Return、cmd+s、ctrl+a
      scroll <应用> <索引|x,y> [--direction up|down|left|right] [--pages N]
      drag <应用> <x,y> <x,y>
      paste <应用> <文本|-> [--format text|markdown|html] [--element N]
      select <应用> [文本] [--element N] [--occurrence N] [--mode select|start|end]
      set <应用> <索引> <值>
      action <应用> <索引> <动作名>          树里 actions: 列出的名字
      do <应用> <kind> [键=值 ...]          直接拼 perform 参数：true/false/数值自动识别，json: 前缀给原始 JSON

    通用选项：--socket 路径（默认同 XIAOYU_COMPUTER_USE_SOCKET）、--json（原样打印结果）、
    --state（动作后再打一遍树）、--no-launch（服务没起就报错，不替你拉起）。
    应用可写名字、bundle id 或 .app 路径；坐标是窗口坐标单位（与 state 的截图像素一致）。
    退出码：0 成功、1 服务报错、2 用法错、3 连不上服务。
    """

    static var programName: String { (CommandLine.arguments[0] as NSString).lastPathComponent }

    static let booleanFlags: Set<String> = ["json", "state", "no-launch", "right", "middle", "append", "help"]

    static func run(_ argv: [String]) -> Never {
        do {
            let (positionals, options) = try parse(argv)
            if positionals.isEmpty || options["help"] != nil {
                print(usage)
                exit(positionals.isEmpty && options["help"] == nil ? 2 : 0)
            }
            let socket = (options["socket"] as? String).flatMap { $0.isEmpty ? nil : $0 } ?? defaultSocketPath()
            let connection = try Connection.open(path: socket, launch: options["no-launch"] == nil)
            let text = try dispatch(positionals, options, connection)
            if !text.isEmpty { print(text) }
            exit(0)
        } catch let failure as Failure {
            FileHandle.standardError.write(Data((failure.message + "\n").utf8))
            exit(failure.status)
        } catch let error as ServiceError {
            FileHandle.standardError.write(Data("错误 \(error.code)：\(error.message)\n".utf8))
            exit(1)
        } catch {
            FileHandle.standardError.write(Data("\(error)\n".utf8))
            exit(1)
        }
    }

    /// `--名 值` / `--开关`，其余按位置；`--` 之后全按位置（文本以 - 开头时用）
    static func parse(_ argv: [String]) throws -> ([String], [String: Any]) {
        var positionals: [String] = []
        var options: [String: Any] = [:]
        var i = 0
        var literal = false
        while i < argv.count {
            let token = argv[i]
            i += 1
            if literal || !token.hasPrefix("--") || token == "-" {
                positionals.append(token)
                continue
            }
            if token == "--" { literal = true; continue }
            let name = String(token.dropFirst(2))
            if booleanFlags.contains(name) {
                options[name] = true
            } else {
                guard i < argv.count else { throw Failure(message: "选项 --\(name) 缺少值\n\n\(usage)", status: 2) }
                options[name] = argv[i]
                i += 1
            }
        }
        return (positionals, options)
    }

    static func dispatch(_ args: [String], _ options: [String: Any], _ connection: Connection) throws -> String {
        let command = args[0]
        let json = options["json"] != nil
        func need(_ n: Int, _ shape: String) throws {
            guard args.count >= n + 1 else { throw Failure(message: "用法：\(command) \(shape)", status: 2) }
        }
        func render(_ result: Any) -> String {
            let data = (try? JSONSerialization.data(withJSONObject: result, options: [.prettyPrinted, .sortedKeys])) ?? Data()
            return String(decoding: data, as: UTF8.self)
        }
        switch command {
        case "ping":
            let result = try connection.call("ping")
            if json { return render(result) }
            let r = result as? [String: Any] ?? [:]
            return "服务在线 pid=\(r["pid"] ?? "?")  辅助功能=\(yesNo(r["accessibility"]))  屏幕录制=\(yesNo(r["screenCapture"]))  协议=\(r["apiVersion"] ?? "?")"
        case "apps":
            let result = try connection.call("list_apps")
            if json { return render(result) }
            let apps = (result as? [String: Any])?["apps"] as? [[String: Any]] ?? []
            return apps.map { "\($0["name"] ?? "?")  (\($0["id"] ?? "?"))" + (($0["active"] as? Bool) == true ? "  ← 前台" : "") }
                .joined(separator: "\n")
        case "state":
            try need(1, "<应用> [--screenshot 文件.png]")
            return try state(args[1], options, connection)
        case "activate", "click", "double-click", "type", "key", "scroll", "drag", "paste", "select", "set", "action", "do":
            try need(1, "<应用> …")
            let action = try build(command, Array(args.dropFirst(2)), options)
            let result = try connection.call("perform", ["app": args[1], "action": action])
            var out = json ? render(result) : "已执行 \(action["kind"] ?? command)：\((result as? [String: Any])?["note"] ?? "")"
            if options["state"] != nil {
                var rest = options
                rest["json"] = nil
                out += "\n\n" + (try state(args[1], rest, connection))
            }
            return out
        default:
            throw Failure(message: "未知命令「\(command)」\n\n\(usage)", status: 2)
        }
    }

    static func yesNo(_ value: Any?) -> String { (value as? Bool) == true ? "已授权" : "未授权" }

    static func state(_ app: String, _ options: [String: Any], _ connection: Connection) throws -> String {
        let shotPath = options["screenshot"] as? String
        var result = try connection.call("get_state", ["app": app, "screenshot": shotPath != nil]) as? [String: Any] ?? [:]
        if let path = shotPath {
            guard let shot = result["screenshot"] as? [String: Any], let b64 = shot["png_base64"] as? String,
                  let png = Data(base64Encoded: b64) else {
                throw Failure(message: "没拿到截图：\(result["screenshot_error"] ?? "服务未返回")", status: 1)
            }
            try png.write(to: URL(fileURLWithPath: path))
            result["screenshot"] = ["file": path, "width": shot["width"] ?? 0, "height": shot["height"] ?? 0]
        }
        if options["json"] != nil {
            let data = (try? JSONSerialization.data(withJSONObject: result, options: [.prettyPrinted, .sortedKeys])) ?? Data()
            return String(decoding: data, as: UTF8.self)
        }
        var lines: [String] = []
        if let window = result["window"] as? [String: Any], let space = result["space"] as? [String: Any] {
            let name = (result["app"] as? [String: Any])?["name"] as? String ?? app
            lines.append("\(name) — \(window["title"] ?? "")  \(space["width"] ?? "?")×\(space["height"] ?? "?")")
        }
        if let path = shotPath { lines.append("截图已存到 \(path)") }
        lines.append(result["tree"] as? String ?? "")
        if (result["truncated"] as? Bool) == true { lines.append("（树过大，已截断）") }
        return lines.joined(separator: "\n")
    }

    /// 把子命令的位置参数与选项拼成 perform 的 action 字典；校验留给服务端，这里只管形状
    static func build(_ command: String, _ rest: [String], _ options: [String: Any]) throws -> [String: Any] {
        var action: [String: Any] = [:]
        func target(_ spec: String, into: inout [String: Any]) throws {
            if spec.contains(",") {
                let parts = spec.split(separator: ",", maxSplits: 1).map { Double($0.trimmingCharacters(in: .whitespaces)) }
                guard parts.count == 2, let x = parts[0], let y = parts[1] else { throw Failure(message: "坐标要写成 x,y：\(spec)", status: 2) }
                into["x"] = x; into["y"] = y
            } else if let index = Int(spec) {
                into["element"] = index
            } else {
                throw Failure(message: "目标要么是元素索引，要么是 x,y 坐标：\(spec)", status: 2)
            }
        }
        func text(_ spec: String) -> String {
            spec == "-" ? String(decoding: FileHandle.standardInput.readDataToEndOfFile(), as: UTF8.self) : spec
        }
        func number(_ name: String) -> Double? { (options[name] as? String).flatMap(Double.init) }
        func integer(_ name: String) -> Int? { (options[name] as? String).flatMap(Int.init) }
        func require(_ n: Int, _ shape: String) throws {
            guard rest.count >= n else { throw Failure(message: "用法：\(command) <应用> \(shape)", status: 2) }
        }
        if let element = integer("element") { action["element"] = element }
        switch command {
        case "activate":
            action["kind"] = "activate"
        case "click", "double-click":
            try require(1, "<索引|x,y>")
            action["kind"] = command == "click" ? "click" : "double_click"
            try target(rest[0], into: &action)
            if options["right"] != nil { action["button"] = "right" }
            if options["middle"] != nil { action["button"] = "middle" }
            if let count = integer("count") { action["count"] = count }
            if let mods = options["mod"] as? String { action["modifiers"] = mods.split(separator: ",").map(String.init) }
        case "type":
            try require(1, "<文本|->")
            action["kind"] = "type"; action["text"] = text(rest[0])
            if options["append"] != nil { action["append"] = true }
        case "key":
            try require(1, "<键>")
            action["kind"] = "key"; action["key"] = rest[0]
        case "scroll":
            try require(1, "<索引|x,y>")
            action["kind"] = "scroll"
            try target(rest[0], into: &action)
            if let direction = options["direction"] as? String { action["direction"] = direction }
            if let pages = number("pages") { action["pages"] = pages }
        case "drag":
            try require(2, "<x,y> <x,y>")
            var from: [String: Any] = [:], to: [String: Any] = [:]
            try target(rest[0], into: &from); try target(rest[1], into: &to)
            guard let fx = from["x"], let fy = from["y"], let tx = to["x"], let ty = to["y"] else {
                throw Failure(message: "drag 的起点与终点都要是 x,y 坐标", status: 2)
            }
            action = ["kind": "drag", "from_x": fx, "from_y": fy, "to_x": tx, "to_y": ty]
        case "paste":
            try require(1, "<文本|->")
            action["kind"] = "paste"; action["text"] = text(rest[0])
            if let format = options["format"] as? String { action["format"] = format }
        case "select":
            action["kind"] = "select_text"
            if let first = rest.first { action["text"] = first }
            if let occurrence = integer("occurrence") { action["occurrence"] = occurrence }
            if let mode = options["mode"] as? String { action["mode"] = mode }
        case "set":
            try require(2, "<索引> <值>")
            guard let index = Int(rest[0]) else { throw Failure(message: "set 的第一个参数是元素索引", status: 2) }
            action = ["kind": "set_value", "element": index, "value": text(rest[1])]
        case "action":
            try require(2, "<索引> <动作名>")
            guard let index = Int(rest[0]) else { throw Failure(message: "action 的第一个参数是元素索引", status: 2) }
            action = ["kind": "action", "element": index, "name": rest[1]]
        case "do":
            try require(1, "<kind> [键=值 ...]")
            action["kind"] = rest[0]
            for pair in rest.dropFirst() {
                guard let eq = pair.firstIndex(of: "=") else { throw Failure(message: "do 的参数要写成 键=值：\(pair)", status: 2) }
                action[String(pair[..<eq])] = literal(String(pair[pair.index(after: eq)...]))
            }
        default:
            throw Failure(message: "未知命令「\(command)」", status: 2)
        }
        if options["background"] != nil { action["background"] = true }
        return action
    }

    static func literal(_ raw: String) -> Any {
        if raw == "true" { return true }
        if raw == "false" { return false }
        if raw.hasPrefix("json:"), let data = raw.dropFirst(5).data(using: .utf8),
           let parsed = try? JSONSerialization.jsonObject(with: data, options: [.fragmentsAllowed]) { return parsed }
        if let int = Int(raw) { return int }
        if let double = Double(raw) { return double }
        return raw
    }

    /// socket 这一侧的最小客户端：连接、必要时经 LaunchServices 拉起本 app、收发一帧
    final class Connection {
        private let fd: Int32
        private var nextId = 1
        private init(fd: Int32) { self.fd = fd }
        deinit { close(fd) }

        static func open(path: String, launch: Bool) throws -> Connection {
            if let fd = connect(path) { return try handshake(Connection(fd: fd)) }
            guard launch else { throw Failure(message: "服务没在 \(path) 上运行（--no-launch 下不替你拉起）", status: 3) }
            let app = bundlePath()
            let open = Process()
            open.executableURL = URL(fileURLWithPath: "/usr/bin/open")
            open.arguments = ["-g", "--env", "XIAOYU_COMPUTER_USE_SOCKET=\(path)", app]
            do { try open.run() } catch { throw Failure(message: "拉不起服务：open \(app) 失败（\(error)）", status: 3) }
            open.waitUntilExit()
            guard open.terminationStatus == 0 else { throw Failure(message: "拉不起服务：open 退出码 \(open.terminationStatus)（\(app)）", status: 3) }
            let deadline = Date().addingTimeInterval(10)
            while Date() < deadline {
                Thread.sleep(forTimeInterval: 0.25)
                if let fd = connect(path) { return try handshake(Connection(fd: fd)) }
            }
            throw Failure(message: "服务已拉起但 10 秒内没在 \(path) 上监听。第一次运行要先在「系统设置 → 隐私与安全性」给它授权。", status: 3)
        }

        private static func handshake(_ c: Connection) throws -> Connection {
            let pong = try c.call("ping") as? [String: Any] ?? [:]
            let version = pong["apiVersion"] as? String ?? "?"
            guard version == apiVersion else {
                throw Failure(message: "服务协议版本 \(version) 与本客户端 \(apiVersion) 不一致：两边版本不同，请重装辅助应用", status: 3)
            }
            return c
        }

        /// 本可执行文件所在的 .app（经符号链接调用时也解析到真身）
        private static func bundlePath() -> String {
            let exe = (Bundle.main.executableURL ?? URL(fileURLWithPath: CommandLine.arguments[0])).resolvingSymlinksInPath()
            let app = exe.deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
            return app.pathExtension == "app" ? app.path : Bundle.main.bundlePath
        }

        private static func connect(_ path: String) -> Int32? {
            let fd = socket(AF_UNIX, SOCK_STREAM, 0)
            guard fd >= 0 else { return nil }
            var addr = sockaddr_un()
            addr.sun_family = sa_family_t(AF_UNIX)
            let bytes = Array(path.utf8)
            guard bytes.count < MemoryLayout.size(ofValue: addr.sun_path) else { close(fd); return nil }
            withUnsafeMutableBytes(of: &addr.sun_path) { raw in raw.copyBytes(from: bytes); raw[bytes.count] = 0 }
            let size = socklen_t(MemoryLayout<sockaddr_un>.size)
            let ok = withUnsafePointer(to: &addr) { $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { Darwin.connect(fd, $0, size) } } == 0
            if !ok { close(fd); return nil }
            return fd
        }

        func call(_ method: String, _ params: [String: Any] = [:]) throws -> Any {
            let id = nextId
            nextId += 1
            let body = try JSONSerialization.data(withJSONObject: ["jsonrpc": "2.0", "id": id, "method": method, "params": params])
            var size = UInt32(body.count).littleEndian
            var frame = Data(bytes: &size, count: 4)
            frame.append(body)
            var sent = 0
            while sent < frame.count {
                let w = frame.withUnsafeBytes { raw in write(fd, raw.baseAddress! + sent, frame.count - sent) }
                guard w > 0 else { throw Failure(message: "发送请求时连接断开", status: 3) }
                sent += w
            }
            guard let header = readExactly(4) else { throw Failure(message: "等应答时连接断开（服务可能崩了或被关掉）", status: 3) }
            let length = Int(header.withUnsafeBytes { $0.load(as: UInt32.self).littleEndian })
            guard length > 0, length <= maxFrameBytes, let reply = readExactly(length) else {
                throw Failure(message: "应答帧不合法（长度 \(length)）", status: 3)
            }
            guard let object = (try? JSONSerialization.jsonObject(with: reply)) as? [String: Any] else {
                throw Failure(message: "应答不是合法 JSON", status: 3)
            }
            if let error = object["error"] as? [String: Any] {
                throw ServiceError(code: (error["code"] as? Int) ?? -32603, message: (error["message"] as? String) ?? "未知错误")
            }
            return object["result"] ?? NSNull()
        }

        private func readExactly(_ n: Int) -> Data? {
            var data = Data(count: n)
            var got = 0
            while got < n {
                let r = data.withUnsafeMutableBytes { raw in read(fd, raw.baseAddress! + got, n - got) }
                if r <= 0 { return nil }
                got += r
            }
            return data
        }
    }
}

// MARK: - 入口

func defaultSocketPath() -> String {
    if let env = ProcessInfo.processInfo.environment["XIAOYU_COMPUTER_USE_SOCKET"], !env.isEmpty { return env }
    return NSHomeDirectory() + "/Library/Application Support/xiaoyu/computer-use/ipc.sock"
}

//  带参数 = 命令行客户端（见 CLI）；LaunchServices 起的服务进程没有参数
let arguments = Array(CommandLine.arguments.dropFirst()).filter { !$0.hasPrefix("-psn_") }
if !arguments.isEmpty { CLI.run(arguments) }

let app = NSApplication.shared
app.setActivationPolicy(.accessory)
//  客户端可能在我们回写之前就断开（超时、被用户打断）：默认的 SIGPIPE 会把整个服务杀掉
signal(SIGPIPE, SIG_IGN)
//  无障碍调用的默认超时约 6 秒，目标应用一卡每个属性都等满：全局压到 2 秒，配合遍历的时间预算
AXUIElementSetMessagingTimeout(AXUIElementCreateSystemWide(), 2)
let server = Server(path: defaultSocketPath())
//  被 pkill / Ctrl-C 结束时先把输入源、鼠标键、socket 文件收干净再退
let terminationSignals: [DispatchSourceSignal] = [SIGTERM, SIGINT, SIGHUP].map { sig in
    signal(sig, SIG_IGN)
    let source = DispatchSource.makeSignalSource(signal: sig, queue: .main)
    source.setEventHandler { server.shutdown(); exit(0) }
    source.resume()
    return source
}
do {
    try server.start()
    FileHandle.standardError.write(Data("xiaoyu computer use: listening on \(server.path)\n".utf8))
} catch {
    FileHandle.standardError.write(Data("xiaoyu computer use: \((error as? ServiceError)?.message ?? "\(error)")\n".utf8))
    exit(1)
}
//  启动时就把两个授权对话框叫出来：用户第一次装好就能一次性批完
_ = AXIsProcessTrustedWithOptions([kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary)
if !CGPreflightScreenCaptureAccess() { _ = CGRequestScreenCaptureAccess() }
//  空闲自动退出：没人连着又长时间没请求就退，小羽下次用时会重新拉起。
//  `defaults write app.xiaoyu.computer-use idleExitMinutes -int 0` 关掉。
let idleExitMinutes = (UserDefaults.standard.object(forKey: "idleExitMinutes") as? NSNumber)?.doubleValue ?? 30
if idleExitMinutes > 0 {
    Timer.scheduledTimer(withTimeInterval: 60, repeats: true) { _ in
        if server.idle(longerThan: idleExitMinutes * 60) { server.shutdown(); exit(0) }
    }
}
app.run()
