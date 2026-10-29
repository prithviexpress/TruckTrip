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
  * Trip report: Supply time -> Trip -> Item x Day for a month, with Excel export

Install:  pip install streamlit pandas pyarrow openpyxl numpy
Run:      streamlit run trip_demand_analysis.py
"""

import html
import io
import math
import re
from collections import Counter

import altair as alt
import numpy as np
import pandas as pd
import streamlit as st
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

DEFAULT_PATH = "nagare_data.parquet"
COLS = ["Schedule_no", "Item", "Item_name", "Vendor_code", "unloading_loc", "Nag_date",
        "Supply_Time", "Nag_qty", "Received_qty", "Truck_number",
        "Consignement_no", "Gate_entry_time", "Gate_Out_time", "location"]
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
    if "Gate_Out_time" in df.columns:
        df["Gate_Out_time"] = pd.to_datetime(df["Gate_Out_time"],
                                             format="%d-%b-%Y %H:%M", errors="coerce")
    df["Supply_Time"] = df["Supply_Time"].astype(str).str.strip().str.slice(0, 5)
    df["unloading_loc"] = df["unloading_loc"].fillna("").astype(str).str.strip()
    if "location" in df.columns:
        df["location"] = df["location"].fillna("").astype(str).str.strip()
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
    sup = pd.to_timedelta(df["Supply_Time"] + ":00", errors="coerce")
    df["slot_dt"] = df["Nag_date"] + sup
    return df.dropna(subset=["slot_dt"])


@st.cache_resource(show_spinner="Loading parquet…")
def load(path: str) -> pd.DataFrame:
    import pyarrow.parquet as pq
    have = set(pq.read_schema(path).names)
    return clean(pd.read_parquet(path, columns=[c for c in COLS if c in have]))


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
    line_keys = ["Schedule_no", "Item", "Nag_date", "Supply_Time"]
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
                     "P90 (min)": round(o.quantile(0.9)),
                     "Late edge (min)": round(o.quantile(hi)),
                     "% before slot": round(100 * (o <= 0).mean()),
                     "% >60 min late": round(100 * (o > 60).mean())})
    return pd.DataFrame(rows)


def _clean_num(v: pd.Series) -> pd.Series:
    return v.round(2).astype(str).str.replace(r"\.0$", "", regex=True)


def build_lines(df: pd.DataFrame) -> pd.DataFrame:
    """One row per schedule line (Schedule_no + Item on a day + slot).
    Parts = what each truck brought, in gate-entry order, as received/planned
    (e.g. '10/15 5/15'); empty when the line came in one truck."""
    keys = ["Nag_date", "Supply_Time", "Schedule_no", "Item", "Item_name"]
    tl = (df.groupby(keys + ["trip_key"])
            .agg(received=("Received_qty", "sum"), entry=("Gate_entry_time", "min"),
                 planned=("Nag_qty", "first"), vendor=("Vendor_code", "first"))
            .reset_index().sort_values(keys + ["entry"]))
    tl["n"] = tl.groupby(keys)["trip_key"].transform("size")
    multi = tl[tl["n"] > 1].copy()
    multi["part"] = (_clean_num(multi["received"])
                     + ("/" + _clean_num(multi["planned"])).where(
                         multi["planned"].notna(), ""))
    parts = multi.groupby(keys)["part"].agg(" ".join).rename("parts")
    lines = (tl.groupby(keys)
               .agg(planned=("planned", "first"), received=("received", "sum"),
                    trucks=("trip_key", "nunique"), first_entry=("entry", "min"),
                    last_entry=("entry", "max"), vendor=("vendor", "first"))
               .reset_index().merge(parts, on=keys, how="left"))
    lines["parts"] = lines["parts"].fillna("")
    return lines


def build_item_days(lines: pd.DataFrame) -> pd.DataFrame:
    """One row per item per day + slot (lines of the same item merged)."""
    keys = ["Nag_date", "Supply_Time", "vendor", "Item", "Item_name"]
    out = (lines.groupby(keys)
                .agg(planned=("planned", "sum"), received=("received", "sum"),
                     trucks=("trucks", "sum"), first_entry=("first_entry", "min"))
                .reset_index())
    sp = lines[lines["parts"] != ""]
    pj = sp.groupby(keys)["parts"].agg("; ".join).rename("parts")
    out = out.merge(pj, on=keys, how="left")
    out["parts"] = out["parts"].fillna("")
    return out.rename(columns={"vendor": "Vendor_code"})


def slot_materials(iday: pd.DataFrame, slot_keys, core_pct) -> pd.DataFrame:
    """What arrives in each slot: per slot x item, how often it shows up, how
    much it brings, and how often it comes in parts."""
    gk = slot_keys + ["Vendor_code", "Item", "Item_name"]
    x = iday.assign(_split=iday["parts"] != "",
                    received=iday["received"].fillna(0))
    g = x.groupby(gk)
    out = g.agg(**{"Days seen": ("Nag_date", "size"),
                   "Qty/day median": ("received", "median"),
                   "Typical parts": ("trucks", "median"),
                   "Came in parts %": ("_split", "mean"),
                   "Total qty": ("received", "sum")})
    out["Qty/day P90"] = g["received"].quantile(0.9)
    last = (x[x["_split"]].sort_values("Nag_date").groupby(gk).tail(1)
              .set_index(gk))
    out["Last split seen"] = ((last["parts"] + " (" + last["Nag_date"].dt.strftime("%d-%b")
                               + ")").reindex(out.index).fillna(""))
    out = out.reset_index()
    for c in ("Qty/day median", "Qty/day P90", "Typical parts", "Total qty"):
        out[c] = out[c].round().astype(int)
    out["Came in parts %"] = (100 * out["Came in parts %"]).round().astype(int)
    run_days = x.groupby(slot_keys)["Nag_date"].nunique().rename("slot_days")
    out = out.merge(run_days.reset_index(), on=slot_keys)
    out["% of slot days"] = (100 * out["Days seen"] / out["slot_days"]).round().astype(int)
    out["Share of slot qty %"] = (100 * out["Total qty"] / out.groupby(slot_keys)
                                  ["Total qty"].transform("sum")).round(1)
    out["Core item"] = np.where(out["% of slot days"] >= core_pct, "yes", "")
    cols = gk + ["Days seen", "Qty/day median", "Qty/day P90", "Typical parts",
                 "Came in parts %", "Last split seen", "% of slot days",
                 "Share of slot qty %", "Core item"]
    return (out[cols].sort_values(slot_keys + ["Vendor_code", "Days seen"],
                                  ascending=[True] * (len(slot_keys) + 1) + [False])
                     .reset_index(drop=True))


def _share_text(shares: pd.Series, min_pct=10, top=5) -> str:
    sh = shares[shares >= min_pct].sort_values(ascending=False).head(top)
    return ", ".join(f"{b} {round(v)}%" for b, v in sh.items())


def slot_bays(df: pd.DataFrame) -> pd.DataFrame:
    """Per supply slot: bays the trucks stop at, as % of gate entries, and the
    average number of bays a truck visits."""
    total = df.groupby("Supply_Time")["trip_key"].nunique()
    visits = df.groupby(["Supply_Time", "unloading_loc"])["trip_key"].nunique()
    rows = []
    for slot, t in total.items():
        sh = 100 * visits.loc[slot] / t
        rows.append({"Supply_Time": slot, "Bays to stop at": _share_text(sh),
                     "Bays per truck": round(visits.loc[slot].sum() / t, 1)})
    return pd.DataFrame(rows)


def item_info(df: pd.DataFrame) -> pd.DataFrame:
    """Per slot x vendor x item: the bays it was unloaded at (% of the days it
    came). Built from de-duplicated rows, so it stays quick."""
    k = ["Supply_Time", "Vendor_code", "Item"]
    days = df.groupby(k)["Nag_date"].nunique().rename("n")
    at = (df.groupby(k + ["unloading_loc"])["Nag_date"].nunique()
            .rename("d").reset_index().merge(days.reset_index(), on=k))
    at["pct"] = 100 * at["d"] / at["n"]
    at = at[at["pct"] >= 10].sort_values(k + ["pct"], ascending=[True] * 3 + [False])
    at = at.groupby(k).head(5)
    at["txt"] = at["unloading_loc"].astype(str) + " " + at["pct"].round().astype(int).astype(str) + "%"
    return at.groupby(k)["txt"].agg(", ".join).rename("Bays (% of days)").reset_index()


SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300",
          "#6250d6", "#e34948"]     # fixed categorical order, one colour per slot


def gate_scatter(trips: pd.DataFrame, slots: list):
    """Every gate entry as a dot (date x clock time), one colour per supply
    slot, a solid line at the slot's median entry time and a dashed line at
    its supply time."""
    t = trips[trips["Supply_Time"].isin(slots)].dropna(subset=["arrival"])
    t = t[t["offset_min"].between(-MAX_EARLY_H * 60, MAX_LATE_H * 60)].copy()
    if t.empty:
        return None, pd.DataFrame()
    t["hours"] = (t["arrival"] - t["Nag_date"]).dt.total_seconds() / 3600
    t["Gate entry"] = t["arrival"].dt.strftime("%d-%b %H:%M")
    t["Minutes vs slot"] = t["offset_min"].round().astype(int)
    meds = (t.groupby("Supply_Time")
              .agg(median=("hours", "median"), n=("hours", "size")).reset_index())
    meds["Median entry"] = [slot_clock(r.Supply_Time,
                                       (r.median * 60) - _slot_min(r.Supply_Time))
                            for r in meds.itertuples()]
    meds["slot_hours"] = meds["Supply_Time"].map(lambda x: _slot_min(x) / 60)
    order = sorted(slots)
    colour = alt.Color("Supply_Time:N", title="Supply time",
                       scale=alt.Scale(domain=order, range=SERIES[:len(order)]))
    y = alt.Y("hours:Q", title="Gate entry (clock time)",
              scale=alt.Scale(zero=False, nice=True),
              axis=alt.Axis(tickMinStep=1, grid=True, gridOpacity=0.25,
                            labelExpr="(((datum.value % 24) + 24) % 24 < 10 ? '0' : '')"
                                      " + floor(((datum.value % 24) + 24) % 24) + ':00'"))
    dots = alt.Chart(t).mark_circle(size=30, opacity=0.35).encode(
        x=alt.X("Nag_date:T", title="Supply date"), y=y, color=colour,
        tooltip=["Supply_Time", "Gate entry", "Minutes vs slot", "trip_key"])
    median = alt.Chart(meds).mark_rule(strokeWidth=3).encode(
        y="median:Q", color=colour, tooltip=["Supply_Time", "Median entry", "n"])
    supply = alt.Chart(meds).mark_rule(strokeDash=[4, 4], opacity=0.55, color="gray") \
        .encode(y="slot_hours:Q", tooltip=["Supply_Time"])
    return (supply + dots + median).properties(height=380), meds


def _slot_min(slot: str) -> int:
    h, m = map(int, slot.split(":"))
    return h * 60 + m


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


def vendor_trips(df: pd.DataFrame) -> pd.DataFrame:
    """One row per gate entry per vendor + day + slot, with minutes vs slot."""
    vt = (df.groupby(["Nag_date", "Supply_Time", "Vendor_code", "trip_key"])
            .agg(arrival=("Gate_entry_time", "min"), slot_dt=("slot_dt", "first"))
            .reset_index())
    vt["offset_min"] = (vt["arrival"] - vt["slot_dt"]).dt.total_seconds() / 60
    return vt[vt["offset_min"].between(-MAX_EARLY_H * 60, MAX_LATE_H * 60)]


def bay_share_rows(df: pd.DataFrame, k: list) -> pd.DataFrame:
    """Per group k and bay: % of the group's gate entries that stop there
    (>=10%, top 5 bays)."""
    total = df.groupby(k)["trip_key"].nunique().rename("n").reset_index()
    at = (df.groupby(k + ["unloading_loc"])["trip_key"].nunique().rename("v")
            .reset_index().merge(total, on=k))
    at["pct"] = 100 * at["v"] / at["n"]
    at = at[at["pct"] >= 10].sort_values(k + ["pct"], ascending=[True] * len(k) + [False])
    return at.groupby(k).head(5)


def bay_share(df: pd.DataFrame, k: list) -> pd.DataFrame:
    """Bays used per group k as text, e.g. 'WR-03 82%, WR-01 40%'."""
    at = bay_share_rows(df, k)
    at["txt"] = (at["unloading_loc"].astype(str) + " "
                 + at["pct"].round().astype(int).astype(str) + "%")
    return at.groupby(k)["txt"].agg(", ".join).rename("Bays").reset_index()


def bay_load_stats(df: pd.DataFrame) -> pd.DataFrame:
    """Trucks starting at each bay per clock hour, over all days (zeros kept)."""
    a = (df.dropna(subset=["Gate_entry_time"])
           .drop_duplicates(["trip_key", "unloading_loc"])
           [["unloading_loc", "Gate_entry_time"]])
    a = a.assign(gdate=a["Gate_entry_time"].dt.normalize(),
                 hr=a["Gate_entry_time"].dt.hour)
    n_days = (a["gdate"].max() - a["gdate"].min()).days + 1
    cnt = a.groupby(["unloading_loc", "hr", "gdate"]).size()
    rows = []
    for (bay, hr), c in cnt.groupby(level=[0, 1]):
        v = np.concatenate([c.to_numpy(), np.zeros(max(n_days - len(c), 0))])
        rows.append({"Bay": str(bay), "Hour": f"{hr:02d}:00",
                     "Typical trucks/hr": float(np.median(v)),
                     "P90 trucks/hr": float(np.quantile(v, 0.9)),
                     "Max trucks/hr": int(v.max())})
    return pd.DataFrame(rows)


def vendor_windows(vt: pd.DataFrame, cov: float, min_n: int, step: int, max_width: int):
    """Arrival window per vendor + slot covering `cov` of past trucks. Vendors
    with fewer than min_n trucks in a slot get the slot-wide window instead."""
    ql, qh = (1 - cov) / 2, 1 - (1 - cov) / 2
    sq = vt.groupby("Supply_Time")["offset_min"].quantile([ql, qh]).unstack()
    sq.columns = ["s_lo", "s_hi"]
    g = vt.groupby(["Vendor_code", "Supply_Time"])["offset_min"]
    vq = g.quantile([ql, qh]).unstack()
    vq.columns = ["v_lo", "v_hi"]
    w = (g.size().rename("Trucks").to_frame().join(vq).reset_index()
          .merge(sq.reset_index(), on="Supply_Time"))
    own = w["Trucks"] >= min_n
    w["lo"] = np.floor(np.where(own, w["v_lo"], w["s_lo"]) / step) * step
    w["hi"] = np.ceil(np.where(own, w["v_hi"], w["s_hi"]) / step) * step
    w["Basis"] = np.where(own, "Vendor history", "Slot default (few trucks)")
    w["Width (min)"] = (w["hi"] - w["lo"]).astype(int)
    w["Check"] = np.where(w["Width (min)"] > max_width,
                          "Wide: truck may serve several slots", "")
    w["Arrive from"] = [slot_clock(a, b) for a, b in zip(w["Supply_Time"], w["lo"])]
    w["Arrive by"] = [slot_clock(a, b) for a, b in zip(w["Supply_Time"], w["hi"])]
    sdef = sq.reset_index()
    sdef["lo"] = np.floor(sdef["s_lo"] / step) * step
    sdef["hi"] = np.ceil(sdef["s_hi"] / step) * step
    return w, sdef[["Supply_Time", "lo", "hi"]]


def replay_windows(vt: pd.DataFrame, cov, min_n, step, max_width, test_days):
    """Fit windows on history before the last `test_days` days, then check them
    against those days. Returns (per-day exception counts, exception list,
    per-vendor rates, summary) or None when history is too short."""
    cut = vt["Nag_date"].max() - pd.Timedelta(days=test_days - 1)
    train, test = vt[vt["Nag_date"] < cut], vt[vt["Nag_date"] >= cut]
    if train.empty or test.empty:
        return None
    w, sdef = vendor_windows(train, cov, min_n, step, max_width)
    t = (test.merge(w[["Vendor_code", "Supply_Time", "lo", "hi"]],
                    on=["Vendor_code", "Supply_Time"], how="left")
             .merge(sdef.rename(columns={"lo": "slo", "hi": "shi"}),
                    on="Supply_Time", how="left"))
    t["lo"], t["hi"] = t["lo"].fillna(t["slo"]), t["hi"].fillna(t["shi"])
    t = t.dropna(subset=["lo", "hi"])
    t["Status"] = np.select([t["offset_min"] < t["lo"], t["offset_min"] > t["hi"]],
                            ["Early", "Late"], "In window")
    t["Minutes outside"] = np.where(t["Status"] == "Early", t["lo"] - t["offset_min"],
                                    np.where(t["Status"] == "Late",
                                             t["offset_min"] - t["hi"], 0)).round()
    days = pd.date_range(test["Nag_date"].min(), test["Nag_date"].max(), freq="D")
    exc = t[t["Status"] != "In window"]
    per_day = (exc.groupby(["Nag_date", "Status"]).size().unstack(fill_value=0)
                  .reindex(days, fill_value=0))
    for c in ("Early", "Late"):
        if c not in per_day:
            per_day[c] = 0
    per_day["Exceptions"] = per_day["Early"] + per_day["Late"]
    per_day["Trucks"] = t.groupby("Nag_date").size().reindex(days, fill_value=0)
    per_day = per_day.rename_axis("Nag_date").reset_index()
    by_v = (t.groupby("Vendor_code")
              .agg(Trucks=("trip_key", "size"),
                   Early=("Status", lambda x: (x == "Early").sum()),
                   Late=("Status", lambda x: (x == "Late").sum()),
                   **{"Worst lateness (min)": ("Minutes outside", "max")})
              .reset_index())
    by_v["Exception %"] = (100 * (by_v["Early"] + by_v["Late"]) / by_v["Trucks"]).round(1)
    by_v = by_v.sort_values("Exception %", ascending=False)
    exc = exc.assign(Arrived=exc["arrival"].dt.strftime("%d-%b %H:%M"),
                     Window=[f"{slot_clock(a, b)} - {slot_clock(a, c)}" for a, b, c in
                             zip(exc["Supply_Time"], exc["lo"], exc["hi"])])
    exc = exc.sort_values("Minutes outside", ascending=False)
    summary = {"trucks": len(t), "in_pct": 100 * (t["Status"] == "In window").mean(),
               "typ": float(per_day["Exceptions"].median()),
               "p90": float(per_day["Exceptions"].quantile(0.9)),
               "late_typ": float(per_day["Late"].median()),
               "days": len(days), "from": cut}
    return per_day, exc, by_v, summary


def stop_minutes(df: pd.DataFrame) -> pd.DataFrame:
    """Observed minutes per bay stop per vendor + slot: gate entry to gate out
    divided by the bays visited. Dwell outside 10 min - 10 h is treated as bad
    data. Includes queueing, so it overstates pure unloading time."""
    if "Gate_Out_time" not in df.columns:
        return pd.DataFrame(columns=["Vendor_code", "Supply_Time", "n", "obs"])
    t = (df.groupby(["Vendor_code", "Supply_Time", "trip_key"])
           .agg(gin=("Gate_entry_time", "min"), gout=("Gate_Out_time", "max"),
                bays=("unloading_loc", "nunique")).reset_index())
    t["dwell"] = (t["gout"] - t["gin"]).dt.total_seconds() / 60
    t = t[t["dwell"].between(10, 600)]
    t["per_bay"] = t["dwell"] / t["bays"]
    return (t.groupby(["Vendor_code", "Supply_Time"])
              .agg(n=("per_bay", "size"), obs=("per_bay", "median")).reset_index())


def arrival_habit(vt: pd.DataFrame, q_start, q_stop, grace, min_n, step):
    """Safe start = the minute by which q_start of a vendor's trucks have
    arrived; hard stop = q_stop plus grace. Slot-wide figures stand in for
    vendors with fewer than min_n trucks in the slot."""
    qs = [q_start, q_stop]
    sq = vt.groupby("Supply_Time")["offset_min"].quantile(qs).unstack()
    sq.columns = ["s_a", "s_b"]
    g = vt.groupby(["Vendor_code", "Supply_Time"])["offset_min"]
    vq = g.quantile(qs).unstack()
    vq.columns = ["v_a", "v_b"]
    w = (g.size().rename("Trucks").to_frame().join(vq).reset_index()
          .merge(sq.reset_index(), on="Supply_Time"))
    own = w["Trucks"] >= min_n
    w["start"] = np.ceil(np.where(own, w["v_a"], w["s_a"]) / step) * step
    w["stop"] = np.ceil((np.where(own, w["v_b"], w["s_b"]) + grace) / step) * step
    w["Basis"] = np.where(own, "Vendor history", "Slot default (few trucks)")
    sdef = sq.reset_index()
    sdef["start"] = np.ceil(sdef["s_a"] / step) * step
    sdef["stop"] = np.ceil((sdef["s_b"] + grace) / step) * step
    return w[["Vendor_code", "Supply_Time", "Trucks", "start", "stop", "Basis"]], \
        sdef[["Supply_Time", "start", "stop"]]


def replay_habit(vt, q_start, q_stop, grace, min_n, step, test_days):
    """Fit start/stop on older data, test on the last `test_days` days. A truck
    before the start waits in the yard; between start and stop it is served late
    against the plan; after the stop its slot is withdrawn."""
    cut = vt["Nag_date"].max() - pd.Timedelta(days=test_days - 1)
    train, test = vt[vt["Nag_date"] < cut], vt[vt["Nag_date"] >= cut]
    if train.empty or test.empty:
        return None
    w, sdef = arrival_habit(train, q_start, q_stop, grace, min_n, step)
    t = (test.merge(w[["Vendor_code", "Supply_Time", "start", "stop"]],
                    on=["Vendor_code", "Supply_Time"], how="left")
             .merge(sdef.rename(columns={"start": "s0", "stop": "s1"}),
                    on="Supply_Time", how="left"))
    t["start"], t["stop"] = t["start"].fillna(t["s0"]), t["stop"].fillna(t["s1"])
    t = t.dropna(subset=["start", "stop"])
    t["Status"] = np.select([t["offset_min"] < t["start"], t["offset_min"] > t["stop"]],
                            ["Waits in yard", "Withdrawn"], "On plan, late")
    early = t["Status"] == "Waits in yard"
    on = t["Status"] == "On plan, late"
    days = pd.date_range(test["Nag_date"].min(), test["Nag_date"].max(), freq="D")
    wd_day = (t[t["Status"] == "Withdrawn"].groupby("Nag_date").size()
                .reindex(days, fill_value=0))
    sm = {"trucks": len(t), "from": cut, "days": len(days),
          "wait_pct": 100 * early.mean(),
          "wait_med": float((t.loc[early, "start"] - t.loc[early, "offset_min"]).median())
          if early.any() else 0.0,
          "late_pct": 100 * on.mean(),
          "late_p90": float((t.loc[on, "offset_min"] - t.loc[on, "start"]).quantile(0.9))
          if on.any() else 0.0,
          "wd_pct": 100 * (t["Status"] == "Withdrawn").mean(),
          "wd_typ": float(wd_day.median()), "wd_p90": float(wd_day.quantile(0.9))}
    wd = t[t["Status"] == "Withdrawn"].copy()
    wd["Arrived"] = wd["arrival"].dt.strftime("%d-%b %H:%M")
    wd["Hard stop"] = [slot_clock(a, b) for a, b in zip(wd["Supply_Time"], wd["stop"])]
    wd["Minutes after stop"] = (wd["offset_min"] - wd["stop"]).round()
    return sm, wd.sort_values("Minutes after stop", ascending=False)


STD_BASES = ["per pack", "per piece", "per item per stop"]


def guess_col(cols, words, default=0):
    for i, c in enumerate(cols):
        if any(w in str(c).lower() for w in words):
            return i
    return default


def read_table(upload) -> pd.DataFrame:
    name = upload.name.lower()
    return pd.read_csv(upload) if name.endswith(".csv") else pd.read_excel(upload)


def std_stop_minutes(ibd, std, item_col, pack_col, time_col, basis, allow_pct,
                     fixed_min, q, weekday, step=5):
    """Minutes for each vendor + slot + bay stop from your unloading standards:
    packs (qty / pack size) x standard time, plus allowance % and a fixed
    minutes per stop. Sized on the q-th percentile of past days. Returns
    (per-bay minutes with % of qty that had a standard, items with no standard)."""
    sd = std[[item_col, pack_col, time_col]].copy()
    sd.columns = ["Item", "pack", "t"]
    sd["Item"] = sd["Item"].astype(str).str.strip()
    sd["pack"] = pd.to_numeric(sd["pack"], errors="coerce")
    sd["t"] = pd.to_numeric(sd["t"], errors="coerce")
    sd = sd.drop_duplicates("Item")
    x = ibd if weekday is None else ibd[ibd["Nag_date"].dt.dayofweek == weekday]
    x = x.assign(Item=x["Item"].astype(str)).merge(sd, on="Item", how="left")
    qty = x["Received_qty"].fillna(0)
    if basis == "per pack":
        mins = np.ceil(qty / x["pack"].where(x["pack"] > 0)) * x["t"]
    elif basis == "per piece":
        mins = qty * x["t"]
    else:
        mins = x["t"].where(qty > 0)
    x = x.assign(mins=mins.fillna(0), q_std=qty.where(mins.notna(), 0), qty=qty)
    missing = (x[mins.isna()].groupby("Item")["qty"].sum().sort_values(ascending=False)
                 .rename("Qty without standard").reset_index())
    k = ["Vendor_code", "Supply_Time", "unloading_loc"]
    day = (x.groupby(k + ["Nag_date"])
             .agg(mins=("mins", "sum"), q_std=("q_std", "sum"), qty=("qty", "sum"))
             .reset_index())
    g = day.groupby(k)
    out = g["mins"].quantile(q).rename("raw").to_frame()
    out["coverage"] = (100 * g["q_std"].sum() / g["qty"].sum().replace(0, np.nan)).round()
    out["std_min"] = (np.ceil((out["raw"] * (1 + allow_pct / 100) + fixed_min) / step)
                      * step).astype(int)
    return out.reset_index().drop(columns="raw"), missing


def bay_parts(bay: str):
    m = re.match(r"^(.*?)(\d+)$", str(bay))
    return (m.group(1), int(m.group(2))) if m else (str(bay), None)


def auto_compat(bays, k: int) -> dict:
    """Fallback bays from the bay name: same letter group, number within +-k
    (AR-L3 -> AR-L2, AR-L4, AR-L1, AR-L5 for k=2), nearest first."""
    parts = {b: bay_parts(b) for b in bays}
    out = {}
    for b, (pre, n) in parts.items():
        if n is None or k <= 0:
            out[b] = []
            continue
        cand = [o for o, (p2, n2) in parts.items()
                if o != b and p2 == pre and n2 is not None and abs(n2 - n) <= k]
        out[b] = sorted(cand, key=lambda o: (abs(parts[o][1] - n), parts[o][1]))
    return out


def compat_table(compat: dict) -> pd.DataFrame:
    return pd.DataFrame({"Bay": list(compat), "Fallback": [", ".join(v) for v in compat.values()]})


def apply_compat_override(compat: dict, table: pd.DataFrame) -> dict:
    """Bays listed in the file replace the automatic fallbacks (columns Bay and
    Fallback; fallbacks separated by comma, semicolon or space)."""
    cols = {str(c).strip().lower(): c for c in table.columns}
    if "bay" not in cols or "fallback" not in cols:
        raise ValueError("needs 'Bay' and 'Fallback' columns")
    out = dict(compat)
    for bay, fb in zip(table[cols["bay"]], table[cols["fallback"]]):
        out[str(bay).strip()] = [x for x in re.split(r"[,; ]+", "" if pd.isna(fb) else str(fb))
                                 if x]
    return out


def build_bay_schedule(plan: pd.DataFrame, bays: pd.DataFrame, crit_bays: set,
                       gap: int, dur: dict = None, compat: dict = None,
                       reserve: bool = True, hot_bays=()) -> pd.DataFrame:
    """Indicative bay windows. Each vendor + slot visits its bays in order
    (critical bays first, then most-used) starting at its safe start. A bay is
    held by one truck at a time. With `reserve`, a stop is only placed where at
    least one fallback bay stays free for its window, so a slip can move to a
    neighbour. Stops that end up with no free fallback are hot; with `hot_bays`
    they are moved to those reserved bays."""
    compat = compat or {}
    rev = {}
    for y, fs in compat.items():
        for f in fs:
            rev.setdefault(f, set()).add(y)
    order = (bays.assign(crit=[(v, s_, b_) in crit_bays for v, s_, b_ in
                               zip(bays["Vendor_code"], bays["Supply_Time"],
                                   bays["unloading_loc"].astype(str))])
                 .sort_values(["Vendor_code", "Supply_Time", "crit", "pct"],
                              ascending=[True, True, False, False]))
    seq = {k: g for k, g in order.groupby(["Vendor_code", "Supply_Time"])}
    has_crit = {k: bool(g["crit"].any()) for k, g in seq.items()}
    book, stops, rows = {}, {}, []

    def free(bay, s_, e_):
        return all(not (s_ < e2 and s2 < e_) for s2, e2, _ in book.get(bay, []))

    def free_fb(bay, s_, e_):
        return [f for f in compat.get(bay, []) if free(f, s_, e_)]

    def keeps_reserve(bay, s_, e_):
        return not compat.get(bay) or bool(free_fb(bay, s_, e_))

    def try_place(bay, cursor, mins, sid, use_reserve):
        ends = sorted({e2 for b_ in [bay] + compat.get(bay, [])
                       for _, e2, _ in book.get(b_, []) if e2 > cursor})
        for begin in [cursor] + ends[:80]:
            end = begin + mins
            if not free(bay, begin, end + gap):
                continue
            if use_reserve:
                book.setdefault(bay, []).append((begin, end + gap, sid))
                ok = keeps_reserve(bay, begin, end) and all(
                    keeps_reserve(v["bay"], v["s"], v["e"]) for k_, v in stops.items()
                    if v["bay"] in rev.get(bay, ()) and v["s"] < end + gap and begin < v["e"])
                book[bay].pop()
                if not ok:
                    continue
            return begin
        return None

    plan = plan.assign(abs_start=[_slot_min(sl) + st for sl, st in
                                  zip(plan["Supply_Time"], plan["start"])])
    plan = plan.assign(_k=[not has_crit.get((v, sl), False) for v, sl in
                           zip(plan["Vendor_code"], plan["Supply_Time"])])
    sid = 0
    for r in plan.sort_values(["_k", "abs_start"]).itertuples():
        g = seq.get((r.Vendor_code, r.Supply_Time))
        if g is None:
            continue
        cursor = r.abs_start
        for i, b in enumerate(g.itertuples(), start=1):
            sid += 1
            bay = str(b.unloading_loc)
            own = (dur or {}).get((r.Vendor_code, r.Supply_Time, bay))
            mins = own if own else r.mins
            begin = try_place(bay, cursor, mins, sid, reserve and bool(compat.get(bay)))
            if begin is None:
                begin = try_place(bay, cursor, mins, sid, False)
            if begin is None:
                begin = max([cursor] + [e2 for _, e2, _ in book.get(bay, [])])
            end = begin + mins
            book.setdefault(bay, []).append((begin, end + gap, sid))
            stops[sid] = {"bay": bay, "s": begin, "e": end}
            rows.append({"sid": sid, "Vendor": r.Vendor_code, "Slot": r.Supply_Time,
                         "Stop": i, "Planned bay": bay, "Min": int(mins),
                         "Minutes basis": "Your standard" if own else r.basis,
                         "Pushed (min)": int(begin - cursor),
                         "Critical bay": "YES" if b.crit else ""})
            cursor = end + gap

    # hot stops: no fallback bay exists or none is free
    def reasons(sid_):
        v = stops[sid_]
        out = []
        if not compat.get(v["bay"]):
            out.append("No fallback bay defined")
        elif not free_fb(v["bay"], v["s"], v["e"]):
            out.append("No free fallback")
        return out

    moved = {}
    hot_bays = [str(h) for h in hot_bays]
    if hot_bays:
        for sid_ in list(stops):
            if reasons(sid_) and stops[sid_]["bay"] not in hot_bays:
                v = stops[sid_]
                book[v["bay"]] = [x for x in book[v["bay"]] if x[2] != sid_]
                best = None
                for hb in hot_bays:
                    cand = sorted({v["s"]} | {e2 for _, e2, _ in book.get(hb, []) if e2 > v["s"]})
                    for begin in cand:
                        if free(hb, begin, begin + (v["e"] - v["s"]) + gap):
                            if best is None or begin < best[0]:
                                best = (begin, hb)
                            break
                if best is None:
                    book[v["bay"]].append((v["s"], v["e"] + gap, sid_))
                    continue
                begin, hb = best
                book.setdefault(hb, []).append((begin, begin + (v["e"] - v["s"]) + gap, sid_))
                moved[sid_] = v["bay"]
                stops[sid_] = {"bay": hb, "s": begin, "e": begin + (v["e"] - v["s"])}

    out = []
    for row in rows:
        sid_ = row.pop("sid")
        v = stops[sid_]
        why = reasons(sid_) if sid_ not in moved else ["Hot lane (no fallback at planned bay)"]
        if sid_ not in moved and v["bay"] in hot_bays:
            why = []          # stops planned on a hot-lane bay are the lane's normal load
        if row["Critical bay"]:
            why = ["Critical part"] + why
        ff = free_fb(v["bay"], v["s"], v["e"]) if sid_ not in moved else []
        out.append({**{k: row[k] for k in ("Vendor", "Slot", "Stop")},
                    "Bay": v["bay"], "Planned bay": row["Planned bay"] if sid_ in moved else "",
                    "From": slot_clock("00:00", v["s"]), "To": slot_clock("00:00", v["e"]),
                    **{k: row[k] for k in ("Min", "Minutes basis", "Pushed (min)",
                                           "Critical bay")},
                    "Free fallback bays": ", ".join(ff), "Hot": "YES" if why else "",
                    "Hot reason": "; ".join(why)})
    return pd.DataFrame(out)


def simulate_bays(trips: pd.DataFrame, durf, compat: dict, gap: int, fallback: bool) -> dict:
    """Replay real gate entries: trucks take their bays in order. Without
    fallback a truck waits for its planned bay; with fallback it takes a free
    neighbour, and if none is free it counts as hot and waits for the earliest."""
    waits, swapped, hot = [], 0, 0
    for day, g in trips.groupby("Nag_date"):
        free = {}
        for r in g.sort_values("arrival").itertuples():
            t = (r.arrival - day).total_seconds() / 60
            for bay in r.bays:
                mins = durf(r.Vendor_code, r.Supply_Time, bay)
                use = bay
                if fallback:
                    opts = [bay] + compat.get(bay, [])
                    avail = [o for o in opts if free.get(o, -1e9) <= t]
                    if bay in avail:
                        use = bay
                    elif avail:
                        use = avail[0]
                        swapped += 1
                    else:
                        use = min(opts, key=lambda o: free[o])
                        hot += 1
                begin = max(t, free.get(use, -1e9))
                waits.append(begin - t)
                free[use] = begin + mins + gap
                t = begin + mins + gap
    w = np.array(waits) if waits else np.zeros(1)
    n = max(len(waits), 1)
    return {"Stops": len(waits), "Average wait (min)": round(float(w.mean()), 1),
            "P90 wait (min)": round(float(np.quantile(w, 0.9)), 1),
            "Stops waiting > 10 min %": round(100 * float((w > 10).mean()), 1),
            "Moved to a fallback bay %": round(100 * swapped / n, 1),
            "Hot (no free bay) %": round(100 * hot / n, 1)}


def read_critical(upload) -> set:
    """Item codes from a CSV/Excel with an 'Item' column; if there is also a
    'Critical' column only rows marked Y/Yes/1/True count."""
    name = upload.name.lower()
    x = pd.read_csv(upload) if name.endswith(".csv") else pd.read_excel(upload)
    cols = {str(c).strip().lower(): c for c in x.columns}
    if "item" not in cols:
        raise ValueError("needs an 'Item' column")
    if "critical" in cols:
        x = x[x[cols["critical"]].astype(str).str.strip().str.lower()
              .isin(["y", "yes", "1", "true"])]
    return set(x[cols["item"]].astype(str).str.strip())


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
def filtered(path, dr, locs, vendors) -> pd.DataFrame:
    df = load(path)
    df = df[(df["Nag_date"] >= pd.Timestamp(dr[0])) & (df["Nag_date"] <= pd.Timestamp(dr[1]))]
    if locs:
        df = df[df["unloading_loc"].isin(locs)]
    if vendors:
        df = df[df["Vendor_code"].isin(vendors)]
    return df


@st.cache_resource(show_spinner="Crunching trips, materials and bays…", max_entries=3)
def analyse(path, dr, locs, vendors, split_loc, lo, plan_q, win, bucket, core, wd):
    """All heavy tables, computed once per set of filters. The page below only
    displays them, so tab switches and dropdowns don't recompute anything."""
    df = filtered(path, dr, locs, vendors)
    R = {"df": df}
    if df.empty:
        return R
    slot_keys = ["Supply_Time"]
    wd = list(wd)
    lines = build_lines(df)
    iday = build_item_days(lines)
    # Trips are gate entries, counted once however many bays the truck visits
    trips = build_trips(df, False)
    plan = planned_qty(df, False)
    sd = slot_days(trips, ["Nag_date"] + slot_keys)
    sd["weekday"] = sd["Nag_date"].dt.dayofweek.map(dict(enumerate(WEEKDAYS)))
    n_days = (df["Nag_date"].max() - df["Nag_date"].min()).days + 1

    table = trip_plan(sd, slot_keys, lo, plan_q)
    table["Runs on % of days"] = (100 * table["Days run"] / n_days).round().astype(int)
    wk = (sd.groupby(slot_keys + ["weekday"])["trips"].median().round()
            .unstack("weekday"))
    wk = wk.reindex(columns=[w for w in WEEKDAYS if w in wk.columns]).reset_index()
    info = item_info(df)
    mats = slot_materials(iday, slot_keys, core).merge(
        info, on=["Supply_Time", "Vendor_code", "Item"], how="left")

    h_sd = sd if not wd else sd[sd["Nag_date"].dt.dayofweek.isin(wd)]
    h_iday = iday if not wd else iday[iday["Nag_date"].dt.dayofweek.isin(wd)]
    h_trips = trips if not wd else trips[trips["Nag_date"].dt.dayofweek.isin(wd)]
    h_df = df if not wd else df[df["Nag_date"].dt.dayofweek.isin(wd)]
    p_info = info if not wd else item_info(h_df)
    p_slots = trip_plan(h_sd, slot_keys, lo, plan_q)
    p_aw = arrival_window(h_trips, slot_keys, *win)
    p_mats = slot_materials(h_iday, slot_keys, core).merge(
        p_info, on=["Supply_Time", "Vendor_code", "Item"], how="left")
    p_bays = slot_bays(h_df)

    by_is_p90 = abs(win[1] - 0.9) < 1e-9     # late edge would just repeat the P90 column
    plan_view = make_plan_view(p_slots, p_aw, p_bays, slot_keys,
                               ["Bays to stop at", "Bays per truck"], by_is_p90)

    # same plan per vendor: trucks per vendor and slot, their window and bays
    vkeys = ["Supply_Time", "Vendor_code"]
    h_vt = vendor_trips(h_df)
    vsd = (h_df.groupby(["Nag_date"] + vkeys)["trip_key"].nunique()
               .rename("trips").reset_index())
    p_vbays = bay_share(h_df, ["Vendor_code", "Supply_Time"]).rename(
        columns={"Bays": "Bays to stop at"})
    plan_view_v = make_plan_view(trip_plan(vsd, vkeys, lo, plan_q),
                                 arrival_window(h_vt, vkeys, *win), p_vbays, vkeys,
                                 ["Bays to stop at"], by_is_p90)

    daily_tot = h_sd.groupby("Nag_date")["trips"].sum()
    if wd:
        full = pd.date_range(dr[0], dr[1], freq="D")
        daily_tot = daily_tot.reindex(full[full.dayofweek.isin(wd)], fill_value=0)

    bay_table, bay_keys, bays_per_trip = pd.DataFrame(), None, None
    if split_loc:
        bay_keys = slot_keys + ["unloading_loc"]
        bay_sd = slot_days(build_trips(df, True), ["Nag_date"] + bay_keys)
        bay_table = trip_plan(bay_sd, bay_keys, lo, plan_q).rename(
            columns={"Min": "Min bay visits", "Typical": "Typical bay visits",
                     "Plan for": "Plan for bay visits", "Max seen": "Max bay visits"})
        bays_per_trip = (df.groupby(["Nag_date", "Supply_Time", "trip_key"])
                           ["unloading_loc"].nunique().mean())

    R.update(
        lines=lines, trips=trips, n_days=n_days, table=table, wk=wk, mats=mats,
        aw=arrival_window(trips, slot_keys, *win),
        by_hour=arrival_by_bucket(df, bucket, plan_q),
        d=daily_features(trips, plan), plan_view=plan_view, plan_view_v=plan_view_v, p_mats=p_mats,
        p_hour=arrival_by_bucket(df, bucket, plan_q, wd), daily_tot=daily_tot,
        bay_table=bay_table, bay_keys=bay_keys, bays_per_trip=bays_per_trip,
        vt=vendor_trips(df), vs_bays=bay_share(df, ["Vendor_code", "Supply_Time"]),
        vs_bay_rows=bay_share_rows(df, ["Vendor_code", "Supply_Time"]),
        stop_min=stop_minutes(df),
        ibd=(df.groupby(["Vendor_code", "Supply_Time", "unloading_loc", "Item", "Nag_date"])
               ["Received_qty"].sum().reset_index()),
        bay_load=bay_load_stats(df))
    return R


