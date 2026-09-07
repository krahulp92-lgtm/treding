
# ============================================================
# dashboard.py
#
# NIFTY LIVE SUPERTREND 20,2
# ANGEL ONE SMARTAPI
#
# FEATURES
# ------------------------------------------------------------
# 1. Angel One login
# 2. Local instrument-master caching
# 3. Automatic daily instrument-master refresh
# 4. Fallback to previous local instrument master
# 5. NIFTY live LTP
# 6. 5-minute candles
# 7. Supertrend 20,2
# 8. 15-minute confirmation
# 9. 4-hour confirmation
# 10. BUY ATM CE on bullish signal
# 11. BUY ATM PE on bearish signal
# 12. Manual BUY button
# 13. Optional automatic order execution
# 14. Duplicate-order protection
# 15. Order-book duplicate checking
# 16. Position display
# 17. Order ID tracking
#
# IMPORTANT
# ------------------------------------------------------------
# LIVE_TRADING = False means NO REAL ORDER is sent.
#
# Set LIVE_TRADING = True only after you have tested:
# - API login
# - NIFTY LTP
# - candles
# - Supertrend
# - option symbol
# - token
# - expiry
# - strike
# - lot size
# - option LTP
# - order book
# ============================================================


import os
import json
import time
from pathlib import Path
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo
from urllib.parse import urlparse, parse_qs

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
    page_title="NIFTY Live Supertrend Trading",
    page_icon="📈",
    layout="wide"
)


# ============================================================
# CONFIGURATION
# ============================================================

IST = ZoneInfo("Asia/Kolkata")

# ------------------------------------------------------------
# SAFETY
# ------------------------------------------------------------

# False = no real order
# True  = real order
LIVE_TRADING = True

# ------------------------------------------------------------
# STRATEGY
# ------------------------------------------------------------

ST_PERIOD = 20
ST_MULTIPLIER = 2.0

CANDLE_INTERVAL = "FIVE_MINUTE"

# NIFTY 50 index token
NIFTY_TOKEN = "99926000"

# NIFTY exchange/symbol used for LTP
NIFTY_EXCHANGE = "NSE"
NIFTY_SYMBOL = "NIFTY"

# ------------------------------------------------------------
# AUTO TRADING
# ------------------------------------------------------------

# False = user clicks BUY button
# True  = dashboard can automatically send order
AUTO_TRADE = False

# ------------------------------------------------------------
# DUPLICATE PROTECTION
# ------------------------------------------------------------

DUPLICATE_PROTECTION = True

# Do not buy the same signal twice on the same day
ONE_ORDER_PER_SIGNAL_PER_DAY = True

# ------------------------------------------------------------
# FILES
# ------------------------------------------------------------

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)

INSTRUMENT_FILE = DATA_DIR / "OpenAPIScripMaster.json"
STATE_FILE = DATA_DIR / "trading_state.json"

# ------------------------------------------------------------
# CURRENT OFFICIAL SMARTAPI INSTRUMENT URL
# ------------------------------------------------------------

INSTRUMENT_MASTER_URL = (
    "https://margincalculator.angelone.in/"
    "OpenAPI_File/files/OpenAPIScripMaster.json"
)

# ------------------------------------------------------------
# CACHE
# ------------------------------------------------------------

INSTRUMENT_REFRESH_HOURS = 24

# ------------------------------------------------------------
# HISTORICAL DATA
# ------------------------------------------------------------

# 30 days gives enough history for 20-period
# Supertrend on 4-hour timeframe.
HISTORY_DAYS = 30


# ============================================================
# ENVIRONMENT VARIABLES
# ============================================================

ANGEL_API_KEY = os.getenv(
    "ANGEL_API_KEY",
    ""
).strip()

ANGEL_CLIENT_ID = os.getenv(
    "ANGEL_CLIENT_ID",
    ""
).strip()

ANGEL_PASSWORD = os.getenv(
    "ANGEL_PASSWORD",
    ""
).strip()

ANGEL_TOTP_SECRET = os.getenv(
    "ANGEL_TOTP_SECRET",
    ""
).strip()


# ============================================================
# SESSION STATE
# ============================================================

DEFAULTS = {
    "api": None,
    "logged_in": False,

    "login_message": "",
    "last_error": "",

    "instruments": None,
    "instruments_source": "",

    "spot": None,

    "candles": None,

    "st5": None,
    "st15": None,
    "st4h": None,

    "signal": "WAIT",

    "option_symbol": None,
    "option_token": None,
    "option_type": None,
    "option_expiry": None,
    "option_strike": None,
    "option_lot_size": None,
    "option_ltp": None,

    "last_order_id": None,
    "last_order_symbol": None,
    "last_order_signal": None,
    "last_order_time": None,

    "auto_trade_last_signal": None,
    "auto_trade_last_date": None,

    "refresh_count": 0,
}

for key, value in DEFAULTS.items():

    if key not in st.session_state:
        st.session_state[key] = value


# ============================================================
# BASIC UTILITIES
# ============================================================

def now_ist():
    return datetime.now(IST)


def today_ist():
    return now_ist().date()


def market_is_open():

    now = now_ist()

    if now.weekday() >= 5:
        return False

    start = now.replace(
        hour=9,
        minute=15,
        second=0,
        microsecond=0
    )

    end = now.replace(
        hour=15,
        minute=30,
        second=0,
        microsecond=0
    )

    return start <= now <= end


def market_status_text():

    if market_is_open():
        return "OPEN"

    return "CLOSED"


# ============================================================
# TOTP
# ============================================================

def get_totp_secret(raw_secret):

    raw_secret = raw_secret.strip()

    if raw_secret.startswith(
        "otpauth://"
    ):

        parsed = urlparse(
            raw_secret
        )

        params = parse_qs(
            parsed.query
        )

        secret = params.get(
            "secret",
            [""]
        )[0]

        return secret.strip()

    return raw_secret


