"""
Nagare Trip Report
------------------
Streamlit web page that turns nagare_data.parquet into the
Supply time -> Trip -> Item  x  Day  layout, one month at a time,
and exports an Excel workbook with one tab per month.

Install:  pip install streamlit pandas pyarrow openpyxl
Run:      streamlit run nagare_trip_report.py
"""

import html
import io
from collections import Counter

import pandas as pd
import streamlit as st
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

# ----------------------------------------------------------------------------
# Settings
# ----------------------------------------------------------------------------
DEFAULT_PATH = "nagare_data.parquet"
COLS = [
    "Vendor_code", "unloading_loc", "Item_name", "location", "Nag_date",
    "Supply_Time", "Nag_qty", "Gate_entry_qty", "Received_qty",
    "Truck_number", "Consignement_no", "Gate_entry_time",
]
QTY_OPTIONS = ["Received_qty", "Nag_qty", "Gate_entry_qty"]

st.set_page_config(page_title="Nagare Trip Report", layout="wide")


# ----------------------------------------------------------------------------
# Load + clean (cached once; never mutated afterwards)
# ----------------------------------------------------------------------------
@st.cache_resource(show_spinner="Loading parquet…")
def load(path: str) -> pd.DataFrame:
    df = pd.read_parquet(path, columns=COLS)

    df["Nag_date"] = pd.to_datetime(df["Nag_date"], format="%d-%b-%Y",
                                    errors="coerce").dt.normalize()
    df["Gate_entry_time"] = pd.to_datetime(df["Gate_entry_time"],
                                           format="%d-%b-%Y %H:%M",
                                           errors="coerce")
    df["Supply_Time"] = df["Supply_Time"].astype(str).str.strip()
    df["Item_name"] = df["Item_name"].fillna("").astype(str).str.strip()
    df["location"] = df["location"].fillna("").astype(str).str.strip()
    for c in QTY_OPTIONS:
        df[c] = pd.to_numeric(df[c], errors="coerce")

    # A trip = one truck (fallback: consignment) per Nag_date + Supply_Time
    df["truck_key"] = df["Truck_number"].astype("string").fillna(
        "CN-" + df["Consignement_no"].astype("string").fillna("NA"))

    df = df.dropna(subset=["Nag_date"])
    df["month"] = df["Nag_date"].dt.to_period("M")
    return df


def filtered(path, vendors, locs) -> pd.DataFrame:
    df = load(path)
    mask = pd.Series(True, index=df.index)
    if vendors:
        mask &= df["Vendor_code"].isin(vendors)
    if locs:
        mask &= df["unloading_loc"].isin(locs)
    return df[mask]


# ----------------------------------------------------------------------------
# Core transform: one month -> list of display rows
# ----------------------------------------------------------------------------
def assign_trips(m: pd.DataFrame) -> pd.DataFrame:
    """Number trucks Trip1, Trip2… within each Nag_date + Supply_Time,
    ordered by their gate entry time."""
    trips = (m.groupby(["Nag_date", "Supply_Time", "truck_key"])
               ["Gate_entry_time"].min().reset_index()
               .rename(columns={"Gate_entry_time": "trip_gate"}))
    trips = trips.sort_values(["Nag_date", "Supply_Time", "trip_gate"],
                              na_position="last")
    trips["trip_no"] = trips.groupby(["Nag_date", "Supply_Time"]).cumcount() + 1
    return m.merge(trips, on=["Nag_date", "Supply_Time", "truck_key"])


def fmt_gate(ts, day):
    if ts is None or pd.isna(ts):
        return ""
    s = ts.strftime("%H:%M")
    diff = (ts.normalize() - day).days
    return s if diff == 0 else f"{s} ({diff:+d}d)"   # entry on another day


def fmt_qty(v):
    if v is None or pd.isna(v):
        return None
    return int(v) if float(v).is_integer() else round(float(v), 2)


def build_rows(m: pd.DataFrame, days, qty_col):
    m = assign_trips(m)
    rows = []
    for sup in sorted(m["Supply_Time"].unique()):
        s = m[m["Supply_Time"] == sup]
        for trip in sorted(s["trip_no"].unique()):
            t = s[s["trip_no"] == trip]
            label = f"Trip{trip}"

            gate = t.groupby("Nag_date")["trip_gate"].first()
            rows.append({"supply": sup, "trip": label, "item": "Gate entry",
                         "loc": "", "kind": "gate",
                         "vals": [fmt_gate(gate.get(d), d) for d in days]})

            q = t.pivot_table(index=["Item_name", "location"],
                              columns="Nag_date", values=qty_col,
                              aggfunc="sum")
            for (item, loc), r in q.iterrows():
                rows.append({"supply": sup, "trip": label, "item": item,
                             "loc": loc, "kind": "item",
                             "vals": [fmt_qty(r.get(d)) for d in days]})
    return rows


def month_days(period):
    return list(pd.date_range(period.start_time,
                              period.end_time.normalize(), freq="D"))


@st.cache_data(show_spinner="Building month…")
def month_rows(path, month_str, vendors, locs, qty_col):
    df = filtered(path, vendors, locs)
    period = pd.Period(month_str, "M")
    m = df[df["month"] == period]
    days = month_days(period)
    return (build_rows(m, days, qty_col) if len(m) else []), days


