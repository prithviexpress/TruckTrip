# TripPlanning in Mendix

What the Python app computes (safe start, hard stop, bay list, stop minutes), rebuilt
so Mendix owns the plan. Mendix has no text model format to import; the pieces are:

| File | What it is |
|---|---|
| `create_model.ts` | Mendix **Model SDK** script: creates module `TripPlanning` with the domain model in your Team Server app |
| `GeneratePlan.java` | Java action that computes the plan from `GateEntry` + `ItemStandard` and writes `VendorSlotPlan` + `PlanStop` |

## 1. Domain model via the Model SDK
```
cd mendix
npm install
export MENDIX_TOKEN=<Personal Access Token: Mendix Profile -> Developer Settings, scope mx:modelrepository:repo:write>
npx tsx create_model.ts <AppId>            # AppId: Sprintr app settings -> General -> App ID
```
Pull the branch in Studio Pro. Untested against a live Team Server; if a call fails,
compare with https://apidocs.rnd.mendix.com/modelsdk/latest/ for your SDK version.
Doing the same by hand in Studio Pro takes under an hour (10 entities, 9 associations).

## 2. Java action in Studio Pro
1. In module TripPlanning add a Java action `GeneratePlan`, parameters
   `Settings : PlanSettings` (object) and `Weekday : String`, return `Long`.
2. Studio Pro generates `javasource/tripplanning/actions/GeneratePlan.java`;
   replace its body with this file, keep the generated header/constructor if they differ.
3. Deploy for Eclipse / run locally once so proxies are generated.

## 3. Microflows (hand-built, small)
- `SE_GeneratePlan` (scheduled event, weekly): retrieve the single `PlanSettings`,
  set `IsActive=false` on current plans, call `GeneratePlan(Settings, '')`, optionally
  loop the 7 weekdays with `GeneratePlan(Settings, 'MONDAY')`.
- `ACT_ReplayPlan` (optional): same Java action over history minus the last 14 days,
  then count `Trip` rows in those 14 days before SafeStart / after HardStop. Do not
  activate a plan whose withdrawals per day exceed what the yard accepts.
- Live allocation (`OnGateEntry`, `AllocateNextStop`, `OnBayFree`, `SweepTimers`) as
  described in the design notes; they only read `VendorSlotPlan`/`PlanStop`.

## 4. Data loads
- `GateEntry`: the same rows as `nagare_data.parquet` (Excel/CSV import or REST).
- `ItemStandard`: your pack-size / unloading-time sheet, with `IsCritical`.
- `Bay` + `BayFallback`: from `fallback_map.csv` exported by the Python app.
- `PlanSettings`: one row. Defaults: HistoryWeeks 10, QStart 0.80, QStop 0.95,
  GraceMin 15, RoundMin 15, MinTrucks 10, MinRunPct 20, AllowancePct 15, FixedMin 5,
  QStopMinutes 0.75, ManualMin 25, GapMin 5, EarlyCapH 12, LateCapH 12, FallbackRange 2.

## 5. Cross-check before go-live
Run the Python app and `GeneratePlan` on the same history window and diff
`VendorSlotPlan` against the "Safe start & hard stop" sheet. Percentile math is the
same (linear interpolation), so differences point to data-load issues.
