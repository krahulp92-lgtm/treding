import os
import json
import time
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
# NIFTY LIVE SUPERTREND 20,2 + ANGEL ONE ORDER BOOK
# ============================================================
# IMPORTANT
# LIVE_TRADING = False -> no real broker order is sent.
# LIVE_TRADING = True  -> BUY buttons send REAL Angel One orders.
# AUTO_TRADE = False    -> Supertrend does NOT automatically place orders.
#
# Strategy:
#   5m Supertrend 20,2 flip + 15m confirmation + 4h confirmation
#   BUY CE on bullish flip, BUY PE on bearish flip.
#   Manual BUY CE / BUY PE buttons are always available.
# ============================================================

# ============================================================
# SETTINGS
# ============================================================
IST = ZoneInfo("Asia/Kolkata")
ST_PERIOD = 20
ST_MULTIPLIER = 2.0

LIVE_TRADING = False          # CHANGE TO TRUE ONLY AFTER VERIFYING EVERYTHING
AUTO_TRADE = False            # Keep False while testing
LOTS = 1
ORDER_TYPE = "MARKET"
PRODUCT_TYPE = "CARRYFORWARD"  # Normal F&O (NRML)
REFRESH_SECONDS = 20
ORDERBOOK_RETRIES = 4
ORDERBOOK_RETRY_DELAY = 1.0

BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "nifty_live_state.json"
INSTRUMENT_FILE = BASE_DIR / "OpenAPIScripMaster.json"
INSTRUMENT_MASTER_URL = (
    "https://margincalculator.angelone.in/"
    "OpenAPI_File/files/OpenAPIScripMaster.json"
)
KNOWN_NIFTY_TOKEN = "99926000"

ANGEL_API_KEY = os.getenv("ANGEL_API_KEY", "").strip()
ANGEL_CLIENT_ID = os.getenv("ANGEL_CLIENT_ID", "").strip()
ANGEL_PASSWORD = os.getenv("ANGEL_PASSWORD", "").strip()
ANGEL_TOTP_SECRET = os.getenv("ANGEL_TOTP_SECRET", "").strip()

st.set_page_config(
    page_title="NIFTY Live Supertrend 20,2",
    page_icon="📈",
    layout="wide",
)

# ============================================================
# SESSION STATE
# ============================================================
defaults = {
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
    "signal": "WAIT",
    "signal_time": None,
    "ce_option": None,
    "pe_option": None,
    "option_symbol": None,
    "option_token": None,
    "option_expiry": None,
    "option_strike": None,
    "option_lot_size": None,
    "option_ltp": None,
    "last_order_id": None,
    "last_unique_order_id": None,
    "last_order_time": None,
    "last_update": None,
    "active_page": "Dashboard",
    "orderbook_data": [],
    "orderbook_response": None,
    "order_response": None,
    "order_payload": None,
    "paper_orders": [],
    "highlight_order_id": None,
    "trade_lock": False,
    "candles": None,
}
for key, value in defaults.items():
    if key not in st.session_state:
        st.session_state[key] = value

# ============================================================
# HELPERS
# ============================================================
def now_ist():
    return datetime.now(IST)


def clear_error():
    st.session_state.last_error = ""


def credentials_ok():
    missing = []
    for name, value in {
        "ANGEL_API_KEY": ANGEL_API_KEY,
        "ANGEL_CLIENT_ID": ANGEL_CLIENT_ID,
        "ANGEL_PASSWORD": ANGEL_PASSWORD,
        "ANGEL_TOTP_SECRET": ANGEL_TOTP_SECRET,
    }.items():
        if not value:
            missing.append(name)

    if missing:
        raise ValueError("Missing environment variables: " + ", ".join(missing))

    secret = get_totp_secret(ANGEL_TOTP_SECRET)
    if not secret:
        raise ValueError("ANGEL_TOTP_SECRET is empty")

    try:
        pyotp.TOTP(secret).now()
    except Exception as exc:
        raise ValueError(f"Invalid ANGEL_TOTP_SECRET: {exc}") from exc


def get_totp_secret(raw):
    if not raw:
        return ""

    value = raw.strip()
    if value.lower().startswith("otpauth://"):
        parsed = urlparse(value)
        secret_values = parse_qs(parsed.query).get("secret", [])
        if not secret_values:
            raise ValueError("No secret= found in otpauth URI")
        value = secret_values[0]

    return (
        value.replace(" ", "")
        .replace("\t", "")
        .replace("\r", "")
        .replace("\n", "")
        .upper()
        .strip()
    )


def generate_totp():
    secret = get_totp_secret(ANGEL_TOTP_SECRET)
    if not secret:
        raise ValueError("ANGEL_TOTP_SECRET is empty")
    return pyotp.TOTP(secret).now()


# ============================================================
# ANGEL ONE LOGIN
# ============================================================
def angel_login():
    credentials_ok()

    api = SmartConnect(api_key=ANGEL_API_KEY)
    response = api.generateSession(
        ANGEL_CLIENT_ID,
        ANGEL_PASSWORD,
        generate_totp(),
    )

    if not response or response.get("status") is not True:
        message = response.get("message") if response else "No response"
        errorcode = response.get("errorcode") if response else ""
        raise RuntimeError(
            f"Angel login failed | message={message} | errorcode={errorcode}"
        )

    st.session_state.api = api
    st.session_state.login_status = "CONNECTED"
    st.session_state.last_error = ""
    return api


