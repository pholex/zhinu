"""Run the native workspace registry against isolated defaults and real path aliases."""
import pathlib
import subprocess
import sys
import tempfile
import unittest
import uuid

LAB = pathlib.Path(__file__).resolve().parents[1]
HARNESS = r'''
import Foundation

func require(_ value: @autoclosure () -> Bool, _ message: String) {
    if !value() { fputs(message + "\n", stderr); exit(1) }
}
let scenario = CommandLine.arguments[1]
let suite = CommandLine.arguments[2]
let root = URL(fileURLWithPath: CommandLine.arguments[3], isDirectory: true)
let defaults = UserDefaults(suiteName: suite)!
defaults.removePersistentDomain(forName: suite)
defer { defaults.removePersistentDomain(forName: suite) }
let registry = WorkspaceRegistry(defaults: defaults)
let a = root.appendingPathComponent("项目 A", isDirectory: true)
let b = root.appendingPathComponent("项目 B", isDirectory: true)
let alias = root.appendingPathComponent("alias A", isDirectory: true)
try FileManager.default.createDirectory(at: a, withIntermediateDirectories: true)
try FileManager.default.createDirectory(at: b, withIntermediateDirectories: true)
try FileManager.default.createSymbolicLink(at: alias, withDestinationURL: a)
let active = WorkspaceViewRecord(id: UUID().uuidString, frame: nil, sessionID: "session_1", page: "workbench")
let closed = WorkspaceViewRecord(id: UUID().uuidString, frame: nil, sessionID: "session_2", page: "connections")

switch scenario {
case "default-workspace":
    let documents = root.appendingPathComponent("Documents")
    require(registry.chooserDirectory == registry.documentsDirectory, "first folder chooser starts in Documents")
    let created = try registry.prepareDefaultWorkspace(documents: documents)
    require(created.path == documents.appendingPathComponent("羽案/Default").path, "default project is scoped below Documents")
    require(FileManager.default.fileExists(atPath: created.path), "default folder is created without a picker")
    let note = created.appendingPathComponent("existing.txt")
    try "keep".write(to: note, atomically: true, encoding: .utf8)
    registry.remember(created.path)
    let again = try registry.prepareDefaultWorkspace(documents: documents)
    require(again == created, "opening default twice keeps its identity")
    let noteText = try String(contentsOf: note, encoding: .utf8)
    require(noteText == "keep", "existing work is preserved")
    require(registry.chooserDirectory == registry.documentsDirectory, "automatic default is not a manual override")
    registry.selectWorkspace(b.path)
    let custom = try registry.prepareDefaultWorkspace(documents: documents)
    require(custom == b, "manual directory overrides the default")
    require(registry.chooserDirectory.path == b.path, "folder picker remembers manual choice")
    let missing = root.appendingPathComponent("missing-manual")
    registry.selectWorkspace(missing.path)
    do { _ = try registry.prepareDefaultWorkspace(documents: documents); require(false, "missing manual directory must not be recreated") } catch {}
    require(!FileManager.default.fileExists(atPath: missing.path), "unavailable custom directory stays untouched")
case "default-file-collision":
    let documents = root.appendingPathComponent("Documents")
    try FileManager.default.createDirectory(at: documents.appendingPathComponent("羽案"), withIntermediateDirectories: true)
    let file = documents.appendingPathComponent("羽案/Default")
    try "keep".write(to: file, atomically: true, encoding: .utf8)
    do { _ = try registry.prepareDefaultWorkspace(documents: documents); require(false, "file collision must fail") } catch {}
    let fileText = try String(contentsOf: file, encoding: .utf8)
    require(fileText == "keep", "default creation never replaces files")
case "legacy-directory":
    registry.remember(a.path)
    let legacy = try registry.prepareDefaultWorkspace(documents: root.appendingPathComponent("Documents"))
    require(legacy == a, "existing user's last directory is retained")
case "canonical":
    require(WorkspaceRegistry.canonical(alias) == WorkspaceRegistry.canonical(a), "symbolic alias must canonicalize")
    require(WorkspaceRegistry.canonical(a.appendingPathComponent("../项目 A")) == WorkspaceRegistry.canonical(a), "dot path must canonicalize")
    require(WorkspaceRegistry.sameDirectory(a.path, alias.path), "same inode must compare equal")
    require(!WorkspaceRegistry.sameDirectory(a.path, b.path), "different directories must remain independent")
    registry.save([WorkspaceRecord(path: a.path, views: [active]),
                   WorkspaceRecord(path: alias.path, views: [closed]),
                   WorkspaceRecord(path: b.path, views: [closed])])
    require(registry.restored.count == 2, "duplicate directory must not reopen twice")
    require(registry.restored[0].views[0].id == active.id, "first valid view must survive")
    registry.remember(a.path); registry.remember(b.path); registry.remember(alias.path)
    require(registry.recent.count == 2 && registry.recent.last == b.path, "recent aliases must deduplicate")
case "archive":
    registry.archive(WorkspaceRecord(path: a.path, views: [active], closedViews: [closed]))
    require(registry.archived(alias.path) != nil, "archive must resolve a directory alias")
    let saved = registry.archived(alias.path)!
    require(saved.views.map(\.id) == [active.id], "active view identity must survive archive")
    require(saved.closedViews.map(\.id) == [closed.id], "closed view identity must survive archive")
    registry.archive(WorkspaceRecord(path: alias.path, views: [closed]))
    require(registry.archived(a.path)?.views.first?.id == closed.id, "archive must replace same workspace by identity")
    require(registry.restored.isEmpty, "archive must not implicitly open a workspace")
case "metadata":
    var view = active
    let secret = "credential-" + "fixture-never-persist"
    view.update(["session_id": "valid-session", "page": "workspace-files", "token": secret,
                 "origin": "http://127.0.0.1:12345", "api_key": secret])
    registry.save([WorkspaceRecord(path: a.path, views: [view])])
    let raw = String(data: defaults.data(forKey: "xiaoyu.desktop.workspaces")!, encoding: .utf8)!
    require(!raw.contains(secret) && !raw.contains("127.0.0.1") && !raw.contains("api_key"), "credentials must not enter persisted registry")
    require(registry.restored[0].views[0].page == "workspace-files", "valid page identifier must survive")
    view.sessionID = "../outside"; view.page = "<script>"; view.frame = String(repeating: "x", count: 301)
    registry.save([WorkspaceRecord(path: a.path, views: [view])])
    let clean = registry.restored[0].views[0]
    require(clean.sessionID == nil && clean.page == nil && clean.frame == nil, "invalid metadata must be removed")
case "closed-separation":
    var invalid = active; invalid.id = "invalid-uuid"
    registry.save([WorkspaceRecord(path: a.path, views: [invalid], closedViews: [closed])])
    let saved = registry.restored[0]
    require(saved.views.count == 1 && saved.views[0].id != closed.id, "invalid active view must not promote a closed view")
    require(saved.closedViews.map(\.id) == [closed.id], "closed view must remain recoverable but closed")
case "uuid-case":
    var duplicate = active; duplicate.id = active.id.lowercased()
    registry.save([WorkspaceRecord(path: a.path, views: [active, duplicate])])
    require(registry.restored[0].views.count == 1, "UUID case aliases must not share a draft namespace")
case "bounds":
    defaults.set(Data("not-json".utf8), forKey: "xiaoyu.desktop.workspaces")
    require(registry.restored.isEmpty, "corrupt state must not reopen workspaces")
    defaults.set(Data(repeating: 32, count: 1_048_577), forKey: "xiaoyu.desktop.workspaces")
    require(registry.restored.isEmpty, "oversized state must not be decoded")
    registry.save([WorkspaceRecord(path: "relative", views: [active]),
                   WorkspaceRecord(path: a.path, views: (0..<20).map { _ in WorkspaceViewRecord() },
                                   closedViews: (0..<110).map { _ in WorkspaceViewRecord() })])
    require(registry.restored.count == 1, "relative workspace path must be rejected")
    require(registry.restored[0].views.count == 16 && registry.restored[0].closedViews.count == 100,
            "restore must bound active and closed window counts")
default: fatalError("unknown scenario")
}
print("registry-ok:" + scenario)
'''


