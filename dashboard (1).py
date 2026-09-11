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
# Completed 2-minute candles
#        ↓
# Supertrend (20, 1.5)
#        ↓
# RED -> GREEN FLIP
#        ↓
# CONFIRM
#        ↓
# Select nearest ATM NIFTY CE
#        ↓
# AUTOMATIC MARKET BUY
#
# IMPORTANT
# ------------------------------------------------------------
# - CE BUY ONLY
# - No manual BUY button
# - No SELL logic
# - Only completed 2-minute candles are used
# - Same signal candle cannot be processed twice
# - Order ID comes directly from placeOrder response
# - Order time is saved immediately
# - Does NOT wait for broker execution confirmation
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
# STREAMLIT CONFIG
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

MARKET_OPEN = "09:15"
MARKET_CLOSE = "15:30"

MAX_SIGNAL_AGE_MINUTES = 5

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

IST = "Asia/Kolkata"


# ============================================================
# TIME
# ============================================================

def now_ist():
    return pd.Timestamp.now(tz=IST)


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
        result.update(data)

        return result

    except Exception:

        return DEFAULT_STATE.copy()


# ============================================================
# SAVE STATE
# ============================================================

def save_state(state):

    try:

        temp_file = STATE_FILE.with_suffix(".tmp")

        with open(
            temp_file,
            "w",
            encoding="utf-8",
        ) as f:

            json.dump(
                state,
                f,
                indent=2,
                default=str,
            )

        temp_file.replace(
            STATE_FILE
        )

    except Exception as e:

        st.session_state[
            "state_save_error"
        ] = str(e)


# ============================================================
# SESSION STATE
# ============================================================

if "api" not in st.session_state:
    st.session_state.api = None

if "login_status" not in st.session_state:
    st.session_state.login_status = False

if "login_message" not in st.session_state:
    st.session_state.login_message = ""

if "instrument_df" not in st.session_state:
    st.session_state.instrument_df = None

if "state" not in st.session_state:
    st.session_state.state = load_state()


state = st.session_state.state


# ============================================================
# CREDENTIALS
# ============================================================

def get_secret(name):

    try:

        value = st.secrets.get(name)

        if value is not None:

            value = str(value).strip()

            if value:
                return value

    except Exception:
        pass

    value = os.getenv(name)

    if value:
        return str(value).strip()

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

            secret_list = params.get(
                "secret"
            )

            if not secret_list:
                raise RuntimeError(
                    "No secret found inside otpauth URI."
                )

            secret = secret_list[0]

        except Exception as e:

            raise RuntimeError(
                f"Invalid otpauth URI: {e}"
            )

    secret = (
        secret
        .replace(" ", "")
        .replace("-", "")
        .upper()
        .strip()
    )

    try:

        return pyotp.TOTP(
            secret
        ).now()

    except Exception as e:

        raise RuntimeError(
            f"Invalid TOTP secret: {e}"
        )


# ============================================================
# LOGIN
# ============================================================

def login():

    if not ANGEL_API_KEY:
        return False, "ANGEL_API_KEY is missing."

    if not ANGEL_CLIENT_ID:
        return False, "ANGEL_CLIENT_ID is missing."

    if not ANGEL_PASSWORD:
        return False, "ANGEL_PASSWORD is missing."

    if not ANGEL_TOTP_SECRET:
        return False, "ANGEL_TOTP_SECRET is missing."

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
                "Angel One returned an empty login response.",
            )

        if not response.get("status"):

            return (
                False,
                "Login failed: "
                f"{response.get('message')} | "
                f"{response.get('errorcode')}",
            )

        data = response.get(
            "data"
        ) or {}

        jwt_token = data.get(
            "jwtToken"
        )

        if not jwt_token:

            return (
                False,
                "Login succeeded but jwtToken is missing.",
            )

        st.session_state.api = api
        st.session_state.login_status = True
        st.session_state.login_message = (
            "Login successful"
        )

        return True, "Login successful"

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
                indent=2,
            )

        return pd.DataFrame(
            data
        )

    except Exception as e:

        raise RuntimeError(
            f"Instrument master failed: {e}"
        )