def critical_input(key: str) -> set:
    """Uploader for critical items; the loaded set is shared across tabs."""
    up = st.file_uploader("Critical items (CSV/Excel with an 'Item' column; optional "
                          "'Critical' Y/N column)", type=["csv", "xlsx"], key=key)
    if up is not None:
        try:
            st.session_state["crit_items"] = read_critical(up)
        except Exception as e:
            st.error(f"Could not read critical items: {e}")
    crit = st.session_state.get("crit_items", set())
    if crit:
        st.caption(f"{len(crit)} critical items loaded (used on every tab).")
    return crit


def bay_schedule_tab(R, df):
    st.markdown("Fix an **indicative bay schedule** from arrival habit. Each vendor and "
                "slot gets a **safe start** (the time by which most of its trucks have "
                "usually arrived), a **hard stop** after which the slot is withdrawn, "
                "and indicative windows for each bay in between.")
    a = st.columns(5)
    day_pick = a[0].selectbox("Plan for weekday", ["All days"] + WEEKDAYS)
    q_start = a[1].select_slider("Safe start = trucks arrived by", [0.5, 0.7, 0.8, 0.9],
                                 value=0.8, format_func=lambda v: f"P{int(v*100)}")
    q_stop = a[2].select_slider("Hard stop = trucks arrived by", [0.9, 0.95, 0.99],
                                value=0.95, format_func=lambda v: f"P{int(v*100)}")
    grace = a[3].select_slider("Grace after that (min)", [0, 15, 30, 45], value=15)
    step = a[4].select_slider("Round to (min)", [5, 15, 30], value=15, key="bs_step")
    b = st.columns(5)
    min_n = b[0].number_input("Min trucks for own timing", 3, 100, 10, key="bs_minn")
    mode = b[1].radio("Minutes per bay stop", ["Manual", "Observed", "Your standards"],
                      horizontal=True)
    manual = b[2].number_input("Manual minutes per stop", 5, 90, 25)
    gap = b[3].number_input("Gap between stops (min)", 0, 30, 5)
    min_run = b[4].number_input("Include vendors running on at least (% days)", 0, 100, 20)
    test_days = st.number_input("Replay: last N days", 7, 60, 14, key="bs_test")
    crit = critical_input("crit_bay")
    std_res = None
    with st.expander("Unloading standards: pack size, unload time, allowances",
                     expanded=(mode == "Your standards")):
        su = st.file_uploader("Standards file (CSV/Excel: one row per item with pack "
                              "size and unloading time)", type=["csv", "xlsx"], key="std_up")
        if su is not None:
            try:
                st.session_state["std_df"] = read_table(su)
            except Exception as e:
                st.error(f"Could not read standards: {e}")
        sdf = st.session_state.get("std_df")
        if sdf is not None:
            cols = list(sdf.columns)
            m = st.columns(4)
            ic = m[0].selectbox("Item column", cols, index=guess_col(cols, ["item", "part"]))
            pc = m[1].selectbox("Pack size column", cols,
                                index=guess_col(cols, ["pack", "snp", "qty"], 1 % len(cols)))
            tc = m[2].selectbox("Unloading time column (minutes)", cols,
                                index=guess_col(cols, ["time", "min", "std"], 2 % len(cols)))
            basis = m[3].selectbox("Time is", STD_BASES)
            n = st.columns(3)
            allow = n[0].number_input("Allowance (% on top)", 0, 100, 15)
            fixed = n[1].number_input("Fixed minutes per stop (positioning, paperwork)",
                                      0, 60, 5)
            sq = n[2].select_slider("Size stop on", [0.5, 0.75, 0.9], value=0.75,
                                    format_func=lambda v: f"P{int(v*100)} of past days")
            st.session_state["std_cfg"] = (ic, pc, tc, basis, allow, fixed, sq)
            st.caption(f"{len(sdf)} items in the standards file.")
        else:
            st.caption("Upload a file to size each bay stop from pack size and standard "
                       "time instead of the manual or observed minutes.")

    vt = R["vt"]
    n_days = df["Nag_date"].nunique()
    if day_pick != "All days":
        dsel = WEEKDAYS.index(day_pick)
        vt = vt[vt["Nag_date"].dt.dayofweek == dsel]
        n_days = df.loc[df["Nag_date"].dt.dayofweek == dsel, "Nag_date"].nunique()
    if vt.empty:
        st.info("No gate entries for this weekday.")
        return

    w, _ = arrival_habit(vt, q_start, q_stop, grace, int(min_n), step)
    runs = (vt.groupby(["Vendor_code", "Supply_Time"])["Nag_date"].nunique()
              .rename("days").reset_index())
    w = w.merge(runs, on=["Vendor_code", "Supply_Time"])
    w["Runs on % of days"] = (100 * w["days"] / max(n_days, 1)).round().astype(int)
    sm_ = R["stop_min"]
    w = w.merge(sm_, on=["Vendor_code", "Supply_Time"], how="left")
    use_obs = (mode == "Observed") & w["n"].fillna(0).ge(int(min_n))
    w["mins"] = np.where(use_obs, (w["obs"].fillna(manual) / 5).round().clip(2, 12) * 5,
                         manual)
    w["Minutes basis"] = np.where(use_obs, "Observed", "Manual")
    if mode == "Observed" and sm_.empty:
        st.warning("No Gate_Out_time in the data, so the manual minutes are used.")
    plan = w[w["Runs on % of days"] >= min_run].copy()
    plan["basis"] = plan["Minutes basis"]
    dur = {}
    if mode == "Your standards":
        sdf, cfg = st.session_state.get("std_df"), st.session_state.get("std_cfg")
        if sdf is None or cfg is None:
            st.warning("No standards loaded yet: using the manual minutes.")
        else:
            wday = None if day_pick == "All days" else WEEKDAYS.index(day_pick)
            stdm, missing = std_stop_minutes(R["ibd"], sdf, *cfg[:3], cfg[3], cfg[4],
                                             cfg[5], cfg[6], wday)
            ok = stdm[stdm["coverage"] >= 50]
            dur = {(str(a), str(b), str(c)): int(d) for a, b, c, d in
                   zip(ok["Vendor_code"], ok["Supply_Time"], ok["unloading_loc"],
                       ok["std_min"])}
            share = len(ok) / max(len(stdm), 1)
            st.caption(f"Standards cover {share:.0%} of vendor-slot-bay stops "
                       "(>= 50% of their quantity). Stops without enough coverage use "
                       "the manual minutes.")
            if len(missing):
                with st.expander(f"{len(missing)} items have no usable standard"):
                    st.dataframe(missing.head(50), hide_index=True, width="stretch")

    rows = R["vs_bay_rows"]
    rows = rows.merge(plan[["Vendor_code", "Supply_Time"]], on=["Vendor_code", "Supply_Time"])
    universe = sorted(df["unloading_loc"].astype(str).unique())
    c3 = st.columns(4)
    kfb = c3[0].select_slider("Fallback bays within +- N numbers", [0, 1, 2, 3], value=2)
    reserve = c3[1].checkbox("Keep a free fallback bay when planning", value=True)
    hot_bays = c3[2].multiselect("Hot-lane bays (optional)", universe,
                                 help="Stops with no free fallback move here")
    compat = auto_compat(universe, int(kfb))
    with c3[3]:
        cu = st.file_uploader("Fallback map to override (CSV: Bay, Fallback)", type=["csv", "xlsx"],
                              key="compat_up")
        if cu is not None:
            try:
                st.session_state["compat_df"] = read_table(cu)
            except Exception as e:
                st.error(f"Could not read fallback map: {e}")
    if st.session_state.get("compat_df") is not None:
        try:
            compat = apply_compat_override(compat, st.session_state["compat_df"])
        except Exception as e:
            st.error(f"Fallback map ignored: {e}")
    st.download_button("Download fallback map to edit", compat_table(compat).to_csv(index=False),
                       file_name="fallback_map.csv", mime="text/csv")
    crit_bays = set()
    if crit:
        cr = df[df["Item"].astype(str).isin(crit)]
        crit_bays = set(zip(cr["Vendor_code"].astype(str), cr["Supply_Time"].astype(str),
                            cr["unloading_loc"].astype(str)))
    sched = build_bay_schedule(plan.assign(Vendor_code=plan["Vendor_code"].astype(str),
                                           Supply_Time=plan["Supply_Time"].astype(str)),
                               rows.assign(Vendor_code=rows["Vendor_code"].astype(str),
                                           Supply_Time=rows["Supply_Time"].astype(str)),
                               crit_bays, int(gap), dur, compat, reserve, hot_bays)

    head = plan.assign(**{
        "Safe start": [slot_clock(a_, b_) for a_, b_ in zip(plan["Supply_Time"], plan["start"])],
        "Hard stop (withdraw slot)": [slot_clock(a_, b_) for a_, b_ in
                                      zip(plan["Supply_Time"], plan["stop"])]})
    nb = rows.groupby(["Vendor_code", "Supply_Time"]).size().rename("Bays").reset_index()
    head = head.merge(nb, on=["Vendor_code", "Supply_Time"], how="left")
    head = head[["Vendor_code", "Supply_Time", "Safe start", "Hard stop (withdraw slot)",
                 "Bays", "mins", "Minutes basis", "Trucks", "Runs on % of days",
                 "Basis"]].rename(columns={"Vendor_code": "Vendor", "Supply_Time": "Slot",
                                           "mins": "Min per stop", "Trucks": "Trucks in history"})
    head = head.sort_values(["Slot", "Vendor"])

    st.subheader("1. Safe start and hard stop per vendor and slot")
    st.dataframe(head, hide_index=True, width="stretch")
    st.caption("Before the safe start the truck waits in the yard. Between safe start "
               "and hard stop it is served as soon as a bay is free. After the hard "
               "stop the slot is withdrawn and the truck is re-queued.")

    st.subheader("2. Replay: how often would this have bitten?")
    rp = replay_habit(vt, q_start, q_stop, grace, int(min_n), step, int(test_days))
    if rp is None:
        st.info("Not enough history before the replay period.")
        wd_list = pd.DataFrame()
    else:
        sm, wd_list = rp
        m = st.columns(4)
        m[0].metric("Trucks waiting in yard", f"{sm['wait_pct']:.0f}%",
                    f"median {sm['wait_med']:.0f} min")
        m[1].metric("Served late vs safe start", f"{sm['late_pct']:.0f}%",
                    f"P90 {sm['late_p90']:.0f} min")
        m[2].metric("Slots withdrawn", f"{sm['wd_pct']:.1f}%")
        m[3].metric("Withdrawals, bad day (P90)", f"{sm['wd_p90']:.0f}",
                    f"typical {sm['wd_typ']:.0f}")
        st.caption(f"Timings fitted before {sm['from']:%d-%b-%Y}, tested on the last "
                   f"{sm['days']} days ({sm['trucks']} trucks). Raise the hard-stop "
                   "percentile or grace if withdrawals are too many; lower the safe-start "
                   "percentile if too many trucks wait.")
        with st.expander("Trucks whose slot would have been withdrawn"):
            if wd_list.empty:
                st.write("None.")
            else:
                e = wd_list[["Nag_date", "Supply_Time", "Vendor_code", "Arrived",
                             "Hard stop", "Minutes after stop"]].copy()
                e["Nag_date"] = e["Nag_date"].dt.date
                st.dataframe(e.head(500), hide_index=True, width="stretch")

    st.subheader("2b. Replay: do fallback bays cut the waiting?")
    sim_cut = vt["Nag_date"].max() - pd.Timedelta(days=int(test_days) - 1)
    dtest = df[df["Nag_date"] >= sim_cut]
    if dtest.empty or sched.empty:
        st.info("Nothing to replay.")
        sim_tbl = pd.DataFrame()
    else:
        rank = {(str(v), str(s_), str(b_)): i for i, (v, s_, b_) in enumerate(
            zip(rows["Vendor_code"], rows["Supply_Time"], rows["unloading_loc"]))}
        tr = (dtest.groupby(["Nag_date", "trip_key", "Vendor_code", "Supply_Time"])
                   .agg(arrival=("Gate_entry_time", "min"),
                        bays=("unloading_loc", lambda s_: list(dict.fromkeys(map(str, s_)))))
                   .reset_index().dropna(subset=["arrival"]))
        tr["bays"] = [sorted(b_, key=lambda x, v=v, s_=s_: rank.get((str(v), str(s_), x), 999))
                      for b_, v, s_ in zip(tr["bays"], tr["Vendor_code"], tr["Supply_Time"])]
        vs_min = {(str(a_), str(b_)): m_ for a_, b_, m_ in
                  zip(plan["Vendor_code"], plan["Supply_Time"], plan["mins"])}
        durf = lambda v, s_, bay: dur.get((str(v), str(s_), bay)) or vs_min.get(
            (str(v), str(s_)), manual)
        base_m = simulate_bays(tr, durf, compat, int(gap), False)
        fb_m = simulate_bays(tr, durf, compat, int(gap), True)
        sim_tbl = pd.DataFrame({"Metric": list(base_m), "Planned bay only": list(base_m.values()),
                                "With fallback bays": list(fb_m.values())})
        st.dataframe(sim_tbl, hide_index=True, width="stretch")
        st.caption(f"Last {int(test_days)} days of real gate entries, each truck taking its "
                   "bays in order. Without fallback a truck waits for its planned bay; with "
                   "fallback it takes a free neighbour, and 'hot' means none was free. "
                   "Stop lengths come from your minutes settings. A simplification: bays "
                   "are not shared with trucks outside the vendors listed.")

    st.subheader("3. Indicative bay windows")
    if sched.empty:
        st.info("No vendor and slot meets the filters.")
    else:
        pushed = sched[sched["Pushed (min)"] > 0]
        k = st.columns(3)
        k[0].metric("Stops planned", len(sched))
        k[1].metric("Stops pushed by a busy bay", len(pushed))
        k[2].metric("Largest push (min)", int(sched["Pushed (min)"].max()))
        pick = st.selectbox("Preview one trip card",
                            sorted({f"{v} @ {s}" for v, s in zip(sched["Vendor"], sched["Slot"])}))
        pv, ps = pick.split(" @ ")
        card = sched[(sched["Vendor"] == pv) & (sched["Slot"] == ps)]
        hrow = head[(head["Vendor"] == pv) & (head["Slot"] == ps)].iloc[0]
        st.markdown(f"**{pv}**, slot {ps}: safe start **{hrow['Safe start']}**, hard stop "
                    f"**{hrow['Hard stop (withdraw slot)']}**")
        st.dataframe(card[["Stop", "Bay", "From", "To", "Min", "Minutes basis",
                           "Pushed (min)", "Critical bay"]],
                     hide_index=True, width="stretch")
        with st.expander("All indicative windows"):
            st.dataframe(sched, hide_index=True, width="stretch")
        st.subheader("4. Hot trips: handle separately")
        hot = sched[sched["Hot"] == "YES"]
        hk = st.columns(3)
        hk[0].metric("Hot stops", f"{100 * len(hot) / len(sched):.0f}%", f"{len(hot)} of {len(sched)}")
        hk[1].metric("Vendor-slots with a hot stop",
                     hot.groupby(["Vendor", "Slot"]).ngroups if len(hot) else 0)
        hk[2].metric("Critical-part stops", int((sched["Critical bay"] == "YES").sum()))
        if hot.empty:
            st.success("No hot stops with these settings.")
        else:
            hot_trips = (hot.groupby(["Vendor", "Slot"])
                            .agg(**{"Hot stops": ("Stop", "size"),
                                    "Bays": ("Bay", lambda v: ", ".join(v)),
                                    "Why": ("Hot reason", lambda v: "; ".join(sorted(set(v))))})
                            .reset_index())
            st.dataframe(hot_trips, hide_index=True, width="stretch")
        st.caption("Hot = carries a critical part, or no fallback bay is free (or defined) "
                   "for its window. If hot stops are more than about 10% of the plan, the "
                   "plan is too tight: widen windows, add a reserved hot-lane bay, or move "
                   "a vendor to another slot.")
        st.caption("Windows are indicative. Stops go in order, critical bays first, then "
                   "the bay the vendor uses most. 'Pushed' means another vendor holds that "
                   "bay then, so this stop starts later than the vendor's own sequence "
                   "would; large pushes mean moving a vendor to another slot or bay. The "
                   "plan assumes every listed vendor comes: that is the busy case.")

    if st.button("Prepare bay schedule file", key="bs_btn"):
        sheets = {"Safe start & hard stop": head, "Bay windows": sched,
                  "Fallback map": compat_table(compat)}
        if not sched.empty:
            sheets["Hot stops"] = sched[sched["Hot"] == "YES"]
        if not sim_tbl.empty:
            sheets["Fallback replay"] = sim_tbl
        if rp is not None and not wd_list.empty:
            sheets["Replay withdrawals"] = wd_list[["Nag_date", "Supply_Time", "Vendor_code",
                                                    "Arrived", "Hard stop",
                                                    "Minutes after stop"]].assign(
                Nag_date=wd_list["Nag_date"].dt.date)
        st.session_state["bayfile"] = to_excel(sheets)
    if "bayfile" in st.session_state:
        st.download_button("Download bay schedule", st.session_state["bayfile"],
                           file_name="bay_schedule.xlsx",
                           mime="application/vnd.openxmlformats-officedocument."
                                "spreadsheetml.sheet")


