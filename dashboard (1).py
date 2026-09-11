# ============================================================
# dashboard.py
#
# NIFTY AUTOMATIC BUY CE ONLY
# ANGEL ONE SMARTAPI + STREAMLIT
#
# STRATEGY
# ------------------------------------------------------------
# NIFTY 1-minute candles
#        ↓
# COMPLETED 2-minute candles
#        ↓
# SUPERTREND (20, 1.5)
#        ↓
# RED -> GREEN FLIP
#        ↓
# AUTOMATIC BUY ATM CE
#
# IMPORTANT
# ------------------------------------------------------------
# CE BUY ONLY
# NO MANUAL BUY
# NO SELL
# NO EXECUTION CONFIRMATION
#
# placeOrder() / placeOrderFullResponse()
# returns immediately.
#
# The returned order ID is saved immediately.
# ============================================================

import os
import json
import time
from pathlib import Path
from datetime import datetime

import numpy as np
import pandas as pd
import pyotp
import requests
import streamlit as st

from SmartApi import SmartConnect


# ============================================================
# STREAMLIT
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

REFRESH_SECONDS = 10

MARKET_OPEN = "09:15"
MARKET_CLOSE = "15:30"

NIFTY_EXCHANGE = "NSE"
NIFTY_SYMBOL = "NIFTY"
NIFTY_TOKEN = "99926000"

OPTION_EXCHANGE = "NFO"

ORDER_TYPE = "MARKET"
PRODUCT_TYPE = "INTRADAY"
VARIETY = "NORMAL"
DURATION = "DAY"

MAX_SIGNAL_AGE_MINUTES = 5

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

IST = "Asia/Kolkata"


# ============================================================
# TIME
# ============================================================

def now_ist():

    return pd.Timestamp.now(
        tz=IST
    )


# ============================================================
# DEFAULT STATE
# ============================================================

DEFAULT_STATE = {

    "status": "WAIT",

    "last_signal": "",
    "last_signal_candle": "",
    "last_processed_candle": "",

    "last_order_id": "",
    "last_order_time": "",

    "last_option_symbol": "",
    "last_option_token": "",
    "last_strike": "",
    "last_expiry": "",
    "last_quantity": 0,

    "last_ltp": "",

    "last_error": "",

    "engine_message": "",

    "last_order_response": "",
}


# ============================================================
# LOAD STATE
# ============================================================

def load_state():

    if not STATE_FILE.exists():

        return DEFAULT_STATE.copy()

    try:

        with open(
            STATE_FILE,
            "r",
            encoding="utf-8",
        ) as f:

            data = json.load(f)

        result = DEFAULT_STATE.copy()

        if isinstance(data, dict):

            result.update(data)

        return result

    except Exception as e:

        return DEFAULT_STATE.copy()


# ============================================================
# SAVE STATE
# ============================================================

def save_state(state):

    try:

        temp = STATE_FILE.with_suffix(
            ".tmp"
        )

        with open(
            temp,
            "w",
            encoding="utf-8",
        ) as f:

            json.dump(
                state,
                f,
                indent=2,
                default=str,
            )

        temp.replace(
            STATE_FILE
        )

    except Exception as e:

        st.session_state[
            "state_save_error"
        ] = str(e)


# ============================================================
# SESSION
# ============================================================

if "api" not in st.session_state:
    st.session_state.api = None

if "login_status" not in st.session_state:
    st.session_state.login_status = False

if "instrument_df" not in st.session_state:
    st.session_state.instrument_df = None

if "state" not in st.session_state:
    st.session_state.state = load_state()


state = st.session_state.state


# ============================================================
# SECRETS
# ============================================================

def get_secret(name):

    try:

        value = st.secrets.get(
            name
        )

        if value:

            return str(
                value
            ).strip()

    except Exception:

        pass

    value = os.getenv(
        name
    )

    if value:

        return str(
            value
        ).strip()

    return ""


ANGEL_API_KEY = get_secret(
    "ANGEL_API_KEY"
)

ANGEL_CLIENT_ID = get_secret(
    "ANGEL_CLIENT_ID"
)

ANGEL_PASSWORD = get_secret(
    "ANGEL_PASSWORD"
)

ANGEL_TOTP_SECRET = get_secret(
    "ANGEL_TOTP_SECRET"
)


# ============================================================
# TOTP
# ============================================================

