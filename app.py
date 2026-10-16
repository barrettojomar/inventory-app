import hmac
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

import gspread
import pandas as pd
import streamlit as st
from google.oauth2.service_account import Credentials

st.set_page_config(page_title="Inventory", page_icon=":package:", layout="centered")

TZ = ZoneInfo("Asia/Manila")
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
CUR = "₱"
COLS = {
    "Items": ["item_code", "description", "item_type", "unit", "reorder_level", "standard_cost", "part_number"],
    "Locations": ["location_code", "name", "allowed_type"],
    "Stock_Ledger": ["txn_id", "date_time", "item_code", "location", "qty", "txn_type", "reference", "notes", "user"],
    "BOM": ["finished_code", "raw_code", "qty_per_unit"],
}
NUM = {"Items": ["reorder_level", "standard_cost"], "Stock_Ledger": ["qty"], "BOM": ["qty_per_unit"]}
TYPES = ["Raw material", "Finished good", "Indirect material"]


@st.cache_resource
def book():
    creds = Credentials.from_service_account_info(dict(st.secrets["gcp_service_account"]), scopes=SCOPES)
    return gspread.authorize(creds).open_by_key(st.secrets["sheet_id"])


@st.cache_data(ttl=20, show_spinner=False)
def load(tab):
    df = pd.DataFrame(book().worksheet(tab).get_all_records())
    for c in COLS[tab]:
        if c not in df:
            df[c] = ""
    df = df[COLS[tab]].copy()
    for c in NUM.get(tab, []):
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)
    for c in df.columns:
        if c not in NUM.get(tab, []):
            df[c] = df[c].fillna("").astype(str).str.strip()
    return df


def append(tab, rows):
    book().worksheet(tab).append_rows(rows, value_input_option="USER_ENTERED")
    load.clear()


def post(entries, ref="", notes=""):
    """entries: list of (item_code, location, signed_qty, txn_type). One shared txn_id per call."""
    tid = uuid.uuid4().hex[:8].upper()
    now = datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")
    user = st.session_state.user["username"]
    append("Stock_Ledger", [[tid, now, i, l, round(float(q), 4), t, ref, notes, user] for i, l, q, t in entries])
    return tid


def login():
    if "user" in st.session_state:
        return True
    st.title("Inventory")
    u = st.text_input("Username").strip().lower()
    p = st.text_input("Password", type="password")
    if st.button("Sign in", type="primary"):
        users = st.secrets["users"]
        if u in users and hmac.compare_digest(str(users[u]["password"]), p):
            st.session_state.user = {"username": u, "name": users[u]["name"], "role": users[u]["role"]}
            st.rerun()
        st.error("Wrong username or password.")
    return False


def fmt(n):
    return f"{n:,.2f}".rstrip("0").rstrip(".")


def on_hand(led, item, loc):
    m = led[(led.item_code == item) & (led.location == loc)]
    return float(m.qty.sum())


def pick_item(items, key, types=None):
    df = items if types is None else items[items.item_type.isin(types)]
    if df.empty:
        st.warning("No items available. Ask an admin to add items first.")
        return None
    labels = {r.item_code: f"{r.item_code} - {r.description}" for r in df.itertuples()}
    return st.selectbox("Item", list(labels), format_func=labels.get, key=key)


def pick_loc(locs, key, item_type, label="Location", exclude=None):
    df = locs[locs.allowed_type == item_type]
    if exclude:
        df = df[df.location_code != exclude]
    labels = {r.location_code: f"{r.location_code} - {r.name}" for r in df.itertuples()}
    if not labels:
        st.warning(f"No location is set up for {item_type}.")
        return None
    return st.selectbox(label, list(labels), format_func=labels.get, key=key)


def done(msg):
    st.session_state.flash = msg
    st.rerun()


if not login():
    st.stop()

user = st.session_state.user
role = user["role"]
can_write = role in ("admin", "staff")
is_admin = role == "admin"

items, locs, led, bom = load("Items"), load("Locations"), load("Stock_Ledger"), load("BOM")
totals = led.groupby("item_code").qty.sum()
items["on_hand"] = items.item_code.map(totals).fillna(0)
items["low"] = (items.reorder_level > 0) & (items.on_hand <= items.reorder_level)
unit = dict(zip(items.item_code, items.unit))

top = st.columns([3, 1])
top[0].caption(f"{user['name']} ({role})")
if top[1].button("Sign out"):
    del st.session_state.user
    st.rerun()
if "flash" in st.session_state:
    st.success(st.session_state.pop("flash"))

names = ["Home", "Items", "Stock"] + (["Admin"] if is_admin else [])
tabs = st.tabs(names)

