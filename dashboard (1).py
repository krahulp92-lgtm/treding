# ============================================================
# dashboard.py
#
# NIFTY 50 AUTOMATIC BUY CE ONLY
# ANGEL ONE SMARTAPI + STREAMLIT
#
# STRATEGY
# ------------------------------------------------------------
# 2-MINUTE SUPERTREND (20, 1.5)
#
# GREEN = YES
#      ↓
# AUTOMATIC BUY ATM NIFTY CE
#
# NO GREEN FLIP REQUIRED
#
# NO 5-MINUTE
# NO 15-MINUTE
# NO 4-HOUR
# NO MANUAL BUY/SELL BUTTONS
# ============================================================

import os
import json
import time
import re

from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import urlparse, parse_qs

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
    page_title="NIFTY 2-Minute Automatic BUY CE",
    page_icon="📈",
    layout="wide",
)


# ============================================================
# CONFIG
# ============================================================

IST = ZoneInfo("Asia/Kolkata")

ST_PERIOD = 20
ST_MULTIPLIER = 1.5

NIFTY_TOKEN = "99926000"
NIFTY_SYMBOL = "NIFTY 50"

REFRESH_SECONDS = int(
    os.getenv(
        "REFRESH_SECONDS",
        "10"
    )
)


# ============================================================
# TRADING MODE
#
# TRUE  = PAPER / NO REAL ORDER
# FALSE = LIVE / REAL ORDER
#
# SAFETY DEFAULT = TRUE
# ============================================================

PAPER_TRADING = (
    os.getenv(
        "PAPER_TRADING",
        "true"
    )
    .strip()
    .lower()
    in (
        "1",
        "true",
        "yes",
        "y",
        "on",
    )
)


# ============================================================
# FILES
# ============================================================

STATE_FILE = Path(
    "nifty_2min_ce_state.json"
)

INSTRUMENT_FILE = Path(
    "OpenAPIScripMaster.json"
)

INSTRUMENT_URL = (
    "https://margincalculator.angelone.in/"
    "OpenAPI_File/files/OpenAPIScripMaster.json"
)


# ============================================================
# SECRETS
# ============================================================

def get_secret(name, default=""):

    try:

        if name in st.secrets:

            value = st.secrets[name]

            if value is not None:

                return str(value).strip()

    except Exception:

        pass

    return str(
        os.getenv(
            name,
            default
        )
    ).strip()


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
# SESSION STATE
# ============================================================

DEFAULTS = {

    "api": None,

    "login_status": "NOT CONNECTED",

    "last_error": "",

    "last_message": "Ready",

    "spot": None,

    "st2": None,

    "st2_direction": None,

    "st2_green": False,

    "st2_red": False,

    "st2_flip_green": False,

    "st2_flip_red": False,

    "signal": "WAIT",

    "signal_time": None,

    "option_symbol": None,

    "option_token": None,

    "option_expiry": None,

    "option_strike": None,

    "option_lot_size": None,

    "option_ltp": None,

    "last_order_id": None,

    "last_order_time": None,

    "last_processed_candle": None,

    "orders": [],

    "instruments": None,

    "last_update": None,
}


for key, value in DEFAULTS.items():

    if key not in st.session_state:

        st.session_state[key] = value


# ============================================================
# LOAD PERSISTENT ORDER STATE
# ============================================================

def load_saved_state():

    if not STATE_FILE.exists():

        return

    try:

        with open(
            STATE_FILE,
            "r",
            encoding="utf-8"
        ) as f:

            data = json.load(f)

        if not isinstance(
            data,
            dict
        ):

            return

        if data.get(
            "last_order_id"
        ):

            st.session_state[
                "last_order_id"
            ] = data[
                "last_order_id"
            ]

        if data.get(
            "last_order_time"
        ):

            st.session_state[
                "last_order_time"
            ] = data[
                "last_order_time"
            ]

        if data.get(
            "last_processed_candle"
        ):

            st.session_state[
                "last_processed_candle"
            ] = data[
                "last_processed_candle"
            ]

    except Exception:

        pass


if (
    st.session_state.get(
        "last_order_id"
    ) is None
):

    load_saved_state()


# ============================================================
# HELPERS
# ============================================================

def now_ist():

    return datetime.now(IST)


def safe_float(value):

    try:

        if value is None:

            return None

        value = str(value).strip()

        if not value:

            return None

        return float(value)

    except Exception:

        return None


def safe_int(value, default=0):

    try:

        return int(float(value))

    except Exception:

        return default


def normalize_expiry(value):

    if value is None:

        return None

    text = str(value).strip()

    if not text:

        return None

    formats = [
        "%d%b%Y",
        "%d%b%y",
        "%Y-%m-%d",
        "%d-%b-%Y",
        "%d-%b-%y",
    ]

    for fmt in formats:

        try:

            return datetime.strptime(
                text.upper(),
                fmt
            ).date()

        except Exception:

            pass

    try:

        return pd.to_datetime(
            text
        ).date()

    except Exception:

        return None