def generate_totp(secret):

    if not secret:

        raise RuntimeError(
            "ANGEL_TOTP_SECRET is missing."
        )

    secret = secret.strip()

    if secret.lower().startswith(
        "otpauth://"
    ):

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

        values = params.get(
            "secret"
        )

        if not values:

            raise RuntimeError(
                "TOTP secret missing in otpauth URI."
            )

        secret = values[0]

    secret = (
        secret
        .replace(" ", "")
        .replace("-", "")
        .upper()
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

def login():

    if not ANGEL_API_KEY:
        return False, "ANGEL_API_KEY missing."

    if not ANGEL_CLIENT_ID:
        return False, "ANGEL_CLIENT_ID missing."

    if not ANGEL_PASSWORD:
        return False, "ANGEL_PASSWORD missing."

    if not ANGEL_TOTP_SECRET:
        return False, "ANGEL_TOTP_SECRET missing."

    try:

        totp = generate_totp(
            ANGEL_TOTP_SECRET
        )

        api = SmartConnect(
            api_key=ANGEL_API_KEY
        )

        response = api.generateSession(
            ANGEL_CLIENT_ID,
            ANGEL_PASSWORD,
            totp,
        )

        if not response:

            return (
                False,
                "Empty Angel One login response.",
            )

        if not response.get(
            "status"
        ):

            return (
                False,
                "Login failed: "
                f"{response.get('message')} | "
                f"{response.get('errorcode')}",
            )

        data = response.get(
            "data"
        ) or {}

        if not data.get(
            "jwtToken"
        ):

            return (
                False,
                "JWT token missing after login.",
            )

        st.session_state.api = api
        st.session_state.login_status = True

        return True, "Angel One login successful."

    except Exception as e:

        st.session_state.api = None
        st.session_state.login_status = False

        return (
            False,
            f"Login exception: {e}",
        )


# ============================================================
# INSTRUMENT MASTER
# ============================================================

def load_instrument_master():

    try:

        if INSTRUMENT_FILE.exists():

            age = (
                time.time()
                - INSTRUMENT_FILE.stat().st_mtime
            )

            if age < 86400:

                df = pd.read_json(
                    INSTRUMENT_FILE,
                    dtype=False,
                )

                if not df.empty:

                    return df

        response = requests.get(
            INSTRUMENT_URL,
            timeout=30,
        )

        response.raise_for_status()

        data = response.json()

        with open(
            INSTRUMENT_FILE,
            "w",
            encoding="utf-8",
        ) as f:

            json.dump(
                data,
                f,
            )

        return pd.DataFrame(
            data
        )

    except Exception as e:

        raise RuntimeError(
            f"Instrument master failed: {e}"
        )


# ============================================================
# MARKET HOURS
# ============================================================

def market_is_open():

    current = now_ist()

    if current.weekday() >= 5:

        return False

    current_time = current.time()

    start = pd.Timestamp(
        MARKET_OPEN
    ).time()

    end = pd.Timestamp(
        MARKET_CLOSE
    ).time()

    return (
        start
        <= current_time
        <= end
    )


# ============================================================
# NIFTY LTP
# ============================================================

def get_nifty_ltp(api):

    response = api.ltpData(
        NIFTY_EXCHANGE,
        NIFTY_SYMBOL,
        NIFTY_TOKEN,
    )

    if not response:

        raise RuntimeError(
            "Empty NIFTY LTP response."
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
    ) or {}

    ltp = data.get(
        "ltp"
    )

    if ltp is None:

        raise RuntimeError(
            f"NIFTY LTP missing: {response}"
        )

    return float(
        ltp
    )


# ============================================================
# 1-MINUTE CANDLES
# ============================================================

def get_nifty_1m_candles(api):

    current = now_ist()

    from_time = (
        current
        - pd.Timedelta(days=3)
    )

    response = api.getCandleData(
        {
            "exchange":
                NIFTY_EXCHANGE,

            "symboltoken":
                NIFTY_TOKEN,

            "interval":
                "ONE_MINUTE",

            "fromdate":
                from_time.strftime(
                    "%Y-%m-%d %H:%M"
                ),

            "todate":
                current.strftime(
                    "%Y-%m-%d %H:%M"
                ),
        }
    )

    if not response:

        raise RuntimeError(
            "Empty candle response."
        )

    if not response.get(
        "status"
    ):

        raise RuntimeError(
            "Candle API FAILED | "
            f"message={response.get('message')} | "
            f"errorcode={response.get('errorcode')}"
        )

    rows = response.get(
        "data"
    )

    if not rows:

        raise RuntimeError(
            "No 1-minute NIFTY candles."
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
        df["timestamp"],
        errors="coerce",
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
        "volume",
    ]:

        df[col] = pd.to_numeric(
            df[col],
            errors="coerce",
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

    df = df.sort_values(
        "timestamp"
    )

    df = df.drop_duplicates(
        "timestamp",
        keep="last",
    )

    df = df.set_index(
        "timestamp"
    )

    # Never use the currently forming 1-minute candle.

    current_minute = now_ist().floor(
        "min"
    )

    df = df[
        df.index < current_minute
    ]

    start = pd.Timestamp(
        MARKET_OPEN
    ).time()

    end = pd.Timestamp(
        MARKET_CLOSE
    ).time()

    df = df[
        (df.index.time >= start)
        &
        (df.index.time < end)
    ]

    return df


# ============================================================
# COMPLETED 2-MINUTE CANDLES
# ============================================================

def build_2m_candles(df):

    if df.empty:

        return pd.DataFrame()

    result = (
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
    )

    count = (
        df["close"]
        .resample(
            "2min",
            origin="start_day",
            offset="9h15min",
            label="right",
            closed="left",
        )
        .count()
    )

    result["one_min_count"] = count

    # IMPORTANT:
    # exactly two 1-minute candles = completed 2-minute candle

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

    return result


# ============================================================
# SUPERTREND
# ============================================================

def calculate_supertrend(
    df,
    period=20,
    multiplier=1.5,
):

    data = df.copy()

    high = data["high"]
    low = data["low"]
    close = data["close"]

    prev_close = close.shift(1)

    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)

    atr = tr.ewm(
        alpha=1 / period,
        adjust=False,
        min_periods=period,
    ).mean()

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
    )

    final_lower = pd.Series(
        np.nan,
        index=data.index,
    )

    direction = pd.Series(
        np.nan,
        index=data.index,
    )

    supertrend = pd.Series(
        np.nan,
        index=data.index,
    )

    first = atr.first_valid_index()

    if first is None:

        data["ATR"] = atr
        data["Supertrend"] = supertrend
        data["ST_Direction"] = direction

        data["ST_Green"] = False
        data["ST_Red"] = False
        data["ST_Flip_Green"] = False
        data["ST_Flip_Red"] = False

        return data

    start = data.index.get_loc(
        first
    )

    for i in range(
        start,
        len(data),
    ):

        if i == start:

            final_upper.iloc[i] = (
                basic_upper.iloc[i]
            )

            final_lower.iloc[i] = (
                basic_lower.iloc[i]
            )

            if close.iloc[i] >= hl2.iloc[i]:

                direction.iloc[i] = 1

                supertrend.iloc[i] = (
                    final_lower.iloc[i]
                )

            else:

                direction.iloc[i] = -1

                supertrend.iloc[i] = (
                    final_upper.iloc[i]
                )

            continue

        if (
            basic_upper.iloc[i]
            < final_upper.iloc[i - 1]
            or
            close.iloc[i - 1]
            > final_upper.iloc[i - 1]
        ):

            final_upper.iloc[i] = (
                basic_upper.iloc[i]
            )

        else:

            final_upper.iloc[i] = (
                final_upper.iloc[i - 1]
            )

        if (
            basic_lower.iloc[i]
            > final_lower.iloc[i - 1]
            or
            close.iloc[i - 1]
            < final_lower.iloc[i - 1]
        ):

            final_lower.iloc[i] = (
                basic_lower.iloc[i]
            )

        else:

            final_lower.iloc[i] = (
                final_lower.iloc[i - 1]
            )

        previous = direction.iloc[
            i - 1
        ]

        if previous == -1:

            if (
                close.iloc[i]
                > final_upper.iloc[i]
            ):

                direction.iloc[i] = 1

            else:

                direction.iloc[i] = -1

        else:

            if (
                close.iloc[i]
                < final_lower.iloc[i]
            ):

                direction.iloc[i] = -1

            else:

                direction.iloc[i] = 1

        if direction.iloc[i] == 1:

            supertrend.iloc[i] = (
                final_lower.iloc[i]
            )

        else:

            supertrend.iloc[i] = (
                final_upper.iloc[i]
            )

    data["ATR"] = atr
    data["Supertrend"] = supertrend
    data["ST_Direction"] = direction

    data["ST_Green"] = (
        direction == 1
    )

    data["ST_Red"] = (
        direction == -1
    )

    previous_direction = (
        direction.shift(1)
    )

    data["ST_Flip_Green"] = (
        (previous_direction == -1)
        &
        (direction == 1)
    )

    data["ST_Flip_Red"] = (
        (previous_direction == 1)
        &
        (direction == -1)
    )

    return data


