# ============================================================
# dashboard.py
#
# NIFTY 50 AUTOMATIC BUY CE ONLY
# ANGEL ONE SMARTAPI + STREAMLIT
#
# STRATEGY
# ------------------------------------------------------------
# 1-minute candles
#        ↓
# 2-minute candles
#        ↓
# Supertrend (20, 1.5)
#        ↓
# GREEN = BUY CE
# RED   = WAIT
#        ↓
# Select nearest NIFTY ATM CE
#        ↓
# Automatically BUY 1 LOT
#        ↓
# Verify Order ID in Angel One Order Book
#
# NO MANUAL BUY / SELL BUTTONS
#
# IMPORTANT:
# PAPER_TRADING=false  -> REAL ORDER
# PAPER_TRADING=true   -> NO REAL ORDER
# ============================================================

import os
import json
import time
import re
from pathlib import Path
from datetime import datetime, timedelta

import requests
import pyotp
import pandas as pd
import numpy as np
import streamlit as st

from SmartApi import SmartConnect


# ============================================================
# STREAMLIT PAGE
# ============================================================

st.set_page_config(
    page_title="NIFTY Automatic CE",
    page_icon="📈",
    layout="wide",
)


# ============================================================
# CONFIGURATION
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
# API RATE LIMIT PROTECTION
# ------------------------------------------------------------

REFRESH_SECONDS = 60

# Order book is not needed every 60 seconds.
ORDERBOOK_REFRESH_SECONDS = 120

# After an order, check Order Book only a few times.
ORDERBOOK_VERIFY_ATTEMPTS = 2
ORDERBOOK_VERIFY_DELAY = 3

CANDLE_DAYS = 3

# ------------------------------------------------------------
# REAL / PAPER TRADING
# ------------------------------------------------------------

PAPER_TRADING = (
    os.getenv("PAPER_TRADING", "false")
    .strip()
    .lower()
    == "true"
)


# ============================================================
# FILE PATHS
# ============================================================

BASE_DIR = Path(__file__).resolve().parent

STATE_FILE = BASE_DIR / "nifty_2min_ce_state.json"

INSTRUMENT_FILE = BASE_DIR / "OpenAPIScripMaster.json"

INSTRUMENT_URL = (
    "https://margincalculator.angelone.in/"
    "OpenAPI_File/files/OpenAPIScripMaster.json"
)


# ============================================================
# ANGEL ONE CREDENTIALS
# ============================================================

ANGEL_API_KEY = os.getenv("ANGEL_API_KEY", "").strip()
ANGEL_CLIENT_ID = os.getenv("ANGEL_CLIENT_ID", "").strip()
ANGEL_PASSWORD = os.getenv("ANGEL_PASSWORD", "").strip()
ANGEL_TOTP_SECRET = os.getenv("ANGEL_TOTP_SECRET", "").strip()


# ============================================================
# SESSION STATE
# ============================================================

DEFAULTS = {
    "api": None,
    "login_done": False,
    "login_message": "",

    "nifty_ltp": None,

    "df_1m": None,
    "df_2m": None,

    "signal": "WAIT",
    "previous_signal": "WAIT",

    "atm_option": None,
    "option_ltp": None,

    "last_order_id": "",
    "last_order_symbol": "",
    "last_order_status": "",
    "order_status_unknown": False,

    "last_buy_candle": "",

    "order_book": [],
    "order_book_time": 0,

    "last_candle_api_time": 0,
    "last_ltp_api_time": 0,

    "rate_limited_until": 0,
    "last_error": "",

    "running": True,

    "automatic_order_attempted": False,
}

for key, value in DEFAULTS.items():
    if key not in st.session_state:
        st.session_state[key] = value


# ============================================================
# STATE FILE
# ============================================================

def load_state():
    if not STATE_FILE.exists():
        return

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        for key in [
            "last_order_id",
            "last_order_symbol",
            "last_order_status",
            "order_status_unknown",
            "last_buy_candle",
        ]:
            if key in data:
                st.session_state[key] = data[key]

    except Exception:
        pass


def save_state():
    data = {
        "last_order_id": st.session_state.get(
            "last_order_id", ""
        ),
        "last_order_symbol": st.session_state.get(
            "last_order_symbol", ""
        ),
        "last_order_status": st.session_state.get(
            "last_order_status", ""
        ),
        "order_status_unknown": st.session_state.get(
            "order_status_unknown", False
        ),
        "last_buy_candle": st.session_state.get(
            "last_buy_candle", ""
        ),
    }

    try:
        with open(
            STATE_FILE,
            "w",
            encoding="utf-8"
        ) as f:
            json.dump(data, f, indent=2)

    except Exception:
        pass


