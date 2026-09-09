# ============================================================
# dashboard.py
# NIFTY AUTOMATIC BUY CE ONLY - ANGEL ONE SMARTAPI
#
# Strategy
#   5-minute Supertrend (20, 2.0) FLIPS GREEN
#       +
#   15-minute Supertrend (20, 2.0) is GREEN
#       +
#   4-hour Supertrend (20, 2.0) is GREEN
#       =>
#   AUTOMATIC BUY ATM NIFTY CE
#
# Important:
#   - NO manual BUY/SELL buttons.
#   - PAPER_TRADING=True is the safe default: no broker order is sent.
#   - Set PAPER_TRADING=False only after credentials/data/option selection
#     have been verified.
#   - The dashboard automatically detects a NEW CLOSED 5-minute candle.
#   - A signal is acted on only once per candle.
#   - The new paper/live order is added to Order Book automatically.
# ============================================================

import json
import os
import time
from datetime import datetime, timedelta, time as dt_time
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pyotp
import requests
import streamlit as st
from SmartApi import SmartConnect


# ============================================================
# PAGE CONFIG
# ============================================================

st.set_page_config(
    page_title="NIFTY Automatic BUY CE",
    page_icon="📈",
    layout="wide",
)


# ============================================================
# CONFIG
# ============================================================

IST = ZoneInfo("Asia/Kolkata")

ANGEL_API_KEY = os.getenv("ANGEL_API_KEY", "")
ANGEL_CLIENT_ID = os.getenv("ANGEL_CLIENT_ID", "")
ANGEL_PASSWORD = os.getenv("ANGEL_PASSWORD", "")
ANGEL_TOTP_SECRET = os.getenv("ANGEL_TOTP_SECRET", "")

# SAFE DEFAULT. No real broker order is sent.
PAPER_TRADING = os.getenv("PAPER_TRADING", "false").strip().lower() in (
    "1", "true", "yes", "y", "on"
)

# Strategy
ST_PERIOD = 20
ST_MULTIPLIER = 2.0

# NIFTY index token
NIFTY_TOKEN = "99926000"
NIFTY_SYMBOL = "NIFTY"

# NFO
EXCHANGE = "NFO"
PRODUCT_TYPE = "INTRADAY"
ORDER_TYPE = "MARKET"
DURATION = "DAY"

# Number of lots. Lot size is taken from the scrip master.
LOTS = int(os.getenv("NIFTY_LOTS", "1"))

# Polling
REFRESH_SECONDS = int(os.getenv("REFRESH_SECONDS", "10"))

# Trading window
ENTRY_START = dt_time(9, 20)
ENTRY_END = dt_time(15, 15)
FORCE_EXIT_TIME = dt_time(15, 20)

# Local files
STATE_FILE = Path("nifty_ce_state.json")
INSTRUMENT_CACHE = Path("OpenAPIScripMaster.json")

# Angel One public scrip-master URL
SCRIP_MASTER_URL = (
    "https://margincalculator.angelone.in/"
    "OpenAPI_File/files/OpenAPIScripMaster.json"
)


# ============================================================
# SESSION STATE
# ============================================================

DEFAULTS = {
    "api": None,
    "login_status": "NOT CONNECTED",
    "last_error": "",
    "last_message": "Ready",
    "spot": None,
    "signal": "WAIT",
    "signal_time": None,
    "st5": None,
    "st15": None,
    "st4h": None,
    "st5_flip": False,
    "option_symbol": None,
    "option_token": None,
    "option_expiry": None,
    "option_strike": None,
    "option_lot_size": None,
    "option_ltp": None,
    "last_order_id": None,
    "last_order_time": None,
    "order_book": [],
    "last_processed_candle": None,
    "last_signal_candle": None,
    "in_position": False,
    "position": None,
    "instruments": None,
    "last_refresh": None,
}

for key, value in DEFAULTS.items():
    if key not in st.session_state:
        st.session_state[key] = value


# ============================================================
# GENERAL HELPERS
# ============================================================

def now_ist():
    return datetime.now(IST)


