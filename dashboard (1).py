# ============================================================
# dashboard.py
#
# NIFTY LIVE DIRECT AUTOMATIC BUY CE
# ANGEL ONE SMARTAPI + STREAMLIT
#
# STRATEGY
# ------------------------------------------------------------
# 1-minute NIFTY candles
#          ↓
# completed 2-minute candles
#          ↓
# Supertrend (20, 1.5)
#          ↓
# GREEN FLIP
#          ↓
# Select nearest ATM NIFTY CE
#          ↓
# DIRECT BUY MARKET ORDER
#
# NO MANUAL BUY BUTTON
# NO SELL LOGIC
# NO BROKER EXECUTION CONFIRMATION REQUIRED
#
# Duplicate protection:
#   Same completed 2-minute candle can trigger only once.
# ============================================================

import os
import json
import time
import math
from pathlib import Path
from datetime import datetime, timedelta

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

LIVE_TRADING = True

LOTS = 1

ST_PERIOD = 20
ST_MULTIPLIER = 1.5

CANDLE_INTERVAL = "ONE_MINUTE"

REFRESH_SECONDS = 10

NIFTY_EXCHANGE = "NSE"
NIFTY_SYMBOL = "NIFTY"
NIFTY_TOKEN = "99926000"

OPTION_EXCHANGE = "NFO"

PRODUCT_TYPE = "INTRADAY"
ORDER_TYPE = "MARKET"
VARIETY = "NORMAL"
DURATION = "DAY"

INSTRUMENT_FILE = Path("OpenAPIScripMaster.json")

STATE_FILE = Path("nifty_auto_ce_state.json")

INSTRUMENT_URL = (
    "https://margincalculator.angelone.com/"
    "OpenAPI_File/files/OpenAPIScripMaster.json"
)


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
    "last_signal": "WAIT",
    "last_error": None,
}


# ============================================================
# STREAMLIT STATE
# ============================================================

if "api" not in st.session_state:
    st.session_state.api = None

if "logged_in" not in st.session_state:
    st.session_state.logged_in = False

if "login_message" not in st.session_state:
    st.session_state.login_message = ""

if "running" not in st.session_state:
    st.session_state.running = True

if "state" not in st.session_state:
    st.session_state.state = DEFAULT_STATE.copy()


# ============================================================
# STATE FILE
# ============================================================

def load_state():
    try:
        if STATE_FILE.exists():
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)

            state = DEFAULT_STATE.copy()
            state.update(data)
            return state

    except Exception:
        pass

    return DEFAULT_STATE.copy()


def save_state(state):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, default=str)
    except Exception:
        pass


if st.session_state.state == DEFAULT_STATE:
    st.session_state.state = load_state()


# ============================================================
# CREDENTIALS
# ============================================================

def get_secret(name, default=""):
    """
    Priority:
      1. Streamlit secrets
      2. Environment variable
    """

    try:
        value = st.secrets.get(name)

        if value:
            return str(value).strip()

    except Exception:
        pass

    return os.getenv(name, default).strip()


API_KEY = get_secret("ANGEL_API_KEY")
CLIENT_ID = get_secret("ANGEL_CLIENT_ID")
PASSWORD = get_secret("ANGEL_PASSWORD")
TOTP_SECRET = get_secret("ANGEL_TOTP_SECRET")


# ============================================================
# TOTP
# ============================================================

def clean_totp_secret(secret):
    secret = secret.strip()

    if secret.lower().startswith("otpauth://"):
        try:
            from urllib.parse import urlparse, parse_qs

            parsed = urlparse(secret)
            params = parse_qs(parsed.query)

            secret = params.get("secret", [""])[0]

        except Exception:
            pass

    return secret.replace(" ", "").upper()


# ============================================================
# LOGIN
# ============================================================

