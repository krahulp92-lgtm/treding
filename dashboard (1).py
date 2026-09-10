# ============================================================
# dashboard.py
#
# NIFTY AUTOMATIC BUY CE ONLY - ANGEL ONE SMARTAPI
#
# STRATEGY
# ------------------------------------------------------------
# 1-minute NIFTY candles
#          ↓
# 2-minute candles
#          ↓
# Supertrend (20, 1.5)
#          ↓
# Latest CLOSED 2-minute candle
#          ↓
# GREEN  -> AUTOMATIC BUY ATM NIFTY CE
# RED    -> WAIT
#
# IMPORTANT
# ------------------------------------------------------------
# NO 5-minute
# NO 15-minute
# NO 4-hour
# NO manual BUY/SELL buttons
#
# PAPER_TRADING=true  -> NO REAL ORDER
# PAPER_TRADING=false -> REAL ANGEL ONE ORDER
#
# To reduce API rate-limit problems:
# - Candle API is called at most once per 2 minutes.
# - NIFTY LTP API is NOT called every refresh.
# - Option LTP API is NOT called before every order.
# - OrderBook is checked only after an order attempt and
#   periodically at a slow interval.
# ============================================================

import json
import os
import time
from datetime import datetime, timedelta, time as dt_time
from pathlib import Path
from urllib.parse import parse_qs, urlparse
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
    page_title="NIFTY 2-Minute Automatic BUY CE",
    page_icon="📈",
    layout="wide",
)


# ============================================================
# CONFIG
# ============================================================

IST = ZoneInfo("Asia/Kolkata")

# ------------------------------------------------------------
# ANGEL ONE CREDENTIALS
# ------------------------------------------------------------
# Streamlit Cloud:
#
# [secrets]
# ANGEL_API_KEY = "YOUR_API_KEY"
# ANGEL_CLIENT_ID = "YOUR_CLIENT_ID"
# ANGEL_PASSWORD = "YOUR_PIN"
# ANGEL_TOTP_SECRET = "YOUR_TOTP_SECRET"
#
# Local environment variables are also supported.
# ------------------------------------------------------------

def get_secret_or_env(name, default=""):
    try:
        value = st.secrets.get(name, None)
        if value is not None:
            return str(value).strip()
    except Exception:
        pass

    return os.getenv(name, default).strip()


API_KEY = get_secret_or_env("ANGEL_API_KEY")
CLIENT_CODE = get_secret_or_env("ANGEL_CLIENT_ID")
PIN = get_secret_or_env("ANGEL_PASSWORD")
TOTP_SECRET = get_secret_or_env("ANGEL_TOTP_SECRET")


# ============================================================
# LIVE / PAPER MODE
# ============================================================

PAPER_TRADING = (
    get_secret_or_env(
        "PAPER_TRADING",
        "false",
    )
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
# STRATEGY
# ============================================================

ST_PERIOD = 20
ST_MULTIPLIER = 1.5


# ============================================================
# NIFTY
# ============================================================

NIFTY_TOKEN = "99926000"
NIFTY_SYMBOL = "NIFTY"


# ============================================================
# ORDER SETTINGS
# ============================================================

EXCHANGE = "NFO"
PRODUCT_TYPE = "INTRADAY"
ORDER_TYPE = "MARKET"
DURATION = "DAY"
VARIETY = "NORMAL"

LOTS = int(
    get_secret_or_env(
        "NIFTY_LOTS",
        "1",
    )
)


# ============================================================
# REFRESH / API RATE LIMIT PROTECTION
# ============================================================

# Streamlit page refresh.
# Do not use 10 seconds for Angel One automatic trading.
REFRESH_SECONDS = int(
    get_secret_or_env(
        "REFRESH_SECONDS",
        "120",
    )
)

# Minimum time between candle API requests.
CANDLE_MIN_INTERVAL = 120

# If Angel One returns a rate-limit response,
# wait before trying the candle API again.
RATE_LIMIT_COOLDOWN = 180

# OrderBook should not be requested continuously.
ORDERBOOK_REFRESH_SECONDS = 300


# ============================================================
# MARKET TIME
# ============================================================

ENTRY_START = dt_time(9, 20)
ENTRY_END = dt_time(15, 15)


# ============================================================
# FILES
# ============================================================

BASE_DIR = Path(__file__).resolve().parent

STATE_FILE = BASE_DIR / "nifty_ce_state.json"

INSTRUMENT_CACHE = (
    BASE_DIR / "OpenAPIScripMaster.json"
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

    "login_status": "NOT CONNECTED",

    "last_error": "",

    "last_message": "Ready",

    "spot": None,

    "signal": "WAIT",

    "signal_time": None,

    "st2": None,

    "st2_green": False,

    "st2_red": False,

    "st2_flip_green": False,

    "st2_flip_red": False,

    "df_1m": None,

    "df_2m": None,

    "option_symbol": None,

    "option_token": None,

    "option_expiry": None,

    "option_strike": None,

    "option_lot_size": None,

    "option_ltp": None,

    "last_order_id": None,

    "last_order_time": None,

    "last_order_status": None,

    "order_status_unknown": False,

    "order_book": [],

    "last_processed_candle": None,

    "last_signal_candle": None,

    "in_position": False,

    "position": None,

    "instruments": None,

    "last_candle_fetch": None,

    "last_orderbook_fetch": None,

    "rate_limited_until": None,

    "fresh_candle_data": False,

    "automation_running": False,
}


for key, value in DEFAULTS.items():

    if key not in st.session_state:

        st.session_state[key] = value


# ============================================================
# GENERAL HELPERS
# ============================================================

def now_ist():
    """
    Always return pandas Timestamp.

    This allows:
        now.floor("min")
    """

    return pd.Timestamp.now(
        tz=IST
    )


def clean_secret(raw):
    """
    Accept:
    - normal base32 TOTP secret
    - otpauth:// URI
    """

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
                [None],
            )[0]

            if secret:

                return (
                    secret
                    .replace(
                        " ",
                        "",
                    )
                    .replace(
                        "-",
                        "",
                    )
                    .upper()
                )

        except Exception:
            pass

    return (
        raw
        .replace(
            " ",
            "",
        )
        .replace(
            "-",
            "",
        )
        .upper()
    )


