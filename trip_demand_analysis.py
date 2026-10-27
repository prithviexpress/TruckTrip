"""
Trip Demand Analysis
--------------------
Answers three planning questions from nagare_data.parquet:
  1. How many trips to plan per supply time (min / typical / plan-for)
  2. When trucks actually arrive relative to the supply time
  3. What conditions push trip counts up (weekday, month position, planned qty)

Install:  pip install streamlit pandas pyarrow openpyxl numpy
Run:      streamlit run trip_demand_analysis.py
"""

import io
import math

import numpy as np
import pandas as pd
import streamlit as st

DEFAULT_PATH = "nagare_data.parquet"
COLS = ["Schedule_no", "Item", "Item_name", "Vendor_code", "unloading_loc", "Nag_date",
        "Supply_Time", "Nag_qty", "Received_qty", "Truck_number",
        "Consignement_no", "Gate_entry_time"]
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
MAX_EARLY_H = 12     # arrivals earlier than this before the slot are treated as
MAX_LATE_H = 12      # a different schedule's truck (or a data error), not this one


# ----------------------------------------------------------------------------
# Load + clean
# ----------------------------------------------------------------------------
def clean(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["Nag_date"] = pd.to_datetime(df["Nag_date"], format="%d-%b-%Y",
                                    errors="coerce").dt.normalize()
    df["Gate_entry_time"] = pd.to_datetime(df["Gate_entry_time"],
                                           format="%d-%b-%Y %H:%M",
                                           errors="coerce")
    df["Supply_Time"] = df["Supply_Time"].astype(str).str.strip().str.slice(0, 5)
    df["unloading_loc"] = df["unloading_loc"].fillna("").astype(str).str.strip()
    df["Item"] = df["Item"].fillna("").astype(str).str.strip()
    df["Item_name"] = df["Item_name"].fillna("").astype(str).str.strip()
    df["Vendor_code"] = df["Vendor_code"].fillna("").astype(str).str.strip()
    for c in ("Nag_qty", "Received_qty"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["truck_key"] = df["Truck_number"].astype("string").fillna(
        "CN-" + df["Consignement_no"].astype("string").fillna("NA"))
    df = df.dropna(subset=["Nag_date"])
    slot_dt = pd.to_datetime(df["Nag_date"].dt.strftime("%Y-%m-%d") + " "
                             + df["Supply_Time"], errors="coerce")
    df["slot_dt"] = slot_dt
    return df.dropna(subset=["slot_dt"])


@st.cache_resource(show_spinner="Loading parquet…")
def load(path: str) -> pd.DataFrame:
    return clean(pd.read_parquet(path, columns=COLS))


# ----------------------------------------------------------------------------
# Tables
# ----------------------------------------------------------------------------
def build_trips(df: pd.DataFrame, split_loc: bool) -> pd.DataFrame:
    """One row per truck per day+slot (+location)."""
    keys = ["Nag_date", "Supply_Time"] + (["unloading_loc"] if split_loc else [])
    trips = (df.groupby(keys + ["truck_key"])
               .agg(arrival=("Gate_entry_time", "min"),
                    slot_dt=("slot_dt", "first"),
                    received=("Received_qty", "sum"))
               .reset_index())
    trips["offset_min"] = (trips["arrival"] - trips["slot_dt"]).dt.total_seconds() / 60
    return trips


def planned_qty(df: pd.DataFrame, split_loc: bool) -> pd.DataFrame:
    """Planned qty per day+slot. Nag_qty repeats on every truck row of a
    schedule line, so take it once per Schedule_no + Item before summing."""
    line_keys = ["Schedule_no", "Item", "Nag_date", "Supply_Time", "unloading_loc"]
    lines = df.drop_duplicates(line_keys)
    keys = ["Nag_date", "Supply_Time"] + (["unloading_loc"] if split_loc else [])
    return lines.groupby(keys)["Nag_qty"].sum().rename("planned_qty").reset_index()


def slot_days(trips: pd.DataFrame, keys) -> pd.DataFrame:
    return trips.groupby(keys).agg(trips=("truck_key", "nunique")).reset_index()


def trip_plan(sd: pd.DataFrame, group_cols, lo, plan_q) -> pd.DataFrame:
    """Min / typical / plan-for trips per slot, over days the slot ran."""
    rows = []
    for g, s in sd.groupby(group_cols):
        g = g if isinstance(g, tuple) else (g,)
        t = s["trips"]
        rows.append({**dict(zip(group_cols, g)),
                     "Days run": len(t),
                     "Min": max(1, math.floor(t.quantile(lo))),
                     "Typical": int(round(t.median())),
                     "Plan for": math.ceil(t.quantile(plan_q)),
                     "Max seen": int(t.max())})
    return pd.DataFrame(rows)


def arrival_window(trips: pd.DataFrame, group_cols, lo, hi) -> pd.DataFrame:
    t = trips[trips["offset_min"].between(-MAX_EARLY_H * 60, MAX_LATE_H * 60)]
    rows = []
    for g, s in t.groupby(group_cols):
        g = g if isinstance(g, tuple) else (g,)
        o = s["offset_min"]
        rows.append({**dict(zip(group_cols, g)),
                     "Trucks": len(o),
                     "Early edge (min)": round(o.quantile(lo)),
                     "Median (min)": round(o.median()),
                     "Late edge (min)": round(o.quantile(hi)),
                     "% before slot": round(100 * (o <= 0).mean()),
                     "% >60 min late": round(100 * (o > 60).mean())})
    return pd.DataFrame(rows)


def slot_materials(df: pd.DataFrame, slot_keys, core_pct) -> pd.DataFrame:
    """What arrives in each slot: per slot x item, how often it shows up and
    how much / how many trucks it brings on the days it does."""
    day = (df.groupby(slot_keys + ["Item", "Item_name", "Nag_date"])
             .agg(qty=("Received_qty", "sum"), trucks=("truck_key", "nunique"),
                  vendors=("Vendor_code", lambda v: ", ".join(sorted(set(v)))[:40]))
             .reset_index())
    run_days = df.groupby(slot_keys)["Nag_date"].nunique().rename("slot_days")
    rows = []
    for g, s in day.groupby(slot_keys + ["Item", "Item_name"]):
        n = s["Nag_date"].nunique()
        rows.append({**dict(zip(slot_keys + ["Item", "Item_name"], g)),
                     "Vendors": ", ".join(sorted({x for v in s["vendors"]
                                                  for x in v.split(", ") if x}))[:40],
                     "Days seen": n,
                     "Qty/day median": round(s["qty"].median()),
                     "Qty/day P90": round(s["qty"].quantile(0.9)),
                     "Trucks/day median": int(round(s["trucks"].median())),
                     "Total qty": round(s["qty"].sum())})
    out = pd.DataFrame(rows).merge(run_days.reset_index(), on=slot_keys)
    out["% of slot days"] = (100 * out["Days seen"] / out["slot_days"]).round().astype(int)
    out["Share of slot qty %"] = (100 * out["Total qty"] / out.groupby(slot_keys)
                                  ["Total qty"].transform("sum")).round(1)
    out["Core item"] = np.where(out["% of slot days"] >= core_pct, "yes", "")
    return (out.drop(columns=["slot_days", "Total qty"])
               .sort_values(slot_keys + ["Days seen"], ascending=[True] * len(slot_keys)
                            + [False]).reset_index(drop=True))


def daily_features(trips: pd.DataFrame, plan: pd.DataFrame) -> pd.DataFrame:
    d = (trips.groupby("Nag_date")
              .agg(trips=("truck_key", "size")).reset_index())
    q = plan.groupby("Nag_date")["planned_qty"].sum().reset_index()
    d = d.merge(q, on="Nag_date", how="left")
    # calendar days with no trucks at all are real zero-trip days
    full = pd.date_range(d["Nag_date"].min(), d["Nag_date"].max(), freq="D")
    d = d.set_index("Nag_date").reindex(full).rename_axis("Nag_date").reset_index()
    d[["trips", "planned_qty"]] = d[["trips", "planned_qty"]].fillna(0)
    d["weekday"] = d["Nag_date"].dt.dayofweek
    dom = d["Nag_date"].dt.day
    dim = d["Nag_date"].dt.days_in_month
    d["month_pos"] = np.select([dom <= 5, dom > dim - 5], ["Start (1-5)", "End (last 5)"],
                               "Middle")
    d["month"] = d["Nag_date"].dt.to_period("M").astype(str)
    return d


def lift_table(d: pd.DataFrame, by: str, labels=None) -> pd.DataFrame:
    base = d["trips"].mean()
    g = d.groupby(by)["trips"].agg(["count", "mean", "median"]).reset_index()
    g["Lift vs avg day"] = (g["mean"] / base).round(2)
    g = g.rename(columns={by: by, "count": "Days", "mean": "Avg trips",
                          "median": "Median trips"})
    g["Avg trips"] = g["Avg trips"].round(1)
    if labels:
        g[by] = g[by].map(labels)
    return g


def fit_drivers(d: pd.DataFrame):
    """OLS: trips ~ planned qty + weekday + month position. Returns
    (coef table, R², n). Association only; 6 months is few weekday samples."""
    x = d.dropna(subset=["planned_qty"]).copy()
    if len(x) < 30:
        return None
    cols = {"Planned qty (per 100 pcs)": x["planned_qty"] / 100}
    for i, w in enumerate(WEEKDAYS[1:], start=1):
        cols[f"{w} vs Mon"] = (x["weekday"] == i).astype(float)
    cols["Month start vs middle"] = (x["month_pos"] == "Start (1-5)").astype(float)
    cols["Month end vs middle"] = (x["month_pos"] == "End (last 5)").astype(float)
    X = np.column_stack([np.ones(len(x))] + [c.to_numpy() for c in cols.values()])
    y = x["trips"].to_numpy()
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    resid = y - X @ beta
    dof = max(len(y) - X.shape[1], 1)
    cov = (resid @ resid / dof) * np.linalg.pinv(X.T @ X)
    se = np.sqrt(np.diag(cov))
    r2 = 1 - (resid @ resid) / ((y - y.mean()) @ (y - y.mean()))
    out = pd.DataFrame({"Driver": ["Baseline (Mon, mid-month, 0 qty)"] + list(cols),
                        "Trips effect": beta.round(2), "± (2 SE)": (2 * se).round(2)})
    out["Clear signal?"] = np.where(np.abs(beta) > 2 * se, "yes", "no")
    out.loc[0, "Clear signal?"] = ""
    return out, float(r2), len(y)


def to_excel(sheets: dict) -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        for name, frame in sheets.items():
            frame.to_excel(xw, sheet_name=name[:31], index=False)
            ws = xw.sheets[name[:31]]
            ws.freeze_panes = "A2"
            for col in ws.columns:
                width = max(len(str(c.value or "")) for c in col) + 2
                ws.column_dimensions[col[0].column_letter].width = min(width, 40)
    return buf.getvalue()


# ----------------------------------------------------------------------------
# Page
# ----------------------------------------------------------------------------
def main():
    st.set_page_config(page_title="Trip Demand Analysis", layout="wide")
    st.title("Trip Demand Analysis")

    with st.sidebar:
        path = st.text_input("Parquet file", DEFAULT_PATH)
        try:
            base = load(path)
        except Exception as e:
            st.error(f"Could not read {path}: {e}")
            st.stop()
        locs = st.multiselect("Unloading location",
                              sorted(base["unloading_loc"].unique()))
        vendors = st.multiselect("Vendor",
                                 sorted(base["Vendor_code"].dropna().astype(str).unique()))
        split_loc = st.checkbox("Treat each location as its own slot", value=False)
        lo, plan_q = st.select_slider("Min case / Plan-for percentile",
                                      options=[0.05, 0.10, 0.20, 0.50, 0.75, 0.80, 0.85,
                                               0.90, 0.95],
                                      value=(0.10, 0.85),
                                      help="Plan-for = trips you cover on this share of days")
        win = st.select_slider("Arrival window percentiles",
                               options=[0.05, 0.10, 0.20, 0.80, 0.90, 0.95],
                               value=(0.10, 0.90))

    df = base
    if locs:
        df = df[df["unloading_loc"].isin(locs)]
    if vendors:
        df = df[df["Vendor_code"].astype(str).isin(vendors)]
    if df.empty:
        st.info("No data for these filters.")
        st.stop()

    trips = build_trips(df, split_loc)
    plan = planned_qty(df, split_loc)
    slot_keys = ["Supply_Time"] + (["unloading_loc"] if split_loc else [])
    sd = slot_days(trips, ["Nag_date"] + slot_keys)
    sd["weekday"] = sd["Nag_date"].dt.dayofweek.map(dict(enumerate(WEEKDAYS)))
    n_days = (df["Nag_date"].max() - df["Nag_date"].min()).days + 1

    st.caption(f"{df['Nag_date'].min():%d-%b-%Y} to {df['Nag_date'].max():%d-%b-%Y} "
               f"({n_days} days). A trip is one truck per day + supply time"
               f"{' + location' if split_loc else ''}.")
    if n_days < 150:
        st.warning("Less than ~5 months of data: seasonality and month-end effects "
                   "are unreliable.")

    t1, t2, t4, t3 = st.tabs(["1. Trips per supply time", "2. Arrival window",
                              "3. Materials per slot", "4. What drives trips up"])

    with t1:
        table = trip_plan(sd, slot_keys, lo, plan_q)
        table["Runs on % of days"] = (100 * table["Days run"] / n_days).round().astype(int)
        st.dataframe(table.sort_values(slot_keys), hide_index=True, width="stretch")
        st.caption(f"Min = P{int(lo*100)}, Plan for = P{int(plan_q*100)} of trips on "
                   "days the slot actually ran. Check 'Runs on % of days' before "
                   "staffing a slot that only runs occasionally.")
        wk = (sd.groupby(slot_keys + ["weekday"])["trips"].median().round()
                .unstack("weekday"))
        wk = wk.reindex(columns=[w for w in WEEKDAYS if w in wk.columns]).reset_index()
        st.subheader("Median trips by weekday")
        st.dataframe(wk, hide_index=True, width="stretch")

    with t2:
        aw = arrival_window(trips, slot_keys, *win)
        st.dataframe(aw.sort_values(slot_keys), hide_index=True, width="stretch")
        st.caption("Minutes relative to supply time (negative = early), first gate "
                   f"entry of each truck. Arrivals beyond {MAX_EARLY_H}h early or "
                   f"{MAX_LATE_H}h late are dropped as likely mismatches.")
        h = trips["arrival"].dt.hour.value_counts().sort_index()
        st.subheader("Gate entries by hour of day")
        st.bar_chart(h.rename("Trucks"))

    with t4:
        core = st.slider("Core item = seen on at least this % of the slot's days",
                         10, 100, 50, step=5)
        mats = slot_materials(df, slot_keys, core)
        pick = st.selectbox("Supply time", ["All"] + sorted(mats["Supply_Time"].unique()))
        view = mats if pick == "All" else mats[mats["Supply_Time"] == pick]
        if pick != "All":
            st.metric("Core items expected", int((view["Core item"] == "yes").sum()))
        st.dataframe(view, hide_index=True, width="stretch")
        st.caption("Qty = Received_qty summed per day, on days the item came in that "
                   "slot. Trucks/day counts trucks carrying the item, and one truck "
                   "can carry several items, so don't add this column across items.")

    with t3:
        d = daily_features(trips, plan)
        c1, c2 = st.columns(2)
        with c1:
            st.subheader("By weekday")
            st.dataframe(lift_table(d, "weekday", dict(enumerate(WEEKDAYS))),
                         hide_index=True, width="stretch")
        with c2:
            st.subheader("By position in month")
            st.dataframe(lift_table(d, "month_pos"), hide_index=True, width="stretch")
        st.subheader("By month")
        st.dataframe(lift_table(d, "month"), hide_index=True, width="stretch")

        fit = fit_drivers(d)
        if fit:
            coefs, r2, n = fit
            st.subheader("Combined effect (trips per day)")
            st.dataframe(coefs, hide_index=True, width="stretch")
            st.caption(f"{n} days, R² = {r2:.2f}. These are associations, not causes. "
                       "'Clear signal' = effect larger than 2 standard errors; "
                       "treat 'no' rows as noise.")
        else:
            st.info("Not enough days with planned quantity for a combined fit.")

    st.divider()
    st.download_button(
        "Download Excel",
        to_excel({"Trips per slot": table, "Arrival window": aw, "Materials per slot": mats,
                  "Daily": d.assign(Nag_date=d["Nag_date"].dt.date)}),
        file_name=f"trip_demand_{df['Nag_date'].max():%Y%m%d}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


if __name__ == "__main__":
    main()