def angel_login():

    if not API_KEY:
        raise RuntimeError("ANGEL_API_KEY is missing")

    if not CLIENT_ID:
        raise RuntimeError("ANGEL_CLIENT_ID is missing")

    if not PASSWORD:
        raise RuntimeError("ANGEL_PASSWORD is missing")

    if not TOTP_SECRET:
        raise RuntimeError("ANGEL_TOTP_SECRET is missing")

    secret = clean_totp_secret(TOTP_SECRET)

    if not secret:
        raise RuntimeError("ANGEL_TOTP_SECRET is empty")

    try:
        totp = pyotp.TOTP(secret).now()
    except Exception as e:
        raise RuntimeError(
            f"TOTP generation failed: {e}"
        )

    api = SmartConnect(api_key=API_KEY)

    response = api.generateSession(
        CLIENT_ID,
        PASSWORD,
        totp
    )

    if not response:
        raise RuntimeError("Angel One returned empty login response")

    if response.get("status") is not True:
        raise RuntimeError(
            f"Angel login failed: {response}"
        )

    if not response.get("data"):
        raise RuntimeError(
            f"Angel login returned no data: {response}"
        )

    if not response["data"].get("jwtToken"):
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

    if st.session_state.api is not None:
        return st.session_state.api

    return angel_login()


# ============================================================
# DOWNLOAD INSTRUMENT MASTER
# ============================================================

@st.cache_data(ttl=3600, show_spinner=False)
def download_instruments():

    try:

        response = requests.get(
            INSTRUMENT_URL,
            timeout=30
        )

        response.raise_for_status()

        data = response.json()

        if not isinstance(data, list):
            raise RuntimeError(
                "Instrument master response is not a list"
            )

        return pd.DataFrame(data)

    except Exception as e:

        raise RuntimeError(
            f"Instrument master download failed: {e}"
        )


# ============================================================
# LOAD INSTRUMENT MASTER
# ============================================================

@st.cache_data(ttl=3600, show_spinner=False)
def load_instruments():

    if INSTRUMENT_FILE.exists():

        try:

            with open(
                INSTRUMENT_FILE,
                "r",
                encoding="utf-8"
            ) as f:

                data = json.load(f)

            if isinstance(data, list):

                df = pd.DataFrame(data)

                if not df.empty:
                    return df

        except Exception:
            pass

    return download_instruments()


# ============================================================
# NIFTY LIVE LTP
# ============================================================

def get_nifty_ltp(api):

    response = api.ltpData(
        NIFTY_EXCHANGE,
        NIFTY_SYMBOL,
        NIFTY_TOKEN
    )

    if not response:

        raise RuntimeError(
            "NIFTY LTP returned empty response"
        )

    if response.get("status") is not True:

        raise RuntimeError(
            f"NIFTY LTP FAILED | {response}"
        )

    data = response.get("data") or {}

    ltp = data.get("ltp")

    if ltp is None:

        raise RuntimeError(
            f"NIFTY LTP missing | {response}"
        )

    return float(ltp)


# ============================================================
# GET 1-MINUTE CANDLES
# ============================================================

def get_1m_candles(api, days=3):

    now = datetime.now()

    from_time = now - timedelta(days=days)

    params = {
        "exchange": "NSE",
        "symboltoken": NIFTY_TOKEN,
        "interval": CANDLE_INTERVAL,
        "fromdate": from_time.strftime(
            "%Y-%m-%d %H:%M"
        ),
        "todate": now.strftime(
            "%Y-%m-%d %H:%M"
        ),
    }

    response = api.getCandleData(params)

    if not response:

        raise RuntimeError(
            "Candle API returned empty response"
        )

    if response.get("status") is not True:

        raise RuntimeError(
            f"Candle API failed | {response}"
        )

    rows = response.get("data")

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
        ],
    )

    df["timestamp"] = pd.to_datetime(
        df["timestamp"]
    )

    # Angel timestamps are treated as candle START.
    # Convert to India time when timezone information exists.
    try:

        if df["timestamp"].dt.tz is not None:

            df["timestamp"] = (
                df["timestamp"]
                .dt.tz_convert("Asia/Kolkata")
                .dt.tz_localize(None)
            )

    except Exception:
        pass

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
            "timestamp",
            "open",
            "high",
            "low",
            "close",
        ]
    )

    df = df.sort_values("timestamp")

    df = df.drop_duplicates(
        subset=["timestamp"],
        keep="last"
    )

    return df.reset_index(drop=True)


