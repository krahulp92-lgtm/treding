# ============================================================
# dashboard.py
# NIFTY LIVE SUPERTREND 20,2 - ANGEL ONE SMARTAPI
#
# FEATURES
# ------------------------------------------------------------
# NIFTY Spot
# 5 Minute Supertrend 20,2
# 15 Minute confirmation
# 4 Hour confirmation
# ATM CE / PE selection
# CE / PE LTP
# ALWAYS visible BUY CE / BUY PE
# Local instrument-master cache
# Duplicate BUY protection
# Real Angel One order placement
# Automatic Order Book refresh
# Automatic switch to Order Book after BUY
# Highlight newly placed order
#
# IMPORTANT:
# LIVE_TRADING = False -> NO REAL ORDER
# LIVE_TRADING = True  -> REAL ORDER
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
# PAGE CONFIG
# ============================================================

st.set_page_config(
    page_title="NIFTY Live Supertrend",
    page_icon="📈",
    layout="wide"
)


# ============================================================
# CONFIG
# ============================================================

IST = ZoneInfo("Asia/Kolkata")

# ============================================================
# SAFETY SWITCH
# ============================================================

# FALSE = PAPER MODE
# TRUE  = REAL ANGEL ONE ORDERS

LIVE_TRADING = True

# Automatic strategy trading
AUTO_TRADE = True

# Duplicate protection
DUPLICATE_PROTECTION = True


# ============================================================
# STRATEGY SETTINGS
# ============================================================

ST_PERIOD = 20
ST_MULTIPLIER = 2.0

# NIFTY 50 SmartAPI token
NIFTY_EXCHANGE = "NSE"
NIFTY_SYMBOL = "NIFTY"
NIFTY_TOKEN = "99926000"


# ============================================================
# LOCAL FILES
# ============================================================

DATA_DIR = Path("data")

DATA_DIR.mkdir(
    parents=True,
    exist_ok=True
)

INSTRUMENT_FILE = (
    DATA_DIR /
    "OpenAPIScripMaster.json"
)

STATE_FILE = (
    DATA_DIR /
    "trading_state.json"
)


# ============================================================
# ANGEL ONE INSTRUMENT MASTER
# ============================================================

INSTRUMENT_URL = (
    "https://margincalculator.angelone.in/"
    "OpenAPI_File/files/"
    "OpenAPIScripMaster.json"
)

INSTRUMENT_CACHE_HOURS = 24


# ============================================================
# ORDER SETTINGS
# ============================================================

ORDER_VARIETY = "NORMAL"
ORDER_TYPE = "MARKET"

# F&O normal product
NFO_PRODUCT_TYPE = "CARRYFORWARD"

ORDER_DURATION = "DAY"

ORDER_TAG = "NIFTYST202"


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

    "instruments": None,

    "login_status": "NOT CONNECTED",

    "last_error": "",

    "last_message": "Ready",

    "spot": None,

    "st5": None,

    "st15": None,

    "st4h": None,

    "signal": "WAIT",

    "signal_time": None,

    "ce_option": None,

    "pe_option": None,

    "ce_ltp": None,

    "pe_ltp": None,

    "order_book": [],

    "positions": [],

    "selected_section": "Dashboard",

    "highlight_order_id": None,

    "highlight_symbol": None,

    "last_order_id": None,

    "last_order_time": None,
}


for key, value in DEFAULTS.items():

    if key not in st.session_state:

        st.session_state[key] = value


# ============================================================
# BASIC HELPERS
# ============================================================

def now_ist():

    return datetime.now(IST)


def fmt_number(
    value,
    decimals=2
):

    if value is None:
        return "-"

    try:

        return f"{float(value):.{decimals}f}"

    except Exception:

        return "-"


def credentials_ok():

    return all([
        ANGEL_API_KEY,
        ANGEL_CLIENT_ID,
        ANGEL_PASSWORD,
        ANGEL_TOTP_SECRET
    ])


# ============================================================
# TOTP
# ============================================================

def get_totp_secret(raw_secret):

    raw_secret = raw_secret.strip()

    if raw_secret.startswith(
        "otpauth://"
    ):

        from urllib.parse import (
            urlparse,
            parse_qs
        )

        parsed = urlparse(
            raw_secret
        )

        params = parse_qs(
            parsed.query
        )

        secret = params.get(
            "secret",
            [None]
        )[0]

        if not secret:

            raise RuntimeError(
                "TOTP secret missing "
                "from otpauth URI."
            )

        return (
            secret
            .replace(" ", "")
            .upper()
        )

    return (
        raw_secret
        .replace(" ", "")
        .upper()
    )


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
# ANGEL ONE LOGIN
# ============================================================

def angel_login():

    if not credentials_ok():

        raise RuntimeError(
            "Missing Angel One credentials."
        )

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

    if not response.get("status"):

        raise RuntimeError(
            "Angel One login failed:\n"
            + str(response)
        )

    st.session_state.api = api

    st.session_state.login_status = (
        "CONNECTED"
    )

    st.session_state.last_error = ""

    return api


# ============================================================
# INSTRUMENT MASTER
# ============================================================