def safe_float(value):

    try:
        return float(value)

    except Exception:
        return None


def json_safe(value):

    if isinstance(
        value,
        (
            np.integer,
        ),
    ):
        return int(value)

    if isinstance(
        value,
        (
            np.floating,
        ),
    ):
        return float(value)

    if isinstance(
        value,
        (
            pd.Timestamp,
            datetime,
        ),
    ):
        return value.isoformat()

    return value


# ============================================================
# RATE LIMIT HELPERS
# ============================================================

def is_rate_limit_error(message):

    text = str(
        message
    ).lower()

    keywords = [
        "rate limit",
        "rate-limit",
        "too many",
        "throttle",
        "throttl",
        "exceeded",
        "request limit",
        "api limit",
    ]

    return any(
        keyword in text
        for keyword in keywords
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

        if now_ist() < until:

            return True

    except Exception:
        return False

    return False


def rate_limit_remaining():

    value = st.session_state.get(
        "rate_limited_until"
    )

    if not value:
        return 0

    try:

        until = pd.Timestamp(
            value
        )

        if until.tzinfo is None:

            until = until.tz_localize(
                IST
            )

        seconds = (
            until - now_ist()
        ).total_seconds()

        return max(
            0,
            int(seconds),
        )

    except Exception:

        return 0


# ============================================================
# CREDENTIAL CHECK
# ============================================================

def credentials_ok():

    missing = []

    if not API_KEY:
        missing.append(
            "ANGEL_API_KEY"
        )

    if not CLIENT_CODE:
        missing.append(
            "ANGEL_CLIENT_ID"
        )

    if not PIN:
        missing.append(
            "ANGEL_PASSWORD"
        )

    if not TOTP_SECRET:
        missing.append(
            "ANGEL_TOTP_SECRET"
        )

    return missing


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

        restore_keys = [
            "last_order_id",
            "last_order_time",
            "last_order_status",
            "order_status_unknown",
            "last_processed_candle",
            "last_signal_candle",
            "in_position",
            "position",
        ]

        for key in restore_keys:

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

        "order_status_unknown":
            st.session_state.get(
                "order_status_unknown",
                False,
            ),

        "last_processed_candle":
            st.session_state.get(
                "last_processed_candle"
            ),

        "last_signal_candle":
            st.session_state.get(
                "last_signal_candle"
            ),

        "in_position":
            st.session_state.get(
                "in_position",
                False,
            ),

        "position":
            st.session_state.get(
                "position"
            ),
    }

    try:

        STATE_FILE.write_text(
            json.dumps(
                data,
                indent=2,
                default=json_safe,
            ),
            encoding="utf-8",
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

    if (
        st.session_state.get(
            "api"
        )
        is not None
    ):

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
        TOTP_SECRET
    )

    # Prevent using 6-digit OTP as secret.
    if (
        len(secret) == 6
        and secret.isdigit()
    ):

        raise RuntimeError(
            "ANGEL_TOTP_SECRET contains "
            "a 6-digit OTP. Use the actual "
            "base32 TOTP secret."
        )

    try:

        totp = pyotp.TOTP(
            secret
        ).now()

    except Exception as exc:

        raise RuntimeError(
            "Invalid ANGEL_TOTP_SECRET. "
            "Use the actual base32 secret "
            "or otpauth URI. "
            f"Details: {exc}"
        )

    try:

        smart_api = SmartConnect(
            api_key=API_KEY
        )

        response = (
            smart_api.generateSession(
                CLIENT_CODE,
                PIN,
                totp,
            )
        )

    except Exception as exc:

        raise RuntimeError(
            f"Angel One login exception: {exc}"
        )

    if (
        not isinstance(
            response,
            dict,
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
    ] = smart_api

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

    return smart_api


# ============================================================
# 1-MINUTE CANDLE DATA
# ============================================================

def get_nifty_1m_candles(
    api,
    days=3,
):

    # --------------------------------------------------------
    # RATE LIMIT CHECK
    # --------------------------------------------------------

    if rate_limit_active():

        raise RuntimeError(
            "Angel One candle API is temporarily "
            "rate-limited. "
            f"Retry in approximately "
            f"{rate_limit_remaining()} seconds."
        )

    end = now_ist()

    start = (
        end
        - pd.Timedelta(
            days=days
        )
    )

    params = {

        "exchange": "NSE",

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

        if is_rate_limit_error(
            exc
        ):

            set_rate_limit()

        raise RuntimeError(
            f"Candle API exception: {exc}"
        )

    if (
        not isinstance(
            response,
            dict,
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
            "Candle API returned "
            "no NIFTY 1-minute candles."
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
        ],
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

    ts = pd.to_datetime(
        df["datetime"],
        errors="coerce",
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
    # IMPORTANT:
    # Remove currently forming 1-minute candle.
    # --------------------------------------------------------

    current_minute = (
        now_ist().floor(
            "min"
        )
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
            "No completed NIFTY candles "
            "inside NSE market hours."
        )

    return df


# ============================================================
# FETCH CANDLES ONLY WHEN DUE
# ============================================================

def fetch_candles_if_due(api):

    previous = st.session_state.get(
        "last_candle_fetch"
    )

    # --------------------------------------------------------
    # Check if previous data is still recent.
    # --------------------------------------------------------

    if previous:

        try:

            previous_ts = pd.Timestamp(
                previous
            )

            if previous_ts.tzinfo is None:

                previous_ts = (
                    previous_ts.tz_localize(
                        IST
                    )
                )

            age = (
                now_ist()
                - previous_ts
            ).total_seconds()

            if age < CANDLE_MIN_INTERVAL:

                cached = (
                    st.session_state.get(
                        "df_1m"
                    )
                )

                if (
                    cached is not None
                    and not cached.empty
                ):

                    st.session_state[
                        "fresh_candle_data"
                    ] = False

                    return cached

        except Exception:
            pass

    # --------------------------------------------------------
    # Rate limit active.
    # --------------------------------------------------------

    if rate_limit_active():

        cached = (
            st.session_state.get(
                "df_1m"
            )
        )

        if (
            cached is not None
            and not cached.empty
        ):

            st.session_state[
                "fresh_candle_data"
            ] = False

            st.session_state[
                "last_message"
            ] = (
                "Using cached candles. "
                "Angel One API cooldown active. "
                f"Retry in {rate_limit_remaining()} sec."
            )

            return cached

        raise RuntimeError(
            "Angel One API rate-limit cooldown "
            "is active and no cached candle data "
            "is available."
        )

    # --------------------------------------------------------
    # Fetch fresh data.
    # --------------------------------------------------------

    df = get_nifty_1m_candles(
        api,
        days=3,
    )

    st.session_state[
        "df_1m"
    ] = df

    st.session_state[
        "last_candle_fetch"
    ] = now_ist().isoformat()

    st.session_state[
        "fresh_candle_data"
    ] = True

    return df


# ============================================================
# RESAMPLE 1-MINUTE -> 2-MINUTE
# ============================================================

def resample_to_2min(
    df,
):

    if df is None or df.empty:

        raise RuntimeError(
            "1-minute candle dataframe is empty."
        )

    x = df.copy()

    x = (
        x
        .set_index(
            "datetime"
        )
        .sort_index()
    )

    result = (
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

    # --------------------------------------------------------
    # Only use CLOSED 2-minute candles.
    #
    # Example:
    # 09:15-09:17 candle is complete at 09:17.
    # --------------------------------------------------------

    current_2min = (
        now_ist().floor(
            "2min"
        )
    )

    result = result[
        result["datetime"]
        < current_2min
    ].copy()

    if result.empty:

        raise RuntimeError(
            "No completed 2-minute candles available."
        )

    return result


# ============================================================
# SUPERTREND
# ============================================================

def calculate_supertrend(
    df,
    period=ST_PERIOD,
    multiplier=ST_MULTIPLIER,
):

    df = df.copy()

    if len(df) < (
        period + 5
    ):

        raise RuntimeError(
            f"Not enough candles for "
            f"Supertrend {period},{multiplier}. "
            f"Received {len(df)}."
        )

    high = df["high"].astype(
        float
    )

    low = df["low"].astype(
        float
    )

    close = df["close"].astype(
        float
    )

    prev_close = close.shift(
        1
    )

    tr = pd.concat(
        [
            high - low,

            (
                high
                - prev_close
            ).abs(),

            (
                low
                - prev_close
            ).abs(),
        ],
        axis=1,
    ).max(
        axis=1
    )

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
        len(df),
    ):

        previous_upper = (
            final_upper.iloc[
                i - 1
            ]
        )

        previous_lower = (
            final_lower.iloc[
                i - 1
            ]
        )

        if (
            pd.isna(
                previous_upper
            )
            or
            basic_upper.iloc[i]
            <
            previous_upper
            or
            close.iloc[
                i - 1
            ]
            >
            previous_upper
        ):

            final_upper.iloc[i] = (
                basic_upper.iloc[i]
            )

        else:

            final_upper.iloc[i] = (
                previous_upper
            )

        if (
            pd.isna(
                previous_lower
            )
            or
            basic_lower.iloc[i]
            >
            previous_lower
            or
            close.iloc[
                i - 1
            ]
            <
            previous_lower
        ):

            final_lower.iloc[i] = (
                basic_lower.iloc[i]
            )

        else:

            final_lower.iloc[i] = (
                previous_lower
            )

    direction = pd.Series(
        index=df.index,
        dtype="float64",
    )

    supertrend = pd.Series(
        index=df.index,
        dtype="float64",
    )

    first_valid = (
        atr.first_valid_index()
    )

    if first_valid is None:

        raise RuntimeError(
            "ATR could not be calculated."
        )

    first_i = df.index.get_loc(
        first_valid
    )

    direction.iloc[
        :first_i
    ] = np.nan

    supertrend.iloc[
        :first_i
    ] = np.nan

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
        len(df),
    ):

        previous_st = (
            supertrend.iloc[
                i - 1
            ]
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
            ==
            final_upper.iloc[
                i - 1
            ]
        ):

            if (
                close.iloc[i]
                <=
                final_upper.iloc[i]
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
                >=
                final_lower.iloc[i]
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
        direction.astype(
            "Int64"
        )
    )

    df["green"] = (
        df["direction"] == 1
    )

    df["red"] = (
        df["direction"] == -1
    )

    previous_direction = (
        df["direction"].shift(1)
    )

    df["flip_green"] = (
        (df["direction"] == 1)
        &
        (previous_direction == -1)
    )

    df["flip_red"] = (
        (df["direction"] == -1)
        &
        (previous_direction == 1)
    )

    return df


# ============================================================
# BUILD 2-MINUTE SUPERTREND
# ============================================================

def build_2min_supertrend(
    df1,
):

    df2 = resample_to_2min(
        df1
    )

    st2 = calculate_supertrend(
        df2,
        period=20,
        multiplier=1.5,
    )

    return st2


# ============================================================
# SIGNAL
# ============================================================

def get_2min_signal(
    st2,
):

    if len(st2) < 3:

        raise RuntimeError(
            "Not enough completed "
            "2-minute candles."
        )

    # --------------------------------------------------------
    # st2 already contains ONLY CLOSED candles.
    # Therefore latest row is the latest CLOSED candle.
    # --------------------------------------------------------

    row = st2.iloc[-1]

    green = bool(
        row["green"]
    )

    red = bool(
        row["red"]
    )

    flip_green = bool(
        row["flip_green"]
    )

    flip_red = bool(
        row["flip_red"]
    )

    if green:

        signal = "BUY CE"

    else:

        signal = "WAIT"

    return {

        "signal":
            signal,

        "candle_time":
            row["datetime"],

        "close":
            float(row["close"]),

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

        "green":
            green,

        "red":
            red,

        "flip_green":
            flip_green,

        "flip_red":
            flip_red,
    }


# ============================================================
# SCRIP MASTER DOWNLOAD
# ============================================================

@st.cache_data(
    ttl=86400,
    show_spinner=False,
)
def download_scrip_master():

    response = requests.get(
        SCRIP_MASTER_URL,
        timeout=30,
    )

    response.raise_for_status()

    data = response.json()

    if (
        not isinstance(
            data,
            list,
        )
        or not data
    ):

        raise RuntimeError(
            "Angel One scrip master is empty."
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

        data = download_scrip_master()

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
            encoding="utf-8",
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

def parse_expiry(
    value,
):

    if not value:

        return None

    value = str(
        value
    ).strip().upper()

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
                fmt,
            ).date()

        except ValueError:
            continue

    return None


# ============================================================
# STRIKE
# ============================================================

def normalize_strike(
    raw,
):

    try:

        value = float(
            raw
        )

        # Some scrip masters store
        # strike × 100.
        if value > 100000:

            value = (
                value / 100.0
            )

        return value

    except Exception:

        return None


def get_atm_strike(
    spot,
    step=50,
):

    return int(
        round(
            float(spot)
            / step
        )
        * step
    )


# ============================================================
# SELECT ATM NIFTY CE
# ============================================================

def select_atm_nifty_ce(
    instruments,
    spot,
):

    if not instruments:

        raise RuntimeError(
            "Scrip master is empty."
        )

    atm = get_atm_strike(
        spot
    )

    today = now_ist().date()

    candidates = []

    for item in instruments:

        if not isinstance(
            item,
            dict,
        ):

            continue

        exch_seg = str(
            item.get(
                "exch_seg",
                "",
            )
        ).upper()

        symbol = str(
            item.get(
                "symbol",
                "",
            )
        ).upper()

        name = str(
            item.get(
                "name",
                "",
            )
        ).upper()

        instrument_type = str(
            item.get(
                "instrumenttype",
                "",
            )
        ).upper()

        # ----------------------------------------------------
        # NFO only
        # ----------------------------------------------------

        if exch_seg != "NFO":

            continue

        # ----------------------------------------------------
        # NIFTY only
        # ----------------------------------------------------

        if (
            "NIFTY" not in name
            and not symbol.startswith(
                "NIFTY"
            )
        ):

            continue

        # ----------------------------------------------------
        # CE only
        # ----------------------------------------------------

        if not symbol.endswith(
            "CE"
        ):

            continue

        # ----------------------------------------------------
        # NIFTY index options
        # ----------------------------------------------------

        if (
            instrument_type
            and instrument_type
            not in (
                "OPTIDX",
                "OPTSTK",
            )
        ):

            continue

        strike = normalize_strike(
            item.get(
                "strike"
            )
        )

        if strike is None:

            continue

        # Exact ATM strike
        if (
            abs(
                strike - atm
            )
            > 0.01
        ):

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
                "",
            )
        ).strip()

        if not token:

            continue

        lot_raw = item.get(
            "lotsize"
        )

        try:

            lot_size = int(
                float(
                    lot_raw
                )
            )

        except Exception:

            continue

        if lot_size <= 0:

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
            f"No ATM NIFTY CE found "
            f"for strike {atm}. "
            "Refresh OpenAPIScripMaster.json."
        )

    candidates.sort(
        key=lambda x: (
            x["expiry"],
            abs(
                x["strike"] - atm
            ),
        )
    )

    return candidates[0]