# ============================================================
# EXPIRY
# ============================================================

def parse_expiry(value):

    if value is None:

        return None

    text = str(
        value
    ).strip()

    if not text:

        return None

    formats = [
        "%d%b%Y",
        "%d%b%y",
        "%d-%b-%Y",
        "%d-%b-%y",
        "%d/%m/%Y",
        "%d/%m/%y",
    ]

    for fmt in formats:

        try:

            return datetime.strptime(
                text.upper(),
                fmt,
            ).date()

        except Exception:

            pass

    try:

        dt = pd.to_datetime(
            text,
            errors="coerce",
        )

        if pd.notna(dt):

            return dt.date()

    except Exception:

        pass

    return None


# ============================================================
# STRIKE
# ============================================================

def normalize_strike(value):

    try:

        value = float(
            value
        )

        if value > 100000:

            value /= 100

        return value

    except Exception:

        return np.nan


# ============================================================
# ATM CE
# ============================================================

def select_atm_ce(
    instrument_df,
    spot,
):

    if instrument_df is None:

        raise RuntimeError(
            "Instrument master not loaded."
        )

    if instrument_df.empty:

        raise RuntimeError(
            "Instrument master is empty."
        )

    df = instrument_df.copy()

    df.columns = [
        str(c)
        .strip()
        .lower()
        for c in df.columns
    ]

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
                f"Instrument column missing: {col}"
            )

    # NFO

    df["exch_seg"] = (
        df["exch_seg"]
        .astype(str)
        .str.upper()
        .str.strip()
    )

    df = df[
        df["exch_seg"] == "NFO"
    ]

    # OPTIDX

    df["instrumenttype"] = (
        df["instrumenttype"]
        .astype(str)
        .str.upper()
        .str.strip()
    )

    df = df[
        df["instrumenttype"] == "OPTIDX"
    ]

    # NIFTY

    df["name"] = (
        df["name"]
        .astype(str)
        .str.upper()
        .str.strip()
    )

    df = df[
        df["name"].isin(
            [
                "NIFTY",
                "NIFTY 50",
            ]
        )
    ]

    # CE

    df["symbol"] = (
        df["symbol"]
        .astype(str)
        .str.upper()
        .str.strip()
    )

    df = df[
        df["symbol"].str.endswith(
            "CE"
        )
    ]

    if df.empty:

        raise RuntimeError(
            "No NIFTY CE contracts found."
        )

    # Expiry

    df["expiry_date"] = (
        df["expiry"].apply(
            parse_expiry
        )
    )

    today = now_ist().date()

    df = df[
        df["expiry_date"].notna()
    ]

    df = df[
        df["expiry_date"] >= today
    ]

    if df.empty:

        raise RuntimeError(
            "No future NIFTY CE expiry."
        )

    nearest_expiry = (
        df["expiry_date"].min()
    )

    df = df[
        df["expiry_date"]
        == nearest_expiry
    ]

    # Strike

    df["strike_value"] = (
        df["strike"].apply(
            normalize_strike
        )
    )

    df = df[
        df["strike_value"].notna()
    ]

    if df.empty:

        raise RuntimeError(
            "No valid NIFTY CE strikes."
        )

    # ATM

    index = (
        (
            df["strike_value"]
            - float(spot)
        )
        .abs()
        .idxmin()
    )

    row = df.loc[
        index
    ]

    lotsize = pd.to_numeric(
        row["lotsize"],
        errors="coerce",
    )

    if pd.isna(
        lotsize
    ):

        raise RuntimeError(
            "Invalid NIFTY lot size."
        )

    lotsize = int(
        lotsize
    )

    quantity = (
        lotsize
        * int(LOTS)
    )

    return {

        "symbol":
            str(row["symbol"]),

        "token":
            str(row["token"]),

        "strike":
            float(row["strike_value"]),

        "expiry":
            str(row["expiry"]),

        "lotsize":
            lotsize,

        "quantity":
            quantity,
    }