def window_planner(R, df):
    st.markdown("One arrival **window** per vendor and slot, from real gate-entry "
                "history. The driver gets a single from-to time and a bay list; yard "
                "staff decide the order on the day and handle only the exceptions.")
    c = st.columns(5)
    cov = c[0].select_slider("Window covers", [0.8, 0.9, 0.95], value=0.9,
                             format_func=lambda v: f"{int(v*100)}% of trucks")
    min_n = c[1].number_input("Min trucks for own window", 3, 100, 10)
    step = c[2].select_slider("Round to (min)", [5, 10, 15, 30], value=15)
    max_w = c[3].number_input("Flag windows wider than (min)", 30, 720, 180, step=30)
    test_days = c[4].number_input("Replay: last N days", 7, 60, 14)
    crit = critical_input("crit_win")

    vt = R["vt"]
    w, _ = vendor_windows(vt, cov, min_n, step, max_w)
    w = w.merge(R["vs_bays"], on=["Vendor_code", "Supply_Time"], how="left")
    crit_trips = set()
    if crit:
        cr = df[df["Item"].astype(str).isin(crit)]
        crit_trips = set(cr["trip_key"].unique())
        ci = (cr[["Vendor_code", "Supply_Time", "Item"]].drop_duplicates()
                .groupby(["Vendor_code", "Supply_Time"])["Item"]
                .agg(lambda v: ", ".join(map(str, sorted(v))[:5])).rename("Critical items")
                .reset_index())
        w = w.merge(ci, on=["Vendor_code", "Supply_Time"], how="left")
    cols = ["Vendor_code", "Supply_Time", "Arrive from", "Arrive by", "Width (min)",
            "Bays", "Trucks", "Basis", "Check"] + (["Critical items"] if crit else [])
    sheet = w[cols].rename(columns={"Vendor_code": "Vendor", "Supply_Time": "Slot",
                                    "Trucks": "Trucks in history"}).fillna("")
    sheet = sheet.sort_values(["Slot", "Vendor"])

    st.subheader("1. Window per vendor and slot")
    st.dataframe(sheet, hide_index=True, width="stretch")
    st.caption("Arrive from / by are gate-entry clock times; (+1d) = after midnight. "
               "Bays are listed by how often that vendor's trucks stop there; no "
               "times per bay. Wide windows usually mean one truck serves several "
               "slots or arrives the day before: check those vendors by hand.")

    st.subheader("2. Replay: would these windows have worked?")
    rp = replay_windows(vt, cov, min_n, step, max_w, int(test_days))
    if rp is None:
        st.info("Not enough history before the replay period.")
        per_day = exc = by_v = None
    else:
        per_day, exc, by_v, sm = rp
        a, b, c2, d = st.columns(4)
        a.metric("Trucks inside window", f"{sm['in_pct']:.0f}%")
        b.metric("Exceptions on a typical day", f"{sm['typ']:.0f}")
        c2.metric("Exceptions on a bad day (P90)", f"{sm['p90']:.0f}")
        d.metric("Late arrivals, typical day", f"{sm['late_typ']:.0f}")
        st.caption(f"Windows fitted on data before {sm['from']:%d-%b-%Y}, then "
                   f"checked against the last {sm['days']} days ({sm['trucks']} "
                   "trucks) they hadn't seen. If a bad day's exceptions are more than "
                   "your yard can handle, widen the window or move the vendor.")
        st.markdown("**Vendors with the most exceptions**")
        st.dataframe(by_v.head(15), hide_index=True, width="stretch")
        with st.expander("Every exception in the replay"):
            e = exc[["Nag_date", "Supply_Time", "Vendor_code", "Status", "Arrived",
                     "Window", "Minutes outside"]].copy()
            e["Nag_date"] = e["Nag_date"].dt.date
            if crit:
                e["Carries critical item"] = np.where(
                    exc["trip_key"].isin(crit_trips), "YES", "")
            st.dataframe(e.head(500), hide_index=True, width="stretch")

    st.subheader("3. Bays that run hot")
    k1, k2 = st.columns(2)
    cap = k1.number_input("Trucks one bay can take per hour", 0.5, 10.0, 2.0, step=0.5)
    thr = k2.slider("Flag when busy-day load is above (% of capacity)", 50, 120, 80)
    bl = R["bay_load"].copy()
    bl["Busy-day load %"] = (100 * bl["P90 trucks/hr"] / cap).round()
    hot = bl[bl["Busy-day load %"] >= thr].sort_values("Busy-day load %", ascending=False)
    if hot.empty:
        st.success("No bay-hour above the threshold.")
    else:
        st.dataframe(hot, hide_index=True, width="stretch")
    st.caption("Counts trucks entering the gate for each bay per hour: a stand-in "
               "for bay load because bay-in/out times aren't in the data. Capacity "
               "is your own estimate.")

    st.subheader("4. Yard rules")
    st.markdown(RULES)
    if st.button("Prepare yard sheet"):
        sheets = {"Windows": sheet, "Yard rules": pd.DataFrame({"Rule": RULES_LIST}),
                  "Hot bays": hot}
        if per_day is not None:
            sheets["Replay by day"] = per_day.assign(
                Nag_date=per_day["Nag_date"].dt.date)
            sheets["Replay by vendor"] = by_v
        st.session_state["yard"] = to_excel(sheets)
    if "yard" in st.session_state:
        st.download_button("Download yard sheet", st.session_state["yard"],
                           file_name="yard_windows.xlsx",
                           mime="application/vnd.openxmlformats-officedocument."
                                "spreadsheetml.sheet")


