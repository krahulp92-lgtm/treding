# ============================================================
# NIFTY SUPERTREND GREEN -> AUTOMATIC BUY ATM CE
# ANGEL ONE SMARTAPI
#
# REAL DATA - NO DUMMY DATA
#
# Strategy:
#   NIFTY 1-minute candles
#       ↓
#   Completed 2-minute candles
#       ↓
#   Supertrend (20, 1.5)
#       ↓
#   RED -> GREEN
#       ↓
#   Select nearest ATM NIFTY CE
#       ↓
#   BUY CE
#       ↓
#   Get Angel One ORDER ID
#
# Order ID source:
#   1. placeOrder() response
#   2. orderBook() fallback
# ============================================================

import os
import re
import json
import time
import math
import requests
import pyotp
import pandas as pd
import numpy as np

from datetime import datetime, timedelta
from pathlib import Path
from SmartApi import SmartConnect


# ============================================================
# CONFIG
# ============================================================

ANGEL_API_KEY = os.getenv("ANGEL_API_KEY")
ANGEL_CLIENT_ID = os.getenv("ANGEL_CLIENT_ID")
ANGEL_PASSWORD = os.getenv("ANGEL_PASSWORD")
ANGEL_TOTP_SECRET = os.getenv("ANGEL_TOTP_SECRET")

if not ANGEL_API_KEY:
    raise RuntimeError("ANGEL_API_KEY is missing")

if not ANGEL_CLIENT_ID:
    raise RuntimeError("ANGEL_CLIENT_ID is missing")

if not ANGEL_PASSWORD:
    raise RuntimeError("ANGEL_PASSWORD is missing")

if not ANGEL_TOTP_SECRET:
    raise RuntimeError("ANGEL_TOTP_SECRET is missing")


# ------------------------------------------------------------
# NIFTY
# ------------------------------------------------------------

NIFTY_EXCHANGE = "NSE"
NIFTY_SYMBOL = "NIFTY"
NIFTY_TOKEN = "99926000"


# ------------------------------------------------------------
# STRATEGY
# ------------------------------------------------------------

ST_PERIOD = 20
ST_MULTIPLIER = 1.5

CANDLE_INTERVAL = "ONE_MINUTE"

# Buy only one CE per signal
LOTS = 1

# Market order
ORDER_TYPE = "MARKET"

# Intraday
PRODUCT_TYPE = "INTRADAY"

# Prevent duplicate buy for same signal
STATE_FILE = Path("nifty_ce_state.json")

# Angel One instrument master
INSTRUMENT_URL = (
    "https://margincalculator.angelone.com/"
    "OpenAPI_File/files/OpenAPIScripMaster.json"
)

INSTRUMENT_FILE = Path("OpenAPIScripMaster.json")


# ============================================================
# TOTP
# ============================================================

def get_totp_secret(secret):
    """
    Accept either:
        ABCDEFG...
    or:
        otpauth://totp/...?secret=ABCDEFG...
    """

    secret = str(secret).strip()

    if secret.lower().startswith("otpauth://"):
        match = re.search(
            r"(?:\?|&)secret=([^&]+)",
            secret,
            flags=re.IGNORECASE
        )

        if not match:
            raise RuntimeError(
                "Could not extract TOTP secret from otpauth URI"
            )

        secret = match.group(1)

    return secret.replace(" ", "").upper()


# ============================================================
# ANGEL LOGIN
# ============================================================

def login_angel():

    print("Logging into Angel One...")

    secret = get_totp_secret(ANGEL_TOTP_SECRET)

    totp = pyotp.TOTP(secret).now()

    api = SmartConnect(api_key=ANGEL_API_KEY)

    response = api.generateSession(
        ANGEL_CLIENT_ID,
        ANGEL_PASSWORD,
        totp
    )

    if not response:
        raise RuntimeError(
            "Angel One returned empty login response"
        )

    if not response.get("status"):
        raise RuntimeError(
            f"Angel login failed: {response}"
        )

    data = response.get("data") or {}

    jwt_token = data.get("jwtToken")

    if not jwt_token:
        raise RuntimeError(
            f"Angel login succeeded but jwtToken missing: {response}"
        )

    print("Angel One login successful")

    return api


# ============================================================
# INSTRUMENT MASTER
# ============================================================

