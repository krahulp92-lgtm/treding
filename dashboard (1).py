# ============================================================
# dashboard.py
#
# NIFTY 50 AUTOMATIC BUY CE ONLY
# ANGEL ONE SMARTAPI + STREAMLIT
#
# STRATEGY
# ------------------------------------------------------------
# 5-minute Supertrend (20, 2.0) FLIPS GREEN
#        +
# 15-minute Supertrend (20, 2.0) GREEN
#        +
# 4-hour Supertrend (20, 2.0) GREEN
#        =>
# AUTOMATIC BUY ATM NIFTY CE
#
# NO MANUAL BUY / SELL BUTTONS
#
# IMPORTANT
# ------------------------------------------------------------
# PAPER_TRADING=true  -> NO REAL ORDER
# PAPER_TRADING=false -> REAL ANGEL ONE ORDER
# ============================================================


import os
import json
import time
import re
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
import pyotp
import pandas as pd
import numpy as np
import streamlit as st

from SmartApi import SmartConnect


# ============================================================
# PAGE CONFIG
# ============================================================

st.set_page_config(
    page_title="NIFTY Automatic CE Dashboard",
    page_icon="📈",
    layout="wide",
)


# ============================================================
# CONSTANTS
# ============================================================

IST = ZoneInfo("Asia/Kolkata")

ST_PERIOD = 20
ST_MULTIPLIER = 2.0

NIFTY_TOKEN = "99926000"
NIFTY_SYMBOL = "NIFTY 50"

NSE_EXCHANGE = "NSE"
NFO_EXCHANGE = "NFO"

INSTRUMENT_URL = (
    "https://margincalculator.angelone.in/"
    "OpenAPI_File/files/OpenAPIScripMaster.json"
)

STATE_FILE = Path("nifty_ce_state.json")
INSTRUMENT_FILE = Path("OpenAPIScripMaster.json")

REFRESH_SECONDS = int(os.getenv("REFRESH_SECONDS", "10"))

# SAFE DEFAULT:
# true  = paper trading
# false = real trading
PAPER_TRADING = (
    os.getenv("PAPER_TRADING", "true").strip().lower()
    in ("1", "true", "yes", "y", "on")
)


# ============================================================
# CREDENTIAL HELPERS
# ============================================================

def get_secret(name, default=""):
    """
    Read from Streamlit secrets first, then environment variables.
    """

    try:
        if name in st.secrets:
            value = st.secrets[name]

            if value is not None:
                return str(value).strip()
    except Exception:
        pass

    return str(os.getenv(name, default)).strip()


ANGEL_API_KEY = get_secret("ANGEL_API_KEY")
ANGEL_CLIENT_ID = get_secret("ANGEL_CLIENT_ID")
ANGEL_PASSWORD = get_secret("ANGEL_PASSWORD")
ANGEL_TOTP_SECRET = get_secret("ANGEL_TOTP_SECRET")


# ============================================================
# SESSION STATE
# ============================================================

DEFAULTS = {
    "api": None,
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
    "last_order_time": None,

    "last_processed_signal_candle": None,

    "orders": [],

    "last_update": None,

    "instruments": None,
    "instrument_loaded": False,

    "running": False,
}


for key, value in DEFAULTS.items():

    if key not in st.session_state:
        st.session_state[key] = value


# ============================================================
# BASIC HELPERS
# ============================================================

def now_ist():
    return datetime.now(IST)


def safe_float(value):
    try:
        if value is None:
            return None

        value = str(value).strip()

        if value == "":
            return None

        return float(value)

    except Exception:
        return None


def safe_int(value, default=0):
    try:
        return int(float(value))
    except Exception:
        return default


def normalize_expiry(value):
    """
    Convert instrument-master expiry into YYYY-MM-DD when possible.
    """

    if value is None:
        return None

    text = str(value).strip()

    if not text:
        return None

    for fmt in (
        "%d%b%Y",
        "%d%b%y",
        "%Y-%m-%d",
        "%d-%b-%Y",
        "%d-%b-%y",
    ):

        try:
            return datetime.strptime(
                text.upper(),
                fmt
            ).date()
        except Exception:
            pass

    try:
        return pd.to_datetime(text).date()
    except Exception:
        return None


def normalize_strike(value):
    """
    Angel instrument master generally stores NIFTY strike as
    2450000 for 24500 depending on contract representation.

    Normalize to actual NIFTY strike.
    """

    value = safe_float(value)

    if value is None:
        return None

    # Angel instrument master commonly stores strikes x100.
    if value >= 100000:
        value = value / 100.0

    return value


# ============================================================
# TOTP
# ============================================================

def get_totp_secret(raw):
    """
    Accept either:

    1. Base32 secret
    2. otpauth:// URI

    Never use the 6-digit OTP itself as ANGEL_TOTP_SECRET.
    """

    if not raw:
        return ""

    raw = str(raw).strip()

    # Handle otpauth:// URI
    if raw.lower().startswith("otpauth://"):

        try:
            from urllib.parse import urlparse, parse_qs

            parsed = urlparse(raw)

            query = parse_qs(parsed.query)

            secret_values = query.get("secret")

            if secret_values:
                return secret_values[0].strip().replace(" ", "")

        except Exception:
            pass

    # Remove spaces
    raw = raw.replace(" ", "")

    return raw


