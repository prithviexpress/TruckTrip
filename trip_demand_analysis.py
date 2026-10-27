"""
Trip Demand Analysis
--------------------
Answers the planning questions from nagare_data.parquet:
  * Plan: trips to plan per supply time, when those trucks really arrive
    (gate entry), and which materials to expect, with part-deliveries as 10/15 5/15
  * Trips per supply time (min / typical / plan-for)
  * Arrival: gate entry vs supply time, and trucks per clock-hour at the gate
  * Materials per slot, and day detail with part-deliveries
  * What conditions push trip counts up (weekday, month position, planned qty)

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
    df["Schedule_no"] = df["Schedule_no"].fillna("").astype(str).str.strip()
    df["Item"] = df["Item"].fillna("").astype(str).str.strip()
    df["Item_name"] = df["Item_name"].fillna("").astype(str).str.strip()
    df["Vendor_code"] = df["Vendor_code"].fillna("").astype(str).str.strip()
    for c in ("Nag_qty", "Received_qty"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    truck = df["Truck_number"].astype("string").fillna(
        "CN-" + df["Consignement_no"].astype("string").fillna("NA"))
    # A trip is one gate entry: the same truck coming back later with new
    # material has a different entry time and counts as another trip.
    df["trip_key"] = (truck.astype(str) + "@"
                      + df["Gate_entry_time"].dt.strftime("%Y%m%d%H%M").fillna("NA"))
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
    trips = (df.groupby(keys + ["trip_key"])
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
    return trips.groupby(keys).agg(trips=("trip_key", "nunique")).reset_index()


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


def _num(v):
    return f"{v:g}"


def build_lines(df: pd.DataFrame) -> pd.DataFrame:
    """One row per schedule line (Schedule_no + Item on a day + slot).
    Parts = what each truck brought, in gate-entry order, as received/planned
    (e.g. '10/15 5/15'); empty when the line came in one truck."""
    keys = ["Nag_date", "Supply_Time", "unloading_loc", "Schedule_no", "Item",
            "Item_name"]
    tl = (df.groupby(keys + ["trip_key"])
            .agg(received=("Received_qty", "sum"), entry=("Gate_entry_time", "min"),
                 planned=("Nag_qty", "first"), vendor=("Vendor_code", "first"))
            .reset_index().sort_values(keys + ["entry"]))
    tl["n"] = tl.groupby(keys)["trip_key"].transform("size")
    part = tl["received"].map(_num)
    den = tl["planned"].map(lambda v: "" if pd.isna(v) else "/" + _num(v))
    tl["part"] = part + den
    parts = (tl[tl["n"] > 1].groupby(keys)["part"].agg(" ".join).rename("parts"))
    lines = (tl.groupby(keys)
               .agg(planned=("planned", "first"), received=("received", "sum"),
                    trucks=("trip_key", "nunique"), first_entry=("entry", "min"),
                    last_entry=("entry", "max"), vendor=("vendor", "first"))
               .reset_index().merge(parts, on=keys, how="left"))
    lines["parts"] = lines["parts"].fillna("")
    return lines


def build_item_days(lines: pd.DataFrame) -> pd.DataFrame:
    """One row per item per day + slot (lines of the same item merged)."""
    keys = ["Nag_date", "Supply_Time", "unloading_loc", "Item", "Item_name"]
    return (lines.groupby(keys)
                 .agg(planned=("planned", "sum"), received=("received", "sum"),
                      trucks=("trucks", "sum"),
                      vendors=("vendor", lambda v: ", ".join(sorted(set(v)))[:40]),
                      parts=("parts", lambda p: "; ".join(x for x in p if x)),
                      first_entry=("first_entry", "min"))
                 .reset_index())


def slot_materials(iday: pd.DataFrame, slot_keys, core_pct) -> pd.DataFrame:
    """What arrives in each slot: per slot x item, how often it shows up, how
    much it brings, and how often it comes in parts."""
    run_days = iday.groupby(slot_keys)["Nag_date"].nunique().rename("slot_days")
    gk = slot_keys + ["Item", "Item_name"]
    rows = []
    for g, s in iday.groupby(gk):
        split = s[s["parts"] != ""].sort_values("Nag_date")
        last = (f"{split['parts'].iloc[-1]} ({split['Nag_date'].iloc[-1]:%d-%b})"
                if len(split) else "")
        rows.append({**dict(zip(gk, g)),
                     "Vendors": ", ".join(sorted({x for v in s["vendors"]
                                                  for x in v.split(", ") if x}))[:40],
                     "Days seen": len(s),
                     "Qty/day median": round(s["received"].median()),
                     "Qty/day P90": round(s["received"].quantile(0.9)),
                     "Typical parts": int(round(s["trucks"].median())),
                     "Came in parts %": round(100 * len(split) / len(s)),
                     "Last split seen": last,
                     "Total qty": round(s["received"].sum())})
    out = pd.DataFrame(rows).merge(run_days.reset_index(), on=slot_keys)
    out["% of slot days"] = (100 * out["Days seen"] / out["slot_days"]).round().astype(int)
    out["Share of slot qty %"] = (100 * out["Total qty"] / out.groupby(slot_keys)
                                  ["Total qty"].transform("sum")).round(1)
    out["Core item"] = np.where(out["% of slot days"] >= core_pct, "yes", "")
    return (out.drop(columns=["slot_days", "Total qty"])
               .sort_values(slot_keys + ["Days seen"], ascending=[True] * len(slot_keys)
                            + [False]).reset_index(drop=True))


def slot_clock(slot: str, minutes: float) -> str:
    """Clock time = supply time + offset, flagged when it rolls to another day."""
    h, m = map(int, slot.split(":"))
    t = h * 60 + m + int(round(minutes))
    day, t = divmod(t, 1440)
    return f"{t // 60:02d}:{t % 60:02d}" + (f" ({day:+d}d)" if day else "")


def arrival_by_bucket(df: pd.DataFrame, bucket_min: int, plan_q: float,
                      weekdays=None) -> pd.DataFrame:
    """Trucks entering the gate per clock bucket, per gate date, zero-filled."""
    a = (df.dropna(subset=["Gate_entry_time"])
           .drop_duplicates(["trip_key", "Gate_entry_time"]))
    if a.empty:
        return pd.DataFrame()
    a = a.assign(gdate=a["Gate_entry_time"].dt.normalize(),
                 bucket=a["Gate_entry_time"].dt.floor(f"{bucket_min}min")
                          .dt.strftime("%H:%M"))
    cnt = a.groupby(["gdate", "bucket"]).size().unstack("bucket", fill_value=0)
    cnt = cnt.reindex(pd.date_range(cnt.index.min(), cnt.index.max(), freq="D"),
                      fill_value=0)
    if weekdays:
        cnt = cnt[cnt.index.dayofweek.isin(weekdays)]
    rows = []
    for b in sorted(cnt.columns):
        c = cnt[b]
        rows.append({"Gate entry from": b, "Days": len(c),
                     "Typical trucks": int(round(c.median())),
                     "Plan for": math.ceil(c.quantile(plan_q)),
                     "Max seen": int(c.max()),
                     "Days with trucks %": round(100 * (c > 0).mean())})
    return pd.DataFrame(rows)


def daily_features(trips: pd.DataFrame, plan: pd.DataFrame) -> pd.DataFrame:
    d = (trips.groupby("Nag_date")
              .agg(trips=("trip_key", "size")).reset_index())
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
        dmin, dmax = base["Nag_date"].min().date(), base["Nag_date"].max().date()
        dr = st.date_input("Date range (Nag_date)", value=(dmin, dmax),
                           min_value=dmin, max_value=dmax)
        if len(dr) != 2:
            st.info("Pick an end date.")
            st.stop()
        locs = st.multiselect("Unloading location",
                              sorted(base["unloading_loc"].unique()))
        vendors = st.multiselect("Vendor",
                                 sorted(base["Vendor_code"].unique()))
        split_loc = st.checkbox("Also show bay (unloading location) breakdown", value=False,
                                help="One gate entry can unload at several bays. Trips are always counted once per gate entry; this only adds bay-visit and per-bay material tables.")
        lo, plan_q = st.select_slider("Min case / Plan-for percentile",
                                      options=[0.05, 0.10, 0.20, 0.50, 0.75, 0.80, 0.85,
                                               0.90, 0.95],
                                      value=(0.10, 0.85),
                                      help="Plan-for = trips you cover on this share of days")
        win = st.select_slider("Arrival window percentiles",
                               options=[0.05, 0.10, 0.20, 0.80, 0.90, 0.95],
                               value=(0.10, 0.90))
        bucket = st.select_slider("Gate-entry bucket (minutes)",
                                  options=[30, 60, 120], value=60)
        core = st.slider("Core item = seen on at least this % of the slot's days",
                         10, 100, 50, step=5)
        plan_days = st.multiselect("Plan for weekday(s)", WEEKDAYS,
                                   help="Plan tab uses only history from these "
                                        "weekdays. Empty = all days.")

    df = base[(base["Nag_date"] >= pd.Timestamp(dr[0]))
              & (base["Nag_date"] <= pd.Timestamp(dr[1]))]
    if locs:
        df = df[df["unloading_loc"].isin(locs)]
    if vendors:
        df = df[df["Vendor_code"].isin(vendors)]
    if df.empty:
        st.info("No data for these filters.")
        st.stop()

    lines, iday = frames(path, dr, tuple(locs), tuple(vendors))
    wd = [WEEKDAYS.index(w) for w in plan_days]
    slot_keys = ["Supply_Time"]
    mat_keys = slot_keys + (["unloading_loc"] if split_loc else [])

    # Trips are gate entries, counted once however many bays the truck visits
    trips = build_trips(df, False)
    plan = planned_qty(df, False)
    sd = slot_days(trips, ["Nag_date"] + slot_keys)
    sd["weekday"] = sd["Nag_date"].dt.dayofweek.map(dict(enumerate(WEEKDAYS)))
    n_days = (df["Nag_date"].max() - df["Nag_date"].min()).days + 1

    st.caption(f"{df['Nag_date'].min():%d-%b-%Y} to {df['Nag_date'].max():%d-%b-%Y} "
               f"({n_days} days). A trip is one gate entry per day + supply time (a truck "
               "coming back later counts again)"
               ". Supply time is the scheduled "
               "slot; gate entry is when the truck really came.")
    if n_days < 150:
        st.warning("Less than ~5 months of data in range: seasonality and "
                   "month-end effects are unreliable.")

    # ---- shared tables ------------------------------------------------------
    table = trip_plan(sd, slot_keys, lo, plan_q)
    table["Runs on % of days"] = (100 * table["Days run"] / n_days).round().astype(int)
    aw = arrival_window(trips, slot_keys, *win)
    mats = slot_materials(iday, mat_keys, core)
    by_hour = arrival_by_bucket(df, bucket, plan_q)
    d = daily_features(trips, plan)
    bay_table = pd.DataFrame()
    bays_per_trip = None
    if split_loc:
        bay_sd = slot_days(build_trips(df, True), ["Nag_date"] + mat_keys)
        bay_table = trip_plan(bay_sd, mat_keys, lo, plan_q).rename(
            columns={"Min": "Min bay visits", "Typical": "Typical bay visits",
                     "Plan for": "Plan for bay visits", "Max seen": "Max bay visits"})
        bays_per_trip = (df.groupby(["Nag_date", "Supply_Time", "trip_key"])
                           ["unloading_loc"].nunique().mean())

    # ---- Plan (the answer) --------------------------------------------------
    h_sd = sd if not wd else sd[sd["Nag_date"].dt.dayofweek.isin(wd)]
    h_iday = iday if not wd else iday[iday["Nag_date"].dt.dayofweek.isin(wd)]
    h_trips = trips if not wd else trips[trips["Nag_date"].dt.dayofweek.isin(wd)]
    p_slots = trip_plan(h_sd, slot_keys, lo, plan_q)
    p_aw = arrival_window(h_trips, slot_keys, *win)
    p_mats = slot_materials(h_iday, mat_keys, core)
    p_hour = arrival_by_bucket(df, bucket, plan_q, wd)

    if p_slots.empty:
        plan_view = pd.DataFrame()
    else:
        plan_view = p_slots.merge(p_aw, on=slot_keys, how="left")
        plan_view["Gate entry from"] = [
            slot_clock(r["Supply_Time"], r["Early edge (min)"])
            if pd.notna(r["Early edge (min)"]) else "" for _, r in plan_view.iterrows()]
        plan_view["Median gate entry"] = [
            slot_clock(r["Supply_Time"], r["Median (min)"])
            if pd.notna(r["Median (min)"]) else "" for _, r in plan_view.iterrows()]
        plan_view["Gate entry by"] = [
            slot_clock(r["Supply_Time"], r["Late edge (min)"])
            if pd.notna(r["Late edge (min)"]) else "" for _, r in plan_view.iterrows()]
        plan_view = plan_view[slot_keys + ["Plan for", "Typical", "Min", "Max seen",
                                           "Days run", "Gate entry from",
                                           "Median gate entry", "Gate entry by"]]
        plan_view = plan_view.rename(columns={"Plan for": "Trips to plan"})

    tp, t1, t2, t4, t5, t3 = st.tabs(
        ["PLAN", "Trips per supply time", "Gate entry vs supply time",
         "Materials per slot", "Day detail", "What drives trips up"])

    with tp:
        who = "all days" if not wd else "/".join(plan_days)
        daily_tot = h_sd.groupby("Nag_date")["trips"].sum()
        if wd:
            full = pd.date_range(dr[0], dr[1], freq="D")
            full = full[full.dayofweek.isin(wd)]
            daily_tot = daily_tot.reindex(full, fill_value=0)
        c1, c2, c3 = st.columns(3)
        c1.metric(f"Trips on a typical day ({who})", int(round(daily_tot.median())))
        c2.metric(f"Trips to plan (P{int(plan_q*100)} of days)",
                  math.ceil(daily_tot.quantile(plan_q)))
        c3.metric("Busiest day seen", int(daily_tot.max()))
        st.subheader("1. Trips to plan, and when they actually arrive")
        st.dataframe(plan_view.sort_values(slot_keys), hide_index=True, width="stretch")
        st.caption("Trips to plan = trucks covered on that share of days the slot ran. "
                   "Gate entry columns are clock times from real gate-entry history "
                   "relative to the slot; (+1d) means after midnight. Slot totals "
                   "overstate the day total because slots rarely all peak together.")
        st.subheader("2. Trucks to expect at the gate by clock time")
        st.dataframe(p_hour[p_hour["Max seen"] > 0], hide_index=True, width="stretch")
        st.subheader("3. Materials to expect")
        only_core = st.checkbox("Core items only", value=True)
        pm = p_mats[p_mats["Core item"] == "yes"] if only_core else p_mats
        st.dataframe(pm.drop(columns=["Core item"]), hide_index=True, width="stretch")
        st.caption("Came in parts % = share of days the item arrived on more than one "
                   "truck. Last split seen is received/planned per truck in gate "
                   "order, e.g. 10/15 5/15.")

    with t1:
        st.dataframe(table.sort_values(slot_keys), hide_index=True, width="stretch")
        st.caption(f"Min = P{int(lo*100)}, Plan for = P{int(plan_q*100)} of trips on "
                   "days the slot actually ran. Check 'Runs on % of days' before "
                   "staffing a slot that only runs occasionally.")
        if split_loc:
            st.subheader("Bay visits per slot")
            st.metric("Average bays visited per gate entry", f"{bays_per_trip:.1f}")
            st.dataframe(bay_table.sort_values(mat_keys), hide_index=True,
                         width="stretch")
            st.caption("A bay visit is one gate entry unloading at that bay. One truck "
                       "can visit several bays, so these do NOT add up to trips; use "
                       "them to size bay and unloading capacity, not truck count.")
        wk = (sd.groupby(slot_keys + ["weekday"])["trips"].median().round()
                .unstack("weekday"))
        wk = wk.reindex(columns=[w for w in WEEKDAYS if w in wk.columns]).reset_index()
        st.subheader("Median trips by weekday")
        st.dataframe(wk, hide_index=True, width="stretch")

    with t2:
        st.dataframe(aw.sort_values(slot_keys), hide_index=True, width="stretch")
        st.caption("Minutes relative to supply time (negative = early), first gate "
                   f"entry of each truck. Arrivals beyond {MAX_EARLY_H}h early or "
                   f"{MAX_LATE_H}h late are dropped as likely mismatches.")
        st.subheader("Trucks per gate-entry bucket (all days, zero days included)")
        st.dataframe(by_hour, hide_index=True, width="stretch")
        if not by_hour.empty:
            st.bar_chart(by_hour.set_index("Gate entry from")["Typical trucks"])

    with t4:
        pick = st.selectbox("Supply time", ["All"] + sorted(mats["Supply_Time"].unique()))
        view = mats if pick == "All" else mats[mats["Supply_Time"] == pick]
        if pick != "All":
            st.metric("Core items expected", int((view["Core item"] == "yes").sum()))
        st.dataframe(view, hide_index=True, width="stretch")
        st.caption("Qty = Received_qty summed per day, on days the item came in that "
                   "slot. Typical parts = trucks carrying the item on a normal day; "
                   "one truck can carry several items, so don't add across items.")

    with t5:
        days = sorted(lines["Nag_date"].dt.date.unique())
        day = st.selectbox("Day", days, index=len(days) - 1)
        dl = lines[lines["Nag_date"].dt.date == day].sort_values(
            ["Supply_Time", "first_entry"])
        dl = dl.rename(columns={"Supply_Time": "Supply time", "planned": "Planned",
                                "received": "Received", "trucks": "Trucks",
                                "first_entry": "First gate entry",
                                "last_entry": "Last gate entry", "parts": "Parts",
                                "vendor": "Vendor"})
        st.dataframe(dl.drop(columns=["Nag_date", "Schedule_no"]), hide_index=True,
                     width="stretch")
        st.caption("Parts = received/planned per truck, in gate-entry order.")

    with t3:
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
    detail = lines.assign(Nag_date=lines["Nag_date"].dt.date)
    st.download_button(
        "Download Excel",
        to_excel({"Plan": plan_view, "Plan arrivals by hour": p_hour,
                  "Plan materials": p_mats, "Trips per slot": table,
                  "Arrival window": aw, "Materials per slot": mats,
                  **({"Bay visits": bay_table} if split_loc else {}),
                  "Line detail": detail, "Daily": d.assign(Nag_date=d["Nag_date"].dt.date)}),
        file_name=f"trip_demand_{df['Nag_date'].max():%Y%m%d}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@st.cache_resource(show_spinner="Building schedule lines…")
def frames(path, dr, locs, vendors):
    df = load(path)
    df = df[(df["Nag_date"] >= pd.Timestamp(dr[0])) & (df["Nag_date"] <= pd.Timestamp(dr[1]))]
    if locs:
        df = df[df["unloading_loc"].isin(locs)]
    if vendors:
        df = df[df["Vendor_code"].isin(vendors)]
    lines = build_lines(df)
    return lines, build_item_days(lines)


if __name__ == "__main__":
    main()
