# ============================================================
# dashboard.py
#
# NIFTY AUTOMATIC BUY CE ONLY - ANGEL ONE SMARTAPI
#
# STRATEGY
# ------------------------------------------------------------
# 1-minute NIFTY candles
#       ↓
# Resample to 2-minute candles
#       ↓
# COMPLETED 2-minute candle only
#       ↓
# Supertrend (20, 1.5)
#       ↓
# RED -> GREEN FLIP
#       ↓
# Select nearest ATM NIFTY CE
#       ↓
# AUTOMATIC BUY
#       ↓
# Show order immediately in dashboard Order Book
#
# NO:
#   - 5-minute Supertrend
#   - 15-minute Supertrend
#   - 4-hour Supertrend
#   - manual BUY button
#   - manual SELL button
#   - broker confirmation before sending order
#
# ============================================================

import json
import os
import time
from datetime import datetime, timedelta, time as dt_time
from pathlib import Path
from zoneinfo import ZoneInfo
from urllib.parse import parse_qs, urlparse

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
    page_title="NIFTY 2-Min Automatic BUY CE",
    page_icon="📈",
    layout="wide",
)


# ============================================================
# CONFIG
# ============================================================

IST = ZoneInfo("Asia/Kolkata")

ST_PERIOD = 20
ST_MULTIPLIER = 1.5

NIFTY_SYMBOL = "NIFTY"
NIFTY_TOKEN = "99926000"

EXCHANGE = "NFO"
PRODUCT_TYPE = "INTRADAY"
ORDER_TYPE = "MARKET"
DURATION = "DAY"

LOTS = int(
    os.getenv("NIFTY_LOTS", "1")
)

REFRESH_SECONDS = int(
    os.getenv("REFRESH_SECONDS", "60")
)

CANDLE_MIN_INTERVAL = 120
RATE_LIMIT_COOLDOWN = 180

ENTRY_START = dt_time(9, 20)
ENTRY_END = dt_time(15, 15)

BASE_DIR = Path(__file__).resolve().parent

STATE_FILE = (
    BASE_DIR / "nifty_ce_state.json"
)

INSTRUMENT_CACHE = (
    BASE_DIR / "OpenAPIScripMaster.json"
)

SCRIP_MASTER_URL = (
    "https://margincalculator.angelone.in/"
    "OpenAPI_File/files/OpenAPIScripMaster.json"
)


# ============================================================
# CREDENTIALS
#
# Supports environment variables.
# Also supports Streamlit secrets.
# ============================================================

def get_config_value(name):

    value = os.getenv(
        name,
        ""
    ).strip()

    if value:
        return value

    try:
        value = str(
            st.secrets.get(
                name,
                ""
            )
        ).strip()

        return value

    except Exception:

        return ""


ANGEL_API_KEY = get_config_value(
    "ANGEL_API_KEY"
)

ANGEL_CLIENT_ID = get_config_value(
    "ANGEL_CLIENT_ID"
)

ANGEL_PASSWORD = get_config_value(
    "ANGEL_PASSWORD"
)

ANGEL_TOTP_SECRET = get_config_value(
    "ANGEL_TOTP_SECRET"
)


# ============================================================
# PAPER / LIVE
# ============================================================

PAPER_TRADING = (
    os.getenv(
        "PAPER_TRADING",
        "false"
    )
    .strip()
    .lower()
    in (
        "1",
        "true",
        "yes",
        "y",
        "on",
    )
)


# ============================================================
# SESSION DEFAULTS
# ============================================================

DEFAULTS = {

    "api": None,

    "login_status":
        "NOT CONNECTED",

    "last_error":
        "",

    "last_message":
        "Ready",

    "spot":
        None,

    "spot_source":
        "",

    "signal":
        "WAIT",

    "signal_time":
        None,

    "previous_direction":
        None,

    "current_direction":
        None,

    "green":
        False,

    "red":
        False,

    "green_flip":
        False,

    "st_value":
        None,

    "option_symbol":
        None,

    "option_token":
        None,

    "option_expiry":
        None,

    "option_strike":
        None,

    "option_lot_size":
        None,

    "last_order_id":
        None,

    "last_order_time":
        None,

    "last_order_status":
        None,

    "order_book":
        [],

    "last_processed_candle":
        None,

    "last_candle_fetch":
        None,

    "last_fresh_candle":
        None,

    "rate_limited_until":
        None,

    "instruments":
        None,

    "position":
        None,

    "in_position":
        False,

    "automatic_order_attempted":
        False,

    "trade_date":
        None,

    "last_ordered_green_candle":
        None,
}


for key, value in DEFAULTS.items():

    if key not in st.session_state:

        st.session_state[key] = value


# ============================================================
# TIME
# ============================================================

def now_ist():

    return pd.Timestamp.now(
        tz=IST
    )


# ============================================================
# HELPERS
# ============================================================

def safe_float(value):

    try:
        return float(value)
    except Exception:
        return None


def json_safe(value):

    if isinstance(
        value,
        np.integer
    ):
        return int(value)

    if isinstance(
        value,
        np.floating
    ):
        return float(value)

    if isinstance(
        value,
        (
            pd.Timestamp,
            datetime
        )
    ):
        return value.isoformat()

    return value