with tabs[0]:
    c = st.columns(3)
    c[0].metric("Items", len(items))
    c[1].metric("Low stock", int(items.low.sum()))
    if is_admin:
        val = (items.on_hand * items.standard_cost).sum()
        c[2].metric("Stock value", f"{CUR}{val:,.0f}")
    st.subheader("Needs reorder")
    low = items[items.low][["item_code", "description", "on_hand", "reorder_level", "unit"]]
    if low.empty:
        st.caption("Nothing is below its reorder level.")
    else:
        st.dataframe(low, hide_index=True, use_container_width=True)
    st.subheader("Recent activity")
    recent = led.tail(10).iloc[::-1][["date_time", "item_code", "location", "qty", "txn_type", "user"]]
    st.dataframe(recent, hide_index=True, use_container_width=True)

with tabs[1]:
    q = st.text_input("Search items", placeholder="Code, name, or part number")
    view = items
    if q:
        mask = items[["item_code", "description", "part_number"]].apply(
            lambda s: s.str.contains(q, case=False, na=False)).any(axis=1)
        view = items[mask]
    cols = ["item_code", "description", "item_type", "on_hand", "unit", "reorder_level"]
    st.dataframe(view[cols], hide_index=True, use_container_width=True)
    st.subheader("Item details")
    code = pick_item(view if len(view) else items, "detail_item")
    if code:
        it = items[items.item_code == code].iloc[0]
        st.caption(f"{it.item_type} | unit: {it.unit} | reorder level: {fmt(it.reorder_level)}"
                   + (f" | cost: {CUR}{fmt(it.standard_cost)}" if is_admin else ""))
        st.write("Stock by location")
        rows = [{"location": r.location_code, "name": r.name, "on_hand": on_hand(led, code, r.location_code)}
                for r in locs[locs.allowed_type == it.item_type].itertuples()]
        st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
        st.write("Recent movements")
        mv = led[led.item_code == code].tail(20).iloc[::-1]
        st.dataframe(mv[["date_time", "location", "qty", "txn_type", "reference", "user"]],
                     hide_index=True, use_container_width=True)

