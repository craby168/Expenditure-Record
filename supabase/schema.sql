-- Fresh, dedicated Supabase project only. Run as database owner in SQL editor.
-- Never put a service_role key in the browser. Disable public signup in Auth settings.
begin;
create table public.profiles (
  id uuid primary key references auth.users(id),
  role text not null check (role in ('manager','employee')),
  display_name text not null default '',
  active boolean not null default true
);
create function public.is_member() returns boolean language sql stable security definer set search_path = '' as $$
  select exists(select 1 from public.profiles where id = auth.uid() and active)
$$;
create function public.is_manager() returns boolean language sql stable security definer set search_path = '' as $$
  select exists(select 1 from public.profiles where id = auth.uid() and active and role='manager')
$$;
revoke all on function public.is_member() from public;
revoke all on function public.is_manager() from public;
grant execute on function public.is_member(), public.is_manager() to authenticated;

create table public.records (
  id uuid primary key,
  payload jsonb not null check (jsonb_typeof(payload)='object'),
  created_by uuid not null default auth.uid() references auth.users(id),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  version integer not null default 1 check (version>0)
);
create index records_date_idx on public.records ((payload->>'date'));
create table public.documents (
  id uuid primary key,
  record_id uuid not null references public.records(id),
  object_path text unique not null,
  filename text not null check (length(filename) between 1 and 255),
  mime text not null default 'application/octet-stream',
  size_bytes bigint not null check (size_bytes between 1 and 26214400),
  sha256 text not null check (sha256 ~ '^[a-f0-9]{64}$'),
  created_by uuid not null default auth.uid() references auth.users(id),
  created_at timestamptz not null default now()
);
create index documents_record_idx on public.documents(record_id);
create function public.submission_complete(target uuid) returns boolean language sql stable security definer set search_path = '' as $$
  select exists(select 1 from public.documents where record_id=target)
    and not exists(select 1 from public.documents d where d.record_id=target and not exists(
      select 1 from storage.objects o where o.bucket_id='evidence' and o.name=d.object_path
    ))
$$;
revoke all on function public.submission_complete(uuid) from public;
-- Not granted directly: only trusted trigger/finalizer uses this helper.
create table public.audit_events (
  id bigint generated always as identity primary key,
  record_id uuid not null references public.records(id),
  actor uuid references auth.users(id),
  action text not null,
  version integer not null,
  old_payload jsonb,
  new_payload jsonb,
  at timestamptz not null default now()
);
create function public.can_attach(target uuid) returns boolean language sql stable security definer set search_path = '' as $$
  select public.is_member() and exists (
    select 1 from public.records where id=target and
    (public.is_manager() or (created_by=auth.uid() and payload->>'status'='pending' and payload->>'uploadState'='uploading'))
  )
$$;
revoke all on function public.can_attach(uuid) from public;
grant execute on function public.can_attach(uuid) to authenticated;

create function public.guard_record() returns trigger language plpgsql security definer set search_path = '' as $$
begin
  if tg_op='INSERT' then
    if not public.is_member() or new.created_by is distinct from auth.uid() then raise exception 'Unauthorized creator'; end if;
    new.version := 1;
    new.created_at := now();
    if not public.is_manager() and new.payload->>'status' is distinct from 'pending' then raise exception 'Employee may only submit pending records'; end if;
  else
    if not public.is_manager() and not (
      public.is_member() and old.created_by=auth.uid() and
      old.payload->>'status'='pending' and old.payload->>'uploadState'='uploading' and
      new.payload->>'uploadState'='complete' and
      (new.payload-'uploadState')=(old.payload-'uploadState') and
      public.submission_complete(old.id)
    ) then raise exception 'Manager required'; end if;
    if new.id<>old.id or new.created_by<>old.created_by or new.created_at<>old.created_at then raise exception 'Immutable record identity'; end if;
    if new.version <> old.version+1 then raise exception 'Expected next revision'; end if;
    if old.payload->>'status'='reviewed' and coalesce(length(trim(new.payload->>'correctionReason')),0)=0 then raise exception 'Correction reason required for reviewed record'; end if;
  end if;
  if coalesce(new.payload->>'status','') not in ('pending','reviewed') then raise exception 'Invalid status'; end if;
  if new.payload->>'status'='reviewed' and (new.payload->>'uploadState' is distinct from 'complete' or not public.submission_complete(new.id)) then raise exception 'Evidence incomplete'; end if;
  if coalesce(new.payload->>'amount','') !~ '^[0-9]{1,12}(\.[0-9]{1,2})?$' then raise exception 'Invalid amount'; end if;
  if (new.payload->>'amount')::numeric<=0 then raise exception 'Amount must be positive'; end if;
  if coalesce(new.payload->>'date','') !~ '^\d{4}-\d{2}-\d{2}$' then raise exception 'Invalid date'; end if;
  perform (new.payload->>'date')::date;
  if coalesce(length(trim(new.payload->>'vendor')),0)=0 then raise exception 'Vendor required'; end if;
  if coalesce(new.payload->>'currency','') !~ '^[A-Z]{3}$' then raise exception 'Invalid currency'; end if;
  if octet_length(new.payload::text)>262144 then raise exception 'Payload too large'; end if;
  new.updated_at := now();
  return new;
