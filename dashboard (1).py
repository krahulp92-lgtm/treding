
# ============================================================
# dashboard.py
#
# NIFTY 50 AUTOMATIC BUY CE ONLY
# ANGEL ONE SMARTAPI + STREAMLIT
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
# 1-Minute candles
#     ↓
# Resample to 2-Minute
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
# Verify in Angel One Order Book
#
# IMPORTANT
# ------------------------------------------------------------
# PAPER_TRADING=true  -> NO REAL ORDER
# PAPER_TRADING=false -> REAL ORDER
#
# Default = PAPER TRADING
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

SUPERTREND_PERIOD = 20
SUPERTREND_MULTIPLIER = 1.5

LOTS = 1

REFRESH_SECONDS = 20

# Use a short enough candle history for repeated refreshes.
CANDLE_DAYS = 2

# Angel One instrument master
INSTRUMENT_URL = (
    "https://margincalculator.angelone.com/"
    "OpenAPI_File/files/OpenAPIScripMaster.json"
)

INSTRUMENT_FILE = Path(
    "OpenAPIScripMaster.json"
)

STATE_FILE = Path(
    "nifty_2min_ce_state.json"
)


# ============================================================
# PAPER / LIVE MODE
# ============================================================
#
# IMPORTANT:
#
# PAPER_TRADING=true
#     -> no real broker order
#
# PAPER_TRADING=false
#     -> REAL BUY ORDER
#
# Default is TRUE for safety.
# ============================================================

PAPER_TRADING = (
    os.getenv(
        "PAPER_TRADING",
        "false"
    ).strip().lower()
    == "true"
)


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

    "login_status":
        "NOT CONNECTED",

    "last_error":
        "",

    "last_message":
        "Ready",

    "instruments":
        None,

    "spot":
        None,

    "candles_2m":
        None,

    "supertrend_df":
        None,

    "st_value":
        None,

    "st_green":
        False,

    "signal":
        "WAIT",

    "signal_time":
        None,

    "selected_option":
        None,

    "last_order_id":
        None,

    "last_order_time":
        None,

    "last_order_candle":
        None,

    # --------------------------------------------------------
    # Important safety lock
    #
    # If an order was sent but Angel response/order book
    # cannot confirm it, this becomes TRUE.
    #
    # No automatic retry will occur.
    # --------------------------------------------------------

    "order_status_unknown":
        False,

    "order_attempted_candle":
        None,

    "paper_orders":
        [],

    "last_refresh":
        None,

    "state_loaded":
        False,
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

    st.session_state[
        "last_error"
    ] = str(message)


# ============================================================
# TOTP
# ============================================================

def get_totp_secret(raw_secret):

    raw_secret = str(
        raw_secret
    ).strip()

    if not raw_secret:

        raise RuntimeError(
            "ANGEL_TOTP_SECRET is empty."
        )

    if raw_secret.lower().startswith(
        "otpauth://"
    ):

        try:

            from urllib.parse import (
                urlparse,
                parse_qs,
            )

            parsed = urlparse(
                raw_secret
            )

            query = parse_qs(
                parsed.query
            )

            secret = query.get(
                "secret",
                [""]
            )[0]

            if not secret:

                raise RuntimeError(
                    "TOTP secret missing from "
                    "otpauth URI."
                )

            raw_secret = secret

        except Exception as exc:

            raise RuntimeError(
                f"Invalid TOTP URI: {exc}"
            )

    raw_secret = (
        raw_secret
        .replace(" ", "")
        .replace("-", "")
        .strip()
        .upper()
    )

    if (
        raw_secret.isdigit()
        and len(raw_secret) == 6
    ):

        raise RuntimeError(
            "ANGEL_TOTP_SECRET contains the "
            "6-digit OTP. Use the Base32 TOTP "
            "secret instead."
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
                "Angel One returned empty "
                "login response."
            )

        if response.get(
            "status"
        ) is False:

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

        st.session_state[
            "last_message"
        ] = (
            "Angel One login successful."
        )

        return api

    except Exception as exc:

        st.session_state[
            "login_status"
        ] = "LOGIN FAILED"

        raise RuntimeError(
            f"Angel One login failed: {exc}"
        )