# ============================================================
# ANGEL LOGIN
# ============================================================

def login_angel_one():

    if not ANGEL_API_KEY:

        raise RuntimeError(
            "ANGEL_API_KEY is missing"
        )

    if not ANGEL_CLIENT_ID:

        raise RuntimeError(
            "ANGEL_CLIENT_ID is missing"
        )

    if not ANGEL_PASSWORD:

        raise RuntimeError(
            "ANGEL_PASSWORD is missing"
        )

    if not ANGEL_TOTP_SECRET:

        raise RuntimeError(
            "ANGEL_TOTP_SECRET is missing"
        )

    secret = get_totp_secret(
        ANGEL_TOTP_SECRET
    )

    try:

        totp = pyotp.TOTP(
            secret
        ).now()

    except Exception as e:

        raise RuntimeError(
            f"TOTP generation failed: {e}"
        )

    try:

        api = SmartConnect(
            api_key=ANGEL_API_KEY
        )

        response = api.generateSession(
            ANGEL_CLIENT_ID,
            ANGEL_PASSWORD,
            totp
        )

    except Exception as e:

        raise RuntimeError(
            f"Angel One login request failed: {e}"
        )

    if not response:

        raise RuntimeError(
            "Angel One returned an empty login response"
        )

    if response.get("status") is not True:

        raise RuntimeError(
            "ANGEL LOGIN FAILED | "
            f"message={response.get('message')} | "
            f"errorcode={response.get('errorcode')}"
        )

    st.session_state.api = api
    st.session_state.logged_in = True

    st.session_state.login_message = (
        "Angel One connected successfully"
    )

    return response


# ============================================================
# INSTRUMENT MASTER HELPERS
# ============================================================

def instrument_file_is_fresh():

    if not INSTRUMENT_FILE.exists():
        return False

    modified_time = datetime.fromtimestamp(
        INSTRUMENT_FILE.stat().st_mtime
    )

    age_hours = (
        datetime.now() - modified_time
    ).total_seconds() / 3600

    return age_hours < INSTRUMENT_REFRESH_HOURS


def validate_instrument_master(data):

    if not isinstance(data, list):

        raise RuntimeError(
            "Instrument master is not a list"
        )

    if len(data) < 1000:

        raise RuntimeError(
            "Instrument master appears incomplete"
        )

    # Check a few important fields
    sample = data[:20]

    valid_rows = 0

    for row in sample:

        if (
            isinstance(row, dict)
            and "token" in row
            and "symbol" in row
            and "exch_seg" in row
        ):

            valid_rows += 1

    if valid_rows == 0:

        raise RuntimeError(
            "Instrument master has invalid structure"
        )

    return True


def load_local_instruments():

    if not INSTRUMENT_FILE.exists():

        raise FileNotFoundError(
            "Local instrument master does not exist"
        )

    try:

        with open(
            INSTRUMENT_FILE,
            "r",
            encoding="utf-8"
        ) as f:

            data = json.load(f)

    except Exception as e:

        raise RuntimeError(
            f"Local instrument master read failed: {e}"
        )

    validate_instrument_master(data)

    return data


def download_instrument_master():

    headers = {
        "User-Agent": (
            "Mozilla/5.0 "
            "(Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 "
            "(KHTML, like Gecko) "
            "Chrome/139 Safari/537.36"
        ),
        "Accept": "application/json,text/plain,*/*",
        "Connection": "keep-alive",
    }

    last_error = None

    # Try more than once because the master file is large
    # and Angel One has historically had intermittent
    # instrument-master availability issues.
    for attempt in range(1, 4):

        try:

            response = requests.get(
                INSTRUMENT_MASTER_URL,
                headers=headers,
                timeout=120
            )

            response.raise_for_status()

            data = response.json()

            validate_instrument_master(data)

            # Atomic-ish write:
            temp_file = INSTRUMENT_FILE.with_suffix(
                ".tmp"
            )

            with open(
                temp_file,
                "w",
                encoding="utf-8"
            ) as f:

                json.dump(
                    data,
                    f,
                    separators=(",", ":")
                )

            temp_file.replace(
                INSTRUMENT_FILE
            )

            return data

        except Exception as e:

            last_error = e

            if attempt < 3:
                time.sleep(3)

    raise RuntimeError(
        f"Instrument master download failed after "
        f"3 attempts: {last_error}"
    )


def get_instruments():

    # --------------------------------------------------------
    # Use fresh local cache
    # --------------------------------------------------------

    if instrument_file_is_fresh():

        try:

            data = load_local_instruments()

            st.session_state.instruments_source = (
                "LOCAL CACHE"
            )

            return data

        except Exception:
            pass

    # --------------------------------------------------------
    # Try current Angel One master
    # --------------------------------------------------------

    try:

        data = download_instrument_master()

        st.session_state.instruments_source = (
            "ANGEL ONE → LOCAL CACHE"
        )

        return data

    except Exception as download_error:

        # ----------------------------------------------------
        # Fallback to old local copy
        # ----------------------------------------------------

        try:

            data = load_local_instruments()

            st.session_state.instruments_source = (
                "OLD LOCAL CACHE"
            )

            st.warning(
                "Angel One instrument master could not "
                "be refreshed. Using the previous local copy.\n\n"
                f"Download error: {download_error}"
            )

            return data

        except Exception as local_error:

            raise RuntimeError(
                "Instrument master unavailable.\n\n"
                f"Angel One download error:\n"
                f"{download_error}\n\n"
                f"Local cache error:\n"
                f"{local_error}"
            )


# ============================================================
# NIFTY LTP
# ============================================================

