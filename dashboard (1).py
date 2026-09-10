# ============================================================
# dashboard.py
#
# NIFTY 50 AUTOMATIC BUY CE ONLY
# ANGEL ONE SMARTAPI + STREAMLIT
#
# STRATEGY
# ------------------------------------------------------------
# 1-minute candles
#       ↓
# 2-minute candles
#       ↓
# Supertrend (20, 1.5)
#       ↓
# GREEN = BUY CE
# RED   = WAIT
#       ↓
# Select nearest ATM NIFTY CE
#       ↓
# Automatically BUY 1 LOT
#       ↓
# Verify Order Book
#
# NO MANUAL BUY / SELL BUTTONS
#
# RATE LIMIT PROTECTION
# ------------------------------------------------------------
# - Candle API only once per 2-minute candle
# - No repeated NIFTY LTP API
# - No option LTP API required for strategy
# - Order Book only after an order / slow refresh
# - 180 second rate-limit cooldown
#
# PAPER_TRADING=false -> REAL ORDER
# PAPER_TRADING=true  -> NO REAL ORDER
# ============================================================

import os
import json
import time
import re
from pathlib import Path
from datetime import datetime

import requests
import pyotp
import pandas as pd
import numpy as np
import streamlit as st

from SmartApi import SmartConnect


# ============================================================
# PAGE
# ============================================================

st.set_page_config(
    page_title="NIFTY Automatic CE",
    page_icon="📈",
    layout="wide",
)


# ============================================================
# CONFIG
# ============================================================

NIFTY_SYMBOL = "NIFTY"
NIFTY_TOKEN = "99926000"

ST_PERIOD = 20
ST_MULTIPLIER = 1.5

LOTS = 1

ORDER_TYPE = "MARKET"
PRODUCT_TYPE = "INTRADAY"
ORDER_VARIETY = "NORMAL"
ORDER_DURATION = "DAY"

# ------------------------------------------------------------
# IMPORTANT RATE LIMIT SETTINGS
# ------------------------------------------------------------

STREAMLIT_REFRESH_SECONDS = 60

CANDLE_MIN_INTERVAL = 120

RATE_LIMIT_COOLDOWN = 180

ORDERBOOK_MIN_INTERVAL = 180

ORDERBOOK_VERIFY_ATTEMPTS = 1

ORDERBOOK_VERIFY_DELAY = 3

# Only one automatic CE order per trading day.
ONE_ORDER_PER_DAY = True

# ------------------------------------------------------------
# Candle history
# ------------------------------------------------------------

CANDLE_DAYS = 2

# ------------------------------------------------------------
# REAL TRADING
# ------------------------------------------------------------

PAPER_TRADING = (
    os.getenv(
        "PAPER_TRADING",
        "false"
    )
    .strip()
    .lower()
    == "true"
)


# ============================================================
# FILES
# ============================================================

BASE_DIR = Path(
    __file__
).resolve().parent

STATE_FILE = (
    BASE_DIR
    / "nifty_2min_ce_state.json"
)

INSTRUMENT_FILE = (
    BASE_DIR
    / "OpenAPIScripMaster.json"
)

INSTRUMENT_URL = (
    "https://margincalculator.angelone.in/"
    "OpenAPI_File/files/OpenAPIScripMaster.json"
)


# ============================================================
# CREDENTIALS
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

    "login_done": False,

    "df_1m": None,

    "df_2m": None,

    "signal": "WAIT",

    "nifty_spot": None,

    "atm_option": None,

    "last_order_id": "",

    "last_order_symbol": "",

    "last_order_status": "",

    "order_status_unknown": False,

    "last_buy_candle": "",

    "last_order_date": "",

    "automatic_order_attempted": False,

    "order_book": [],

    "order_book_time": 0,

    "last_candle_api_time": 0,

    "last_candle_boundary": "",

    "rate_limited_until": 0,

    "last_error": "",
}


for key, value in DEFAULTS.items():

    if key not in st.session_state:

        st.session_state[key] = value


# ============================================================
# TIME
# ============================================================

IST = "Asia/Kolkata"


def now_ist():

    # Always Pandas Timestamp.
    # Therefore .floor() works.
    return pd.Timestamp.now(
        tz=IST
    )


def today_string():

    return now_ist().strftime(
        "%Y-%m-%d"
    )


# ============================================================
# STATE FILE
# ============================================================

def load_state():

    if not STATE_FILE.exists():

        return

    try:

        with open(
            STATE_FILE,
            "r",
            encoding="utf-8"
        ) as f:

            data = json.load(f)

        keys = [
            "last_order_id",
            "last_order_symbol",
            "last_order_status",
            "order_status_unknown",
            "last_buy_candle",
            "last_order_date",
        ]

        for key in keys:

            if key in data:

                st.session_state[key] = (
                    data[key]
                )

    except Exception:

        pass