# ============================================================
# LOCAL ORDER BOOK
# ============================================================

def add_local_order(
    order,
):

    existing = (
        st.session_state.get(
            "order_book"
        )
        or []
    )

    existing.insert(
        0,
        order,
    )

    st.session_state[
        "order_book"
    ] = existing[:50]


# ============================================================
# ORDER RESPONSE PARSER
# ============================================================

def extract_order_id(
    response,
):

    if response is None:

        return None

    if isinstance(
        response,
        dict,
    ):

        data = (
            response.get(
                "data"
            )
            or {}
        )

        if isinstance(
            data,
            dict,
        ):

            order_id = (
                data.get(
                    "orderid"
                )
                or data.get(
                    "orderId"
                )
                or data.get(
                    "order_id"
                )
            )

            if order_id:

                return str(
                    order_id
                )

        order_id = (
            response.get(
                "orderid"
            )
            or response.get(
                "orderId"
            )
            or response.get(
                "order_id"
            )
        )

        if order_id:

            return str(
                order_id
            )

        return None

    if isinstance(
        response,
        str,
    ):

        value = response.strip()

        return (
            value
            if value
            else None
        )

    return None


# ============================================================
# BROKER ORDER BOOK
# ============================================================

def fetch_broker_order_book(
    api,
):

    try:

        response = api.orderBook()

    except Exception as exc:

        if is_rate_limit_error(
            exc
        ):

            set_rate_limit()

        return []

    if not isinstance(
        response,
        dict,
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
        list,
    ):

        return []

    rows = []

    for item in data:

        if not isinstance(
            item,
            dict,
        ):

            continue

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
                        "",
                    ),

                "side":
                    item.get(
                        "transactiontype",
                        "",
                    ),

                "symbol":
                    item.get(
                        "tradingsymbol",
                        "",
                    ),

                "token":
                    item.get(
                        "symboltoken",
                        "",
                    ),

                "quantity":
                    item.get(
                        "quantity",
                        "",
                    ),

                "price":
                    item.get(
                        "price",
                        "",
                    ),
            }
        )

    return rows


