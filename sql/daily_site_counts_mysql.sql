-- Daily counts per MRC, to compare against the data collectors' daily reports.
-- MySQL 8.0+ version of daily_site_counts.sql (Postgres); same output.
--
-- Uganda only. Expects the dashboard's enrollee CSV imported as `enrollee`,
-- plus `ug_mrcs` (mrc code, facility name).
-- Change report_date below, then run the whole thing.
--
-- Columns line up with the site report:
--   screened           "Number screened"   every record started that day
--   enrolled           "Number enrolled"   records with a barcode
--   with_card          "With Card"         enrolled, vx_card = 1
--   without_card       "Without Card"      enrolled, vx_card = 0 or blank
--   total_enrollments  "Total enrollments" enrolled from the start up to report_date
--
-- The date is the interview date (startdate), not when the phone uploaded.
-- Every MRC in ug_mrcs is listed, even with no records, so a site that
-- reported work but has nothing in the data shows up as zeros. An MRC found
-- in enrollee but missing from ug_mrcs is listed too, with no name.
--
-- Casts make this work whether the CSV import typed mrc/startdate/vx_card
-- as text or as numbers/dates (e.g. mrc 53 vs '053').

with params as (
    select date '2026-11-26' as report_date
),
e as (
    select lpad(cast(mrc as char), 3, '0')          as mrc,
           cast(startdate as date)                  as startdate,
           nullif(trim(cast(barcode as char)), '')  as barcode,
           coalesce(cast(vx_card as char), '')      as vx_card
    from enrollee
),
mrcs as (
    select lpad(cast(mrc as char), 3, '0') as mrc, name from ug_mrcs
    union
    select distinct e.mrc, null from e
    where e.mrc not in (select lpad(cast(mrc as char), 3, '0') from ug_mrcs)
)
select
    p.report_date                                                         as `date`,
    m.mrc,
    m.name,
    coalesce(sum(e.startdate = p.report_date), 0)                         as screened,
    coalesce(sum(e.startdate = p.report_date
                 and e.barcode is not null), 0)                           as enrolled,
    coalesce(sum(e.startdate = p.report_date and e.barcode is not null
                 and e.vx_card = '1'), 0)                                 as with_card,
    coalesce(sum(e.startdate = p.report_date and e.barcode is not null
                 and e.vx_card <> '1'), 0)                                as without_card,
    coalesce(sum(e.startdate <= p.report_date
                 and e.barcode is not null), 0)                           as total_enrollments
from mrcs m
cross join params p
left join e on e.mrc = m.mrc
group by p.report_date, m.mrc, m.name
order by m.mrc;
