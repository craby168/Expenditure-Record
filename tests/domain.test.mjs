import test from 'node:test';
import assert from 'node:assert/strict';
import {LocalStore,validateRecord,validDate,csvCell,toCSV,hashBlob} from '../store.js';

const valid={id:'test',vendor:'測試商家',date:'2026-10-31',amount:'1250.50',currency:'TWD',category:'餐飲',documentType:'收據',status:'pending',documents:[]};
test('financial amounts reject invalid, negative, excessive precision and exponent inputs',()=>{
  assert.equal(validateRecord(valid),true);
  for(const amount of ['0','-1','1.001','1e4','Infinity','NaN','1000000000000','1,200']) assert.throws(()=>validateRecord({...valid,amount}));
});
test('real dates are checked, including leap years',()=>{
  assert.equal(validDate('2024-02-29'),true);
  for(const date of ['2026-02-29','2026-04-31','2026-13-01','31/10/2026']) assert.equal(validDate(date),false);
});
test('currency remains explicit; unsupported code never silently changes to TWD',()=>{
  assert.equal(validateRecord({...valid,currency:'USD'}),true);
  assert.throws(()=>validateRecord({...valid,currency:'NT$'}));
});
test('CSV neutralizes spreadsheet formulas and preserves embedded quotes and newlines',()=>{
  for(const input of ['=HYPERLINK("bad")','+1+1','-1+1','@SUM(1)','  =1','\t=1']) assert.ok(csvCell(input).startsWith('"\''));
  assert.equal(csvCell('商家 "A"\n台北'),'"商家 ""A""\n台北"');
  const text=toCSV([{...valid,vendor:'=1+1',documents:[]}]);
  assert.ok(text.includes('"\'=1+1"'));
  assert.ok(text.startsWith('\uFEFF'));
});
test('original byte checksum uses SHA-256, not filename',async()=>{
  assert.equal(await hashBlob(new Blob(['abc'])),'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad');
  assert.notEqual(await hashBlob(new Blob(['abc'])),await hashBlob(new Blob(['abd'])));
});
test('corrupted or unlinked evidence backup is rejected before any database write',async()=>{
  const id='00000000-0000-4000-8000-000000000001',docId='00000000-0000-4000-8000-000000000002';
  const doc={id:docId,recordId:id,filename:'receipt.txt',mime:'text/plain',sizeBytes:3,sha256:await hashBlob(new Blob(['abc'])),base64:btoa('abc')};
  const backup={format:'if-expense-backup',schemaVersion:1,records:[{...valid,id,version:1,createdAt:'2026-10-31T00:00:00Z',updatedAt:'2026-10-31T00:00:00Z',history:[],documents:[doc]}],documents:[doc]};
  const s=new LocalStore();s.listRecords=async()=>[];let writes=0;s.db={transaction(){writes++;throw new Error('Unexpected database write');}};
  const badBytes=structuredClone(backup);badBytes.documents[0].base64=btoa('abd');
  await assert.rejects(()=>s.restoreBackup(badBytes),/校驗失敗/);
  const missing=structuredClone(backup);missing.documents=[];
  await assert.rejects(()=>s.restoreBackup(missing),/對應不一致/);
  assert.equal(writes,0);
});
