/** Local view history contains navigation state, never executable requests. */
export function createViewHistory({initial, key = JSON.stringify, onChange = ()=>{}, limit = 100}) {
  const copy = value=>structuredClone(value);
  let entries=[copy(initial)], index=0, previous='';
  function publish(force=false) {
    const state={canGoBack:index>0,canGoForward:index<entries.length-1};
    const signature=JSON.stringify(state);
    if(force||signature!==previous){previous=signature;onChange(state);}
    return state;
  }
  function replace(value){entries[index]=copy(value);publish();}
  function visit(value,{replace:replaceCurrent=false}={}) {
    if(replaceCurrent||key(entries[index])===key(value)){replace(value);return false;}
    entries=entries.slice(0,index+1);entries.push(copy(value));
    if(entries.length>limit)entries.shift();index=entries.length-1;publish();return true;
  }
  function move(delta){const next=index+delta;if(next<0||next>=entries.length)return null;index=next;publish();return copy(entries[index]);}
  publish(true);
  return {visit,replace,back:()=>move(-1),forward:()=>move(1),current:()=>copy(entries[index]),state:()=>({canGoBack:index>0,canGoForward:index<entries.length-1}),reset(value){entries=[copy(value)];index=0;publish(true);}};
}

export function viewHistoryKey(view) {
  const context = view.source==='project'||view.source==='content-project' ? view.projectSelected : view.source==='cli' ? view.cliSelected : view.source==='workbench' ? `${view.selected}:${view.newTask}:${view.inputProject||""}:${view.workspace||""}:${view.filter}:${view.collaborationPage||""}` : '';
  return `${view.source}:${context||''}`;
}
