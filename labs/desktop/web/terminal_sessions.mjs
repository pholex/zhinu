// 终端会话与应用会话在同一份清单里出现：来源只是一个标签。
// 这里只做判定与排序，不发请求、不碰 DOM，方便单独测试。

// 清单里要显示的终端会话：已被本服务原地继续的那几条由对应的应用会话代表，不重复列出。
export function terminalRows(items = [], sessions = [], query = "") {
  const live = new Set(sessions.map(s => s.session_id));
  const needle = query.trim().toLowerCase();
  return items
    .filter(item => !(item.session_id && live.has(item.session_id)))
    .filter(item => !needle || `${item.title || ""} ${item.workspace || ""} ${item.model || ""}`.toLowerCase().includes(needle))
    .sort((a, b) => Number(b.updated_at || 0) - Number(a.updated_at || 0) || String(a.catalog_id).localeCompare(String(b.catalog_id)));
}

// 最近一栏按时间合并两种来源；updated_at 两边都是秒。
export function mergeRecent(sessions = [], items = []) {
  return [...sessions.map(s => ({kind: "session", at: Number(s.updated_at || 0), id: s.session_id, value: s})),
    ...items.map(i => ({kind: "terminal", at: Number(i.updated_at || 0), id: i.catalog_id, value: i}))]
    .sort((a, b) => b.at - a.at || a.id.localeCompare(b.id));
}

export function terminalLabel(item = {}) {
  if (item.session_id) return "终端 · 已在此继续";
  if (item.locked) return "终端 · 使用中";
  return "终端";
}

export function isContinued(session = {}) { return session.fork_origin?.kind === "cli_adopted"; }

// 打开一条终端会话时，输入区该是什么样。历史始终可读；差别只在能不能写。
// writable：当前窗口能否向服务提交；pending：已有一次新建/继续请求在途。
export function takeover(view, {writable = true, pending = false, native = false} = {}) {
  if (!view || view.state === "initial" || view.state === "loading" && !view.revision)
    return {kind: "loading", title: "正在读取这段历史…", detail: "", canContinue: false, canFork: false, canRetry: false};
  const ready = view.state === "ready" && writable && !pending;
  const canFork = ready && view.canFork === true, canRetry = view.state !== "loading";
  if (view.inScope === false)
    return {kind: "elsewhere", title: "属于另一个工作目录", detail: view.workspace || "", note: native ? "" : "在桌面应用里可以直接转到那个目录的工作台。",
      canOpen: native && view.state !== "loading", canContinue: false, canFork: false, canRetry: false};
  if (view.sessionId)
    return {kind: "held", title: "已在羽案中继续", detail: "这段历史正由应用里的一场会话续写。", sessionId: view.sessionId, canContinue: false, canFork: false, canRetry: false};
  if (view.locked)
    return {kind: "conflict", title: "已在终端中打开", detail: "请先在那边结束会话，才能在这里继续。",
      note: canFork ? "" : view.forkReason || "", canContinue: false, canFork, canRetry};
  if (view.canContinue)
    return {kind: "ready", title: "可以接着这场终端会话", detail: "往同一份历史续写；继续期间终端不能同时打开它。", canContinue: ready, canFork, canRetry: false};
  return {kind: "blocked", title: "暂时不能在这里继续", detail: view.continueReason || view.forkReason || "这段历史当前只能查看。",
    canContinue: false, canFork, canRetry};
}
