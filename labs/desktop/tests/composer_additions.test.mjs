import test from 'node:test';
import assert from 'node:assert/strict';
import {appendReferences,localReference} from '../web/composer_additions.mjs';

test('references preserve a typed draft, avoid blank lines and deduplicate',()=>{
 const file=localReference({path:'/Users/example/资料/brief.md',kind:'file'});
 const text=appendReferences('分析这份文档',[file,file]);
 assert.equal(text,'分析这份文档\n参考文件："/Users/example/资料/brief.md"');
 assert.equal(appendReferences(text,[file]),text);
 assert.equal(appendReferences('',[file]),file);
});
test('folder references remain explicit and unusual path characters stay quoted',()=>{
 assert.equal(localReference({path:'/tmp/project',kind:'directory'}),'参考文件夹："/tmp/project"');
 assert.equal(localReference({path:'/tmp/a\nb".txt',kind:'file'}),'参考文件："/tmp/a\\nb\\".txt"');
 assert.throws(()=>localReference({path:'relative',kind:'file'}));
 assert.throws(()=>localReference({path:'/tmp/foo',kind:'unknown'}));
});