# ============================================================
# NIFTY LTP
# ============================================================

def get_nifty_ltp(api):

    try:

        response = api.ltpData(
            NIFTY_EXCHANGE,
            NIFTY_SYMBOL,
            NIFTY_TOKEN,
        )

        if not response:

            raise RuntimeError(
                "Empty LTP response."
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
                f"LTP missing in response: {response}"
            )

        return float(ltp)

    except Exception as e:

        raise RuntimeError(
            f"NIFTY LTP FAILED | {e}"
        )


# ============================================================
# HISTORICAL 1-MINUTE DATA
# ============================================================

def get_nifty_1m_candles(api):

    current = now_ist()

    from_time = (
        current
        - pd.Timedelta(days=3)
    )

    try:

        response = api.getCandleData(
            {
                "exchange": NIFTY_EXCHANGE,
                "symboltoken": NIFTY_TOKEN,
                "interval": CANDLE_INTERVAL,
                "fromdate": from_time.strftime(
                    "%Y-%m-%d %H:%M"
                ),
                "todate": current.strftime(
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
                "Candle API failed | "
                f"message={response.get('message')} | "
                f"errorcode={response.get('errorcode')}"
            )

        rows = response.get(
            "data"
        )

        if not rows:

            raise RuntimeError(
                "No NIFTY candle data returned."
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

        # ----------------------------------------------------
        # TIMESTAMP
        # ----------------------------------------------------

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

        # ----------------------------------------------------
        # NUMERIC
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
            subset=["timestamp"],
            keep="last",
        )

        df = df.set_index(
            "timestamp"
        )

        # ----------------------------------------------------
        # REMOVE CURRENT FORMING CANDLE
        # ----------------------------------------------------

        current_minute = (
            current.floor("min")
        )

        df = df[
            df.index < current_minute
        ]

        # ----------------------------------------------------
        # MARKET HOURS
        # ----------------------------------------------------

        open_time = pd.Timestamp(
            MARKET_OPEN
        ).time()

        close_time = pd.Timestamp(
            MARKET_CLOSE
        ).time()

        df = df[
            (df.index.time >= open_time)
            &
            (df.index.time < close_time)
        ]

        return df

    except Exception as e:

        raise RuntimeError(
            f"1-minute candle failed: {e}"
        )


# ============================================================
# BUILD COMPLETED 2-MINUTE CANDLES
# ============================================================

def build_2m_candles(df_1m):

    if (
        df_1m is None
        or df_1m.empty
    ):

        return pd.DataFrame()

    df = df_1m.copy()

    agg = df.resample(
        "2min",
        origin="start_day",
        offset="9h15min",
        label="right",
        closed="left",
    )

    result = agg.agg(
        {
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum",
        }
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

    # ONLY COMPLETE 2-MINUTE CANDLES

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

    current = now_ist()

    result = result[
        result.index <= current
    ]

    result = result[
        result.index.time
        < pd.Timestamp(
            "15:31"
        ).time()
    ]

    return result


# ============================================================
# SUPERTREND
# ============================================================

def calculate_supertrend(
    df,
    period=20,
    multiplier=1.5,
):

    if (
        df is None
        or df.empty
    ):

        return pd.DataFrame()

    data = df.copy()

    high = data["high"]
    low = data["low"]
    close = data["close"]

    previous_close = close.shift(1)

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
            tr3,
        ],
        axis=1,
    ).max(axis=1)

    # Wilder RMA

    atr = true_range.ewm(
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
        dtype=float,
    )

    final_lower = pd.Series(
        np.nan,
        index=data.index,
        dtype=float,
    )

    supertrend = pd.Series(
        np.nan,
        index=data.index,
        dtype=float,
    )

    direction = pd.Series(
        np.nan,
        index=data.index,
        dtype=float,
    )

    first_valid = (
        atr.first_valid_index()
    )

    if first_valid is None:

        data["ATR"] = atr
        data["Basic_Upper"] = basic_upper
        data["Basic_Lower"] = basic_lower
        data["Final_Upper"] = final_upper
        data["Final_Lower"] = final_lower
        data["Supertrend"] = supertrend
        data["ST_Direction"] = direction

        data["ST_Green"] = False
        data["ST_Red"] = False
        data["ST_Flip_Green"] = False
        data["ST_Flip_Red"] = False

        return data

    start_pos = data.index.get_loc(
        first_valid
    )

    for i in range(
        start_pos,
        len(data),
    ):

        if i == start_pos:

            final_upper.iloc[i] = (
                basic_upper.iloc[i]
            )

            final_lower.iloc[i] = (
                basic_lower.iloc[i]
            )

            if (
                close.iloc[i]
                >= hl2.iloc[i]
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

            continue

        # ----------------------------------------------------
        # FINAL UPPER
        # ----------------------------------------------------

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

        # ----------------------------------------------------
        # FINAL LOWER
        # ----------------------------------------------------

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

        previous_direction = (
            direction.iloc[i - 1]
        )

        # ----------------------------------------------------
        # DIRECTION
        # ----------------------------------------------------

        if previous_direction == -1:

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

        # ----------------------------------------------------
        # SUPERTREND
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
        data["ST_Direction"] == 1
    )

    data["ST_Red"] = (
        data["ST_Direction"] == -1
    )

    previous_direction = (
        data["ST_Direction"].shift(1)
    )

    data["ST_Flip_Green"] = (
        (previous_direction == -1)
        &
        (data["ST_Direction"] == 1)
    )

    data["ST_Flip_Red"] = (
        (previous_direction == 1)
        &
        (data["ST_Direction"] == -1)
    )

    return data


# ============================================================
# LATEST SIGNAL
# ============================================================

def get_latest_signal(st_df):

    if (
        st_df is None
        or st_df.empty
    ):

        return {
            "status": "WAIT",
            "signal_candle": None,
            "direction": None,
            "flip_green": False,
        }

    valid = st_df.dropna(
        subset=[
            "ST_Direction",
            "Supertrend",
        ]
    )

    if valid.empty:

        return {
            "status": "WAIT",
            "signal_candle": None,
            "direction": None,
            "flip_green": False,
        }

    latest = valid.iloc[-1]

    latest_time = valid.index[-1]

    direction = int(
        latest["ST_Direction"]
    )

    flip_green = bool(
        latest["ST_Flip_Green"]
    )

    if flip_green:

        return {
            "status": "CONFIRM",
            "signal_candle": latest_time,
            "direction": direction,
            "flip_green": True,
        }

    return {
        "status": "WAIT",
        "signal_candle": latest_time,
        "direction": direction,
        "flip_green": False,
    }


# ============================================================
# EXPIRY PARSER
# ============================================================

def parse_expiry(value):

    try:

        if value is None:
            return None

        text = str(value).strip()

        if not text:
            return None

        if "-" in text or "/" in text:

            dt = pd.to_datetime(
                text,
                errors="coerce",
            )

            if pd.isna(dt):
                return None

            return dt.date()

        for fmt in [
            "%d%b%Y",
            "%d%b%y",
            "%d-%b-%Y",
            "%d-%b-%y",
            "%d/%m/%Y",
            "%d/%m/%y",
        ]:

            try:

                return datetime.strptime(
                    text.upper(),
                    fmt,
                ).date()

            except Exception:
                pass

        return None

    except Exception:

        return None


# ============================================================
# STRIKE NORMALIZATION
# ============================================================

def normalize_strike(value):

    try:

        x = float(value)

        if x > 100000:
            x = x / 100.0

        return x

    except Exception:

        return np.nan


# ============================================================
# SELECT ATM NIFTY CE
# ============================================================

def select_atm_ce(
    instrument_df,
    spot,
):

    if (
        instrument_df is None
        or instrument_df.empty
    ):

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

    missing = [
        c
        for c in required
        if c not in df.columns
    ]

    if missing:

        raise RuntimeError(
            f"Instrument master missing columns: {missing}"
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
        df["exch_seg"] == "NFO"
    ]

    # --------------------------------------------------------
    # OPTIDX
    # --------------------------------------------------------

    df["instrumenttype"] = (
        df["instrumenttype"]
        .astype(str)
        .str.upper()
        .str.strip()
    )

    df = df[
        df["instrumenttype"]
        == "OPTIDX"
    ]

    # --------------------------------------------------------
    # NIFTY
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # CE ONLY
    # --------------------------------------------------------

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
            "No NIFTY CE options found."
        )

    # --------------------------------------------------------
    # EXPIRY
    # --------------------------------------------------------

    df["expiry_date"] = (
        df["expiry"]
        .apply(parse_expiry)
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
            "No future NIFTY CE expiry found."
        )

    nearest_expiry = (
        df["expiry_date"].min()
    )

    df = df[
        df["expiry_date"]
        == nearest_expiry
    ]

    # --------------------------------------------------------
    # STRIKE
    # --------------------------------------------------------

    df["strike_value"] = (
        df["strike"]
        .apply(normalize_strike)
    )

    df = df[
        df["strike_value"].notna()
    ]

    if df.empty:

        raise RuntimeError(
            "No valid NIFTY CE strikes found."
        )

    # --------------------------------------------------------
    # ATM
    # --------------------------------------------------------

    nearest_row_index = (
        (
            df["strike_value"]
            - float(spot)
        )
        .abs()
        .idxmin()
    )

    row = df.loc[
        nearest_row_index
    ]

    # --------------------------------------------------------
    # LOT SIZE
    # --------------------------------------------------------

    lotsize = pd.to_numeric(
        row["lotsize"],
        errors="coerce",
    )

    if pd.isna(lotsize):

        raise RuntimeError(
            "Invalid lot size."
        )

    lotsize = int(
        lotsize
    )

    quantity = (
        lotsize * int(LOTS)
    )

    if quantity <= 0:

        raise RuntimeError(
            "Invalid order quantity."
        )

    return {
        "symbol": str(
            row["symbol"]
        ),
        "token": str(
            row["token"]
        ),
        "strike": float(
            row["strike_value"]
        ),
        "expiry": str(
            row["expiry"]
        ),
        "lotsize": lotsize,
        "quantity": quantity,
    }


