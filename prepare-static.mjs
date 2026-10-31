import {mkdir,copyFile,readdir} from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
const root=path.dirname(fileURLToPath(import.meta.url));
const dest=path.join(root,'dist');
await mkdir(path.join(dest,'assets'),{recursive:true});
for(const name of ['index.html','app.js','store.js','cloud-store.js','styles.css','_headers']) await copyFile(path.join(root,name),path.join(dest,name));
for(const name of await readdir(path.join(root,'assets'))) if(/^[a-zA-Z0-9_.-]+$/.test(name)) await copyFile(path.join(root,'assets',name),path.join(dest,'assets',name));
console.log('Static web files prepared in dist/. No SQL, backups or internal docs included.');