# ----------------------------------------------------------------------------
# HTML table (merged Supply time / Trip cells)
# ----------------------------------------------------------------------------
CSS = """
<style>
.nt-wrap{overflow:auto;max-height:75vh;border:1px solid #cfd6de}
.nt{border-collapse:collapse;font-size:13px;font-variant-numeric:tabular-nums}
.nt th,.nt td{border:1px solid #cfd6de;padding:3px 8px;white-space:nowrap}
.nt thead th{position:sticky;top:0;background:#1f3a5f;color:#fff;z-index:2}
.nt td.num{text-align:right}
.nt td.grp{vertical-align:middle;text-align:center;font-weight:600;background:#eef2f6}
.nt tr.gate td{background:#fff4d6;font-weight:600;color:#7a5200}
.nt tr.gate td.lbl{font-style:italic;font-weight:500}
</style>
"""


def to_html(rows, days):
    sup_span = Counter(r["supply"] for r in rows)
    trip_span = Counter((r["supply"], r["trip"]) for r in rows)
    seen_s, seen_t = set(), set()

    head = ("<tr><th>Supply time</th><th>Trip</th><th>Items</th>"
            "<th>Location</th>"
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
            cells.append(f'<td class="grp" rowspan="{trip_span[key]}">'
                         f'{r["trip"]}</td>')
        cls = "lbl" if r["kind"] == "gate" else ""
        cells.append(f'<td class="{cls}">{html.escape(r["item"])}</td>')
        cells.append(f'<td>{html.escape(r["loc"])}</td>')
        cells += [f'<td class="num">{"" if v is None else v}</td>'
                  for v in r["vals"]]
        body.append(f'<tr class="{r["kind"]}">{"".join(cells)}</tr>')

    return (CSS + '<div class="nt-wrap"><table class="nt"><thead>' + head
            + "</thead><tbody>" + "".join(body) + "</tbody></table></div>")


# ----------------------------------------------------------------------------
# Excel export: one sheet per month
# ----------------------------------------------------------------------------
THIN = Side(style="thin", color="A6B0BB")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
HEAD_FILL = PatternFill("solid", fgColor="1F3A5F")
GRP_FILL = PatternFill("solid", fgColor="EEF2F6")
GATE_FILL = PatternFill("solid", fgColor="FFF4D6")


def write_sheet(ws, rows, days):
    header = ["Supply time", "Trip", "Items", "Location"] + \
             [d.strftime("%d-%b") for d in days]
    ws.append(header)
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = HEAD_FILL
        c.alignment = Alignment(horizontal="center")
        c.border = BORDER

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

        first_s = (i == s_start)
        first_t = (i == t_start)
        ws.append([r["supply"] if first_s else None,
                   r["trip"] if first_t else None,
                   r["item"], r["loc"]] + r["vals"])
        for c in ws[i]:
            c.border = BORDER
            if r["kind"] == "gate" and c.column > 2:
                c.fill = GATE_FILL
                c.font = Font(bold=True, color="7A5200",
                              italic=(c.column == 3))
                c.alignment = Alignment(horizontal="center")

    last = len(rows) + 1
    if rows:
        merges.append((1, s_start, last))
        merges.append((2, t_start, last))
    for col, a, b in merges:
        if b > a:
            ws.merge_cells(start_row=a, end_row=b,
                           start_column=col, end_column=col)
        cell = ws.cell(row=a, column=col)
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.font = Font(bold=True)
        cell.fill = GRP_FILL

    ws.column_dimensions["A"].width = 12
    ws.column_dimensions["B"].width = 8
    ws.column_dimensions["C"].width = 38
    ws.column_dimensions["D"].width = 11
    for j in range(5, 5 + len(days)):
        ws.column_dimensions[get_column_letter(j)].width = 8
    ws.freeze_panes = "E2"


@st.cache_data(show_spinner="Building Excel (all months)…")
def build_excel(path, vendors, locs, qty_col) -> bytes:
    df = filtered(path, vendors, locs)
    wb = Workbook()
    wb.remove(wb.active)
    for period in sorted(df["month"].unique()):
        m = df[df["month"] == period]
        days = month_days(period)
        ws = wb.create_sheet(title=period.strftime("%b-%Y"))
        write_sheet(ws, build_rows(m, days, qty_col), days)
    if not wb.sheetnames:
        wb.create_sheet("No data")
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ----------------------------------------------------------------------------
# Page
# ----------------------------------------------------------------------------
st.title("Nagare Trip Report")

with st.sidebar:
    path = st.text_input("Parquet file", DEFAULT_PATH)
    try:
        base = load(path)
    except Exception as e:
        st.error(f"Could not read {path}: {e}")
        st.stop()

    qty_col = st.radio("Quantity shown", QTY_OPTIONS, index=0)
    vendors = tuple(st.multiselect(
        "Vendor", sorted(base["Vendor_code"].dropna().astype(str).unique())))
    locs = tuple(st.multiselect(
        "Unloading location",
        sorted(base["unloading_loc"].dropna().astype(str).unique())))

    months = sorted(base["month"].unique())
    month = st.selectbox("Month", months, index=len(months) - 1,
                         format_func=lambda p: p.strftime("%B %Y"))

rows, days = month_rows(path, str(month), vendors, locs, qty_col)

c1, c2, c3 = st.columns(3)
c1.metric("Supply times", len({r["supply"] for r in rows}))
c2.metric("Trips (max per slot)",
          len({(r["supply"], r["trip"]) for r in rows}))
c3.metric("Item rows", sum(r["kind"] == "item" for r in rows))

if rows:
    st.markdown(to_html(rows, days), unsafe_allow_html=True)
else:
    st.info("No rows for this month with the current filters.")

st.divider()
if st.button("Prepare Excel (one tab per month)"):
    data = build_excel(path, vendors, locs, qty_col)
    st.download_button("Download Excel", data,
                       file_name="nagare_trip_report.xlsx",
                       mime="application/vnd.openxmlformats-officedocument."
                            "spreadsheetml.sheet")
