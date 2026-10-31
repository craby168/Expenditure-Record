# 支出憑證紀錄系統

Standalone expenditure prototype for 如果建築師事務所. This project is independent of IF_ERP. Prepared 31 October 2026, Asia/Taipei.

## Start the prototype

Install Node.js, open this folder in a terminal, and run:

```powershell
node server.mjs
```

Open **http://127.0.0.1:4175** in the same computer's browser. The preview binds only to loopback. The responsive interface supports phone-sized screens; testing on an actual phone requires an HTTPS-hosted copy or a deliberately configured development-network preview. The PC preview URL is not reachable from a phone by typing its own localhost.

The builtless application requires no npm dependencies. The supplied company JPG is preserved in `assets/company-logo.jpg`. The logo contains white space and print marks; it can be replaced later with an approved clean logo file.

## Confirmed implementation scope

- Traditional Chinese application, expenditure recording only, fewer than five initial users.
- Mobile camera/files and scanned evidence; preserve originals and show previews where supported. HEIC and other unsupported browser formats must remain downloadable without inventing a preview.
- Amount, currency, vendor, document date/type/number, purpose, category and supporting notes; upload, search, review, correction history and expenditure dashboard.
- Employee role submits evidence and receives confirmation, without the expenditure register, dashboard or exports. Manager handles finance views and accountant reports. A trusted accountant can be provisioned as a manager for this single practice.
- On-demand accountant CSV and portable backups; revised records retain history and a correction reason.
- Income, payment reconciliation, payroll, full ledger, project accounting and final financial statements are later scope. The accountant prepares authoritative statements elsewhere.
- Tax profile remains unconfirmed. Architectural professional practices may fall under 執行業務者. Do not apply the initial ordinary-business tax proposal automatically.

## Modes and honest limitations

**Local prototype:** records and original file bytes live in this browser's IndexedDB. Demo manager/employee switching illustrates the screens; it is not authentication or access security. Anyone using that browser profile can switch demo roles or inspect its local database. Data does not automatically synchronize to another device, survive browser-data removal, or reach the NAS. Use non-sensitive test records and download a backup to preserve tests.

**Cloud connection:** the optional Supabase adapter uses real email/password authentication and database-enforced roles. It needs a project URL and public publishable/anon key, plus the schema and provisioned accounts described below. No cloud tenant, paid service or real account has been created by this prototype. Until credentials exist, shared-device cloud functionality and live RLS are not verified. Never paste service-role, secret, database-password or OCR vendor keys into the application.

**OCR:** capture works with manual confirmation. Cloud OCR is not configured and must not fabricate extraction results. Choose and benchmark a provider later; secrets belong in a server-side function. Preserve the source, provider/version and confidence separately, and require human confirmation. A future local OCR adapter can replace the cloud provider for more private processing.

The local backup is a portable snapshot including file data, not an immutable regulatory archive. Cloud audit snapshots are created by database triggers; user-entered history is additional context rather than proof of authorization. No tax return, tax credit or deduction certification is produced.

## Suggested hosting and current budget

For the first shared pilot: host the static interface on Cloudflare Pages and use Supabase Free for Auth/Postgres/private evidence. This avoids dependence on an always-on office PC. It is a cloud service even though it does not require your own server.

Official pricing checked 31 October 2026:

| Component | Current option | Limits / consequence |
|---|---|---|
| Static interface | Cloudflare Pages static requests are free and unlimited | Dynamic functions have separate quotas. Publish only web assets, not SQL, backups or internal documents. |
| Auth + database + files | Supabase Free: 500 MB DB, 1 GB file storage, 5 GB egress | Can pause after one week of inactivity. Free does not include automatic backups. Full-resolution phone photos may fill file storage quickly. |
| Paid Supabase | Pro starts US$25/month | Exceeds the requested under-US$10 monthly target. Do not upgrade automatically. |
| Future lower-cost storage | Cloudflare R2 plus an authenticated server adapter, or company NAS | Not implemented as a second storage adapter in this prototype. Requires access-control, backup and restore verification; prices depend on usage. |
| OCR | Unconfigured | Processing is an additional cost, not included in the hosting estimate. |

