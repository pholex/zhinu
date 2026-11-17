import test from "node:test";
import assert from "node:assert/strict";
import {defaultComposerModel} from "../web/model_picker.mjs";
const models=[{id:"enabled-first"},{id:"enabled-second"}];
test("first enabled model is explicit, saved selection wins on later opens",()=>{
  assert.equal(defaultComposerModel({models,effective:"other-service-default"}),"enabled-first");
  assert.equal(defaultComposerModel({models,saved:"enabled-second"}),"enabled-second");
  assert.equal(defaultComposerModel({models,pending:"enabled-first",saved:"enabled-second"}),"enabled-first");
});
test("removed model falls back to an enabled choice; wildcard-only services keep their configured model",()=>{
  assert.equal(defaultComposerModel({models,pending:"removed",saved:"disabled"}),"enabled-first");
  assert.equal(defaultComposerModel({effective:"custom-model"}),"custom-model");
  assert.equal(defaultComposerModel(),"");
});

test("inherited configured model precedes the catalog, saved desktop choice wins",()=>{
  assert.equal(defaultComposerModel({models,effective:"enabled-second"}),"enabled-second");
  assert.equal(defaultComposerModel({models,effective:"enabled-second",saved:"enabled-first"}),"enabled-first");
});