def normalize_strike(value):

    value = safe_float(value)

    if value is None:

        return None

    # Angel One commonly stores
    # strike * 100

    if value >= 100000:

        value = value / 100.0

    return value


# ============================================================
# TOTP
# ============================================================

def get_totp_secret(raw):

    if not raw:

        return ""

    raw = str(raw).strip()

    if raw.lower().startswith(
        "otpauth://"
    ):

        try:

            parsed = urlparse(raw)

            query = parse_qs(
                parsed.query
            )

            values = query.get(
                "secret"
            )

            if values:

                return (
                    values[0]
                    .strip()
                    .replace(
                        " ",
                        ""
                    )
                )

        except Exception:

            pass

    return raw.replace(
        " ",
        ""
    )


def validate_totp_secret(secret):

    if not secret:

        return (
            False,
            "ANGEL_TOTP_SECRET is missing."
        )

    secret = (
        secret
        .upper()
        .strip()
    )

    if not re.fullmatch(
        r"[A-Z2-7]+=*",
        secret
    ):

        return (
            False,
            "ANGEL_TOTP_SECRET must be "
            "the Base32 secret, not the "
            "6-digit OTP."
        )

    try:

        pyotp.TOTP(
            secret
        ).now()

        return (
            True,
            "TOTP secret is valid."
        )

    except Exception as exc:

        return (
            False,
            f"Invalid TOTP secret: {exc}"
        )


# ============================================================
# ANGEL LOGIN
# ============================================================

def angel_login():

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

    secret = get_totp_secret(
        ANGEL_TOTP_SECRET
    )

    valid, message = (
        validate_totp_secret(
            secret
        )
    )

    if not valid:

        raise RuntimeError(
            message
        )

    try:

        totp = pyotp.TOTP(
            secret
        ).now()

        api = SmartConnect(
            api_key=ANGEL_API_KEY
        )

        response = api.generateSession(
            ANGEL_CLIENT_ID,
            ANGEL_PASSWORD,
            totp
        )

        if not isinstance(
            response,
            dict
        ):

            raise RuntimeError(
                f"Invalid login response: {response}"
            )

        if not response.get(
            "status"
        ):

            raise RuntimeError(
                "Angel One login failed: "
                + str(response)
            )

        st.session_state[
            "api"
        ] = api

        st.session_state[
            "login_status"
        ] = "CONNECTED"

        return api

    except Exception as exc:

        st.session_state[
            "login_status"
        ] = "LOGIN FAILED"

        raise RuntimeError(
            f"Angel One login failed: {exc}"
        )


# ============================================================
# NIFTY LTP
# ============================================================

def get_nifty_ltp(api):

    if api is None:

        raise RuntimeError(
            "Angel One API is not connected."
        )

    try:

        response = api.ltpData(
            "NSE",
            NIFTY_SYMBOL,
            NIFTY_TOKEN
        )

    except Exception as exc:

        raise RuntimeError(
            f"NIFTY LTP API exception: {exc}"
        )

    if not isinstance(
        response,
        dict
    ):

        raise RuntimeError(
            f"Invalid NIFTY LTP response: {response}"
        )

    if not response.get(
        "status"
    ):

        raise RuntimeError(
            "NIFTY LTP failed: "
            + str(response)
        )

    data = (
        response.get(
            "data"
        )
        or {}
    )

    ltp = safe_float(
        data.get(
            "ltp"
        )
    )

    if ltp is None:

        raise RuntimeError(
            "NIFTY LTP missing: "
            + str(response)
        )

    return ltp


# ============================================================
# NIFTY 1-MINUTE DATA
# → 2-MINUTE CANDLES
# ============================================================

def get_nifty_2min_candles(
    api,
    days=30
):

    end = now_ist()

    start = (
        end
        - timedelta(
            days=days
        )
    )

    params = {

        "exchange": "NSE",

        "symboltoken": NIFTY_TOKEN,

        "interval": "ONE_MINUTE",

        "fromdate": start.strftime(
            "%Y-%m-%d %H:%M"
        ),

        "todate": end.strftime(
            "%Y-%m-%d %H:%M"
        ),
    }

    try:

        response = api.getCandleData(
            params
        )

    except Exception as exc:

        raise RuntimeError(
            f"Candle API exception: {exc}"
        )

    if not isinstance(
        response,
        dict
    ):

        raise RuntimeError(
            f"Invalid candle response: {response}"
        )

    if not response.get(
        "status"
    ):

        raise RuntimeError(
            "Candle API failed: "
            + str(response)
        )

    rows = response.get(
        "data"
    )

    if not rows:

        raise RuntimeError(
            "Candle API returned no NIFTY candles."
        )

    records = []

    for row in rows:

        if len(row) < 5:

            continue

        records.append(
            {
                "datetime": row[0],
                "open": row[1],
                "high": row[2],
                "low": row[3],
                "close": row[4],
                "volume": (
                    row[5]
                    if len(row) > 5
                    else 0
                ),
            }
        )

    df = pd.DataFrame(
        records
    )

    if df.empty:

        raise RuntimeError(
            "No valid NIFTY candles."
        )

    df["datetime"] = pd.to_datetime(
        df["datetime"],
        errors="coerce"
    )

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
            "datetime",
            "open",
            "high",
            "low",
            "close"
        ]
    )

    df = df.sort_values(
        "datetime"
    )

    df = df.drop_duplicates(
        "datetime",
        keep="last"
    )

    df = df.set_index(
        "datetime"
    )

    # ========================================================
    # 09:15-09:17
    # 09:17-09:19
    # 09:19-09:21
    # ========================================================

    df2 = (
        df[
            [
                "open",
                "high",
                "low",
                "close",
                "volume"
            ]
        ]
        .resample(
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
                "volume": "sum"
            }
        )
    )

    df2 = df2.dropna(
        subset=[
            "open",
            "high",
            "low",
            "close"
        ]
    )

    df2 = df2.reset_index()

    return df2


