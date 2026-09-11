# ============================================================
# EXTRACT ORDER ID - ROBUST
# ============================================================

def extract_order_id(response):
    """
    Extract Angel One order ID from the placeOrder response.

    Expected official response:
    {
        "status": True,
        "message": "SUCCESS",
        "data": {
            "orderid": "200910000000111",
            "uniqueorderid": "..."
        }
    }
    """

    if response is None:
        return ""

    # --------------------------------------------------------
    # String response
    # --------------------------------------------------------

    if isinstance(response, str):

        text = response.strip()

        if not text:
            return ""

        # Sometimes a JSON response can arrive as a string
        try:
            parsed = json.loads(text)

            if parsed != response:
                return extract_order_id(parsed)

        except Exception:
            pass

        # Do not blindly treat arbitrary text as order ID.
        # Angel order IDs are normally numeric.
        if text.isdigit():
            return text

        return ""

    # --------------------------------------------------------
    # Dictionary response
    # --------------------------------------------------------

    if isinstance(response, dict):

        # Direct keys
        for key in [
            "orderid",
            "orderId",
            "order_id",
            "orderID",
        ]:

            value = response.get(key)

            if value is not None:

                value = str(value).strip()

                if value:
                    return value

        # Nested data
        data = response.get("data")

        if isinstance(data, dict):

            for key in [
                "orderid",
                "orderId",
                "order_id",
                "orderID",
            ]:

                value = data.get(key)

                if value is not None:

                    value = str(value).strip()

                    if value:
                        return value

        # Some wrappers use response/result/body
        for parent_key in [
            "response",
            "result",
            "body",
        ]:

            nested = response.get(parent_key)

            if nested is not None:

                found = extract_order_id(
                    nested
                )

                if found:
                    return found

    return ""


# ============================================================
# AUTOMATIC BUY CE
# ============================================================

def automatic_buy_ce(
    api,
    option,
):
    """
    Submit ONE real MARKET BUY order.

    IMPORTANT:
    - Calls placeOrder() exactly once.
    - Does NOT call placeOrderFullResponse() afterward.
    - Does NOT retry the order.
    - Does NOT wait for execution.
    - Uses the order ID returned by Angel One.
    """

    if api is None:
        raise RuntimeError(
            "Angel One API session is not available."
        )

    if not option:
        raise RuntimeError(
            "ATM CE option information is missing."
        )

    symbol = str(
        option.get("symbol", "")
    ).strip()

    token = str(
        option.get("token", "")
    ).strip()

    quantity = int(
        option.get("quantity", 0)
    )

    if not symbol:
        raise RuntimeError(
            "ATM CE trading symbol is empty."
        )

    if not token:
        raise RuntimeError(
            f"ATM CE token is empty for {symbol}."
        )

    if quantity <= 0:
        raise RuntimeError(
            f"Invalid quantity: {quantity}"
        )

    # --------------------------------------------------------
    # Angel One order parameters
    # --------------------------------------------------------

    order_params = {
        "variety": VARIETY,
        "tradingsymbol": symbol,
        "symboltoken": token,
        "transactiontype": "BUY",
        "exchange": OPTION_EXCHANGE,
        "ordertype": ORDER_TYPE,
        "producttype": PRODUCT_TYPE,
        "duration": DURATION,
        "quantity": str(quantity),
    }

    # --------------------------------------------------------
    # IMPORTANT
    #
    # placeOrder() is called ONLY ONCE.
    #
    # The official SmartAPI SDK returns:
    #
    # response["data"]["orderid"]
    #
    # on a successful response.
    # --------------------------------------------------------

    try:

        response = api.placeOrder(
            order_params
        )

    except Exception as e:

        raise RuntimeError(
            "Angel One order API exception: "
            f"{type(e).__name__}: {e}"
        )

    # --------------------------------------------------------
    # Extract broker order ID
    # --------------------------------------------------------

    order_id = extract_order_id(
        response
    )

    # --------------------------------------------------------
    # Successful order submission
    # --------------------------------------------------------

    if order_id:

        return order_id, response

    # --------------------------------------------------------
    # No order ID
    #
    # DO NOT RETRY.
    #
    # We don't know whether Angel accepted the order.
    # Retrying could create a duplicate real BUY.
    # --------------------------------------------------------

    if isinstance(response, dict):

        status = response.get(
            "status"
        )

        message = response.get(
            "message"
        )

        errorcode = response.get(
            "errorcode"
        )

        data = response.get(
            "data"
        )

        diagnostic = (
            f"status={status} | "
            f"message={message} | "
            f"errorcode={errorcode} | "
            f"data={data}"
        )

    else:

        diagnostic = (
            f"response_type="
            f"{type(response).__name__} | "
            f"response={response}"
        )

    raise RuntimeError(
        "Angel One did not return an order ID. "
        "NO RETRY WAS SENT to prevent duplicate BUY. | "
        f"{diagnostic}"
    )
