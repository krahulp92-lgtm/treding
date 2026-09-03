
# ============================================================
# dashboard.py
# NIFTY LIVE SUPERTREND 20,2 - ANGEL ONE
# ============================================================

import json
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
    page_title="NIFTY Live Supertrend",
    page_icon="📈",
    layout="wide",
)


# ============================================================
# SETTINGS
# ============================================================

IST = ZoneInfo("Asia/Kolkata")

# REQUIRED STRATEGY SETTINGS
ST_PERIOD = 20
ST_MULTIPLIER = 2.0

# SAFETY
# False = no real order
# True  = REAL Angel One order
LIVE_TRADING = True

LOTS = 1
ORDER_TYPE = "MARKET"
PRODUCT_TYPE = "INTRADAY"

REFRESH_SECONDS = 20

# History
CANDLE_DAYS = 40

# Angel One NIFTY 50 index token
NIFTY_TOKEN = "99926000"
NIFTY_SYMBOL = "NIFTY"

BASE_DIR = Path(__file__).resolve().parent

STATE_FILE = BASE_DIR / "nifty_live_state.json"

INSTRUMENT_FILE = BASE_DIR / "OpenAPIScripMaster.json"

INSTRUMENT_MASTER_URL = (
    "https://margincalculator.angelone.in/"
    "OpenAPI_File/files/OpenAPIScripMaster.json"
)


# ============================================================
# SESSION STATE
# ============================================================

DEFAULTS = {
    "running": False,

    "api": None,

    "instruments": None,

    "login_status": "NOT CONNECTED",

    "last_error": "",

    "last_message": "Ready",

    "spot": None,

    "st5": None,
    "st15": None,
    "st4h": None,

    "st5_value": None,
    "st15_value": None,
    "st4h_value": None,

    "signal": "WAIT",

    "signal_time": None,

    "option_symbol": None,
    "option_token": None,
    "option_expiry": None,
    "option_strike": None,
    "option_lot_size": None,
    "option_ltp": None,

    "last_order_id": None,

    "last_update": None,

    "candles": None,

    "last_candle_time": None,
}

for key, value in DEFAULTS.items():

    if key not in st.session_state:

        st.session_state[key] = value


# ============================================================
# SECRETS
# ============================================================

def get_secret(name):

    try:

        value = st.secrets.get(name)

    except Exception:

        value = None

    if value is None:

        return ""

    return str(value).strip()


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
# TIME
# ============================================================

def now_ist():

    return datetime.now(IST)


# ============================================================
# TOTP
# ============================================================

def get_totp_secret(raw):

    if not raw:

        return ""

    value = str(raw).strip()

    if value.lower().startswith(
        "otpauth://"
    ):

        parsed = urlparse(value)

        values = parse_qs(
            parsed.query
        ).get("secret", [])

        if not values:

            raise ValueError(
                "No secret found in otpauth URI."
            )

        value = values[0]

    return (
        value
        .replace(" ", "")
        .replace("\t", "")
        .replace("\r", "")
        .replace("\n", "")
        .upper()
        .strip()
    )


def generate_totp():

    secret = get_totp_secret(
        ANGEL_TOTP_SECRET
    )

    if not secret:

        raise ValueError(
            "ANGEL_TOTP_SECRET is empty."
        )

    return pyotp.TOTP(
        secret
    ).now()


# ============================================================
# CREDENTIAL VALIDATION
# ============================================================

def validate_credentials():

    missing = []

    credentials = {
        "ANGEL_API_KEY": ANGEL_API_KEY,
        "ANGEL_CLIENT_ID": ANGEL_CLIENT_ID,
        "ANGEL_PASSWORD": ANGEL_PASSWORD,
        "ANGEL_TOTP_SECRET": ANGEL_TOTP_SECRET,
    }

    for name, value in credentials.items():

        if not value:

            missing.append(name)

    if missing:

        raise RuntimeError(
            "Missing Streamlit secrets: "
            + ", ".join(missing)
        )

    # Validate TOTP before login
    totp = generate_totp()

    if len(totp) != 6:

        raise RuntimeError(
            "Invalid TOTP generated."
        )


# ============================================================
# ANGEL ONE LOGIN
# ============================================================

def angel_login():

    validate_credentials()

    api = SmartConnect(
        api_key=ANGEL_API_KEY
    )

    totp = generate_totp()

    response = api.generateSession(
        ANGEL_CLIENT_ID,
        ANGEL_PASSWORD,
        totp,
    )

    if not response:

        raise RuntimeError(
            "Angel One returned no login response."
        )

    if response.get("status") is not True:

        raise RuntimeError(
            "Angel One LOGIN FAILED | "
            f"message={response.get('message')} | "
            f"errorcode={response.get('errorcode')} | "
            f"status={response.get('status')}"
        )

    data = response.get("data") or {}

    if not data.get("jwtToken"):

        raise RuntimeError(
            "Login succeeded but JWT token was not returned."
        )

    st.session_state.api = api

    st.session_state.login_status = (
        "CONNECTED"
    )

    st.session_state.last_error = ""

    st.session_state.last_message = (
        "Angel One login successful."
    )

    return api


