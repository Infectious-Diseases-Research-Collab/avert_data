-- Adds a Data Quality check for a barcode submitted more than once for
-- vaccination_status (or blood_smear -- both key on barcode the same way).
--
-- This can't be added as another branch of refresh_quality_issues()'s scan,
-- the way duplicate_barcode (enrollee) and duplicate_subjid are: those work
-- because enrollee upserts on uniqueid, so two records sharing a subjid or a
-- barcode both still land in the table and a live-table scan finds them.
-- vaccination_status and blood_smear upsert on barcode itself (their
-- primary key), so when two interviews share a barcode, only the most
-- recently modified one ever reaches the table -- upload_to_supabase.py
-- drops the other one before Supabase ever sees it (see
-- dedupe_on_conflict() in that script). By the time refresh_quality_issues()
-- runs, there is only ever one row per barcode to find. The collision is
-- only ever visible at upload time, from the pre-collapse CSV -- so the
-- Python loader reports it directly through this function instead, using
-- the same identity/open/resolve lifecycle as every other check.
--
-- Run in the Supabase SQL editor, then re-dump supabase/avert_dashboard.json.

CREATE OR REPLACE FUNCTION public.sync_duplicate_barcode_issues(p_check_code text, p_issues jsonb)
 RETURNS integer
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public'
AS $function$
declare
  n integer;
begin
  -- Open (or reopen) an issue for every collision this run detected.
  -- p_issues is a JSON array of {country, barcode, description, description_fr}.
  insert into public.data_quality_issues
    (country, check_code, severity, barcode, description, description_fr, status, detected_at, resolved_at)
  select
    i->>'country', p_check_code, 'warning',
    i->>'barcode', i->>'description', i->>'description_fr',
    'open', now(), null
  from jsonb_array_elements(p_issues) as i
  on conflict (check_code, coalesce(uniqueid,''), coalesce(barcode,''), coalesce(field,''), coalesce(related_barcode,''))
  do update set
    country        = excluded.country,
    description    = excluded.description,
    description_fr = excluded.description_fr,
    status         = 'open',
    detected_at    = case when data_quality_issues.status = 'resolved'
                          then now() else data_quality_issues.detected_at end,
    resolved_at    = null
  -- Never disturb a manually dismissed issue, same rule as refresh_quality_issues().
  where data_quality_issues.status <> 'dismissed';

  get diagnostics n = row_count;

  -- Resolve a previously reported collision for this check that isn't
  -- firing this run -- e.g. the source data was corrected (a device
  -- re-upload with a fixed barcode) so there's nothing left to collapse.
  update public.data_quality_issues d
  set status = 'resolved', resolved_at = now()
  where d.status = 'open'
    and d.check_code = p_check_code
    and not exists (
      select 1 from jsonb_array_elements(p_issues) as i
      where (i->>'barcode') = d.barcode
    );

  return n;
end;
$function$;
