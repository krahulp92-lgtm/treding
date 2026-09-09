# ============================================================
# dashboard.py
#
# NIFTY 50 AUTOMATIC BUY CE ONLY
#
# STRATEGY
# ------------------------------------------------------------
# 2-Minute Supertrend (20, 1.5)
#
# GREEN = BUY CE
# RED   = WAIT
#
# NO GREEN FLIP REQUIRED
#
# FLOW
# ------------------------------------------------------------
# NIFTY SPOT
#     ↓
# 2-Minute candles
#     ↓
# Supertrend (20, 1.5)
#     ↓
# GREEN
#     ↓
# Nearest NIFTY expiry
#     ↓
# ATM NIFTY CE
#     ↓
# Automatic BUY 1 LOT
#     ↓
# Angel One Order Book
#
# ============================================================

import os
import json
import time
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
import pyotp
import pandas as pd
import numpy as np
import streamlit as st

from SmartApi import SmartConnect


# ============================================================
# CONFIG
# ============================================================

IST = ZoneInfo("Asia/Kolkata")

NIFTY_TOKEN = "99926000"
NIFTY_SYMBOL = "NIFTY 50"

SUPERTRREND_PERIOD = 20
SUPERTREND_MULTIPLIER = 1.5

# ------------------------------------------------------------
# SAFETY
# ------------------------------------------------------------
# TRUE  = NO REAL BROKER ORDER
# FALSE = REAL BUY ORDER
#
# Change to False ONLY after checking everything.
# ------------------------------------------------------------

PAPER_TRADING = (
    os.getenv("PAPER_TRADING", "true").lower()
    == "false"
)

# Instrument master
INSTRUMENT_URL = (
    "https://margincalculator.angelone.in/"
    "OpenAPI_File/files/OpenAPIScripMaster.json"
)

INSTRUMENT_FILE = Path(
    "OpenAPIScripMaster.json"
)

STATE_FILE = Path(
    "nifty_2min_ce_state.json"
)

REFRESH_SECONDS = 20


# ============================================================
# PAGE
# ============================================================

st.set_page_config(
    page_title="NIFTY 2-Minute CE Auto Trader",
    page_icon="📈",
    layout="wide",
)


# ============================================================
# SESSION STATE
# ============================================================

DEFAULTS = {
    "api": None,
    "login_status": "NOT CONNECTED",
    "last_error": "",
    "last_message": "Ready",

    "instruments": None,

    "spot": None,

    "candles_2m": None,
    "supertrend_df": None,

    "st_value": None,
    "st_green": False,
    "signal": "WAIT",
    "signal_time": None,

    "selected_option": None,

    "last_order_id": None,
    "last_order_time": None,
    "last_order_candle": None,

    "paper_orders": [],

    "last_refresh": None,
}


for key, value in DEFAULTS.items():

    if key not in st.session_state:
        st.session_state[key] = value


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
# UTILITY
# ============================================================

def now_ist():
    return datetime.now(IST)


def log_error(message):
    st.session_state["last_error"] = str(message)


# ============================================================
# TOTP
# ============================================================

def get_totp_secret(raw_secret):

    raw_secret = str(raw_secret).strip()

    if not raw_secret:
        raise RuntimeError(
            "ANGEL_TOTP_SECRET is empty."
        )

    # If user pasted otpauth URI
    if raw_secret.lower().startswith(
        "otpauth://"
    ):

        try:
            from urllib.parse import urlparse, parse_qs

            parsed = urlparse(raw_secret)

            query = parse_qs(
                parsed.query
            )

            secret = query.get(
                "secret",
                [""]
            )[0]

            if not secret:
                raise RuntimeError(
                    "TOTP secret missing from otpauth URI."
                )

            raw_secret = secret

        except Exception as exc:

            raise RuntimeError(
                f"Invalid TOTP URI: {exc}"
            )

    # Remove spaces
    raw_secret = (
        raw_secret
        .replace(" ", "")
        .replace("-", "")
        .strip()
        .upper()
    )

    # This catches common mistake
    if raw_secret.isdigit() and len(raw_secret) == 6:

        raise RuntimeError(
            "ANGEL_TOTP_SECRET contains the 6-digit OTP. "
            "Use the Base32 TOTP secret instead."
        )

    return raw_secret


