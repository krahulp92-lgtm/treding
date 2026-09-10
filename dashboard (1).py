
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
#
# NO GREEN FLIP REQUIRED
#
# GREEN
#   ↓
# Select nearest NIFTY expiry
#   ↓
# Select ATM NIFTY CE
#   ↓
# Automatically BUY 1 LOT
#   ↓
# Verify Order Book
#
# IMPORTANT:
# PAPER_TRADING=true  -> NO REAL ORDER
# PAPER_TRADING=false -> REAL ANGEL ONE ORDER
#
# Required local file:
# OpenAPIScripMaster.json
#
# Put it in the SAME folder as dashboard.py
# ============================================================

import os
import json
import time
from pathlib import Path
from datetime import datetime, timedelta, time as dt_time
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
    page_title="NIFTY Automatic CE",
    page_icon="📈",
    layout="wide",
)


# ============================================================
# CONFIG
# ============================================================

IST = ZoneInfo("Asia/Kolkata")

NIFTY_TOKEN = "99926000"
NIFTY_SYMBOL = "NIFTY"

SUPERTREND_PERIOD = 20
SUPERTREND_MULTIPLIER = 1.5

LOTS = 1

REFRESH_SECONDS = 20

CANDLE_DAYS = 3

ORDER_TYPE = "MARKET"
PRODUCT_TYPE = "INTRADAY"
DURATION = "DAY"

# ------------------------------------------------------------
# SAFETY
# ------------------------------------------------------------

# DEFAULT = PAPER
#
# PAPER_TRADING=true
#       No broker order is sent.
#
# PAPER_TRADING=false
#       Real order can be sent.
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
# FILE PATHS
# ============================================================

BASE_DIR = Path(__file__).resolve().parent

INSTRUMENT_FILE = (
 "https://margincalculator.angelone.in/OpenAPI_File/files/OpenAPIScripMaster.json?utm_source=chatgpt.com"
)

STATE_FILE = (
    BASE_DIR / "nifty_2min_ce_state.json"
)


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
# SESSION DEFAULTS
# ============================================================

DEFAULTS = {
    "api": None,
    "running": True,
    "login_status": False,
    "last_error": "",
    "nifty_ltp": None,
    "signal": "WAIT",
    "supertrend": None,
    "st_direction": None,
    "last_candle_time": None,
    "atm_symbol": "",
    "atm_token": "",
    "atm_strike": None,
    "atm_expiry": "",
    "atm_lot_size": 0,
    "atm_ltp": None,
    "last_order_id": "",
    "last_order_symbol": "",
    "order_status": "",
    "order_status_unknown": False,
}

for key, value in DEFAULTS.items():

    if key not in st.session_state:
        st.session_state[key] = value


# ============================================================
# UTILITY
# ============================================================

def now_ist():
    return datetime.now(IST)


def is_market_open():

    now = now_ist()

    if now.weekday() >= 5:
        return False

    market_start = dt_time(
        9,
        15
    )

    market_end = dt_time(
        15,
        30
    )

    return (
        market_start
        <= now.time()
        <= market_end
    )


def safe_float(value, default=None):

    try:

        if value is None:
            return default

        return float(value)

    except Exception:

        return default


# ============================================================
# TOTP
# ============================================================

def get_totp():

    secret = ANGEL_TOTP_SECRET.strip()

    if not secret:
        raise RuntimeError(
            "ANGEL_TOTP_SECRET is empty."
        )

    # --------------------------------------------------------
    # If an otpauth URI was supplied, extract secret.
    # --------------------------------------------------------

    if secret.lower().startswith(
        "otpauth://"
    ):

        try:

            from urllib.parse import (
                urlparse,
                parse_qs
            )

            parsed = urlparse(secret)

            params = parse_qs(
                parsed.query
            )

            secret = params.get(
                "secret",
                [""]
            )[0]

        except Exception as exc:

            raise RuntimeError(
                "Could not read TOTP secret: "
                + str(exc)
            )

    secret = (
        secret
        .replace(" ", "")
        .replace("-", "")
        .upper()
    )

    # --------------------------------------------------------
    # Do not accidentally use the 6-digit OTP.
    # --------------------------------------------------------

    if (
        len(secret) == 6
        and secret.isdigit()
    ):

        raise RuntimeError(
            "ANGEL_TOTP_SECRET contains a "
            "6-digit OTP.\n\n"
            "Use the actual Base32 TOTP secret, "
            "not the current 6-digit code."
        )

    try:

        return pyotp.TOTP(secret).now()

    except Exception as exc:

        raise RuntimeError(
            "Invalid TOTP secret: "
            + str(exc)
        )


# ============================================================
# ANGEL LOGIN
# ============================================================

