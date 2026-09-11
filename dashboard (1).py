# ============================================================
# dashboard.py
#
# NIFTY LIVE DIRECT AUTOMATIC BUY CE
# ANGEL ONE SMARTAPI + STREAMLIT
#
# STRATEGY
# ------------------------------------------------------------
# NIFTY 1-MINUTE CANDLES
#          ↓
# COMPLETED 2-MINUTE CANDLES
#          ↓
# SUPERTREND (20, 1.5)
#          ↓
# RED → GREEN FLIP
#          ↓
# SELECT NEAREST ATM NIFTY CE
#          ↓
# DIRECT BUY MARKET ORDER
#
# BUY CE ONLY
# NO SELL LOGIC
# NO MANUAL BUY BUTTON
# NO BROKER EXECUTION CONFIRMATION
#
# DUPLICATE PROTECTION
# ------------------------------------------------------------
# One completed 2-minute candle can generate at most
# ONE automatic BUY attempt.
#
# Additional protections:
# - Only completed 2-minute candles
# - Exactly two 1-minute candles required
# - Current signal must be recent
# - Market-hours check
# - Persistent state
# - Same signal candle cannot repeat
# ============================================================

import os
import json
import time
from pathlib import Path
from datetime import datetime, timedelta, time as dt_time

import numpy as np
import pandas as pd
import requests
import pyotp
import streamlit as st

from SmartApi import SmartConnect


# ============================================================
# PAGE CONFIG
# ============================================================

st.set_page_config(
    page_title="NIFTY Automatic BUY CE",
    page_icon="📈",
    layout="wide",
)


# ============================================================
# SETTINGS
# ============================================================

# TRUE = REAL ANGEL ONE ORDER
# FALSE = PAPER ORDER
LIVE_TRADING = True

# Number of NIFTY lots
LOTS = 1

# Supertrend
ST_PERIOD = 20
ST_MULTIPLIER = 1.5

# Angel One candle interval
CANDLE_INTERVAL = "ONE_MINUTE"

# Dashboard refresh
REFRESH_SECONDS = 10

# Maximum age allowed for a fresh signal
# Prevents buying an old GREEN flip after app restart.
MAX_SIGNAL_AGE_MINUTES = 3

# NSE trading session
MARKET_OPEN = dt_time(9, 15)
MARKET_CLOSE = dt_time(15, 30)

# NIFTY
NIFTY_EXCHANGE = "NSE"
NIFTY_SYMBOL = "NIFTY"
NIFTY_TOKEN = "99926000"

# Options
OPTION_EXCHANGE = "NFO"

PRODUCT_TYPE = "INTRADAY"
ORDER_TYPE = "MARKET"
VARIETY = "NORMAL"
DURATION = "DAY"

# Instrument master
INSTRUMENT_FILE = Path(
    "OpenAPIScripMaster.json"
)

STATE_FILE = Path(
    "nifty_auto_ce_state.json"
)

INSTRUMENT_URL = (
    "https://margincalculator.angelone.com/"
    "OpenAPI_File/files/OpenAPIScripMaster.json"
)


# ============================================================
# IST
# ============================================================

IST = "Asia/Kolkata"


def now_ist():
    """
    Return current time in IST as timezone-aware Timestamp.
    """

    return pd.Timestamp.now(
        tz=IST
    )


def today_ist():
    return now_ist().date()


# ============================================================
# DEFAULT STATE
# ============================================================

DEFAULT_STATE = {
    "last_processed_candle": None,
    "last_signal_candle": None,

    "last_order_id": None,
    "last_order_time": None,

    "last_symbol": None,
    "last_token": None,
    "last_strike": None,
    "last_expiry": None,
    "last_quantity": None,

    "last_signal": "WAIT",
    "last_direction": "-",

    "last_error": None,
}


# ============================================================
# STREAMLIT SESSION STATE
# ============================================================

if "api" not in st.session_state:
    st.session_state.api = None

if "logged_in" not in st.session_state:
    st.session_state.logged_in = False

if "running" not in st.session_state:
    st.session_state.running = True

if "state" not in st.session_state:
    st.session_state.state = None


# ============================================================
# STATE FILE
# ============================================================

def load_state():

    try:

        if STATE_FILE.exists():

            with open(
                STATE_FILE,
                "r",
                encoding="utf-8"
            ) as f:

                data = json.load(f)

            state = DEFAULT_STATE.copy()

            if isinstance(data, dict):
                state.update(data)

            return state

    except Exception:

        pass

    return DEFAULT_STATE.copy()


