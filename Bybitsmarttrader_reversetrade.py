import flask
import requests
import json
import hmac
import hashlib
import time
import logging
from logging.handlers import RotatingFileHandler
from cryptography.fernet import Fernet
from flask import Flask, request, jsonify
import certifi
import ssl
from cachetools import TTLCache
import aiohttp
from botencryptapis import bot_ENCRYPTED_API_KEY, bot_ENCRYPTED_API_SECRET, bot_ENCRYPTION_KEY
import asyncio
from backoff import on_exception, expo
import csv
import os
from quart import Quart, request, jsonify

# Flask app
app = Quart(__name__)

# Base URL for Bybit API
BASE_URL = 'https://api.bybit.com/'

# WebSocket URL for Bybit
WS_URL = "wss://stream.bybit.com/v5/public/spot"

# Decrypt API keys using the Fernet encryption
cipher_suite = Fernet(bot_ENCRYPTION_KEY)
api_key = cipher_suite.decrypt(bot_ENCRYPTED_API_KEY).decode()
api_secret = cipher_suite.decrypt(bot_ENCRYPTED_API_SECRET).decode()

# Create SSL context with certifi to verify SSL certificates
ssl_context = ssl.create_default_context(cafile=certifi.where())

# Cache for precision and other non-critical data (cached for 1 hour)
cache = TTLCache(maxsize=100, ttl=3600)

# Static precision and quantity information for known symbols
static_precision_data = {
    "BTCUSDT": {"price_precision": 2, "qty_precision": 6, "qty_step": 0.000001},
    "ETHUSDT": {"price_precision": 2, "qty_precision": 4, "qty_step": 0.0001},
    "BTCUSDC": {"price_precision": 2, "qty_precision": 6, "qty_step": 0.000001},
    "ETHUSDC": {"price_precision": 2, "qty_precision": 4, "qty_step": 0.0001},
    # Add more symbols as needed
}

# Rate limit constants (adjust these based on Bybit's API limits)
RATE_LIMIT_CALLS = 5  # Max calls per second (example)
RATE_LIMIT_WINDOW = 1  # Window of 1 second

# Logging setup
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
handler = RotatingFileHandler('../bot/trade_executions.log', maxBytes=5000000, backupCount=5)
handler.setLevel(logging.INFO)
logger.addHandler(handler)

# Trade logger for logging successful trade executions
trade_logger = logging.getLogger('trade_execution_logger')
trade_logger.setLevel(logging.INFO)
trade_handler = RotatingFileHandler('../bot/trade_executions.log', maxBytes=5000000, backupCount=5)
trade_handler.setLevel(logging.INFO)
trade_logger.addHandler(trade_handler)


# Function to log trades to CSV
def log_trade_to_csv(symbol, side, quantity, price, tp, sl, leverage, action, trade_type):
    """Logs trade details to a CSV file."""
    log_file = '../bot/trade_log.csv'
    fieldnames = ['Timestamp', 'Symbol', 'Side', 'Quantity', 'Price', 'TP', 'SL', 'Leverage', 'Action', 'Trade Type']

    file_exists = os.path.isfile(log_file)
    with open(log_file, mode='a', newline='') as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        if not file_exists:
            writer.writeheader()  # Write header if the file doesn't exist
        writer.writerow({
            'Timestamp': time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
            'Symbol': symbol,
            'Side': side,
            'Quantity': quantity,
            'Price': price,
            'TP': tp,
            'SL': sl,
            'Leverage': leverage,
            'Action': action,
            'Trade Type': trade_type
        })

# Function to record all trade events in logs
def record_trade_event(symbol, side, quantity, price, tp, sl, leverage, action, trade_type):
    """Logs detailed trade event information."""
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
    trade_logger.info(f"{timestamp} | Action: {action} | Symbol: {symbol} | Side: {side} | Quantity: {quantity} | "
                      f"Price: {price} | TP: {tp} | SL: {sl} | Leverage: {leverage} | Trade Type: {trade_type}")

