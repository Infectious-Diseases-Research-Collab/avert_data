-- Tidy two things in audittrail.
--
--   1. old_startdate / new_startdate become a single startdate. It is the
--      interview date -- context for the change, not a value that changes --
--      and the two were identical on both sides of all 408 rows recorded
--      before this.
--
--   2. Remove the subject-id correction rows. They were written with
--      new_lastmod as a bare date ('2026-08-10') where every device-sent row
--      carries a full timestamp. new_lastmod is part of the upsert key, so
--      re-uploading with a proper timestamp would insert fresh rows and leave
--      these behind as orphans. The next upload recreates all 22 correctly.
--
-- Run in the Supabase SQL editor, then re-dump supabase/avert_dashboard.json.
--
-- Afterwards, on the pipeline machine:
--     python process_data.py        # rebuilds audittrail.csv with the new columns
--     python upload_to_supabase.py  # re-uploads, recreating the 22 correction rows
--
-- process_data.py notices the changed columns by itself and forces a full
-- rebuild; nothing needs deleting by hand.

begin;

-- 1. One startdate column. Guarded so the whole file can be re-run: on a
--    second pass old_startdate is already gone, and an unguarded update
--    referencing it would abort the transaction.
alter table public.audittrail add column if not exists startdate text;

do $$
begin
  if exists (
    select 1 from information_schema.columns
    where table_schema = 'public'
      and table_name = 'audittrail'
      and column_name = 'old_startdate'
  ) then
    update public.audittrail
    set startdate = old_startdate
    where startdate is null;
  else
    raise notice 'old_startdate is already gone — nothing to carry over';
  end if;
end $$;

alter table public.audittrail drop column if exists old_startdate;
alter table public.audittrail drop column if exists new_startdate;

-- 2. Correction rows, to be rewritten with a proper timestamp on the next run.
delete from public.audittrail
where new_sourcefile = 'corrections/subjid_corrections.csv';

commit;