Sources: [Supabase pricing](https://supabase.com/pricing), [Pages pricing](https://developers.cloudflare.com/pages/functions/pricing/), [R2 pricing](https://developers.cloudflare.com/r2/pricing/).

The proposed starting service bill can be US$0 within quotas, but long-term production operation under US$10 is not guaranteed by the Free tier. Monitor storage and export to NAS; do not delete the online original just because a backup exists without an approved archive/retrieval plan. Production host selection remains a decision after prototype testing.

## Set up a dedicated Supabase pilot

1. Create a dedicated project owned by the company/project owner. Choose the region and review service processing/privacy terms.
2. Run `supabase/schema.sql` once in the project's SQL editor as its owner. It is a fresh-install schema, not an upgrade migration for an existing system.
3. Disable public sign-up in Auth. Create/invite named test users from the trusted Auth dashboard, then provision their profile rows using the SQL below. No browser may promote its own role.
4. Enter the project HTTPS URL and **public** publishable/anon key in the application's cloud settings. Login using a provisioned account. The public key is intentionally not a database administrator credential; RLS enforces access.
5. Configure Auth's allowed site URLs for the hosted HTTPS origin. Use a dedicated test tenant and sample documents first.
6. Execute the role, upload-failure and concurrency checks in `supabase/ACCEPTANCE.md` before enabling real staff uploads. Recheck configuration with Supabase's security advisor.

Provision profiles using UUIDs copied from Auth, via SQL editor only:

```sql
insert into public.profiles(id,role,display_name) values
  ('REPLACE_WITH_AUTH_USER_UUID','manager','管理者');
-- Additional users: role 'employee'; an authorized accountant can use 'manager'.
-- Revoke membership through trusted administration:
-- update public.profiles set active=false where id='USER_UUID';
```

The browser uses these tables: `profiles`, `records` (payload plus server version), `documents` (private original-file metadata) and `audit_events` (server snapshots). No DELETE access is granted to application users. Private evidence objects cannot be overwritten. Employees have INSERT access for their pending submission and evidence, but no SELECT access to expense records or documents. Managers have read/update privileges, not role administration.

Upload sequence: create pending/uploading record, insert metadata, upload each private original, then invoke `finalize_submission`. Finalization verifies all declared objects exist before setting complete. A failed upload remains visibly incomplete for manager follow-up. The storage policy requires matching metadata and owner/path. Optimistic updates use `id` plus expected `version`; a stale update must fail rather than overwrite another reviewer. A reviewed record requires a correction reason before revision.

Reference: [Supabase RLS](https://supabase.com/docs/guides/database/postgres/row-level-security), [private storage policies](https://supabase.com/docs/guides/storage/security/access-control).

## NAS and privacy transition

The NAS is initially a backup destination, not a prerequisite for the running interface. After prototype acceptance, select either cloud primary + NAS backup, or NAS-hosted application/data over the company's approved network/VPN. If data moves onto the NAS, the NAS must be available whenever users access it. A cloud database cannot make offline NAS files accessible.

For local testing, export a full backup including evidence and copy it to a restricted NAS folder. Confirm restore in a separate test browser before relying on it. Do not place financial exports in a public shared folder.

For a cloud deployment, schedule both database export and private-file copies from a trusted machine/NAS task. Browser export is on demand and does not itself meet a daily automatic-backup objective. Keep backup credentials outside web assets. Encrypt/restrict backups, record file hashes and run a restore drill; align database snapshots and file copies. Adopt the one-day RPO/one-business-day RTO only after this process has been demonstrated. This prototype has not scheduled a NAS task or accessed the company's NAS.

## Tax and accountant notes

The revised professional-practice memo is in `docs/tax-profile-addendum.md`. The tax profile, accounting basis, withholding-unit registration and deductions await accountant confirmation. Restaurant receipts need business context; fuel needs business use and evidence; foreign software can need additional review. A recorded or manager-reviewed expense is not a tax-authority-approved deduction.

Keep original paper evidence under the practice's existing custody process. Uploaded images do not authorize destruction. Payment evidence/dates may still be requested by the accountant even though payment reconciliation is outside MVP. The expenditure dashboard does not calculate net profit, annual entertainment limits or complete financial statements.

## Prototype acceptance

Test employee upload-only view; manager filtering and totals by currency; original-byte download; multi-attachment capture; duplicate warnings; review and correction reason/history; CSV literal safety; backup/restore with files; mobile layout; reload persistence; and cloud RLS checks when a tenant is configured. See `QA.md` for the verification actually performed and remaining limits.
