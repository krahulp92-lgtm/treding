# ============================================================
# dashboard.py
# NIFTY LIVE SUPERTREND 20,2 - ANGEL ONE SMARTAPI
#
# FEATURES
# ------------------------------------------------------------
# 1. NIFTY spot LTP
# 2. 5-minute candles
# 3. 15-minute confirmation
# 4. 4-hour confirmation
# 5. Supertrend 20,2
# 6. BUY CE / BUY PE buttons ALWAYS visible
# 7. ATM CE / PE selection
# 8. CE / PE LTP
# 9. Local instrument-master caching
# 10. Duplicate-order protection
# 11. Angel One order placement
# 12. Automatic Order Book refresh after BUY
# 13. Automatically opens Order Book after BUY
# 14. Highlights newly placed order
# 15. Order status
# 16. Positions
# 17. LIVE_TRADING safety switch
#
# ENVIRONMENT VARIABLES
# ------------------------------------------------------------
# ANGEL_API_KEY
# ANGEL_CLIENT_ID
# ANGEL_PASSWORD
# ANGEL_TOTP_SECRET
#
# INSTALL
# ------------------------------------------------------------
# pip install streamlit pandas numpy requests pyotp logzero websocket-client
# pip install smartapi-python
#
# RUN
# ------------------------------------------------------------
# streamlit run dashboard.py
# ============================================================


# ============================================================
# IMPORTS
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
    page_title="NIFTY Live Supertrend Dashboard",
    page_icon="📈",
    layout="wide",
)


# ============================================================
# CONFIGURATION
# ============================================================

IST = ZoneInfo("Asia/Kolkata")

# ------------------------------------------------------------
# SAFETY SWITCH
# ------------------------------------------------------------
#
# FALSE = NO REAL ORDER
# TRUE  = REAL ANGEL ONE ORDER
#
LIVE_TRADING = False

# Automatic trading is OFF.
AUTO_TRADE = False

# Duplicate protection.
DUPLICATE_PROTECTION = True

# ------------------------------------------------------------
# STRATEGY
# ------------------------------------------------------------

ST_PERIOD = 20
ST_MULTIPLIER = 2.0

PRIMARY_TIMEFRAME = "5m"

# ------------------------------------------------------------
# NIFTY TOKEN
# ------------------------------------------------------------

NIFTY_EXCHANGE = "NSE"
NIFTY_SYMBOL = "NIFTY"
NIFTY_TOKEN = "99926000"

# ------------------------------------------------------------
# INSTRUMENT MASTER
# ------------------------------------------------------------

INSTRUMENT_URL = (
    "https://margincalculator.angelone.in/"
    "OpenAPI_File/files/OpenAPIScripMaster.json"
)

DATA_DIR = Path("data")
DATA_DIR.mkdir(parents=True, exist_ok=True)

INSTRUMENT_FILE = DATA_DIR / "OpenAPIScripMaster.json"
STATE_FILE = DATA_DIR / "trading_state.json"

INSTRUMENT_CACHE_HOURS = 24

# ------------------------------------------------------------
# ORDER SETTINGS
# ------------------------------------------------------------

ORDER_VARIETY = "NORMAL"
ORDER_TYPE = "MARKET"

# Angel One documents CARRYFORWARD as the normal F&O product.
NFO_PRODUCT_TYPE = "CARRYFORWARD"

ORDER_DURATION = "DAY"

ORDER_TAG = "NIFTYST202"

# ============================================================
# ENVIRONMENT VARIABLES
# ============================================================

ANGEL_API_KEY = os.getenv("ANGEL_API_KEY", "").strip()
ANGEL_CLIENT_ID = os.getenv("ANGEL_CLIENT_ID", "").strip()
ANGEL_PASSWORD = os.getenv("ANGEL_PASSWORD", "").strip()
ANGEL_TOTP_SECRET = os.getenv("ANGEL_TOTP_SECRET", "").strip()


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

    "running": False,

    "auto_trade_last_signal": None,
}


for key, value in DEFAULTS.items():
    if key not in st.session_state:
        st.session_state[key] = value


# ============================================================
# HELPER FUNCTIONS
# ============================================================

def now_ist():
    return datetime.now(IST)


def fmt_number(value, decimals=2):
    try:
        if value is None:
            return "-"
        return f"{float(value):.{decimals}f}"
    except Exception:
        return "-"


def credentials_ok():
    return all([
        ANGEL_API_KEY,
        ANGEL_CLIENT_ID,
        ANGEL_PASSWORD,
        ANGEL_TOTP_SECRET,
    ])


# ============================================================
# TOTP
# ============================================================