# ============================================================
# REMOVE CURRENT FORMING 1-MINUTE CANDLE
# ============================================================

def remove_current_candle(df):

    now = pd.Timestamp.now()

    current_minute = now.floor("min")

    df = df[
        df["timestamp"] < current_minute
    ].copy()

    return df


# ============================================================
# RESAMPLE 1M → 2M
# ============================================================

def resample_to_2m(df):

    if df.empty:
        return df

    x = df.copy()

    x = x.set_index("timestamp")

    # NSE session starts 09:15.
    #
    # 09:15-09:16 => first 2-minute bar
    # 09:17-09:18 => second 2-minute bar
    # etc.

    x = x.between_time(
        "09:15",
        "15:29"
    )

    result = x.resample(
        "2min",
        origin="start_day",
        offset="9h15min",
        label="right",
        closed="left",
    ).agg(
        {
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum",
        }
    )

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

    high = df["high"]
    low = df["low"]
    close = df["close"]

    previous_close = close.shift(1)

    tr1 = high - low

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
    ).max(axis=1)

    atr = tr.ewm(
        alpha=1 / period,
        adjust=False,
        min_periods=period
    ).mean()

    hl2 = (
        high + low
    ) / 2.0

    basic_upper = (
        hl2 + multiplier * atr
    )

    basic_lower = (
        hl2 - multiplier * atr
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

    for i in range(len(df)):

        if pd.isna(atr.iloc[i]):

            continue

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

        # Final upper band
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

        # Final lower band
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

        # Supertrend direction
        if supertrend.iloc[i - 1] == final_upper.iloc[i - 1]:

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
        direction == 1
    )

    df["ST_Red"] = (
        direction == -1
    )

    df["GREEN_FLIP"] = (
        (direction == 1)
        &
        (direction.shift(1) == -1)
    )

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

    for col in required:

        if col not in df.columns:

            raise RuntimeError(
                f"Instrument master missing column: {col}"
            )

    # NFO only
    df = df[
        df["exch_seg"]
        .astype(str)
        .str.upper()
        == "NFO"
    ].copy()

    # Index options
    df = df[
        df["instrumenttype"]
        .astype(str)
        .str.upper()
        == "OPTIDX"
    ].copy()

    # NIFTY
    name = (
        df["name"]
        .astype(str)
        .str.upper()
        .str.strip()
    )

    df = df[
        name.isin(
            [
                "NIFTY",
                "NIFTY 50",
            ]
        )
    ].copy()

    # CE only
    symbol = (
        df["symbol"]
        .astype(str)
        .str.upper()
    )

    df = df[
        symbol.str.endswith("CE")
    ].copy()

    if df.empty:

        raise RuntimeError(
            "No NIFTY CE contracts found"
        )

    # --------------------------------------------------------
    # EXPIRY
    # --------------------------------------------------------

    today = pd.Timestamp.now().normalize()

    df["expiry_dt"] = pd.to_datetime(
        df["expiry"],
        errors="coerce"
    )

    df = df.dropna(
        subset=["expiry_dt"]
    )

    future = df[
        df["expiry_dt"] >= today
    ].copy()

    if future.empty:

        raise RuntimeError(
            "No future NIFTY CE expiry found"
        )

    nearest_expiry = (
        future["expiry_dt"]
        .min()
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

    # Angel strike can be represented
    # in paise-style units.
    df["strike_actual"] = np.where(
        df["strike_num"] > 100000,
        df["strike_num"] / 100.0,
        df["strike_num"]
    )

    # NIFTY strike interval = 50
    atm_strike = (
        round(float(spot) / 50.0)
        * 50.0
    )

    df["distance"] = (
        df["strike_actual"]
        - atm_strike
    ).abs()

    df = df.sort_values(
        [
            "distance",
            "strike_actual"
        ]
    )

    if df.empty:

        raise RuntimeError(
            "Could not select ATM CE"
        )

    row = df.iloc[0]

    return {
        "symbol": str(row["symbol"]),
        "token": str(row["token"]),
        "strike": float(row["strike_actual"]),
        "expiry": str(
            row["expiry"]
        ),
        "lotsize": int(
            float(row["lotsize"])
        ),
    }


# ============================================================
# PLACE DIRECT BUY CE
# ============================================================

def place_direct_buy_ce(
    api,
    option
):

    quantity = (
        option["lotsize"]
        * LOTS
    )

    order_params = {

        "variety": VARIETY,

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

        # Required/accepted fields
        # for the normal order structure.
        "price":
            "0",

        "squareoff":
            "0",

        "stoploss":
            "0",
    }

    if not LIVE_TRADING:

        return {
            "order_id": "PAPER_ORDER",
            "response": order_params,
        }

    # DIRECT BUY
    order_id = api.placeOrder(
        order_params
    )

    if not order_id:

        raise RuntimeError(
            "Angel One returned empty order ID"
        )

    return {
        "order_id": str(order_id),
        "response": order_params,
    }


# ============================================================
# AUTOMATIC SIGNAL PROCESSOR
# ============================================================

def process_automatic_buy():

    api = get_api()

    # --------------------------------------------------------
    # 1. GET LIVE NIFTY
    # --------------------------------------------------------

    spot = get_nifty_ltp(api)

    # --------------------------------------------------------
    # 2. GET 1-MINUTE DATA
    # --------------------------------------------------------

    df_1m = get_1m_candles(
        api,
        days=3
    )

    # Remove current incomplete 1-minute candle.
    df_1m = remove_current_candle(
        df_1m
    )

    if len(df_1m) < 50:

        raise RuntimeError(
            "Not enough completed 1-minute candles"
        )

    # --------------------------------------------------------
    # 3. CREATE COMPLETED 2-MINUTE CANDLES
    # --------------------------------------------------------

    df_2m = resample_to_2m(
        df_1m
    )

    if len(df_2m) < ST_PERIOD + 5:

        raise RuntimeError(
            "Not enough 2-minute candles"
        )

    # --------------------------------------------------------
    # 4. SUPERTREND 20, 1.5
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
            "Supertrend not ready"
        )

    # Last COMPLETED 2-minute candle
    latest = valid.iloc[-1]

    candle_time = pd.Timestamp(
        latest["timestamp"]
    )

    candle_key = candle_time.strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    # --------------------------------------------------------
    # 5. DUPLICATE PROTECTION
    # --------------------------------------------------------

    previous_processed = (
        st.session_state.state
        .get("last_processed_candle")
    )

    # Same candle already checked.
    if previous_processed == candle_key:

        return {
            "status": "WAIT",
            "reason": "Already processed this 2-minute candle",
            "spot": spot,
            "candle": candle_key,
            "df": df_2m,
        }

    # Mark candle as processed BEFORE order logic.
    #
    # This prevents Streamlit reruns from sending
    # multiple orders for the same green flip.
    st.session_state.state[
        "last_processed_candle"
    ] = candle_key

    save_state(
        st.session_state.state
    )

    # --------------------------------------------------------
    # 6. SIGNAL
    # --------------------------------------------------------

    is_green = bool(
        latest["ST_Direction"] == 1
    )

    is_green_flip = bool(
        latest["GREEN_FLIP"]
    )

    if not is_green_flip:

        st.session_state.state[
            "last_signal"
        ] = "WAIT"

        save_state(
            st.session_state.state
        )

        return {
            "status": "WAIT",
            "reason": "No GREEN flip",
            "spot": spot,
            "candle": candle_key,
            "direction":
                "GREEN" if is_green else "RED",
            "df": df_2m,
        }

    # --------------------------------------------------------
    # 7. EXTRA SIGNAL DUPLICATE PROTECTION
    # --------------------------------------------------------

    last_signal_candle = (
        st.session_state.state
        .get("last_signal_candle")
    )

    if last_signal_candle == candle_key:

        return {
            "status": "WAIT",
            "reason": "Signal already processed",
            "spot": spot,
            "candle": candle_key,
            "df": df_2m,
        }

    # --------------------------------------------------------
    # 8. RECORD SIGNAL
    # --------------------------------------------------------

    st.session_state.state[
        "last_signal_candle"
    ] = candle_key

    st.session_state.state[
        "last_signal"
    ] = "GREEN FLIP"

    save_state(
        st.session_state.state
    )

    # --------------------------------------------------------
    # 9. LOAD OPTION MASTER
    # --------------------------------------------------------

    instruments = load_instruments()

    # --------------------------------------------------------
    # 10. SELECT ATM CE
    # --------------------------------------------------------

    option = select_atm_ce(
        instruments,
        spot
    )

    # --------------------------------------------------------
    # 11. DIRECT BUY
    # --------------------------------------------------------

    result = place_direct_buy_ce(
        api,
        option
    )

    order_id = result["order_id"]

    # --------------------------------------------------------
    # 12. SAVE ORDER INFORMATION
    # --------------------------------------------------------

    st.session_state.state[
        "last_order_id"
    ] = order_id

    st.session_state.state[
        "last_order_time"
    ] = datetime.now().strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    st.session_state.state[
        "last_symbol"
    ] = option["symbol"]

    st.session_state.state[
        "last_token"
    ] = option["token"]

    st.session_state.state[
        "last_strike"
    ] = option["strike"]

    st.session_state.state[
        "last_error"
    ] = None

    save_state(
        st.session_state.state
    )

    return {
        "status": "BUY_SENT",
        "reason": "GREEN FLIP",
        "spot": spot,
        "candle": candle_key,
        "option": option,
        "order_id": order_id,
        "df": df_2m,
    }