# ============================================================
# CHECK ORDER BOOK FOR SYMBOL / ORDER ID
# ============================================================

def find_order_in_book(
    orders,
    order_id=None,
    symbol=None,
):

    for item in orders:

        if not isinstance(
            item,
            dict,
        ):

            continue

        item_id = str(
            item.get(
                "order_id",
                ""
            )
        )

        item_symbol = str(
            item.get(
                "symbol",
                ""
            )
        ).upper()

        if (
            order_id
            and item_id
            == str(order_id)
        ):

            return item

        if (
            symbol
            and item_symbol
            == str(
                symbol
            ).upper()
        ):

            return item

    return None


# ============================================================
# PLACE BUY CE
# ============================================================

def place_buy_ce(
    api,
    option,
):

    lot_size = option.get(
        "lot_size"
    )

    if not lot_size or lot_size <= 0:

        raise RuntimeError(
            "Invalid lot size: "
            + str(lot_size)
        )

    quantity = (
        LOTS
        * lot_size
    )

    params = {

        "variety":
            VARIETY,

        "tradingsymbol":
            option["symbol"],

        "symboltoken":
            option["token"],

        "transactiontype":
            "BUY",

        "exchange":
            EXCHANGE,

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
        now_ist().strftime(
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
            order,
        )

    # ========================================================
    # LIVE MODE
    # ========================================================

    try:

        # ----------------------------------------------------
        # Prefer full response if this SmartAPI version has it.
        # ----------------------------------------------------

        if hasattr(
            api,
            "placeOrderFullResponse",
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
            f"Angel One order exception: {exc}"
        )

    # --------------------------------------------------------
    # Broker explicitly returned failure
    # --------------------------------------------------------

    if isinstance(
        response,
        dict,
    ):

        if response.get(
            "status"
        ) is False:

            raise RuntimeError(
                "Angel One order failed: "
                + str(response)
            )

    order_id = extract_order_id(
        response
    )

    # --------------------------------------------------------
    # Empty broker response
    #
    # DO NOT immediately place another order.
    # OrderBook verification is handled separately.
    # --------------------------------------------------------

    if not order_id:

        raise RuntimeError(
            "Angel One returned an empty "
            "order response. "
            "The order status must be verified "
            "from Order Book before retrying."
        )

    order = {

        "time":
            timestamp,

        "order_id":
            order_id,

        "mode":
            "LIVE",

        "status":
            "SUBMITTED",

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
        str(order_id),
        order,
    )


# ============================================================
# VERIFY LIVE ORDER
# ============================================================

def verify_live_order(
    api,
    order_id,
    symbol,
):

    # --------------------------------------------------------
    # Give broker a short moment.
    # --------------------------------------------------------

    time.sleep(2)

    for attempt in range(
        2
    ):

        orders = (
            fetch_broker_order_book(
                api
            )
        )

        if orders:

            found = find_order_in_book(
                orders,
                order_id=order_id,
                symbol=symbol,
            )

            if found:

                return (
                    True,
                    found,
                )

        if attempt == 0:

            time.sleep(2)

    return (
        False,
        None,
    )


# ============================================================
# AUTOMATIC BUY LOGIC
# ============================================================

def automatic_buy_ce(
    api,
    info,
):

    candle_key = (
        info["candle_time"]
        .isoformat()
    )

    # --------------------------------------------------------
    # IMPORTANT:
    # Never trade using cached/stale candles.
    # --------------------------------------------------------

    if not st.session_state.get(
        "fresh_candle_data",
        False,
    ):

        st.session_state[
            "last_message"
        ] = (
            "Candle data is cached because "
            "of API cooldown. "
            "No automatic order will be placed "
            "until fresh data is received."
        )

        return

    # --------------------------------------------------------
    # Already processed this candle.
    # --------------------------------------------------------

    if (
        st.session_state.get(
            "last_processed_candle"
        )
        == candle_key
    ):

        return

    # --------------------------------------------------------
    # Mark candle processed BEFORE order attempt.
    #
    # This prevents duplicate orders if Streamlit reruns.
    # --------------------------------------------------------

    st.session_state[
        "last_processed_candle"
    ] = candle_key

    save_state()

    # --------------------------------------------------------
    # RED -> WAIT
    # --------------------------------------------------------

    if info["signal"] != "BUY CE":

        st.session_state[
            "last_message"
        ] = (
            "2-minute Supertrend is RED. "
            "WAIT."
        )

        save_state()

        return

    # --------------------------------------------------------
    # Existing position
    # --------------------------------------------------------

    if st.session_state.get(
        "in_position"
    ):

        st.session_state[
            "last_message"
        ] = (
            "2-minute Supertrend is GREEN, "
            "but an automated CE position "
            "is already recorded as OPEN."
        )

        save_state()

        return

    # --------------------------------------------------------
    # Existing unknown order
    # --------------------------------------------------------

    if st.session_state.get(
        "order_status_unknown"
    ):

        st.session_state[
            "last_message"
        ] = (
            "Previous order status is UNKNOWN. "
            "No new order will be sent until "
            "the previous order is verified."
        )

        return

    # --------------------------------------------------------
    # Extra duplicate protection
    # --------------------------------------------------------

    if (
        st.session_state.get(
            "last_signal_candle"
        )
        == candle_key
    ):

        return

    # ========================================================
    # ATM SELECTION
    # ========================================================

    instruments = (
        load_instruments()
    )

    # Latest completed 2-minute NIFTY close
    # is used for ATM calculation.
    spot = float(
        info["close"]
    )

    option = (
        select_atm_nifty_ce(
            instruments,
            spot,
        )
    )

    # --------------------------------------------------------
    # Store selected option.
    # --------------------------------------------------------

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

    # ========================================================
    # AUTOMATIC BUY
    # ========================================================

    try:

        order_id, order = (
            place_buy_ce(
                api,
                option,
            )
        )

    except Exception as exc:

        # ----------------------------------------------------
        # If order submission failed, DO NOT blindly retry.
        # ----------------------------------------------------

        st.session_state[
            "last_order_status"
        ] = "ERROR"

        st.session_state[
            "last_error"
        ] = str(exc)

        st.session_state[
            "last_message"
        ] = (
            "Automatic BUY CE submission "
            "needs verification: "
            + str(exc)
        )

        # ----------------------------------------------------
        # If broker response was empty, mark UNKNOWN.
        # ----------------------------------------------------

        if (
            "empty order response"
            in str(exc).lower()
        ):

            st.session_state[
                "order_status_unknown"
            ] = True

        save_state()

        return

    # ========================================================
    # STORE ORDER
    # ========================================================

    st.session_state[
        "last_order_id"
    ] = order_id

    st.session_state[
        "last_order_time"
    ] = now_ist().isoformat()

    st.session_state[
        "last_order_status"
    ] = "SUBMITTED"

    st.session_state[
        "last_signal_candle"
    ] = candle_key

    # ========================================================
    # LIVE ORDER VERIFICATION
    # ========================================================

    if not PAPER_TRADING:

        verified, broker_order = (
            verify_live_order(
                api,
                order_id,
                option["symbol"],
            )
        )

        if verified:

            broker_status = (
                broker_order.get(
                    "status",
                    "FOUND",
                )
            )

            st.session_state[
                "last_order_status"
            ] = str(
                broker_status
            )

            st.session_state[
                "order_status_unknown"
            ] = False

            st.session_state[
                "last_message"
            ] = (
                f"🟢 AUTOMATIC BUY CE "
                f"ORDER FOUND IN ANGEL ONE "
                f"ORDER BOOK: "
                f"{option['symbol']} "
                f"| {order_id}"
            )

        else:

            # ------------------------------------------------
            # Do not send a duplicate order.
            # ------------------------------------------------

            st.session_state[
                "order_status_unknown"
            ] = True

            st.session_state[
                "last_order_status"
            ] = "UNKNOWN"

            st.session_state[
                "last_message"
            ] = (
                f"Order submitted but could not "
                f"yet be verified in Order Book: "
                f"{order_id}. "
                "NO RETRY will be sent automatically."
            )

            save_state()

            return

    else:

        st.session_state[
            "last_order_status"
        ] = "PAPER ORDER"

    # ========================================================
    # POSITION RECORD
    # ========================================================

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
            LOTS
            * option["lot_size"],

        "entry_spot":
            spot,

        "signal":
            "BUY CE",

        "signal_candle":
            candle_key,

        "mode":
            (
                "PAPER"
                if PAPER_TRADING
                else "LIVE"
            ),
    }

    st.session_state[
        "last_message"
    ] = (
        f"🟢 2-MINUTE SUPERTREND GREEN "
        f"→ AUTOMATIC BUY CE "
        f"{option['symbol']} "
        f"| ORDER {order_id}"
    )

    save_state()


