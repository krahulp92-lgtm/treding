
# ============================================================
# NIFTY LIVE SUPERTREND 20,2 + ANGEL ONE SMARTAPI
# BUY ATM CE / PE
# ============================================================

import os
import json
import time
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import numpy as np
import streamlit as st
import pyotp

from SmartApi import SmartConnect


# ============================================================
# CONFIG
# ============================================================

st.set_page_config(
    page_title="NIFTY Live Supertrend Trading",
    page_icon="📈",
    layout="wide"
)

IST = ZoneInfo("Asia/Kolkata")

# ------------------------------------------------------------
# IMPORTANT SAFETY SWITCH
# ------------------------------------------------------------
# False = NO REAL ORDER
# True  = REAL BUY ORDER
LIVE_TRADING = True

ST_PERIOD = 20
ST_MULTIPLIER = 2.0

CANDLE_INTERVAL = "FIVE_MINUTE"

# NIFTY 50 index token
# Verify this against your SmartAPI instrument master if needed.
NIFTY_TOKEN = "99926000"

STATE_FILE = Path("trading_state.json")


# ============================================================
# CREDENTIALS
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
    "logged_in": False,
    "login_message": "",
    "last_error": "",

    "spot": None,

    "signal": "WAIT",
    "st5": None,
    "st15": None,
    "st4h": None,

    "option_symbol": None,
    "option_token": None,
    "option_type": None,
    "option_expiry": None,
    "option_strike": None,
    "option_lot_size": None,
    "option_ltp": None,

    "last_order_id": None,
    "last_order_signal": None,
    "last_order_time": None,

    "candles": None,
    "instruments": None,
}

for key, value in DEFAULTS.items():
    if key not in st.session_state:
        st.session_state[key] = value


# ============================================================
# UTILITY
# ============================================================

def now_ist():
    return datetime.now(IST)


def market_is_open():
    now = now_ist()

    if now.weekday() >= 5:
        return False

    market_start = now.replace(
        hour=9, minute=15, second=0, microsecond=0
    )

    market_end = now.replace(
        hour=15, minute=30, second=0, microsecond=0
    )

    return market_start <= now <= market_end


def get_totp_secret(raw):
    """
    Accepts either:
      ABCDEFG...
    or:
      otpauth://totp/...
    """

    raw = raw.strip()

    if raw.startswith("otpauth://"):
        from urllib.parse import urlparse, parse_qs

        parsed = urlparse(raw)
        params = parse_qs(parsed.query)

        secret = params.get("secret", [""])[0]
        return secret.strip()

    return raw


# ============================================================
# LOGIN
# ============================================================

def login_angel_one():

    if not ANGEL_API_KEY:
        raise RuntimeError("ANGEL_API_KEY is missing")

    if not ANGEL_CLIENT_ID:
        raise RuntimeError("ANGEL_CLIENT_ID is missing")

    if not ANGEL_PASSWORD:
        raise RuntimeError("ANGEL_PASSWORD is missing")

    if not ANGEL_TOTP_SECRET:
        raise RuntimeError("ANGEL_TOTP_SECRET is missing")

    secret = get_totp_secret(ANGEL_TOTP_SECRET)

    if not secret:
        raise RuntimeError("Invalid TOTP secret")

    try:
        totp = pyotp.TOTP(secret).now()
    except Exception as e:
        raise RuntimeError(
            f"TOTP generation failed: {e}"
        )

    api = SmartConnect(api_key=ANGEL_API_KEY)

    response = api.generateSession(
        ANGEL_CLIENT_ID,
        ANGEL_PASSWORD,
        totp
    )

    if not response:
        raise RuntimeError("Empty login response")

    if response.get("status") is not True:
        raise RuntimeError(
            "ANGEL LOGIN FAILED | "
            f"message={response.get('message')} | "
            f"errorcode={response.get('errorcode')}"
        )

    st.session_state.api = api
    st.session_state.logged_in = True

    return response


# ============================================================
# LOAD INSTRUMENT MASTER
# ============================================================