def generate_totp():

    secret = get_totp_secret(
        ANGEL_TOTP_SECRET
    )

    try:

        return pyotp.TOTP(
            secret
        ).now()

    except Exception as exc:

        raise RuntimeError(
            f"Unable to generate TOTP: {exc}"
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

        raise RuntimeError(
            "Missing environment variables: "
            + ", ".join(missing)
        )


# ============================================================
# ANGEL ONE LOGIN
# ============================================================

def angel_login():

    credentials_ok()

    try:

        api = SmartConnect(
            api_key=ANGEL_API_KEY
        )

        totp = generate_totp()

        response = api.generateSession(
            ANGEL_CLIENT_ID,
            ANGEL_PASSWORD,
            totp
        )

        if not response:

            raise RuntimeError(
                "Angel One returned empty login response."
            )

        if response.get("status") is False:

            raise RuntimeError(
                "Angel One login failed: "
                + str(response)
            )

        st.session_state["api"] = api

        st.session_state[
            "login_status"
        ] = "CONNECTED"

        st.session_state[
            "last_message"
        ] = "Angel One login successful."

        return api

    except Exception as exc:

        st.session_state[
            "login_status"
        ] = "LOGIN FAILED"

        raise RuntimeError(
            f"Angel One login failed: {exc}"
        )


# ============================================================
# INSTRUMENT MASTER
# ============================================================

@st.cache_data(ttl=3600)
def download_instrument_master():

    try:

        response = requests.get(
            INSTRUMENT_URL,
            timeout=30
        )

        response.raise_for_status()

        data = response.json()

        if not isinstance(data, list):

            raise RuntimeError(
                "Instrument master format is invalid."
            )

        return data

    except Exception as exc:

        # Try local file
        if INSTRUMENT_FILE.exists():

            try:

                with open(
                    INSTRUMENT_FILE,
                    "r",
                    encoding="utf-8"
                ) as f:

                    data = json.load(f)

                if isinstance(data, list):
                    return data

            except Exception:
                pass

        raise RuntimeError(
            f"Instrument master download failed: {exc}"
        )


# ============================================================
# NIFTY LTP
# ============================================================

def get_nifty_ltp(api):

    if api is None:

        raise RuntimeError(
            "Angel One API not connected."
        )

    try:

        response = api.ltpData(
            "NSE",
            NIFTY_SYMBOL,
            NIFTY_TOKEN
        )

        if not response:

            raise RuntimeError(
                "NIFTY LTP returned empty response."
            )

        if response.get("status") is False:

            raise RuntimeError(
                "NIFTY LTP failed: "
                + str(response)
            )

        data = response.get("data")

        if not data:

            raise RuntimeError(
                "NIFTY LTP response has no data."
            )

        ltp = data.get("ltp")

        if ltp is None:

            raise RuntimeError(
                "NIFTY LTP missing."
            )

        return float(ltp)

    except Exception as exc:

        raise RuntimeError(
            f"NIFTY LTP FAILED: {exc}"
        )


# ============================================================
# CANDLE API
# ============================================================

def get_nifty_1m_candles(
    api,
    days=5
):

    if api is None:

        raise RuntimeError(
            "Angel One API not connected."
        )

    end_time = now_ist()
    start_time = (
        end_time
        - timedelta(days=days)
    )

    params = {
        "exchange": "NSE",
        "symboltoken": NIFTY_TOKEN,
        "interval": "ONE_MINUTE",
        "fromdate": start_time.strftime(
            "%Y-%m-%d %H:%M"
        ),
        "todate": end_time.strftime(
            "%Y-%m-%d %H:%M"
        ),
    }

    try:

        response = api.getCandleData(
            params
        )

        if not response:

            raise RuntimeError(
                "Candle API returned empty response."
            )

        if response.get("status") is False:

            raise RuntimeError(
                "Candle API failed: "
                + str(response)
            )

        data = response.get("data")

        if not data:

            raise RuntimeError(
                "Candle API returned no candle data."
            )

        rows = []

        for row in data:

            if len(row) < 6:
                continue

            rows.append({
                "datetime": row[0],
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "volume": float(row[5]),
            })

        df = pd.DataFrame(rows)

        if df.empty:

            raise RuntimeError(
                "No NIFTY candles received."
            )

        df["datetime"] = pd.to_datetime(
            df["datetime"],
            errors="coerce"
        )

        # Convert timezone
        if df["datetime"].dt.tz is None:

            df["datetime"] = (
                df["datetime"]
                .dt.tz_localize(IST)
            )

        else:

            df["datetime"] = (
                df["datetime"]
                .dt.tz_convert(IST)
            )

        df = df.dropna(
            subset=["datetime"]
        )

        df = df.sort_values(
            "datetime"
        )

        df = df.drop_duplicates(
            "datetime"
        )

        df = df.set_index(
            "datetime"
        )

        return df

    except Exception as exc:

        raise RuntimeError(
            f"Candle API failed: {exc}"
        )


# ============================================================
# RESAMPLE 1-MIN → 2-MIN
# ============================================================

def resample_to_2min(df):

    if df is None or df.empty:

        raise RuntimeError(
            "1-minute candle dataframe is empty."
        )

    result = (
        df[
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
            closed="left"
        )
        .agg({
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum",
        })
        .dropna()
    )

    return result


# ============================================================
# LAST CLOSED 2-MIN CANDLE
# ============================================================

def get_last_closed_2min(df):

    if df is None or df.empty:

        return None

    current = now_ist()

    # Market starts 09:15.
    # Candle buckets:
    #
    # 09:15 -> 09:17
    # 09:17 -> 09:19
    # etc.
    #
    # Current forming bucket is excluded.

    minutes_from_open = (
        current.hour * 60
        + current.minute
        - (9 * 60 + 15)
    )

    if minutes_from_open < 0:

        return None

    bucket_number = (
        minutes_from_open // 2
    )

    bucket_start = (
        current.replace(
            hour=9,
            minute=15,
            second=0,
            microsecond=0
        )
        + timedelta(
            minutes=bucket_number * 2
        )
    )

    bucket_end = (
        bucket_start
        + timedelta(minutes=2)
    )

    # Because our resampled candle is labelled
    # at the right side, its timestamp is bucket_end.

    closed = df[
        df.index <= bucket_end
    ]

    if closed.empty:

        return None

    # Do not use a candle whose right edge
    # is still in the future.
    closed = closed[
        closed.index <= current
    ]

    if closed.empty:

        return None

    return closed.iloc[-1]


# ============================================================
# SUPERTREND
# ============================================================

def calculate_supertrend(
    df,
    period=20,
    multiplier=1.5
):

    data = df.copy()

    if len(data) < period + 5:

        raise RuntimeError(
            f"Not enough candles for "
            f"Supertrend {period},{multiplier}."
        )

    high = data["high"]
    low = data["low"]
    close = data["close"]

    # True Range
    previous_close = close.shift(1)

    tr1 = high - low

    tr2 = (
        high - previous_close
    ).abs()

    tr3 = (
        low - previous_close
    ).abs()

    tr = pd.concat(
        [tr1, tr2, tr3],
        axis=1
    ).max(axis=1)

    # Wilder ATR
    atr = tr.ewm(
        alpha=1 / period,
        adjust=False,
        min_periods=period
    ).mean()

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
        index=data.index,
        dtype=float
    )

    final_lower = pd.Series(
        index=data.index,
        dtype=float
    )

    supertrend = pd.Series(
        index=data.index,
        dtype=float
    )

    direction = pd.Series(
        index=data.index,
        dtype=int
    )

    for i in range(len(data)):

        if i == 0:

            final_upper.iloc[i] = (
                basic_upper.iloc[i]
            )

            final_lower.iloc[i] = (
                basic_lower.iloc[i]
            )

            supertrend.iloc[i] = np.nan
            direction.iloc[i] = 1

            continue

        prev_fu = (
            final_upper.iloc[i - 1]
        )

        prev_fl = (
            final_lower.iloc[i - 1]
        )

        prev_close = (
            close.iloc[i - 1]
        )

        bu = basic_upper.iloc[i]
        bl = basic_lower.iloc[i]

        # Final upper band
        if (
            pd.isna(prev_fu)
            or bu < prev_fu
            or prev_close > prev_fu
        ):

            final_upper.iloc[i] = bu

        else:

            final_upper.iloc[i] = prev_fu

        # Final lower band
        if (
            pd.isna(prev_fl)
            or bl > prev_fl
            or prev_close < prev_fl
        ):

            final_lower.iloc[i] = bl

        else:

            final_lower.iloc[i] = prev_fl

        # Direction
        if pd.isna(
            supertrend.iloc[i - 1]
        ):

            direction.iloc[i] = 1

            supertrend.iloc[i] = (
                final_lower.iloc[i]
            )

        else:

            previous_st = (
                supertrend.iloc[i - 1]
            )

            if previous_st == prev_fu:

                if close.iloc[i] <= (
                    final_upper.iloc[i]
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

                if close.iloc[i] >= (
                    final_lower.iloc[i]
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
        (direction == 1)
        & (direction.shift(1) == -1)
    )

    data["ST_Flip_Red"] = (
        (direction == -1)
        & (direction.shift(1) == 1)
    )

    return data


# ============================================================
# SELECT ATM NIFTY CE
# ============================================================

def select_atm_nifty_ce(
    instruments,
    spot
):

    if not instruments:

        raise RuntimeError(
            "Instrument master is empty."
        )

    if spot is None:

        raise RuntimeError(
            "NIFTY spot price unavailable."
        )

    df = pd.DataFrame(
        instruments
    ).copy()

    # Normalize column names
    df.columns = [
        str(c).strip().lower()
        for c in df.columns
    ]

    # Handle possible field variations
    rename_map = {}

    if (
        "token" in df.columns
        and "symboltoken"
        not in df.columns
    ):

        rename_map["token"] = (
            "symboltoken"
        )

    if (
        "symbol" in df.columns
        and "tradingsymbol"
        not in df.columns
    ):

        rename_map["symbol"] = (
            "tradingsymbol"
        )

    df = df.rename(
        columns=rename_map
    )

    required = [
        "symboltoken",
        "tradingsymbol",
        "expiry",
        "strike",
        "lotsize",
        "instrumenttype",
        "exch_seg",
    ]

    missing = [
        col
        for col in required
        if col not in df.columns
    ]

    if missing:

        raise RuntimeError(
            "Instrument master missing columns: "
            + str(missing)
        )

    # --------------------------------------------------------
    # NFO
    # --------------------------------------------------------

    df = df[
        df["exch_seg"]
        .astype(str)
        .str.upper()
        .eq("NFO")
    ].copy()

    # --------------------------------------------------------
    # OPTIDX
    # --------------------------------------------------------

    df = df[
        df["instrumenttype"]
        .astype(str)
        .str.upper()
        .eq("OPTIDX")
    ].copy()

    # --------------------------------------------------------
    # NIFTY
    # --------------------------------------------------------

    df = df[
        df["tradingsymbol"]
        .astype(str)
        .str.upper()
        .str.startswith("NIFTY")
    ].copy()

    # --------------------------------------------------------
    # CE ONLY
    # --------------------------------------------------------

    df = df[
        df["tradingsymbol"]
        .astype(str)
        .str.upper()
        .str.endswith("CE")
    ].copy()

    if df.empty:

        raise RuntimeError(
            "No NIFTY CE contracts found."
        )

    # --------------------------------------------------------
    # STRIKE
    # --------------------------------------------------------

    df["strike_raw"] = pd.to_numeric(
        df["strike"],
        errors="coerce"
    )

    df = df[
        df["strike_raw"].notna()
    ].copy()

    # Angel One generally stores strike × 100
    df["strike_actual"] = (
        df["strike_raw"] / 100.0
    )

    # --------------------------------------------------------
    # EXPIRY
    # --------------------------------------------------------

    df["expiry_date"] = pd.to_datetime(
        df["expiry"],
        errors="coerce"
    )

    df = df[
        df["expiry_date"].notna()
    ].copy()

    today = pd.Timestamp(
        now_ist().date()
    )

    df = df[
        df["expiry_date"].dt.normalize()
        >= today
    ].copy()

    if df.empty:

        raise RuntimeError(
            "No active NIFTY CE expiry found."
        )

    # --------------------------------------------------------
    # NEAREST EXPIRY
    # --------------------------------------------------------

    nearest_expiry = (
        df["expiry_date"]
        .dt.normalize()
        .min()
    )

    df = df[
        df["expiry_date"]
        .dt.normalize()
        == nearest_expiry
    ].copy()

    if df.empty:

        raise RuntimeError(
            "No NIFTY CE contracts "
            "for nearest expiry."
        )

    # --------------------------------------------------------
    # ATM
    # --------------------------------------------------------

    spot = float(spot)

    df["distance"] = (
        df["strike_actual"]
        - spot
    ).abs()

    selected = (
        df.sort_values(
            [
                "distance",
                "strike_actual"
            ]
        )
        .iloc[0]
    )

    option = {
        "symbol": str(
            selected["tradingsymbol"]
        ).strip(),

        "token": str(
            selected["symboltoken"]
        ).strip(),

        "strike": float(
            selected["strike_actual"]
        ),

        "expiry": (
            pd.Timestamp(
                selected["expiry_date"]
            )
            .strftime("%d-%b-%Y")
        ),

        "lot_size": int(
            float(selected["lotsize"])
        ),
    }

    return option


# ============================================================
# ORDER BOOK
# ============================================================

def get_order_book(api):

    if api is None:
        return []

    try:

        response = api.orderBook()

        if not response:
            return []

        if (
            isinstance(response, dict)
            and response.get("status") is False
        ):

            return []

        data = response.get(
            "data"
        )

        if isinstance(data, list):
            return data

        return []

    except Exception as exc:

        st.warning(
            f"Order Book error: {exc}"
        )

        return []


# ============================================================
# FIND ORDER IN ORDER BOOK
# ============================================================

def find_order_in_book(
    api,
    symbol=None
):

    orders = get_order_book(
        api
    )

    if not orders:
        return None

    for order in orders:

        if not isinstance(
            order,
            dict
        ):
            continue

        if symbol:

            order_symbol = str(
                order.get(
                    "tradingsymbol",
                    ""
                )
            ).strip()

            if (
                order_symbol
                != str(symbol).strip()
            ):
                continue

        return order

    return None


# ============================================================
# PAPER ORDER
# ============================================================

def create_paper_order(
    option,
    spot
):

    order_id = (
        "PAPER-"
        + now_ist().strftime(
            "%Y%m%d%H%M%S"
        )
    )

    order = {
        "orderid": order_id,
        "tradingsymbol": option[
            "symbol"
        ],
        "symboltoken": option[
            "token"
        ],
        "transactiontype": "BUY",
        "exchange": "NFO",
        "ordertype": "MARKET",
        "producttype": "INTRADAY",
        "quantity": str(
            option["lot_size"]
        ),
        "status": "PAPER ORDER",
        "spot": spot,
        "time": now_ist().strftime(
            "%Y-%m-%d %H:%M:%S"
        ),
    }

    st.session_state[
        "paper_orders"
    ].insert(
        0,
        order
    )

    return order_id


# ============================================================
# REAL BUY ORDER
# ============================================================

def place_real_buy_order(
    api,
    option
):

    if api is None:

        raise RuntimeError(
            "Angel One API is not connected."
        )

    order_params = {
        "variety": "NORMAL",
        "tradingsymbol": option[
            "symbol"
        ],
        "symboltoken": option[
            "token"
        ],
        "transactiontype": "BUY",
        "exchange": "NFO",
        "ordertype": "MARKET",
        "producttype": "INTRADAY",
        "duration": "DAY",
        "price": "0",
        "squareoff": "0",
        "stoploss": "0",
        "quantity": str(
            option["lot_size"]
        ),
    }

    # ========================================================
    # FIRST ATTEMPT
    # ========================================================

    try:

        if hasattr(
            api,
            "placeOrderFullResponse"
        ):

            response = (
                api.placeOrderFullResponse(
                    order_params
                )
            )

        else:

            order_id = api.placeOrder(
                order_params
            )

            if order_id:

                return str(
                    order_id
                )

            response = None

    except Exception as exc:

        raise RuntimeError(
            f"Angel One order exception: {exc}"
        )

    # ========================================================
    # NORMAL RESPONSE
    # ========================================================

    if response:

        if isinstance(
            response,
            dict
        ):

            if response.get(
                "status"
            ) is False:

                raise RuntimeError(
                    "Order rejected: "
                    + str(response)
                )

            data = response.get(
                "data"
            )

            if isinstance(
                data,
                dict
            ):

                order_id = (
                    data.get("orderid")
                    or data.get("orderId")
                )

                if order_id:

                    return str(
                        order_id
                    )

        if isinstance(
            response,
            str
        ):

            return response

    # ========================================================
    # EMPTY RESPONSE
    #
    # DO NOT RETRY IMMEDIATELY.
    #
    # Check Order Book first.
    # ========================================================

    time.sleep(2)

    existing = find_order_in_book(
        api,
        option["symbol"]
    )

    if existing:

        order_id = (
            existing.get("orderid")
            or existing.get("orderId")
        )

        if order_id:

            return str(
                order_id
            )

    raise RuntimeError(
        "Angel One returned an empty order response "
        "and no matching order was found in Order Book. "
        "No automatic retry was performed."
    )


# ============================================================
# AUTOMATIC BUY LOGIC
# ============================================================

def automatic_buy_ce(
    api,
    instruments,
    spot,
    candle_time
):

    # --------------------------------------------------------
    # Duplicate protection
    # --------------------------------------------------------

    last_order_id = (
        st.session_state.get(
            "last_order_id"
        )
    )

    if last_order_id:

        return {
            "status": "ALREADY_ORDERED",
            "order_id": last_order_id,
        }

    last_candle = (
        st.session_state.get(
            "last_order_candle"
        )
    )

    candle_key = str(
        candle_time
    )

    if last_candle == candle_key:

        return {
            "status": "ALREADY_PROCESSED",
            "order_id": None,
        }

    # --------------------------------------------------------
    # SELECT ATM CE
    # --------------------------------------------------------

    option = select_atm_nifty_ce(
        instruments,
        spot
    )

    st.session_state[
        "selected_option"
    ] = option

    # --------------------------------------------------------
    # PAPER MODE
    # --------------------------------------------------------

    if PAPER_TRADING:

        order_id = create_paper_order(
            option,
            spot
        )

    # --------------------------------------------------------
    # REAL MODE
    # --------------------------------------------------------

    else:

        order_id = place_real_buy_order(
            api,
            option
        )

    # --------------------------------------------------------
    # SAVE STATE ONLY AFTER ORDER CONFIRMED
    # --------------------------------------------------------

    st.session_state[
        "last_order_id"
    ] = order_id

    st.session_state[
        "last_order_time"
    ] = now_ist().strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    st.session_state[
        "last_order_candle"
    ] = candle_key

    return {
        "status": "ORDER_PLACED",
        "order_id": order_id,
        "option": option,
    }


# ============================================================
# STATE LOAD
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

            state = json.load(f)

        st.session_state[
            "last_order_id"
        ] = state.get(
            "last_order_id"
        )

        st.session_state[
            "last_order_time"
        ] = state.get(
            "last_order_time"
        )

        st.session_state[
            "last_order_candle"
        ] = state.get(
            "last_order_candle"
        )

        st.session_state[
            "selected_option"
        ] = state.get(
            "selected_option"
        )

    except Exception:
        pass


# ============================================================
# STATE SAVE
# ============================================================

def save_state():

    state = {
        "last_order_id":
            st.session_state.get(
                "last_order_id"
            ),

        "last_order_time":
            st.session_state.get(
                "last_order_time"
            ),

        "last_order_candle":
            st.session_state.get(
                "last_order_candle"
            ),

        "selected_option":
            st.session_state.get(
                "selected_option"
            ),
    }

    try:

        with open(
            STATE_FILE,
            "w",
            encoding="utf-8"
        ) as f:

            json.dump(
                state,
                f,
                indent=2
            )

    except Exception as exc:

        st.warning(
            f"Could not save state: {exc}"
        )


# ============================================================
# MARKET HOURS
# ============================================================

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

    return (
        start <= now <= end
    )


# ============================================================
# LOAD STATE
# ============================================================

if (
    "state_loaded"
    not in st.session_state
):

    load_state()

    st.session_state[
        "state_loaded"
    ] = True


# ============================================================
# HEADER
# ============================================================

st.title(
    "📈 NIFTY 2-Minute Automatic CE Trader"
)

st.caption(
    "ONLY 2-Minute Supertrend (20, 1.5)"
)

if PAPER_TRADING:

    st.warning(
        "PAPER TRADING MODE — NO REAL ORDER WILL BE SENT"
    )

else:

    st.error(
        "LIVE TRADING ENABLED — REAL NIFTY CE ORDERS CAN BE PLACED"
    )


# ============================================================
# SIDEBAR
# ============================================================

with st.sidebar:

    st.header("System")

    st.write(
        "Login:",
        st.session_state[
            "login_status"
        ]
    )

    st.write(
        "Market:",
        "OPEN"
        if market_is_open()
        else "CLOSED"
    )

    st.write(
        "Strategy:",
        "Supertrend 20, 1.5"
    )

    st.write(
        "Timeframe:",
        "2 Minute"
    )

    st.write(
        "Signal:",
        "GREEN = BUY CE"
    )

    st.write(
        "Trading:",
        "PAPER"
        if PAPER_TRADING
        else "LIVE"
    )

    if st.button(
        "Connect Angel One"
    ):

        try:

            angel_login()

            st.success(
                "Angel One connected."
            )

        except Exception as exc:

            log_error(exc)

            st.error(
                str(exc)
            )


# ============================================================
# AUTO LOGIN
# ============================================================

if (
    st.session_state["api"]
    is None
):

    if (
        ANGEL_API_KEY
        and ANGEL_CLIENT_ID
        and ANGEL_PASSWORD
        and ANGEL_TOTP_SECRET
    ):

        try:

            angel_login()

        except Exception as exc:

            log_error(exc)


# ============================================================
# MAIN
# ============================================================

api = st.session_state[
    "api"
]


if api is None:

    st.error(
        "Angel One is not connected."
    )

    st.info(
        "Set the four ANGEL_* environment variables "
        "and connect."
    )

    if st.session_state[
        "last_error"
    ]:

        st.code(
            st.session_state[
                "last_error"
            ]
        )

    st.stop()


# ============================================================
# INSTRUMENT MASTER
# ============================================================

if (
    st.session_state[
        "instruments"
    ]
    is None
):

    try:

        st.session_state[
            "instruments"
        ] = download_instrument_master()

    except Exception as exc:

        st.error(
            str(exc)
        )

        st.stop()


instruments = st.session_state[
    "instruments"
]


# ============================================================
# GET NIFTY SPOT
# ============================================================

try:

    spot = get_nifty_ltp(
        api
    )

    st.session_state[
        "spot"
    ] = spot

except Exception as exc:

    st.error(
        str(exc)
    )

    st.stop()


# ============================================================
# GET 1-MIN DATA
# ============================================================

try:

    df1 = get_nifty_1m_candles(
        api,
        days=5
    )

    df2 = resample_to_2min(
        df1
    )

except Exception as exc:

    st.error(
        str(exc)
    )

    st.stop()


st.session_state[
    "candles_2m"
] = df2


# ============================================================
# SUPERTREND
# ============================================================

try:

    st_df = calculate_supertrend(
        df2,
        period=20,
        multiplier=1.5
    )

except Exception as exc:

    st.error(
        str(exc)
    )

    st.stop()


st.session_state[
    "supertrend_df"
] = st_df


# ============================================================
# LAST CLOSED CANDLE
# ============================================================

last_candle = get_last_closed_2min(
    st_df
)

if last_candle is None:

    st.warning(
        "Waiting for a closed 2-minute candle."
    )

    st.stop()


# Get timestamp of last candle
last_candle_time = st_df.index[-1]

# Find actual last closed candle
closed_df = st_df[
    st_df.index <= now_ist()
]

if closed_df.empty:

    st.warning(
        "No closed candle available."
    )

    st.stop()

last_closed_time = (
    closed_df.index[-1]
)

last_row = closed_df.iloc[-1]


# ============================================================
# SIGNAL
# ============================================================

st_green = bool(
    last_row["ST_Green"]
)

if st_green:

    signal = "BUY CE"

else:

    signal = "WAIT"


st.session_state[
    "st_green"
] = st_green

st.session_state[
    "st_value"
] = float(
    last_row["Supertrend"]
)

st.session_state[
    "signal"
] = signal

st.session_state[
    "signal_time"
] = str(
    last_closed_time
)


# ============================================================
# TOP METRICS
# ============================================================

c1, c2, c3, c4, c5 = st.columns(5)

c1.metric(
    "NIFTY Spot",
    f"{spot:.2f}"
)

c2.metric(
    "Supertrend",
    f"{float(last_row['Supertrend']):.2f}"
)

c3.metric(
    "ST Status",
    "GREEN"
    if st_green
    else "RED"
)

c4.metric(
    "Signal",
    signal
)

c5.metric(
    "Candle",
    last_closed_time.strftime(
        "%H:%M:%S"
    )
)


# ============================================================
# AUTOMATIC BUY
# ============================================================

if signal == "BUY CE":

    st.success(
        "🟢 SUPERTREND GREEN → BUY CE SIGNAL"
    )

    try:

        result = automatic_buy_ce(
            api,
            instruments,
            spot,
            last_closed_time
        )

        if result["status"] == "ORDER_PLACED":

            st.success(
                "✅ AUTOMATIC BUY ORDER PLACED"
            )

            st.write(
                "Order ID:",
                result["order_id"]
            )

            option = result.get(
                "option"
            )

            if option:

                st.write(
                    "ATM CE:",
                    option["symbol"]
                )

        elif result[
            "status"
        ] == "ALREADY_ORDERED":

            st.info(
                "Duplicate protection active. "
                "Existing order: "
                + str(
                    result["order_id"]
                )
            )

        elif result[
            "status"
        ] == "ALREADY_PROCESSED":

            st.info(
                "This candle has already been processed."
            )

    except Exception as exc:

        log_error(exc)

        st.error(
            "Automatic BUY CE error: "
            + str(exc)
        )

else:

    st.info(
        "🔴 Supertrend is RED → WAIT"
    )


# ============================================================
# SELECTED ATM CE
# ============================================================

st.divider()

st.subheader(
    "🎯 Selected ATM NIFTY CE"
)

option = st.session_state.get(
    "selected_option"
)

if option:

    a1, a2, a3, a4 = st.columns(4)

    a1.metric(
        "Trading Symbol",
        option["symbol"]
    )

    a2.metric(
        "ATM Strike",
        f"{option['strike']:.0f}"
    )

    a3.metric(
        "Expiry",
        option["expiry"]
    )

    a4.metric(
        "Lot Size",
        option["lot_size"]
    )

    st.caption(
        f"Symbol Token: {option['token']}"
    )

else:

    st.info(
        "ATM NIFTY CE will be selected automatically "
        "when the signal becomes GREEN."
    )


# ============================================================
# ORDER STATUS
# ============================================================

st.divider()

st.subheader(
    "📦 Automatic Order Status"
)

o1, o2, o3 = st.columns(3)

o1.metric(
    "Last Order ID",
    str(
        st.session_state.get(
            "last_order_id"
        )
        or "-"
    )
)

o2.metric(
    "Order Time",
    str(
        st.session_state.get(
            "last_order_time"
        )
        or "-"
    )
)

o3.metric(
    "Mode",
    "PAPER"
    if PAPER_TRADING
    else "LIVE"
)


# ============================================================
# ANGEL ONE ORDER BOOK
# ============================================================

st.divider()

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

    # Put important columns first
    preferred = [
        "orderid",
        "tradingsymbol",
        "symboltoken",
        "transactiontype",
        "exchange",
        "ordertype",
        "producttype",
        "quantity",
        "price",
        "averageprice",
        "orderstatus",
        "text",
        "updatetime",
    ]

    existing = [
        c
        for c in preferred
        if c in order_df.columns
    ]

    remaining = [
        c
        for c in order_df.columns
        if c not in existing
    ]

    order_df = order_df[
        existing + remaining
    ]

    st.dataframe(
        order_df,
        use_container_width=True,
        hide_index=True
    )

else:

    st.info(
        "No Angel One orders found."
    )


# ============================================================
# PAPER ORDER BOOK
# ============================================================

if PAPER_TRADING:

    st.divider()

    st.subheader(
        "🧪 Paper Order Book"
    )

    paper_orders = (
        st.session_state[
            "paper_orders"
        ]
    )

    if paper_orders:

        st.dataframe(
            pd.DataFrame(
                paper_orders
            ),
            use_container_width=True,
            hide_index=True
        )

    else:

        st.info(
            "No paper order yet."
        )


# ============================================================
# RECENT CANDLES
# ============================================================

st.divider()

st.subheader(
    "📊 Recent 2-Minute Supertrend"
)

display_df = st_df[
    [
        "open",
        "high",
        "low",
        "close",
        "Supertrend",
        "ST_Green",
        "ST_Flip_Green",
        "ST_Flip_Red",
    ]
].tail(20).copy()

display_df["Signal"] = np.where(
    display_df["ST_Green"],
    "BUY CE",
    "WAIT"
)

st.dataframe(
    display_df,
    use_container_width=True
)


# ============================================================
# SAVE STATE
# ============================================================

save_state()


# ============================================================
# FOOTER
# ============================================================

st.caption(
    f"Last update: "
    f"{now_ist().strftime('%Y-%m-%d %H:%M:%S IST')}"
)

st.caption(
    "Strategy: 2-Minute Supertrend (20, 1.5) | "
    "GREEN = BUY ATM NIFTY CE | "
    "No GREEN FLIP required"
)


# ============================================================
# AUTO REFRESH
# ============================================================

time.sleep(
    REFRESH_SECONDS
)

st.rerun()
