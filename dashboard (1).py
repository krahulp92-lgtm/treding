# ============================================================
# dashboard.py
# NIFTY 50 AUTOMATIC BUY CE ONLY
#
# Strategy:
#   5-minute Supertrend 20,2 FLIPS GREEN
#   + 15-minute Supertrend GREEN
#   + 4-hour Supertrend GREEN
#       =>
#   Automatically BUY ATM NIFTY CE
#
# NO MANUAL BUY/SELL BUTTONS
#
# Order flow:
#   Signal
#      ↓
#   Select ATM CE
#      ↓
#   Get token / expiry / lot size / LTP
#      ↓
#   Duplicate protection
#      ↓
#   BUY MARKET
#      ↓
#   Angel One Order ID
#      ↓
#   Refresh Order Book
#      ↓
#   Highlight new order
# ============================================================


# ============================================================
# IMPORTS
# ============================================================

import json
import math
import os
import time

from datetime import datetime, timedelta
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
    page_title="NIFTY Automatic CE",
    page_icon="📈",
    layout="wide",
)


# ============================================================
# CONSTANTS
# ============================================================

IST = ZoneInfo("Asia/Kolkata")

NIFTY_TOKEN = "99926000"
NIFTY_EXCHANGE = "NSE"
NIFTY_NAME = "NIFTY"

ST_PERIOD = 20
ST_MULTIPLIER = 2.0

CANDLE_INTERVAL = "FIVE_MINUTE"

MARKET_OPEN = (9, 15)
MARKET_CLOSE = (15, 30)

AUTO_TRADE = True
AUTO_TRADE_CE = True

# IMPORTANT:
# Default is PAPER.
#
# To enable real orders:
#
# Windows CMD:
#   set LIVE_TRADING=true
#
# PowerShell:
#   $env:LIVE_TRADING="true"
#
# Streamlit Cloud secrets:
#   LIVE_TRADING="true"
#
LIVE_TRADING = (
    os.getenv(
        "LIVE_TRADING",
        "false",
    )
    .strip()
    .lower()
    == "true"
)

AUTO_REFRESH_SECONDS = 10

DUPLICATE_PROTECTION = True

ORDER_VARIETY = "NORMAL"
ORDER_TYPE = "MARKET"
ORDER_PRODUCT = "CARRYFORWARD"
ORDER_DURATION = "DAY"

ORDER_TAG = "NIFTYCE20"

STATE_FILE = Path("nifty_auto_ce_state.json")

INSTRUMENT_MASTER_URL = (
    "https://margincalculator.angelone.in/"
    "OpenAPI_File/files/OpenAPIScripMaster.json"
)

INSTRUMENT_MASTER_FILE = Path("OpenAPIScripMaster.json")

INSTRUMENTS_CACHE = None
def load_instruments():
    """
    Load Angel One instrument master JSON.
    Returns a list of instrument records.
    """

    global INSTRUMENTS_CACHE

    if INSTRUMENTS_CACHE is not None:
        return INSTRUMENTS_CACHE

    if INSTRUMENT_MASTER_FILE.exists():
        try:
            with open(INSTRUMENT_MASTER_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)

            if isinstance(data, list):
                INSTRUMENTS_CACHE = data
                return data

        except Exception:
            pass

    try:
        response = requests.get(
            INSTRUMENT_MASTER_URL,
            timeout=30
        )
        response.raise_for_status()

        data = response.json()

        if not isinstance(data, list):
            raise RuntimeError("Invalid instrument master format")

        with open(INSTRUMENT_MASTER_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f)

        INSTRUMENTS_CACHE = data
        return data

    except Exception as e:
        raise RuntimeError(
            f"Unable to load Angel One instrument master: {e}"
        )


# ============================================================
# CREDENTIALS
# ============================================================

ANGEL_API_KEY = os.getenv(
    "ANGEL_API_KEY",
    "",
).strip()

ANGEL_CLIENT_ID = os.getenv(
    "ANGEL_CLIENT_ID",
    "",
).strip()

ANGEL_PASSWORD = os.getenv(
    "ANGEL_PASSWORD",
    "",
).strip()

ANGEL_TOTP_SECRET = os.getenv(
    "ANGEL_TOTP_SECRET",
    "",
).strip()


# ============================================================
# SESSION STATE
# ============================================================

DEFAULTS = {
    "api": None,
    "login_status": "NOT CONNECTED",

    "instruments": None,

    "spot": None,

    "st5": None,
    "st15": None,
    "st4h": None,

    "signal": "WAIT",
    "signal_time": None,

    "ce_option": None,
    "ce_ltp": None,

    "order_book": [],
    "positions": [],

    "last_order_id": None,
    "highlight_order_id": None,
    "highlight_symbol": None,

    "last_auto_signal_key": None,

    "auto_trade_status": (
        "Waiting for BUY CE signal."
    ),

    "last_message": "",
    "last_error": "",

    "selected_section": "Dashboard",
}


for key, value in DEFAULTS.items():
    if key not in st.session_state:
        st.session_state[key] = value


# ============================================================
# BASIC HELPERS
# ============================================================

def now_ist():
    return datetime.now(IST)


def fmt_number(value, decimals=2):
    if value is None:
        return "-"

    try:
        value = float(value)

        if decimals == 0:
            return f"{value:,.0f}"

        return f"{value:,.{decimals}f}"

    except Exception:
        return str(value)


def normalize_date(value):
    if value is None:
        return None

    try:
        return pd.to_datetime(
            value,
            errors="coerce",
        )
    except Exception:
        return None


# ============================================================
# STATE FILE
# ============================================================

def load_state():
    default = {
        "processed_signal_keys": [],
        "orders": [],
    }

    try:
        if not STATE_FILE.exists():
            return default

        with open(
            STATE_FILE,
            "r",
            encoding="utf-8",
        ) as f:
            data = json.load(f)

        if not isinstance(data, dict):
            return default

        data.setdefault(
            "processed_signal_keys",
            [],
        )

        data.setdefault(
            "orders",
            [],
        )

        return data

    except Exception:
        return default


def save_state(state):
    try:
        temp = STATE_FILE.with_suffix(
            ".tmp"
        )

        with open(
            temp,
            "w",
            encoding="utf-8",
        ) as f:
            json.dump(
                state,
                f,
                indent=2,
                default=str,
            )

        temp.replace(STATE_FILE)

    except Exception as e:
        st.session_state.last_error = (
            f"State save failed: {e}"
        )