def clean_secret(raw):
    """Accept either a plain base32 secret or an otpauth:// URI."""
    raw = (raw or "").strip()

    if raw.startswith("otpauth://"):
        try:
            from urllib.parse import parse_qs, urlparse

            parsed = urlparse(raw)
            secret = parse_qs(parsed.query).get("secret", [None])[0]
            if secret:
                return secret.replace(" ", "").upper()
        except Exception:
            pass

    return raw.replace(" ", "").replace("-", "").upper()


def credentials_ok():
    missing = []

    if not ANGEL_API_KEY:
        missing.append("ANGEL_API_KEY")
    if not ANGEL_CLIENT_CODE:
        missing.append("ANGEL_CLIENT_ID")
    if not ANGEL_PASSWORD:
        missing.append("ANGEL_PASSWORD")
    if not ANGEL_TOTP_SECRET:
        missing.append("ANGEL_TOTP_SECRET")

    return missing


def safe_float(value):
    try:
        return float(value)
    except Exception:
        return None


def json_safe(value):
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return float(value)
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.isoformat()
    return value


# ============================================================
# STATE FILE
# ============================================================

def load_state():
    if not STATE_FILE.exists():
        return

    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))

        # Do not blindly restore the API object.
        for key in (
            "last_order_id",
            "last_order_time",
            "last_processed_candle",
            "last_signal_candle",
            "in_position",
            "position",
        ):
            if key in data:
                st.session_state[key] = data[key]

    except Exception as exc:
        st.session_state["last_error"] = f"State load failed: {exc}"


def save_state():
    data = {
        "last_order_id": st.session_state.get("last_order_id"),
        "last_order_time": st.session_state.get("last_order_time"),
        "last_processed_candle": st.session_state.get("last_processed_candle"),
        "last_signal_candle": st.session_state.get("last_signal_candle"),
        "in_position": st.session_state.get("in_position", False),
        "position": st.session_state.get("position"),
    }

    try:
        STATE_FILE.write_text(
            json.dumps(data, indent=2, default=json_safe),
            encoding="utf-8",
        )
    except Exception as exc:
        st.session_state["last_error"] = f"State save failed: {exc}"


# ============================================================
# ANGEL ONE LOGIN
# ============================================================

def angel_login():
    if st.session_state.get("api") is not None:
        return st.session_state["api"]

    missing = credentials_ok()
    if missing:
        raise RuntimeError(
            "Missing credentials: " + ", ".join(missing)
        )

    secret = clean_secret(TOTP_SECRET)

    try:
        totp = pyotp.TOTP(secret).now()
    except Exception as exc:
        raise RuntimeError(
            "Invalid ANGEL_TOTP_SECRET. Use the base32 secret from "
            f"Angel One or the otpauth URI. Details: {exc}"
        )

    smart_api = SmartConnect(api_key=API_KEY)

    response = smart_api.generateSession(
        CLIENT_CODE,
        PIN,
        totp,
    )

    if not isinstance(response, dict) or not response.get("status"):
        raise RuntimeError(
            "Angel One login failed: " + str(response)
        )

    st.session_state["api"] = smart_api
    st.session_state["login_status"] = "CONNECTED"
    st.session_state["last_error"] = ""
    st.session_state["last_message"] = "Angel One login successful"

    return smart_api


# ============================================================
# NIFTY SPOT
# ============================================================

def get_nifty_ltp(api):
    response = api.ltpData(
        "NSE",
        NIFTY_SYMBOL,
        NIFTY_TOKEN,
    )

    if not isinstance(response, dict) or not response.get("status"):
        raise RuntimeError(
            "NIFTY LTP failed: " + str(response)
        )

    data = response.get("data") or {}
    ltp = safe_float(data.get("ltp"))

    if ltp is None:
        raise RuntimeError(
            "NIFTY LTP response did not contain a valid ltp: "
            + str(response)
        )

    return ltp


# ============================================================
# CANDLE DATA
# ============================================================