def download_instrument_master():

    if INSTRUMENT_FILE.exists():

        # Reuse existing file
        age = time.time() - INSTRUMENT_FILE.stat().st_mtime

        # Refresh if older than 12 hours
        if age < 12 * 60 * 60:
            print("Using existing instrument master")
            return

    print("Downloading Angel One instrument master...")

    response = requests.get(
        INSTRUMENT_URL,
        timeout=30
    )

    response.raise_for_status()

    INSTRUMENT_FILE.write_bytes(response.content)

    print("Instrument master downloaded")


def load_instruments():

    download_instrument_master()

    with open(
        INSTRUMENT_FILE,
        "r",
        encoding="utf-8"
    ) as f:
        data = json.load(f)

    df = pd.DataFrame(data)

    if df.empty:
        raise RuntimeError(
            "Instrument master is empty"
        )

    return df


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
            "NIFTY LTP API returned empty response"
        )

    if not response.get("status"):
        raise RuntimeError(
            f"NIFTY LTP failed: {response}"
        )

    data = response.get("data") or {}

    ltp = data.get("ltp")

    if ltp is None:
        raise RuntimeError(
            f"NIFTY LTP missing: {response}"
        )

    return float(ltp)


# ============================================================
# HISTORICAL NIFTY 1-MINUTE DATA
# ============================================================

def get_nifty_1m(api):

    now = datetime.now()

    start = now - timedelta(days=3)

    params = {
        "exchange": "NSE",
        "symboltoken": NIFTY_TOKEN,
        "interval": CANDLE_INTERVAL,
        "fromdate": start.strftime("%Y-%m-%d %H:%M"),
        "todate": now.strftime("%Y-%m-%d %H:%M")
    }

    response = api.getCandleData(params)

    if not response:
        raise RuntimeError(
            "Candle API returned empty response"
        )

    if not response.get("status"):
        raise RuntimeError(
            f"Candle API failed: {response}"
        )

    rows = response.get("data")

    if not rows:
        raise RuntimeError(
            "No NIFTY candle data returned"
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
        df["datetime"]
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

    df = df.dropna()

    df = df.sort_values(
        "datetime"
    )

    df = df.drop_duplicates(
        subset=["datetime"]
    )

    return df


# ============================================================
# CREATE COMPLETED 2-MINUTE CANDLES
# ============================================================

def make_2m_candles(df):

    x = df.copy()

    x = x.set_index("datetime")

    # IMPORTANT:
    # Angel timestamps represent candle start.
    # Remove currently forming 1-minute candle.
    now = pd.Timestamp.now()

    current_minute = now.replace(
        second=0,
        microsecond=0
    )

    x = x[x.index < current_minute]

    # NSE session
    x = x.between_time(
        "09:15",
        "15:30"
    )

    if x.empty:
        return pd.DataFrame()

    result = (
        x.resample(
            "2min",
            origin="start_day",
            offset="9h15min",
            label="right",
            closed="left"
        )
        .agg({
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum"
        })
    )

    result = result.dropna(
        subset=[
            "open",
            "high",
            "low",
            "close"
        ]
    )

    # A 2-minute candle ending at current time
    # can still be forming.
    completed_cutoff = (
        pd.Timestamp.now()
        .floor("2min")
    )

    result = result[
        result.index <= completed_cutoff
    ]

    return result.reset_index()


# ============================================================
# SUPERTREND
# ============================================================

def calculate_supertrend(
    df,
    period=20,
    multiplier=1.5
):

    x = df.copy()

    high = x["high"]
    low = x["low"]
    close = x["close"]

    # --------------------------------------------------------
    # True Range
    # --------------------------------------------------------

    previous_close = close.shift(1)

    tr1 = high - low

    tr2 = (
        high - previous_close
    ).abs()

    tr3 = (
        low - previous_close
    ).abs()

    tr = pd.concat(
        [tr1, tr2, tr3],
        axis=1
    ).max(axis=1)

    # --------------------------------------------------------
    # ATR
    # --------------------------------------------------------

    atr = tr.ewm(
        alpha=1 / period,
        adjust=False,
        min_periods=period
    ).mean()

    # --------------------------------------------------------
    # Basic bands
    # --------------------------------------------------------

    hl2 = (high + low) / 2

    basic_upper = (
        hl2 + multiplier * atr
    )

    basic_lower = (
        hl2 - multiplier * atr
    )

    final_upper = pd.Series(
        np.nan,
        index=x.index
    )

    final_lower = pd.Series(
        np.nan,
        index=x.index
    )

    supertrend = pd.Series(
        np.nan,
        index=x.index
    )

    direction = pd.Series(
        np.nan,
        index=x.index
    )

    # --------------------------------------------------------
    # Supertrend calculation
    # --------------------------------------------------------

    for i in range(len(x)):

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
                final_upper.iloc[i]
            )

            direction.iloc[i] = -1

            continue

        prev_fu = final_upper.iloc[i - 1]
        prev_fl = final_lower.iloc[i - 1]
        prev_close = close.iloc[i - 1]

        # Final upper
        if (
            basic_upper.iloc[i] < prev_fu
            or prev_close > prev_fu
        ):
            final_upper.iloc[i] = (
                basic_upper.iloc[i]
            )
        else:
            final_upper.iloc[i] = prev_fu

        # Final lower
        if (
            basic_lower.iloc[i] > prev_fl
            or prev_close < prev_fl
        ):
            final_lower.iloc[i] = (
                basic_lower.iloc[i]
            )
        else:
            final_lower.iloc[i] = prev_fl

        prev_st = supertrend.iloc[i - 1]

        # Previous supertrend was upper band
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

        # Previous supertrend was lower band
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

    x["ATR"] = atr

    x["BasicUpper"] = basic_upper
    x["BasicLower"] = basic_lower

    x["FinalUpper"] = final_upper
    x["FinalLower"] = final_lower

    x["Supertrend"] = supertrend

    x["ST_Direction"] = direction

    x["ST_Green"] = (
        x["ST_Direction"] == 1
    )

    x["ST_Red"] = (
        x["ST_Direction"] == -1
    )

    # --------------------------------------------------------
    # Flip
    # --------------------------------------------------------

    previous_direction = (
        x["ST_Direction"].shift(1)
    )

    x["RED_TO_GREEN"] = (
        (previous_direction == -1)
        &
        (x["ST_Direction"] == 1)
    )

    x["GREEN_TO_RED"] = (
        (previous_direction == 1)
        &
        (x["ST_Direction"] == -1)
    )

    return x