# ============================================================
# SUPERTREND 20, 1.5
# ============================================================

def supertrend(
    df,
    period=20,
    multiplier=1.5
):

    x = df.copy()

    high = x[
        "high"
    ].astype(float)

    low = x[
        "low"
    ].astype(float)

    close = x[
        "close"
    ].astype(float)

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
            tr3
        ],
        axis=1
    ).max(
        axis=1
    )

    # Wilder ATR

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

    final_upper = (
        basic_upper.copy()
    )

    final_lower = (
        basic_lower.copy()
    )

    direction = pd.Series(
        index=x.index,
        dtype="int64"
    )

    st_value = pd.Series(
        index=x.index,
        dtype="float64"
    )

    if len(x) == 0:

        return x

    direction.iloc[0] = 1
    st_value.iloc[0] = np.nan

    for i in range(
        1,
        len(x)
    ):

        previous_final_upper = (
            final_upper.iloc[i - 1]
        )

        previous_final_lower = (
            final_lower.iloc[i - 1]
        )

        if (
            pd.isna(
                previous_final_upper
            )
            or basic_upper.iloc[i]
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

        if (
            pd.isna(
                previous_final_lower
            )
            or basic_lower.iloc[i]
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

        previous_direction = (
            direction.iloc[i - 1]
        )

        if pd.isna(
            atr.iloc[i]
        ):

            direction.iloc[i] = (
                previous_direction
            )

            st_value.iloc[i] = np.nan

            continue

        if previous_direction == 1:

            if (
                close.iloc[i]
                <= final_lower.iloc[i]
            ):

                direction.iloc[i] = -1

                st_value.iloc[i] = (
                    final_upper.iloc[i]
                )

            else:

                direction.iloc[i] = 1

                st_value.iloc[i] = (
                    final_lower.iloc[i]
                )

        else:

            if (
                close.iloc[i]
                >= final_upper.iloc[i]
            ):

                direction.iloc[i] = 1

                st_value.iloc[i] = (
                    final_lower.iloc[i]
                )

            else:

                direction.iloc[i] = -1

                st_value.iloc[i] = (
                    final_upper.iloc[i]
                )

    x["ATR"] = atr

    x["Basic_Upper"] = basic_upper

    x["Basic_Lower"] = basic_lower

    x["Final_Upper"] = final_upper

    x["Final_Lower"] = final_lower

    x["Supertrend"] = st_value

    x["ST_Direction"] = direction

    x["ST_Green"] = (
        direction == 1
    )

    x["ST_Red"] = (
        direction == -1
    )

    previous_green = (
        x["ST_Green"]
        .shift(1)
        .fillna(False)
        .astype(bool)
    )

    previous_red = (
        x["ST_Red"]
        .shift(1)
        .fillna(False)
        .astype(bool)
    )

    x["ST_Flip_Green"] = (
        x["ST_Green"]
        & ~previous_green
    )

    x["ST_Flip_Red"] = (
        x["ST_Red"]
        & ~previous_red
    )

    return x


# ============================================================
# CALCULATE 2-MIN SUPERTREND
# ============================================================

def calculate_2min_supertrend(
    df2
):

    if df2 is None or df2.empty:

        return pd.DataFrame()

    x = df2.copy()

    x["datetime"] = pd.to_datetime(
        x["datetime"],
        errors="coerce"
    )

    x = x.dropna(
        subset=[
            "datetime"
        ]
    )

    x = x.sort_values(
        "datetime"
    )

    x = x.drop_duplicates(
        "datetime",
        keep="last"
    )

    for col in [
        "open",
        "high",
        "low",
        "close",
        "volume"
    ]:

        if col in x.columns:

            x[col] = pd.to_numeric(
                x[col],
                errors="coerce"
            )

    x = x.dropna(
        subset=[
            "open",
            "high",
            "low",
            "close"
        ]
    )

    return supertrend(
        x.reset_index(
            drop=True
        ),
        period=ST_PERIOD,
        multiplier=ST_MULTIPLIER
    )


# ============================================================
# LAST CLOSED 2-MIN CANDLE
# ============================================================

def get_last_closed_2min(df):

    if df is None or df.empty:

        return None

    now = now_ist()

    market_start = now.replace(
        hour=9,
        minute=15,
        second=0,
        microsecond=0
    )

    if now < market_start:

        return None

    elapsed_seconds = (
        now - market_start
    ).total_seconds()

    bucket_number = int(
        elapsed_seconds // 120
    )

    current_candle_start = (
        market_start
        + timedelta(
            seconds=(
                bucket_number * 120
            )
        )
    )

    closed = df[
        df["datetime"]
        < current_candle_start
    ]

    if closed.empty:

        return None

    return closed.iloc[-1]


# ============================================================
# SIGNAL
#
# IMPORTANT:
# GREEN = YES
# NO FLIP REQUIRED
# ============================================================

def get_2min_signal(st2):

    if st2 is None or st2.empty:

        raise RuntimeError(
            "2-minute Supertrend data is empty."
        )

    row = get_last_closed_2min(
        st2
    )

    if row is None:

        raise RuntimeError(
            "No closed 2-minute candle available."
        )

    green = bool(
        row["ST_Green"]
    )

    red = bool(
        row["ST_Red"]
    )

    flip_green = bool(
        row["ST_Flip_Green"]
    )

    flip_red = bool(
        row["ST_Flip_Red"]
    )

    # ========================================================
    # NEW RULE
    #
    # GREEN = YES
    #     ↓
    # BUY CE
    #
    # GREEN FLIP NOT REQUIRED
    # ========================================================

    if green:

        signal = "BUY CE"

    else:

        signal = "WAIT"

    return {

        "signal": signal,

        "candle_time": row[
            "datetime"
        ],

        "close": safe_float(
            row["close"]
        ),

        "supertrend": safe_float(
            row["Supertrend"]
        ),

        "direction": safe_int(
            row["ST_Direction"]
        ),

        "green": green,

        "red": red,

        "flip_green": flip_green,

        "flip_red": flip_red
    }


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
        start
        <= now
        <= end
    )


# ============================================================
# INSTRUMENT MASTER
# ============================================================

def download_instrument_master():

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
                "Instrument master is not a list."
            )

        with open(
            INSTRUMENT_FILE,
            "w",
            encoding="utf-8"
        ) as f:

            json.dump(
                data,
                f,
                ensure_ascii=False
            )

        return data

    except Exception as exc:

        if INSTRUMENT_FILE.exists():

            try:

                with open(
                    INSTRUMENT_FILE,
                    "r",
                    encoding="utf-8"
                ) as f:

                    data = json.load(f)

                if (
                    isinstance(
                        data,
                        list
                    )
                    and data
                ):

                    return data

            except Exception:

                pass

        raise RuntimeError(
            "Instrument master download failed: "
            + str(exc)
        )