# ============================================================
# EXTRACT ORDER ID
# ============================================================

def extract_order_id(response):

    if response is None:
        return ""

    # Direct string

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
# AUTOMATIC MARKET BUY CE
# ============================================================

def automatic_buy_ce(
    api,
    option,
):

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
            str(option["quantity"]),
    }

    try:

        response = api.placeOrder(
            order_params
        )

        order_id = extract_order_id(
            response
        )

        if not order_id:

            raise RuntimeError(
                "Angel One returned no order ID. "
                f"Response={response}"
            )

        return (
            str(order_id),
            response,
        )

    except Exception as e:

        raise RuntimeError(
            f"Automatic BUY CE failed: {e}"
        )


# ============================================================
# DUPLICATE PROTECTION
# ============================================================

def already_processed_signal(
    signal_candle
):

    if signal_candle is None:
        return False

    candle_text = str(
        signal_candle
    )

    return (
        state.get(
            "last_processed_candle",
            "",
        )
        == candle_text
    )


# ============================================================
# PROCESS SIGNAL
# ============================================================

def process_signal(
    api,
    spot,
    st_df,
):

    signal = get_latest_signal(
        st_df
    )

    status = signal[
        "status"
    ]

    signal_candle = signal[
        "signal_candle"
    ]

    # --------------------------------------------------------
    # WAIT
    # --------------------------------------------------------

    if status != "CONFIRM":

        state["status"] = "WAIT"

        if signal_candle is not None:

            state[
                "last_signal_candle"
            ] = str(
                signal_candle
            )

        save_state(state)

        return {
            "status": "WAIT",
            "message":
                "Waiting for RED → GREEN flip.",
            "order_id": "",
        }

    # --------------------------------------------------------
    # CONFIRM
    # --------------------------------------------------------

    state["status"] = "CONFIRM"

    state["last_signal"] = (
        "GREEN FLIP"
    )

    state[
        "last_signal_candle"
    ] = str(
        signal_candle
    )

    # --------------------------------------------------------
    # SIGNAL AGE
    # --------------------------------------------------------

    current = now_ist()

    try:

        signal_ts = pd.Timestamp(
            signal_candle
        )

        if signal_ts.tzinfo is None:

            signal_ts = (
                signal_ts
                .tz_localize(IST)
            )

        else:

            signal_ts = (
                signal_ts
                .tz_convert(IST)
            )

        age_minutes = (
            current - signal_ts
        ).total_seconds() / 60.0

    except Exception:

        age_minutes = 999

    if (
        age_minutes
        > MAX_SIGNAL_AGE_MINUTES
    ):

        state["last_error"] = (
            "GREEN signal is too old: "
            f"{age_minutes:.1f} minutes"
        )

        save_state(state)

        return {
            "status": "CONFIRM",
            "message":
                "GREEN detected but signal is too old.",
            "order_id": "",
        }

    # --------------------------------------------------------
    # DUPLICATE PROTECTION
    # --------------------------------------------------------

    if already_processed_signal(
        signal_candle
    ):

        return {
            "status": "CONFIRM",
            "message":
                "This signal candle was already processed.",
            "order_id":
                state.get(
                    "last_order_id",
                    "",
                ),
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

        state["last_error"] = (
            f"ATM CE selection failed: {e}"
        )

        save_state(state)

        return {
            "status": "CONFIRM",
            "message":
                f"ATM CE selection failed: {e}",
            "order_id": "",
        }

    # --------------------------------------------------------
    # SAVE OPTION DETAILS
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

    if LIVE_TRADING:

        try:

            order_id, response = (
                automatic_buy_ce(
                    api,
                    option,
                )
            )

            # =================================================
            # IMPORTANT
            #
            # Save immediately after placeOrder returns
            # order ID.
            #
            # NO execution confirmation is requested.
            # =================================================

            state[
                "last_processed_candle"
            ] = str(
                signal_candle
            )

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
                "status"
            ] = "CONFIRM"

            save_state(
                state
            )

            return {
                "status":
                    "CONFIRM",

                "message":
                    "AUTOMATIC BUY CE SUBMITTED | "
                    f"{option['symbol']} | "
                    f"Order ID: {order_id}",

                "order_id":
                    str(order_id),
            }

        except Exception as e:

            state[
                "last_error"
            ] = str(e)

            save_state(
                state
            )

            return {
                "status":
                    "CONFIRM",

                "message":
                    str(e),

                "order_id":
                    "",
            }

    # --------------------------------------------------------
    # TEST MODE
    # --------------------------------------------------------

    state[
        "last_processed_candle"
    ] = str(
        signal_candle
    )

    state[
        "last_order_id"
    ] = "TEST_ORDER"

    state[
        "last_order_time"
    ] = now_ist().strftime(
        "%Y-%m-%d %H:%M:%S %Z"
    )

    save_state(
        state
    )

    return {
        "status":
            "CONFIRM",

        "message":
            f"TEST MODE | BUY {option['symbol']}",

        "order_id":
            "TEST_ORDER",
    }