# ============================================================
# ORDER ID
# ============================================================

def extract_order_id(response):

    if response is None:

        return ""

    if isinstance(
        response,
        str,
    ):

        return response.strip()

    if isinstance(
        response,
        dict,
    ):

        data = response.get(
            "data"
        )

        if isinstance(
            data,
            dict,
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

    return ""


# ============================================================
# DIRECT AUTOMATIC BUY
# ============================================================

def automatic_buy_ce(
    api,
    option,
):

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
            OPTION_EXCHANGE,

        "ordertype":
            ORDER_TYPE,

        "producttype":
            PRODUCT_TYPE,

        "duration":
            DURATION,

        "quantity":
            str(option["quantity"]),
    }

    # --------------------------------------------------------
    # IMPORTANT
    # --------------------------------------------------------
    # Do not use order-book polling.
    # Do not wait for execution.
    #
    # First try placeOrderFullResponse if available.
    # Otherwise use placeOrder.
    # --------------------------------------------------------

    try:

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

    except Exception as e:

        raise RuntimeError(
            "Angel BUY API exception: "
            f"{e}"
        )

    if not response:

        raise RuntimeError(
            "Angel BUY returned empty response."
        )

    order_id = extract_order_id(
        response
    )

    if not order_id:

        raise RuntimeError(
            "BUY request returned no order ID. "
            f"Broker response: {response}"
        )

    return (
        order_id,
        response,
    )


