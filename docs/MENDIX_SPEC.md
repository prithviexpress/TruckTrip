# TruckTrip in Mendix: Build Spec

A brief for Claude Code to rebuild the two Python/Streamlit tools in this repo as a Mendix app.
The Python files (`nagare_trip_report.py`, `vendor_trip_planner.py`) are the working reference: when this spec and the code disagree, the code's logic wins.

## 1. What the app is for

A plant receives parts from vendors in trucks against a Nagare (scheduled supply) plan. Each schedule line says which item, from which vendor, for which date and supply time slot, and records the truck that delivered it with its gate entry and gate out time.

The app answers two questions:

1. **Trip Report:** for a chosen month, what did each supply slot look like day by day: which trips came, when they entered the gate, and how much of each item each trip delivered?
2. **Trip Planner:** for each vendor, how many trips should we plan per day: the minimum case, the typical case and the maximum case, based on past data?

Both export to Excel.

## 2. Source data

One file, `nagare_data.parquet`, about **2.15 million rows, 20 columns**. It holds vendor codes, truck numbers and driver names, so it must never be committed to git.

| Column | Example | Notes |
|---|---|---|
| Schedule_no | 01Q650170872AR2 | |
| Type | NAGARE | |
| Item | 83661M71U00 | part number |
| Item_name | RUN,FRONT DOOR GLASS,R | |
| Vendor_code | P209 | |
| unloading_loc | AR-R8 | dock |
| Nag_date | 01-MAR-2026 | text, format `DD-MON-YYYY` |
| Supply_Time | 06:29 | text, `HH:MM`, the scheduled slot |
| Nag_qty | 100 | planned qty |
| location | AR-R8 | |
| UTL_Qty | 100 | |
| Gate_entry_qty | 100.0 | |
| Received_qty | 100.0 | |
| Rejection_qty | NaN | often empty |
| Truck_number | UP16ET7125 | |
| Gate_entry_time | 01-MAR-2026 06:45 | text, `DD-MON-YYYY HH:MM` |
| Truck_driver_name | PARBHU+RAVIKUMAR | personal data, do not import |
| Truck_driver_no | NaN | personal data, do not import |
| Consignement_no | 72817394 | spelling as in source |
| Gate_Out_time | 01-MAR-2026 07:27 | text, `DD-MON-YYYY HH:MM` |

**Mendix cannot read parquet.** Convert it outside Mendix first:

```python
import pandas as pd
df = pd.read_parquet("nagare_data.parquet").drop(columns=["Truck_driver_name", "Truck_driver_no"])
df["month"] = pd.to_datetime(df["Nag_date"], format="%d-%b-%Y").dt.to_period("M").astype(str)
for m, g in df.groupby("month"):
    g.drop(columns="month").to_csv(f"nagare_{m}.csv", index=False)
```

Import one month per file (≈180k rows each), not one 2.15M-row file.

## 3. Core rules (the part that must be right)

These come from the Python code. Every number in the app depends on them.

- **Trip.** One trip = one truck per `Nag_date` + `Supply_Time`. The truck is identified by `Truck_number`; if that is empty, use `"CN-" + Consignement_no`; if both are empty, `"CN-NA"`.
  - In the Planner, the trip key also includes `Vendor_code` (same truck, two vendors = counted once for each vendor).
- **Trip number.** Within each `Nag_date` + `Supply_Time`, sort trips by their earliest `Gate_entry_time` (empty times last) and number them Trip1, Trip2, …
- **Day.** A row belongs to the day of its `Nag_date`, not its gate entry date. If the gate entry falls on another day, show the time with an offset, e.g. `00:40 (+1d)`.
- **Quantity.** The user picks one of `Received_qty`, `Nag_qty` or `Gate_entry_qty`. The report defaults to `Received_qty`, the planner to `Nag_qty`. Quantities are summed.
- **Load per trip** is in pieces, not volume. This is a known weakness; keep it visible in the UI.

## 4. Domain model

Module `TruckTrip`.

