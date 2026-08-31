import os
import json
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import urlparse, parse_qs

import requests
import pyotp
import pandas as pd
import numpy as np
import streamlit as st
from SmartApi import SmartConnect

# ============================================================
# SETTINGS
# ============================================================
IST = ZoneInfo("Asia/Kolkata")
ST_PERIOD = 20
ST_MULTIPLIER = 2.0

# SAFETY: False = no real orders; True = real Angel One orders.
LIVE_TRADING = True
LOTS = 1
ORDER_TYPE = "MARKET"
PRODUCT_TYPE = "INTRADAY"
REFRESH_SECONDS = 20

BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "nifty_live_state.json"
INSTRUMENT_FILE = BASE_DIR / "OpenAPIScripMaster.json"
INSTRUMENT_MASTER_URL = (
    "https://margincalculator.angelone.in/"
    "OpenAPI_File/files/OpenAPIScripMaster.json"
)
KNOWN_NIFTY_TOKEN = "99926000"

ANGEL_API_KEY = os.getenv("ANGEL_API_KEY", "").strip()
ANGEL_CLIENT_ID = os.getenv("ANGEL_CLIENT_ID", "").strip()
ANGEL_PASSWORD = os.getenv("ANGEL_PASSWORD", "").strip()
ANGEL_TOTP_SECRET = os.getenv("ANGEL_TOTP_SECRET", "").strip()

st.set_page_config(
    page_title="NIFTY Live Supertrend Dashboard",
    page_icon="📈",
    layout="wide",
)

# ============================================================
# SESSION STATE
# ============================================================
defaults = {
    "running": False,
    "api": None,
    "instruments": None,
    "login_status": "NOT CONNECTED",
    "last_error": "",
    "last_message": "Ready",
    "spot": None,
    "st5": None,
    "st15": None,
    "st4h": None,
    "signal": "WAIT",
    "signal_time": None,
    "option_symbol": None,
    "option_token": None,
    "option_expiry": None,
    "option_strike": None,
    "option_lot_size": None,
    "option_ltp": None,
    "last_order_id": None,
    "last_update": None,
}
for k, v in defaults.items():
    if k not in st.session_state:
        st.session_state[k] = v

# ============================================================
# HELPERS
# ============================================================
def now_ist():
    return datetime.now(IST)

def get_totp_secret(raw):
    if not raw:
        return ""
    value = raw.strip()
    if value.lower().startswith("otpauth://"):
        parsed = urlparse(value)
        values = parse_qs(parsed.query).get("secret", [])
        if not values:
            raise ValueError("No secret= found in otpauth URI")
        value = values[0]
    return (
        value.replace(" ", "")
        .replace("\t", "")
        .replace("\r", "")
        .replace("\n", "")
        .upper()
        .strip()
    )

def generate_totp():
    secret = get_totp_secret(ANGEL_TOTP_SECRET)
    if not secret:
        raise ValueError("ANGEL_TOTP_SECRET is empty")
    return pyotp.TOTP(secret).now()

def credentials_ok():
    missing = []
    for name, value in {
        "ANGEL_API_KEY": ANGEL_API_KEY,
        "ANGEL_CLIENT_ID": ANGEL_CLIENT_ID,
        "ANGEL_PASSWORD": ANGEL_PASSWORD,
        "ANGEL_TOTP_SECRET": ANGEL_TOTP_SECRET,
    }.items():
        if not value:
            missing.append(name)
    if missing:
        raise ValueError("Missing: " + ", ".join(missing))
    generate_totp()
    return True

def angel_login():
    credentials_ok()
    api = SmartConnect(api_key=ANGEL_API_KEY)
    response = api.generateSession(
        ANGEL_CLIENT_ID,
        ANGEL_PASSWORD,
        generate_totp(),
    )
    if not response or response.get("status") is not True:
        msg = response.get("message") if response else "No response"
        code = response.get("errorcode") if response else ""
        raise RuntimeError(f"Angel login failed: {msg} {code}".strip())
    st.session_state.api = api
    st.session_state.login_status = "CONNECTED"
    return api

def ensure_login():
    api = st.session_state.api
    if api is None:
        api = angel_login()
    return api