def get_nifty_candles(api, days=7):
    end = now_ist()
    start = end - timedelta(days=days)

    params = {
        "exchange": "NSE",
        "symboltoken": NIFTY_TOKEN,
        "interval": "FIVE_MINUTE",
        "fromdate": start.strftime("%Y-%m-%d %H:%M"),
        "todate": end.strftime("%Y-%m-%d %H:%M"),
    }

    response = api.getCandleData(params)

    if not isinstance(response, dict) or not response.get("status"):
        raise RuntimeError(
            "Candle API failed: " + str(response)
        )

    rows = response.get("data") or []

    if not rows:
        raise RuntimeError("Candle API returned no NIFTY candles.")

    df = pd.DataFrame(
        rows,
        columns=[
            "datetime",
            "open",
            "high",
            "low",
            "close",
            "volume",
        ],
    )

    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    # SmartAPI timestamps are commonly returned as ISO strings.
    ts = pd.to_datetime(df["datetime"], errors="coerce")

    # Convert to IST safely.
    if getattr(ts.dt, "tz", None) is None:
        ts = ts.dt.tz_localize(IST)
    else:
        ts = ts.dt.tz_convert(IST)

    df["datetime"] = ts

    df = (
        df.dropna(subset=["datetime", "open", "high", "low", "close"])
        .sort_values("datetime")
        .drop_duplicates("datetime")
        .reset_index(drop=True)
    )

    return df


# ============================================================
# SUPERTREND
# ============================================================

def calculate_supertrend(
    df,
    period=ST_PERIOD,
    multiplier=ST_MULTIPLIER,
):
    df = df.copy()

    if len(df) < period + 5:
        raise RuntimeError(
            f"Not enough candles for Supertrend {period},{multiplier}. "
            f"Received {len(df)}."
        )

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

    # Wilder-style ATR.
    atr = tr.ewm(
        alpha=1 / period,
        adjust=False,
        min_periods=period,
    ).mean()

    hl2 = (high + low) / 2.0

    basic_upper = hl2 + multiplier * atr
    basic_lower = hl2 - multiplier * atr

    final_upper = basic_upper.copy()
    final_lower = basic_lower.copy()

    for i in range(1, len(df)):
        if (
            pd.isna(final_upper.iloc[i - 1])
            or basic_upper.iloc[i] < final_upper.iloc[i - 1]
            or close.iloc[i - 1] > final_upper.iloc[i - 1]
        ):
            final_upper.iloc[i] = basic_upper.iloc[i]
        else:
            final_upper.iloc[i] = final_upper.iloc[i - 1]

        if (
            pd.isna(final_lower.iloc[i - 1])
            or basic_lower.iloc[i] > final_lower.iloc[i - 1]
            or close.iloc[i - 1] < final_lower.iloc[i - 1]
        ):
            final_lower.iloc[i] = basic_lower.iloc[i]
        else:
            final_lower.iloc[i] = final_lower.iloc[i - 1]

    direction = pd.Series(
        index=df.index,
        dtype="int64",
    )

    supertrend = pd.Series(
        index=df.index,
        dtype="float64",
    )

    first_valid = atr.first_valid_index()

    if first_valid is None:
        raise RuntimeError("ATR could not be calculated.")

    first_i = df.index.get_loc(first_valid)

    direction.iloc[:first_i] = np.nan
    supertrend.iloc[:first_i] = np.nan

    direction.iloc[first_i] = 1
    supertrend.iloc[first_i] = final_lower.iloc[first_i]

    for i in range(first_i + 1, len(df)):
        prev_st = supertrend.iloc[i - 1]

        if pd.isna(prev_st):
            direction.iloc[i] = 1
            supertrend.iloc[i] = final_lower.iloc[i]
            continue

        if prev_st == final_upper.iloc[i - 1]:
            if close.iloc[i] <= final_upper.iloc[i]:
                supertrend.iloc[i] = final_upper.iloc[i]
                direction.iloc[i] = -1
            else:
                supertrend.iloc[i] = final_lower.iloc[i]
                direction.iloc[i] = 1
        else:
            if close.iloc[i] >= final_lower.iloc[i]:
                supertrend.iloc[i] = final_lower.iloc[i]
                direction.iloc[i] = 1
            else:
                supertrend.iloc[i] = final_upper.iloc[i]
                direction.iloc[i] = -1

    df["atr"] = atr
    df["final_upper"] = final_upper
    df["final_lower"] = final_lower
    df["supertrend"] = supertrend
    df["direction"] = direction.astype("Int64")

    df["green"] = df["direction"] == 1
    df["red"] = df["direction"] == -1

    previous_direction = df["direction"].shift(1)

    df["flip_green"] = (
        (df["direction"] == 1)
        & (previous_direction == -1)
    )

    df["flip_red"] = (
        (df["direction"] == -1)
        & (previous_direction == 1)
    )

    return df