# Function to record errors and retries
def record_error(symbol, side, error_message):
    """Logs errors and failures."""
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
    trade_logger.error(f"{timestamp} | Error in trade for {symbol} | Side: {side} | Error: {error_message}")

def record_retry(symbol, side, attempt_number):
    """Logs when a trade is retried."""
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
    trade_logger.warning(f"{timestamp} | Retrying trade due to rate limit or error: {symbol} | Side: {side} | Attempt: {attempt_number}")


# Rate limiting using asyncio
async def rate_limit():
    await asyncio.sleep(RATE_LIMIT_WINDOW / RATE_LIMIT_CALLS)

# Helper function to generate API signature
def generate_signature(secret, params):
    param_str = '&'.join([f'{key}={value}' for key, value in sorted(params.items())])
    return hmac.new(secret.encode(), param_str.encode(), hashlib.sha256).hexdigest()

# Retry with exponential backoff for API rate limits and transient errors
@on_exception(expo, (aiohttp.ClientError, asyncio.TimeoutError), max_tries=5)
async def fetch_api_with_backoff(url, params, session):
    try:
        params['sign'] = generate_signature(api_secret, params)
        async with session.get(url, params=params, ssl=ssl_context) as response:
            data = await response.json()
            if response.status == 429:  # Rate limited
                logger.warning(f"Rate limit exceeded. Retrying... {response.status}")
                raise aiohttp.ClientError(f"Rate limit exceeded {data}")
            return data
    except aiohttp.ClientError as e:
        logger.error(f"API error: {e}")
        raise

# Cache invalidation mechanism for precision data
async def invalidate_cache(symbol):
    logger.info(f"Invalidating cache for {symbol}")
    if symbol in static_precision_data:
        del static_precision_data[symbol]

# Fetch precision and prices for the symbol, updating the static dictionary if a new symbol is found
async def fetch_precision_and_prices(symbol, session):
    """Fetches price precision, quantity precision, and bid/ask prices. Uses static precision if available."""

    if symbol in static_precision_data:
        logger.info(f"Using cached precision data for symbol: {symbol}")
        precision_data = static_precision_data[symbol]
        prices_url = f"{BASE_URL}v5/market/tickers?symbol={symbol}&category=spot"

        async with session.get(prices_url, ssl=ssl_context) as prices_response:
            prices_data = await prices_response.json()

            if prices_response.status != 200:
                logger.error(f"Error fetching prices: {prices_response.status}")
                return None

            # Fetch current bid/ask prices
            ticker = prices_data['result']['list'][0]
            bid_price = float(ticker.get('bid1Price'))
            ask_price = float(ticker.get('ask1Price'))

            logger.debug(f"Fetched cached precision and live prices: Bid = {bid_price}, Ask = {ask_price}")
            return precision_data['price_precision'], precision_data['qty_precision'], bid_price, ask_price

    logger.info(f"Fetching precision data for {symbol} from API")
    precision_url = f"{BASE_URL}v5/market/instruments-info?symbol={symbol}&category=spot"
    prices_url = f"{BASE_URL}v5/market/tickers?symbol={symbol}&category=spot"

    try:
        async with session.get(precision_url, ssl=ssl_context) as precision_response, session.get(prices_url,
                                                                                                  ssl=ssl_context) as prices_response:
            precision_data = await precision_response.json()
            prices_data = await prices_response.json()

            if precision_response.status != 200 or prices_response.status != 200:
                logger.error(f"Error fetching data: {precision_response.status}, {prices_response.status}")
                return None

            # Parse instrument data for precision
            instrument = precision_data['result']['list'][0]
            tick_size = instrument['priceFilter']['tickSize']
            qty_step = instrument['lotSizeFilter'].get('qtyStep', '0.0001')

            price_precision = len(tick_size.rstrip('0').split('.')[1]) if '.' in tick_size else 0
            qty_precision = len(qty_step.rstrip('0').split('.')[1]) if '.' in qty_step else 0

            ticker = prices_data['result']['list'][0]
            bid_price = float(ticker.get('bid1Price'))
            ask_price = float(ticker.get('ask1Price'))

            static_precision_data[symbol] = {
                "price_precision": price_precision,
                "qty_precision": qty_precision,
                "qty_step": float(qty_step)
            }

            logger.info(f"Updated static precision data for symbol: {symbol}")
            logger.debug(f"Fetched live precision and prices: Bid = {bid_price}, Ask = {ask_price}")
            return price_precision, qty_precision, bid_price, ask_price

    except Exception as e:
        logger.error(f"Error in fetch_precision_and_prices: {e}")
        return None

