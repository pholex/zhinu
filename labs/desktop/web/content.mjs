import {escapeHTML as h, renderText} from './client.mjs';
import {icon} from './ui.mjs';


const obj=v=>!!v && typeof v==='object' && !Array.isArray(v);
const labels={page:'页面',space:'空间',project:'项目',material:'项目资料',artifact:'作品',session:'会话',message:'会话正文'};
const typeLabels={document:'文档',image:'图片',web:'网页',file:'其他文件'};
const at=t=>typeof t==='number' && t>0 ? new Date(t*1000).toLocaleString('zh-CN') : '时间未记录';
export function validateContent(value){
  const s=value?.state;
  if(!s || !Number.isInteger(s.revision) || s.revision<0 || ['pages','spaces','projects','favorites'].some(k=>!obj(s[k])) || !Array.isArray(value.catalogue?.projects))throw new Error('内容库响应无效，保留原数据。');
  for(const [id,p] of Object.entries(s.pages))if(p.id!==id || ['title','format','body','source','project_id','space_id'].some(k=>typeof p[k]!=='string') || !['text','markdown','html'].includes(p.format) || !Number.isInteger(p.version) || !Array.isArray(p.versions) || !Array.isArray(p.references) || typeof p.deleted!=='boolean')throw new Error('页面版本格式无效，保留原数据。');
  for(const [id,space] of Object.entries(s.spaces))if(space.id!==id || typeof space.title!=='string' || !Array.isArray(space.project_ids) || !Array.isArray(space.artifact_ids))throw new Error('空间格式无效。');
  return value;
}
export function contentReference(page){return `页面引用（明确加入本次任务草稿，尚未发送）\n${page.title} · 页面 ${page.id} · 保存版本 ${page.version}\n${page.source ? `来源说明：${page.source}\n` : ''}\n${page.body}`;}
export function staticHTML(body){
  // Rebuild a small structural subset with no attributes, links, resource tags or CSS.
  const allowed=new Set('main article section div span p h1 h2 h3 h4 h5 h6 ul ol li dl dt dd table thead tbody tfoot tr th td caption pre code blockquote strong em b i u s small sub sup br hr details summary'.split(' '));
  let result='',offset=0;
  for(const m of String(body).matchAll(/<\/?([a-z][a-z0-9]*)\b[^>]*>/gi)){result+=h(body.slice(offset,m.index));const tag=m[1].toLowerCase();if(allowed.has(tag))result+=`<${m[0].startsWith('</') ? '/' : ''}${tag}>`;offset=m.index+m[0].length;}
  return result+h(body.slice(offset));
}
export function safePagePreview(body,format){
  if(format==='html'){
    const source=`<!doctype html><html><head><meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src data:; base-uri 'none'; form-action 'none'"><meta charset="utf-8"></head><body>${staticHTML(body)}</body></html>`;
    return `<p class="note">受限网页预览：脚本、网络、表单、弹窗和应用接口均不可用；仅显示保存内容的文本与结构，样式与外部资源不加载。</p><iframe title="受限网页预览" sandbox="" referrerpolicy="no-referrer" srcdoc="${h(source)}" style="width:100%;min-height:260px;border:1px solid var(--line)"></iframe>`;
  }
  return format==='markdown' ? `<div class="message-content">${renderText(body)}</div>` : `<pre>${h(body)}</pre>`;
}
export function mergeContentDraft(existing,text,mode){
  if(!existing.trim() || existing===text || mode==='replace')return text;
  if(mode==='append')return `${existing}\n\n${text}`;
  return null;
}
function validEdit(kind,value){
  if(!obj(value) || typeof value.id!=='string')return false;
  const strings=keys=>keys.every(k=>typeof value[k]==='string');
  const refs=key=>Array.isArray(value[key]) && value[key].every(v=>typeof v==='string');
  if(kind==='page')return strings(['title','body','format','source','project_id','space_id']) && ['text','markdown','html'].includes(value.format) && Number.isInteger(value.expected_version) && value.expected_version>=0 && refs('references');
  if(kind==='space')return strings(['title','description']) && refs('project_ids') && refs('artifact_ids');
  if(kind==='project')return strings(['instructions']) && Array.isArray(value.materials) && value.materials.every(m=>obj(m) && ['id','title','text','source'].every(k=>typeof m[k]==='string'));
  return false;
}
function validPending(p){
  return obj(p) && Number.isInteger(p.revision) && p.revision>=0 && typeof p.request_id==='string' && /^[A-Za-z0-9_-]{1,100}$/.test(p.request_id) && ['page_save','page_delete','page_restore','space_save','space_delete','project_save','favorite'].includes(p.action) && obj(p.value) && typeof p.value.id==='string' && (p.action==='page_save' ? validEdit('page',p.value) : p.action==='space_save' ? validEdit('space',p.value) : p.action==='project_save' ? validEdit('project',p.value) : p.action==='favorite' ? typeof p.value.favorite==='boolean' : true);
}
const pageValue=p=>({id:p.id,title:p.title,format:p.format,body:p.body,source:p.source,project_id:p.project_id,space_id:p.space_id,references:[...p.references],expected_version:p.version});
export function createContentController(ui){
  const namespace=typeof ui.draftNamespace==='string' ? ui.draftNamespace.replace(/[^A-Za-z0-9:_-]/g,'').slice(0,120) : '';
  const DRAFT=`content-editor${namespace ? ':'+namespace : ''}`,PENDING=`content-pending${namespace ? ':'+namespace : ''}`;
  let data=null,error='',generation=0,loading=false,restored=false,draft=null,draftError='',pending=null,sending=false,notice='',view='library',projectId='',spaceId='',selectedPage='',historyVersion=0,placement=null;
  let catalogue=null,libraryError='',libraryLoading=false,query='',searchKind='',searchResult=null,searchError='',searchLoading=false,searchSequence=0,filterType='',favoritesOnly=false,showDeleted=false;
  const ctx=()=>ui.context(),store=()=>ctx().conn?.localState,paint=()=>ui.render();
  const writable=()=>!!(ctx().writable && ctx().conn && store() && data && !error && !draftError);
  const pendingAllowed=()=>writable() && !sending && !pending;
  function restore(){if(restored || !store())return;restored=true;
    try{const raw=store().getDraft(DRAFT);if(raw){const d=JSON.parse(raw);if(!obj(d) || !['page','space','project'].includes(d.kind) || !validEdit(d.kind,d.value))throw new Error();draft=d;}}
    catch{draftError='保存的内容草稿无法读取，原文保留；暂停写入。';}
    try{const raw=store().getDraft(PENDING);if(raw){const b=JSON.parse(raw);if(!validPending(b))throw new Error();pending={body:b,status:'unknown',message:'存在待核对的内容保存请求；未自动重发。'};}}
    catch{draftError='保存的内容请求无法读取，原文保留；暂停写入。';}
  }
  function persist(){if(draft)store()?.setDraft(DRAFT,JSON.stringify(draft));}
  function apply(value){validateContent(value);if(!data || value.state.revision>=data.state.revision)data=value;}
  async function load(conn=ctx().conn){if(!conn || conn!==ctx().conn || loading)return;const epoch=generation;restore();loading=true;paint();
    try{const next=await conn.client.get('/desktop/content',conn.controller.signal);if(conn!==ctx().conn || epoch!==generation)return;apply(next);error='';restore();}
    catch(e){if(conn===ctx().conn && epoch===generation)error=e.status===404 ? '当前服务尚未提供内容库。' : e.message;}
    finally{if(conn===ctx().conn && epoch===generation){loading=false;paint();}}
  }
  async function loadLibrary(){const conn=ctx().conn,epoch=generation;if(!conn || libraryLoading)return;libraryLoading=true;paint();
    try{const next=await conn.client.get('/desktop/content/library',conn.controller.signal);if(conn!==ctx().conn || epoch!==generation)return;
      if(!Array.isArray(next.artifacts) || next.artifacts.some(a=>typeof a.content_id!=='string' || typeof a.session_id!=='string' || !obj(a.source) || !a.artifact?.id))throw new Error('作品来源格式不正确。');catalogue=next;libraryError='';}
    catch(e){if(conn===ctx().conn && epoch===generation)libraryError=e.message;}
    finally{if(conn===ctx().conn && epoch===generation){libraryLoading=false;paint();}}
  }
  async function search(rebuild=false){const conn=ctx().conn,epoch=generation,seq=++searchSequence;if(!conn)return;searchLoading=true;paint();
    try{if(rebuild)await conn.client.post('/desktop/content/search/rebuild',{}, {signal:conn.controller.signal});
      if(!query.trim()){searchResult=null;searchError='请输入搜索词；索引已可按需读取。';return;}
      const p=new URLSearchParams({q:query,kind:searchKind,project_id:projectId,limit:'200'});
      const next=await conn.client.get(`/desktop/content/search?${p}`,conn.controller.signal);
      if(conn!==ctx().conn || epoch!==generation || seq!==searchSequence)return;
      if(!Array.isArray(next.results))throw new Error('搜索响应格式不正确。');searchResult=next;searchError='';
    }catch(e){if(conn===ctx().conn && epoch===generation && seq===searchSequence)searchError=e.message;}
    finally{if(conn===ctx().conn && epoch===generation && seq===searchSequence){searchLoading=false;paint();}}
  }
  function edit(kind,value){restore();if(draftError)return;
    if(draft && (draft.kind!==kind || draft.value.id!==value.id)){notice='还有一份编辑草稿。请先保存或明确放弃，再编辑其他内容。';paint();return;}
    if(!draft){draft={kind,value:structuredClone(value)};persist();}notice='';if(kind==='page'){selectedPage=value.id;historyVersion=0;}paint();
  }
  function newPage(format='markdown'){
    edit('page',{id:crypto.randomUUID(),title:format==='html' ? '新网页' : '新页面',format,body:format==='html' ? '<main><h1>新网页</h1><p>在这里编写内容。</p></main>' : '',project_id:projectId,space_id:spaceId,references:[],source:'用户在羽案创建',expected_version:0});
  }
  function openPage(id){if(!data?.state.pages[id])return false;selectedPage=id;historyVersion=0;view='pages';paint();return true;}
  function openProject(id){projectId=id;view='project';paint();}
  function request(action,value){if(!pendingAllowed())return;const body={revision:data.state.revision,request_id:crypto.randomUUID(),action,value};
    if(JSON.stringify(body).length>95000){notice='此编辑超过可恢复草稿容量，请拆分资料或页面后保存。';paint();return;}
    pending={body,status:'ready',message:''};dispatch();
  }
  async function dispatch(){const conn=ctx().conn,epoch=generation,p=pending;if(!conn || !p || sending || !writable() || p.status==='conflict')return;sending=true;paint();
    try{store().setDraft(PENDING,JSON.stringify(p.body));await store().flush();const next=await conn.client.post('/desktop/content',p.body,{signal:conn.controller.signal});if(conn!==ctx().conn || epoch!==generation)return;apply(next);error='';
      if(draft && draft.value.id===p.body.value.id){
        if(p.body.action==='page_save' && draft.kind==='page'){const saved=next.state.pages[draft.value.id],submitted=p.body.value;const same=saved && ['title','format','body','source','project_id','space_id','references'].every(k=>JSON.stringify(saved[k])===JSON.stringify(submitted[k]));if(same)draft.value.expected_version=saved.version;persist();}
        notice='保存请求已确认；编辑内容保留。列表与版本以当前服务记录为准。';
      }
      pending=null;store().deleteDraft(PENDING);await store().flush();if(p.body.action==='favorite')await loadLibrary();
    }catch(e){if(conn===ctx().conn && epoch===generation){pending=p;p.status=e.status===409 ? 'conflict' : e.status===422 ? 'rejected' : 'unknown';p.message=p.status==='conflict' ? '内容已有新修改；原请求未覆盖新版本。请核对当前内容与本地草稿。' : p.status==='rejected' ? '保存请求未被接受；请核对名称、容量、来源与版本。原草稿保留。' : `保存结果待核对：${e.message}；未自动重试。`;await load(conn);}}
    finally{if(conn===ctx().conn && epoch===generation){sending=false;paint();}}
  }
  function discardReceipt(){if(sending)return;pending=null;store()?.deleteDraft(PENDING);notice='已清除本地待核对回执；这不会撤销已生效的保存。';paint();}
  function place(text,mode){const c=ctx();if(!c.sid || c.active===false){notice='先选择目标会话，再将内容放入任务草稿。';paint();return;}
    if(mode && (!placement || placement.sid!==c.sid || placement.conn!==c.conn)){placement=null;notice='目标会话已变化，请重新选择引用内容。';paint();return;}
    const merged=mergeContentDraft(ui.getComposer(),text,mode);
    if(merged===null)placement={text,sid:c.sid,conn:c.conn};else{ui.putComposer(merged);placement=null;notice='内容已放入任务草稿，尚未发送；已有会话不会自动采用更新后的内容。';}paint();
  }
  async function fromArtifact(id,preview=false){const row=catalogue?.artifacts.find(a=>a.content_id===id),conn=ctx().conn,epoch=generation;if(!row || !conn)return;
    const p=new URLSearchParams(row.source);notice='正在读取登记成果的交付版本…';paint();
    try{const next=await conn.client.get(`/session/${encodeURIComponent(row.session_id)}/artifacts/${encodeURIComponent(row.artifact.id)}?${p}`,conn.controller.signal);if(conn!==ctx().conn || epoch!==generation)return;
      if(!next.delivered?.available || next.delivered.truncated)throw new Error('没有完整交付文本；请使用成果检查查看实际可用版本。');
      if(next.delivered.text.length>40000)throw new Error('交付正文超过页面编辑容量，未截断复制。');
      const format=row.type==='web' ? 'html' : /\.md$/i.test(row.artifact.path) ? 'markdown' : 'text';
      if(preview){artifactPreview={id,body:next.delivered.text,format,version:'已保存交付文本'};notice='';paint();}
      else{edit('page',{id:crypto.randomUUID(),title:row.artifact.path,format,body:next.delivered.text,project_id:row.project_id,space_id:spaceId,references:[id],source:`复制交付作品 ${id}；来源会话 ${row.session_id}；原文件不改写`,expected_version:0});view='pages';paint();}
    }catch(e){if(conn===ctx().conn && epoch===generation){notice=e.message;paint();}}
  }
  let artifactPreview=null,fileDirectory=null,filePreview=null,fileError='',fileLoading=false,fileSequence=0;
  async function browseFiles(pid,directory='',path=null,folder=fileDirectory?.project_id===pid?fileDirectory.folder:''){const conn=ctx().conn,epoch=generation,seq=++fileSequence;if(!conn)return;fileLoading=true;paint();
    try{const p=new URLSearchParams({project_id:pid,...(folder?{folder}:{}),[path===null ? 'directory' : 'path']:path===null ? directory : path});const next=await conn.client.get(`/desktop/content/${path===null ? 'files' : 'file'}?${p}`,conn.controller.signal);if(conn!==ctx().conn || epoch!==generation || seq!==fileSequence)return;
      if(next.project_id!==pid || path===null && !Array.isArray(next.entries) || path!==null && typeof next.text!=='string')throw new Error('项目文件响应不正确。');
      if(path===null){fileDirectory=next;filePreview=null;}else filePreview=next;fileError='';
    }catch(e){if(conn===ctx().conn && epoch===generation && seq===fileSequence)fileError=e.message;}
    finally{if(conn===ctx().conn && epoch===generation && seq===fileSequence){fileLoading=false;paint();}}
  }
  function renderFiles(pid){const d=fileDirectory?.project_id===pid ? fileDirectory : null,f=filePreview?.project_id===pid ? filePreview : null;
    return `<section class="content-files"><div class="section-heading"><h3>项目文件</h3>${(projects().find(p=>p.project_id===pid)?.folders||[]).length>1?`<select data-content-field="file-folder" aria-label="浏览文件夹">${projects().find(p=>p.project_id===pid).folders.map(folder=>`<option value="${h(folder)}" ${folder===(d?.folder||projects().find(p=>p.project_id===pid).workspace)?'selected':''}>${h(folder)}</option>`).join('')}</select>`:''}${button('files','浏览 / 刷新项目根目录',pid,fileLoading)}</div><p class="note">仅在明确打开目录或文件时读取；不递归扫描。隐藏项、链接和常见凭据名称不在浏览范围，引用前仍需核对内容。</p>${fileError ? `<p class="form-error">${h(fileError)}；旧预览可能过期，暂不可引用。</p>` : ''}${d ? `<p class="note">目录：${h(d.directory || '/')} · ${h(d.warning)}</p>${d.directory ? `<button type="button" data-content-action="directory" data-content-id="${h(d.directory.split('/').slice(0,-1).join('/'))}">${icon("arrowLeft")}<span>返回上级</span></button>` : ''}${d.entries.map(entry=>`<div><button type="button" data-content-action="${entry.kind==='directory' ? 'directory' : 'file'}" data-content-id="${h(entry.path)}" aria-label="${entry.kind==='directory' ? '打开目录' : '打开文件'} ${h(entry.name)}">${icon(entry.kind==='directory' ? 'folder' : 'document')}<span>${h(entry.name)}</span></button>${entry.kind==='file' ? `<span class="note"> ${h(entry.size)} 字节</span>` : ''}</div>`).join('')}` : ''}${f ? `<section data-content-file-preview><h4>${h(f.path)}</h4><p class="note">${h(f.size)} 字节 · ${h(f.warning)}${f.truncated ? ' 仅预览前 64 KiB，内容已截断。' : ''}</p>${f.available ? `<textarea data-content-file-text readonly rows="12" aria-label="当前文件文本，可选择引用范围">${h(f.text)}</textarea>${button('reference-file','引用所选文本（未选择则引用预览全文）','',!!fileError || fileLoading)}${ui.openPane ? button('file-pane','在工作区打开','',!!fileError || fileLoading) : ''}` : ''}</section>` : ''}</section>`;
  }
  async function exportPage(id,version){const conn=ctx().conn,epoch=generation;if(!conn)return;try{
    const file=await conn.client.get(`/desktop/content/pages/${encodeURIComponent(id)}/export?version=${version}`,conn.controller.signal);if(conn!==ctx().conn || epoch!==generation)return;
    if(typeof file.text!=='string' || typeof file.filename!=='string')throw new Error('导出响应格式不正确。');
    if(ui.exportFile)await ui.exportFile(file);else if(typeof document!=='undefined'){const blob=new Blob([file.text],{type:'text/plain;charset=utf-8'}),url=URL.createObjectURL(blob),link=document.createElement('a');link.href=url;link.download=file.filename;document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);}else throw new Error('当前宿主未提供文件导出。');
    notice=`已交给文件导出：${file.filename}（保存版本 ${file.version}）；请以系统保存结果为准。`;
  }catch(e){if(conn===ctx().conn && epoch===generation)notice=e.message;}paint();}
  const actionIcons={files:'folder','file-pane':'sidebar',source:'message','artifact-preview':'globe','close-preview':'close','search-result':'search','new-space':'plus','new-page':'plus','new-text':'plus','new-web':'plus',space:'space','edit-space':'edit','clear-space':'document','open-project':'folder','reference-artifact':'library',page:'document','edit-page':'edit','page-pane':'sidebar','edit-project':'edit',export:'export',library:'refresh',reload:'refresh',rebuild:'refresh','copy-version':'copy','artifact-copy':'copy','reference-page':'quote','reference-file':'quote','reference-project':'quote'};
  const button=(action,label,id='',disabled=false)=>`<button type="button" data-content-action="${action}" data-content-id="${h(id)}" ${disabled ? 'disabled' : ''}>${actionIcons[action] ? icon(actionIcons[action]) : ''}<span>${label}</span></button>`;
  const projects=()=>data?.catalogue.projects || [];
  const options=(items,value,empty='不关联')=>`<option value="">${empty}</option>${items.map(p=>`<option value="${h(p.id)}" ${value===p.id ? 'selected' : ''}>${h(p.title)}</option>`).join('')}`;
  const projectOptions=value=>options(projects().map(p=>({id:p.project_id,title:p.name||p.workspace})),value,'所有 / 未指定项目');
  function status(){return `${loading && !data ? '<p class="note">正在读取内容库…</p>' : ''}${error || draftError ? `<p class="form-error">${h(error || draftError)}${data ? ' 以下保留上次快照，可能过期。' : ''}</p>` : ''}${notice ? `<p class="note" role="status">${h(notice)}</p>` : ''}${pending ? `<div class="callout warning"><p>${h(pending.message || '保存中…')}</p><p class="note">${h(pending.body.action)} · 原修订 ${pending.body.revision}。清除回执不撤销服务已保存内容。</p>${button('retry','使用原请求重试','',sending || ['conflict','rejected'].includes(pending.status))}${button('forget','已核对，清除回执','',sending)}</div>` : ''}${placement ? `<div class="callout warning"><p>目标会话已有草稿。请选择如何放入引用：</p>${button('append','追加到草稿')}${button('replace','明确替换草稿')}${button('cancel-place','取消引用')}</div>` : ''}`;}
  function renderLibrary(){const rows=(catalogue?.artifacts || []).filter(r=>(!projectId || r.project_id===projectId) && (!filterType || r.type===filterType) && (!favoritesOnly || data?.state.favorites[r.content_id]));
    return `<div class="section-heading"><h3>登记作品</h3>${button('library','刷新作品','',libraryLoading)}</div><p class="note">只聚合服务登记的交付成果；同一路径的不同轮次分别保留。点击检查后才能确定文件当前是否改动或丢失。</p><div class="actions"><label>项目<select data-content-field="project-filter">${projectOptions(projectId)}</select></label><label>类型<select data-content-field="type-filter">${options(Object.entries(typeLabels).map(([id,title])=>({id,title})),filterType,'全部类型')}</select></label><label><input type="checkbox" data-content-field="favorites-filter" ${favoritesOnly ? 'checked' : ''}>仅收藏</label></div>${libraryError ? `<p class="form-error">${h(libraryError)}；旧列表可能过期。</p>` : ''}${catalogue?.warning ? `<p class="note">${h(catalogue.warning)}</p>` : ''}${catalogue?.truncated ? '<p class="note">作品目录已截断。</p>' : ''}${!catalogue ? '<p class="note">请刷新读取作品目录。</p>' : !rows.length ? '<p class="empty">当前筛选下没有登记作品。</p>' : ''}${rows.map(row=>`<article class="artifact"><span class="badge">${h(typeLabels[row.type] || '文件')}</span>${ui.artifactCard ? ui.artifactCard(row.artifact,{session_id:row.session_id,...row.source}) : `<strong>${h(row.artifact.path)}</strong>`}<p class="note">${h(at(row.created_at))} · 来源 ${h(row.session_id)} · 交付标识 ${h(row.content_id)}</p><div class="actions">${button('source','查看来源会话',row.session_id)}${button('favorite',data?.state.favorites[row.content_id] ? '取消收藏' : '收藏',row.content_id,!pendingAllowed() || !!libraryError)}${['document','web'].includes(row.type) ? button('artifact-copy','交付文本另存页面',row.content_id,!!libraryError) : ''}${row.type==='web' ? button('artifact-preview','受限预览交付网页',row.content_id,!!libraryError) : ''}</div></article>`).join('')}${artifactPreview ? `<section><h3>${h(artifactPreview.version)}</h3>${button('close-preview','关闭预览')}${safePagePreview(artifactPreview.body,artifactPreview.format)}</section>` : ''}<p class="note">PNG/JPEG 可在成果检查中预览；高级图像与办公文档编辑尚未提供。另存页面只编辑应用副本，不改写原项目文件。</p>`;
  }
  function renderSearch(){return `<form data-content-form="search"><label>搜索当前工作台<input data-content-field="query" value="${h(query)}" maxlength="200" required></label><div class="actions"><label>类别<select data-content-field="search-kind">${options(Object.entries(labels).map(([id,title])=>({id,title})),searchKind,'全部内容')}</select></label><label>项目<select data-content-field="project-filter">${projectOptions(projectId)}</select></label><button type="submit" ${searchLoading ? 'disabled' : ''}>搜索</button>${button('rebuild','更新 / 重建索引','',searchLoading)}</div></form><p class="note">只检索当前服务已登记内容；不搜索任意目录、系统提示、凭据或未交付实时轮次。新消息和作品需更新索引。</p>${searchError ? `<p class="form-error">${h(searchError)}；保留上次结果。</p>` : ''}${searchResult ? `<p class="note">${h(searchResult.scope)} · 索引时间 ${h(at(searchResult.indexed_at))} · ${searchResult.total} 条匹配${searchResult.stale ? ' · 内容已变化，请更新索引' : ''}${searchResult.next_cursor!==null ? ' · 仅显示前 200 条，请缩小搜索范围' : ''}</p>${searchResult.warning ? `<p class="note">${h(searchResult.warning)}</p>` : ''}${searchResult.results.map((r,index)=>`<article class="artifact"><span class="badge">${h(labels[r.kind] || r.kind)}</span><h4>${h(r.title)}</h4><pre>${h(r.excerpt)}</pre>${button('search-result','定位来源',String(index))}${r.message_id ? `<span class="note">正文位置 ${h(r.message_id)}</span>` : ''}</article>`).join('')}` : ''}`;}
  function renderSpaces(){const current=data?.state.spaces[spaceId];return `<div class="section-heading"><h3>空间与页面</h3><div class="actions">${button('new-space','新建空间','',!pendingAllowed())}${button('new-page','新建 Markdown 页面','',!pendingAllowed())}${button('new-text','新建文本','',!pendingAllowed())}${button('new-web','新建网页','',!pendingAllowed())}</div></div><p class="note">空间保存持续维护的内容与引用，页面版本与任务执行记录分别管理。保存不会自动送入模型。</p>${Object.values(data?.state.spaces || {}).map(s=>`<article class="artifact"><h4>${h(s.title)}</h4><p>${h(s.description)}</p><p class="note">${Object.values(data.state.pages).filter(p=>p.space_id===s.id && !p.deleted).length} 个页面 · ${s.project_ids.length} 个项目 · ${s.artifact_ids.length} 个作品引用</p>${button('space','打开空间',s.id)}${button('edit-space','编辑空间',s.id,!pendingAllowed())}${button('delete-space','删除空空间',s.id,!pendingAllowed())}</article>`).join('')}${spaceId ? `<p class="note">当前空间：${h(data?.state.spaces[spaceId]?.title || '来源已不存在')}</p>${button('clear-space','查看全部页面')}` : ''}${current ? `<section><h4>空间引用</h4>${current.project_ids.map(id=>button('open-project',h(projects().find(p=>p.project_id===id)?.workspace || id),id)).join('')}${current.artifact_ids.map(id=>{const row=catalogue?.artifacts.find(a=>a.content_id===id);return `<p>${h(row?.artifact.path || id)} ${button('reference-artifact','查看引用作品',id)}</p>`;}).join('')}</section>` : ''}<label>页面项目<select data-content-field="project-filter">${projectOptions(projectId)}</select></label><label><input type="checkbox" data-content-field="deleted-filter" ${showDeleted ? 'checked' : ''}>查看页面回收区</label>${Object.values(data?.state.pages || {}).filter(p=>p.deleted===showDeleted && (!spaceId || p.space_id===spaceId) && (!projectId || p.project_id===projectId)).map(p=>`<article><h4>${h(p.title)} <span class="badge">${h(p.format)} · v${p.version}</span></h4>${button('page','查看页面',p.id)}${p.deleted ? button('restore-page','恢复页面',p.id,!pendingAllowed()) : ''}</article>`).join('')}`;}
  function renderPage(){const page=data?.state.pages[selectedPage];if(!page)return '';
    const selected=historyVersion ? page.versions.find(v=>v.number===historyVersion) : page.versions.at(-1);if(!selected)return '';
    return `<article class="content-page"><h3>${h(selected.title)}</h3><p class="note">${page.deleted ? '回收区 · ' : ''}保存版本 ${selected.number} / ${page.version} · ${h(at(selected.created_at))} · ${h(selected.source)}</p><div class="actions">${button('edit-page','编辑当前版本',page.id,!pendingAllowed() || page.deleted)}${button('reference-page','引用此版本到任务',page.id,!!error)}${button('export','导出此版本',page.id)}${ui.openPane ? button('page-pane','在工作区打开',page.id) : ''}${button('delete-page','移入回收区',page.id,!pendingAllowed() || page.deleted)}</div><label>版本<select data-content-field="history-version">${page.versions.map(v=>`<option value="${v.number}" ${v.number===selected.number ? 'selected' : ''}>v${v.number} · ${h(at(v.created_at))}</option>`).join('')}</select></label>${historyVersion && historyVersion!==page.version ? button('copy-version','以此历史版本起草新版本',page.id,!pendingAllowed() || page.deleted) : ''}${safePagePreview(selected.body,selected.format)}<details><summary>项目 / 作品来源引用</summary><p>${h(page.project_id || '未关联项目')} · ${h(page.space_id || '未关联空间')}</p>${page.references.map(id=>`<p>${h(id)} ${button('reference-artifact','查看登记作品',id)}</p>`).join('')}</details></article>`;
  }
  function renderProject(pid){const p=projects().find(p=>p.project_id===pid);if(!p)return '<p class="note">请选择一个已登记项目。</p>';const d=data?.state.projects[pid] || {instructions:'',materials:[]};
    return `<section><div class="section-heading"><h3>项目指令与资料</h3>${button('edit-project','编辑项目资料',pid,!pendingAllowed())}</div><p class="note">${h(p.name||p.workspace)}。这些内容只在明确引用并发送后进入模型；更新不会改写已有会话上下文。</p><h4>项目指令</h4><pre>${h(d.instructions || '尚未保存项目指令。')}</pre>${button('reference-project','将已保存指令与资料引用到任务',pid,!!error || !d.instructions && !d.materials.length)}${d.materials.map(m=>`<article class="artifact"><h4>${h(m.title)}</h4><p class="note">${h(m.source)}</p><pre>${h(m.text)}</pre></article>`).join('')}<h4>关联页面</h4>${Object.values(data?.state.pages || {}).filter(page=>!page.deleted && page.project_id===pid).map(page=>button('page',h(page.title),page.id)).join('')}${button('new-page','新建项目页面','',!pendingAllowed())}${renderFiles(pid)}</section>`;
  }
  function editor(){if(!draft)return '';const d=draft.value;let form='';
    if(draft.kind==='page')form=`<label>名称<input data-content-edit="title" value="${h(d.title)}" maxlength="200" required></label><label>类型<select data-content-edit="format">${options([{id:'markdown',title:'Markdown'},{id:'text',title:'纯文本'},{id:'html',title:'受限网页'}],d.format).replace('<option value="">不关联</option>','')}</select></label><label>项目<select data-content-edit="project_id">${projectOptions(d.project_id)}</select></label><label>空间<select data-content-edit="space_id">${options(Object.values(data?.state.spaces || {}).map(s=>({id:s.id,title:s.title})),d.space_id)}</select></label><label>来源说明<input data-content-edit="source" value="${h(d.source)}" maxlength="1000"></label><label>正文<textarea data-content-edit="body" rows="14" maxlength="40000">${h(d.body)}</textarea></label><p class="note">以保存版本 ${d.expected_version} 为基线。原项目文件不会改写；编辑副本保留来源。</p>${data?.state.pages[d.id] && data.state.pages[d.id].version!==d.expected_version ? `<div class="callout warning">当前页面已变为 v${data.state.pages[d.id].version}。${button('rebase-page','已核对，以当前版本为基线保存我的草稿','',!!pending)}</div>` : ''}<details><summary>作品引用（不自动复制内容）</summary>${(catalogue?.artifacts || []).slice(0,100).map(a=>`<label><input type="checkbox" data-content-reference="${h(a.content_id)}" ${d.references.includes(a.content_id) ? 'checked' : ''}>${h(a.artifact.path)} · ${h(a.session_id)}</label>`).join('')}${(catalogue?.artifacts.length || 0)>100 ? '<p class="note">选择列表仅展示前 100 条；原有引用仍保留。</p>' : ''}</details><details><summary>草稿预览（尚未保存）</summary>${safePagePreview(d.body,d.format)}</details>`;
    if(draft.kind==='space')form=`<label>名称<input data-content-edit="title" value="${h(d.title)}" maxlength="200" required></label><label>说明<textarea data-content-edit="description" rows="3" maxlength="5000">${h(d.description)}</textarea></label><h4>项目引用</h4>${projects().map(p=>`<label><input type="checkbox" data-content-project-ref="${h(p.project_id)}" ${d.project_ids.includes(p.project_id) ? 'checked' : ''}>${h(p.name||p.workspace)}</label>`).join('')}<h4>作品引用</h4>${(catalogue?.artifacts || []).slice(0,100).map(a=>`<label><input type="checkbox" data-content-space-artifact="${h(a.content_id)}" ${d.artifact_ids.includes(a.content_id) ? 'checked' : ''}>${h(a.artifact.path)}</label>`).join('')}`;
    if(draft.kind==='project')form=`<label>项目指令<textarea data-content-edit="instructions" rows="5" maxlength="20000">${h(d.instructions)}</textarea></label><h4>资料（粘贴文本；来源仅记录，不自动抓取网址或文件）</h4>${d.materials.map((m,i)=>`<fieldset><legend>资料 ${i+1}</legend><label>标题<input data-content-material="${i}" data-material-field="title" value="${h(m.title)}" maxlength="200" required></label><label>来源说明<input data-content-material="${i}" data-material-field="source" value="${h(m.source)}" maxlength="1000"></label><label>正文<textarea data-content-material="${i}" data-material-field="text" rows="4" maxlength="20000">${h(m.text)}</textarea></label>${button('remove-material','移除此条资料',String(i))}</fieldset>`).join('')}${button('add-material','添加资料','',d.materials.length>=30)}<p class="note">单份可恢复编辑草稿总计不超过 95000 字符；过大内容请拆成页面。</p>`;
    return `<form data-content-form="save" class="automation-form"><h3>编辑${h(labels[draft.kind])}（独立草稿）</h3>${form}<div class="actions"><button type="submit" ${!pendingAllowed() ? 'disabled' : ''}>保存${draft.kind==='page' ? '为新版本' : ''}</button>${button('discard','明确放弃编辑草稿','',!!pending || sending)}</div></form>`;
  }
  function render(nextView=view,options={}){restore();view=nextView;if(Object.hasOwn(options,'projectId'))projectId=options.projectId || '';
    const body=view==='search' ? renderSearch() : view==='project' ? renderProject(projectId) : view==='page-panel' ? renderPage() : view==='spaces' || view==='pages' ? renderSpaces()+renderPage() : renderLibrary();
    return `<section class="content-workspace"><div class="section-heading"><h2>${h({library:'作品',search:'统一搜索',spaces:'空间',pages:'页面','page-panel':'页面编辑器',project:'项目资料'}[view] || '内容')}</h2>${button('reload','刷新内容','',loading)}</div>${status()}${body}${editor()}</section>`;
  }
  return {load,loadLibrary,search,dispatch,render,openPage,openProject,renderProject,
    ready:()=>!!data, currentView:()=>view,
    reset(){generation++;searchSequence++;fileSequence++;fileDirectory=null;filePreview=null;fileError='';fileLoading=false;data=null;error='';loading=false;restored=false;draft=null;draftError='';pending=null;sending=false;notice='';catalogue=null;libraryError='';libraryLoading=false;searchResult=null;searchError='';searchLoading=false;selectedPage='';historyVersion=0;placement=null;artifactPreview=null;projectId='';spaceId='';query='';searchKind='';},
    input(e){if(e.target.dataset.contentField==='file-folder'){browseFiles(projectId,'',null,e.target.value);return true;}const n=e.target,k=n.dataset.contentEdit;if(k && draft){if(Object.hasOwn(draft.value,k) && typeof draft.value[k]==='string'){draft.value[k]=n.value;persist();if(['format','project_id','space_id'].includes(k))paint();}return true;}
      const field=n.dataset.contentField;if(field){if(field==='query')query=n.value;if(field==='search-kind')searchKind=n.value;if(field==='project-filter')projectId=n.value;if(field==='type-filter')filterType=n.value;if(field==='favorites-filter')favoritesOnly=n.checked;if(field==='deleted-filter')showDeleted=n.checked;if(field==='history-version')historyVersion=Number(n.value);if(field!=='query')paint();return true;}
      if(n.dataset.contentMaterial!==undefined && draft?.kind==='project'){const m=draft.value.materials[Number(n.dataset.contentMaterial)];if(m && ['title','text','source'].includes(n.dataset.materialField)){m[n.dataset.materialField]=n.value;persist();}return true;}
      const ref=n.dataset.contentReference,pr=n.dataset.contentProjectRef,ar=n.dataset.contentSpaceArtifact;
      if(ref && draft?.kind==='page' || pr && draft?.kind==='space' || ar && draft?.kind==='space'){const key=ref ? 'references' : pr ? 'project_ids' : 'artifact_ids',id=ref || pr || ar;draft.value[key]=n.checked ? [...new Set([...draft.value[key],id])] : draft.value[key].filter(v=>v!==id);persist();return true;}return false;},
    onSubmit(e){const action=e.target.dataset.contentForm;if(!action)return false;e.preventDefault();if(action==='search'){search();return true;}if(action==='save' && draft){if(draft.kind==='page' && (!draft.value.title.trim() || draft.value.body.length>40000)){notice='请填写标题；正文最多 40000 字符。';paint();return true;}request({page:'page_save',space:'space_save',project:'project_save'}[draft.kind],structuredClone(draft.value));}return true;},
    click(e){const n=e.target.closest('[data-content-action]');if(!n)return false;const a=n.dataset.contentAction,id=n.dataset.contentId;
      if(a==='page-pane' && data?.state.pages[id] && ui.openPane)ui.openPane({id:`page:${id}`,kind:'page',title:data.state.pages[id].title,source:{page_id:id}});
      if(a==='file-pane' && filePreview?.project_id===projectId && !fileError && ui.openPane && typeof document!=='undefined'){
        const f=structuredClone(filePreview),p=projects().find(p=>p.project_id===projectId),node=document.createElement('section');
        node.innerHTML=`<h3>${h(f.path)}</h3><p class="note">当前文件只读快照 · ${h(at(f.modified_at))}${f.truncated ? ' · 已截断到前 64 KiB' : ''}。关闭视图不改变项目文件。</p><textarea readonly rows="16" aria-label="文件文本，可选取引用">${h(f.text)}</textarea><button type="button">将选中内容引用到任务（未选择则全文）</button>`;
        const feedback=document.createElement('div');feedback.setAttribute('role','status');node.append(feedback);
        node.querySelector('button').addEventListener('click',()=>{
          const target=ctx(),area=node.querySelector('textarea'),start=area.selectionStart,end=area.selectionEnd,text=end>start ? f.text.slice(start,end) : f.text;
          const reference=`项目文件引用（手动选取，尚未发送）\n项目 ${p?.workspace || f.project_id}\n文件 ${f.folder?f.folder+'/':''}${f.path} · 读取时修改时间 ${at(f.modified_at)}\n${end>start ? `字符范围 ${start}–${end}` : '预览全文'}${f.truncated ? ' · 已截断' : ''}\n\n${text}`;
          const commit=mode=>{const current=ctx();if(!current.sid || current.active===false || current.sid!==target.sid || current.conn!==target.conn){feedback.textContent='目标会话不可用或已变化，请重新选择引用。';return;}const merged=mergeContentDraft(ui.getComposer(),reference,mode);if(merged===null){feedback.innerHTML='<p>任务已有草稿，请明确选择：</p><button type="button" data-choice="append">追加</button><button type="button" data-choice="replace">明确替换</button>';for(const b of feedback.querySelectorAll('button'))b.addEventListener('click',()=>commit(b.dataset.choice));}else{ui.putComposer(merged);feedback.textContent='文件引用已放入任务草稿，尚未发送。';}};
          commit();
        });
        ui.openPane({id:`file:${projectId}:${f.path}`,kind:'file',node,title:f.path,source:{project_id:projectId,path:f.path}});
      }
      if(a==='files')browseFiles(id);if(a==='directory')browseFiles(projectId,id);if(a==='file')browseFiles(projectId,'',id);
      if(a==='reference-file' && filePreview?.project_id===projectId && filePreview.available && !fileError && !fileLoading){const area=n.closest?.('[data-content-file-preview]')?.querySelector?.('[data-content-file-text]'),start=area?.selectionStart || 0,end=area?.selectionEnd || 0;const selected=end>start ? filePreview.text.slice(start,end) : filePreview.text;place(`项目文件引用（手动选取，尚未发送）\n项目 ${projects().find(p=>p.project_id===projectId)?.workspace || projectId}\n文件 ${filePreview.folder?filePreview.folder+'/':''}${filePreview.path} · 读取时修改时间 ${at(filePreview.modified_at)}\n${end>start ? `字符范围 ${start}–${end}` : '预览全文'}${filePreview.truncated ? ' · 文件预览已截断，不是完整文件' : ''}\n\n${selected}`);}
      if(a==='reload')load();if(a==='library')loadLibrary();if(a==='retry')dispatch();if(a==='forget')discardReceipt();if(a==='rebuild')search(true);
      if(a==='source')ui.openSession(id);if(a==='favorite'){const row=catalogue?.artifacts.find(v=>v.content_id===id);if(row && !libraryError)request('favorite',{id,favorite:!data.state.favorites[id]});}
      if(a==='artifact-copy')fromArtifact(id);if(a==='artifact-preview')fromArtifact(id,true);if(a==='close-preview'){artifactPreview=null;paint();}
      if(a==='new-page' || a==='new-text' || a==='new-web'){view='pages';newPage(a==='new-text' ? 'text' : a==='new-web' ? 'html' : 'markdown');}
      if(a==='open-project')openProject(id);if(a==='page')openPage(id);if(a==='space'){spaceId=id;view='spaces';paint();}if(a==='clear-space'){spaceId='';paint();}
      if(a==='new-space')edit('space',{id:crypto.randomUUID(),title:'新空间',description:'',project_ids:projectId ? [projectId] : [],artifact_ids:[]});
      if(a==='edit-space' && data?.state.spaces[id]){const s=data.state.spaces[id];edit('space',{id,title:s.title,description:s.description,project_ids:[...s.project_ids],artifact_ids:[...s.artifact_ids]});}
      if(a==='delete-space')request('space_delete',{id});if(a==='delete-page')request('page_delete',{id});if(a==='restore-page')request('page_restore',{id});
      if(a==='edit-page' && data?.state.pages[id] && !data.state.pages[id].deleted)edit('page',pageValue(data.state.pages[id]));
      if(a==='copy-version' && data?.state.pages[id]){const p=data.state.pages[id],v=p.versions.find(v=>v.number===historyVersion);if(v)edit('page',{...pageValue(p),body:v.body,title:v.title,format:v.format,source:`以页面 ${id} 的历史 v${v.number} 起草；${v.source}`.slice(0,1000)});}
      if(a==='rebase-page' && draft?.kind==='page' && !pending && data?.state.pages[draft.value.id]){draft.value.expected_version=data.state.pages[draft.value.id].version;persist();notice='已更新保存基线；你的草稿仍保留，下一次保存将新增版本。';paint();}
      if(a==='discard' && !pending && !sending){draft=null;store()?.deleteDraft(DRAFT);notice='已放弃此编辑草稿；保存版本仍保留。';paint();}
      if(a==='reference-page' && data?.state.pages[id]){const p=data.state.pages[id],v=p.versions.find(v=>v.number===(historyVersion || p.version));if(v)place(contentReference({...p,...v,version:v.number}));}
      if(a==='reference-artifact'){view='library';projectId='';filterType='';favoritesOnly=false;notice=`引用作品标识 ${id}；可按来源检查对应交付。`;loadLibrary();paint();}
      if(a==='export' && data?.state.pages[id])exportPage(id,historyVersion || data.state.pages[id].version);
      if(a==='append' || a==='replace')placement && place(placement.text,a);if(a==='cancel-place'){placement=null;paint();}
      if(a==='edit-project' && projects().some(p=>p.project_id===id))edit('project',{id,...structuredClone(data.state.projects[id] || {instructions:'',materials:[]})});
      if(a==='add-material' && draft?.kind==='project' && draft.value.materials.length<30){draft.value.materials.push({id:crypto.randomUUID(),title:'',text:'',source:''});persist();paint();}
      if(a==='remove-material' && draft?.kind==='project'){draft.value.materials.splice(Number(id),1);persist();paint();}
      if(a==='reference-project'){const d=data?.state.projects[id],p=projects().find(p=>p.project_id===id);if(d && p)place(`项目资料引用（明确加入本次任务，尚未发送）\n${p.workspace}\n内容库修订 ${data.state.revision}\n\n${d.instructions}\n\n${d.materials.map(m=>`${m.title}\n来源：${m.source}\n${m.text}`).join('\n\n')}`);}
      if(a==='search-result'){const r=searchResult?.results[Number(id)];if(r){if(r.kind==='page')openPage(r.id);else if(r.kind==='space'){spaceId=r.id;view='spaces';paint();}else if(['project','material'].includes(r.kind))openProject(r.project_id);else if(r.session_id)ui.openSession(r.session_id,{messageId:r.message_id,artifactId:r.artifact_id,source:r.source});}}
      return true;},
  };
}