def get_totp_secret(raw_secret):
    """
    Accept either:
      ABCDE...
    or:
      otpauth://totp/...
    """

    raw_secret = raw_secret.strip()

    if raw_secret.startswith("otpauth://"):
        try:
            from urllib.parse import urlparse, parse_qs

            parsed = urlparse(raw_secret)

            params = parse_qs(parsed.query)

            secret = params.get("secret", [None])[0]

            if not secret:
                raise ValueError(
                    "TOTP secret missing from otpauth URI."
                )

            return secret.replace(" ", "").upper()

        except Exception as e:
            raise RuntimeError(
                f"Invalid otpauth TOTP URI: {e}"
            )

    return raw_secret.replace(" ", "").upper()


def generate_totp():
    secret = get_totp_secret(ANGEL_TOTP_SECRET)

    try:
        return pyotp.TOTP(secret).now()
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
            "Angel One credentials are missing. "
            "Set ANGEL_API_KEY, ANGEL_CLIENT_ID, "
            "ANGEL_PASSWORD and ANGEL_TOTP_SECRET."
        )

    totp = generate_totp()

    smart_api = SmartConnect(
        api_key=ANGEL_API_KEY
    )

    response = smart_api.generateSession(
        ANGEL_CLIENT_ID,
        ANGEL_PASSWORD,
        totp,
    )

    if not response:
        raise RuntimeError(
            "Angel One returned an empty login response."
        )

    if not response.get("status"):
        raise RuntimeError(
            "Angel One login failed: "
            + str(response)
        )

    st.session_state.api = smart_api
    st.session_state.login_status = "CONNECTED"
    st.session_state.last_error = ""

    return smart_api


# ============================================================
# INSTRUMENT MASTER
# ============================================================

def download_instrument_master():
    """
    Download instrument master and save locally.
    """

    headers = {
        "User-Agent": (
            "Mozilla/5.0 "
            "(Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 "
            "(KHTML, like Gecko) "
            "Chrome/131 Safari/537.36"
        )
    }

    response = requests.get(
        INSTRUMENT_URL,
        headers=headers,
        timeout=60,
    )

    response.raise_for_status()

    data = response.json()

    if not isinstance(data, list):
        raise RuntimeError(
            "Instrument master response is not a JSON list."
        )

    if len(data) < 1000:
        raise RuntimeError(
            "Instrument master appears incomplete."
        )

    temp_file = INSTRUMENT_FILE.with_suffix(".tmp")

    with open(
        temp_file,
        "w",
        encoding="utf-8",
    ) as f:
        json.dump(
            data,
            f,
            separators=(",", ":"),
        )

    temp_file.replace(INSTRUMENT_FILE)

    return data


def load_local_instrument_master():
    if not INSTRUMENT_FILE.exists():
        return None

    try:
        with open(
            INSTRUMENT_FILE,
            "r",
            encoding="utf-8",
        ) as f:
            data = json.load(f)

        if not isinstance(data, list):
            return None

        if len(data) < 1000:
            return None

        return data

    except Exception:
        return None


def instrument_cache_is_fresh():
    if not INSTRUMENT_FILE.exists():
        return False

    modified = datetime.fromtimestamp(
        INSTRUMENT_FILE.stat().st_mtime,
        tz=IST,
    )

    age = now_ist() - modified

    return age < timedelta(
        hours=INSTRUMENT_CACHE_HOURS
    )


def load_instruments(force_refresh=False):

    if (
        not force_refresh
        and instrument_cache_is_fresh()
    ):
        data = load_local_instrument_master()

        if data:
            return data

    try:
        data = download_instrument_master()

        return data

    except Exception as e:

        local = load_local_instrument_master()

        if local:
            st.warning(
                "Instrument master download failed. "
                "Using local cached copy."
            )

            return local

        raise RuntimeError(
            "Instrument master unavailable and "
            "no local cache exists.\n\n"
            f"{e}"
        )


# ============================================================
# FIND ATM OPTION
# ============================================================