@unittest.skipUnless(sys.platform == "darwin", "native macOS registry")
class NativeWorkspaceRegistryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="xiaoyu-registry-")
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.root = pathlib.Path(cls.temporary.name)
        # Compile the actual value/store declarations without starting NSApplication.
        source = (LAB / "macos/DesktopCoordinator.swift").read_text(encoding="utf-8", errors="replace")
        registry = cls.root / "WorkspaceRegistry.swift"
        registry.write_text(source.split("final class DesktopApp:", 1)[0], encoding="utf-8")
        main = cls.root / "main.swift"
        main.write_text(HARNESS, encoding="utf-8")
        cls.binary = cls.root / "registry-tests"
        built = subprocess.run(["xcrun", "swiftc", "-swift-version", "5", str(registry), str(main),
                                "-o", str(cls.binary), "-module-cache-path", "/private/tmp/xiaoyu-swift-cache"],
                               capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
        if built.returncode:
            raise AssertionError(built.stderr)

    def run_case(self, case):
        suite = "test.xiaoyu.workspace." + uuid.uuid4().hex
        with tempfile.TemporaryDirectory(dir=self.root) as fixture:
            run = subprocess.run([str(self.binary), case, suite, str(pathlib.Path(fixture).resolve())],
                                 capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("registry-ok:" + case, run.stdout)

    def test_default_directory_creates_project_and_respects_manual_choice(self):
        self.run_case("default-workspace")

    def test_default_directory_never_overwrites_existing_file(self):
        self.run_case("default-file-collision")

    def test_existing_users_keep_their_previous_directory(self):
        self.run_case("legacy-directory")

    def test_canonical_paths_and_recent_workspaces_deduplicate(self):
        self.run_case("canonical")

    def test_archive_keeps_view_ids_without_reopening_workspace(self):
        self.run_case("archive")

    def test_only_valid_navigation_metadata_is_persisted(self):
        self.run_case("metadata")

    def test_invalid_active_view_does_not_reopen_closed_view(self):
        self.run_case("closed-separation")

    def test_case_aliases_cannot_share_view_draft_namespace(self):
        self.run_case("uuid-case")

    def test_invalid_and_oversized_restore_data_is_bounded(self):
        self.run_case("bounds")
