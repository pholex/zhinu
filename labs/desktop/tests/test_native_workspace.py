"""Offline native configuration checks; no website or account is opened."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[1] / 'macos' / 'App.swift'


@unittest.skipUnless(sys.platform == 'darwin', 'native macOS workspace')
class NativeWorkspaceTests(unittest.TestCase):
    def test_isolated_webview_configuration_and_navigation(self):
        prefix = SOURCE.read_text().split('private final class LocalLookupDelegate:', 1)[0]
        harness = prefix + '''
let app = NSApplication.shared
private let pane = BrowserPane(blockedOrigin: URL(string: "http://127.0.0.1:12345/workbench/")!)
precondition(!pane.view.configuration.websiteDataStore.isPersistent)
precondition(pane.view.configuration.userContentController.userScripts.isEmpty)
precondition(!pane.view.configuration.preferences.javaScriptCanOpenWindowsAutomatically)
precondition(!pane.allowed(URL(string: "http://127.0.0.1:12345/health")!))
precondition(!pane.allowed(URL(string: "file:///etc/passwd")!))
precondition(!pane.allowed(URL(string: "https://user:secret@example.test")!))
precondition(pane.allowed(URL(string: "https://example.test/reference")!))
precondition(pane.state()["model_access"] as? Bool == false)
print("isolated-browser-ok")
'''
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); swift = root/'main.swift'; swift.write_text(harness)
            built = subprocess.run(['xcrun','swiftc',str(swift),'-o',str(root/'check'),'-module-cache-path','/private/tmp/xiaoyu-swift-cache'], capture_output=True,text=True,errors='replace',timeout=40)
            self.assertEqual(built.returncode,0,built.stderr)
            run = subprocess.run([str(root/'check')],capture_output=True,text=True,errors='replace',timeout=15)
            self.assertEqual(run.returncode,0,run.stderr)
            self.assertIn('isolated-browser-ok',run.stdout)

    def test_multwindow_reuses_backend_and_exports_only_user_selected_path(self):
        source = SOURCE.read_text()
        method = source.split('private func openWorkbenchWindow',1)[1].split('\n    private func',1)[0]
        self.assertNotIn('Process()',method)
        self.assertNotIn('startBackend',method)
        self.assertIn('installWorkbench',method)
        export = source.split('if operation == "export_text"',1)[1].split('if operation == "new_window"',1)[0]
        self.assertIn('NSSavePanel()',export)
        self.assertIn('save.url',export)
        self.assertNotIn('body["path"]',export)

    def test_toolbar_navigation_state_and_actions(self):
        source = SOURCE.read_text()
        helper = source.split('private final class WorkbenchToolbar:', 1)[1].split('private final class LocalLookupDelegate:', 1)[0]
        harness = 'import AppKit\nprivate final class WorkbenchToolbar:' + helper + """
let app = NSApplication.shared
let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1000, height: 700),
    styleMask: [.titled, .closable], backing: .buffered, defer: false)
var actions: [String] = []
private let chrome = WorkbenchToolbar(window: window, onNavigate: { actions.append($0) },
                                      onToggle: { actions.append("sidebar") }, onTogglePanel: { actions.append("panel") })
let buttons = Dictionary(uniqueKeysWithValues: chrome.toolbar.items.compactMap { item -> (String, NSButton)? in
    guard let button = item.view as? NSButton else { return nil }
    return (item.itemIdentifier.rawValue, button)
})
let panel = buttons["workbench.panel"]!
precondition(panel.isEnabled && panel.image != nil)
precondition(chrome.toolbar.items.last?.itemIdentifier.rawValue == "workbench.panel")
precondition(panel.frame.size == buttons["workbench.sidebar"]!.frame.size)
let back = buttons["workbench.back"]!, forward = buttons["workbench.forward"]!, sidebar = buttons["workbench.sidebar"]!
precondition(!back.isEnabled && !forward.isEnabled && sidebar.isEnabled)
back.performClick(nil); forward.performClick(nil)
precondition(actions.isEmpty)
chrome.updateNavigation(canGoBack: true, canGoForward: false)
back.performClick(nil)
chrome.updateNavigation(canGoBack: false, canGoForward: true)
forward.performClick(nil); sidebar.performClick(nil); panel.performClick(nil)
precondition(actions == ["back", "forward", "sidebar", "panel"])
precondition(!back.isEnabled && forward.isEnabled && sidebar.isEnabled)
print("toolbar-navigation-ok")
"""
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); swift = root/'main.swift'; swift.write_text(harness)
            built = subprocess.run(['xcrun','swiftc',str(swift),'-o',str(root/'check'),'-module-cache-path','/private/tmp/xiaoyu-swift-cache'], capture_output=True,text=True,errors='replace',timeout=40)
            self.assertEqual(built.returncode,0,built.stderr)
            run = subprocess.run([str(root/'check')],capture_output=True,text=True,errors='replace',timeout=15)
            self.assertEqual(run.returncode,0,run.stderr)
            self.assertIn('toolbar-navigation-ok',run.stdout)