def select_atm_option(
    instruments,
    spot,
    option_type,
):
    if not instruments:
        raise RuntimeError(
            "Instrument master is empty."
        )

    if spot is None:
        raise RuntimeError(
            "NIFTY spot unavailable."
        )

    rows = []

    today = now_ist().date()

    for item in instruments:

        try:
            exch_seg = str(
                item.get("exch_seg", "")
            ).upper()

            instrument_type = str(
                item.get("instrumenttype", "")
            ).upper()

            symbol = str(
                item.get("symbol", "")
            ).strip()

            name = str(
                item.get("name", "")
            ).upper()

            if exch_seg != "NFO":
                continue

            if instrument_type not in (
                "OPTIDX",
                "OPTSTK",
            ):
                continue

            if option_type not in symbol:
                continue

            if not symbol.endswith(
                option_type
            ):
                continue

            if name != "NIFTY":
                continue

            expiry_raw = item.get(
                "expiry"
            )

            if not expiry_raw:
                continue

            expiry = pd.to_datetime(
                expiry_raw,
                errors="coerce",
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

            # Angel One strike is normally scaled by 100.
            if strike > 100000:
                strike = strike / 100.0

            token = str(
                item.get("token")
            )

            lot_size = int(
                float(
                    item.get(
                        "lotsize",
                        0,
                    )
                )
            )

            if not token or lot_size <= 0:
                continue

            distance = abs(
                strike - float(spot)
            )

            rows.append({
                "symbol": symbol,
                "token": token,
                "strike": strike,
                "expiry": expiry_date,
                "lot_size": lot_size,
                "option_type": option_type,
                "distance": distance,
                "exchange": "NFO",
            })

        except Exception:
            continue

    if not rows:
        raise RuntimeError(
            f"No NIFTY {option_type} options found."
        )

    df = pd.DataFrame(rows)

    nearest_expiry = df[
        "expiry"
    ].min()

    df = df[
        df["expiry"]
        == nearest_expiry
    ]

    df = df.sort_values(
        "distance"
    )

    option = df.iloc[0].to_dict()

    return option


# ============================================================
# LTP
# ============================================================

def get_ltp(
    exchange,
    symbol,
    token,
):
    api = st.session_state.api

    if api is None:
        raise RuntimeError(
            "Not connected to Angel One."
        )

    response = api.ltpData(
        exchange,
        symbol,
        str(token),
    )

    if not response:
        raise RuntimeError(
            "Empty LTP response."
        )

    if not response.get("status"):
        raise RuntimeError(
            "LTP API failed: "
            + str(response)
        )

    data = response.get("data")

    if not data:
        raise RuntimeError(
            "LTP response has no data."
        )

    ltp = data.get("ltp")

    if ltp is None:
        raise RuntimeError(
            "LTP missing from response."
        )

    return float(ltp)


def get_nifty_ltp():
    return get_ltp(
        NIFTY_EXCHANGE,
        NIFTY_SYMBOL,
        NIFTY_TOKEN,
    )


def get_option_ltp(option):
    return get_ltp(
        option["exchange"],
        option["symbol"],
        option["token"],
    )


# ============================================================
# CANDLE DATA
# ============================================================

def get_nifty_candles(
    days=30,
):
    api = st.session_state.api

    if api is None:
        raise RuntimeError(
            "Not connected to Angel One."
        )

    end = now_ist()
    start = end - timedelta(
        days=days
    )

    params = {
        "exchange": "NSE",
        "symboltoken": NIFTY_TOKEN,
        "interval": "FIVE_MINUTE",
        "fromdate": start.strftime(
            "%Y-%m-%d %H:%M"
        ),
        "todate": end.strftime(
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
            "Candle API failed: "
            + str(response)
        )

    rows = response.get("data")

    if not rows:
        raise RuntimeError(
            "Candle API returned no candles."
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

    # Convert timezone if needed.
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
        subset=["timestamp"]
    )

    return df.reset_index(
        drop=True
    )


# ============================================================
# REMOVE INCOMPLETE CANDLE
# ============================================================

def remove_incomplete_candle(df):
    if df.empty:
        return df

    current = now_ist()

    last_time = df.iloc[-1]["timestamp"]

    if last_time.tzinfo is None:
        last_time = last_time.replace(
            tzinfo=IST
        )

    candle_end = (
        last_time
        + timedelta(minutes=5)
    )

    if candle_end > current:
        df = df.iloc[:-1]

    return df


# ============================================================
# SUPERTREND
# ============================================================

def calculate_supertrend(
    df,
    period=20,
    multiplier=2.0,
):
    df = df.copy()

    high = df["high"]
    low = df["low"]
    close = df["close"]

    prev_close = close.shift(1)

    tr1 = high - low
    tr2 = (
        high - prev_close
    ).abs()
    tr3 = (
        low - prev_close
    ).abs()

    true_range = pd.concat(
        [tr1, tr2, tr3],
        axis=1,
    ).max(axis=1)

    atr = (
        true_range
        .ewm(
            alpha=1 / period,
            adjust=False,
        )
        .mean()
    )

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
        index=df.index,
        dtype=float,
    )

    final_lower = pd.Series(
        index=df.index,
        dtype=float,
    )

    supertrend = pd.Series(
        index=df.index,
        dtype=float,
    )

    direction = pd.Series(
        index=df.index,
        dtype=int,
    )

    for i in range(len(df)):

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
            or prev_close_value
            > prev_fu
        ):
            final_upper.iloc[i] = (
                basic_upper.iloc[i]
            )
        else:
            final_upper.iloc[i] = prev_fu

        if (
            basic_lower.iloc[i]
            > prev_fl
            or prev_close_value
            < prev_fl
        ):
            final_lower.iloc[i] = (
                basic_lower.iloc[i]
            )
        else:
            final_lower.iloc[i] = prev_fl

        prev_st = (
            supertrend.iloc[i - 1]
        )

        if prev_st == prev_fu:

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
    df["ST_Green"] = direction == 1
    df["ST_Red"] = direction == -1

    previous_direction = (
        df["ST_Direction"]
        .shift(1)
    )

    df["ST_Flip_Green"] = (
        (df["ST_Direction"] == 1)
        & (previous_direction == -1)
    )

    df["ST_Flip_Red"] = (
        (df["ST_Direction"] == -1)
        & (previous_direction == 1)
    )

    return df