def clean_secret(raw):

    raw = (
        raw or ""
    ).strip()

    if raw.startswith(
        "otpauth://"
    ):

        try:

            parsed = urlparse(
                raw
            )

            secret = parse_qs(
                parsed.query
            ).get(
                "secret",
                [None]
            )[0]

            if secret:

                return (
                    secret
                    .replace(" ", "")
                    .replace("-", "")
                    .upper()
                )

        except Exception:

            pass

    return (
        raw
        .replace(" ", "")
        .replace("-", "")
        .upper()
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

    return missing


# ============================================================
# RATE LIMIT
# ============================================================

def is_rate_limit_error(
    message
):

    text = str(
        message
    ).lower()

    return any(
        x in text
        for x in (
            "rate limit",
            "rate-limit",
            "too many",
            "throttle",
            "throttled",
            "exceed",
            "429",
        )
    )


def set_rate_limit():

    until = (
        now_ist()
        + pd.Timedelta(
            seconds=RATE_LIMIT_COOLDOWN
        )
    )

    st.session_state[
        "rate_limited_until"
    ] = until.isoformat()


def rate_limit_active():

    value = st.session_state.get(
        "rate_limited_until"
    )

    if not value:
        return False

    try:

        until = pd.Timestamp(
            value
        )

        if until.tzinfo is None:

            until = until.tz_localize(
                IST
            )

        else:

            until = until.tz_convert(
                IST
            )

        if now_ist() < until:
            return True

        st.session_state[
            "rate_limited_until"
        ] = None

        return False

    except Exception:

        st.session_state[
            "rate_limited_until"
        ] = None

        return False


# ============================================================
# STATE FILE
# ============================================================

def load_state():

    if not STATE_FILE.exists():
        return

    try:

        data = json.loads(
            STATE_FILE.read_text(
                encoding="utf-8"
            )
        )

        keys = [

            "last_order_id",
            "last_order_time",
            "last_order_status",
            "last_processed_candle",
            "last_fresh_candle",
            "position",
            "in_position",
            "automatic_order_attempted",
            "trade_date",
            "last_ordered_green_candle",
        ]

        for key in keys:

            if key in data:

                st.session_state[
                    key
                ] = data[key]

    except Exception as exc:

        st.session_state[
            "last_error"
        ] = (
            "State load failed: "
            + str(exc)
        )


def save_state():

    data = {

        "last_order_id":
            st.session_state.get(
                "last_order_id"
            ),

        "last_order_time":
            st.session_state.get(
                "last_order_time"
            ),

        "last_order_status":
            st.session_state.get(
                "last_order_status"
            ),

        "last_processed_candle":
            st.session_state.get(
                "last_processed_candle"
            ),

        "last_fresh_candle":
            st.session_state.get(
                "last_fresh_candle"
            ),

        "position":
            st.session_state.get(
                "position"
            ),

        "in_position":
            st.session_state.get(
                "in_position",
                False
            ),

        "automatic_order_attempted":
            st.session_state.get(
                "automatic_order_attempted",
                False
            ),

        "trade_date":
            st.session_state.get(
                "trade_date"
            ),

        "last_ordered_green_candle":
            st.session_state.get(
                "last_ordered_green_candle"
            ),
    }

    try:

        STATE_FILE.write_text(
            json.dumps(
                data,
                indent=2,
                default=json_safe
            ),
            encoding="utf-8"
        )

    except Exception as exc:

        st.session_state[
            "last_error"
        ] = (
            "State save failed: "
            + str(exc)
        )


# ============================================================
# ANGEL ONE LOGIN
# ============================================================

def angel_login():

    if st.session_state.get(
        "api"
    ) is not None:

        return st.session_state[
            "api"
        ]

    missing = credentials_ok()

    if missing:

        raise RuntimeError(
            "Missing credentials: "
            + ", ".join(missing)
        )

    secret = clean_secret(
        ANGEL_TOTP_SECRET
    )

    if (
        secret.isdigit()
        and len(secret) == 6
    ):

        raise RuntimeError(
            "ANGEL_TOTP_SECRET contains "
            "the 6-digit OTP. Use the actual "
            "base32 TOTP secret."
        )

    try:

        totp = pyotp.TOTP(
            secret
        ).now()

    except Exception as exc:

        raise RuntimeError(
            "Invalid TOTP secret: "
            + str(exc)
        )

    api = SmartConnect(
        api_key=ANGEL_API_KEY
    )

    response = api.generateSession(
        ANGEL_CLIENT_ID,
        ANGEL_PASSWORD,
        totp
    )

    if (
        not isinstance(
            response,
            dict
        )
        or not response.get(
            "status"
        )
    ):

        raise RuntimeError(
            "Angel One login failed: "
            + str(response)
        )

    st.session_state[
        "api"
    ] = api

    st.session_state[
        "login_status"
    ] = "CONNECTED"

    st.session_state[
        "last_error"
    ] = ""

    st.session_state[
        "last_message"
    ] = (
        "Angel One login successful"
    )

    return api


# ============================================================
# MARKET HOURS
# ============================================================

def market_is_open():

    now = now_ist()

    if now.weekday() >= 5:
        return False

    return (
        ENTRY_START
        <= now.time()
        <= ENTRY_END
    )


# ============================================================
# LAST COMPLETED 2-MINUTE BOUNDARY
# ============================================================

def last_completed_2min_boundary():

    current = now_ist()

    market_open = pd.Timestamp(
        datetime.combine(
            current.date(),
            dt_time(9, 15)
        ),
        tz=IST
    )

    if current < market_open:
        return None

    elapsed = (
        current - market_open
    ).total_seconds()

    blocks = int(
        elapsed // 120
    )

    return (
        market_open
        + pd.Timedelta(
            seconds=blocks * 120
        )
    )


# ============================================================
# GET 1-MINUTE NIFTY CANDLES
# ============================================================

def get_nifty_1m_candles(
    api,
    days=3
):

    end = now_ist()

    start = (
        end
        - timedelta(days=days)
    )

    params = {

        "exchange":
            "NSE",

        "symboltoken":
            NIFTY_TOKEN,

        "interval":
            "ONE_MINUTE",

        "fromdate":
            start.strftime(
                "%Y-%m-%d %H:%M"
            ),

        "todate":
            end.strftime(
                "%Y-%m-%d %H:%M"
            ),
    }

    try:

        response = api.getCandleData(
            params
        )

    except Exception as exc:

        if is_rate_limit_error(exc):
            set_rate_limit()

        raise

    if (
        not isinstance(
            response,
            dict
        )
        or not response.get(
            "status"
        )
    ):

        if is_rate_limit_error(
            response
        ):
            set_rate_limit()

        raise RuntimeError(
            "Candle API failed: "
            + str(response)
        )

    rows = (
        response.get("data")
        or []
    )

    if not rows:

        raise RuntimeError(
            "Candle API returned no "
            "NIFTY candles."
        )

    df = pd.DataFrame(
        rows,
        columns=[
            "datetime",
            "open",
            "high",
            "low",
            "close",
            "volume",
        ]
    )

    for col in (
        "open",
        "high",
        "low",
        "close",
        "volume",
    ):

        df[col] = pd.to_numeric(
            df[col],
            errors="coerce"
        )

    ts = pd.to_datetime(
        df["datetime"],
        errors="coerce"
    )

    if ts.dt.tz is None:

        ts = ts.dt.tz_localize(
            IST
        )

    else:

        ts = ts.dt.tz_convert(
            IST
        )

    df["datetime"] = ts

    df = (
        df
        .dropna(
            subset=[
                "datetime",
                "open",
                "high",
                "low",
                "close",
            ]
        )
        .sort_values(
            "datetime"
        )
        .drop_duplicates(
            "datetime"
        )
        .reset_index(
            drop=True
        )
    )

    # Never use current 1-minute candle.
    current_minute = (
        now_ist().floor("min")
    )

    df = df[
        df["datetime"]
        < current_minute
    ].copy()

    # NSE session.
    df = df[
        (
            df["datetime"].dt.time
            >= dt_time(9, 15)
        )
        &
        (
            df["datetime"].dt.time
            <= dt_time(15, 30)
        )
    ].copy()

    if df.empty:

        raise RuntimeError(
            "No completed NIFTY 1-minute "
            "candles available."
        )

    return df


# ============================================================
# RESAMPLE TO 2 MINUTES
# ============================================================

def make_2min_candles(
    df1
):

    x = (
        df1
        .copy()
        .set_index("datetime")
        .sort_index()
    )

    df2 = (
        x
        .resample(
            "2min",
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

    if df2.empty:

        raise RuntimeError(
            "Could not create 2-minute candles."
        )

    return df2


# ============================================================
# ONLY COMPLETED 2-MINUTE CANDLES
# ============================================================

def get_completed_2min_candles(
    df2
):

    boundary = (
        last_completed_2min_boundary()
    )

    if boundary is None:

        raise RuntimeError(
            "2-minute boundary unavailable."
        )

    completed = df2[
        df2["datetime"]
        <= boundary
    ].copy()

    if completed.empty:

        raise RuntimeError(
            "No completed 2-minute candle."
        )

    return completed


# ============================================================
# SUPERTREND
# ============================================================

def calculate_supertrend(
    df,
    period=20,
    multiplier=1.5
):

    df = df.copy()

    if len(df) < period + 5:

        raise RuntimeError(
            f"Not enough candles for "
            f"Supertrend {period},{multiplier}. "
            f"Received {len(df)}."
        )

    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)

    previous_close = close.shift(1)

    tr = pd.concat(
        [
            high - low,
            (
                high - previous_close
            ).abs(),
            (
                low - previous_close
            ).abs(),
        ],
        axis=1,
    ).max(axis=1)

    atr = tr.ewm(
        alpha=1 / period,
        adjust=False,
        min_periods=period,
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

    final_upper = (
        basic_upper.copy()
    )

    final_lower = (
        basic_lower.copy()
    )

    for i in range(
        1,
        len(df)
    ):

        prev_fu = (
            final_upper.iloc[i - 1]
        )

        prev_fl = (
            final_lower.iloc[i - 1]
        )

        if (
            pd.isna(prev_fu)
            or
            basic_upper.iloc[i]
            < prev_fu
            or
            close.iloc[i - 1]
            > prev_fu
        ):

            final_upper.iloc[i] = (
                basic_upper.iloc[i]
            )

        else:

            final_upper.iloc[i] = (
                prev_fu
            )

        if (
            pd.isna(prev_fl)
            or
            basic_lower.iloc[i]
            > prev_fl
            or
            close.iloc[i - 1]
            < prev_fl
        ):

            final_lower.iloc[i] = (
                basic_lower.iloc[i]
            )

        else:

            final_lower.iloc[i] = (
                prev_fl
            )

    direction = pd.Series(
        index=df.index,
        dtype="float64"
    )

    supertrend = pd.Series(
        index=df.index,
        dtype="float64"
    )

    first_valid = (
        atr.first_valid_index()
    )

    if first_valid is None:

        raise RuntimeError(
            "ATR calculation failed."
        )

    first_i = df.index.get_loc(
        first_valid
    )

    direction.iloc[first_i] = 1

    supertrend.iloc[first_i] = (
        final_lower.iloc[first_i]
    )

    for i in range(
        first_i + 1,
        len(df)
    ):

        previous_st = (
            supertrend.iloc[i - 1]
        )

        if pd.isna(previous_st):

            direction.iloc[i] = 1

            supertrend.iloc[i] = (
                final_lower.iloc[i]
            )

            continue

        if (
            previous_st
            == final_upper.iloc[i - 1]
        ):

            if (
                close.iloc[i]
                <= final_upper.iloc[i]
            ):

                supertrend.iloc[i] = (
                    final_upper.iloc[i]
                )

                direction.iloc[i] = -1

            else:

                supertrend.iloc[i] = (
                    final_lower.iloc[i]
                )

                direction.iloc[i] = 1

        else:

            if (
                close.iloc[i]
                >= final_lower.iloc[i]
            ):

                supertrend.iloc[i] = (
                    final_lower.iloc[i]
                )

                direction.iloc[i] = 1

            else:

                supertrend.iloc[i] = (
                    final_upper.iloc[i]
                )

                direction.iloc[i] = -1

    df["atr"] = atr

    df["final_upper"] = final_upper

    df["final_lower"] = final_lower

    df["supertrend"] = supertrend

    df["direction"] = (
        direction
        .round()
        .astype("Int64")
    )

    df["green"] = (
        df["direction"] == 1
    )

    df["red"] = (
        df["direction"] == -1
    )

    return df


# ============================================================
# SIGNAL
#
# IMPORTANT:
# BUY CE ONLY WHEN:
#
# previous completed candle = RED
# current completed candle  = GREEN
#
# ============================================================

def get_2min_signal(
    df2
):

    completed = (
        get_completed_2min_candles(
            df2
        )
    )

    if len(completed) < (
        ST_PERIOD + 5
    ):

        raise RuntimeError(
            "Not enough completed 2-minute "
            "candles for Supertrend."
        )

    st_df = calculate_supertrend(
        completed,
        ST_PERIOD,
        ST_MULTIPLIER
    )

    current = st_df.iloc[-1]

    previous = st_df.iloc[-2]

    current_direction = (
        int(current["direction"])
        if pd.notna(
            current["direction"]
        )
        else None
    )

    previous_direction = (
        int(previous["direction"])
        if pd.notna(
            previous["direction"]
        )
        else None
    )

    green = (
        current_direction == 1
    )

    red = (
        current_direction == -1
    )

    green_flip = (
        previous_direction == -1
        and
        current_direction == 1
    )

    return {

        "signal":
            "BUY CE"
            if green_flip
            else "WAIT",

        "green":
            green,

        "red":
            red,

        "green_flip":
            green_flip,

        "current_direction":
            current_direction,

        "previous_direction":
            previous_direction,

        "candle_time":
            current["datetime"],

        "close":
            float(
                current["close"]
            ),

        "supertrend":
            (
                float(
                    current["supertrend"]
                )
                if pd.notna(
                    current["supertrend"]
                )
                else None
            ),

        "dataframe":
            st_df,
    }


# ============================================================
# SCRIP MASTER
# ============================================================

@st.cache_data(
    ttl=3600,
    show_spinner=False
)
def download_scrip_master():

    response = requests.get(
        SCRIP_MASTER_URL,
        timeout=30
    )

    response.raise_for_status()

    data = response.json()

    if (
        not isinstance(data, list)
        or not data
    ):

        raise RuntimeError(
            "Scrip master is empty."
        )

    return data


def load_instruments():

    if st.session_state.get(
        "instruments"
    ) is not None:

        return st.session_state[
            "instruments"
        ]

    try:

        data = (
            download_scrip_master()
        )

    except Exception as exc:

        if not INSTRUMENT_CACHE.exists():

            raise RuntimeError(
                "Could not download Angel One "
                "scrip master: "
                + str(exc)
            )

        data = json.loads(
            INSTRUMENT_CACHE.read_text(
                encoding="utf-8"
            )
        )

    try:

        INSTRUMENT_CACHE.write_text(
            json.dumps(data),
            encoding="utf-8"
        )

    except Exception:

        pass

    st.session_state[
        "instruments"
    ] = data

    return data


# ============================================================
# EXPIRY
# ============================================================

def parse_expiry(value):

    if not value:
        return None

    value = (
        str(value)
        .strip()
        .upper()
    )

    for fmt in (
        "%d%b%Y",
        "%d%b%y",
        "%Y-%m-%d",
        "%d-%b-%Y",
        "%d/%m/%Y",
    ):

        try:

            return datetime.strptime(
                value,
                fmt
            ).date()

        except ValueError:

            continue

    return None


# ============================================================
# STRIKE
# ============================================================

def normalize_strike(raw):

    try:

        value = float(raw)

        if value > 100000:

            value /= 100

        return value

    except Exception:

        return None


# ============================================================
# SELECT ATM NIFTY CE
# ============================================================

def select_atm_nifty_ce(
    instruments,
    spot
):

    if spot is None:

        raise RuntimeError(
            "NIFTY Spot unavailable."
        )

    today = now_ist().date()

    candidates = []

    for item in instruments:

        if not isinstance(
            item,
            dict
        ):
            continue

        exch = str(
            item.get(
                "exch_seg",
                ""
            )
        ).upper()

        symbol = str(
            item.get(
                "symbol",
                ""
            )
        ).upper()

        name = str(
            item.get(
                "name",
                ""
            )
        ).upper()

        instrument_type = str(
            item.get(
                "instrumenttype",
                ""
            )
        ).upper()

        if exch != "NFO":
            continue

        if (
            "NIFTY" not in name
            and
            not symbol.startswith(
                "NIFTY"
            )
        ):
            continue

        if not symbol.endswith("CE"):
            continue

        if instrument_type not in (
            "",
            "OPTIDX",
        ):
            continue

        strike = normalize_strike(
            item.get("strike")
        )

        if strike is None:
            continue

        expiry_raw = item.get(
            "expiry"
        )

        expiry = parse_expiry(
            expiry_raw
        )

        if expiry is None:
            continue

        if expiry < today:
            continue

        token = str(
            item.get(
                "token",
                ""
            )
        ).strip()

        if not token:
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

        candidates.append(
            {

                "symbol":
                    symbol,

                "token":
                    token,

                "expiry":
                    expiry,

                "expiry_raw":
                    str(expiry_raw),

                "strike":
                    int(
                        round(strike)
                    ),

                "lot_size":
                    lot_size,
            }
        )

    if not candidates:

        raise RuntimeError(
            "No future NIFTY CE options "
            "found."
        )

    nearest_expiry = min(
        x["expiry"]
        for x in candidates
    )

    candidates = [
        x
        for x in candidates
        if x["expiry"]
        == nearest_expiry
    ]

    candidates.sort(
        key=lambda x: (
            abs(
                x["strike"]
                - float(spot)
            ),
            x["strike"],
        )
    )

    return candidates[0]


# ============================================================
# ADD LOCAL ORDER
#
# This makes the order visible immediately, even if Angel One
# orderBook() takes a moment to return it.
# ============================================================

def add_local_order(
    order
):

    orders = (
        st.session_state.get(
            "order_book",
            []
        )
    )

    order_id = str(
        order.get(
            "order_id",
            ""
        )
    )

    # Prevent duplicate local rows.
    for existing in orders:

        if (
            order_id
            and
            str(
                existing.get(
                    "order_id",
                    ""
                )
            )
            == order_id
        ):

            return

    orders.insert(
        0,
        order
    )

    st.session_state[
        "order_book"
    ] = orders[:50]


# ============================================================
# EXTRACT ORDER ID
# ============================================================

def extract_order_id(
    response
):

    if response is None:
        return None

    if isinstance(
        response,
        str
    ):

        value = response.strip()

        if not value:
            return None

        if value.startswith("{"):

            try:

                return extract_order_id(
                    json.loads(value)
                )

            except Exception:

                pass

        return value

    if isinstance(
        response,
        dict
    ):

        for key in (
            "orderid",
            "orderId",
            "order_id",
            "orderID",
        ):

            value = response.get(key)

            if value is not None:

                value = str(
                    value
                ).strip()

                if value:
                    return value

        for key in (
            "data",
            "response",
            "result",
            "body",
        ):

            found = (
                extract_order_id(
                    response.get(key)
                )
            )

            if found:
                return found

    return None


# ============================================================
# PLACE BUY CE
# ============================================================

def place_buy_ce(
    api,
    option
):

    quantity = (
        LOTS
        * int(
            option["lot_size"]
        )
    )

    params = {

        "variety":
            "NORMAL",

        "tradingsymbol":
            option["symbol"],

        "symboltoken":
            str(
                option["token"]
            ),

        "transactiontype":
            "BUY",

        "exchange":
            "NFO",

        "ordertype":
            "MARKET",

        "producttype":
            "INTRADAY",

        "duration":
            "DAY",

        "price":
            "0",

        "quantity":
            str(quantity),
    }

    timestamp = (
        now_ist()
        .strftime(
            "%Y-%m-%d %H:%M:%S"
        )
    )

    # --------------------------------------------------------
    # PAPER
    # --------------------------------------------------------

    if PAPER_TRADING:

        order_id = (
            "PAPER-"
            + now_ist().strftime(
                "%Y%m%d%H%M%S%f"
            )
        )

        order = {

            "time":
                timestamp,

            "order_id":
                order_id,

            "mode":
                "PAPER",

            "status":
                "BUY CE SENT",

            "side":
                "BUY",

            "symbol":
                option["symbol"],

            "token":
                option["token"],

            "expiry":
                option["expiry_raw"],

            "strike":
                option["strike"],

            "quantity":
                quantity,

            "price":
                "MARKET",
        }

        add_local_order(
            order
        )

        return order_id, order

    # --------------------------------------------------------
    # REAL ANGEL ONE ORDER
    # --------------------------------------------------------

    try:

        if hasattr(
            api,
            "placeOrderFullResponse"
        ):

            response = (
                api.placeOrderFullResponse(
                    params
                )
            )

        else:

            response = (
                api.placeOrder(
                    params
                )
            )

    except Exception as exc:

        if is_rate_limit_error(exc):

            set_rate_limit()

        raise RuntimeError(
            "Angel One BUY request failed: "
            + str(exc)
        )

    if response is None:

        raise RuntimeError(
            "Angel One returned empty "
            "order response."
        )

    if isinstance(
        response,
        dict
    ):

        if response.get(
            "status"
        ) is False:

            raise RuntimeError(
                "Angel One rejected BUY order: "
                + str(response)
            )

    order_id = extract_order_id(
        response
    )

    order = {

        "time":
            timestamp,

        "order_id":
            order_id or "",

        "mode":
            "LIVE",

        "status":
            "BUY CE SENT",

        "side":
            "BUY",

        "symbol":
            option["symbol"],

        "token":
            option["token"],

        "expiry":
            option["expiry_raw"],

        "strike":
            option["strike"],

        "quantity":
            quantity,

        "price":
            "MARKET",
    }

    # IMPORTANT:
    # Show the submitted order immediately.
    add_local_order(
        order
    )

    return order_id, order


# ============================================================
# AUTOMATIC BUY
#
# NO broker order-book confirmation is required.
# ============================================================

def automatic_buy_ce(
    api,
    option,
    candle_key
):

    today = (
        now_ist()
        .date()
        .isoformat()
    )

    # New trading day.
    if (
        st.session_state.get(
            "trade_date"
        )
        != today
    ):

        st.session_state[
            "trade_date"
        ] = today

        st.session_state[
            "automatic_order_attempted"
        ] = False

        st.session_state[
            "last_ordered_green_candle"
        ] = None

    # --------------------------------------------------------
    # SAME GREEN CANDLE
    # --------------------------------------------------------

    if (
        st.session_state.get(
            "last_ordered_green_candle"
        )
        == candle_key
    ):

        return

    # --------------------------------------------------------
    # ONE ORDER ATTEMPT PER DAY
    # --------------------------------------------------------

    if st.session_state.get(
        "automatic_order_attempted"
    ):

        st.session_state[
            "last_message"
        ] = (
            "BUY CE already sent today. "
            "Duplicate BUY blocked."
        )

        return

    try:

        order_id, order = (
            place_buy_ce(
                api,
                option
            )
        )

        quantity = (
            LOTS
            * int(
                option["lot_size"]
            )
        )

        st.session_state[
            "automatic_order_attempted"
        ] = True

        st.session_state[
            "last_ordered_green_candle"
        ] = candle_key

        st.session_state[
            "last_processed_candle"
        ] = candle_key

        st.session_state[
            "last_order_id"
        ] = order_id

        st.session_state[
            "last_order_time"
        ] = now_ist().isoformat()

        st.session_state[
            "last_order_status"
        ] = "BUY CE SENT"

        st.session_state[
            "in_position"
        ] = True

        st.session_state[
            "position"
        ] = {

            "symbol":
                option["symbol"],

            "token":
                option["token"],

            "strike":
                option["strike"],

            "expiry":
                option["expiry_raw"],

            "quantity":
                quantity,

            "entry_price":
                "MARKET",

            "signal_candle":
                candle_key,

            "mode":
                (
                    "PAPER"
                    if PAPER_TRADING
                    else "LIVE"
                ),
        }

        if order_id:

            order_text = (
                f"Order ID: {order_id}"
            )

        else:

            order_text = (
                "Order submitted; "
                "Order ID not returned"
            )

        st.session_state[
            "last_message"
        ] = (
            "AUTOMATIC BUY CE SENT | "
            + option["symbol"]
            + " | Qty: "
            + str(quantity)
            + " | "
            + order_text
        )

        save_state()

    except Exception as exc:

        st.session_state[
            "last_order_status"
        ] = "BUY ERROR"

        st.session_state[
            "last_message"
        ] = (
            "Automatic BUY CE error: "
            + str(exc)
        )

        save_state()

        raise


# ============================================================
# BROKER ORDER BOOK
#
# This is DISPLAY ONLY.
# It is NOT used to decide whether to send the BUY.
# ============================================================

def fetch_broker_order_book(
    api
):

    try:

        response = api.orderBook()

    except Exception:

        return []

    if not isinstance(
        response,
        dict
    ):

        return []

    if not response.get(
        "status"
    ):

        return []

    data = (
        response.get("data")
        or []
    )

    if not isinstance(
        data,
        list
    ):

        return []

    rows = []

    for item in data:

        rows.append(
            {

                "time":
                    item.get(
                        "updatetime"
                    )
                    or item.get(
                        "orderdate"
                    )
                    or "",

                "order_id":
                    item.get(
                        "orderid"
                    )
                    or item.get(
                        "orderId"
                    )
                    or "",

                "mode":
                    "LIVE",

                "status":
                    item.get(
                        "status",
                        ""
                    ),

                "side":
                    item.get(
                        "transactiontype",
                        ""
                    ),

                "symbol":
                    item.get(
                        "tradingsymbol",
                        ""
                    ),

                "token":
                    item.get(
                        "symboltoken",
                        ""
                    ),

                "quantity":
                    item.get(
                        "quantity",
                        ""
                    ),

                "price":
                    item.get(
                        "price",
                        ""
                    ),
            }
        )

    return rows


# ============================================================
# MERGE ORDER BOOK
#
# Local submitted order is always retained.
# Broker rows are added when available.
# ============================================================

def build_order_book():

    local_orders = (
        st.session_state.get(
            "order_book",
            []
        )
    )

    api = (
        st.session_state.get(
            "api"
        )
    )

    if (
        api is not None
        and
        not PAPER_TRADING
    ):

        broker_orders = (
            fetch_broker_order_book(
                api
            )
        )

    else:

        broker_orders = []

    combined = []

    seen_ids = set()

    # Broker first.
    for row in broker_orders:

        order_id = str(
            row.get(
                "order_id",
                ""
            )
        ).strip()

        if order_id:

            seen_ids.add(
                order_id
            )

        combined.append(
            row
        )

    # Local submitted order.
    for row in local_orders:

        order_id = str(
            row.get(
                "order_id",
                ""
            )
        ).strip()

        if (
            order_id
            and
            order_id in seen_ids
        ):

            continue

        combined.append(
            row
        )

    return combined[:50]


# ============================================================
# RUN AUTOMATION
# ============================================================

def run_automation():

    api = angel_login()

    if not market_is_open():

        st.session_state[
            "signal"
        ] = "WAIT"

        st.session_state[
            "last_message"
        ] = (
            "Outside automatic entry window."
        )

        return

    if rate_limit_active():

        st.session_state[
            "last_message"
        ] = (
            "Angel One API cooldown active."
        )

        return

    # --------------------------------------------------------
    # CANDLE REQUEST COOLDOWN
    # --------------------------------------------------------

    last_fetch = (
        st.session_state.get(
            "last_candle_fetch"
        )
    )

    if last_fetch:

        try:

            last_ts = pd.Timestamp(
                last_fetch
            )

            if last_ts.tzinfo is None:

                last_ts = (
                    last_ts.tz_localize(
                        IST
                    )
                )

            else:

                last_ts = (
                    last_ts.tz_convert(
                        IST
                    )
                )

            seconds_since = (
                now_ist()
                - last_ts
            ).total_seconds()

        except Exception:

            seconds_since = 999999

    else:

        seconds_since = 999999

    if (
        seconds_since
        < CANDLE_MIN_INTERVAL
    ):

        st.session_state[
            "last_message"
        ] = (
            "Waiting for next candle "
            "request window."
        )

        return

    # --------------------------------------------------------
    # GET 1-MIN DATA
    # --------------------------------------------------------

    df1 = get_nifty_1m_candles(
        api,
        days=3
    )

    st.session_state[
        "last_candle_fetch"
    ] = now_ist().isoformat()

    # --------------------------------------------------------
    # SPOT
    # --------------------------------------------------------

    spot = safe_float(
        df1.iloc[-1]["close"]
    )

    if spot is None:

        raise RuntimeError(
            "NIFTY Spot unavailable."
        )

    st.session_state[
        "spot"
    ] = spot

    st.session_state[
        "spot_source"
    ] = (
        "Latest completed 1-minute candle"
    )

    # --------------------------------------------------------
    # 2-MIN
    # --------------------------------------------------------

    df2 = make_2min_candles(
        df1
    )

    # --------------------------------------------------------
    # SUPERTREND
    # --------------------------------------------------------

    info = get_2min_signal(
        df2
    )

    candle_key = (
        info["candle_time"]
        .isoformat()
    )

    st.session_state[
        "signal_time"
    ] = candle_key

    st.session_state[
        "current_direction"
    ] = (
        info["current_direction"]
    )

    st.session_state[
        "previous_direction"
    ] = (
        info["previous_direction"]
    )

    st.session_state[
        "green"
    ] = info["green"]

    st.session_state[
        "red"
    ] = info["red"]

    st.session_state[
        "green_flip"
    ] = info["green_flip"]

    st.session_state[
        "st_value"
    ] = info["supertrend"]

    st.session_state[
        "last_fresh_candle"
    ] = candle_key

    # --------------------------------------------------------
    # GREEN FLIP
    # --------------------------------------------------------

    if info["green_flip"]:

        st.session_state[
            "signal"
        ] = "BUY CE"

        st.session_state[
            "last_message"
        ] = (
            "🟢 RED → GREEN FLIP detected. "
            "Selecting ATM NIFTY CE."
        )

        # ----------------------------------------------------
        # DO NOT REPEAT SAME CANDLE
        # ----------------------------------------------------

        if (
            st.session_state.get(
                "last_ordered_green_candle"
            )
            == candle_key
        ):

            return

        # ----------------------------------------------------
        # DAILY DUPLICATE PROTECTION
        # ----------------------------------------------------

        if st.session_state.get(
            "automatic_order_attempted"
        ):

            st.session_state[
                "last_processed_candle"
            ] = candle_key

            st.session_state[
                "last_message"
            ] = (
                "BUY CE already sent today. "
                "Duplicate BUY blocked."
            )

            save_state()

            return

        # ----------------------------------------------------
        # SELECT ATM CE
        # ----------------------------------------------------

        instruments = (
            load_instruments()
        )

        option = (
            select_atm_nifty_ce(
                instruments,
                spot
            )
        )

        st.session_state[
            "option_symbol"
        ] = option["symbol"]

        st.session_state[
            "option_token"
        ] = option["token"]

        st.session_state[
            "option_expiry"
        ] = option["expiry_raw"]

        st.session_state[
            "option_strike"
        ] = option["strike"]

        st.session_state[
            "option_lot_size"
        ] = option["lot_size"]

        # ----------------------------------------------------
        # AUTOMATIC BUY
        # ----------------------------------------------------

        automatic_buy_ce(
            api,
            option,
            candle_key
        )

        return

    # --------------------------------------------------------
    # CURRENTLY GREEN BUT NO NEW FLIP
    # --------------------------------------------------------

    if info["green"]:

        st.session_state[
            "signal"
        ] = "WAIT"

        st.session_state[
            "last_message"
        ] = (
            "🟢 Supertrend is GREEN, but "
            "there is no new RED → GREEN flip."
        )

        return

    # --------------------------------------------------------
    # RED
    # --------------------------------------------------------

    st.session_state[
        "signal"
    ] = "WAIT"

    st.session_state[
        "last_message"
    ] = (
        "🔴 Supertrend is RED. Signal = WAIT."
    )


# ============================================================
# LOAD STATE
# ============================================================

load_state()


# ============================================================
# DAILY RESET
# ============================================================

today_string = (
    now_ist()
    .date()
    .isoformat()
)

if (
    st.session_state.get(
        "trade_date"
    )
    != today_string
):

    st.session_state[
        "trade_date"
    ] = today_string

    st.session_state[
        "automatic_order_attempted"
    ] = False

    st.session_state[
        "last_ordered_green_candle"
    ] = None

    st.session_state[
        "last_processed_candle"
    ] = None

    st.session_state[
        "in_position"
    ] = False

    st.session_state[
        "position"
    ] = None

    save_state()


# ============================================================
# HEADER
# ============================================================

st.title(
    "📈 NIFTY 2-Minute Automatic BUY CE"
)

st.caption(
    "2-Minute Supertrend (20, 1.5) | "
    "RED → GREEN = AUTOMATIC BUY ATM NIFTY CE"
)


# ============================================================
# MODE
# ============================================================

if PAPER_TRADING:

    st.warning(
        "PAPER TRADING MODE — "
        "no real Angel One order will be sent."
    )

else:

    st.error(
        "LIVE TRADING MODE — "
        "RED → GREEN can send a REAL "
        "BUY NIFTY CE order automatically."
    )


# ============================================================
# AUTOMATION
# ============================================================

automation_error = None

try:

    run_automation()

except Exception as exc:

    automation_error = str(exc)

    st.session_state[
        "last_error"
    ] = automation_error

    st.session_state[
        "last_message"
    ] = (
        "Automation error."
    )


# ============================================================
# TOP METRICS
# ============================================================

c1, c2, c3, c4, c5 = (
    st.columns(5)
)


with c1:

    st.metric(
        "Angel One",
        st.session_state.get(
            "login_status",
            "NOT CONNECTED"
        )
    )


with c2:

    spot = st.session_state.get(
        "spot"
    )

    st.metric(
        "NIFTY Spot",
        "-"
        if spot is None
        else f"{spot:,.2f}"
    )


with c3:

    st.metric(
        "Signal",
        st.session_state.get(
            "signal",
            "WAIT"
        )
    )


with c4:

    direction = (
        st.session_state.get(
            "current_direction"
        )
    )

    st.metric(
        "Supertrend",
        "GREEN"
        if direction == 1
        else
        "RED"
        if direction == -1
        else "-"
    )


with c5:

    st.metric(
        "Mode",
        "PAPER"
        if PAPER_TRADING
        else "LIVE"
    )


# ============================================================
# SUPERTREND STATUS
# ============================================================

st.subheader(
    "2-Minute Supertrend (20, 1.5)"
)

a, b, c, d = st.columns(4)


with a:

    if (
        st.session_state.get(
            "current_direction"
        )
        == 1
    ):

        st.success(
            "🟢 GREEN"
        )

    elif (
        st.session_state.get(
            "current_direction"
        )
        == -1
    ):

        st.error(
            "🔴 RED"
        )

    else:

        st.info("-")


with b:

    st.write(
        "**Supertrend**"
    )

    value = (
        st.session_state.get(
            "st_value"
        )
    )

    st.write(
        "-"
        if value is None
        else f"{value:,.2f}"
    )


with c:

    st.write(
        "**Signal**"
    )

    if (
        st.session_state.get(
            "green_flip"
        )
    ):

        st.success(
            "🚀 BUY CE"
        )

    else:

        st.info(
            "WAIT"
        )


with d:

    st.write(
        "**Completed Candle**"
    )

    st.write(
        st.session_state.get(
            "signal_time"
        )
        or "-"
    )


# ============================================================
# ATM CE
# ============================================================

st.subheader(
    "Selected ATM NIFTY CE"
)

o1, o2, o3, o4 = st.columns(4)


with o1:

    st.write("**Symbol**")

    st.write(
        st.session_state.get(
            "option_symbol"
        )
        or "-"
    )


with o2:

    st.write("**Expiry**")

    st.write(
        st.session_state.get(
            "option_expiry"
        )
        or "-"
    )


with o3:

    st.write("**Strike**")

    strike = (
        st.session_state.get(
            "option_strike"
        )
    )

    st.write(
        "-"
        if strike is None
        else str(strike)
    )


with o4:

    st.write("**Lot Size**")

    lot = (
        st.session_state.get(
            "option_lot_size"
        )
    )

    st.write(
        "-"
        if lot is None
        else str(lot)
    )


# ============================================================
# AUTOMATIC ORDER STATUS
# ============================================================

st.subheader(
    "Automatic Order Status"
)

order_id = (
    st.session_state.get(
        "last_order_id"
    )
)

order_status = (
    st.session_state.get(
        "last_order_status"
    )
)

if order_id:

    st.success(
        "BUY CE SENT"
    )

    st.write(
        "**Order ID:**",
        order_id
    )

    st.write(
        "**Status:**",
        order_status or "-"
    )

elif order_status:

    if order_status == "BUY CE SENT":

        st.success(
            "AUTOMATIC BUY CE SENT"
        )

    else:

        st.error(
            order_status
        )

else:

    st.info(
        "No automatic CE order yet."
    )


# ============================================================
# POSITION
# ============================================================

st.subheader(
    "Automatic CE Position"
)

position = (
    st.session_state.get(
        "position"
    )
)

if position:

    st.json(
        position
    )

else:

    st.info(
        "No automatic CE position recorded."
    )


# ============================================================
# ORDER BOOK
# ============================================================

st.subheader(
    "📋 Order Book"
)

orders = build_order_book()


if orders:

    order_df = pd.DataFrame(
        orders
    )

    preferred_columns = [
        "time",
        "order_id",
        "mode",
        "status",
        "side",
        "symbol",
        "token",
        "quantity",
        "price",
    ]

    available_columns = [
        col
        for col in preferred_columns
        if col in order_df.columns
    ]

    order_df = order_df[
        available_columns
    ]

    st.dataframe(
        order_df,
        use_container_width=True,
        hide_index=True
    )

else:

    st.info(
        "No automatic order yet."
    )


# ============================================================
# IMPORTANT MESSAGE
# ============================================================

if (
    st.session_state.get(
        "last_order_id"
    )
):

    st.success(
        "✅ The automatic BUY CE submission "
        "has been added to the Order Book display."
    )


# ============================================================
# AUTOMATION STATUS
# ============================================================

st.subheader(
    "Automation Status"
)

st.write(
    st.session_state.get(
        "last_message",
        "Ready"
    )
)


# ============================================================
# ERROR
# ============================================================

if automation_error:

    with st.expander(
        "Error diagnostics",
        expanded=True
    ):

        st.error(
            automation_error
        )

        if (
            "TOTP"
            in automation_error
            or
            "base32"
            in automation_error.lower()
        ):

            st.code(
                "Use the actual ANGEL_TOTP_SECRET "
                "base32 secret, not the 6-digit OTP."
            )

        if (
            "Invalid API Key"
            in automation_error
        ):

            st.code(
                "Check ANGEL_API_KEY."
            )

        if is_rate_limit_error(
            automation_error
        ):

            st.code(
                "Angel One API rate limit detected. "
                "The application will wait before "
                "requesting candles again."
            )


# ============================================================
# HOW IT WORKS
# ============================================================

with st.expander(
    "How automatic BUY CE works"
):

    st.markdown(
        """
### AUTOMATIC FLOW

NIFTY 1-minute candles
↓
2-minute candles
↓
Completed 2-minute candle
↓
Supertrend (20, 1.5)
↓
RED → GREEN
↓
Select nearest ATM NIFTY CE
↓
Automatic MARKET BUY
↓
Show submitted order in Order Book

### GREEN WITHOUT A NEW FLIP

If Supertrend remains GREEN:

**WAIT**

It does NOT buy another CE.

### RED

**WAIT**

### Protection

- Current/forming candle is excluded.
- Only completed 2-minute candles are used.
- BUY occurs on RED → GREEN.
- Same candle cannot trigger twice.
- One automatic BUY attempt per trading day.
- No manual BUY button.
- No manual SELL button.
- No broker confirmation is required before sending the BUY.
- Order Book is display-only.
- The locally recorded submitted order is shown immediately.
"""
    )


# ============================================================
# REFRESH
# ============================================================

st.caption(
    f"Refresh: {REFRESH_SECONDS}s | "
    f"Candle interval: {CANDLE_MIN_INTERVAL}s | "
    f"Supertrend: {ST_PERIOD}, {ST_MULTIPLIER}"
)


# ============================================================
# AUTO REFRESH
# ============================================================

time.sleep(
    REFRESH_SECONDS
)

st.rerun()
