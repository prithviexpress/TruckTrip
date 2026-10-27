# Truck Trend

Streamlit tools for Nagare truck data (`nagare_data.parquet`, kept out of git).

| App | What it does |
|---|---|
| `nagare_trip_report.py` | Supply time → Trip → Item × Day table for one month; Excel export with one tab per month |
| `vendor_trip_planner.py` | Min / typical / max trips per vendor, by weekday and supply slot, plus trips from planned quantity; Excel export |
| `trip_demand_analysis.py` | Trips to plan per supply time, arrival window vs supply time, materials that arrive in each slot, and what drives trips up (weekday, month position, planned qty); Excel export |

## Run
```
pip install -r requirements.txt
streamlit run nagare_trip_report.py
streamlit run vendor_trip_planner.py
streamlit run trip_demand_analysis.py
```
Put `nagare_data.parquet` next to the scripts or enter its path in the sidebar.

## Assumptions
- A trip is one truck (`Truck_number`, falling back to `Consignement_no`) per `Nag_date` + `Supply_Time` (+ vendor in the planner).
- Days come from `Nag_date`, not gate entry date.
- Load per trip is in pieces, not volume.
- `trip_demand_analysis.py` takes `Nag_qty` once per Schedule_no + Item line (it repeats on every truck row) and measures arrival as first gate entry minus Nag_date + Supply_Time.
