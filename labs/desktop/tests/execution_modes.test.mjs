import test from "node:test";
import assert from "node:assert/strict";
import {executionModePresentation,executionPermissionChoices} from "../web/execution_modes.mjs";
test("permission presentation keeps service capabilities and inheritance values intact",()=>{
  const modes=[{id:"plan",label:"plan"},{id:"default",label:"确认"}];
  const before=structuredClone(modes), choices=executionPermissionChoices(modes,"default");
  assert.deepEqual(choices.map(row=>row.id),["plan","default",""]);
  assert.equal(choices.at(-1).caption,executionModePresentation("default").label);
  assert.equal(choices.at(-1).inherited,true);
  assert.deepEqual(modes,before);
  assert.equal(executionPermissionChoices(modes,"plan").at(-1).caption,executionModePresentation("plan").label);
});
test("missing default does not claim a permission mode and future service labels remain usable",()=>{
  const choices=executionPermissionChoices([],"auto");
  assert.deepEqual(choices.map(row=>row.id),[""]);
  assert.equal(choices[0].caption,"使用默认设置");
  const mode={id:"custom",label:"定制权限",description:"遵循组织策略"};
  assert.equal(executionPermissionChoices([mode],"custom").at(-1).caption,"定制权限");
  assert.equal(executionModePresentation(mode).description,"遵循组织策略");
});
