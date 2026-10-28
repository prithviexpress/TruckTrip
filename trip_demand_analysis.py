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

import altair as alt
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
    sup = pd.to_timedelta(df["Supply_Time"] + ":00", errors="coerce")
    df["slot_dt"] = df["Nag_date"] + sup
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
    keys = ["Nag_date", "Supply_Time", "Item", "Item_name"]
    out = (lines.groupby(keys)
                .agg(planned=("planned", "sum"), received=("received", "sum"),
                     trucks=("trucks", "sum"), first_entry=("first_entry", "min"))
                .reset_index())
    sp = lines[lines["parts"] != ""]
    pj = sp.groupby(keys)["parts"].agg("; ".join).rename("parts")
    out = out.merge(pj, on=keys, how="left")
    out["parts"] = out["parts"].fillna("")
    return out


def slot_materials(iday: pd.DataFrame, slot_keys, core_pct) -> pd.DataFrame:
    """What arrives in each slot: per slot x item, how often it shows up, how
    much it brings, and how often it comes in parts."""
    gk = slot_keys + ["Item", "Item_name"]
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
    return (out[cols].sort_values(slot_keys + ["Days seen"],
                                  ascending=[True] * len(slot_keys) + [False])
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
    """Per slot x item: vendors, and the bays it was unloaded at (% of the
    days it came). Built from de-duplicated rows, so it stays quick."""
    k = ["Supply_Time", "Item"]
    vend = (df[k + ["Vendor_code"]].drop_duplicates().sort_values(k + ["Vendor_code"])
              .groupby(k)["Vendor_code"].agg(", ".join).str.slice(0, 40)
              .rename("Vendors"))
    days = df.groupby(k)["Nag_date"].nunique().rename("n")
    at = (df.groupby(k + ["unloading_loc"])["Nag_date"].nunique()
            .rename("d").reset_index().merge(days.reset_index(), on=k))
    at["pct"] = 100 * at["d"] / at["n"]
    at = at[at["pct"] >= 10].sort_values(k + ["pct"], ascending=[True, True, False])
    at = at.groupby(k).head(5)
    at["txt"] = at["unloading_loc"].astype(str) + " " + at["pct"].round().astype(int).astype(str) + "%"
    bays = at.groupby(k)["txt"].agg(", ".join).rename("Bays (% of days)")
    return pd.concat([vend, bays], axis=1).reset_index()


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


def bay_share(df: pd.DataFrame, k: list) -> pd.DataFrame:
    """Bays used per group k, as % of its gate entries (>=10%, top 5)."""
    total = df.groupby(k)["trip_key"].nunique().rename("n").reset_index()
    at = (df.groupby(k + ["unloading_loc"])["trip_key"].nunique().rename("v")
            .reset_index().merge(total, on=k))
    at["pct"] = 100 * at["v"] / at["n"]
    at = at[at["pct"] >= 10].sort_values(k + ["pct"], ascending=[True] * len(k) + [False])
    at = at.groupby(k).head(5)
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
@st.cache_resource(show_spinner="Crunching trips, materials and bays…", max_entries=3)
def analyse(path, dr, locs, vendors, split_loc, lo, plan_q, win, bucket, core, wd):
    """All heavy tables, computed once per set of filters. The page below only
    displays them, so tab switches and dropdowns don't recompute anything."""
    df = load(path)
    df = df[(df["Nag_date"] >= pd.Timestamp(dr[0])) & (df["Nag_date"] <= pd.Timestamp(dr[1]))]
    if locs:
        df = df[df["unloading_loc"].isin(locs)]
    if vendors:
        df = df[df["Vendor_code"].isin(vendors)]
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
        info, on=["Supply_Time", "Item"], how="left")

    h_sd = sd if not wd else sd[sd["Nag_date"].dt.dayofweek.isin(wd)]
    h_iday = iday if not wd else iday[iday["Nag_date"].dt.dayofweek.isin(wd)]
    h_trips = trips if not wd else trips[trips["Nag_date"].dt.dayofweek.isin(wd)]
    h_df = df if not wd else df[df["Nag_date"].dt.dayofweek.isin(wd)]
    p_info = info if not wd else item_info(h_df)
    p_slots = trip_plan(h_sd, slot_keys, lo, plan_q)
    p_aw = arrival_window(h_trips, slot_keys, *win)
    p_mats = slot_materials(h_iday, slot_keys, core).merge(
        p_info, on=["Supply_Time", "Item"], how="left")
    p_bays = slot_bays(h_df)

    plan_view = pd.DataFrame()
    if not p_slots.empty:
        plan_view = p_slots.merge(p_aw, on=slot_keys, how="left")
        for col, src in (("Gate entry from", "Early edge (min)"),
                         ("Median gate entry", "Median (min)"),
                         ("Gate entry by", "Late edge (min)")):
            plan_view[col] = [slot_clock(a, b) if pd.notna(b) else ""
                              for a, b in zip(plan_view["Supply_Time"], plan_view[src])]
        plan_view = plan_view.merge(p_bays, on="Supply_Time", how="left")
        plan_view = plan_view[slot_keys + ["Plan for", "Typical", "Min", "Max seen",
                                           "Days run", "Gate entry from",
                                           "Median gate entry", "Gate entry by",
                                           "Bays to stop at", "Bays per truck"]]
        plan_view = plan_view.rename(columns={"Plan for": "Trips to plan"})

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
        d=daily_features(trips, plan), plan_view=plan_view, p_mats=p_mats,
        p_hour=arrival_by_bucket(df, bucket, plan_q, wd), daily_tot=daily_tot,
        bay_table=bay_table, bay_keys=bay_keys, bays_per_trip=bays_per_trip,
        vt=vendor_trips(df), vs_bays=bay_share(df, ["Vendor_code", "Supply_Time"]),
        bay_load=bay_load_stats(df))
    return R


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
    up = st.file_uploader("Critical items (CSV/Excel with an 'Item' column; optional "
                          "'Critical' Y/N column)", type=["csv", "xlsx"])
    crit = set()
    if up is not None:
        try:
            crit = read_critical(up)
            st.caption(f"{len(crit)} critical items loaded.")
        except Exception as e:
            st.error(f"Could not read critical items: {e}")

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