def login_angel():

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

    totp = get_totp()

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
            "Angel One login returned empty response."
        )

    if not response.get("status"):

        raise RuntimeError(
            "Angel One login failed:\n"
            + str(response)
        )

    if not response.get("data"):

        raise RuntimeError(
            "Angel One login returned no session data."
        )

    if not response["data"].get(
        "jwtToken"
    ):

        raise RuntimeError(
            "Angel One login returned no jwtToken."
        )

    return api


# ============================================================
# LOCAL INSTRUMENT MASTER
# ============================================================

def load_instrument_master():

    if not INSTRUMENT_FILE.exists():

        raise RuntimeError(
            "OpenAPIScripMaster.json NOT FOUND.\n\n"
            "Expected location:\n"
            f"{INSTRUMENT_FILE}\n\n"
            "Download the actual Angel One "
            "instrument master and place it "
            "beside dashboard.py."
        )

    try:

        with open(
            INSTRUMENT_FILE,
            "r",
            encoding="utf-8"
        ) as f:

            data = json.load(f)

    except json.JSONDecodeError as exc:

        raise RuntimeError(
            "OpenAPIScripMaster.json contains "
            "invalid JSON:\n"
            + str(exc)
        )

    except Exception as exc:

        raise RuntimeError(
            "Cannot read OpenAPIScripMaster.json:\n"
            + str(exc)
        )

    if not isinstance(data, list):

        raise RuntimeError(
            "OpenAPIScripMaster.json must contain "
            "a JSON list."
        )

    if not data:

        raise RuntimeError(
            "OpenAPIScripMaster.json is empty."
        )

    return data


# ============================================================
# NIFTY LTP
# ============================================================

def get_nifty_ltp(api):

    response = api.ltpData(
        "NSE",
        NIFTY_SYMBOL,
        NIFTY_TOKEN
    )

    if not response:

        raise RuntimeError(
            "NIFTY LTP returned empty response."
        )

    if not response.get("status"):

        raise RuntimeError(
            "NIFTY LTP FAILED | "
            f"message={response.get('message')} | "
            f"errorcode={response.get('errorcode')}"
        )

    data = response.get("data")

    if not data:

        raise RuntimeError(
            "NIFTY LTP response has no data."
        )

    ltp = safe_float(
        data.get("ltp")
    )

    if ltp is None:

        raise RuntimeError(
            "NIFTY LTP is invalid:\n"
            + str(data)
        )

    return ltp


# ============================================================
# ANGEL CANDLE DATA
# ============================================================

def get_nifty_1m_candles(api):

    now = now_ist()

    from_dt = (
        now
        - timedelta(
            days=CANDLE_DAYS
        )
    )

    to_dt = now

    params = {
        "exchange": "NSE",
        "symboltoken": NIFTY_TOKEN,
        "interval": "ONE_MINUTE",
        "fromdate": from_dt.strftime(
            "%Y-%m-%d %H:%M"
        ),
        "todate": to_dt.strftime(
            "%Y-%m-%d %H:%M"
        ),
    }

    response = api.getCandleData(
        params
    )

    if not response:

        raise RuntimeError(
            "Candle API returned empty response."
        )

    if not response.get("status"):

        raise RuntimeError(
            "Candle API FAILED:\n"
            + str(response)
        )

    rows = response.get("data")

    if not rows:

        raise RuntimeError(
            "Candle API returned no candles."
        )

    df = pd.DataFrame(
        rows
    )

    if df.empty:

        raise RuntimeError(
            "Candle dataframe is empty."
        )

    # Angel format normally:
    #
    # datetime, open, high, low, close, volume
    #
    if df.shape[1] < 5:

        raise RuntimeError(
            "Unexpected candle response."
        )

    df = df.iloc[:, :6].copy()

    columns = [
        "datetime",
        "open",
        "high",
        "low",
        "close",
        "volume",
    ]

    df.columns = columns[:df.shape[1]]

    df["datetime"] = pd.to_datetime(
        df["datetime"],
        errors="coerce"
    )

    # --------------------------------------------------------
    # Convert to IST
    # --------------------------------------------------------

    if df["datetime"].dt.tz is None:

        df["datetime"] = (
            df["datetime"]
            .dt.tz_localize(
                IST
            )
        )

    else:

        df["datetime"] = (
            df["datetime"]
            .dt.tz_convert(
                IST
            )
        )

    for column in [
        "open",
        "high",
        "low",
        "close",
        "volume",
    ]:

        if column in df.columns:

            df[column] = pd.to_numeric(
                df[column],
                errors="coerce"
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
        subset=["datetime"],
        keep="last"
    )

    # --------------------------------------------------------
    # Remove current forming 1-minute candle
    # --------------------------------------------------------

    current_minute = now.replace(
        second=0,
        microsecond=0
    )

    df = df[
        df["datetime"]
        < current_minute
    ]

    # --------------------------------------------------------
    # NSE market hours
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
    ]

    if df.empty:

        raise RuntimeError(
            "No completed NSE 1-minute candles."
        )

    return df