# ============================================================
# RESAMPLE
# ============================================================

def resample_ohlcv(
    df,
    rule,
):
    temp = df.copy()

    temp = temp.set_index(
        "timestamp"
    )

    result = (
        temp.resample(
            rule,
            origin="start_day",
            offset="9h15min",
            label="right",
            closed="left",
        )
        .agg({
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum",
        })
        .dropna()
        .reset_index()
    )

    return result


# ============================================================
# BUILD TIMEFRAMES
# ============================================================

def build_timeframes(df5):

    st5 = calculate_supertrend(
        df5,
        ST_PERIOD,
        ST_MULTIPLIER,
    )

    df15 = resample_ohlcv(
        df5,
        "15min",
    )

    df4h = resample_ohlcv(
        df5,
        "4h",
    )

    st15 = calculate_supertrend(
        df15,
        ST_PERIOD,
        ST_MULTIPLIER,
    )

    st4h = calculate_supertrend(
        df4h,
        ST_PERIOD,
        ST_MULTIPLIER,
    )

    return (
        st5,
        st15,
        st4h,
    )


# ============================================================
# CURRENT SIGNAL
# ============================================================

def current_signal(
    st5,
    st15,
    st4h,
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
        bool(latest5["ST_Flip_Green"])
        and bool(latest15["ST_Green"])
        and bool(latest4h["ST_Green"])
    ):
        return (
            "BUY_CE",
            latest5["timestamp"],
        )

    if (
        bool(latest5["ST_Flip_Red"])
        and bool(latest15["ST_Red"])
        and bool(latest4h["ST_Red"])
    ):
        return (
            "BUY_PE",
            latest5["timestamp"],
        )

    return "WAIT", None


# ============================================================
# STATE FILE
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
            encoding="utf-8",
        ) as f:
            state = json.load(f)

        if not isinstance(state, dict):
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


def save_state(state):

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

    temp.replace(STATE_FILE)


# ============================================================
# DUPLICATE PROTECTION
# ============================================================

def local_duplicate_exists(
    signal_type,
    symbol,
):
    state = load_state()

    today = now_ist().date().isoformat()

    for order in state.get(
        "orders",
        [],
    ):

        if (
            order.get("date")
            == today
            and order.get("signal")
            == signal_type
            and order.get("symbol")
            == symbol
            and order.get("live_order")
            is True
        ):
            return True

    return False


def orderbook_duplicate_exists(
    signal_type,
    symbol,
):
    """
    Check today's Angel One order book
    for an existing BUY order for the
    same CE/PE symbol.
    """

    api = st.session_state.api

    if api is None:
        return False

    try:
        response = api.orderBook()

        if not response:
            return False

        if not response.get("status"):
            return False

        orders = response.get(
            "data"
        ) or []

        today = now_ist().date()

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

            if (
                order_symbol
                != symbol
            ):
                continue

            if transaction != "BUY":
                continue

            update_text = str(
                order.get(
                    "updatetime",
                    ""
                )
            )

            is_today = True

            if update_text:
                try:
                    parsed = pd.to_datetime(
                        update_text,
                        errors="coerce",
                    )

                    if not pd.isna(parsed):
                        is_today = (
                            parsed.date()
                            == today
                        )
                except Exception:
                    pass

            if is_today:
                return True

    except Exception:
        pass

    return False


# ============================================================
# ORDER BOOK
# ============================================================

def refresh_order_book():
    api = st.session_state.api

    if api is None:
        raise RuntimeError(
            "Not connected to Angel One."
        )

    response = api.orderBook()

    if not response:
        raise RuntimeError(
            "Order Book returned empty response."
        )

    if not response.get("status"):
        raise RuntimeError(
            "Order Book failed: "
            + str(response)
        )

    orders = response.get(
        "data"
    ) or []

    st.session_state.order_book = orders

    return orders


# ============================================================
# POSITIONS
# ============================================================

def refresh_positions():

    api = st.session_state.api

    if api is None:
        return []

    try:

        response = api.position()

        if (
            response
            and response.get("status")
        ):
            positions = (
                response.get(
                    "data"
                )
                or []
            )

            st.session_state.positions = (
                positions
            )

            return positions

    except Exception:
        pass

    return []


# ============================================================
# PLACE BUY ORDER
# ============================================================