load_state()


# ============================================================
# TIME HELPERS
# ============================================================

IST = "Asia/Kolkata"


def now_ist():
    """
    ALWAYS return pandas Timestamp.
    This prevents:
        'datetime.datetime' object has no attribute 'floor'
    """
    return pd.Timestamp.now(tz=IST)


def today_ist():
    return now_ist().date()


# ============================================================
# TOTP
# ============================================================

def get_totp_secret(raw_secret):
    if not raw_secret:
        raise RuntimeError(
            "ANGEL_TOTP_SECRET is empty"
        )

    secret = raw_secret.strip()

    # Handle otpauth:// URL
    if secret.lower().startswith("otpauth://"):
        match = re.search(
            r"(?:\?|&)secret=([^&]+)",
            secret,
            re.IGNORECASE,
        )

        if not match:
            raise RuntimeError(
                "TOTP otpauth URL does not contain secret"
            )

        secret = match.group(1)

    secret = secret.replace(" ", "")
    secret = secret.replace("-", "")
    secret = secret.upper()

    # Reject current 6-digit OTP
    if re.fullmatch(r"\d{6}", secret):
        raise RuntimeError(
            "ANGEL_TOTP_SECRET must be the Base32 secret, "
            "not the 6-digit OTP."
        )

    return secret


def generate_totp():
    secret = get_totp_secret(
        ANGEL_TOTP_SECRET
    )

    try:
        return pyotp.TOTP(secret).now()

    except Exception as e:
        raise RuntimeError(
            f"Unable to generate TOTP: {e}"
        )


# ============================================================
# RATE LIMIT DETECTION
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
        pattern in text
        for pattern in patterns
    )


def mark_rate_limited():
    st.session_state.rate_limited_until = (
        time.time() + 60
    )


def rate_limit_active():
    return (
        time.time()
        < st.session_state.get(
            "rate_limited_until",
            0
        )
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
            totp,
        )

        if not response:
            raise RuntimeError(
                "Angel One returned empty login response."
            )

        if not response.get("status"):
            raise RuntimeError(
                f"Angel login failed: {response}"
            )

        data = response.get("data") or {}

        jwt_token = data.get("jwtToken")

        if not jwt_token:
            raise RuntimeError(
                f"Angel login succeeded but jwtToken "
                f"is missing: {response}"
            )

        st.session_state.api = api
        st.session_state.login_done = True
        st.session_state.login_message = (
            "Angel One login successful"
        )

        return api

    except Exception as e:

        if is_rate_limit_error(e):
            mark_rate_limited()

        raise RuntimeError(
            f"Angel One login error: {e}"
        )


# ============================================================
# NIFTY LTP
# ============================================================

def get_nifty_ltp(api):
    if rate_limit_active():
        return st.session_state.nifty_ltp

    try:
        response = api.ltpData(
            "NSE",
            NIFTY_SYMBOL,
            NIFTY_TOKEN,
        )

        if not response:
            raise RuntimeError(
                "Empty NIFTY LTP response"
            )

        if not response.get("status"):
            raise RuntimeError(
                response.get(
                    "message",
                    "NIFTY LTP failed"
                )
            )

        data = response.get("data") or {}

        ltp = data.get("ltp")

        if ltp is None:
            raise RuntimeError(
                f"NIFTY LTP missing: {response}"
            )

        ltp = float(ltp)

        st.session_state.nifty_ltp = ltp
        st.session_state.last_ltp_api_time = time.time()

        return ltp

    except Exception as e:

        if is_rate_limit_error(e):
            mark_rate_limited()

            st.session_state.last_error = (
                "Angel One rate limit reached. "
                "Waiting before next API request."
            )

            return st.session_state.nifty_ltp

        raise RuntimeError(
            f"NIFTY LTP Error: {e}"
        )


# ============================================================
# GET 1-MINUTE CANDLES
# ============================================================