def save_state(state):

    try:

        temp_file = Path(
            str(STATE_FILE) + ".tmp"
        )

        with open(
            temp_file,
            "w",
            encoding="utf-8"
        ) as f:

            json.dump(
                state,
                f,
                indent=2,
                default=str
            )

        temp_file.replace(
            STATE_FILE
        )

    except Exception as e:

        st.warning(
            f"State save warning: {e}"
        )


if st.session_state.state is None:

    st.session_state.state = load_state()


# ============================================================
# CREDENTIALS
# ============================================================

def get_secret(
    name,
    default=""
):

    # Streamlit Cloud secrets first
    try:

        value = st.secrets.get(
            name,
            None
        )

        if value:

            return str(
                value
            ).strip()

    except Exception:

        pass

    # Local environment fallback
    return os.getenv(
        name,
        default
    ).strip()


API_KEY = get_secret(
    "ANGEL_API_KEY"
)

CLIENT_ID = get_secret(
    "ANGEL_CLIENT_ID"
)

PASSWORD = get_secret(
    "ANGEL_PASSWORD"
)

TOTP_SECRET = get_secret(
    "ANGEL_TOTP_SECRET"
)


# ============================================================
# TOTP
# ============================================================

def clean_totp_secret(
    secret
):

    secret = (
        str(secret)
        .strip()
    )

    # Accept otpauth:// URI
    if secret.lower().startswith(
        "otpauth://"
    ):

        try:

            from urllib.parse import (
                urlparse,
                parse_qs
            )

            parsed = urlparse(
                secret
            )

            params = parse_qs(
                parsed.query
            )

            secret = params.get(
                "secret",
                [""]
            )[0]

        except Exception:

            pass

    return (
        secret
        .replace(" ", "")
        .replace("\n", "")
        .replace("\r", "")
        .upper()
    )


# ============================================================
# ANGEL ONE LOGIN
# ============================================================

def angel_login():

    if not API_KEY:

        raise RuntimeError(
            "ANGEL_API_KEY is missing"
        )

    if not CLIENT_ID:

        raise RuntimeError(
            "ANGEL_CLIENT_ID is missing"
        )

    if not PASSWORD:

        raise RuntimeError(
            "ANGEL_PASSWORD is missing"
        )

    if not TOTP_SECRET:

        raise RuntimeError(
            "ANGEL_TOTP_SECRET is missing"
        )

    secret = clean_totp_secret(
        TOTP_SECRET
    )

    if not secret:

        raise RuntimeError(
            "ANGEL_TOTP_SECRET is empty"
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
            api_key=API_KEY
        )

        response = api.generateSession(
            CLIENT_ID,
            PASSWORD,
            totp
        )

    except Exception as e:

        raise RuntimeError(
            f"Angel One login exception: {e}"
        )

    if not response:

        raise RuntimeError(
            "Angel One returned empty login response"
        )

    if response.get("status") is not True:

        raise RuntimeError(
            f"Angel login failed: {response}"
        )

    data = response.get(
        "data"
    )

    if not data:

        raise RuntimeError(
            f"Angel login returned no data: {response}"
        )

    if not data.get(
        "jwtToken"
    ):

        raise RuntimeError(
            f"Angel login returned no jwtToken: {response}"
        )

    st.session_state.api = api
    st.session_state.logged_in = True

    return api


# ============================================================
# GET API
# ============================================================

def get_api():

    if (
        st.session_state.api
        is not None
    ):

        return st.session_state.api

    return angel_login()


# ============================================================
# MARKET HOURS
# ============================================================

def market_is_open():

    now = now_ist()

    # Monday = 0
    # Sunday = 6
    if now.weekday() >= 5:

        return False

    current = now.time()

    return (
        MARKET_OPEN
        <= current
        <= MARKET_CLOSE
    )


# ============================================================
# DOWNLOAD INSTRUMENT MASTER
# ============================================================

@st.cache_data(
    ttl=3600,
    show_spinner=False
)
def download_instruments():

    try:

        response = requests.get(
            INSTRUMENT_URL,
            timeout=30
        )

        response.raise_for_status()

        data = response.json()

        if not isinstance(
            data,
            list
        ):

            raise RuntimeError(
                "Instrument master response is not a list"
            )

        df = pd.DataFrame(
            data
        )

        if df.empty:

            raise RuntimeError(
                "Instrument master is empty"
            )

        return df

    except Exception as e:

        raise RuntimeError(
            f"Instrument master download failed: {e}"
        )


# ============================================================
# LOAD INSTRUMENT MASTER
# ============================================================