# ============================================================
# AUTOMATION
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


def run_automation():

    st.session_state[
        "automation_running"
    ] = True

    api = angel_login()

    if not market_is_open():

        st.session_state[
            "signal"
        ] = "WAIT"

        st.session_state[
            "last_message"
        ] = (
            "Market is outside "
            "the automatic entry window."
        )

        st.session_state[
            "automation_running"
        ] = False

        return

    # --------------------------------------------------------
    # Fetch 1-minute candles only when due.
    # --------------------------------------------------------

    df1 = fetch_candles_if_due(
        api
    )

    # --------------------------------------------------------
    # 1m -> 2m
    # --------------------------------------------------------

    st2 = build_2min_supertrend(
        df1
    )

    st.session_state[
        "df_2m"
    ] = st2

    # --------------------------------------------------------
    # ONLY 2-MINUTE SIGNAL
    # --------------------------------------------------------

    info = get_2min_signal(
        st2
    )

    st.session_state[
        "signal"
    ] = info["signal"]

    st.session_state[
        "signal_time"
    ] = info[
        "candle_time"
    ].isoformat()

    st.session_state[
        "st2"
    ] = info["supertrend"]

    st.session_state[
        "st2_green"
    ] = info["green"]

    st.session_state[
        "st2_red"
    ] = info["red"]

    st.session_state[
        "st2_flip_green"
    ] = info["flip_green"]

    st.session_state[
        "st2_flip_red"
    ] = info["flip_red"]

    # --------------------------------------------------------
    # Spot for ATM calculation.
    #
    # Use latest CLOSED 2-minute candle close.
    # This avoids an extra ltpData API call.
    # --------------------------------------------------------

    st.session_state[
        "spot"
    ] = float(
        info["close"]
    )

    # --------------------------------------------------------
    # AUTOMATIC BUY
    # --------------------------------------------------------

    automatic_buy_ce(
        api,
        info,
    )

    st.session_state[
        "automation_running"
    ] = False