# ============================================================
# INSTRUMENT MASTER
# ============================================================
def download_instrument_master(force=False):
    if INSTRUMENT_FILE.exists() and not force:
        age = datetime.now().timestamp() - INSTRUMENT_FILE.stat().st_mtime
        if age < 86400:
            try:
                data = json.loads(INSTRUMENT_FILE.read_text(encoding="utf-8"))
                if isinstance(data, list) and data:
                    return data
            except Exception:
                pass

    r = requests.get(
        INSTRUMENT_MASTER_URL,
        timeout=120,
        headers={"User-Agent": "Mozilla/5.0"},
    )
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, list) or not data:
        raise ValueError("Instrument master is empty/invalid")
    INSTRUMENT_FILE.write_text(json.dumps(data), encoding="utf-8")
    return data

# ============================================================
# CANDLES
# ============================================================
def get_nifty_5m_candles(days=10):
    api = ensure_login()
    now = now_ist()
    start = now - timedelta(days=days)

    params = {
        "exchange": "NSE",
        "symboltoken": KNOWN_NIFTY_TOKEN,
        "interval": "FIVE_MINUTE",
        "fromdate": start.strftime("%Y-%m-%d %H:%M"),
        "todate": now.strftime("%Y-%m-%d %H:%M"),
    }

    response = api.getCandleData(params)
    if not response or response.get("status") is not True:
        msg = response.get("message") if response else "No response"
        code = response.get("errorcode") if response else ""
        raise RuntimeError(f"Candle API failed: {msg} {code}".strip())

    rows = response.get("data") or []
    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(
        rows,
        columns=["timestamp", "open", "high", "low", "close", "volume"],
    )
    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")

    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df = (
        df.dropna(subset=["timestamp", "open", "high", "low", "close"])
        .set_index("timestamp")
        .sort_index()
    )

    if df.index.tz is None:
        df.index = df.index.tz_localize(IST)
    else:
        df.index = df.index.tz_convert(IST)

    # Angel candle timestamps are start-times. Keep completed bars only.
    cutoff = pd.Timestamp(now) - pd.Timedelta(minutes=5)
    return df[df.index <= cutoff]

# ============================================================
# SUPERTREND
# ============================================================
def supertrend(df, period=20, multiplier=2.0):
    df = df.copy()
    if df.empty or len(df) < period + 2:
        return df

    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)
    prev_close = close.shift(1)

    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)

    atr = tr.ewm(
        alpha=1 / period,
        adjust=False,
        min_periods=period,
    ).mean()

    hl2 = (high + low) / 2.0
    upper_basic = hl2 + multiplier * atr
    lower_basic = hl2 - multiplier * atr

    upper = pd.Series(np.nan, index=df.index, dtype=float)
    lower = pd.Series(np.nan, index=df.index, dtype=float)
    direction = pd.Series(0, index=df.index, dtype=int)
    st_line = pd.Series(np.nan, index=df.index, dtype=float)

    for i in range(len(df)):
        if i == 0:
            upper.iloc[i] = upper_basic.iloc[i]
            lower.iloc[i] = lower_basic.iloc[i]
            continue

        if pd.isna(upper_basic.iloc[i]) or pd.isna(lower_basic.iloc[i]):
            continue

        prev_upper = upper.iloc[i - 1]
        prev_lower = lower.iloc[i - 1]

        if pd.isna(prev_upper) or upper_basic.iloc[i] < prev_upper or close.iloc[i - 1] > prev_upper:
            upper.iloc[i] = upper_basic.iloc[i]
        else:
            upper.iloc[i] = prev_upper

        if pd.isna(prev_lower) or lower_basic.iloc[i] > prev_lower or close.iloc[i - 1] < prev_lower:
            lower.iloc[i] = lower_basic.iloc[i]
        else:
            lower.iloc[i] = prev_lower

        prev_dir = int(direction.iloc[i - 1])

        if prev_dir == 0:
            if close.iloc[i] >= lower.iloc[i]:
                direction.iloc[i] = 1
                st_line.iloc[i] = lower.iloc[i]
            else:
                direction.iloc[i] = -1
                st_line.iloc[i] = upper.iloc[i]
        elif prev_dir == 1:
            if close.iloc[i] < lower.iloc[i]:
                direction.iloc[i] = -1
                st_line.iloc[i] = upper.iloc[i]
            else:
                direction.iloc[i] = 1
                st_line.iloc[i] = lower.iloc[i]
        else:
            if close.iloc[i] > upper.iloc[i]:
                direction.iloc[i] = 1
                st_line.iloc[i] = lower.iloc[i]
            else:
                direction.iloc[i] = -1
                st_line.iloc[i] = upper.iloc[i]

    df["ATR"] = atr
    df["Supertrend"] = st_line
    df["ST_DIRECTION"] = direction
    return df

