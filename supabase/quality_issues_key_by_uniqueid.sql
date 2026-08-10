-- Identify a data-quality issue by uniqueid, not subjid.
--
-- subjid was part of dq_identity_idx. That breaks in two directions:
--
--   * Where a record has no barcode -- which is exactly what the
--     missing_barcode check fires on -- subjid was the only thing separating
--     one participant's issue from another's. Dropping it outright would
--     collapse them into a single row.
--   * subjid is not stable. Renumbering a duplicated subject ID changes the
--     identity of every issue attached to that record: dismissed issues come
--     back as new, and detected_at resets.
--
-- uniqueid is immutable, always present, and is the record's real identity.
-- barcode stays in the key so the grouped duplicate_barcode check, which is
-- about a barcode rather than a record, keeps one row per barcode. subjid
-- remains as a display column.
--
-- Run in the Supabase SQL editor, then re-dump supabase/avert_dashboard.json.

begin;

-- 1. Carry uniqueid on the issue itself.
alter table public.data_quality_issues add column if not exists uniqueid text;

-- 2. Backfill. Barcode identifies a record uniquely, so it is the first pass;
--    issues with no barcode fall back to subjid, taking the lowest uniqueid
--    where that subjid was itself duplicated.
update public.data_quality_issues d
set uniqueid = e.uniqueid
from public.enrollee e
where d.uniqueid is null
  and d.country = e.country
  and nullif(d.barcode, '') is not null
  and d.barcode = e.barcode;

update public.data_quality_issues d
set uniqueid = s.uniqueid
from (
  select distinct on (country, subjid) country, subjid, uniqueid
  from public.enrollee
  order by country, subjid, uniqueid
) s
where d.uniqueid is null
  and nullif(d.barcode, '') is null
  and d.country = s.country
  and d.subjid = s.subjid;

-- Anything still unmatched refers to a record no longer in enrollee. It will
-- resolve itself on the next refresh; nothing is lost.
do $$
declare n integer;
begin
  select count(*) into n from public.data_quality_issues where uniqueid is null;
  if n > 0 then
    raise notice 'data_quality_issues rows with no uniqueid after backfill: %', n;
  end if;
end $$;

-- 3. Rebuild the identity index on the new key.
drop index if exists public.dq_identity_idx;
create unique index dq_identity_idx on public.data_quality_issues
  using btree (check_code, coalesce(uniqueid, ''::text), coalesce(barcode, ''::text),
               coalesce(field, ''::text), coalesce(related_barcode, ''::text));

-- 4. Replace the function so it populates and keys on uniqueid.

CREATE OR REPLACE FUNCTION public.refresh_quality_issues()
 RETURNS integer
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public'
AS $function$
declare
  n_firing integer;