@st.cache_data(ttl=3600)
def download_instruments():

    url = (
        "https://margincalculator.angelone.com/"
        "OpenAPI_File/files/OpenAPIScripMaster.json"
    )

    import requests

    response = requests.get(
        url,
        timeout=30
    )

    response.raise_for_status()

    data = response.json()

    if not isinstance(data, list):
        raise RuntimeError(
            "Instrument master returned invalid data"
        )

    return data


# ============================================================
# NIFTY SPOT
# ============================================================

def get_nifty_ltp():

    api = st.session_state.api

    if api is None:
        raise RuntimeError("SmartAPI is not connected")

    try:

        response = api.ltpData(
            "NSE",
            "NIFTY",
            NIFTY_TOKEN
        )

    except Exception as e:
        raise RuntimeError(
            f"NIFTY LTP FAILED | {e}"
        )

    if not response:
        raise RuntimeError(
            "NIFTY LTP FAILED | Empty response"
        )

    if response.get("status") is not True:
        raise RuntimeError(
            "NIFTY LTP FAILED | "
            f"message={response.get('message')} | "
            f"errorcode={response.get('errorcode')}"
        )

    data = response.get("data")

    if not data:
        raise RuntimeError(
            "NIFTY LTP FAILED | No data"
        )

    ltp = data.get("ltp")

    if ltp is None:
        raise RuntimeError(
            "NIFTY LTP FAILED | LTP missing"
        )

    return float(ltp)


# ============================================================
# HISTORICAL CANDLES
# ============================================================

def get_nifty_candles(days=5):

    api = st.session_state.api

    if api is None:
        raise RuntimeError("SmartAPI is not connected")

    end = now_ist()
    start = end - timedelta(days=days)

    params = {
        "exchange": "NSE",
        "symboltoken": NIFTY_TOKEN,
        "interval": CANDLE_INTERVAL,
        "fromdate": start.strftime("%Y-%m-%d %H:%M"),
        "todate": end.strftime("%Y-%m-%d %H:%M"),
    }

    try:

        response = api.getCandleData(params)

    except Exception as e:
        raise RuntimeError(
            f"Candle API failed: {e}"
        )

    if not response:
        raise RuntimeError(
            "Candle API failed: Empty response"
        )

    if response.get("status") is not True:
        raise RuntimeError(
            "Candle API failed: "
            f"{response.get('message')} "
            f"{response.get('errorcode')}"
        )

    rows = response.get("data")

    if not rows:
        raise RuntimeError(
            "Candle API returned no candles"
        )

    df = pd.DataFrame(
        rows,
        columns=[
            "datetime",
            "open",
            "high",
            "low",
            "close",
            "volume"
        ]
    )

    df["datetime"] = pd.to_datetime(
        df["datetime"],
        errors="coerce"
    )

    df["open"] = pd.to_numeric(df["open"])
    df["high"] = pd.to_numeric(df["high"])
    df["low"] = pd.to_numeric(df["low"])
    df["close"] = pd.to_numeric(df["close"])
    df["volume"] = pd.to_numeric(df["volume"])

    df = df.dropna()

    df = df.sort_values("datetime")

    df = df.drop_duplicates(
        subset=["datetime"]
    )

    return df.reset_index(drop=True)


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

    previous_close = close.shift(1)

    tr1 = high - low
    tr2 = (high - previous_close).abs()
    tr3 = (low - previous_close).abs()

    tr = pd.concat(
        [tr1, tr2, tr3],
        axis=1
    ).max(axis=1)

    atr = tr.ewm(
        alpha=1 / period,
        adjust=False
    ).mean()

    hl2 = (high + low) / 2

    basic_upper = hl2 + multiplier * atr
    basic_lower = hl2 - multiplier * atr

    final_upper = basic_upper.copy()
    final_lower = basic_lower.copy()

    supertrend = pd.Series(
        np.nan,
        index=df.index
    )

    direction = pd.Series(
        0,
        index=df.index,
        dtype=int
    )

    for i in range(1, len(df)):

        if (
            basic_upper.iloc[i] < final_upper.iloc[i - 1]
            or close.iloc[i - 1] > final_upper.iloc[i - 1]
        ):
            final_upper.iloc[i] = basic_upper.iloc[i]
        else:
            final_upper.iloc[i] = final_upper.iloc[i - 1]

        if (
            basic_lower.iloc[i] > final_lower.iloc[i - 1]
            or close.iloc[i - 1] < final_lower.iloc[i - 1]
        ):
            final_lower.iloc[i] = basic_lower.iloc[i]
        else:
            final_lower.iloc[i] = final_lower.iloc[i - 1]

        if i == 1:

            if close.iloc[i] <= final_upper.iloc[i]:
                direction.iloc[i] = -1
                supertrend.iloc[i] = final_upper.iloc[i]
            else:
                direction.iloc[i] = 1
                supertrend.iloc[i] = final_lower.iloc[i]

        else:

            if supertrend.iloc[i - 1] == final_upper.iloc[i - 1]:

                if close.iloc[i] <= final_upper.iloc[i]:
                    direction.iloc[i] = -1
                    supertrend.iloc[i] = final_upper.iloc[i]
                else:
                    direction.iloc[i] = 1
                    supertrend.iloc[i] = final_lower.iloc[i]

            else:

                if close.iloc[i] >= final_lower.iloc[i]:
                    direction.iloc[i] = 1
                    supertrend.iloc[i] = final_lower.iloc[i]
                else:
                    direction.iloc[i] = -1
                    supertrend.iloc[i] = final_upper.iloc[i]

    df["ATR"] = atr
    df["Final_Upper"] = final_upper
    df["Final_Lower"] = final_lower
    df["Supertrend"] = supertrend
    df["Direction"] = direction

    df["ST_Green"] = df["Direction"] == 1
    df["ST_Red"] = df["Direction"] == -1

    df["ST_Flip_Green"] = (
        (df["Direction"] == 1)
        & (df["Direction"].shift(1) == -1)
    )

    df["ST_Flip_Red"] = (
        (df["Direction"] == -1)
        & (df["Direction"].shift(1) == 1)
    )

    return df


