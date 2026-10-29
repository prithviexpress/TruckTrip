# TripPlanning in Mendix (via mxcli)

`tripplanning.mdl` rebuilds the plan step of `trip_demand_analysis.py` inside Mendix:
safe start, hard stop, bay list and stop minutes per vendor + slot, as a module
`TripPlanning` with entities, one Java action, two microflows and a weekly scheduled
event. Written for [mxcli](https://github.com/mendixlabs/mxcli) (Mendix Labs, alpha,
tested by them on Mendix 11.6).

## Apply
```
# 1. install mxcli: download mxcli-<os>-<arch> from the releases page (nightly), chmod +x
# 2. close Studio Pro, commit your project
mxcli check mendix/tripplanning.mdl                              # syntax + rules
mxcli -p path/to/App.mpr -c "execute script 'mendix/tripplanning.mdl'"
mxcli -p path/to/App.mpr -c "describe java action TripPlanning.GeneratePlan"
# 3. reopen in Studio Pro, build (F7). Fix any Java compile error in the generated
#    javasource/tripplanning/actions/GeneratePlan.java, then re-import with
#    'create or modify java action' if you want the .mdl to stay the source.
```
Not validated here: I could not run `mxcli check` in this environment. Expect a
syntax nit or two on the first run; the statement forms follow the mxcli docs and
examples in the repo.

## What runs
- `SE_GeneratePlan`: retires the active plan, runs `GeneratePlan` for all days and
  once per weekday. Scheduled weekly (Sunday 22:00, disabled until you enable it);
  `ACT_GeneratePlanNow` wraps it for a button.
- `GeneratePlan` (Java): trips = truck + gate-entry time; drops entries more than
  EarlyCapH/LateCapH hours from the slot; safe start = P(QStart) rounded up, hard stop
  = P(QStop) + grace rounded up; slot-wide fallback under MinTrucks; bay list = bays on
  >= 10% of trips, top 5, critical first; stop minutes = P(QStopMinutes) of daily
  standard minutes x (1 + allowance) + fixed, rounded up to 5, else ManualMin.
  Same percentile math as the Python (linear interpolation).

## Data loads
- `GateEntry`: the parquet rows (Excel/CSV import or REST).
- `ItemStandard`: pack size, standard minutes, basis, IsCritical.
- `Bay`: codes; `IsHotLane` for reserved bays.
- `PlanSettings`: created with defaults on first run; edit on a page.

## Cross-check before go-live
Run the Python app on the same history window and diff `VendorSlotPlan` against the
"Safe start & hard stop" sheet. Differences point to data-load issues, not math.