def get_nifty_1m_candles(api, days=CANDLE_DAYS):

    if rate_limit_active():
        old_df = st.session_state.df_1m

        if old_df is not None and not old_df.empty:
            return old_df

        raise RuntimeError(
            "Angel One API rate limit active."
        )

    now = now_ist()

    from_dt = now - pd.Timedelta(
        days=days
    )

    params = {
        "exchange": "NSE",
        "symboltoken": NIFTY_TOKEN,
        "interval": "ONE_MINUTE",
        "fromdate": from_dt.strftime(
            "%Y-%m-%d %H:%M"
        ),
        "todate": now.strftime(
            "%Y-%m-%d %H:%M"
        ),
    }

    try:
        response = api.getCandleData(
            params
        )

        if not response:
            raise RuntimeError(
                "Empty candle response"
            )

        if not response.get("status"):
            raise RuntimeError(
                response.get(
                    "message",
                    "Candle API failed"
                )
            )

        data = response.get("data")

        if not data:
            raise RuntimeError(
                "Candle API returned no data"
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
            ],
        )

        # ----------------------------------------------------
        # Timestamp conversion
        # ----------------------------------------------------

        df["timestamp"] = pd.to_datetime(
            df["timestamp"],
            errors="coerce",
        )

        df = df.dropna(
            subset=["timestamp"]
        )

        if df.empty:
            raise RuntimeError(
                "No valid candle timestamps"
            )

        # ----------------------------------------------------
        # Timezone
        # ----------------------------------------------------

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

        df = df.set_index(
            "timestamp"
        )

        df = df.sort_index()

        # ----------------------------------------------------
        # Numeric conversion
        # ----------------------------------------------------

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
                "open",
                "high",
                "low",
                "close",
            ]
        )

        # ----------------------------------------------------
        # IMPORTANT:
        # Remove currently forming 1-minute candle.
        #
        # now_ist() returns pandas Timestamp,
        # therefore .floor() works.
        # ----------------------------------------------------

        current_minute = (
            now_ist().floor("min")
        )

        df = df[
            df.index < current_minute
        ]

        # ----------------------------------------------------
        # NSE market hours
        # ----------------------------------------------------

        df = df.between_time(
            "09:15",
            "15:30",
        )

        if df.empty:
            raise RuntimeError(
                "No completed NSE candles available."
            )

        st.session_state.df_1m = df
        st.session_state.last_candle_api_time = (
            time.time()
        )

        return df

    except Exception as e:

        if is_rate_limit_error(e):

            mark_rate_limited()

            old_df = st.session_state.df_1m

            if old_df is not None and not old_df.empty:
                return old_df

            raise RuntimeError(
                "Angel One candle API rate limit reached."
            )

        raise RuntimeError(
            f"Candle API Error: {e}"
        )


# ============================================================
# RESAMPLE 1-MINUTE → 2-MINUTE
# ============================================================