# ============================================================
# MARKET HOURS
# ============================================================

def market_is_open():

    current = now_ist()

    if current.weekday() >= 5:
        return False

    current_time = (
        current.time()
    )

    open_time = pd.Timestamp(
        MARKET_OPEN
    ).time()

    close_time = pd.Timestamp(
        MARKET_CLOSE
    ).time()

    return (
        open_time
        <= current_time
        <= close_time
    )


# ============================================================
# HEADER
# ============================================================

st.title(
    "📈 NIFTY Automatic BUY CE"
)

st.caption(
    "1-Minute → Completed 2-Minute → "
    "Supertrend (20, 1.5) → RED → GREEN → "
    "CONFIRM → Automatic ATM CE BUY"
)


# ============================================================
# SIDEBAR
# ============================================================

with st.sidebar:

    st.subheader(
        "Trading Settings"
    )

    st.write(
        "**Live Trading:** "
        + (
            "ON"
            if LIVE_TRADING
            else "OFF"
        )
    )

    st.write(
        f"**Supertrend:** "
        f"{ST_PERIOD}, {ST_MULTIPLIER}"
    )

    st.write(
        "**Timeframe:** 2 Minute"
    )

    st.write(
        "**Instrument:** NIFTY ATM CE"
    )

    st.write(
        f"**Lots:** {LOTS}"
    )

    st.write(
        f"**Refresh:** "
        f"{REFRESH_SECONDS}s"
    )

    st.divider()

    if st.button(
        "Login to Angel One",
        use_container_width=True,
    ):

        with st.spinner(
            "Logging in..."
        ):

            ok, message = login()

        if ok:

            st.success(
                message
            )

        else:

            st.error(
                message
            )

    if (
        st.session_state.login_status
    ):

        st.success(
            "Angel One: Connected"
        )

    else:

        st.warning(
            "Angel One: Not connected"
        )