def place_buy_order(
    signal_type,
    option,
):
    """
    Places a BUY order.

    Returns:
        order_id
    """

    if signal_type not in (
        "BUY_CE",
        "BUY_PE",
    ):
        raise ValueError(
            "Invalid signal type."
        )

    if not option:
        raise RuntimeError(
            "Option information unavailable."
        )

    symbol = option["symbol"]
    token = str(option["token"])

    quantity = int(
        option["lot_size"]
    )

    # --------------------------------------------------------
    # DUPLICATE PROTECTION
    # --------------------------------------------------------

    if DUPLICATE_PROTECTION:

        if local_duplicate_exists(
            signal_type,
            symbol,
        ):
            raise RuntimeError(
                f"Duplicate protection blocked "
                f"{signal_type} for {symbol}. "
                f"A live order already exists today."
            )

        if orderbook_duplicate_exists(
            signal_type,
            symbol,
        ):
            raise RuntimeError(
                f"Duplicate protection blocked "
                f"{signal_type} for {symbol}. "
                f"The Angel One Order Book already "
                f"contains a BUY order for this symbol today."
            )

    # --------------------------------------------------------
    # PAPER MODE
    # --------------------------------------------------------

    if not LIVE_TRADING:

        fake_order_id = (
            "PAPER-"
            + datetime.now(
                IST
            ).strftime(
                "%Y%m%d%H%M%S"
            )
        )

        state = load_state()

        state["orders"].append({
            "date": now_ist().date().isoformat(),
            "time": now_ist().isoformat(),
            "signal": signal_type,
            "symbol": symbol,
            "token": token,
            "quantity": quantity,
            "order_id": fake_order_id,
            "live_order": False,
            "mode": "PAPER",
        })

        save_state(state)

        return fake_order_id

    # --------------------------------------------------------
    # REAL ORDER
    # --------------------------------------------------------

    api = st.session_state.api

    if api is None:
        raise RuntimeError(
            "Not connected to Angel One."
        )

    order_params = {
        "variety": ORDER_VARIETY,

        "tradingsymbol": symbol,

        "symboltoken": token,

        "transactiontype": "BUY",

        "exchange": "NFO",

        "ordertype": ORDER_TYPE,

        "producttype": NFO_PRODUCT_TYPE,

        "duration": ORDER_DURATION,

        "quantity": str(quantity),

        "price": "0",

        "squareoff": "0",

        "stoploss": "0",

        "ordertag": ORDER_TAG,
    }

    order_id = api.placeOrder(
        order_params
    )

    if not order_id:
        raise RuntimeError(
            "Angel One did not return an Order ID."
        )

    # --------------------------------------------------------
    # SAVE LOCAL ORDER STATE
    # --------------------------------------------------------

    state = load_state()

    state["orders"].append({
        "date": now_ist().date().isoformat(),
        "time": now_ist().isoformat(),
        "signal": signal_type,
        "symbol": symbol,
        "token": token,
        "quantity": quantity,
        "order_id": str(order_id),
        "live_order": True,
        "mode": "LIVE",
    })

    save_state(state)

    return str(order_id)


# ============================================================
# EXECUTE ORDER + OPEN ORDER BOOK
# ============================================================

def execute_buy_and_open_orderbook(
    signal_type,
    option,
):

    symbol = option["symbol"]

    try:

        # ----------------------------------------------------
        # PLACE ORDER
        # ----------------------------------------------------

        order_id = place_buy_order(
            signal_type,
            option,
        )

        # ----------------------------------------------------
        # SAVE HIGHLIGHT
        # ----------------------------------------------------

        st.session_state.last_order_id = (
            str(order_id)
        )

        st.session_state.last_order_time = (
            now_ist().isoformat()
        )

        st.session_state.highlight_order_id = (
            str(order_id)
        )

        st.session_state.highlight_symbol = (
            symbol
        )

        # ----------------------------------------------------
        # REFRESH ORDER BOOK
        # ----------------------------------------------------

        if LIVE_TRADING:

            # Give the broker a short moment
            # to make the order visible.
            time.sleep(0.5)

            try:
                refresh_order_book()

            except Exception as e:
                st.warning(
                    "Order was submitted, but "
                    "Order Book refresh failed: "
                    f"{e}"
                )

        else:

            # Create a paper order-book entry
            # so the UI demonstrates the same flow.
            paper_order = {
                "variety": "NORMAL",
                "ordertype": "MARKET",
                "producttype": NFO_PRODUCT_TYPE,
                "duration": "DAY",
                "price": "0",
                "quantity": str(
                    option["lot_size"]
                ),
                "tradingsymbol": symbol,
                "transactiontype": "BUY",
                "exchange": "NFO",
                "symboltoken": str(
                    option["token"]
                ),
                "orderid": str(
                    order_id
                ),
                "status": "PAPER",
                "orderstatus": "PAPER",
                "updatetime": now_ist().strftime(
                    "%d-%b-%Y %H:%M:%S"
                ),
                "ordertag": ORDER_TAG,
            }

            current_orders = (
                st.session_state.order_book
                or []
            )

            st.session_state.order_book = [
                paper_order
            ] + current_orders

        # ----------------------------------------------------
        # AUTOMATICALLY OPEN ORDER BOOK
        # ----------------------------------------------------

        st.session_state.selected_section = (
            "Order Book"
        )

        st.session_state.last_message = (
            f"{signal_type} submitted: "
            f"{symbol} | Order ID: {order_id}"
        )

        return True, str(order_id)

    except Exception as e:

        st.session_state.last_error = str(e)

        return False, str(e)


