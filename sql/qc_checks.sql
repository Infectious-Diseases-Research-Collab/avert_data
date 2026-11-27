-- QC: record-level checks on the Uganda enrollee data.
--
-- Runs on the local Postgres import (`enrollee`, `ug_mrcs`). Returns one row
-- per problem found: which check failed, the record, and what is wrong.
-- No rows means every check passed. Run it as is; the settings in `params`
-- rarely need changing.
--
-- Checks, roughly most serious first:
--
--   IDs and site
--     barcode_mrc_mismatch     barcode is from a different MRC than the form
--     subjid_mrc_mismatch      MRC inside the subjid differs from mrc
--     subjid_device_mismatch   device inside the subjid differs from deviceid
--     mrc_confirm_mismatch     mrc and its confirmation mrc2 differ
--     device_confirm_mismatch  deviceid and deviceid2 differ
--     barcode_confirm_mismatch barcode and its confirmation barcode2 differ
--     mrc_unknown              mrc is not in ug_mrcs
--     subjid_format            subjid is not 11 digits starting with 1
--     barcode_format           barcode is not R21U-###-XXXX
--     duplicate_subjid         subjid used by more than one record
--     duplicate_barcode        barcode used by more than one record
--
--   Eligibility and enrolment
--     enrolled_not_eligible    has a barcode but an eligibility flag or consent is not 1
--     consented_no_barcode     consent = 1 but no barcode
--     enrolled_missing_core    enrolled but sex, dob or malaria test is blank
--                              (the dashboard export has no `result` column)
--
--   Dates and times
--     startdate_out_of_range   before params.study_start or after today
--     startdate_not_starttime  startdate differs from the date of starttime
--     stop_before_start        stoptime earlier than starttime
--     interview_too_short      enrolled interview under params.min_minutes
--     dob_after_startdate      date of birth after the interview
--
--   Vaccination
--     vx_dose_after_interview  a dose date after the interview date
--     vx_dose_before_dob       a dose date before date of birth
--     vx_dose_out_of_order     a dose date not after the previous dose
--     vx_dose_count_mismatch   vx_doses_received differs from the number of dose dates
--     vx_none_but_doses        vx_any = 0 but dose dates are filled in

