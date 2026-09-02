import streamlit as st
import time

st.set_page_config(
    page_title="Live Trading",
    page_icon="📈",
    layout="wide"
)

st.title("📈 Live Trading")

# ---------------------------------------------------------
# SESSION STATE
# ---------------------------------------------------------
if "running" not in st.session_state:
    st.session_state.running = False

if "last_signal" not in st.session_state:
    st.session_state.last_signal = "WAIT"


# ---------------------------------------------------------
# CHECK WHETHER TRADING WAS STARTED
# ---------------------------------------------------------
if not st.session_state.running:
    st.warning("Trading is not running.")

    if st.button("⬅ Back to Dashboard"):
        st.switch_page("dashboard.py")

    st.stop()


# ---------------------------------------------------------
# TRADING PAGE
# ---------------------------------------------------------
st.success("🟢 Trading Engine Running")

col1, col2, col3, col4 = st.columns(4)

with col1:
    st.metric("NIFTY", "Loading...")

with col2:
    st.metric("5 Minute ST", "Loading...")

with col3:
    st.metric("15 Minute ST", "Loading...")

with col4:
    st.metric("4 Hour ST", "Loading...")


st.divider()

st.subheader("Current Trading Signal")

signal_box = st.empty()

signal_box.info("Checking market conditions...")


# ---------------------------------------------------------
# PLACE YOUR LIVE DATA / SUPERTREND FUNCTIONS ABOVE
# ---------------------------------------------------------

# Example:
#
# df_5m = get_candle_data("FIVE_MINUTE")
# df_15m = get_candle_data("FIFTEEN_MINUTE")
# df_4h = get_candle_data("FOUR_HOUR")
#
# df_5m = calculate_supertrend(df_5m, 20, 2)
# df_15m = calculate_supertrend(df_15m, 20, 2)
# df_4h = calculate_supertrend(df_4h, 20, 2)


# ---------------------------------------------------------
# EXAMPLE SIGNAL LOGIC
# Replace these values with your real Supertrend values
# ---------------------------------------------------------

st_5m = 1
st_15m = 1
st_4h = 1

previous_5m = -1


if (
    previous_5m == -1
    and st_5m == 1
    and st_15m == 1
    and st_4h == 1
):
    signal = "BUY CE"

elif (
    previous_5m == 1
    and st_5m == -1
    and st_15m == -1
    and st_4h == -1
):
    signal = "BUY PE"

else:
    signal = "WAIT"


# ---------------------------------------------------------
# DISPLAY SIGNAL
# ---------------------------------------------------------

if signal == "BUY CE":
    signal_box.success("🟢 BUY ATM CE")

elif signal == "BUY PE":
    signal_box.error("🔴 BUY ATM PE")

else:
    signal_box.info("⚪ WAIT — No new trade signal")


# ---------------------------------------------------------
# PREVENT DUPLICATE ORDERS
# ---------------------------------------------------------

if signal != "WAIT" and signal != st.session_state.last_signal:

    st.session_state.last_signal = signal

    st.write("New signal detected:", signal)

    # -----------------------------------------
    # CALL YOUR ORDER FUNCTION HERE
    # -----------------------------------------

    # Example:
    #
    # if LIVE_TRADING:
    #     place_option_order(signal)


# ---------------------------------------------------------
# STOP BUTTON
# ---------------------------------------------------------

st.divider()