def get_nifty_ltp():

    api = st.session_state.api

    if api is None:

        raise RuntimeError(
            "SmartAPI is not connected"
        )

    try:

        response = api.ltpData(
            NIFTY_EXCHANGE,
            NIFTY_SYMBOL,
            NIFTY_TOKEN
        )

    except Exception as e:

        raise RuntimeError(
            f"NIFTY LTP FAILED: {e}"
        )

    if not response:

        raise RuntimeError(
            "NIFTY LTP FAILED: Empty response"
        )

    if response.get("status") is not True:

        raise RuntimeError(
            "NIFTY LTP FAILED | "
            f"message={response.get('message')} | "
            f"errorcode={response.get('errorcode')}"
        )

    data = response.get("data")

    if not data:

        raise RuntimeError(
            "NIFTY LTP FAILED: data missing"
        )

    ltp = data.get("ltp")

    if ltp is None:

        raise RuntimeError(
            "NIFTY LTP FAILED: ltp missing"
        )

    return float(ltp)


# ============================================================
# HISTORICAL CANDLES
# ============================================================

def get_nifty_candles(days=HISTORY_DAYS):

    api = st.session_state.api

    if api is None:

        raise RuntimeError(
            "SmartAPI is not connected"
        )

    end = now_ist()

    start = end - timedelta(
        days=days
    )

    params = {
        "exchange": "NSE",
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
            f"Candle API failed: {e}"
        )

    if not response:

        raise RuntimeError(
            "Candle API failed: Empty response"
        )

    if response.get("status") is not True:

        raise RuntimeError(
            "Candle API failed | "
            f"message={response.get('message')} | "
            f"errorcode={response.get('errorcode')}"
        )

    rows = response.get("data")

    if not rows:

        raise RuntimeError(
            "Candle API returned no data"
        )

    df = pd.DataFrame(
        rows,
        columns=[
            "datetime",
            "open",
            "high",
            "low",
            "close",
            "volume"
        ]
    )

    df["datetime"] = pd.to_datetime(
        df["datetime"],
        errors="coerce"
    )

    for column in [
        "open",
        "high",
        "low",
        "close",
        "volume"
    ]:

        df[column] = pd.to_numeric(
            df[column],
            errors="coerce"
        )

    df = df.dropna()

    df = df.sort_values(
        "datetime"
    )

    df = df.drop_duplicates(
        subset=["datetime"]
    )

    return df.reset_index(
        drop=True
    )


# ============================================================
# REMOVE INCOMPLETE CURRENT CANDLE
# ============================================================

def remove_incomplete_candle(df):

    if df.empty:
        return df

    x = df.copy()

    last_time = pd.Timestamp(
        x.iloc[-1]["datetime"]
    )

    # SmartAPI timestamps represent candle start.
    # A 5-minute candle is complete after its end time.
    now = pd.Timestamp(
        now_ist()
    ).tz_localize(None)

    last_time_naive = (
        last_time.tz_localize(None)
        if last_time.tzinfo is not None
        else last_time
    )

    candle_end = (
        last_time_naive
        + pd.Timedelta(minutes=5)
    )

    if candle_end > now:

        x = x.iloc[:-1]

    return x.reset_index(
        drop=True
    )


# ============================================================
# SUPERTREND
# ============================================================

def calculate_supertrend(
    df,
    period=20,
    multiplier=2.0
):

    x = df.copy()

    high = x["high"]
    low = x["low"]
    close = x["close"]

    previous_close = close.shift(1)

    tr1 = high - low

    tr2 = (
        high - previous_close
    ).abs()

    tr3 = (
        low - previous_close
    ).abs()

    true_range = pd.concat(
        [tr1, tr2, tr3],
        axis=1
    ).max(axis=1)

    atr = true_range.ewm(
        alpha=1 / period,
        adjust=False
    ).mean()

    hl2 = (
        high + low
    ) / 2

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
        index=x.index,
        dtype=float
    )

    final_lower = pd.Series(
        np.nan,
        index=x.index,
        dtype=float
    )

    supertrend = pd.Series(
        np.nan,
        index=x.index,
        dtype=float
    )

    direction = pd.Series(
        0,
        index=x.index,
        dtype=int
    )

    for i in range(len(x)):

        if i == 0:

            final_upper.iloc[i] = (
                basic_upper.iloc[i]
            )

            final_lower.iloc[i] = (
                basic_lower.iloc[i]
            )

            continue

        if (
            basic_upper.iloc[i]
            < final_upper.iloc[i - 1]
            or
            close.iloc[i - 1]
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
            basic_lower.iloc[i]
            > final_lower.iloc[i - 1]
            or
            close.iloc[i - 1]
            < final_lower.iloc[i - 1]
        ):

            final_lower.iloc[i] = (
                basic_lower.iloc[i]
            )

        else:

            final_lower.iloc[i] = (
                final_lower.iloc[i - 1]
            )

        if i == 1:

            if close.iloc[i] <= final_upper.iloc[i]:

                direction.iloc[i] = -1

                supertrend.iloc[i] = (
                    final_upper.iloc[i]
                )

            else:

                direction.iloc[i] = 1

                supertrend.iloc[i] = (
                    final_lower.iloc[i]
                )

        elif (
            supertrend.iloc[i - 1]
            == final_upper.iloc[i - 1]
        ):

            if close.iloc[i] <= final_upper.iloc[i]:

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

            if close.iloc[i] >= final_lower.iloc[i]:

                direction.iloc[i] = 1

                supertrend.iloc[i] = (
                    final_lower.iloc[i]
                )

            else:

                direction.iloc[i] = -1

                supertrend.iloc[i] = (
                    final_upper.iloc[i]
                )

    x["ATR"] = atr
    x["Basic_Upper"] = basic_upper
    x["Basic_Lower"] = basic_lower
    x["Final_Upper"] = final_upper
    x["Final_Lower"] = final_lower
    x["Supertrend"] = supertrend
    x["Direction"] = direction

    x["ST_Green"] = (
        x["Direction"] == 1
    )

    x["ST_Red"] = (
        x["Direction"] == -1
    )

    x["ST_Flip_Green"] = (
        (x["Direction"] == 1)
        &
        (x["Direction"].shift(1) == -1)
    )

    x["ST_Flip_Red"] = (
        (x["Direction"] == -1)
        &
        (x["Direction"].shift(1) == 1)
    )

    return x