def day_bays(df: pd.DataFrame, day) -> pd.DataFrame:
    """Bays per schedule line for one day (only computed for the day shown)."""
    x = df[df["Nag_date"].dt.date == day]
    k = ["Supply_Time", "Schedule_no", "Item"]
    return (x[k + ["unloading_loc"]].drop_duplicates()
              .sort_values(k + ["unloading_loc"]).groupby(k)["unloading_loc"]
              .agg(lambda v: ", ".join(map(str, v))).rename("Bays").reset_index())


def build_excel(R, wd_label, include_lines) -> bytes:
    sheets = {"Plan": R["plan_view"], "Plan arrivals by hour": R["p_hour"],
              "Plan materials": R["p_mats"], "Trips per slot": R["table"],
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

    tp, tw, t1, t2, t4, t5, t3 = st.tabs(
        ["PLAN", "Window planner", "Trips per supply time", "Gate entry vs supply time",
         "Materials per slot", "Day detail", "What drives trips up"])

    with tw:
        window_planner(R, df)

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
        st.subheader("2. Trucks to expect at the gate by clock time")
        p_hour = R["p_hour"]
        st.dataframe(p_hour[p_hour["Max seen"] > 0], hide_index=True, width="stretch")
        st.subheader("3. Materials to expect")
        only_core = st.checkbox("Core items only", value=True)
        p_mats = R["p_mats"]
        pm = p_mats[p_mats["Core item"] == "yes"] if only_core else p_mats
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
        st.dataframe(dl.drop(columns=["Nag_date", "Schedule_no"]), hide_index=True,
                     width="stretch")
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