# ============================================================
# FIND ATM NIFTY CE
# ============================================================

def find_atm_ce(
    instruments,
    nifty_ltp
):

    df = instruments.copy()

    # --------------------------------------------------------
    # NFO only
    # --------------------------------------------------------

    df = df[
        df["exch_seg"]
        .astype(str)
        .str.upper()
        .eq("NFO")
    ]

    # --------------------------------------------------------
    # NIFTY options
    # --------------------------------------------------------

    if "name" in df.columns:

        df = df[
            df["name"]
            .astype(str)
            .str.upper()
            .isin(
                ["NIFTY", "NIFTY 50"]
            )
        ]

    # --------------------------------------------------------
    # CE only
    # --------------------------------------------------------

    df = df[
        df["symbol"]
        .astype(str)
        .str.upper()
        .str.endswith("CE")
    ]

    # --------------------------------------------------------
    # Option type
    # --------------------------------------------------------

    if "instrumenttype" in df.columns:

        df = df[
            df["instrumenttype"]
            .astype(str)
            .str.upper()
            .eq("OPTIDX")
        ]

    if df.empty:
        raise RuntimeError(
            "No NIFTY CE options found"
        )

    # --------------------------------------------------------
    # Expiry
    # --------------------------------------------------------

    df["expiry_dt"] = pd.to_datetime(
        df["expiry"],
        errors="coerce"
    )

    today = pd.Timestamp.now().normalize()

    df = df[
        df["expiry_dt"] >= today
    ]

    if df.empty:
        raise RuntimeError(
            "No future NIFTY CE expiry found"
        )

    nearest_expiry = (
        df["expiry_dt"].min()
    )

    df = df[
        df["expiry_dt"]
        == nearest_expiry
    ]

    # --------------------------------------------------------
    # Strike
    # --------------------------------------------------------

    df["strike_num"] = pd.to_numeric(
        df["strike"],
        errors="coerce"
    )

    # Angel master normally stores strike
    # multiplied by 100.
    if df["strike_num"].median() > 100000:

        df["strike_actual"] = (
            df["strike_num"] / 100
        )

    else:

        df["strike_actual"] = (
            df["strike_num"]
        )

    # --------------------------------------------------------
    # ATM
    # --------------------------------------------------------

    df["distance"] = (
        df["strike_actual"] - nifty_ltp
    ).abs()

    selected = (
        df.sort_values(
            "distance"
        )
        .iloc[0]
    )

    return selected


# ============================================================
# GET LOT SIZE
# ============================================================

def get_quantity(option_row):

    lot_size = int(
        float(option_row["lotsize"])
    )

    return lot_size * LOTS


# ============================================================
# STATE
# ============================================================