# ============================================================
# PROCESS AUTOMATIC BUY
# ============================================================

def process_automatic_buy(
    api,
    spot,
    st_df,
):

    # --------------------------------------------------------
    # No data
    # --------------------------------------------------------

    if st_df.empty:

        state["engine_message"] = (
            "No completed 2-minute candles."
        )

        state["status"] = "WAIT"

        save_state(state)

        return {
            "status": "WAIT",
            "message":
                state["engine_message"],
            "order_id": "",
        }

    valid = st_df.dropna(
        subset=[
            "ST_Direction",
            "Supertrend",
        ]
    )

    if valid.empty:

        state["engine_message"] = (
            "Waiting for enough candles "
            "to calculate Supertrend."
        )

        state["status"] = "WAIT"

        save_state(state)

        return {
            "status": "WAIT",
            "message":
                state["engine_message"],
            "order_id": "",
        }

    latest = valid.iloc[-1]

    candle = valid.index[-1]

    direction = int(
        latest["ST_Direction"]
    )

    is_green = (
        direction == 1
    )

    is_flip_green = bool(
        latest["ST_Flip_Green"]
    )

    state[
        "last_signal_candle"
    ] = str(
        candle
    )

    # --------------------------------------------------------
    # WAIT
    # --------------------------------------------------------

    if not is_flip_green:

        if is_green:

            message = (
                "GREEN but no new RED → GREEN flip."
            )

        else:

            message = (
                "RED — waiting for RED → GREEN."
            )

        state["status"] = "WAIT"

        state[
            "engine_message"
        ] = message

        save_state(state)

        return {
            "status": "WAIT",
            "message": message,
            "order_id": "",
        }

    # --------------------------------------------------------
    # NEW GREEN FLIP
    # --------------------------------------------------------

    state[
        "last_signal"
    ] = "GREEN FLIP"

    state[
        "status"
    ] = "CONFIRM"

    # --------------------------------------------------------
    # AGE CHECK
    # --------------------------------------------------------

    try:

        candle_ts = pd.Timestamp(
            candle
        )

        if candle_ts.tzinfo is None:

            candle_ts = (
                candle_ts
                .tz_localize(IST)
            )

        else:

            candle_ts = (
                candle_ts
                .tz_convert(IST)
            )

        age = (
            now_ist() - candle_ts
        ).total_seconds() / 60

    except Exception:

        age = 999

    if age > MAX_SIGNAL_AGE_MINUTES:

        message = (
            f"GREEN flip is {age:.1f} minutes old."
        )

        state[
            "engine_message"
        ] = message

        state[
            "last_error"
        ] = message

        save_state(state)

        return {
            "status": "CONFIRM",
            "message": message,
            "order_id": "",
        }

    # --------------------------------------------------------
    # DUPLICATE CHECK
    # --------------------------------------------------------

    candle_text = str(
        candle
    )

    if (
        state.get(
            "last_processed_candle",
            "",
        )
        == candle_text
    ):

        message = (
            "This GREEN flip was already processed."
        )

        state[
            "engine_message"
        ] = message

        save_state(state)

        return {
            "status":
                "CONFIRM",

            "message":
                message,

            "order_id":
                state.get(
                    "last_order_id",
                    "",
                ),
        }

    # --------------------------------------------------------
    # CHECK SPOT
    # --------------------------------------------------------

    if spot is None:

        message = (
            "NIFTY spot unavailable. "
            "BUY not attempted."
        )

        state[
            "last_error"
        ] = message

        state[
            "engine_message"
        ] = message

        save_state(state)

        return {
            "status":
                "CONFIRM",

            "message":
                message,

            "order_id":
                "",
        }

    # --------------------------------------------------------
    # SELECT ATM CE
    # --------------------------------------------------------

    try:

        option = select_atm_ce(
            st.session_state.instrument_df,
            spot,
        )

    except Exception as e:

        message = (
            f"ATM CE selection failed: {e}"
        )

        state[
            "last_error"
        ] = message

        state[
            "engine_message"
        ] = message

        save_state(state)

        return {
            "status":
                "CONFIRM",

            "message":
                message,

            "order_id":
                "",
        }

    # --------------------------------------------------------
    # SAVE OPTION
    # --------------------------------------------------------

    state[
        "last_option_symbol"
    ] = option["symbol"]

    state[
        "last_option_token"
    ] = option["token"]

    state[
        "last_strike"
    ] = option["strike"]

    state[
        "last_expiry"
    ] = option["expiry"]

    state[
        "last_quantity"
    ] = option["quantity"]

    # --------------------------------------------------------
    # LIVE BUY
    # --------------------------------------------------------

    if not LIVE_TRADING:

        message = (
            f"TEST MODE | "
            f"BUY {option['symbol']} | "
            f"Qty {option['quantity']}"
        )

        state[
            "last_processed_candle"
        ] = candle_text

        state[
            "last_order_id"
        ] = "TEST_ORDER"

        state[
            "last_order_time"
        ] = now_ist().strftime(
            "%Y-%m-%d %H:%M:%S %Z"
        )

        state[
            "engine_message"
        ] = message

        state[
            "last_error"
        ] = ""

        save_state(state)

        return {
            "status":
                "CONFIRM",

            "message":
                message,

            "order_id":
                "TEST_ORDER",
        }

    # --------------------------------------------------------
    # ACTUAL AUTOMATIC BUY
    # --------------------------------------------------------

    try:

        state[
            "engine_message"
        ] = (
            "NEW GREEN FLIP → "
            f"BUYING {option['symbol']}..."
        )

        save_state(state)

        order_id, response = (
            automatic_buy_ce(
                api,
                option,
            )
        )

        # ----------------------------------------------------
        # SUCCESS
        # ----------------------------------------------------

        state[
            "last_processed_candle"
        ] = candle_text

        state[
            "last_order_id"
        ] = str(
            order_id
        )

        state[
            "last_order_time"
        ] = now_ist().strftime(
            "%Y-%m-%d %H:%M:%S %Z"
        )

        state[
            "last_error"
        ] = ""

        state[
            "engine_message"
        ] = (
            "AUTOMATIC CE BUY SUBMITTED | "
            f"{option['symbol']} | "
            f"Qty {option['quantity']} | "
            f"Order ID {order_id}"
        )

        state[
            "last_order_response"
        ] = str(
            response
        )

        state[
            "status"
        ] = "CONFIRM"

        save_state(state)

        return {
            "status":
                "CONFIRM",

            "message":
                state[
                    "engine_message"
                ],

            "order_id":
                str(order_id),
        }

    except Exception as e:

        # ----------------------------------------------------
        # BUY FAILED
        # ----------------------------------------------------

        state[
            "last_error"
        ] = str(e)

        state[
            "engine_message"
        ] = (
            "AUTOMATIC BUY FAILED | "
            f"{e}"
        )

        # DO NOT mark the candle processed
        # if the broker BUY failed.

        save_state(state)

        return {
            "status":
                "CONFIRM",

            "message":
                state[
                    "engine_message"
                ],

            "order_id":
                "",
        }