def load_instruments():

    existing = (
        st.session_state.get(
            "instruments"
        )
    )

    if existing:

        return existing

    if INSTRUMENT_FILE.exists():

        try:

            with open(
                INSTRUMENT_FILE,
                "r",
                encoding="utf-8"
            ) as f:

                data = json.load(f)

            if (
                isinstance(
                    data,
                    list
                )
                and data
            ):

                st.session_state[
                    "instruments"
                ] = data

                return data

        except Exception:

            pass

    data = download_instrument_master()

    st.session_state[
        "instruments"
    ] = data

    return data


# ============================================================
# ATM NIFTY CE
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
            "NIFTY spot unavailable."
        )

    today = now_ist().date()

    candidates = []

    for item in instruments:

        if not isinstance(
            item,
            dict
        ):

            continue

        exchange = str(
            item.get(
                "exch_seg",
                ""
            )
        ).upper()

        symbol = str(
            item.get(
                "symbol",
                ""
            )
        ).upper()

        name = str(
            item.get(
                "name",
                ""
            )
        ).upper()

        instrument_type = str(
            item.get(
                "instrumenttype",
                ""
            )
        ).upper()

        if exchange != "NFO":

            continue

        if name != "NIFTY":

            continue

        if not symbol.endswith(
            "CE"
        ):

            continue

        if instrument_type not in (
            "",
            "OPTIDX"
        ):

            continue

        expiry = normalize_expiry(
            item.get(
                "expiry"
            )
        )

        if expiry is None:

            continue

        if expiry < today:

            continue

        strike = normalize_strike(
            item.get(
                "strike"
            )
        )

        if strike is None:

            continue

        token = str(
            item.get(
                "token",
                ""
            )
        ).strip()

        if not token:

            continue

        lot_size = safe_int(
            item.get(
                "lotsize"
            ),
            0
        )

        if lot_size <= 0:

            continue

        candidates.append(
            {
                "symbol": symbol,
                "token": token,
                "expiry": expiry,
                "strike": strike,
                "lot_size": lot_size,
                "distance": abs(
                    strike - spot
                )
            }
        )

    if not candidates:

        raise RuntimeError(
            "No NIFTY CE contracts found."
        )

    nearest_expiry = min(
        item["expiry"]
        for item in candidates
    )

    candidates = [
        item
        for item in candidates
        if item["expiry"]
        == nearest_expiry
    ]

    selected = min(
        candidates,
        key=lambda x: x[
            "distance"
        ]
    )

    return selected