# ============================================================
# RESAMPLE
# ============================================================

def resample_ohlcv(
    df,
    rule
):

    x = df.copy()

    x["datetime"] = pd.to_datetime(
        x["datetime"]
    )

    x = x.set_index(
        "datetime"
    )

    result = x.resample(
        rule,
        origin="start_day",
        offset="9h15min",
        label="right",
        closed="left"
    ).agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum"
    })

    result = result.dropna()

    return result.reset_index()


# ============================================================
# BUILD TIMEFRAMES
# ============================================================

def build_timeframes(df5):

    st5 = calculate_supertrend(
        df5,
        ST_PERIOD,
        ST_MULTIPLIER
    )

    df15 = resample_ohlcv(
        df5,
        "15min"
    )

    df4h = resample_ohlcv(
        df5,
        "4h"
    )

    st15 = calculate_supertrend(
        df15,
        ST_PERIOD,
        ST_MULTIPLIER
    )

    st4h = calculate_supertrend(
        df4h,
        ST_PERIOD,
        ST_MULTIPLIER
    )

    return (
        st5,
        st15,
        st4h
    )


# ============================================================
# CURRENT SIGNAL
# ============================================================

def current_signal(
    st5,
    st15,
    st4h
):

    if len(st5) < 2:
        return "WAIT"

    if len(st15) < 2:
        return "WAIT"

    if len(st4h) < 2:
        return "WAIT"

    five = st5.iloc[-1]

    fifteen = st15.iloc[-1]

    four_hour = st4h.iloc[-1]

    # --------------------------------------------------------
    # BUY CE
    # --------------------------------------------------------

    if (
        bool(five["ST_Flip_Green"])
        and
        bool(fifteen["ST_Green"])
        and
        bool(four_hour["ST_Green"])
    ):

        return "BUY_CE"

    # --------------------------------------------------------
    # BUY PE
    # --------------------------------------------------------

    if (
        bool(five["ST_Flip_Red"])
        and
        bool(fifteen["ST_Red"])
        and
        bool(four_hour["ST_Red"])
    ):

        return "BUY_PE"

    return "WAIT"


# ============================================================
# OPTION EXPIRY PARSER
# ============================================================

def parse_expiry(value):

    if value is None:
        return None

    value = str(value).strip()

    if not value:
        return None

    formats = [
        "%d%b%Y",
        "%d%b%y",
        "%Y-%m-%d",
        "%d-%b-%Y",
    ]

    for fmt in formats:

        try:

            return datetime.strptime(
                value.upper(),
                fmt
            ).date()

        except Exception:
            continue

    return None


# ============================================================
# SELECT ATM OPTION
# ============================================================

def select_atm_option(
    instruments,
    spot,
    option_type
):

    rows = []

    for item in instruments:

        if not isinstance(
            item,
            dict
        ):
            continue

        exchange = str(
            item.get(
                "exch_seg",
                ""
            )
        ).upper()

        if exchange != "NFO":
            continue

        instrument_type = str(
            item.get(
                "instrumenttype",
                ""
            )
        ).upper()

        if instrument_type != "OPTIDX":
            continue

        name = str(
            item.get(
                "name",
                ""
            )
        ).upper()

        if name != "NIFTY":
            continue

        symbol = str(
            item.get(
                "symbol",
                ""
            )
        ).upper()

        if not symbol.endswith(
            option_type
        ):
            continue

        expiry = parse_expiry(
            item.get("expiry")
        )

        if expiry is None:
            continue

        if expiry < today_ist():
            continue

        try:

            strike_raw = float(
                item.get("strike")
            )

            # Angel One instrument master
            # represents option strikes in
            # scaled form.
            strike = strike_raw / 100.0

        except Exception:

            continue

        try:

            lot_size = int(
                float(
                    item.get(
                        "lotsize"
                    )
                )
            )

        except Exception:

            continue

        token = str(
            item.get(
                "token",
                ""
            )
        ).strip()

        if not token:
            continue

        rows.append({
            "symbol": symbol,
            "token": token,
            "expiry": expiry,
            "strike": strike,
            "lot_size": lot_size,
        })

    if not rows:

        raise RuntimeError(
            f"No NIFTY {option_type} options found"
        )

    df = pd.DataFrame(
        rows
    )

    nearest_expiry = (
        df["expiry"].min()
    )

    df = df[
        df["expiry"]
        == nearest_expiry
    ].copy()

    df["distance"] = (
        df["strike"]
        - float(spot)
    ).abs()

    df = df.sort_values(
        [
            "distance",
            "strike"
        ]
    )

    if df.empty:

        raise RuntimeError(
            f"No ATM {option_type} found"
        )

    return df.iloc[0].to_dict()


# ============================================================
# OPTION LTP
# ============================================================