@st.cache_data(
    ttl=3600,
    show_spinner=False
)
def load_instruments():

    if INSTRUMENT_FILE.exists():

        try:

            with open(
                INSTRUMENT_FILE,
                "r",
                encoding="utf-8"
            ) as f:

                data = json.load(f)

            if isinstance(
                data,
                list
            ):

                df = pd.DataFrame(
                    data
                )

                if not df.empty:

                    return df

        except Exception:

            pass

    return download_instruments()


# ============================================================
# NIFTY LTP
# ============================================================

def get_nifty_ltp(api):

    try:

        response = api.ltpData(
            NIFTY_EXCHANGE,
            NIFTY_SYMBOL,
            NIFTY_TOKEN
        )

    except Exception as e:

        raise RuntimeError(
            f"NIFTY LTP exception: {e}"
        )

    if not response:

        raise RuntimeError(
            "NIFTY LTP returned empty response"
        )

    if response.get(
        "status"
    ) is not True:

        raise RuntimeError(
            f"NIFTY LTP FAILED | {response}"
        )

    data = response.get(
        "data"
    ) or {}

    ltp = data.get(
        "ltp"
    )

    if ltp is None:

        raise RuntimeError(
            f"NIFTY LTP missing | {response}"
        )

    try:

        return float(
            ltp
        )

    except Exception:

        raise RuntimeError(
            f"Invalid NIFTY LTP: {ltp}"
        )


# ============================================================
# GET 1-MINUTE CANDLES
# ============================================================