RULES_LIST = [
    "Truck inside its window: unload at the first free bay from its bay list.",
    "Early truck: hold in the yard; unload sooner only if a listed bay is free.",
    "Late truck (past the window end): serve at the next free bay after the trucks "
    "already waiting, but ahead of any truck without critical items.",
    "Truck carrying a critical item: always served first, whatever its arrival order.",
    "No-show: if a truck has not arrived 60 minutes after its window ends, release "
    "its bay and re-queue the truck on arrival.",
    "Log every exception with a reason (vendor late, breakdown, wrong slot, other) "
    "so the windows can be re-tuned each week.",
]
RULES = "\n".join(f"- {r}" for r in RULES_LIST)


# ----------------------------------------------------------------------------
# Trip report: Supply time -> Trip -> Item x Day, one month at a time
# ----------------------------------------------------------------------------
REPORT_QTY = ["Received_qty", "Planned qty (Nag_qty once per schedule line)"]


def _fmt_gate(ts, day):
    if ts is None or pd.isna(ts):
        return ""
    t = ts.strftime("%H:%M")
    diff = (ts.normalize() - day).days
    return t if diff == 0 else f"{t} ({diff:+d}d)"      # entry on another day


def _fmt_qty(v):
    if v is None or pd.isna(v):
        return None
    return int(v) if float(v).is_integer() else round(float(v), 2)