# ============================================================
# RESAMPLE 1 MINUTE -> 2 MINUTE
# ============================================================

def resample_to_2min(df):

    temp = df.copy()

    temp = temp.set_index(
        "datetime"
    )

    result = (
        temp[
            [
                "open",
                "high",
                "low",
                "close",
                "volume",
            ]
        ]
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
    )

    result = result.dropna(
        subset=[
            "open",
            "high",
            "low",
            "close",
        ]
    )

    # --------------------------------------------------------
    # Only completed 2-minute candles
    # --------------------------------------------------------

    current = now_ist()

    result = result[
        result.index
        <= current
    ]

    # --------------------------------------------------------
    # Keep NSE session
    # --------------------------------------------------------

    result = result[
        (
            result.index.time
            >= dt_time(9, 17)
        )
        &
        (
            result.index.time
            <= dt_time(15, 30)
        )
    ]

    return result


# ============================================================
# SUPERTREND
# ============================================================

def calculate_supertrend(
    df,
    period=20,
    multiplier=1.5
):

    data = df.copy()

    high = data["high"]
    low = data["low"]
    close = data["close"]

    # --------------------------------------------------------
    # True Range
    # --------------------------------------------------------

    previous_close = close.shift(1)

    tr1 = high - low

    tr2 = (
        high
        - previous_close
    ).abs()

    tr3 = (
        low
        - previous_close
    ).abs()

    true_range = pd.concat(
        [
            tr1,
            tr2,
            tr3,
        ],
        axis=1
    ).max(
        axis=1
    )

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
        dtype=float,
    )

    final_lower = pd.Series(
        np.nan,
        index=data.index,
        dtype=float,
    )

    direction = pd.Series(
        np.nan,
        index=data.index,
        dtype=float,
    )

    supertrend = pd.Series(
        np.nan,
        index=data.index,
        dtype=float,
    )

    # --------------------------------------------------------
    # Calculate bands and direction
    # --------------------------------------------------------

    for i in range(len(data)):

        if pd.isna(atr.iloc[i]):

            continue

        if i == 0:

            final_upper.iloc[i] = (
                basic_upper.iloc[i]
            )

            final_lower.iloc[i] = (
                basic_lower.iloc[i]
            )

            continue

        previous_final_upper = (
            final_upper.iloc[i - 1]
        )

        previous_final_lower = (
            final_lower.iloc[i - 1]
        )

        previous_close_value = (
            close.iloc[i - 1]
        )

        # ----------------------------------------------------
        # Final upper band
        # ----------------------------------------------------

        if (
            pd.isna(previous_final_upper)
            or
            basic_upper.iloc[i]
            < previous_final_upper
            or
            previous_close_value
            > previous_final_upper
        ):

            final_upper.iloc[i] = (
                basic_upper.iloc[i]
            )

        else:

            final_upper.iloc[i] = (
                previous_final_upper
            )

        # ----------------------------------------------------
        # Final lower band
        # ----------------------------------------------------

        if (
            pd.isna(previous_final_lower)
            or
            basic_lower.iloc[i]
            > previous_final_lower
            or
            previous_close_value
            < previous_final_lower
        ):

            final_lower.iloc[i] = (
                basic_lower.iloc[i]
            )

        else:

            final_lower.iloc[i] = (
                previous_final_lower
            )

        # ----------------------------------------------------
        # Direction
        # ----------------------------------------------------

        if i == 0 or pd.isna(
            direction.iloc[i - 1]
        ):

            direction.iloc[i] = 1

        else:

            previous_direction = (
                direction.iloc[i - 1]
            )

            if (
                previous_direction == -1
                and close.iloc[i]
                > final_upper.iloc[i]
            ):

                direction.iloc[i] = 1

            elif (
                previous_direction == 1
                and close.iloc[i]
                < final_lower.iloc[i]
            ):

                direction.iloc[i] = -1

            else:

                direction.iloc[i] = (
                    previous_direction
                )

        # ----------------------------------------------------
        # Supertrend
        # ----------------------------------------------------

        if direction.iloc[i] == 1:

            supertrend.iloc[i] = (
                final_lower.iloc[i]
            )

        else:

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
        (direction == 1)
        &
        (direction.shift(1) == -1)
    )

    data["ST_Flip_Red"] = (
        (direction == -1)
        &
        (direction.shift(1) == 1)
    )

    return data


# ============================================================
# ATM CE SELECTION
# ============================================================