**NagareLine** (persistent; one per source row)
`ScheduleNo`, `Type`, `Item`, `ItemName`, `VendorCode`, `UnloadingLoc`, `Location`, `NagDate` (Date, localize = No), `SupplyTime` (String 5), `NagQty`, `UtlQty`, `GateEntryQty`, `ReceivedQty`, `RejectionQty` (Decimal), `TruckNumber`, `ConsignmentNo`, `GateEntryTime`, `GateOutTime` (DateTime, localize = No), `TruckKey` (String, computed on import), `MonthKey` (String `YYYY-MM`, computed on import).
Add indexes on (`MonthKey`, `VendorCode`), (`NagDate`, `SupplyTime`, `TruckKey`).

**Trip** (persistent; rebuilt after each import)
`VendorCode`, `NagDate`, `SupplyTime`, `TruckKey`, `TripNo` (Integer), `FirstGateEntry` (DateTime), `QtyNag`, `QtyReceived`, `QtyGateEntry` (Decimal), `MonthKey`.

**VendorDay** (persistent; rebuilt after each import)
`VendorCode`, `NagDate`, `Weekday` (enum Mon–Sun), `Trips` (Integer), `Slots` (Integer), `QtyNag`, `QtyReceived`, `QtyGateEntry`.

**ImportBatch**: `FileName`, `MonthKey`, `RowCount`, `ImportedOn`, `Status`.

Non-persistent helper entities for pages:

- **ReportFilter**: `MonthKey`, `QtyBasis` (enum), and selected vendors and unloading locations.
- **ReportRow**: `SupplyTime`, `TripLabel`, `ItemName`, `Location`, `RowKind` (enum Gate / Item), `D01` … `D31` (String), `ShowSupply`, `ShowTrip` (Boolean).
  - Fixed 31 day columns are deliberate: Data Grid 2 cannot add columns dynamically. Hide D29–D31 when the month is shorter.
- **PlannerFilter**: `LookbackDays` (default 90), `LowPct` (default 10), `HighPct` (default 90), `QtyBasis`, `UnloadingLocs`.
- **VendorPlan**: `Vendor`, `SupplyDays`, `SupplyDaysPct`, `MinCase`, `Typical`, `MaxCase`, `RecordLow`, `RecordHigh`, `Trend`, `AvgQtyPerDay`, `LoadP50`, `LoadP90`.
- **WeekdayPlan**: `Vendor`, `Mon` … `Sun` (String, e.g. `"3–5"`).
- **SlotPlan**: `Vendor`, `SupplyTime`, `DaysUsed`, `MinCase`, `Typical`, `MaxCase`.
- **QtyPlan**: `Vendor`, `PlannedQty`, `MinTrips`, `MaxTrips`, `SimilarDays`, `SimilarRange`.

## 5. Logic

**Import (per month CSV).** Use the CSV module or Excel Importer to create `NagareLine` rows.
- Parse the date texts with the exact formats in section 2. Month names are upper case (`MAR`), so normalize the case before parsing.
- Compute `TruckKey` and `MonthKey`.
- Delete any existing rows for that `MonthKey` first, so re-importing a month replaces it.
- Then call `ACT_RebuildMonth(MonthKey)`.

**ACT_RebuildMonth.** Build the summary tables with OQL or a Java action, not microflow loops: 180k rows per month is too many for list loops.
1. Delete `Trip` and `VendorDay` rows for the month.
2. Insert `Trip` = group `NagareLine` by `VendorCode`, `NagDate`, `SupplyTime`, `TruckKey`, with min gate entry and the sums of each qty.
3. Set `TripNo`: within `NagDate` + `SupplyTime` (across vendors, as the report does), order by `FirstGateEntry`, nulls last.
   - The planner does not use `TripNo`.
   - The report numbers trips per slot over all vendors that pass the current filter. Because of that, compute the report's trip numbers at report time from the filtered rows. Store the unfiltered number only as a default.