def get_option_ltp(
    option
):

    api = st.session_state.api

    if api is None:

        raise RuntimeError(
            "SmartAPI not connected"
        )

    try:

        response = api.ltpData(
            "NFO",
            option["symbol"],
            str(option["token"])
        )

    except Exception as e:

        raise RuntimeError(
            f"Option LTP failed: {e}"
        )

    if not response:

        raise RuntimeError(
            "Option LTP returned empty response"
        )

    if response.get("status") is not True:

        raise RuntimeError(
            "Option LTP failed | "
            f"message={response.get('message')} | "
            f"errorcode={response.get('errorcode')}"
        )

    data = response.get(
        "data"
    )

    if not data:

        raise RuntimeError(
            "Option LTP data missing"
        )

    ltp = data.get(
        "ltp"
    )

    if ltp is None:

        raise RuntimeError(
            "Option LTP missing"
        )

    return float(ltp)


# ============================================================
# LOAD STATE
# ============================================================

def load_state():

    if not STATE_FILE.exists():

        return {
            "orders": []
        }

    try:

        with open(
            STATE_FILE,
            "r",
            encoding="utf-8"
        ) as f:

            data = json.load(f)

        if not isinstance(
            data,
            dict
        ):

            return {
                "orders": []
            }

        if "orders" not in data:

            data["orders"] = []

        return data

    except Exception:

        return {
            "orders": []
        }


# ============================================================
# SAVE STATE
# ============================================================

def save_state(state):

    temp = STATE_FILE.with_suffix(
        ".tmp"
    )

    with open(
        temp,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            state,
            f,
            indent=2,
            default=str
        )

    temp.replace(
        STATE_FILE
    )


# ============================================================
# RECORD ORDER
# ============================================================

def record_order(
    signal,
    option,
    order_id,
    paper=False
):

    state = load_state()

    record = {
        "date": str(
            today_ist()
        ),
        "time": str(
            now_ist()
        ),
        "signal": signal,
        "symbol": option["symbol"],
        "token": str(
            option["token"]
        ),
        "strike": float(
            option["strike"]
        ),
        "expiry": str(
            option["expiry"]
        ),
        "lot_size": int(
            option["lot_size"]
        ),
        "order_id": order_id,
        "paper": paper,
    }

    state["orders"].append(
        record
    )

    # Keep latest 100 records
    state["orders"] = (
        state["orders"][-100:]
    )

    save_state(
        state
    )


# ============================================================
# LOCAL DUPLICATE CHECK
# ============================================================

def local_duplicate_exists(
    signal,
    option
):

    state = load_state()

    today = str(
        today_ist()
    )

    for order in state.get(
        "orders",
        []
    ):

        if str(
            order.get("date")
        ) != today:

            continue

        if str(
            order.get("signal")
        ) != signal:

            continue

        if str(
            order.get("symbol")
        ) != option["symbol"]:

            continue

        # Ignore paper trades when
        # checking real duplicate orders.
        if order.get(
            "paper",
            False
        ):

            continue

        return True

    return False


# ============================================================
# CHECK ANGEL ORDER BOOK FOR DUPLICATE
# ============================================================

def orderbook_duplicate_exists(
    signal,
    option
):

    api = st.session_state.api

    if api is None:
        return False

    try:

        response = api.orderBook()

    except Exception:

        # Do not block an order solely because
        # order book retrieval failed.
        return False

    if not response:
        return False

    if response.get(
        "status"
    ) is not True:

        return False

    orders = response.get(
        "data",
        []
    )

    today_string = (
        today_ist().strftime(
            "%Y-%m-%d"
        )
    )

    for order in orders:

        symbol = str(
            order.get(
                "tradingsymbol",
                ""
            )
        ).upper()

        transaction = str(
            order.get(
                "transactiontype",
                ""
            )
        ).upper()

        status = str(
            order.get(
                "status",
                ""
            )
        ).upper()

        order_date = str(
            order.get(
                "updatetime",
                ""
            )
        )

        # Only BUY orders for same option
        if symbol != str(
            option["symbol"]
        ).upper():

            continue

        if transaction != "BUY":
            continue

        # Do not treat rejected/cancelled
        # orders as successful duplicates.
        bad_statuses = {
            "REJECTED",
            "CANCELLED",
            "CANCELED",
            "FAILED",
        }

        if status in bad_statuses:
            continue

        # If today's date appears in update time,
        # consider it a duplicate.
        if today_string in order_date:

            return True

    return False


# ============================================================
# DUPLICATE PROTECTION
# ============================================================

def duplicate_order_exists(
    signal,
    option
):

    if not DUPLICATE_PROTECTION:
        return False

    if local_duplicate_exists(
        signal,
        option
    ):

        return True

    if orderbook_duplicate_exists(
        signal,
        option
    ):

        return True

    return False


# ============================================================
# PLACE BUY ORDER
# ============================================================