def make_2min_candles(df):

    if df is None or df.empty:
        return pd.DataFrame()

    df = df.copy()

    df_2m = (
        df.resample(
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
    )

    df_2m = df_2m.between_time(
        "09:17",
        "15:30",
    )

    return df_2m


# ============================================================
# SUPERTREND
# ============================================================

def calculate_supertrend(
    df,
    period=ST_PERIOD,
    multiplier=ST_MULTIPLIER,
):

    if df is None or df.empty:
        return pd.DataFrame()

    df = df.copy()

    high = df["high"]
    low = df["low"]
    close = df["close"]

    # --------------------------------------------------------
    # True Range
    # --------------------------------------------------------

    prev_close = close.shift(1)

    tr1 = high - low

    tr2 = (
        high - prev_close
    ).abs()

    tr3 = (
        low - prev_close
    ).abs()

    true_range = pd.concat(
        [tr1, tr2, tr3],
        axis=1,
    ).max(axis=1)

    # --------------------------------------------------------
    # Wilder ATR
    # --------------------------------------------------------

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
    ) / 2

    basic_upper = (
        hl2 + multiplier * atr
    )

    basic_lower = (
        hl2 - multiplier * atr
    )

    final_upper = pd.Series(
        index=df.index,
        dtype=float,
    )

    final_lower = pd.Series(
        index=df.index,
        dtype=float,
    )

    supertrend = pd.Series(
        index=df.index,
        dtype=float,
    )

    direction = pd.Series(
        index=df.index,
        dtype=int,
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
        # Final Upper
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
        # Final Lower
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
        # Supertrend
        # ----------------------------------------------------

        previous_st = (
            supertrend.iloc[i - 1]
        )

        if pd.isna(previous_st):

            if close.iloc[i] <= final_upper.iloc[i]:

                supertrend.iloc[i] = (
                    final_upper.iloc[i]
                )

                direction.iloc[i] = -1

            else:

                supertrend.iloc[i] = (
                    final_lower.iloc[i]
                )

                direction.iloc[i] = 1

        elif previous_st == final_upper.iloc[i - 1]:

            if close.iloc[i] <= final_upper.iloc[i]:

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

            if close.iloc[i] >= final_lower.iloc[i]:

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

    df["Basic_Upper"] = basic_upper
    df["Basic_Lower"] = basic_lower

    df["Final_Upper"] = final_upper
    df["Final_Lower"] = final_lower

    df["Supertrend"] = supertrend
    df["ST_Direction"] = direction

    df["ST_Green"] = (
        df["ST_Direction"] == 1
    )

    df["ST_Red"] = (
        df["ST_Direction"] == -1
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
    show_spinner=False,
)
def load_instrument_master():

    # --------------------------------------------------------
    # Download only if local file does not exist
    # --------------------------------------------------------

    if not INSTRUMENT_FILE.exists():

        response = requests.get(
            INSTRUMENT_URL,
            timeout=30,
        )

        response.raise_for_status()

        INSTRUMENT_FILE.write_bytes(
            response.content
        )

    with open(
        INSTRUMENT_FILE,
        "r",
        encoding="utf-8",
    ) as f:

        data = json.load(f)

    if not isinstance(data, list):
        raise RuntimeError(
            "Invalid Angel One instrument master."
        )

    return pd.DataFrame(data)


# ============================================================
# STRIKE NORMALIZATION
# ============================================================

def normalize_strike(value):

    try:
        strike = float(value)

        # Angel master can contain strikes such as 2500000
        if strike > 100000:
            strike = strike / 100.0

        return strike

    except Exception:
        return np.nan


# ============================================================
# ATM CE SELECTION
# ============================================================

def select_atm_ce(
    instruments,
    spot,
):

    if instruments is None or instruments.empty:
        return None

    if spot is None:
        return None

    df = instruments.copy()

    # --------------------------------------------------------
    # Exchange
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

    name_mask = pd.Series(
        False,
        index=df.index,
    )

    if "name" in df.columns:

        name_mask |= (
            df["name"]
            .astype(str)
            .str.upper()
            .eq("NIFTY")
        )

    if "symbol" in df.columns:

        name_mask |= (
            df["symbol"]
            .astype(str)
            .str.upper()
            .str.startswith("NIFTY")
        )

    df = df[name_mask]

    # --------------------------------------------------------
    # CE only
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

        optidx = df[
            df["instrumenttype"]
            .astype(str)
            .str.upper()
            .eq("OPTIDX")
        ]

        if not optidx.empty:
            df = optidx

    # --------------------------------------------------------
    # Expiry
    # --------------------------------------------------------

    if "expiry" not in df.columns:
        return None

    df["expiry_dt"] = pd.to_datetime(
        df["expiry"],
        errors="coerce",
    )

    today = pd.Timestamp(
        today_ist()
    ).normalize()

    df = df[
        df["expiry_dt"].notna()
        & (
            df["expiry_dt"].dt.normalize()
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
    # Strike
    # --------------------------------------------------------

    if "strike" not in df.columns:
        return None

    df["strike_normalized"] = (
        df["strike"]
        .apply(normalize_strike)
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
        df.sort_values("distance")
        .iloc[0]
    )

    lot_size = selected.get(
        "lotsize",
        1,
    )

    try:
        lot_size = int(float(lot_size))
    except Exception:
        lot_size = 1

    quantity = lot_size * LOTS

    return {
        "symbol": str(
            selected["symbol"]
        ),
        "token": str(
            selected["token"]
        ),
        "strike": float(
            selected["strike_normalized"]
        ),
        "expiry": str(
            selected["expiry"]
        ),
        "lotsize": lot_size,
        "quantity": quantity,
    }


# ============================================================
# OPTION LTP
# ============================================================

def get_option_ltp(
    api,
    option,
):

    if not option:
        return None

    if rate_limit_active():
        return st.session_state.option_ltp

    try:

        response = api.ltpData(
            "NFO",
            option["symbol"],
            option["token"],
        )

        if not response:
            return None

        if not response.get("status"):
            return None

        data = response.get("data") or {}

        ltp = data.get("ltp")

        if ltp is None:
            return None

        ltp = float(ltp)

        st.session_state.option_ltp = ltp

        return ltp

    except Exception as e:

        if is_rate_limit_error(e):
            mark_rate_limited()
            return st.session_state.option_ltp

        return None


# ============================================================
# ORDER RESPONSE EXTRACTION
# ============================================================

def extract_order_id(response):

    if response is None:
        return ""

    if isinstance(response, str):

        text = response.strip()

        if text:
            return text

        return ""

    if not isinstance(response, dict):
        return ""

    data = response.get("data")

    if isinstance(data, dict):

        for key in [
            "orderid",
            "orderId",
            "orderID",
        ]:

            value = data.get(key)

            if value:
                return str(value)

    for key in [
        "orderid",
        "orderId",
        "orderID",
    ]:

        value = response.get(key)

        if value:
            return str(value)

    return ""


# ============================================================
# PLACE ORDER
# ============================================================

def place_ce_order(
    api,
    option,
):

    if not option:
        raise RuntimeError(
            "ATM CE option not selected."
        )

    params = {
        "variety": ORDER_VARIETY,
        "tradingsymbol": option["symbol"],
        "symboltoken": str(
            option["token"]
        ),
        "transactiontype": "BUY",
        "exchange": "NFO",
        "ordertype": ORDER_TYPE,
        "producttype": PRODUCT_TYPE,
        "duration": ORDER_DURATION,
        "price": "0",
        "squareoff": "0",
        "stoploss": "0",
        "quantity": str(
            option["quantity"]
        ),
    }

    # ========================================================
    # PAPER MODE
    # ========================================================

    if PAPER_TRADING:

        fake_order_id = (
            "PAPER-"
            + datetime.now().strftime(
                "%Y%m%d%H%M%S"
            )
        )

        return {
            "order_id": fake_order_id,
            "response": {
                "status": True,
                "message": "PAPER ORDER",
                "data": {
                    "orderid": fake_order_id
                },
            },
        }

    # ========================================================
    # REAL ORDER
    # ========================================================

    try:

        # ----------------------------------------------------
        # Prefer Full Response if available
        # ----------------------------------------------------

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

            response = api.placeOrder(
                params
            )

        order_id = extract_order_id(
            response
        )

        # ----------------------------------------------------
        # Empty response is NOT automatically failure.
        #
        # The request may have reached Angel.
        # We must verify Order Book before retrying.
        # ----------------------------------------------------

        if not order_id:

            return {
                "order_id": "",
                "response": response,
                "unknown": True,
            }

        return {
            "order_id": order_id,
            "response": response,
            "unknown": False,
        }

    except Exception as e:

        if is_rate_limit_error(e):
            mark_rate_limited()

        # ----------------------------------------------------
        # DO NOT blindly retry.
        # An exception can happen after broker received order.
        # ----------------------------------------------------

        return {
            "order_id": "",
            "response": str(e),
            "unknown": True,
        }


# ============================================================
# ORDER BOOK
# ============================================================

def get_order_book(api):

    try:

        response = api.orderBook()

        if not response:
            return []

        if not response.get("status"):
            return []

        data = response.get("data")

        if not data:
            return []

        if isinstance(data, list):
            return data

        return []

    except Exception as e:

        if is_rate_limit_error(e):
            mark_rate_limited()

        return []


# ============================================================
# FIND ORDER BY EXACT ORDER ID
# ============================================================

def find_order_by_id(
    order_book,
    order_id,
):

    if not order_id:
        return None

    target = str(
        order_id
    ).strip()

    for order in order_book:

        if not isinstance(order, dict):
            continue

        broker_order_id = str(
            order.get("orderid", "")
        ).strip()

        if (
            broker_order_id
            == target
        ):
            return order

    return None


# ============================================================
# FIND ORDER BY SYMBOL
# Used ONLY when broker did not return order ID.
# ============================================================

def find_buy_order_for_symbol(
    order_book,
    symbol,
):

    if not symbol:
        return None

    target = (
        str(symbol)
        .strip()
        .upper()
    )

    candidates = []

    for order in order_book:

        if not isinstance(order, dict):
            continue

        order_symbol = str(
            order.get(
                "tradingsymbol",
                ""
            )
        ).strip().upper()

        transaction = str(
            order.get(
                "transactiontype",
                ""
            )
        ).strip().upper()

        if (
            order_symbol == target
            and transaction == "BUY"
        ):

            candidates.append(order)

    if not candidates:
        return None

    # Latest item generally appears first,
    # but returning first is safer than guessing timestamps.
    return candidates[0]


# ============================================================
# VERIFY NEW ORDER
# ============================================================

def verify_order(
    api,
    order_id,
    symbol,
):

    latest_book = []

    for attempt in range(
        ORDERBOOK_VERIFY_ATTEMPTS
    ):

        latest_book = get_order_book(
            api
        )

        # ----------------------------------------------------
        # Exact Order ID
        # ----------------------------------------------------

        if order_id:

            found = find_order_by_id(
                latest_book,
                order_id,
            )

            if found:

                return (
                    found,
                    latest_book,
                )

        # ----------------------------------------------------
        # If order ID was not returned,
        # search symbol.
        # ----------------------------------------------------

        else:

            found = (
                find_buy_order_for_symbol(
                    latest_book,
                    symbol,
                )
            )

            if found:

                return (
                    found,
                    latest_book,
                )

        if attempt < (
            ORDERBOOK_VERIFY_ATTEMPTS - 1
        ):

            time.sleep(
                ORDERBOOK_VERIFY_DELAY
            )

    return (
        None,
        latest_book,
    )


# ============================================================
# AUTOMATIC BUY CE
# ============================================================

def automatic_buy_ce(
    api,
    option,
    candle_time,
):

    if not option:
        return

    # --------------------------------------------------------
    # Already placed/verified order
    # --------------------------------------------------------

    if st.session_state.last_order_id:
        return

    # --------------------------------------------------------
    # Unknown previous order:
    # DO NOT place another order.
    # --------------------------------------------------------

    if st.session_state.order_status_unknown:
        return

    # --------------------------------------------------------
    # One automatic order per candle.
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
    # Prevent repeated execution during reruns
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

    save_state()

    # ========================================================
    # PLACE
    # ========================================================

    result = place_ce_order(
        api,
        option,
    )

    order_id = result.get(
        "order_id",
        "",
    )

    unknown = result.get(
        "unknown",
        False,
    )

    # --------------------------------------------------------
    # Successful order response
    # --------------------------------------------------------

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

        # ----------------------------------------------------
        # Verify exact order in Order Book
        # ----------------------------------------------------

        found, book = verify_order(
            api,
            order_id,
            option["symbol"],
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
                        "FOUND",
                    ),
                )
            )

        else:

            st.session_state.last_order_status = (
                "ORDER ID RECEIVED - "
                "WAITING FOR ORDER BOOK"
            )

        save_state()

        return

    # --------------------------------------------------------
    # Empty/uncertain response
    # --------------------------------------------------------

    if unknown:

        found, book = verify_order(
            api,
            "",
            option["symbol"],
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
                    "",
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
                        "FOUND",
                    ),
                )
            )

            st.session_state.order_status_unknown = (
                False
            )

            save_state()

        else:

            # ------------------------------------------------
            # VERY IMPORTANT:
            # Do not automatically retry.
            # ------------------------------------------------

            st.session_state.order_status_unknown = (
                True
            )

            st.session_state.last_order_status = (
                "UNKNOWN - CHECK ANGEL ONE"
            )

            save_state()