# ============================================================
# TIMEFRAME RESAMPLING
# ============================================================

def resample_ohlcv(df, rule):
    x = df.copy()

    if x.empty:
        return x

    x = x.set_index("datetime").sort_index()

    result = (
        x.resample(
            rule,
            origin="start_day",
            offset="9h15min",
            label="right",
            closed="left",
        )
        .agg(
            {
                "open": "first",
                "high": "max",
                "low": "min",
                "close": "last",
                "volume": "sum",
            }
        )
        .dropna(subset=["open", "high", "low", "close"])
        .reset_index()
    )

    return result


def build_timeframes(df5):
    st5 = calculate_supertrend(df5)

    df15 = resample_ohlcv(df5, "15min")
    df4h = resample_ohlcv(df5, "4h")

    st15 = calculate_supertrend(df15)
    st4h = calculate_supertrend(df4h)

    return st5, st15, st4h


# ============================================================
# CLOSED CANDLE / SIGNAL
# ============================================================

def closed_rows(st5, st15, st4h):
    """
    SmartAPI can return the currently-forming 5m candle as the last row.
    Therefore the strategy uses the LAST CLOSED candle = [-2].
    """

    if len(st5) < 3:
        raise RuntimeError("Not enough 5-minute candles.")

    # 5m: use second-last row.
    row5 = st5.iloc[-2]

    # Higher timeframe values are aligned by candle close.
    t5 = row5["datetime"]

    valid15 = st15[st15["datetime"] <= t5]
    valid4h = st4h[st4h["datetime"] <= t5]

    if len(valid15) < 2:
        raise RuntimeError("Not enough completed 15-minute candles.")

    if len(valid4h) < 2:
        raise RuntimeError("Not enough completed 4-hour candles.")

    row15 = valid15.iloc[-1]
    row4h = valid4h.iloc[-1]

    return row5, row15, row4h


def get_signal(st5, st15, st4h):
    row5, row15, row4h = closed_rows(st5, st15, st4h)

    green_flip_5m = bool(row5["flip_green"])
    green_15m = bool(row15["green"])
    green_4h = bool(row4h["green"])

    if green_flip_5m and green_15m and green_4h:
        signal = "BUY CE"
    else:
        signal = "WAIT"

    return {
        "signal": signal,
        "candle_time": row5["datetime"],
        "close": float(row5["close"]),
        "st5": float(row5["supertrend"]) if pd.notna(row5["supertrend"]) else None,
        "st15": float(row15["supertrend"]) if pd.notna(row15["supertrend"]) else None,
        "st4h": float(row4h["supertrend"]) if pd.notna(row4h["supertrend"]) else None,
        "flip_green": green_flip_5m,
        "green_15": green_15m,
        "green_4h": green_4h,
    }


# ============================================================
# INSTRUMENT MASTER
# ============================================================

@st.cache_data(ttl=3600, show_spinner=False)
def download_scrip_master():
    response = requests.get(
        SCRIP_MASTER_URL,
        timeout=30,
    )
    response.raise_for_status()

    data = response.json()

    if not isinstance(data, list) or not data:
        raise RuntimeError("Angel One scrip master is empty/invalid.")

    return data


def load_instruments():
    if st.session_state.get("instruments") is not None:
        return st.session_state["instruments"]

    try:
        data = download_scrip_master()
    except Exception as exc:
        # Use local cache if available.
        if INSTRUMENT_CACHE.exists():
            data = json.loads(
                INSTRUMENT_CACHE.read_text(encoding="utf-8")
            )
        else:
            raise RuntimeError(
                "Could not download Angel One scrip master and no local "
                f"cache exists. Error: {exc}"
            )

    try:
        INSTRUMENT_CACHE.write_text(
            json.dumps(data),
            encoding="utf-8",
        )
    except Exception:
        pass

    st.session_state["instruments"] = data
    return data


# ============================================================
# OPTION SELECTION
# ============================================================

def parse_expiry(value):
    if not value:
        return None

    value = str(value).strip().upper()

    for fmt in (
        "%d%b%Y",
        "%d%b%y",
        "%Y-%m-%d",
        "%d-%b-%Y",
        "%d/%m/%Y",
    ):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue

    return None