def load_state():

    if not STATE_FILE.exists():

        return {
            "last_signal_time": None,
            "last_order_id": None,
            "last_symbol": None
        }

    try:

        with open(
            STATE_FILE,
            "r",
            encoding="utf-8"
        ) as f:

            return json.load(f)

    except Exception:

        return {
            "last_signal_time": None,
            "last_order_id": None,
            "last_symbol": None
        }


def save_state(state):

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


# ============================================================
# PLACE REAL BUY CE ORDER
# ============================================================

def buy_ce(api, option):

    symbol = str(
        option["symbol"]
    )

    token = str(
        option["token"]
    )

    quantity = get_quantity(option)

    order_params = {

        "variety": "NORMAL",

        "tradingsymbol": symbol,

        "symboltoken": token,

        "transactiontype": "BUY",

        "exchange": "NFO",

        "ordertype": "MARKET",

        "producttype": PRODUCT_TYPE,

        "duration": "DAY",

        "price": "0",

        "squareoff": "0",

        "stoploss": "0",

        "quantity": str(quantity)
    }

    print()
    print("========================================")
    print("PLACING REAL BUY ORDER")
    print("========================================")

    print(
        "Symbol       :",
        symbol
    )

    print(
        "Token        :",
        token
    )

    print(
        "Quantity     :",
        quantity
    )

    print(
        "Transaction  : BUY"
    )

    print(
        "Order Type   : MARKET"
    )

    print(
        "========================================"
    )

    # --------------------------------------------------------
    # THIS IS THE IMPORTANT PART
    # --------------------------------------------------------

    response = api.placeOrderFullResponse(
        order_params
    )

    print()
    print("Angel One placeOrder response:")
    print(
        json.dumps(
            response,
            indent=2,
            default=str
        )
    )

    # --------------------------------------------------------
    # Extract order ID
    # --------------------------------------------------------

    order_id = None
    unique_order_id = None

    if isinstance(response, dict):

        data = response.get("data") or {}

        order_id = data.get(
            "orderid"
        )

        unique_order_id = data.get(
            "uniqueorderid"
        )

    if order_id:

        print()
        print("========================================")
        print("ORDER ID FOUND")
        print("========================================")
        print(
            "ORDER ID       :",
            order_id
        )

        if unique_order_id:
            print(
                "UNIQUE ORDER ID:",
                unique_order_id
            )

        print(
            "Symbol         :",
            symbol
        )

        print("========================================")

        return {
            "orderid": str(order_id),
            "uniqueorderid": (
                str(unique_order_id)
                if unique_order_id
                else None
            )
        }

    # --------------------------------------------------------
    # If placeOrder response did not expose orderid,
    # use Order Book fallback.
    # --------------------------------------------------------

    print()
    print(
        "placeOrder did not return orderid."
    )

    print(
        "Searching Angel One Order Book..."
    )

    recovered = recover_order_id_from_book(
        api=api,
        symbol=symbol,
        token=token,
        quantity=quantity
    )

    if recovered:

        print()
        print("========================================")
        print("ORDER ID RECOVERED FROM ORDER BOOK")
        print("========================================")

        print(
            "ORDER ID:",
            recovered["orderid"]
        )

        print(
            "STATUS:",
            recovered.get("status")
        )

        print("========================================")

        return recovered

    raise RuntimeError(
        "Angel One did not return an order ID "
        "and no matching order was found in Order Book."
    )


# ============================================================
# ORDER BOOK FALLBACK
# ============================================================

def recover_order_id_from_book(
    api,
    symbol,
    token,
    quantity
):

    try:

        response = api.orderBook()

    except Exception as e:

        print(
            "orderBook() error:",
            e
        )

        return None

    if not response:

        return None

    if not response.get("status"):

        print(
            "Order Book failed:",
            response
        )

        return None

    orders = response.get("data") or []

    if not orders:

        return None

    # Newest orders first
    orders = list(reversed(orders))

    for order in orders:

        order_symbol = str(
            order.get(
                "tradingsymbol",
                ""
            )
        ).upper()

        order_token = str(
            order.get(
                "symboltoken",
                ""
            )
        )

        order_transaction = str(
            order.get(
                "transactiontype",
                ""
            )
        ).upper()

        order_quantity = str(
            order.get(
                "quantity",
                ""
            )
        )

        if order_symbol != symbol.upper():
            continue

        if order_token != token:
            continue

        if order_transaction != "BUY":
            continue

        if order_quantity != str(quantity):
            continue

        order_id = order.get(
            "orderid"
        )

        if not order_id:
            continue

        return {
            "orderid": str(order_id),

            "uniqueorderid": (
                str(
                    order.get(
                        "uniqueorderid"
                    )
                )
                if order.get("uniqueorderid")
                else None
            ),

            "status": order.get(
                "orderstatus"
            ),

            "symbol": order_symbol
        }

    return None


