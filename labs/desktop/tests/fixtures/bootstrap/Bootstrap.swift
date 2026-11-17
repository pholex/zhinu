import AppKit
import WebKit

let input = FileHandle.standardInput.readDataToEndOfFile()
let config = try! JSONSerialization.jsonObject(with: input) as! [String: String]
final class Bootstrap: NSObject, NSApplicationDelegate, WKNavigationDelegate {
    var window: NSWindow!
    var web: WKWebView!
    var phase = 0
    var expected = ""
    func applicationDidFinishLaunching(_ notification: Notification) {
        let settings = WKWebViewConfiguration()
        settings.websiteDataStore = .nonPersistent()
        let native: [String: Any] = ["origin": config["origin"]!, "token": config["token"]!, "fixture": false,
            "titlebar_sidebar": true, "titlebar_panel": true, "view_id": UUID().uuidString]
        let json = String(decoding: try! JSONSerialization.data(withJSONObject: native), as: UTF8.self)
        settings.userContentController.addUserScript(WKUserScript(source: """
          if (location.origin === "\(config["origin"]!)" && location.pathname === '/workbench/') { window.__XIAOYU_DESKTOP__ = \(json); }
          window.__testErrors = [];
          addEventListener('error',e=>window.__testErrors.push(e.message || 'resource load error'),true);
          addEventListener('unhandledrejection',e=>window.__testErrors.push(String(e.reason)));
          """, injectionTime: .atDocumentStart, forMainFrameOnly: true))
        web = WKWebView(frame: NSRect(x: 0, y: 0, width: 1200, height: 850), configuration: settings)
        web.navigationDelegate = self
        window = NSWindow(contentRect: web.frame, styleMask: [.titled, .closable], backing: .buffered, defer: false)
        window.contentView = web; window.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps: true)
        web.load(URLRequest(url: URL(string: config["origin"]! + "/workbench/")!))
        DispatchQueue.main.asyncAfter(deadline: .now() + 40) {
            self.web.evaluateJavaScript("JSON.stringify({stage:window.__testStage,errors:window.__testErrors,title:document.title,dialog:document.getElementById('archive-delete-dialog')?.open,description:document.getElementById('archive-delete-description')?.textContent,disabled:document.querySelector('[data-workspace-action=delete-all-archived]')?.disabled,body:document.body.innerText.slice(0,1600)})") { value, error in
                FileHandle.standardError.write(Data("BOOTSTRAP_DIAGNOSTIC \(String(describing: value)) \(String(describing: error))\n".utf8))
            }
        }

    }
    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        NSApp.activate(ignoringOtherApps: true)
        window.makeKeyAndOrderFront(nil)
        window.makeFirstResponder(web)
        let script = #"""
        const $=id=>document.getElementById(id), sleep=()=>new Promise(r=>setTimeout(r,100));
        const check=(ok,message)=>{if(!ok)throw new Error(message)};
        async function until(fn,label){const deadline=Date.now()+15000;while(Date.now()<deadline){if(fn())return;await sleep();}throw new Error(label+' '+JSON.stringify(window.__testErrors));}
        await until(()=>$('connection-state').textContent.includes('已连接') && !$('composer-model').disabled,'automatic connection/model load');
        check($('connection-panel').hidden && $('connect-form').hidden,'native connection form must stay hidden');
        check($('send').querySelector('svg') && $('send').getAttribute('aria-label')==='发送','send icon and accessible name');
        check(document.querySelector('#new-session svg'),'navigation icons initialized');
        check(window.__testErrors.length===0,'page errors: '+JSON.stringify(window.__testErrors));
        if(phase===0){
          check($('home').getAttribute('aria-label')==='首页' && $('home').querySelector('svg'),'home uses house icon');
          for(const id of ['home','spaces','capabilities','automations'])check(!!$(id).dataset.tooltip,'navigation tooltip '+id);
          const originalDraft=$('message').value;
          $('capabilities').click();
          await until(()=>document.querySelector('[data-key="plugin-skill-desktop-fixture"]'),'plugin skill list');
          check($('title').textContent==='插件','plugin title');
          await until(()=>!document.querySelector('.plugin-hub [data-capability-action=refresh]').disabled,'plugin list finishes loading');
          const pluginHTML=document.querySelector('.plugin-hub').innerHTML;
          $('settings').click();
          await until(()=>!!document.querySelector('[data-settings-navigate="capabilities"]'),'settings plugin shortcut');
          check(!document.querySelector('[data-settings-section="integrations"]'),'settings has no duplicate plugin landing page');
          document.querySelector('[data-settings-section=appearance]').click();check(!document.querySelector('.nav-preferences') && !$('thread').textContent.includes('导航顺序'),'appearance omits navigation ordering controls');
          document.querySelector('[data-settings-navigate="capabilities"]').click();
          await until(()=>!!document.querySelector('.plugin-hub') && !document.querySelector('.plugin-hub').textContent.includes('正在读取插件清单'),'settings opens plugin manager directly');
          check($('capabilities').getAttribute('aria-pressed')==='true' && !document.querySelector('.settings-layout'),'both entries share navigation');
          check(document.querySelector('.plugin-hub').innerHTML===pluginHTML,'both entries show the same loaded plugin view');

          check(!document.querySelector('.plugin-hub [data-workflow]'),'workflows absent from plugin page');
          const group=id=>document.querySelector('[data-capability-action="group"][data-group="'+id+'"]');
          const query=value=>{const n=$('plugin-search');n.focus();n.value=value;n.dispatchEvent(new Event('input',{bubbles:true}));};
          check(group('aws').getAttribute('aria-expanded')==='false','AWS initially collapsed');
          check($(group('aws').getAttribute('aria-controls')).hidden,'collapsed rows hidden');
          group('aws').focus();group('aws').click();
          check(group('aws').getAttribute('aria-expanded')==='true' && document.activeElement===group('aws'),'family toggle keeps keyboard focus');
          const skillToggle=id=>$('skill-switch-skill-'+id);
          const awsToggle=()=>group('aws').closest('.skill-group-header').querySelector('.skill-switch');
          await until(()=>!skillToggle('aws-auth').disabled,'skill settings ready');
          check(skillToggle('aws-auth').getBoundingClientRect().width>skillToggle('aws-auth').getBoundingClientRect().height*1.5,'switch has compact pill proportions');
          skillToggle('aws-auth').focus();skillToggle('aws-auth').click();
          await until(()=>skillToggle('aws-auth').getAttribute('aria-checked')==='false' && !skillToggle('aws-auth').disabled,'individual skill disabled').catch(e=>{throw new Error(e.message+' '+$('thread').textContent+' disabled='+skillToggle('aws-auth').disabled)});
          check(awsToggle().getAttribute('aria-checked')==='mixed','group shows partial enabled state');
          check(document.querySelector('[data-key="plugin-skill-aws-auth"]').closest('.skill-item').classList.contains('skill-disabled'),'disabled skill stays visible');
          check(group('aws').getAttribute('aria-expanded')==='true','switch preserves expanded family');
          check(document.activeElement===skillToggle('aws-auth'),'switch keeps keyboard focus');
          awsToggle().click();
          await until(()=>awsToggle().getAttribute('aria-checked')==='true' && !awsToggle().disabled,'enable whole group');
          awsToggle().click();
          await until(()=>awsToggle().getAttribute('aria-checked')==='false' && !awsToggle().disabled,'disable whole group');
          query('aws');check(document.querySelectorAll('.skill-disabled').length===2,'disabled skills remain searchable');
          query('');awsToggle().click();
          await until(()=>awsToggle().getAttribute('aria-checked')==='true' && !awsToggle().disabled,'restore group');
          query('审批');check(group('lark').getAttribute('aria-expanded')==='true','Chinese search opens matching family');
          check($('thread').textContent.includes('找到 1 个技能') && !group('aws'),'search count and filtering');
          query('');check(group('aws').getAttribute('aria-expanded')==='true' && group('lark').getAttribute('aria-expanded')==='false','clear search restores expansion');
          group('aws').click();
          const search=$('plugin-search');search.focus();search.value='no-match';search.dispatchEvent(new Event('input',{bubbles:true}));
          check($('thread').textContent.includes('没有匹配的技能'),'skill search filters rows');
          check(document.activeElement===$('plugin-search'),'search focus survives render');
          $('plugin-view-discover').click();
          check($('plugin-search').value==='' && $('thread').textContent.includes('从本机添加技能'),'discover resets search and offers actual sources');
          $('plugin-type-connectors').click();
          check($('thread').textContent.includes('添加 MCP 连接器'),'MCP is a connector discovery option');
          $('plugin-view-installed').click();
          await until(()=>document.querySelector('[data-connection-action="new"]')?.disabled===false,'connector catalog loaded');
          check(document.querySelectorAll('#connection-usage-hint').length===1 && !document.querySelector('[data-key=connection-help]'),'single connector usage hint');
          document.querySelector('[data-connection-action="new"]').click();
          const field=(id,value)=>{const el=$('connection-'+id);el.value=value;el.dispatchEvent(new Event('input',{bubbles:true}));};
          check(!$('connection-session'),'adding does not require selecting a conversation');
          check(document.activeElement===$('connection-name'),'new connector focuses the name field');
          check(getComputedStyle(document.querySelector('.plugin-search')).display==='none','editor hides unrelated search');
          check(!document.querySelector('.connection-advanced').open,'advanced settings start collapsed');
          check($('connection-name').placeholder==='MCP 服务器名称','neutral MCP name placeholder');
          check($('connection-type-http').textContent==='流式 HTTP','correct HTTP transport name');
          field('name','资料服务（测试）');field('url','https://example.invalid/mcp');
          field('headers-0-0','X-Client');field('headers-0-1','desktop');
          $('connection-add-headers').click();field('headers-1-0','X-Keep');field('headers-1-1','keep value');
          document.querySelector('[data-connection-action="remove-pair"][data-connection-pair="headers"][data-connection-index="0"]').click();
          check($('connection-headers-0-0').value==='X-Keep' && $('connection-headers-0-1').value==='keep value','header delete preserves next row');
          field('env_headers-0-0','X-Tenant');field('env_headers-0-1','MCP_TENANT');field('bearer_token_env_var','MCP_BEARER_TOKEN');
          $('connection-type-stdio').click();field('command','npx');
          field('env-0-0','MCP_LOCAL');field('env-0-1','local value');field('inherit-0','MCP_PASSED');field('cwd','~/code');
          field('arg-0','-y');$('connection-add-argument').click();field('arg-1','a path with spaces');
          $('connection-add-argument').click();field('arg-2','--read-only');
          document.querySelector('[data-connection-action="remove-arg"][data-connection-index="0"]').click();
          check($('connection-arg-0').value==='a path with spaces' && $('connection-arg-1').value==='--read-only','deleting parameter retains following values');
          $('connection-type-http').click();check($('connection-url').value==='https://example.invalid/mcp','switching transport restores URL');
          document.querySelector('[data-connection-action="close"]').click();
          document.querySelector('[data-connection-action="new"]').click();
          check($('connection-url').value==='https://example.invalid/mcp','return preserves unfinished connector');
          $('plugin-type-skills').click();$('plugin-type-connectors').click();
          check($('connection-name').value==='资料服务（测试）','connector draft survives category switch');
          document.querySelector('[data-connection-form]').requestSubmit();
          await until(()=>!!document.querySelector('.connection-card'),'saved connector appears');
          check(document.querySelector('.connection-card .plugin-entry-state').textContent==='已配置','saved configuration is not reported as connected');
          check(!document.querySelector('.connection-card').open,'connector details collapsed by default');
          check(!document.querySelector('[data-connection-form]'),'saving closes the editor');
          const res=await fetch('/desktop/connections',{headers:{Authorization:'Bearer '+token}});
          const saved=(await res.json()).connections;check(saved.length===1,'one real connector config saved');
          check(saved[0].definition.headers['X-Keep']==='keep value' && saved[0].definition.env_headers['X-Tenant']==='MCP_TENANT' && saved[0].definition.bearer_token_env_var==='MCP_BEARER_TOKEN','HTTP fields saved to backend');
          check(!saved[0].definition.env && !saved[0].definition.cwd,'inactive STDIO fields are not submitted');
          $('plugin-view-discover').click();
          check([...document.querySelectorAll('[data-capability-action="configure-connector"]')].map(n=>n.dataset.connectorPreset).join(',')==='github,supabase,vercel,cloudflare,amap','connector order');
          const mapButton=()=>document.querySelector('[data-connector-preset="amap"]');
          check(mapButton() && mapButton().textContent==='配置','map discovery provides configuration entry');
          query('高德');check(mapButton() && !document.querySelector('[data-capability-action="add-connector"]'),'map discovery search');
          mapButton().click();
          check($('connection-name').value==='高德地图' && $('connection-env-0-1').type==='password','prefilled map form and masked key');
          check(!document.querySelector('[data-connection-manage]') && !document.querySelector('#connection-type-http'),'simple map setup has no premature connection controls');
          check(!document.querySelector('.connection-advanced').open,'technical parameters collapsed');
          check(document.querySelector('[data-connection-action="preset-guide"]').href==='https://lbs.amap.com/api/mcp-server/create-project-and-key','official key guide');
          check(!document.querySelector('[data-connection-form]').checkValidity(),'empty map key cannot submit');
          field('env-0-1','fixture-'+'map-key');
          document.querySelector('[data-connection-action="close"]').click();
          $('plugin-view-discover').click();mapButton().click();
          check($('connection-env-0-1').value==='fixture-'+'map-key','map draft retained');
          document.querySelector('[data-connection-form]').requestSubmit();
          await until(()=>document.querySelectorAll('.connection-card').length===2,'map configuration saved');
          const mapsResponse=await fetch('/desktop/connections',{headers:{Authorization:'Bearer '+token}});
          const mapConfig=(await mapsResponse.json()).connections.find(c=>c.definition.name==='高德地图');
          check(mapConfig.definition.command==='npx' && mapConfig.definition.args.join(' ')==='-y @amap/amap-maps-mcp-server','official map launch configuration');
          check(mapConfig.definition.env.AMAP_MAPS_API_KEY==='fixture-'+'map-key','key passed through environment configuration');
          check(!document.querySelector('.connection-page').textContent.includes('fixture-'+'map-key'),'saved list does not display key');
          document.querySelector('[data-connection-action="edit"][data-connection-id="'+mapConfig.id+'"]').click();
          check($('connection-env-0-1').type==='password','saved map edit stays masked');
          document.querySelector('[data-connection-action="discard"]').click();
          const providers=[['supabase','Supabase','https://mcp.supabase.com/mcp'],['cloudflare','Cloudflare','https://mcp.cloudflare.com/mcp'],['github','GitHub','https://api.githubcopilot.com/mcp/']];
          let expectedCount=2;
          for(const [id,title,url] of providers){
            $('plugin-view-discover').click();query(id.toUpperCase());
            document.querySelector('[data-connector-preset="'+id+'"]').click();
            check($('connection-name').value===title && $('connection-token').type==='password','cloud preset form '+id);
            check(!document.querySelector('[data-connection-form]').checkValidity(),'cloud token required '+id);
            field('token','fixture-'+id+'-token');
            document.querySelector('[data-connection-action="close"]').click();
            $('plugin-view-discover').click();document.querySelector('[data-connector-preset="'+id+'"]').click();
            check($('connection-token').value==='fixture-'+id+'-token','cloud token draft '+id);
            document.querySelector('[data-connection-form]').requestSubmit();
            expectedCount++;
            await until(()=>document.querySelectorAll('.connection-card').length===expectedCount,'cloud config saved '+id);
            const response=await fetch('/desktop/connections',{headers:{Authorization:'Bearer '+token}});
            const config=(await response.json()).connections.find(c=>c.definition.name===title);
            check(config.definition.url===url && config.definition.headers.Authorization==='Bearer fixture-'+id+'-token','cloud backend definition '+id);
            check(!document.querySelector('.connection-page').textContent.includes('fixture-'+id+'-token'),'token hidden from saved list '+id);
            document.querySelector('[data-connection-action="edit"][data-connection-id="'+config.id+'"]').click();
            check($('connection-token').type==='password','saved cloud editor masked '+id);
            document.querySelector('[data-connection-action="discard"]').click();
          }
          $('plugin-view-discover').click();query('vercel');
          document.querySelector('[data-connector-preset="vercel"]').click();
          check(document.querySelector('.connection-preset-editor').textContent.includes('客户端通过审核'),'Vercel review prerequisite');
          check(!$('connection-token'),'Vercel uses browser authorization');
          document.querySelector('[data-connection-form]').requestSubmit();
          await until(()=>document.querySelectorAll('.connection-card').length===6,'bridge configuration saved without launching');
          const bridgeResponse=await fetch('/desktop/connections',{headers:{Authorization:'Bearer '+token}});
          const bridge=(await bridgeResponse.json()).connections.find(c=>c.definition.name==='Vercel');
          check(bridge.definition.args[1]==='mcp-remote@0.15.1' && bridge.definition.args[2]==='https://mcp.vercel.com','pinned Vercel bridge');
          $('home').click();check($('message').value===originalDraft,'plugin browsing preserves composer draft');
        }
        const select=$('composer-model'), choices=[...select.options].map(o=>o.value);
        check(choices.length>1 && choices.every(Boolean),'explicit configured model list');
        check($('composer-project-label').textContent==='Default','default project');
        check(getComputedStyle($('toggle-panes')).display==='none','native panel control replaces page control');
        const panel=$('workspace-panes'), work=$('workspace');
        const splitGeometry=()=>{
          const pane=panel.getBoundingClientRect(),chat=work.getBoundingClientRect(),area=work.parentElement.getBoundingClientRect();
          check(!panel.hidden && getComputedStyle(work).display!=='none','conversation remains visible');
          check(chat.width>=area.width/2-1 && pane.width>250,'usable split widths');
          check(pane.left>=chat.right-1 && pane.right<=area.right+1,'right pane never overlays conversation');
          check(Math.abs(pane.top-chat.top)<2,'panes share top edge');
          const input=$('message').getBoundingClientRect(),send=$('send').getBoundingClientRect();
          check(input.width>100 && input.left>=chat.left && input.right<=chat.right+1,'input remains inside conversation');
          check(send.left>=chat.left && send.right<=chat.right+1,'send remains inside conversation');
        };
        window.dispatchEvent(new Event('xiaoyu-toggle-panel'));splitGeometry();
        panel.querySelector('[data-pane="expand"]').click();
        check(getComputedStyle(work).display==='none','explicit enlargement can fill work area');
        check(panel.querySelector('[data-pane="expand"]').title==='还原工作区','enlargement offers restore');
        panel.querySelector('[data-pane="expand"]').click();splitGeometry();
        panel.querySelector('[data-pane="expand"]').click();
        window.dispatchEvent(new Event('xiaoyu-toggle-panel'));
        window.dispatchEvent(new Event('xiaoyu-toggle-panel'));splitGeometry();
        panel.style.width='1000px';splitGeometry();
        panel.style.width='420px';
        const newTab=()=>panel.querySelector('[data-pane="new"]').click();
        const paneTabs=()=>[...panel.querySelectorAll('[role="tab"]')];
        newTab();
        check(paneTabs().length===1 && paneTabs()[0].getAttribute('aria-selected')==='true','new tab creates selected tab');
        const firstTab=paneTabs()[0].dataset.paneSelect;
        let address=panel.querySelector('[data-pane-new-browser] input');
        check(document.activeElement===address,'new tab focuses address');
        const addressForm=panel.querySelector('[data-pane-new-browser]');
        addressForm.requestSubmit();
        await until(()=>panel.querySelector('.pane-error').textContent.includes('请输入网址'),'empty address shows Chinese feedback');
        address.value='baidu.com';
        let bareDomainSubmitted=false;
        addressForm.addEventListener('submit',event=>{event.preventDefault();event.stopPropagation();bareDomainSubmitted=true;},{once:true});
        addressForm.requestSubmit();
        check(bareDomainSubmitted,'bare domain reaches submit handler without native URL validation bubble');
        address.value='https://example.test/reference';address.dispatchEvent(new Event('input',{bubbles:true}));
        newTab();check(paneTabs().length===2,'repeated new tab creates another tab');
        check(panel.querySelector('[data-pane-new-browser] input').value==='','new tab has independent address');
        paneTabs().find(t=>t.dataset.paneSelect===firstTab).click();
        check(panel.querySelector('[data-pane-new-browser] input').value==='https://example.test/reference','tab switching retains address draft');
        splitGeometry();
        while(paneTabs().length)paneTabs()[0].querySelector('[data-pane-close]').click();
        check(paneTabs().length===0 && panel.querySelector('[data-pane-new-browser]'),'blank tabs can close back to start page');
        window.dispatchEvent(new Event('xiaoyu-toggle-panel'));
        check(panel.hidden,'titlebar event closes right panel');

        if(phase===1){
          check($('message').value==='Draft only','reload restores unsent input without a session');
          check(window.__XIAOYU_VIEW_STATE__().session_id==='','cached input has no session');
          $('message').value='';$('message').dispatchEvent(new Event('input',{bubbles:true}));
          check(select.value===expected,'saved model after reload');
          const headers={Authorization:'Bearer '+token};
          for(const [mode,selector] of [['weave','.weave-compose'],['contest','.contest-compose'],['fleet','.fleet-command-tabs']]) {
            $('new-session').click();
            document.querySelector('#workflow-picker summary').click();
            document.querySelector('#workflow-picker [data-model-value="'+mode+'"]').click();
            await until(()=>!!document.querySelector(selector),'collaboration form opens: '+mode);
            check(!document.querySelector('#notice')?.textContent.includes('保存失败'),'receipt persisted: '+mode);
            check($('thread').dataset.workflow===mode,'dedicated page: '+mode);
            check($('composer-area').hidden,'conversation composer hidden: '+mode);
            check(!$('return-chat').hidden,'return to conversation available');
            for(const [other,form] of [['weave','.weave-compose'],['contest','.contest-compose'],['fleet','.fleet-command-section']]){
              if(other!==mode)check(!document.querySelector(form),'no unrelated form: '+other);
            }
            check(!$('thread').textContent.includes('准备好了'),'no conversation empty state');
            check(!$('thread').textContent.includes('历轮成果与结算'),'no generic settlement');
            check(!$('thread').textContent.includes('会话与执行详情'),'no technical dump');
            check($('thread-scroll').scrollTop<10,'task page starts at top');
            const field=mode==='weave'?$('weave-template'):mode==='contest'?$('contest-task'):null;
            if(field){field.value='保留 '+mode+' 的任务草稿';field.dispatchEvent(new Event('input',{bubbles:true}));}
            $('return-chat').click();
            check(!$('composer-area').hidden && !$('thread').dataset.workflow,'returns to conversation');
            check(!document.querySelector('.weave-compose,.contest-compose,.fleet-command-section'),'conversation has no task forms');
            $('new-'+mode).click();
            await until(()=>!!document.querySelector(selector),'reopen mode page: '+mode);
            if(field)check($(field.id).value==='保留 '+mode+' 的任务草稿','mode draft retained: '+mode);
            window.dispatchEvent(new Event('xiaoyu-navigate-back'));
            check(!$('composer-area').hidden,'back returns to conversation');
            window.dispatchEvent(new Event('xiaoyu-navigate-forward'));
            check($('thread').dataset.workflow===mode && $('composer-area').hidden,'forward restores mode page');
            if(field){
              $('new-session').click();
              document.querySelector('#workflow-picker summary').click();
              document.querySelector('#workflow-picker [data-model-value="'+mode+'"]').click();
              await until(()=>!!document.querySelector(selector),'fresh preparation page opens');
              check($(field.id).value==='保留 '+mode+' 的任务草稿','mode input survives replacement of unused session');
            }



          }
          let completed=false;
          for(let i=0;i<50;i++){
            const saved=await (await fetch('/desktop/state',{headers})).json();
            if(Object.keys(saved.outbox).length===0){completed=true;break;}
            await sleep();
          }
          check(completed,'creation receipts completed');
          $('return-chat').click();
          $('message').value='';$('message').dispatchEvent(new Event('input',{bubbles:true}));
          const originalFetch=window.fetch,sid=(document.querySelector('#session-menu-items [data-workspace-session]')?.dataset.workspaceSession||'');
          check(!document.querySelector('#sessions [data-key="section-recent"] [data-session="'+sid+'"]'),'never-started session is absent from recent');
          check(!document.querySelector('#sessions [data-session="'+sid+'"]'),'empty conversation absent from every sidebar section');
          $('message').value='保留未发送的草稿';$('message').dispatchEvent(new Event('input',{bubbles:true}));
          $('new-session').click();
          check($('message').value==='保留未发送的草稿','new conversation restores folder input cache');
          check(window.__XIAOYU_VIEW_STATE__().session_id==='','draft has no saved conversation identity');
          const beforeCount=(await (await fetch('/session',{headers})).json()).sessions.length;
          const folderA=$('workspace-path').value,folderB=folderA+'/另一个目录';
          $('workspace-path').value=folderB;$('create-form').requestSubmit();
          check($('message').value==='','different folder starts with its own empty input');
          $('message').value='B 的输入';$('message').dispatchEvent(new Event('input',{bubbles:true}));
          $('workspace-path').value=folderA;$('create-form').requestSubmit();
          check($('message').value==='保留未发送的草稿','return to folder A restores A input');
          check((await (await fetch('/session',{headers})).json()).sessions.length===beforeCount,'typing and changing folders never create sessions');
          $('message').value='';$('message').dispatchEvent(new Event('input',{bubbles:true}));
          // Supply a completed conversation for sidebar actions without calling a model.
          window.fetch=async(...args)=>{
            const response=await originalFetch(...args);
            const path=new URL(typeof args[0]==='string'?args[0]:args[0].url,location.href).pathname;
            if(path!=='/session')return response;
            const data=await response.clone().json();
            for(const item of data.sessions||[])if(item.session_id===sid)item.turns=1;
            return new Response(JSON.stringify(data),{status:response.status,headers:response.headers});
          };
          $('refresh').click();
          await until(()=>!!document.querySelector('#sessions .project-group [data-session="'+sid+'"]'),'started conversation enters project');
          const projectGroup=document.querySelector('#sessions .project-group');projectGroup.open=true;
          check(!projectGroup.querySelector('.project-overview-link'),'project details no longer occupies a conversation row');
          document.querySelector('#sessions .project-group [data-session="'+sid+'"]').click();
          const sidebarRow=()=>document.querySelector('#sessions .project-group [data-session="'+sid+'"]').closest('.session-row');
          check(!sidebarRow().querySelector('.session-bottom'),'sidebar has no repeated second line');
          check(sidebarRow().getBoundingClientRect().height<=38,'sidebar row remains compact');
          sidebarRow().querySelector('.session').focus();await sleep();
          const keyboardFocusChecked=document.hasFocus();
          if(keyboardFocusChecked)check(getComputedStyle(sidebarRow().querySelector('.session-row-actions')).visibility==='visible','keyboard focus reveals actions');
          sidebarRow().querySelector('[data-workspace-action="pin"]').click();
          await until(()=>!!document.querySelector('#sessions [data-key="section-pinned"] [data-session="'+sid+'"]'),'pin adds conversation to pinned group');
          check((document.querySelector('#session-menu-items [data-workspace-session]')?.dataset.workspaceSession||'')===sid,'pin does not navigate conversation');
          document.querySelector('#sessions [data-key="section-pinned"] [data-workspace-session="'+sid+'"][data-workspace-action="pin"]').click();
          await until(()=>!document.querySelector('#sessions [data-key="section-pinned"] [data-session="'+sid+'"]') && !!document.querySelector('#sessions .project-group [data-session="'+sid+'"]'),'unpin keeps draft available in project');
          sidebarRow().querySelector('[data-workspace-action="archive"]').click();
          await until(()=>!document.querySelector('#sessions [data-session="'+sid+'"]'),'archive removes item from active sidebar');
          check(!$('connections'),'more menu has no duplicate connector entry');
          check($('cli-sessions').textContent.trim()==='Xiaoyu CLI 会话','CLI entry has an explicit name');
          $('sidebar-more').querySelector('summary').click();
          window.__testStage='archive open';$('archive-sessions').click();
          await until(()=>!!document.querySelector('.archive-row [data-workspace-session="'+sid+'"]'),'archive has its own page');
          check($('title').textContent==='已归档' && $('composer-area').hidden,'archive has title and hides composer');
          check(!$('sidebar-more').open && $('archive-sessions').getAttribute('aria-pressed')==='true','archive navigation closes menu and marks active page');
          await until(()=>!document.querySelector('.archive-row [data-workspace-action="delete-archived"]').disabled,'archive deletion ready');
          window.__testStage='archive single cancel';document.querySelector('.archive-row [data-workspace-action="delete-archived"]').click();
          await until(()=>$('archive-delete-dialog').open,'delete opens a confirmation dialog');
          window.__testStage='single dialog open';check($('archive-delete-description').textContent.includes('将删除'),'single delete names the selected conversation');
          $('archive-delete-dialog').querySelector('[value="cancel"]').click();await sleep();
          window.__testStage='single cancelled';check(!!document.querySelector('.archive-row')&&!$('archive-delete-dialog').open,'cancel keeps archived conversation');
          document.querySelector('[data-workspace-action="delete-all-archived"]').click();
          window.__testStage='bulk cancel waiting';await until(()=>$('archive-delete-dialog').open,'delete all opens confirmation');
          check($('archive-delete-description').textContent.includes('全部 1 个'),'delete all confirms count');
          $('archive-delete-dialog').close('cancel');await sleep();

          check(window.__XIAOYU_VIEW_STATE__().page==='archived','native view persists archive route');
          check(!document.querySelector('#sessions [data-session="'+sid+'"]'),'archive page leaves active sidebar unchanged');
          $('archive-search').value='no-such-archived-conversation';$('archive-search').dispatchEvent(new Event('input',{bubbles:true}));
          check(!document.querySelector('.archive-row') && $('thread').textContent.includes('没有找到匹配的对话'),'archive search filters results');
          $('archive-search').value='';$('archive-search').dispatchEvent(new Event('input',{bubbles:true}));
          document.querySelector('.archive-open').click();
          check(window.__XIAOYU_VIEW_STATE__().session_id===sid && !$('composer-area').hidden,'archive opens original conversation');
          window.dispatchEvent(new Event('xiaoyu-navigate-back'));
          await until(()=>!!document.querySelector('.archive-row'),'back returns to archive');
          await until(()=>!document.querySelector('.archive-row [data-workspace-action="archive"]').disabled,'restore ready');
          document.querySelector('.archive-row [data-workspace-action="archive"]').click();
          await until(()=>!document.querySelector('.archive-row') && $('thread').textContent.includes('暂无归档对话'),'restored item leaves archive with empty state');
          check($('title').textContent==='已归档','restore stays on archive page');
          // Real deletion uses only throwaway sessions in this isolated fixture.
          window.__testStage='archive actual delete';const disposable=[];
          const archiveState=(await (await fetch('/desktop/workspace',{headers})).json()).state;
          const archiveProjectId=crypto.randomUUID().replaceAll('-','').slice(0,24);
          const archiveProjectReply=await fetch('/desktop/workspace',{method:'POST',headers:{...headers,'Content-Type':'application/json'},body:JSON.stringify({revision:archiveState.revision,group:'projects',key:archiveProjectId,value:{context:'',name:'归档验收',folders:[],primary:''}})});
          check(archiveProjectReply.ok,'archive fixture project created');
          const archiveProjects=(await archiveProjectReply.json()).catalogue.projects;
          check(archiveProjects.length>=2,'archive grouping needs two projects');
          for(let i=0;i<3;i++){
            const made=await (await fetch('/session',{method:'POST',headers:{...headers,'Content-Type':'application/json'},body:JSON.stringify({project_id:archiveProjects[i===2?1:0].project_id})})).json();
            check(!!made.session_id,'disposable conversation created');disposable.push(made.session_id);
            const state=(await (await fetch('/desktop/workspace',{headers})).json()).state;
            const archived=await fetch('/desktop/workspace',{method:'POST',headers:{...headers,'Content-Type':'application/json'},body:JSON.stringify({revision:state.revision,group:'sessions',key:made.session_id,value:{pinned:false,archived:true}})});
            check(archived.ok,'disposable archived');
          }
          $('refresh').click();
          await until(()=>document.querySelectorAll('.archive-row').length===3 && !document.querySelector('[data-workspace-action="delete-all-archived"]').disabled,'three disposable archives ready');
          check(document.querySelectorAll('.archive-group').length===2,'archives grouped by project');
          $('archive-project').value=archiveProjects[1].project_id;$('archive-project').dispatchEvent(new Event('change',{bubbles:true}));
          check(document.querySelectorAll('.archive-row').length===1,'project filter restricts rows');
          check(document.querySelector('.archive-restore').textContent==='取消归档','restore uses explicit wording');
          check(document.querySelector('.archive-delete').title==='删除对话' && !!document.querySelector('.archive-delete svg'),'delete icon has hover description');
          $('archive-search').value='no-match';$('archive-search').dispatchEvent(new Event('input',{bubbles:true}));
          check(!document.querySelector('.archive-row') && $('archive-project').value===archiveProjects[1].project_id,'search combines with selected project');
          $('archive-search').value='';$('archive-search').dispatchEvent(new Event('input',{bubbles:true}));
          document.querySelector('[data-workspace-action="delete-all-archived"]').click();
          await until(()=>$('archive-delete-dialog').open,'filtered global delete confirmation');
          check($('archive-delete-description').textContent.includes('全部 3 个'),'global delete explicitly includes filtered-out rows');
          $('archive-delete-dialog').close('cancel');await sleep();
          $('archive-project').value='';$('archive-project').dispatchEvent(new Event('change',{bubbles:true}));
          check(document.querySelectorAll('.archive-row').length===3,'clearing filter restores all groups');
          document.querySelector('.archive-row [data-workspace-action="delete-archived"]').click();
          await until(()=>$('archive-delete-dialog').open,'single delete confirmation');
          $('archive-delete-dialog').querySelector('[value="delete"]').click();
          await until(()=>document.querySelectorAll('.archive-row').length===2,'single delete updates archive');
          await until(()=>!document.querySelector('[data-workspace-action="delete-all-archived"]').disabled,'bulk deletion ready');
          document.querySelector('[data-workspace-action="delete-all-archived"]').click();
          await until(()=>$('archive-delete-dialog').open,'bulk delete confirmation');
          check($('archive-delete-description').textContent.includes('全部 2 个'),'bulk count follows single deletion');
          $('archive-delete-dialog').querySelector('[value="delete"]').click();
          await until(()=>!document.querySelector('.archive-row') && $('thread').textContent.includes('暂无归档对话'),'bulk delete empties archive');
          const remaining=(await (await fetch('/session',{headers})).json()).sessions.map(s=>s.session_id);
          check(disposable.every(id=>!remaining.includes(id)) && remaining.includes(sid),'delete keeps original active conversation');

          $('return-chat').click();
          await until(()=>!!document.querySelector('#sessions .project-group [data-session="'+sid+'"]'),'restored draft remains available in project');
          window.__testStage='archive done';const recentGroup=document.querySelector('#sessions [data-key="section-recent"]');recentGroup.open=false;
          $('refresh').click();await sleep();
          check(!document.querySelector('#sessions [data-key="section-recent"]').open,'refresh preserves collapsed sections');
          document.querySelector('#sessions [data-key="section-recent"]').open=true;
          $('sidebar-activity').querySelector('summary').click();
          check($('sidebar-activity').open && $('filters').getBoundingClientRect().width>150,'activity filters accessible through bell');
          document.querySelector('#filters [data-filter="running"]').click();
          check(!$('sidebar-activity').open && document.querySelector('[data-sidebar-clear]'),'filter selection shows named results and dismisses menu');
          document.querySelector('[data-sidebar-clear]').click();
          check(document.querySelector('#sessions [data-key="section-projects"]'),'clear filter returns grouped navigation');

          document.querySelector('#sessions .project-row-details').click();
          check(!!document.querySelector('.project-overview'),'project settings entry opens existing management page');
          window.dispatchEvent(new Event('xiaoyu-navigate-back'));
          check((document.querySelector('#session-menu-items [data-workspace-session]')?.dataset.workspaceSession||'')===sid && !$('composer-area').hidden,'return from project settings preserves conversation');
          // Create and configure a project through the real interface, without a model call.
          document.querySelector('#sessions .project-row-details').click();
          const newProject=document.querySelector('.project-create');newProject.open=true;
          newProject.querySelector('input').value='产品研究';newProject.querySelector('form').requestSubmit();
          await until(()=>$('title').textContent==='产品研究','new folderless project created');
          const newPid=document.querySelector('[data-project-create]').dataset.projectCreate;
          check(document.querySelector('.project-folders').textContent.includes('尚未关联文件夹'),'folderless project state');
          document.querySelector('[data-project-create]').click();
          check($('composer-project-label').textContent==='产品研究' && $('message').value==='','new project has isolated empty input');
          $('message').value='项目独立草稿';$('message').dispatchEvent(new Event('input',{bubbles:true}));
          document.querySelector('[data-project="'+newPid+'"]').click();
          const addFolder=async path=>{const form=document.querySelector('[data-project-folder]');form.elements.folder.value=path;form.requestSubmit();await until(()=>[...document.querySelectorAll('.project-folder-path')].some(n=>n.title===path),'folder attached');await until(()=>!document.querySelector('[data-project-name] button').disabled,'folder save settled');};
          await addFolder(folderA);
          const related=folderA.slice(0,folderA.lastIndexOf('/'))+'/Reference';await addFolder(related);
          document.querySelector('[data-workspace-action="primary-folder"][data-folder="'+related+'"]').click();
          await until(()=>[...document.querySelectorAll('.project-folder-row')].some(n=>n.querySelector('.project-folder-path')?.title===related && n.querySelector('.note')?.textContent==='主文件夹'),'primary folder changed');
          const projectNewChat=document.querySelector('[data-project-new-chat="'+newPid+'"]');
          check(projectNewChat.title==='在此项目新建对话' && !!projectNewChat.querySelector('svg'),'project row new chat icon and tooltip');
          const newChatGroup=projectNewChat.closest('details');newChatGroup.open=false;
          projectNewChat.click();
          check(!document.querySelector('[data-project-new-chat="'+newPid+'"]').closest('details').open,'new chat button does not expand project');
          check($('workspace-path').value===related,'new chat uses primary folder');
          check($('message').value==='项目独立草稿','project draft survives primary folder change');
          $('composer-project').click();
          document.querySelector('[data-input-project]:not([data-input-project="'+newPid+'"])').click();
          check($('message').value!== '项目独立草稿','same attached folder does not share project drafts');
          $('composer-project').click();document.querySelector('[data-input-project="'+newPid+'"]').click();
          check($('message').value==='项目独立草稿','project picker restores exact input');
          check((await (await originalFetch('/session',{headers})).json()).sessions.length===beforeCount,'project configuration never creates a conversation');
          document.querySelector('#sessions .project-group [data-session="'+sid+'"]').click();
          check(!$('composer-model').disabled,'existing conversation allows model choice');
          const nextChoice=[...$('composer-model').options].map(o=>o.value).find(v=>v!==$('composer-model').value);
          $('composer-model').value=nextChoice;$('composer-model').dispatchEvent(new Event('change',{bubbles:true}));
          await until(()=>!$('composer-model').disabled && $('composer-hint').textContent.includes(nextChoice),'model selection saved for next turn');
          const actual=(await (await originalFetch('/session',{headers})).json()).sessions.find(item=>item.session_id===sid);
          check(actual.next_model===nextChoice,'native chooser reaches real model endpoint');
          $('composer-model').value=actual.model;$('composer-model').dispatchEvent(new Event('change',{bubbles:true}));
          await until(()=>!$('composer-model').disabled && !$('composer-hint').textContent.includes('下一条'),'model choice cancellation');

          let simulatedBusy=true;
          window.fetch=async(...args)=>{
            const response=await originalFetch(...args);
            const path=new URL(typeof args[0]==='string'?args[0]:args[0].url,location.href).pathname;
            if(path==='/session/'+sid+'/history')return new Response(JSON.stringify({messages:[{id:'fixture-model',role:'model',from_model:'deepseek-chat',model:'deepseek-flash'},{id:'fixture-user',role:'user',content:'帮我整理这周的工作重点。'},{id:'fixture-before',role:'assistant',content:'我先查看工作记录。'},{id:'fixture-tool',role:'tool',name:'读取工作记录',content:'{"items":["整理需求","完成验证"]}',args:{path:'weekly.md'},ok:true},{id:'fixture-answer',role:'assistant',content:'可以。先列出本周要完成的事项，再按优先级整理。'}],total:5,next_cursor:null,live_from_seq:0,persisted:true}),{status:200,headers:response.headers});
            if(path!=='/session')return response;
            const data=await response.clone().json();
            for(const item of data.sessions||[])if(item.session_id===sid){item.busy=simulatedBusy;item.turns=simulatedBusy?0:1;item.in_flight=simulatedBusy?{turn:1,command:{kind:'prompt'}}:null;}
            return new Response(JSON.stringify(data),{status:response.status,headers:response.headers});
          };
          $('refresh').click();
          await until(()=>!$('stop').hidden && $('send').hidden,'busy swaps arrow for stop');
          check(!!document.querySelector('#sessions [data-key="section-recent"] [data-session="'+sid+'"]'),'first running turn enters recent before completion');
          check($('stop').getAttribute('aria-label')==='停止生成','stop has correct label');
          $('message').value='补充要求';$('message').dispatchEvent(new Event('input',{bubbles:true}));
          check(!$('stop').hidden && !$('send').hidden && $('send').getAttribute('aria-label')==='发送补充','supplement and stop remain available');
          simulatedBusy=false;$('refresh').click();
          await until(()=>$('stop').hidden && !$('send').hidden,'idle restores send arrow');
          check(!!document.querySelector('#sessions [data-key="section-recent"] [data-session="'+sid+'"]'),'completed conversation remains recent');
          await until(()=>!!document.querySelector('[data-copy-message="fixture-answer"]'),'answer copy action');
          check(document.querySelector('[data-message-id="fixture-user"]').classList.contains('user'),'history identity and user bubble');
          check(document.querySelector('.model-change')?.textContent.includes('deepseek-flash'),'actual model change displayed in transcript');
          check(!$('subtitle').textContent && !$('session-state').textContent,'no redundant conversation header metadata');
          check(!$('notice').textContent,'no sticky success message');
          check(!$('thread').textContent.includes('运行详情'),'technical details outside transcript');
          const activity=document.querySelector('.turn-activity');
          check(activity && !activity.open,'tool process collapsed above answer');
          activity.open=true;
          const tool=activity.querySelector('.tool-activity');tool.open=true;
          check(tool.textContent.includes('整理需求'),'expanded tool output present');
          activity.open=false;
          $('session-menu').open=true;document.querySelector('[data-session-command="details"]').click();
          check($('execution-dialog').open,'details available from session menu');
          $('close-execution-details').click();check(!$('execution-dialog').open,'details dialog closes');

          let copied='';Object.defineProperty(navigator,'clipboard',{value:{writeText:async text=>{copied=text}},configurable:true});
          document.querySelector('[data-copy-message="fixture-answer"]').click();await sleep();
          check(copied==='可以。先列出本周要完成的事项，再按优先级整理。','copy preserves source answer');
          $('message').value='';$('message').dispatchEvent(new Event('input',{bubbles:true}));
          const simulatedFetch=window.fetch;
          let acceptSubmission,flashedReceipt=false;
          const observer=new MutationObserver(records=>{
            for(const record of records)for(const node of record.addedNodes)
              if(node.textContent.includes('提交任务'))flashedReceipt=true;
          });
          observer.observe($('outbox'),{childList:true,subtree:true});
          window.fetch=(...args)=>{
            const path=new URL(typeof args[0]==='string'?args[0]:args[0].url,location.href).pathname;
            if(path==='/session/'+sid+'/prompt_async' && args[1]?.method==='POST')
              return new Promise(resolve=>{acceptSubmission=resolve;});
            return simulatedFetch(...args);
          };
          $('message').value='隔离发送反馈验证';$('message').dispatchEvent(new Event('input',{bubbles:true}));
          $('composer').requestSubmit();
          await until(()=>!!acceptSubmission,'submission dispatched to fixture');
          check(!$('outbox').textContent.trim(),'sending has no top receipt card');
          check($('send').disabled,'composer retains pending feedback');
          acceptSubmission(new Response(JSON.stringify({session_id:sid}),{status:200,headers:{'Content-Type':'application/json'}}));
          await until(()=>!$('message').readOnly,'accepted receipt reconciled');
          await sleep();observer.disconnect();window.fetch=simulatedFetch;
          check(!flashedReceipt && !$('outbox').textContent.trim(),'accepted receipt never flashes above transcript');
          check(window.__testErrors.length===0,'collaboration page errors');
          return {done:true,keyboardFocusChecked};
        }
        check(select.value===choices[0],'first enabled model default');
        const editor=$('message'), form=$('composer'), requestSubmit=form.requestSubmit;
        let enterSends=0;form.requestSubmit=()=>enterSends++;
        editor.value='第一行';editor.setSelectionRange(3,3);
        editor.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true,cancelable:true}));
        check(enterSends===1,'Enter sends by default');
        for(const modifier of ['shiftKey','altKey']){
          editor.value='前后';editor.setSelectionRange(1,1);
          editor.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',[modifier]:true,bubbles:true,cancelable:true}));
          check(editor.value==='前\n后' && enterSends===1,modifier+' inserts newline');
        }
        editor.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',isComposing:true,bubbles:true,cancelable:true}));
        check(enterSends===1,'IME confirmation does not send');
        form.requestSubmit=requestSubmit;editor.value='';editor.dispatchEvent(new Event('input',{bubbles:true}));

        for(const id of ['permission-picker','workflow-picker']) {
          const root=$(id), trigger=root.querySelector('summary'), menu=root.querySelector('.menu-items');
          trigger.click();await sleep();
          check(root.querySelector('details').open,id+' opens');
          const box=menu.getBoundingClientRect(),anchor=trigger.getBoundingClientRect();
          check(box.bottom<=anchor.top-8 && box.top>=0,id+' leaves trigger visible');
          check(box.left>=0 && box.right<=innerWidth,id+' stays in window');
          check(trigger.querySelector('svg').getBoundingClientRect().width>0,id+' icon visible');
          trigger.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}));
          check(!root.querySelector('details').open && document.activeElement===trigger,id+' escape restores focus');
        }
        const permission=$('composer-mode'), permissionRoot=$('permission-picker');
        const descriptions=[...permissionRoot.querySelectorAll('.picker-option-description')];
        check(descriptions.length===permission.options.length,'each permission choice explains behavior');
        check([...permission.options].filter(o=>o.value).every(o=>!['auto','plan','确认'].includes(o.textContent)),'user-facing permission names');
        check(permission.value==='' && permissionRoot.querySelector('.model-picker-label').textContent!=='使用默认设置','inherited permission shows effective mode');
        check(permissionRoot.querySelector('.picker-option-inherited').dataset.modelValue==='','default remains inherited');
        const alternate=[...permission.options].find(o=>o.value);
        check(!!alternate,'permission choices available');
        permissionRoot.querySelector('summary').click();
        [...permissionRoot.querySelectorAll('[data-model-value]')].find(n=>n.dataset.modelValue===alternate.value).click();
        check(permission.value===alternate.value,'permission choice updates underlying control');
        permission.disabled=true;permission.dispatchEvent(new Event('change'));
        permissionRoot.querySelector('summary').click();
        check(!permissionRoot.querySelector('details').open,'disabled permission picker cannot open');
        permission.disabled=false;permission.value='';permission.dispatchEvent(new Event('change'));
        const workflowRoot=$('workflow-picker');
        check(workflowRoot.querySelectorAll('.picker-option-description').length===4,'workflow choices explain their purpose');
        check(workflowRoot.querySelectorAll('.picker-option-icon svg').length===4,'workflow choices have icons');
        workflowRoot.querySelector('summary').dispatchEvent(new KeyboardEvent('keydown',{key:'Home',bubbles:true}));
        document.activeElement.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true}));
        check($('work-mode').value==='auto' && $('message').value==='', 'workflow selection leaves empty draft unchanged');
        check([...$('work-mode').options].map(o=>o.value).join(',')==='auto,weave,contest,fleet','only distinct execution workflows are offered');
        $('new-session').click();
        check($('work-mode').value==='auto' && workflowRoot.querySelector('.model-picker-label').textContent==='对话','new conversation resets workflow caption');
        const addMenu=$('starter-menu');addMenu.querySelector('summary').click();await sleep();
        check(addMenu.querySelectorAll('[data-add]').length===2 && !$('attach-reference'),'plus offers direct actions without a management page');
        addMenu.querySelector('[data-add="skills"]').click();
        await until(()=>!!addMenu.querySelector('[data-add-search]'),'inline skill list appears');
        check(addMenu.open,'switching to skills must keep add menu open');
        check(addMenu.querySelectorAll('[data-add^="skill:"]').length===4,'real discovered fixture skill can be selected');
        const skillSearch=addMenu.querySelector('[data-add-search]');skillSearch.value='no-match';skillSearch.dispatchEvent(new Event('input',{bubbles:true}));
        check(addMenu.querySelectorAll('[data-add^="skill:"]').length===0 && addMenu.open,'search keeps menu open');
        skillSearch.value='desktop-fixture';skillSearch.dispatchEvent(new Event('input',{bubbles:true}));
        addMenu.querySelector('[data-add^="skill:"]').click();
        check($('message').value.includes('请使用技能 desktop-fixture') && !addMenu.open,'selecting real discovered skill adds request and closes menu');
        // Reopen with an existing submenu: opening must initialize synchronously.
        addMenu.querySelector('summary').click();
        check(!!addMenu.querySelector('[data-add="skills"]'),'reopen immediately restores actions');
        addMenu.querySelector('[data-add="skills"]').click();
        await until(()=>!!addMenu.querySelector('[data-add-search]'),'reopen skill picker');
        await sleep();
        check(addMenu.open && !!addMenu.querySelector('[data-add-search]'),'queued toggle cannot overwrite the selected subview');
        addMenu.querySelector('[data-add="back"]').click();
        check(addMenu.open,'back keeps add menu open');
        addMenu.querySelector('summary').click();
        $('message').value='';$('message').dispatchEvent(new Event('input',{bubbles:true}));
        workflowRoot.querySelector('summary').click();await sleep();
        $('message').value='保留我的草稿';$('message').dispatchEvent(new Event('input',{bubbles:true}));
        const mouseOption=workflowRoot.querySelector('[data-model-value="auto"]');
        const pointer=new PointerEvent('pointerdown',{bubbles:true,cancelable:true,button:0});
        mouseOption.dispatchEvent(pointer);
        check(pointer.defaultPrevented,'mouse selection keeps list focus until commit');
        mouseOption.click();
        check($('work-mode').value==='auto' && $('message').value==='保留我的草稿','workflow choice preserves existing draft');
        check(!workflowRoot.querySelector('details').open,'mouse choice closes menu');

        check(!$('template-menu'),'no permanent template toolbar');
        const starterGroup=document.querySelector('.welcome-starters');
        const starterChoices=[...starterGroup.querySelectorAll('[data-starter]')];
        check(starterChoices.length===3,'three welcome starters');
        const originalModel=select.value,originalPermission=permission.value;
        const input=value=>{$('message').value=value;$('message').dispatchEvent(new Event('input',{bubbles:true}));};
        let submissions=0;const trackSubmit=()=>submissions++;$('composer').addEventListener('submit',trackSubmit);
        for(const [i,button] of starterChoices.entries()) {
          input('');check(!starterGroup.hidden,'empty draft shows starters');
          button.click();const draft=$('message').value;
          check(draft.length>0 && !draft.includes('\n'),'single prompt without blank lines '+i);
          check($('work-mode').value==='auto','starter selects workflow '+i);
          check(workflowRoot.querySelector('.model-picker-label').textContent==='对话','workflow caption updates '+i);
          check(starterGroup.hidden && document.activeElement===$('message'),'starter hides and focuses draft');
          button.click();check($('message').value===draft,'repeated selection cannot append');
          check(select.value===originalModel && permission.value===originalPermission,'starter preserves model and permission');
        }
        input('我的草稿');const savedMode=$('work-mode').value;
        starterChoices[0].click();
        check(starterGroup.hidden && $('message').value==='我的草稿' && $('work-mode').value===savedMode,'typed draft protected');
        input('');$('message').readOnly=true;starterChoices[0].click();
        check($('message').value==='','readonly blocks starter');$('message').readOnly=false;
        $('message').disabled=true;starterChoices[0].click();
        check($('message').value==='','disabled blocks starter');$('message').disabled=false;
        check(submissions===0,'starters do not submit');$('composer').removeEventListener('submit',trackSubmit);
        const {createComposerAdditions}=await import('./composer_additions.mjs');
        const host=document.createElement('details');host.innerHTML='<summary>添加测试</summary><div class="menu-items"></div>';document.body.append(host);
        let draft='原始草稿',sid='first',resolveFiles;
        const testConnection={client:{get:async()=>({scope:'installed_user_sources',skills_enabled:true,skills:[{id:'sample',name:'示例技能',description:'用于测试'}],plugins:[],workflows:[]})}};
        createComposerAdditions({root:host,boundary:$('workspace'),context:()=>({conn:testConnection,sid,workspace:'/tmp',enabled:true}),get:()=>draft,put:value=>draft=value,notice:()=>{},focus:()=>{},pickFiles:()=>new Promise(resolve=>resolveFiles=resolve)});
        const openAdd=async()=>{host.querySelector('summary').click();await sleep();};
        await openAdd();host.querySelector('[data-add="files"]').click();await sleep();
        sid='second';resolveFiles({items:[{kind:'file',path:'/tmp/first.txt'}]});await sleep();
        check(draft==='原始草稿','late file selection cannot land in another conversation');
        await openAdd();host.querySelector('[data-add="files"]').click();await sleep();
        draft+='编辑中';resolveFiles({items:[{kind:'directory',path:'/tmp/project'}]});await sleep();
        check(draft.startsWith('原始草稿编辑中') && draft.includes('参考文件夹："/tmp/project"'),'file choice preserves latest typed draft');
        const withFile=draft;await openAdd();host.querySelector('[data-add="files"]').click();await sleep();resolveFiles({cancelled:true});await sleep();
        check(draft===withFile,'cancelling native selection changes nothing');
        await openAdd();host.querySelector('[data-add="skills"]').click();await sleep();
        host.querySelector('[data-add="skill:0"]').click();
        check(draft.includes('请使用技能 sample') && draft.startsWith(withFile),'inline skill picker preserves draft');host.remove();
        const next=choices[1];
        document.querySelector('#model-picker .model-picker-trigger').click();
        check(document.querySelector('#model-picker .model-picker').open,'model menu opens');
        [...document.querySelectorAll('[data-model-value]')].find(n=>n.dataset.modelValue===next).click();
        await until(()=>select.value===next && document.querySelector('#model-picker .model-picker-label').textContent===next,'model selected');
        for(let i=0;i<100;i++){
          const response=await fetch('/desktop/preferences',{headers:{Authorization:'Bearer '+token}});
          const prefs=await response.json();
          if(prefs.values?.default_model===next){
            $('new-session').click();check(select.value===next,'new conversation remembers model');
            $('message').value='Draft only';$('message').dispatchEvent(new Event('input',{bubbles:true}));
            check(!$('send').disabled,'ready to send without manual connection');
            await window.__XIAOYU_FLUSH__();
            return {next};
          }
          await sleep();
        }
        throw new Error('model preference not saved');
        """#
        web.callAsyncJavaScript(script, arguments: ["phase": phase, "expected": expected, "token": config["token"]!], in: nil, in: .page) { result in
            switch result {
            case .failure(let error): print("BOOTSTRAP_FAIL", error.localizedDescription, (error as NSError).userInfo["WKJavaScriptExceptionMessage"] ?? ""); exit(1)
            case .success(let value):
                if self.phase == 0 {
                    guard let data = value as? [String: Any], let next = data["next"] as? String else { exit(2) }
                    self.expected = next; self.phase = 1; self.window.setContentSize(NSSize(width: 900, height: 850)); self.web.reload()
                } else {
                    if let data = value as? [String: Any], data["keyboardFocusChecked"] as? Bool == false { print("BOOTSTRAP_NOTE keyboard visibility check skipped: test window inactive") }
                    print("BOOTSTRAP_PASS auto-connect icons model-list save reload default-project")
                    if let output = config["output"] { self.window.setContentSize(NSSize(width: 1200, height: 850)); self.capturePages(["chat", "process", "weave", "contest", "fleet", "split", "project", "plugins", "skills-expanded", "skills-switches", "skills-search", "plugin-discovery", "connector-discovery", "amap-setup", "supabase-setup", "vercel-setup", "github-setup", "plugin-connectors", "mcp-http", "mcp-stdio", "mcp-narrow", "archive-page", "archive-wide", "archive-narrow", "archive-confirm", "more-menu", "archive-empty"], output: output) }
                    else { NSApplication.shared.terminate(nil) }
                }
            }
        }
    }
    func capturePages(_ modes: [String], output: String) {
        guard let mode = modes.first else { NSApplication.shared.terminate(nil); return }
        if mode == "mcp-http" || mode == "mcp-stdio" { window.setContentSize(NSSize(width: 1200, height: 1250)) }
        if mode == "mcp-narrow" { window.setContentSize(NSSize(width: 850, height: 1250)) }
        if mode == "archive-narrow" { window.setContentSize(NSSize(width: 850, height: 850)) }
        if mode == "archive-wide" || mode == "archive-confirm" { window.setContentSize(NSSize(width: 1200, height: 850)) }
        if mode == "archive-page" { window.setContentSize(NSSize(width: 1200, height: 850)) }
        let script = mode == "archive-page" ? "document.getElementById('return-chat').click();document.querySelector('#session-menu-items [data-workspace-action=archive]').click();document.getElementById('archive-sessions').click();" : mode == "archive-wide" ? "window.dispatchEvent(new Event('xiaoyu-toggle-panel'));" : mode == "archive-narrow" ? "void 0" : mode == "archive-confirm" ? "document.querySelector('[data-workspace-action=delete-all-archived]').click();" : mode == "more-menu" ? "document.getElementById('archive-delete-dialog').close('cancel');document.getElementById('sidebar-more').open=true;" : mode == "archive-empty" ? "document.getElementById('sidebar-more').open=false;document.querySelector('.archive-row [data-workspace-action=archive]').click();" : mode == "mcp-http" ? "window.dispatchEvent(new Event('xiaoyu-toggle-panel'));document.querySelector('[data-connection-action=new]').click();" : mode == "mcp-stdio" ? "document.getElementById('connection-type-stdio').click();" : mode == "mcp-narrow" ? "void 0" : mode == "plugins" ? "document.getElementById('capabilities').click();" : mode == "skills-expanded" ? "document.querySelector('[data-capability-action=group][data-group=aws]').click();" : mode == "skills-switches" ? "document.getElementById('skill-switch-skill-aws-auth').click();" : mode == "skills-search" ? "const input=document.getElementById('plugin-search');input.value='审批';input.dispatchEvent(new Event('input',{bubbles:true}));" : mode == "plugin-discovery" ? "document.getElementById('plugin-view-discover').click();" : mode == "connector-discovery" ? "document.getElementById('plugin-type-connectors').click();window.dispatchEvent(new Event('xiaoyu-toggle-panel'));" : mode == "amap-setup" ? "document.querySelector('[data-connector-preset=amap]').click();" : ["supabase-setup", "vercel-setup", "github-setup"].contains(mode) ? "document.querySelector('[data-connection-action=discard]')?.click();document.getElementById('plugin-view-discover').click();document.querySelector('[data-connector-preset=\(mode.replacingOccurrences(of: "-setup", with: ""))]').click();" : mode == "plugin-connectors" ? "document.querySelector('[data-connection-action=discard]')?.click();document.getElementById('plugin-type-connectors').click();document.getElementById('plugin-view-installed').click();" : mode == "project" ? "document.querySelectorAll(\".project-row-details\")[1].click();" : mode == "split" ? "document.getElementById('return-chat').click();window.dispatchEvent(new Event('xiaoyu-toggle-panel'));document.querySelector('[data-pane=\"new\"]').click();" : mode == "chat" ? "document.getElementById('return-chat').click();document.querySelector('.project-group').open=true;document.getElementById('message').value='请整理一份项目进展，重点说明本周完成的工作。\\nProject notes · 2026 / 本周重点';document.getElementById('message').dispatchEvent(new Event('input',{bubbles:true}));" : mode == "process" ? "document.querySelector('.turn-activity').open=true;document.querySelector('.tool-activity').open=true;" : "document.getElementById('new-\(mode)').click()"
        web.evaluateJavaScript(script) { _, error in
            if error != nil { exit(5) }
            DispatchQueue.main.asyncAfter(deadline: .now() + (["skills-switches", "archive-page", "archive-empty"].contains(mode) ? 0.6 : 0.2)) {
                self.web.takeSnapshot(with: nil) { image, error in
                    guard let image = image, let tiff = image.tiffRepresentation,
                          let bitmap = NSBitmapImageRep(data: tiff), let png = bitmap.representation(using: .png, properties: [:]) else { exit(3) }
                    do {
                        try png.write(to: URL(fileURLWithPath: output.replacingOccurrences(of: ".png", with: "-\(mode).png")))
                        if modes.count == 1 { try png.write(to: URL(fileURLWithPath: output)) }
                    } catch { exit(4) }
                    self.capturePages(Array(modes.dropFirst()), output: output)
                }
            }
        }
    }

}
let app=NSApplication.shared
app.setActivationPolicy(.accessory)
let delegate=Bootstrap()
app.delegate=delegate
app.run()
