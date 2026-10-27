"""
Vendor Trip Planner
-------------------
Recommends how many trips to plan per vendor from past nagare data:
  * Min / Typical / Max case per vendor (percentiles of daily trips)
  * Same, split by weekday and by supply slot
  * What-if: enter a planned quantity -> min / max trips from truck load history

Install:  pip install streamlit pandas pyarrow openpyxl numpy
Run:      streamlit run vendor_trip_planner.py
"""

import io
import math

import numpy as np
import pandas as pd
import streamlit as st

DEFAULT_PATH = "nagare_data.parquet"
COLS = ["Vendor_code", "unloading_loc", "Nag_date", "Supply_Time", "Nag_qty",
        "Received_qty", "Gate_entry_qty", "Truck_number", "Consignement_no",
        "Gate_entry_time"]
QTY_OPTIONS = ["Nag_qty", "Received_qty", "Gate_entry_qty"]
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

st.set_page_config(page_title="Vendor Trip Planner", layout="wide")


# ----------------------------------------------------------------------------
# Load
# ----------------------------------------------------------------------------
@st.cache_resource(show_spinner="Loading parquet…")
def load(path: str) -> pd.DataFrame:
    df = pd.read_parquet(path, columns=COLS)
    df["Nag_date"] = pd.to_datetime(df["Nag_date"], format="%d-%b-%Y",
                                    errors="coerce").dt.normalize()
    df["Supply_Time"] = df["Supply_Time"].astype(str).str.strip()
    df["Vendor_code"] = df["Vendor_code"].astype(str).str.strip()
    for c in QTY_OPTIONS:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    # One trip = one truck (fallback: consignment) per vendor/date/slot
    df["truck_key"] = df["Truck_number"].astype("string").fillna(
        "CN-" + df["Consignement_no"].astype("string").fillna("NA"))
    return df.dropna(subset=["Nag_date"])


# ----------------------------------------------------------------------------
# Trip-level and day-level tables
# ----------------------------------------------------------------------------
@st.cache_data(show_spinner="Counting trips…")
def build_tables(path, lookback_days, locs, qty_col):
    df = load(path)
    end = df["Nag_date"].max()
    start = end - pd.Timedelta(days=lookback_days - 1)
    df = df[df["Nag_date"] >= start]
    if locs:
        df = df[df["unloading_loc"].isin(locs)]

    trips = (df.groupby(["Vendor_code", "Nag_date", "Supply_Time", "truck_key"])
               .agg(qty=(qty_col, "sum")).reset_index())

    daily = (trips.groupby(["Vendor_code", "Nag_date"])
                  .agg(trips=("truck_key", "size"), qty=("qty", "sum"),
                       slots=("Supply_Time", "nunique"))
                  .reset_index())
    daily["weekday"] = daily["Nag_date"].dt.dayofweek.map(dict(enumerate(WEEKDAYS)))

    slot = (trips.groupby(["Vendor_code", "Supply_Time", "Nag_date"])
                 .agg(trips=("truck_key", "size")).reset_index())
    return trips, daily, slot, start, end


def case_stats(s: pd.Series, lo, hi):
    """Min / typical / max case from a series of trip counts."""
    if s.empty:
        return dict(min_case=np.nan, typical=np.nan, max_case=np.nan,
                    record_low=np.nan, record_high=np.nan)
    return dict(
        min_case=max(1, math.floor(s.quantile(lo))),
        typical=int(round(s.median())),
        max_case=math.ceil(s.quantile(hi)),
        record_low=int(s.min()),
        record_high=int(s.max()),
    )


def vendor_summary(trips, daily, start, end, lo, hi):
    cal_days = (end - start).days + 1
    recent_cut = end - pd.Timedelta(days=29)
    out = []
    for v, d in daily.groupby("Vendor_code"):
        t = trips[trips["Vendor_code"] == v]
        stats = case_stats(d["trips"], lo, hi)
        recent = d[d["Nag_date"] >= recent_cut]["trips"]
        older = d[d["Nag_date"] < recent_cut]["trips"]
        trend = ""
        if len(recent) >= 5 and len(older) >= 5:
            r, o = recent.median(), older.median()
            trend = "Rising" if r > o else "Falling" if r < o else "Stable"
        out.append({
            "Vendor": v,
            "Supply days": len(d),
            "Supply days %": round(100 * len(d) / cal_days),
            "Min case": stats["min_case"],
            "Typical": stats["typical"],
            "Max case": stats["max_case"],
            "Record low": stats["record_low"],
            "Record high": stats["record_high"],
            "Last 30d trend": trend,
            "Avg qty/day": round(d["qty"].mean()),
            "Load/trip P50": round(t["qty"].median()),
            "Load/trip P90": round(t["qty"].quantile(0.9)),
        })
    return (pd.DataFrame(out)
              .sort_values("Typical", ascending=False)
              .reset_index(drop=True))