# ============================================================
# ORDER BOOK TABLE
# ============================================================

def show_order_book():

    st.subheader("📋 Order Book")

    col1, col2 = st.columns(
        [1, 5]
    )

    with col1:

        if st.button(
            "🔄 Refresh",
            key="refresh_order_book",
            use_container_width=True,
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
                "⭐ Highlighted order: "
                f"{st.session_state.highlight_order_id}"
            )

    orders = (
        st.session_state.order_book
        or []
    )

    if not orders:

        st.info(
            "No orders in Order Book."
        )

        return

    rows = []

    for order in orders:

        rows.append({
            "⭐": (
                "NEW"
                if str(
                    order.get(
                        "orderid",
                        ""
                    )
                )
                == str(
                    st.session_state.highlight_order_id
                )
                else ""
            ),

            "Order ID": order.get(
                "orderid",
                "",
            ),

            "Symbol": order.get(
                "tradingsymbol",
                "",
            ),

            "Side": order.get(
                "transactiontype",
                "",
            ),

            "Qty": order.get(
                "quantity",
                "",
            ),

            "Order Type": order.get(
                "ordertype",
                "",
            ),

            "Product": order.get(
                "producttype",
                "",
            ),

            "Status": order.get(
                "orderstatus",
                order.get(
                    "status",
                    "",
                ),
            ),

            "Price": order.get(
                "price",
                "",
            ),

            "Average Price": order.get(
                "averageprice",
                "",
            ),

            "Time": order.get(
                "updatetime",
                "",
            ),

            "Message": order.get(
                "text",
                "",
            ),
        })

    df = pd.DataFrame(rows)

    # Put highlighted order first.
    highlight_id = str(
        st.session_state.highlight_order_id
        or ""
    )

    if highlight_id:

        df["_highlight"] = (
            df["Order ID"]
            .astype(str)
            .eq(highlight_id)
        )

        df = df.sort_values(
            "_highlight",
            ascending=False,
        )

        df = df.drop(
            columns=["_highlight"]
        )

    st.dataframe(
        df,
        use_container_width=True,
        hide_index=True,
    )

    # --------------------------------------------------------
    # NEW ORDER DETAILS
    # --------------------------------------------------------

    if highlight_id:

        highlighted = None

        for order in orders:

            if str(
                order.get(
                    "orderid",
                    ""
                )
            ) == highlight_id:

                highlighted = order
                break

        if highlighted:

            st.divider()

            st.subheader(
                "⭐ Newly Placed Order"
            )

            h1, h2, h3, h4 = st.columns(
                4
            )

            h1.metric(
                "Order ID",
                str(
                    highlighted.get(
                        "orderid",
                        "-"
                    )
                ),
            )

            h2.metric(
                "Symbol",
                str(
                    highlighted.get(
                        "tradingsymbol",
                        "-"
                    )
                ),
            )

            h3.metric(
                "Side",
                str(
                    highlighted.get(
                        "transactiontype",
                        "-"
                    )
                ),
            )

            h4.metric(
                "Status",
                str(
                    highlighted.get(
                        "orderstatus",
                        highlighted.get(
                            "status",
                            "-"
                        ),
                    )
                ),
            )

            message = highlighted.get(
                "text",
                "",
            )

            if message:
                st.warning(
                    f"Broker message: {message}"
                )


# ============================================================
# ORDER PANEL
# ============================================================