4. Insert `VendorDay` = group `Trip` by `VendorCode`, `NagDate`.

**Percentiles.** OQL has no percentile function. Do this in a Java action: load the `VendorDay.Trips` values per vendor (small: ≤365 per vendor) and compute pandas-style linear-interpolation quantiles.
- Min case = `max(1, floor(Pq_low))`
- Typical = `round(median)`
- Max case = `ceil(Pq_high)`
- Record low and record high = min and max.

**Trend.** Compare the median trips in the last 30 days of the window with the median of the days before that. Output Rising, Falling or Stable. Only compute it when each side has at least 5 days; otherwise leave it blank.

**Weekday matrix.** Same case stats per vendor per weekday. Show `"min–max"`, or a single number when they are equal; `-` for no data.

**Slot plan.** Same case stats per vendor per `SupplyTime`, using trips per day in that slot.

**Plan from quantity.** For one vendor, over the vendor's `Trip` rows in the window:
- MinTrips = ceil(qty / P90 of qty per trip)
- MaxTrips = ceil(qty / P25 of qty per trip)
- Similar days = `VendorDay` rows whose qty is within ±15% of the planned qty. Show their count and min–max trips.

## 6. Pages

**Trip Report.** Filters: month, qty basis, vendors, unloading locations.
- Summary tiles: supply slots, trips, item rows.
- Grid columns: Supply time | Trip | Items | Location | 01 … 31.
- Group rows by Supply time, then Trip. Show the Supply time and Trip text only on the first row of each group (`ShowSupply`/`ShowTrip`); Data Grid 2 has no row-span.
- Each trip starts with a highlighted **Gate entry** row (gate time per day), followed by one row per Item + Location with the summed qty per day.
- Build the rows with a Java action from the filtered data. Return a list of `ReportRow`.

**Trip Planner.** Filters as in `PlannerFilter`.
- Tiles: vendors, total min-case trips per day, total max-case trips per day.
  - Note under them: these sums assume every vendor peaks on the same day, so treat them as a ceiling.
- Tabs: Per vendor, By weekday, By supply slot, Plan from quantity.

**Import.** Upload a month CSV, see `ImportBatch` history, and re-run the rebuild.

## 7. Excel export

Use a Java action with Apache POI, which Mendix already bundles. The Excel Exporter module cannot create a dynamic number of month sheets with merged cells.

- **Trip Report export:**
  - One sheet per month in the data, named `Mar-2026` etc., respecting the current vendor and location filters.
  - Same columns as the grid. Merge Supply time and Trip cells vertically, centred.
  - Header fill `#1F3A5F` with white bold text. Gate entry rows filled `#FFF4D6`.
  - Freeze panes at E2. Column widths: Supply 12, Trip 8, Items 38, Location 11, days 8.
- **Planner export:** sheets Per vendor, By weekday, By supply slot, Daily history (the `VendorDay` rows in the window).
  - File name `vendor_trip_plan_YYYYMMDD.xlsx`, using the last date in the data.

## 8. Acceptance checks

Use the rows from the original screenshot (all on `01-MAR-2026`):

- Slot 06:29: truck UP16ET7125 delivers 4 glass items, gate entry 06:45.
  - The report shows one Trip1 with a Gate entry row of `06:45` in the column for day 01, then 4 item rows with Received qty 100, 100, 100, 200.
- Slot 07:01: truck HR47F3784 delivers vendor P539, gate entry 07:49 → Trip1 for that slot.
- Planner: for vendor P209 on 01-Mar, `VendorDay.Trips` = 1 for that slot's truck.
- Python parity: running `vendor_trip_planner.py` and the Mendix planner on the same month gives the same Min case, Typical and Max case per vendor.

## 9. Open questions for the plant team

1. Is "one truck per date + slot" really one trip? Or can a truck do two rounds in one slot, or does the plant use fixed trip numbers?
2. Which quantity should planning use: ordered (`Nag_qty`) or received?
3. Is pack size or bin count available, so load per trip can be measured in volume instead of pieces?