# ============================================================
# HEADER
# ============================================================

st.title(
    "📈 NIFTY Automatic BUY CE"
)

st.caption(
    "2-Minute Supertrend (20, 1.5) | "
    "RED → GREEN = Automatic ATM CE BUY"
)


# ============================================================
# SIDEBAR
# ============================================================

with st.sidebar:

    st.subheader(
        "Trading Settings"
    )

    st.write(
        f"**LIVE_TRADING:** "
        f"{LIVE_TRADING}"
    )

    st.write(
        f"**Supertrend:** "
        f"{ST_PERIOD}, {ST_MULTIPLIER}"
    )

    st.write(
        "**Timeframe:** 2 Minutes"
    )

    st.write(
        "**Direction:** CE BUY ONLY"
    )

    st.write(
        f"**Lots:** {LOTS}"
    )

    st.write(
        f"**Refresh:** {REFRESH_SECONDS}s"
    )

    st.divider()

    if st.button(
        "Login to Angel One",
        use_container_width=True,
    ):

        ok, msg = login()

        if ok:

            st.success(msg)

        else:

            st.error(msg)

    if st.session_state.login_status:

        st.success(
            "Angel One CONNECTED"
        )

    else:

        st.warning(
            "Angel One NOT CONNECTED"
        )


# ============================================================
# MAIN ENGINE
# ============================================================