# ============================================================
# LOGIN CHECK
# ============================================================

if not st.session_state.login_status:

    st.warning(
        "Login to Angel One to start the "
        "live NIFTY signal."
    )

    st.info(
        "The dashboard will remain WAIT until "
        "a completed 2-minute candle produces "
        "a RED → GREEN Supertrend flip."
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
                "Loading NFO instrument master..."
            ):

                st.session_state.instrument_df = (
                    load_instrument_master()
                )

    except Exception as e:

        st.error(
            f"Instrument master error: {e}"
        )

        st.stop()

    # ========================================================
    # MARKET STATUS
    # ========================================================

    if market_is_open():

        st.success(
            "🟢 NSE market is OPEN"
        )

    else:

        st.warning(
            "🔴 NSE market is CLOSED"
        )

    # ========================================================
    # NIFTY LTP
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

    # ========================================================
    # 1-MINUTE DATA
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

    # ========================================================
    # 2-MINUTE DATA
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
    # PROCESS AUTOMATIC SIGNAL
    # ========================================================

    result = {
        "status":
            "WAIT",

        "message":
            "Waiting...",

        "order_id":
            "",
    }

    if (
        market_is_open()
        and not st_df.empty
        and spot is not None
    ):

        result = process_signal(
            api,
            spot,
            st_df,
        )

    elif not market_is_open():

        state[
            "status"
        ] = "WAIT"

        result = {
            "status":
                "WAIT",

            "message":
                "Market closed.",

            "order_id":
                "",
        }

    save_state(
        state
    )

    # ========================================================
    # CURRENT STATUS
    # ========================================================

    latest_direction = "WAIT"
    latest_candle_time = "-"
    latest_close = None
    latest_supertrend = None
    latest_flip = False

    if not st_df.empty:

        valid = st_df.dropna(
            subset=[
                "ST_Direction",
                "Supertrend",
            ]
        )

        if not valid.empty:

            latest = valid.iloc[-1]

            latest_candle_time = (
                valid.index[-1]
            )

            latest_close = float(
                latest["close"]
            )

            latest_supertrend = float(
                latest["Supertrend"]
            )

            latest_flip = bool(
                latest[
                    "ST_Flip_Green"
                ]
            )

            if int(
                latest[
                    "ST_Direction"
                ]
            ) == 1:

                latest_direction = (
                    "GREEN"
                )

            else:

                latest_direction = (
                    "RED"
                )

    # ========================================================
    # METRICS
    # ========================================================

    col1, col2, col3, col4 = (
        st.columns(4)
    )

    with col1:

        if spot is not None:

            st.metric(
                "NIFTY Spot",
                f"{spot:.2f}",
            )

        else:

            st.metric(
                "NIFTY Spot",
                "-",
            )

    with col2:

        st.metric(
            "2-Min Supertrend",
            latest_direction,
        )

    with col3:

        st.metric(
            "Signal",
            state.get(
                "status",
                "WAIT",
            ),
        )

    with col4:

        if (
            latest_supertrend
            is not None
        ):

            st.metric(
                "ST Value",
                f"{latest_supertrend:.2f}",
            )

        else:

            st.metric(
                "ST Value",
                "-",
            )

    # ========================================================
    # BIG SIGNAL
    # ========================================================

    st.divider()

    status = state.get(
        "status",
        "WAIT",
    )

    if status == "CONFIRM":

        st.success(
            "🟢 CONFIRM — RED → GREEN detected"
        )

        if result.get(
            "message"
        ):

            st.info(
                result["message"]
            )

    else:

        st.info(
            "🟡 WAIT — Waiting for RED → GREEN flip"
        )

    # ========================================================
    # SIGNAL INFORMATION
    # ========================================================

    st.subheader(
        "Live Signal"
    )

    c1, c2, c3 = st.columns(3)

    with c1:

        st.write(
            "**Latest completed 2-minute candle:**"
        )

        st.write(
            str(
                latest_candle_time
            )
        )

    with c2:

        st.write(
            "**Supertrend direction:**"
        )

        st.write(
            latest_direction
        )

    with c3:

        st.write(
            "**RED → GREEN flip:**"
        )

        st.write(
            "YES"
            if latest_flip
            else "NO"
        )

    # ========================================================
    # SELECTED ATM CE
    # ========================================================

    st.divider()

    st.subheader(
        "Selected ATM CE"
    )

    option_cols = st.columns(5)

    with option_cols[0]:

        st.write(
            "**Symbol**"
        )

        st.write(
            state.get(
                "last_option_symbol",
                "-",
            )
            or "-"
        )

    with option_cols[1]:

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

    with option_cols[2]:

        st.write(
            "**Strike**"
        )

        strike = state.get(
            "last_strike",
            "",
        )

        if strike:

            try:

                st.write(
                    f"{float(strike):.0f}"
                )

            except Exception:

                st.write(
                    str(strike)
                )

        else:

            st.write("-")

    with option_cols[3]:

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

    with option_cols[4]:

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
    # ORDER INFORMATION
    # ========================================================

    st.divider()

    st.subheader(
        "Automatic Order"
    )

    order_cols = st.columns(2)

    with order_cols[0]:

        st.write(
            "**Last Order ID**"
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

    with order_cols[1]:

        st.write(
            "**Order Time**"
        )

        order_time = state.get(
            "last_order_time",
            "",
        )

        if order_time:

            st.write(
                order_time
            )

        else:

            st.write("-")

    # ========================================================
    # LAST SIGNAL CANDLE
    # ========================================================

    st.divider()

    signal_cols = st.columns(3)

    with signal_cols[0]:

        st.write(
            "**Last Signal**"
        )

        st.write(
            state.get(
                "last_signal",
                "-",
            )
            or "-"
        )

    with signal_cols[1]:

        st.write(
            "**Signal Candle**"
        )

        st.write(
            state.get(
                "last_signal_candle",
                "-",
            )
            or "-"
        )

    with signal_cols[2]:

        st.write(
            "**Processed Candle**"
        )

        st.write(
            state.get(
                "last_processed_candle",
                "-",
            )
            or "-"
        )

    # ========================================================
    # ERROR
    # ========================================================

    last_error = state.get(
        "last_error",
        "",
    )

    if last_error:

        st.divider()

        st.error(
            last_error
        )

    # ========================================================
    # DEBUG TABLE
    # ========================================================

    with st.expander(
        "2-Minute Supertrend Data"
    ):

        if not st_df.empty:

            display_columns = [
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

            available_columns = [
                c
                for c in display_columns
                if c in st_df.columns
            ]

            debug_df = (
                st_df[
                    available_columns
                ]
                .tail(20)
                .copy()
            )

            st.dataframe(
                debug_df,
                use_container_width=True,
            )

        else:

            st.write(
                "No completed 2-minute data available."
            )

    # ========================================================
    # 2-MINUTE CHART
    # ========================================================

    if not st_df.empty:

        st.divider()

        st.subheader(
            "NIFTY 2-Minute Supertrend"
        )

        chart_df = (
            st_df
            .tail(100)
            .copy()
        )

        chart_data = pd.DataFrame(
            index=chart_df.index
        )

        chart_data["NIFTY"] = (
            chart_df["close"]
        )

        chart_data["Supertrend"] = (
            chart_df["Supertrend"]
        )

        st.line_chart(
            chart_data,
            use_container_width=True,
        )


# ============================================================
# AUTO REFRESH
# ============================================================

time.sleep(
    REFRESH_SECONDS
)

st.rerun()
