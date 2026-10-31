import test from 'node:test';
import assert from 'node:assert/strict';
import {CloudStore,validateConfig} from '../cloud-store.js';
const config={url:'https://example.supabase.co',key:'sb_publishable_test_key_for_contract_tests'};
const record={id:'00000000-0000-4000-8000-000000000001',vendor:'測試商家',date:'2026-10-31',amount:'1250.50',currency:'TWD',category:'餐飲',documentType:'收據',status:'pending'};
function store(role='employee'){const s=new CloudStore(config);s.profile={id:'00000000-0000-4000-8000-000000000002',role};s.session={access_token:'test-token',expires_at:Date.now()/1000+1000};return s;}
test('browser configuration rejects secret or privileged API keys and unsafe URLs',()=>{
  assert.throws(()=>validateConfig('http://example.supabase.co',config.key));
  assert.throws(()=>validateConfig(config.url,'sb_secret_do_not_use_this_key'));
  const token='e30.'+btoa(JSON.stringify({role:'service_role'}))+'.test';
  assert.throws(()=>validateConfig(config.url,token));
  assert.equal(validateConfig(config.url,config.key).url,config.url);
});
test('employee finance actions fail before any network request',async()=>{
  const s=store();await assert.rejects(()=>s.listRecords(),/管理者/);
  await assert.rejects(()=>s.getDocument('anything'),/管理者/);
  await assert.rejects(()=>s.exportBackup(),/管理者/);
  await assert.rejects(()=>s.saveRecord(record,[],1),/管理者/);
});
test('failed original upload never finalizes or reports successful submission',async()=>{
  const prior=globalThis.fetch;const paths=[];
  globalThis.fetch=async(url)=>{paths.push(String(url));return String(url).includes('/storage/')?new Response('failed',{status:500}):new Response(null,{status:201});};
  try {await assert.rejects(()=>store().saveRecord(record,[{file:new Blob(['x']),filename:'test.jpg',sha256:'a'.repeat(64)}]),/附件未完成/);assert.ok(!paths.some(x=>x.includes('finalize_submission')));}
  finally{globalThis.fetch=prior;}
});
test('optimistic update conflict is surfaced instead of silently succeeding',async()=>{
  const prior=globalThis.fetch;globalThis.fetch=async()=>new Response('[]',{status:200});
  try{await assert.rejects(()=>store('manager').saveRecord({...record,uploadState:'complete'},[],3),/已被更新/);}
  finally{globalThis.fetch=prior;}
});