# ============================================================
# REFRESH ORDER BOOK ONLY WHEN NEEDED
# ============================================================

def refresh_order_book_if_needed(api):

    last_time = st.session_state.get(
        "order_book_time",
        0,
    )

    # No need to call every 60 seconds
    if (
        time.time() - last_time
        < ORDERBOOK_REFRESH_SECONDS
    ):
        return st.session_state.order_book

    book = get_order_book(api)

    if book:

        st.session_state.order_book = (
            book
        )

        st.session_state.order_book_time = (
            time.time()
        )

    return st.session_state.order_book


# ============================================================
# UPDATE ORDER STATUS
# ============================================================

def update_saved_order_status():

    order_id = (
        st.session_state.last_order_id
    )

    if not order_id:
        return

    book = st.session_state.get(
        "order_book",
        [],
    )

    found = find_order_by_id(
        book,
        order_id,
    )

    if found:

        status = found.get(
            "orderstatus",
            found.get(
                "status",
                "",
            ),
        )

        st.session_state.last_order_status = (
            str(status)
        )

        save_state()


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
# PAPER / LIVE WARNING
# ============================================================

if PAPER_TRADING:

    st.warning(
        "PAPER TRADING MODE — "
        "No real Angel One order will be sent."
    )

else:

    st.error(
        "🔴 LIVE TRADING ENABLED — "
        "GREEN Supertrend will automatically "
        "place a real NIFTY CE BUY order."
    )