def _month_days(period):
    return list(pd.date_range(period.start_time, period.end_time.normalize(), freq="D"))


def report_rows(m: pd.DataFrame, days, qty_mode: str):
    """Rows for one month. A trip is one gate entry (a returning truck is a new
    trip); trips are numbered Trip1, Trip2... per day + supply time by gate time."""
    loc = "location" if "location" in m.columns else "unloading_loc"
    m = m.assign(_loc=m[loc].astype(str), Item_name=m["Item_name"].astype(str))
    if qty_mode == REPORT_QTY[0]:
        m = m.assign(_q=m["Received_qty"])
    else:   # Nag_qty repeats on every truck row of a line: count it once
        m = m.sort_values("Gate_entry_time")
        first = ~m.duplicated(["Schedule_no", "Item", "Nag_date", "Supply_Time"])
        m = m.assign(_q=m["Nag_qty"].where(first))
    trips = (m.groupby(["Nag_date", "Supply_Time", "trip_key"])["Gate_entry_time"].min()
              .reset_index().rename(columns={"Gate_entry_time": "trip_gate"})
              .sort_values(["Nag_date", "Supply_Time", "trip_gate"], na_position="last"))
    trips["trip_no"] = trips.groupby(["Nag_date", "Supply_Time"]).cumcount() + 1
    m = m.merge(trips[["Nag_date", "Supply_Time", "trip_key", "trip_no"]],
                on=["Nag_date", "Supply_Time", "trip_key"])
    gate = trips.pivot(index=["Supply_Time", "trip_no"], columns="Nag_date",
                       values="trip_gate")
    q = (m.groupby(["Supply_Time", "trip_no", "Item_name", "_loc", "Nag_date"])["_q"]
          .sum(min_count=1).unstack("Nag_date"))
    if qty_mode == REPORT_QTY[0]:
        q = q.dropna(how="all")     # planned view keeps later parts as blank rows
    qg = {k: g for k, g in q.groupby(level=[0, 1])}
    rows = []
    for (sup, trip), grow in gate.iterrows():
        # gate-entry habit of this trip across the month, as minutes from the
        # supply date so an entry after midnight sorts after a late-evening one
        mins = pd.Series([(g - d).total_seconds() / 60 for d, g in grow.items()
                          if pd.notna(g)])
        label = (f"Trip{trip}\nfrom {slot_clock('00:00', mins.min())}\n"
                 f"median {slot_clock('00:00', mins.median())}\n"
                 f"P90 {slot_clock('00:00', mins.quantile(0.9))}" if len(mins) else f"Trip{trip}")
        rows.append({"supply": sup, "trip": label, "item": "Gate entry",
                     "loc": "", "kind": "gate",
                     "vals": [_fmt_gate(grow.get(d), d) for d in days]})
        for (_, _, item, lc), r in (qg.get((sup, trip)) if (sup, trip) in qg
                                    else pd.DataFrame()).iterrows():
            rows.append({"supply": sup, "trip": label, "item": item, "loc": lc,
                         "kind": "item", "vals": [_fmt_qty(r.get(d)) for d in days]})
    return rows