with tabs[2]:
    if not can_write:
        st.info("Your role is view only.")
    else:
        kind = st.selectbox("What are you recording?",
                            ["Receive stock", "Issue stock", "Transfer", "Count / adjust", "Production output"])

        if kind in ("Receive stock", "Issue stock"):
            code = pick_item(items, "mv_item")
            if code:
                it = items[items.item_code == code].iloc[0]
                loc = pick_loc(locs, "mv_loc", it.item_type)
                qty = st.number_input(f"Quantity ({it.unit})", min_value=0.0, step=1.0, key="mv_qty")
                ref = st.text_input("Reference (PO, DR, job number)", key="mv_ref")
                notes = st.text_input("Notes", key="mv_notes")
                if loc:
                    st.caption(f"On hand now: {fmt(on_hand(led, code, loc))} {it.unit}")
                if st.button("Save", type="primary", key="mv_save"):
                    sign = 1 if kind == "Receive stock" else -1
                    if qty <= 0:
                        st.error("Enter a quantity above zero.")
                    elif loc is None:
                        st.error("Choose a location.")
                    elif sign < 0 and qty > on_hand(led, code, loc):
                        st.error("That's more than the stock on hand.")
                    else:
                        t = "RECEIPT" if sign > 0 else "ISSUE"
                        tid = post([(code, loc, sign * qty, t)], ref, notes)
                        done(f"Saved {t.lower()} {tid}")

        elif kind == "Transfer":
            code = pick_item(items, "tr_item")
            if code:
                it = items[items.item_code == code].iloc[0]
                src = pick_loc(locs, "tr_from", it.item_type, "From")
                dst = pick_loc(locs, "tr_to", it.item_type, "To", exclude=src)
                qty = st.number_input(f"Quantity ({it.unit})", min_value=0.0, step=1.0, key="tr_qty")
                ref = st.text_input("Reference", key="tr_ref")
                if src:
                    st.caption(f"On hand at {src}: {fmt(on_hand(led, code, src))} {it.unit}")
                if st.button("Transfer", type="primary", key="tr_save"):
                    if qty <= 0 or not src or not dst:
                        st.error("Choose both locations and enter a quantity above zero.")
                    elif qty > on_hand(led, code, src):
                        st.error("That's more than the stock on hand.")
                    else:
                        tid = post([(code, src, -qty, "TRANSFER_OUT"), (code, dst, qty, "TRANSFER_IN")], ref)
                        done(f"Transferred. Reference {tid}")

        elif kind == "Count / adjust":
            code = pick_item(items, "adj_item")
            if code:
                it = items[items.item_code == code].iloc[0]
                loc = pick_loc(locs, "adj_loc", it.item_type)
                cur = on_hand(led, code, loc) if loc else 0
                st.caption(f"System quantity: {fmt(cur)} {it.unit}")
                counted = st.number_input("Counted quantity", min_value=0.0, step=1.0, key="adj_qty")
                reason = st.text_input("Reason (required)", placeholder="Cycle count, damaged, opening balance", key="adj_reason")
                if loc and counted != cur:
                    st.caption(f"Variance: {fmt(counted - cur)} {it.unit}")
                if st.button("Post adjustment", type="primary", key="adj_save"):
                    if not reason.strip() or not loc:
                        st.error("Enter a reason and choose a location.")
                    elif counted == cur:
                        st.error("The count matches the system. Nothing to adjust.")
                    else:
                        tid = post([(code, loc, counted - cur, "ADJUSTMENT")], notes=reason)
                        done(f"Adjustment posted {tid}")

        else:
            fg = pick_item(items, "pr_item", ["Finished good"])
            if fg:
                lines = bom[bom.finished_code == fg]
                if lines.empty:
                    st.warning("This part has no bill of materials yet. Ask an admin to add one.")
                else:
                    raw_loc = pick_loc(locs, "pr_raw", "Raw material", "Take raw material from")
                    fg_loc = pick_loc(locs, "pr_fg", "Finished good", "Put finished parts in")
                    good = st.number_input(f"Good parts made ({unit.get(fg, 'pcs')})", min_value=0.0, step=1.0, key="pr_good")
                    scrap = st.number_input("Scrap parts", min_value=0.0, step=1.0, key="pr_scrap")
                    ref = st.text_input("Job or work order number", key="pr_ref")
                    need = lines.assign(
                        needed=lines.qty_per_unit * (good + scrap),
                        on_hand=[on_hand(led, r, raw_loc) if raw_loc else 0 for r in lines.raw_code])
                    st.dataframe(need[["raw_code", "needed", "on_hand"]], hide_index=True, use_container_width=True)
                    if st.button("Post production", type="primary", key="pr_save"):
                        if good + scrap <= 0 or not raw_loc or not fg_loc:
                            st.error("Enter the quantity made and choose both locations.")
                        elif (need.needed > need.on_hand).any():
                            st.error("Not enough raw material at that location.")
                        else:
                            e = []
                            for r in lines.itertuples():
                                if good > 0:
                                    e.append((r.raw_code, raw_loc, -good * r.qty_per_unit, "PRODUCTION_CONSUME"))
                                if scrap > 0:
                                    e.append((r.raw_code, raw_loc, -scrap * r.qty_per_unit, "SCRAP"))
                            if good > 0:
                                e.append((fg, fg_loc, good, "PRODUCTION_OUTPUT"))
                            tid = post(e, ref)
                            done(f"Production posted {tid}")

if is_admin:
    with tabs[3]:
        st.subheader("Add item")
        code = st.text_input("Item code", key="ni_code").strip()
        desc = st.text_input("Description", key="ni_desc")
        typ = st.selectbox("Type", TYPES, key="ni_type")
        c = st.columns(3)
        u = c[0].text_input("Unit", value="pcs", key="ni_unit")
        ro = c[1].number_input("Reorder level", min_value=0.0, step=1.0, key="ni_ro")
        cost = c[2].number_input(f"Cost ({CUR})", min_value=0.0, step=0.5, key="ni_cost")
        pn = st.text_input("Customer part number (optional)", key="ni_pn")
        if st.button("Add item", type="primary", key="ni_save"):
            if not code or not desc:
                st.error("Item code and description are required.")
            elif code in set(items.item_code):
                st.error("That item code already exists.")
            else:
                append("Items", [[code, desc, typ, u, ro, cost, pn]])
                done(f"Added {code}")

        st.subheader("Add bill of materials line")
        fgs = items[items.item_type == "Finished good"]
        raws = items[items.item_type == "Raw material"]
        if fgs.empty or raws.empty:
            st.caption("Add at least one finished good and one raw material first.")
        else:
            f = st.selectbox("Finished part", fgs.item_code, key="bm_f")
            r = st.selectbox("Raw material used", raws.item_code, key="bm_r")
            per = st.number_input("Raw material per 1 finished part", min_value=0.0, step=0.01, format="%.4f", key="bm_q")
            if st.button("Add BOM line", key="bm_save"):
                if per <= 0:
                    st.error("Enter a quantity above zero.")
                else:
                    append("BOM", [[f, r, per]])
                    done(f"Added {r} to {f}")