# ============================================================
# TOTP
# ============================================================

def get_totp_secret(raw):
    raw = (
        raw or ""
    ).strip()

    if raw.startswith(
        "otpauth://"
    ):
        try:
            from urllib.parse import (
                urlparse,
                parse_qs,
            )

            parsed = urlparse(raw)

            params = parse_qs(
                parsed.query
            )

            secret = params.get(
                "secret",
                [""],
            )[0]

            if secret:
                return secret.replace(
                    " ",
                    "",
                ).upper()

        except Exception:
            pass

    return raw.replace(
        " ",
        "",
    ).upper()


def generate_totp():
    secret = get_totp_secret(
        ANGEL_TOTP_SECRET
    )

    if not secret:
        raise RuntimeError(
            "ANGEL_TOTP_SECRET is empty."
        )

    try:
        return pyotp.TOTP(
            secret
        ).now()

    except Exception as e:
        raise RuntimeError(
            "Invalid ANGEL_TOTP_SECRET. "
            "Use the Base32 TOTP secret, "
            "not the current 6-digit OTP.\n"
            f"{e}"
        )


# ============================================================
# CREDENTIAL CHECK
# ============================================================

def credentials_ok():
    missing = []

    if not ANGEL_API_KEY:
        missing.append(
            "ANGEL_API_KEY"
        )

    if not ANGEL_CLIENT_ID:
        missing.append(
            "ANGEL_CLIENT_ID"
        )

    if not ANGEL_PASSWORD:
        missing.append(
            "ANGEL_PASSWORD"
        )

    if not ANGEL_TOTP_SECRET:
        missing.append(
            "ANGEL_TOTP_SECRET"
        )

    if missing:
        return False, missing

    return True, []


# ============================================================
# ANGEL ONE LOGIN
# ============================================================

def angel_login():
    ok, missing = credentials_ok()

    if not ok:
        raise RuntimeError(
            "Missing credentials: "
            + ", ".join(missing)
        )

    totp = generate_totp()

    api = SmartConnect(
        api_key=ANGEL_API_KEY
    )

    try:
        response = api.generateSession(
            ANGEL_CLIENT_ID,
            ANGEL_PASSWORD,
            totp,
        )

    except Exception as e:
        raise RuntimeError(
            f"Angel One login exception: {e}"
        )

    if not response:
        raise RuntimeError(
            "Angel One returned empty login response."
        )

    if not response.get("status"):
        raise RuntimeError(
            "Angel One login failed.\n"
            f"Message: {response.get('message')}\n"
            f"Error code: {response.get('errorcode')}\n"
            f"Response: {response}"
        )

    st.session_state.api = api
    st.session_state.login_status = (
        "CONNECTED"
    )

    st.session_state.last_message = (
        "Angel One connected successfully."
    )

    return api


# ============================================================
# INSTRUMENT MASTER
# ============================================================

@st.cache_data(ttl=60, show_spinner=False)
def get_nifty_candles_cached(_api, token, from_date, to_date):
    params = {
        "exchange": "NSE",
        "symboltoken": str(token),
        "interval": "FIVE_MINUTE",
        "fromdate": from_date,
        "todate": to_date,
    }

    response = _api.getCandleData(params)

    if not response or response.get("status") is not True:
        message = str(response)
        if "rate" in message.lower() or "access denied" in message.lower():
            raise RuntimeError(
                "Angel One Candle API rate limit reached. "
                "Please wait before requesting candles again."
            )
        raise RuntimeError(f"Candle API failed: {message}")

    data = response.get("data") or []

    if not data:
        raise RuntimeError("Candle API returned no candle data.")

    return data


# ============================================================
# NIFTY LTP
# ============================================================

def get_nifty_ltp():
    api = st.session_state.api

    if api is None:
        raise RuntimeError(
            "Angel One is not connected."
        )

    try:
        response = api.ltpData(
            NIFTY_EXCHANGE,
            "Nifty 50",
            NIFTY_TOKEN,
        )

    except Exception as e:
        raise RuntimeError(
            f"NIFTY LTP API exception: {e}"
        )

    if not response:
        raise RuntimeError(
            "NIFTY LTP returned empty response."
        )

    if not response.get("status"):
        raise RuntimeError(
            "NIFTY LTP FAILED\n"
            f"Message: {response.get('message')}\n"
            f"Error code: {response.get('errorcode')}\n"
            f"Response: {response}"
        )

    data = (
        response.get("data")
        or {}
    )

    ltp = data.get("ltp")

    if ltp is None:
        raise RuntimeError(
            f"NIFTY LTP missing: {response}"
        )

    return float(ltp)


# ============================================================
# NIFTY CANDLES
# ============================================================

