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

## Pages (third script)
`tripplanning_pages.mdl` adds the pages. Open **Home_TripPlanning**: it links to all of them. The
script does not touch your navigation; set that page as home or add one menu item.

| Group | Pages | Shows |
|---|---|---|
| Plan | Plan_Overview, Plan_History, Plan_Detail, PlanRun_Log | the plan in force and every version; each plan row's bays and minutes; **why**: rows read, trips kept, percentiles, every bay and minute decision |
| Live | Control_Tower, Trips_Board, Bays_Board, Allocations_Log, DecisionLog_Page, Exceptions_Log | hot / waiting / at-bay / withdrawn trucks and the latest reasons; bay state; who was sent where and **why**, in plain words |
| Inputs | Settings_*, ItemStandard_*, Bay_Edit, BayFallback_*, GateEntry_Overview | everything the plan reads |
| Debug | HistDrop_View, HistTrip_View, HistStop_View, VsGroup_View, BayShare_View | what the last plan run actually worked with, including entries it left out and why |

Explanatory logs: `PlanRunLog` (plan side) and `DecisionLog` (live side, e.g. "Stop 1: planned
bay AR-L3 is held until 10:35; AR-L2 is occupied; AR-L4 is free: sent there instead").

First step: load a small `Bay`, `ItemStandard`, `BayFallback` and `GateEntry` sample, press
"Generate plan now" on the hub, read PlanRun_Log, then simulate a truck by calling `OnGateEntry`.

## Re-running
The scripts create the module, so a second run fails with "already exists". To start
over, restore your backup, or run `00_reset.mdl` (drops module TripPlanning) and apply
`tripplanning.mdl` then `tripplanning_live.mdl` again. Always quote paths with spaces.

## Live allocation (second script)
`tripplanning_live.mdl` adds the gate-time decision: `OnGateEntry` (call from the
truck-entry alert), `AllocateNextStop`, `OnBayFree` (call from the bay occupancy feed),
`SE_Sweep` (every 5 min, disabled until enabled), `BayFallback` rows, Trip, Allocation
and ExceptionLog. Run it after `tripplanning.mdl`:
```
mxcli check mendix/tripplanning_live.mdl
mxcli -p path/to/App.mpr -c "execute script 'mendix/tripplanning_live.mdl'"
```
`BayFallback` has one row per bay and fallback (`BayCode`, `FallbackCode`, `Rank` = position in
the list): split `fallback_map.csv` (Download fallback map in the Bay schedule tab) into rows.
Still to wire in Studio Pro: the two integrations (truck-entry alert -> `OnGateEntry`,
occupancy feed -> `Bay.Occupied` and `OnBayFree`) and a yard page on Trip/Allocation.

## What runs (microflows only; one tiny Java helper)
`SE_GeneratePlan` (weekly scheduled event, disabled until enabled; button via `ACT_GeneratePlanNow`):
1. `SUB_BuildHistory`: reads `GateEntry` 5000 rows at a time, sorted by truck, entry time and bay;
   a trip = truck + entry time; drops entries more than EarlyCapH/LateCapH hours from the slot;
   writes `HistTrip` (minutes vs slot) and `HistStop` (standard unloading minutes per bay).
2. `SUB_BuildGroups`: trips and run-days per vendor + slot, trips per slot, bay usage (`BayShare`).
3. `SUB_WritePlans`: per vendor + slot: safe start = P(QStart) rounded up, hard stop = P(QStop)
   + grace rounded up (slot-wide percentile when under MinTrucks); bays used on >= 10% of
   trips, top 5, critical first; stop minutes = P(QStopMinutes) of standard minutes x
   (1 + allowance) + fixed, rounded up to 5, else ManualMin. Every decision is a `PlanRunLog` line.
Percentiles come from `SUB_PercentileTrip` / `SUB_PercentileStop`: the database returns the two
neighbouring rows in sorted order, so no big list is held in memory. The only Java is
`MinutesFromSlot` in the live script (a few lines of date arithmetic).
Not yet in the Mendix version: per-weekday plans (the plan is for all days).

## Data loads
- `GateEntry`: the parquet rows (Excel/CSV import or REST).
- `ItemStandard`: pack size, standard minutes, basis, IsCritical.
- `Bay`: codes; `IsHotLane` for reserved bays.
- `PlanSettings`: created with defaults on first run; edit on a page.

## Cross-check before go-live
Run the Python app on the same history window and diff `VendorSlotPlan` against the
"Safe start & hard stop" sheet. Differences point to data-load issues, not math.