# ============================================================
# RATE LIMIT MESSAGE
# ============================================================

if rate_limit_active():

    remaining = int(
        max(
            0,
            st.session_state.rate_limited_until
            - time.time(),
        )
    )

    st.warning(
        f"Angel One API rate limit is active. "
        f"Waiting approximately {remaining} seconds."
    )


# ============================================================
# LOGIN
# ============================================================

try:

    api = login_to_angel()

    st.success(
        "Angel One: Connected"
    )

except Exception as e:

    st.error(str(e))

    st.stop()


# ============================================================
# FETCH NIFTY LTP
# ============================================================

try:

    nifty_ltp = get_nifty_ltp(
        api
    )

except Exception as e:

    st.error(str(e))
    nifty_ltp = st.session_state.nifty_ltp


# ============================================================
# FETCH CANDLES
# ============================================================

try:

    df_1m = get_nifty_1m_candles(
        api,
        CANDLE_DAYS,
    )

    df_2m = make_2min_candles(
        df_1m
    )

    if df_2m.empty:

        raise RuntimeError(
            "2-minute candle dataframe is empty."
        )

    df_st = calculate_supertrend(
        df_2m,
        ST_PERIOD,
        ST_MULTIPLIER,
    )

    if df_st.empty:

        raise RuntimeError(
            "Supertrend dataframe is empty."
        )

    st.session_state.df_2m = (
        df_st
    )