# Close the existing position by placing a market order
async def close_existing_position(symbol, side, session):
    """Closes the existing position by placing a market order in the opposite direction."""
    logger.info(f"Closing {side} position for {symbol}")
    close_side = 'Sell' if side == 'Buy' else 'Buy'
    market_order_url = f"{BASE_URL}v5/order/create"

    # Dynamically fetch open position size
    position_size = await get_open_position_size(symbol, close_side, session)
    if not position_size:
        logger.warning(f"No position found to close for {symbol}")
        return  # Exit gracefully if no position found

    # Proceed with closing the position
    params = {
        'category': 'spot',
        'symbol': symbol,
        'side': close_side,
        'orderType': 'Market',
        'qty': str(position_size),
        'api_key': api_key,
        'timestamp': str(int(time.time() * 1000))
    }
    params['sign'] = generate_signature(api_secret, params)

    logger.info(f"API Request: {market_order_url} with params: {params}")  # Log the API request
    async with session.post(market_order_url, json=params, ssl=ssl_context) as response:
        response_data = await response.json()
        logger.info(f"API Response: {response_data}")  # Log the response from the API
        if response.status == 200 and response_data.get('retCode') == 0:
            logger.info(f"Successfully closed {side} position for {symbol}. Response: {response_data}")
        else:
            logger.error(f"Failed to close position. Error: {response_data.get('retMsg', 'No error message')}")


async def handle_reverse_trade(symbol, side, session):
    """Reverses the current position by closing the existing trade and placing a new one in the opposite direction."""
    logger.info(f"Reversing trade for {symbol}, closing {side} position")

    # Cancel active TP/SL orders before reversing the trade
    cancel_result = await cancel_active_orders(symbol, session)
    if cancel_result is None:
        logger.error(f"Failed to cancel active orders for {symbol}, aborting reverse trade")
        return  # Exit early if orders are not canceled

    # Close the current position
    await close_existing_position(symbol, side, session)
    log_trade_to_csv(symbol, side, 0, 0, 0, 0, 'N/A', 'Exit', 'Exit')

    # Determine the reverse side (Sell if current side is Buy, and vice versa)
    reverse_side = "Sell" if side == "Buy" else "Buy"

    logger.info(f"Placing reverse trade for {symbol}, side: {reverse_side}")

    # Place the reverse spot margin order
    await place_spot_margin_order(symbol=symbol, side=reverse_side, session=session)

    record_trade_event(symbol, reverse_side, 0, 0, 0, 0, 1, 'Trade Placed', 'Reverse')
    log_trade_to_csv(symbol, reverse_side, 0, 0, 0, 0, 1, 'Trade Placed', 'Reverse')




# Dynamic quantity based on position size
async def get_open_position_size(symbol, side, session):
    """Fetches the open position size for the given symbol and side (Buy/Sell)."""
    logger.info(f"Fetching open position size for {symbol}")
    position_url = f"{BASE_URL}v5/position/list"

    params = {
        'symbol': symbol,
        'category': 'spot',
        'side': side,
        'api_key': api_key,
        'timestamp': str(int(time.time() * 1000))
    }
    params['sign'] = generate_signature(api_secret, params)

    async with session.get(position_url, params=params, ssl=ssl_context) as response:
        position_data = await response.json()
        if response.status == 200 and position_data.get('retCode') == 0:
            for position in position_data['result']:
                if position['symbol'] == symbol and position['side'] == side.capitalize():
                    position_size = float(position['qty'])
                    logger.info(f"Position size for {symbol}: {position_size}")
                    return position_size
        logger.error(f"Failed to fetch position size for {symbol}")
        return None