def validate_totp_secret(secret):

    if not secret:
        return False, "ANGEL_TOTP_SECRET is missing."

    secret = secret.upper().strip()

    # Base32 characters only
    if not re.fullmatch(r"[A-Z2-7]+=*", secret):
        return (
            False,
            "ANGEL_TOTP_SECRET must contain the Base32 secret, "
            "not the 6-digit OTP."
        )

    try:
        pyotp.TOTP(secret).now()
        return True, "TOTP secret is valid."

    except Exception as exc:
        return False, f"Invalid TOTP secret: {exc}"


# ============================================================
# ANGEL LOGIN
# ============================================================

def angel_login():

    if not ANGEL_API_KEY:
        raise RuntimeError(
            "ANGEL_API_KEY is missing."
        )

    if not ANGEL_CLIENT_ID:
        raise RuntimeError(
            "ANGEL_CLIENT_ID is missing."
        )

    if not ANGEL_PASSWORD:
        raise RuntimeError(
            "ANGEL_PASSWORD is missing."
        )

    secret = get_totp_secret(ANGEL_TOTP_SECRET)

    valid, message = validate_totp_secret(secret)

    if not valid:
        raise RuntimeError(message)

    try:

        totp = pyotp.TOTP(secret).now()

        api = SmartConnect(
            api_key=ANGEL_API_KEY
        )

        response = api.generateSession(
            ANGEL_CLIENT_ID,
            ANGEL_PASSWORD,
            totp,
        )

        if not isinstance(response, dict):
            raise RuntimeError(
                f"Invalid Angel One login response: {response}"
            )

        if not response.get("status"):

            raise RuntimeError(
                "Angel One login failed: "
                + str(response)
            )

        st.session_state["api"] = api
        st.session_state["login_status"] = "CONNECTED"

        return api

    except Exception as exc:

        st.session_state["login_status"] = "LOGIN FAILED"

        raise RuntimeError(
            f"Angel One login failed: {exc}"
        )


# ============================================================
# NIFTY LTP
# ============================================================

def get_nifty_ltp(api):

    if api is None:
        raise RuntimeError(
            "Angel One API session is not connected."
        )

    try:

        # IMPORTANT:
        # Correct symbol = NIFTY 50
        # Correct token = 99926000

        response = api.ltpData(
            "NSE",
            "NIFTY 50",
            "99926000",
        )

    except Exception as exc:

        raise RuntimeError(
            f"NIFTY LTP API exception: {exc}"
        )

    if not isinstance(response, dict):

        raise RuntimeError(
            f"NIFTY LTP returned invalid response: {response}"
        )

    if not response.get("status"):

        raise RuntimeError(
            "NIFTY LTP failed: "
            + str(response)
        )

    data = response.get("data") or {}

    ltp = safe_float(
        data.get("ltp")
    )

    if ltp is None:

        raise RuntimeError(
            "NIFTY LTP missing from response: "
            + str(response)
        )

    return ltp


# ============================================================
# INSTRUMENT MASTER
# ============================================================

def download_instrument_master():

    try:

        response = requests.get(
            INSTRUMENT_URL,
            timeout=30,
        )

        response.raise_for_status()

        data = response.json()

        if not isinstance(data, list):

            raise RuntimeError(
                "Instrument master response is not a list."
            )

        with open(
            INSTRUMENT_FILE,
            "w",
            encoding="utf-8",
        ) as f:

            json.dump(
                data,
                f,
                ensure_ascii=False,
            )

        return data

    except Exception as exc:

        # Try local cache
        if INSTRUMENT_FILE.exists():

            try:

                with open(
                    INSTRUMENT_FILE,
                    "r",
                    encoding="utf-8",
                ) as f:

                    data = json.load(f)

                if isinstance(data, list):
                    return data

            except Exception:
                pass

        raise RuntimeError(
            "Unable to download Angel One instrument master "
            f"and no usable local cache exists. Error: {exc}"
        )


def load_instruments():

    if st.session_state.get("instruments"):

        return st.session_state["instruments"]

    if INSTRUMENT_FILE.exists():

        try:

            with open(
                INSTRUMENT_FILE,
                "r",
                encoding="utf-8",
            ) as f:

                data = json.load(f)

            if isinstance(data, list) and len(data) > 0:

                st.session_state["instruments"] = data
                st.session_state["instrument_loaded"] = True

                return data

        except Exception:
            pass

    data = download_instrument_master()

    st.session_state["instruments"] = data
    st.session_state["instrument_loaded"] = True

    return data


# ============================================================
# NIFTY CANDLES
# ============================================================