# ============================================================
# OPTION LTP
# ============================================================

def get_option_ltp(
    api,
    option
):

    try:

        response = api.ltpData(
            "NFO",
            option["symbol"],
            option["token"]
        )

    except Exception as exc:

        raise RuntimeError(
            f"Option LTP API exception: {exc}"
        )

    if not isinstance(
        response,
        dict
    ):

        raise RuntimeError(
            f"Invalid option LTP response: {response}"
        )

    if not response.get(
        "status"
    ):

        raise RuntimeError(
            "Option LTP failed: "
            + str(response)
        )

    data = (
        response.get(
            "data"
        )
        or {}
    )

    ltp = safe_float(
        data.get(
            "ltp"
        )
    )

    if ltp is None:

        raise RuntimeError(
            "Option LTP missing: "
            + str(response)
        )

    return ltp


# ============================================================
# PAPER ORDER
# ============================================================

def create_paper_order(
    option,
    ltp
):

    order_id = (
        "PAPER-"
        + now_ist().strftime(
            "%Y%m%d%H%M%S"
        )
    )

    return {

        "order_id": order_id,

        "symbol": option[
            "symbol"
        ],

        "token": option[
            "token"
        ],

        "transaction_type": "BUY",

        "quantity": option[
            "lot_size"
        ],

        "order_type": "MARKET",

        "price": ltp,

        "status": "PAPER ORDER",

        "time": now_ist().strftime(
            "%Y-%m-%d %H:%M:%S"
        )
    }


# ============================================================
# REAL ANGEL ONE ORDER
#
# FIX:
# Use placeOrderFullResponse when available.
# If response is empty, verify Order Book.
# ============================================================

def place_real_buy_order(
    api,
    option
):

    order_params = {

        "variety": "NORMAL",

        "tradingsymbol": option[
            "symbol"
        ],

        "symboltoken": str(
            option["token"]
        ),

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
        )
    }

    # ========================================================
    # PLACE ORDER
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

            response = api.placeOrder(
                order_params
            )

    except Exception as exc:

        raise RuntimeError(
            "Angel One order API exception: "
            + str(exc)
        )

    # ========================================================
    # EMPTY RESPONSE
    # → VERIFY ORDER BOOK
    # ========================================================

    if response is None:

        try:

            time.sleep(1)

            order_book_response = (
                api.orderBook()
            )

            if isinstance(
                order_book_response,
                dict
            ):

                if order_book_response.get(
                    "status"
                ):

                    orders = (
                        order_book_response.get(
                            "data"
                        )
                        or []
                    )

                    if isinstance(
                        orders,
                        list
                    ):

                        for broker_order in orders:

                            if not isinstance(
                                broker_order,
                                dict
                            ):

                                continue

                            symbol = str(
                                broker_order.get(
                                    "tradingsymbol",
                                    ""
                                )
                            ).upper()

                            if (
                                symbol
                                == option[
                                    "symbol"
                                ].upper()
                            ):

                                order_id = (
                                    broker_order.get(
                                        "orderid"
                                    )
                                )

                                return {

                                    "orderid":
                                        order_id,

                                    "status":
                                        str(
                                            broker_order.get(
                                                "status",
                                                "ORDER FOUND"
                                            )
                                        ),

                                    "raw":
                                        broker_order
                                }

        except Exception as exc:

            raise RuntimeError(
                "Order response was empty and "
                "Order Book verification failed: "
                + str(exc)
            )

        raise RuntimeError(
            "Angel One returned empty response "
            "and the order was NOT confirmed "
            "in Order Book."
        )

    # ========================================================
    # DICT RESPONSE
    # ========================================================

    if isinstance(
        response,
        dict
    ):

        status = response.get(
            "status"
        )

        if status is False:

            raise RuntimeError(
                "Angel One rejected order: "
                + str(response)
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
                or data.get(
                    "orderId"
                )
            )

        elif isinstance(
            data,
            str
        ):

            order_id = data

        order_id = (
            order_id
            or response.get(
                "orderid"
            )
            or response.get(
                "orderId"
            )
        )

        return {

            "orderid": order_id,

            "status": (
                "ORDER SENT"
                if order_id
                else "ORDER RESPONSE RECEIVED"
            ),

            "raw": response
        }

    # ========================================================
    # STRING RESPONSE
    # ========================================================

    if isinstance(
        response,
        str
    ):

        return {

            "orderid": response,

            "status": "ORDER ID RECEIVED",

            "raw": response
        }

    return {

        "orderid": None,

        "status": "UNKNOWN RESPONSE",

        "raw": response
    }


