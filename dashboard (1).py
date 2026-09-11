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
# Supertrend (20, 1.5)
#       ↓
# GREEN  -> AUTOMATIC BUY ATM NIFTY CE
# RED    -> WAIT
#
# ONLY 2-MINUTE SUPERTREND
#
# NO:
#   - 5-minute Supertrend
#   - 15-minute Supertrend
#   - 4-hour Supertrend
#   - manual BUY/SELL buttons
#
# IMPORTANT:
#   The current/forming 2-minute candle is NEVER used
#   for automatic BUY decisions.
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
# CONFIGURATION
# ============================================================

IST = ZoneInfo("Asia/Kolkata")


# ============================================================
# ANGEL ONE CREDENTIALS
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
# TRADING MODE
# ============================================================
#
# PAPER_TRADING=true
#     No real broker order.
#
# PAPER_TRADING=false
#     REAL Angel One order.
#
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
# 2-MINUTE SUPERTREND
# ============================================================

ST_PERIOD = 20
ST_MULTIPLIER = 1.5


# ============================================================
# NIFTY
# ============================================================

NIFTY_SYMBOL = "NIFTY"
NIFTY_TOKEN = "99926000"


# ============================================================
# OPTION ORDER
# ============================================================

EXCHANGE = "NFO"
PRODUCT_TYPE = "INTRADAY"
ORDER_TYPE = "MARKET"
DURATION = "DAY"

LOTS = int(
    os.getenv(
        "NIFTY_LOTS",
        "1"
    )
)


# ============================================================
# REFRESH / RATE LIMIT CONTROL
# ============================================================

REFRESH_SECONDS = int(
    os.getenv(
        "REFRESH_SECONDS",
        "60"
    )
)

# Never fetch candle data more frequently than this.
CANDLE_MIN_INTERVAL = 120

# Angel One rate-limit cooldown.
RATE_LIMIT_COOLDOWN = 180


# ============================================================
# TRADING WINDOW
# ============================================================

ENTRY_START = dt_time(
    9,
    20
)

ENTRY_END = dt_time(
    15,
    15
)


# ============================================================
# LOCAL FILES
# ============================================================

BASE_DIR = Path(
    __file__
).resolve().parent

STATE_FILE = (
    BASE_DIR
    / "nifty_ce_state.json"
)

INSTRUMENT_CACHE = (
    BASE_DIR
    / "OpenAPIScripMaster.json"
)


# ============================================================
# ANGEL ONE SCRIP MASTER
# ============================================================

SCRIP_MASTER_URL = (
    "https://margincalculator.angelone.in/"
    "OpenAPI_File/files/OpenAPIScripMaster.json"
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

    "st_value":
        None,

    "st_direction":
        None,

    "green":
        False,

    "red":
        False,

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

    "option_ltp":
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
}


for key, value in DEFAULTS.items():

    if key not in st.session_state:

        st.session_state[
            key
        ] = value


# ============================================================
# TIME
# ============================================================

def now_ist():

    return pd.Timestamp.now(
        tz=IST
    )