end $$;
revoke all on function public.guard_record() from public;
create trigger guard_record before insert or update on public.records for each row execute function public.guard_record();
create function public.audit_record() returns trigger language plpgsql security definer set search_path = '' as $$
begin
  insert into public.audit_events(record_id,actor,action,version,old_payload,new_payload)
  values(new.id,auth.uid(),tg_op,new.version,case when tg_op='UPDATE' then old.payload else null end,new.payload);
  return new;
end $$;
revoke all on function public.audit_record() from public;
create trigger audit_record after insert or update on public.records for each row execute function public.audit_record();
create function public.guard_document() returns trigger language plpgsql security definer set search_path = '' as $$
declare parent public.records;
begin
  select * into parent from public.records where id=new.record_id for update;
  if not found or not public.is_member() or new.created_by is distinct from auth.uid() then raise exception 'Unauthorized evidence'; end if;
  if not public.is_manager() and not (parent.created_by=auth.uid() and parent.payload->>'status'='pending' and parent.payload->>'uploadState'='uploading') then raise exception 'Submission is closed'; end if;
  if new.object_path is distinct from (auth.uid()::text || '/' || new.record_id::text || '/' || new.id::text) then raise exception 'Invalid evidence path'; end if;
  return new;
end $$;
revoke all on function public.guard_document() from public;
create trigger guard_document before insert on public.documents for each row execute function public.guard_document();

alter table public.profiles enable row level security;
alter table public.records enable row level security;
alter table public.documents enable row level security;
alter table public.audit_events enable row level security;
revoke all on public.profiles,public.records,public.documents,public.audit_events from anon,authenticated;
grant select on public.profiles to authenticated;
grant select,insert,update on public.records to authenticated;
grant select,insert on public.documents to authenticated;
grant select on public.audit_events to authenticated;
create policy own_profile on public.profiles for select to authenticated using (id=auth.uid() and active);
-- Employees cannot SELECT even their own submission. No client-side role switch can grant this access.
create policy manager_records_read on public.records for select to authenticated using (public.is_manager());
create policy members_submit on public.records for insert to authenticated with check (public.is_member() and created_by=auth.uid());
create policy manager_records_update on public.records for update to authenticated using (public.is_manager()) with check (public.is_manager());
create policy manager_documents_read on public.documents for select to authenticated using (public.is_manager());
create policy members_documents_insert on public.documents for insert to authenticated with check (public.can_attach(record_id) and created_by=auth.uid());
create policy manager_audit_read on public.audit_events for select to authenticated using (public.is_manager());

insert into storage.buckets(id,name,public,file_size_limit) values ('evidence','evidence',false,26214400);
create function public.can_upload_object(object_name text) returns boolean language plpgsql stable security definer set search_path = '' as $$
begin
  if object_name !~ '^[a-f0-9-]{36}/[a-f0-9-]{36}/[a-f0-9-]{36}$' then return false; end if;
  return public.is_member() and exists (
    select 1 from public.documents d where d.object_path=object_name and d.created_by=auth.uid()
      and split_part(object_name,'/',1)=auth.uid()::text and public.can_attach(d.record_id)
  );
end $$;
revoke all on function public.can_upload_object(text) from public;
grant execute on function public.can_upload_object(text) to authenticated;
create policy manager_evidence_read on storage.objects for select to authenticated using (bucket_id='evidence' and public.is_manager());
create policy members_evidence_create on storage.objects for insert to authenticated with check (
  bucket_id='evidence' and public.can_upload_object(name)
);
create function public.finalize_submission(p_record_id uuid) returns void language plpgsql security definer set search_path = '' as $$
begin
  if not public.is_member() then raise exception 'Membership required'; end if;
  perform 1 from public.records where id=p_record_id and
    (created_by=auth.uid() or public.is_manager()) and payload->>'status'='pending' and payload->>'uploadState'='uploading' for update;
  if not found then raise exception 'Submission unavailable'; end if;
  if not public.submission_complete(p_record_id) then raise exception 'Evidence incomplete'; end if;
  update public.records set payload=jsonb_set(payload,'{uploadState}','"complete"'::jsonb),version=version+1 where id=p_record_id;
end $$;
revoke all on function public.finalize_submission(uuid) from public;
grant execute on function public.finalize_submission(uuid) to authenticated;
-- No object UPDATE/DELETE policy: originals cannot be overwritten by application accounts.
-- Profiles and role grants are provisioned only via trusted Auth/SQL administration.
commit;
