# Cloud pilot acceptance (must execute against dedicated test tenant)

These are required live checks, not assertions that an unconfigured tenant has passed. Provision one manager and two employees. Use their own bearer tokens/public key; never test access using a service-role token, which bypasses RLS.

1. Anonymous REST access to records/documents/audit fails. Private evidence is not accessible through a public object URL.
2. Employee A inserts their pending/uploading record using `Prefer: return=minimal`. A cannot SELECT any record, document or audit row, including their own. Returning an empty record list must not be mistaken for authenticated manager access.
3. Employee A cannot forge created_by, submit reviewed status, change a profile role, access employee B's record/files, PATCH a record, or bypass role restrictions with a changed UI/local storage value.
4. A uploads a file to `userUUID/recordUUID/documentUUID` only after matching document metadata exists. Another user's prefix, missing record and mismatched metadata are rejected. Reusing an existing path cannot overwrite the original.
5. Finalizing a record with no documents, missing object bytes or an incomplete upload fails. Completing all declared objects succeeds. The employee sees a success receipt, without a finance list.
6. Manager sees pending/complete submissions, reads original bytes and confirms SHA-256. Incomplete records stay visibly incomplete and cannot be marked reviewed.
7. Manager updates with `?id=eq.UUID&version=eq.N` and body version N+1. A stale competing update returns zero rows and the UI reports a conflict. Direct nonincrementing update is rejected by trigger.
8. Changing an already-reviewed record without a nonempty correctionReason fails. With a reason, an incremented version succeeds; prior payload exists in audit_events with the real auth actor and server time.
9. Application users cannot DELETE records, documents or audit events, or UPDATE/DELETE originals. Managers cannot provision their own user privileges through browser requests.
10. Setting a profile active=false blocks database and file access even while an old access token remains valid. Check both read and new submission paths.
11. Cloud account signout and mode switching clear finance views and revoke browser object URLs. A new employee login shows no previous manager data in the DOM.
12. Full export/backup reconciles rows and original files. Restore into a separate test environment verifies counts, currency totals and byte hashes; a JSON-only metadata export is not an evidence backup.

Record date, tenant, tester, result and any failures. Obtain owner acceptance before real employee uploads. Configure automated NAS backups separately; this browser app does not schedule them.
