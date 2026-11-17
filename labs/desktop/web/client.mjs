// Explicit loopback HTTP consumer; execution policy stays in the service.
export function endpoint(raw) {
  let url;
  try { url = new URL(raw); } catch { throw new Error("请输入完整的本机服务地址，如 http://127.0.0.1:8420"); }
  if (url.protocol !== "http:" || !["127.0.0.1", "localhost", "[::1]"].includes(url.hostname)
      || url.username || url.password || url.search || url.hash || url.pathname !== "/") {
    throw new Error("只连接本机 HTTP 服务；地址中不能包含凭据、路径或查询参数。");
  }
  return url.origin;
}

export class ReadClient {
  constructor(base, token = "", transport = (...args) => fetch(...args)) {
    this.base = endpoint(base);
    this.token = token;
    this.transport = transport;
  }

  get(path, signal) { return this.request("GET", path, undefined, signal); }
  post(path, body, {key = "", instance = "", signal} = {}) {
    return this.request("POST", path, body, signal, key, instance);
  }

  async request(method, path, body, signal, key = "", instance = "") {
    const read = /^\/(health|desktop\/(?:state|workspace|projects\/[^/?]+\/artifacts)|catalog\/sessions(?:\/[^/?]+\/history)?|session(?:\/[^/?]+(?:\/(?:status|events|history|runs|fleets(?:\/[^/?]+\/missions\/[^/?]+\/changes)?|fleet-commands\/capabilities|(?:fleet|weave)-operations\/[^/?]+|weaves(?:\/capabilities)?|contests(?:\/capabilities)?|artifacts\/[^/?]+|questions(?:\/[^/?]+)?))?)?)(?:\?[^#]*)?$/;
    const write = /^\/(?:desktop\/(?:state|workspace)|session(?:\/[^/?]+\/(?:prompt_async|steer|abort|permissions|weaves(?:\/preview)?|contests(?:\/preview|\/[^/?]+\/selection)?|fleet-commands(?:\/preview)?|weaves\/[^/?]+\/items\/[^/?]+\/(?:messages|stop)|fleets\/[^/?]+\/members\/[^/?]+\/(?:messages|stop)|questions\/[^/?]+\/answers))?)$/;
    const managementRead = /^\/desktop\/(?:automations|connections(?:\/[^/?]+\/authorizations\/[^/?]+)?)$/;
    const managementWrite = /^\/desktop\/(?:automations\/(?:configure|[^/?]+\/enabled)|automation-runs\/[^/?]+\/acknowledge|connections(?:\/[^/?]+\/(?:delete|revoke|authorizations(?:\/[^/?]+\/cancel)?))?)$/;
    const sessionConnections = /^\/session\/[^/?]+\/connections(?:\/[^/?]+)?$/;
    const productRead = /^\/desktop\/(?:usage|data(?:\/backup(?:\?kind=(?:preferences|workspace))?)?|preferences|providers|capabilities(?:\?[^#]*)?|sessions\/[^/?]+\/export(?:\?[^#]*)?|content(?:\/(?:library|search|files|file|pages\/[^/?]+\/export))?(?:\?[^#]*)?|terminals(?:\/[^/?]+\/output(?:\?[^#]*)?)?)$/;
    const productWrite = /^\/desktop\/(?:archived-sessions\/delete|data\/(?:clear-index|import)|preferences|providers|sessions\/[^/?]+\/(?:title|model|skills\/reload)|skills\/(?:reload|settings)|content(?:\/search\/rebuild)?|terminals(?:\/[^/?]+\/(?:input|resize|stop))?)$/;
    const allowedManagement = (method === "GET" ? managementRead : managementWrite).test(path)
      || (method === "GET" ? productRead : productWrite).test(path)
      || sessionConnections.test(path) && (method === "GET" ? path.endsWith("/connections") : !path.endsWith("/connections"));
    if ((!(method === "GET" ? read : write).test(path) && !allowedManagement) || (path.includes("#") || /%2f|%5c|%2e|\.\.|\\/i.test(path.split("?")[0]))) {
      throw new Error("不支持的工作台请求路径");
    }
    const controller = new AbortController();
    const cancel = () => controller.abort();
    signal?.addEventListener("abort", cancel, {once: true});
    if (signal?.aborted) cancel();
    const timeout = setTimeout(cancel, path.includes("wait=") ? 30000 : 10000);
    try {
      const headers = {Accept: "application/json"};
      if (this.token) headers.Authorization = `Bearer ${this.token}`;
      if (body !== undefined) headers["Content-Type"] = "application/json";
      if (key) headers["Idempotency-Key"] = key;
      if (instance) headers["X-Xiaoyu-Instance"] = instance;
      const response = await this.transport(this.base + path, {
        method, headers, signal: controller.signal,
        ...(body === undefined ? {} : {body: JSON.stringify(body)}),
        cache: "no-store", credentials: "omit", redirect: "error",
      });
      if (!response.ok) {
        const error = new Error(response.status === 401 ? "令牌缺失或不正确，请重新连接。"
          : response.status === 403 ? "服务拒绝此来源或工作目录，请核对配置。"
          : response.status === 404 ? "会话、审批或问题已不存在，请刷新状态。"
          : response.status === 409 ? "状态已变化或请求已处理，请核对最新状态。"
          : response.status === 423 ? "这场会话已在终端中打开。请先在那边结束它，再回来重试。"
          : path === "/desktop/state" && [400,422].includes(response.status) ? "应用未能保存草稿和待提交记录：保存格式不兼容，请更新应用后重试。"
          : path === "/desktop/state" && response.status === 413 ? "草稿和待提交记录超出保存容量，请保留输入并清理不再需要的草稿。"
          : response.status === 400 || response.status === 422 ? "请求未被接受，请检查工作目录和输入内容。"
          : `服务返回 HTTP ${response.status}；提交结果可能未知，请先刷新状态。`);
        error.status = response.status;
        // Only a fixed machine code is consumed. Arbitrary error bodies may contain credentials.
        if (response.status === 409) {
          const data = await response.json().catch(() => ({}));
          if (data.detail?.code === "submission_recorded") error.recorded = true;
          if(path === "/desktop/archived-sessions/delete") {
            const messages={session_delete_busy:"所选对话仍有任务在执行，请结束任务后再删除。",session_delete_unowned:"无法确认会话记录的写入状态，请重启应用后再试。",archive_delete_changed:"归档列表已变化，请刷新后重新选择要删除的对话。"};
            if(messages[data.detail?.code])error.message=messages[data.detail.code];
          }
        }
        throw error;
      }
      return await response.json();
    } catch (error) {
      if (signal?.aborted) throw new DOMException("Cancelled", "AbortError");
      if (controller.signal.aborted) throw new Error("请求超时；写入结果未知，请保留原请求并核对状态。");
      if (error instanceof TypeError) throw new Error("连接中断；写入结果未知，请检查服务后使用原请求重试。");
      throw error;
    } finally {
      clearTimeout(timeout);
      signal?.removeEventListener("abort", cancel);
    }
  }
}

// Native application state is scoped by the backend workspace, independent of its port.
// Only drafts and action receipts are saved; connection credentials stay in ReadClient.
export class LocalState {
  static validate(state) {
    if (!state || !Number.isInteger(state.revision) || state.revision < 0
        || !state.drafts || Array.isArray(state.drafts) || typeof state.drafts !== "object"
        || !state.outbox || Array.isArray(state.outbox) || typeof state.outbox !== "object"
        || Object.values(state.drafts).some(v => typeof v !== "string")) {
      throw new Error("工作台保存状态无法识别；没有覆盖原数据。");
    }
    return state;
  }
  static async load(client, signal) {
    return new LocalState(client, LocalState.validate(await client.get("/desktop/state", signal)), signal);
  }
  static value(map, key) { return Object.hasOwn(map, key) ? map[key] : undefined; }
  static equal(a, b) {
    if (a === b) return true;
    if (!a || !b || typeof a !== "object" || typeof b !== "object") return false;
    const keys = Object.keys(a), other = Object.keys(b);
    return keys.length === other.length && keys.every(k => Object.hasOwn(b, k) && LocalState.equal(a[k], b[k]));
  }
  constructor(client, state, signal) {
    LocalState.validate(state);
    this.client = client; this.signal = signal; this.revision = state.revision;
    this.drafts = structuredClone(state.drafts); this.outbox = structuredClone(state.outbox);
    this.base = {drafts: structuredClone(state.drafts), outbox: structuredClone(state.outbox)};
    this.storageView = structuredClone(state.outbox); this.conflicts = [];
    this.generation = 0; this.savedGeneration = 0; this.flushing = null; this.timer = null;
    this.onError = () => {}; this.onSaved = () => {};
    this.storage = {
      getItem: () => { this.storageView = structuredClone(this.outbox); return JSON.stringify(this.outbox); },
      setItem: (_key, value) => {
        const next = JSON.parse(value);
        // An Outbox may still hold a pre-merge snapshot. Apply only changes that
        // it made, preserving other windows' unrelated receipts.
        for (const key of new Set([...Object.keys(this.storageView), ...Object.keys(next)])) {
          if (LocalState.equal(LocalState.value(this.storageView, key), LocalState.value(next, key))) continue;
          if (!LocalState.equal(LocalState.value(this.storageView, key), LocalState.value(this.outbox, key)) && !LocalState.equal(LocalState.value(next, key), LocalState.value(this.outbox, key))
              && !this.conflicts.some(c => c.group === "outbox" && c.key === key)) {
            this.conflicts.push({group: "outbox", key, local: structuredClone(next[key]), remote: structuredClone(LocalState.value(this.outbox, key))});
          }
          if (Object.hasOwn(next, key)) Object.defineProperty(this.outbox, key, {value: structuredClone(next[key]), enumerable: true, writable: true, configurable: true});
          else delete this.outbox[key];
        }
        this.storageView = structuredClone(next); this.changed();
      },
      flush: () => this.flush(),
    };
  }
  getDraft(sid) { return Object.hasOwn(this.drafts, sid) ? this.drafts[sid] : ""; }
  setDraft(sid, text) {
    if (!sid || typeof text !== "string" || this.getDraft(sid) === text) return;
    if (text) Object.defineProperty(this.drafts, sid, {value: text, enumerable: true, writable: true, configurable: true});
    else delete this.drafts[sid];
    this.changed();
  }
  deleteDraft(sid) { this.setDraft(sid, ""); }
  changed() {
    this.generation += 1;
    for (const conflict of this.conflicts) conflict.local = structuredClone(this[conflict.group][conflict.key]);
    clearTimeout(this.timer);
    this.timer = setTimeout(() => { this.flush().catch(() => {}); }, 250);
  }
  mergeRemote(remote) {
    LocalState.validate(remote);
    const conflicts = [];
    for (const group of ["drafts", "outbox"]) {
      const merged = {};
      for (const key of new Set([...Object.keys(this.base[group]), ...Object.keys(this[group]), ...Object.keys(remote[group])])) {
        const before = LocalState.value(this.base[group], key), local = LocalState.value(this[group], key), other = LocalState.value(remote[group], key);
        let chosen;
        if (LocalState.equal(local, before)) chosen = other;
        else if (LocalState.equal(other, before) || LocalState.equal(local, other)) chosen = local;
        else { chosen = local; conflicts.push({group, key, local: structuredClone(local), remote: structuredClone(other)}); }
        if (chosen !== undefined) Object.defineProperty(merged, key, {value: structuredClone(chosen), enumerable: true, writable: true, configurable: true});
      }
      this[group] = merged;
    }
    this.base = {drafts: structuredClone(remote.drafts), outbox: structuredClone(remote.outbox)};
    this.revision = remote.revision; this.conflicts = conflicts;
  }
  resolveConflict(group, key, choice) {
    const conflict = this.conflicts.find(c => c.group === group && c.key === key);
    if (!conflict || !["local", "remote"].includes(choice)) throw new Error("此冲突已变化，请重新核对。");
    if (group === "outbox" && choice !== "remote") throw new Error("同一任务范围已有另一窗口的提交；不能用新请求覆盖，请核对服务回执后采用已保存请求。");
    if (choice === "remote") {
      if (conflict.remote === undefined) delete this[group][key];
      else Object.defineProperty(this[group], key, {value: structuredClone(conflict.remote), enumerable: true, writable: true, configurable: true});
    }
    this.conflicts = this.conflicts.filter(c => c !== conflict);
    if (group === "outbox") this.storageView = structuredClone(this.outbox);
    this.changed();
  }
  async flush() {
    clearTimeout(this.timer);
    if (this.flushing) {
      await this.flushing;
      if (this.savedGeneration !== this.generation) return this.flush();
      return;
    }
    this.flushing = (async () => {
      let races = 0;
      while (this.savedGeneration !== this.generation) {
        if (this.conflicts.length) throw new Error("其他窗口修改了同一草稿或提交回执；双方内容已保留，请先处理保存冲突。任务尚未因此重发。");
        const generation = this.generation;
        const body = {revision: this.revision, drafts: structuredClone(this.drafts), outbox: structuredClone(this.outbox)};
        let result;
        try { result = await this.client.post("/desktop/state", body, {signal: this.signal}); }
        catch (error) {
          if (error.status !== 409 || !this.client.get || ++races > 5) throw error;
          const remote = await this.client.get("/desktop/state", this.signal);
          this.mergeRemote(remote);
          if (!this.conflicts.length && LocalState.equal(this.drafts, remote.drafts) && LocalState.equal(this.outbox, remote.outbox)) this.savedGeneration = this.generation;
          continue;
        }
        if (!Number.isInteger(result.revision) || result.revision <= body.revision) throw new Error("工作台保存版本异常，请保留输入并重新打开界面核对。");
        this.revision = result.revision;
        this.base = {drafts: structuredClone(body.drafts), outbox: structuredClone(body.outbox)};
        this.savedGeneration = generation;
      }
      this.onSaved();
    })();
    try { await this.flushing; }
    catch (error) { this.onError(error); throw error; }
    finally { this.flushing = null; }
  }
}

// Persist BEFORE sending, never replay on load, never store connection tokens.
export function reconcileAcceptedDraft(localState, item) {
  if (!localState || item?.state !== "accepted" || !["prompt", "steer"].includes(item.kind)
      || !item.sid || typeof item.body?.text !== "string"
      || localState.getDraft(item.sid) !== item.body.text) return false;
  localState.deleteDraft(item.sid);
  return true;
}

export class Outbox {
  constructor(storage, namespace) {
    this.storage = storage;
    this.key = `xiaoyu-desktop-p1:${namespace}`;
    this.items = JSON.parse(storage.getItem(this.key) || "{}");
    for (const item of Object.values(this.items)) if (item.state === "sending") item.state = "unknown";
    this.running = new Set();
  }
  save() { this.storage.setItem(this.key, JSON.stringify(this.items)); }
  prepare(scope, action) {
    if (this.items[scope]) return this.items[scope];
    const item = {...structuredClone(action), key: crypto.randomUUID(), state: "ready"};
    this.items[scope] = item;
    try { this.save(); } catch (error) { delete this.items[scope]; throw error; }
    return item;
  }
  forget(scope) {
    if (this.running.has(scope)) throw new Error("请求仍在提交，请等待回执。");
    const previous = this.items[scope];
    delete this.items[scope];
    try { this.save(); } catch (error) { this.items[scope] = previous; throw error; }
  }
  async send(scope, client, signal) {
    const item = this.items[scope];
    if (!item || this.running.has(scope) || !["ready", "unknown"].includes(item.state)) return null;
    if (item.state === "unknown" && !["create", "prompt", "answer", "abort", "weave", "contest", "selection", "fleet_message", "fleet_stop", "fleet_command", "weave_message", "weave_stop"].includes(item.kind)) {
      throw new Error("这类操作不自动重试，请先核对服务状态。");
    }
    this.running.add(scope);
    let dispatched = false;
    try {
      item.state = "sending"; this.save();
      await this.storage.flush?.();
      const body = item.kind === "answer" ? {...item.body, idempotency_key: item.key} : item.body;
      dispatched = true;
      const result = await client.post(item.path, body, {key: item.key, instance: item.instance, signal});
      item.state = "accepted"; item.result = {session_id: result.session_id};
      if (["fleet_message", "fleet_stop", "weave_message", "weave_stop"].includes(item.kind) && typeof result.operation?.operation_id === "string") {
        item.result.operation_id = result.operation.operation_id;
        item.result.operation_status = result.operation.status;
        if (["weave_message", "weave_stop"].includes(item.kind)) {
          item.result.operation_persisted = result.operation.persisted === true;
          item.result.operation_warning = typeof result.operation.warning === "string" ? result.operation.warning : "";
        }
      }
      this.save();
      await this.storage.flush?.();
      return result;
    } catch (error) {
      if (!dispatched) error.message = `待提交回执保存失败，操作尚未发送：${error.message}`;
      item.state = !dispatched ? "ready" : error.recorded ? "recorded" : error.status >= 400 && error.status < 500 ? "rejected" : "unknown";
      item.message = error.recorded ? "服务已记录这次提交，不会重复执行；请查看历史、轮次结果与文件成果。" : error.message;
      this.save();
      throw error;
    } finally { this.running.delete(scope); }
  }
}

export function conversation(events) {
  const blocks = [];
  let reply = null;
  for (const event of events) {
    if (event.kind === "run.started" || event.kind === "steer.accepted") {
      reply = null;
      blocks.push({kind: "user", text: event.prompt ?? event.text, seq: event.seq,
        clipped: event.prompt_truncated || event.text_truncated});
    } else if (event.kind === "text.delta") {
      if (!reply) { reply = {kind: "assistant", text: "", seq: event.seq}; blocks.push(reply); }
      reply.text += event.text || "";
      reply.clipped ||= event.text_truncated;
    } else if (event.kind === "text.end" || event.kind === "tool.pending" || event.kind.startsWith("run.")) {
      reply = null;
    }
  }
  return blocks;
}

export function historyText(content) {
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return "";
  return content.map(part => typeof part?.text === "string" ? part.text : "[非文本内容，请在原始成果中查看]").join("\n");
}

export function historyConversation(messages) {
  return messages.filter(m => m.role === "user" || m.role === "assistant")
    .map(m => ({id: m.id, kind: m.role, text: historyText(m.content)
      + (m.has_attachments && m.content !== "[附件]" ? "\n[含附件，本界面暂不预览]" : "")}));
}

// Deliberately small text renderer: no HTML, remote media, links or executable content.
export function renderText(text) {
  const parts = String(text || "").split(/(```[^\n]*\n[\s\S]*?(?:```|$))/g);
  return parts.map(part => {
    if (part.startsWith("```")) return `<pre class="code"><code>${escapeHTML(part.replace(/^```[^\n]*\n/, "").replace(/```$/, ""))}</code></pre>`;
    return part.split(/\n\s*\n/).filter(Boolean).map(p => {
      const match = p.match(/^(#{1,3}) (.*)$/s);
      const content = escapeHTML(match ? match[2] : p).replace(/`([^`\n]+)`/g, "<code>$1</code>").replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
      return match ? `<h3>${content}</h3>` : `<p>${content}</p>`;
    }).join("");
  }).join("");
}

export function stateOf(session) {
  const state = (label, tone, attention, rank) => ({label, tone, attention, rank});
  if (session.status === "error" || session.detail === "failed") return state("执行失败", "error", true, 0);
  if (session.pending_approvals?.length || session.detail === "waiting_for_approval") return state("等待审批", "warning", true, 1);
  if (session.questionError) return state("待答状态未知", "warning", true, 2);
  if (session.questionItems?.some(q => ["open", "pending"].includes(q.state))) return state("等待回答", "warning", true, 2);
  if (session.detail === "interrupted_by_restart") return state("重启中断", "error", true, 3);
  if (session.detail === "budget_reached") return state("预算已用尽", "warning", true, 3);
  if (session.questionItems?.some(q => q.state === "queued") && !session.busy) return state("答案待接纳", "warning", true, 4);
  if (session.pending_notifications?.some(n => n.wake) && !session.busy) return state("后台结果待处理", "warning", true, 4);
  if (session.busy || session.status === "running") return state("执行中", "running", false, 5);
  if (session.detail === "interrupted") return state("已打断", "neutral", false, 6);
  if (session.last_result?.stopped === "turn_cap") return state("达到轮次上限", "warning", true, 3);
  if (session.last_result?.stopped === "budget") return state("预算已用尽", "warning", true, 3);
  if (session.last_result?.stopped === "truncated") return state("输出被截断", "warning", true, 3);
  if (session.last_result?.stopped === "output_missing") return state("结果待检查", "warning", true, 3);
  if (session.detail === "recovered") return session.last_result?.stopped === "done"
    ? state("已完成", "success", false, 8) : state("已恢复 · 待继续", "neutral", false, 7);
  if (session.detail === "finished") return state("已完成", "success", false, 8);
  if (session.status === "idle" && !session.turns) return state("尚未开始", "neutral", false, 9);
  return state("状态待确认", "neutral", true, 10);
}

export function sortSessions(sessions) {
  return [...sessions].sort((a, b) => stateOf(a).rank - stateOf(b).rank
    || (b.updated_at || 0) - (a.updated_at || 0) || a.session_id.localeCompare(b.session_id));
}

export function projectName(path) {
  return String(path || "未提供工作目录").replace(/[\\/]+$/, "").split(/[\\/]/).pop() || path;
}

export function visibleSessions(sessions, project, filter, search) {
  const needle = search.trim().toLowerCase();
  return sortSessions(sessions.filter(s => (!project || s.workspace === project)
    && (filter === "all" || (filter === "attention" && stateOf(s).attention)
      || (filter === "running" && stateOf(s).tone === "running") || (filter === "finished" && stateOf(s).label === "已完成"))
    && `${s.session_id} ${s.workspace} ${s.model}`.toLowerCase().includes(needle)));
}

export const EVENT_LIMIT = 200;
export function eventWindow(session) {
  return {cursor: Math.max(1, (session.next_seq || 1) - EVENT_LIMIT), events: [],
    gap: (session.first_seq || 1) > 1, clipped: (session.next_seq || 1) > EVENT_LIMIT + 1};
}

export function mergeEvents(previous, page) {
  if (!Array.isArray(page.events) || !Number.isInteger(page.next) || page.next < previous.cursor) {
    throw new Error("事件游标异常，请重新选择会话以读取当前窗口。");
  }
  let last = previous.cursor - 1;
  let gap = previous.gap || page.first_seq > previous.cursor;
  for (const event of page.events) {
    if (!Number.isInteger(event.seq) || event.seq <= last || event.seq >= page.next) {
      throw new Error("事件顺序异常，请重新选择会话。");
    }
    if (event.seq > last + 1) gap = true;
    last = event.seq;
  }
  const combined = [...previous.events, ...page.events];
  return {cursor: page.next, events: combined.slice(-EVENT_LIMIT), gap,
    clipped: previous.clipped || combined.length > EVENT_LIMIT};
}

export function escapeHTML(value) {
  return String(value ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
}

// A service may retain execution ownership after an unconfirmed interruption.
export function sessionActionAllowed(session, action = {}) {
  if (session?.writable !== false) return true;
  return ["abort", "fleet_stop", "weave_stop"].includes(action.kind)
    || action.kind === "approval" && action.body?.decision === "deny";
}

// Service terminology is translated only for status copy, never task content or identifiers.
export function workflowStatusText(value) {
  return String(value ?? "").replaceAll("七襄", "批量执行").replaceAll("斗巧", "方案比选").replaceAll("宸枢", "统筹协作").replaceAll("批量执行执行", "批量执行").replaceAll("方案比选比选", "方案比选").replaceAll("统筹协作协作", "统筹协作");
}