def instrument_cache_is_fresh():

    if not INSTRUMENT_FILE.exists():

        return False

    modified = datetime.fromtimestamp(
        INSTRUMENT_FILE.stat().st_mtime,
        tz=IST
    )

    age = now_ist() - modified

    return (
        age <
        timedelta(
            hours=INSTRUMENT_CACHE_HOURS
        )
    )


def load_local_instrument_master():

    if not INSTRUMENT_FILE.exists():

        return None

    try:

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

            return None

        if len(data) < 1000:

            return None

        return data

    except Exception:

        return None


def download_instrument_master():

    headers = {

        "User-Agent":
            "Mozilla/5.0 "
            "(Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 "
            "(KHTML, like Gecko) "
            "Chrome/131 Safari/537.36"
    }

    response = requests.get(
        INSTRUMENT_URL,
        headers=headers,
        timeout=90
    )

    response.raise_for_status()

    data = response.json()

    if not isinstance(
        data,
        list
    ):

        raise RuntimeError(
            "Instrument master is not a list."
        )

    if len(data) < 1000:

        raise RuntimeError(
            "Instrument master appears incomplete."
        )

    temp_file = (
        INSTRUMENT_FILE
        .with_suffix(".tmp")
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


def load_instruments(
    force_refresh=False
):

    if (
        not force_refresh
        and instrument_cache_is_fresh()
    ):

        local = (
            load_local_instrument_master()
        )

        if local:

            return local

    try:

        return download_instrument_master()

    except Exception as e:

        local = (
            load_local_instrument_master()
        )

        if local:

            st.warning(
                "Instrument master download "
                "failed. Using local cache."
            )

            return local

        raise RuntimeError(
            "Instrument master unavailable.\n"
            f"{e}"
        )


# ============================================================
# ATM OPTION
# ============================================================

def select_atm_option(
    instruments,
    spot,
    option_type
):

    if not instruments:

        raise RuntimeError(
            "Instrument master unavailable."
        )

    if spot is None:

        raise RuntimeError(
            "NIFTY spot unavailable."
        )

    today = now_ist().date()

    rows = []

    for item in instruments:

        try:

            exchange = str(
                item.get(
                    "exch_seg",
                    ""
                )
            ).upper()

            instrument_type = str(
                item.get(
                    "instrumenttype",
                    ""
                )
            ).upper()

            symbol = str(
                item.get(
                    "symbol",
                    ""
                )
            ).strip()

            name = str(
                item.get(
                    "name",
                    ""
                )
            ).upper()

            if exchange != "NFO":
                continue

            if instrument_type != "OPTIDX":
                continue

            if name != "NIFTY":
                continue

            if not symbol.endswith(
                option_type
            ):
                continue

            expiry_raw = item.get(
                "expiry"
            )

            if not expiry_raw:
                continue

            expiry = pd.to_datetime(
                expiry_raw,
                errors="coerce"
            )

            if pd.isna(expiry):
                continue

            expiry_date = expiry.date()

            if expiry_date < today:
                continue

            strike_raw = item.get(
                "strike"
            )

            strike = float(
                strike_raw
            )

            # Angel One master normally
            # stores strike x100.
            if strike > 100000:

                strike /= 100.0

            token = str(
                item.get(
                    "token",
                    ""
                )
            )

            lot_size = int(
                float(
                    item.get(
                        "lotsize",
                        0
                    )
                )
            )

            if not token:
                continue

            if lot_size <= 0:
                continue

            rows.append({

                "symbol": symbol,

                "token": token,

                "strike": strike,

                "expiry": expiry_date,

                "lot_size": lot_size,

                "option_type":
                    option_type,

                "exchange": "NFO",

                "distance":
                    abs(
                        strike -
                        float(spot)
                    )
            })

        except Exception:

            continue

    if not rows:

        raise RuntimeError(
            f"No NIFTY {option_type} "
            "option found."
        )

    df = pd.DataFrame(
        rows
    )

    # Nearest expiry
    nearest_expiry = df[
        "expiry"
    ].min()

    df = df[
        df["expiry"]
        == nearest_expiry
    ]

    # Nearest strike
    df = df.sort_values(
        "distance"
    )

    return df.iloc[0].to_dict()


# ============================================================
# LTP
# ============================================================

def get_ltp(
    exchange,
    symbol,
    token
):

    api = st.session_state.api

    if api is None:

        raise RuntimeError(
            "Angel One is not connected."
        )

    response = api.ltpData(
        exchange,
        symbol,
        str(token)
    )

    if not response:

        raise RuntimeError(
            "Empty LTP response."
        )

    if not response.get("status"):

        raise RuntimeError(
            "LTP failed:\n"
            + str(response)
        )

    data = response.get(
        "data"
    )

    if not data:

        raise RuntimeError(
            "LTP data missing."
        )

    ltp = data.get(
        "ltp"
    )

    if ltp is None:

        raise RuntimeError(
            "LTP value missing."
        )

    return float(ltp)


def get_nifty_ltp():

    return get_ltp(
        NIFTY_EXCHANGE,
        NIFTY_SYMBOL,
        NIFTY_TOKEN
    )


def get_option_ltp(
    option
):

    return get_ltp(
        "NFO",
        option["symbol"],
        option["token"]
    )


# ============================================================
# CANDLES
# ============================================================

def get_nifty_candles(
    days=30
):

    api = st.session_state.api

    if api is None:

        raise RuntimeError(
            "Angel One is not connected."
        )

    end = now_ist()

    start = (
        end -
        timedelta(
            days=days
        )
    )

    params = {

        "exchange":
            "NSE",

        "symboltoken":
            NIFTY_TOKEN,

        "interval":
            "FIVE_MINUTE",

        "fromdate":
            start.strftime(
                "%Y-%m-%d %H:%M"
            ),

        "todate":
            end.strftime(
                "%Y-%m-%d %H:%M"
            )
    }

    response = api.getCandleData(
        params
    )

    if not response:

        raise RuntimeError(
            "Empty Candle API response."
        )

    if not response.get("status"):

        raise RuntimeError(
            "Candle API failed:\n"
            + str(response)
        )

    rows = response.get(
        "data"
    )

    if not rows:

        raise RuntimeError(
            "No candle data returned."
        )

    df = pd.DataFrame(
        rows,
        columns=[
            "timestamp",
            "open",
            "high",
            "low",
            "close",
            "volume"
        ]
    )

    df["timestamp"] = pd.to_datetime(
        df["timestamp"],
        errors="coerce"
    )

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

    for col in [
        "open",
        "high",
        "low",
        "close",
        "volume"
    ]:

        df[col] = pd.to_numeric(
            df[col],
            errors="coerce"
        )

    df = df.dropna(
        subset=[
            "timestamp",
            "open",
            "high",
            "low",
            "close"
        ]
    )

    df = df.sort_values(
        "timestamp"
    )

    df = df.drop_duplicates(
        "timestamp"
    )

    return df.reset_index(
        drop=True
    )


# ============================================================
# REMOVE INCOMPLETE CANDLE
# ============================================================

def remove_incomplete_candle(
    df
):

    if df.empty:

        return df

    current = now_ist()

    last_time = df.iloc[-1][
        "timestamp"
    ]

    if last_time.tzinfo is None:

        last_time = last_time.replace(
            tzinfo=IST
        )

    candle_end = (
        last_time +
        timedelta(
            minutes=5
        )
    )

    if candle_end > current:

        return df.iloc[:-1]

    return df


# ============================================================
# SUPERTREND
# ============================================================

def calculate_supertrend(
    df,
    period=20,
    multiplier=2.0
):

    df = df.copy()

    high = df["high"]
    low = df["low"]
    close = df["close"]

    prev_close = close.shift(1)

    tr1 = (
        high - low
    )

    tr2 = (
        high - prev_close
    ).abs()

    tr3 = (
        low - prev_close
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
            adjust=False
        )
        .mean()
    )

    hl2 = (
        high + low
    ) / 2

    basic_upper = (
        hl2 +
        multiplier * atr
    )

    basic_lower = (
        hl2 -
        multiplier * atr
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

    for i in range(
        len(df)
    ):

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

        prev_fu = (
            final_upper.iloc[i - 1]
        )

        prev_fl = (
            final_lower.iloc[i - 1]
        )

        prev_close_value = (
            close.iloc[i - 1]
        )

        if (
            basic_upper.iloc[i]
            < prev_fu
            or
            prev_close_value
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
            basic_lower.iloc[i]
            > prev_fl
            or
            prev_close_value
            < prev_fl
        ):

            final_lower.iloc[i] = (
                basic_lower.iloc[i]
            )

        else:

            final_lower.iloc[i] = (
                prev_fl
            )

        prev_st = (
            supertrend.iloc[i - 1]
        )

        if prev_st == prev_fu:

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

    df["Final_Upper"] = final_upper

    df["Final_Lower"] = final_lower

    df["Supertrend"] = supertrend

    df["ST_Direction"] = direction

    df["ST_Green"] = (
        direction == 1
    )

    df["ST_Red"] = (
        direction == -1
    )

    previous_direction = (
        direction.shift(1)
    )

    df["ST_Flip_Green"] = (
        (direction == 1)
        &
        (previous_direction == -1)
    )

    df["ST_Flip_Red"] = (
        (direction == -1)
        &
        (previous_direction == 1)
    )

    return df


# ============================================================
# RESAMPLE
# ============================================================

def resample_ohlcv(
    df,
    rule
):

    temp = df.copy()

    temp = temp.set_index(
        "timestamp"
    )

    result = (
        temp
        .resample(
            rule,
            origin="start_day",
            offset="9h15min",
            label="right",
            closed="left"
        )
        .agg({

            "open":
                "first",

            "high":
                "max",

            "low":
                "min",

            "close":
                "last",

            "volume":
                "sum"
        })
        .dropna()
        .reset_index()
    )

    return result


# ============================================================
# BUILD TIMEFRAMES
# ============================================================

def build_timeframes(
    df5
):

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
# SIGNAL
# ============================================================

def current_signal(
    st5,
    st15,
    st4h
):

    if (
        st5 is None
        or st15 is None
        or st4h is None
    ):

        return "WAIT", None

    if len(st5) < 2:

        return "WAIT", None

    if len(st15) < 1:

        return "WAIT", None

    if len(st4h) < 1:

        return "WAIT", None

    latest5 = st5.iloc[-1]

    latest15 = st15.iloc[-1]

    latest4h = st4h.iloc[-1]

    if (
        bool(
            latest5["ST_Flip_Green"]
        )
        and
        bool(
            latest15["ST_Green"]
        )
        and
        bool(
            latest4h["ST_Green"]
        )
    ):

        return (
            "BUY_CE",
            latest5["timestamp"]
        )

    if (
        bool(
            latest5["ST_Flip_Red"]
        )
        and
        bool(
            latest15["ST_Red"]
        )
        and
        bool(
            latest4h["ST_Red"]
        )
    ):

        return (
            "BUY_PE",
            latest5["timestamp"]
        )

    return "WAIT", None


# ============================================================
# LOCAL STATE
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

            state = json.load(f)

        if not isinstance(
            state,
            dict
        ):

            return {
                "orders": []
            }

        if "orders" not in state:

            state["orders"] = []

        return state

    except Exception:

        return {
            "orders": []
        }


def save_state(
    state
):

    temp = (
        STATE_FILE
        .with_suffix(".tmp")
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
# ORDER BOOK
# ============================================================

def refresh_order_book():

    api = st.session_state.api

    if api is None:

        raise RuntimeError(
            "Angel One is not connected."
        )

    response = api.orderBook()

    if not response:

        raise RuntimeError(
            "Empty Order Book response."
        )

    if not response.get("status"):

        raise RuntimeError(
            "Order Book failed:\n"
            + str(response)
        )

    orders = (
        response.get(
            "data"
        )
        or []
    )

    st.session_state.order_book = (
        orders
    )

    return orders


# ============================================================
# DUPLICATE CHECK
# ============================================================

def orderbook_duplicate_exists(
    symbol
):

    api = st.session_state.api

    if api is None:

        return False

    try:

        response = api.orderBook()

        if not response:
            return False

        if not response.get("status"):
            return False

        orders = (
            response.get(
                "data"
            )
            or []
        )

        today = (
            now_ist()
            .date()
        )

        for order in orders:

            order_symbol = str(
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
            ).upper()

            if order_symbol != symbol:
                continue

            if transaction != "BUY":
                continue

            update_text = str(
                order.get(
                    "updatetime",
                    ""
                )
            )

            if update_text:

                parsed = pd.to_datetime(
                    update_text,
                    errors="coerce"
                )

                if not pd.isna(
                    parsed
                ):

                    if (
                        parsed.date()
                        == today
                    ):

                        return True

            else:

                # If broker doesn't provide
                # timestamp, treat existing BUY
                # as duplicate.
                return True

    except Exception:

        return False

    return False


# ============================================================
# PLACE BUY ORDER
# ============================================================

def place_buy_order(
    signal_type,
    option
):

    if signal_type not in (
        "BUY_CE",
        "BUY_PE"
    ):

        raise RuntimeError(
            "Invalid BUY type."
        )

    if not option:

        raise RuntimeError(
            "Option data missing."
        )

    api = st.session_state.api

    if api is None:

        raise RuntimeError(
            "Angel One is not connected."
        )

    symbol = str(
        option["symbol"]
    )

    token = str(
        option["token"]
    )

    quantity = int(
        option["lot_size"]
    )

    # ========================================================
    # DUPLICATE PROTECTION
    # ========================================================

    if DUPLICATE_PROTECTION:

        if orderbook_duplicate_exists(
            symbol
        ):

            raise RuntimeError(
                f"Duplicate BUY blocked.\n\n"
                f"Symbol: {symbol}\n"
                f"A BUY order for this symbol "
                f"already exists in today's "
                f"Order Book."
            )

    # ========================================================
    # PAPER MODE
    # ========================================================

    if not LIVE_TRADING:

        paper_order_id = (
            "PAPER-"
            +
            now_ist().strftime(
                "%Y%m%d%H%M%S"
            )
        )

        state = load_state()

        state["orders"].append({

            "date":
                now_ist()
                .date()
                .isoformat(),

            "time":
                now_ist()
                .isoformat(),

            "signal":
                signal_type,

            "symbol":
                symbol,

            "token":
                token,

            "quantity":
                quantity,

            "order_id":
                paper_order_id,

            "live_order":
                False,

            "mode":
                "PAPER"
        })

        save_state(
            state
        )

        return paper_order_id

    # ========================================================
    # REAL ANGEL ONE ORDER
    # ========================================================

    order_params = {

        "variety":
            ORDER_VARIETY,

        "tradingsymbol":
            symbol,

        "symboltoken":
            token,

        "transactiontype":
            "BUY",

        "exchange":
            "NFO",

        "ordertype":
            ORDER_TYPE,

        "producttype":
            NFO_PRODUCT_TYPE,

        "duration":
            ORDER_DURATION,

        "quantity":
            str(quantity),

        "price":
            "0",

        "squareoff":
            "0",

        "stoploss":
            "0",

        "ordertag":
            ORDER_TAG
    }

    # Show parameters for debugging.
    with st.expander(
        "🔎 Order parameters",
        expanded=False
    ):

        st.json(
            order_params
        )

    # ========================================================
    # SEND TO ANGEL ONE
    # ========================================================

    try:

        response = api.placeOrder(
            order_params
        )

    except Exception as e:

        raise RuntimeError(
            "SmartAPI placeOrder exception:\n"
            f"{e}"
        )

    # ========================================================
    # PROCESS RESPONSE
    # ========================================================

    if isinstance(
        response,
        dict
    ):

        if not response.get(
            "status"
        ):

            raise RuntimeError(
                "Angel One rejected order.\n\n"
                f"Message: "
                f"{response.get('message')}\n"
                f"Error Code: "
                f"{response.get('errorcode')}\n"
                f"Response: {response}"
            )

        data = (
            response.get(
                "data"
            )
            or {}
        )

        order_id = data.get(
            "orderid"
        )

        if not order_id:

            raise RuntimeError(
                "Angel One accepted the "
                "request but returned no "
                "Order ID.\n\n"
                f"Response: {response}"
            )

        order_id = str(
            order_id
        )

    else:

        if not response:

            raise RuntimeError(
                "Angel One returned an "
                "empty order response."
            )

        order_id = str(
            response
        )

    # ========================================================
    # SAVE STATE
    # ========================================================

    state = load_state()

    state["orders"].append({

        "date":
            now_ist()
            .date()
            .isoformat(),

        "time":
            now_ist()
            .isoformat(),

        "signal":
            signal_type,

        "symbol":
            symbol,

        "token":
            token,

        "quantity":
            quantity,

        "order_id":
            order_id,

        "live_order":
            True,

        "mode":
            "LIVE"
    })

    save_state(
        state
    )

    return order_id


# ============================================================
# EXECUTE BUY
# ============================================================

def execute_buy(
    signal_type,
    option
):

    try:

        # ----------------------------------------------------
        # PLACE ORDER
        # ----------------------------------------------------

        order_id = place_buy_order(
            signal_type,
            option
        )

        # ----------------------------------------------------
        # SAVE NEW ORDER
        # ----------------------------------------------------

        st.session_state.last_order_id = (
            order_id
        )

        st.session_state.highlight_order_id = (
            order_id
        )

        st.session_state.highlight_symbol = (
            option["symbol"]
        )

        st.session_state.last_order_time = (
            now_ist().isoformat()
        )

        # ----------------------------------------------------
        # PAPER ORDER BOOK
        # ----------------------------------------------------

        if not LIVE_TRADING:

            paper_order = {

                "orderid":
                    order_id,

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
                    NFO_PRODUCT_TYPE,

                "duration":
                    "DAY",

                "quantity":
                    str(
                        option["lot_size"]
                    ),

                "price":
                    "0",

                "averageprice":
                    "0",

                "orderstatus":
                    "PAPER",

                "status":
                    "PAPER",

                "updatetime":
                    now_ist().strftime(
                        "%d-%b-%Y %H:%M:%S"
                    ),

                "ordertag":
                    ORDER_TAG
            }

            existing = (
                st.session_state
                .order_book
                or []
            )

            st.session_state.order_book = (
                [paper_order]
                +
                existing
            )

        # ----------------------------------------------------
        # LIVE ORDER BOOK
        # ----------------------------------------------------

        else:

            # Give Angel One a moment.
            time.sleep(1)

            try:

                refresh_order_book()

            except Exception as e:

                st.warning(
                    "Order was submitted, "
                    "but Order Book refresh "
                    f"failed: {e}"
                )

        # ----------------------------------------------------
        # AUTOMATICALLY SELECT ORDER BOOK
        # ----------------------------------------------------

        st.session_state.selected_section = (
            "Order Book"
        )

        st.session_state.last_message = (
            f"{signal_type} submitted | "
            f"{option['symbol']} | "
            f"Order ID: {order_id}"
        )

        return True, order_id

    except Exception as e:

        st.session_state.last_error = (
            str(e)
        )

        return False, str(e)


# ============================================================
# ORDER BOOK DISPLAY
# ============================================================

def show_order_book():

    st.subheader(
        "📋 Order Book"
    )

    col1, col2 = st.columns(
        [1, 4]
    )

    with col1:

        if st.button(
            "🔄 Refresh",
            use_container_width=True
        ):

            try:

                refresh_order_book()

                st.success(
                    "Order Book refreshed."
                )

                st.rerun()

            except Exception as e:

                st.error(
                    f"Refresh failed: {e}"
                )

    with col2:

        if st.session_state.highlight_order_id:

            st.info(
                "⭐ Newly placed order: "
                +
                str(
                    st.session_state
                    .highlight_order_id
                )
            )

    orders = (
        st.session_state
        .order_book
        or []
    )

    if not orders:

        st.info(
            "No orders found."
        )

        return

    rows = []

    for order in orders:

        order_id = str(
            order.get(
                "orderid",
                ""
            )
        )

        is_new = (
            order_id
            ==
            str(
                st.session_state
                .highlight_order_id
                or ""
            )
        )

        rows.append({

            "⭐":
                "NEW"
                if is_new
                else "",

            "Order ID":
                order_id,

            "Symbol":
                order.get(
                    "tradingsymbol",
                    ""
                ),

            "Side":
                order.get(
                    "transactiontype",
                    ""
                ),

            "Quantity":
                order.get(
                    "quantity",
                    ""
                ),

            "Order Type":
                order.get(
                    "ordertype",
                    ""
                ),

            "Product":
                order.get(
                    "producttype",
                    ""
                ),

            "Status":
                order.get(
                    "orderstatus",
                    order.get(
                        "status",
                        ""
                    )
                ),

            "Price":
                order.get(
                    "price",
                    ""
                ),

            "Average Price":
                order.get(
                    "averageprice",
                    ""
                ),

            "Time":
                order.get(
                    "updatetime",
                    ""
                )
        })

    df = pd.DataFrame(
        rows
    )

    # --------------------------------------------------------
    # PUT NEW ORDER FIRST
    # --------------------------------------------------------

    highlight_id = str(
        st.session_state
        .highlight_order_id
        or ""
    )

    if highlight_id:

        df["_new"] = (
            df["Order ID"]
            .astype(str)
            .eq(
                highlight_id
            )
        )

        df = df.sort_values(
            "_new",
            ascending=False
        )

        df = df.drop(
            columns=[
                "_new"
            ]
        )

    # --------------------------------------------------------
    # DISPLAY TABLE
    # --------------------------------------------------------

    st.dataframe(
        df,
        use_container_width=True,
        hide_index=True
    )

    # --------------------------------------------------------
    # HIGHLIGHTED ORDER
    # --------------------------------------------------------

    if highlight_id:

        selected = None

        for order in orders:

            if str(
                order.get(
                    "orderid",
                    ""
                )
            ) == highlight_id:

                selected = order

                break

        if selected:

            st.divider()

            st.success(
                "⭐ Newly placed order"
            )

            a, b, c, d = st.columns(
                4
            )

            with a:

                st.metric(
                    "Order ID",
                    str(
                        selected.get(
                            "orderid",
                            "-"
                        )
                    )
                )

            with b:

                st.metric(
                    "Symbol",
                    str(
                        selected.get(
                            "tradingsymbol",
                            "-"
                        )
                    )
                )

            with c:

                st.metric(
                    "Quantity",
                    str(
                        selected.get(
                            "quantity",
                            "-"
                        )
                    )
                )

            with d:

                st.metric(
                    "Status",
                    str(
                        selected.get(
                            "orderstatus",
                            selected.get(
                                "status",
                                "-"
                            )
                        )
                    )
                )


# ============================================================
# ORDER PANEL
# ============================================================

def show_order_panel(
    spot
):

    st.divider()

    st.subheader(
        "🛒 NIFTY BUY ORDER"
    )

    st.caption(
        "BUY CE and BUY PE are manual "
        "buttons and remain available "
        "even when strategy signal is WAIT."
    )

    ce_option = None
    pe_option = None

    # ========================================================
    # CE
    # ========================================================

    try:

        ce_option = select_atm_option(
            st.session_state.instruments,
            spot,
            "CE"
        )

        try:

            ce_option["ltp"] = (
                get_option_ltp(
                    ce_option
                )
            )

        except Exception:

            ce_option["ltp"] = None

        st.session_state.ce_option = (
            ce_option
        )

    except Exception as e:

        st.error(
            f"CE unavailable: {e}"
        )

    # ========================================================
    # PE
    # ========================================================

    try:

        pe_option = select_atm_option(
            st.session_state.instruments,
            spot,
            "PE"
        )

        try:

            pe_option["ltp"] = (
                get_option_ltp(
                    pe_option
                )
            )

        except Exception:

            pe_option["ltp"] = None

        st.session_state.pe_option = (
            pe_option
        )

    except Exception as e:

        st.error(
            f"PE unavailable: {e}"
        )

    # ========================================================
    # TWO PANELS
    # ========================================================

    c1, c2 = st.columns(2)

    # ========================================================
    # CE
    # ========================================================

    with c1:

        st.markdown(
            "### 🟢 ATM CALL OPTION"
        )

        if ce_option:

            x1, x2, x3 = st.columns(
                3
            )

            x1.metric(
                "Strike",
                fmt_number(
                    ce_option["strike"],
                    0
                )
            )

            x2.metric(
                "LTP",
                fmt_number(
                    ce_option.get(
                        "ltp"
                    )
                )
            )

            x3.metric(
                "Lot Size",
                str(
                    ce_option[
                        "lot_size"
                    ]
                )
            )

            st.write(
                "**Symbol:** "
                f"`{ce_option['symbol']}`"
            )

            st.write(
                "**Expiry:** "
                f"`{ce_option['expiry']}`"
            )

            st.write(
                "**Token:** "
                f"`{ce_option['token']}`"
            )

            # =================================================
            # BUY CE BUTTON ALWAYS VISIBLE
            # =================================================

            if st.button(
                "🛒 BUY CE",
                key="BUY_CE_MANUAL",
                type="primary",
                use_container_width=True
            ):

                success, result = (
                    execute_buy(
                        "BUY_CE",
                        ce_option
                    )
                )

                if success:

                    if LIVE_TRADING:

                        st.success(
                            "✅ BUY CE order "
                            "submitted."
                        )

                    else:

                        st.success(
                            "🟢 PAPER BUY CE "
                            "created."
                        )

                    # Automatic Order Book
                    # switch
                    st.rerun()

                else:

                    st.error(
                        "❌ BUY CE failed:\n"
                        f"{result}"
                    )

    # ========================================================
    # PE
    # ========================================================

    with c2:

        st.markdown(
            "### 🔴 ATM PUT OPTION"
        )

        if pe_option:

            x1, x2, x3 = st.columns(
                3
            )

            x1.metric(
                "Strike",
                fmt_number(
                    pe_option["strike"],
                    0
                )
            )

            x2.metric(
                "LTP",
                fmt_number(
                    pe_option.get(
                        "ltp"
                    )
                )
            )

            x3.metric(
                "Lot Size",
                str(
                    pe_option[
                        "lot_size"
                    ]
                )
            )

            st.write(
                "**Symbol:** "
                f"`{pe_option['symbol']}`"
            )

            st.write(
                "**Expiry:** "
                f"`{pe_option['expiry']}`"
            )

            st.write(
                "**Token:** "
                f"`{pe_option['token']}`"
            )

            # =================================================
            # BUY PE BUTTON ALWAYS VISIBLE
            # =================================================

            if st.button(
                "🛒 BUY PE",
                key="BUY_PE_MANUAL",
                type="primary",
                use_container_width=True
            ):

                success, result = (
                    execute_buy(
                        "BUY_PE",
                        pe_option
                    )
                )

                if success:

                    if LIVE_TRADING:

                        st.success(
                            "✅ BUY PE order "
                            "submitted."
                        )

                    else:

                        st.success(
                            "🔴 PAPER BUY PE "
                            "created."
                        )

                    # Automatic Order Book
                    # switch
                    st.rerun()

                else:

                    st.error(
                        "❌ BUY PE failed:\n"
                        f"{result}"
                    )


# ============================================================
# DASHBOARD
# ============================================================

def show_dashboard():

    st.title(
        "📈 NIFTY Live Supertrend Dashboard"
    )

    # ========================================================
    # TOP BAR
    # ========================================================

    c1, c2, c3, c4 = st.columns(
        4
    )

    with c1:

        if st.button(
            "🔌 Connect Angel One",
            use_container_width=True
        ):

            try:

                angel_login()

                st.success(
                    "Angel One connected."
                )

                st.rerun()

            except Exception as e:

                st.session_state.login_status = (
                    "FAILED"
                )

                st.session_state.last_error = (
                    str(e)
                )

                st.error(
                    f"Login failed: {e}"
                )

    with c2:

        if st.button(
            "📥 Load Instruments",
            use_container_width=True
        ):

            try:

                instruments = (
                    load_instruments()
                )

                st.session_state.instruments = (
                    instruments
                )

                st.success(
                    f"Loaded "
                    f"{len(instruments):,} "
                    f"instruments."
                )

            except Exception as e:

                st.error(
                    f"Instrument error: {e}"
                )

    with c3:

        st.metric(
            "Connection",
            st.session_state.login_status
        )

    with c4:

        if LIVE_TRADING:

            st.error(
                "🔴 LIVE"
            )

        else:

            st.success(
                "🟢 PAPER"
            )

    # ========================================================
    # ERROR
    # ========================================================

    if st.session_state.last_error:

        st.error(
            st.session_state.last_error
        )

        # Clear after displaying
        st.session_state.last_error = ""

    # ========================================================
    # MESSAGE
    # ========================================================

    if st.session_state.last_message:

        st.caption(
            st.session_state.last_message
        )

    # ========================================================
    # CONNECTION
    # ========================================================

    if st.session_state.api is None:

        st.info(
            "Click **Connect Angel One** "
            "to start."
        )

        return

    # ========================================================
    # INSTRUMENTS
    # ========================================================

    if (
        st.session_state.instruments
        is None
    ):

        try:

            with st.spinner(
                "Loading instrument master..."
            ):

                st.session_state.instruments = (
                    load_instruments()
                )

        except Exception as e:

            st.error(
                f"Instrument master error: {e}"
            )

            return

    # ========================================================
    # NIFTY SPOT
    # ========================================================

    try:

        spot = get_nifty_ltp()

        st.session_state.spot = (
            spot
        )

    except Exception as e:

        st.error(
            "NIFTY LTP FAILED:\n"
            f"{e}"
        )

        return

    # ========================================================
    # CANDLES
    # ========================================================

    try:

        df5 = get_nifty_candles(
            days=30
        )

        df5 = (
            remove_incomplete_candle(
                df5
            )
        )

        if len(df5) < 50:

            st.warning(
                "Not enough candles."
            )

            return

        (
            st5,
            st15,
            st4h
        ) = build_timeframes(
            df5
        )

        st.session_state.st5 = st5

        st.session_state.st15 = st15

        st.session_state.st4h = st4h

    except Exception as e:

        st.error(
            "Supertrend calculation failed:\n"
            f"{e}"
        )

        return

    # ========================================================
    # SIGNAL
    # ========================================================

    signal, signal_time = (
        current_signal(
            st5,
            st15,
            st4h
        )
    )

    st.session_state.signal = (
        signal
    )

    st.session_state.signal_time = (
        signal_time
    )

    # ========================================================
    # METRICS
    # ========================================================

    st.divider()

    m1, m2, m3, m4, m5 = st.columns(
        5
    )

    latest5 = st5.iloc[-1]

    latest15 = st15.iloc[-1]

    latest4h = st4h.iloc[-1]

    m1.metric(
        "NIFTY",
        fmt_number(
            spot
        )
    )

    m2.metric(
        "5 MIN",
        "GREEN"
        if latest5["ST_Green"]
        else "RED"
    )

    m3.metric(
        "15 MIN",
        "GREEN"
        if latest15["ST_Green"]
        else "RED"
    )

    m4.metric(
        "4 HOUR",
        "GREEN"
        if latest4h["ST_Green"]
        else "RED"
    )

    m5.metric(
        "SIGNAL",
        signal
    )

    # ========================================================
    # SIGNAL MESSAGE
    # ========================================================

    if signal == "BUY_CE":

        st.success(
            "🟢 CONFIRMED BUY CE SIGNAL"
        )

    elif signal == "BUY_PE":

        st.error(
            "🔴 CONFIRMED BUY PE SIGNAL"
        )

    else:

        st.info(
            "⏳ WAIT — strategy has "
            "no confirmed entry."
        )

    # ========================================================
    # ORDER PANEL
    # ========================================================

    show_order_panel(
        spot
    )

    # ========================================================
    # CHART
    # ========================================================

    st.divider()

    st.subheader(
        "📊 NIFTY 5-Minute Chart"
    )

    chart_df = st5[
        [
            "timestamp",
            "close",
            "Supertrend"
        ]
    ].copy()

    chart_df = chart_df.set_index(
        "timestamp"
    )

    st.line_chart(
        chart_df,
        use_container_width=True
    )

    # ========================================================
    # CANDLES
    # ========================================================

    with st.expander(
        "Latest 5-Minute Candles"
    ):

        st.dataframe(
            st5.tail(50),
            use_container_width=True,
            hide_index=True
        )


# ============================================================
# POSITIONS
# ============================================================

def show_positions():

    st.subheader(
        "📊 Positions"
    )

    try:

        response = (
            st.session_state
            .api
            .position()
        )

        if not response:

            st.info(
                "No position response."
            )

            return

        if not response.get(
            "status"
        ):

            st.error(
                str(response)
            )

            return

        positions = (
            response.get(
                "data"
            )
            or []
        )

        st.session_state.positions = (
            positions
        )

        if not positions:

            st.info(
                "No open positions."
            )

            return

        df = pd.DataFrame(
            positions
        )

        st.dataframe(
            df,
            use_container_width=True,
            hide_index=True
        )

    except Exception as e:

        st.error(
            f"Positions failed: {e}"
        )


# ============================================================
# SIDEBAR NAVIGATION
# ============================================================

st.sidebar.title(
    "📈 NIFTY Trading"
)

st.sidebar.caption(
    "Supertrend 20,2"
)

# ------------------------------------------------------------
# LIVE MODE
# ------------------------------------------------------------

if LIVE_TRADING:

    st.sidebar.error(
        "🔴 LIVE TRADING ENABLED"
    )

    st.sidebar.warning(
        "Real orders can be submitted."
    )

else:

    st.sidebar.success(
        "🟢 PAPER MODE"
    )

    st.sidebar.info(
        "No real order will be sent."
    )


# ------------------------------------------------------------
# NAVIGATION
# ------------------------------------------------------------

sections = [
    "Dashboard",
    "Order Book",
    "Positions"
]

current_section = (
    st.session_state.selected_section
)

if current_section not in sections:

    current_section = "Dashboard"

section = st.sidebar.radio(
    "Open",
    sections,
    index=sections.index(
        current_section
    )
)

st.session_state.selected_section = (
    section
)


# ============================================================
# ROUTING
# ============================================================

if section == "Dashboard":

    show_dashboard()


elif section == "Order Book":

    st.title(
        "📋 Angel One Order Book"
    )

    if st.session_state.api is None:

        st.warning(
            "Connect Angel One first."
        )

    else:

        show_order_book()


elif section == "Positions":

    st.title(
        "📊 Angel One Positions"
    )

    if st.session_state.api is None:

        st.warning(
            "Connect Angel One first."
        )

    else:

        show_positions()


# ============================================================
# FOOTER
# ============================================================

st.sidebar.divider()

st.sidebar.caption(
    "NIFTY Supertrend 20,2"
)

st.sidebar.caption(
    "5m + 15m + 4H"
)

st.sidebar.caption(
    "Options: ATM CE / ATM PE"
)

st.sidebar.caption(
    "Updated: "
    +
    now_ist().strftime(
        "%d-%m-%Y %H:%M:%S"
    )
)