def normalize_strike(raw):
    try:
        value = float(raw)

        # Some scrip masters use strike * 100.
        if value > 100000:
            value = value / 100.0

        return value
    except Exception:
        return None


def get_atm_strike(spot, step=50):
    return int(round(float(spot) / step) * step)


def select_atm_nifty_ce(instruments, spot):
    """
    Select nearest-expiry NIFTY CE at the ATM strike.
    The scrip master supplies the exact trading symbol, token and lot size.
    """

    if not instruments:
        raise RuntimeError("Scrip master is empty.")

    atm = get_atm_strike(spot)

    candidates = []

    for item in instruments:
        if not isinstance(item, dict):
            continue

        exch_seg = str(
            item.get("exch_seg", "")
        ).upper()

        symbol = str(
            item.get("symbol", "")
        ).upper()

        name = str(
            item.get("name", "")
        ).upper()

        instrument_type = str(
            item.get("instrumenttype", "")
        ).upper()

        option_type = str(
            item.get("symbol", "")
        ).upper()

        # NFO NIFTY index options only.
        if exch_seg != "NFO":
            continue

        if "NIFTY" not in name and not symbol.startswith("NIFTY"):
            continue

        if not symbol.endswith("CE"):
            continue

        if instrument_type and instrument_type not in (
            "OPTIDX",
            "OPTSTK",
        ):
            # Most NIFTY option rows are OPTIDX.
            continue

        strike = normalize_strike(item.get("strike"))

        if strike is None:
            continue

        if abs(strike - atm) > 0.01:
            continue

        expiry_raw = item.get("expiry")
        expiry = parse_expiry(expiry_raw)

        if expiry is None:
            continue

        if expiry < now_ist().date():
            continue

        token = str(item.get("token", "")).strip()

        if not token:
            continue

        lot_raw = item.get("lotsize")
        try:
            lot_size = int(float(lot_raw))
        except Exception:
            lot_size = None

        candidates.append(
            {
                "symbol": symbol,
                "token": token,
                "expiry": expiry,
                "expiry_raw": str(expiry_raw),
                "strike": int(round(strike)),
                "lot_size": lot_size,
            }
        )

    if not candidates:
        raise RuntimeError(
            f"No ATM NIFTY CE found for strike {atm}. "
            "Refresh the scrip master and verify market expiry."
        )

    candidates.sort(
        key=lambda x: (
            x["expiry"],
            abs(x["strike"] - atm),
        )
    )

    return candidates[0]


# ============================================================
# OPTION LTP
# ============================================================

def get_option_ltp(api, option):
    response = api.ltpData(
        EXCHANGE,
        option["symbol"],
        option["token"],
    )

    if not isinstance(response, dict) or not response.get("status"):
        raise RuntimeError(
            "Option LTP failed: " + str(response)
        )

    ltp = safe_float(
        (response.get("data") or {}).get("ltp")
    )

    if ltp is None:
        raise RuntimeError(
            "Option LTP response has no valid ltp."
        )

    return ltp


# ============================================================
# ORDER BOOK
# ============================================================

def add_local_order(order):
    st.session_state["order_book"].insert(
        0,
        order,
    )

    # Keep a manageable local list.
    st.session_state["order_book"] = (
        st.session_state["order_book"][:50]
    )