def resample_ohlcv(df, rule):
    if df.empty:
        return pd.DataFrame()

    out = df.resample(
        rule,
        origin="start_day",
        offset="9h15min",
        label="right",
        closed="left",
    ).agg(
        {
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum",
        }
    )
    out = out.dropna(subset=["open", "high", "low", "close"])

    # Only fully closed resampled bars.
    now = pd.Timestamp(now_ist())
    return out[out.index <= now]

def build_timeframes(df5):
    st5 = supertrend(df5, ST_PERIOD, ST_MULTIPLIER)
    st15 = supertrend(resample_ohlcv(df5, "15min"), ST_PERIOD, ST_MULTIPLIER)
    st4h = supertrend(resample_ohlcv(df5, "4h"), ST_PERIOD, ST_MULTIPLIER)
    return st5, st15, st4h

# ============================================================
# SIGNALS
# ============================================================
def current_signal(df5, df15, df4h):
    v5 = df5.dropna(subset=["Supertrend"])
    v15 = df15.dropna(subset=["Supertrend"])
    v4h = df4h.dropna(subset=["Supertrend"])

    if len(v5) < 2 or v15.empty or v4h.empty:
        return None

    prev5 = v5.iloc[-2]
    last5 = v5.iloc[-1]
    last15 = v15.iloc[-1]
    last4h = v4h.iloc[-1]

    dprev = int(prev5["ST_DIRECTION"])
    d5 = int(last5["ST_DIRECTION"])
    d15 = int(last15["ST_DIRECTION"])
    d4h = int(last4h["ST_DIRECTION"])

    buy_ce = dprev == -1 and d5 == 1 and d15 == 1 and d4h == 1
    buy_pe = dprev == 1 and d5 == -1 and d15 == -1 and d4h == -1

    action = "WAIT"
    option_type = None
    if buy_ce:
        action = "BUY CE"
        option_type = "CE"
    elif buy_pe:
        action = "BUY PE"
        option_type = "PE"

    return {
        "time": v5.index[-1],
        "spot": float(last5["close"]),
        "5m": d5,
        "15m": d15,
        "4h": d4h,
        "action": action,
        "option_type": option_type,
    }

# ============================================================
# OPTION SELECTION
# ============================================================
def parse_expiry(value):
    text = str(value or "").strip().upper()
    for fmt in ("%d%b%Y", "%d-%b-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    return None

def normalized_strike(value):
    try:
        strike = float(value)
    except (TypeError, ValueError):
        return None
    if strike > 100000:
        strike /= 100.0
    return strike

def select_atm_nifty_option(instruments, spot, option_type):
    today = now_ist().date()
    candidates = []

    for item in instruments:
        exchange = str(item.get("exch_seg", "")).upper().strip()
        inst_type = str(item.get("instrumenttype", "")).upper().strip()
        name = str(item.get("name", "")).upper().strip()
        symbol = str(item.get("symbol", "")).upper().strip()

        if exchange != "NFO" or inst_type != "OPTIDX":
            continue
        if name not in ("NIFTY", "NIFTY 50"):
            continue
        if not symbol.endswith(option_type.upper()):
            continue

        expiry = parse_expiry(item.get("expiry"))
        if expiry is None or expiry < today:
            continue

        strike = normalized_strike(item.get("strike"))
        if strike is None or strike <= 0:
            continue

        try:
            lot_size = int(float(item.get("lotsize", 0)))
        except (TypeError, ValueError):
            lot_size = 0

        if lot_size <= 0:
            continue

        candidates.append(
            {
                "symbol": item.get("symbol"),
                "token": str(item.get("token")),
                "expiry": expiry,
                "strike": strike,
                "lot_size": lot_size,
            }
        )

    if not candidates:
        return None

    nearest_expiry = min(x["expiry"] for x in candidates)
    same_expiry = [x for x in candidates if x["expiry"] == nearest_expiry]
    return min(same_expiry, key=lambda x: abs(x["strike"] - float(spot)))

# ============================================================
# LTP / ORDER
# ============================================================
def get_ltp(exchange, symbol, token):
    api = ensure_login()
    response = api.ltpData(exchange, symbol, token)
    if not response or response.get("status") is not True:
        return None
    value = (response.get("data") or {}).get("ltp")
    return float(value) if value is not None else None

def place_market_buy(option):
    quantity = int(option["lot_size"]) * int(LOTS)
    params = {
        "variety": "NORMAL",
        "tradingsymbol": option["symbol"],
        "symboltoken": option["token"],
        "transactiontype": "BUY",
        "exchange": "NFO",
        "ordertype": ORDER_TYPE,
        "producttype": PRODUCT_TYPE,
        "duration": "DAY",
        "price": "0",
        "squareoff": "0",
        "stoploss": "0",
        "quantity": str(quantity),
    }

    if not LIVE_TRADING:
        return "DRY-RUN"

    api = ensure_login()
    if hasattr(api, "placeOrderFullResponse"):
        response = api.placeOrderFullResponse(params)
        if isinstance(response, dict):
            if response.get("status") is not True:
                raise RuntimeError(
                    f"Order rejected: {response.get('message')} {response.get('errorcode')}"
                )
            data = response.get("data") or {}
            return data.get("orderid") or data.get("uniqueorderid")
        return response

    return api.placeOrder(params)

# ============================================================
# PERSISTENT DUPLICATE PROTECTION
# ============================================================
def load_state():
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}