def select_atm_nifty_ce(
    spot
):

    records = load_instrument_master()

    df = pd.DataFrame(
        records
    )

    if df.empty:

        raise RuntimeError(
            "Instrument master is empty."
        )

    # --------------------------------------------------------
    # Normalize column names
    # --------------------------------------------------------

    df.columns = [
        str(c).strip().lower()
        for c in df.columns
    ]

    required = [
        "token",
        "symbol",
        "expiry",
        "strike",
        "lotsize",
        "exch_seg",
    ]

    missing = [
        c
        for c in required
        if c not in df.columns
    ]

    if missing:

        raise RuntimeError(
            "Instrument master is missing columns:\n"
            + ", ".join(missing)
        )

    # --------------------------------------------------------
    # Exchange
    # --------------------------------------------------------

    df["exch_seg"] = (
        df["exch_seg"]
        .astype(str)
        .str.upper()
        .str.strip()
    )

    df = df[
        df["exch_seg"] == "NFO"
    ]

    # --------------------------------------------------------
    # NIFTY
    # --------------------------------------------------------

    df["symbol"] = (
        df["symbol"]
        .astype(str)
        .str.upper()
        .str.strip()
    )

    if "name" in df.columns:

        df["name"] = (
            df["name"]
            .astype(str)
            .str.upper()
            .str.strip()
        )

        nifty_mask = (
            (df["name"] == "NIFTY")
            |
            (
                df["symbol"]
                .str.startswith("NIFTY")
            )
        )

        df = df[
            nifty_mask
        ]

    else:

        df = df[
            df["symbol"]
            .str.startswith("NIFTY")
        ]

    # --------------------------------------------------------
    # CE only
    # --------------------------------------------------------

    df = df[
        df["symbol"]
        .str.endswith("CE")
    ]

    # --------------------------------------------------------
    # Option type
    # --------------------------------------------------------

    if "instrumenttype" in df.columns:

        instrument_type = (
            df["instrumenttype"]
            .astype(str)
            .str.upper()
            .str.strip()
        )

        option_mask = (
            instrument_type
            == "OPTIDX"
        )

        if option_mask.any():

            df = df[
                option_mask
            ]

    # --------------------------------------------------------
    # Expiry
    # --------------------------------------------------------

    df["expiry_dt"] = pd.to_datetime(
        df["expiry"],
        errors="coerce",
        dayfirst=True
    )

    # Fallback for formats such as:
    # 30SEP2026

    missing_expiry = (
        df["expiry_dt"].isna()
    )

    if missing_expiry.any():

        df.loc[
            missing_expiry,
            "expiry_dt"
        ] = pd.to_datetime(
            df.loc[
                missing_expiry,
                "expiry"
            ].astype(str),
            format="%d%b%Y",
            errors="coerce"
        )

    today = pd.Timestamp(
        now_ist().date()
    )

    df = df[
        df["expiry_dt"]
        >= today
    ]

    if df.empty:

        raise RuntimeError(
            "No future NIFTY CE expiry found."
        )

    # --------------------------------------------------------
    # Nearest expiry
    # --------------------------------------------------------

    nearest_expiry = (
        df["expiry_dt"]
        .min()
    )

    df = df[
        df["expiry_dt"]
        == nearest_expiry
    ]

    # --------------------------------------------------------
    # Strike
    # --------------------------------------------------------

    df["strike_raw"] = pd.to_numeric(
        df["strike"],
        errors="coerce"
    )

    # Angel instrument master can store
    # strikes multiplied by 100.

    df["strike_actual"] = np.where(
        df["strike_raw"] > 100000,
        df["strike_raw"] / 100,
        df["strike_raw"]
    )

    df = df.dropna(
        subset=[
            "strike_actual"
        ]
    )

    if df.empty:

        raise RuntimeError(
            "No valid NIFTY CE strikes found."
        )

    # --------------------------------------------------------
    # ATM = nearest available strike
    # --------------------------------------------------------

    df["distance"] = (
        df["strike_actual"]
        - float(spot)
    ).abs()

    selected = (
        df.sort_values(
            [
                "distance",
                "strike_actual",
            ]
        )
        .iloc[0]
    )

    token = str(
        selected["token"]
    ).strip()

    symbol = str(
        selected["symbol"]
    ).strip()

    lot_size = int(
        safe_float(
            selected["lotsize"],
            0
        )
    )

    strike = float(
        selected["strike_actual"]
    )

    expiry = (
        selected["expiry_dt"]
        .strftime("%d-%b-%Y")
    )

    if not token:

        raise RuntimeError(
            "Selected CE has empty token."
        )

    if not symbol:

        raise RuntimeError(
            "Selected CE has empty symbol."
        )

    if lot_size <= 0:

        raise RuntimeError(
            f"Invalid lot size for {symbol}: "
            f"{lot_size}"
        )

    return {
        "token": token,
        "symbol": symbol,
        "strike": strike,
        "expiry": expiry,
        "lot_size": lot_size,
        "quantity": lot_size * LOTS,
    }


# ============================================================
# OPTION LTP
# ============================================================