except Exception as e:

    st.error(
        f"Candle / Supertrend Error: {e}"
    )

    if (
        st.session_state.df_2m
        is not None
    ):

        df_st = (
            st.session_state.df_2m
        )

    else:

        st.stop()


# ============================================================
# LATEST COMPLETED CANDLE
# ============================================================

valid_st = df_st[
    df_st["ST_Direction"] != 0
].dropna(
    subset=["Supertrend"]
)

if valid_st.empty:

    signal = "WAIT"
    latest = None

else:

    latest = valid_st.iloc[-1]

    signal = get_signal(
        df_st
    )


st.session_state.signal = signal


# ============================================================
# DISPLAY METRICS
# ============================================================

c1, c2, c3, c4 = st.columns(4)

with c1:

    if nifty_ltp is not None:

        st.metric(
            "NIFTY Spot",
            f"{nifty_ltp:,.2f}",
        )

    else:

        st.metric(
            "NIFTY Spot",
            "-",
        )


with c2:

    st.metric(
        "Supertrend",
        "GREEN"
        if signal == "BUY CE"
        else "RED",
    )


with c3:

    st.metric(
        "Signal",
        signal,
    )


with c4:

    if latest is not None:

        candle_time = latest.name

        st.metric(
            "Last 2-Min Candle",
            candle_time.strftime(
                "%H:%M"
            ),
        )

    else:

        st.metric(
            "Last 2-Min Candle",
            "-",
        )


# ============================================================
# SUPERTREND INFORMATION
# ============================================================

if latest is not None:

    a1, a2, a3, a4 = st.columns(4)

    with a1:

        st.write(
            "**Close**"
        )

        st.write(
            f"{latest['close']:,.2f}"
        )

    with a2:

        st.write(
            "**Supertrend**"
        )

        st.write(
            f"{latest['Supertrend']:,.2f}"
        )

    with a3:

        st.write(
            "**ATR**"
        )

        st.write(
            f"{latest['ATR']:,.2f}"
        )

    with a4:

        st.write(
            "**Direction**"
        )

        st.write(
            "GREEN"
            if latest["ST_Direction"] == 1
            else "RED"
        )


# ============================================================
# ATM CE SELECTION
# ============================================================

option = None

if (
    signal == "BUY CE"
    and nifty_ltp is not None
):

    try:

        instruments = (
            load_instrument_master()
        )

        option = select_atm_ce(
            instruments,
            nifty_ltp,
        )

        st.session_state.atm_option = (
            option
        )

    except Exception as e:

        st.error(
            f"ATM CE Selection Error: {e}"
        )


# ============================================================
# OPTION INFORMATION
# ============================================================

st.subheader(
    "ATM NIFTY CE"
)

if option:

    o1, o2, o3, o4, o5 = st.columns(5)

    with o1:

        st.write(
            "**Symbol**"
        )

        st.write(
            option["symbol"]
        )

    with o2:

        st.write(
            "**Strike**"
        )

        st.write(
            f"{option['strike']:,.0f}"
        )

    with o3:

        st.write(
            "**Expiry**"
        )

        st.write(
            option["expiry"]
        )

    with o4:

        st.write(
            "**Lot Size**"
        )

        st.write(
            option["lotsize"]
        )

    with o5:

        st.write(
            "**Quantity**"
        )

        st.write(
            option["quantity"]
        )

    # --------------------------------------------------------
    # Option LTP only when BUY CE signal
    # --------------------------------------------------------

    option_ltp = get_option_ltp(
        api,
        option,
    )

    if option_ltp is not None:

        st.metric(
            "ATM CE LTP",
            f"₹{option_ltp:,.2f}",
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
            "when Supertrend becomes GREEN."
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
        latest.name,
    )


# ============================================================
# ORDER STATUS
# ============================================================

st.subheader(
    "Automatic Order Status"
)

if st.session_state.last_order_id:

    st.success(
        f"Order ID: "
        f"{st.session_state.last_order_id}"
    )

    st.write(
        f"Symbol: "
        f"{st.session_state.last_order_symbol}"
    )

    st.write(
        f"Status: "
        f"{st.session_state.last_order_status}"
    )