def show_order_panel(
    spot,
):

    st.subheader(
        "🛒 BUY NIFTY OPTIONS"
    )

    st.caption(
        "BUY CE / BUY PE buttons are "
        "always available. They do not "
        "depend on the current Supertrend signal."
    )

    ce_option = None
    pe_option = None

    # --------------------------------------------------------
    # CE
    # --------------------------------------------------------

    try:

        ce_option = select_atm_option(
            st.session_state.instruments,
            spot,
            "CE",
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

        st.session_state.ce_ltp = (
            ce_option["ltp"]
        )

    except Exception as e:

        st.error(
            f"ATM CE unavailable: {e}"
        )

    # --------------------------------------------------------
    # PE
    # --------------------------------------------------------

    try:

        pe_option = select_atm_option(
            st.session_state.instruments,
            spot,
            "PE",
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

        st.session_state.pe_ltp = (
            pe_option["ltp"]
        )

    except Exception as e:

        st.error(
            f"ATM PE unavailable: {e}"
        )

    # --------------------------------------------------------
    # DISPLAY
    # --------------------------------------------------------

    c1, c2 = st.columns(2)

    # ========================================================
    # CE PANEL
    # ========================================================

    with c1:

        st.markdown(
            "### 🟢 NIFTY ATM CE"
        )

        if ce_option:

            m1, m2, m3 = st.columns(3)

            m1.metric(
                "Strike",
                fmt_number(
                    ce_option["strike"],
                    0,
                ),
            )

            m2.metric(
                "LTP",
                fmt_number(
                    ce_option.get("ltp")
                ),
            )

            m3.metric(
                "Lot",
                str(
                    ce_option["lot_size"]
                ),
            )

            st.write(
                f"**Symbol:** "
                f"`{ce_option['symbol']}`"
            )

            st.write(
                f"**Expiry:** "
                f"`{ce_option['expiry']}`"
            )

            st.write(
                f"**Token:** "
                f"`{ce_option['token']}`"
            )

            st.warning(
                "Manual BUY CE is available "
                "even when the strategy signal "
                "is WAIT."
            )

            if st.button(
                "🛒 BUY CE",
                key="buy_ce_button",
                type="primary",
                use_container_width=True,
            ):

                success, result = (
                    execute_buy_and_open_orderbook(
                        "BUY_CE",
                        ce_option,
                    )
                )

                if success:

                    if LIVE_TRADING:

                        st.success(
                            "BUY CE order submitted."
                        )

                    else:

                        st.success(
                            "PAPER BUY CE "
                            "created successfully."
                        )

                    st.rerun()

                else:

                    st.error(
                        f"BUY CE failed: {result}"
                    )

    # ========================================================
    # PE PANEL
    # ========================================================

    with c2:

        st.markdown(
            "### 🔴 NIFTY ATM PE"
        )

        if pe_option:

            m1, m2, m3 = st.columns(3)

            m1.metric(
                "Strike",
                fmt_number(
                    pe_option["strike"],
                    0,
                ),
            )

            m2.metric(
                "LTP",
                fmt_number(
                    pe_option.get("ltp")
                ),
            )

            m3.metric(
                "Lot",
                str(
                    pe_option["lot_size"]
                ),
            )

            st.write(
                f"**Symbol:** "
                f"`{pe_option['symbol']}`"
            )

            st.write(
                f"**Expiry:** "
                f"`{pe_option['expiry']}`"
            )

            st.write(
                f"**Token:** "
                f"`{pe_option['token']}`"
            )

            st.warning(
                "Manual BUY PE is available "
                "even when the strategy signal "
                "is WAIT."
            )

            if st.button(
                "🛒 BUY PE",
                key="buy_pe_button",
                type="primary",
                use_container_width=True,
            ):

                success, result = (
                    execute_buy_and_open_orderbook(
                        "BUY_PE",
                        pe_option,
                    )
                )

                if success:

                    if LIVE_TRADING:

                        st.success(
                            "BUY PE order submitted."
                        )

                    else:

                        st.success(
                            "PAPER BUY PE "
                            "created successfully."
                        )

                    st.rerun()

                else:

                    st.error(
                        f"BUY PE failed: {result}"
                    )


# ============================================================
# DASHBOARD
# ============================================================

def show_dashboard():

    st.title(
        "📈 NIFTY Live Supertrend Dashboard"
    )

    # --------------------------------------------------------
    # TOP CONTROLS
    # --------------------------------------------------------

    c1, c2, c3, c4 = st.columns(
        4
    )

    with c1:

        if st.button(
            "🔌 Connect Angel One",
            use_container_width=True,
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
            use_container_width=True,
        ):

            try:

                st.session_state.instruments = (
                    load_instruments(
                        force_refresh=False
                    )
                )

                st.success(
                    f"Loaded "
                    f"{len(st.session_state.instruments):,} "
                    f"instruments."
                )

            except Exception as e:

                st.error(
                    f"Instrument load failed: {e}"
                )

    with c3:

        st.metric(
            "Connection",
            st.session_state.login_status,
        )

    with c4:

        if LIVE_TRADING:

            st.error(
                "🔴 LIVE TRADING"
            )

        else:

            st.success(
                "🟢 PAPER MODE"
            )

    # --------------------------------------------------------
    # ERROR / MESSAGE
    # --------------------------------------------------------

    if st.session_state.last_error:

        st.error(
            st.session_state.last_error
        )

    if st.session_state.last_message:

        st.caption(
            st.session_state.last_message
        )

    # --------------------------------------------------------
    # CONNECTION CHECK
    # --------------------------------------------------------

    if st.session_state.api is None:

        st.info(
            "Click **Connect Angel One** "
            "to connect to SmartAPI."
        )

        return

    # --------------------------------------------------------
    # LOAD INSTRUMENTS
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # NIFTY LTP
    # --------------------------------------------------------

    try:

        spot = get_nifty_ltp()

        st.session_state.spot = spot

    except Exception as e:

        st.error(
            f"NIFTY LTP failed: {e}"
        )

        return

    # --------------------------------------------------------
    # CANDLES
    # --------------------------------------------------------

    try:

        df5 = get_nifty_candles(
            days=30
        )

        df5 = remove_incomplete_candle(
            df5
        )

        if len(df5) < 50:

            st.warning(
                "Not enough candles for "
                "Supertrend calculation."
            )

            return

        (
            st5,
            st15,
            st4h,
        ) = build_timeframes(
            df5
        )

        st.session_state.st5 = st5
        st.session_state.st15 = st15
        st.session_state.st4h = st4h

    except Exception as e:

        st.error(
            f"Candle/Supertrend error: {e}"
        )

        return

    # --------------------------------------------------------
    # SIGNAL
    # --------------------------------------------------------

    signal, signal_time = (
        current_signal(
            st5,
            st15,
            st4h,
        )
    )

    st.session_state.signal = signal
    st.session_state.signal_time = (
        signal_time
    )

    # --------------------------------------------------------
    # TOP METRICS
    # --------------------------------------------------------

    st.divider()

    m1, m2, m3, m4, m5 = st.columns(
        5
    )

    m1.metric(
        "NIFTY",
        fmt_number(spot),
    )

    latest5 = st5.iloc[-1]
    latest15 = st15.iloc[-1]
    latest4h = st4h.iloc[-1]

    m2.metric(
        "5m",
        "GREEN"
        if latest5["ST_Green"]
        else "RED",
    )

    m3.metric(
        "15m",
        "GREEN"
        if latest15["ST_Green"]
        else "RED",
    )

    m4.metric(
        "4H",
        "GREEN"
        if latest4h["ST_Green"]
        else "RED",
    )

    m5.metric(
        "SIGNAL",
        signal,
    )

    # --------------------------------------------------------
    # SIGNAL INFORMATION
    # --------------------------------------------------------

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
            "⏳ WAIT — No confirmed "
            "Supertrend entry."
        )

    # --------------------------------------------------------
    # ORDER PANEL
    # --------------------------------------------------------

    show_order_panel(
        spot
    )

    # --------------------------------------------------------
    # CHART DATA
    # --------------------------------------------------------

    st.divider()

    st.subheader(
        "📊 NIFTY 5-Minute Supertrend"
    )

    chart_df = st5[
        [
            "timestamp",
            "close",
            "Supertrend",
        ]
    ].copy()

    chart_df = chart_df.set_index(
        "timestamp"
    )

    st.line_chart(
        chart_df,
        use_container_width=True,
    )

    # --------------------------------------------------------
    # LATEST CANDLE DATA
    # --------------------------------------------------------

    with st.expander(
        "Latest 5-minute candles"
    ):

        st.dataframe(
            st5.tail(50),
            use_container_width=True,
            hide_index=True,
        )


# ============================================================
# POSITIONS
# ============================================================

def show_positions():

    st.subheader(
        "📊 Positions"
    )

    try:

        positions = refresh_positions()

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
            hide_index=True,
        )

    except Exception as e:

        st.error(
            f"Positions failed: {e}"
        )