def get_option_ltp(
    api,
    symbol,
    token
):

    try:

        response = api.ltpData(
            "NFO",
            symbol,
            str(token)
        )

        if not response:
            return None

        if not response.get("status"):
            return None

        data = response.get(
            "data"
        )

        if not data:
            return None

        return safe_float(
            data.get("ltp")
        )

    except Exception:

        return None


# ============================================================
# LOAD STATE
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

        for key in [
            "last_order_id",
            "last_order_symbol",
            "order_status",
            "order_status_unknown",
        ]:

            if key in data:

                st.session_state[
                    key
                ] = data[key]

    except Exception:

        pass


def save_state():

    data = {
        "last_order_id":
            st.session_state.last_order_id,

        "last_order_symbol":
            st.session_state.last_order_symbol,

        "order_status":
            st.session_state.order_status,

        "order_status_unknown":
            st.session_state.order_status_unknown,

        "updated":
            now_ist().isoformat(),
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

        data = response.get(
            "data"
        )

        if not data:

            return []

        return data

    except Exception:

        return []


# ============================================================
# FIND ORDER IN ORDER BOOK
# ============================================================

def find_order_for_symbol(
    orders,
    symbol
):

    if not orders:
        return None

    symbol = str(
        symbol
    ).upper().strip()

    for order in orders:

        order_symbol = str(
            order.get(
                "tradingsymbol",
                ""
            )
        ).upper().strip()

        transaction = str(
            order.get(
                "transactiontype",
                ""
            )
        ).upper().strip()

        exchange = str(
            order.get(
                "exchange",
                ""
            )
        ).upper().strip()

        if (
            order_symbol == symbol
            and transaction == "BUY"
            and exchange == "NFO"
        ):

            return order

    return None


# ============================================================
# PLACE PAPER ORDER
# ============================================================

def place_paper_order(
    option
):

    order_id = (
        "PAPER-"
        + now_ist().strftime(
            "%Y%m%d%H%M%S"
        )
    )

    st.session_state.last_order_id = (
        order_id
    )

    st.session_state.last_order_symbol = (
        option["symbol"]
    )

    st.session_state.order_status = (
        "PAPER ORDER"
    )

    st.session_state.order_status_unknown = (
        False
    )

    save_state()

    return order_id


# ============================================================
# PLACE REAL ANGEL ORDER
# ============================================================

def place_real_order(
    api,
    option
):

    params = {
        "variety": "NORMAL",

        "tradingsymbol":
            option["symbol"],

        "symboltoken":
            str(option["token"]),

        "transactiontype": "BUY",

        "exchange": "NFO",

        "ordertype": ORDER_TYPE,

        "producttype": PRODUCT_TYPE,

        "duration": DURATION,

        "price": "0",

        "squareoff": "0",

        "stoploss": "0",

        "quantity": str(
            option["quantity"]
        ),
    }

    # --------------------------------------------------------
    # IMPORTANT:
    # Mark order attempt BEFORE sending request.
    #
    # If Angel returns empty response, we do NOT blindly
    # submit another order.
    # --------------------------------------------------------

    st.session_state.last_order_symbol = (
        option["symbol"]
    )

    st.session_state.order_status = (
        "ORDER SENT - VERIFYING"
    )

    st.session_state.order_status_unknown = (
        True
    )

    save_state()

    # --------------------------------------------------------
    # Try Full Response API when available.
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

        # ----------------------------------------------------
        # The request itself may have reached Angel.
        # Therefore do NOT automatically retry.
        # ----------------------------------------------------

        st.session_state.order_status = (
            "ORDER RESPONSE ERROR - CHECK ORDER BOOK"
        )

        st.session_state.order_status_unknown = (
            True
        )

        save_state()

        return None, str(exc)

    # --------------------------------------------------------
    # Empty response
    # --------------------------------------------------------

    if not response:

        st.session_state.order_status = (
            "EMPTY RESPONSE - CHECKING ORDER BOOK"
        )

        save_state()

        return None, "EMPTY RESPONSE"

    # --------------------------------------------------------
    # Dictionary response
    # --------------------------------------------------------

    if isinstance(
        response,
        dict
    ):

        status = response.get(
            "status"
        )

        message = response.get(
            "message",
            ""
        )

        errorcode = response.get(
            "errorcode",
            ""
        )

        data = response.get(
            "data"
        )

        order_id = None

        if isinstance(
            data,
            dict
        ):

            order_id = (
                data.get("orderid")
                or data.get("orderId")
            )

        elif isinstance(
            data,
            str
        ):

            order_id = data

        if order_id:

            st.session_state.last_order_id = (
                str(order_id)
            )

            st.session_state.order_status = (
                "ORDER ACCEPTED"
            )

            st.session_state.order_status_unknown = (
                False
            )

            save_state()

            return (
                str(order_id),
                None
            )

        if status is False:

            st.session_state.order_status = (
                "BROKER REJECTED ORDER"
            )

            st.session_state.order_status_unknown = (
                False
            )

            save_state()

            return (
                None,
                f"{message} | {errorcode}"
            )

    # --------------------------------------------------------
    # String response
    # --------------------------------------------------------

    if isinstance(
        response,
        str
    ):

        if response.strip():

            st.session_state.last_order_id = (
                response.strip()
            )

            st.session_state.order_status = (
                "ORDER ACCEPTED"
            )

            st.session_state.order_status_unknown = (
                False
            )

            save_state()

            return (
                response.strip(),
                None
            )

    # --------------------------------------------------------
    # Unknown response
    # --------------------------------------------------------

    st.session_state.order_status = (
        "UNKNOWN RESPONSE - CHECK ORDER BOOK"
    )

    st.session_state.order_status_unknown = (
        True
    )

    save_state()

    return (
        None,
        str(response)
    )


# ============================================================
# VERIFY ORDER BOOK AFTER UNCERTAIN RESPONSE
# ============================================================

def verify_order_book(
    api,
    symbol,
    checks=3,
    delay=2
):

    for _ in range(checks):

        orders = get_order_book(
            api
        )

        found = find_order_for_symbol(
            orders,
            symbol
        )

        if found:

            order_id = found.get(
                "orderid"
            )

            status = found.get(
                "orderstatus",
                "FOUND"
            )

            st.session_state.last_order_id = (
                str(order_id or "")
            )

            st.session_state.order_status = (
                str(status)
            )

            st.session_state.order_status_unknown = (
                False
            )

            save_state()

            return found

        time.sleep(delay)

    return None


# ============================================================
# AUTOMATIC BUY CE
# ============================================================

def automatic_buy_ce(
    api,
    option
):

    # --------------------------------------------------------
    # Duplicate protection
    # --------------------------------------------------------

    if (
        st.session_state.last_order_id
        or st.session_state.order_status_unknown
    ):

        return (
            False,
            "Order already attempted. "
            "Automatic retry disabled."
        )

    # --------------------------------------------------------
    # Market check
    # --------------------------------------------------------

    if not is_market_open():

        return (
            False,
            "Market is closed."
        )

    # --------------------------------------------------------
    # PAPER MODE
    # --------------------------------------------------------

    if PAPER_TRADING:

        order_id = place_paper_order(
            option
        )

        return (
            True,
            order_id
        )

    # --------------------------------------------------------
    # REAL ORDER
    # --------------------------------------------------------

    order_id, error = place_real_order(
        api,
        option
    )

    if order_id:

        return (
            True,
            order_id
        )

    # --------------------------------------------------------
    # Empty/uncertain response:
    # check Order Book before considering anything else.
    # --------------------------------------------------------

    found = verify_order_book(
        api,
        option["symbol"]
    )

    if found:

        return (
            True,
            str(
                found.get(
                    "orderid",
                    ""
                )
            )
        )

    return (
        False,
        "Order status UNKNOWN. "
        "Check Angel One Order Book. "
        "No automatic retry was performed."
    )


# ============================================================
# CHART
# ============================================================

def prepare_chart_data(
    df
):

    chart = df[
        [
            "open",
            "high",
            "low",
            "close",
            "Supertrend",
        ]
    ].copy()

    chart = chart.rename(
        columns={
            "open": "Open",
            "high": "High",
            "low": "Low",
            "close": "Close",
            "Supertrend": "Supertrend",
        }
    )

    return chart


# ============================================================
# LOAD PERSISTENT STATE
# ============================================================

load_state()


# ============================================================
# HEADER
# ============================================================

st.title(
    "📈 NIFTY Automatic BUY CE"
)

st.caption(
    "2-Minute Supertrend (20, 1.5) | "
    "GREEN = BUY CE | RED = WAIT"
)


# ============================================================
# SAFETY STATUS
# ============================================================

if PAPER_TRADING:

    st.warning(
        "🟡 PAPER TRADING MODE — "
        "NO REAL ORDER WILL BE SENT"
    )

else:

    st.error(
        "🔴 LIVE TRADING MODE — "
        "REAL ANGEL ONE ORDERS CAN BE PLACED"
    )


# ============================================================
# LOGIN
# ============================================================

try:

    if (
        st.session_state.api is None
        or not st.session_state.login_status
    ):

        with st.spinner(
            "Logging into Angel One..."
        ):

            api = login_angel()

        st.session_state.api = api

        st.session_state.login_status = (
            True
        )

        st.session_state.last_error = ""

except Exception as exc:

    st.session_state.login_status = (
        False
    )

    st.session_state.last_error = str(
        exc
    )

    st.error(
        "Angel One Login Error"
    )

    st.code(
        str(exc)
    )

    st.stop()


api = st.session_state.api


# ============================================================
# TOP STATUS
# ============================================================

col1, col2, col3, col4 = st.columns(4)

with col1:

    st.metric(
        "Login",
        "CONNECTED"
    )

with col2:

    st.metric(
        "Market",
        "OPEN"
        if is_market_open()
        else "CLOSED"
    )

with col3:

    st.metric(
        "Mode",
        "PAPER"
        if PAPER_TRADING
        else "LIVE"
    )

with col4:

    st.metric(
        "Strategy",
        "2M ST 20,1.5"
    )


# ============================================================
# GET NIFTY LTP
# ============================================================

try:

    nifty_ltp = get_nifty_ltp(
        api
    )

    st.session_state.nifty_ltp = (
        nifty_ltp
    )

except Exception as exc:

    st.session_state.last_error = str(
        exc
    )

    st.error(
        "NIFTY LTP ERROR"
    )

    st.code(
        str(exc)
    )

    st.stop()


# ============================================================
# GET CANDLES
# ============================================================

try:

    df_1m = get_nifty_1m_candles(
        api
    )

    df_2m = resample_to_2min(
        df_1m
    )

    if len(df_2m) < (
        SUPERTREND_PERIOD + 5
    ):

        raise RuntimeError(
            "Not enough 2-minute candles "
            "for Supertrend calculation."
        )

    df_st = calculate_supertrend(
        df_2m,
        SUPERTREND_PERIOD,
        SUPERTREND_MULTIPLIER
    )

except Exception as exc:

    st.error(
        "Candle / Supertrend Error"
    )

    st.code(
        str(exc)
    )

    st.stop()


# ============================================================
# LATEST CLOSED CANDLE
# ============================================================

latest = df_st.iloc[-1]

latest_time = df_st.index[-1]

direction = latest[
    "ST_Direction"
]

supertrend_value = safe_float(
    latest["Supertrend"]
)

close_value = safe_float(
    latest["close"]
)

st.session_state.last_candle_time = (
    str(latest_time)
)

st.session_state.supertrend = (
    supertrend_value
)

st.session_state.st_direction = (
    direction
)


# ============================================================
# SIGNAL
# ============================================================

if direction == 1:

    signal = "BUY CE"

else:

    signal = "WAIT"


st.session_state.signal = signal


# ============================================================
# SIGNAL DISPLAY
# ============================================================

st.subheader(
    "Current Signal"
)

signal_col1, signal_col2, signal_col3, signal_col4 = (
    st.columns(4)
)

with signal_col1:

    st.metric(
        "NIFTY",
        f"{nifty_ltp:,.2f}"
    )

with signal_col2:

    st.metric(
        "2M Close",
        f"{close_value:,.2f}"
    )

with signal_col3:

    st.metric(
        "Supertrend",
        (
            f"{supertrend_value:,.2f}"
            if supertrend_value is not None
            else "-"
        )
    )

with signal_col4:

    st.metric(
        "Signal",
        signal
    )


st.write(
    f"Last closed 2-minute candle: "
    f"`{latest_time}`"
)


# ============================================================
# ATM CE
# ============================================================

option = None

if signal == "BUY CE":

    try:

        option = select_atm_nifty_ce(
            nifty_ltp
        )

        st.session_state.atm_symbol = (
            option["symbol"]
        )

        st.session_state.atm_token = (
            option["token"]
        )

        st.session_state.atm_strike = (
            option["strike"]
        )

        st.session_state.atm_expiry = (
            option["expiry"]
        )

        st.session_state.atm_lot_size = (
            option["lot_size"]
        )

    except Exception as exc:

        st.error(
            "ATM CE selection failed."
        )

        st.code(
            str(exc)
        )

        st.stop()


# ============================================================
# OPTION INFORMATION
# ============================================================

if option:

    option_ltp = get_option_ltp(
        api,
        option["symbol"],
        option["token"]
    )

    st.session_state.atm_ltp = (
        option_ltp
    )

    st.subheader(
        "Selected ATM NIFTY CE"
    )

    c1, c2, c3, c4, c5 = st.columns(5)

    with c1:

        st.metric(
            "Symbol",
            option["symbol"]
        )

    with c2:

        st.metric(
            "Strike",
            f"{option['strike']:,.0f}"
        )

    with c3:

        st.metric(
            "Expiry",
            option["expiry"]
        )

    with c4:

        st.metric(
            "Lot Size",
            option["lot_size"]
        )

    with c5:

        st.metric(
            "CE LTP",
            (
                f"{option_ltp:,.2f}"
                if option_ltp is not None
                else "-"
            )
        )


# ============================================================
# AUTOMATIC ORDER ENGINE
# ============================================================

if signal == "BUY CE" and option:

    # --------------------------------------------------------
    # IMPORTANT:
    # This executes automatically.
    #
    # There is NO manual BUY button.
    # --------------------------------------------------------

    if (
        not st.session_state.last_order_id
        and not st.session_state.order_status_unknown
    ):

        with st.spinner(
            "BUY CE signal detected — "
            "processing automatic order..."
        ):

            success, result = (
                automatic_buy_ce(
                    api,
                    option
                )
            )

        if success:

            st.success(
                "Automatic BUY CE processed."
            )

            st.write(
                f"Order ID: `{result}`"
            )

        else:

            st.warning(
                str(result)
            )

    else:

        st.info(
            "Automatic order already attempted "
            "or order status is uncertain. "
            "No duplicate order will be sent."
        )


# ============================================================
# ORDER STATUS
# ============================================================

st.subheader(
    "Order Status"
)

status_col1, status_col2 = st.columns(2)

with status_col1:

    st.metric(
        "Last Order ID",
        (
            st.session_state.last_order_id
            or "-"
        )
    )

with status_col2:

    st.metric(
        "Status",
        (
            st.session_state.order_status
            or "NONE"
        )
    )


if st.session_state.order_status_unknown:

    st.warning(
        "⚠️ Order status is UNKNOWN.\n\n"
        "The broker response was empty or uncertain. "
        "The program will NOT automatically retry because "
        "a retry could create a duplicate order.\n\n"
        "Check the Angel One Order Book."
    )


# ============================================================
# ORDER BOOK
# ============================================================

st.subheader(
    "📋 Angel One Order Book"
)

orders = get_order_book(
    api
)

if orders:

    order_df = pd.DataFrame(
        orders
    )

    # --------------------------------------------------------
    # Highlight most recent selected CE order
    # --------------------------------------------------------

    if (
        st.session_state.last_order_symbol
        and "tradingsymbol"
        in order_df.columns
    ):

        selected_symbol = (
            st.session_state.last_order_symbol
        )

        def highlight_selected(
            row
        ):

            if str(
                row.get(
                    "tradingsymbol",
                    ""
                )
            ).upper() == str(
                selected_symbol
            ).upper():

                return [
                    "background-color: #fff3cd"
                ] * len(row)

            return [""] * len(row)

        st.dataframe(
            order_df.style.apply(
                highlight_selected,
                axis=1
            ),
            use_container_width=True
        )

    else:

        st.dataframe(
            order_df,
            use_container_width=True
        )

else:

    st.info(
        "No orders found in Angel One Order Book."
    )


# ============================================================
# RECENT 2-MINUTE CANDLES
# ============================================================

st.subheader(
    "Recent 2-Minute Supertrend"
)

recent = df_st[
    [
        "open",
        "high",
        "low",
        "close",
        "ATR",
        "Supertrend",
        "ST_Direction",
        "ST_Green",
        "ST_Red",
    ]
].tail(20).copy()

recent["Signal"] = np.where(
    recent["ST_Green"],
    "BUY CE",
    "WAIT"
)

st.dataframe(
    recent,
    use_container_width=True
)


# ============================================================
# PRICE / SUPERTREND CHART
# ============================================================

st.subheader(
    "NIFTY 2-Minute Chart"
)

chart_data = prepare_chart_data(
    df_st.tail(100)
)

st.line_chart(
    chart_data[
        [
            "Close",
            "Supertrend",
        ]
    ],
    use_container_width=True
)


# ============================================================
# STRATEGY RULES
# ============================================================

with st.expander(
    "Strategy Rules"
):

    st.write(
        """
        **Instrument:** NIFTY 50

        **Candle:** 2-minute

        **Supertrend:** Period 20, Multiplier 1.5

        **GREEN:** BUY CE

        **RED:** WAIT

        **Green flip is NOT required.**

        When the latest completed 2-minute candle is GREEN,
        the program selects the nearest available NIFTY
        expiry and nearest strike to the NIFTY spot price.

        That option is the ATM NIFTY CE.

        The program automatically attempts to BUY one lot.

        There are no manual BUY/SELL buttons.

        Duplicate automatic orders are blocked after an
        order has been attempted.
        """
    )


# ============================================================
# FILE CHECK
# ============================================================

with st.expander(
    "System Information"
):

    st.write(
        f"dashboard.py: `{BASE_DIR}`"
    )

    st.write(
        f"Instrument master: `{INSTRUMENT_FILE}`"
    )

    st.write(
        "Instrument master exists: "
        f"`{INSTRUMENT_FILE.exists()}`"
    )

    st.write(
        f"PAPER_TRADING: `{PAPER_TRADING}`"
    )

    st.write(
        f"LOTS: `{LOTS}`"
    )

    st.write(
        f"Refresh: `{REFRESH_SECONDS}` seconds"
    )


# ============================================================
# REFRESH
# ============================================================

time.sleep(
    REFRESH_SECONDS
)

st.rerun()
