import {validateRecord} from './store.js';
export class ServerStore {
  mode='server';
  config={url:location.origin};
  async request(path,{method='GET',body}={}){
    const r=await fetch(`/api${path}`,{method,credentials:'same-origin',headers:{...(body?{'Content-Type':'application/json'}:{}),...(this.csrfToken?{'X-CSRF-Token':this.csrfToken}:{})},body:body?JSON.stringify(body):undefined});
    if(!r.ok){let j;try{j=await r.json();}catch{}if(r.status===401)this.profile=null;throw new Error(j?.error||j?.message||`請求失敗 (${r.status})`);}
    if(r.status===204)return null;return r.json();
  }
  async init(){try{const s=await this.request('/session');this.profile=s.user;this.csrfToken=s.csrfToken;}catch(e){this.profile=null;}return this;}
  async login(email,password){const s=await this.request('/login',{method:'POST',body:{email,password}});this.profile=s.user;this.csrfToken=s.csrfToken;return this.profile;}
  async logout(){try{await this.request('/logout',{method:'POST'});}finally{this.profile=null;this.csrfToken=null;}}
  managerOnly(){if(this.profile?.role!=='manager')throw new Error('此功能僅供管理者使用');}
  async listRecords(){this.managerOnly();const rows=await this.request('/records');this.documents=rows.flatMap(r=>r.documents);return rows;}
  async saveRecord(record,files=[],expectedVersion=null){
    validateRecord(record);const attachments=[];
    for(const a of files){const bytes=new Uint8Array(await a.file.arrayBuffer());let text='';for(let i=0;i<bytes.length;i+=32768)text+=String.fromCharCode(...bytes.subarray(i,i+32768));attachments.push({id:a.id,filename:a.filename||a.file.name,mime:a.mime||a.file.type||'application/octet-stream',sha256:a.sha256,base64:btoa(text)});}
    return this.request(expectedVersion===null?'/records':`/records/${record.id}`,{method:expectedVersion===null?'POST':'PATCH',body:{record,expectedVersion,attachments}});
  }
  async getDocument(id){this.managerOnly();const r=await fetch(`/api/documents/${id}`,{credentials:'same-origin'});if(!r.ok)throw new Error('無權存取或找不到附件');const meta=this.documents?.find(d=>d.id===id);return {...meta,id,blob:await r.blob()};}
  async exportBackup(){this.managerOnly();return this.request('/backup');}
  async listAudit(id){this.managerOnly();return this.request(`/audit/${id}`);}
}