# # Close the existing position by placing a market order
# async def close_existing_position(symbol, side, session):
#     """Closes the existing position by placing a market order in the opposite direction."""
#     logger.info(f"Closing {side} position for {symbol}")
#     close_side = 'Sell' if side == 'Buy' else 'Buy'
#     market_order_url = f"{BASE_URL}v5/order/create"
#
#     # Dynamically fetch open position size
#     position_size = await get_open_position_size(symbol, close_side, session)
#     if not position_size:
#         logger.error(f"No position found to close for {symbol}")
#         return
#
#     params = {
#         'category': 'spot',
#         'symbol': symbol,
#         'side': close_side,
#         'orderType': 'Market',  # Close the position using market order
#         'qty': str(position_size),  # Close the full position dynamically
#         'api_key': api_key,
#         'timestamp': str(int(time.time() * 1000))
#     }
#     params['sign'] = generate_signature(api_secret, params)
#
#     async with session.post(market_order_url, json=params, ssl=ssl_context) as response:
#         response_data = await response.json()
#         if response.status == 200 and response_data.get('retCode') == 0:
#             logger.info(f"Successfully closed {side} position for {symbol}. Response: {response_data}")
#         else:
#             logger.error(f"Failed to close position. Error: {response_data.get('retMsg', 'No error message')}")


from backoff import on_exception, expo

@on_exception(expo, (aiohttp.ClientError, asyncio.TimeoutError), max_tries=3)
async def cancel_active_orders(symbol, session):
    """Cancels all active TP/SL orders for the given symbol."""
    logger.info(f"Cancelling active orders for {symbol}")
    cancel_url = f"{BASE_URL}v5/order/cancel-all"
    params = {
        'category': 'spot',
        'symbol': symbol,
        'api_key': api_key,
        'timestamp': str(int(time.time() * 1000))
    }
    params['sign'] = generate_signature(api_secret, params)

    logger.info(f"API Request: {cancel_url} with params: {params}")  # Log the API request
    async with session.post(cancel_url, json=params, ssl=ssl_context) as response:
        response_data = await response.json()
        logger.info(f"API Response: {response_data}")  # Log the response from the API
        if response.status == 200 and response_data.get('retCode') == 0:
            logger.info(f"Cancelled all active orders for {symbol}.")
            record_trade_event(symbol, 'N/A', 0, 0, 0, 0, 0, 'Cancel Orders', 'Cancel')
            log_trade_to_csv(symbol, 'N/A', 0, 0, 0, 0, 0, 'Cancel Orders', 'Cancel')
        else:
            logger.error(f"Failed to cancel orders: {response_data.get('retMsg', 'No error message')}")
            # Add logic here to handle the failure, like retrying or raising an alert




def calculate_tp_sl(price, tp_type, tp_value, sl_type, sl_value, precision, side):
    """Calculates TP and SL prices based on the side (buy/sell), type (percentage/dollar), and provided values."""
    logger.info(
        f"Calculating TP and SL with price: {price}, tp_type: {tp_type}, tp_value: {tp_value}, sl_type: {sl_type}, sl_value: {sl_value}, side: {side}")

    if side.lower() == 'buy':
        # For buy orders, TP is higher than the current price, and SL is lower
        tp_price = round(price * (1 + tp_value / 100), precision) if tp_type == 'percentage' else round(
            price + tp_value, precision)
        sl_price = round(price * (1 - sl_value / 100), precision) if sl_type == 'percentage' else round(
            price - sl_value, precision)
    elif side.lower() == 'sell':
        # For sell orders, TP is lower than the current price, and SL is higher
        tp_price = round(price * (1 - tp_value / 100), precision) if tp_type == 'percentage' else round(
            price - tp_value, precision)
        sl_price = round(price * (1 + sl_value / 100), precision) if sl_type == 'percentage' else round(
            price + sl_value, precision)

    logger.debug(f"Calculated TP: {tp_price}, SL: {sl_price}")
    return tp_price, sl_price