def save_state(state):
    STATE_FILE.write_text(
        json.dumps(state, indent=2, default=str),
        encoding="utf-8",
    )

def market_is_open(now=None):
    now = now or now_ist()
    if now.weekday() >= 5:
        return False
    start = now.replace(hour=9, minute=15, second=0, microsecond=0)
    end = now.replace(hour=15, minute=30, second=0, microsecond=0)
    return start <= now <= end

# ============================================================
# LIVE CYCLE
# ============================================================
def run_live_cycle():
    st.session_state.last_error = ""

    if st.session_state.instruments is None:
        st.session_state.instruments = download_instrument_master()

    df5raw = get_nifty_5m_candles(days=10)
    if df5raw.empty:
        raise RuntimeError("No NIFTY 5-minute candle data received")

    st5, st15, st4h = build_timeframes(df5raw)
    sig = current_signal(st5, st15, st4h)
    if sig is None:
        raise RuntimeError("Not enough data to calculate all Supertrend values")

    st.session_state.spot = sig["spot"]
    st.session_state.st5 = sig["5m"]
    st.session_state.st15 = sig["15m"]
    st.session_state.st4h = sig["4h"]
    st.session_state.signal = sig["action"]
    st.session_state.signal_time = str(sig["time"])
    st.session_state.last_update = now_ist().strftime("%Y-%m-%d %H:%M:%S")

    # Do nothing unless a fresh directional flip signal exists.
    if sig["option_type"] is None:
        st.session_state.last_message = "Waiting for fresh 5m Supertrend flip"
        return

    state = load_state()
    signal_key = f"{sig['time'].isoformat()}|{sig['action']}"

    if state.get("last_signal") == signal_key:
        st.session_state.last_message = "Signal already processed; duplicate order blocked"
        return

    option = select_atm_nifty_option(
        st.session_state.instruments,
        sig["spot"],
        sig["option_type"],
    )
    if option is None:
        raise RuntimeError(f"ATM NIFTY {sig['option_type']} not found")

    ltp = get_ltp("NFO", option["symbol"], option["token"])

    st.session_state.option_symbol = option["symbol"]
    st.session_state.option_token = option["token"]
    st.session_state.option_expiry = str(option["expiry"])
    st.session_state.option_strike = option["strike"]
    st.session_state.option_lot_size = option["lot_size"]
    st.session_state.option_ltp = ltp

    order_id = place_market_buy(option)
    st.session_state.last_order_id = order_id

    state.update(
        {
            "last_signal": signal_key,
            "last_signal_candle": sig["time"].isoformat(),
            "last_action": sig["action"],
            "last_option": option["symbol"],
            "last_order_id": order_id,
            "last_updated": now_ist().isoformat(),
        }
    )
    save_state(state)

    if LIVE_TRADING:
        st.session_state.last_message = f"Order sent: {sig['action']} {option['symbol']}"
    else:
        st.session_state.last_message = f"DRY RUN: {sig['action']} {option['symbol']}"

# ============================================================
# SIDEBAR
# ============================================================
st.sidebar.title("Trading Control")
st.sidebar.write(f"Supertrend: **{ST_PERIOD},{ST_MULTIPLIER:g}**")
st.sidebar.write(f"Lots: **{LOTS}**")
st.sidebar.write(f"Refresh: **{REFRESH_SECONDS}s**")

if LIVE_TRADING:
    st.sidebar.error("LIVE TRADING = ON")