# ============================================================
# RESAMPLE
# ============================================================

def resample_ohlcv(df, rule):

    x = df.copy()

    x = x.set_index("datetime")

    result = x.resample(
        rule,
        origin="start_day",
        offset="9h15min",
        label="right",
        closed="left"
    ).agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum"
    })

    result = result.dropna()

    return result.reset_index()


# ============================================================
# BUILD TIMEFRAMES
# ============================================================

def build_timeframes(df5):

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

    return st5, st15, st4h


# ============================================================
# CURRENT SIGNAL
# ============================================================

def get_current_signal(
    st5,
    st15,
    st4h
):

    if len(st5) < 2:
        return "WAIT"

    if len(st15) < 1:
        return "WAIT"

    if len(st4h) < 1:
        return "WAIT"

    five = st5.iloc[-1]

    fifteen = st15.iloc[-1]
    four_hour = st4h.iloc[-1]

    if (
        bool(five["ST_Flip_Green"])
        and bool(fifteen["ST_Green"])
        and bool(four_hour["ST_Green"])
    ):
        return "BUY_CE"

    if (
        bool(five["ST_Flip_Red"])
        and bool(fifteen["ST_Red"])
        and bool(four_hour["ST_Red"])
    ):
        return "BUY_PE"

    return "WAIT"


# ============================================================
# OPTION SELECTION
# ============================================================