# ============================================================
# ORDER BOOK REFRESH
# ============================================================

def refresh_order_book_if_due():

    if PAPER_TRADING:

        return

    api = st.session_state.get(
        "api"
    )

    if api is None:

        return

    previous = (
        st.session_state.get(
            "last_orderbook_fetch"
        )
    )

    if previous:

        try:

            previous_ts = pd.Timestamp(
                previous
            )

            if previous_ts.tzinfo is None:

                previous_ts = (
                    previous_ts.tz_localize(
                        IST
                    )
                )

            age = (
                now_ist()
                - previous_ts
            ).total_seconds()

            if age < ORDERBOOK_REFRESH_SECONDS:

                return

        except Exception:
            pass

    orders = (
        fetch_broker_order_book(
            api
        )
    )

    if orders:

        st.session_state[
            "order_book"
        ] = orders

    st.session_state[
        "last_orderbook_fetch"
    ] = now_ist().isoformat()


# ============================================================
# LOAD PERSISTED STATE
# ============================================================

load_state()


# ============================================================
# PAGE HEADER
# ============================================================

st.title(
    "📈 NIFTY 2-Minute Automatic BUY CE"
)

st.caption(
    "ONLY 2-Minute Supertrend (20, 1.5) "
    "→ GREEN = AUTOMATIC BUY ATM NIFTY CE "
    "→ RED = WAIT"
)