def place_buy_ce(api, option):
    lot_size = option.get("lot_size")

    if not lot_size or lot_size <= 0:
        raise RuntimeError(
            f"Invalid lot size from scrip master: {lot_size}"
        )

    quantity = LOTS * lot_size

    order_params = {
        "variety": "NORMAL",
        "tradingsymbol": option["symbol"],
        "symboltoken": option["token"],
        "transactiontype": "BUY",
        "exchange": "NFO",
        "ordertype": ORDER_TYPE,
        "producttype": PRODUCT_TYPE,
        "duration": DURATION,
        "price": "0",
        "quantity": str(quantity),
    }

    timestamp = now_ist().strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    if PAPER_TRADING:
        order_id = "PAPER-" + now_ist().strftime(
            "%Y%m%d%H%M%S%f"
        )

        order = {
            "time": timestamp,
            "order_id": order_id,
            "mode": "PAPER",
            "status": "PAPER ORDER",
            "side": "BUY",
            "symbol": option["symbol"],
            "token": option["token"],
            "expiry": option["expiry_raw"],
            "strike": option["strike"],
            "quantity": quantity,
            "price": "MARKET",
        }

        add_local_order(order)

        return order_id, order

    response = api.placeOrder(order_params)

    # SmartAPI versions differ in return format.
    if isinstance(response, dict):
        if not response.get("status"):
            raise RuntimeError(
                "Angel One order failed: " + str(response)
            )

        data = response.get("data") or {}

        order_id = (
            data.get("orderid")
            or data.get("orderId")
            or response.get("orderid")
        )
    else:
        order_id = str(response)

    if not order_id:
        order_id = "LIVE-UNKNOWN"

    order = {
        "time": timestamp,
        "order_id": order_id,
        "mode": "LIVE",
        "status": "SUBMITTED",
        "side": "BUY",
        "symbol": option["symbol"],
        "token": option["token"],
        "expiry": option["expiry_raw"],
        "strike": option["strike"],
        "quantity": quantity,
        "price": "MARKET",
    }

    add_local_order(order)

    return order_id, order


def fetch_broker_order_book(api):
    """
    Best-effort broker order book.
    Local order records are still retained so a newly generated
    paper order immediately appears in the dashboard.
    """
    try:
        response = api.orderBook()

        if not isinstance(response, dict):
            return []

        if not response.get("status"):
            return []

        data = response.get("data") or []

        if not isinstance(data, list):
            return []

        rows = []

        for item in data:
            rows.append(
                {
                    "time": item.get("updatetime")
                    or item.get("orderdate")
                    or "",
                    "order_id": item.get("orderid")
                    or item.get("orderId")
                    or "",
                    "mode": "LIVE",
                    "status": item.get("status", ""),
                    "side": item.get("transactiontype", ""),
                    "symbol": item.get("tradingsymbol", ""),
                    "token": item.get("symboltoken", ""),
                    "expiry": "",
                    "strike": "",
                    "quantity": item.get("quantity", ""),
                    "price": item.get("price", ""),
                }
            )

        return rows

    except Exception:
        return []


# ============================================================
# AUTOMATION
# ============================================================

def run_automation():
    api = angel_login()

    if not market_is_open():
        st.session_state["signal"] = "WAIT"
        st.session_state["last_message"] = (
            "Market is outside the automatic entry window."
        )
        return

    df5 = get_nifty_candles(api, days=7)

    st5, st15, st4h = build_timeframes(df5)

    info = get_signal(
        st5,
        st15,
        st4h,
    )

    st.session_state["signal"] = info["signal"]
    st.session_state["signal_time"] = (
        info["candle_time"].isoformat()
    )
    st.session_state["st5"] = info["st5"]
    st.session_state["st15"] = info["st15"]
    st.session_state["st4h"] = info["st4h"]
    st.session_state["st5_flip"] = info["flip_green"]

    # Spot can be read independently from candle close.
    spot = get_nifty_ltp(api)
    st.session_state["spot"] = spot

    candle_key = info["candle_time"].isoformat()

    # Do not process the same closed candle repeatedly.
    already_processed = (
        st.session_state.get("last_processed_candle")
        == candle_key
    )

    if already_processed:
        st.session_state["last_message"] = (
            f"Waiting for next 5-minute candle. "
            f"Last checked: {candle_key}"
        )
        return

    # Mark the candle as processed before attempting the order.
    # This prevents duplicate orders during repeated Streamlit reruns.
    st.session_state["last_processed_candle"] = candle_key

    if info["signal"] != "BUY CE":
        st.session_state["last_message"] = (
            "No BUY CE signal on the new closed candle."
        )
        save_state()
        return

    # Only one open automated CE position at a time.
    if st.session_state.get("in_position"):
        st.session_state["last_message"] = (
            "BUY CE signal detected, but an automated CE position "
            "is already marked open."
        )
        save_state()
        return

    # Extra signal-candle duplicate protection.
    if (
        st.session_state.get("last_signal_candle")
        == candle_key
    ):
        return

    instruments = load_instruments()

    option = select_atm_nifty_ce(
        instruments,
        spot,
    )

    option_ltp = get_option_ltp(
        api,
        option,
    )

    st.session_state["option_symbol"] = option["symbol"]
    st.session_state["option_token"] = option["token"]
    st.session_state["option_expiry"] = option["expiry_raw"]
    st.session_state["option_strike"] = option["strike"]
    st.session_state["option_lot_size"] = option["lot_size"]
    st.session_state["option_ltp"] = option_ltp

    order_id, order = place_buy_ce(
        api,
        option,
    )

    st.session_state["last_order_id"] = order_id
    st.session_state["last_order_time"] = now_ist().isoformat()
    st.session_state["last_signal_candle"] = candle_key
    st.session_state["in_position"] = True
    st.session_state["position"] = {
        "symbol": option["symbol"],
        "token": option["token"],
        "strike": option["strike"],
        "expiry": option["expiry_raw"],
        "quantity": LOTS * option["lot_size"],
        "entry_ltp": option_ltp,
        "signal_candle": candle_key,
        "mode": "PAPER" if PAPER_TRADING else "LIVE",
    }

    st.session_state["last_message"] = (
        f"AUTOMATIC BUY CE completed: {option['symbol']} "
        f"| order={order_id}"
    )

    save_state()


