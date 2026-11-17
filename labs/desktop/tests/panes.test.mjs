import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {TerminalScreen,terminalKey,browserURL,terminalCellWidth,createPanes} from '../web/panes.mjs';
test('browser accepts explicit web navigation only',()=>{assert.equal(browserURL('https://example.test/a'),'https://example.test/a');for(const value of ['javascript:alert(1)','file:///etc/passwd','https://user:pass@example.test'])assert.throws(()=>browserURL(value));});
test('terminal input uses real control sequences',()=>{assert.equal(terminalKey({key:'c',ctrlKey:true}),'\x03');assert.equal(terminalKey({key:'ArrowUp'}),'\x1b[A');assert.equal(terminalKey({key:'v',metaKey:true}),null);});
test('VT cursor/erase and split escape sequences do not produce HTML or OSC links',()=>{const s=new TerminalScreen();s.feed('hello\rOK\x1b[');s.feed('K\n\r\x1b]8;;https://evil.test\x07world\x1b]8;;\x1b\\');assert.equal(s.text(),'OK\nworld');s.feed('\x1b[?1049hTEMP\x1b[?1049l');assert.equal(s.text(),'OK\nworld');});
test('untrusted cursor and repeated newlines stay bounded',()=>{const s=new TerminalScreen();s.feed('\x1b[999999999999999;99999999HX');s.feed('\n'.repeat(20000)+'end');assert.ok(s.lines.length<=1000);assert.ok(s.text().length<10000);assert.ok(s.text().endsWith('end'));});
test('keyless terminal presentation contract',()=>{const s=new TerminalScreen(30,10);s.feed('羽案终端\r\nuser$ echo ready\r\nready\r\n');assert.deepEqual({text:s.text(),ctrl_c:terminalKey({key:'c',ctrlKey:true}),url:browserURL('https://example.test/reference')},JSON.parse(fs.readFileSync(new URL('./panes.expected.json',import.meta.url))));});