# ============================================================
# BASIC HELPERS
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
                    .replace(
                        " ",
                        ""
                    )
                    .replace(
                        "-",
                        ""
                    )
                    .upper()
                )

        except Exception:

            pass

    return (
        raw
        .replace(
            " ",
            ""
        )
        .replace(
            "-",
            ""
        )
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

    keywords = [
        "rate limit",
        "rate-limit",
        "too many",
        "throttle",
        "throttled",
        "exceed",
        "429",
    ]

    return any(
        x in text
        for x in keywords
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
            f"State load failed: {exc}"
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
            f"State save failed: {exc}"
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

    try:

        if (
            secret.isdigit()
            and len(secret) == 6
        ):

            raise RuntimeError(
                "ANGEL_TOTP_SECRET contains "
                "the 6-digit OTP. Use the actual "
                "base32 TOTP secret."
            )

        totp = pyotp.TOTP(
            secret
        ).now()

    except Exception as exc:

        raise RuntimeError(
            "Invalid ANGEL_TOTP_SECRET: "
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
# GET LAST COMPLETED 2-MINUTE BOUNDARY
#
# NSE session starts at 09:15.
#
# Examples:
#
# 09:16 -> no 2m candle completed after 09:15
#
# 09:17 -> 09:15-09:17 candle is complete
#
# 09:18 -> 09:17-09:19 is NOT complete yet
#
# 09:19 -> 09:17-09:19 is complete
#
# 09:20 -> 09:19-09:21 is NOT complete yet
#
# This prevents the current/forming 2-minute candle
# from being used for an automatic order.
# ============================================================

def last_completed_2min_boundary(
    current_time=None
):

    if current_time is None:

        current_time = now_ist()

    current_time = pd.Timestamp(
        current_time
    )

    if current_time.tzinfo is None:

        current_time = current_time.tz_localize(
            IST
        )

    else:

        current_time = current_time.tz_convert(
            IST
        )

    market_open = pd.Timestamp(
        datetime.combine(
            current_time.date(),
            dt_time(
                9,
                15
            )
        ),
        tz=IST
    )

    if current_time < market_open:

        return None

    elapsed_seconds = (
        current_time
        - market_open
    ).total_seconds()

    completed_blocks = int(
        elapsed_seconds
        // 120
    )

    return (
        market_open
        + pd.Timedelta(
            seconds=completed_blocks * 120
        )
    )


# ============================================================
# FETCH 1-MINUTE NIFTY CANDLES
# ============================================================

def get_nifty_1m_candles(
    api,
    days=3
):

    end = now_ist()

    start = (
        end
        - timedelta(
            days=days
        )
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

        response = (
            api.getCandleData(
                params
            )
        )

    except Exception as exc:

        if is_rate_limit_error(
            exc
        ):

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

        message = str(
            response
        )

        if is_rate_limit_error(
            message
        ):

            set_rate_limit()

        raise RuntimeError(
            "Candle API failed: "
            + message
        )

    rows = (
        response.get(
            "data"
        )
        or []
    )

    if not rows:

        raise RuntimeError(
            "Candle API returned no "
            "NIFTY 1-minute candles."
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

    for col in [
        "open",
        "high",
        "low",
        "close",
        "volume",
    ]:

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

    # --------------------------------------------------------
    # REMOVE CURRENT FORMING 1-MINUTE CANDLE
    # --------------------------------------------------------

    current_minute = (
        now_ist()
        .floor("min")
    )

    df = df[
        df["datetime"]
        < current_minute
    ].copy()

    # --------------------------------------------------------
    # NSE MARKET HOURS
    # --------------------------------------------------------

    df = df[
        (
            df["datetime"].dt.time
            >= dt_time(
                9,
                15
            )
        )
        &
        (
            df["datetime"].dt.time
            <= dt_time(
                15,
                30
            )
        )
    ].copy()

    if df.empty:

        raise RuntimeError(
            "No completed NSE 1-minute "
            "candles available."
        )

    return df


# ============================================================
# 2-MINUTE RESAMPLING
# ============================================================

def make_2min_candles(
    df1
):

    if df1.empty:

        raise RuntimeError(
            "1-minute candle dataframe "
            "is empty."
        )

    x = (
        df1
        .copy()
        .set_index(
            "datetime"
        )
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
# REMOVE CURRENT/INCOMPLETE 2-MINUTE CANDLE
# ============================================================

def get_completed_2min_candles(
    df2
):

    if df2.empty:

        raise RuntimeError(
            "2-minute dataframe is empty."
        )

    boundary = (
        last_completed_2min_boundary()
    )

    if boundary is None:

        raise RuntimeError(
            "2-minute candle boundary "
            "is not available yet."
        )

    completed = df2[
        df2["datetime"]
        <= boundary
    ].copy()

    if completed.empty:

        raise RuntimeError(
            "No completed 2-minute candle "
            "is available yet."
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
            f"Not enough 2-minute candles "
            f"for Supertrend {period},{multiplier}. "
            f"Received {len(df)}."
        )

    high = (
        df["high"]
        .astype(float)
    )

    low = (
        df["low"]
        .astype(float)
    )

    close = (
        df["close"]
        .astype(float)
    )

    previous_close = (
        close.shift(1)
    )

    tr = pd.concat(
        [
            high - low,

            (
                high
                - previous_close
            ).abs(),

            (
                low
                - previous_close
            ).abs(),
        ],
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

        previous_fu = (
            final_upper.iloc[i - 1]
        )

        previous_fl = (
            final_lower.iloc[i - 1]
        )

        if (
            pd.isna(previous_fu)
            or
            basic_upper.iloc[i]
            < previous_fu
            or
            close.iloc[i - 1]
            > previous_fu
        ):

            final_upper.iloc[i] = (
                basic_upper.iloc[i]
            )

        else:

            final_upper.iloc[i] = (
                previous_fu
            )

        if (
            pd.isna(previous_fl)
            or
            basic_lower.iloc[i]
            > previous_fl
            or
            close.iloc[i - 1]
            < previous_fl
        ):

            final_lower.iloc[i] = (
                basic_lower.iloc[i]
            )

        else:

            final_lower.iloc[i] = (
                previous_fl
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
            "ATR could not be calculated."
        )

    first_i = (
        df.index.get_loc(
            first_valid
        )
    )

    direction.iloc[
        first_i
    ] = 1

    supertrend.iloc[
        first_i
    ] = final_lower.iloc[
        first_i
    ]

    for i in range(
        first_i + 1,
        len(df)
    ):

        previous_st = (
            supertrend.iloc[i - 1]
        )

        if pd.isna(
            previous_st
        ):

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

    df["final_upper"] = (
        final_upper
    )

    df["final_lower"] = (
        final_lower
    )

    df["supertrend"] = (
        supertrend
    )

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
# GET COMPLETED 2-MINUTE SIGNAL
# ============================================================

def get_2min_signal(
    df2
):

    # --------------------------------------------------------
    # VERY IMPORTANT:
    # Remove the current/incomplete 2-minute candle BEFORE
    # calculating the final signal.
    # --------------------------------------------------------

    completed = (
        get_completed_2min_candles(
            df2
        )
    )

    if len(completed) < (
        ST_PERIOD + 5
    ):

        raise RuntimeError(
            "Not enough COMPLETED 2-minute "
            "candles for Supertrend "
            f"{ST_PERIOD},{ST_MULTIPLIER}. "
            f"Received {len(completed)}."
        )

    st_df = calculate_supertrend(
        completed,
        period=ST_PERIOD,
        multiplier=ST_MULTIPLIER
    )

    row = st_df.iloc[-1]

    direction = row[
        "direction"
    ]

    if pd.isna(
        direction
    ):

        raise RuntimeError(
            "Latest completed 2-minute "
            "Supertrend direction is "
            "not available."
        )

    direction = int(
        direction
    )

    green = (
        direction == 1
    )

    red = (
        direction == -1
    )

    signal = (
        "BUY CE"
        if green
        else "WAIT"
    )

    return {

        "signal":
            signal,

        "candle_time":
            row["datetime"],

        "close":
            float(
                row["close"]
            ),

        "supertrend":
            (
                float(
                    row["supertrend"]
                )
                if pd.notna(
                    row["supertrend"]
                )
                else None
            ),

        "direction":
            direction,

        "green":
            green,

        "red":
            red,

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
        not isinstance(
            data,
            list
        )
        or not data
    ):

        raise RuntimeError(
            "Angel One scrip master is "
            "empty/invalid."
        )

    return data


def load_instruments():

    if (
        st.session_state.get(
            "instruments"
        )
        is not None
    ):

        return st.session_state[
            "instruments"
        ]

    try:

        data = (
            download_scrip_master()
        )

    except Exception as exc:

        if INSTRUMENT_CACHE.exists():

            data = json.loads(
                INSTRUMENT_CACHE.read_text(
                    encoding="utf-8"
                )
            )

        else:

            raise RuntimeError(
                "Could not download Angel One "
                "scrip master and no local cache "
                f"exists. Error: {exc}"
            )

    try:

        INSTRUMENT_CACHE.write_text(
            json.dumps(
                data
            ),
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

    formats = [

        "%d%b%Y",

        "%d%b%y",

        "%Y-%m-%d",

        "%d-%b-%Y",

        "%d/%m/%Y",
    ]

    for fmt in formats:

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

        value = float(
            raw
        )

        # Angel master can store strike as e.g. 2500000
        # instead of 25000.
        if value > 100000:

            value /= 100.0

        return value

    except Exception:

        return None


# ============================================================
# ATM CE SELECTION
#
# IMPORTANT:
# Select the nearest AVAILABLE NIFTY CE strike rather
# than requiring an exact calculated 50-point strike.
# ============================================================

def select_atm_nifty_ce(
    instruments,
    spot
):

    if not instruments:

        raise RuntimeError(
            "Scrip master is empty."
        )

    if spot is None:

        raise RuntimeError(
            "NIFTY Spot is unavailable "
            "for ATM CE selection."
        )

    today = (
        now_ist()
        .date()
    )

    candidates = []

    for item in instruments:

        if not isinstance(
            item,
            dict
        ):

            continue

        exch_seg = str(
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

        # ----------------------------------------------------
        # NFO
        # ----------------------------------------------------

        if exch_seg != "NFO":

            continue

        # ----------------------------------------------------
        # NIFTY
        # ----------------------------------------------------

        if (
            "NIFTY" not in name
            and
            not symbol.startswith(
                "NIFTY"
            )
        ):

            continue

        # ----------------------------------------------------
        # CE ONLY
        # ----------------------------------------------------

        if not symbol.endswith(
            "CE"
        ):

            continue

        # ----------------------------------------------------
        # INDEX OPTION
        # ----------------------------------------------------

        if instrument_type not in (
            "",
            "OPTIDX",
        ):

            continue

        strike = normalize_strike(
            item.get(
                "strike"
            )
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

            lot_size = None

        if not lot_size:

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
                    str(
                        expiry_raw
                    ),

                "strike":
                    int(
                        round(
                            strike
                        )
                    ),

                "lot_size":
                    lot_size,
            }
        )

    if not candidates:

        raise RuntimeError(
            "No future NIFTY CE options "
            "found in scrip master."
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
    # SECOND: nearest available strike to spot
    # --------------------------------------------------------

    expiry_candidates.sort(
        key=lambda x: (
            abs(
                x["strike"]
                - float(spot)
            ),
            x["strike"],
            x["symbol"],
        )
    )

    return expiry_candidates[0]


# ============================================================
# ORDER BOOK
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

    orders.insert(
        0,
        order
    )

    st.session_state[
        "order_book"
    ] = orders[:50]


def fetch_broker_order_book(
    api
):

    try:

        response = (
            api.orderBook()
        )

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
            response.get(
                "data"
            )
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

    except Exception:

        return []


# ============================================================
# FIND ORDER
# ============================================================

def find_order(
    orders,
    order_id=None,
    symbol=None
):

    for item in orders:

        broker_id = str(
            item.get(
                "order_id",
                ""
            )
        )

        broker_symbol = str(
            item.get(
                "symbol",
                ""
            )
        ).upper()

        if (
            order_id
            and
            broker_id
            == str(order_id)
        ):

            return item

        if (
            symbol
            and
            broker_symbol
            == str(symbol).upper()
        ):

            return item

    return None



# ============================================================
# EXTRACT BROKER ORDER ID
# ============================================================

def extract_order_id(response):
    """
    Extract the real Angel One order ID from the different
    response shapes returned by SmartAPI.
    """
    if response is None:
        return None

    if isinstance(response, str):
        value = response.strip()
        if not value:
            return None

        # Some wrappers may return a JSON string.
        if value.startswith("{"):
            try:
                parsed = json.loads(value)
                return extract_order_id(parsed)
            except Exception:
                pass

        # A normal placeOrder() response is usually the ID itself.
        return value

    if isinstance(response, dict):
        # Prefer the standard SmartAPI keys first.
        for key in (
            "orderid",
            "orderId",
            "order_id",
            "orderID",
        ):
            value = response.get(key)
            if value is not None and str(value).strip():
                return str(value).strip()

        # Search nested data/response/result objects.
        for key in (
            "data",
            "response",
            "result",
            "body",
        ):
            value = response.get(key)
            found = extract_order_id(value)
            if found:
                return found

        return None

    # Be tolerant of SmartAPI wrapper objects.
    for attr in (
        "orderid",
        "orderId",
        "order_id",
        "orderID",
        "data",
        "response",
        "result",
    ):
        try:
            value = getattr(response, attr, None)
        except Exception:
            value = None

        if value is not None:
            found = extract_order_id(value)
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
        * option["lot_size"]
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
            ORDER_TYPE,

        "producttype":
            PRODUCT_TYPE,

        "duration":
            DURATION,

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

    # ========================================================
    # PAPER MODE
    # ========================================================

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
                "PAPER ORDER",

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

        return (
            order_id,
            order
        )

    # ========================================================
    # LIVE ORDER
    # ========================================================

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

        if is_rate_limit_error(
            exc
        ):

            set_rate_limit()

        raise RuntimeError(
            "Angel One order request failed: "
            + str(exc)
        )

    if response is None:

        raise RuntimeError(
            "Angel One returned EMPTY "
            "order response."
        )

    if isinstance(response, dict):

        if (
            response.get("status")
            is False
        ):

            raise RuntimeError(
                "Angel One order failed: "
                + str(response)
            )

    order_id = extract_order_id(
        response
    )

    # --------------------------------------------------------
    # NEVER invent a broker order ID.
    # --------------------------------------------------------

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

    # Keep a local audit row, but never use LIVE-UNKNOWN.
    add_local_order(
        order
    )

    return (
        order_id,
        order
    )


# ============================================================
# FIND RECENT MATCHING BROKER ORDER
# ============================================================

def find_recent_matching_order(
    orders,
    symbol,
    quantity=None,
    side="BUY"
):

    if not orders:
        return None

    wanted_symbol = str(
        symbol or ""
    ).upper()

    wanted_side = str(
        side or ""
    ).upper()

    matches = []

    for item in orders:

        if not isinstance(
            item,
            dict
        ):
            continue

        item_symbol = str(
            item.get(
                "symbol",
                ""
            )
        ).upper()

        item_side = str(
            item.get(
                "side",
                ""
            )
        ).upper()

        if item_symbol != wanted_symbol:
            continue

        if wanted_side and item_side != wanted_side:
            continue

        if quantity is not None:

            try:
                item_qty = int(
                    float(
                        item.get(
                            "quantity",
                            0
                        )
                    )
                )
                if item_qty != int(
                    quantity
                ):
                    continue
            except Exception:
                continue

        matches.append(
            item
        )

    if not matches:
        return None

    # Prefer the most recently updated order.
    def sort_key(item):

        raw = (
            item.get("time")
            or ""
        )

        try:
            ts = pd.to_datetime(
                raw,
                errors="coerce"
            )
            if pd.isna(ts):
                return pd.Timestamp.min
            return ts
        except Exception:
            return pd.Timestamp.min

    matches.sort(
        key=sort_key,
        reverse=True
    )

    return matches[0]


# ============================================================
# VERIFY LIVE ORDER
# ============================================================

def verify_live_order(
    api,
    order_id,
    symbol,
    quantity=None,
    attempts=4,
    delay_seconds=2
):

    if PAPER_TRADING:
        return None

    for attempt in range(
        attempts
    ):

        orders = (
            fetch_broker_order_book(
                api
            )
        )

        # If the broker gave us a real ID, match by ID first.
        if order_id:

            found = find_order(
                orders,
                order_id=order_id,
                symbol=None
            )

        else:

            found = (
                find_recent_matching_order(
                    orders,
                    symbol=symbol,
                    quantity=quantity,
                    side="BUY"
                )
            )

        # When no ID was returned by placeOrderFullResponse,
        # identify the newly created order from symbol/side/qty.
        if (
            found is None
            and not order_id
        ):

            found = (
                find_recent_matching_order(
                    orders,
                    symbol=symbol,
                    quantity=quantity,
                    side="BUY"
                )
            )

        if found:
            return found

        if attempt < attempts - 1:
            time.sleep(
                delay_seconds
            )

    return None


# ============================================================
# BROKER STATUS HELPERS
# ============================================================

def normalize_order_status(value):

    return str(
        value or ""
    ).strip().upper()


def order_is_filled(status):

    return normalize_order_status(
        status
    ) in (
        "COMPLETE",
        "COMPLETED",
        "EXECUTED",
        "FILLED",
    )


def order_is_rejected(status):

    return normalize_order_status(
        status
    ) in (
        "REJECTED",
        "CANCELLED",
        "CANCELED",
        "FAILED",
        "ERROR",
    )


def record_live_position(
    option,
    quantity,
    candle_key,
    found
):

    broker_status = (
        found.get(
            "status"
        )
        or "COMPLETE"
    )

    st.session_state[
        "last_order_id"
    ] = (
        found.get(
            "order_id"
        )
        or st.session_state.get(
            "last_order_id"
        )
    )

    st.session_state[
        "last_order_status"
    ] = broker_status

    # A position is recorded ONLY after the broker
    # reports a filled/completed order.
    if not order_is_filled(
        broker_status
    ):
        return False

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
            found.get(
                "price",
                "MARKET"
            ),

        "signal_candle":
            candle_key,

        "mode":
            "LIVE",

        "broker_status":
            broker_status,
    }

    return True




# ============================================================
# RECONCILE LIVE ORDER
# ============================================================

def reconcile_live_order(api):
    """Read-only reconciliation of the tracked LIVE BUY CE order.

    Never places another order. A position is recorded only when
    Angel One reports a filled/completed status.
    """

    if PAPER_TRADING or api is None:
        return None

    order_id = st.session_state.get("last_order_id")
    symbol = st.session_state.get("option_symbol")

    if not order_id and not symbol:
        return None

    orders = fetch_broker_order_book(api)
    if not orders:
        if order_id:
            st.session_state["last_order_status"] = (
                "AWAITING BROKER CONFIRMATION"
            )
        return None

    found = None
    if order_id:
        found = find_order(orders, order_id=order_id, symbol=None)

    if found is None and symbol:
        found = find_recent_matching_order(
            orders, symbol=symbol, quantity=None, side="BUY"
        )

    if found is None:
        if order_id:
            st.session_state["last_order_status"] = (
                "AWAITING BROKER CONFIRMATION"
            )
        return None

    broker_id = str(found.get("order_id") or order_id or "").strip()
    if broker_id:
        st.session_state["last_order_id"] = broker_id

    broker_status = normalize_order_status(found.get("status")) or "SUBMITTED"
    st.session_state["last_order_status"] = broker_status

    # Update the local audit row with the broker-confirmed row.
    old = st.session_state.get("order_book", [])
    st.session_state["order_book"] = [found] + [
        x for x in old
        if str(x.get("order_id", "")) != broker_id
    ]

    quantity = found.get("quantity") or (
        LOTS * (st.session_state.get("option_lot_size") or 0)
    )

    option = {
        "symbol": found.get("symbol") or symbol,
        "token": found.get("token") or st.session_state.get("option_token"),
        "strike": st.session_state.get("option_strike"),
        "expiry_raw": st.session_state.get("option_expiry"),
    }

    if order_is_filled(broker_status):
        st.session_state["in_position"] = True
        st.session_state["position"] = {
            "symbol": option["symbol"],
            "token": option["token"],
            "strike": option["strike"],
            "expiry": option["expiry_raw"],
            "quantity": quantity,
            "entry_price": found.get("price", "MARKET"),
            "signal_candle": st.session_state.get("signal_time"),
            "mode": "LIVE",
            "broker_status": broker_status,
            "order_id": broker_id,
        }
        st.session_state["last_message"] = (
            "LIVE BUY CE confirmed by Angel One: "
            f"{option['symbol']} | Order ID: {broker_id} | Status: {broker_status}"
        )
    elif order_is_rejected(broker_status):
        st.session_state["in_position"] = False
        st.session_state["position"] = None
        st.session_state["last_message"] = (
            f"Angel One BUY CE order was not filled: {broker_status} | "
            f"Order ID: {broker_id}"
        )
    else:
        st.session_state["in_position"] = False
        st.session_state["position"] = None
        st.session_state["last_message"] = (
            f"Angel One BUY CE order is still pending: {broker_status} | "
            f"Order ID: {broker_id}"
        )

    save_state()
    return found


def automatic_buy_ce(
    api,
    option,
    candle_key
):
    """Send the automatic ATM NIFTY CE BUY immediately.

    IMPORTANT:
    - No broker order-book confirmation is required.
    - No reconciliation is performed before/after sending.
    - The strategy is triggered only from a completed 2-minute
      GREEN Supertrend candle.
    - One automatic BUY attempt is allowed per trading day.
    """

    # Same completed candle must never trigger twice.
    if st.session_state.get("last_processed_candle") == candle_key:
        return

    today = now_ist().date().isoformat()

    # New trading day -> reset daily BUY protection.
    if st.session_state.get("trade_date") != today:
        st.session_state["trade_date"] = today
        st.session_state["automatic_order_attempted"] = False
        st.session_state["last_order_id"] = None
        st.session_state["last_order_time"] = None
        st.session_state["last_order_status"] = None
        st.session_state["in_position"] = False
        st.session_state["position"] = None

    # One automatic BUY attempt per day.
    if st.session_state.get("automatic_order_attempted"):
        st.session_state["last_processed_candle"] = candle_key
        st.session_state["last_message"] = (
            "Automatic BUY CE already attempted today. Duplicate BUY blocked."
        )
        save_state()
        return

    quantity = LOTS * int(option["lot_size"])

    try:
        # ----------------------------------------------------
        # PAPER MODE
        # ----------------------------------------------------
        if PAPER_TRADING:
            order_id = f"PAPER-{now_ist().strftime('%Y%m%d%H%M%S%f')}"

            order = {
                "time": now_ist().strftime("%Y-%m-%d %H:%M:%S"),
                "order_id": order_id,
                "mode": "PAPER",
                "status": "BUY CE SENT",
                "side": "BUY",
                "symbol": option["symbol"],
                "token": option["token"],
                "expiry": option["expiry_raw"],
                "strike": option["strike"],
                "quantity": quantity,
                "price": "MARKET",
            }

            add_local_order(order)

            st.session_state["last_order_id"] = order_id
            st.session_state["last_order_time"] = now_ist().isoformat()
            st.session_state["last_order_status"] = "BUY CE SENT"
            st.session_state["automatic_order_attempted"] = True
            st.session_state["last_processed_candle"] = candle_key
            st.session_state["in_position"] = True
            st.session_state["position"] = {
                "symbol": option["symbol"],
                "token": option["token"],
                "strike": option["strike"],
                "expiry": option["expiry_raw"],
                "quantity": quantity,
                "entry_price": "MARKET",
                "signal_candle": candle_key,
                "mode": "PAPER",
            }
            st.session_state["last_message"] = (
                f"AUTOMATIC BUY CE SENT: {option['symbol']} | Qty: {quantity}"
            )
            save_state()
            return

        # ----------------------------------------------------
        # LIVE ANGEL ONE ORDER
        # ----------------------------------------------------
        # place_buy_ce() sends the MARKET BUY directly.
        # We DO NOT call verify_live_order() or
        # reconcile_live_order() here.
        order_id, order = place_buy_ce(api, option)

        # Mark the candle/day only after the API request was
        # successfully accepted by the SmartAPI call.
        st.session_state["automatic_order_attempted"] = True
        st.session_state["last_processed_candle"] = candle_key
        st.session_state["last_order_id"] = order_id or None
        st.session_state["last_order_time"] = now_ist().isoformat()
        st.session_state["last_order_status"] = "BUY CE SENT"

        # This is local strategy state only; it is NOT a claim
        # that Angel One has filled the order.
        st.session_state["in_position"] = True
        st.session_state["position"] = {
            "symbol": option["symbol"],
            "token": option["token"],
            "strike": option["strike"],
            "expiry": option["expiry_raw"],
            "quantity": quantity,
            "entry_price": "MARKET",
            "signal_candle": candle_key,
            "mode": "LIVE",
        }

        order_text = (
            f" | Order ID: {order_id}"
            if order_id
            else ""
        )

        st.session_state["last_message"] = (
            f"AUTOMATIC BUY CE SENT: {option['symbol']} | "
            f"Qty: {quantity}{order_text}"
        )

        save_state()

    except Exception as exc:
        # Do not mark the day as successfully attempted if the
        # SmartAPI request itself raised an exception.
        st.session_state["last_order_status"] = "BUY ERROR"
        st.session_state["last_message"] = (
            "Automatic BUY CE error: " + str(exc)
        )
        save_state()
        raise


# ============================================================
# AUTOMATION
# ============================================================

def run_automation():

    api = angel_login()

    # --------------------------------------------------------
    # RECONCILE ANY PREVIOUSLY SUBMITTED LIVE ORDER
    #
    # This only checks Angel One Order Book. It NEVER sends
    # another order.
    # --------------------------------------------------------


    # --------------------------------------------------------
    # OUTSIDE ENTRY WINDOW
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # RATE LIMIT COOLDOWN
    # --------------------------------------------------------

    if rate_limit_active():

        st.session_state[
            "last_message"
        ] = (
            "Angel One API rate-limit cooldown "
            "is active. Waiting before the next "
            "candle request."
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

            last_fetch_ts = pd.Timestamp(
                last_fetch
            )

            if last_fetch_ts.tzinfo is None:

                last_fetch_ts = (
                    last_fetch_ts.tz_localize(
                        IST
                    )
                )

            else:

                last_fetch_ts = (
                    last_fetch_ts.tz_convert(
                        IST
                    )
                )

            seconds_since = (
                now_ist()
                - last_fetch_ts
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
            "Using recent 1-minute candle "
            "data. Waiting for next candle "
            "request window."
        )

        return

    # --------------------------------------------------------
    # FETCH ONLY 1-MINUTE DATA
    # --------------------------------------------------------

    try:

        df1 = (
            get_nifty_1m_candles(
                api,
                days=3
            )
        )

    except Exception as exc:

        if is_rate_limit_error(
            exc
        ):

            set_rate_limit()

        raise

    st.session_state[
        "last_candle_fetch"
    ] = now_ist().isoformat()

    # --------------------------------------------------------
    # NIFTY SPOT
    #
    # Latest completed 1-minute candle close.
    # No ltpData() call is made.
    # --------------------------------------------------------

    latest_1m = df1.iloc[-1]

    spot = safe_float(
        latest_1m["close"]
    )

    if spot is None:

        raise RuntimeError(
            "Could not determine NIFTY Spot "
            "from completed 1-minute candle."
        )

    st.session_state[
        "spot"
    ] = spot

    st.session_state[
        "spot_source"
    ] = (
        "Latest completed 1-minute candle close"
    )

    # --------------------------------------------------------
    # CREATE 2-MINUTE CANDLES
    # --------------------------------------------------------

    df2 = make_2min_candles(
        df1
    )

    # --------------------------------------------------------
    # ONLY 2-MINUTE SUPERTREND
    # --------------------------------------------------------

    info = get_2min_signal(
        df2
    )

    st.session_state[
        "signal"
    ] = info["signal"]

    st.session_state[
        "signal_time"
    ] = (
        info["candle_time"]
        .isoformat()
    )

    st.session_state[
        "st_value"
    ] = info["supertrend"]

    st.session_state[
        "st_direction"
    ] = info["direction"]

    st.session_state[
        "green"
    ] = info["green"]

    st.session_state[
        "red"
    ] = info["red"]

    st.session_state[
        "last_fresh_candle"
    ] = (
        info["candle_time"]
        .isoformat()
    )

    candle_key = (
        info["candle_time"]
        .isoformat()
    )

    # --------------------------------------------------------
    # GREEN = BUY CE
    # RED = WAIT
    # --------------------------------------------------------

    if info["signal"] != "BUY CE":

        st.session_state[
            "last_processed_candle"
        ] = candle_key

        st.session_state[
            "last_message"
        ] = (
            "Completed 2-minute Supertrend "
            "is RED. Signal = WAIT."
        )

        save_state()

        return

    # --------------------------------------------------------
    # GREEN
    # --------------------------------------------------------

    st.session_state[
        "last_message"
    ] = (
        "Completed 2-minute Supertrend is "
        "GREEN. BUY CE condition detected."
    )

    # --------------------------------------------------------
    # SAME CANDLE
    # --------------------------------------------------------

    if (
        st.session_state.get(
            "last_processed_candle"
        )
        == candle_key
    ):

        st.session_state[
            "last_message"
        ] = (
            "This completed 2-minute GREEN "
            "candle has already been processed."
        )

        return

    # --------------------------------------------------------
    # ONE ORDER PER DAY
    # --------------------------------------------------------

    today = (
        now_ist()
        .date()
        .isoformat()
    )

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
            "in_position"
        ] = False

        st.session_state[
            "position"
        ] = None

    if st.session_state.get(
        "automatic_order_attempted"
    ):

        st.session_state[
            "last_processed_candle"
        ] = candle_key

        st.session_state[
            "last_message"
        ] = (
            "BUY CE already attempted today. "
            "Duplicate order blocked."
        )

        save_state()

        return

    # --------------------------------------------------------
    # LOAD SCRIP MASTER
    # --------------------------------------------------------

    instruments = (
        load_instruments()
    )

    # --------------------------------------------------------
    # SELECT ATM CE
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # NO OPTION LTP CALL
    # --------------------------------------------------------

    st.session_state[
        "option_ltp"
    ] = None

    # --------------------------------------------------------
    # AUTOMATIC BUY
    # --------------------------------------------------------

    automatic_buy_ce(
        api,
        option,
        candle_key
    )


# ============================================================
# LOAD SAVED STATE
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
# PAGE HEADER
# ============================================================

st.title(
    "📈 NIFTY 2-Minute Automatic BUY CE"
)

st.caption(
    "ONLY 2-Minute Supertrend (20, 1.5) "
    "→ GREEN = AUTOMATIC BUY ATM NIFTY CE"
)


# ============================================================
# SAFETY MESSAGE
# ============================================================

if PAPER_TRADING:

    st.warning(
        "PAPER TRADING MODE — no real Angel One "
        "order will be sent."
    )

else:

    st.error(
        "LIVE TRADING MODE — a completed GREEN "
        "2-minute Supertrend can send a REAL "
        "BUY NIFTY CE order."
    )


# ============================================================
# RUN AUTOMATION
# ============================================================

automation_error = None

try:

    run_automation()

except Exception as exc:

    automation_error = str(
        exc
    )

    st.session_state[
        "last_error"
    ] = automation_error

    st.session_state[
        "last_message"
    ] = (
        "Automation error."
    )


# ============================================================
# MAIN METRICS
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

    if spot is None:

        spot_text = "-"

    else:

        spot_text = (
            f"{spot:,.2f}"
        )

    st.metric(
        "NIFTY Spot",
        spot_text
    )


with c3:

    st.metric(
        "2-Min Signal",
        st.session_state.get(
            "signal",
            "WAIT"
        )
    )


with c4:

    direction = (
        st.session_state.get(
            "st_direction"
        )
    )

    if direction == 1:

        direction_text = "GREEN"

    elif direction == -1:

        direction_text = "RED"

    else:

        direction_text = "-"

    st.metric(
        "Supertrend",
        direction_text
    )


with c5:

    st.metric(
        "Mode",
        (
            "PAPER"
            if PAPER_TRADING
            else "LIVE"
        )
    )


# ============================================================
# NIFTY SPOT DETAILS
# ============================================================

st.subheader(
    "NIFTY Spot"
)

spot = st.session_state.get(
    "spot"
)

if spot is not None:

    st.success(
        f"₹ {spot:,.2f}"
    )

    st.caption(
        "Spot source: "
        + (
            st.session_state.get(
                "spot_source"
            )
            or "Completed candle"
        )
    )

else:

    st.warning(
        "NIFTY Spot is not available yet."
    )


# ============================================================
# 2-MINUTE SUPERTREND
# ============================================================

st.subheader(
    "2-Minute Supertrend (20, 1.5)"
)

a, b, c, d = (
    st.columns(4)
)


with a:

    st.write(
        "**Direction**"
    )

    direction = (
        st.session_state.get(
            "st_direction"
        )
    )

    if direction == 1:

        st.success(
            "🟢 GREEN"
        )

    elif direction == -1:

        st.error(
            "🔴 RED"
        )

    else:

        st.info(
            "-"
        )


with b:

    st.write(
        "**Supertrend**"
    )

    st_value = (
        st.session_state.get(
            "st_value"
        )
    )

    if st_value is not None:

        st.write(
            f"{st_value:,.2f}"
        )

    else:

        st.write("-")


with c:

    st.write(
        "**Signal**"
    )

    signal = (
        st.session_state.get(
            "signal",
            "WAIT"
        )
    )

    if signal == "BUY CE":

        st.success(
            "AUTOMATIC BUY CE"
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

o1, o2, o3, o4 = (
    st.columns(4)
)


with o1:

    st.write(
        "**Symbol**"
    )

    st.write(
        st.session_state.get(
            "option_symbol"
        )
        or "-"
    )


with o2:

    st.write(
        "**Expiry**"
    )

    st.write(
        st.session_state.get(
            "option_expiry"
        )
        or "-"
    )


with o3:

    st.write(
        "**Strike**"
    )

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

    st.write(
        "**Lot Size**"
    )

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

status = (
    st.session_state.get(
        "last_order_status"
    )
)

order_id = (
    st.session_state.get(
        "last_order_id"
    )
)

if order_id:

    st.success(
        f"Order ID: {order_id}"
    )

    st.write(
        "Status:",
        status or "-"
    )

elif status:

    st.warning(
        "Order ID: Awaiting Angel One confirmation"
    )

    st.write(
        "Status:",
        status
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
    "Order Book"
)

api = (
    st.session_state.get(
        "api"
    )
)

broker_orders = []

if (
    api is not None
    and
    not PAPER_TRADING
    and
    st.session_state.get(
        "automatic_order_attempted"
    )
):

    broker_orders = (
        fetch_broker_order_book(
            api
        )
    )

local_orders = (
    st.session_state.get(
        "order_book",
        []
    )
)

if PAPER_TRADING:

    all_orders = local_orders

else:

    all_orders = (
        broker_orders
        if broker_orders
        else local_orders
    )


if all_orders:

    order_df = pd.DataFrame(
        all_orders
    )

    st.dataframe(
        order_df,
        use_container_width=True,
        hide_index=True
    )

    if any(
        not str(
            row.get("order_id", "")
        ).strip()
        for row in all_orders
        if isinstance(row, dict)
    ):
        st.caption(
            "A blank Order ID means the local request is "
            "still awaiting broker confirmation. It is "
            "not an Angel One order ID."
        )

else:

    st.info(
        "No automatic order in the Order Book yet."
    )


# ============================================================
# STATUS
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
# RATE LIMIT STATUS
# ============================================================

if rate_limit_active():

    until = (
        st.session_state.get(
            "rate_limited_until"
        )
    )

    st.warning(
        "Angel One API rate-limit cooldown "
        f"is active until {until}."
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
            "Invalid API Key"
            in automation_error
        ):

            st.code(
                "Check ANGEL_API_KEY. "
                "Use the API key belonging to "
                "your Angel One SmartAPI application."
            )

        if (
            "TOTP"
            in automation_error
            or
            "base32"
            in automation_error
            or
            "totp"
            in automation_error.lower()
        ):

            st.code(
                "Check ANGEL_TOTP_SECRET. "
                "Use the actual base32 TOTP secret, "
                "not the 6-digit OTP."
            )

        if is_rate_limit_error(
            automation_error
        ):

            st.code(
                "Angel One is throttling API requests. "
                "This version uses one 1-minute candle "
                "request every 120 seconds and does not "
                "request NIFTY LTP or option LTP during "
                "the normal automation loop."
            )


# ============================================================
# HOW IT WORKS
# ============================================================

with st.expander(
    "How automatic BUY CE works"
):

    st.markdown(
        """
### ONLY 2-MINUTE STRATEGY

**NIFTY 1-minute candles**
↓
**2-minute candles**
↓
**Completed 2-minute candle**
↓
**Supertrend (20, 1.5)**

### GREEN

🟢 Completed 2-minute Supertrend = GREEN

↓

**BUY CE**

↓

Use latest completed NIFTY Spot

↓

Find nearest future NIFTY expiry

↓

Find nearest available ATM NIFTY CE

↓

Automatically BUY 1 lot

### RED

🔴 Completed 2-minute Supertrend = RED

↓

**WAIT**

### Protection

- No 5-minute Supertrend
- No 15-minute Supertrend
- No 4-hour Supertrend
- No manual BUY button
- No manual SELL button
- No green-flip requirement
- Current/forming 2-minute candle is excluded
- Same candle cannot place another order
- One automatic order attempt per trading day
- NIFTY Spot uses latest completed 1-minute candle close
- Option LTP is not requested every refresh
- Live order is not automatically retried if broker response is uncertain
"""
    )


# ============================================================
# REFRESH INFORMATION
# ============================================================

st.caption(
    f"Auto refresh: every {REFRESH_SECONDS} seconds | "
    f"Candle API minimum interval: {CANDLE_MIN_INTERVAL}s | "
    f"2-minute Supertrend: {ST_PERIOD},{ST_MULTIPLIER}"
)


# ============================================================
# AUTO REFRESH
# ============================================================

time.sleep(
    REFRESH_SECONDS
)

st.rerun()