@st.cache_resource(show_spinner="Building month…", max_entries=6)
def report_month(path, dr, locs, vendors, month, qty_mode):
    df = filtered(path, dr, locs, vendors)
    period = pd.Period(month, "M")
    m = df[df["Nag_date"].dt.to_period("M") == period]
    days = _month_days(period)
    return (report_rows(m, days, qty_mode) if len(m) else []), days


REPORT_CSS = """
<style>
.nt-wrap{overflow:auto;max-height:75vh;border:1px solid #cfd6de}
.nt{border-collapse:collapse;font-size:13px;font-variant-numeric:tabular-nums}
.nt th,.nt td{border:1px solid #cfd6de;padding:3px 8px;white-space:nowrap}
.nt thead th{position:sticky;top:0;background:#1f3a5f;color:#fff;z-index:2}
.nt td.num{text-align:right}
.nt td.grp{vertical-align:middle;text-align:center;font-weight:600;background:#eef2f6}
.nt td.trip{font-weight:400;font-size:12px;line-height:1.35}
.nt td.trip::first-line{font-weight:600}
.nt tr.gate td{background:#fff4d6;font-weight:600;color:#7a5200}
.nt tr.gate td.lbl{font-style:italic;font-weight:500}
</style>
"""


def report_html(rows, days):
    sup_span = Counter(r["supply"] for r in rows)
    trip_span = Counter((r["supply"], r["trip"]) for r in rows)
    seen_s, seen_t = set(), set()
    head = ("<tr><th>Supply time</th><th>Trip</th><th>Items</th><th>Location</th>"
            + "".join(f"<th>{d:%d-%b}</th>" for d in days) + "</tr>")
    body = []
    for r in rows:
        cells = []
        if r["supply"] not in seen_s:
            seen_s.add(r["supply"])
            cells.append(f'<td class="grp" rowspan="{sup_span[r["supply"]]}">'
                         f'{html.escape(r["supply"])}</td>')
        key = (r["supply"], r["trip"])
        if key not in seen_t:
            seen_t.add(key)
            cells.append(f'<td class="grp trip" rowspan="{trip_span[key]}">'
                         f'{html.escape(r["trip"]).replace(chr(10), "<br>")}</td>')
        cls = "lbl" if r["kind"] == "gate" else ""
        cells.append(f'<td class="{cls}">{html.escape(r["item"])}</td>')
        cells.append(f'<td>{html.escape(r["loc"])}</td>')
        cells += [f'<td class="num">{"" if v is None else v}</td>' for v in r["vals"]]
        body.append(f'<tr class="{r["kind"]}">{"".join(cells)}</tr>')
    return (REPORT_CSS + '<div class="nt-wrap"><table class="nt"><thead>' + head
            + "</thead><tbody>" + "".join(body) + "</tbody></table></div>")