# ============================================================
# AUTOMATIC BUY CE
# ============================================================

def automatic_buy_ce(
    api,
    instruments,
    spot,
    signal_info
):

    if signal_info[
        "signal"
    ] != "BUY CE":

        return None

    candle_key = str(
        signal_info[
            "candle_time"
        ]
    )

    # ========================================================
    # SAME CANDLE PROTECTION
    # ========================================================

    if (
        st.session_state[
            "last_processed_candle"
        ]
        == candle_key
    ):

        return None

    # ========================================================
    # ONLY ONE OPEN/AUTOMATED ORDER
    #
    # Prevents repeated CE buying every 10 seconds.
    # ========================================================

    if st.session_state[
        "last_order_id"
    ]:

        return None

    # ========================================================
    # SELECT ATM CE
    # ========================================================

    option = select_atm_nifty_ce(
        instruments,
        spot
    )

    # ========================================================
    # CE LTP
    # ========================================================

    option_ltp = get_option_ltp(
        api,
        option
    )

    # ========================================================
    # SAVE OPTION
    # ========================================================

    st.session_state[
        "option_symbol"
    ] = option[
        "symbol"
    ]

    st.session_state[
        "option_token"
    ] = option[
        "token"
    ]

    st.session_state[
        "option_expiry"
    ] = str(
        option[
            "expiry"
        ]
    )

    st.session_state[
        "option_strike"
    ] = option[
        "strike"
    ]

    st.session_state[
        "option_lot_size"
    ] = option[
        "lot_size"
    ]

    st.session_state[
        "option_ltp"
    ] = option_ltp

    # ========================================================
    # PAPER / LIVE
    # ========================================================

    if PAPER_TRADING:

        order = create_paper_order(
            option,
            option_ltp
        )

    else:

        result = place_real_buy_order(
            api,
            option
        )

        order_id = result.get(
            "orderid"
        )

        order_status = result.get(
            "status",
            "UNKNOWN"
        )

        order = {

            "order_id": str(
                order_id
                if order_id
                else "UNKNOWN"
            ),

            "symbol": option[
                "symbol"
            ],

            "token": option[
                "token"
            ],

            "transaction_type": "BUY",

            "quantity": option[
                "lot_size"
            ],

            "order_type": "MARKET",

            "price": option_ltp,

            "status": order_status,

            "time": now_ist().strftime(
                "%Y-%m-%d %H:%M:%S"
            )
        }

    # ========================================================
    # SAVE STATE
    # ========================================================

    st.session_state[
        "last_order_id"
    ] = order[
        "order_id"
    ]

    st.session_state[
        "last_order_time"
    ] = order[
        "time"
    ]

    st.session_state[
        "last_processed_candle"
    ] = candle_key

    orders = (
        st.session_state[
            "orders"
        ]
        or []
    )

    orders.insert(
        0,
        order
    )

    st.session_state[
        "orders"
    ] = orders

    # ========================================================
    # PERSISTENT STATE
    # ========================================================

    try:

        with open(
            STATE_FILE,
            "w",
            encoding="utf-8"
        ) as f:

            json.dump(
                {
                    "last_order_id":
                        st.session_state[
                            "last_order_id"
                        ],

                    "last_order_time":
                        st.session_state[
                            "last_order_time"
                        ],

                    "last_processed_candle":
                        candle_key
                },
                f,
                indent=2,
                default=str
            )

    except Exception:

        pass

    return order


# ============================================================
# ANGEL ORDER BOOK
# ============================================================

def get_order_book(api):

    if api is None:

        return []

    try:

        response = api.orderBook()

        if not isinstance(
            response,
            dict
        ):

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

    except Exception:

        pass

    return []


# ============================================================
# AUTOMATION ENGINE
# ============================================================