# Dynamic Quantity Calculation Logic
def calculate_quantity(account_balance, leverage, price, qty_precision, qty_step):
    """Calculates the quantity of the asset to buy based on the balance, leverage, and asset price."""
    logger.info(f"Calculating quantity with account_balance: {account_balance}, leverage: {leverage}, price: {price}")
    raw_quantity = (account_balance * leverage) / price
    quantity = (raw_quantity // qty_step) * qty_step
    rounded_quantity = round(quantity, qty_precision)
    logger.debug(f"Calculated quantity: {rounded_quantity}")
    return rounded_quantity

# Fetch the unified account balance for a specific asset asynchronously
async def get_unified_account_balance(asset, session):
    """Fetches the unified account balance for a specific asset."""
    logger.info(f"Fetching unified account balance for asset: {asset}")
    endpoint = "v5/account/wallet-balance"
    url = BASE_URL + endpoint
    params = {'accountType': 'UNIFIED', 'coin': asset, 'api_key': api_key, 'timestamp': str(int(time.time() * 1000))}
    params['sign'] = generate_signature(api_secret, params)

    async with session.get(url, params=params, ssl=ssl_context) as response:
        data = await response.json()
        if response.status == 200 and data.get('retCode') == 0:
            for coin_data in data['result']['list'][0]['coin']:
                if coin_data['coin'] == asset:
                    available_balance = float(coin_data['availableToWithdraw'])
                    logger.info(f"Available balance for {asset}: {available_balance}")
                    return available_balance
        logger.error(f"Error fetching balance for {asset}: {data.get('retMsg', 'No error message')}")
        return None

# Function to log bid and ask prices during trade execution
def record_trade_prices(symbol, bid_price, ask_price, side):
    """Logs the bid and ask prices at the moment of trade execution."""
    try:
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
        trade_logger.info(f"{timestamp} | Action: {side.upper()} | Symbol: {symbol} | Bid: {bid_price} | Ask: {ask_price}")
    except Exception as e:
        logger.error(f"Failed to log trade prices: {e}")


# Place spot margin order with enhanced error handling and rate limiting
@on_exception(expo, (aiohttp.ClientError, asyncio.TimeoutError), max_tries=5)
async def place_spot_margin_order(symbol, side="Buy", price=None, price_option=None, tp_type=None, tp_value=None,
                                  sl_type=None, sl_value=None, leverage=10, tpsl_mode="Full", tp_order_type="Limit",
                                  sl_order_type="Limit", market_unit='baseCoin', session=None):
    """Places a spot margin order without requiring an explicit order_value. Quantity is calculated based on available balance."""
    try:
        logger.info(f"Placing order for {symbol} | Side={side} | Price={price} | Leverage={leverage}")

        # Fetch precision and prices
        precision_result = await fetch_precision_and_prices(symbol, session)
        if not precision_result:
            record_error(symbol, side, "Failed to fetch precision and prices.")
            return

        price_precision, qty_precision, bid_price, ask_price = precision_result
        logger.info(
            f"Fetched precision and prices: Price Precision={price_precision}, Quantity Precision={qty_precision}, Bid={bid_price}, Ask={ask_price}")

        # Determine base price for the limit order
        if price is not None:
            limit_price = round(float(price), price_precision)
            logger.debug(f"Using provided limit price: {limit_price}")
        elif price_option:
            base_price = ask_price if price_option['type'] == 'ask' else bid_price
            limit_price = calculate_limit_price(base_price, price_option['variation_type'], price_option['variation_value'], price_option['direction'], price_precision)
            logger.debug(f"Calculated limit price: {limit_price}")
        else:
            # Fallback to using ask price for buy orders and bid price for sell orders
            limit_price = ask_price if side.lower() == 'buy' else bid_price
            logger.debug(f"No price or price option provided, using market price: {limit_price}")

        # Fetch the unified account balance in USDC
        account_balance = await get_unified_account_balance("USDC", session)
        if account_balance is None or account_balance <= 0:
            record_error(symbol, side, "Failed to fetch account balance or insufficient balance.")
            return  # Exit early if no balance is available

        # Calculate quantity based on account balance, leverage, and price
        quantity = calculate_quantity(account_balance, leverage, limit_price, qty_precision, 0.0001)
        logger.info(f"Calculated quantity: {quantity}")

        # Log the bid and ask price at the moment of placing the order
        record_trade_prices(symbol, bid_price, ask_price, side)

        # Calculate TP and SL based on the side (Buy/Sell)
        tp_price, sl_price = calculate_tp_sl(limit_price, tp_type, tp_value, sl_type, sl_value, price_precision, side)
        logger.info(f"TP: {tp_price}, SL: {sl_price}")

        # Adjust TP trigger price based on side
        if tp_price is not None:
            if side.lower() == 'buy':
                # Make sure the trigger price is lower than the tp_price, for example 1% or 0.5% less
                tp_trigger_price = round(tp_price - 0.1)  #  0.05$ less for buy orders
            elif side.lower() == 'sell':
                # Make sure the trigger price is higher than the tp_price, for example 1% or 0.5% more
                tp_trigger_price = round(tp_price + 0.1)  # 0.05$ more for sell orders
            logger.info(f"Using TP Trigger Price: {tp_trigger_price}")
        else:
            tp_trigger_price = None

        # Add TP and SL limit prices if order type is 'Limit'
        tp_limit_price = tp_price if tp_price is not None and tp_order_type == "Limit" else None
        if tp_limit_price:
            tp_limit_price = round(tp_limit_price)
            logger.info(f"Using TP Limit Price: {tp_limit_price}")

        sl_limit_price = sl_price if sl_price is not None and sl_order_type == "Limit" else None
        if sl_limit_price:
            sl_limit_price = round(sl_limit_price, price_precision)
            logger.info(f"Using SL Limit Price: {sl_limit_price}")

        # Prepare the order parameters
        params = {
            'category': 'spot',
            'symbol': symbol,
            'side': side.capitalize(),
            'orderType': 'Limit',
            'qty': str(quantity),
            'price': str(round(limit_price, price_precision)),
            'timeInForce': 'GTC',
            'isLeverage': 1,
            'leverage': str(leverage),
            'marketUnit': market_unit,
            'tpslMode': tpsl_mode,
            'api_key': api_key,
            'timestamp': str(int(time.time() * 1000))
        }

        # Add TP parameters if applicable
        if tp_price is not None:
            params['takeProfit'] = str(tp_price)
            params['tpTriggerPrice'] = str(tp_trigger_price)  # Set TP trigger price
            params['tpOrderType'] = tp_order_type
            if tp_limit_price:
                params['tpLimitPrice'] = str(tp_limit_price)  # The limit price for take profit

        # Add SL as Market Order
        if sl_price is not None:
            params['stopLoss'] = str(sl_price)
            params['slOrderType'] = 'Market'  # Set SL to be a market order
            logger.info(f"Using SL Market Price Trigger: {sl_price}")

        # Generate API signature
        params['sign'] = generate_signature(api_secret, params)

        # Rate limiting before API call
        await rate_limit()

        # Send the request to place the order
        url = f"{BASE_URL}v5/order/create"
        headers = {'Content-Type': 'application/json'}
        logger.info(f"Sending order with params: {params}")

        async with session.post(url, json=params, ssl=ssl_context, headers=headers) as response:
            response_data = await response.json()
            logger.debug(f"Order placement response: {response_data}")

            if response.status == 200 and response_data.get('retCode') == 0:
                record_trade_event(symbol, side, quantity, limit_price, tp_price, sl_price, leverage, 'Trade Placed',
                                   'Entry')
                log_trade_to_csv(symbol, side, quantity, limit_price, tp_price, sl_price, leverage, 'Trade Placed',
                                 'Entry')

                logger.info(f"Spot Margin Order Placed Successfully: {response_data}")
                trade_logger.info(
                    f"Trade executed: {symbol} | {side} | Qty: {quantity} | Limit Price: {limit_price} | TP: {tp_price} | SL: {sl_price} | Margin: {'Yes' if leverage > 1 else 'No'}")
            else:
                logger.error(f"Failed to place order. Error: {response_data.get('retMsg', 'No error message')}")
                record_error(symbol, side, response_data.get('retMsg', 'Unknown error'))


    except Exception as e:
        logger.error(f"Error placing spot margin order: {e}")
        record_error(symbol, side, str(e))


# Check the status of a placed order
async def check_order_status(symbol, order_id, session):
    """Checks the status of a placed order and logs the result."""
    try:
        url = f"{BASE_URL}v5/order/realtime"
        params = {
            'category': 'spot',
            'symbol': symbol,
            'orderId': order_id,
            'api_key': api_key,
            'timestamp': str(int(time.time() * 1000))
        }
        params['sign'] = generate_signature(api_secret, params)

        await rate_limit()

        async with session.get(url, params=params, ssl=ssl_context) as response:
            response_data = await response.json()

            if response.status == 200 and response_data.get('retCode') == 0:
                order_status = response_data['result']['orderStatus']
                trade_logger.info(f"Order {order_id} for {symbol}: Status = {order_status}")
                return order_status
            else:
                logger.error(f"Failed to fetch order status: {response_data.get('retMsg', 'No error message')}")
                return None

    except Exception as e:
        logger.error(f"Error checking order status: {e}")
        return None

def calculate_limit_price(base_price, variation_type, variation_value, direction, precision):
    """Calculates the limit price based on variation type and value."""
    logger.info(f"Calculating limit price with base price: {base_price}, variation type: {variation_type}, "
                f"variation value: {variation_value}, direction: {direction}")

    if direction is None:
        logger.info(f"Direction is None, returning base price as limit price: {base_price}")
        return round(base_price, precision)

    if variation_type == 'percentage':
        if direction == 'increase':
            limit_price = base_price * (1 + variation_value / 100)
        elif direction == 'decrease':
            limit_price = base_price * (1 - variation_value / 100)
        else:
            raise ValueError("Direction must be 'increase' or 'decrease'")
    elif variation_type == 'dollar':
        if direction == 'increase':
            limit_price = base_price + variation_value
        elif direction == 'decrease':
            limit_price = base_price - variation_value
        else:
            raise ValueError("Direction must be 'increase' or 'decrease'")
    else:
        raise ValueError("Variation type must be 'percentage' or 'dollar'")

    return round(limit_price, precision)

@app.route('/', methods=['POST'])
async def receive_payload():
    """Handles incoming TradingView alerts and manages orders."""
    try:
        data_payload = await request.get_json()
        logger.info(f"Received webhook payload: {data_payload}")

        action = data_payload.get('action')
        symbol = data_payload.get('symbol')
        leverage = data_payload.get('leverage', 1)
        tp_type = data_payload.get('tp_type', 'dollar')
        tp_value = data_payload.get('tp_value', 0)
        sl_type = data_payload.get('sl_type', 'dollar')
        sl_value = data_payload.get('sl_value', 0)

        if not action or not symbol:
            return jsonify({'status': 'error', 'message': 'Missing required fields'}), 400

        logger.info(f"Action: {action}, Symbol: {symbol}, Leverage: {leverage}, TP Type: {tp_type}, TP Value: {tp_value}, SL Type: {sl_type}, SL Value: {sl_value}")

        async with aiohttp.ClientSession() as session:
            # Fetch the current position (Buy/Sell) for the symbol
            current_position = await get_open_position_size(symbol, action.capitalize(), session)

            if current_position:
                # If there's an existing position, reverse the trade
                await handle_reverse_trade(symbol, action.capitalize(), session)
            else:
                # If no existing position, place a new order
                await place_spot_margin_order(
                    symbol=symbol,
                    side=action.capitalize(),
                    tp_type=tp_type,
                    tp_value=tp_value,
                    sl_type=sl_type,
                    sl_value=sl_value,
                    leverage=leverage,
                    session=session
                )

        return jsonify({'status': 'success', 'message': 'Payload received and order is being processed'}), 200

    except Exception as e:
        logger.error(f"An error occurred while processing the payload: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 400



if __name__ == '__main__':
    import hypercorn.asyncio
    from hypercorn.config import Config

    config = Config()
    config.bind = ["0.0.0.0:5003"]
    asyncio.run(hypercorn.asyncio.serve(app, config))