# ============================================================
# SAFETY BANNER
# ============================================================

if PAPER_TRADING:

    st.warning(
        "🟡 PAPER TRADING MODE — "
        "NO REAL ANGEL ONE ORDER WILL BE SENT."
    )

else:

    st.error(
        "🔴 LIVE TRADING MODE — "
        "A GREEN closed 2-minute Supertrend "
        "can automatically send a REAL "
        "NIFTY CE BUY MARKET order."
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
        "Automation error. "
        "See diagnostics below."
    )

    st.session_state[
        "automation_running"
    ] = False


# ============================================================
# ORDER BOOK
# ============================================================

try:

    refresh_order_book_if_due()

except Exception:
    pass


# ============================================================
# HEADER METRICS
# ============================================================

c1, c2, c3, c4, c5 = (
    st.columns(5)
)


with c1:

    st.metric(
        "Angel One",
        st.session_state.get(
            "login_status",
            "NOT CONNECTED",
        ),
    )


with c2:

    spot = (
        st.session_state.get(
            "spot"
        )
    )

    st.metric(
        "NIFTY Spot",
        (
            "-"
            if spot is None
            else f"{spot:,.2f}"
        ),
    )


with c3:

    st.metric(
        "2-Min Signal",
        st.session_state.get(
            "signal",
            "WAIT",
        ),
    )


with c4:

    st.metric(
        "Position",
        (
            "OPEN"
            if st.session_state.get(
                "in_position"
            )
            else "NONE"
        ),
    )


with c5:

    st.metric(
        "Mode",
        (
            "PAPER"
            if PAPER_TRADING
            else "LIVE"
        ),
    )


# ============================================================
# SIGNAL
# ============================================================

st.subheader(
    "2-Minute Supertrend"
)

s1, s2, s3, s4 = (
    st.columns(4)
)


with s1:

    st.write(
        "**Supertrend**"
    )

    value = (
        st.session_state.get(
            "st2"
        )
    )

    if value is None:

        st.write("—")

    else:

        st.write(
            f"{value:,.2f}"
        )


with s2:

    st.write(
        "**Direction**"
    )

    if st.session_state.get(
        "st2_green"
    ):

        st.success(
            "🟢 GREEN"
        )

    elif st.session_state.get(
        "st2_red"
    ):

        st.error(
            "🔴 RED"
        )

    else:

        st.info(
            "WAIT"
        )


with s3:

    st.write(
        "**Signal**"
    )

    if (
        st.session_state.get(
            "signal"
        )
        == "BUY CE"
    ):

        st.success(
            "🟢 AUTOMATIC BUY CE"
        )

    else:

        st.info(
            "WAIT"
        )


with s4:

    st.write(
        "**Candle**"
    )

    signal_time = (
        st.session_state.get(
            "signal_time"
        )
    )

    st.write(
        signal_time
        or "—"
    )


# ============================================================
# FLIP INFORMATION
# ============================================================

st.subheader(
    "2-Minute Candle Status"
)

f1, f2, f3 = (
    st.columns(3)
)

with f1:

    if st.session_state.get(
        "st2_flip_green"
    ):

        st.success(
            "🟢 GREEN FLIP"
        )

    else:

        st.write(
            "No green flip"
        )


with f2:

    if st.session_state.get(
        "st2_flip_red"
    ):

        st.error(
            "🔴 RED FLIP"
        )

    else:

        st.write(
            "No red flip"
        )


with f3:

    if st.session_state.get(
        "fresh_candle_data"
    ):

        st.success(
            "Fresh candle data"
        )

    else:

        st.warning(
            "Cached candle data"
        )


# ============================================================
# SELECTED ATM CE
# ============================================================

st.subheader(
    "Selected ATM NIFTY CE"
)

o1, o2, o3, o4, o5 = (
    st.columns(5)
)


with o1:

    st.write(
        "**Symbol**"
    )

    st.write(
        st.session_state.get(
            "option_symbol"
        )
        or "—"
    )


with o2:

    st.write(
        "**Expiry**"
    )

    st.write(
        st.session_state.get(
            "option_expiry"
        )
        or "—"
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
        "—"
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
        "—"
        if lot is None
        else str(lot)
    )


with o5:

    st.write(
        "**CE LTP**"
    )

    st.write(
        "Not requested"
    )


# ============================================================
# AUTOMATED POSITION
# ============================================================