begin
  drop table if exists _firing;

  create temporary table _firing on commit drop as
  with enrolled as (
    select e.*
    from public.enrollee e
    where e.age_eligible = 1
      and e.mal_test_eligible = 1
      and e.consent_eligible = 1
      and (nullif(e.raw->>'ill_noteligible','')::int is null
           or (e.raw->>'ill_noteligible')::int = 0)
  )

  -- 1. Missing barcode
  select e.country, 'missing_barcode'::text as check_code, 'warning'::text as severity,
         e.uniqueid, e.subjid, e.barcode, e.mrc, 'barcode'::text as field, null::text as related_barcode,
         'Enrolled participant is missing a study barcode.'::text as description,
         'Le participant inclus n''a pas de code-barres d''étude.'::text as description_fr
  from enrolled e
  where e.barcode is null or e.barcode = ''

  union all
  -- 2. Missing core demographics
  select e.country, 'missing_demographics', 'warning',
         e.uniqueid, e.subjid, e.barcode, e.mrc, 'agemonths_calculated/gender/village', null,
         'Missing age, sex, or village for an enrolled participant.',
         'Âge, sexe ou village manquant pour un participant inclus.'
  from enrolled e
  where e.agemonths_calculated is null or e.gender is null
     or e.village is null or e.village = ''

  union all
  -- 3. Missing malaria diagnostic result
  select e.country, 'missing_diagnostic', 'warning',
         e.uniqueid, e.subjid, e.barcode, e.mrc, 'diagnostic', null,
         'Missing malaria diagnostic type for an enrolled participant.',
         'Type de diagnostic du paludisme manquant pour un participant inclus.'
  from enrolled e
  where nullif(e.raw->>'diagnostic','') is null

  union all
  -- 4. Missing vaccine-card status
  select e.country, 'missing_vx_card', 'warning',
         e.uniqueid, e.subjid, e.barcode, e.mrc, 'vx_card', null,
         'Missing vaccine-card status for an enrolled participant.',
         'Statut de la carte de vaccination manquant pour un participant inclus.'
  from enrolled e
  where nullif(e.raw->>'vx_card','') is null

  union all
  -- 5. Missing "received any doses" (yes/no). Skipped when
  -- vx_doses_received_rtss = 1 -- that means this section of the form was
  -- itself skipped by design, so a null vx_any here is expected, not missing.
  select e.country, 'missing_vx_any', 'warning',
         e.uniqueid, e.subjid, e.barcode, e.mrc, 'vx_any', null,
         'Missing "received any vaccine doses" (yes/no) for an enrolled participant.',
         'Réponse manquante à "a reçu des doses de vaccin" pour un participant inclus.'
  from enrolled e
  where e.vx_any is null
    and (nullif(e.raw->>'vx_doses_received_rtss','')::int is null
         or (e.raw->>'vx_doses_received_rtss')::int <> 1)

  union all
  -- 6. vx_any = yes but the number of doses received is missing
  select e.country, 'missing_vx_doses_received', 'warning',
         e.uniqueid, e.subjid, e.barcode, e.mrc, 'vx_doses_received', null,
         'Participant received doses but the number of doses is missing.',
         'Le participant a reçu des doses mais le nombre de doses est manquant.'
  from enrolled e
  where e.vx_any = 1 and nullif(e.raw->>'vx_doses_received','') is null

  union all
  -- 7-10. Missing dose-detail fields (where/date/verification) for each
  -- reported dose, gated by the number of doses actually received.
  select e.country, 'missing_dose_info', 'warning',
         e.uniqueid, e.subjid, e.barcode, e.mrc, d.field, null,
         format('Missing detail (date, location, or verification) for %s.', d.field),
         format('Détail manquant (date, lieu ou vérification) pour %s.', d.field)
  from enrolled e
  cross join lateral (values
    (1, 'vx_dose1_date', e.vx_dose1_date, e.raw->>'vx_dose1_date_ver', e.raw->>'vx_dose1_where'),
    (2, 'vx_dose2_date', e.vx_dose2_date, e.raw->>'vx_dose2_date_ver', e.raw->>'vx_dose2_where'),
    (3, 'vx_dose3_date', e.vx_dose3_date, e.raw->>'vx_dose3_date_ver', e.raw->>'vx_dose3_where'),
    (4, 'vx_dose4_date', e.vx_dose4_date, e.raw->>'vx_dose4_date_ver', e.raw->>'vx_dose4_where')
  ) as d(dose_num, field, dose_date, date_ver, dose_where)
  where coalesce(e.vx_doses_received, 0) >= d.dose_num
    and (d.dose_date is null or nullif(d.date_ver,'') is null or nullif(d.dose_where,'') is null)

  union all
  -- 11. Missing malaria risk-behavior fields. Skipped when
  -- vx_doses_received_rtss = 1 -- that means this section of the form was
  -- itself skipped by design, so nulls here are expected, not missing.
  select e.country, 'missing_malaria_risk', 'warning',
         e.uniqueid, e.subjid, e.barcode, e.mrc, 'timetobed/structuresprayed/bednetlastnight', null,
         'Missing malaria risk-behavior data (bedtime, spraying, or bednet use).',
         'Données sur les comportements à risque de paludisme manquantes (heure du coucher, pulvérisation ou moustiquaire).'
  from enrolled e
  where (nullif(e.raw->>'timetobed','') is null
     or nullif(e.raw->>'structuresprayed','') is null
     or nullif(e.raw->>'bednetlastnight','') is null)
    and (nullif(e.raw->>'vx_doses_received_rtss','')::int is null
         or (e.raw->>'vx_doses_received_rtss')::int <> 1)

  union all
  -- 12. Missing previous-diagnosis info. Skipped when
  -- vx_doses_received_rtss = 1 -- that means this section of the form was
  -- itself skipped by design, so nulls here are expected, not missing.
  select e.country, 'missing_prevdiag', 'warning',
         e.uniqueid, e.subjid, e.barcode, e.mrc, 'prevdiag', null,
         'Missing previous-diagnosis data (or missing date of a reported previous diagnosis).',
         'Données de diagnostic antérieur manquantes (ou date manquante pour un diagnostic antérieur signalé).'
  from enrolled e
  where (nullif(e.raw->>'prevdiag','') is null
     or ((e.raw->>'prevdiag')::int = 1 and nullif(e.raw->>'prevdiag_when','') is null))
    and (nullif(e.raw->>'vx_doses_received_rtss','')::int is null
         or (e.raw->>'vx_doses_received_rtss')::int <> 1)

  union all
  -- 13. Barcode doesn't match the expected per-country prefix
  select e.country, 'barcode_country_mismatch', 'error',
         e.uniqueid, e.subjid, e.barcode, e.mrc, 'barcode', null,
         format('Barcode %s does not match the expected prefix for %s.', e.barcode, e.country),
         format('Le code-barres %s ne correspond pas au préfixe attendu pour %s.', e.barcode, e.country)
  from enrolled e
  where e.barcode is not null and e.barcode <> ''
    and ((e.country = 'UG' and e.barcode not like 'R21U-%')
      or (e.country = 'BF' and e.barcode not like 'R21B-%'))

  union all
  -- 14. Barcode re-entry mismatch (barcode vs barcode2)
  select e.country, 'barcode_reentry_mismatch', 'error',
         e.uniqueid, e.subjid, e.barcode, e.mrc, 'barcode2', null,
         'Barcode and re-entered barcode (barcode2) do not match.',
         'Le code-barres et le code-barres ressaisi (barcode2) ne correspondent pas.'
  from enrolled e
  where nullif(e.barcode,'') is not null and nullif(e.raw->>'barcode2','') is not null
    and e.barcode <> (e.raw->>'barcode2')

  union all
  -- 15. Age outside the program's original catchment reference date
  select e.country, 'age_ineligible_reference_date', 'error',
         e.uniqueid, e.subjid, e.barcode, e.mrc,
         case when e.country = 'UG' then 'age_at_apr2025' else 'age_at_sep2023' end, null,
         format('Age at program reference date (%s months) exceeds the eligibility threshold.',
                case when e.country = 'UG' then e.raw->>'age_at_apr2025' else e.raw->>'age_at_sep2023' end),
         format('L''âge à la date de référence du programme (%s mois) dépasse le seuil d''éligibilité.',
                case when e.country = 'UG' then e.raw->>'age_at_apr2025' else e.raw->>'age_at_sep2023' end)
  from enrolled e
  where (e.country = 'UG' and nullif(e.raw->>'age_at_apr2025','') is not null and (e.raw->>'age_at_apr2025')::numeric > 12)
     or (e.country = 'BF' and nullif(e.raw->>'age_at_sep2023','') is not null and (e.raw->>'age_at_sep2023')::numeric > 12)

  union all
  -- 16. Main consent not marked as provided
  select e.country, 'consent_not_provided', 'error',
         e.uniqueid, e.subjid, e.barcode, e.mrc, 'consent', null,
         'Main consent is not marked as provided for an enrolled participant.',
         'Le consentement principal n''est pas marqué comme fourni pour un participant inclus.'
  from enrolled e
  where nullif(e.raw->>'consent','') is not null and (e.raw->>'consent')::numeric <> 1

  union all
  -- 17. Sample/specimen consent not marked as provided
  select e.country, 'consent2_not_provided', 'error',
         e.uniqueid, e.subjid, e.barcode, e.mrc, 'consent2', null,
         'Sample/specimen consent is not marked as provided for an enrolled participant.',
         'Le consentement pour l''échantillon n''est pas marqué comme fourni pour un participant inclus.'
  from enrolled e
  where nullif(e.raw->>'consent2','') is not null and (e.raw->>'consent2')::numeric <> 1

  union all
  -- 18. Possible duplicate participant: SAME date of birth AND a near-identical
  -- name, within the same country + facility. Tightened from the earlier
  -- similarity>0.45 (too noisy): now requires an exact DOB match plus a name
  -- trigram distance (1 - pg_trgm similarity) <= 0.075, i.e. similarity >= 0.925
  -- (all but identical). a.barcode < b.barcode makes each pair fire once.
  -- related_barcode (b.barcode) carries the specific match so a participant
  -- matching two or more others produces one row per match instead of
  -- colliding on (check_code, subjid, barcode, field) alone.
  select a.country, 'possible_duplicate_name', 'warning',
         a.uniqueid, a.subjid, a.barcode, a.mrc, 'participantsname', b.barcode,
         format('Participant has the same date of birth and a near-identical name to barcode %s at the same facility (name distance %s).',
                b.barcode, round((1 - similarity(upper(a.raw->>'participantsname'), upper(b.raw->>'participantsname')))::numeric, 3)),
         format('Le participant a la même date de naissance et un nom quasi identique au code-barres %s dans la même formation sanitaire (distance du nom %s).',
                b.barcode, round((1 - similarity(upper(a.raw->>'participantsname'), upper(b.raw->>'participantsname')))::numeric, 3))
  from enrolled a
  join enrolled b
    on a.country = b.country and a.mrc = b.mrc and a.barcode < b.barcode
  where a.dob is not null and a.dob = b.dob
    and nullif(a.raw->>'participantsname','') is not null
    and nullif(b.raw->>'participantsname','') is not null
    and (1 - similarity(upper(a.raw->>'participantsname'), upper(b.raw->>'participantsname'))) <= 0.075

  union all
  -- 19. Enrollee barcode not on the deployed/allocated list for its country.
  -- Only evaluated for a country that actually has a deployed list loaded, so
  -- an empty or not-yet-populated deployed_barcodes table never floods issues.
  select e.country, 'barcode_not_deployed', 'error',
         e.uniqueid, e.subjid, e.barcode, e.mrc, 'barcode', null,
         format('Barcode %s is not on the deployed barcode list for %s.', e.barcode, e.country),
         format('Le code-barres %s ne figure pas sur la liste des codes-barres déployés pour %s.', e.barcode, e.country)
  from enrolled e
  where e.barcode is not null and e.barcode <> ''
    and exists (select 1 from public.deployed_barcodes d where d.country = e.country)
    and not exists (
      select 1 from public.deployed_barcodes d
      where d.country = e.country and d.barcode = e.barcode
    )

  union all
  -- 20. The same barcode on more than one record. Barcode is the key clinic
  -- and lab data are joined on, so a collision attaches one child's sample
  -- results to another child's record. Checked over every record rather than
  -- only enrolled ones: a screened-out record still consumed that barcode.
  -- Grouped, so a barcode used by three records raises one issue, not three.
  select e.country, 'duplicate_barcode', 'error',
         min(e.uniqueid), min(e.subjid), e.barcode, min(e.mrc), 'barcode', null,
         format('Barcode %s is recorded on %s different records (subject IDs %s).',
                e.barcode, count(*), string_agg(distinct e.subjid, ', ')),
         format('Le code-barres %s figure sur %s enregistrements differents (identifiants %s).',
                e.barcode, count(*), string_agg(distinct e.subjid, ', '))
  from public.enrollee e
  where nullif(e.barcode, '') is not null
  group by e.country, e.barcode
  having count(*) > 1

  union all
  -- 21. The same subject ID on more than one record. A warning, not an error:
  -- subject ID is not what clinic and lab data are joined on. It happens when
  -- a device loses its database -- uninstalling the app does this -- because
  -- the counter is derived from the device's own table and restarts, reissuing
  -- IDs already given out. It still needs correcting: anything analysed by
  -- subject ID would silently merge two children.
  select e.country, 'duplicate_subjid', 'warning',
         min(e.uniqueid), e.subjid, min(e.barcode), min(e.mrc), 'subjid', null,
         format('Subject ID %s is used by %s different participants (barcodes %s).',
                e.subjid, count(*), string_agg(e.barcode, ', ' order by e.barcode)),
         format('L''identifiant %s est utilise par %s participants differents (codes-barres %s).',
                e.subjid, count(*), string_agg(e.barcode, ', ' order by e.barcode))
  from public.enrollee e
  where nullif(e.subjid, '') is not null
  group by e.country, e.subjid
  having count(*) > 1;

  select count(*) into n_firing from _firing;

  -- Upsert firing issues: insert new, re-open previously resolved.
  insert into public.data_quality_issues
    (country, check_code, severity, uniqueid, subjid, barcode, mrc, field, related_barcode, description, description_fr, status, detected_at, resolved_at)
  select country, check_code, severity, uniqueid, subjid, barcode, mrc, field, related_barcode, description, description_fr, 'open', now(), null
  from _firing
  on conflict (check_code, coalesce(uniqueid,''), coalesce(barcode,''), coalesce(field,''), coalesce(related_barcode,''))
  do update set
    country        = excluded.country,
    severity       = excluded.severity,
    subjid         = excluded.subjid,
    mrc            = excluded.mrc,
    description    = excluded.description,
    description_fr = excluded.description_fr,
    status         = 'open',
    detected_at    = case when data_quality_issues.status = 'resolved'
                          then now() else data_quality_issues.detected_at end,
    resolved_at    = null
  -- Never disturb a manually dismissed issue: a user reviewed it and marked it
  -- "not a problem", so even if it keeps firing it stays out of the open list.
  where data_quality_issues.status <> 'dismissed';

  -- Resolve open issues that no longer fire.
  update public.data_quality_issues d
  set status = 'resolved', resolved_at = now()
  where d.status = 'open'
    and not exists (
      select 1 from _firing f
      where f.check_code = d.check_code
        and coalesce(f.uniqueid,'')        = coalesce(d.uniqueid,'')
        and coalesce(f.barcode,'')         = coalesce(d.barcode,'')
        and coalesce(f.field,'')           = coalesce(d.field,'')
        and coalesce(f.related_barcode,'') = coalesce(d.related_barcode,'')
    );

  return n_firing;
end;
$function$;
commit;