def weekday_matrix(daily, lo, hi):
    def rng(s):
        c = case_stats(s, lo, hi)
        return (f'{c["min_case"]}' if c["min_case"] == c["max_case"]
                else f'{c["min_case"]}–{c["max_case"]}')
    m = (daily.groupby(["Vendor_code", "weekday"])["trips"].apply(rng)
              .unstack("weekday"))
    return m.reindex(columns=[w for w in WEEKDAYS if w in m.columns]).fillna("-")


def slot_table(slot, lo, hi):
    rows = []
    for (v, s), g in slot.groupby(["Vendor_code", "Supply_Time"]):
        c = case_stats(g["trips"], lo, hi)
        rows.append({"Vendor": v, "Supply time": s, "Days used": len(g),
                     "Min case": c["min_case"], "Typical": c["typical"],
                     "Max case": c["max_case"]})
    return pd.DataFrame(rows).sort_values(["Vendor", "Supply time"])


def to_excel(sheets: dict) -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        for name, frame in sheets.items():
            frame.to_excel(xw, sheet_name=name, index=name == "By weekday")
            ws = xw.sheets[name]
            ws.freeze_panes = "B2"
            for col in ws.columns:
                width = max(len(str(c.value or "")) for c in col) + 2
                ws.column_dimensions[col[0].column_letter].width = min(width, 40)
    return buf.getvalue()


# ----------------------------------------------------------------------------
# Page
# ----------------------------------------------------------------------------
st.title("Vendor Trip Planner")

with st.sidebar:
    path = st.text_input("Parquet file", DEFAULT_PATH)
    try:
        base = load(path)
    except Exception as e:
        st.error(f"Could not read {path}: {e}")
        st.stop()
    lookback = st.slider("History window (days)", 30, 365, 90, step=15)
    lo, hi = st.select_slider("Min / Max case percentiles",
                              options=[0.05, 0.10, 0.20, 0.25, 0.75, 0.80, 0.90, 0.95],
                              value=(0.10, 0.90))
    qty_col = st.radio("Quantity basis", QTY_OPTIONS, index=0)
    locs = tuple(st.multiselect(
        "Unloading location",
        sorted(base["unloading_loc"].dropna().astype(str).unique())))

trips, daily, slot, start, end = build_tables(path, lookback, locs, qty_col)
st.caption(f"History used: {start:%d-%b-%Y} to {end:%d-%b-%Y}. "
           f"Min case = P{int(lo*100)}, Max case = P{int(hi*100)} of trips per supply day.")

if daily.empty:
    st.info("No data in this window.")
    st.stop()

summary = vendor_summary(trips, daily, start, end, lo, hi)
wk = weekday_matrix(daily, lo, hi)
slots = slot_table(slot, lo, hi)

c1, c2, c3 = st.columns(3)
c1.metric("Vendors", len(summary))
c2.metric("Total trips/day, min case", int(summary["Min case"].sum()))
c3.metric("Total trips/day, max case", int(summary["Max case"].sum()))
st.caption("Totals add each vendor's case, so they overstate a real day: "
           "vendors rarely all peak together.")

tab1, tab2, tab3, tab4 = st.tabs(["Per vendor", "By weekday", "By supply slot",
                                  "Plan from quantity"])

with tab1:
    st.dataframe(summary, hide_index=True, width="stretch")

with tab2:
    st.write("Trips per day, min–max case, for each weekday.")
    st.dataframe(wk, width="stretch")

with tab3:
    st.dataframe(slots, hide_index=True, width="stretch")

with tab4:
    v = st.selectbox("Vendor", summary["Vendor"])
    t = trips[trips["Vendor_code"] == v]
    d = daily[daily["Vendor_code"] == v]
    default_qty = int(d["qty"].median()) if len(d) else 0
    qty = st.number_input(f"Planned {qty_col} for the day", min_value=0,
                          value=default_qty, step=10)

    full_load = t["qty"].quantile(0.90)      # truck packed well
    light_load = t["qty"].quantile(0.25)     # truck sent light
    min_trips = math.ceil(qty / full_load) if full_load > 0 else 0
    max_trips = math.ceil(qty / light_load) if light_load > 0 else 0

    near = d[(d["qty"] >= qty * 0.85) & (d["qty"] <= qty * 1.15)]["trips"]

    a, b, c = st.columns(3)
    a.metric("Min trips (full trucks)", min_trips,
             help=f"Qty ÷ P90 load per trip ({full_load:.0f})")
    b.metric("Max trips (light trucks)", max_trips,
             help=f"Qty ÷ P25 load per trip ({light_load:.0f})")
    if len(near):
        c.metric("Past days with similar qty", f"{near.min()}–{near.max()} trips",
                 help=f"{len(near)} days within ±15% of this qty")
    else:
        c.metric("Past days with similar qty", "none")

st.divider()
st.download_button(
    "Download Excel",
    to_excel({"Per vendor": summary, "By weekday": wk,
              "By supply slot": slots,
              "Daily history": daily.assign(Nag_date=daily["Nag_date"].dt.date)}),
    file_name=f"vendor_trip_plan_{end:%Y%m%d}.xlsx",
    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
