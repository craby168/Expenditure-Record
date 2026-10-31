# 支出憑證紀錄系統 v1.0

如果建築師事務所的支出憑證管理工具。繁體中文、手機與桌面響應式介面。

- [使用手冊（繁體中文）](docs/使用手冊-v1.0.md)
- [給後續 AI／公司維護者的交接](docs/AI部署交接-v1.0.md)
- [稅務適用身分補充](docs/tax-profile-addendum.md)

## 分支與架構

`prototype` 保存 IndexedDB 示範版；`v1.0` 使用 Python 後端、SQLite 及伺服器私有原始附件，所有正式操作必須以實際帳號登入。前端透過同源 /api 呼叫後端，session 使用 HttpOnly cookie，變更請求需要 CSRF token。employee 僅能送出支出；manager 可查詢、檢閱、修改及匯出。帳號管理僅透過受信任的伺服器 CLI。

原型的 Node server.mjs、Supabase schema／adapter 與相關測試保留供歷史參考，**不是 v1.0 的啟動或部署方式**。v1.0 不會讀取或自動上傳瀏覽器原型資料。靜態前端單獨發布也不能取代後端。

## NAS 部署

需要支援 Docker Compose 的 NAS（或相容 Python 及 SQLite 的主機）。實際 NAS、網域、憑證、volume 權限與帳號需由公司端配置。開發機測試不代表 NAS 實機部署驗收。

1. 取得 v1.0 分支，複製 .env.example 為 .env。
2. 設定 APP_PUBLIC_ORIGIN 為正式 HTTPS origin，設定 NAS 持久資料掛載位置，並預先讓 UID/GID 10001 可讀寫該目錄。正式環境不要開啟 APP_DEV_LOCALHOST。
3. 執行 docker compose up -d --build。
4. 建立管理者帳號：
   `docker compose exec app python -m backend.app create-user --email manager@example.com --name 管理者 --role manager`
   系統會互動詢問密碼，沒有預設帳密。密碼至少 12 字元。
5. 以相同方式建立 employee 帳號。只有獲授權維護者可以執行 CLI。
6. 設定 NAS HTTPS 反向代理到容器服務，配置請求大小、來源及逾時，再以實際帳號驗收。

資料與附件保存在持久資料目錄；不可將此目錄設成公開網頁目錄。SQLite 放在單一 NAS 主機本地磁碟 volume，不以 SMB/NFS 讓多台應用程式共用即時資料庫。NAS 必須持續運作才能存取服務。

## 同機開發

Python 3.12+，無額外 Python 套件。PowerShell 範例：

```powershell
$env:APP_DEV_LOCALHOST = '1'
$env:APP_HOST = '127.0.0.1'
$env:APP_PORT = '8080'
$env:APP_PUBLIC_ORIGIN = 'http://127.0.0.1:8080'
python -m backend.app create-user --email manager@example.com --name 測試管理者 --role manager
python -m backend.app serve
```

再開啟 http://127.0.0.1:8080 。開發設定僅限同機測試。建立帳號及啟動服務必須使用同一個 APP_DATA_DIR。

## 維護 CLI

```text
python -m backend.app list-users
python -m backend.app reset-password --email USER_EMAIL
python -m backend.app disable-user --email USER_EMAIL
python -m backend.app enable-user --email USER_EMAIL
python -m backend.app set-role --email USER_EMAIL --role employee
```

密碼預設透過 getpass 輸入；--password-stdin 只供安全本機自動化，不能將密碼寫入 Git 或命令列。關閉或重設帳號後，需驗證舊 session 已失效。

## 測試

```text
node --test tests/domain.test.mjs tests/cloud-contract.test.mjs
python -m unittest backend.test_backend -v
```

後端測試以臨時資料與實際 HTTP 驗證登入、角色、CSRF、附件、版本衝突及持久化。Docker/NAS 實機、TLS 反向代理、手機相機及公司備份排程，仍需在部署環境驗收。

## 資料與備份

上傳成功才代表後端已儲存該筆與全部附件。上限為每份 25 MB、每次最多 20 份新附件，整筆原始附件總量另有限制。介面提供 CSV 及包含原始附件的 JSON 交付快照；完整災難復原需要一致性備份整個持久資料目錄。建議停機備份後重新啟動，並在隔離環境驗證還原。勿將運作中的 SQLite 單檔複製當成已驗證備份。

程式碼、.env、資料庫及備份分開；機密與正式資料不得提交 Git。此交付未建立公司 NAS 帳號、未存取公司 NAS、未安排公司備份工作。

## 範圍

已收錄支出、附件、分類、用途、管理者檢閱、更正歷程及匯出。OCR 尚未配置，採手動填寫；收入、付款對帳、薪資計算、正式財務報表、稅額／扣抵認定與郵件密碼復原不在本版。請參閱使用手冊與 AI 交接文件完成公司端設定。