def market_is_open():
    now = now_ist()

    if now.weekday() >= 5:
        return False

    return ENTRY_START <= now.time() <= ENTRY_END


def force_close_at_end(api):
    """
    This version does not automatically SELL the CE.
    The requested strategy is BUY CE only. Therefore the dashboard
    stops creating new entries after the entry window.

    A broker position remains a broker position until separately managed.
    This is intentionally not converted into an automatic SELL rule.
    """
    return


# ============================================================
# DASHBOARD UI
# ============================================================

load_state()

st.title("📈 NIFTY Automatic BUY CE Dashboard")

st.caption(
    "5m Supertrend 20,2 GREEN FLIP + 15m GREEN + 4H GREEN "
    "→ automatic ATM NIFTY CE BUY"
)

# Safety banner
if PAPER_TRADING:
    st.warning(
        "PAPER TRADING MODE — no real Angel One order will be sent."
    )
else:
    st.error(
        "LIVE TRADING MODE — matching BUY CE signals can send real "
        "Angel One orders."
    )


# ============================================================
# AUTOMATION RUN
# ============================================================

automation_error = None

try:
    run_automation()
except Exception as exc:
    automation_error = str(exc)
    st.session_state["last_error"] = automation_error
    st.session_state["last_message"] = (
        "Automation error. See diagnostics below."
    )


# ============================================================
# HEADER METRICS
# ============================================================

c1, c2, c3, c4, c5 = st.columns(5)

with c1:
    st.metric(
        "Angel One",
        st.session_state["login_status"],
    )

with c2:
    spot = st.session_state.get("spot")
    st.metric(
        "NIFTY Spot",
        "-" if spot is None else f"{spot:,.2f}",
    )

with c3:
    st.metric(
        "Signal",
        st.session_state.get("signal", "WAIT"),
    )

with c4:
    st.metric(
        "Position",
        "OPEN" if st.session_state.get("in_position") else "NONE",
    )

with c5:
    st.metric(
        "Mode",
        "PAPER" if PAPER_TRADING else "LIVE",
    )


# ============================================================
# SIGNAL STATUS
# ============================================================

st.subheader("Signal Conditions")

s1, s2, s3, s4 = st.columns(4)

with s1:
    st.write("**5m Supertrend**")
    if st.session_state.get("st5") is not None:
        st.write(
            f"{'🟢 GREEN' if st.session_state.get('st5_flip') else '⚪ NO NEW FLIP'}"
        )
        st.caption(
            f"ST: {st.session_state['st5']:.2f}"
        )
    else:
        st.write("—")

with s2:
    st.write("**15m Supertrend**")
    if st.session_state.get("st15") is not None:
        st.write(f"ST: {st.session_state['st15']:.2f}")
    else:
        st.write("—")

with s3:
    st.write("**4H Supertrend**")
    if st.session_state.get("st4h") is not None:
        st.write(f"ST: {st.session_state['st4h']:.2f}")
    else:
        st.write("—")

with s4:
    st.write("**Trigger**")
    if st.session_state.get("signal") == "BUY CE":
        st.success("AUTOMATIC BUY CE")
    else:
        st.info("WAIT")


# ============================================================
# SELECTED OPTION
# ============================================================

st.subheader("ATM CE Selected")

