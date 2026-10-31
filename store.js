// Local prototype only. IndexedDB is not a multi-user authorization boundary.
export const CATEGORIES = ['餐飲','加油／交通','一般採購','軟體／訂閱','辦公／文具','設備','租金／水電','專業服務','其他'];
export const DOCUMENT_TYPES = ['電子發票','統一發票','收據','國外憑證','其他證明'];
export const MAX_FILE_SIZE = 25 * 1024 * 1024;
export const PAYLOAD_FIELDS = ['vendor','date','amount','currency','documentType','category','purpose','notes','invoiceNo','status','reviewedAt','reviewedBy','history','correctionReason','uploadState','isSample'];
const request = r => new Promise((resolve,reject)=>{r.onsuccess=()=>resolve(r.result);r.onerror=()=>reject(r.error);});
const completion = tx => new Promise((resolve,reject)=>{tx.oncomplete=()=>resolve();tx.onabort=()=>reject(tx.error||new Error('寫入已取消'));tx.onerror=()=>reject(tx.error);});
export const newId = () => crypto.randomUUID();
export const now = () => new Date().toISOString();
export const today = () => new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Taipei',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date());
export async function hashBlob(blob){return [...new Uint8Array(await crypto.subtle.digest('SHA-256',await blob.arrayBuffer()))].map(x=>x.toString(16).padStart(2,'0')).join('');}
export function payloadOf(r){return Object.fromEntries(PAYLOAD_FIELDS.filter(k=>r[k]!==undefined).map(k=>[k,r[k]]));}
export function validDate(s){if(!/^\d{4}-\d{2}-\d{2}$/.test(s))return false;const d=new Date(`${s}T00:00:00Z`);return !isNaN(d)&&d.toISOString().slice(0,10)===s;}
export function validateRecord(r){
  if(!r||typeof r!=='object')throw new Error('紀錄格式不正確');
  if(typeof r.vendor!=='string'||!r.vendor.trim()||r.vendor.length>200)throw new Error('請填寫商家／收款對象（200 字內）');
  if(!validDate(r.date))throw new Error('請填寫有效的憑證日期');
  if(!/^\d{1,12}(\.\d{1,2})?$/.test(String(r.amount))||Number(r.amount)<=0)throw new Error('金額須大於 0，最多 12 位整數與 2 位小數');
  if(!['TWD','USD','JPY','EUR','CNY','HKD','GBP'].includes(r.currency))throw new Error('幣別不正確');
  if(!CATEGORIES.includes(r.category)||!DOCUMENT_TYPES.includes(r.documentType))throw new Error('請選擇有效的分類與憑證類型');
  if(!['pending','reviewed'].includes(r.status))throw new Error('檢閱狀態不正確');
  for(const k of ['purpose','notes','invoiceNo','correctionReason'])if(r[k]!=null&&(typeof r[k]!=='string'||r[k].length>(k==='notes'?5000:1000)))throw new Error('欄位內容過長或格式不正確');
  return true;
}
export function csvCell(value){const s=String(value??'');const safe=/^[\s]*[=+\-@\t\r]/.test(s)?"'"+s:s;return '"'+safe.replaceAll('"','""')+'"';}
export function toCSV(records){
  const cols=[['紀錄編號','id'],['版本','version'],['憑證日期','date'],['商家／收款對象','vendor'],['金額','amount'],['幣別','currency'],['憑證類型','documentType'],['分類','category'],['發票號碼','invoiceNo'],['用途','purpose'],['備註','notes'],['狀態','status'],['建立時間','createdAt'],['更新時間','updatedAt'],['附件檔名','attachments'],['附件 SHA-256','hashes']];
  return '\uFEFF'+[cols.map(x=>csvCell(x[0])).join(','),...records.map(r=>cols.map(([,k])=>csvCell(k==='attachments'?r.documents.map(d=>d.filename).join(' | '):k==='hashes'?r.documents.map(d=>d.sha256).join(' | '):k==='status'?(r.status==='reviewed'?'已檢閱':'待檢閱'):r[k])).join(','))].join('\r\n');
}
function bytesToBase64(buffer){let s='';const b=new Uint8Array(buffer);for(let i=0;i<b.length;i+=32768)s+=String.fromCharCode(...b.subarray(i,i+32768));return btoa(s);}
function base64ToBlob(s,mime){if(typeof s!=='string'||s.length>MAX_FILE_SIZE*1.4)throw new Error('備份附件超過大小限制');const b=atob(s);return new Blob([Uint8Array.from(b,c=>c.charCodeAt(0))],{type:mime});}
export class LocalStore{
  mode='local';
  async init(){this.db=await new Promise((resolve,reject)=>{const r=indexedDB.open('if-expense-records-v1',1);r.onupgradeneeded=()=>{r.result.createObjectStore('records',{keyPath:'id'});r.result.createObjectStore('documents',{keyPath:'id'});};r.onsuccess=()=>resolve(r.result);r.onerror=()=>reject(r.error);});return this;}
  async listRecords(){const tx=this.db.transaction('records');return (await request(tx.objectStore('records').getAll())).sort((a,b)=>b.createdAt.localeCompare(a.createdAt));}
  async getDocument(id){return request(this.db.transaction('documents').objectStore('documents').get(id));}
  async saveRecord(record,attachments=[],expectedVersion=null){
    validateRecord(record);const tx=this.db.transaction(['records','documents'],'readwrite');const done=completion(tx);const rs=tx.objectStore('records');
    const old=await request(rs.get(record.id));
    if((old?.version??null)!==expectedVersion){tx.abort();try{await done;}catch{}throw new Error('這筆資料已被更新，請重新開啟後再修改。');}
    const docs=[...(old?.documents||[])];for(const a of attachments){const doc={id:a.id||newId(),recordId:record.id,filename:a.filename||a.file.name,mime:a.mime||a.file.type||'application/octet-stream',sizeBytes:a.file.size,sha256:a.sha256};docs.push(doc);tx.objectStore('documents').add({...doc,blob:a.file});}
    if(!docs.length){tx.abort();try{await done;}catch{}throw new Error('每筆紀錄至少需要一份附件');}
    const saved={...payloadOf(record),id:record.id,version:(old?.version||0)+1,createdAt:old?.createdAt||now(),updatedAt:now(),createdBy:old?.createdBy||record.createdBy||'local-demo',documents:docs,uploadState:'complete'};
    rs.put(saved);await done;return saved;
  }
  async exportBackup(){
    const records=await this.listRecords();const docs=await request(this.db.transaction('documents').objectStore('documents').getAll());
    const documents=[];for(const d of docs){const {blob,...meta}=d;documents.push({...meta,base64:bytesToBase64(await blob.arrayBuffer())});}
    return {format:'if-expense-backup',schemaVersion:1,exportedAt:now(),mode:'local',records,documents};
  }
  async restoreBackup(backup){
    if(backup?.format!=='if-expense-backup'||backup.schemaVersion!==1||!Array.isArray(backup.records)||!Array.isArray(backup.documents))throw new Error('不是可支援的支出憑證備份');
    if(backup.records.length>10000||backup.documents.length>20000)throw new Error('備份筆數超過本機原型上限');
    const uuid=/^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
    const existing=new Map((await this.listRecords()).map(r=>[r.id,r]));const recordIds=new Set();const docIds=new Set();const prepared=[];const documents=new Map();
    for(const d of backup.documents){if(!uuid.test(d.id)||!uuid.test(d.recordId)||docIds.has(d.id)||typeof d.filename!=='string'||d.filename.length>255||typeof d.mime!=='string'||d.mime.length>100)throw new Error('備份附件中有無效或重複識別碼');docIds.add(d.id);const blob=base64ToBlob(d.base64,d.mime);if(blob.size!==d.sizeBytes||await hashBlob(blob)!==d.sha256)throw new Error('附件校驗失敗，備份可能不完整');documents.set(d.id,{id:d.id,recordId:d.recordId,filename:d.filename,mime:d.mime,sizeBytes:blob.size,sha256:d.sha256,blob});}
    for(const r of backup.records){validateRecord(r);if(!uuid.test(r.id)||recordIds.has(r.id)||!Number.isInteger(r.version)||r.version<1||typeof r.createdAt!=='string'||!Number.isFinite(Date.parse(r.createdAt))||typeof r.updatedAt!=='string'||!Number.isFinite(Date.parse(r.updatedAt))||!Array.isArray(r.documents)||!r.documents.length||!Array.isArray(r.history))throw new Error('備份紀錄資料不完整');recordIds.add(r.id);
      const docs=r.documents.map(d=>{const full=documents.get(d.id);if(!full||full.recordId!==r.id)throw new Error('備份紀錄與附件對應不一致');const {blob,...meta}=full;return meta;});
      if(new Set(docs.map(d=>d.id)).size!==docs.length)throw new Error('備份有重複附件');
      if(!existing.has(r.id))prepared.push({...payloadOf(r),id:r.id,version:r.version,createdAt:r.createdAt,updatedAt:r.updatedAt,createdBy:'restored-local',documents:docs,history:r.history.map(h=>({at:String(h.at||''),action:String(h.action||''),actor:String(h.actor||''),changes:String(h.changes||'')})),uploadState:'complete'});
    }
    const tx=this.db.transaction(['records','documents'],'readwrite');const done=completion(tx);
    for(const r of prepared){tx.objectStore('records').add(r);for(const m of r.documents)tx.objectStore('documents').add(documents.get(m.id));}await done;return {imported:prepared.length,skipped:backup.records.length-prepared.length};
  }
  async seedSamples(){
    if((await this.listRecords()).some(r=>r.isSample))throw new Error('示範資料已載入');
    const samples=[['範例・午餐餐廳','餐飲','680','團隊工作餐','pending'],['範例・加油站','加油／交通','1320','現場勘查交通','reviewed'],['範例・軟體訂閱','軟體／訂閱','960','繪圖工具月費','reviewed'],['範例・文具店','辦公／文具','420','事務所日常用品','pending'],['範例・材料商','一般採購','2850','材料樣品採購','pending'],['範例・電腦設備','設備','32900','工作用螢幕與周邊','reviewed']];
    for(let i=0;i<samples.length;i++){const [vendor,category,amount,purpose,status]=samples[i];const date=new Date();date.setUTCDate(date.getUTCDate()-i*3);const file=new File([`此為虛構示範憑證，沒有報稅效力。\n${vendor}\n${amount} TWD\n${purpose}`],`示範憑證-${i+1}.txt`,{type:'text/plain'});const r={id:newId(),vendor,category,amount,purpose,status,date:date.toISOString().slice(0,10),currency:'TWD',documentType:'其他證明',invoiceNo:'',notes:'虛構示範資料，請勿用於正式記帳。',history:[{at:now(),action:'建立示範資料',actor:'本機示範',changes:''}],isSample:true};await this.saveRecord(r,[{file,sha256:await hashBlob(file)}],null);}
  }
}
