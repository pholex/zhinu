import Foundation

// Finder launches do not run shell startup scripts. Keep Python isolated while
// making explicitly installed user tools available to the backend and children.
func desktopExecutablePath(python: String, inherited: String, home: String) -> String {
    let candidates = [URL(fileURLWithPath: python).deletingLastPathComponent().path]
        + ["/usr/bin", "/bin", "/usr/sbin", "/sbin"]
        + inherited.components(separatedBy: ":")
        + ["/opt/homebrew/bin", "/usr/local/bin", "\(home)/.local/bin",
           "\(home)/.volta/bin", "\(home)/.asdf/shims", "\(home)/.local/share/mise/shims"]
    var seen = Set<String>()
    return candidates.filter { $0.hasPrefix("/") && !$0.contains("\n") && seen.insert($0).inserted }
        .joined(separator: ":")
}