with params as (
    select date '2026-08-27' as study_start,   -- first Uganda enrolments
           3                 as min_minutes
),
e as (
    select subjid,
           nullif(trim(barcode::text), '')  as barcode,
           nullif(trim(barcode2::text), '') as barcode2,
           deviceid,
           deviceid2,
           lpad(mrc::text, 3, '0')          as mrc,
           lpad(nullif(trim(mrc2::text), ''), 3, '0') as mrc2,
           startdate::date                  as startdate,
           starttime::timestamp             as starttime,
           stoptime::timestamp              as stoptime,
           dob::date                        as dob,
           gender, diagnostic, consent,
           age_eligible, catchment_eligible, mal_test_eligible, consent_eligible,
           vx_any, vx_doses_received,
           vx_dose1_date::date as d1, vx_dose2_date::date as d2,
           vx_dose3_date::date as d3, vx_dose4_date::date as d4
    from enrollee
),
issues as (
    -- IDs and site --------------------------------------------------------
    select 'barcode_mrc_mismatch' as check_name, e.*,
           'barcode MRC ' || substr(barcode, 6, 3) || ', form MRC ' || mrc as detail
    from e where barcode is not null and substr(barcode, 6, 3) <> mrc

    union all
    select 'subjid_mrc_mismatch', e.*,
           'subjid MRC ' || substr(subjid, 5, 3) || ', form MRC ' || mrc
    from e where substr(subjid, 5, 3) <> mrc

    union all
    select 'subjid_device_mismatch', e.*,
           'subjid device ' || substr(subjid, 2, 3) || ', deviceid ' || deviceid
    from e where substr(subjid, 2, 3) <> lpad(deviceid::text, 3, '0')

    union all
    select 'mrc_confirm_mismatch', e.*, 'mrc ' || mrc || ', mrc2 ' || mrc2
    from e where mrc2 is not null and mrc2 <> mrc

    union all
    select 'device_confirm_mismatch', e.*,
           'deviceid ' || deviceid || ', deviceid2 ' || deviceid2
    from e where deviceid2 is not null and deviceid2 <> deviceid

    union all
    select 'barcode_confirm_mismatch', e.*,
           'barcode ' || barcode || ', barcode2 ' || barcode2
    from e where barcode2 is not null and barcode2 <> coalesce(barcode, '')

    union all
    select 'mrc_unknown', e.*, 'MRC ' || mrc || ' is not in ug_mrcs'
    from e where mrc not in (select lpad(mrc::text, 3, '0') from ug_mrcs)

    union all
    select 'subjid_format', e.*, 'subjid ' || coalesce(subjid, '(blank)')
    from e where subjid is null or subjid !~ '^1[0-9]{10}$'

    union all
    select 'barcode_format', e.*, 'barcode ' || barcode
    from e where barcode is not null and barcode !~ '^R21U-[0-9]{3}-[A-Z0-9]{4}$'

    union all
    select 'duplicate_subjid', e.*,
           count(*) over (partition by subjid) || ' records share this subjid'
    from e where subjid in (select subjid from e group by subjid having count(*) > 1)

    union all
    select 'duplicate_barcode', e.*,
           count(*) over (partition by barcode) || ' records share this barcode'
    from e where barcode in (select barcode from e where barcode is not null
                             group by barcode having count(*) > 1)

    -- Eligibility and enrolment -------------------------------------------
    union all
    select 'enrolled_not_eligible', e.*,
           concat_ws(', ',
               case when age_eligible is distinct from 1       then 'age_eligible '       || coalesce(age_eligible::text, 'blank') end,
               case when catchment_eligible is distinct from 1 then 'catchment_eligible ' || coalesce(catchment_eligible::text, 'blank') end,
               case when mal_test_eligible is distinct from 1  then 'mal_test_eligible '  || coalesce(mal_test_eligible::text, 'blank') end,
               case when consent_eligible is distinct from 1   then 'consent_eligible '   || coalesce(consent_eligible::text, 'blank') end,
               case when consent is distinct from 1            then 'consent '            || coalesce(consent::text, 'blank') end)
    from e
    where barcode is not null
      and (age_eligible is distinct from 1 or catchment_eligible is distinct from 1
           or mal_test_eligible is distinct from 1 or consent_eligible is distinct from 1
           or consent is distinct from 1)

    union all
    select 'consented_no_barcode', e.*, 'consent = 1 but barcode is blank'
    from e where consent = 1 and barcode is null

    union all
    select 'enrolled_missing_core', e.*,
           'blank: ' || concat_ws(', ',
               case when gender is null     then 'gender' end,
               case when dob is null        then 'dob' end,
               case when diagnostic is null then 'diagnostic' end)
    from e
    where barcode is not null
      and (gender is null or dob is null or diagnostic is null)

    -- Dates and times -----------------------------------------------------
    union all
    select 'startdate_out_of_range', e.*, 'startdate ' || coalesce(startdate::text, 'blank')
    from e cross join params p
    where startdate is null or startdate < p.study_start or startdate > current_date

    union all
    select 'startdate_not_starttime', e.*,
           'startdate ' || startdate || ', starttime ' || starttime
    from e where starttime is not null and startdate <> starttime::date

    union all
    select 'stop_before_start', e.*,
           'starttime ' || starttime || ', stoptime ' || stoptime
    from e where stoptime < starttime

    union all
    select 'interview_too_short', e.*,
           round(extract(epoch from stoptime - starttime) / 60.0, 1) || ' minutes'
    from e cross join params p
    where barcode is not null
      and stoptime - starttime < make_interval(mins => p.min_minutes)

    union all
    select 'dob_after_startdate', e.*, 'dob ' || dob || ', startdate ' || startdate
    from e where dob > startdate

    -- Vaccination ---------------------------------------------------------
    union all
    select 'vx_dose_after_interview', e.*,
           'startdate ' || startdate || '; ' || concat_ws(', ',
               case when d1 > startdate then 'dose1 ' || d1 end,
               case when d2 > startdate then 'dose2 ' || d2 end,
               case when d3 > startdate then 'dose3 ' || d3 end,
               case when d4 > startdate then 'dose4 ' || d4 end)
    from e where greatest(d1, d2, d3, d4) > startdate

    union all
    select 'vx_dose_before_dob', e.*, 'dob ' || dob || ', earliest dose ' || least(d1, d2, d3, d4)
    from e where least(d1, d2, d3, d4) < dob

    union all
    select 'vx_dose_out_of_order', e.*,
           concat_ws(', ', 'dose1 ' || d1, 'dose2 ' || d2, 'dose3 ' || d3, 'dose4 ' || d4)
    from e where d2 <= d1 or d3 <= d2 or d4 <= d3

    union all
    select 'vx_dose_count_mismatch', e.*,
           'vx_doses_received ' || coalesce(vx_doses_received::text, 'blank') || ', dose dates '
           || ((d1 is not null)::int + (d2 is not null)::int + (d3 is not null)::int + (d4 is not null)::int)
    from e
    where barcode is not null
      and coalesce(vx_doses_received, 0)
          <> (d1 is not null)::int + (d2 is not null)::int + (d3 is not null)::int + (d4 is not null)::int

    union all
    select 'vx_none_but_doses', e.*, 'vx_any 0 but a dose date is filled in'
    from e where vx_any = 0 and coalesce(d1, d2, d3, d4) is not null
)
select check_name, subjid, barcode, deviceid, mrc, startdate, detail
from issues
order by check_name, startdate, subjid;
