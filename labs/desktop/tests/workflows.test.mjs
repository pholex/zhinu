import test from "node:test";
import assert from "node:assert/strict";
import {renderWeaveForm, renderWeaves, renderArtifactInspection, renderArtifactImage} from "../web/workflows.mjs";

const draft = () => ({mode:"new", spec:"reader", prompt_template:"检查 {{item}}", items:[{id:"a",text:"项目一"},{id:"b",text:"项目二"}]});
const capabilities = () => ({available:true, can_start:true, can_resume:true, min_items:2, max_items:64,
  specs:[{name:"reader", description:"读取资料", readonly:true}],
  batches:[{batch_id:"batch", items:[{id:"item",can_resume:true,resume_reason:""}]}]});
const preview = () => ({spec:"reader", count:2, items:[{label:"项目一",task:"检查项目一"},{label:"项目二",task:"检查项目二"}]});
const batchState = overrides => ({items:[{batch_id:"batch",spec:"reader",final:true,
  items:[{id:"item",index:0,label:"项目一",status:"completed",answer:"原结果",model:"scripted"}],...overrides}],cursor:null,total:1});
const artifact = overrides => ({key:'{"session_id":"one"}', data:{artifact:{path:"result.md"},version:"delivered",
  delivered:{available:true,text:"交付文本"},current:{available:true,text:"当前文本"},
  diff:{available:false,note:"没有改前内容，无法比较。"},can_reveal:true,...overrides}});

test("batch form exposes only configured specs and requires a preview before confirmation", () => {
  assert.equal(renderWeaveForm({open:false},capabilities()), "");
  const state = {open:true,draft:draft()};
  const before = renderWeaveForm(state,capabilities());
  assert.match(before, /value="reader" selected/);
  assert.match(before, /读取资料/);
  assert.doesNotMatch(before, /data-weave-submit/);
  const after = renderWeaveForm({...state,preview:preview()},capabilities());
  assert.match(after, /确认 2 项任务/);
  assert.match(after, /检查项目二/);
  assert.match(after, /data-weave-submit\s*>/);
});

test("busy and unavailable capabilities keep task drafts visible but prevent execution", () => {
  const state = {open:true,draft:draft(),preview:preview()};
  const busy = renderWeaveForm(state,capabilities(),true);
  assert.match(busy, /data-weave-submit disabled/);
  assert.match(busy, /type="submit" disabled/);
  assert.match(busy, /项目一<\/textarea>/);
  const unavailable = renderWeaveForm(state,{...capabilities(),available:false,can_start:false,specs:[],reason:"当前会话没有已加载的织手。"});
  assert.match(unavailable, /没有已加载的织手/);
  assert.doesNotMatch(unavailable, /value="reader"/);
  assert.match(unavailable, /type="submit" disabled/);
  assert.match(unavailable, /data-weave-submit disabled/);
});

test("task drafts, preview content, and errors remain inert text", () => {
  const value = '</textarea><img src=x onerror="bad()">';
  const state = {open:true,error:value,draft:{...draft(),prompt_template:value,items:[{id:"a",text:value},{id:"b",text:"two"}]},
    preview:{spec:value,count:2,items:[{label:value,task:'<script>bad()</script>'}]}};
  const html = renderWeaveForm(state,capabilities());
  assert.doesNotMatch(html, /<img|<script>/);
  assert.match(html, /&lt;img/);
  assert.match(html, /&lt;script&gt;/);
});

test("resume view retains the exact source and supplement without new-task fields", () => {
  const html = renderWeaveForm({open:true,draft:{mode:"resume",source_batch_id:"old-batch",item_ids:["i1","i2"],instruction:"补齐引用"}},capabilities());
  assert.match(html, /来源批次：old-batch · 已选 2 项/);
  assert.match(html, /补齐引用<\/textarea>/);
  assert.match(html, /旧结果保留/);
  assert.doesNotMatch(html, /id="weave-spec"|id="weave-template"/);
});

test("empty batches do not create a placeholder and unavailable resume selections cannot submit", () => {
  assert.equal(renderWeaves({items:[],cursor:null}), "");
  const caps = capabilities();
  caps.batches[0].items[0] = {id:"item",can_resume:false,resume_reason:"子历史已不存在。"};
  const html = renderWeaves(batchState(),{capabilities:caps,selections:new Map([["batch",new Set(["item"])]]),canSubmit:true});
  assert.match(html, /子历史已不存在/);
  assert.match(html, /data-resume-item="item"\s+disabled/);
  assert.match(html, /data-resume-weave="batch" disabled/);
  assert.doesNotMatch(html, /data-resume-item="item"[^>]*checked/);
  const eligible = renderWeaves(batchState(),{capabilities:capabilities(),selections:new Map([["batch",new Set(["item"])]]),canSubmit:true});
  assert.match(eligible, /data-resume-weave="batch"\s*>/);
});