def save_state():

    data = {

        "last_order_id":
            st.session_state.get(
                "last_order_id",
                ""
            ),

        "last_order_symbol":
            st.session_state.get(
                "last_order_symbol",
                ""
            ),

        "last_order_status":
            st.session_state.get(
                "last_order_status",
                ""
            ),

        "order_status_unknown":
            st.session_state.get(
                "order_status_unknown",
                False
            ),

        "last_buy_candle":
            st.session_state.get(
                "last_buy_candle",
                ""
            ),

        "last_order_date":
            st.session_state.get(
                "last_order_date",
                ""
            ),
    }

    try:

        with open(
            STATE_FILE,
            "w",
            encoding="utf-8"
        ) as f:

            json.dump(
                data,
                f,
                indent=2
            )

    except Exception:

        pass


load_state()


# ============================================================
# RATE LIMIT
# ============================================================

def is_rate_limit_error(value):

    text = str(value).lower()

    patterns = [
        "access denied",
        "exceeding access rate",
        "rate limit",
        "too many requests",
        "429",
    ]

    return any(
        p in text
        for p in patterns
    )


def set_rate_limit():

    st.session_state.rate_limited_until = (
        time.time()
        + RATE_LIMIT_COOLDOWN
    )


def rate_limit_active():

    return (
        time.time()
        < st.session_state.get(
            "rate_limited_until",
            0
        )
    )


def seconds_until_rate_limit_end():

    return max(
        0,
        int(
            st.session_state.get(
                "rate_limited_until",
                0
            )
            - time.time()
        )
    )


# ============================================================
# TOTP
# ============================================================

def get_totp_secret(raw):

    if not raw:

        raise RuntimeError(
            "ANGEL_TOTP_SECRET is empty."
        )

    secret = raw.strip()

    if secret.lower().startswith(
        "otpauth://"
    ):

        match = re.search(
            r"(?:\?|&)secret=([^&]+)",
            secret,
            re.IGNORECASE
        )

        if not match:

            raise RuntimeError(
                "TOTP secret missing from "
                "otpauth URL."
            )

        secret = match.group(1)

    secret = (
        secret
        .replace(" ", "")
        .replace("-", "")
        .upper()
    )

    if re.fullmatch(
        r"\d{6}",
        secret
    ):

        raise RuntimeError(
            "ANGEL_TOTP_SECRET must be the "
            "Base32 secret, not the current "
            "6-digit OTP."
        )

    return secret


def generate_totp():

    secret = get_totp_secret(
        ANGEL_TOTP_SECRET
    )

    try:

        return pyotp.TOTP(
            secret
        ).now()

    except Exception as e:

        raise RuntimeError(
            f"TOTP generation failed: {e}"
        )


# ============================================================
# LOGIN
# ============================================================

def login_to_angel():

    if st.session_state.api is not None:

        return st.session_state.api

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

    if not ANGEL_TOTP_SECRET:

        raise RuntimeError(
            "ANGEL_TOTP_SECRET is missing."
        )

    try:

        totp = generate_totp()

        api = SmartConnect(
            api_key=ANGEL_API_KEY
        )

        response = api.generateSession(
            ANGEL_CLIENT_ID,
            ANGEL_PASSWORD,
            totp
        )

        if not response:

            raise RuntimeError(
                "Empty Angel One login response."
            )

        if not response.get(
            "status"
        ):

            raise RuntimeError(
                f"Angel login failed: "
                f"{response}"
            )

        data = (
            response.get("data")
            or {}
        )

        if not data.get(
            "jwtToken"
        ):

            raise RuntimeError(
                "Angel login succeeded but "
                "jwtToken is missing."
            )

        st.session_state.api = api

        st.session_state.login_done = True

        return api

    except Exception as e:

        if is_rate_limit_error(e):

            set_rate_limit()

        raise RuntimeError(
            f"Angel One login error: {e}"
        )


# ============================================================
# SHOULD FETCH CANDLES?
# ============================================================

def candle_request_allowed():

    if rate_limit_active():

        return False

    last_call = (
        st.session_state.get(
            "last_candle_api_time",
            0
        )
    )

    elapsed = (
        time.time()
        - last_call
    )

    if elapsed < CANDLE_MIN_INTERVAL:

        return False

    return True


# ============================================================
# GET 1-MINUTE CANDLES
# ============================================================