def ensure_login():
    api = st.session_state.api
    if api is None:
        api = angel_login()
    return api


# ============================================================
# INSTRUMENT MASTER
# ============================================================
def download_instrument_master(force=False):
    if INSTRUMENT_FILE.exists() and not force:
        age = datetime.now().timestamp() - INSTRUMENT_FILE.stat().st_mtime
        if age < 24 * 60 * 60:
            try:
                cached = json.loads(INSTRUMENT_FILE.read_text(encoding="utf-8"))
                if isinstance(cached, list) and cached:
                    return cached
            except Exception:
                pass

    response = requests.get(
        INSTRUMENT_MASTER_URL,
        timeout=120,
        headers={"User-Agent": "Mozilla/5.0"},
    )
    response.raise_for_status()
    data = response.json()

    if not isinstance(data, list) or not data:
        raise ValueError("Instrument master is empty/invalid")

    INSTRUMENT_FILE.write_text(
        json.dumps(data),
        encoding="utf-8",
    )
    return data


def parse_expiry(value):
    text = str(value or "").strip().upper()
    for fmt in ("%d%b%Y", "%d-%b-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    return None


def normalized_strike(value):
    try:
        strike = float(value)
    except (TypeError, ValueError):
        return None

    # Angel One instrument master commonly stores option strike x100.
    if strike > 100000:
        strike /= 100.0
    return strike


def select_atm_nifty_option(instruments, spot, option_type):
    if not instruments or spot is None:
        return None

    today = now_ist().date()
    option_type = option_type.upper().strip()
    candidates = []

    for item in instruments:
        exchange = str(item.get("exch_seg", "")).upper().strip()
        inst_type = str(item.get("instrumenttype", "")).upper().strip()
        name = str(item.get("name", "")).upper().strip()
        symbol = str(item.get("symbol", "")).upper().strip()

        if exchange != "NFO":
            continue
        if inst_type != "OPTIDX":
            continue
        if name not in ("NIFTY", "NIFTY 50"):
            continue
        if not symbol.endswith(option_type):
            continue

        token = str(item.get("token", "")).strip()
        if not token:
            continue

        expiry = parse_expiry(item.get("expiry"))
        if expiry is None or expiry < today:
            continue

        strike = normalized_strike(item.get("strike"))
        if strike is None or strike <= 0:
            continue

        try:
            lot_size = int(float(item.get("lotsize", 0)))
        except (TypeError, ValueError):
            lot_size = 0

        if lot_size <= 0:
            continue

        candidates.append(
            {
                "symbol": str(item.get("symbol")),
                "token": token,
                "expiry": expiry,
                "strike": strike,
                "lot_size": lot_size,
            }
        )

    if not candidates:
        return None

    nearest_expiry = min(item["expiry"] for item in candidates)
    same_expiry = [
        item for item in candidates
        if item["expiry"] == nearest_expiry
    ]

    return min(
        same_expiry,
        key=lambda item: abs(item["strike"] - float(spot)),
    )


# ============================================================
# MARKET DATA
# ============================================================
def get_ltp(exchange, symbol, token):
    api = ensure_login()

    response = api.ltpData(
        exchange,
        symbol,
        str(token),
    )

    if not response or response.get("status") is not True:
        message = response.get("message") if response else "No response"
        errorcode = response.get("errorcode") if response else ""
        raise RuntimeError(
            f"LTP failed | symbol={symbol} | token={token} | "
            f"message={message} | errorcode={errorcode}"
        )

    data = response.get("data") or {}
    value = data.get("ltp")

    if value is None:
        raise RuntimeError(f"LTP missing for {symbol} ({token})")

    return float(value)


def get_nifty_5m_candles(days=10):
    api = ensure_login()
    now = now_ist()
    start = now - timedelta(days=days)

    params = {
        "exchange": "NSE",
        "symboltoken": KNOWN_NIFTY_TOKEN,
        "interval": "FIVE_MINUTE",
        "fromdate": start.strftime("%Y-%m-%d %H:%M"),
        "todate": now.strftime("%Y-%m-%d %H:%M"),
    }

    response = api.getCandleData(params)

    if not response or response.get("status") is not True:
        message = response.get("message") if response else "No response"
        errorcode = response.get("errorcode") if response else ""
        raise RuntimeError(
            f"Candle API failed | message={message} | errorcode={errorcode}"
        )

    rows = response.get("data") or []
    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(
        rows,
        columns=["timestamp", "open", "high", "low", "close", "volume"],
    )

    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")

    for column in ["open", "high", "low", "close", "volume"]:
        df[column] = pd.to_numeric(df[column], errors="coerce")

    df = (
        df.dropna(subset=["timestamp", "open", "high", "low", "close"])
        .set_index("timestamp")
        .sort_index()
    )

    if df.index.tz is None:
        df.index = df.index.tz_localize(IST)
    else:
        df.index = df.index.tz_convert(IST)

    # Angel One candle timestamps are start-times.
    cutoff = pd.Timestamp(now) - pd.Timedelta(minutes=5)
    df = df[df.index <= cutoff]

    return df


# ============================================================
# SUPERTREND 20,2
# ============================================================
def supertrend(df, period=20, multiplier=2.0):
    df = df.copy()

    if df.empty or len(df) < period + 2:
        return df

    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)
    previous_close = close.shift(1)

    tr = pd.concat(
        [
            high - low,
            (high - previous_close).abs(),
            (low - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)

    atr = tr.ewm(
        alpha=1 / period,
        adjust=False,
        min_periods=period,
    ).mean()

    hl2 = (high + low) / 2.0
    upper_basic = hl2 + multiplier * atr
    lower_basic = hl2 - multiplier * atr

    upper = pd.Series(np.nan, index=df.index, dtype=float)
    lower = pd.Series(np.nan, index=df.index, dtype=float)
    direction = pd.Series(0, index=df.index, dtype=int)
    st_line = pd.Series(np.nan, index=df.index, dtype=float)

    for i in range(len(df)):
        if pd.isna(upper_basic.iloc[i]) or pd.isna(lower_basic.iloc[i]):
            continue

        if i == 0:
            upper.iloc[i] = upper_basic.iloc[i]
            lower.iloc[i] = lower_basic.iloc[i]
            continue

        previous_upper = upper.iloc[i - 1]
        previous_lower = lower.iloc[i - 1]

        if (
            pd.isna(previous_upper)
            or upper_basic.iloc[i] < previous_upper
            or close.iloc[i - 1] > previous_upper
        ):
            upper.iloc[i] = upper_basic.iloc[i]
        else:
            upper.iloc[i] = previous_upper

        if (
            pd.isna(previous_lower)
            or lower_basic.iloc[i] > previous_lower
            or close.iloc[i - 1] < previous_lower
        ):
            lower.iloc[i] = lower_basic.iloc[i]
        else:
            lower.iloc[i] = previous_lower

        previous_direction = int(direction.iloc[i - 1])

        if previous_direction == 0:
            if close.iloc[i] >= lower.iloc[i]:
                direction.iloc[i] = 1
                st_line.iloc[i] = lower.iloc[i]
            else:
                direction.iloc[i] = -1
                st_line.iloc[i] = upper.iloc[i]
        elif previous_direction == 1:
            if close.iloc[i] < lower.iloc[i]:
                direction.iloc[i] = -1
                st_line.iloc[i] = upper.iloc[i]
            else:
                direction.iloc[i] = 1
                st_line.iloc[i] = lower.iloc[i]
        else:
            if close.iloc[i] > upper.iloc[i]:
                direction.iloc[i] = 1
                st_line.iloc[i] = lower.iloc[i]
            else:
                direction.iloc[i] = -1
                st_line.iloc[i] = upper.iloc[i]

    df["ATR"] = atr
    df["Final_Upper"] = upper
    df["Final_Lower"] = lower
    df["Supertrend"] = st_line
    df["ST_DIRECTION"] = direction
    df["ST_GREEN"] = direction.eq(1)
    df["ST_RED"] = direction.eq(-1)
    df["ST_FLIP_GREEN"] = direction.eq(1) & direction.shift(1).eq(-1)
    df["ST_FLIP_RED"] = direction.eq(-1) & direction.shift(1).eq(1)

    return df


def resample_ohlcv(df, rule):
    if df.empty:
        return pd.DataFrame()

    out = df.resample(
        rule,
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

    out = out.dropna(subset=["open", "high", "low", "close"])
    out = out[out.index <= pd.Timestamp(now_ist())]
    return out


def build_timeframes(df5):
    st5 = supertrend(df5, ST_PERIOD, ST_MULTIPLIER)
    st15 = supertrend(
        resample_ohlcv(df5, "15min"),
        ST_PERIOD,
        ST_MULTIPLIER,
    )
    st4h = supertrend(
        resample_ohlcv(df5, "4h"),
        ST_PERIOD,
        ST_MULTIPLIER,
    )
    return st5, st15, st4h


def current_signal(df5, df15, df4h):
    v5 = df5.dropna(subset=["Supertrend"])
    v15 = df15.dropna(subset=["Supertrend"])
    v4h = df4h.dropna(subset=["Supertrend"])

    if len(v5) < 2 or v15.empty or v4h.empty:
        return None

    previous5 = v5.iloc[-2]
    last5 = v5.iloc[-1]
    last15 = v15.iloc[-1]
    last4h = v4h.iloc[-1]

    dprev = int(previous5["ST_DIRECTION"])
    d5 = int(last5["ST_DIRECTION"])
    d15 = int(last15["ST_DIRECTION"])
    d4h = int(last4h["ST_DIRECTION"])

    flip_green = dprev == -1 and d5 == 1
    flip_red = dprev == 1 and d5 == -1

    bullish = flip_green and d15 == 1 and d4h == 1
    bearish = flip_red and d15 == -1 and d4h == -1

    if bullish:
        action = "BUY CE"
        option_type = "CE"
    elif bearish:
        action = "BUY PE"
        option_type = "PE"
    else:
        action = "WAIT"
        option_type = None

    return {
        "time": v5.index[-1],
        "spot": float(last5["close"]),
        "5m": d5,
        "15m": d15,
        "4h": d4h,
        "action": action,
        "option_type": option_type,
        "flip_green": bool(flip_green),
        "flip_red": bool(flip_red),
    }


# ============================================================
# LOCAL STATE
# ============================================================
def load_state():
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(state):
    STATE_FILE.write_text(
        json.dumps(state, indent=2, default=str),
        encoding="utf-8",
    )


# ============================================================
# ORDER BOOK
# ============================================================
def fetch_order_book():
    api = ensure_login()

    response = api.orderBook()
    st.session_state.orderbook_response = response

    if not isinstance(response, dict):
        raise RuntimeError(
            f"Unexpected Order Book response: {response!r}"
        )

    if response.get("status") is not True:
        raise RuntimeError(
            "Order Book failed | "
            f"message={response.get('message')} | "
            f"errorcode={response.get('errorcode')} | "
            f"response={response}"
        )

    data = response.get("data") or []
    st.session_state.orderbook_data = data
    return data


def refresh_order_book_with_retry(expected_order_id=None):
    last_data = []

    for attempt in range(1, ORDERBOOK_RETRIES + 1):
        try:
            last_data = fetch_order_book()
        except Exception:
            last_data = []

        if expected_order_id:
            found = any(
                str(row.get("orderid", "")) == str(expected_order_id)
                for row in last_data
                if isinstance(row, dict)
            )
            if found:
                return last_data

        if attempt < ORDERBOOK_RETRIES:
            time.sleep(ORDERBOOK_RETRY_DELAY)

    return last_data


def order_status_by_id(order_id):
    if not order_id:
        return None

    data = st.session_state.get("orderbook_data") or []
    for row in data:
        if str(row.get("orderid", "")) == str(order_id):
            return row
    return None


def show_raw_order_debug():
    st.subheader("🔎 Order Debug")

    if st.session_state.get("order_payload"):
        with st.expander("Order payload sent"):
            st.json(st.session_state.order_payload)

    if st.session_state.get("order_response") is not None:
        with st.expander("Place Order response"):
            st.json(st.session_state.order_response)

    if st.session_state.get("orderbook_response") is not None:
        with st.expander("Order Book response"):
            st.json(st.session_state.orderbook_response)


# ============================================================
# DUPLICATE PROTECTION
# ============================================================
def same_day_ist(value):
    if value is None:
        return None

    text = str(value).strip()
    for fmt in (
        "%d-%b-%Y %H:%M:%S",
        "%d-%b-%Y %H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S",
    ):
        try:
            parsed = datetime.strptime(text, fmt)
            return parsed.date()
        except ValueError:
            pass

    try:
        parsed = pd.to_datetime(text, errors="coerce")
        if pd.isna(parsed):
            return None
        if getattr(parsed, "tzinfo", None) is not None:
            parsed = parsed.tz_convert(IST)
        return parsed.date()
    except Exception:
        return None


def duplicate_order_exists(symbol, transaction_type="BUY"):
    today = now_ist().date()

    # Broker order book check.
    try:
        rows = refresh_order_book_with_retry()
    except Exception:
        rows = []

    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("tradingsymbol", "")).upper() != str(symbol).upper():
            continue
        if str(row.get("transactiontype", "")).upper() != transaction_type.upper():
            continue

        date_value = same_day_ist(row.get("updatetime"))
        if date_value == today:
            return True, f"Existing broker order found: {row.get('orderid')}"

    # Local state / paper order check.
    for row in st.session_state.get("paper_orders", []):
        if str(row.get("tradingsymbol", "")).upper() == str(symbol).upper():
            if str(row.get("transactiontype", "")).upper() == transaction_type.upper():
                return True, f"Existing local order found: {row.get('orderid')}"

    state = load_state()
    previous_symbol = str(state.get("last_option", "")).upper()
    previous_time = same_day_ist(state.get("last_updated"))
    previous_action = str(state.get("last_action", "")).upper()

    if (
        previous_symbol == str(symbol).upper()
        and previous_time == today
        and previous_action.startswith("BUY")
    ):
        return True, f"Local duplicate protection: {state.get('last_order_id', 'previous order')}"

    return False, ""


# ============================================================
# REAL / PAPER ORDER
# ============================================================
def make_order_params(option):
    quantity = int(option["lot_size"]) * int(LOTS)

    if quantity <= 0:
        raise ValueError(f"Invalid quantity: {quantity}")

    return {
        "variety": "NORMAL",
        "tradingsymbol": str(option["symbol"]),
        "symboltoken": str(option["token"]),
        "transactiontype": "BUY",
        "exchange": "NFO",
        "ordertype": ORDER_TYPE,
        "producttype": PRODUCT_TYPE,
        "duration": "DAY",
        "price": "0",
        "squareoff": "0",
        "stoploss": "0",
        "quantity": str(quantity),
        "ordertag": "NIFTYST202",
    }


def create_paper_order(option, params):
    order_id = f"PAPER-{now_ist().strftime('%Y%m%d%H%M%S%f')}"

    row = {
        "variety": "NORMAL",
        "ordertype": params["ordertype"],
        "producttype": params["producttype"],
        "duration": "DAY",
        "price": "0",
        "triggerprice": "0",
        "quantity": params["quantity"],
        "disclosedquantity": "0",
        "tradingsymbol": params["tradingsymbol"],
        "transactiontype": "BUY",
        "exchange": "NFO",
        "symboltoken": params["symboltoken"],
        "averageprice": str(st.session_state.get("option_ltp") or 0),
        "filledshares": params["quantity"],
        "unfilledshares": "0",
        "status": "PAPER",
        "orderstatus": "PAPER",
        "updatetime": now_ist().strftime("%d-%b-%Y %H:%M:%S"),
        "orderid": order_id,
        "uniqueorderid": order_id,
        "text": "DRY RUN - no broker order sent",
    }

    st.session_state.paper_orders.insert(0, row)
    return {
        "status": True,
        "message": "PAPER ORDER CREATED",
        "errorcode": "",
        "data": {
            "orderid": order_id,
            "uniqueorderid": order_id,
        },
    }


def place_buy_order(option):
    if option is None:
        raise RuntimeError("Option contract is not available")

    params = make_order_params(option)
    st.session_state.order_payload = params
    st.session_state.order_response = None

    if not LIVE_TRADING:
        return create_paper_order(option, params)

    api = ensure_login()

    try:
        if hasattr(api, "placeOrderFullResponse"):
            response = api.placeOrderFullResponse(params)
        else:
            order_id = api.placeOrder(params)
            response = {
                "status": bool(order_id),
                "message": "SUCCESS" if order_id else "FAILED",
                "errorcode": "",
                "data": {
                    "orderid": order_id,
                } if order_id else None,
            }
    except Exception as exc:
        raise RuntimeError(
            f"Angel One placeOrder exception | "
            f"{type(exc).__name__}: {exc}"
        ) from exc

    st.session_state.order_response = response

    if not isinstance(response, dict):
        raise RuntimeError(
            f"Unexpected placeOrder response: {response!r}"
        )

    if response.get("status") is not True:
        raise RuntimeError(
            "ORDER REJECTED BY ANGEL ONE | "
            f"message={response.get('message')} | "
            f"errorcode={response.get('errorcode')} | "
            f"response={response}"
        )

    data = response.get("data") or {}
    order_id = data.get("orderid")
    unique_order_id = data.get("uniqueorderid")

    if not order_id and not unique_order_id:
        raise RuntimeError(
            f"Angel One returned SUCCESS but no order ID: {response}"
        )

    return response


def execute_manual_buy(option, option_label):
    if st.session_state.trade_lock:
        st.warning("Another order operation is already running.")
        return

    st.session_state.trade_lock = True
    clear_error()

    try:
        if option is None:
            raise RuntimeError(f"{option_label}: option contract not available")

        if st.session_state.login_status != "CONNECTED":
            ensure_login()

        # Refresh LTP right before sending the order.
        current_ltp = get_ltp(
            "NFO",
            option["symbol"],
            option["token"],
        )
        st.session_state.option_ltp = current_ltp

        duplicate, message = duplicate_order_exists(
            option["symbol"],
            "BUY",
        )
        if duplicate:
            raise RuntimeError(
                f"DUPLICATE ORDER BLOCKED | {message}"
            )

        response = place_buy_order(option)
        data = response.get("data") or {}

        order_id = data.get("orderid")
        unique_order_id = data.get("uniqueorderid") or order_id

        st.session_state.last_order_id = order_id or unique_order_id
        st.session_state.last_unique_order_id = unique_order_id
        st.session_state.last_order_time = now_ist().strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        st.session_state.highlight_order_id = (
            order_id or unique_order_id
        )

        # Persist last order.
        state = load_state()
        state.update(
            {
                "last_signal": f"MANUAL|{option_label}|{now_ist().isoformat()}",
                "last_signal_candle": None,
                "last_action": option_label,
                "last_option": option["symbol"],
                "last_order_id": order_id or unique_order_id,
                "last_updated": now_ist().isoformat(),
            }
        )
        save_state(state)

        # Broker accepts first, then Order Book is fetched.
        order_rows = refresh_order_book_with_retry(
            expected_order_id=order_id
        ) if LIVE_TRADING else st.session_state.paper_orders

        st.session_state.orderbook_data = order_rows
        st.session_state.active_page = "Order Book"

        status_row = order_status_by_id(order_id) if LIVE_TRADING else next(
            (
                row for row in order_rows
                if str(row.get("orderid")) == str(order_id)
            ),
            None,
        )

        mode_text = "LIVE" if LIVE_TRADING else "PAPER"
        st.session_state.last_message = (
            f"{mode_text} BUY accepted: {option_label} | "
            f"{option['symbol']} | Qty={option['lot_size'] * LOTS} | "
            f"Order ID={order_id or unique_order_id}"
        )

        if LIVE_TRADING and order_id and status_row is None:
            st.session_state.last_message += (
                " | Broker accepted the request, but the new row was not "
                "visible in Order Book yet. Use Refresh Order Book."
            )

        st.rerun()

    except Exception as exc:
        st.session_state.last_error = str(exc)
        st.session_state.last_message = "BUY order was NOT confirmed"
        st.session_state.active_page = "Order Book"

    finally:
        st.session_state.trade_lock = False


# ============================================================
# LIVE ENGINE
# ============================================================
def update_selected_options():
    instruments = st.session_state.instruments
    if not instruments or st.session_state.spot is None:
        return

    ce = select_atm_nifty_option(
        instruments,
        st.session_state.spot,
        "CE",
    )
    pe = select_atm_nifty_option(
        instruments,
        st.session_state.spot,
        "PE",
    )

    st.session_state.ce_option = ce
    st.session_state.pe_option = pe

    # Default selected option follows signal if available.
    if st.session_state.signal == "BUY CE" and ce:
        selected = ce
    elif st.session_state.signal == "BUY PE" and pe:
        selected = pe
    elif ce:
        selected = ce
    else:
        selected = pe

    if selected:
        st.session_state.option_symbol = selected["symbol"]
        st.session_state.option_token = selected["token"]
        st.session_state.option_expiry = str(selected["expiry"])
        st.session_state.option_strike = selected["strike"]
        st.session_state.option_lot_size = selected["lot_size"]

        try:
            st.session_state.option_ltp = get_ltp(
                "NFO",
                selected["symbol"],
                selected["token"],
            )
        except Exception:
            pass


def run_live_cycle():
    clear_error()

    if st.session_state.instruments is None:
        st.session_state.instruments = download_instrument_master()

    df5raw = get_nifty_5m_candles(days=10)
    if df5raw.empty:
        raise RuntimeError("No NIFTY 5-minute candle data received")

    st.session_state.candles = df5raw.tail(100).reset_index()

    st5, st15, st4h = build_timeframes(df5raw)
    sig = current_signal(st5, st15, st4h)

    if sig is None:
        raise RuntimeError("Not enough data to calculate Supertrend values")

    st.session_state.spot = sig["spot"]
    st.session_state.st5 = sig["5m"]
    st.session_state.st15 = sig["15m"]
    st.session_state.st4h = sig["4h"]
    st.session_state.signal = sig["action"]
    st.session_state.signal_time = str(sig["time"])
    st.session_state.last_update = now_ist().strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    update_selected_options()

    # Automatic orders are OFF by default.
    if not AUTO_TRADE or sig["option_type"] is None:
        if sig["option_type"] is None:
            st.session_state.last_message = (
                "Data updated. No fresh 5m Supertrend flip."
            )
        else:
            st.session_state.last_message = (
                f"Signal detected: {sig['action']}. "
                "AUTO_TRADE is OFF; use the manual BUY button."
            )
        return

    state = load_state()
    signal_key = f"{sig['time'].isoformat()}|{sig['action']}"

    if state.get("last_signal") == signal_key:
        st.session_state.last_message = (
            "Signal already processed; automatic duplicate blocked."
        )
        return

    option = (
        st.session_state.ce_option
        if sig["option_type"] == "CE"
        else st.session_state.pe_option
    )

    if option is None:
        raise RuntimeError(
            f"ATM NIFTY {sig['option_type']} not found"
        )

    execute_manual_buy(option, sig["action"])


# ============================================================
# UI HELPERS
# ============================================================
def direction_text(value):
    if value == 1:
        return "🟢 GREEN"
    if value == -1:
        return "🔴 RED"
    return "-"


def option_card(option, title, button_key):
    if not option:
        st.warning(f"{title} contract not available")
        return

    try:
        live_ltp = get_ltp(
            "NFO",
            option["symbol"],
            option["token"],
        )
    except Exception:
        live_ltp = None

    st.markdown(f"### {title}")

    c1, c2, c3 = st.columns(3)
    c1.metric("Strike", f"{option['strike']:g}")
    c2.metric("Lot Size", option["lot_size"])
    c3.metric(
        "LTP",
        f"₹{live_ltp:.2f}" if live_ltp is not None else "-",
    )

    st.write(f"**Symbol:** `{option['symbol']}`")
    st.write(f"**Token:** `{option['token']}`")
    st.write(f"**Expiry:** `{option['expiry']}`")
    st.write(f"**Quantity:** `{option['lot_size'] * LOTS}`")

    if title == "ATM CE":
        clicked = st.button(
            "🟢 BUY CE",
            key=button_key,
            type="primary",
            use_container_width=True,
        )
    else:
        clicked = st.button(
            "🔴 BUY PE",
            key=button_key,
            type="primary",
            use_container_width=True,
        )

    if clicked:
        execute_manual_buy(
            option,
            "BUY CE" if title == "ATM CE" else "BUY PE",
        )


def show_dashboard():
    st.title("📈 NIFTY LIVE SUPERTREND 20,2")
    st.caption(
        "5m flip + 15m confirmation + 4h confirmation | "
        "ATM CE/PE | Manual BUY + Angel One Order Book"
    )

    if st.session_state.last_message:
        st.info(st.session_state.last_message)
    if st.session_state.last_error:
        st.error(st.session_state.last_error)

    top1, top2, top3, top4 = st.columns(4)
    top1.metric("Angel One", st.session_state.login_status)
    top2.metric(
        "Engine",
        "RUNNING" if st.session_state.running else "STOPPED",
    )
    top3.metric(
        "Market",
        "OPEN" if market_is_open() else "CLOSED",
    )
    top4.metric(
        "NIFTY Spot",
        (
            f"₹{st.session_state.spot:,.2f}"
            if st.session_state.spot is not None
            else "-"
        ),
    )

    st.subheader("Supertrend Status")
    a, b, c = st.columns(3)
    a.metric("5 Minute", direction_text(st.session_state.st5))
    b.metric("15 Minute", direction_text(st.session_state.st15))
    c.metric("4 Hour", direction_text(st.session_state.st4h))

    st.subheader("Strategy Signal")
    signal = st.session_state.signal
    if signal == "BUY CE":
        st.success("🟢 BUY ATM CE signal")
    elif signal == "BUY PE":
        st.error("🔴 BUY ATM PE signal")
    else:
        st.info("⚪ WAIT")

    st.write("Signal candle:", st.session_state.signal_time or "-")

    st.divider()
    st.subheader("🛒 Manual Buy Orders")
    st.caption(
        "Buttons stay available regardless of WAIT/BUY signal. "
        "Orders are quantity = lot size × LOTS."
    )

    left, right = st.columns(2)
    with left:
        option_card(
            st.session_state.ce_option,
            "ATM CE",
            "manual_buy_ce",
        )
    with right:
        option_card(
            st.session_state.pe_option,
            "ATM PE",
            "manual_buy_pe",
        )

    st.divider()
    st.subheader("Selected Option")
    s1, s2, s3, s4, s5 = st.columns(5)
    s1.metric("Symbol", st.session_state.option_symbol or "-")
    s2.metric("Strike", st.session_state.option_strike or "-")
    s3.metric("Expiry", st.session_state.option_expiry or "-")
    s4.metric(
        "LTP",
        (
            f"₹{st.session_state.option_ltp:.2f}"
            if st.session_state.option_ltp is not None
            else "-"
        ),
    )
    quantity = (st.session_state.option_lot_size or 0) * LOTS
    s5.metric("Quantity", quantity or "-")

    st.divider()
    st.subheader("Order / Runtime")
    r1, r2, r3, r4 = st.columns(4)
    r1.metric("Last Order ID", st.session_state.last_order_id or "-")
    r2.metric("Mode", "LIVE" if LIVE_TRADING else "PAPER")
    r3.metric("Last Order Time", st.session_state.last_order_time or "-")
    r4.metric("Last Update", st.session_state.last_update or "-")

    st.warning(
        "PAPER mode creates a local test order only. It will NOT appear in "
        "Angel One's real Order Book. To send a real BUY, set "
        "LIVE_TRADING = True in the code."
        if not LIVE_TRADING
        else
        "LIVE TRADING is ON. BUY buttons send real Angel One orders."
    )


def show_order_book():
    st.title("📋 Angel One Order Book")

    if st.button("🔄 Refresh Order Book", use_container_width=True):
        try:
            if LIVE_TRADING:
                fetch_order_book()
            else:
                st.session_state.orderbook_data = st.session_state.paper_orders
            st.session_state.last_message = "Order Book refreshed"
            st.session_state.last_error = ""
        except Exception as exc:
            st.session_state.last_error = str(exc)
        st.rerun()

    try:
        if LIVE_TRADING:
            rows = fetch_order_book()
        else:
            rows = st.session_state.paper_orders
    except Exception as exc:
        st.error(str(exc))
        rows = st.session_state.paper_orders if not LIVE_TRADING else []

    if not rows:
        st.info(
            "No orders returned. Click BUY CE/BUY PE first. "
            "In LIVE mode the broker Order Book must contain the order after "
            "successful placement."
        )
        show_raw_order_debug()
        return

    df = pd.DataFrame(rows)

    new_id = st.session_state.highlight_order_id or st.session_state.last_order_id
    if new_id and "orderid" in df.columns:
        df["__new"] = df["orderid"].astype(str).eq(str(new_id))
        df = pd.concat(
            [df[df["__new"]], df[~df["__new"]]],
            ignore_index=True,
        )

        if df["__new"].any():
            st.success(f"⭐ Latest order: {new_id}")

    preferred = [
        "__new",
        "orderid",
        "uniqueorderid",
        "tradingsymbol",
        "symboltoken",
        "transactiontype",
        "exchange",
        "ordertype",
        "producttype",
        "quantity",
        "filledshares",
        "unfilledshares",
        "price",
        "averageprice",
        "status",
        "orderstatus",
        "text",
        "updatetime",
    ]

    columns = [column for column in preferred if column in df.columns]
    df = df[columns]

    st.dataframe(
        df,
        use_container_width=True,
        hide_index=True,
    )

    # Show latest order prominently.
    if new_id and "orderid" in df.columns:
        latest = df[
            df["orderid"].astype(str) == str(new_id)
        ]
        if not latest.empty:
            st.subheader("Latest Submitted Order")
            st.dataframe(
                latest,
                use_container_width=True,
                hide_index=True,
            )

    show_raw_order_debug()


def show_positions():
    st.title("📊 Positions")

    try:
        api = ensure_login()
        response = api.position()

        if not isinstance(response, dict) or response.get("status") is not True:
            st.error(
                f"Position API failed | message="
                f"{response.get('message') if isinstance(response, dict) else response}"
            )
            return

        rows = response.get("data") or []
        if not rows:
            st.info("No open positions returned by Angel One.")
            return

        st.dataframe(
            pd.DataFrame(rows),
            use_container_width=True,
            hide_index=True,
        )
    except Exception as exc:
        st.error(str(exc))


def market_is_open(now=None):
    now = now or now_ist()
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
# SIDEBAR
# ============================================================
st.sidebar.title("Trading Control")
st.sidebar.write(f"Supertrend: **{ST_PERIOD},{ST_MULTIPLIER:g}**")
st.sidebar.write(f"Lots: **{LOTS}**")
st.sidebar.write(f"Product: **{PRODUCT_TYPE}**")
st.sidebar.write(f"Refresh: **{REFRESH_SECONDS}s**")

if LIVE_TRADING:
    st.sidebar.error("🔴 LIVE TRADING = ON")
else:
    st.sidebar.success("🟡 PAPER TRADING = ON")

if st.sidebar.button("🔐 Login Angel One", use_container_width=True):
    try:
        angel_login()
        st.session_state.last_message = "Angel One login successful"
        st.session_state.last_error = ""
    except Exception as exc:
        st.session_state.login_status = "NOT CONNECTED"
        st.session_state.last_error = str(exc)

if st.sidebar.button("📥 Load / Refresh Instruments", use_container_width=True):
    try:
        st.session_state.instruments = download_instrument_master(force=True)
        st.session_state.last_message = "Instrument master loaded"
        st.session_state.last_error = ""
    except Exception as exc:
        st.session_state.last_error = str(exc)

start_col, stop_col = st.sidebar.columns(2)
if start_col.button("▶ START", use_container_width=True):
    st.session_state.running = True

if stop_col.button("■ STOP", use_container_width=True):
    st.session_state.running = False

st.sidebar.divider()

page = st.sidebar.radio(
    "Page",
    ["Dashboard", "Order Book", "Positions"],
    index=["Dashboard", "Order Book", "Positions"].index(
        st.session_state.active_page
        if st.session_state.active_page in {"Dashboard", "Order Book", "Positions"}
        else "Dashboard"
    ),
)
st.session_state.active_page = page

# ============================================================
# FIRST LOAD: LOGIN + INSTRUMENTS + DATA
# ============================================================
if st.session_state.login_status == "NOT CONNECTED":
    st.sidebar.info(
        "Login first. Credentials are read from environment variables; "
        "do not put them into the source code."
    )

# ============================================================
# DASHBOARD LIVE ENGINE
# ============================================================
if page == "Dashboard":
    # Load instruments automatically after successful login.
    if st.session_state.login_status == "CONNECTED" and st.session_state.instruments is None:
        try:
            st.session_state.instruments = download_instrument_master()
        except Exception as exc:
            st.session_state.last_error = str(exc)

    # Run one initial cycle when running.
    if st.session_state.running and st.session_state.login_status == "CONNECTED":
        try:
            if market_is_open():
                run_live_cycle()
        except Exception as exc:
            st.session_state.last_error = (
                f"{type(exc).__name__}: {exc}"
            )

    show_dashboard()

    # Optional auto-refresh fragment.
    run_every = REFRESH_SECONDS if st.session_state.running else None

    @st.fragment(run_every=run_every)
    def live_refresh_fragment():
        if not st.session_state.running:
            st.caption("Live engine stopped. Click START to update live data.")
            return

        if not market_is_open():
            st.info("Market closed — no live order evaluation.")
            return

        try:
            run_live_cycle()
            st.success(
                f"Live update {st.session_state.last_update or '-'} | "
                f"NIFTY ₹{st.session_state.spot:,.2f} | "
                f"5m {direction_text(st.session_state.st5)} | "
                f"15m {direction_text(st.session_state.st15)} | "
                f"4h {direction_text(st.session_state.st4h)} | "
                f"Signal {st.session_state.signal}"
            )
        except Exception as exc:
            st.error(f"{type(exc).__name__}: {exc}")

    live_refresh_fragment()

elif page == "Order Book":
    show_order_book()

elif page == "Positions":
    show_positions()

# ============================================================
# SAFETY / STATUS
# ============================================================
st.sidebar.divider()
st.sidebar.write(
    f"**Mode:** {'LIVE' if LIVE_TRADING else 'PAPER'}"
)
st.sidebar.write(
    f"**Auto trade:** {'ON' if AUTO_TRADE else 'OFF'}"
)
st.sidebar.caption(
    "Strategy: 5m Supertrend 20,2 flip + 15m + 4h confirmation. "
    "Manual CE/PE BUY buttons remain available."
)