def place_buy_order(
    signal,
    option
):

    # --------------------------------------------------------
    # MARKET CHECK
    # --------------------------------------------------------

    if not market_is_open():

        raise RuntimeError(
            "Market is closed. "
            "No order was sent."
        )

    # --------------------------------------------------------
    # DUPLICATE CHECK
    # --------------------------------------------------------

    if duplicate_order_exists(
        signal,
        option
    ):

        raise RuntimeError(
            "DUPLICATE ORDER BLOCKED.\n\n"
            f"Signal: {signal}\n"
            f"Symbol: {option['symbol']}\n\n"
            "An order for this signal/option "
            "already exists today."
        )

    quantity = int(
        option["lot_size"]
    )

    # --------------------------------------------------------
    # PAPER MODE
    # --------------------------------------------------------

    if not LIVE_TRADING:

        fake_id = (
            "PAPER-"
            + now_ist().strftime(
                "%Y%m%d%H%M%S"
            )
        )

        record_order(
            signal,
            option,
            fake_id,
            paper=True
        )

        return {
            "status": True,
            "paper": True,
            "order_id": fake_id,
            "message": (
                "LIVE_TRADING=False. "
                "No real Angel One order was sent."
            ),
        }

    # --------------------------------------------------------
    # REAL ORDER
    # --------------------------------------------------------

    api = st.session_state.api

    if api is None:

        raise RuntimeError(
            "SmartAPI not connected"
        )

    order_params = {
        "variety": "NORMAL",
        "tradingsymbol": str(
            option["symbol"]
        ),
        "symboltoken": str(
            option["token"]
        ),
        "transactiontype": "BUY",
        "exchange": "NFO",
        "ordertype": "MARKET",
        "producttype": "INTRADAY",
        "duration": "DAY",
        "price": "0",
        "squareoff": "0",
        "stoploss": "0",
        "quantity": str(
            quantity
        ),
        "ordertag": (
            "NST202"
        ),
    }

    try:

        response = api.placeOrder(
            order_params
        )

    except Exception as e:

        raise RuntimeError(
            f"ORDER API FAILED: {e}"
        )

    if not response:

        raise RuntimeError(
            "ORDER API FAILED: Empty response"
        )

    if isinstance(
        response,
        str
    ):

        order_id = response

        record_order(
            signal,
            option,
            order_id,
            paper=False
        )

        return {
            "status": True,
            "paper": False,
            "order_id": order_id,
        }

    if response.get(
        "status"
    ) is not True:

        raise RuntimeError(
            "ORDER FAILED | "
            f"message={response.get('message')} | "
            f"errorcode={response.get('errorcode')}"
        )

    data = response.get(
        "data"
    ) or {}

    order_id = (
        data.get("orderid")
        or data.get("order_id")
    )

    if not order_id:

        raise RuntimeError(
            "Angel One returned success but "
            "no order ID was found."
        )

    record_order(
        signal,
        option,
        order_id,
        paper=False
    )

    return {
        "status": True,
        "paper": False,
        "order_id": order_id,
        "response": response,
    }


# ============================================================
# POSITIONS
# ============================================================

def get_positions():

    api = st.session_state.api

    if api is None:
        return []

    try:

        response = api.position()

        if (
            response
            and
            response.get("status")
        ):

            return response.get(
                "data",
                []
            )

    except Exception:
        pass

    return []


# ============================================================
# ORDER BOOK
# ============================================================

def get_order_book():

    api = st.session_state.api

    if api is None:
        return []

    try:

        response = api.orderBook()

        if (
            response
            and
            response.get("status")
        ):

            return response.get(
                "data",
                []
            )

    except Exception:
        pass

    return []


# ============================================================
# ORDER EXECUTION HELPER
# ============================================================

def execute_signal_order(
    signal,
    option
):

    result = place_buy_order(
        signal,
        option
    )

    st.session_state.last_order_id = (
        result.get("order_id")
    )

    st.session_state.last_order_symbol = (
        option["symbol"]
    )

    st.session_state.last_order_signal = (
        signal
    )

    st.session_state.last_order_time = (
        str(now_ist())
    )

    return result


# ============================================================
# HEADER
# ============================================================

st.title(
    "📈 NIFTY Live Supertrend 20,2"
)

st.caption(
    "Angel One SmartAPI | BUY ATM CE / PE"
)


# ============================================================
# SIDEBAR
# ============================================================

with st.sidebar:

    st.header(
        "⚙️ Trading Controls"
    )

    if LIVE_TRADING:

        st.error(
            "🔴 LIVE TRADING = ON"
        )

    else:

        st.success(
            "🟢 LIVE TRADING = OFF"
        )

    st.write(
        "Auto Trade:",
        "ON" if AUTO_TRADE else "OFF"
    )

    st.write(
        "Duplicate Protection:",
        "ON" if DUPLICATE_PROTECTION else "OFF"
    )

    st.divider()

    st.write(
        f"Market: **{market_status_text()}**"
    )

    st.write(
        f"Strategy: **Supertrend {ST_PERIOD},{ST_MULTIPLIER:g}**"
    )

    st.divider()

    if st.button(
        "🔐 Connect Angel One",
        use_container_width=True
    ):

        try:

            login_angel_one()

            st.success(
                "Angel One connected"
            )

        except Exception as e:

            st.session_state.last_error = (
                str(e)
            )

            st.error(
                st.session_state.last_error
            )

    if st.session_state.logged_in:

        st.success(
            "API: CONNECTED"
        )

    else:

        st.warning(
            "API: NOT CONNECTED"
        )


# ============================================================
# STOP IF NOT CONNECTED
# ============================================================

if not st.session_state.logged_in:

    st.info(
        "Click **Connect Angel One** to continue."
    )

    st.stop()


# ============================================================
# INSTRUMENT MASTER
# ============================================================

if st.session_state.instruments is None:

    try:

        with st.spinner(
            "Loading instrument master..."
        ):

            st.session_state.instruments = (
                get_instruments()
            )

    except Exception as e:

        st.error(
            str(e)
        )

        st.stop()


# ============================================================
# INSTRUMENT STATUS
# ============================================================

with st.expander(
    "Instrument Master Status"
):

    st.write(
        "Source:",
        st.session_state.instruments_source
    )

    if INSTRUMENT_FILE.exists():

        modified = datetime.fromtimestamp(
            INSTRUMENT_FILE.stat().st_mtime
        )

        st.write(
            "Local file:",
            str(INSTRUMENT_FILE)
        )

        st.write(
            "Last modified:",
            modified.strftime(
                "%Y-%m-%d %H:%M:%S"
            )
        )

        st.write(
            "Number of instruments:",
            f"{len(st.session_state.instruments):,}"
        )

    else:

        st.warning(
            "Local instrument file does not exist."
        )


# ============================================================
# NIFTY SPOT
# ============================================================

try:

    spot = get_nifty_ltp()

    st.session_state.spot = spot

