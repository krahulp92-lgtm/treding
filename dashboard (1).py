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
# PAPER_TRADING=true  -> NO REAL ORDER
# PAPER_TRADING=false -> REAL ANGEL ONE ORDER
#
# IMPORTANT:
# Angel One API must not be called excessively.
# This version uses 60-second refresh and avoids
# unnecessary Order Book polling.
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

# IMPORTANT:
# Do not use 20 seconds.
REFRESH_SECONDS = 60

CANDLE_DAYS = 3

ORDER_TYPE = "MARKET"
PRODUCT_TYPE = "INTRADAY"
DURATION = "DAY"


# ============================================================
# PAPER / LIVE MODE
# ============================================================

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

# FIXED:
# This MUST be a Path because .exists() is used later.
INSTRUMENT_FILE = (
    BASE_DIR / "OpenAPIScripMaster.json"
)

INSTRUMENT_URL = (
    "https://margincalculator.angelone.in/"
    "OpenAPI_File/files/OpenAPIScripMaster.json"
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
# SESSION STATE
# ============================================================

DEFAULTS = {
    "api": None,
    "login_status": False,

    "nifty_ltp": None,

    "signal": "WAIT",
    "supertrend": None,
    "st_direction": None,
    "last_candle_time": "",

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

    "last_order_check": 0.0,

    "last_error": "",
}


for key, value in DEFAULTS.items():

    if key not in st.session_state:

        st.session_state[key] = value


# ============================================================
# UTILITY
# ============================================================

def now_ist():

    return datetime.now(IST)


def safe_float(
    value,
    default=None
):

    try:

        if value is None:

            return default

        return float(value)

    except Exception:

        return default


def is_market_open():

    now = now_ist()

    if now.weekday() >= 5:

        return False

    return (
        dt_time(9, 15)
        <= now.time()
        <= dt_time(15, 30)
    )


# ============================================================
# TOTP
# ============================================================

def get_totp():

    secret = (
        ANGEL_TOTP_SECRET
        .strip()
    )

    if not secret:

        raise RuntimeError(
            "ANGEL_TOTP_SECRET is empty."
        )

    # --------------------------------------------------------
    # Support otpauth:// URI
    # --------------------------------------------------------

    if secret.lower().startswith(
        "otpauth://"
    ):

        try:

            from urllib.parse import (
                urlparse,
                parse_qs,
            )

            parsed = urlparse(
                secret
            )

            params = parse_qs(
                parsed.query
            )

            secret = params.get(
                "secret",
                [""],
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

    if (
        len(secret) == 6
        and secret.isdigit()
    ):

        raise RuntimeError(
            "ANGEL_TOTP_SECRET contains a "
            "6-digit OTP.\n\n"
            "Use the original Base32 TOTP secret."
        )

    try:

        return pyotp.TOTP(
            secret
        ).now()

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

    data = response.get(
        "data"
    )

    if not data:

        raise RuntimeError(
            "Angel One returned no session data."
        )

    if not data.get(
        "jwtToken"
    ):

        raise RuntimeError(
            "Angel One returned no jwtToken."
        )

    return api


# ============================================================
# DOWNLOAD INSTRUMENT MASTER
# ============================================================

def ensure_instrument_master():

    if INSTRUMENT_FILE.exists():

        return

    try:

        response = requests.get(
            INSTRUMENT_URL,
            timeout=30,
        )

        response.raise_for_status()

        if not response.content:

            raise RuntimeError(
                "Instrument master download is empty."
            )

        INSTRUMENT_FILE.write_bytes(
            response.content
        )

    except Exception as exc:

        raise RuntimeError(
            "Could not download Angel One "
            "instrument master:\n"
            + str(exc)
        )


# ============================================================
# LOAD INSTRUMENT MASTER
# ============================================================

@st.cache_data(ttl=3600)
def load_instrument_master_cached(
    file_path,
    modified_time
):

    with open(
        file_path,
        "r",
        encoding="utf-8"
    ) as f:

        data = json.load(f)

    if not isinstance(
        data,
        list
    ):

        raise RuntimeError(
            "Instrument master must be a JSON list."
        )

    if not data:

        raise RuntimeError(
            "Instrument master is empty."
        )

    return data


def load_instrument_master():

    ensure_instrument_master()

    return load_instrument_master_cached(
        str(INSTRUMENT_FILE),
        INSTRUMENT_FILE.stat().st_mtime,
    )


# ============================================================
# NIFTY LTP
# ============================================================

def get_nifty_ltp(
    api
):

    response = api.ltpData(
        "NSE",
        NIFTY_SYMBOL,
        NIFTY_TOKEN
    )

    if not response:

        raise RuntimeError(
            "NIFTY LTP returned empty response."
        )

    if not response.get(
        "status"
    ):

        raise RuntimeError(
            "NIFTY LTP FAILED | "
            f"message={response.get('message')} | "
            f"errorcode={response.get('errorcode')}"
        )

    data = response.get(
        "data"
    )

    if not data:

        raise RuntimeError(
            "NIFTY LTP has no data."
        )

    ltp = safe_float(
        data.get("ltp")
    )

    if ltp is None:

        raise RuntimeError(
            "Invalid NIFTY LTP:\n"
            + str(data)
        )

    return ltp


# ============================================================
# NIFTY 1-MINUTE CANDLES
# ============================================================

def get_nifty_1m_candles(
    api
):

    now = now_ist()

    from_dt = (
        now
        - timedelta(
            days=CANDLE_DAYS
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
            from_dt.strftime(
                "%Y-%m-%d %H:%M"
            ),

        "todate":
            now.strftime(
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

    if not response.get(
        "status"
    ):

        raise RuntimeError(
            "Candle API FAILED:\n"
            + str(response)
        )

    rows = response.get(
        "data"
    )

    if not rows:

        raise RuntimeError(
            "Candle API returned no candles."
        )

    df = pd.DataFrame(
        rows
    )

    if df.shape[1] < 5:

        raise RuntimeError(
            "Unexpected candle format."
        )

    df = df.iloc[
        :,
        :6
    ].copy()

    columns = [
        "datetime",
        "open",
        "high",
        "low",
        "close",
        "volume",
    ]

    df.columns = columns[
        :df.shape[1]
    ]

    df["datetime"] = pd.to_datetime(
        df["datetime"],
        errors="coerce"
    )

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
        subset=[
            "datetime"
        ],
        keep="last"
    )

    # --------------------------------------------------------
    # Remove currently forming minute candle
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
    # NSE session
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
            "No completed NSE candles."
        )

    return df


# ============================================================
# 1-MINUTE -> 2-MINUTE
# ============================================================

def resample_to_2min(
    df
):

    temp = (
        df.copy()
        .set_index("datetime")
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
                "open":
                    "first",

                "high":
                    "max",

                "low":
                    "min",

                "close":
                    "last",

                "volume":
                    "sum",
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

    current_boundary = (
        current.floor("2min")
    )

    result = result[
        result.index
        < current_boundary
    ]

    # --------------------------------------------------------
    # NSE session
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

    previous_close = (
        close.shift(1)
    )

    tr1 = (
        high - low
    )

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
        axis=1
    ).max(
        axis=1
    )

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

    for i in range(
        len(data)
    ):

        if pd.isna(
            atr.iloc[i]
        ):

            continue

        if i == 0:

            final_upper.iloc[i] = (
                basic_upper.iloc[i]
            )

            final_lower.iloc[i] = (
                basic_lower.iloc[i]
            )

            continue

        previous_upper = (
            final_upper.iloc[i - 1]
        )

        previous_lower = (
            final_lower.iloc[i - 1]
        )

        previous_close_value = (
            close.iloc[i - 1]
        )

        # ----------------------------------------------------
        # Upper band
        # ----------------------------------------------------

        if (
            pd.isna(previous_upper)
            or
            basic_upper.iloc[i]
            < previous_upper
            or
            previous_close_value
            > previous_upper
        ):

            final_upper.iloc[i] = (
                basic_upper.iloc[i]
            )

        else:

            final_upper.iloc[i] = (
                previous_upper
            )

        # ----------------------------------------------------
        # Lower band
        # ----------------------------------------------------

        if (
            pd.isna(previous_lower)
            or
            basic_lower.iloc[i]
            > previous_lower
            or
            previous_close_value
            < previous_lower
        ):

            final_lower.iloc[i] = (
                basic_lower.iloc[i]
            )

        else:

            final_lower.iloc[i] = (
                previous_lower
            )

        # ----------------------------------------------------
        # Direction
        # ----------------------------------------------------

        if pd.isna(
            direction.iloc[i - 1]
        ):

            direction.iloc[i] = 1

        else:

            previous_direction = (
                direction.iloc[i - 1]
            )

            if (
                previous_direction == -1
                and
                close.iloc[i]
                > final_upper.iloc[i]
            ):

                direction.iloc[i] = 1

            elif (
                previous_direction == 1
                and
                close.iloc[i]
                < final_lower.iloc[i]
            ):

                direction.iloc[i] = -1

            else:

                direction.iloc[i] = (
                    previous_direction
                )

        if (
            direction.iloc[i]
            == 1
        ):

            supertrend.iloc[i] = (
                final_lower.iloc[i]
            )

        else:

            supertrend.iloc[i] = (
                final_upper.iloc[i]
            )

    data["ATR"] = atr

    data["Basic_Upper"] = (
        basic_upper
    )

    data["Basic_Lower"] = (
        basic_lower
    )

    data["Final_Upper"] = (
        final_upper
    )

    data["Final_Lower"] = (
        final_lower
    )

    data["Supertrend"] = (
        supertrend
    )

    data["ST_Direction"] = (
        direction
    )

    data["ST_Green"] = (
        direction == 1
    )

    data["ST_Red"] = (
        direction == -1
    )

    return data


# ============================================================
# ATM NIFTY CE
# ============================================================

def select_atm_nifty_ce(
    spot
):

    records = (
        load_instrument_master()
    )

    df = pd.DataFrame(
        records
    )

    if df.empty:

        raise RuntimeError(
            "Instrument master is empty."
        )

    df.columns = [
        str(c)
        .strip()
        .lower()
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
            "Instrument master missing:\n"
            + ", ".join(missing)
        )

    # --------------------------------------------------------
    # NFO
    # --------------------------------------------------------

    df["exch_seg"] = (
        df["exch_seg"]
        .astype(str)
        .str.upper()
        .str.strip()
    )

    df = df[
        df["exch_seg"]
        == "NFO"
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

        df = df[
            (
                df["name"]
                == "NIFTY"
            )
            |
            (
                df["symbol"]
                .str.startswith(
                    "NIFTY"
                )
            )
        ]

    else:

        df = df[
            df["symbol"]
            .str.startswith(
                "NIFTY"
            )
        ]

    # --------------------------------------------------------
    # CE only
    # --------------------------------------------------------

    df = df[
        df["symbol"]
        .str.endswith("CE")
    ]

    # --------------------------------------------------------
    # OPTIDX
    # --------------------------------------------------------

    if "instrumenttype" in df.columns:

        instrument_type = (
            df["instrumenttype"]
            .astype(str)
            .str.upper()
            .str.strip()
        )

        mask = (
            instrument_type
            == "OPTIDX"
        )

        if mask.any():

            df = df[
                mask
            ]

    # --------------------------------------------------------
    # Expiry
    # --------------------------------------------------------

    df["expiry_dt"] = pd.to_datetime(
        df["expiry"],
        errors="coerce",
        dayfirst=True,
    )

    missing_expiry = (
        df["expiry_dt"]
        .isna()
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
            errors="coerce",
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
            "No future NIFTY CE expiry."
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
            "No valid NIFTY CE strikes."
        )

    # --------------------------------------------------------
    # ATM
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

    strike = float(
        selected["strike_actual"]
    )

    lot_size = int(
        safe_float(
            selected["lotsize"],
            0
        )
    )

    expiry = (
        selected["expiry_dt"]
        .strftime(
            "%d-%b-%Y"
        )
    )

    if not token:

        raise RuntimeError(
            "Selected CE token is empty."
        )

    if not symbol:

        raise RuntimeError(
            "Selected CE symbol is empty."
        )

    if lot_size <= 0:

        raise RuntimeError(
            f"Invalid lot size: {lot_size}"
        )

    return {

        "token":
            token,

        "symbol":
            symbol,

        "strike":
            strike,

        "expiry":
            expiry,

        "lot_size":
            lot_size,

        "quantity":
            lot_size * LOTS,
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

        if not response.get(
            "status"
        ):
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
# STATE
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
            "order_status",
            "order_status_unknown",
        ]

        for key in keys:

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

def get_order_book(
    api
):

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

        if not data:

            return []

        return data

    except Exception:

        return []


# ============================================================
# FIND EXACT ORDER ID
# ============================================================

def find_order_by_id(
    orders,
    order_id
):

    if not orders or not order_id:

        return None

    wanted = str(
        order_id
    ).strip()

    for order in orders:

        current = str(
            order.get(
                "orderid",
                ""
            )
        ).strip()

        if current == wanted:

            return order

    return None


# ============================================================
# FIND BUY CE
# ============================================================

def find_buy_order(
    orders,
    symbol
):

    if not orders:

        return None

    wanted = (
        str(symbol)
        .upper()
        .strip()
    )

    for order in orders:

        order_symbol = (
            str(
                order.get(
                    "tradingsymbol",
                    ""
                )
            )
            .upper()
            .strip()
        )

        transaction = (
            str(
                order.get(
                    "transactiontype",
                    ""
                )
            )
            .upper()
            .strip()
        )

        exchange = (
            str(
                order.get(
                    "exchange",
                    ""
                )
            )
            .upper()
            .strip()
        )

        if (
            order_symbol == wanted
            and transaction == "BUY"
            and exchange == "NFO"
        ):

            return order

    return None


# ============================================================
# PAPER ORDER
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
# REAL ANGEL ORDER
# ============================================================

def place_real_order(
    api,
    option
):

    params = {

        "variety":
            "NORMAL",

        "tradingsymbol":
            option["symbol"],

        "symboltoken":
            str(option["token"]),

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

        "squareoff":
            "0",

        "stoploss":
            "0",

        "quantity":
            str(
                option["quantity"]
            ),
    }

    # --------------------------------------------------------
    # LOCK BEFORE REQUEST
    # --------------------------------------------------------

    st.session_state.last_order_symbol = (
        option["symbol"]
    )

    st.session_state.order_status = (
        "ORDER REQUEST SENT"
    )

    st.session_state.order_status_unknown = (
        True
    )

    save_state()

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

        st.session_state.order_status = (
            "ORDER RESPONSE ERROR - CHECK ORDER BOOK"
        )

        st.session_state.order_status_unknown = (
            True
        )

        save_state()

        return (
            None,
            str(exc)
        )

    # --------------------------------------------------------
    # Empty response
    # --------------------------------------------------------

    if not response:

        st.session_state.order_status = (
            "EMPTY RESPONSE - CHECK ORDER BOOK"
        )

        st.session_state.order_status_unknown = (
            True
        )

        save_state()

        return (
            None,
            "EMPTY RESPONSE"
        )

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
                data.get(
                    "orderid"
                )
                or
                data.get(
                    "orderId"
                )
            )

        elif isinstance(
            data,
            str
        ):

            order_id = data

        # ----------------------------------------------------
        # Success
        # ----------------------------------------------------

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

        # ----------------------------------------------------
        # Broker rejection
        # ----------------------------------------------------

        if status is False:

            st.session_state.order_status = (
                "BROKER REJECTED"
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

            order_id = response.strip()

            st.session_state.last_order_id = (
                order_id
            )

            st.session_state.order_status = (
                "ORDER ACCEPTED"
            )

            st.session_state.order_status_unknown = (
                False
            )

            save_state()

            return (
                order_id,
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
# VERIFY ORDER
#
# IMPORTANT:
# Only a few checks.
# We do NOT continuously poll the Order Book.
# ============================================================

def verify_order_once(
    api,
    order_id,
    symbol
):

    orders = get_order_book(
        api
    )

    # --------------------------------------------------------
    # First try exact order ID.
    # --------------------------------------------------------

    if order_id:

        found = find_order_by_id(
            orders,
            order_id
        )

        if found:

            return found

    # --------------------------------------------------------
    # Fallback to symbol.
    # --------------------------------------------------------

    if symbol:

        found = find_buy_order(
            orders,
            symbol
        )

        if found:

            return found

    return None


# ============================================================
# AUTOMATIC BUY CE
# ============================================================

def automatic_buy_ce(
    api,
    option
):

    # --------------------------------------------------------
    # NEVER DUPLICATE
    # --------------------------------------------------------

    if (
        st.session_state.last_order_id
        or
        st.session_state.order_status_unknown
    ):

        return (
            False,
            "Order already attempted. "
            "No duplicate order will be sent."
        )

    # --------------------------------------------------------
    # MARKET
    # --------------------------------------------------------

    if not is_market_open():

        return (
            False,
            "Market is closed."
        )

    # --------------------------------------------------------
    # PAPER
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
    # REAL
    # --------------------------------------------------------

    order_id, error = (
        place_real_order(
            api,
            option
        )
    )

    if order_id:

        # ----------------------------------------------------
        # One Order Book check only.
        # ----------------------------------------------------

        found = verify_order_once(
            api,
            order_id,
            option["symbol"]
        )

        if found:

            status = found.get(
                "orderstatus",
                "FOUND"
            )

            st.session_state.order_status = (
                str(status)
            )

            st.session_state.order_status_unknown = (
                False
            )

            save_state()

        return (
            True,
            order_id
        )

    # --------------------------------------------------------
    # Uncertain broker response
    # --------------------------------------------------------

    found = verify_order_once(
        api,
        "",
        option["symbol"]
    )

    if found:

        found_id = str(
            found.get(
                "orderid",
                ""
            )
        )

        st.session_state.last_order_id = (
            found_id
        )

        st.session_state.order_status = (
            str(
                found.get(
                    "orderstatus",
                    "FOUND"
                )
            )
        )

        st.session_state.order_status_unknown = (
            False
        )

        save_state()

        return (
            True,
            found_id
        )

    return (
        False,
        "Order status UNKNOWN. "
        "No automatic retry was made."
    )


# ============================================================
# LOAD STATE
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
# MODE
# ============================================================

if PAPER_TRADING:

    st.warning(
        "🟡 PAPER TRADING MODE — "
        "NO REAL ANGEL ONE ORDER WILL BE SENT"
    )

else:

    st.error(
        "🔴 LIVE TRADING MODE — "
        "REAL ANGEL ONE ORDER CAN BE PLACED"
    )


# ============================================================
# LOGIN
# ============================================================

try:

    if (
        st.session_state.api is None
        or
        not st.session_state.login_status
    ):

        with st.spinner(
            "Logging into Angel One..."
        ):

            api = login_angel()

        st.session_state.api = (
            api
        )

        st.session_state.login_status = (
            True
        )

    else:

        api = (
            st.session_state.api
        )

except Exception as exc:

    st.session_state.login_status = (
        False
    )

    st.session_state.last_error = (
        str(exc)
    )

    st.error(
        "Angel One Login Error"
    )

    st.code(
        str(exc)
    )

    st.stop()


# ============================================================
# STATUS
# ============================================================

c1, c2, c3, c4 = st.columns(4)

with c1:

    st.metric(
        "Login",
        "CONNECTED"
    )

with c2:

    st.metric(
        "Market",
        "OPEN"
        if is_market_open()
        else "CLOSED"
    )

with c3:

    st.metric(
        "Mode",
        "PAPER"
        if PAPER_TRADING
        else "LIVE"
    )

with c4:

    st.metric(
        "Strategy",
        "2M ST 20,1.5"
    )


# ============================================================
# NIFTY LTP
# ============================================================

try:

    nifty_ltp = get_nifty_ltp(
        api
    )

    st.session_state.nifty_ltp = (
        nifty_ltp
    )

except Exception as exc:

    st.session_state.last_error = (
        str(exc)
    )

    st.error(
        "NIFTY LTP ERROR"
    )

    st.code(
        str(exc)
    )

    st.stop()


# ============================================================
# CANDLES
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
            "for Supertrend."
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

close_value = safe_float(
    latest["close"]
)

supertrend_value = safe_float(
    latest["Supertrend"]
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

s1, s2, s3, s4 = st.columns(4)

with s1:

    st.metric(
        "NIFTY",
        f"{nifty_ltp:,.2f}"
    )

with s2:

    st.metric(
        "2M Close",
        (
            f"{close_value:,.2f}"
            if close_value is not None
            else "-"
        )
    )

with s3:

    st.metric(
        "Supertrend",
        (
            f"{supertrend_value:,.2f}"
            if supertrend_value is not None
            else "-"
        )
    )

with s4:

    st.metric(
        "Signal",
        signal
    )


st.write(
    "Latest completed 2-minute candle: "
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

    o1, o2, o3, o4, o5 = st.columns(5)

    with o1:

        st.metric(
            "Symbol",
            option["symbol"]
        )

    with o2:

        st.metric(
            "Strike",
            f"{option['strike']:,.0f}"
        )

    with o3:

        st.metric(
            "Expiry",
            option["expiry"]
        )

    with o4:

        st.metric(
            "Quantity",
            option["quantity"]
        )

    with o5:

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

if (
    signal == "BUY CE"
    and option is not None
):

    # --------------------------------------------------------
    # NO MANUAL BUTTON
    #
    # Automatic execution happens here.
    # --------------------------------------------------------

    if (
        not st.session_state.last_order_id
        and
        not st.session_state.order_status_unknown
    ):

        with st.spinner(
            "BUY CE signal detected — "
            "placing automatic order..."
        ):

            success, result = (
                automatic_buy_ce(
                    api,
                    option
                )
            )

        if success:

            st.success(
                "✅ Automatic BUY CE order processed."
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
            "Automatic order already attempted. "
            "Duplicate order is blocked."
        )


# ============================================================
# ORDER STATUS
# ============================================================

st.subheader(
    "📦 Order Status"
)

q1, q2, q3 = st.columns(3)

with q1:

    st.metric(
        "Order ID",
        (
            st.session_state.last_order_id
            or "-"
        )
    )

with q2:

    st.metric(
        "Symbol",
        (
            st.session_state.last_order_symbol
            or "-"
        )
    )

with q3:

    st.metric(
        "Status",
        (
            st.session_state.order_status
            or "NONE"
        )
    )


if st.session_state.order_status_unknown:

    st.warning(
        "⚠️ Order response is uncertain. "
        "The program will NOT submit another order automatically."
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

    selected_order_id = (
        st.session_state.last_order_id
    )

    selected_symbol = (
        st.session_state.last_order_symbol
    )

    def highlight_order(row):

        row_order_id = str(
            row.get(
                "orderid",
                ""
            )
        ).strip()

        row_symbol = str(
            row.get(
                "tradingsymbol",
                ""
            )
        ).upper().strip()

        if (
            selected_order_id
            and
            row_order_id
            == str(
                selected_order_id
            ).strip()
        ):

            return [
                "background-color: #90EE90"
            ] * len(row)

        if (
            selected_symbol
            and
            row_symbol
            == str(
                selected_symbol
            ).upper().strip()
        ):

            return [
                "background-color: #fff3cd"
            ] * len(row)

        return [
            ""
        ] * len(row)

    if (
        selected_order_id
        or selected_symbol
    ):

        st.dataframe(
            order_df.style.apply(
                highlight_order,
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
# RECENT CANDLES
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
# CHART
# ============================================================

st.subheader(
    "📊 NIFTY 2-Minute Chart"
)

chart = df_st[
    [
        "close",
        "Supertrend",
    ]
].tail(100).copy()

chart.columns = [
    "NIFTY Close",
    "Supertrend",
]

st.line_chart(
    chart,
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
        Instrument: NIFTY 50

        Candle: 2-minute

        Supertrend: 20, 1.5

        GREEN = BUY CE

        RED = WAIT

        Green flip is NOT required.

        When the latest completed 2-minute candle is GREEN:

        1. Select nearest future NIFTY expiry.
        2. Select nearest available NIFTY strike.
        3. Select CE.
        4. Automatically BUY 1 lot.
        5. Check Angel One Order Book.
        6. Highlight the order.

        No manual BUY/SELL button is used.

        Duplicate automatic orders are blocked.
        """
    )


# ============================================================
# SYSTEM INFORMATION
# ============================================================

with st.expander(
    "System Information"
):

    st.write(
        f"Dashboard directory: `{BASE_DIR}`"
    )

    st.write(
        f"Instrument file: `{INSTRUMENT_FILE}`"
    )

    st.write(
        "Instrument master exists: "
        f"`{INSTRUMENT_FILE.exists()}`"
    )

    st.write(
        f"Instrument URL: `{INSTRUMENT_URL}`"
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

    st.write(
        f"Latest candle: `{latest_time}`"
    )


# ============================================================
# REFRESH
# ============================================================

time.sleep(
    REFRESH_SECONDS
)

st.rerun()
