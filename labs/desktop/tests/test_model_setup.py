"""Native setup uses fake credentials and isolated preferences; never the user keychain."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid

from labs.desktop.macos.build import model_setup_providers
from xiaoyu.providers import PRESETS

LAB = Path(__file__).resolve().parents[1]

HARNESS = r'''
import AppKit
import Foundation

final class FakeCredentials: ModelSetupCredentialStore {
    var value: String?
    var stored: [String:String] = [:]
    var available = false
    var authorization = false
    var denyRead = false
    var denySave = false
    var reads: [(String,Bool)] = []
    var writes: [(String,String)] = []
    func presence(service:String,account:String) -> ModelCredentialPresence {
        if authorization { return .authorizationRequired }
        return stored[service+"#"+account] != nil || available ? .available : .missing
    }
    func read(service:String,account:String,allowInteraction:Bool) throws -> String? {
        reads.append((account,allowInteraction))
        if denyRead { throw NSError(domain:NSOSStatusErrorDomain,code:-25293,userInfo:nil) }
        return stored[service+"#"+account] ?? value
    }
    func save(_ value:String,service:String,account:String) throws {
        if denySave { throw NSError(domain:NSOSStatusErrorDomain,code:-25293,userInfo:nil) }
        stored[service+"#"+account]=value;writes.append((service,value))
    }
}
func find(_ id:String,in view:NSView) -> NSView? {
    if view.identifier?.rawValue == id { return view }
    return view.subviews.lazy.compactMap{find(id,in:$0)}.first
}
func descendants(_ view:NSView) -> [NSView] { [view]+view.subviews.flatMap(descendants) }
func spin() { RunLoop.current.run(until:Date(timeIntervalSinceNow:0.15)) }
func validateRows(_ view:NSView,_ providers:[ModelSetupProvider],dark:Bool=false) {
    let scroll=find("provider-scroll",in:view) as! NSScrollView
    let document=scroll.documentView!
    for provider in providers {
        let row=find("provider.\(provider.id)",in:view)!
        let rect=row.convert(row.bounds,to:document)
        precondition(rect.minX >= -1 && rect.maxX <= document.bounds.width+1,"provider card stays in document")
        precondition(rect.width >= document.bounds.width-4,"provider card fills document")
        for suffix in ["toggle","secure-key","save"] {
            let control=find("provider.\(provider.id).\(suffix)",in:view)!
            if control.isHiddenOrHasHiddenAncestor {continue}
            let bounds=control.convert(control.bounds,to:row)
            precondition(bounds.minX >= -1 && bounds.maxX <= row.bounds.width+1,"provider controls stay in card")
        }
        if dark {
            let color=NSColor(cgColor:row.layer!.backgroundColor!)!.usingColorSpace(.deviceRGB)!
            precondition(max(color.redComponent,color.greenComponent,color.blueComponent)<0.5,"card background follows dark appearance")
        }
    }
}
let action=CommandLine.arguments[1]
let providers=try JSONDecoder().decode([ModelSetupProvider].self,from:Data(contentsOf:URL(fileURLWithPath:CommandLine.arguments[2])))
let suite=CommandLine.arguments[3]
let defaults=UserDefaults(suiteName:suite)!
defaults.removePersistentDomain(forName:suite)
defer { defaults.removePersistentDomain(forName:suite) }
let store=FakeCredentials()
let first=providers[0],second=providers[1]
let primary=first.keyEnvs[0],otherKey=second.keyEnvs[0]
func make(_ environment:[String:String]=[:]) -> ModelSetupController {
    ModelSetupController(providers:providers,environment:["XDG_CONFIG_HOME":NSTemporaryDirectory()+suite].merging(environment){_,new in new},defaults:defaults,credentialStore:store)
}
func absent(_ state:ModelCredentialPresence) -> Bool { if case .missing=state {return true};return false }
func available(_ state:ModelCredentialPresence) -> Bool { if case .available=state {return true};return false }
func noSecrets(_ secrets:[String]) {
    let data=try! JSONSerialization.data(withJSONObject:defaults.persistentDomain(forName:suite) ?? [:],options:[.sortedKeys])
    let text=String(data:data,encoding:.utf8)!
    precondition(secrets.allSatisfy{!text.contains($0)},"credentials must never enter preferences")
}
switch action {
case "missing":
    let controller=make()
    precondition(controller.selectedProviderIDs.isEmpty)
    precondition(providers.allSatisfy{absent(controller.presence(for:$0.id)) && !controller.isEnabled($0.id)})
    precondition(store.reads.isEmpty && store.writes.isEmpty,"initial detection never authorizes keychain")
case "environment":
    store.stored[primary+"#"+NSUserName()]="fake-store-precedence"
    let controller=make([primary:"fake-environment-precedence"])
    precondition(controller.isEnabled(first.id),"readable environment credentials enable by default")
    let output=try controller.launchEnvironment()
    precondition(output[primary]=="fake-environment-precedence")
    precondition(output["XIAOYU_PROVIDERS"]==first.id)
    precondition(store.reads.isEmpty,"environment wins without keychain authorization")
    let restored=make([primary:"fake-environment-precedence"])
    precondition(restored.isEnabled(first.id),"previous explicit environment selection restores")
    precondition(store.reads.isEmpty)
    noSecrets(["fake-environment-precedence","fake-store-precedence"])
case "auto-keychain":
    store.stored[primary+"#"+NSUserName()]="fake-auto-credential"
    let controller=make()
    precondition(controller.isEnabled(first.id))
    precondition(controller.selectedProviderIDs == [first.id])
    precondition(store.reads.count==1 && !store.reads[0].1,"automatic detection does not prompt")
    let output=try controller.launchEnvironment()
    precondition(output[primary]=="fake-auto-credential")
    controller.disableProvider(first.id)
    let restarted=make()
    precondition(!restarted.isEnabled(first.id),"manual disable survives restart")
    noSecrets(["fake-auto-credential"])
case "automatic-authorization":
    store.stored[primary+"#"+NSUserName()]="fake-auto-protected"
    store.denyRead=true
    defaults.set([second.id],forKey:ModelSetupController.selectedProvidersKey)
    defaults.set(true,forKey:ModelSetupController.explicitSelectionKey)
    let controller=make()
    precondition(!controller.isEnabled(first.id))
    store.denyRead=false
    controller.enableDetectedKeychainProviders()
    precondition(controller.isEnabled(first.id),"previously unreadable entries enable after system authorization")
    precondition(store.reads.last!.1)
    let count=store.reads.count
    controller.enableDetectedKeychainProviders()
    precondition(store.reads.count==count,"automatic authorization runs once per controller")
    controller.disableProvider(first.id)
    let restarted=make()
    restarted.enableDetectedKeychainProviders()
    precondition(!restarted.isEnabled(first.id),"manual disable is remembered")
    noSecrets(["fake-auto-protected"])
case "automatic-visible":
    _ = NSApplication.shared
    store.stored[primary+"#"+NSUserName()]="fake-visible-keychain"
    store.denyRead=true
    let controller=make()
    let view=controller.makeView(logo:nil,onContinue:{})
    store.denyRead=false
    let window=NSWindow(contentRect:NSRect(x:0,y:0,width:820,height:600),styleMask:[.titled,.closable],backing:.buffered,defer:false)
    window.contentView=view;window.makeKeyAndOrderFront(nil);spin()
    precondition(controller.isEnabled(first.id))
    precondition((find("provider.\(first.id).toggle",in:view) as! NSButton).state == .on)
    precondition(store.reads.last!.1)
    noSecrets(["fake-visible-keychain"])
    window.orderOut(nil)
case "automatic-start":
    _ = NSApplication.shared
    let controller=make([primary:"fake-default-start"])
    var continued=0
    let view=controller.makeView(logo:nil,autoStart:true,onContinue:{continued+=1})
    let window=NSWindow(contentRect:NSRect(x:0,y:0,width:820,height:600),styleMask:[.titled,.closable],backing:.buffered,defer:false)
    window.contentView=view;window.makeKeyAndOrderFront(nil);spin()
    precondition(continued==1,"existing credentials skip the first-start configuration step")
    precondition(defaults.bool(forKey:ModelSetupController.explicitSelectionKey))
    let empty=make()
    var emptyContinued=0
    window.contentView=empty.makeView(logo:nil,autoStart:true,onContinue:{emptyContinued+=1})
    spin();precondition(emptyContinued==0,"missing credentials keep setup visible")
    (find("setup-continue",in:window.contentView!) as! NSButton).performClick(nil)
    precondition(emptyContinued==1,"continue can defer to CLI file configuration")
    let inherited=make(["XIAOYU_MODEL":"fake-configured-model"])
    var inheritedContinued=0
    window.contentView=inherited.makeView(logo:nil,autoStart:true,onContinue:{inheritedContinued+=1})
    spin();precondition(inheritedContinued==1,"existing CLI configuration enters workbench by default")
    noSecrets(["fake-default-start"])
    window.orderOut(nil)
case "automatic-denial":
    store.stored[primary+"#"+NSUserName()]="fake-auto-denied"
    store.denyRead=true
    let controller=make()
    controller.enableDetectedKeychainProviders()
    precondition(!controller.isEnabled(first.id))
    let count=store.reads.count
    controller.enableDetectedKeychainProviders()
    precondition(store.reads.count==count,"denial must not trigger repeated dialogs")
    store.denyRead=false
    try controller.enableProvider(first.id)
    precondition(controller.isEnabled(first.id),"manual retry remains available")
    noSecrets(["fake-auto-denied"])
case "auto-unreadable":
    store.available=true;store.denyRead=true
    let controller=make()
    precondition(controller.selectedProviderIDs.isEmpty)
    precondition(!store.reads.isEmpty && store.reads.allSatisfy{!$0.1})
case "authorize":
    store.authorization=true;store.value="fake-authorized-credential"
    let controller=make()
    precondition(!controller.isEnabled(first.id) && store.reads.isEmpty)
    try controller.enableProvider(first.id)
    precondition(controller.isEnabled(first.id))
    precondition(store.reads.count==1 && store.reads[0].1)
    let output=try controller.launchEnvironment()
    precondition(output[primary]=="fake-authorized-credential")
    precondition(store.reads.count==1,"launch reuses authorized memory")
    controller.disableProvider(first.id)
    precondition(!controller.isEnabled(first.id))
    noSecrets(["fake-authorized-credential"])
case "denied":
    store.authorization=true;store.denyRead=true
    let controller=make()
    do { try controller.enableProvider(first.id);preconditionFailure("denial must fail") } catch {}
    precondition(!controller.isEnabled(first.id))
    precondition(!controller.selectedProviderIDs.contains(first.id))
    precondition(store.reads.count==1 && store.reads[0].1)
case "save-failure":
    store.denySave=true
    let controller=make()
    do {try controller.saveKey("fake-rejected-save",for:first.id);preconditionFailure("save denial must fail")} catch {}
    precondition(!controller.isEnabled(first.id))
    precondition(store.stored.isEmpty)
    noSecrets(["fake-rejected-save"])
case "invalid-key":
    let controller=make()
    for value in ["   ","fake-invalid\nvalue","fake-invalid\0value",String(repeating:"x",count:16385)] {
        do {try controller.saveKey(value,for:first.id);preconditionFailure("invalid key must fail")} catch {}
    }
    precondition(!controller.isEnabled(first.id) && store.writes.isEmpty)
case "save-launch":
    let controller=make([otherKey:"fake-disabled-environment","UNRELATED_SETTING":"preserve-me"])
    try controller.saveKey("  fake-saved-credential\n",for:first.id)
    controller.disableProvider(second.id)
    precondition(controller.isEnabled(first.id) && !controller.isEnabled(second.id))
    let output=try controller.launchEnvironment()
    precondition(output[primary]=="fake-saved-credential")
    precondition(output["XIAOYU_PROVIDERS"]==first.id)
    precondition(output["UNRELATED_SETTING"]==nil,"launch returns only the selected configuration overlay")
    precondition(output[otherKey]==nil,"disabled provider keys are not in the launch overlay")
    precondition(store.writes.count==1 && store.writes[0].1=="fake-saved-credential","clipboard whitespace is trimmed")
    noSecrets(["fake-saved-credential","fake-disabled-environment"])
case "nonfirst-default":
    for provider in providers.dropFirst() {
        defaults.removePersistentDomain(forName:suite)
        store.stored.removeAll()
        let controller=make()
        try controller.saveKey("fake-nonfirst-default",for:provider.id)
        let output=try controller.launchEnvironment()
        precondition(output["XIAOYU_PROVIDERS"]==provider.id)
        precondition(provider.models.contains(output["XIAOYU_MODEL"] ?? ""),"default model must route to selected provider")
    }
case "remembered":
    defaults.set([first.id],forKey:ModelSetupController.selectedProvidersKey)
    defaults.set(true,forKey:ModelSetupController.explicitSelectionKey)
    store.available=true;store.value="fake-remembered-key"
    let controller=make([otherKey:"fake-excluded-environment"])
    precondition(controller.isEnabled(first.id))
    precondition(!controller.isEnabled(second.id),"explicit exclusions survive detection")
    precondition(store.reads.count==1 && !store.reads[0].1,"remembered credentials restore without prompting")
    noSecrets(["fake-remembered-key","fake-excluded-environment"])
case "remembered-denied":
    defaults.set([first.id],forKey:ModelSetupController.selectedProvidersKey)
    defaults.set(true,forKey:ModelSetupController.explicitSelectionKey)
    store.available=true;store.denyRead=true
    let controller=make()
    precondition(!controller.isEnabled(first.id))
    precondition(store.reads.count==1 && !store.reads[0].1)
case "toggle-state":
    _ = NSApplication.shared
    let environmentController=make([primary:"fake-toggle-environment"])
    let environmentView=environmentController.makeView(logo:nil,onContinue:{})
    precondition((find("provider.\(first.id).toggle",in:environmentView) as! NSButton).isEnabled)
    precondition(!(find("provider.\(second.id).toggle",in:environmentView) as! NSButton).isEnabled)
    store.authorization=true
    let authorizationController=make()
    let authorizationView=authorizationController.makeView(logo:nil,onContinue:{})
    precondition((find("provider.\(first.id).toggle",in:authorizationView) as! NSButton).isEnabled)
    precondition(store.reads.isEmpty,"rendering enable controls never authorizes")
case "detected-interface":
    _ = NSApplication.shared
    store.available=true;store.denyRead=true
    let controller=make()
    let view=controller.makeView(logo:nil,onContinue:{})
    let field=find("provider.\(first.id).secure-key",in:view) as! NSSecureTextField
    let replace=find("provider.\(first.id).replace",in:view) as! NSButton
    precondition(field.isHiddenOrHasHiddenAncestor,"detected credentials do not ask for duplicate input")
    precondition(!replace.isHiddenOrHasHiddenAncestor)
    replace.performClick(nil)
    precondition(!field.isHiddenOrHasHiddenAncestor)
    field.stringValue="fake-replacement"
    replace.performClick(nil)
    precondition(field.isHiddenOrHasHiddenAncestor && field.stringValue.isEmpty)
    precondition(!controller.isEnabled(first.id),"cancelling replacement never enables unreadable key")
case "interface":
    let app=NSApplication.shared
    let controller=make([primary:"fake-ui-environment"])
    try controller.enableProvider(first.id)
    let logo=NSImage(contentsOfFile:CommandLine.arguments[4])!
    var continued=0
    let view=controller.makeView(logo:logo,onContinue:{continued+=1})
    let window=NSWindow(contentRect:NSRect(x:0,y:0,width:820,height:600),styleMask:[.titled,.closable],backing:.buffered,defer:false)
    window.contentView=view;window.makeKeyAndOrderFront(nil);view.layoutSubtreeIfNeeded();spin()
    let image=find("welcome-logo",in:view) as! NSImageView
    precondition(image.image === logo && !image.isHidden && image.bounds.width>0)
    for provider in providers {
        precondition(find("provider.\(provider.id).toggle",in:view) is NSButton)
        let secure=find("provider.\(provider.id).secure-key",in:view) as! NSSecureTextField
        if provider.id != first.id {precondition(!secure.isHiddenOrHasHiddenAncestor)}
        precondition(find("provider.\(provider.id).save",in:view) is NSButton)
    }
    let scroll=find("provider-scroll",in:view) as! NSScrollView
    precondition(scroll.hasVerticalScroller)
    let document=scroll.documentView!
    precondition(document.bounds.height>scroll.contentView.bounds.height,"all eight providers must scroll at 820 by 600")
    document.scroll(NSPoint(x:0,y:document.bounds.height));scroll.reflectScrolledClipView(scroll.contentView)
    precondition(scroll.contentView.bounds.origin.y>0,"scroll actually reaches lower rows")
    precondition(document.bounds.width<=scroll.contentView.bounds.width+1,"no horizontal overflow")
    validateRows(view,providers)
    let missing=providers[1]
    let field=find("provider.\(missing.id).secure-key",in:view)!
    let title=find("provider.\(missing.id).title",in:view)!
    let fieldRect=field.convert(field.bounds,to:view),titleRect=title.convert(title.bounds,to:view)
    precondition(abs(fieldRect.midY-titleRect.midY)<20,"key input shares company row")
    precondition(fieldRect.width>=140,"compact window preserves usable key input")
    window.appearance=NSAppearance(named:.darkAqua);view.layoutSubtreeIfNeeded();spin()
    validateRows(view,providers,dark:true)
    (find("setup-continue",in:view) as! NSButton).performClick(nil)
    precondition(find("setup-existing",in:view)==nil,"single default entry")
    precondition(continued==1,"onboarding action reaches host callback")
    precondition(store.reads.isEmpty && store.writes.isEmpty)
    window.orderOut(nil)
    _ = app
    noSecrets(["fake-ui-environment"])
default:preconditionFailure("unknown isolated setup scenario")
}
print("model-setup-ok:\(action)")
'''


class ModelSetupCatalogTests(unittest.TestCase):
    def test_eight_providers_follow_core_presets(self):
        rows = model_setup_providers()
        expected = {"deepseek", "moonshot", "qwen", "zhipu", "anthropic", "gemini", "openai", "xai"}
        self.assertEqual({row["id"] for row in rows}, expected)
        self.assertEqual(len(rows), 8)
        labels = {row["id"]: row["label"] for row in rows}
        self.assertEqual([labels[k] for k in ("deepseek", "moonshot", "qwen")], ["深度求索", "月之暗面", "千问"])
        for row in rows:
            self.assertEqual(row["keyEnvs"], list(PRESETS[row["id"]].key_envs))
            self.assertTrue(row["label"])
            self.assertEqual(row["models"], list(PRESETS[row["id"]].models))


@unittest.skipUnless(sys.platform == "darwin", "native macOS setup")
class NativeModelSetupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="xiaoyu-model-setup-")
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.root = Path(cls.temporary.name)
        cls.catalog = cls.root / "providers.json"
        cls.catalog.write_text(json.dumps(model_setup_providers()), encoding="utf-8")
        source = cls.root / "main.swift"
        source.write_text(HARNESS, encoding="utf-8")
        cls.binary = cls.root / "model-setup-tests"
        built = subprocess.run(
            ["xcrun", "swiftc", "-swift-version", "5", str(LAB / "macos/ModelSetup.swift"),
             str(source), "-o", str(cls.binary), "-module-cache-path", "/private/tmp/xiaoyu-swift-cache"],
            capture_output=True, text=True, errors="replace", timeout=60,
        )
        if built.returncode:
            raise AssertionError(built.stderr)

    def run_case(self, case):
        suite = "test.xiaoyu.modelsetup." + uuid.uuid4().hex
        run = subprocess.run(
            [str(self.binary), case, str(self.catalog), suite, str(LAB / "web/logo.png")],
            capture_output=True, text=True, errors="replace", timeout=20,
        )
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("model-setup-ok:" + case, run.stdout)

    def test_missing_keys_never_read_keychain(self):
        self.run_case("missing")

    def test_environment_has_priority_without_authorization(self):
        self.run_case("environment")

    def test_readable_keychain_auto_enables_and_disable_persists(self):
        self.run_case("auto-keychain")

    def test_detected_keychain_auto_requests_authorization(self):
        self.run_case("automatic-authorization")

    def test_visible_setup_automatically_enables_protected_key(self):
        self.run_case("automatic-visible")

    def test_initial_start_skips_setup_only_with_available_credentials(self):
        self.run_case("automatic-start")

    def test_automatic_denial_does_not_loop_and_allows_retry(self):
        self.run_case("automatic-denial")

    def test_unreadable_keychain_never_auto_enables(self):
        self.run_case("auto-unreadable")

    def test_enable_authorizes_once_and_launch_uses_memory(self):
        self.run_case("authorize")

    def test_system_denial_does_not_enable_provider(self):
        self.run_case("denied")

    def test_failed_save_does_not_enable_or_persist_secret(self):
        self.run_case("save-failure")

    def test_invalid_key_is_rejected_before_storage(self):
        self.run_case("invalid-key")

    def test_saved_selection_shapes_child_environment(self):
        self.run_case("save-launch")

    def test_only_nonfirst_provider_has_a_routable_default_model(self):
        self.run_case("nonfirst-default")

    def test_remembered_selection_restores_silently(self):
        self.run_case("remembered")

    def test_remembered_denial_stays_disabled_without_prompt(self):
        self.run_case("remembered-denied")

    def test_detected_key_hides_input_until_explicit_replacement(self):
        self.run_case("detected-interface")

    def test_enable_control_requires_detected_or_authorizable_key(self):
        self.run_case("toggle-state")

    def test_secure_fields_logo_callbacks_and_compact_scroll(self):
        self.run_case("interface")
