# Truck Trend

Streamlit tools for Nagare truck data (`nagare_data.parquet`, kept out of git).

| App | What it does |
|---|---|
| `nagare_trip_report.py` | Supply time → Trip → Item × Day table for one month; Excel export with one tab per month |
| `vendor_trip_planner.py` | Min / typical / max trips per vendor, by weekday and supply slot, plus trips from planned quantity; Excel export |

## Run
```
pip install -r requirements.txt
streamlit run nagare_trip_report.py
streamlit run vendor_trip_planner.py
```
Put `nagare_data.parquet` next to the scripts or enter its path in the sidebar.

## Assumptions
- A trip is one truck (`Truck_number`, falling back to `Consignement_no`) per `Nag_date` + `Supply_Time` (+ vendor in the planner).
- Days come from `Nag_date`, not gate entry date.
- Load per trip is in pieces, not volume.