elif st.session_state.order_status_unknown:

    st.error(
        "Order status is UNKNOWN. "
        "No automatic retry will be made to prevent "
        "duplicate orders. Check Angel One Order Book."
    )

else:

    st.info(
        "No automatic order has been placed yet."
    )


# ============================================================
# ORDER BOOK
# ============================================================

st.subheader(
    "📋 Angel One Order Book"
)

# Only refresh periodically.
book = refresh_order_book_if_needed(
    api
)

update_saved_order_status()

book = st.session_state.get(
    "order_book",
    [],
)


# ============================================================
# ORDER BOOK DISPLAY
# ============================================================

if book:

    rows = []

    target_order_id = (
        st.session_state.last_order_id
    )

    for order in book:

        if not isinstance(
            order,
            dict,
        ):
            continue

        order_id = str(
            order.get(
                "orderid",
                "",
            )
        )

        symbol = str(
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
        )

        status = str(
            order.get(
                "orderstatus",
                order.get(
                    "status",
                    "",
                ),
            )
        )

        quantity = order.get(
            "quantity",
            "",
        )

        price = order.get(
            "price",
            order.get(
                "averageprice",
                "",
            ),
        )

        rows.append(
            {
                "NEW": (
                    "⭐ NEW"
                    if (
                        target_order_id
                        and order_id
                        == target_order_id
                    )
                    else ""
                ),
                "Order ID": order_id,
                "Symbol": symbol,
                "Transaction": transaction,
                "Quantity": quantity,
                "Price": price,
                "Status": status,
            }
        )

    if rows:

        order_df = pd.DataFrame(
            rows
        )

        st.dataframe(
            order_df,
            use_container_width=True,
            hide_index=True,
        )

else:

    st.info(
        "Order Book has no cached orders yet. "
        "It will be checked immediately after an "
        "automatic order and periodically afterward."
    )


# ============================================================
# SUPERTREND TABLE
# ============================================================

st.subheader(
    "📊 2-Minute Supertrend"
)

display_df = df_st[
    [
        "open",
        "high",
        "low",
        "close",
        "ATR",
        "Supertrend",
        "ST_Direction",
    ]
].tail(20).copy()

display_df["Signal"] = np.where(
    display_df["ST_Direction"] == 1,
    "GREEN",
    np.where(
        display_df["ST_Direction"] == -1,
        "RED",
        "WAIT",
    ),
)

display_df = display_df.reset_index()

display_df["timestamp"] = (
    display_df["timestamp"]
    .dt.strftime("%Y-%m-%d %H:%M")
)

st.dataframe(
    display_df,
    use_container_width=True,
    hide_index=True,
)


# ============================================================
# PRICE CHART
# ============================================================

st.subheader(
    "📈 NIFTY 2-Minute Chart"
)

chart_df = df_st[
    [
        "close",
        "Supertrend",
    ]
].tail(100)

st.line_chart(
    chart_df,
    use_container_width=True,
)


# ============================================================
# SYSTEM INFORMATION
# ============================================================

with st.expander(
    "System Information"
):

    st.write(
        f"**Strategy:** "
        f"2-Minute Supertrend (20, 1.5)"
    )

    st.write(
        f"**NIFTY Token:** "
        f"{NIFTY_TOKEN}"
    )

    st.write(
        f"**Refresh:** "
        f"{REFRESH_SECONDS} seconds"
    )

    st.write(
        f"**Order Book Refresh:** "
        f"{ORDERBOOK_REFRESH_SECONDS} seconds"
    )

    st.write(
        f"**Lots:** "
        f"{LOTS}"
    )

    st.write(
        f"**Order Type:** "
        f"{ORDER_TYPE}"
    )

    st.write(
        f"**Product:** "
        f"{PRODUCT_TYPE}"
    )

    st.write(
        f"**Paper Trading:** "
        f"{PAPER_TRADING}"
    )

    st.write(
        f"**Instrument File:** "
        f"{INSTRUMENT_FILE}"
    )

    st.write(
        f"**Instrument File Exists:** "
        f"{INSTRUMENT_FILE.exists()}"
    )


# ============================================================
# LAST ERROR
# ============================================================

if st.session_state.last_error:

    st.warning(
        st.session_state.last_error
    )


# ============================================================
# AUTOMATIC REFRESH
#
# IMPORTANT:
# 60 seconds instead of 20 seconds.
#
# This greatly reduces Angel One API calls.
# ============================================================

time.sleep(
    REFRESH_SECONDS
)

st.rerun()