def get_nifty_candles(api, days=30):

    end = now_ist()

    start = end - timedelta(
        days=days
    )

    params = {

        "exchange": "NSE",

        "symboltoken": NIFTY_TOKEN,

        "interval": "FIVE_MINUTE",

        "fromdate": start.strftime(
            "%Y-%m-%d %H:%M"
        ),

        "todate": end.strftime(
            "%Y-%m-%d %H:%M"
        ),
    }

    try:

        response = api.getCandleData(
            params
        )

    except Exception as exc:

        raise RuntimeError(
            f"Candle API exception: {exc}"
        )

    if not isinstance(response, dict):

        raise RuntimeError(
            f"Invalid candle response: {response}"
        )

    if not response.get("status"):

        raise RuntimeError(
            "Candle API failed: "
            + str(response)
        )

    rows = response.get("data")

    if not rows:

        raise RuntimeError(
            "Candle API returned no NIFTY candles."
        )

    records = []

    for row in rows:

        if len(row) < 5:
            continue

        records.append(
            {
                "datetime": row[0],
                "open": row[1],
                "high": row[2],
                "low": row[3],
                "close": row[4],
                "volume": row[5] if len(row) > 5 else 0,
            }
        )

    df = pd.DataFrame(records)

    if df.empty:

        raise RuntimeError(
            "No valid NIFTY candle records."
        )

    df["datetime"] = pd.to_datetime(
        df["datetime"],
        errors="coerce",
    )

    # SmartAPI may return timezone-naive timestamps.
    if df["datetime"].dt.tz is None:

        df["datetime"] = (
            df["datetime"]
            .dt.tz_localize(
                IST,
                ambiguous="NaT",
                nonexistent="NaT",
            )
        )

    else:

        df["datetime"] = (
            df["datetime"]
            .dt.tz_convert(IST)
        )

    for col in [
        "open",
        "high",
        "low",
        "close",
        "volume",
    ]:

        df[col] = pd.to_numeric(
            df[col],
            errors="coerce",
        )

    df = df.dropna(
        subset=[
            "datetime",
            "open",
            "high",
            "low",
            "close",
        ]
    )

    df = df.sort_values(
        "datetime"
    )

    df = df.drop_duplicates(
        "datetime",
        keep="last",
    )

    return df.reset_index(
        drop=True
    )


# ============================================================
# SUPERTREND
# ============================================================

def supertrend(
    df,
    period=20,
    multiplier=2.0,
):

    x = df.copy()

    high = x["high"].astype(float)
    low = x["low"].astype(float)
    close = x["close"].astype(float)

    previous_close = close.shift(1)

    tr1 = high - low
    tr2 = (high - previous_close).abs()
    tr3 = (low - previous_close).abs()

    tr = pd.concat(
        [tr1, tr2, tr3],
        axis=1,
    ).max(axis=1)

    # Wilder ATR
    atr = tr.ewm(
        alpha=1 / period,
        adjust=False,
        min_periods=period,
    ).mean()

    hl2 = (
        high + low
    ) / 2.0

    basic_upper = (
        hl2 + multiplier * atr
    )

    basic_lower = (
        hl2 - multiplier * atr
    )

    final_upper = basic_upper.copy()
    final_lower = basic_lower.copy()

    direction = pd.Series(
        index=x.index,
        dtype="int64",
    )

    st_value = pd.Series(
        index=x.index,
        dtype="float64",
    )

    direction.iloc[0] = 1

    st_value.iloc[0] = np.nan

    for i in range(1, len(x)):

        if (
            pd.isna(final_upper.iloc[i - 1])
            or basic_upper.iloc[i]
            < final_upper.iloc[i - 1]
            or close.iloc[i - 1]
            > final_upper.iloc[i - 1]
        ):

            final_upper.iloc[i] = (
                basic_upper.iloc[i]
            )

        else:

            final_upper.iloc[i] = (
                final_upper.iloc[i - 1]
            )

        if (
            pd.isna(final_lower.iloc[i - 1])
            or basic_lower.iloc[i]
            > final_lower.iloc[i - 1]
            or close.iloc[i - 1]
            < final_lower.iloc[i - 1]
        ):

            final_lower.iloc[i] = (
                basic_lower.iloc[i]
            )

        else:

            final_lower.iloc[i] = (
                final_lower.iloc[i - 1]
            )

        previous_direction = (
            direction.iloc[i - 1]
        )

        if pd.isna(atr.iloc[i]):

            direction.iloc[i] = (
                previous_direction
            )

            st_value.iloc[i] = np.nan

            continue

        if previous_direction == 1:

            if close.iloc[i] <= final_lower.iloc[i]:

                direction.iloc[i] = -1
                st_value.iloc[i] = (
                    final_upper.iloc[i]
                )

            else:

                direction.iloc[i] = 1
                st_value.iloc[i] = (
                    final_lower.iloc[i]
                )

        else:

            if close.iloc[i] >= final_upper.iloc[i]:

                direction.iloc[i] = 1
                st_value.iloc[i] = (
                    final_lower.iloc[i]
                )

            else:

                direction.iloc[i] = -1
                st_value.iloc[i] = (
                    final_upper.iloc[i]
                )

    x["ATR"] = atr

    x["Basic_Upper"] = basic_upper
    x["Basic_Lower"] = basic_lower

    x["Final_Upper"] = final_upper
    x["Final_Lower"] = final_lower

    x["Supertrend"] = st_value

    x["ST_Direction"] = direction

    x["ST_Green"] = (
        direction == 1
    )

    x["ST_Red"] = (
        direction == -1
    )

    x["ST_Flip_Green"] = (
        x["ST_Green"]
        & ~x["ST_Green"].shift(1).fillna(False)
    )

    x["ST_Flip_Red"] = (
        x["ST_Red"]
        & ~x["ST_Red"].shift(1).fillna(False)
    )

    return x