test('ANSI colors use escaped text and numeric styles only',()=>{const s=new TerminalScreen();s.feed('\x1b[31;1m<red>\x1b[0m plain \x1b[38;2;1;2;3mRGB\x1b[0m');assert.equal(s.text(),'<red> plain RGB');assert.match(s.html(),/color:#cd5c5c;font-weight:bold/);assert.match(s.html(),/&lt;red&gt;/);assert.doesNotMatch(s.html(),/<red>/);assert.match(s.html(),/color:rgb\(1,2,3\)/);});
test('CJK double cells, combining marks and overwrite respect terminal columns',()=>{const s=new TerminalScreen(20,5);s.feed('羽案A');assert.equal(s.x,5);assert.equal(terminalCellWidth('羽'),2);assert.equal(terminalCellWidth('e'),1);assert.equal(terminalCellWidth('\u0301'),0);s.feed('\r\x1b[3GZ');assert.equal(s.text(),'羽Z A');assert.match(s.html(),/pane-wide-cell/);const accent=new TerminalScreen();accent.feed('e\u0301');assert.equal(accent.x,1);assert.equal(accent.text(),'e\u0301');accent.feed('\u0301'.repeat(10000));assert.ok(accent.text().length<=64);});
function paneHarness(preferences={}){const events={},calls=[],tools=[];const node={addEventListener(){},focus(){},value:'',clientWidth:400,clientHeight:300,getBoundingClientRect:()=>({x:10,y:10,width:400,height:300})};const host={innerHTML:'',hidden:true,style:{},dataset:{},addEventListener:(n,f)=>events[n]=f,removeEventListener(){},querySelector:()=>node};const client={post:async(path,body,options)=>{calls.push({path,body,options});return path==='/desktop/terminals'?{id:'pty',shell:body.shell,workspace:'/work',state:'running',input_next:1}:{};},get:async()=>({chunks:[],next_seq:1,state:'running',exit_code:null})};const panes=createPanes({host,loadLayout:()=>preferences,context:()=>({client,session_id:'s',instance_id:'epoch'}),onOpenTool:kind=>tools.push(kind)});const click=name=>events.click({target:{closest:selector=>selector==='[data-pane]'?{dataset:{pane:name}}:null}});return{host,node,panes,calls,tools,events,click};}
const turn=()=>new Promise(resolve=>setTimeout(resolve,0));
test('workspace starts with inline browser address and routes real tool entry callbacks',async()=>{const h=paneHarness();try{assert.match(h.host.innerHTML,/data-pane-new-browser/);assert.match(h.host.innerHTML,/临时网站数据/);for(const name of ['files','page','collaboration'])h.click(name);await turn();assert.deepEqual(h.tools,['files','page','collaboration']);h.panes.applyLayout({width:620,expanded:false,open:true});assert.equal(h.host.style.width,'620px');assert.equal(h.host.hidden,false);assert.equal(h.panes.layout().width,620);}finally{h.panes.destroy();}});
test('terminal close offers three inline choices and only explicit stop invokes API',async()=>{const h=paneHarness();try{await h.panes.openContent({id:'t',kind:'terminal',shell:'bash'});assert.equal(h.calls[0].body.shell,'bash');await h.panes.close('t');assert.match(h.host.innerHTML,/停止并关闭/);assert.match(h.host.innerHTML,/仅关闭视图/);assert.match(h.host.innerHTML,/close-cancel/);assert.equal(h.calls.filter(c=>c.path.endsWith('/stop')).length,0);h.click('close-cancel');await turn();assert.doesNotMatch(h.host.innerHTML,/pane-close-confirm/);await h.panes.close('t');h.click('close-stop');await turn();assert.equal(h.calls.filter(c=>c.path.endsWith('/stop')).length,1);assert.match(h.host.innerHTML,/data-pane-new-browser/);}finally{h.panes.destroy();}});

test('inline address submit opens native browser, invalid URL does not',async()=>{const oldWindow=globalThis.window,bridge=[];globalThis.window={webkit:{messageHandlers:{workspace:{postMessage:async body=>{bridge.push(body);return{};}}}}};const h=paneHarness();try{const submit=url=>h.events.submit({preventDefault(){},target:{matches:selector=>selector==='[data-pane-new-browser]',querySelector:()=>({value:url})}});submit('javascript:alert(1)');await turn();assert.equal(bridge.filter(c=>c.operation==='browser_open').length,0);submit('https://example.test/read');await turn();assert.equal(bridge.filter(c=>c.operation==='browser_open').length,1);assert.equal(bridge.find(c=>c.operation==='browser_open').url,'https://example.test/read');}finally{h.panes.destroy();globalThis.window=oldWindow;}});
test('detach closes only the view and retains terminal process',async()=>{const h=paneHarness();try{await h.panes.openContent({id:'t',kind:'terminal'});await h.panes.close('t');h.click('close-detach');await turn();assert.equal(h.calls.filter(c=>c.path.endsWith('/stop')).length,0);assert.match(h.host.innerHTML,/data-pane-new-browser/);}finally{h.panes.destroy();}});

test('sidebar opens split despite legacy expanded preference and after explicit enlargement',async()=>{
  const h=paneHarness({expanded:true,width:620});
  try{
    h.panes.toggle();assert.equal(h.panes.layout().expanded,false);
    h.click('expand');assert.equal(h.panes.layout().expanded,true);
    assert.match(h.host.innerHTML,/还原工作区/);
    h.panes.applyLayout({width:500});assert.equal(h.panes.layout().expanded,true);
    h.panes.toggle();h.panes.toggle();assert.equal(h.panes.layout().expanded,false);
    assert.equal(h.host.style.width,'500px');
    h.click('expand');h.click('hide');
    await h.panes.openContent({kind:'text',title:'参考',content:'资料'});
    assert.equal(h.panes.layout().expanded,false);
  }finally{h.panes.destroy();}
});

test('new tab is visible, retains its address draft and becomes content without leaving a blank tab',async()=>{
  const h=paneHarness(), tabs=()=>[...h.host.innerHTML.matchAll(/aria-selected="(true|false)" data-pane-select="([^"]+)"/g)].map(m=>({selected:m[1]==='true',id:m[2]}));
  const select=id=>h.events.click({target:{closest:selector=>selector==='[data-pane-select]'?{dataset:{paneSelect:id}}:null}});
  try{
    await h.panes.openContent({id:'existing',kind:'terminal'});
    h.click('new');await turn();
    assert.equal(tabs().length,2);const first=tabs().find(t=>t.selected).id;
    assert.notEqual(first,'existing');assert.match(h.host.innerHTML,/新标签页/);
    h.events.input({target:{matches:selector=>selector==='[data-pane-new-browser] input',value:'https://example.test/notes?a=1&b=2'}});
    h.click('new');await turn();assert.equal(tabs().length,3);
    const second=tabs().find(t=>t.selected).id;assert.notEqual(first,second);
    select(first);assert.match(h.host.innerHTML,/value="https:\/\/example.test\/notes\?a=1&amp;b=2"/);
    await h.panes.openContent({kind:'text',title:'资料',content:'打开的内容'});
    assert.equal(tabs().length,3);assert.equal(tabs().find(t=>t.selected).id,first);
    assert.match(h.host.innerHTML,/打开的内容/);
    select('existing');assert.match(h.host.innerHTML,/pane-terminal-screen/);
    assert.equal(h.calls.filter(c=>c.path==='/desktop/terminals').length,1);
    await h.panes.close(second);assert.equal(tabs().length,2);
  }finally{h.panes.destroy();}
});

test('new tabs obey the limit and failed opening keeps the selected blank tab',async()=>{
  const h=paneHarness(), count=()=>[...h.host.innerHTML.matchAll(/role="tab"/g)].length;
  try{
    for(let i=0;i<16;i++)h.click('new');await turn();assert.equal(count(),16);
    h.click('new');await turn();assert.equal(count(),16);assert.match(h.node.textContent,/最多同时打开/);
    await assert.rejects(h.panes.openContent({kind:'browser',url:'javascript:alert(1)'}));
    assert.equal(count(),16);assert.match(h.host.innerHTML,/data-pane-new-browser/);
    await h.panes.openContent({kind:'text',title:'资料',content:'允许替换空白标签'});
    assert.equal(count(),16);assert.match(h.host.innerHTML,/允许替换空白标签/);
  }finally{h.panes.destroy();}
});

test('web addresses accept familiar bare domains and retain explicit protocols',()=>{
  for(const [input,expected] of [
    ['baidu.com','https://baidu.com/'],[' www.example.com/a?q=1#part ','https://www.example.com/a?q=1#part'],
    ['example.com:8443/path','https://example.com:8443/path'],['localhost:8080','https://localhost:8080/'],
    ['127.0.0.1:8080','https://127.0.0.1:8080/'],['[::1]:8443','https://[::1]:8443/'],
    ['//example.com/a','https://example.com/a'],['http://localhost:8000/a','http://localhost:8000/a'],
    ['例子.中国','https://xn--fsqu00a.xn--fiqs8s/'],
  ])assert.equal(browserURL(input),expected,input);
  for(const input of ['','  ','https://','not a domain','/etc/passwd','?q=abc','javascript:alert(1)','data:text/html,test','file:///etc/passwd','ftp://example.com','user:pass@example.com','https://user:pass@example.com','exam\nple.com']){
    assert.throws(()=>browserURL(input),/请输入网址|网址无效/,input);
  }
});

test('both new-tab submission and existing browser navigation normalize bare domains',async()=>{
  const oldWindow=globalThis.window,bridge=[];
  globalThis.window={webkit:{messageHandlers:{workspace:{postMessage:async body=>{bridge.push(body);return{};}}}}};
  const h=paneHarness();
  const submit=(selector,value)=>h.events.submit({preventDefault(){},target:{matches:s=>s===selector,querySelector:()=>({value})}});
  try{
    h.click('new');submit('[data-pane-new-browser]','baidu.com');await turn();
    assert.equal(bridge.find(c=>c.operation==='browser_open').url,'https://baidu.com/');
    assert.match(h.host.innerHTML,/data-pane-address novalidate/);
    submit('[data-pane-address]','www.example.com/path');await turn();
    assert.equal(bridge.find(c=>c.operation==='browser_navigate').url,'https://www.example.com/path');
    submit('[data-pane-address]','');await turn();
    assert.match(h.node.textContent,/请输入网址/);
    assert.equal(bridge.filter(c=>c.operation==='browser_navigate').length,1);
  }finally{h.panes.destroy();globalThis.window=oldWindow;}
});
