
import AppKit
import WebKit
import Foundation
@MainActor final class LifecycleTest:NSObject,NSApplicationDelegate {
 let coordinator=DesktopApp()
 let root=URL(fileURLWithPath:ProcessInfo.processInfo.environment["WORKSPACE_TEST_ROOT"]!)
 func applicationDidFinishLaunching(_ note:Notification) {Task {@MainActor in await run()}}
 func expect(_ condition:Bool,_ message:String) {if !condition {print("FAIL",message);exit(1)}}
 func until(_ message:String,_ condition:@escaping @MainActor ()->Bool) async {
   for _ in 0..<200 {if condition(){return};try? await Task.sleep(nanoseconds:50_000_000)}
   print("TIMEOUT_STATE",coordinator.workspaces.map{[$0.displayName,String($0.windows.count),String($0.ownsBackend),String($0.lifecycleBusy)]})
   func labels(_ view:NSView)->[String] { (view as? NSTextField).map{[$0.stringValue]} ?? view.subviews.flatMap{labels($0)} }
   print("WINDOW_DIAGNOSTICS",NSApp.windows.map{[$0.title]+($0.contentView.map{labels($0)} ?? [])})
   expect(false,"timeout "+message)
 }
 func js(_ web:WKWebView,_ script:String) async -> Any? {
   await withCheckedContinuation {next in web.evaluateJavaScript(script){result,error in
     if let error=error {print("JS_ERROR",error.localizedDescription)};next.resume(returning:result)
   }}
 }
 func web(_ controller:WorkbenchController) async -> WKWebView {
   await until("web installed"){controller.windows.first?.contentView is WKWebView}
   let view=controller.windows.first!.contentView as! WKWebView
   for _ in 0..<200 {if await js(view,"window.fixtureReady === true") as? Bool == true{return view};try? await Task.sleep(nanoseconds:50_000_000)}
   expect(false,"web loaded");return view
 }
 func ready(_ view:WKWebView) async {
   for _ in 0..<200 {if await js(view,"window.fixtureReady === true") as? Bool == true{return};try? await Task.sleep(nanoseconds:50_000_000)}
   expect(false,"new view loaded")
 }
 func run() async {
   let defaults=UserDefaults.standard
   defaults.removePersistentDomain(forName:Bundle.main.bundleIdentifier!)
   defaults.set("existing",forKey:"xiaoyu.desktop.modelMode")
   let aURL=root.appendingPathComponent("workspace-a"),bURL=root.appendingPathComponent("workspace-b")
   try! FileManager.default.createDirectory(at:aURL,withIntermediateDirectories:true)
   try! FileManager.default.createDirectory(at:bURL,withIntermediateDirectories:true)
   let alias=root.appendingPathComponent("workspace-a-alias")
   try! FileManager.default.createSymbolicLink(at:alias,withDestinationURL:aURL)
   coordinator.open(aURL,from:nil);coordinator.open(bURL,from:nil)
   expect(coordinator.workspaces.count==2,"two controllers")
   let a=coordinator.workspaces.first{$0.mode==aURL.path}!,b=coordinator.workspaces.first{$0.mode==bURL.path}!
   let aw=await web(a),bw=await web(b)
   expect(a.ownsBackend && b.ownsBackend,"both own children")
   expect(aw.url!.port != bw.url!.port,"different backend ports")
   let bid=await js(bw,"window.__XIAOYU_DESKTOP__.view_id") as! String
   let aid=await js(aw,"window.__XIAOYU_DESKTOP__.view_id") as! String
   let originalPort=aw.url!.port!
   let originalToken=await js(aw,"window.__XIAOYU_DESKTOP__.token") as! String
   expect(aid != bid,"workspace views have independent IDs")
   coordinator.open(alias,from:nil)
   expect(coordinator.workspaces.count==2 && a.windows.count==1,"same inode directory deduplicates")
   print("PASS two-workspaces-and-directory-dedup")
   a.newWindow()
   await until("second view"){a.windows.count==2}
   let a2=a.windows.first{($0.contentView as? WKWebView) !== aw}!
   let a2w=a2.contentView as! WKWebView
   await ready(a2w)
   let a2id=await js(a2w,"window.__XIAOYU_DESKTOP__.view_id") as! String
   expect(a2id != aid && a2w.url!.port==aw.url!.port,"same backend independent views")
   _=await js(aw,"Object.assign(window.testState,{draft:'draft-a-one',session_id:'session_a',page:'sessions'})")
   _=await js(a2w,"Object.assign(window.testState,{draft:'draft-a-two',session_id:'session_second',page:'project'})")
   _=await js(bw,"Object.assign(window.testState,{draft:'draft-b',session_id:'session_b',page:'library'})")
   let originalWindow=a.windows.first{($0.contentView as? WKWebView) === aw}!
   a2.performClose(nil)
   expect(a.lifecycleBusy,"first view is still flushing before concurrent close")
   originalWindow.performClose(nil)
   await until("view closes"){a.windows.count==1 && a.hasClosedViews}
   expect(a.ownsBackend && b.ownsBackend,"closing a view keeps both services")
   expect(a.windows[0] === originalWindow,"concurrent close is serialized without an orphan backend")
   expect(a.restoration!.closedViews.last?.id==a2id,"closed view ID archived")
   expect(a.restoration!.closedViews.last?.page=="project","closed navigation saved")
   a.restoreClosedView()
   await until("view restored"){a.windows.count==2}
   let restored=a.windows.first{($0.contentView as? WKWebView) !== aw}!.contentView as! WKWebView
   await ready(restored)
   expect(await js(restored,"window.__XIAOYU_DESKTOP__.view_id") as? String==a2id,"restored original view ID")
   expect(await js(restored,"window.testState.draft") as? String=="draft-a-two","restored view draft")
   expect(await js(aw,"window.testState.draft") as? String=="draft-a-one","other view draft unchanged")
   print("PASS independent-views-close-restore-drafts")
   let closingWindows=a.windows
   var closedBeforeStop=0
   let closeObserver=NotificationCenter.default.addObserver(forName:NSWindow.willCloseNotification,object:nil,queue:.main){ note in
     if let target=note.object as? NSWindow, closingWindows.contains(target), a.ownsBackend { closedBeforeStop += 1 }
   }
   a.closeWorkspace()
   await until("A shuts down"){self.coordinator.workspaces.count==1 && !a.ownsBackend}
   NotificationCenter.default.removeObserver(closeObserver)
   expect(closedBeforeStop==closingWindows.count,"all windows close before the backend exits")
   expect(coordinator.workspaces[0] === b && b.ownsBackend && b.windows.count==1,"B survives closing A")
   expect(await js(bw,"window.testState.draft") as? String=="draft-b","B draft survives")
   let (_,response)=try! await URLSession.shared.data(from:bw.url!)
   expect((response as? HTTPURLResponse)?.statusCode==200,"B HTTP still live")
   expect(WorkspaceRegistry().archived(aURL.path)?.views.contains{$0.id==a2id}==true,"A view IDs archived after service close")
   print("PASS close-workspace-A-preserves-B")
   let archive=WorkspaceRegistry().archived(aURL.path)!
   let paused=WorkbenchController()
   paused.start(coordinator:coordinator,restored:archive,autoStart:true)
   _=await web(paused)
   expect(paused.ownsBackend && paused.windows.allSatisfy{$0.contentView is WKWebView},"restoration automatically connects all windows")
   expect(Set(paused.restoration!.views.map(\.id))==Set(archive.views.map(\.id)),"automatic restoration preserves view IDs")
   for window in paused.windows {await ready(window.contentView as! WKWebView)}
   paused.closeWorkspace()
   await until("automatically restored owner exits"){!paused.ownsBackend}
   expect(WorkspaceRegistry().recent.contains(aURL.path),"closed directory remains recent")
   coordinator.open(aURL,from:nil)
   let reopened=coordinator.workspaces.first{$0.mode==aURL.path}!
   _=await web(reopened)
   await until("both archived views restored"){reopened.windows.count==2}
   var recovered:WKWebView?
   for window in reopened.windows {
      let candidate=window.contentView as! WKWebView
      await ready(candidate)
      if await js(candidate,"window.__XIAOYU_DESKTOP__.view_id") as? String==a2id {recovered=candidate}
   }
   expect(recovered != nil,"opening recent directory restores closed workspace view IDs")
   let recoveredWeb=recovered!
   expect(await js(recoveredWeb,"window.testState.draft") as? String=="draft-a-two","reopened workspace recovers persisted draft")
   expect(await js(recoveredWeb,"window.testState.session_id") as? String=="session_second","reopened workspace recovers session")
   expect(await js(recoveredWeb,"window.testState.page") as? String=="project","reopened workspace recovers page")
   expect(recoveredWeb.url!.port! != originalPort,"reopen starts a fresh server port")
   expect(await js(recoveredWeb,"window.__XIAOYU_DESKTOP__.token") as? String != originalToken,"reopen issues fresh authorization token")
   reopened.closeWorkspace()
   await until("reopened A stops"){self.coordinator.workspaces.count==1 && !reopened.ownsBackend}
   print("PASS automatic-restoration-and-reopen-recent")
   b.closeWorkspace()
   await until("B shuts down"){self.coordinator.workspaces.isEmpty && !b.ownsBackend}
   func button(_ owner:WorkbenchController,_ name:String)->NSButton? {
     func find(_ view:NSView)->NSButton? {
       if let b=view as? NSButton, b.identifier?.rawValue==name || b.title==name {return b}
       return view.subviews.compactMap{find($0)}.first
     }
     return owner.windows.compactMap{$0.contentView.flatMap{find($0)}}.first
   }
   let resources=Bundle.main.resourceURL!,runtime=resources.appendingPathComponent("runtime.json")
   let originalRuntime=try! Data(contentsOf:runtime)
   for failFirst in [false,true] {
     let directory=root.appendingPathComponent(failFirst ? "setup-recovery" : "setup-initial")
     try! FileManager.default.createDirectory(at:directory,withIntermediateDirectories:true)
     defaults.set(directory.path,forKey:"xiaoyu.desktop.preferredWorkspace")
     defaults.removeObject(forKey:"xiaoyu.desktop.modelMode")
     coordinator.newWorkspace()
     let owner=coordinator.workspaces.last!
     expect(button(owner,"setup-continue") != nil,"real setup continue button")
     if failFirst {try! Data(#"{"python":"/missing/fixture-python"}"#.utf8).write(to:runtime)}
     button(owner,"setup-continue")!.performClick(nil)
     if failFirst {
       expect(owner.mode==directory.path && !owner.ownsBackend,"failed launch retains selected workspace")
       expect(button(owner,"模型配置…") != nil,"failure panel permits configuration")
       button(owner,"模型配置…")!.performClick(nil)
       expect(button(owner,"setup-continue") != nil,"returned to real setup")
       try! originalRuntime.write(to:runtime)
       let start=button(owner,"setup-continue")!
       start.performClick(nil);start.performClick(nil)
     }
     _=await web(owner)
     expect(coordinator.workspaces.count==1 && owner.ownsBackend,"start uses existing owner without duplicate window")
     owner.closeWorkspace()
     await until("setup owner exits"){self.coordinator.workspaces.isEmpty && !owner.ownsBackend}
   }
   let setupPreview=WorkbenchController()
   setupPreview.start(coordinator:coordinator,autoStart:true,showSetup:true)
   try? await Task.sleep(nanoseconds:500_000_000)
   expect(button(setupPreview,"setup-continue") != nil && !setupPreview.ownsBackend,"explicit setup remains visible with existing model configuration")
   setupPreview.closeAllWindows()
   print("PASS setup-continue-initial-and-failed-launch-recovery")
   defaults.removePersistentDomain(forName:Bundle.main.bundleIdentifier!)
   print("MULTIWORKSPACE_NATIVE_PASS 6")
   NSApp.terminate(nil)
 }
}
@main enum TestMain {
 static func main(){setbuf(stdout,nil);let app=NSApplication.shared;let delegate=LifecycleTest();app.delegate=delegate;app.setActivationPolicy(.regular);app.run();withExtendedLifetime(delegate){}}
}