def get_nifty_1m_candles(api):

    # --------------------------------------------------------
    # IMPORTANT:
    # Do not call Angel again if cooldown is active.
    # --------------------------------------------------------

    if rate_limit_active():

        cached = (
            st.session_state.df_1m
        )

        if (
            cached is not None
            and not cached.empty
        ):

            return cached

        raise RuntimeError(
            "Angel One candle API is "
            "rate limited. "
            f"Wait approximately "
            f"{seconds_until_rate_limit_end()} "
            f"seconds."
        )

    # --------------------------------------------------------
    # API interval protection
    # --------------------------------------------------------

    if not candle_request_allowed():

        cached = (
            st.session_state.df_1m
        )

        if (
            cached is not None
            and not cached.empty
        ):

            return cached

        raise RuntimeError(
            "Candle API cooldown active."
        )

    now = now_ist()

    from_dt = (
        now
        - pd.Timedelta(
            days=CANDLE_DAYS
        )
    )

    params = {

        "exchange": "NSE",

        "symboltoken":
            NIFTY_TOKEN,

        "interval":
            "ONE_MINUTE",

        "fromdate":
            from_dt.strftime(
                "%Y-%m-%d %H:%M"
            ),

        "todate":
            now.strftime(
                "%Y-%m-%d %H:%M"
            ),
    }

    # Record request time BEFORE request.
    # This prevents repeated requests if
    # the API throws an exception.
    st.session_state.last_candle_api_time = (
        time.time()
    )

    try:

        response = api.getCandleData(
            params
        )

        if not response:

            raise RuntimeError(
                "Empty candle response."
            )

        if not response.get(
            "status"
        ):

            message = response.get(
                "message",
                "Candle API failed"
            )

            raise RuntimeError(
                message
            )

        data = response.get(
            "data"
        )

        if not data:

            raise RuntimeError(
                "Angel One returned no candles."
            )

        df = pd.DataFrame(
            data,
            columns=[
                "timestamp",
                "open",
                "high",
                "low",
                "close",
                "volume",
            ]
        )

        # ----------------------------------------------------
        # TIMESTAMP
        # ----------------------------------------------------

        df["timestamp"] = (
            pd.to_datetime(
                df["timestamp"],
                errors="coerce"
            )
        )

        df = df.dropna(
            subset=["timestamp"]
        )

        if df.empty:

            raise RuntimeError(
                "No valid candle timestamps."
            )

        # ----------------------------------------------------
        # TIMEZONE
        # ----------------------------------------------------

        if (
            df["timestamp"]
            .dt.tz is None
        ):

            df["timestamp"] = (
                df["timestamp"]
                .dt.tz_localize(IST)
            )

        else:

            df["timestamp"] = (
                df["timestamp"]
                .dt.tz_convert(IST)
            )

        df = df.set_index(
            "timestamp"
        )

        df = df.sort_index()

        # ----------------------------------------------------
        # NUMERIC
        # ----------------------------------------------------

        for column in [
            "open",
            "high",
            "low",
            "close",
            "volume",
        ]:

            df[column] = pd.to_numeric(
                df[column],
                errors="coerce"
            )

        df = df.dropna(
            subset=[
                "open",
                "high",
                "low",
                "close",
            ]
        )

        # ----------------------------------------------------
        # REMOVE CURRENT FORMING CANDLE
        # ----------------------------------------------------

        current_minute = (
            now_ist().floor("min")
        )

        df = df[
            df.index < current_minute
        ]

        # ----------------------------------------------------
        # MARKET HOURS
        # ----------------------------------------------------

        df = df.between_time(
            "09:15",
            "15:30"
        )

        if df.empty:

            raise RuntimeError(
                "No completed NSE candles."
            )

        st.session_state.df_1m = df

        st.session_state.last_error = ""

        return df

    except Exception as e:

        if is_rate_limit_error(e):

            set_rate_limit()

            cached = (
                st.session_state.df_1m
            )

            if (
                cached is not None
                and not cached.empty
            ):

                return cached

            raise RuntimeError(
                "Angel One candle API rate "
                "limit reached. "
                "No new request will be made "
                "for 180 seconds."
            )

        raise RuntimeError(
            f"Candle API Error: {e}"
        )


# ============================================================
# 1 MIN → 2 MIN
# ============================================================