def get_nifty_candles(days=30):
    api = st.session_state.api

    if api is None:
        raise RuntimeError(
            "Angel One is not connected."
        )

    end = now_ist()

    start = end - timedelta(
        days=days
    )

    params = {
        "exchange": NIFTY_EXCHANGE,
        "symboltoken": NIFTY_TOKEN,
        "interval": CANDLE_INTERVAL,
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

    except Exception as e:
        raise RuntimeError(
            f"Candle API exception: {e}"
        )

    if not response:
        raise RuntimeError(
            "Candle API returned empty response."
        )

    if not response.get("status"):
        raise RuntimeError(
            "Candle API FAILED\n"
            f"Message: {response.get('message')}\n"
            f"Error code: {response.get('errorcode')}\n"
            f"Response: {response}"
        )

    rows = (
        response.get("data")
        or []
    )

    if not rows:
        raise RuntimeError(
            "Candle API returned no candles."
        )

    df = pd.DataFrame(
        rows,
        columns=[
            "timestamp",
            "open",
            "high",
            "low",
            "close",
            "volume",
        ],
    )

    df["timestamp"] = pd.to_datetime(
        df["timestamp"],
        errors="coerce",
    )

    if df["timestamp"].dt.tz is None:
        df["timestamp"] = (
            df["timestamp"]
            .dt.tz_localize(IST)
        )
    else:
        df["timestamp"] = (
            df["timestamp"]
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

    df = (
        df.dropna(
            subset=[
                "timestamp",
                "open",
                "high",
                "low",
                "close",
            ]
        )
        .sort_values("timestamp")
        .drop_duplicates(
            subset=["timestamp"]
        )
        .reset_index(drop=True)
    )

    return df


# ============================================================
# REMOVE INCOMPLETE CURRENT CANDLE
# ============================================================

def remove_incomplete_candle(df):
    if df is None or df.empty:
        return df

    df = df.copy()

    last_time = df.iloc[-1]["timestamp"]

    if last_time.tzinfo is None:
        last_time = last_time.replace(
            tzinfo=IST
        )

    now = now_ist()

    # Angel candle timestamp is treated
    # as candle start.
    expected_close = (
        last_time
        + timedelta(minutes=5)
    )

    if expected_close > now:
        df = df.iloc[:-1].copy()

    return df.reset_index(
        drop=True
    )


# ============================================================
# SUPERTREND
# ============================================================

def calculate_supertrend(
    df,
    period=20,
    multiplier=2.0,
):
    data = df.copy()

    high = data["high"]
    low = data["low"]
    close = data["close"]

    previous_close = (
        close.shift(1)
    )

    tr1 = high - low

    tr2 = (
        high - previous_close
    ).abs()

    tr3 = (
        low - previous_close
    ).abs()

    true_range = pd.concat(
        [
            tr1,
            tr2,
            tr3,
        ],
        axis=1,
    ).max(axis=1)

    atr = (
        true_range
        .ewm(
            alpha=1 / period,
            adjust=False,
            min_periods=period,
        )
        .mean()
    )

    hl2 = (
        high + low
    ) / 2.0

    basic_upper = (
        hl2
        + multiplier * atr
    )

    basic_lower = (
        hl2
        - multiplier * atr
    )

    final_upper = pd.Series(
        np.nan,
        index=data.index,
    )

    final_lower = pd.Series(
        np.nan,
        index=data.index,
    )

    supertrend = pd.Series(
        np.nan,
        index=data.index,
    )

    direction = pd.Series(
        0,
        index=data.index,
        dtype=int,
    )

    for i in range(
        len(data)
    ):
        if pd.isna(atr.iloc[i]):
            continue

        if i == 0:
            final_upper.iloc[i] = (
                basic_upper.iloc[i]
            )

            final_lower.iloc[i] = (
                basic_lower.iloc[i]
            )

            direction.iloc[i] = 1

            supertrend.iloc[i] = (
                final_lower.iloc[i]
            )

            continue

        prev_upper = (
            final_upper.iloc[i - 1]
        )

        prev_lower = (
            final_lower.iloc[i - 1]
        )

        prev_close_value = (
            close.iloc[i - 1]
        )

        if (
            basic_upper.iloc[i]
            < prev_upper
            or prev_close_value
            > prev_upper
        ):
            final_upper.iloc[i] = (
                basic_upper.iloc[i]
            )
        else:
            final_upper.iloc[i] = (
                prev_upper
            )

        if (
            basic_lower.iloc[i]
            > prev_lower
            or prev_close_value
            < prev_lower
        ):
            final_lower.iloc[i] = (
                basic_lower.iloc[i]
            )
        else:
            final_lower.iloc[i] = (
                prev_lower
            )

        previous_st = (
            supertrend.iloc[i - 1]
        )

        if pd.isna(previous_st):
            direction.iloc[i] = 1
            supertrend.iloc[i] = (
                final_lower.iloc[i]
            )

        elif (
            previous_st
            == prev_upper
        ):
            if (
                close.iloc[i]
                <= final_upper.iloc[i]
            ):
                direction.iloc[i] = -1
                supertrend.iloc[i] = (
                    final_upper.iloc[i]
                )
            else:
                direction.iloc[i] = 1
                supertrend.iloc[i] = (
                    final_lower.iloc[i]
                )

        else:
            if (
                close.iloc[i]
                >= final_lower.iloc[i]
            ):
                direction.iloc[i] = 1
                supertrend.iloc[i] = (
                    final_lower.iloc[i]
                )
            else:
                direction.iloc[i] = -1
                supertrend.iloc[i] = (
                    final_upper.iloc[i]
                )

    data["ATR"] = atr
    data["Basic_Upper"] = basic_upper
    data["Basic_Lower"] = basic_lower
    data["Final_Upper"] = final_upper
    data["Final_Lower"] = final_lower
    data["Supertrend"] = supertrend
    data["ST_Direction"] = direction
    data["ST_Green"] = (
        direction == 1
    )
    data["ST_Red"] = (
        direction == -1
    )

    data["ST_Flip_Green"] = (
        data["ST_Green"]
        & ~data["ST_Green"].shift(
            1,
            fill_value=False,
        )
    )

    data["ST_Flip_Red"] = (
        data["ST_Red"]
        & ~data["ST_Red"].shift(
            1,
            fill_value=False,
        )
    )

    return data


# ============================================================
# RESAMPLE
# ============================================================

def resample_ohlcv(
    df,
    rule,
):
    data = df.copy()

    data = data.set_index(
        "timestamp"
    )

    result = (
        data.resample(
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
        .dropna(
            subset=[
                "open",
                "high",
                "low",
                "close",
            ]
        )
        .reset_index()
    )

    return result


# ============================================================
# BUILD 5m / 15m / 4h
# ============================================================

def build_timeframes(df5):
    st5 = calculate_supertrend(
        df5,
        ST_PERIOD,
        ST_MULTIPLIER,
    )

    df15 = resample_ohlcv(
        df5,
        "15min",
    )

    st15 = calculate_supertrend(
        df15,
        ST_PERIOD,
        ST_MULTIPLIER,
    )

    df4h = resample_ohlcv(
        df5,
        "4h",
    )

    st4h = calculate_supertrend(
        df4h,
        ST_PERIOD,
        ST_MULTIPLIER,
    )

    return (
        st5,
        st15,
        st4h,
    )


# ============================================================
# CONFIRMED SIGNAL
# ============================================================

def current_signal(
    st5,
    st15,
    st4h,
):
    if len(st5) < 2:
        return "WAIT", None

    if len(st15) < 2:
        return "WAIT", None

    if len(st4h) < 2:
        return "WAIT", None

    latest5 = st5.iloc[-1]
    latest15 = st15.iloc[-1]
    latest4h = st4h.iloc[-1]

    # --------------------------------------------------------
    # IMPORTANT:
    # Signal is generated ONLY when the latest completed
    # 5-minute candle flips GREEN.
    # --------------------------------------------------------

    flip_green = bool(
        latest5["ST_Flip_Green"]
    )

    green_15 = bool(
        latest15["ST_Green"]
    )

    green_4h = bool(
        latest4h["ST_Green"]
    )

    if (
        flip_green
        and green_15
        and green_4h
    ):
        return (
            "BUY_CE",
            latest5["timestamp"],
        )

    return (
        "WAIT",
        latest5["timestamp"],
    )


# ============================================================
# OPTION MASTER HELPERS
# ============================================================

def is_nifty_option(row):
    exchange = str(
        row.get(
            "exch_seg",
            ""
        )
    ).upper()

    symbol = str(
        row.get(
            "symbol",
            ""
        )
    ).upper()

    name = str(
        row.get(
            "name",
            ""
        )
    ).upper()

    instrument_type = str(
        row.get(
            "instrumenttype",
            ""
        )
    ).upper()

    return (
        exchange == "NFO"
        and (
            "OPTIDX" in instrument_type
            or instrument_type == "OPTFUT"
            or symbol.startswith("NIFTY")
            or name == "NIFTY"
        )
    )


def parse_strike(row):
    try:
        value = float(
            row.get(
                "strike",
                0,
            )
        )

        # Angel One instrument master
        # commonly stores strike in
        # multiplied format.
        if value > 100000:
            value = value / 100.0

        return value

    except Exception:
        return None


def parse_expiry(row):
    value = row.get(
        "expiry"
    )

    if not value:
        return None

    try:
        return pd.to_datetime(
            value,
            errors="coerce",
        ).date()

    except Exception:
        return None


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

    for row in instruments:

        if not is_nifty_option(
            row
        ):
            continue

        symbol = str(
            row.get(
                "symbol",
                ""
            )
        ).strip()

        if not symbol:
            continue

        option_type = str(
            row.get(
                "optiontype",
                ""
            )
        ).upper().strip()

        # Some versions of the
        # instrument master identify
        # the option type in symbol.
        if option_type != "CE":
            if not symbol.upper().endswith(
                "CE"
            ):
                continue

        expiry = parse_expiry(
            row
        )

        if expiry is None:
            continue

        if expiry < today:
            continue

        strike = parse_strike(
            row
        )

        if strike is None:
            continue

        try:
            token = str(
                row.get(
                    "token",
                    ""
                )
            )

            lot_size = int(
                float(
                    row.get(
                        "lotsize",
                        0,
                    )
                )
            )

        except Exception:
            continue

        if not token:
            continue

        if lot_size <= 0:
            continue

        candidates.append(
            {
                "symbol": symbol,
                "token": token,
                "expiry": expiry,
                "strike": strike,
                "lot_size": lot_size,
                "exchange": "NFO",
            }
        )

    if not candidates:
        raise RuntimeError(
            "No valid NIFTY CE options "
            "were found in the instrument master."
        )

    # --------------------------------------------------------
    # FIRST: nearest expiry
    # --------------------------------------------------------

    nearest_expiry = min(
        x["expiry"]
        for x in candidates
    )

    expiry_candidates = [
        x
        for x in candidates
        if x["expiry"]
        == nearest_expiry
    ]

    # --------------------------------------------------------
    # SECOND: closest strike to NIFTY spot
    # --------------------------------------------------------

    selected = min(
        expiry_candidates,
        key=lambda x: abs(
            x["strike"]
            - float(spot)
        ),
    )

    return selected


# ============================================================
# OPTION LTP
# ============================================================

def get_option_ltp(option):
    api = st.session_state.api

    if api is None:
        raise RuntimeError(
            "Angel One is not connected."
        )

    response = api.ltpData(
        "NFO",
        option["symbol"],
        str(option["token"]),
    )

    if not response:
        raise RuntimeError(
            "Option LTP returned empty response."
        )

    if not response.get("status"):
        raise RuntimeError(
            "Option LTP failed.\n"
            f"Message: {response.get('message')}\n"
            f"Error code: {response.get('errorcode')}\n"
            f"Response: {response}"
        )

    data = (
        response.get("data")
        or {}
    )

    ltp = data.get("ltp")

    if ltp is None:
        raise RuntimeError(
            f"Option LTP missing: {response}"
        )

    return float(ltp)


# ============================================================
# PREPARE ATM CE
# ============================================================

def prepare_ce(
    spot,
):
    instruments = (
        st.session_state.instruments
    )

    option = select_atm_nifty_ce(
        instruments,
        spot,
    )

    option["ltp"] = get_option_ltp(
        option
    )

    st.session_state.ce_option = (
        option
    )

    st.session_state.ce_ltp = (
        option["ltp"]
    )

    return option


# ============================================================
# DUPLICATE CHECK
# ============================================================

def orderbook_duplicate_exists(
    symbol,
):
    api = st.session_state.api

    if api is None:
        return False

    try:
        response = api.orderBook()

    except Exception:
        return False

    if not response:
        return False

    if not response.get("status"):
        return False

    orders = (
        response.get("data")
        or []
    )

    today = now_ist().date()

    for order in orders:

        order_symbol = str(
            order.get(
                "tradingsymbol",
                "",
            )
        )

        transaction = str(
            order.get(
                "transactiontype",
                "",
            )
        ).upper()

        if order_symbol != symbol:
            continue

        if transaction != "BUY":
            continue

        update_text = str(
            order.get(
                "updatetime",
                "",
            )
        )

        if not update_text:
            return True

        parsed = pd.to_datetime(
            update_text,
            errors="coerce",
        )

        if pd.isna(parsed):
            return True

        if parsed.date() == today:
            return True

    return False


# ============================================================
# MARKET HOURS
# ============================================================

def market_is_open():
    now = now_ist()

    if now.weekday() >= 5:
        return False

    current = (
        now.hour * 60
        + now.minute
    )

    opening = (
        MARKET_OPEN[0] * 60
        + MARKET_OPEN[1]
    )

    closing = (
        MARKET_CLOSE[0] * 60
        + MARKET_CLOSE[1]
    )

    return (
        opening
        <= current
        <= closing
    )


# ============================================================
# SIGNAL KEY
# ============================================================

def signal_key(
    signal,
    signal_time,
):
    if (
        signal != "BUY_CE"
        or signal_time is None
    ):
        return None

    timestamp = pd.Timestamp(
        signal_time
    )

    return (
        "BUY_CE:"
        + timestamp.isoformat()
    )


# ============================================================
# PLACE BUY CE
# ============================================================

def place_buy_ce(
    option,
):
    if not option:
        raise RuntimeError(
            "CE option data missing."
        )

    api = st.session_state.api

    if api is None:
        raise RuntimeError(
            "Angel One is not connected."
        )

    symbol = str(
        option["symbol"]
    )

    token = str(
        option["token"]
    )

    quantity = int(
        option["lot_size"]
    )

    # --------------------------------------------------------
    # DUPLICATE PROTECTION
    # --------------------------------------------------------

    if DUPLICATE_PROTECTION:

        if orderbook_duplicate_exists(
            symbol
        ):
            raise RuntimeError(
                "Duplicate BUY blocked.\n\n"
                f"Symbol: {symbol}\n"
                "A BUY order for this symbol "
                "already exists today."
            )

    # --------------------------------------------------------
    # PAPER MODE
    # --------------------------------------------------------

    if not LIVE_TRADING:

        order_id = (
            "PAPER-"
            + now_ist().strftime(
                "%Y%m%d%H%M%S"
            )
        )

        order = {
            "orderid": order_id,
            "tradingsymbol": symbol,
            "symboltoken": token,
            "transactiontype": "BUY",
            "exchange": "NFO",
            "ordertype": "MARKET",
            "producttype": ORDER_PRODUCT,
            "duration": ORDER_DURATION,
            "quantity": str(
                quantity
            ),
            "price": "0",
            "averageprice": str(
                option.get(
                    "ltp",
                    0,
                )
            ),
            "orderstatus": "PAPER",
            "status": "PAPER",
            "updatetime": now_ist().strftime(
                "%d-%b-%Y %H:%M:%S"
            ),
            "ordertag": ORDER_TAG,
        }

        state = load_state()

        state["orders"].insert(
            0,
            {
                "orderid": order_id,
                "symbol": symbol,
                "token": token,
                "quantity": quantity,
                "signal": "BUY_CE",
                "mode": "PAPER",
                "time": now_ist().isoformat(),
            },
        )

        save_state(state)

        st.session_state.order_book = (
            [order]
            + (
                st.session_state.order_book
                or []
            )
        )

        return order_id

    # --------------------------------------------------------
    # LIVE ANGEL ONE ORDER
    # --------------------------------------------------------

    order_params = {
        "variety": ORDER_VARIETY,
        "tradingsymbol": symbol,
        "symboltoken": token,
        "transactiontype": "BUY",
        "exchange": "NFO",
        "ordertype": ORDER_TYPE,
        "producttype": ORDER_PRODUCT,
        "duration": ORDER_DURATION,
        "quantity": str(quantity),
        "price": "0",
        "squareoff": "0",
        "stoploss": "0",
        "ordertag": ORDER_TAG,
    }

    try:
        response = api.placeOrder(
            order_params
        )

    except Exception as e:
        raise RuntimeError(
            "SmartAPI placeOrder exception:\n"
            f"{e}"
        )

    if not response:
        raise RuntimeError(
            "Angel One returned an empty "
            "placeOrder response."
        )

    # --------------------------------------------------------
    # SDK RESPONSE CAN BE STRING OR DICT
    # --------------------------------------------------------

    if isinstance(
        response,
        dict,
    ):

        if not response.get(
            "status"
        ):
            raise RuntimeError(
                "Angel One rejected BUY CE.\n\n"
                f"Message: {response.get('message')}\n"
                f"Error Code: {response.get('errorcode')}\n"
                f"Response: {response}"
            )

        data = (
            response.get("data")
            or {}
        )

        order_id = data.get(
            "orderid"
        )

        if not order_id:
            raise RuntimeError(
                "Angel One accepted the order "
                "request but returned no order ID.\n\n"
                f"Response: {response}"
            )

        return str(order_id)

    return str(response)


# ============================================================
# SAVE LIVE ORDER
# ============================================================

def save_order_record(
    order_id,
    option,
):
    state = load_state()

    state["orders"].insert(
        0,
        {
            "orderid": str(
                order_id
            ),
            "symbol": option["symbol"],
            "token": str(
                option["token"]
            ),
            "quantity": int(
                option["lot_size"]
            ),
            "signal": "BUY_CE",
            "mode": (
                "LIVE"
                if LIVE_TRADING
                else "PAPER"
            ),
            "time": now_ist().isoformat(),
        },
    )

    state["orders"] = (
        state["orders"][:100]
    )

    save_state(state)


# ============================================================
# REFRESH ORDER BOOK
# ============================================================

def refresh_order_book():
    api = st.session_state.api

    if api is None:
        raise RuntimeError(
            "Angel One is not connected."
        )

    try:
        response = api.orderBook()

    except Exception as e:
        raise RuntimeError(
            f"Order Book API exception: {e}"
        )

    if not response:
        raise RuntimeError(
            "Order Book returned empty response."
        )

    if not response.get("status"):
        raise RuntimeError(
            "Order Book failed.\n"
            f"Message: {response.get('message')}\n"
            f"Error code: {response.get('errorcode')}\n"
            f"Response: {response}"
        )

    orders = (
        response.get("data")
        or []
    )

    st.session_state.order_book = (
        orders
    )

    return orders


# ============================================================
# FIND ORDER IN ORDER BOOK
# ============================================================

def find_order(
    order_id,
):
    orders = (
        st.session_state.order_book
        or []
    )

    for order in orders:

        if str(
            order.get(
                "orderid",
                "",
            )
        ) == str(order_id):
            return order

    return None


# ============================================================
# EXECUTE AUTOMATIC CE
# ============================================================

def execute_automatic_ce(
    signal,
    signal_time,
    spot,
):
    if not AUTO_TRADE:
        st.session_state.auto_trade_status = (
            "Automatic trading disabled."
        )
        return False

    if not AUTO_TRADE_CE:
        st.session_state.auto_trade_status = (
            "Automatic CE trading disabled."
        )
        return False

    if signal != "BUY_CE":
        st.session_state.auto_trade_status = (
            "Waiting for BUY CE signal."
        )
        return False

    if not market_is_open():
        st.session_state.auto_trade_status = (
            "BUY CE signal detected outside "
            "NSE market hours. No order sent."
        )
        return False

    key = signal_key(
        signal,
        signal_time,
    )

    if not key:
        st.session_state.auto_trade_status = (
            "Signal timestamp unavailable."
        )
        return False

    # --------------------------------------------------------
    # SAME SIGNAL PROTECTION
    # --------------------------------------------------------

    state = load_state()

    processed = state.get(
        "processed_signal_keys",
        [],
    )

    if (
        key in processed
        or key
        == st.session_state.last_auto_signal_key
    ):
        st.session_state.auto_trade_status = (
            f"Already processed: {key}"
        )
        return False

    try:

        st.session_state.auto_trade_status = (
            "🟡 BUY CE signal confirmed. "
            "Selecting ATM CE..."
        )

        option = prepare_ce(
            spot
        )

        if not option:
            raise RuntimeError(
                "ATM CE selection failed."
            )

        st.session_state.auto_trade_status = (
            "🟡 ATM CE selected: "
            f"{option['symbol']} | "
            f"Strike {option['strike']} | "
            f"Expiry {option['expiry']} | "
            f"Lot {option['lot_size']} | "
            f"LTP {option['ltp']}"
        )

        st.session_state.auto_trade_status = (
            "🟡 Checking duplicate orders..."
        )

        # ----------------------------------------------------
        # PLACE ORDER
        # ----------------------------------------------------

        st.session_state.auto_trade_status = (
            "🟡 Sending automatic BUY CE..."
        )

        order_id = place_buy_ce(
            option
        )

        # ----------------------------------------------------
        # SAVE ORDER
        # ----------------------------------------------------

        save_order_record(
            order_id,
            option,
        )

        # ----------------------------------------------------
        # PROCESS SIGNAL ONLY AFTER SUCCESS
        # ----------------------------------------------------

        processed.append(
            key
        )

        state["processed_signal_keys"] = (
            processed[-100:]
        )

        save_state(state)

        st.session_state.last_auto_signal_key = (
            key
        )

        st.session_state.last_order_id = (
            order_id
        )

        st.session_state.highlight_order_id = (
            order_id
        )

        st.session_state.highlight_symbol = (
            option["symbol"]
        )

        st.session_state.last_message = (
            "Automatic BUY CE submitted | "
            f"{option['symbol']} | "
            f"Order ID: {order_id}"
        )

        # ----------------------------------------------------
        # REFRESH ORDER BOOK
        # ----------------------------------------------------

        if LIVE_TRADING:

            # Give broker a moment to make
            # the order visible.
            time.sleep(1)

            try:
                refresh_order_book()

            except Exception as e:
                st.warning(
                    "Order was submitted, but "
                    "Order Book refresh failed:\n"
                    f"{e}"
                )

        # ----------------------------------------------------
        # PAPER ORDER ALREADY INSERTED ABOVE
        # ----------------------------------------------------

        st.session_state.selected_section = (
            "Order Book"
        )

        st.session_state.auto_trade_status = (
            "✅ AUTOMATIC BUY CE SUCCESS\n"
            f"Symbol: {option['symbol']}\n"
            f"Strike: {option['strike']}\n"
            f"Expiry: {option['expiry']}\n"
            f"Quantity: {option['lot_size']}\n"
            f"Order ID: {order_id}"
        )

        return True

    except Exception as e:

        st.session_state.auto_trade_status = (
            "❌ Automatic BUY CE failed:\n"
            f"{e}"
        )

        st.session_state.last_error = str(
            e
        )

        return False


# ============================================================
# ORDER BOOK DISPLAY
# ============================================================

def show_order_book():
    st.title(
        "📋 NIFTY CE Order Book"
    )

    c1, c2 = st.columns(
        [1, 4]
    )

    with c1:

        if st.button(
            "🔄 Refresh",
            use_container_width=True,
        ):

            try:

                if LIVE_TRADING:
                    refresh_order_book()

                st.rerun()

            except Exception as e:

                st.error(
                    f"Refresh failed: {e}"
                )

    with c2:

        if (
            st.session_state
            .highlight_order_id
        ):
            st.success(
                "⭐ Newly placed order: "
                + str(
                    st.session_state
                    .highlight_order_id
                )
            )

    orders = (
        st.session_state.order_book
        or []
    )

    # --------------------------------------------------------
    # PAPER MODE: restore from local state
    # --------------------------------------------------------

    if (
        not LIVE_TRADING
        and not orders
    ):

        state = load_state()

        local_orders = (
            state.get(
                "orders",
                [],
            )
        )

        for saved in local_orders:

            orders.append(
                {
                    "orderid": saved.get(
                        "orderid",
                        "",
                    ),
                    "tradingsymbol": saved.get(
                        "symbol",
                        "",
                    ),
                    "symboltoken": saved.get(
                        "token",
                        "",
                    ),
                    "transactiontype": "BUY",
                    "exchange": "NFO",
                    "ordertype": "MARKET",
                    "producttype": ORDER_PRODUCT,
                    "duration": ORDER_DURATION,
                    "quantity": str(
                        saved.get(
                            "quantity",
                            "",
                        )
                    ),
                    "orderstatus": "PAPER",
                    "status": "PAPER",
                    "updatetime": saved.get(
                        "time",
                        "",
                    ),
                }
            )

        st.session_state.order_book = (
            orders
        )

    if not orders:

        st.info(
            "No orders found."
        )

        return

    rows = []

    highlight_id = str(
        st.session_state.highlight_order_id
        or ""
    )

    for order in orders:

        order_id = str(
            order.get(
                "orderid",
                "",
            )
        )

        rows.append(
            {
                "⭐": (
                    "NEW"
                    if order_id
                    == highlight_id
                    else ""
                ),
                "Order ID": order_id,
                "Symbol": order.get(
                    "tradingsymbol",
                    "",
                ),
                "Side": order.get(
                    "transactiontype",
                    "",
                ),
                "Quantity": order.get(
                    "quantity",
                    "",
                ),
                "Order Type": order.get(
                    "ordertype",
                    "",
                ),
                "Product": order.get(
                    "producttype",
                    "",
                ),
                "Status": order.get(
                    "orderstatus",
                    order.get(
                        "status",
                        "",
                    ),
                ),
                "Price": order.get(
                    "price",
                    "",
                ),
                "Average Price": order.get(
                    "averageprice",
                    "",
                ),
                "Time": order.get(
                    "updatetime",
                    "",
                ),
            }
        )

    df = pd.DataFrame(
        rows
    )

    # Newest order first
    if highlight_id:

        df["_new"] = (
            df["Order ID"]
            .astype(str)
            .eq(highlight_id)
        )

        df = (
            df.sort_values(
                "_new",
                ascending=False,
            )
            .drop(
                columns=[
                    "_new"
                ]
            )
        )

    st.dataframe(
        df,
        use_container_width=True,
        hide_index=True,
    )

    # --------------------------------------------------------
    # HIGHLIGHT NEW ORDER
    # --------------------------------------------------------

    if highlight_id:

        selected = find_order(
            highlight_id
        )

        if selected:

            st.divider()

            st.subheader(
                "⭐ Newly Placed CE Order"
            )

            a, b, c, d = st.columns(
                4
            )

            with a:
                st.metric(
                    "Order ID",
                    str(
                        selected.get(
                            "orderid",
                            "-",
                        )
                    ),
                )

            with b:
                st.metric(
                    "Symbol",
                    str(
                        selected.get(
                            "tradingsymbol",
                            "-",
                        )
                    ),
                )

            with c:
                st.metric(
                    "Quantity",
                    str(
                        selected.get(
                            "quantity",
                            "-",
                        )
                    ),
                )

            with d:
                st.metric(
                    "Status",
                    str(
                        selected.get(
                            "orderstatus",
                            selected.get(
                                "status",
                                "-",
                            ),
                        )
                    ),
                )


# ============================================================
# AUTOMATED CE PANEL
# ============================================================

def show_auto_ce_panel():
    st.divider()

    st.subheader(
        "🤖 Automatic NIFTY CE"
    )

    option = (
        st.session_state.ce_option
    )

    if option:

        a, b, c, d = st.columns(
            4
        )

        a.metric(
            "Signal",
            "BUY CE",
        )

        b.metric(
            "Strike",
            fmt_number(
                option["strike"],
                0,
            ),
        )

        c.metric(
            "CE LTP",
            fmt_number(
                option.get(
                    "ltp"
                )
            ),
        )

        d.metric(
            "Lot Size",
            str(
                option["lot_size"]
            ),
        )

        st.write(
            f"**Symbol:** `{option['symbol']}`"
        )

        st.write(
            f"**Expiry:** `{option['expiry']}`"
        )

        st.write(
            f"**Token:** `{option['token']}`"
        )

    st.info(
        st.session_state.auto_trade_status
    )


# ============================================================
# DASHBOARD
# ============================================================

def show_dashboard():

    st.title(
        "📈 NIFTY Automatic BUY CE"
    )

    # --------------------------------------------------------
    # TOP BAR
    # --------------------------------------------------------

    c1, c2, c3, c4 = st.columns(
        4
    )

    with c1:

        if st.button(
            "🔌 Connect Angel One",
            use_container_width=True,
        ):

            try:

                angel_login()

                st.success(
                    "Angel One connected."
                )

                st.rerun()

            except Exception as e:

                st.session_state.login_status = (
                    "FAILED"
                )

                st.session_state.last_error = (
                    str(e)
                )

                st.error(
                    f"Login failed: {e}"
                )

    with c2:

        if st.button(
            "📥 Load Instruments",
            use_container_width=True,
        ):

            try:

                instruments = (
                    load_instruments()
                )

                st.session_state.instruments = (
                    instruments
                )

                st.success(
                    f"Loaded {len(instruments):,} "
                    "instruments."
                )

            except Exception as e:

                st.error(
                    f"Instrument error: {e}"
                )

    with c3:

        st.metric(
            "Connection",
            st.session_state.login_status,
        )

    with c4:

        if LIVE_TRADING:

            st.error(
                "🔴 LIVE TRADING"
            )

        else:

            st.success(
                "🟢 PAPER MODE"
            )

    # --------------------------------------------------------
    # ERROR
    # --------------------------------------------------------

    if st.session_state.last_error:

        st.error(
            st.session_state.last_error
        )

        st.session_state.last_error = ""

    # --------------------------------------------------------
    # AUTO STATUS
    # --------------------------------------------------------

    st.caption(
        st.session_state.auto_trade_status
    )

    # --------------------------------------------------------
    # CONNECTION REQUIRED
    # --------------------------------------------------------

    if st.session_state.api is None:

        st.info(
            "Click **Connect Angel One** "
            "to start automatic trading."
        )

        return

    # --------------------------------------------------------
    # INSTRUMENTS
    # --------------------------------------------------------

    if (
        st.session_state.instruments
        is None
    ):

        try:

            with st.spinner(
                "Loading instrument master..."
            ):

                st.session_state.instruments = (
                    load_instruments()
                )

        except Exception as e:

            st.error(
                f"Instrument master error: {e}"
            )

            return

    # --------------------------------------------------------
    # NIFTY SPOT
    # --------------------------------------------------------

    try:

        spot = get_nifty_ltp()

        st.session_state.spot = (
            spot
        )

    except Exception as e:

        st.error(
            "NIFTY LTP FAILED\n"
            f"{e}"
        )

        return

    # --------------------------------------------------------
    # CANDLES
    # --------------------------------------------------------

    try:

        df5 = get_nifty_candles(
            days=30
        )

        df5 = (
            remove_incomplete_candle(
                df5
            )
        )

        if len(df5) < 100:

            st.warning(
                "Not enough 5-minute "
                "candles."
            )

            return

        (
            st5,
            st15,
            st4h,
        ) = build_timeframes(
            df5
        )

        st.session_state.st5 = (
            st5
        )

        st.session_state.st15 = (
            st15
        )

        st.session_state.st4h = (
            st4h
        )

    except Exception as e:

        st.error(
            "Supertrend calculation failed:\n"
            f"{e}"
        )

        return

    # --------------------------------------------------------
    # SIGNAL
    # --------------------------------------------------------

    signal, signal_time = (
        current_signal(
            st5,
            st15,
            st4h,
        )
    )

    st.session_state.signal = (
        signal
    )

    st.session_state.signal_time = (
        signal_time
    )

    # --------------------------------------------------------
    # AUTOMATIC CE EXECUTION
    # --------------------------------------------------------

    order_submitted = (
        execute_automatic_ce(
            signal,
            signal_time,
            spot,
        )
    )

    # --------------------------------------------------------
    # IMPORTANT
    #
    # If automatic order was submitted,
    # go directly to Order Book.
    # --------------------------------------------------------

    if order_submitted:

        st.session_state.selected_section = (
            "Order Book"
        )

        st.rerun()

    # --------------------------------------------------------
    # METRICS
    # --------------------------------------------------------

    st.divider()

    m1, m2, m3, m4, m5 = st.columns(
        5
    )

    latest5 = st5.iloc[-1]
    latest15 = st15.iloc[-1]
    latest4h = st4h.iloc[-1]

    m1.metric(
        "NIFTY",
        fmt_number(
            spot
        ),
    )

    m2.metric(
        "5 MIN",
        (
            "GREEN"
            if latest5["ST_Green"]
            else "RED"
        ),
    )

    m3.metric(
        "15 MIN",
        (
            "GREEN"
            if latest15["ST_Green"]
            else "RED"
        ),
    )

    m4.metric(
        "4 HOUR",
        (
            "GREEN"
            if latest4h["ST_Green"]
            else "RED"
        ),
    )

    m5.metric(
        "SIGNAL",
        signal,
    )

    # --------------------------------------------------------
    # SIGNAL
    # --------------------------------------------------------

    if signal == "BUY_CE":

        st.success(
            "🟢 CONFIRMED BUY CE SIGNAL"
        )

    else:

        st.info(
            "⏳ WAIT — waiting for a new "
            "5-minute Supertrend GREEN flip "
            "with 15m + 4H confirmation."
        )

    # --------------------------------------------------------
    # AUTOMATIC CE PANEL
    # --------------------------------------------------------

    show_auto_ce_panel()

    # --------------------------------------------------------
    # CHART
    # --------------------------------------------------------

    st.divider()

    st.subheader(
        "📊 NIFTY 5-Minute Supertrend"
    )

    chart_df = st5[
        [
            "timestamp",
            "close",
            "Supertrend",
        ]
    ].copy()

    chart_df = chart_df.tail(
        150
    )

    chart_df = chart_df.set_index(
        "timestamp"
    )

    st.line_chart(
        chart_df,
        use_container_width=True,
    )

    # --------------------------------------------------------
    # CANDLES
    # --------------------------------------------------------

    with st.expander(
        "Latest 5-Minute Candles"
    ):

        st.dataframe(
            st5.tail(50),
            use_container_width=True,
            hide_index=True,
        )


# ============================================================
# POSITIONS
# ============================================================

def show_positions():

    st.title(
        "📊 NIFTY Positions"
    )

    api = st.session_state.api

    if api is None:

        st.warning(
            "Connect Angel One first."
        )

        return

    try:

        response = api.position()

        if not response:

            st.info(
                "No position response."
            )

            return

        if not response.get(
            "status"
        ):

            st.error(
                str(response)
            )

            return

        positions = (
            response.get("data")
            or []
        )

        st.session_state.positions = (
            positions
        )

        if not positions:

            st.info(
                "No open positions."
            )

            return

        df = pd.DataFrame(
            positions
        )

        st.dataframe(
            df,
            use_container_width=True,
            hide_index=True,
        )

    except Exception as e:

        st.error(
            f"Positions failed: {e}"
        )


# ============================================================
# SIDEBAR
# ============================================================

st.sidebar.title(
    "📈 NIFTY AUTO CE"
)

st.sidebar.caption(
    "Supertrend 20,2"
)

st.sidebar.caption(
    "5m + 15m + 4H"
)

st.sidebar.divider()

# ------------------------------------------------------------
# AUTOMATION STATUS
# ------------------------------------------------------------

if AUTO_TRADE and AUTO_TRADE_CE:

    st.sidebar.success(
        "🤖 AUTO BUY CE ENABLED"
    )

else:

    st.sidebar.warning(
        "Automatic CE disabled"
    )

# ------------------------------------------------------------
# LIVE MODE
# ------------------------------------------------------------

if LIVE_TRADING:

    st.sidebar.error(
        "🔴 LIVE TRADING ENABLED"
    )

    st.sidebar.warning(
        "Real NIFTY CE orders can be submitted."
    )

else:

    st.sidebar.success(
        "🟢 PAPER MODE"
    )

    st.sidebar.info(
        "No real broker order will be sent."
    )

# ------------------------------------------------------------
# NAVIGATION
# ------------------------------------------------------------

sections = [
    "Dashboard",
    "Order Book",
    "Positions",
]

current = (
    st.session_state.selected_section
)

if current not in sections:
    current = "Dashboard"

section = st.sidebar.radio(
    "Open",
    sections,
    index=sections.index(
        current
    ),
)

st.session_state.selected_section = (
    section
)


# ============================================================
# ROUTING
# ============================================================

if section == "Dashboard":

    show_dashboard()

elif section == "Order Book":

    show_order_book()

elif section == "Positions":

    show_positions()


# ============================================================
# FOOTER
# ============================================================

st.sidebar.divider()

st.sidebar.caption(
    "NIFTY 50"
)

st.sidebar.caption(
    "Automatic BUY CE only"
)

st.sidebar.caption(
    "ATM / nearest valid expiry"
)

st.sidebar.caption(
    "Supertrend 20,2"
)

st.sidebar.caption(
    "5m + 15m + 4H"
)

st.sidebar.caption(
    "Updated: "
    + now_ist().strftime(
        "%d-%m-%Y %H:%M:%S"
    )
)


# ============================================================
# AUTOMATIC REFRESH
# ============================================================

if (
    section == "Dashboard"
    and AUTO_REFRESH_SECONDS > 0
):

    time.sleep(
        AUTO_REFRESH_SECONDS
    )

    st.rerun()