else:
    st.sidebar.success("DRY RUN = ON")

if st.sidebar.button("Login Angel One", use_container_width=True):
    try:
        angel_login()
        st.session_state.last_error = ""
        st.session_state.last_message = "Angel One login successful"
    except Exception as exc:
        st.session_state.login_status = "NOT CONNECTED"
        st.session_state.last_error = str(exc)

cstart, cstop = st.sidebar.columns(2)
if cstart.button("▶ START", use_container_width=True):
    st.session_state.running = True
if cstop.button("■ STOP", use_container_width=True):
    st.session_state.running = False

if st.sidebar.button("Refresh Instrument Master", use_container_width=True):
    try:
        st.session_state.instruments = download_instrument_master(force=True)
        st.session_state.last_message = "Instrument master refreshed"
    except Exception as exc:
        st.session_state.last_error = str(exc)

# ============================================================
# DASHBOARD
# ============================================================
st.title("📈 NIFTY LIVE SUPERTREND 20,2")
st.caption("5m flip + 15m confirmation + 4h confirmation | ATM CE/PE")

top1, top2, top3, top4 = st.columns(4)
top1.metric("Angel One", st.session_state.login_status)
top2.metric("Engine", "RUNNING" if st.session_state.running else "STOPPED")
top3.metric("Market", "OPEN" if market_is_open() else "CLOSED")
top4.metric(
    "NIFTY Spot",
    f"{st.session_state.spot:.2f}" if st.session_state.spot is not None else "-",
)

def dir_text(v):
    if v == 1:
        return "🟢 GREEN"
    if v == -1:
        return "🔴 RED"
    return "-"

st.subheader("Supertrend Status")
a, b, c = st.columns(3)
a.metric("5 Minute", dir_text(st.session_state.st5))
b.metric("15 Minute", dir_text(st.session_state.st15))
c.metric("4 Hour", dir_text(st.session_state.st4h))

st.subheader("Signal")
signal = st.session_state.signal
if signal == "BUY CE":
    st.success("🟢 BUY ATM CE")
elif signal == "BUY PE":
    st.error("🔴 BUY ATM PE")
else:
    st.info("⚪ WAIT")

st.write("Signal candle:", st.session_state.signal_time or "-")

st.subheader("Selected Option")
o1, o2, o3, o4, o5 = st.columns(5)
o1.metric("Symbol", st.session_state.option_symbol or "-")
o2.metric("Strike", st.session_state.option_strike or "-")
o3.metric("Expiry", st.session_state.option_expiry or "-")
o4.metric(
    "LTP",
    f"₹{st.session_state.option_ltp:.2f}"
    if st.session_state.option_ltp is not None
    else "-",
)
qty = (st.session_state.option_lot_size or 0) * LOTS
o5.metric("Quantity", qty or "-")

st.subheader("Order / Runtime")
r1, r2, r3 = st.columns(3)
r1.metric("Order ID", st.session_state.last_order_id or "-")
r2.metric("Mode", "LIVE" if LIVE_TRADING else "DRY RUN")
r3.metric("Last Update", st.session_state.last_update or "-")

if st.session_state.last_message:
    st.info(st.session_state.last_message)
if st.session_state.last_error:
    st.error(st.session_state.last_error)

st.caption(
    "Bearish signal means BUY ATM PE. This code does not short-sell naked options. "
    "Keep LIVE_TRADING=False until data, signals, expiry, strike, lot size and quantity are verified."
)

# ============================================================
# AUTO-LIVE FRAGMENT
# ============================================================
run_every = REFRESH_SECONDS if st.session_state.running else None

@st.fragment(run_every=run_every)
def live_engine():
    if not st.session_state.running:
        st.caption("Live engine stopped.")
        return

    try:
        if not market_is_open():
            st.info("Market closed — no order evaluation.")
            return

        run_live_cycle()

        st.write(
            "Live:",
            st.session_state.last_update or "-",
            "| Spot:",
            st.session_state.spot or "-",
            "| 5m:",
            dir_text(st.session_state.st5),
            "| 15m:",
            dir_text(st.session_state.st15),
            "| 4h:",
            dir_text(st.session_state.st4h),
            "| Signal:",
            st.session_state.signal,
        )
        if st.session_state.last_error:
            st.error(st.session_state.last_error)

    except Exception as exc:
        st.session_state.last_error = f"{type(exc).__name__}: {exc}"
        st.error(st.session_state.last_error)

live_engine()