# ============================================================
# UI
# ============================================================

st.title(
    "📈 NIFTY Live Direct Automatic BUY CE"
)

st.caption(
    "2-Minute Supertrend (20, 1.5) → GREEN FLIP → "
    "Automatic ATM NIFTY CE BUY"
)


# ============================================================
# STATUS
# ============================================================

col1, col2, col3, col4 = st.columns(4)

col1.metric(
    "Trading",
    "LIVE" if LIVE_TRADING else "PAPER"
)

col2.metric(
    "Strategy",
    "ST 20 / 1.5"
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
# RUN AUTOMATIC TRADING
# ============================================================

if st.session_state.running:

    try:

        result = process_automatic_buy()

        status = result.get(
            "status",
            "WAIT"
        )

        spot = result.get(
            "spot"
        )

        candle = result.get(
            "candle"
        )

        if status == "BUY_SENT":

            option = result["option"]

            st.success(
                "🚀 AUTOMATIC BUY CE ORDER SENT"
            )

            a, b, c, d = st.columns(4)

            a.metric(
                "NIFTY",
                f"{spot:.2f}"
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

        else:

            direction = result.get(
                "direction",
                "-"
            )

            st.info(
                f"Signal: {status} | "
                f"NIFTY: {spot:.2f} | "
                f"2M candle: {candle} | "
                f"Direction: {direction}"
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
# LAST ORDER
# ============================================================

st.divider()

st.subheader(
    "Last Automatic BUY"
)

state = st.session_state.state

a, b, c, d = st.columns(4)

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
    ) or "-"
)

c.metric(
    "Last Strike",
    (
        f"{state['last_strike']:.0f}"
        if state.get("last_strike")
        else "-"
    )
)

d.metric(
    "Order ID",
    state.get(
        "last_order_id"
    ) or "-"
)


if state.get("last_order_time"):

    st.caption(
        f"Order sent: {state['last_order_time']}"
    )


if state.get("last_error"):

    st.error(
        f"Last error: {state['last_error']}"
    )


# ============================================================
# AUTOMATIC ORDER RULE
# ============================================================

st.divider()

st.subheader(
    "Automatic Order Rule"
)

st.write(
    "1. Read live NIFTY 1-minute candles."
)

st.write(
    "2. Build completed 2-minute candles."
)

st.write(
    "3. Calculate Supertrend (20, 1.5)."
)

st.write(
    "4. Detect RED → GREEN flip."
)

st.write(
    "5. Select nearest ATM NIFTY CE."
)

st.write(
    "6. Immediately send BUY MARKET order."
)

st.write(
    "7. Same 2-minute candle cannot trigger twice."
)


# ============================================================
# AUTO REFRESH
# ============================================================

time.sleep(
    REFRESH_SECONDS
)

st.rerun()