def make_2min_candles(df):

    if df is None or df.empty:

        return pd.DataFrame()

    result = (
        df.resample(
            "2min",
            origin="start_day",
            offset="9h15min",
            label="right",
            closed="left"
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
    )

    result = result.between_time(
        "09:17",
        "15:30"
    )

    return result


# ============================================================
# SUPERTREND 20 / 1.5
# ============================================================

def calculate_supertrend(
    df,
    period=20,
    multiplier=1.5
):

    if df is None or df.empty:

        return pd.DataFrame()

    df = df.copy()

    high = df["high"]
    low = df["low"]
    close = df["close"]

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
            tr3
        ],
        axis=1
    ).max(axis=1)

    atr = (
        true_range
        .ewm(
            alpha=1 / period,
            adjust=False,
            min_periods=period
        )
        .mean()
    )

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
        index=df.index,
        dtype=float
    )

    final_lower = pd.Series(
        index=df.index,
        dtype=float
    )

    supertrend = pd.Series(
        index=df.index,
        dtype=float
    )

    direction = pd.Series(
        index=df.index,
        dtype=int
    )

    for i in range(len(df)):

        if i == 0:

            final_upper.iloc[i] = (
                basic_upper.iloc[i]
            )

            final_lower.iloc[i] = (
                basic_lower.iloc[i]
            )

            supertrend.iloc[i] = np.nan

            direction.iloc[i] = 0

            continue

        # ----------------------------------------------------
        # FINAL UPPER
        # ----------------------------------------------------

        if (
            basic_upper.iloc[i]
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

        # ----------------------------------------------------
        # FINAL LOWER
        # ----------------------------------------------------

        if (
            basic_lower.iloc[i]
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

        # ----------------------------------------------------
        # SUPERTREND
        # ----------------------------------------------------

        previous_st = (
            supertrend.iloc[i - 1]
        )

        if pd.isna(previous_st):

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

        elif (
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

    df["ATR"] = atr

    df["Final_Upper"] = (
        final_upper
    )

    df["Final_Lower"] = (
        final_lower
    )

    df["Supertrend"] = (
        supertrend
    )

    df["ST_Direction"] = (
        direction
    )

    df["ST_Green"] = (
        direction == 1
    )

    df["ST_Red"] = (
        direction == -1
    )

    return df


# ============================================================
# SIGNAL
# ============================================================

def get_signal(df):

    if df is None or df.empty:

        return "WAIT"

    valid = df[
        df["ST_Direction"] != 0
    ].dropna(
        subset=["Supertrend"]
    )

    if valid.empty:

        return "WAIT"

    latest = valid.iloc[-1]

    if latest["ST_Direction"] == 1:

        return "BUY CE"

    return "WAIT"


# ============================================================
# INSTRUMENT MASTER
# ============================================================

@st.cache_data(
    ttl=86400,
    show_spinner=False
)
def load_instrument_master():

    if not INSTRUMENT_FILE.exists():

        response = requests.get(
            INSTRUMENT_URL,
            timeout=30
        )

        response.raise_for_status()

        INSTRUMENT_FILE.write_bytes(
            response.content
        )

    with open(
        INSTRUMENT_FILE,
        "r",
        encoding="utf-8"
    ) as f:

        data = json.load(f)

    if not isinstance(
        data,
        list
    ):

        raise RuntimeError(
            "Invalid instrument master."
        )

    return pd.DataFrame(
        data
    )


# ============================================================
# STRIKE
# ============================================================

def normalize_strike(value):

    try:

        value = float(value)

        if value > 100000:

            value = value / 100

        return value

    except Exception:

        return np.nan


# ============================================================
# ATM CE
# ============================================================

def select_atm_ce(
    instruments,
    spot
):

    if (
        instruments is None
        or instruments.empty
        or spot is None
    ):

        return None

    df = instruments.copy()

    # --------------------------------------------------------
    # NFO
    # --------------------------------------------------------

    if "exch_seg" in df.columns:

        df = df[
            df["exch_seg"]
            .astype(str)
            .str.upper()
            .eq("NFO")
        ]

    # --------------------------------------------------------
    # NIFTY
    # --------------------------------------------------------

    mask = pd.Series(
        False,
        index=df.index
    )

    if "name" in df.columns:

        mask |= (
            df["name"]
            .astype(str)
            .str.upper()
            .eq("NIFTY")
        )

    if "symbol" in df.columns:

        mask |= (
            df["symbol"]
            .astype(str)
            .str.upper()
            .str.startswith("NIFTY")
        )

    df = df[mask]

    # --------------------------------------------------------
    # CE
    # --------------------------------------------------------

    if "symbol" not in df.columns:

        return None

    df = df[
        df["symbol"]
        .astype(str)
        .str.upper()
        .str.endswith("CE")
    ]

    # --------------------------------------------------------
    # OPTIDX
    # --------------------------------------------------------

    if "instrumenttype" in df.columns:

        temp = df[
            df["instrumenttype"]
            .astype(str)
            .str.upper()
            .eq("OPTIDX")
        ]

        if not temp.empty:

            df = temp

    # --------------------------------------------------------
    # EXPIRY
    # --------------------------------------------------------

    if "expiry" not in df.columns:

        return None

    df["expiry_dt"] = (
        pd.to_datetime(
            df["expiry"],
            errors="coerce"
        )
    )

    today = pd.Timestamp(
        today_string()
    )

    df = df[
        df["expiry_dt"].notna()
        & (
            df["expiry_dt"]
            .dt.normalize()
            >= today
        )
    ]

    if df.empty:

        return None

    nearest_expiry = (
        df["expiry_dt"]
        .dt.normalize()
        .min()
    )

    df = df[
        df["expiry_dt"]
        .dt.normalize()
        == nearest_expiry
    ]

    # --------------------------------------------------------
    # STRIKE
    # --------------------------------------------------------

    if "strike" not in df.columns:

        return None

    df["strike_normalized"] = (
        df["strike"]
        .apply(
            normalize_strike
        )
    )

    df = df[
        df["strike_normalized"]
        .notna()
    ]

    if df.empty:

        return None

    df["distance"] = (
        df["strike_normalized"]
        - float(spot)
    ).abs()

    selected = (
        df.sort_values(
            "distance"
        )
        .iloc[0]
    )

    try:

        lot_size = int(
            float(
                selected.get(
                    "lotsize",
                    1
                )
            )
        )

    except Exception:

        lot_size = 1

    return {

        "symbol":
            str(
                selected["symbol"]
            ),

        "token":
            str(
                selected["token"]
            ),

        "strike":
            float(
                selected[
                    "strike_normalized"
                ]
            ),

        "expiry":
            str(
                selected["expiry"]
            ),

        "lotsize":
            lot_size,

        "quantity":
            lot_size * LOTS,
    }


# ============================================================
# ORDER ID
# ============================================================

def extract_order_id(response):

    if response is None:

        return ""

    if isinstance(
        response,
        str
    ):

        return response.strip()

    if not isinstance(
        response,
        dict
    ):

        return ""

    data = response.get(
        "data"
    )

    if isinstance(
        data,
        dict
    ):

        for key in [
            "orderid",
            "orderId",
            "orderID"
        ]:

            value = data.get(
                key
            )

            if value:

                return str(
                    value
                )

    for key in [
        "orderid",
        "orderId",
        "orderID"
    ]:

        value = response.get(
            key
        )

        if value:

            return str(
                value
            )

    return ""


# ============================================================
# PLACE REAL ORDER
# ============================================================

def place_ce_order(
    api,
    option
):

    params = {

        "variety":
            ORDER_VARIETY,

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
            ORDER_DURATION,

        "price":
            "0",

        "squareoff":
            "0",

        "stoploss":
            "0",

        "quantity":
            str(
                option["quantity"]
            ),
    }

    # ========================================================
    # PAPER
    # ========================================================

    if PAPER_TRADING:

        order_id = (
            "PAPER-"
            + datetime.now().strftime(
                "%Y%m%d%H%M%S"
            )
        )

        return {
            "order_id": order_id,
            "unknown": False,
            "response": {
                "status": True,
                "data": {
                    "orderid": order_id
                }
            }
        }

    # ========================================================
    # REAL
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

        order_id = (
            extract_order_id(
                response
            )
        )

        if order_id:

            return {
                "order_id":
                    order_id,
                "unknown":
                    False,
                "response":
                    response,
            }

        return {
            "order_id": "",
            "unknown": True,
            "response": response,
        }

    except Exception as e:

        if is_rate_limit_error(e):

            set_rate_limit()

        # Never blindly retry an
        # uncertain broker request.
        return {
            "order_id": "",
            "unknown": True,
            "response": str(e),
        }


# ============================================================
# ORDER BOOK
# ============================================================

def get_order_book(api):

    if rate_limit_active():

        return st.session_state.order_book

    try:

        response = api.orderBook()

        if not response:

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

    except Exception as e:

        if is_rate_limit_error(e):

            set_rate_limit()

        return []


# ============================================================
# FIND ORDER
# ============================================================

def find_order_by_id(
    book,
    order_id
):

    if not order_id:

        return None

    target = str(
        order_id
    ).strip()

    for order in book:

        if not isinstance(
            order,
            dict
        ):

            continue

        oid = str(
            order.get(
                "orderid",
                ""
            )
        ).strip()

        if oid == target:

            return order

    return None


def find_buy_order_for_symbol(
    book,
    symbol
):

    target = (
        str(symbol)
        .strip()
        .upper()
    )

    for order in book:

        if not isinstance(
            order,
            dict
        ):

            continue

        order_symbol = (
            str(
                order.get(
                    "tradingsymbol",
                    ""
                )
            )
            .strip()
            .upper()
        )

        transaction = (
            str(
                order.get(
                    "transactiontype",
                    ""
                )
            )
            .strip()
            .upper()
        )

        if (
            order_symbol == target
            and transaction == "BUY"
        ):

            return order

    return None


# ============================================================
# VERIFY ORDER
# ============================================================

def verify_order(
    api,
    order_id,
    symbol
):

    book = []

    for attempt in range(
        ORDERBOOK_VERIFY_ATTEMPTS
    ):

        book = get_order_book(
            api
        )

        if order_id:

            found = find_order_by_id(
                book,
                order_id
            )

        else:

            found = (
                find_buy_order_for_symbol(
                    book,
                    symbol
                )
            )

        if found:

            return (
                found,
                book
            )

        if attempt < (
            ORDERBOOK_VERIFY_ATTEMPTS - 1
        ):

            time.sleep(
                ORDERBOOK_VERIFY_DELAY
            )

    return (
        None,
        book
    )


# ============================================================
# AUTOMATIC BUY
# ============================================================

def automatic_buy_ce(
    api,
    option,
    candle_time
):

    if not option:

        return

    # --------------------------------------------------------
    # Existing order
    # --------------------------------------------------------

    if (
        st.session_state.last_order_id
    ):

        return

    # --------------------------------------------------------
    # Unknown order
    # --------------------------------------------------------

    if (
        st.session_state.order_status_unknown
    ):

        return

    # --------------------------------------------------------
    # One order per day
    # --------------------------------------------------------

    if ONE_ORDER_PER_DAY:

        today = today_string()

        if (
            st.session_state.last_order_date
            == today
        ):

            return

    # --------------------------------------------------------
    # One order attempt per candle
    # --------------------------------------------------------

    candle_key = str(
        candle_time
    )

    if (
        st.session_state.last_buy_candle
        == candle_key
    ):

        return

    # --------------------------------------------------------
    # Prevent duplicate Streamlit rerun
    # --------------------------------------------------------

    if (
        st.session_state.automatic_order_attempted
    ):

        return

    st.session_state.automatic_order_attempted = (
        True
    )

    st.session_state.last_buy_candle = (
        candle_key
    )

    st.session_state.last_order_date = (
        today_string()
    )

    save_state()

    # ========================================================
    # PLACE
    # ========================================================

    result = place_ce_order(
        api,
        option
    )

    order_id = result.get(
        "order_id",
        ""
    )

    unknown = result.get(
        "unknown",
        False
    )

    # ========================================================
    # ORDER ID RECEIVED
    # ========================================================

    if order_id:

        st.session_state.last_order_id = (
            order_id
        )

        st.session_state.last_order_symbol = (
            option["symbol"]
        )

        st.session_state.order_status_unknown = (
            False
        )

        save_state()

        found, book = verify_order(
            api,
            order_id,
            option["symbol"]
        )

        st.session_state.order_book = (
            book
        )

        st.session_state.order_book_time = (
            time.time()
        )

        if found:

            st.session_state.last_order_status = str(
                found.get(
                    "orderstatus",
                    found.get(
                        "status",
                        "FOUND"
                    )
                )
            )

        else:

            st.session_state.last_order_status = (
                "ORDER ID RECEIVED"
            )

        save_state()

        return

    # ========================================================
    # EMPTY / UNKNOWN RESPONSE
    # ========================================================

    if unknown:

        # Check Order Book ONCE.
        # Do not retry the order.

        found, book = verify_order(
            api,
            "",
            option["symbol"]
        )

        st.session_state.order_book = (
            book
        )

        st.session_state.order_book_time = (
            time.time()
        )

        if found:

            broker_id = str(
                found.get(
                    "orderid",
                    ""
                )
            )

            st.session_state.last_order_id = (
                broker_id
            )

            st.session_state.last_order_symbol = (
                option["symbol"]
            )

            st.session_state.last_order_status = str(
                found.get(
                    "orderstatus",
                    found.get(
                        "status",
                        "FOUND"
                    )
                )
            )

            st.session_state.order_status_unknown = (
                False
            )

        else:

            st.session_state.order_status_unknown = (
                True
            )

            st.session_state.last_order_status = (
                "UNKNOWN - CHECK ANGEL ONE"
            )

        save_state()


# ============================================================
# ORDER BOOK REFRESH
# ============================================================

def refresh_order_book_if_needed(api):

    last_time = (
        st.session_state.get(
            "order_book_time",
            0
        )
    )

    if (
        time.time()
        - last_time
        < ORDERBOOK_MIN_INTERVAL
    ):

        return st.session_state.order_book

    book = get_order_book(
        api
    )

    if book:

        st.session_state.order_book = (
            book
        )

        st.session_state.order_book_time = (
            time.time()
        )

    return (
        st.session_state.order_book
    )


# ============================================================
# HEADER
# ============================================================

st.title(
    "📈 NIFTY Automatic BUY CE"
)

st.caption(
    "2-Minute Supertrend (20, 1.5) • "
    "GREEN = Automatic BUY ATM CE • "
    "RED = WAIT"
)


# ============================================================
# LIVE / PAPER
# ============================================================

if PAPER_TRADING:

    st.warning(
        "PAPER TRADING MODE — "
        "No real order will be sent."
    )

else:

    st.error(
        "🔴 LIVE TRADING ENABLED — "
        "A GREEN signal can automatically "
        "place a REAL NIFTY CE BUY order."
    )


# ============================================================
# RATE LIMIT STATUS
# ============================================================

if rate_limit_active():

    seconds_left = (
        seconds_until_rate_limit_end()
    )

    st.warning(
        "Angel One API rate limit is active. "
        f"Waiting {seconds_left} seconds. "
        "No candle API request will be made."
    )


# ============================================================
# LOGIN
# ============================================================

try:

    api = login_to_angel()

    st.success(
        "Angel One Connected"
    )

except Exception as e:

    st.error(
        str(e)
    )

    st.stop()


# ============================================================
# CANDLE DATA
# ============================================================

try:

    df_1m = get_nifty_1m_candles(
        api
    )

    df_2m = make_2min_candles(
        df_1m
    )

    if df_2m.empty:

        raise RuntimeError(
            "2-minute candle data is empty."
        )

    df_st = calculate_supertrend(
        df_2m,
        ST_PERIOD,
        ST_MULTIPLIER
    )

    if df_st.empty:

        raise RuntimeError(
            "Supertrend calculation returned empty data."
        )

    st.session_state.df_2m = (
        df_st
    )

except Exception as e:

    # --------------------------------------------------------
    # Use previous calculated data if available.
    # --------------------------------------------------------

    if (
        st.session_state.df_2m
        is not None
        and not st.session_state.df_2m.empty
    ):

        df_st = (
            st.session_state.df_2m
        )

        st.warning(
            str(e)
            + " Using previous cached candles."
        )

    else:

        st.error(
            f"Candle / Supertrend Error: {e}"
        )

        st.info(
            "Stop the Streamlit app, wait "
            "2–5 minutes, and start it again. "
            "Do not open multiple dashboard tabs."
        )

        st.stop()


# ============================================================
# LATEST COMPLETED CANDLE
# ============================================================

valid = df_st[
    df_st["ST_Direction"] != 0
].dropna(
    subset=[
        "Supertrend"
    ]
)

if valid.empty:

    latest = None

    signal = "WAIT"

else:

    latest = valid.iloc[-1]

    signal = get_signal(
        df_st
    )


st.session_state.signal = signal


# ============================================================
# USE CANDLE CLOSE AS NIFTY SPOT
#
# This eliminates the extra ltpData() call.
# ============================================================

if latest is not None:

    nifty_spot = float(
        latest["close"]
    )

    st.session_state.nifty_spot = (
        nifty_spot
    )

else:

    nifty_spot = (
        st.session_state.nifty_spot
    )


# ============================================================
# METRICS
# ============================================================

m1, m2, m3, m4 = st.columns(4)

with m1:

    if nifty_spot is not None:

        st.metric(
            "NIFTY Spot",
            f"{nifty_spot:,.2f}"
        )

    else:

        st.metric(
            "NIFTY Spot",
            "-"
        )


with m2:

    st.metric(
        "Supertrend",
        (
            "GREEN"
            if signal == "BUY CE"
            else "RED"
        )
    )


with m3:

    st.metric(
        "Signal",
        signal
    )


with m4:

    if latest is not None:

        st.metric(
            "Last 2-Min Candle",
            latest.name.strftime(
                "%H:%M"
            )
        )

    else:

        st.metric(
            "Last 2-Min Candle",
            "-"
        )


# ============================================================
# SUPERTREND VALUES
# ============================================================

if latest is not None:

    a, b, c, d = st.columns(4)

    with a:

        st.write(
            "**Close**"
        )

        st.write(
            f"{latest['close']:,.2f}"
        )

    with b:

        st.write(
            "**Supertrend**"
        )

        st.write(
            f"{latest['Supertrend']:,.2f}"
        )

    with c:

        st.write(
            "**ATR**"
        )

        st.write(
            f"{latest['ATR']:,.2f}"
        )

    with d:

        st.write(
            "**Direction**"
        )

        st.write(
            "GREEN"
            if latest["ST_Direction"] == 1
            else "RED"
        )


# ============================================================
# ATM CE
# ============================================================

option = None

if (
    signal == "BUY CE"
    and nifty_spot is not None
):

    try:

        instruments = (
            load_instrument_master()
        )

        option = select_atm_ce(
            instruments,
            nifty_spot
        )

        st.session_state.atm_option = (
            option
        )

    except Exception as e:

        st.error(
            f"ATM CE Selection Error: {e}"
        )


# ============================================================
# ATM CE DISPLAY
# ============================================================

st.subheader(
    "ATM NIFTY CE"
)

if option:

    x1, x2, x3, x4, x5 = (
        st.columns(5)
    )

    with x1:

        st.write(
            "**Symbol**"
        )

        st.write(
            option["symbol"]
        )

    with x2:

        st.write(
            "**Strike**"
        )

        st.write(
            f"{option['strike']:,.0f}"
        )

    with x3:

        st.write(
            "**Expiry**"
        )

        st.write(
            option["expiry"]
        )

    with x4:

        st.write(
            "**Lot Size**"
        )

        st.write(
            option["lotsize"]
        )

    with x5:

        st.write(
            "**Quantity**"
        )

        st.write(
            option["quantity"]
        )

else:

    if signal == "BUY CE":

        st.warning(
            "BUY CE signal detected, "
            "but ATM CE could not be selected."
        )

    else:

        st.info(
            "Signal is WAIT. "
            "ATM CE will be selected automatically "
            "when the completed 2-minute candle is GREEN."
        )


# ============================================================
# AUTOMATIC ORDER
# ============================================================

if (
    signal == "BUY CE"
    and option is not None
    and latest is not None
):

    automatic_buy_ce(
        api,
        option,
        latest.name
    )


# ============================================================
# ORDER STATUS
# ============================================================

st.subheader(
    "🤖 Automatic Order Status"
)

if st.session_state.last_order_id:

    st.success(
        "Order ID: "
        + st.session_state.last_order_id
    )

    st.write(
        "Symbol: "
        + st.session_state.last_order_symbol
    )

    st.write(
        "Status: "
        + st.session_state.last_order_status
    )

elif st.session_state.order_status_unknown:

    st.error(
        "Order status UNKNOWN. "
        "The application will NOT automatically "
        "retry because the original request may "
        "already have reached Angel One."
    )

else:

    st.info(
        "No automatic CE order placed yet."
    )


# ============================================================
# ORDER BOOK
# ============================================================

st.subheader(
    "📋 Angel One Order Book"
)

book = refresh_order_book_if_needed(
    api
)

if book:

    rows = []

    target_id = (
        st.session_state.last_order_id
    )

    for order in book:

        if not isinstance(
            order,
            dict
        ):

            continue

        oid = str(
            order.get(
                "orderid",
                ""
            )
        )

        symbol = str(
            order.get(
                "tradingsymbol",
                ""
            )
        )

        transaction = str(
            order.get(
                "transactiontype",
                ""
            )
        )

        quantity = order.get(
            "quantity",
            ""
        )

        price = order.get(
            "price",
            order.get(
                "averageprice",
                ""
            )
        )

        status = str(
            order.get(
                "orderstatus",
                order.get(
                    "status",
                    ""
                )
            )
        )

        rows.append({

            "NEW":
                (
                    "⭐ NEW"
                    if (
                        target_id
                        and oid
                        == target_id
                    )
                    else ""
                ),

            "Order ID":
                oid,

            "Symbol":
                symbol,

            "Transaction":
                transaction,

            "Quantity":
                quantity,

            "Price":
                price,

            "Status":
                status,
        })

    if rows:

        order_df = pd.DataFrame(
            rows
        )

        st.dataframe(
            order_df,
            use_container_width=True,
            hide_index=True
        )

else:

    st.info(
        "No cached Order Book data."
    )


# ============================================================
# SUPERTREND TABLE
# ============================================================

st.subheader(
    "📊 2-Minute Supertrend"
)

table = df_st[
    [
        "open",
        "high",
        "low",
        "close",
        "ATR",
        "Supertrend",
        "ST_Direction"
    ]
].tail(20).copy()

table["Signal"] = np.where(
    table["ST_Direction"] == 1,
    "GREEN",
    np.where(
        table["ST_Direction"] == -1,
        "RED",
        "WAIT"
    )
)

table = table.reset_index()

table["timestamp"] = (
    table["timestamp"]
    .dt.strftime(
        "%Y-%m-%d %H:%M"
    )
)

st.dataframe(
    table,
    use_container_width=True,
    hide_index=True
)


# ============================================================
# CHART
# ============================================================

st.subheader(
    "📈 NIFTY 2-Minute Chart"
)

chart = df_st[
    [
        "close",
        "Supertrend"
    ]
].tail(100)

st.line_chart(
    chart,
    use_container_width=True
)


# ============================================================
# SYSTEM INFORMATION
# ============================================================

with st.expander(
    "System Information"
):

    st.write(
        "Strategy: "
        "2-Minute Supertrend (20, 1.5)"
    )

    st.write(
        f"NIFTY Token: {NIFTY_TOKEN}"
    )

    st.write(
        f"Candle API minimum interval: "
        f"{CANDLE_MIN_INTERVAL} seconds"
    )

    st.write(
        f"Rate-limit cooldown: "
        f"{RATE_LIMIT_COOLDOWN} seconds"
    )

    st.write(
        f"Dashboard refresh: "
        f"{STREAMLIT_REFRESH_SECONDS} seconds"
    )

    st.write(
        f"Lots: {LOTS}"
    )

    st.write(
        f"Order Type: {ORDER_TYPE}"
    )

    st.write(
        f"Product Type: {PRODUCT_TYPE}"
    )

    st.write(
        f"Paper Trading: {PAPER_TRADING}"
    )

    st.write(
        f"Instrument File: "
        f"{INSTRUMENT_FILE}"
    )

    st.write(
        f"Instrument File Exists: "
        f"{INSTRUMENT_FILE.exists()}"
    )

    st.write(
        f"Last candle API request: "
        f"{st.session_state.last_candle_api_time}"
    )


# ============================================================
# AUTOMATIC REFRESH
# ============================================================

time.sleep(
    STREAMLIT_REFRESH_SECONDS
)

st.rerun()