def select_atm_option(
    instruments,
    spot,
    option_type
):

    if not instruments:
        raise RuntimeError(
            "Instrument master is empty"
        )

    rows = []

    for item in instruments:

        if str(item.get("exch_seg", "")).upper() != "NFO":
            continue

        if str(item.get("instrumenttype", "")).upper() != "OPTIDX":
            continue

        if str(item.get("name", "")).upper() != "NIFTY":
            continue

        symbol = str(
            item.get("symbol", "")
        ).upper()

        if not symbol.endswith(option_type):
            continue

        try:
            expiry = datetime.strptime(
                str(item["expiry"]),
                "%d%b%Y"
            ).date()
        except Exception:
            continue

        if expiry < now_ist().date():
            continue

        try:
            strike_raw = float(
                item["strike"]
            )

            # Angel instrument master commonly
            # represents strike with ×100.
            strike = strike_raw / 100.0

            lot_size = int(
                float(item["lotsize"])
            )

            token = str(
                item["token"]
            )

        except Exception:
            continue

        rows.append({
            "symbol": symbol,
            "token": token,
            "expiry": expiry,
            "strike": strike,
            "lot_size": lot_size
        })

    if not rows:
        raise RuntimeError(
            f"No NIFTY {option_type} options found"
        )

    df = pd.DataFrame(rows)

    nearest_expiry = df["expiry"].min()

    df = df[
        df["expiry"] == nearest_expiry
    ].copy()

    df["distance"] = (
        df["strike"] - spot
    ).abs()

    df = df.sort_values(
        "distance"
    )

    if df.empty:
        raise RuntimeError(
            "ATM option not found"
        )

    return df.iloc[0].to_dict()


# ============================================================
# OPTION LTP
# ============================================================

def get_option_ltp(
    symbol,
    token
):

    api = st.session_state.api

    response = api.ltpData(
        "NFO",
        symbol,
        str(token)
    )

    if not response:
        raise RuntimeError(
            "Option LTP returned empty response"
        )

    if response.get("status") is not True:
        raise RuntimeError(
            f"Option LTP failed: "
            f"{response.get('message')}"
        )

    data = response.get("data")

    if not data:
        raise RuntimeError(
            "Option LTP data missing"
        )

    return float(data["ltp"])


# ============================================================
# PLACE BUY ORDER
# ============================================================

def place_buy_order(option):

    api = st.session_state.api

    if api is None:
        raise RuntimeError(
            "SmartAPI not connected"
        )

    symbol = option["symbol"]
    token = str(option["token"])
    quantity = int(option["lot_size"])

    # --------------------------------------------------------
    # SAFETY
    # --------------------------------------------------------

    if not LIVE_TRADING:

        return {
            "status": True,
            "paper": True,
            "message": (
                "LIVE_TRADING=False. "
                "No real order was sent."
            ),
            "symbol": symbol,
            "token": token,
            "quantity": quantity
        }

    # --------------------------------------------------------
    # REAL ORDER
    # --------------------------------------------------------

    order_params = {
        "variety": "NORMAL",
        "tradingsymbol": symbol,
        "symboltoken": token,
        "transactiontype": "BUY",
        "exchange": "NFO",
        "ordertype": "MARKET",
        "producttype": "INTRADAY",
        "duration": "DAY",
        "price": "0",
        "squareoff": "0",
        "stoploss": "0",
        "quantity": str(quantity),
    }

    try:

        response = api.placeOrder(
            order_params
        )

    except Exception as e:

        raise RuntimeError(
            f"ORDER API FAILED: {e}"
        )

    if not response:
        raise RuntimeError(
            "ORDER API FAILED: Empty response"
        )

    if isinstance(response, str):

        order_id = response

        return {
            "status": True,
            "paper": False,
            "order_id": order_id
        }

    if response.get("status") is not True:

        raise RuntimeError(
            "ORDER FAILED | "
            f"message={response.get('message')} | "
            f"errorcode={response.get('errorcode')}"
        )

    data = response.get("data") or {}

    order_id = (
        data.get("orderid")
        or data.get("order_id")
    )

    return {
        "status": True,
        "paper": False,
        "order_id": order_id,
        "response": response
    }


# ============================================================
# ORDER BOOK
# ============================================================

def get_order_book():

    api = st.session_state.api

    if api is None:
        return []

    try:

        response = api.orderBook()

        if response and response.get("status"):

            return response.get(
                "data",
                []
            )

    except Exception:
        pass

    return []


# ============================================================
# POSITIONS
# ============================================================

def get_positions():

    api = st.session_state.api

    if api is None:
        return []

    try:

        response = api.position()

        if response and response.get("status"):

            return response.get(
                "data",
                []
            )

    except Exception:
        pass

    return []