_THIN = Side(style="thin", color="A6B0BB")
_BORDER = Border(left=_THIN, right=_THIN, top=_THIN, bottom=_THIN)
_HEAD_FILL = PatternFill("solid", fgColor="1F3A5F")
_GRP_FILL = PatternFill("solid", fgColor="EEF2F6")
_GATE_FILL = PatternFill("solid", fgColor="FFF4D6")


def write_report_sheet(ws, rows, days):
    ws.append(["Supply time", "Trip", "Items", "Location"] + [d.strftime("%d-%b") for d in days])
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = _HEAD_FILL
        c.alignment = Alignment(horizontal="center")
        c.border = _BORDER
    prev_s = prev_t = None
    s_start = t_start = 2
    merges = []
    for i, r in enumerate(rows, start=2):
        key_t = (r["supply"], r["trip"])
        if r["supply"] != prev_s:
            if prev_s is not None:
                merges.append((1, s_start, i - 1))
            s_start, prev_s = i, r["supply"]
        if key_t != prev_t:
            if prev_t is not None:
                merges.append((2, t_start, i - 1))
            t_start, prev_t = i, key_t
        ws.append([r["supply"] if i == s_start else None,
                   r["trip"] if i == t_start else None, r["item"], r["loc"]] + r["vals"])
        for c in ws[i]:
            c.border = _BORDER
            if r["kind"] == "gate" and c.column > 2:
                c.fill = _GATE_FILL
                c.font = Font(bold=True, color="7A5200", italic=(c.column == 3))
                c.alignment = Alignment(horizontal="center")
    if rows:
        merges += [(1, s_start, len(rows) + 1), (2, t_start, len(rows) + 1)]
    for col, a, b in merges:
        if b > a:
            ws.merge_cells(start_row=a, end_row=b, start_column=col, end_column=col)
        cell = ws.cell(row=a, column=col)
        cell.alignment = Alignment(horizontal="center", vertical="center",
                                   wrap_text=(col == 2))
        cell.font = Font(bold=True)
        cell.fill = _GRP_FILL
    for col, w in (("A", 12), ("B", 14), ("C", 38), ("D", 11)):
        ws.column_dimensions[col].width = w
    for j in range(5, 5 + len(days)):
        ws.column_dimensions[get_column_letter(j)].width = 8
    ws.freeze_panes = "E2"


def report_excel(path, dr, locs, vendors, qty_mode) -> bytes:
    df = filtered(path, dr, locs, vendors)
    wb = Workbook()
    wb.remove(wb.active)
    for period in sorted(df["Nag_date"].dt.to_period("M").unique()):
        m = df[df["Nag_date"].dt.to_period("M") == period]
        days = _month_days(period)
        write_report_sheet(wb.create_sheet(title=period.strftime("%b-%Y")),
                           report_rows(m, days, qty_mode), days)
    if not wb.sheetnames:
        wb.create_sheet("No data")
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def trip_report_tab(path, dr, locs, vendors, df):
    st.markdown("Supply time, then each trip (one gate entry), then items by day for "
                "one month. Days come from the supply date, not the gate-entry date; a "
                "gate entry on another day shows as (+1d) or (-1d).")
    months = sorted(df["Nag_date"].dt.to_period("M").unique())
    c = st.columns(2)
    month = c[0].selectbox("Month", months, index=len(months) - 1, key="rep_month",
                           format_func=lambda p: p.strftime("%B %Y"))
    qty_mode = c[1].radio("Quantity shown", REPORT_QTY, horizontal=True, key="rep_qty")
    rows, days = report_month(path, tuple(dr), tuple(locs), tuple(vendors), str(month),
                              qty_mode)
    k = st.columns(3)
    k[0].metric("Supply times", len({r["supply"] for r in rows}))
    k[1].metric("Trips (max per slot)", len({(r["supply"], r["trip"]) for r in rows}))
    st.caption("Trip cell: from = earliest gate entry of that trip in the month, median "
               "= typical, P90 = 90% of entries were in by then; (+1d) means after midnight.")
    k[2].metric("Item rows", sum(r["kind"] == "item" for r in rows))
    if rows:
        st.markdown(report_html(rows, days), unsafe_allow_html=True)
    else:
        st.info("No rows for this month with the current filters.")
    if st.button("Prepare trip report Excel (one tab per month)", key="rep_btn"):
        with st.spinner("Building Excel for all months…"):
            st.session_state["rep_xl"] = report_excel(path, tuple(dr), tuple(locs),
                                                       tuple(vendors), qty_mode)
    if "rep_xl" in st.session_state:
        st.download_button("Download trip report", st.session_state["rep_xl"],
                           file_name="nagare_trip_report.xlsx",
                           mime="application/vnd.openxmlformats-officedocument."
                                "spreadsheetml.sheet")


def make_plan_view(slots, aw, bays, keys, bay_cols, drop_by=False):
    """Trips to plan + gate-entry window (clock times) + bays, per `keys`."""
    if slots.empty:
        return pd.DataFrame()
    v = slots.merge(aw, on=keys, how="left")
    for col, src in (("Gate entry from", "Early edge (min)"),
                     ("P90 gate entry", "P90 (min)"),
                     ("Gate entry by", "Late edge (min)")):
        v[col] = [slot_clock(a, b) if pd.notna(b) else ""
                  for a, b in zip(v["Supply_Time"], v[src])]
    v = v.merge(bays, on=keys, how="left")
    cols = ["Plan for", "Typical", "Min", "Max seen", "Days run", "Gate entry from",
            "P90 gate entry"] + ([] if drop_by else ["Gate entry by"]) + bay_cols
    v = v[keys + cols]
    return v.rename(columns={"Plan for": "Trips to plan"})