test("resume lineage and interrupted handoff content are explicitly represented", () => {
  const state = batchState({source_batch_id:"prior",source_item_ids:["prior-item"]});
  state.items.push({batch_id:"prior",spec:"reader",final:true,items:[{id:"prior-item",label:"原任务名称",status:"completed",answer:"旧答案"}]});
  const lineage = renderWeaves(state);
  assert.match(lineage, /查看续跑来源/);
  assert.match(lineage, /来源批次：prior/);
  assert.match(lineage, /<li>原任务名称<\/li>/);
  assert.match(lineage, /旧答案/);
  const interrupted = renderWeaves(batchState({interrupted_by_restart:true,final:false,items:[{id:"item",status:"completed",answer:""}]}));
  assert.match(interrupted, /重启中断/);
  assert.match(interrupted, /交接正文未记录/);
  assert.match(interrupted, /不会自动重跑/);
});

test("artifact display distinguishes delivery from changed current file and missing baseline", () => {
  const html = renderArtifactInspection(artifact({version:"current_changed"}));
  assert.match(html, /交付后已变化/);
  assert.match(html, /交付版本/);
  assert.match(html, /交付文本/);
  assert.match(html, /当前文件（读取时快照）/);
  assert.match(html, /当前文本/);
  assert.match(html, /没有改前内容，无法比较/);
  assert.match(renderArtifactInspection(artifact({version:"missing",current:{available:false}})), /当前文件已不存在/);
  assert.match(renderArtifactInspection(artifact({version:"unavailable"})), /当前文件不可读取/);
});

test("artifact markdown, diff, and warning never execute markup or external resources", () => {
  const html = renderArtifactInspection(artifact({
    delivered:{available:true,text:'<img src="https://invalid.example/x" onerror="bad()">',truncated:true},
    diff:{available:true,text:'- <script>bad()</script>',label:"改前 → 交付",truncated:true},
    warning:'<iframe src="https://invalid.example">',
  }));
  assert.doesNotMatch(html, /<img|<script>|<iframe/);
  assert.match(html, /&lt;img/);
  assert.match(html, /正文已截断/);
  assert.match(html, /差异内容已截断/);
});

test("Finder action requires native capability and server permission; failed refresh labels stale data", () => {
  assert.doesNotMatch(renderArtifactInspection(artifact(),{canReveal:false}), /data-artifact-reveal/);
  assert.doesNotMatch(renderArtifactInspection(artifact({can_reveal:false}),{canReveal:true}), /data-artifact-reveal/);
  assert.match(renderArtifactInspection(artifact(),{canReveal:true}), /data-artifact-reveal/);
  assert.match(renderArtifactInspection({...artifact(),error:"连接中断"}), /下方保留上次读取的快照/);
  assert.match(renderArtifactInspection({loading:true}), /正在检查成果文件/);
});

const previewImage = () => ({available:true,mime_type:"image/png",width:100,height:50,data_url:"data:image/png;base64,AAAA",version:"delivered",sha256:"sha",note:"读取时快照"});
test("image preview distinguishes delivered and changed current version without external image sources",()=>{
  const delivered=renderArtifactImage(previewImage());assert.match(delivered,/<img /);assert.match(delivered,/与交付时一致/);assert.match(delivered,/读取时快照/);
  const changed=renderArtifactImage({...previewImage(),version:"current_changed"});assert.match(changed,/当前变更版本（非交付原图）/);assert.equal((changed.match(/<img /g)||[]).length,1);
});
test("image preview rejects unsupported type, payload, dimensions and unverified versions",()=>{
  for(const patch of [{mime_type:"image/svg+xml",data_url:"data:image/svg+xml;base64,AAAA"},{data_url:"https://example.invalid/a.png"},{data_url:'data:image/png;base64,AAAA" onerror="bad()'},{version:"unavailable"},{width:8193},{width:8192,height:8192},{width:0},{data_url:"data:image/png;base64,"+"A".repeat(2796228)}]) assert.doesNotMatch(renderArtifactImage({...previewImage(),...patch}),/<img /);
  assert.match(renderArtifactImage({...previewImage(),mime_type:"image/jpeg",data_url:"data:image/jpeg;base64,AAAA"}),/<img /);
});
test("image warning remains inert and an unavailable image never invents an older delivery blob",()=>{
  const html=renderArtifactImage({...previewImage(),available:false,version:"missing",note:"<script>"});assert.doesNotMatch(html,/<img |<script>/);assert.match(html,/&lt;script&gt;/);
  const inspected=renderArtifactInspection({key:"image",data:{artifact:{path:"a.png"},version:"current_changed",image:{...previewImage(),version:"current_changed"},delivered:{available:false},current:{available:false},diff:{available:false,note:"没有改前文件"}}});assert.match(inspected,/非交付原图/);assert.match(inspected,/没有改前文件/);assert.equal((inspected.match(/<img /g)||[]).length,1);
});
