import test from 'node:test';
import assert from 'node:assert/strict';
import {DEFAULT_SHORTCUTS,validateShortcuts,shortcutAction,createShortcutsController} from '../web/shortcuts.mjs';
const event=(key,extra={})=>({key,metaKey:true,ctrlKey:false,shiftKey:false,altKey:false,target:{tagName:'DIV'},...extra});
test('shortcuts normalize case, reject duplicate and reserved bindings and permit disabling',()=>{
  assert.equal(validateShortcuts({...DEFAULT_SHORTCUTS,search:' Mod+Shift+J '}).search,'mod+shift+j');
  assert.throws(()=>validateShortcuts({...DEFAULT_SHORTCUTS,search:'MOD+N'}),/相同/);
  for(const key of ['mod+q','mod+shift+c','mod+enter','mod+alt+j','j'])assert.throws(()=>validateShortcuts({...DEFAULT_SHORTCUTS,search:key}));
  assert.equal(validateShortcuts({...DEFAULT_SHORTCUTS,search:'',settings:''}).search,'');
});
test('global dispatch respects exact modifier sets, composing, editing and disabled actions',()=>{
  assert.equal(shortcutAction(event('n')),'new_task');assert.equal(shortcutAction(event('n',{metaKey:false,ctrlKey:true})),'new_task');
  assert.equal(shortcutAction(event('n',{shiftKey:true})),null);assert.equal(shortcutAction(event('n',{isComposing:true})),null);assert.equal(shortcutAction(event('n',{repeat:true})),null);assert.equal(shortcutAction(event('n',{defaultPrevented:true})),null);assert.equal(shortcutAction(event('n',{ctrlKey:true})),null);
  assert.equal(shortcutAction(event('b',{target:{tagName:'TEXTAREA'}})),null);assert.equal(shortcutAction(event('b',{target:{isContentEditable:true}})),null);assert.equal(shortcutAction(event('k',{target:{tagName:'INPUT'}})),'search');
  assert.equal(shortcutAction(event('B',{shiftKey:true})),'panel');assert.equal(shortcutAction(event('?',{shiftKey:true,code:'Slash'}),{...DEFAULT_SHORTCUTS,search:'mod+shift+/'}),'search');
  assert.equal(shortcutAction(event('k'),{...DEFAULT_SHORTCUTS,search:''}),null);
});
test('invalid local edits do not persist and restore defaults is explicit',()=>{
  const applied=[];const c=createShortcutsController({values:()=>DEFAULT_SHORTCUTS,onChange:v=>applied.push(v)});
  const input=value=>c.input({target:{dataset:{shortcut:'search'},value}});const click=action=>c.click({target:{closest:()=>({dataset:{shortcutAction:action}})}});
  input('mod+n');click('apply');assert.equal(applied.length,0);assert.match(c.render(),/相同快捷键/);
  input('mod+j');assert.equal(applied.length,0);click('apply');assert.equal(applied[0].search,'mod+j');click('defaults');assert.deepEqual(applied[1],DEFAULT_SHORTCUTS);
});