def run_automation():

    # ========================================================
    # LOGIN
    # ========================================================

    try:

        api = st.session_state.get(
            "api"
        )

        if api is None:

            api = angel_login()

    except Exception as exc:

        st.session_state[
            "last_error"
        ] = str(exc)

        return

    # ========================================================
    # MARKET
    # ========================================================

    if not market_is_open():

        st.session_state[
            "last_message"
        ] = (
            "Market closed — "
            "waiting for 09:15-15:30."
        )

        return

    # ========================================================
    # NIFTY SPOT
    # ========================================================

    try:

        spot = get_nifty_ltp(
            api
        )

        st.session_state[
            "spot"
        ] = spot

        st.session_state[
            "last_error"
        ] = ""

    except Exception as exc:

        st.session_state[
            "last_error"
        ] = str(exc)

        return

    # ========================================================
    # 1M → 2M DATA
    # ========================================================

    try:

        df2 = get_nifty_2min_candles(
            api,
            days=30
        )

    except Exception as exc:

        st.session_state[
            "last_error"
        ] = str(exc)

        return

    # ========================================================
    # SUPERTREND
    # ========================================================

    try:

        st2 = calculate_2min_supertrend(
            df2
        )

    except Exception as exc:

        st.session_state[
            "last_error"
        ] = (
            "2-minute Supertrend error: "
            + str(exc)
        )

        return

    if st2.empty:

        st.session_state[
            "last_error"
        ] = (
            "2-minute Supertrend returned no data."
        )

        return

    # ========================================================
    # SIGNAL
    # ========================================================

    try:

        signal_info = get_2min_signal(
            st2
        )

    except Exception as exc:

        st.session_state[
            "last_error"
        ] = (
            "2-minute signal error: "
            + str(exc)
        )

        return

    # ========================================================
    # UPDATE DISPLAY
    # ========================================================

    st.session_state[
        "st2"
    ] = signal_info[
        "supertrend"
    ]

    st.session_state[
        "st2_direction"
    ] = signal_info[
        "direction"
    ]

    st.session_state[
        "st2_green"
    ] = signal_info[
        "green"
    ]

    st.session_state[
        "st2_red"
    ] = signal_info[
        "red"
    ]

    st.session_state[
        "st2_flip_green"
    ] = signal_info[
        "flip_green"
    ]

    st.session_state[
        "st2_flip_red"
    ] = signal_info[
        "flip_red"
    ]

    st.session_state[
        "signal"
    ] = signal_info[
        "signal"
    ]

    st.session_state[
        "signal_time"
    ] = signal_info[
        "candle_time"
    ]

    # ========================================================
    # GREEN = YES
    # → AUTOMATIC BUY CE
    # ========================================================

    if signal_info[
        "green"
    ]:

        try:

            instruments = (
                load_instruments()
            )

            order = automatic_buy_ce(
                api,
                instruments,
                spot,
                signal_info
            )

            if order:

                st.session_state[
                    "last_message"
                ] = (
                    "🟢 GREEN = YES → "
                    "AUTOMATIC BUY CE: "
                    + order[
                        "symbol"
                    ]
                )

            else:

                st.session_state[
                    "last_message"
                ] = (
                    "🟢 GREEN = YES. "
                    "Order already processed "
                    "or duplicate protection active."
                )

        except Exception as exc:

            st.session_state[
                "last_error"
            ] = (
                "Automatic BUY CE error: "
                + str(exc)
            )

    else:

        st.session_state[
            "last_message"
        ] = (
            "WAIT — 2-minute Supertrend is RED."
        )

    st.session_state[
        "last_update"
    ] = now_ist()


# ============================================================
# HEADER
# ============================================================

st.title(
    "📈 NIFTY 2-Minute Automatic BUY CE"
)

st.caption(
    "ONLY 2-Minute Supertrend (20, 1.5)"
)

st.caption(
    "GREEN = YES → AUTOMATIC BUY ATM NIFTY CE"
)


# ============================================================
# MODE
# ============================================================

if PAPER_TRADING:

    st.warning(
        "🟡 PAPER TRADING MODE — "
        "NO REAL ANGEL ONE ORDER WILL BE SENT."
    )

else:

    st.error(
        "🔴 LIVE TRADING MODE — "
        "REAL AUTOMATIC ORDERS ARE ENABLED."
    )


# ============================================================
# RUN
# ============================================================

run_automation()


# ============================================================
# VARIABLES
# ============================================================

spot = st.session_state.get(
    "spot"
)

st2 = st.session_state.get(
    "st2"
)

signal = st.session_state.get(
    "signal",
    "WAIT"
)

option_ltp = st.session_state.get(
    "option_ltp"
)


# ============================================================
# METRICS
# ============================================================

c1, c2, c3, c4 = st.columns(4)


with c1:

    st.metric(
        "NIFTY Spot",
        "-"
        if spot is None
        else f"₹{spot:,.2f}"
    )


with c2:

    st.metric(
        "2M Supertrend",
        "-"
        if st2 is None
        else f"₹{st2:,.2f}"
    )


with c3:

    st.metric(
        "2M Direction",
        (
            "GREEN"
            if st.session_state[
                "st2_green"
            ]

            else "RED"
            if st.session_state[
                "st2_red"
            ]

            else "-"
        )
    )


with c4:

    st.metric(
        "Signal",
        signal
    )


# ============================================================
# SIGNAL
# ============================================================

st.subheader(
    "Automatic Signal"
)

if (
    signal == "BUY CE"
    and st.session_state[
        "st2_green"
    ]
):

    st.success(
        "🟢 GREEN = YES → BUY CE"
    )

else:

    st.info(
        "⏳ WAIT — Supertrend is not GREEN."
    )


signal_time = (
    st.session_state.get(
        "signal_time"
    )
)

if signal_time:

    st.write(
        "Closed 2-minute candle:",
        str(signal_time)
    )


# ============================================================
# CONDITIONS
# ============================================================

st.subheader(
    "2-Minute Supertrend Status"
)

a, b, c, d = st.columns(4)


with a:

    st.write("GREEN")

    if st.session_state[
        "st2_green"
    ]:

        st.success("YES")

    else:

        st.write("NO")