def get_1m_candles(
    api,
    days=3
):

    current = now_ist()

    from_time = (
        current
        - pd.Timedelta(
            days=days
        )
    )

    params = {

        "exchange": "NSE",

        "symboltoken":
            NIFTY_TOKEN,

        "interval":
            CANDLE_INTERVAL,

        "fromdate":
            from_time.strftime(
                "%Y-%m-%d %H:%M"
            ),

        "todate":
            current.strftime(
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
            "Candle API returned empty response"
        )

    if response.get(
        "status"
    ) is not True:

        raise RuntimeError(
            f"Candle API failed | {response}"
        )

    rows = response.get(
        "data"
    )

    if not rows:

        raise RuntimeError(
            "No NIFTY candle data returned"
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
        ]
    )

    # --------------------------------------------------------
    # Timestamp
    # --------------------------------------------------------

    df["timestamp"] = pd.to_datetime(
        df["timestamp"],
        errors="coerce"
    )

    df = df.dropna(
        subset=["timestamp"]
    )

    # --------------------------------------------------------
    # Convert to IST
    # --------------------------------------------------------

    try:

        if df["timestamp"].dt.tz is None:

            # Angel data normally represents IST timestamps.
            # Localize them explicitly as Asia/Kolkata.
            df["timestamp"] = (
                df["timestamp"]
                .dt.tz_localize(
                    IST
                )
            )

        else:

            df["timestamp"] = (
                df["timestamp"]
                .dt.tz_convert(
                    IST
                )
            )

    except Exception as e:

        raise RuntimeError(
            f"Timestamp conversion failed: {e}"
        )

    # --------------------------------------------------------
    # Numeric values
    # --------------------------------------------------------

    numeric_cols = [
        "open",
        "high",
        "low",
        "close",
        "volume",
    ]

    for col in numeric_cols:

        df[col] = pd.to_numeric(
            df[col],
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

    df = df.sort_values(
        "timestamp"
    )

    df = df.drop_duplicates(
        subset=["timestamp"],
        keep="last"
    )

    return df.reset_index(
        drop=True
    )


# ============================================================
# REMOVE CURRENT FORMING 1-MINUTE CANDLE
# ============================================================

def remove_current_candle(
    df
):

    if df.empty:

        return df

    current_minute = (
        now_ist()
        .floor("min")
    )

    return df[
        df["timestamp"]
        < current_minute
    ].copy()


# ============================================================
# RESAMPLE 1-MINUTE → 2-MINUTE
# ============================================================

def resample_to_2m(
    df
):

    if df.empty:

        return df

    x = df.copy()

    x = x.set_index(
        "timestamp"
    )

    # NSE session only
    x = x.between_time(
        "09:15",
        "15:29"
    )

    if x.empty:

        return pd.DataFrame()

    # --------------------------------------------------------
    # IMPORTANT
    #
    # 09:15 + 09:16 = first 2-minute candle
    # 09:17 + 09:18 = second
    #
    # label="right" means:
    #
    # 09:15-09:17 → timestamp 09:17
    # 09:17-09:19 → timestamp 09:19
    # --------------------------------------------------------

    grouped = x.resample(
        "2min",
        origin="start_day",
        offset="9h15min",
        label="right",
        closed="left"
    )

    # --------------------------------------------------------
    # Require EXACTLY TWO 1-minute candles.
    #
    # This prevents a missing 1-minute candle from creating
    # a false "completed" 2-minute candle.
    # --------------------------------------------------------

    count = grouped["close"].count()

    result = grouped.agg(
        {
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum",
        }
    )

    result["one_min_count"] = count

    result = result[
        result["one_min_count"] == 2
    ]

    result = result.dropna(
        subset=[
            "open",
            "high",
            "low",
            "close",
        ]
    )

    result = result.reset_index()

    return result


# ============================================================
# SUPERTREND
# ============================================================

def calculate_supertrend(
    df,
    period=20,
    multiplier=1.5
):

    df = df.copy()

    if len(df) == 0:

        return df

    high = df["high"]
    low = df["low"]
    close = df["close"]

    previous_close = close.shift(
        1
    )

    # --------------------------------------------------------
    # True Range
    # --------------------------------------------------------

    tr1 = (
        high - low
    )

    tr2 = (
        high - previous_close
    ).abs()

    tr3 = (
        low - previous_close
    ).abs()

    tr = pd.concat(
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
    # ATR
    # --------------------------------------------------------

    atr = tr.ewm(
        alpha=1 / period,
        adjust=False,
        min_periods=period
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

    final_upper = pd.Series(
        np.nan,
        index=df.index,
        dtype=float
    )

    final_lower = pd.Series(
        np.nan,
        index=df.index,
        dtype=float
    )

    supertrend = pd.Series(
        np.nan,
        index=df.index,
        dtype=float
    )

    direction = pd.Series(
        np.nan,
        index=df.index,
        dtype=float
    )

    # --------------------------------------------------------
    # Supertrend calculation
    # --------------------------------------------------------

    for i in range(
        len(df)
    ):

        # ATR not ready
        if pd.isna(
            atr.iloc[i]
        ):

            continue

        # First valid ATR candle
        if i == 0:

            final_upper.iloc[i] = (
                basic_upper.iloc[i]
            )

            final_lower.iloc[i] = (
                basic_lower.iloc[i]
            )

            supertrend.iloc[i] = (
                basic_upper.iloc[i]
            )

            direction.iloc[i] = -1

            continue

        # ----------------------------------------------------
        # Final Upper Band
        # ----------------------------------------------------

        previous_final_upper = (
            final_upper.iloc[i - 1]
        )

        if pd.isna(
            previous_final_upper
        ):

            final_upper.iloc[i] = (
                basic_upper.iloc[i]
            )

        elif (
            basic_upper.iloc[i]
            < previous_final_upper
            or close.iloc[i - 1]
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
        # Final Lower Band
        # ----------------------------------------------------

        previous_final_lower = (
            final_lower.iloc[i - 1]
        )

        if pd.isna(
            previous_final_lower
        ):

            final_lower.iloc[i] = (
                basic_lower.iloc[i]
            )

        elif (
            basic_lower.iloc[i]
            > previous_final_lower
            or close.iloc[i - 1]
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

        previous_supertrend = (
            supertrend.iloc[i - 1]
        )

        if pd.isna(
            previous_supertrend
        ):

            if close.iloc[i] >= (
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

            continue

        # Previous trend was RED
        if previous_supertrend == (
            final_upper.iloc[i - 1]
        ):

            if close.iloc[i] <= (
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

        # Previous trend was GREEN
        else:

            if close.iloc[i] >= (
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

    # --------------------------------------------------------
    # Output columns
    # --------------------------------------------------------

    df["ATR"] = atr

    df["Basic_Upper"] = (
        basic_upper
    )

    df["Basic_Lower"] = (
        basic_lower
    )

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

    # RED → GREEN
    df["GREEN_FLIP"] = (
        (direction == 1)
        &
        (direction.shift(1) == -1)
    )

    # GREEN → RED
    df["RED_FLIP"] = (
        (direction == -1)
        &
        (direction.shift(1) == 1)
    )

    return df


# ============================================================
# SELECT ATM NIFTY CE
# ============================================================

def select_atm_ce(
    instruments,
    spot
):

    df = instruments.copy()

    required = [
        "exch_seg",
        "instrumenttype",
        "name",
        "symbol",
        "token",
        "expiry",
        "strike",
        "lotsize",
    ]

    missing = [
        col
        for col in required
        if col not in df.columns
    ]

    if missing:

        raise RuntimeError(
            "Instrument master missing columns: "
            + ", ".join(missing)
        )

    # --------------------------------------------------------
    # NFO
    # --------------------------------------------------------

    df = df[
        df["exch_seg"]
        .astype(str)
        .str.upper()
        .str.strip()
        == "NFO"
    ].copy()

    # --------------------------------------------------------
    # OPTIDX
    # --------------------------------------------------------

    df = df[
        df["instrumenttype"]
        .astype(str)
        .str.upper()
        .str.strip()
        == "OPTIDX"
    ].copy()

    # --------------------------------------------------------
    # NIFTY
    # --------------------------------------------------------

    names = (
        df["name"]
        .astype(str)
        .str.upper()
        .str.strip()
    )

    df = df[
        names.isin(
            [
                "NIFTY",
                "NIFTY 50",
            ]
        )
    ].copy()

    # --------------------------------------------------------
    # CE only
    # --------------------------------------------------------

    symbols = (
        df["symbol"]
        .astype(str)
        .str.upper()
        .str.strip()
    )

    df = df[
        symbols.str.endswith(
            "CE"
        )
    ].copy()

    if df.empty:

        raise RuntimeError(
            "No NIFTY CE contracts found"
        )

    # --------------------------------------------------------
    # EXPIRY
    # --------------------------------------------------------

    df["expiry_dt"] = pd.to_datetime(
        df["expiry"],
        errors="coerce",
        dayfirst=False
    )

    df = df.dropna(
        subset=["expiry_dt"]
    )

    # Make expiry timezone-naive
    try:

        if df["expiry_dt"].dt.tz is not None:

            df["expiry_dt"] = (
                df["expiry_dt"]
                .dt.tz_localize(None)
            )

    except Exception:

        pass

    today = pd.Timestamp(
        today_ist()
    )

    future = df[
        df["expiry_dt"] >= today
    ].copy()

    if future.empty:

        raise RuntimeError(
            "No future NIFTY CE expiry found"
        )

    nearest_expiry = (
        future["expiry_dt"].min()
    )

    df = future[
        future["expiry_dt"]
        == nearest_expiry
    ].copy()

    # --------------------------------------------------------
    # STRIKE
    # --------------------------------------------------------

    df["strike_num"] = pd.to_numeric(
        df["strike"],
        errors="coerce"
    )

    df = df.dropna(
        subset=["strike_num"]
    )

    # Angel master generally stores NIFTY
    # strikes as 100x actual strike.
    df["strike_actual"] = np.where(
        df["strike_num"] > 100000,
        df["strike_num"] / 100.0,
        df["strike_num"]
    )

    # --------------------------------------------------------
    # ATM
    # --------------------------------------------------------

    atm_strike = (
        round(
            float(spot) / 50.0
        )
        * 50.0
    )

    df["distance"] = (
        df["strike_actual"]
        - atm_strike
    ).abs()

    df = df.sort_values(
        [
            "distance",
            "strike_actual",
        ]
    )

    if df.empty:

        raise RuntimeError(
            "Could not select ATM NIFTY CE"
        )

    row = df.iloc[0]

    try:

        lotsize = int(
            float(
                row["lotsize"]
            )
        )

    except Exception:

        raise RuntimeError(
            f"Invalid lot size: {row['lotsize']}"
        )

    if lotsize <= 0:

        raise RuntimeError(
            f"Invalid NIFTY lot size: {lotsize}"
        )

    return {

        "symbol":
            str(row["symbol"]).strip(),

        "token":
            str(row["token"]).strip(),

        "strike":
            float(row["strike_actual"]),

        "expiry":
            str(row["expiry"]),

        "lotsize":
            lotsize,
    }


# ============================================================
# EXTRACT ORDER ID
# ============================================================

def extract_order_id(
    response
):

    if response is None:

        return None

    # Most SmartAPI versions:
    # placeOrder() -> order ID string
    if isinstance(
        response,
        str
    ):

        value = response.strip()

        return value or None

    # Some versions may return dict
    if isinstance(
        response,
        dict
    ):

        # Direct
        for key in [
            "orderid",
            "orderId",
            "order_id",
        ]:

            value = response.get(
                key
            )

            if value:

                return str(
                    value
                )

        # Nested data
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
                "order_id",
            ]:

                value = data.get(
                    key
                )

                if value:

                    return str(
                        value
                    )

    return None


# ============================================================
# DIRECT BUY CE
# ============================================================

def place_direct_buy_ce(
    api,
    option
):

    quantity = (
        option["lotsize"]
        * LOTS
    )

    if quantity <= 0:

        raise RuntimeError(
            f"Invalid order quantity: {quantity}"
        )

    order_params = {

        "variety":
            VARIETY,

        "tradingsymbol":
            option["symbol"],

        "symboltoken":
            option["token"],

        "transactiontype":
            "BUY",

        "exchange":
            OPTION_EXCHANGE,

        "ordertype":
            ORDER_TYPE,

        "producttype":
            PRODUCT_TYPE,

        "duration":
            DURATION,

        "quantity":
            str(quantity),

    }

    # --------------------------------------------------------
    # PAPER MODE
    # --------------------------------------------------------

    if not LIVE_TRADING:

        return {
            "order_id":
                "PAPER_ORDER",
            "quantity":
                quantity,
            "params":
                order_params,
            "raw":
                order_params,
        }

    # --------------------------------------------------------
    # REAL DIRECT ORDER
    # --------------------------------------------------------

    try:

        response = api.placeOrder(
            order_params
        )

    except Exception as e:

        raise RuntimeError(
            f"Angel One placeOrder exception: {e}"
        )

    order_id = extract_order_id(
        response
    )

    if not order_id:

        raise RuntimeError(
            "Angel One returned no order ID | "
            f"response={response}"
        )

    return {

        "order_id":
            order_id,

        "quantity":
            quantity,

        "params":
            order_params,

        "raw":
            response,
    }


# ============================================================
# GET LAST COMPLETED 2-MINUTE CANDLE
# ============================================================

def get_latest_completed_signal(
    api
):

    # --------------------------------------------------------
    # Get 1-minute candles
    # --------------------------------------------------------

    df_1m = get_1m_candles(
        api,
        days=3
    )

    # Remove current forming 1-minute candle
    df_1m = remove_current_candle(
        df_1m
    )

    if len(df_1m) < 50:

        raise RuntimeError(
            "Not enough completed 1-minute candles"
        )

    # --------------------------------------------------------
    # 2-minute candles
    # --------------------------------------------------------

    df_2m = resample_to_2m(
        df_1m
    )

    if df_2m.empty:

        raise RuntimeError(
            "No completed 2-minute candles available"
        )

    if len(df_2m) < (
        ST_PERIOD + 5
    ):

        raise RuntimeError(
            "Not enough completed 2-minute candles "
            f"for Supertrend {ST_PERIOD}"
        )

    # --------------------------------------------------------
    # Supertrend
    # --------------------------------------------------------

    df_2m = calculate_supertrend(
        df_2m,
        period=ST_PERIOD,
        multiplier=ST_MULTIPLIER
    )

    valid = df_2m.dropna(
        subset=[
            "Supertrend",
            "ST_Direction",
        ]
    ).copy()

    if valid.empty:

        raise RuntimeError(
            "Supertrend is not ready"
        )

    latest = valid.iloc[-1]

    return df_2m, latest


# ============================================================
# AUTOMATIC BUY PROCESS
# ============================================================

def process_automatic_buy():

    # --------------------------------------------------------
    # Market hours
    # --------------------------------------------------------

    if not market_is_open():

        return {
            "status":
                "MARKET_CLOSED",
            "reason":
                "NSE market is closed",
        }

    # --------------------------------------------------------
    # API
    # --------------------------------------------------------

    api = get_api()

    # --------------------------------------------------------
    # NIFTY LTP
    # --------------------------------------------------------

    spot = get_nifty_ltp(
        api
    )

    # --------------------------------------------------------
    # Signal
    # --------------------------------------------------------

    df_2m, latest = (
        get_latest_completed_signal(
            api
        )
    )

    # --------------------------------------------------------
    # Signal candle
    # --------------------------------------------------------

    candle_timestamp = (
        pd.Timestamp(
            latest["timestamp"]
        )
    )

    if candle_timestamp.tzinfo is None:

        candle_timestamp = (
            candle_timestamp
            .tz_localize(IST)
        )

    else:

        candle_timestamp = (
            candle_timestamp
            .tz_convert(IST)
        )

    candle_key = (
        candle_timestamp
        .strftime(
            "%Y-%m-%d %H:%M:%S"
        )
    )

    # --------------------------------------------------------
    # Check signal age
    # --------------------------------------------------------

    age_seconds = (
        now_ist()
        - candle_timestamp
    ).total_seconds()

    # Negative age should not normally happen.
    if age_seconds < -30:

        return {
            "status":
                "WAIT",
            "reason":
                "Candle timestamp is in the future",
            "spot":
                spot,
            "candle":
                candle_key,
            "df":
                df_2m,
        }

    if (
        age_seconds
        >
        MAX_SIGNAL_AGE_MINUTES * 60
    ):

        direction = (
            "GREEN"
            if latest["ST_Direction"] == 1
            else "RED"
        )

        return {
            "status":
                "WAIT",
            "reason":
                "Latest completed candle is too old",
            "spot":
                spot,
            "candle":
                candle_key,
            "direction":
                direction,
            "df":
                df_2m,
        }

    # --------------------------------------------------------
    # DUPLICATE PROTECTION
    # --------------------------------------------------------

    state = st.session_state.state

    previous_processed = (
        state.get(
            "last_processed_candle"
        )
    )

    # Same candle was already handled.
    if previous_processed == candle_key:

        direction = (
            "GREEN"
            if latest["ST_Direction"] == 1
            else "RED"
        )

        return {
            "status":
                "WAIT",
            "reason":
                "This 2-minute candle was already processed",
            "spot":
                spot,
            "candle":
                candle_key,
            "direction":
                direction,
            "df":
                df_2m,
        }

    # --------------------------------------------------------
    # CURRENT TREND
    # --------------------------------------------------------

    is_green = (
        latest["ST_Direction"] == 1
    )

    is_green_flip = bool(
        latest["GREEN_FLIP"]
    )

    # --------------------------------------------------------
    # NO GREEN FLIP
    # --------------------------------------------------------

    if not is_green_flip:

        # Mark this candle processed only after
        # determining that it has no signal.
        state[
            "last_processed_candle"
        ] = candle_key

        state[
            "last_signal"
        ] = "WAIT"

        state[
            "last_direction"
        ] = (
            "GREEN"
            if is_green
            else "RED"
        )

        state[
            "last_error"
        ] = None

        save_state(
            state
        )

        return {
            "status":
                "WAIT",
            "reason":
                "No RED → GREEN flip",
            "spot":
                spot,
            "candle":
                candle_key,
            "direction":
                (
                    "GREEN"
                    if is_green
                    else "RED"
                ),
            "df":
                df_2m,
        }

    # --------------------------------------------------------
    # GREEN FLIP FOUND
    # --------------------------------------------------------

    # Check signal duplicate separately.
    last_signal_candle = (
        state.get(
            "last_signal_candle"
        )
    )

    if last_signal_candle == candle_key:

        return {
            "status":
                "WAIT",
            "reason":
                "GREEN signal already processed",
            "spot":
                spot,
            "candle":
                candle_key,
            "direction":
                "GREEN",
            "df":
                df_2m,
        }

    # --------------------------------------------------------
    # ATM CE
    # --------------------------------------------------------

    instruments = load_instruments()

    option = select_atm_ce(
        instruments,
        spot
    )

    # --------------------------------------------------------
    # DIRECT BUY
    # --------------------------------------------------------

    result = place_direct_buy_ce(
        api,
        option
    )

    order_id = result[
        "order_id"
    ]

    # --------------------------------------------------------
    # SAVE EVERYTHING
    # --------------------------------------------------------

    state[
        "last_processed_candle"
    ] = candle_key

    state[
        "last_signal_candle"
    ] = candle_key

    state[
        "last_signal"
    ] = "GREEN FLIP"

    state[
        "last_direction"
    ] = "GREEN"

    state[
        "last_order_id"
    ] = order_id

    state[
        "last_order_time"
    ] = now_ist().strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    state[
        "last_symbol"
    ] = option["symbol"]

    state[
        "last_token"
    ] = option["token"]

    state[
        "last_strike"
    ] = option["strike"]

    state[
        "last_expiry"
    ] = option["expiry"]

    state[
        "last_quantity"
    ] = result[
        "quantity"
    ]

    state[
        "last_error"
    ] = None

    save_state(
        state
    )

    # --------------------------------------------------------
    # RETURN
    # --------------------------------------------------------

    return {

        "status":
            "BUY_SENT",

        "reason":
            "RED → GREEN FLIP",

        "spot":
            spot,

        "candle":
            candle_key,

        "option":
            option,

        "quantity":
            result["quantity"],

        "order_id":
            order_id,

        "df":
            df_2m,
    }


# ============================================================
# HEADER
# ============================================================

st.title(
    "📈 NIFTY Live Direct Automatic BUY CE"
)

st.caption(
    "Completed 2-Minute Supertrend (20, 1.5) "
    "→ RED → GREEN FLIP "
    "→ Automatic ATM NIFTY CE BUY"
)


# ============================================================
# STATUS CARDS
# ============================================================

col1, col2, col3, col4 = st.columns(
    4
)

col1.metric(
    "Trading",
    "LIVE"
    if LIVE_TRADING
    else "PAPER"
)

col2.metric(
    "Supertrend",
    "20 / 1.5"
)

col3.metric(
    "Order",
    "BUY CE"
)

col4.metric(
    "Refresh",
    f"{REFRESH_SECONDS}s"
)


# ============================================================
# LOGIN
# ============================================================

if not st.session_state.logged_in:

    st.warning(
        "Angel One login required."
    )

    if st.button(
        "LOGIN TO ANGEL ONE",
        type="primary"
    ):

        try:

            with st.spinner(
                "Logging in..."
            ):

                angel_login()

            st.success(
                "Angel One login successful."
            )

            st.rerun()

        except Exception as e:

            st.error(
                str(e)
            )

    st.stop()


# ============================================================
# MARKET STATUS
# ============================================================

if market_is_open():

    st.success(
        "🟢 NSE MARKET OPEN — AUTOMATIC BUY ENGINE ACTIVE"
    )

else:

    st.warning(
        "🔴 NSE MARKET CLOSED — NO AUTOMATIC ORDER WILL BE SENT"
    )


# ============================================================
# AUTOMATIC ENGINE
# ============================================================

if st.session_state.running:

    try:

        result = (
            process_automatic_buy()
        )

        status = result.get(
            "status",
            "WAIT"
        )

        if status == "BUY_SENT":

            option = result[
                "option"
            ]

            st.success(
                "🚀 AUTOMATIC BUY CE ORDER SENT"
            )

            a, b, c, d = st.columns(
                4
            )

            a.metric(
                "NIFTY",
                f"{result['spot']:.2f}"
            )

            b.metric(
                "ATM Strike",
                f"{option['strike']:.0f}"
            )

            c.metric(
                "CE",
                option["symbol"]
            )

            d.metric(
                "Order ID",
                result["order_id"]
            )

            st.info(
                f"Quantity: {result['quantity']} | "
                f"Expiry: {option['expiry']} | "
                f"Signal candle: {result['candle']}"
            )

        elif status == "MARKET_CLOSED":

            st.info(
                "Automatic trading is waiting for NSE market hours."
            )

        else:

            spot = result.get(
                "spot"
            )

            candle = result.get(
                "candle",
                "-"
            )

            direction = result.get(
                "direction",
                "-"
            )

            reason = result.get(
                "reason",
                "-"
            )

            if spot is not None:

                st.info(
                    f"Signal: WAIT | "
                    f"NIFTY: {spot:.2f} | "
                    f"2M candle: {candle} | "
                    f"Direction: {direction} | "
                    f"{reason}"
                )

            else:

                st.info(
                    f"Signal: WAIT | {reason}"
                )

    except Exception as e:

        st.session_state.state[
            "last_error"
        ] = str(e)

        save_state(
            st.session_state.state
        )

        st.error(
            f"Automatic BUY error: {e}"
        )


# ============================================================
# LAST AUTOMATIC BUY
# ============================================================

st.divider()

st.subheader(
    "Last Automatic BUY"
)

state = st.session_state.state

a, b, c, d = st.columns(
    4
)

a.metric(
    "Last Signal",
    state.get(
        "last_signal",
        "WAIT"
    )
)

b.metric(
    "Last CE",
    state.get(
        "last_symbol"
    )
    or "-"
)

c.metric(
    "Last Strike",
    (
        f"{float(state['last_strike']):.0f}"
        if state.get(
            "last_strike"
        )
        else "-"
    )
)

d.metric(
    "Order ID",
    state.get(
        "last_order_id"
    )
    or "-"
)


if state.get(
    "last_order_time"
):

    st.caption(
        "Order sent: "
        + str(
            state[
                "last_order_time"
            ]
        )
    )


if state.get(
    "last_quantity"
):

    st.caption(
        f"Quantity: {state['last_quantity']}"
    )


if state.get(
    "last_expiry"
):

    st.caption(
        f"Expiry: {state['last_expiry']}"
    )


if state.get(
    "last_error"
):

    st.error(
        "Last error: "
        + str(
            state[
                "last_error"
            ]
        )
    )


# ============================================================
# STRATEGY INFORMATION
# ============================================================

st.divider()

st.subheader(
    "Automatic Strategy"
)

st.write(
    "1️⃣ Live NIFTY 1-minute candles"
)

st.write(
    "2️⃣ Only completed 1-minute candles are used"
)

st.write(
    "3️⃣ Exactly two 1-minute candles are required "
    "for every 2-minute candle"
)

st.write(
    "4️⃣ Supertrend = Period 20, Multiplier 1.5"
)

st.write(
    "5️⃣ RED → GREEN flip is the BUY signal"
)

st.write(
    "6️⃣ Nearest ATM NIFTY CE is selected automatically"
)

st.write(
    "7️⃣ BUY MARKET order is sent automatically"
)

st.write(
    "8️⃣ Same completed 2-minute candle cannot "
    "generate another BUY"
)


# ============================================================
# AUTO REFRESH
# ============================================================

time.sleep(
    REFRESH_SECONDS
)

st.rerun()