# ============================================================
# UI
# ============================================================

st.title(
    "📈 NIFTY Live Supertrend 20,2"
)

st.caption(
    "Angel One SmartAPI | ATM CE / PE"
)


# ============================================================
# SIDEBAR
# ============================================================

with st.sidebar:

    st.header("Trading Control")

    st.write(
        "LIVE_TRADING:",
        "**ON**" if LIVE_TRADING else "**OFF**"
    )

    if LIVE_TRADING:

        st.error(
            "REAL MONEY MODE"
        )

    else:

        st.warning(
            "Paper/Test Mode — no real orders"
        )

    st.divider()

    if st.button(
        "🔐 Connect Angel One",
        use_container_width=True
    ):

        try:

            login_angel_one()

            st.success(
                "Angel One connected"
            )

        except Exception as e:

            st.session_state.last_error = str(e)

            st.error(
                st.session_state.last_error
            )

    if st.session_state.logged_in:

        st.success("API Connected")

    else:

        st.info("Not connected")


# ============================================================
# MAIN
# ============================================================

if not st.session_state.logged_in:

    st.info(
        "Click 'Connect Angel One' from the sidebar."
    )

    st.stop()


# ============================================================
# LOAD INSTRUMENTS
# ============================================================

if st.session_state.instruments is None:

    try:

        st.session_state.instruments = (
            download_instruments()
        )

    except Exception as e:

        st.error(
            f"Instrument master failed: {e}"
        )

        st.stop()


# ============================================================
# GET NIFTY
# ============================================================

try:

    spot = get_nifty_ltp()

    st.session_state.spot = spot

except Exception as e:

    st.error(str(e))

    st.stop()


# ============================================================
# GET CANDLES
# ============================================================

try:

    candles = get_nifty_candles(
        days=5
    )

    st.session_state.candles = candles

except Exception as e:

    st.error(str(e))

    st.stop()


# ============================================================
# SUPERTREND
# ============================================================

try:

    st5, st15, st4h = build_timeframes(
        candles
    )

    signal = get_current_signal(
        st5,
        st15,
        st4h
    )

    st.session_state.st5 = bool(
        st5.iloc[-1]["ST_Green"]
    )

    st.session_state.st15 = bool(
        st15.iloc[-1]["ST_Green"]
    )

    st.session_state.st4h = bool(
        st4h.iloc[-1]["ST_Green"]
    )

    st.session_state.signal = signal

except Exception as e:

    st.error(
        f"Supertrend calculation failed: {e}"
    )

    st.stop()


# ============================================================
# TOP METRICS
# ============================================================

c1, c2, c3, c4 = st.columns(4)

c1.metric(
    "NIFTY",
    f"{spot:,.2f}"
)

c2.metric(
    "5M ST",
    "GREEN"
    if st.session_state.st5
    else "RED"
)

c3.metric(
    "15M ST",
    "GREEN"
    if st.session_state.st15
    else "RED"
)

c4.metric(
    "4H ST",
    "GREEN"
    if st.session_state.st4h
    else "RED"
)


# ============================================================
# SIGNAL
# ============================================================

st.divider()

if signal == "BUY_CE":

    st.success(
        "🟢 BUY CE SIGNAL"
    )

elif signal == "BUY_PE":

    st.error(
        "🔴 BUY PE SIGNAL"
    )

else:

    st.info(
        "⏳ WAIT — No new confirmed signal"
    )


# ============================================================
# SELECT OPTION
# ============================================================

selected_option = None

if signal in ["BUY_CE", "BUY_PE"]:

    option_type = (
        "CE"
        if signal == "BUY_CE"
        else "PE"
    )

    try:

        selected_option = select_atm_option(
            st.session_state.instruments,
            spot,
            option_type
        )

        option_ltp = get_option_ltp(
            selected_option["symbol"],
            selected_option["token"]
        )

        st.session_state.option_symbol = (
            selected_option["symbol"]
        )

        st.session_state.option_token = (
            selected_option["token"]
        )

        st.session_state.option_type = (
            option_type
        )

        st.session_state.option_expiry = (
            selected_option["expiry"]
        )

        st.session_state.option_strike = (
            selected_option["strike"]
        )

        st.session_state.option_lot_size = (
            selected_option["lot_size"]
        )

        st.session_state.option_ltp = (
            option_ltp
        )

    except Exception as e:

        st.error(
            f"Option selection failed: {e}"
        )