st.subheader(
    "Automatic Position"
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
# ORDER STATUS
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
        f"Latest Order ID: {order_id}"
    )

    st.write(
        f"Status: {order_status or '—'}"
    )

    if (
        st.session_state.get(
            "order_status_unknown"
        )
    ):

        st.warning(
            "Order status is UNKNOWN. "
            "No duplicate order will be sent."
        )

else:

    st.info(
        "No automatic order has been generated yet."
    )


# ============================================================
# ORDER BOOK
# ============================================================

st.subheader(
    "Angel One Order Book"
)

orders = (
    st.session_state.get(
        "order_book"
    )
    or []
)

if orders:

    order_df = pd.DataFrame(
        orders
    )

    # --------------------------------------------------------
    # Highlight newest automatic order
    # --------------------------------------------------------

    latest_order_id = (
        st.session_state.get(
            "last_order_id"
        )
    )

    if latest_order_id:

        def highlight_latest(
            row
        ):

            if (
                str(
                    row.get(
                        "order_id",
                        ""
                    )
                )
                ==
                str(
                    latest_order_id
                )
            ):

                return [
                    "font-weight: bold"
                    for _ in row
                ]

            return [
                ""
                for _ in row
            ]

        try:

            st.dataframe(
                order_df.style.apply(
                    highlight_latest,
                    axis=1,
                ),
                use_container_width=True,
                hide_index=True,
            )

        except Exception:

            st.dataframe(
                order_df,
                use_container_width=True,
                hide_index=True,
            )

    else:

        st.dataframe(
            order_df,
            use_container_width=True,
            hide_index=True,
        )

else:

    st.info(
        "No order is currently shown. "
        "When the closed 2-minute Supertrend "
        "becomes GREEN, the automatic CE order "
        "will be processed."
    )


# ============================================================
# LAST MESSAGE
# ============================================================

st.subheader(
    "Automation Status"
)

st.write(
    st.session_state.get(
        "last_message",
        "Ready",
    )
)

if st.session_state.get(
    "last_order_time"
):

    st.caption(
        "Last automatic order: "
        + str(
            st.session_state[
                "last_order_time"
            ]
        )
    )


# ============================================================
# RATE LIMIT STATUS
# ============================================================

if rate_limit_active():

    st.warning(
        "Angel One API cooldown active. "
        f"Retry in approximately "
        f"{rate_limit_remaining()} seconds. "
        "No automatic order will be placed "
        "using stale candle data."
    )


# ============================================================
# ERROR DIAGNOSTICS
# ============================================================

if automation_error:

    with st.expander(
        "Error diagnostics",
        expanded=True,
    ):

        st.error(
            automation_error
        )

        text = (
            automation_error.lower()
        )

        if (
            "invalid api key"
            in text
            or
            "invalid app"
            in text
        ):

            st.code(
                "Check ANGEL_API_KEY. "
                "Use the API key belonging to "
                "your correct Angel One SmartAPI application."
            )

        if (
            "totp"
            in text
            or
            "base32"
            in text
            or
            "ab1050"
            in text
        ):

            st.code(
                "Check ANGEL_TOTP_SECRET. "
                "Use the actual TOTP base32 secret, "
                "not the 6-digit OTP."
            )

        if (
            "rate limit"
            in text
            or
            "throttle"
            in text
            or
            "too many"
            in text
        ):

            st.code(
                "Angel One is throttling API requests. "
                "This version reduces normal API requests "
                "to help prevent repeated rate-limit errors. "
                "If the broker is already throttling the account, "
                "wait for the cooldown before restarting."
            )


# ============================================================
# STRATEGY EXPLANATION
# ============================================================

with st.expander(
    "How automatic BUY CE works"
):

    st.markdown(
        """
### Strategy

**ONLY 2-Minute Supertrend (20, 1.5)**

There is NO 5-minute, 15-minute or 4-hour confirmation.

### Flow

1. Login to Angel One.
2. Request NIFTY 1-minute candles.
3. Remove the currently forming 1-minute candle.
4. Convert 1-minute candles to 2-minute candles.
5. Use only the latest CLOSED 2-minute candle.
6. Calculate Supertrend `(20, 1.5)`.
7. If Supertrend is GREEN:
   **BUY CE**
8. If Supertrend is RED:
   **WAIT**
9. Calculate ATM NIFTY strike from the latest completed NIFTY close.
10. Select the nearest valid future NIFTY CE expiry.
11. Get the exact CE symbol/token/lot size from the Angel One scrip master.
12. Automatically submit a BUY MARKET order.
13. Verify the order in Angel One Order Book.
14. The same 2-minute candle cannot create another order.
15. If an order response is uncertain, the program does NOT blindly retry.

### Important

The strategy does NOT require a GREEN FLIP.

If the latest CLOSED 2-minute Supertrend is already GREEN,
the signal is **BUY CE**.
"""
    )


# ============================================================
# SYSTEM INFORMATION
# ============================================================

with st.expander(
    "System information"
):

    st.write(
        f"Supertrend period: {ST_PERIOD}"
    )

    st.write(
        f"Supertrend multiplier: {ST_MULTIPLIER}"
    )

    st.write(
        f"Lots: {LOTS}"
    )

    st.write(
        f"Refresh: {REFRESH_SECONDS} seconds"
    )

    st.write(
        f"Candle API minimum interval: "
        f"{CANDLE_MIN_INTERVAL} seconds"
    )

    st.write(
        f"Entry window: "
        f"{ENTRY_START} - {ENTRY_END}"
    )

    st.write(
        f"Paper trading: {PAPER_TRADING}"
    )

    st.write(
        f"Instrument cache: "
        f"{INSTRUMENT_CACHE}"
    )


# ============================================================
# AUTO REFRESH
# ============================================================

st.caption(
    f"Automatic dashboard refresh: "
    f"every {REFRESH_SECONDS} seconds"
)

time.sleep(
    REFRESH_SECONDS
)

st.rerun()