except Exception as e:

    st.error(
        str(e)
    )

    st.stop()


# ============================================================
# HISTORICAL CANDLES
# ============================================================

try:

    with st.spinner(
        "Loading NIFTY candles..."
    ):

        candles = get_nifty_candles(
            HISTORY_DAYS
        )

        candles = remove_incomplete_candle(
            candles
        )

        st.session_state.candles = candles

except Exception as e:

    st.error(
        str(e)
    )

    st.stop()


# ============================================================
# BUILD SUPERTRENDS
# ============================================================

try:

    st5, st15, st4h = (
        build_timeframes(
            candles
        )
    )

except Exception as e:

    st.error(
        f"Supertrend calculation failed: {e}"
    )

    st.stop()


# ============================================================
# SIGNAL
# ============================================================

signal = current_signal(
    st5,
    st15,
    st4h
)

st.session_state.signal = signal


# ============================================================
# STORE CURRENT ST STATES
# ============================================================

st.session_state.st5 = bool(
    st5.iloc[-1]["ST_Green"]
)

st.session_state.st15 = bool(
    st15.iloc[-1]["ST_Green"]
)

st.session_state.st4h = bool(
    st4h.iloc[-1]["ST_Green"]
)


# ============================================================
# TOP METRICS
# ============================================================

st.divider()

c1, c2, c3, c4, c5 = st.columns(5)

c1.metric(
    "NIFTY",
    f"{spot:,.2f}"
)

c2.metric(
    "5M ST",
    "🟢 GREEN"
    if st.session_state.st5
    else "🔴 RED"
)

c3.metric(
    "15M ST",
    "🟢 GREEN"
    if st.session_state.st15
    else "🔴 RED"
)

c4.metric(
    "4H ST",
    "🟢 GREEN"
    if st.session_state.st4h
    else "🔴 RED"
)

c5.metric(
    "Signal",
    signal
)


# ============================================================
# SIGNAL DISPLAY
# ============================================================

st.divider()

if signal == "BUY_CE":

    st.success(
        "🟢 BUY CE SIGNAL\n\n"
        "5M Supertrend flipped GREEN "
        "with 15M + 4H GREEN confirmation."
    )

elif signal == "BUY_PE":

    st.error(
        "🔴 BUY PE SIGNAL\n\n"
        "5M Supertrend flipped RED "
        "with 15M + 4H RED confirmation."
    )

else:

    st.info(
        "⏳ WAIT — No new confirmed Supertrend signal."
    )


# ============================================================
# OPTION SELECTION
# ============================================================

selected_option = None

if signal in [
    "BUY_CE",
    "BUY_PE"
]:

    option_type = (
        "CE"
        if signal == "BUY_CE"
        else "PE"
    )

    try:

        selected_option = select_atm_option(
            st.session_state.instruments,
            spot,
            option_type
        )

        option_ltp = get_option_ltp(
            selected_option
        )

        selected_option["ltp"] = (
            option_ltp
        )

        st.session_state.option_symbol = (
            selected_option["symbol"]
        )

        st.session_state.option_token = (
            selected_option["token"]
        )

        st.session_state.option_type = (
            option_type
        )

        st.session_state.option_expiry = (
            selected_option["expiry"]
        )

        st.session_state.option_strike = (
            selected_option["strike"]
        )

        st.session_state.option_lot_size = (
            selected_option["lot_size"]
        )

        st.session_state.option_ltp = (
            option_ltp
        )

    except Exception as e:

        st.error(
            f"Option selection failed: {e}"
        )


# ============================================================
# OPTION DETAILS
# ============================================================

if selected_option:

    st.divider()

    st.subheader(
        "🎯 Selected ATM Option"
    )

    a, b, c, d, e = st.columns(5)

    a.metric(
        "Symbol",
        selected_option["symbol"]
    )

    b.metric(
        "Strike",
        f"{selected_option['strike']:,.0f}"
    )

    c.metric(
        "Expiry",
        str(
            selected_option["expiry"]
        )
    )

    d.metric(
        "Lot Size",
        str(
            selected_option["lot_size"]
        )
    )

    e.metric(
        "LTP",
        f"₹{selected_option['ltp']:,.2f}"
    )

    with st.expander(
        "Option technical details"
    ):

        st.write(
            "Token:",
            selected_option["token"]
        )

        st.write(
            "Type:",
            selected_option["symbol"][-2:]
        )

        st.write(
            "Quantity:",
            selected_option["lot_size"]
        )

        st.write(
            "Spot:",
            spot
        )


# ============================================================
# BUY ORDER SECTION
# ============================================================

st.divider()

st.subheader(
    "🛒 Order Panel"
)

if selected_option:

    buy_col, refresh_col = st.columns(
        [3, 1]
    )

    with buy_col:

        buy_text = (
            f"BUY {selected_option['symbol']} "
            f"× {selected_option['lot_size']}"
        )

        if st.button(
            buy_text,
            type="primary",
            use_container_width=True
        ):

            try:

                result = execute_signal_order(
                    signal,
                    selected_option
                )

                if result.get(
                    "paper"
                ):

                    st.warning(
                        result["message"]
                    )

                    st.info(
                        f"Paper order ID: "
                        f"{result['order_id']}"
                    )

                else:

                    st.success(
                        "BUY order submitted successfully."
                    )

                    st.write(
                        "Order ID:",
                        result["order_id"]
                    )

                    st.warning(
                        "Order ID means the order was "
                        "accepted by the API; verify "
                        "the actual execution status "
                        "in the Order Book."
                    )

            except Exception as e:

                st.error(
                    str(e)
                )

    with refresh_col:

        if st.button(
            "🔄 Refresh",
            use_container_width=True
        ):

            st.rerun()

else:

    st.info(
        "A BUY button appears only when a "
        "confirmed BUY_CE or BUY_PE signal occurs."
    )