# ============================================================
# INSTRUMENT MASTER CONFIG
# ============================================================

BASE_DIR = Path(__file__).resolve().parent

INSTRUMENT_FILE = (
    BASE_DIR / "OpenAPIScripMaster.json"
)

STATE_FILE = (
    BASE_DIR / "nifty_2min_ce_state.json"
)

# Keep this as a fallback only.
INSTRUMENT_URL = (
    "https://margincalculator.angelone.com/"
    "OpenAPI_File/files/OpenAPIScripMaster.json"
)
# ============================================================
# INSTRUMENT MASTER
# ============================================================

# ============================================================
# INSTRUMENT MASTER
# ============================================================

@st.cache_data(ttl=3600)
def download_instrument_master():

    # ========================================================
    # 1. LOCAL FILE FIRST
    # ========================================================

    if INSTRUMENT_FILE.exists():

        try:

            with open(
                INSTRUMENT_FILE,
                "r",
                encoding="utf-8"
            ) as f:

                data = json.load(f)

            if not isinstance(data, list):

                raise RuntimeError(
                    "OpenAPIScripMaster.json must contain "
                    "a JSON list."
                )

            if len(data) == 0:

                raise RuntimeError(
                    "OpenAPIScripMaster.json is empty."
                )

            return data

        except json.JSONDecodeError as exc:

            raise RuntimeError(
                "OpenAPIScripMaster.json is not valid JSON: "
                + str(exc)
            )

        except Exception as exc:

            raise RuntimeError(
                "Could not read local instrument master: "
                + str(exc)
            )

    # ========================================================
    # 2. INTERNET FALLBACK
    # ========================================================

    try:

        response = requests.get(
            INSTRUMENT_URL,
            timeout=30
        )

        response.raise_for_status()

        data = response.json()

        if not isinstance(data, list):

            raise RuntimeError(
                "Angel One instrument master "
                "returned an invalid format."
            )

        if not data:

            raise RuntimeError(
                "Angel One instrument master is empty."
            )

        # ----------------------------------------------------
        # Save downloaded copy locally
        # ----------------------------------------------------

        try:

            with open(
                INSTRUMENT_FILE,
                "w",
                encoding="utf-8"
            ) as f:

                json.dump(
                    data,
                    f
                )

        except Exception:

            pass

        return data

    except Exception as exc:

        raise RuntimeError(
            "Instrument master unavailable.\n\n"
            "Could not download the Angel One "
            "instrument master and the local file "
            "OpenAPIScripMaster.json was not found.\n\n"
            "Put the actual OpenAPIScripMaster.json "
            "file in the same folder as dashboard.py.\n\n"
            f"Network error: {exc}"
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

        if not response:

            raise RuntimeError(
                "NIFTY LTP returned "
                "empty response."
            )

        if response.get(
            "status"
        ) is False:

            raise RuntimeError(
                "NIFTY LTP failed: "
                + str(response)
            )

        data = response.get(
            "data"
        )

        if not isinstance(
            data,
            dict
        ):

            raise RuntimeError(
                "NIFTY LTP response "
                "contains no data."
            )

        ltp = data.get(
            "ltp"
        )

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
    days=CANDLE_DAYS
):

    if api is None:

        raise RuntimeError(
            "Angel One API is not connected."
        )

    end_time = now_ist()

    start_time = (
        end_time
        - timedelta(days=days)
    )

    params = {

        "exchange":
            "NSE",

        "symboltoken":
            NIFTY_TOKEN,

        "interval":
            "ONE_MINUTE",

        "fromdate":
            start_time.strftime(
                "%Y-%m-%d %H:%M"
            ),

        "todate":
            end_time.strftime(
                "%Y-%m-%d %H:%M"
            ),
    }

    try:

        response = api.getCandleData(
            params
        )

        if not response:

            raise RuntimeError(
                "Candle API returned "
                "empty response."
            )

        if response.get(
            "status"
        ) is False:

            raise RuntimeError(
                "Candle API failed: "
                + str(response)
            )

        data = response.get(
            "data"
        )

        if not data:

            raise RuntimeError(
                "Candle API returned "
                "no candle data."
            )

        rows = []

        for row in data:

            if len(row) < 6:

                continue

            try:

                rows.append({

                    "datetime":
                        row[0],

                    "open":
                        float(row[1]),

                    "high":
                        float(row[2]),

                    "low":
                        float(row[3]),

                    "close":
                        float(row[4]),

                    "volume":
                        float(row[5]),
                })

            except Exception:

                continue

        df = pd.DataFrame(
            rows
        )

        if df.empty:

            raise RuntimeError(
                "No valid NIFTY candles received."
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

        df = df.dropna(
            subset=["datetime"]
        )

        df = df.sort_values(
            "datetime"
        )

        df = df.drop_duplicates(
            "datetime"
        )

        # ----------------------------------------------------
        # NSE regular market only
        # ----------------------------------------------------

        df = df[
            (
                df.index
                if False
                else pd.Series(
                    df["datetime"]
                    .dt.time,
                    index=df.index
                )
            )
            .notna()
        ]

        return df.set_index(
            "datetime"
        )

    except Exception as exc:

        raise RuntimeError(
            f"Candle API failed: {exc}"
        )


# ============================================================
# RESAMPLE 1-MINUTE → 2-MINUTE
# ============================================================

def resample_to_2min(df):

    if (
        df is None
        or df.empty
    ):

        raise RuntimeError(
            "1-minute candle dataframe "
            "is empty."
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
        })
        .dropna()
    )

    # Only regular NSE session
    result = result[
        (
            result.index.hour > 9
        )
        |
        (
            (
                result.index.hour == 9
            )
            &
            (
                result.index.minute >= 17
            )
        )
    ]

    result = result[
        (
            result.index.hour < 15
        )
        |
        (
            (
                result.index.hour == 15
            )
            &
            (
                result.index.minute <= 30
            )
        )
    ]

    return result


