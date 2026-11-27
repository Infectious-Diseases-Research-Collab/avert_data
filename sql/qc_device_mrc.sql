-- QC: which MRCs each device has recorded, to catch an MRC typed wrongly.
--
-- Uganda only. Runs on the local Postgres import (`enrollee`, `ug_mrcs`).
-- No dates to change; run it as is.
--
-- A tablet normally works at one facility, so each device should show one
-- MRC, or a few when a tablet has moved site. Look hard at rows flagged
-- CHECK: a device/MRC pair with only a handful of records while the same
-- device has many more at another MRC. That is what a mistyped MRC looks
-- like (e.g. device 124 with one record at 052 and the rest at 053).
--
--   deviceid            the tablet
--   mrc, name           the facility recorded on the form
--   records             all records (screened) for this device and MRC
--   enrolled            of those, records with a barcode
--   barcode_mrc_other   enrolled records whose barcode is from another MRC
--   first_date/last_date  interview dates (startdate) seen for this pair
--   device_mrcs         how many different MRCs this device has used
--   device_main_mrc     the MRC this device used most
--   flag                CHECK when this pair looks like a typing mistake

with e as (
    select deviceid,
           lpad(mrc::text, 3, '0')                as mrc,
           startdate::date                        as startdate,
           nullif(trim(barcode::text), '')        as barcode
    from enrollee
),
pairs as (
    select e.deviceid,
           e.mrc,
           count(*)                                               as records,
           count(e.barcode)                                       as enrolled,
           count(*) filter (where e.barcode is not null
                              and substr(e.barcode, 6, 3) <> e.mrc) as barcode_mrc_other,
           min(e.startdate)                                       as first_date,
           max(e.startdate)                                       as last_date
    from e
    group by e.deviceid, e.mrc
),
ranked as (
    select p.*,
           count(*)     over (partition by p.deviceid)                         as device_mrcs,
           max(p.records) over (partition by p.deviceid)                       as device_max,
           first_value(p.mrc) over (partition by p.deviceid
                                    order by p.records desc, p.mrc)            as device_main_mrc
    from pairs p
)
select r.deviceid,
       r.mrc,
       m.name,
       r.records,
       r.enrolled,
       r.barcode_mrc_other,
       r.first_date,
       r.last_date,
       r.device_mrcs,
       r.device_main_mrc,
       case
           when r.barcode_mrc_other > 0                          then 'CHECK'
           when r.device_mrcs > 1
            and r.mrc <> r.device_main_mrc
            and r.records <= 3
            and r.device_max >= 2 * r.records                    then 'CHECK'
           when m.mrc is null                                    then 'CHECK'
           else ''
       end                                                       as flag
from ranked r
left join ug_mrcs m on lpad(m.mrc::text, 3, '0') = r.mrc
order by r.deviceid, r.records desc, r.mrc;