o1, o2, o3, o4, o5 = st.columns(5)

with o1:
    st.write("Symbol")
    st.write(
        st.session_state.get("option_symbol") or "—"
    )

with o2:
    st.write("Expiry")
    st.write(
        st.session_state.get("option_expiry") or "—"
    )

with o3:
    st.write("Strike")
    strike = st.session_state.get("option_strike")
    st.write("—" if strike is None else str(strike))

with o4:
    st.write("Lot Size")
    lot = st.session_state.get("option_lot_size")
    st.write("—" if lot is None else str(lot))

with o5:
    st.write("CE LTP")
    ltp = st.session_state.get("option_ltp")
    st.write("—" if ltp is None else f"{ltp:,.2f}")


# ============================================================
# POSITION
# ============================================================

st.subheader("Automated Position")

position = st.session_state.get("position")

if position:
    st.json(position)
else:
    st.info("No automated CE position recorded.")


# ============================================================
# ORDER BOOK
# ============================================================

st.subheader("Order Book")

api = st.session_state.get("api")

broker_orders = []
if api is not None and not PAPER_TRADING:
    broker_orders = fetch_broker_order_book(api)

local_orders = st.session_state.get("order_book", [])

all_orders = broker_orders + local_orders

if all_orders:
    order_df = pd.DataFrame(all_orders)

    # Newest first where possible.
    if "time" in order_df.columns:
        order_df = order_df.reset_index(drop=True)

    st.dataframe(
        order_df,
        use_container_width=True,
        hide_index=True,
    )

    last_id = st.session_state.get("last_order_id")

    if last_id:
        st.success(
            f"Latest automatic order: {last_id}"
        )
else:
    st.info(
        "No automatic order has been generated yet. "
        "When the BUY CE condition becomes true, the new order "
        "will appear here automatically."
    )


# ============================================================
# STATUS / DIAGNOSTICS
# ============================================================

st.subheader("Automation Status")

st.write(
    st.session_state.get(
        "last_message",
        "Ready",
    )
)

if st.session_state.get("signal_time"):
    st.caption(
        "Last closed 5m candle checked: "
        + str(st.session_state["signal_time"])
    )

if st.session_state.get("last_order_time"):
    st.caption(
        "Last automatic order: "
        + str(st.session_state["last_order_time"])
    )

if automation_error:
    with st.expander("Error diagnostics", expanded=True):
        st.error(automation_error)

        if "Invalid API Key" in automation_error:
            st.code(
                "Check ANGEL_API_KEY. "
                "The SmartAPI API key must belong to the correct "
                "Angel One application."
            )

        if (
            "TOTP" in automation_error
            or "base32" in automation_error
            or "totp" in automation_error.lower()
        ):
            st.code(
                "Check ANGEL_TOTP_SECRET. "
                "Use the actual TOTP secret/base32 value, not the "
                "6-digit OTP shown by the authenticator."
            )


# ============================================================
# STRATEGY LOGIC EXPLANATION
# ============================================================

with st.expander("How automatic BUY CE works"):
    st.markdown(
        """
1. Angel One login is performed automatically.
2. Fresh NIFTY 5-minute candles are requested.
3. The currently forming candle is ignored.
4. Supertrend 20,2 is calculated on the last CLOSED 5-minute candle.
5. 15-minute and 4-hour Supertrend confirmations are calculated.
6. The trigger is:

   **5m GREEN FLIP + 15m GREEN + 4H GREEN**

7. When the trigger first becomes true on a new closed 5m candle:
   - NIFTY spot is read.
   - ATM strike is calculated.
   - Nearest valid NIFTY CE expiry is selected from the Angel One scrip master.
   - Exact option symbol/token/lot size are obtained.
   - CE LTP is read.
   - An automatic BUY MARKET order is created.
   - In PAPER mode, no broker order is sent.
   - In LIVE mode, the Angel One order is submitted.
   - The order is immediately added to Order Book.
8. The same 5-minute candle cannot trigger another order.
9. No manual BUY/SELL controls are used.
"""
    )


# ============================================================
# AUTO REFRESH
# ============================================================

st.caption(
    f"Dashboard auto-refresh: every {REFRESH_SECONDS} seconds"
)

time.sleep(REFRESH_SECONDS)
st.rerun()