def ensure_login():

    api = st.session_state.api

    if api is None:

        return angel_login()

    return api


# ============================================================
# MARKET HOURS
# ============================================================

def market_is_open():

    now = now_ist()

    # Saturday / Sunday
    if now.weekday() >= 5:

        return False

    start = now.replace(
        hour=9,
        minute=15,
        second=0,
        microsecond=0,
    )

    end = now.replace(
        hour=15,
        minute=30,
        second=0,
        microsecond=0,
    )

    return start <= now <= end


# ============================================================
# LIVE NIFTY LTP
# ============================================================

def get_nifty_live_ltp():

    api = ensure_login()

    response = api.ltpData(
        "NSE",
        NIFTY_SYMBOL,
        NIFTY_TOKEN,
    )

    if not response:

        raise RuntimeError(
            "NIFTY LTP API returned no response."
        )

    if response.get("status") is not True:

        raise RuntimeError(
            "NIFTY LTP FAILED | "
            f"message={response.get('message')} | "
            f"errorcode={response.get('errorcode')} | "
            f"status={response.get('status')}"
        )

    data = response.get("data") or {}

    ltp = data.get("ltp")

    if ltp is None:

        raise RuntimeError(
            "NIFTY LTP missing from response: "
            + str(response)
        )

    return float(ltp)


# ============================================================
# NIFTY 5 MINUTE CANDLES
# ============================================================