def day_bays(df: pd.DataFrame, day) -> pd.DataFrame:
    """Bays per schedule line for one day (only computed for the day shown)."""
    x = df[df["Nag_date"].dt.date == day]
    k = ["Supply_Time", "Schedule_no", "Item"]
    return (x[k + ["unloading_loc"]].drop_duplicates()
              .sort_values(k + ["unloading_loc"]).groupby(k)["unloading_loc"]
              .agg(lambda v: ", ".join(map(str, v))).rename("Bays").reset_index())


def build_excel(R, wd_label, include_lines) -> bytes:
    sheets = {"Plan": R["plan_view"], "Plan arrivals by hour": R["p_hour"],
              "Plan by vendor": R["plan_view_v"], "Plan materials": R["p_mats"], "Trips per slot": R["table"],
              "Arrival window": R["aw"], "Materials per slot": R["mats"]}
    if len(R["bay_table"]):
        sheets["Bay visits"] = R["bay_table"]
    sheets["Daily"] = R["d"].assign(Nag_date=R["d"]["Nag_date"].dt.date)
    if include_lines:
        sheets["Line detail"] = R["lines"].assign(Nag_date=R["lines"]["Nag_date"].dt.date)
    return to_excel(sheets)


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
        # One form: nothing recomputes until Apply is pressed
        with st.form("filters"):
            dr = st.date_input("Date range (Nag_date)", value=(dmin, dmax),
                               min_value=dmin, max_value=dmax)
            locs = st.multiselect("Unloading location",
                                  sorted(base["unloading_loc"].unique()))
            vendors = st.multiselect("Vendor", sorted(base["Vendor_code"].unique()))
            split_loc = st.checkbox(
                "Also show bay (unloading location) breakdown", value=False,
                help="One gate entry can unload at several bays. Trips are always "
                     "counted once per gate entry; this only adds a bay-visits table.")
            lo, plan_q = st.select_slider(
                "Min case / Plan-for percentile",
                options=[0.05, 0.10, 0.20, 0.50, 0.75, 0.80, 0.85, 0.90, 0.95],
                value=(0.10, 0.85),
                help="Plan-for = trips you cover on this share of days")
            win = st.select_slider("Arrival window percentiles",
                                   options=[0.05, 0.10, 0.20, 0.80, 0.90, 0.95],
                                   value=(0.10, 0.90))
            bucket = st.select_slider("Gate-entry bucket (minutes)",
                                      options=[30, 60, 120], value=60)
            core = st.slider("Core item = seen on at least this % of the slot's days",
                             10, 100, 50, step=5)
            plan_days = st.multiselect(
                "Plan for weekday(s)", WEEKDAYS,
                help="Plan tab uses only history from these weekdays. Empty = all days.")
            st.form_submit_button("Apply", type="primary")
        if len(dr) != 2:
            st.info("Pick an end date, then Apply.")
            st.stop()

    wd = tuple(WEEKDAYS.index(w) for w in plan_days)
    R = analyse(path, tuple(dr), tuple(locs), tuple(vendors), split_loc, lo, plan_q,
                win, bucket, core, wd)
    df = R["df"]
    if df.empty:
        st.info("No data for these filters.")
        st.stop()
    slot_keys = ["Supply_Time"]
    n_days, trips, lines = R["n_days"], R["trips"], R["lines"]

    st.caption(f"{df['Nag_date'].min():%d-%b-%Y} to {df['Nag_date'].max():%d-%b-%Y} "
               f"({n_days} days). A trip is one gate entry per day + supply time (a truck "
               "coming back later counts again). Supply time is the scheduled "
               "slot; gate entry is when the truck really came.")
    if n_days < 150:
        st.warning("Less than ~5 months of data in range: seasonality and "
                   "month-end effects are unreliable.")

    tp, tw, tb, t1, t2, t4, t5, tr, t3 = st.tabs(
        ["PLAN", "Window planner", "Bay schedule", "Trips per supply time",
         "Gate entry vs supply time", "Materials per slot", "Day detail", "Trip report",
         "What drives trips up"])

    with tr:
        trip_report_tab(path, dr, locs, vendors, df)

    with tw:
        window_planner(R, df)

    with tb:
        bay_schedule_tab(R, df)

    with tp:
        who = "all days" if not wd else "/".join(plan_days)
        daily_tot = R["daily_tot"]
        c1, c2, c3 = st.columns(3)
        c1.metric(f"Trips on a typical day ({who})", int(round(daily_tot.median())))
        c2.metric(f"Trips to plan (P{int(plan_q*100)} of days)",
                  math.ceil(daily_tot.quantile(plan_q)))
        c3.metric("Busiest day seen", int(daily_tot.max()))
        st.subheader("1. Trips to plan, and when they actually arrive")
        st.dataframe(R["plan_view"].sort_values(slot_keys), hide_index=True,
                     width="stretch")
        st.caption("Trips to plan = trucks covered on that share of days the slot ran. "
                   "Gate entry columns are clock times from real gate-entry history "
                   "relative to the slot; (+1d) means after midnight. Slot totals "
                   "overstate the day total because slots rarely all peak together.")
        st.subheader("1b. The same, per vendor")
        pv = R["plan_view_v"]
        vsel = st.multiselect("Vendor", sorted(pv["Vendor_code"].astype(str).unique()),
                              key="plan_v")
        pv = pv[pv["Vendor_code"].astype(str).isin(vsel)] if vsel else pv
        st.dataframe(pv.sort_values(["Supply_Time", "Vendor_code"]), hide_index=True,
                     width="stretch")
        st.caption("Trucks and gate-entry window for each vendor within each slot. A vendor "
                   "with few trucks has a thin history, so treat its window loosely.")
        st.subheader("2. Trucks to expect at the gate by clock time")
        p_hour = R["p_hour"]
        st.dataframe(p_hour[p_hour["Max seen"] > 0], hide_index=True, width="stretch")
        st.subheader("3. Materials to expect")
        only_core = st.checkbox("Core items only", value=True)
        p_mats = R["p_mats"]
        pm = p_mats[p_mats["Core item"] == "yes"] if only_core else p_mats
        pvs = st.multiselect("Vendor", sorted(pm["Vendor_code"].astype(str).unique()),
                             key="mat_v")
        pm = pm[pm["Vendor_code"].astype(str).isin(pvs)] if pvs else pm
        st.dataframe(pm.drop(columns=["Core item"]), hide_index=True, width="stretch")
        st.caption("Came in parts % = share of days the item arrived on more than one "
                   "truck. Last split seen is received/planned per truck in gate "
                   "order, e.g. 10/15 5/15.")

    with t1:
        st.dataframe(R["table"].sort_values(slot_keys), hide_index=True, width="stretch")
        st.caption(f"Min = P{int(lo*100)}, Plan for = P{int(plan_q*100)} of trips on "
                   "days the slot actually ran. Check 'Runs on % of days' before "
                   "staffing a slot that only runs occasionally.")
        if split_loc:
            st.subheader("Bay visits per slot")
            st.metric("Average bays visited per gate entry", f"{R['bays_per_trip']:.1f}")
            st.dataframe(R["bay_table"].sort_values(R["bay_keys"]), hide_index=True,
                         width="stretch")
            st.caption("A bay visit is one gate entry unloading at that bay. One truck "
                       "can visit several bays, so these do NOT add up to trips; use "
                       "them to size bay and unloading capacity, not truck count.")
        st.subheader("Median trips by weekday")
        st.dataframe(R["wk"], hide_index=True, width="stretch")

    with t2:
        slots_all = sorted(trips["Supply_Time"].unique())
        pick_s = st.selectbox("Supply time to plot", ["All slots"] + slots_all,
                              index=0 if len(slots_all) <= 4 else 1)
        chosen = slots_all if pick_s == "All slots" else [pick_s]
        if len(chosen) > len(SERIES):
            st.info(f"More than {len(SERIES)} slots: pick one to plot.")
        else:
            chart, meds = gate_scatter(trips, chosen)
            if chart is None:
                st.info("No gate entries to plot.")
            else:
                st.altair_chart(chart, width="stretch")
                st.caption("Each dot is one gate entry. Solid line = median entry "
                           "time, dashed grey line = supply time. "
                           + "  ".join(f"{r.Supply_Time}: median {r._4} (n={r.n})"
                                       for r in meds.itertuples()))
        st.dataframe(R["aw"].sort_values(slot_keys), hide_index=True, width="stretch")
        st.caption("Minutes relative to supply time (negative = early), first gate "
                   f"entry of each truck. Arrivals beyond {MAX_EARLY_H}h early or "
                   f"{MAX_LATE_H}h late are dropped as likely mismatches.")
        st.subheader("Trucks per gate-entry bucket (all days, zero days included)")
        by_hour = R["by_hour"]
        st.dataframe(by_hour, hide_index=True, width="stretch")
        if not by_hour.empty:
            st.bar_chart(by_hour.set_index("Gate entry from")["Typical trucks"])

    with t4:
        mats = R["mats"]
        pick = st.selectbox("Supply time", ["All"] + sorted(mats["Supply_Time"].unique()))
        view = mats if pick == "All" else mats[mats["Supply_Time"] == pick]
        mv = st.multiselect("Vendor", sorted(view["Vendor_code"].astype(str).unique()),
                            key="mats_v")
        view = view[view["Vendor_code"].astype(str).isin(mv)] if mv else view
        if pick != "All":
            st.metric("Core items expected", int((view["Core item"] == "yes").sum()))
        st.dataframe(view, hide_index=True, width="stretch")
        st.caption("Qty = Received_qty summed per day, on days the item came in that "
                   "slot. Typical parts = trucks carrying the item on a normal day; "
                   "one truck can carry several items, so don't add across items.")

    with t5:
        days = sorted(lines["Nag_date"].dt.date.unique())
        day = st.selectbox("Day", days, index=len(days) - 1)
        dl = lines[lines["Nag_date"].dt.date == day].merge(
            day_bays(df, day), on=["Supply_Time", "Schedule_no", "Item"], how="left")
        dl = dl.sort_values(["Supply_Time", "first_entry"]).rename(columns={
            "Supply_Time": "Supply time", "planned": "Planned", "received": "Received",
            "trucks": "Trucks", "first_entry": "First gate entry",
            "last_entry": "Last gate entry", "parts": "Parts", "vendor": "Vendor"})
        dl = dl.drop(columns=["Nag_date", "Schedule_no"])
        dl = dl[["Vendor"] + [c for c in dl.columns if c != "Vendor"]]
        st.dataframe(dl, hide_index=True, width="stretch")
        st.caption("Parts = received/planned per truck, in gate-entry order.")

    with t3:
        d = R["d"]
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
    inc = st.checkbox("Include every schedule line in the Excel (slow on big data)",
                      value=False)
    if st.button("Prepare Excel"):
        with st.spinner("Writing Excel…"):
            st.session_state["xl"] = build_excel(R, None, inc)
    if "xl" in st.session_state:
        st.download_button(
            "Download Excel", st.session_state["xl"],
            file_name=f"trip_demand_{df['Nag_date'].max():%Y%m%d}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


if __name__ == "__main__":
    main()