with b:

    st.write("RED")

    if st.session_state[
        "st2_red"
    ]:

        st.error("YES")

    else:

        st.write("NO")


with c:

    st.write("GREEN FLIP")

    st.write(
        "YES"
        if st.session_state[
            "st2_flip_green"
        ]
        else "NO"
    )


with d:

    st.write("RED FLIP")

    st.write(
        "YES"
        if st.session_state[
            "st2_flip_red"
        ]
        else "NO"
    )


# ============================================================
# OPTION
# ============================================================

st.subheader(
    "Selected ATM NIFTY CE"
)

o1, o2, o3, o4, o5 = st.columns(5)


with o1:

    st.metric(
        "Symbol",
        st.session_state.get(
            "option_symbol"
        )
        or "-"
    )


with o2:

    strike = (
        st.session_state.get(
            "option_strike"
        )
    )

    st.metric(
        "Strike",
        "-"
        if strike is None
        else f"{strike:,.0f}"
    )


with o3:

    st.metric(
        "Expiry",
        st.session_state.get(
            "option_expiry"
        )
        or "-"
    )


with o4:

    lot_size = (
        st.session_state.get(
            "option_lot_size"
        )
    )

    st.metric(
        "Lot Size",
        "-"
        if lot_size is None
        else str(lot_size)
    )


with o5:

    st.metric(
        "CE LTP",
        "-"
        if option_ltp is None
        else f"₹{option_ltp:,.2f}"
    )


# ============================================================
# ORDER STATUS
# ============================================================

st.subheader(
    "Automatic Order Status"
)

last_order_id = (
    st.session_state.get(
        "last_order_id"
    )
)

if last_order_id:

    st.success(
        f"Order ID: {last_order_id}"
    )

    st.write(
        "Order time:",
        st.session_state.get(
            "last_order_time"
        )
        or "-"
    )

else:

    st.info(
        "No automatic CE order has been placed."
    )


# ============================================================
# LOCAL ORDER BOOK
# ============================================================

st.subheader(
    "Order Book"
)

local_orders = (
    st.session_state.get(
        "orders"
    )
    or []
)

if local_orders:

    order_df = pd.DataFrame(
        local_orders
    )

    st.dataframe(
        order_df,
        use_container_width=True,
        hide_index=True
    )

else:

    st.info(
        "No automatic orders yet."
    )


# ============================================================
# LIVE ANGEL ONE ORDER BOOK
# ============================================================

if not PAPER_TRADING:

    st.subheader(
        "Angel One Order Book"
    )

    api = st.session_state.get(
        "api"
    )

    broker_orders = get_order_book(
        api
    )

    if broker_orders:

        broker_df = pd.DataFrame(
            broker_orders
        )

        st.dataframe(
            broker_df,
            use_container_width=True,
            hide_index=True
        )

    else:

        st.info(
            "No orders returned by Angel One."
        )


# ============================================================
# SYSTEM STATUS
# ============================================================

st.subheader(
    "System Status"
)

s1, s2, s3 = st.columns(3)


with s1:

    st.write(
        "Angel One:",
        st.session_state.get(
            "login_status",
            "NOT CONNECTED"
        )
    )


with s2:

    st.write(
        "Market:",
        (
            "OPEN"
            if market_is_open()
            else "CLOSED"
        )
    )


with s3:

    st.write(
        "Last update:",
        str(
            st.session_state.get(
                "last_update"
            )
            or "-"
        )
    )


# ============================================================
# ERROR
# ============================================================

last_error = (
    st.session_state.get(
        "last_error"
    )
)

if last_error:

    st.error(
        last_error
    )


# ============================================================
# MESSAGE
# ============================================================

last_message = (
    st.session_state.get(
        "last_message"
    )
)

if last_message:

    st.info(
        last_message
    )


# ============================================================
# STRATEGY
# ============================================================

with st.expander(
    "Strategy Configuration"
):

    st.write(
        "Timeframe: 2-minute ONLY"
    )

    st.write(
        "Supertrend Period: 20"
    )

    st.write(
        "Supertrend Multiplier: 1.5"
    )

    st.write(
        "ENTRY CONDITION: GREEN = YES"
    )

    st.write(
        "GREEN FLIP: NOT REQUIRED"
    )

    st.write(
        "Action: Automatic BUY ATM NIFTY CE"
    )

    st.write(
        "5-minute: NOT USED"
    )

    st.write(
        "15-minute: NOT USED"
    )

    st.write(
        "4-hour: NOT USED"
    )

    st.write(
        "NIFTY Symbol: NIFTY 50"
    )

    st.write(
        "NIFTY Token: 99926000"
    )

    st.write(
        "Trading mode:",
        (
            "PAPER"
            if PAPER_TRADING
            else "LIVE"
        )
    )


# ============================================================
# AUTO REFRESH
# ============================================================

time.sleep(
    REFRESH_SECONDS
)

st.rerun()