if not st.session_state.login_status:

    st.warning(
        "Login to Angel One."
    )

else:

    api = st.session_state.api

    # ========================================================
    # INSTRUMENT MASTER
    # ========================================================

    try:

        if (
            st.session_state.instrument_df
            is None
        ):

            with st.spinner(
                "Loading instrument master..."
            ):

                st.session_state.instrument_df = (
                    load_instrument_master()
                )

    except Exception as e:

        state[
            "last_error"
        ] = str(e)

        state[
            "engine_message"
        ] = str(e)

        save_state(state)

        st.error(
            str(e)
        )

        st.stop()

    # ========================================================
    # MARKET
    # ========================================================

    if market_is_open():

        st.success(
            "🟢 MARKET OPEN"
        )

    else:

        st.warning(
            "🔴 MARKET CLOSED"
        )

    # ========================================================
    # NIFTY
    # ========================================================

    spot = None

    try:

        spot = get_nifty_ltp(
            api
        )

        state[
            "last_ltp"
        ] = spot

    except Exception as e:

        state[
            "last_error"
        ] = str(e)

        state[
            "engine_message"
        ] = str(e)

        save_state(state)

    # ========================================================
    # CANDLES
    # ========================================================

    df_1m = pd.DataFrame()

    try:

        df_1m = get_nifty_1m_candles(
            api
        )

    except Exception as e:

        state[
            "last_error"
        ] = str(e)

        state[
            "engine_message"
        ] = str(e)

        save_state(state)

    # ========================================================
    # 2-MINUTE
    # ========================================================

    df_2m = pd.DataFrame()

    if not df_1m.empty:

        df_2m = build_2m_candles(
            df_1m
        )

    # ========================================================
    # SUPERTREND
    # ========================================================

    st_df = pd.DataFrame()

    if not df_2m.empty:

        st_df = calculate_supertrend(
            df_2m,
            ST_PERIOD,
            ST_MULTIPLIER,
        )

    # ========================================================
    # AUTOMATIC BUY ENGINE
    # ========================================================

    result = {
        "status":
            "WAIT",

        "message":
            "Waiting...",

        "order_id":
            "",
    }

    if market_is_open():

        result = process_automatic_buy(
            api,
            spot,
            st_df,
        )

    else:

        state[
            "status"
        ] = "WAIT"

        state[
            "engine_message"
        ] = "Market closed."

        save_state(state)

    # ========================================================
    # METRICS
    # ========================================================

    latest_direction = "WAIT"
    latest_flip = False
    latest_candle = "-"
    latest_close = None
    latest_st = None

    if not st_df.empty:

        valid = st_df.dropna(
            subset=[
                "ST_Direction",
                "Supertrend",
            ]
        )

        if not valid.empty:

            latest = valid.iloc[-1]

            latest_candle = (
                valid.index[-1]
            )

            latest_close = float(
                latest["close"]
            )

            latest_st = float(
                latest["Supertrend"]
            )

            latest_direction = (
                "GREEN"
                if int(
                    latest[
                        "ST_Direction"
                    ]
                ) == 1
                else "RED"
            )

            latest_flip = bool(
                latest[
                    "ST_Flip_Green"
                ]
            )

    c1, c2, c3, c4 = st.columns(4)

    with c1:

        st.metric(
            "NIFTY",
            f"{spot:.2f}"
            if spot is not None
            else "-",
        )

    with c2:

        st.metric(
            "Supertrend",
            latest_direction,
        )

    with c3:

        st.metric(
            "GREEN FLIP",
            "YES"
            if latest_flip
            else "NO",
        )

    with c4:

        st.metric(
            "ST Value",
            f"{latest_st:.2f}"
            if latest_st is not None
            else "-",
        )

    # ========================================================
    # ENGINE MESSAGE
    # ========================================================

    st.divider()

    st.subheader(
        "Automatic Trading Engine"
    )

    engine_message = state.get(
        "engine_message",
        "",
    )

    if engine_message:

        if (
            "FAILED"
            in engine_message.upper()
            or
            "ERROR"
            in engine_message.upper()
        ):

            st.error(
                engine_message
            )

        elif (
            "SUBMITTED"
            in engine_message.upper()
        ):

            st.success(
                engine_message
            )

        else:

            st.info(
                engine_message
            )

    else:

        st.info(
            "Waiting for signal..."
        )

    # ========================================================
    # LAST ORDER
    # ========================================================

    st.divider()

    st.subheader(
        "Last Automatic Order"
    )

    o1, o2, o3, o4 = st.columns(4)

    with o1:

        st.write(
            "**Order ID**"
        )

        order_id = state.get(
            "last_order_id",
            "",
        )

        if order_id:

            st.code(
                str(order_id)
            )

        else:

            st.write("-")

    with o2:

        st.write(
            "**Order Time**"
        )

        st.write(
            state.get(
                "last_order_time",
                "-",
            )
            or "-"
        )

    with o3:

        st.write(
            "**CE Symbol**"
        )

        st.write(
            state.get(
                "last_option_symbol",
                "-",
            )
            or "-"
        )

    with o4:

        st.write(
            "**Quantity**"
        )

        quantity = state.get(
            "last_quantity",
            0,
        )

        st.write(
            str(quantity)
            if quantity
            else "-"
        )

    # ========================================================
    # ATM DETAILS
    # ========================================================

    st.subheader(
        "Selected ATM CE"
    )

    a1, a2, a3 = st.columns(3)

    with a1:

        st.write(
            "**Strike**"
        )

        strike = state.get(
            "last_strike",
            "",
        )

        st.write(
            f"{float(strike):.0f}"
            if strike
            else "-"
        )

    with a2:

        st.write(
            "**Expiry**"
        )

        st.write(
            state.get(
                "last_expiry",
                "-",
            )
            or "-"
        )

    with a3:

        st.write(
            "**Token**"
        )

        st.write(
            state.get(
                "last_option_token",
                "-",
            )
            or "-"
        )

    # ========================================================
    # ERROR
    # ========================================================

    error = state.get(
        "last_error",
        "",
    )

    if error:

        st.divider()

        st.subheader(
            "Last Error"
        )

        st.error(
            error
        )

    # ========================================================
    # SIGNAL DEBUG
    # ========================================================

    with st.expander(
        "Signal Debug"
    ):

        st.write(
            "Latest completed candle:",
            str(latest_candle),
        )

        st.write(
            "Direction:",
            latest_direction,
        )

        st.write(
            "GREEN flip:",
            latest_flip,
        )

        st.write(
            "Last signal candle:",
            state.get(
                "last_signal_candle",
                "",
            ),
        )

        st.write(
            "Last processed candle:",
            state.get(
                "last_processed_candle",
                "",
            ),
        )

        st.write(
            "Spot:",
            spot,
        )

        st.write(
            "1-minute candles:",
            len(df_1m),
        )

        st.write(
            "2-minute candles:",
            len(df_2m),
        )

        st.write(
            "LIVE_TRADING:",
            LIVE_TRADING,
        )

    # ========================================================
    # SUPERTREND TABLE
    # ========================================================

    with st.expander(
        "2-Minute Supertrend Data"
    ):

        if not st_df.empty:

            cols = [
                "open",
                "high",
                "low",
                "close",
                "one_min_count",
                "ATR",
                "Supertrend",
                "ST_Direction",
                "ST_Green",
                "ST_Red",
                "ST_Flip_Green",
                "ST_Flip_Red",
            ]

            cols = [
                c
                for c in cols
                if c in st_df.columns
            ]

            st.dataframe(
                st_df[cols].tail(30),
                use_container_width=True,
            )

        else:

            st.write(
                "No completed 2-minute candles."
            )


# ============================================================
# AUTO REFRESH
# ============================================================

time.sleep(
    REFRESH_SECONDS
)

st.rerun()
