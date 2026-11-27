-- QC: record-level checks on the Uganda enrollee data.
--
-- Runs on the local Postgres import (`enrollee`, `ug_mrcs`). Returns one row
-- per problem found: which check failed, the record, and what is wrong.
-- No rows means every check passed. Run it as is; the settings in `params`
-- rarely need changing.
--
-- Includes the survey checks from SOP AVERT-DataMgmt-002 (script "01a Survey
-- data quality checks"), adapted to the AVERT fields. Not carried over:
-- uid/uid2 (AVERT has no uid), "barcode not on generated list" (the dashboard's
-- barcode_not_deployed check covers it), and age_at_apr2025 > 12 (AVERT's
-- own age_in_range_ug flag is used instead). Dose details are checked up to
-- vx_doses_received, not vx_any as in the R script.
--
-- To count problems per check, wrap it:
--   select check_name, count(*) from (<this query without the ;>) q group by 1;
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
--     enrolled_not_eligible    has a barcode but an eligibility flag, age range,
--                              or consent says otherwise
--     sample_consent_missing   enrolled but sample consent (consent2) is not 1
--     consented_no_barcode     consent = 1 but no barcode
--     possible_repeat_participant  same name (any word order) as another
--                              enrolled child at the same MRC; check dob and sex.
--                              Needs participantsname: the dashboard export
--                              leaves names out, so on that import it finds nothing
--
--   Missing data (enrolled children; SOP 01a "missing key variables")
--     enrolled_missing_core    sex, dob, age or malaria test is blank
--                              (the dashboard export has no `result` column)
--     missing_vx_card          vx_card is blank
--     missing_vx_any           vx_any (any vaccine doses yes/no) is blank
--     missing_vx_doses         vx_any = 1 but vx_doses_received is blank
--     missing_vx_dose_info     a dose up to vx_doses_received lacks its date,
--                              date source or place
--     missing_malaria_risk     time to bed, house sprayed or bed net last night is blank
--     missing_prevdiag         previous malaria diagnosis, or when, is blank
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
           gender, diagnostic, consent, consent2,
           agemonths_calculated,
           age_eligible, catchment_eligible, mal_test_eligible, consent_eligible,
           ill_noteligible, age_in_range_ug,
           vx_card, vx_any, vx_doses_received,
           vx_dose1_date::date as d1, vx_dose2_date::date as d2,
           vx_dose3_date::date as d3, vx_dose4_date::date as d4,
           vx_dose1_date_ver as d1_ver, vx_dose2_date_ver as d2_ver,
           vx_dose3_date_ver as d3_ver, vx_dose4_date_ver as d4_ver,
           vx_dose1_where as d1_where, vx_dose2_where as d2_where,
           vx_dose3_where as d3_where, vx_dose4_where as d4_where,
           timetobed, structuresprayed, bednetlastnight,
           prevdiag, prevdiag_when,
           -- name in capitals, letters only, words sorted, so "OKELLO JOHN"
           -- and "John  Okello" match (the SOP script cleans names the same way)
           nullif(array_to_string(array(
               select w from regexp_split_to_table(
                   regexp_replace(upper(coalesce(participantsname, '')), '[^A-Z ]', '', 'g'),
                   '\s+') as w
               where w <> '' order by w), ' '), '') as name_key
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
               case when coalesce(ill_noteligible, 0) <> 0     then 'ill_noteligible '    || ill_noteligible end,
               case when age_in_range_ug is distinct from 1    then 'age_in_range_ug '    || coalesce(age_in_range_ug::text, 'blank') end,
               case when consent is distinct from 1            then 'consent '            || coalesce(consent::text, 'blank') end)
    from e
    where barcode is not null
      and (age_eligible is distinct from 1 or catchment_eligible is distinct from 1
           or mal_test_eligible is distinct from 1 or consent_eligible is distinct from 1
           or coalesce(ill_noteligible, 0) <> 0 or age_in_range_ug is distinct from 1
           or consent is distinct from 1)

    union all
    select 'sample_consent_missing', e.*, 'consent2 ' || coalesce(consent2::text, 'blank')
    from e where barcode is not null and consent2 is distinct from 1

    union all
    select 'consented_no_barcode', e.*, 'consent = 1 but barcode is blank'
    from e where consent = 1 and barcode is null

    union all
    select 'possible_repeat_participant', a.*,
           'same name as ' || (
               select string_agg(b.subjid || ' (dob ' || coalesce(b.dob::text, 'blank')
                                 || ', sex ' || coalesce(b.gender::text, 'blank') || ')',
                                 '; ' order by b.subjid)
               from e b
               where b.mrc = a.mrc and b.name_key = a.name_key
                 and b.barcode is not null and b.barcode <> a.barcode)
           || '; this record dob ' || coalesce(a.dob::text, 'blank')
           || ', sex ' || coalesce(a.gender::text, 'blank')
    from e a
    where a.barcode is not null and a.name_key is not null
      and exists (select 1 from e b
                  where b.mrc = a.mrc and b.name_key = a.name_key
                    and b.barcode is not null and b.barcode <> a.barcode)

    -- Missing data (enrolled children) -------------------------------------
    union all
    select 'enrolled_missing_core', e.*,
           'blank: ' || concat_ws(', ',
               case when gender is null               then 'gender' end,
               case when dob is null                  then 'dob' end,
               case when agemonths_calculated is null then 'agemonths_calculated' end,
               case when diagnostic is null           then 'diagnostic' end)
    from e
    where barcode is not null
      and (gender is null or dob is null or agemonths_calculated is null
           or diagnostic is null)

    union all
    select 'missing_vx_card', e.*, 'vx_card is blank'
    from e where barcode is not null and vx_card is null

    union all
    select 'missing_vx_any', e.*, 'vx_any is blank'
    from e where barcode is not null and vx_any is null

    union all
    select 'missing_vx_doses', e.*, 'vx_any = 1 but vx_doses_received is blank'
    from e where barcode is not null and vx_any = 1 and vx_doses_received is null

    union all
    select 'missing_vx_dose_info', e.*,
           'blank: ' || concat_ws(', ',
               case when vx_doses_received >= 1 and d1 is null       then 'dose1 date' end,
               case when vx_doses_received >= 1 and d1_ver is null   then 'dose1 date source' end,
               case when vx_doses_received >= 1 and d1_where is null then 'dose1 place' end,
               case when vx_doses_received >= 2 and d2 is null       then 'dose2 date' end,
               case when vx_doses_received >= 2 and d2_ver is null   then 'dose2 date source' end,
               case when vx_doses_received >= 2 and d2_where is null then 'dose2 place' end,
               case when vx_doses_received >= 3 and d3 is null       then 'dose3 date' end,
               case when vx_doses_received >= 3 and d3_ver is null   then 'dose3 date source' end,
               case when vx_doses_received >= 3 and d3_where is null then 'dose3 place' end,
               case when vx_doses_received >= 4 and d4 is null       then 'dose4 date' end,
               case when vx_doses_received >= 4 and d4_ver is null   then 'dose4 date source' end,
               case when vx_doses_received >= 4 and d4_where is null then 'dose4 place' end)
    from e
    where barcode is not null
      and (   (vx_doses_received >= 1 and (d1 is null or d1_ver is null or d1_where is null))
           or (vx_doses_received >= 2 and (d2 is null or d2_ver is null or d2_where is null))
           or (vx_doses_received >= 3 and (d3 is null or d3_ver is null or d3_where is null))
           or (vx_doses_received >= 4 and (d4 is null or d4_ver is null or d4_where is null)))

    union all
    select 'missing_malaria_risk', e.*,
           'blank: ' || concat_ws(', ',
               case when timetobed is null        then 'timetobed' end,
               case when structuresprayed is null then 'structuresprayed' end,
               case when bednetlastnight is null  then 'bednetlastnight' end)
    from e
    where barcode is not null
      and (timetobed is null or structuresprayed is null or bednetlastnight is null)

    union all
    select 'missing_prevdiag', e.*,
           case when prevdiag is null then 'prevdiag is blank'
                else 'prevdiag = 1 but prevdiag_when is blank' end
    from e
    where barcode is not null
      and (prevdiag is null or (prevdiag = 1 and prevdiag_when is null))

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