def get_nifty_5m_candles(
    days=CANDLE_DAYS
):

    api = ensure_login()

    now = now_ist()

    start = now - timedelta(
        days=days
    )

    params = {
        "exchange": "NSE",
        "symboltoken": NIFTY_TOKEN,
        "interval": "FIVE_MINUTE",
        "fromdate": start.strftime(
            "%Y-%m-%d %H:%M"
        ),
        "todate": now.strftime(
            "%Y-%m-%d %H:%M"
        ),
    }

    response = api.getCandleData(
        params
    )

    if not response:

        raise RuntimeError(
            "Candle API returned no response."
        )

    if response.get("status") is not True:

        raise RuntimeError(
            "CANDLE API FAILED | "
            f"message={response.get('message')} | "
            f"errorcode={response.get('errorcode')} | "
            f"status={response.get('status')}"
        )

    rows = response.get("data") or []

    if not rows:

        raise RuntimeError(
            "Candle API succeeded but returned "
            "zero candles."
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

    if df.empty:

        return df

    # Angel One candle timestamp = candle START.
    #
    # Example:
    # 09:15 timestamp represents
    # 09:15 -> 09:20 candle.
    #
    # Therefore exclude currently forming candle.

    current_start = pd.Timestamp(
        now
    ).floor("5min")

    df = df[
        df["timestamp"] < current_start
    ]

    return df.reset_index(
        drop=True
    )


# ============================================================
# NORMALIZE OHLC
# ============================================================

def normalize_ohlc_columns(df):

    if df is None or df.empty:

        return pd.DataFrame()

    out = df.copy()

    if isinstance(
        out.columns,
        pd.MultiIndex
    ):

        out.columns = (
            out.columns
            .get_level_values(0)
        )

    rename_map = {}

    for col in out.columns:

        name = (
            str(col)
            .strip()
            .lower()
        )

        if name == "open":
            rename_map[col] = "open"

        elif name == "high":
            rename_map[col] = "high"

        elif name == "low":
            rename_map[col] = "low"

        elif name == "close":
            rename_map[col] = "close"

        elif name == "volume":
            rename_map[col] = "volume"

    out = out.rename(
        columns=rename_map
    )

    required = [
        "open",
        "high",
        "low",
        "close",
    ]

    missing = [
        x
        for x in required
        if x not in out.columns
    ]

    if missing:

        raise ValueError(
            f"Missing OHLC columns: {missing}"
        )

    for col in required:

        out[col] = pd.to_numeric(
            out[col],
            errors="coerce",
        )

    if "volume" in out.columns:

        out["volume"] = pd.to_numeric(
            out["volume"],
            errors="coerce",
        )

    out = out.dropna(
        subset=required
    )

    return out


# ============================================================
# SUPERTREND 20,2
# ============================================================

def calculate_supertrend(
    df,
    period=ST_PERIOD,
    multiplier=ST_MULTIPLIER,
):

    df = normalize_ohlc_columns(
        df
    )

    if df.empty:

        return df

    if len(df) < period + 2:

        raise ValueError(
            f"Not enough candles for "
            f"Supertrend {period},{multiplier}. "
            f"Need {period + 2}; "
            f"got {len(df)}."
        )

    previous_close = (
        df["close"].shift(1)
    )

    tr1 = (
        df["high"]
        - df["low"]
    )

    tr2 = (
        df["high"]
        - previous_close
    ).abs()

    tr3 = (
        df["low"]
        - previous_close
    ).abs()

    true_range = pd.concat(
        [tr1, tr2, tr3],
        axis=1,
    ).max(axis=1)

    # Wilder RMA
    df["ATR"] = true_range.ewm(
        alpha=1 / period,
        adjust=False,
        min_periods=period,
    ).mean()

    hl2 = (
        df["high"]
        + df["low"]
    ) / 2.0

    df["Basic_Upper"] = (
        hl2
        + multiplier * df["ATR"]
    )

    df["Basic_Lower"] = (
        hl2
        - multiplier * df["ATR"]
    )

    final_upper = pd.Series(
        np.nan,
        index=df.index,
        dtype=float,
    )

    final_lower = pd.Series(
        np.nan,
        index=df.index,
        dtype=float,
    )

    supertrend_line = pd.Series(
        np.nan,
        index=df.index,
        dtype=float,
    )

    direction = pd.Series(
        0,
        index=df.index,
        dtype=int,
    )

    valid = np.flatnonzero(
        df["ATR"]
        .notna()
        .to_numpy()
    )

    if len(valid) == 0:

        raise ValueError(
            "ATR could not be calculated."
        )

    start = int(valid[0])

    for i in range(
        start,
        len(df)
    ):

        if i == start:

            final_upper.iloc[i] = (
                df["Basic_Upper"].iloc[i]
            )

            final_lower.iloc[i] = (
                df["Basic_Lower"].iloc[i]
            )

            direction.iloc[i] = 1

            supertrend_line.iloc[i] = (
                final_lower.iloc[i]
            )

            continue

        prev_fu = (
            final_upper.iloc[i - 1]
        )

        prev_fl = (
            final_lower.iloc[i - 1]
        )

        basic_u = (
            df["Basic_Upper"].iloc[i]
        )

        basic_l = (
            df["Basic_Lower"].iloc[i]
        )

        close_prev = (
            df["close"].iloc[i - 1]
        )

        close_now = (
            df["close"].iloc[i]
        )

        if (
            basic_u < prev_fu
            or close_prev > prev_fu
        ):

            final_upper.iloc[i] = (
                basic_u
            )

        else:

            final_upper.iloc[i] = (
                prev_fu
            )

        if (
            basic_l > prev_fl
            or close_prev < prev_fl
        ):

            final_lower.iloc[i] = (
                basic_l
            )

        else:

            final_lower.iloc[i] = (
                prev_fl
            )

        # Previous RED
        if direction.iloc[i - 1] == -1:

            if close_now > final_upper.iloc[i]:

                direction.iloc[i] = 1

                supertrend_line.iloc[i] = (
                    final_lower.iloc[i]
                )

            else:

                direction.iloc[i] = -1

                supertrend_line.iloc[i] = (
                    final_upper.iloc[i]
                )

        # Previous GREEN
        else:

            if close_now < final_lower.iloc[i]:

                direction.iloc[i] = -1

                supertrend_line.iloc[i] = (
                    final_upper.iloc[i]
                )

            else:

                direction.iloc[i] = 1

                supertrend_line.iloc[i] = (
                    final_lower.iloc[i]
                )

    df["Final_Upper"] = final_upper

    df["Final_Lower"] = final_lower

    df["Supertrend"] = (
        supertrend_line
    )

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
        & (previous_direction == -1)
    )

    df["ST_Flip_Red"] = (
        (direction == -1)
        & (previous_direction == 1)
    )

    return df


# ============================================================
# RESAMPLE 5m -> 15m / 4h
# ============================================================