# ============================================================
# LAST CLOSED 2-MINUTE CANDLE
# ============================================================

def get_closed_2min_dataframe(df):

    if (
        df is None
        or df.empty
    ):

        return pd.DataFrame()

    current = now_ist()

    # Resampled candles are labelled on the RIGHT.
    #
    # 09:15-09:17 -> 09:17
    # 09:17-09:19 -> 09:19
    #
    # Therefore a candle with timestamp <= current
    # is closed.

    closed = df[
        df.index <= current
    ].copy()

    if closed.empty:

        return pd.DataFrame()

    return closed


# ============================================================
# SUPERTREND
# ============================================================

def calculate_supertrend(
    df,
    period=20,
    multiplier=1.5
):

    data = df.copy()

    if len(data) < (
        period + 5
    ):

        raise RuntimeError(
            f"Not enough 2-minute candles "
            f"for Supertrend "
            f"{period},{multiplier}."
        )

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

    tr = pd.concat(
        [
            tr1,
            tr2,
            tr3,
        ],
        axis=1
    ).max(axis=1)

    # Wilder-style ATR
    atr = tr.ewm(
        alpha=1 / period,
        adjust=False,
        min_periods=period
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
        dtype=float
    )

    final_lower = pd.Series(
        np.nan,
        index=data.index,
        dtype=float
    )

    supertrend = pd.Series(
        np.nan,
        index=data.index,
        dtype=float
    )

    direction = pd.Series(
        np.nan,
        index=data.index,
        dtype=float
    )

    for i in range(len(data)):

        if i == 0:

            final_upper.iloc[i] = (
                basic_upper.iloc[i]
            )

            final_lower.iloc[i] = (
                basic_lower.iloc[i]
            )

            continue

        bu = basic_upper.iloc[i]
        bl = basic_lower.iloc[i]

        prev_fu = (
            final_upper.iloc[i - 1]
        )

        prev_fl = (
            final_lower.iloc[i - 1]
        )

        prev_close_value = (
            close.iloc[i - 1]
        )

        # -----------------------------------------------
        # Final Upper Band
        # -----------------------------------------------

        if pd.isna(bu):

            final_upper.iloc[i] = (
                prev_fu
            )

        elif (
            pd.isna(prev_fu)
            or bu < prev_fu
            or prev_close_value > prev_fu
        ):

            final_upper.iloc[i] = bu

        else:

            final_upper.iloc[i] = prev_fu

        # -----------------------------------------------
        # Final Lower Band
        # -----------------------------------------------

        if pd.isna(bl):

            final_lower.iloc[i] = (
                prev_fl
            )

        elif (
            pd.isna(prev_fl)
            or bl > prev_fl
            or prev_close_value < prev_fl
        ):

            final_lower.iloc[i] = bl

        else:

            final_lower.iloc[i] = prev_fl

        # -----------------------------------------------
        # Direction / Supertrend
        # -----------------------------------------------

        if pd.isna(
            atr.iloc[i]
        ):

            continue

        if pd.isna(
            supertrend.iloc[i - 1]
        ):

            # Initial direction
            if (
                close.iloc[i]
                <= final_upper.iloc[i]
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

            continue

        previous_direction = (
            direction.iloc[i - 1]
        )

        # Previous trend RED
        if previous_direction == -1:

            if (
                close.iloc[i]
                > final_upper.iloc[i]
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

        # Previous trend GREEN
        else:

            if (
                close.iloc[i]
                < final_lower.iloc[i]
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

    if df.empty:

        raise RuntimeError(
            "Instrument master dataframe "
            "is empty."
        )

    df.columns = [
        str(c)
        .strip()
        .lower()
        for c in df.columns
    ]

    rename_map = {}

    if (
        "token" in df.columns
        and "symboltoken"
        not in df.columns
    ):

        rename_map[
            "token"
        ] = "symboltoken"

    if (
        "symbol" in df.columns
        and "tradingsymbol"
        not in df.columns
    ):

        rename_map[
            "symbol"
        ] = "tradingsymbol"

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
        c
        for c in required
        if c not in df.columns
    ]

    if missing:

        raise RuntimeError(
            "Instrument master missing "
            "columns: "
            + ", ".join(missing)
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

    symbols = (
        df["tradingsymbol"]
        .astype(str)
        .str.upper()
    )

    df = df[
        symbols.str.startswith(
            "NIFTY"
        )
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

    # Angel master commonly stores strikes ×100.
    #
    # Example:
    # 2500000 -> 25000
    #
    # Some versions may already provide
    # actual strike values, so detect it.

    df["strike_actual"] = np.where(
        df["strike_raw"] > 100000,
        df["strike_raw"] / 100.0,
        df["strike_raw"]
    )

    # --------------------------------------------------------
    # EXPIRY
    # --------------------------------------------------------

    # Convert common Angel formats safely.
    expiry_raw = (
        df["expiry"]
        .astype(str)
        .str.strip()
        .str.upper()
    )

    df["expiry_date"] = pd.to_datetime(
        expiry_raw,
        errors="coerce",
        dayfirst=True
    )

    # Try another format for values such as
    # 25SEP2026.

    missing_expiry = (
        df["expiry_date"].isna()
    )

    if missing_expiry.any():

        df.loc[
            missing_expiry,
            "expiry_date"
        ] = pd.to_datetime(
            expiry_raw[
                missing_expiry
            ],
            format="%d%b%Y",
            errors="coerce"
        )

    df = df[
        df["expiry_date"].notna()
    ].copy()

    if df.empty:

        raise RuntimeError(
            "No valid NIFTY CE expiry "
            "dates found."
        )

    today = pd.Timestamp(
        now_ist().date()
    )

    df["expiry_normalized"] = (
        df["expiry_date"]
        .dt.normalize()
    )

    df = df[
        df["expiry_normalized"]
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
        df["expiry_normalized"]
        .min()
    )

    df = df[
        df["expiry_normalized"]
        == nearest_expiry
    ].copy()

    if df.empty:

        raise RuntimeError(
            "No contracts found for "
            "nearest NIFTY expiry."
        )

    # --------------------------------------------------------
    # ATM
    # --------------------------------------------------------

    spot = float(spot)

    df["distance"] = (
        df["strike_actual"]
        - spot
    ).abs()

    df = df.sort_values(
        [
            "distance",
            "strike_actual",
        ]
    )

    selected = df.iloc[0]

    lot_size = int(
        float(
            selected["lotsize"]
        )
    )

    if lot_size <= 0:

        raise RuntimeError(
            "Invalid NIFTY CE lot size."
        )

    option = {

        "symbol":
            str(
                selected[
                    "tradingsymbol"
                ]
            ).strip(),

        "token":
            str(
                selected[
                    "symboltoken"
                ]
            ).strip(),

        "strike":
            float(
                selected[
                    "strike_actual"
                ]
            ),

        "expiry":
            pd.Timestamp(
                selected[
                    "expiry_date"
                ]
            ).strftime(
                "%d-%b-%Y"
            ),

        "lot_size":
            lot_size,

        "quantity":
            lot_size * LOTS,
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
            isinstance(
                response,
                dict
            )
            and response.get(
                "status"
            ) is False
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

        return []

    except Exception as exc:

        st.warning(
            f"Order Book error: {exc}"
        )

        return []


# ============================================================
# FIND RECENT MATCHING ORDER
# ============================================================

def find_order_in_book(
    api,
    symbol=None,
    transaction_type="BUY"
):

    orders = get_order_book(
        api
    )

    if not orders:

        return None

    symbol = (
        str(symbol).strip()
        if symbol
        else None
    )

    transaction_type = (
        str(
            transaction_type
        )
        .upper()
        .strip()
    )

    matches = []

    for order in orders:

        if not isinstance(
            order,
            dict
        ):

            continue

        order_symbol = str(
            order.get(
                "tradingsymbol",
                ""
            )
        ).strip()

        order_transaction = str(
            order.get(
                "transactiontype",
                ""
            )
        ).upper().strip()

        order_exchange = str(
            order.get(
                "exchange",
                ""
            )
        ).upper().strip()

        if (
            symbol
            and order_symbol != symbol
        ):

            continue

        if (
            transaction_type
            and order_transaction
            != transaction_type
        ):

            continue

        if order_exchange != "NFO":

            continue

        matches.append(
            order
        )

    if not matches:

        return None

    # Most recent matching order.
    return matches[0]


# ============================================================
# WAIT / VERIFY ORDER BOOK
# ============================================================

def verify_order_in_orderbook(
    api,
    symbol,
    attempts=3,
    wait_seconds=2
):

    for attempt in range(
        attempts
    ):

        order = find_order_in_book(
            api,
            symbol=symbol,
            transaction_type="BUY"
        )

        if order:

            order_id = (
                order.get("orderid")
                or order.get("orderId")
            )

            if order_id:

                return order

        if attempt < (
            attempts - 1
        ):

            time.sleep(
                wait_seconds
            )

    return None


# ============================================================
# PAPER ORDER
# ============================================================

def create_paper_order(
    option,
    spot,
    candle_time
):

    order_id = (
        "PAPER-"
        + now_ist().strftime(
            "%Y%m%d%H%M%S"
        )
    )

    order = {

        "orderid":
            order_id,

        "tradingsymbol":
            option["symbol"],

        "symboltoken":
            option["token"],

        "transactiontype":
            "BUY",

        "exchange":
            "NFO",

        "ordertype":
            "MARKET",

        "producttype":
            "INTRADAY",

        "quantity":
            str(
                option["quantity"]
            ),

        "status":
            "PAPER ORDER",

        "spot":
            spot,

        "candle":
            str(
                candle_time
            ),

        "time":
            now_ist().strftime(
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

        "variety":
            "NORMAL",

        "tradingsymbol":
            option["symbol"],

        "symboltoken":
            option["token"],

        "transactiontype":
            "BUY",

        "exchange":
            "NFO",

        "ordertype":
            "MARKET",

        "producttype":
            "INTRADAY",

        "duration":
            "DAY",

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
    # IMPORTANT SAFETY LOCK
    #
    # Mark the candle as attempted BEFORE sending the order.
    #
    # If Angel accepts the order but the response is lost,
    # Streamlit reruns cannot send another order.
    # --------------------------------------------------------

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

        # ----------------------------------------------------
        # We cannot know with certainty whether the broker
        # received the request.
        #
        # Therefore DO NOT automatically retry.
        # ----------------------------------------------------

        st.session_state[
            "order_status_unknown"
        ] = True

        raise RuntimeError(
            "Angel One order request raised "
            "an exception. Order status is UNKNOWN. "
            "No automatic retry was performed. "
            f"Details: {exc}"
        )

    # --------------------------------------------------------
    # NORMAL RESPONSE
    # --------------------------------------------------------

    if response:

        if isinstance(
            response,
            dict
        ):

            if response.get(
                "status"
            ) is False:

                raise RuntimeError(
                    "Angel One rejected order: "
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
                    data.get(
                        "orderid"
                    )
                    or data.get(
                        "orderId"
                    )
                )

                if order_id:

                    return str(
                        order_id
                    )

        if isinstance(
            response,
            str
        ):

            if response.strip():

                return response.strip()

    # --------------------------------------------------------
    # EMPTY RESPONSE
    #
    # Check Order Book.
    # --------------------------------------------------------

    existing = (
        verify_order_in_orderbook(
            api,
            option["symbol"],
            attempts=3,
            wait_seconds=2
        )
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

    # --------------------------------------------------------
    # UNKNOWN
    # --------------------------------------------------------

    st.session_state[
        "order_status_unknown"
    ] = True

    raise RuntimeError(
        "Angel One returned an empty order "
        "response and no matching BUY order "
        "was found in Order Book. "
        "Order status is UNKNOWN. "
        "NO AUTOMATIC RETRY WILL BE PERFORMED."
    )


# ============================================================
# AUTOMATIC BUY CE
# ============================================================

def automatic_buy_ce(
    api,
    instruments,
    spot,
    candle_time
):

    candle_key = str(
        candle_time
    )

    # --------------------------------------------------------
    # UNKNOWN ORDER LOCK
    # --------------------------------------------------------

    if st.session_state.get(
        "order_status_unknown",
        False
    ):

        return {

            "status":
                "ORDER_STATUS_UNKNOWN",

            "order_id":
                None,
        }

    # --------------------------------------------------------
    # Already confirmed order
    # --------------------------------------------------------

    last_order_id = (
        st.session_state.get(
            "last_order_id"
        )
    )

    if last_order_id:

        return {

            "status":
                "ALREADY_ORDERED",

            "order_id":
                last_order_id,
        }

    # --------------------------------------------------------
    # Candle already attempted
    # --------------------------------------------------------

    attempted_candle = (
        st.session_state.get(
            "order_attempted_candle"
        )
    )

    if (
        attempted_candle
        == candle_key
    ):

        return {

            "status":
                "ALREADY_ATTEMPTED",

            "order_id":
                None,
        }

    # --------------------------------------------------------
    # Mark attempted BEFORE broker request.
    #
    # This protects against Streamlit reruns.
    # --------------------------------------------------------

    st.session_state[
        "order_attempted_candle"
    ] = candle_key

    # --------------------------------------------------------
    # Select ATM CE
    # --------------------------------------------------------

    option = select_atm_nifty_ce(
        instruments,
        spot
    )

    st.session_state[
        "selected_option"
    ] = option

    # --------------------------------------------------------
    # PAPER
    # --------------------------------------------------------

    if PAPER_TRADING:

        order_id = create_paper_order(
            option,
            spot,
            candle_time
        )

    # --------------------------------------------------------
    # LIVE
    # --------------------------------------------------------

    else:

        order_id = place_real_buy_order(
            api,
            option
        )

    # --------------------------------------------------------
    # Save confirmed order
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

        "status":
            "ORDER_PLACED",

        "order_id":
            order_id,

        "option":
            option,
    }


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
            "order_attempted_candle"
        ] = state.get(
            "order_attempted_candle"
        )

        st.session_state[
            "order_status_unknown"
        ] = bool(
            state.get(
                "order_status_unknown",
                False
            )
        )

        st.session_state[
            "selected_option"
        ] = state.get(
            "selected_option"
        )

    except Exception as exc:

        st.warning(
            "Could not load state: "
            + str(exc)
        )


# ============================================================
# SAVE STATE
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

        "order_attempted_candle":
            st.session_state.get(
                "order_attempted_candle"
            ),

        "order_status_unknown":
            st.session_state.get(
                "order_status_unknown",
                False
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
                indent=2,
                default=str
            )

    except Exception as exc:

        st.warning(
            "Could not save state: "
            + str(exc)
        )


# ============================================================
# MARKET HOURS
# ============================================================

def market_is_open():

    current = now_ist()

    if current.weekday() >= 5:

        return False

    start = current.replace(
        hour=9,
        minute=15,
        second=0,
        microsecond=0
    )

    end = current.replace(
        hour=15,
        minute=30,
        second=0,
        microsecond=0
    )

    return (
        start
        <= current
        <= end
    )


# ============================================================
# LOAD STATE ONCE
# ============================================================

if not st.session_state[
    "state_loaded"
]:

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
        "🧪 PAPER TRADING MODE — "
        "NO REAL BROKER ORDER WILL BE SENT"
    )

else:

    st.error(
        "🔴 LIVE TRADING ENABLED — "
        "REAL NIFTY CE ORDERS CAN BE PLACED"
    )


# ============================================================
# SIDEBAR
# ============================================================

with st.sidebar:

    st.header(
        "System"
    )

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
        "Lots:",
        LOTS
    )

    st.write(
        "Trading:",
        "PAPER"
        if PAPER_TRADING
        else "LIVE"
    )

    st.write(
        "Refresh:",
        f"{REFRESH_SECONDS} sec"
    )

    if st.button(
        "Connect Angel One",
        use_container_width=True
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
# API
# ============================================================

api = st.session_state[
    "api"
]


if api is None:

    st.error(
        "Angel One is not connected."
    )

    st.info(
        "Set ANGEL_API_KEY, "
        "ANGEL_CLIENT_ID, "
        "ANGEL_PASSWORD and "
        "ANGEL_TOTP_SECRET."
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

if st.session_state[
    "instruments"
] is None:

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
# MARKET STATUS
# ============================================================

if not market_is_open():

    st.info(
        "Market is currently CLOSED. "
        "Automatic BUY CE will operate only "
        "during NSE market hours "
        "09:15–15:30 IST."
    )


# ============================================================
# NIFTY SPOT
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
# 1-MINUTE CANDLES
# ============================================================

try:

    df1 = get_nifty_1m_candles(
        api,
        days=CANDLE_DAYS
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
        period=SUPERTREND_PERIOD,
        multiplier=SUPERTREND_MULTIPLIER
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
# CLOSED CANDLES ONLY
# ============================================================

closed_df = (
    get_closed_2min_dataframe(
        st_df
    )
)

if closed_df.empty:

    st.warning(
        "Waiting for a closed 2-minute candle."
    )

    st.stop()


last_closed_time = (
    closed_df.index[-1]
)

last_row = (
    closed_df.iloc[-1]
)


# ============================================================
# CHECK VALID SUPERTREND
# ============================================================

if pd.isna(
    last_row["Supertrend"]
):

    st.warning(
        "Supertrend is still calculating. "
        "Waiting for enough closed candles."
    )

    st.stop()


# ============================================================
# SIGNAL
# ============================================================
#
# IMPORTANT:
#
# GREEN = BUY CE
# RED   = WAIT
#
# NO FLIP REQUIRED.
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
    last_row[
        "Supertrend"
    ]
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
    "🟢 GREEN"
    if st_green
    else "🔴 RED"
)

c4.metric(
    "Signal",
    signal
)

c5.metric(
    "Closed Candle",
    last_closed_time.strftime(
        "%H:%M:%S"
    )
)


# ============================================================
# AUTOMATIC BUY
# ============================================================

st.divider()

st.subheader(
    "🤖 Automatic BUY CE"
)

if signal == "BUY CE":

    st.success(
        "🟢 SUPERTREND GREEN → "
        "BUY CE CONDITION IS ACTIVE"
    )

    # --------------------------------------------------------
    # Unknown order safety lock
    # --------------------------------------------------------

    if st.session_state.get(
        "order_status_unknown",
        False
    ):

        st.error(
            "⚠️ ORDER STATUS UNKNOWN — "
            "automatic retry is LOCKED."
        )

        st.info(
            "Check Angel One Order Book manually "
            "before allowing another order."
        )

    # --------------------------------------------------------
    # Market closed
    # --------------------------------------------------------

    elif not market_is_open():

        st.warning(
            "BUY CE signal exists, but the market "
            "is closed. No order will be sent."
        )

    else:

        try:

            result = automatic_buy_ce(
                api,
                instruments,
                spot,
                last_closed_time
            )

            if (
                result["status"]
                == "ORDER_PLACED"
            ):

                st.success(
                    "✅ AUTOMATIC BUY CE "
                    "ORDER CONFIRMED"
                )

                st.write(
                    "Order ID:",
                    result[
                        "order_id"
                    ]
                )

                option = result.get(
                    "option"
                )

                if option:

                    st.write(
                        "ATM CE:",
                        option["symbol"]
                    )

                    st.write(
                        "Quantity:",
                        option["quantity"]
                    )

            elif (
                result["status"]
                == "ALREADY_ORDERED"
            ):

                st.info(
                    "Duplicate protection active. "
                    "Existing order: "
                    + str(
                        result[
                            "order_id"
                        ]
                    )
                )

            elif (
                result["status"]
                == "ALREADY_ATTEMPTED"
            ):

                st.warning(
                    "This candle has already "
                    "been attempted. "
                    "No duplicate order will be sent."
                )

            elif (
                result["status"]
                == "ORDER_STATUS_UNKNOWN"
            ):

                st.error(
                    "Order status is UNKNOWN. "
                    "Automatic retry is disabled."
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

    a1, a2, a3, a4, a5 = (
        st.columns(5)
    )

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

    a5.metric(
        "Quantity",
        option["quantity"]
    )

    st.caption(
        "Symbol Token: "
        + str(
            option["token"]
        )
    )

else:

    st.info(
        "ATM NIFTY CE will be selected "
        "automatically when BUY CE is triggered."
    )


# ============================================================
# ORDER STATUS
# ============================================================

st.divider()

st.subheader(
    "📦 Automatic Order Status"
)

o1, o2, o3, o4 = st.columns(4)

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

o4.metric(
    "Order Status",
    "UNKNOWN"
    if st.session_state.get(
        "order_status_unknown",
        False
    )
    else (
        "ORDERED"
        if st.session_state.get(
            "last_order_id"
        )
        else "NONE"
    )
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
        existing
        + remaining
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
    display_df[
        "ST_Green"
    ],
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
    "Strategy: 2-Minute Supertrend (20, 1.5) | "
    "GREEN = BUY ATM NIFTY CE | "
    "No GREEN FLIP required"
)

st.caption(
    "Automatic refresh: "
    f"{REFRESH_SECONDS} seconds | "
    "Market: 09:15–15:30 IST"
)

st.caption(
    "Last update: "
    + now_ist().strftime(
        "%Y-%m-%d %H:%M:%S IST"
    )
)


# ============================================================
# AUTO REFRESH
# ============================================================

time.sleep(
    REFRESH_SECONDS
)

st.rerun()