# ============================================================
# MAIN NAVIGATION
# ============================================================

st.sidebar.title(
    "📈 NIFTY Trading"
)

st.sidebar.caption(
    "Supertrend 20,2"
)

# ------------------------------------------------------------
# LIVE MODE WARNING
# ------------------------------------------------------------

if LIVE_TRADING:

    st.sidebar.error(
        "🔴 LIVE TRADING ENABLED"
    )

else:

    st.sidebar.success(
        "🟢 PAPER MODE"
    )

# ------------------------------------------------------------
# NAVIGATION
# ------------------------------------------------------------

section = st.sidebar.radio(
    "Open",
    [
        "Dashboard",
        "Order Book",
        "Positions",
    ],
    index=[
        "Dashboard",
        "Order Book",
        "Positions",
    ].index(
        st.session_state.selected_section
    ),
)

st.session_state.selected_section = (
    section
)


# ============================================================
# SECTION ROUTING
# ============================================================

if section == "Dashboard":

    show_dashboard()

elif section == "Order Book":

    st.title(
        "📋 Angel One Order Book"
    )

    if st.session_state.api is None:

        st.warning(
            "Connect to Angel One first."
        )

    else:

        if st.button(
            "🔄 Refresh Order Book",
            use_container_width=True,
        ):

            try:

                refresh_order_book()

                st.success(
                    "Order Book refreshed."
                )

            except Exception as e:

                st.error(
                    f"Order Book error: {e}"
                )

        show_order_book()


elif section == "Positions":

    st.title(
        "📊 Positions"
    )

    if st.session_state.api is None:

        st.warning(
            "Connect to Angel One first."
        )

    else:

        show_positions()


# ============================================================
# FOOTER
# ============================================================

st.sidebar.divider()

st.sidebar.write(
    f"Last update: "
    f"{now_ist().strftime('%d-%m-%Y %H:%M:%S')}"
)

st.sidebar.caption(
    "NIFTY Supertrend 20,2 | "
    "5m + 15m + 4H"
)