# ============================================================
# OPTION DETAILS
# ============================================================

if selected_option is not None:

    st.subheader(
        "ATM Option"
    )

    a, b, c, d, e = st.columns(5)

    a.metric(
        "Symbol",
        selected_option["symbol"]
    )

    b.metric(
        "Strike",
        f"{selected_option['strike']:,.0f}"
    )

    c.metric(
        "Expiry",
        str(selected_option["expiry"])
    )

    d.metric(
        "Lot Size",
        str(selected_option["lot_size"])
    )

    e.metric(
        "Option LTP",
        f"₹{st.session_state.option_ltp:,.2f}"
    )


# ============================================================
# MANUAL BUY BUTTON
# ============================================================

st.divider()

st.subheader(
    "Order"
)

if selected_option is not None:

    buy_col, refresh_col = st.columns(2)

    with buy_col:

        if st.button(
            f"🛒 BUY {selected_option['symbol']}",
            type="primary",
            use_container_width=True
        ):

            if not market_is_open():

                st.error(
                    "Market is closed."
                )

            else:

                try:

                    result = place_buy_order(
                        selected_option
                    )

                    if result.get("paper"):

                        st.warning(
                            result["message"]
                        )

                        st.json(result)

                    else:

                        order_id = result.get(
                            "order_id"
                        )

                        st.session_state.last_order_id = (
                            order_id
                        )

                        st.session_state.last_order_signal = (
                            signal
                        )

                        st.session_state.last_order_time = (
                            str(now_ist())
                        )

                        st.success(
                            f"BUY ORDER SENT | "
                            f"Order ID: {order_id}"
                        )

                        st.json(result)

                except Exception as e:

                    st.error(
                        str(e)
                    )

    with refresh_col:

        if st.button(
            "🔄 Refresh",
            use_container_width=True
        ):

            st.rerun()

else:

    st.info(
        "No CE/PE order available until a confirmed signal occurs."
    )


# ============================================================
# LAST ORDER
# ============================================================

if st.session_state.last_order_id:

    st.divider()

    st.subheader(
        "Last Order"
    )

    x, y, z = st.columns(3)

    x.metric(
        "Order ID",
        str(st.session_state.last_order_id)
    )

    y.metric(
        "Signal",
        str(st.session_state.last_order_signal)
    )

    z.metric(
        "Time",
        str(st.session_state.last_order_time)
    )


# ============================================================
# POSITIONS
# ============================================================

st.divider()

st.subheader(
    "Open Positions"
)

positions = get_positions()

if positions:

    st.dataframe(
        pd.DataFrame(positions),
        use_container_width=True
    )

else:

    st.info(
        "No position data."
    )


# ============================================================
# ORDER BOOK
# ============================================================

st.divider()

st.subheader(
    "Order Book"
)

orders = get_order_book()

if orders:

    st.dataframe(
        pd.DataFrame(orders),
        use_container_width=True
    )

else:

    st.info(
        "No orders."
    )


# ============================================================
# CANDLE TABLE
# ============================================================

with st.expander(
    "Latest NIFTY Candles"
):

    display_df = st5[
        [
            "datetime",
            "open",
            "high",
            "low",
            "close",
            "Supertrend",
            "ST_Green",
            "ST_Flip_Green",
            "ST_Flip_Red"
        ]
    ].tail(30)

    st.dataframe(
        display_df,
        use_container_width=True
    )


# ============================================================
# AUTO REFRESH
# ============================================================

st.caption(
    f"Last update: {now_ist().strftime('%Y-%m-%d %H:%M:%S IST')}"
)

if st.button(
    "🔁 Refresh Data",
    use_container_width=True
):

    st.rerun()