# ============================================================
# CALCULATE SUPERTREND
# ============================================================

def calculate_supertrend(df):

    if df is None or df.empty:

        return pd.DataFrame()

    x = df.copy()

    x["datetime"] = pd.to_datetime(
        x["datetime"],
        errors="coerce",
    )

    x = x.dropna(
        subset=["datetime"]
    )

    x = x.sort_values(
        "datetime"
    )

    x = x.drop_duplicates(
        "datetime",
        keep="last",
    )

    for col in [
        "open",
        "high",
        "low",
        "close",
        "volume",
    ]:

        x[col] = pd.to_numeric(
            x[col],
            errors="coerce",
        )

    x = x.dropna(
        subset=[
            "open",
            "high",
            "low",
            "close",
        ]
    )

    x = x.set_index(
        "datetime"
    )

    x = supertrend(
        x,
        period=ST_PERIOD,
        multiplier=ST_MULTIPLIER,
    )

    return x.reset_index()


# ============================================================
# RESAMPLE TIMEFRAMES
# ============================================================

def resample_ohlcv(df, rule):

    if df is None or df.empty:

        return pd.DataFrame()

    x = df.copy()

    x["datetime"] = pd.to_datetime(
        x["datetime"],
        errors="coerce",
    )

    x = x.dropna(
        subset=["datetime"]
    )

    x = x.set_index(
        "datetime"
    )

    result = (
        x[
            [
                "open",
                "high",
                "low",
                "close",
                "volume",
            ]
        ]
        .resample(
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
    )

    result = result.dropna(
        subset=[
            "open",
            "high",
            "low",
            "close",
        ]
    )

    return result.reset_index()


# ============================================================
# BUILD ALL TIMEFRAMES
# ============================================================

def build_timeframes(df5):

    st5 = calculate_supertrend(
        df5
    )

    df15 = resample_ohlcv(
        df5,
        "15min",
    )

    df4h = resample_ohlcv(
        df5,
        "4h",
    )

    st15 = calculate_supertrend(
        df15
    )

    st4h = calculate_supertrend(
        df4h
    )

    return st5, st15, st4h


# ============================================================
# MARKET HOURS
# ============================================================

def market_is_open():

    now = now_ist()

    if now.weekday() >= 5:
        return False

    market_start = now.replace(
        hour=9,
        minute=15,
        second=0,
        microsecond=0,
    )

    market_end = now.replace(
        hour=15,
        minute=30,
        second=0,
        microsecond=0,
    )

    return (
        market_start
        <= now
        <= market_end
    )


# ============================================================
# GET LAST CLOSED 5M CANDLE
# ============================================================

def get_last_closed_5m(df):

    if df is None or df.empty:

        return None

    now = now_ist()

    # 5-minute candle starts:
    # 09:15, 09:20, 09:25, etc.

    minutes_since_open = (
        (now.hour * 60 + now.minute)
        - (9 * 60 + 15)
    )

    if minutes_since_open < 0:
        return None

    bucket = (
        minutes_since_open // 5
    )

    candle_start = (
        now.replace(
            hour=9,
            minute=15,
            second=0,
            microsecond=0,
        )
        + timedelta(
            minutes=bucket * 5
        )
    )

    # Current candle has not closed yet.
    # Therefore latest valid candle must be
    # strictly before current candle start.

    closed = df[
        df["datetime"]
        < candle_start
    ]

    if closed.empty:

        return None

    return closed.iloc[-1]


# ============================================================
# CLOSED ROWS FOR MULTI-TIMEFRAME SIGNAL
# ============================================================

def closed_rows(
    st5,
    st15,
    st4h,
):

    if st5.empty:
        raise RuntimeError(
            "5-minute Supertrend data is empty."
        )

    row5 = get_last_closed_5m(
        st5
    )

    if row5 is None:

        raise RuntimeError(
            "No closed 5-minute candle available."
        )

    candle_time = row5["datetime"]

    rows15 = st15[
        st15["datetime"]
        <= candle_time
    ]

    rows4h = st4h[
        st4h["datetime"]
        <= candle_time
    ]

    if rows15.empty:

        raise RuntimeError(
            "No closed 15-minute candle available."
        )

    if rows4h.empty:

        raise RuntimeError(
            "No closed 4-hour candle available."
        )

    row15 = rows15.iloc[-1]
    row4h = rows4h.iloc[-1]

    return (
        row5,
        row15,
        row4h,
    )


# ============================================================
# SIGNAL
# ============================================================

def get_signal(
    st5,
    st15,
    st4h,
):

    row5, row15, row4h = closed_rows(
        st5,
        st15,
        st4h,
    )

    flip_green_5m = bool(
        row5["ST_Flip_Green"]
    )

    green_15m = bool(
        row15["ST_Green"]
    )

    green_4h = bool(
        row4h["ST_Green"]
    )

    if (
        flip_green_5m
        and green_15m
        and green_4h
    ):

        signal = "BUY CE"

    else:

        signal = "WAIT"

    return {

        "signal": signal,

        "candle_time": row5["datetime"],

        "close": safe_float(
            row5["close"]
        ),

        "st5": safe_float(
            row5["Supertrend"]
        ),

        "st15": safe_float(
            row15["Supertrend"]
        ),

        "st4h": safe_float(
            row4h["Supertrend"]
        ),

        "flip_green": flip_green_5m,

        "green_15": green_15m,

        "green_4h": green_4h,
    }


# ============================================================
# OPTION SELECTION
# ============================================================

def select_atm_nifty_ce(
    instruments,
    spot,
):

    if not instruments:

        raise RuntimeError(
            "Instrument master is empty."
        )

    if spot is None:

        raise RuntimeError(
            "NIFTY spot is unavailable."
        )

    today = now_ist().date()

    candidates = []

    for item in instruments:

        if not isinstance(item, dict):
            continue

        exchange = str(
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

        if exchange != "NFO":
            continue

        # NIFTY index options
        if name != "NIFTY":
            continue

        if not symbol.endswith("CE"):
            continue

        if instrument_type not in (
            "",
            "OPTIDX",
        ):

            continue

        expiry = normalize_expiry(
            item.get("expiry")
        )

        if expiry is None:
            continue

        if expiry < today:
            continue

        strike = normalize_strike(
            item.get("strike")
        )

        if strike is None:
            continue

        token = str(
            item.get("token", "")
        ).strip()

        if not token:
            continue

        lot_size = safe_int(
            item.get("lotsize"),
            0,
        )

        if lot_size <= 0:
            continue

        distance = abs(
            strike - spot
        )

        candidates.append(
            {
                "symbol": symbol,
                "token": token,
                "expiry": expiry,
                "strike": strike,
                "lot_size": lot_size,
                "distance": distance,
            }
        )

    if not candidates:

        raise RuntimeError(
            "No NIFTY CE contracts found in instrument master."
        )

    # Nearest expiry
    nearest_expiry = min(
        x["expiry"]
        for x in candidates
    )

    candidates = [
        x
        for x in candidates
        if x["expiry"] == nearest_expiry
    ]

    # ATM strike
    selected = min(
        candidates,
        key=lambda x: x["distance"]
    )

    return selected


# ============================================================
# OPTION LTP
# ============================================================

def get_option_ltp(
    api,
    option,
):

    if api is None:

        raise RuntimeError(
            "API is not connected."
        )

    response = api.ltpData(
        "NFO",
        option["symbol"],
        option["token"],
    )

    if not isinstance(response, dict):

        raise RuntimeError(
            f"Invalid option LTP response: {response}"
        )

    if not response.get("status"):

        raise RuntimeError(
            "Option LTP failed: "
            + str(response)
        )

    data = response.get(
        "data"
    ) or {}

    ltp = safe_float(
        data.get("ltp")
    )

    if ltp is None:

        raise RuntimeError(
            "Option LTP missing: "
            + str(response)
        )

    return ltp


# ============================================================
# STATE
# ============================================================

def load_state():

    if not STATE_FILE.exists():
        return {}

    try:

        with open(
            STATE_FILE,
            "r",
            encoding="utf-8",
        ) as f:

            data = json.load(f)

        return data if isinstance(
            data,
            dict
        ) else {}

    except Exception:

        return {}


def save_state(data):

    try:

        with open(
            STATE_FILE,
            "w",
            encoding="utf-8",
        ) as f:

            json.dump(
                data,
                f,
                indent=2,
                default=str,
            )

    except Exception as exc:

        st.warning(
            f"Unable to save state: {exc}"
        )


# ============================================================
# REAL ORDER
# ============================================================

def place_real_buy_order(
    api,
    option,
):

    order_params = {

        "variety": "NORMAL",

        "tradingsymbol": option["symbol"],

        "symboltoken": option["token"],

        "transactiontype": "BUY",

        "exchange": "NFO",

        "ordertype": "MARKET",

        "producttype": "INTRADAY",

        "duration": "DAY",

        "price": "0",

        "squareoff": "0",

        "stoploss": "0",

        "quantity": str(
            option["lot_size"]
        ),
    }

    response = api.placeOrder(
        order_params
    )

    if not response:

        raise RuntimeError(
            "Angel One returned empty order response."
        )

    return response


# ============================================================
# PAPER ORDER
# ============================================================

def create_paper_order(
    option,
    option_ltp,
):

    order_id = (
        "PAPER-"
        + now_ist().strftime(
            "%Y%m%d%H%M%S"
        )
    )

    return {
        "order_id": order_id,
        "symbol": option["symbol"],
        "token": option["token"],
        "transaction_type": "BUY",
        "quantity": option["lot_size"],
        "order_type": "MARKET",
        "price": option_ltp,
        "status": "PAPER ORDER",
        "time": now_ist().strftime(
            "%Y-%m-%d %H:%M:%S"
        ),
    }


# ============================================================
# AUTOMATIC BUY CE
# ============================================================

def automatic_buy_ce(
    api,
    instruments,
    spot,
    signal_info,
):

    if signal_info["signal"] != "BUY CE":

        return None

    candle_time = signal_info[
        "candle_time"
    ]

    candle_key = str(
        candle_time
    )

    # --------------------------------------------------------
    # DUPLICATE PROTECTION
    # --------------------------------------------------------

    if (
        st.session_state[
            "last_processed_signal_candle"
        ]
        == candle_key
    ):

        return None

    # --------------------------------------------------------
    # CHECK EXISTING ORDER
    # --------------------------------------------------------

    if st.session_state[
        "last_order_id"
    ]:

        # Do not automatically place another order
        # while previous order is already stored.
        return None

    # --------------------------------------------------------
    # SELECT ATM CE
    # --------------------------------------------------------

    option = select_atm_nifty_ce(
        instruments,
        spot,
    )

    # --------------------------------------------------------
    # GET OPTION LTP
    # --------------------------------------------------------

    option_ltp = get_option_ltp(
        api,
        option,
    )

    # Save selected contract
    st.session_state[
        "option_symbol"
    ] = option["symbol"]

    st.session_state[
        "option_token"
    ] = option["token"]

    st.session_state[
        "option_expiry"
    ] = str(
        option["expiry"]
    )

    st.session_state[
        "option_strike"
    ] = option["strike"]

    st.session_state[
        "option_lot_size"
    ] = option["lot_size"]

    st.session_state[
        "option_ltp"
    ] = option_ltp

    # --------------------------------------------------------
    # PAPER / LIVE
    # --------------------------------------------------------

    if PAPER_TRADING:

        order = create_paper_order(
            option,
            option_ltp,
        )

    else:

        order_response = (
            place_real_buy_order(
                api,
                option,
            )
        )

        order_id = (
            order_response
            if isinstance(
                order_response,
                str
            )
            else order_response.get(
                "data"
            )
            if isinstance(
                order_response,
                dict
            )
            else None
        )

        if isinstance(
            order_id,
            dict
        ):

            order_id = (
                order_id.get(
                    "orderid"
                )
            )

        order = {
            "order_id": str(
                order_id
            )
            if order_id
            else "UNKNOWN",
            "symbol": option[
                "symbol"
            ],
            "token": option[
                "token"
            ],
            "transaction_type": "BUY",
            "quantity": option[
                "lot_size"
            ],
            "order_type": "MARKET",
            "price": option_ltp,
            "status": "LIVE ORDER SENT",
            "time": now_ist().strftime(
                "%Y-%m-%d %H:%M:%S"
            ),
        }

    # --------------------------------------------------------
    # SAVE STATE
    # --------------------------------------------------------

    st.session_state[
        "last_order_id"
    ] = order["order_id"]

    st.session_state[
        "last_order_time"
    ] = order["time"]

    st.session_state[
        "last_processed_signal_candle"
    ] = candle_key

    st.session_state[
        "orders"
    ].insert(
        0,
        order,
    )

    save_state(
        {
            "last_order_id":
                st.session_state[
                    "last_order_id"
                ],

            "last_order_time":
                st.session_state[
                    "last_order_time"
                ],

            "last_processed_signal_candle":
                st.session_state[
                    "last_processed_signal_candle"
                ],
        }
    )

    return order


# ============================================================
# ORDER BOOK
# ============================================================

def get_order_book(api):

    if api is None:

        return []

    try:

        response = api.orderBook()

        if not isinstance(
            response,
            dict
        ):

            return []

        if not response.get(
            "status"
        ):

            return []

        data = response.get(
            "data"
        )

        if isinstance(
            data,
            list
        ):

            return data

        return []

    except Exception:

        return []


# ============================================================
# AUTOMATION
# ============================================================

def run_automation():

    # --------------------------------------------------------
    # LOGIN
    # --------------------------------------------------------

    try:

        api = st.session_state.get(
            "api"
        )

        if api is None:

            api = angel_login()

    except Exception as exc:

        st.session_state[
            "last_error"
        ] = str(exc)

        return

    # --------------------------------------------------------
    # NIFTY RATE FIRST
    # --------------------------------------------------------

    try:

        spot = get_nifty_ltp(
            api
        )

        st.session_state[
            "spot"
        ] = spot

        st.session_state[
            "last_error"
        ] = ""

    except Exception as exc:

        st.session_state[
            "last_error"
        ] = str(exc)

        return

    # --------------------------------------------------------
    # MARKET STATUS
    # --------------------------------------------------------

    if not market_is_open():

        st.session_state[
            "last_message"
        ] = (
            "Market closed. "
            "NIFTY LTP is displayed when available."
        )

        return

    # --------------------------------------------------------
    # CANDLES
    # --------------------------------------------------------

    try:

        df5 = get_nifty_candles(
            api,
            days=30,
        )

    except Exception as exc:

        st.session_state[
            "last_error"
        ] = str(exc)

        return

    # --------------------------------------------------------
    # SUPERTRENDS
    # --------------------------------------------------------

    try:

        st5, st15, st4h = (
            build_timeframes(
                df5
            )
        )

        st.session_state[
            "st5"
        ] = st5

        st.session_state[
            "st15"
        ] = st15

        st.session_state[
            "st4h"
        ] = st4h

    except Exception as exc:

        st.session_state[
            "last_error"
        ] = (
            "Supertrend calculation error: "
            + str(exc)
        )

        return

    # --------------------------------------------------------
    # SIGNAL
    # --------------------------------------------------------

    try:

        signal_info = get_signal(
            st5,
            st15,
            st4h,
        )

        st.session_state[
            "signal"
        ] = signal_info[
            "signal"
        ]

        st.session_state[
            "signal_time"
        ] = signal_info[
            "candle_time"
        ]

        st.session_state[
            "st5"
        ] = signal_info[
            "st5"
        ]

        st.session_state[
            "st15"
        ] = signal_info[
            "st15"
        ]

        st.session_state[
            "st4h"
        ] = signal_info[
            "st4h"
        ]

    except Exception as exc:

        st.session_state[
            "last_error"
        ] = (
            "Signal calculation error: "
            + str(exc)
        )

        return

    # --------------------------------------------------------
    # AUTOMATIC CE
    # --------------------------------------------------------

    if signal_info["signal"] == "BUY CE":

        try:

            instruments = load_instruments()

            order = automatic_buy_ce(
                api,
                instruments,
                spot,
                signal_info,
            )

            if order:

                st.session_state[
                    "last_message"
                ] = (
                    "Automatic BUY CE triggered: "
                    + order["symbol"]
                )

            else:

                st.session_state[
                    "last_message"
                ] = (
                    "BUY CE signal detected. "
                    "Duplicate protection checked."
                )

        except Exception as exc:

            st.session_state[
                "last_error"
            ] = (
                "Automatic BUY CE error: "
                + str(exc)
            )

    else:

        st.session_state[
            "last_message"
        ] = (
            "Waiting for 5m GREEN FLIP + "
            "15m GREEN + 4H GREEN."
        )

    st.session_state[
        "last_update"
    ] = now_ist()


# ============================================================
# HEADER
# ============================================================

st.title(
    "📈 NIFTY Automatic BUY CE Dashboard"
)

st.caption(
    "5m Supertrend 20,2 + 15m confirmation + 4H confirmation"
)


# ============================================================
# MODE WARNING
# ============================================================

if PAPER_TRADING:

    st.warning(
        "PAPER TRADING MODE — NO REAL BROKER ORDER WILL BE SENT."
    )

else:

    st.error(
        "LIVE TRADING MODE — AUTOMATIC REAL ORDERS ARE ENABLED."
    )


# ============================================================
# RUN AUTOMATION
# ============================================================

run_automation()


# ============================================================
# TOP METRICS
# ============================================================

spot = st.session_state.get(
    "spot"
)

signal = st.session_state.get(
    "signal",
    "WAIT",
)

st5_value = st.session_state.get(
    "st5"
)

st15_value = st.session_state.get(
    "st15"
)

st4h_value = st.session_state.get(
    "st4h"
)

option_ltp = st.session_state.get(
    "option_ltp"
)


col1, col2, col3, col4 = st.columns(4)


with col1:

    st.metric(
        "NIFTY Spot",
        "-"
        if spot is None
        else f"₹{spot:,.2f}",
    )


with col2:

    st.metric(
        "5M Supertrend",
        "-"
        if st5_value is None
        else f"{st5_value:,.2f}",
    )


with col3:

    st.metric(
        "15M Supertrend",
        "-"
        if st15_value is None
        else f"{st15_value:,.2f}",
    )


with col4:

    st.metric(
        "4H Supertrend",
        "-"
        if st4h_value is None
        else f"{st4h_value:,.2f}",
    )


# ============================================================
# SIGNAL
# ============================================================

st.subheader("Automatic Strategy Signal")


if signal == "BUY CE":

    st.success(
        "🟢 BUY CE SIGNAL"
    )

else:

    st.info(
        "⏳ WAIT"
    )


signal_time = st.session_state.get(
    "signal_time"
)

if signal_time:

    st.write(
        "Signal candle:",
        str(signal_time)
    )


# ============================================================
# CONDITIONS
# ============================================================

st.subheader(
    "Signal Conditions"
)

c1, c2, c3 = st.columns(3)


with c1:

    st.write(
        "5M GREEN FLIP"
    )

    if (
        st.session_state.get(
            "signal"
        )
        == "BUY CE"
    ):

        st.success("TRUE")

    else:

        st.info("FALSE")


with c2:

    st.write(
        "15M GREEN"
    )

    st.write(
        "Checked automatically"
    )


with c3:

    st.write(
        "4H GREEN"
    )

    st.write(
        "Checked automatically"
    )


# ============================================================
# SELECTED OPTION
# ============================================================

st.subheader(
    "Selected NIFTY CE"
)

o1, o2, o3, o4, o5 = st.columns(5)


with o1:

    st.metric(
        "Symbol",
        st.session_state.get(
            "option_symbol"
        )
        or "-",
    )


with o2:

    strike = st.session_state.get(
        "option_strike"
    )

    st.metric(
        "Strike",
        "-"
        if strike is None
        else f"{strike:,.0f}",
    )


with o3:

    st.metric(
        "Expiry",
        st.session_state.get(
            "option_expiry"
        )
        or "-",
    )


with o4:

    lot_size = st.session_state.get(
        "option_lot_size"
    )

    st.metric(
        "Lot Size",
        "-"
        if lot_size is None
        else str(lot_size),
    )


with o5:

    st.metric(
        "CE LTP",
        "-"
        if option_ltp is None
        else f"₹{option_ltp:,.2f}",
    )


# ============================================================
# ORDER STATUS
# ============================================================

st.subheader(
    "Automatic Order Status"
)

last_order_id = st.session_state.get(
    "last_order_id"
)

last_order_time = st.session_state.get(
    "last_order_time"
)

if last_order_id:

    st.success(
        f"Order ID: {last_order_id}"
    )

    st.write(
        "Order time:",
        last_order_time or "-"
    )

else:

    st.info(
        "No automatic CE order has been placed yet."
    )


# ============================================================
# LOCAL ORDER BOOK
# ============================================================

st.subheader(
    "Order Book"
)

local_orders = (
    st.session_state.get(
        "orders"
    )
    or []
)

if local_orders:

    order_df = pd.DataFrame(
        local_orders
    )

    st.dataframe(
        order_df,
        use_container_width=True,
        hide_index=True,
    )

else:

    st.info(
        "No automatic orders yet."
    )


# ============================================================
# REAL ANGEL ORDER BOOK
# ============================================================

if not PAPER_TRADING:

    st.subheader(
        "Angel One Order Book"
    )

    api = st.session_state.get(
        "api"
    )

    broker_orders = get_order_book(
        api
    )

    if broker_orders:

        broker_df = pd.DataFrame(
            broker_orders
        )

        st.dataframe(
            broker_df,
            use_container_width=True,
            hide_index=True,
        )

    else:

        st.info(
            "No orders returned by Angel One."
        )


# ============================================================
# STATUS
# ============================================================

st.subheader(
    "System Status"
)

s1, s2, s3 = st.columns(3)


with s1:

    st.write(
        "Angel One:",
        st.session_state.get(
            "login_status",
            "NOT CONNECTED",
        )
    )


with s2:

    st.write(
        "Market:",
        "OPEN"
        if market_is_open()
        else "CLOSED",
    )


with s3:

    st.write(
        "Last update:",
        str(
            st.session_state.get(
                "last_update"
            )
            or "-"
        ),
    )


# ============================================================
# MESSAGE / ERROR
# ============================================================

last_message = st.session_state.get(
    "last_message"
)

if last_message:

    st.info(
        last_message
    )


last_error = st.session_state.get(
    "last_error"
)

if last_error:

    st.error(
        last_error
    )


# ============================================================
# CONFIGURATION INFO
# ============================================================

with st.expander(
    "Strategy Configuration"
):

    st.write(
        f"Supertrend Period: {ST_PERIOD}"
    )

    st.write(
        f"Supertrend Multiplier: {ST_MULTIPLIER}"
    )

    st.write(
        "Primary timeframe: 5 minutes"
    )

    st.write(
        "Confirmation timeframe: 15 minutes"
    )

    st.write(
        "Confirmation timeframe: 4 hours"
    )

    st.write(
        "Instrument: NIFTY 50"
    )

    st.write(
        "NIFTY token: 99926000"
    )

    st.write(
        "Option: ATM CE only"
    )

    st.write(
        "Order type: MARKET"
    )

    st.write(
        "Trading mode:",
        "PAPER"
        if PAPER_TRADING
        else "LIVE",
    )


# ============================================================
# AUTOMATIC REFRESH
# ============================================================

time.sleep(
    REFRESH_SECONDS
)

st.rerun()