# ============================================================
# AUTOMATIC TRADING
# ============================================================

if AUTO_TRADE and selected_option:

    today_string = str(
        today_ist()
    )

    already_processed_today = (
        st.session_state.auto_trade_last_date
        == today_string
        and
        st.session_state.auto_trade_last_signal
        == signal
    )

    if not already_processed_today:

        try:

            result = execute_signal_order(
                signal,
                selected_option
            )

            st.session_state.auto_trade_last_signal = (
                signal
            )

            st.session_state.auto_trade_last_date = (
                today_string
            )

            if result.get(
                "paper"
            ):

                st.warning(
                    "AUTO TRADE TEST: "
                    "No real order sent."
                )

            else:

                st.success(
                    "AUTOMATIC BUY ORDER SENT | "
                    f"Order ID: "
                    f"{result['order_id']}"
                )

        except Exception as e:

            # A duplicate is deliberately not
            # retried automatically.
            if "DUPLICATE ORDER BLOCKED" in str(e):

                st.warning(
                    str(e)
                )

                st.session_state.auto_trade_last_signal = (
                    signal
                )

                st.session_state.auto_trade_last_date = (
                    today_string
                )

            else:

                st.error(
                    f"Automatic order failed: {e}"
                )


# ============================================================
# LAST ORDER
# ============================================================

if st.session_state.last_order_id:

    st.divider()

    st.subheader(
        "📋 Last Order"
    )

    x, y, z = st.columns(3)

    x.metric(
        "Order ID",
        str(
            st.session_state.last_order_id
        )
    )

    y.metric(
        "Symbol",
        str(
            st.session_state.last_order_symbol
        )
    )

    z.metric(
        "Signal",
        str(
            st.session_state.last_order_signal
        )
    )

    st.caption(
        f"Order time: "
        f"{st.session_state.last_order_time}"
    )


# ============================================================
# POSITIONS
# ============================================================

st.divider()

st.subheader(
    "📊 Positions"
)

positions = get_positions()

if positions:

    positions_df = pd.DataFrame(
        positions
    )

    st.dataframe(
        positions_df,
        use_container_width=True,
        hide_index=True
    )

else:

    st.info(
        "No position data returned."
    )


# ============================================================
# ORDER BOOK
# ============================================================

st.divider()

st.subheader(
    "📒 Order Book"
)

orders = get_order_book()

if orders:

    orders_df = pd.DataFrame(
        orders
    )

    st.dataframe(
        orders_df,
        use_container_width=True,
        hide_index=True
    )

else:

    st.info(
        "No orders returned."
    )


# ============================================================
# SUPERTREND DATA
# ============================================================

st.divider()

st.subheader(
    "📈 Supertrend Data"
)

tab5, tab15, tab4h = st.tabs(
    [
        "5 Minute",
        "15 Minute",
        "4 Hour"
    ]
)

with tab5:

    st.dataframe(
        st5[
            [
                "datetime",
                "open",
                "high",
                "low",
                "close",
                "Supertrend",
                "ST_Green",
                "ST_Flip_Green",
                "ST_Flip_Red",
            ]
        ].tail(50),
        use_container_width=True,
        hide_index=True
    )

with tab15:

    st.dataframe(
        st15[
            [
                "datetime",
                "open",
                "high",
                "low",
                "close",
                "Supertrend",
                "ST_Green",
                "ST_Flip_Green",
                "ST_Flip_Red",
            ]
        ].tail(50),
        use_container_width=True,
        hide_index=True
    )

with tab4h:

    st.dataframe(
        st4h[
            [
                "datetime",
                "open",
                "high",
                "low",
                "close",
                "Supertrend",
                "ST_Green",
                "ST_Flip_Green",
                "ST_Flip_Red",
            ]
        ].tail(50),
        use_container_width=True,
        hide_index=True
    )


# ============================================================
# LOCAL ORDER STATE
# ============================================================

with st.expander(
    "Local Order History"
):

    state = load_state()

    if state.get(
        "orders"
    ):

        state_df = pd.DataFrame(
            state["orders"]
        )

        st.dataframe(
            state_df,
            use_container_width=True,
            hide_index=True
        )

    else:

        st.info(
            "No local orders recorded."
        )


# ============================================================
# DEBUG / STATUS
# ============================================================

with st.expander(
    "System Status"
):

    st.write(
        "Current time:",
        now_ist().strftime(
            "%Y-%m-%d %H:%M:%S %Z"
        )
    )

    st.write(
        "Market:",
        market_status_text()
    )

    st.write(
        "LIVE_TRADING:",
        LIVE_TRADING
    )

    st.write(
        "AUTO_TRADE:",
        AUTO_TRADE
    )

    st.write(
        "Duplicate protection:",
        DUPLICATE_PROTECTION
    )

    st.write(
        "NIFTY token:",
        NIFTY_TOKEN
    )

    st.write(
        "5M candles:",
        len(st5)
    )

    st.write(
        "15M candles:",
        len(st15)
    )

    st.write(
        "4H candles:",
        len(st4h)
    )


# ============================================================
# REFRESH
# ============================================================

st.divider()

refresh_col1, refresh_col2 = st.columns(2)

with refresh_col1:

    if st.button(
        "🔄 Refresh Market Data",
        use_container_width=True
    ):

        st.session_state.refresh_count += 1

        st.rerun()

with refresh_col2:

    st.caption(
        "Refresh the page during market hours "
        "to obtain the latest signal."
    )


# ============================================================
# FOOTER
# ============================================================

st.caption(
    "NIFTY Supertrend 20,2 | "
    "Angel One SmartAPI | "
    f"Updated {now_ist().strftime('%H:%M:%S IST')}"
)