def resample_ohlcv(
    df,
    rule,
):

    df = normalize_ohlc_columns(
        df
    )

    if df.empty:

        return pd.DataFrame()

    temp = df.copy()

    if "timestamp" in temp.columns:

        temp["timestamp"] = pd.to_datetime(
            temp["timestamp"],
            errors="coerce",
        )

        temp = temp.dropna(
            subset=["timestamp"]
        )

        temp = temp.set_index(
            "timestamp"
        )

    if temp.index.tz is None:

        temp.index = (
            temp.index.tz_localize(
                IST
            )
        )

    else:

        temp.index = (
            temp.index.tz_convert(
                IST
            )
        )

    if "volume" not in temp.columns:

        temp["volume"] = 0

    out = (
        temp.resample(
            rule,
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
        .dropna(
            subset=[
                "open",
                "high",
                "low",
                "close",
            ]
        )
    )

    # The resampled timestamp is the END
    # of the candle.
    #
    # Keep only candles whose end time
    # has already passed.

    now = pd.Timestamp(
        now_ist()
    )

    if out.index.tz is None:

        out.index = (
            out.index.tz_localize(
                IST
            )
        )

    else:

        out.index = (
            out.index.tz_convert(
                IST
            )
        )

    out = out[
        out.index <= now
    ]

    return out


# ============================================================
# BUILD TIMEFRAMES
# ============================================================

def build_timeframes(
    df5
):

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

    if len(df15) < ST_PERIOD + 2:

        raise ValueError(
            "Not enough completed "
            "15-minute candles."
        )

    if len(df4h) < ST_PERIOD + 2:

        raise ValueError(
            "Not enough completed "
            "4-hour candles. "
            "Increase CANDLE_DAYS."
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

def get_current_signal(
    df5,
    df15,
    df4h,
):

    v5 = df5.dropna(
        subset=[
            "Supertrend",
            "ST_Direction",
        ]
    )

    v15 = df15.dropna(
        subset=[
            "Supertrend",
            "ST_Direction",
        ]
    )

    v4h = df4h.dropna(
        subset=[
            "Supertrend",
            "ST_Direction",
        ]
    )

    if (
        len(v5) < 2
        or v15.empty
        or v4h.empty
    ):

        return None

    previous5 = v5.iloc[-2]

    last5 = v5.iloc[-1]

    last15 = v15.iloc[-1]

    last4h = v4h.iloc[-1]

    previous_direction = int(
        previous5["ST_Direction"]
    )

    d5 = int(
        last5["ST_Direction"]
    )

    d15 = int(
        last15["ST_Direction"]
    )

    d4h = int(
        last4h["ST_Direction"]
    )

    buy_ce = (
        previous_direction == -1
        and d5 == 1
        and d15 == 1
        and d4h == 1
    )

    buy_pe = (
        previous_direction == 1
        and d5 == -1
        and d15 == -1
        and d4h == -1
    )

    action = "WAIT"

    option_type = None

    if buy_ce:

        action = "BUY CE"

        option_type = "CE"

    elif buy_pe:

        action = "BUY PE"

        option_type = "PE"

    return {
        "time": v5.index[-1],
        "spot": float(
            last5["close"]
        ),
        "5m": d5,
        "15m": d15,
        "4h": d4h,
        "st5": float(
            last5["Supertrend"]
        ),
        "st15": float(
            last15["Supertrend"]
        ),
        "st4h": float(
            last4h["Supertrend"]
        ),
        "action": action,
        "option_type": option_type,
    }


# ============================================================
# INSTRUMENT MASTER
# ============================================================

def download_instrument_master(
    force=False
):

    if (
        INSTRUMENT_FILE.exists()
        and not force
    ):

        try:

            age = (
                datetime.now().timestamp()
                - INSTRUMENT_FILE.stat().st_mtime
            )

            if age < 86400:

                data = json.loads(
                    INSTRUMENT_FILE.read_text(
                        encoding="utf-8"
                    )
                )

                if (
                    isinstance(data, list)
                    and data
                ):

                    return data

        except Exception:

            pass

    response = requests.get(
        INSTRUMENT_MASTER_URL,
        timeout=120,
        headers={
            "User-Agent": "Mozilla/5.0"
        },
    )

    response.raise_for_status()

    data = response.json()

    if (
        not isinstance(data, list)
        or not data
    ):

        raise ValueError(
            "Instrument master invalid."
        )

    try:

        INSTRUMENT_FILE.write_text(
            json.dumps(data),
            encoding="utf-8",
        )

    except Exception:

        # Streamlit Cloud may not always
        # allow persistent filesystem writes.
        pass

    return data


# ============================================================
# OPTION HELPERS
# ============================================================

def parse_expiry(value):

    text = str(
        value or ""
    ).strip().upper()

    for fmt in [
        "%d%b%Y",
        "%d-%b-%Y",
        "%Y-%m-%d",
    ]:

        try:

            return datetime.strptime(
                text,
                fmt,
            ).date()

        except ValueError:

            continue

    return None


def normalized_strike(value):

    try:

        strike = float(value)

    except (
        TypeError,
        ValueError,
    ):

        return None

    if strike > 100000:

        strike /= 100

    return strike


def select_atm_option(
    instruments,
    spot,
    option_type,
):

    today = now_ist().date()

    candidates = []

    for item in instruments:

        exchange = str(
            item.get(
                "exch_seg",
                ""
            )
        ).upper().strip()

        instrument_type = str(
            item.get(
                "instrumenttype",
                ""
            )
        ).upper().strip()

        name = str(
            item.get(
                "name",
                ""
            )
        ).upper().strip()

        symbol = str(
            item.get(
                "symbol",
                ""
            )
        ).upper().strip()

        if exchange != "NFO":
            continue

        if instrument_type != "OPTIDX":
            continue

        if name not in [
            "NIFTY",
            "NIFTY 50",
        ]:
            continue

        if not symbol.endswith(
            option_type.upper()
        ):
            continue

        expiry = parse_expiry(
            item.get("expiry")
        )

        if (
            expiry is None
            or expiry < today
        ):
            continue

        strike = normalized_strike(
            item.get("strike")
        )

        if (
            strike is None
            or strike <= 0
        ):
            continue

        try:

            lot_size = int(
                float(
                    item.get(
                        "lotsize",
                        0
                    )
                )
            )

        except (
            TypeError,
            ValueError,
        ):

            lot_size = 0

        if lot_size <= 0:
            continue

        candidates.append(
            {
                "symbol": item.get(
                    "symbol"
                ),
                "token": str(
                    item.get("token")
                ),
                "expiry": expiry,
                "strike": strike,
                "lot_size": lot_size,
            }
        )

    if not candidates:

        return None

    nearest_expiry = min(
        x["expiry"]
        for x in candidates
    )

    same_expiry = [
        x
        for x in candidates
        if x["expiry"]
        == nearest_expiry
    ]

    return min(
        same_expiry,
        key=lambda x: abs(
            x["strike"]
            - float(spot)
        ),
    )


# ============================================================
# OPTION LTP
# ============================================================

def get_ltp(
    exchange,
    symbol,
    token,
):

    api = ensure_login()

    response = api.ltpData(
        exchange,
        symbol,
        token,
    )

    if not response:

        return None

    if response.get(
        "status"
    ) is not True:

        return None

    data = response.get(
        "data"
    ) or {}

    value = data.get(
        "ltp"
    )

    if value is None:

        return None

    return float(value)


# ============================================================
# ORDER
# ============================================================

def place_market_buy(
    option
):

    quantity = (
        int(option["lot_size"])
        * int(LOTS)
    )

    if quantity <= 0:

        raise ValueError(
            "Quantity must be > 0."
        )

    params = {
        "variety": "NORMAL",
        "tradingsymbol": option[
            "symbol"
        ],
        "symboltoken": option[
            "token"
        ],
        "transactiontype": "BUY",
        "exchange": "NFO",
        "ordertype": ORDER_TYPE,
        "producttype": PRODUCT_TYPE,
        "duration": "DAY",
        "price": "0",
        "squareoff": "0",
        "stoploss": "0",
        "quantity": str(quantity),
    }

    # --------------------------------------------------------
    # SAFETY
    # --------------------------------------------------------

    if not LIVE_TRADING:

        return "DRY-RUN"

    api = ensure_login()

    if hasattr(
        api,
        "placeOrderFullResponse",
    ):

        response = (
            api.placeOrderFullResponse(
                params
            )
        )

        if (
            isinstance(
                response,
                dict
            )
            and response.get(
                "status"
            ) is not True
        ):

            raise RuntimeError(
                "ORDER REJECTED | "
                f"message={response.get('message')} | "
                f"errorcode={response.get('errorcode')}"
            )

        if isinstance(
            response,
            dict
        ):

            data = (
                response.get(
                    "data"
                )
                or {}
            )

            return (
                data.get("orderid")
                or data.get(
                    "uniqueorderid"
                )
            )

        return response

    return api.placeOrder(
        params
    )


# ============================================================
# STATE FILE
# ============================================================

def load_state():

    if not STATE_FILE.exists():

        return {}

    try:

        return json.loads(
            STATE_FILE.read_text(
                encoding="utf-8"
            )
        )

    except Exception:

        return {}


def save_state(state):

    try:

        STATE_FILE.write_text(
            json.dumps(
                state,
                indent=2,
                default=str,
            ),
            encoding="utf-8",
        )

    except Exception:

        # Don't crash Streamlit Cloud
        # if filesystem is read-only.
        pass


# ============================================================
# DIRECTION TEXT
# ============================================================

def direction_text(
    value
):

    if value == 1:

        return "🟢 GREEN"

    if value == -1:

        return "🔴 RED"

    return "⚪ -"


# ============================================================
# LIVE CYCLE
# ============================================================

def run_live_cycle():

    st.session_state.last_error = ""

    # --------------------------------------------------------
    # LOGIN
    # --------------------------------------------------------

    ensure_login()

    # --------------------------------------------------------
    # LIVE NIFTY LTP
    # --------------------------------------------------------

    live_spot = get_nifty_live_ltp()

    st.session_state.spot = (
        live_spot
    )

    # --------------------------------------------------------
    # CANDLES
    # --------------------------------------------------------

    df5 = get_nifty_5m_candles(
        CANDLE_DAYS
    )

    if df5.empty:

        raise RuntimeError(
            "NIFTY 5-minute candle data is empty."
        )

    st.session_state.candles = (
        df5.tail(50)
        .copy()
    )

    # --------------------------------------------------------
    # TIMEFRAMES
    # --------------------------------------------------------

    st5, st15, st4h = (
        build_timeframes(df5)
    )

    # --------------------------------------------------------
    # SIGNAL
    # --------------------------------------------------------

    sig = get_current_signal(
        st5,
        st15,
        st4h,
    )

    if sig is None:

        raise RuntimeError(
            "Unable to calculate Supertrend."
        )

    # --------------------------------------------------------
    # DASHBOARD STATE
    # --------------------------------------------------------

    st.session_state.spot = (
        live_spot
    )

    st.session_state.st5 = (
        sig["5m"]
    )

    st.session_state.st15 = (
        sig["15m"]
    )

    st.session_state.st4h = (
        sig["4h"]
    )

    st.session_state.st5_value = (
        sig["st5"]
    )

    st.session_state.st15_value = (
        sig["st15"]
    )

    st.session_state.st4h_value = (
        sig["st4h"]
    )

    st.session_state.signal = (
        sig["action"]
    )

    st.session_state.signal_time = (
        str(sig["time"])
    )

    st.session_state.last_candle_time = (
        str(df5.iloc[-1]["timestamp"])
    )

    st.session_state.last_update = (
        now_ist().strftime(
            "%Y-%m-%d %H:%M:%S"
        )
    )

    # --------------------------------------------------------
    # NO NEW FLIP
    # --------------------------------------------------------

    if sig["option_type"] is None:

        st.session_state.last_message = (
            f"NIFTY ₹{live_spot:.2f} | "
            "No fresh 5m signal."
        )

        return

    # --------------------------------------------------------
    # DUPLICATE PROTECTION
    # --------------------------------------------------------

    state = load_state()

    signal_key = (
        f"{sig['time'].isoformat()}|"
        f"{sig['action']}"
    )

    if (
        state.get(
            "last_signal"
        )
        == signal_key
    ):

        st.session_state.last_message = (
            "Signal already processed. "
            "Duplicate order blocked."
        )

        return

    # --------------------------------------------------------
    # INSTRUMENT MASTER
    # --------------------------------------------------------

    if (
        st.session_state.instruments
        is None
    ):

        st.session_state.instruments = (
            download_instrument_master()
        )

    # --------------------------------------------------------
    # ATM OPTION
    # --------------------------------------------------------

    option = select_atm_option(
        st.session_state.instruments,
        live_spot,
        sig["option_type"],
    )

    if option is None:

        raise RuntimeError(
            f"ATM NIFTY "
            f"{sig['option_type']} option "
            "not found."
        )

    # --------------------------------------------------------
    # OPTION LTP
    # --------------------------------------------------------

    option_ltp = get_ltp(
        "NFO",
        option["symbol"],
        option["token"],
    )

    st.session_state.option_symbol = (
        option["symbol"]
    )

    st.session_state.option_token = (
        option["token"]
    )

    st.session_state.option_expiry = (
        str(option["expiry"])
    )

    st.session_state.option_strike = (
        option["strike"]
    )

    st.session_state.option_lot_size = (
        option["lot_size"]
    )

    st.session_state.option_ltp = (
        option_ltp
    )

    # --------------------------------------------------------
    # ORDER
    # --------------------------------------------------------

    order_id = place_market_buy(
        option
    )

    st.session_state.last_order_id = (
        order_id
    )

    # --------------------------------------------------------
    # SAVE SIGNAL
    # --------------------------------------------------------

    state.update(
        {
            "last_signal": signal_key,

            "last_signal_candle":
                sig["time"].isoformat(),

            "last_action":
                sig["action"],

            "last_option":
                option["symbol"],

            "last_order_id":
                order_id,

            "last_updated":
                now_ist().isoformat(),
        }
    )

    save_state(state)

    if LIVE_TRADING:

        st.session_state.last_message = (
            f"LIVE ORDER SENT: "
            f"{sig['action']} "
            f"{option['symbol']}"
        )

    else:

        st.session_state.last_message = (
            f"DRY RUN: "
            f"{sig['action']} "
            f"{option['symbol']}"
        )


# ============================================================
# SIDEBAR
# ============================================================

st.sidebar.title(
    "Trading Control"
)

st.sidebar.write(
    f"Supertrend: "
    f"**{ST_PERIOD},{ST_MULTIPLIER:g}**"
)

st.sidebar.write(
    f"History: **{CANDLE_DAYS} days**"
)

st.sidebar.write(
    f"Refresh: **{REFRESH_SECONDS}s**"
)

if LIVE_TRADING:

    st.sidebar.error(
        "⚠️ LIVE TRADING = ON"
    )

else:

    st.sidebar.success(
        "🛡️ DRY RUN = ON"
    )


# ============================================================
# LOGIN BUTTON
# ============================================================

if st.sidebar.button(
    "🔐 Login Angel One",
    use_container_width=True,
):

    try:

        angel_login()

        st.sidebar.success(
            "Login successful"
        )

    except Exception as exc:

        st.session_state.login_status = (
            "NOT CONNECTED"
        )

        st.session_state.last_error = (
            f"{type(exc).__name__}: {exc}"
        )

        st.sidebar.error(
            st.session_state.last_error
        )


# ============================================================
# TEST NIFTY BUTTON
# ============================================================

if st.sidebar.button(
    "📈 Test Live NIFTY",
    use_container_width=True,
):

    try:

        ensure_login()

        ltp = get_nifty_live_ltp()

        st.session_state.spot = ltp

        st.session_state.last_update = (
            now_ist().strftime(
                "%Y-%m-%d %H:%M:%S"
            )
        )

        st.session_state.last_message = (
            f"Live NIFTY = ₹{ltp:.2f}"
        )

        st.sidebar.success(
            f"NIFTY ₹{ltp:.2f}"
        )

    except Exception as exc:

        st.session_state.last_error = (
            f"{type(exc).__name__}: {exc}"
        )

        st.sidebar.error(
            st.session_state.last_error
        )


# ============================================================
# TEST CANDLES BUTTON
# ============================================================

if st.sidebar.button(
    "🕯 Test NIFTY Candles",
    use_container_width=True,
):

    try:

        ensure_login()

        df = get_nifty_5m_candles()

        st.session_state.candles = (
            df.tail(50)
        )

        st.sidebar.success(
            f"{len(df)} candles received"
        )

    except Exception as exc:

        st.session_state.last_error = (
            f"{type(exc).__name__}: {exc}"
        )

        st.sidebar.error(
            st.session_state.last_error
        )


# ============================================================
# START / STOP
# ============================================================

cstart, cstop = (
    st.sidebar.columns(2)
)


if cstart.button(
    "▶ START",
    use_container_width=True,
):

    st.session_state.running = True

    st.session_state.last_message = (
        "Live engine started."
    )

    st.rerun()


if cstop.button(
    "■ STOP",
    use_container_width=True,
):

    st.session_state.running = False

    st.session_state.last_message = (
        "Live engine stopped."
    )

    st.rerun()


# ============================================================
# RESET
# ============================================================

if st.sidebar.button(
    "🔄 Reset Connection",
    use_container_width=True,
):

    st.session_state.api = None

    st.session_state.login_status = (
        "NOT CONNECTED"
    )

    st.session_state.spot = None

    st.session_state.candles = None

    st.session_state.last_error = ""

    st.session_state.last_message = (
        "Connection reset."
    )

    st.rerun()


# ============================================================
# MAIN TITLE
# ============================================================

st.title(
    "📈 NIFTY LIVE SUPERTREND 20,2"
)

st.caption(
    "Live NIFTY + 5m Supertrend flip + "
    "15m confirmation + 4h confirmation"
)


# ============================================================
# TOP STATUS
# ============================================================

top1, top2, top3, top4 = (
    st.columns(4)
)

top1.metric(
    "Angel One",
    st.session_state.login_status,
)

top2.metric(
    "Engine",
    (
        "🟢 RUNNING"
        if st.session_state.running
        else "🔴 STOPPED"
    ),
)

top3.metric(
    "Market",
    (
        "OPEN"
        if market_is_open()
        else "CLOSED"
    ),
)

top4.metric(
    "NIFTY LIVE",
    (
        f"₹{st.session_state.spot:,.2f}"
        if st.session_state.spot
        is not None
        else "-"
    ),
)


# ============================================================
# SUPERTREND STATUS
# ============================================================

st.subheader(
    "Supertrend Status"
)

a, b, c = st.columns(3)

a.metric(
    "5 Minute",
    direction_text(
        st.session_state.st5
    ),
)

b.metric(
    "15 Minute",
    direction_text(
        st.session_state.st15
    ),
)

c.metric(
    "4 Hour",
    direction_text(
        st.session_state.st4h
    ),
)


# ============================================================
# SUPERTREND VALUES
# ============================================================

v1, v2, v3 = st.columns(3)

v1.metric(
    "5m Supertrend",
    (
        f"{st.session_state.st5_value:.2f}"
        if st.session_state.st5_value
        is not None
        else "-"
    ),
)

v2.metric(
    "15m Supertrend",
    (
        f"{st.session_state.st15_value:.2f}"
        if st.session_state.st15_value
        is not None
        else "-"
    ),
)

v3.metric(
    "4h Supertrend",
    (
        f"{st.session_state.st4h_value:.2f}"
        if st.session_state.st4h_value
        is not None
        else "-"
    ),
)


# ============================================================
# SIGNAL
# ============================================================

st.subheader(
    "Signal"
)

signal = st.session_state.signal

if signal == "BUY CE":

    st.success(
        "🟢 BUY ATM CE"
    )

elif signal == "BUY PE":

    st.error(
        "🔴 BUY ATM PE"
    )

else:

    st.info(
        "⚪ WAIT"
    )

st.write(
    "Signal candle:",
    st.session_state.signal_time
    or "-",
)


# ============================================================
# OPTION
# ============================================================

st.subheader(
    "Selected Option"
)

o1, o2, o3, o4, o5 = (
    st.columns(5)
)

o1.metric(
    "Symbol",
    st.session_state.option_symbol
    or "-",
)

o2.metric(
    "Strike",
    (
        st.session_state.option_strike
        if st.session_state.option_strike
        is not None
        else "-"
    ),
)

o3.metric(
    "Expiry",
    st.session_state.option_expiry
    or "-",
)

o4.metric(
    "Option LTP",
    (
        f"₹{st.session_state.option_ltp:.2f}"
        if st.session_state.option_ltp
        is not None
        else "-"
    ),
)

quantity = (
    (
        st.session_state.option_lot_size
        or 0
    )
    * LOTS
)

o5.metric(
    "Quantity",
    quantity or "-",
)


# ============================================================
# RUNTIME
# ============================================================

st.subheader(
    "Runtime"
)

r1, r2, r3 = (
    st.columns(3)
)

r1.metric(
    "Order ID",
    st.session_state.last_order_id
    or "-",
)

r2.metric(
    "Mode",
    (
        "LIVE"
        if LIVE_TRADING
        else "DRY RUN"
    ),
)

r3.metric(
    "Last Update",
    st.session_state.last_update
    or "-",
)


# ============================================================
# MESSAGES
# ============================================================

if st.session_state.last_message:

    st.info(
        st.session_state.last_message
    )


if st.session_state.last_error:

    st.error(
        st.session_state.last_error
    )


# ============================================================
# CANDLE TABLE
# ============================================================

if st.session_state.candles is not None:

    st.subheader(
        "NIFTY 5-Minute Candles"
    )

    st.dataframe(
        st.session_state.candles,
        use_container_width=True,
        hide_index=True,
    )


# ============================================================
# AUTO LIVE ENGINE
# ============================================================

run_every = (
    REFRESH_SECONDS
    if st.session_state.running
    else None
)


@st.fragment(
    run_every=run_every
)
def live_engine():

    if not st.session_state.running:

        st.caption(
            "Live engine stopped."
        )

        return

    if not market_is_open():

        st.info(
            "Market closed — "
            "no order evaluation."
        )

        return

    try:

        run_live_cycle()

        st.success(
            f"LIVE NIFTY: "
            f"₹{st.session_state.spot:,.2f} | "
            f"5m {direction_text(st.session_state.st5)} | "
            f"15m {direction_text(st.session_state.st15)} | "
            f"4h {direction_text(st.session_state.st4h)}"
        )

    except Exception as exc:

        st.session_state.last_error = (
            f"{type(exc).__name__}: {exc}"
        )

        st.error(
            st.session_state.last_error
        )


# ============================================================
# RUN ENGINE
# ============================================================

live_engine()

# ============================================================
# SAFETY NOTE
# ============================================================

st.caption(
    "Supertrend = 20,2 | "
    "Bullish 5m flip + 15m GREEN + 4h GREEN = BUY ATM CE | "
    "Bearish 5m flip + 15m RED + 4h RED = BUY ATM PE | "
    "LIVE_TRADING=False: real orders disabled."
)

if st.button("🚀 START TRADING", type="primary"):
    st.session_state.running = True
    st.switch_page("pages/2_Trading.py")
    st.rerun()