# ============================================================
# MAIN TRADING LOOP
# ============================================================

def main():

    print()
    print("============================================")
    print("NIFTY SUPERTREND AUTOMATIC CE BUY")
    print("Supertrend: 20 / 1.5")
    print("Timeframe: Completed 2-minute")
    print("============================================")

    api = login_angel()

    instruments = load_instruments()

    state = load_state()

    print()
    print("Trading engine started.")

    while True:

        try:

            # ------------------------------------------------
            # Get NIFTY live price
            # ------------------------------------------------

            nifty_ltp = get_nifty_ltp(
                api
            )

            print()
            print(
                datetime.now().strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
            )

            print(
                "NIFTY LTP:",
                nifty_ltp
            )

            # ------------------------------------------------
            # Get real 1-minute candles
            # ------------------------------------------------

            df_1m = get_nifty_1m(
                api
            )

            # ------------------------------------------------
            # Convert to completed 2-minute candles
            # ------------------------------------------------

            df_2m = make_2m_candles(
                df_1m
            )

            if len(df_2m) < ST_PERIOD + 3:

                print(
                    "Not enough completed candles."
                )

                time.sleep(20)

                continue

            # ------------------------------------------------
            # Supertrend
            # ------------------------------------------------

            st_df = calculate_supertrend(
                df_2m,
                ST_PERIOD,
                ST_MULTIPLIER
            )

            latest = st_df.iloc[-1]

            signal_time = str(
                latest["datetime"]
            )

            current_direction = (
                latest["ST_Direction"]
            )

            is_green = (
                current_direction == 1
            )

            is_flip = bool(
                latest["RED_TO_GREEN"]
            )

            print(
                "2M Supertrend:",
                "GREEN" if is_green
                else "RED"
            )

            print(
                "RED -> GREEN:",
                is_flip
            )

            print(
                "Signal candle:",
                signal_time
            )

            # ------------------------------------------------
            # BUY ONLY ON RED -> GREEN
            # ------------------------------------------------

            if not is_flip:

                print(
                    "No new GREEN BUY signal."
                )

                time.sleep(20)

                continue

            # ------------------------------------------------
            # DUPLICATE PROTECTION
            # ------------------------------------------------

            if (
                state.get(
                    "last_signal_time"
                )
                == signal_time
            ):

                print(
                    "This signal already processed."
                )

                time.sleep(20)

                continue

            # ------------------------------------------------
            # Select ATM CE
            # ------------------------------------------------

            option = find_atm_ce(
                instruments,
                nifty_ltp
            )

            print()
            print(
                "ATM CE selected:",
                option["symbol"]
            )

            print(
                "Strike:",
                option["strike_actual"]
            )

            print(
                "Expiry:",
                option["expiry"]
            )

            print(
                "Token:",
                option["token"]
            )

            print(
                "Lot size:",
                option["lotsize"]
            )

            # ------------------------------------------------
            # MARK SIGNAL AS PROCESSED
            #
            # This prevents Streamlit / loop duplicates.
            # ------------------------------------------------

            state["last_signal_time"] = (
                signal_time
            )

            state["last_symbol"] = (
                str(option["symbol"])
            )

            save_state(state)

            # ------------------------------------------------
            # REAL BUY
            # ------------------------------------------------

            result = buy_ce(
                api,
                option
            )

            # ------------------------------------------------
            # SAVE ORDER ID
            # ------------------------------------------------

            state["last_order_id"] = (
                result["orderid"]
            )

            if result.get(
                "uniqueorderid"
            ):

                state["last_unique_order_id"] = (
                    result["uniqueorderid"]
                )

            save_state(state)

            print()
            print(
                "****************************************"
            )

            print(
                "AUTOMATIC BUY CE COMPLETED"
            )

            print(
                "ORDER ID:",
                result["orderid"]
            )

            print(
                "****************************************"
            )

            # ------------------------------------------------
            # Wait before next polling cycle
            # ------------------------------------------------

            time.sleep(20)

        except KeyboardInterrupt:

            print()
            print(
                "Trading engine stopped."
            )

            break

        except Exception as e:

            print()
            print(
                "ERROR:",
                repr(e)
            )

            time.sleep(20)


# ============================================================
# START
# ============================================================

if __name__ == "__main__":
    main()
