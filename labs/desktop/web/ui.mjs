/** Shared presentation primitives; no service or task state lives here. */
const paths = {
  sidebarRight:'<rect x="3" y="4" width="18" height="16" rx="3"/><path d="M15 4v16"/>',
  cloud:'<path d="M6 19a5 5 0 0 1-1-10 7 7 0 0 1 13-2 6 6 0 0 1 0 12z"/>',
  home:'<path d="m3 10 9-7 9 7M5 9v12h5v-7h4v7h5V9"/>',
  palette:'<path d="M12 3a9 9 0 1 0 0 18h1a2 2 0 0 0 1-4 2 2 0 0 1 1-4h3a3 3 0 0 0 3-3c0-4-4-7-9-7z"/><path d="M7 9h.01M10 6h.01M15 7h.01M6 13h.01"/>',
  cpu:'<rect x="6" y="6" width="12" height="12" rx="2"/><path d="M9 3v3m6-3v3M9 18v3m6-3v3M3 9h3m-3 6h3M18 9h3m-3 6h3"/><rect x="10" y="10" width="4" height="4"/>',
  bell:'<path d="M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9M10 21h4"/>',
  keyboard:'<rect x="2" y="5" width="20" height="14" rx="2"/><path d="M6 9h.01M10 9h.01M14 9h.01M18 9h.01M6 12h.01M10 12h.01M14 12h.01M18 12h.01M7 16h10"/>',
  chart:'<path d="M4 3v17h17M8 16v-5m5 5V7m5 9V4"/>',
  info:'<circle cx="12" cy="12" r="9"/><path d="M12 11v6m0-10h.01"/>',
  arrowLeft:'<path d="M20 12H4m6-6-6 6 6 6"/>',
  arrowRight:'<path d="M4 12h16m-6-6 6 6-6 6"/>',
  expand:'<path d="M8 3H3v5m13-5h5v5M3 16v5h5m13-5v5h-5"/>',
  trash:'<path d="M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7m4-7v7"/>',
  close:'<path d="m6 6 12 12M6 18 18 6"/>',
  quote:'<path d="M9 5H3v7h6V5zm12 0h-6v7h6V5zM9 12c0 5-2 7-6 7m18-7c0 5-2 7-6 7"/>',
  loading:'<path d="M21 12a9 9 0 1 1-9-9"/>',
  stop:'<rect x="6" y="6" width="12" height="12" rx="2"/>',
  play:'<path d="m8 4 12 8-12 8z"/>',
  globe:'<circle cx="12" cy="12" r="9"/><ellipse cx="12" cy="12" rx="4" ry="9"/><path d="M3 12h18"/>',
  pin:'<path d="m15 3 6 6-4 1-4 6-5-5 6-4zM8 16l-5 5"/>',
  group:'<circle cx="9" cy="7" r="3"/><path d="M3 21v-3a6 6 0 0 1 12 0v3M16 4a3 3 0 0 1 0 6m2 5a5 5 0 0 1 3 4v2"/>',
  fork:'<circle cx="6" cy="5" r="2"/><circle cx="6" cy="19" r="2"/><circle cx="18" cy="5" r="2"/><path d="M6 7v10m0-5c8 0 12-1 12-5"/>',
  copy:'<rect x="8" y="8" width="13" height="13" rx="2"/><path d="M16 8V3H3v13h5"/>',
  export:'<path d="M12 15V3m-4 4 4-4 4 4M4 13v8h16v-8"/>',
  window:'<rect x="3" y="3" width="18" height="18" rx="3"/><path d="M3 8h18M7 5.5h.01M10 5.5h.01"/>',
  check:'<path d="m5 12 4 4L19 6"/>',
  lock:'<rect x="5" y="11" width="14" height="9" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/>',
  message:'<path d="M21 11a8 8 0 0 1-8 8H7l-5 3 1-7a8 8 0 0 1 7-12h3a8 8 0 0 1 8 8z"/>',
  approval:'<path d="M9 12V5a1.5 1.5 0 0 1 3 0v6m0-6V3.5a1.5 1.5 0 0 1 3 0V11m0-5a1.5 1.5 0 0 1 3 0v6m0-3a1.5 1.5 0 0 1 3 0v5a8 8 0 0 1-15 4l-3-5a1.5 1.5 0 0 1 2.5-1.5L9 15"/>',
  shield:'<path d="M12 3 4 6v5c0 5 4 8 8 10 4-2 8-5 8-10V6zM9 12l2 2 4-4"/>',

  sidebar:'<rect x="3" y="4" width="18" height="16" rx="3"/><path d="M9 4v16"/>',
  edit:'<path d="M13 5H5a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-8M16 3l5 5M10 14l2-6 6-6 5 5-6 6z"/>',
  search:'<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/>',
  folder:'<path d="M3 7V5a2 2 0 0 1 2-2h5l3 4h6a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7z"/>',
  clock:'<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  library:'<rect x="3" y="4" width="18" height="16" rx="3"/><path d="M3 14l5-5 6 6 3-3 4 4"/><circle cx="16" cy="8" r="1"/>',
  capabilities:'<rect x="3" y="3" width="7" height="7" rx="2"/><rect x="14" y="3" width="7" height="7" rx="2"/><rect x="3" y="14" width="7" height="7" rx="2"/><path d="M17.5 14v7M14 17.5h7"/>',
  space:'<rect x="4" y="7" width="16" height="14" rx="2"/><path d="M7 7V3h10v4M8 12h8M8 16h5"/>',
  more:'<circle cx="5" cy="12" r=".8"/><circle cx="12" cy="12" r=".8"/><circle cx="19" cy="12" r=".8"/>',
  settings:'<path d="M3 7h4m6 0h8M3 17h10m6 0h2"/><circle cx="10" cy="7" r="3"/><circle cx="16" cy="17" r="3"/>',
  terminal:'<rect x="3" y="4" width="18" height="16" rx="3"/><path d="m7 9 3 3-3 3m6 0h4"/>',
  connection:'<path d="m9 15 6-6M8 13l-2 2a3 3 0 0 0 4 4l3-3M11 8l3-3a3 3 0 0 1 4 4l-2 2"/>',
  archive:'<path d="M4 8v12h16V8M3 3h18v5H3zm6 9h6"/>',
  refresh:'<path d="M21 12a9 9 0 1 1-2.64-6.36L21 8M21 3v5h-5"/>',
  plus:'<path d="M12 5v14M5 12h14"/>',
  arrow:'<path d="M12 19V5m-6 6 6-6 6 6"/>',
  chevron:'<path d="m8 10 4 4 4-4"/>',
  monitor:'<rect x="3" y="4" width="18" height="13" rx="2"/><path d="M8 21h8m-4-4v4"/>',
  compare:'<path d="M8 3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h3M16 3h3a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-3M12 3v18M6 8h2M16 8h2M6 12h2M16 12h2"/>',
  document:'<path d="M14 3H5v18h14V8l-5-5zM14 3v5h5M8 12h8M8 16h5"/>'
};
export function icon(name) {
  return `<svg class="ui-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths[name] || paths.document}</svg>`;
}
export function hydrateIcons(root = document) {
  root.querySelectorAll('[data-icon]').forEach(node => { node.innerHTML = icon(node.dataset.icon); });
}
export const taskStarters = [
  {icon:'folder',title:'了解项目',detail:'了解结构，找到下一步',draft:'请先了解这个项目的结构和现状，梳理关键模块，并建议下一步值得推进的工作。'},
  {icon:'document',title:'起草文档',detail:'把想法整理成一份文档',draft:'我想完成一份文档。请先帮我明确读者、目标与结构，再一起完善内容。'},
  {icon:'compare',title:'比较方案',detail:'从多个角度作出选择',draft:'请帮我比较几个可行方案，从收益、成本和实施难度分析，并给出建议。'}
];
export function welcomeHTML() {
  return `<div class="empty welcome"><img class="empty-mark" src="./logo.png" width="48" height="48" alt=""><h2>今天，想完成什么？</h2><div class="welcome-starters" hidden role="group" aria-label="从一个想法开始">${taskStarters.map((s,i)=>`<button type="button" data-starter="${i}" title="${s.detail}">${icon(s.icon)}<span>${s.title}</span></button>`).join("")}</div></div>`;
}
export function fitComposer(textarea) {
  if (!textarea || !textarea.getClientRects().length) return;
  const previous = textarea.scrollTop;
  textarea.style.height = 'auto';
  textarea.style.height = `${Math.min(220, Math.max(60, textarea.scrollHeight))}px`;
  textarea.scrollTop = previous;
}

/** Keep toolbar popovers inside the visible conversation column. */
export function positionMenu(menu, boundary) {
  if (!menu.open || !boundary.getClientRects().length) return;
  const panel = menu.querySelector('.menu-items');
  panel.style.setProperty('--menu-shift', '0px');
  const box = panel.getBoundingClientRect(), bounds = boundary.getBoundingClientRect();
  const shift = box.left < bounds.left + 12 ? bounds.left + 12 - box.left
    : box.right > bounds.right - 12 ? bounds.right - 12 - box.right : 0;
  panel.style.setProperty('--menu-shift', `${shift}px`);
}
