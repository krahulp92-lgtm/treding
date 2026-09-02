import time
import math

import streamlit as st


# =========================================================
# PAGE CONFIG
# =========================================================

st.set_page_config(
    page_title="Live Trading",
    page_icon="📈",
    layout="wide"
)


# =========================================================
# SESSION STATE
# =========================================================

if "running" not in st.session_state:
    st.session_state.running = False

if "last_signal" not in st.session_state:
    st.session_state.last_signal = "WAIT"

if "atm_strike" not in st.session_state:
    st.session_state.atm_strike = None


# =========================================================
# FUNCTIONS
# =========================================================

def get_atm_strike(nifty_price, strike_interval=50):
    """
    Calculate ATM strike from NIFTY spot price.
    """

    if nifty_price is None:
        return None

    return int(
        math.floor(
            (float(nifty_price) / strike_interval) + 0.5
        ) * strike_interval
    )


def generate_signal(st_5m, st_15m, st_4h, previous_5m):
    """
    Supertrend strategy:

    RED -> GREEN on 5M
    + 15M GREEN
    + 4H GREEN
        = BUY ATM CE

    GREEN -> RED on 5M
    + 15M RED
    + 4H RED
        = BUY ATM PE
    """

    if (
        previous_5m == -1
        and st_5m == 1
        and st_15m == 1
        and st_4h == 1
    ):
        return "BUY CE"

    elif (
        previous_5m == 1
        and st_5m == -1
        and st_15m == -1
        and st_4h == -1
    ):
        return "BUY PE"

    return "WAIT"


# =========================================================
# TITLE
# =========================================================

st.title("📈 Live Trading")


# =========================================================
# CHECK RUNNING STATUS
# =========================================================

if not st.session_state.running:

    st.warning("Trading is not running.")

    if st.button("⬅ Back to Dashboard"):

        st.switch_page("treding.py")

    st.stop()


# =========================================================
# TRADING ACTIVE
# =========================================================

st.success("🟢 Trading Engine Running")


# =========================================================
# MARKET INFORMATION
# =========================================================

col1, col2, col3, col4 = st.columns(4)

with col1:
    st.metric(
        "NIFTY",
        "Loading..."
    )

with col2:
    st.metric(
        "5 Minute ST",
        "Loading..."
    )

with col3:
    st.metric(
        "15 Minute ST",
        "Loading..."
    )

with col4:
    st.metric(
        "4 Hour ST",
        "Loading..."
    )


st.divider()


# =========================================================
# SIGNAL
# =========================================================

st.subheader("Current Trading Signal")

signal_box = st.empty()

signal_box.info(
    "Checking market conditions..."
)


# =========================================================
# LIVE DATA
# =========================================================
#
# IMPORTANT:
# Replace this section with your Angel One SmartAPI
# candle-data functions.
#
# Example:
#
# df_5m = get_candle_data("FIVE_MINUTE")
# df_15m = get_candle_data("FIFTEEN_MINUTE")
# df_4h = get_candle_data("FOUR_HOUR")
#
# df_5m = calculate_supertrend(df_5m, 20, 2)
# df_15m = calculate_supertrend(df_15m, 20, 2)
# df_4h = calculate_supertrend(df_4h, 20, 2)
#
# =========================================================


# ---------------------------------------------------------
# TEMPORARY TEST VALUES
# ---------------------------------------------------------
#
# REMOVE THESE when connecting real data.
#

nifty_price = 25183

st_5m = 1
st_15m = 1
st_4h = 1

previous_5m = -1


# =========================================================
# ATM STRIKE
# =========================================================

atm_strike = get_atm_strike(
    nifty_price,
    strike_interval=50
)

st.session_state.atm_strike = atm_strike


# =========================================================
# GENERATE SIGNAL
# =========================================================

signal = generate_signal(
    st_5m,
    st_15m,
    st_4h,
    previous_5m
)


# =========================================================
# DISPLAY MARKET DATA
# =========================================================

col1, col2, col3, col4 = st.columns(4)

with col1:
    st.metric(
        "NIFTY",
        f"{nifty_price:,.2f}"
    )

with col2:
    st.metric(
        "5M Supertrend",
        "GREEN" if st_5m == 1 else "RED"
    )

with col3:
    st.metric(
        "15M Supertrend",
        "GREEN" if st_15m == 1 else "RED"
    )

with col4:
    st.metric(
        "4H Supertrend",
        "GREEN" if st_4h == 1 else "RED"
    )


# =========================================================
# DISPLAY SIGNAL
# =========================================================

if signal == "BUY CE":

    signal_box.success(
        f"🟢 BUY ATM CE\n\n"
        f"NIFTY: {nifty_price:,.2f}\n\n"
        f"ATM Strike: {atm_strike}\n\n"
        f"Trade: BUY NIFTY {atm_strike} CE"
    )

elif signal == "BUY PE":

    signal_box.error(
        f"🔴 BUY ATM PE\n\n"
        f"NIFTY: {nifty_price:,.2f}\n\n"
        f"ATM Strike: {atm_strike}\n\n"
        f"Trade: BUY NIFTY {atm_strike} PE"
    )

else:

    signal_box.info(
        "⚪ WAIT — No new trade signal"
    )


# =========================================================
# TRADE DETAILS
# =========================================================

st.divider()

st.subheader("Trade Details")

trade_col1, trade_col2, trade_col3 = st.columns(3)

with trade_col1:
    st.metric(
        "ATM Strike",
        str(atm_strike)
    )

with trade_col2:

    if signal == "BUY CE":
        option_type = "CE"

    elif signal == "BUY PE":
        option_type = "PE"

    else:
        option_type = "-"

    st.metric(
        "Option",
        option_type
    )

with trade_col3:

    if signal != "WAIT":
        trade_status = "SIGNAL GENERATED"
    else:
        trade_status = "WAITING"

    st.metric(
        "Status",
        trade_status
    )


# =========================================================
# DUPLICATE SIGNAL PROTECTION
# =========================================================

if (
    signal != "WAIT"
    and signal != st.session_state.last_signal
):

    st.session_state.last_signal = signal

    st.success(
        f"New signal detected: {signal}"
    )

    # =====================================================
    # IMPORTANT
    # =====================================================
    #
    # This is where your actual SmartAPI order function
    # will be called.
    #
    # Example:
    #
    # if LIVE_TRADING:
    #     place_option_order(
    #         signal=signal,
    #         atm_strike=atm_strike
    #     )
    #
    # =====================================================


# =========================================================
# STOP TRADING
# =========================================================

st.divider()

if st.button(
    "🛑 STOP TRADING",
    type="primary"
):

    st.session_state.running = False
    st.session_state.last_signal = "WAIT"
    st.session_state.atm_strike = None

    st.switch_page("treding.py")
