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

    if nifty_price is None:
        return None

    return int(
        math.floor(
            (float(nifty_price) / strike_interval) + 0.5
        ) * strike_interval
    )


def generate_signal(st_5m, st_15m, st_4h, previous_5m):

    # BUY CE
    if (
        previous_5m == -1
        and st_5m == 1
        and st_15m == 1
        and st_4h == 1
    ):
        return "BUY CE"

    # BUY PE
    if (
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
# CHECK TRADING STATUS
# =========================================================

if not st.session_state.running:

    st.warning("⚠️ Trading is not running.")

    if st.button("⬅ Back to Dashboard"):
        st.switch_page("dashboard (1).py")

    st.stop()


st.success("🟢 Trading Engine Running")


# =========================================================
# TEMPORARY TEST DATA
# Replace with Angel One SmartAPI data
# =========================================================

nifty_price = 25183.00

previous_5m = -1

st_5m = 1
st_15m = 1
st_4h = 1


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
    st_5m=st_5m,
    st_15m=st_15m,
    st_4h=st_4h,
    previous_5m=previous_5m
)


# =========================================================
# MARKET INFORMATION
# =========================================================

col1, col2, col3, col4 = st.columns(4)

with col1:
    st.metric(
        "NIFTY",
        f"{nifty_price:,.2f}"
    )

with col2:
    st.metric(
        "5 Minute ST",
        "🟢 GREEN" if st_5m == 1 else "🔴 RED"
    )

with col3:
    st.metric(
        "15 Minute ST",
        "🟢 GREEN" if st_15m == 1 else "🔴 RED"
    )

with col4:
    st.metric(
        "4 Hour ST",
        "🟢 GREEN" if st_4h == 1 else "🔴 RED"
    )


st.divider()


# =========================================================
# CURRENT SIGNAL
# =========================================================

st.subheader("Current Trading Signal")


if signal == "BUY CE":

    st.success(
        f"""
        🟢 **BUY ATM CE**

        NIFTY: **{nifty_price:,.2f}**

        ATM Strike: **{atm_strike}**

        Trade: **BUY NIFTY {atm_strike} CE**
        """
    )


elif signal == "BUY PE":

    st.error(
        f"""
        🔴 **BUY ATM PE**

        NIFTY: **{nifty_price:,.2f}**

        ATM Strike: **{atm_strike}**

        Trade: **BUY NIFTY {atm_strike} PE**
        """
    )


else:

    st.info(
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

    trade_status = (
        "SIGNAL GENERATED"
        if signal != "WAIT"
        else "WAITING"
    )

    st.metric(
        "Status",
        trade_status
    )


# =========================================================
# NEW SIGNAL / DUPLICATE PROTECTION
# =========================================================

if signal != "WAIT":

    if signal != st.session_state.last_signal:

        st.session_state.last_signal = signal

        st.success(
            f"🚨 New Signal Detected: {signal}"
        )

        # ================================================
        # SMARTAPI ORDER WILL GO HERE
        # ================================================
        #
        # Example:
        #
        # if LIVE_TRADING:
        #
        #     place_option_order(
        #         signal=signal,
        #         atm_strike=atm_strike
        #     )
        #
        # ================================================

else:

    # Reset after no signal so a later new signal
    # can be detected correctly.
    st.session_state.last_signal = "WAIT"


# =========================================================
# STRATEGY INFORMATION
# =========================================================

st.divider()

st.subheader("Strategy")

st.write(
    """
    **Supertrend Settings: 20, 2**

    🟢 BUY CE:

    5 Minute Supertrend flips **RED → GREEN**

    AND

    15 Minute Supertrend = **GREEN**

    AND

    4 Hour Supertrend = **GREEN**

    ---

    🔴 BUY PE:

    5 Minute Supertrend flips **GREEN → RED**

    AND

    15 Minute Supertrend = **RED**

    AND

    4 Hour Supertrend = **RED**
    """
)


# =========================================================
# STOP TRADING
# =========================================================

st.divider()


if st.button(
    "🛑 STOP TRADING",
    type="primary",
    use_container_width=True
):

    st.session_state.running = False
    st.session_state.last_signal = "WAIT"
    st.session_state.atm_strike = None

    st.switch_page("dashboard (1).py")
