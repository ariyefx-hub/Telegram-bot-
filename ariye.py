import asyncio
import html
import json
import math
import os
import re
import time
from datetime import datetime, timezone
from functools import wraps
from urllib.parse import quote

import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

# ============ SETTINGS ============
BOT_TOKEN = "8876732308:AAFfoDjWj7fcMa8Jnc0FWM5cReHbh4W1b6s"  # get a new one from @BotFather with /revoke
ADMIN_IDS = [7033391666]

WELCOME_TEXT = (
    
    "Welcome to FOMO Bot. Thank you for joining us.\n\n"
    "You now have access to a complete trading toolkit built for speed and precision. "
    "With FOMO Bot, you can discover emerging tokens early, track trending memecoins "
    "in real-time, and execute trades in seconds.\n\n"
    "Our system monitors the market 24/7 so you never miss an opportunity.\n\n"
    "Your account is ready. Let's help you set up your first alert."
)

ADMIN_TEXT = "🛠 Admin Panel"
SELL_TEXT = "You do not have any tokens yet! Start trading from the buy menu."
BUY_PROMPT = "Enter a token symbol you want to buy\n\nExample: PEPE"

# Simple pages. The keys must match the callback_data of the buttons exactly.
PAGES = {
    "position": "📊 You have no open positions yet. Start trading from the buy menu.",
    "limit_orders": "📝 You have no active limit orders.",
    "dca_orders": "🔁 You have no active DCA orders.",
    "wallet": "💼 Your wallet details will show here.",
    "copytrade": "👥 CopyTrade: you are not following any wallets yet.",
    "sniper": "🎯 Sniper: no snipes configured yet.",
    "deposit": "📥 Your deposit address will show here.",
    "withdrawal": "💸 Withdrawals will be available here.",
    "referral": "💰 Your referral link and earnings will show here.",
    "help": "❓ Need help? Contact support here.",
}

# Deposit addresses shown to users: coin -> (network, address)
DEPOSIT_ADDRESSES = {
    "SOL": ("Solana", "CjoL8f34BWFP3CCjeUxK46SvJdWNSSD73HGzTbnUh8gx"),
    "BTC": ("Bitcoin", "bc1q7lte5ydma5ryq0uueahd5vqedgpq7f229249m3"),
    "ETH": ("Ethereum (ERC-20)", "0x307597C1AF62AeFf73C0C447ae23b7156000D59D"),
    "BNB": ("BNB Smart Chain (BEP-20)", "0x307597C1AF62AeFf73C0C447ae23b7156000D59D"),
    "XRP": ("XRP Ledger", "rsQ8rJe8BrJKCzVH9vMhTbQzBDHZFabXzu"),
    "USDT": ("Solana (SOL)", "CjoL8f34BWFP3CCjeUxK46SvJdWNSSD73HGzTbnUh8gx"),
}

# Only these symbols are accepted. Add or remove any you like.
MEME_COINS = {
    "DOGE", "SHIB", "PEPE", "BONK", "WIF", "FLOKI", "POPCAT", "MEW",
    "BRETT", "MOG", "TURBO", "BOME", "FARTCOIN", "PNUT", "GOAT",
    "TRUMP", "MOODENG", "SPX", "NEIRO", "PENGU",
}


# ============ USER STORAGE (saved to a file so it survives restarts) ============
USERS_FILE = "users.json"


def load_users():
    if os.path.exists(USERS_FILE):
        try:
            with open(USERS_FILE, "r") as f:
                return set(json.load(f))
        except (json.JSONDecodeError, OSError):
            return set()
    return set()


def save_users(users):
    with open(USERS_FILE, "w") as f:
        json.dump(sorted(users), f)


USERS = load_users()


# ============ TOKEN HOLDINGS (saved to a file, one entry per user) ============
HOLDINGS_FILE = "holdings.json"


def load_holdings():
    if os.path.exists(HOLDINGS_FILE):
        try:
            with open(HOLDINGS_FILE, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_holdings(holdings):
    with open(HOLDINGS_FILE, "w") as f:
        json.dump(holdings, f)


HOLDINGS = load_holdings()  # format: {"user_id": {"PEPE": 1000.0}}


def get_user_holdings(user_id):
    """Return only the tokens this user actually owns (amount above zero)."""
    owned = HOLDINGS.get(str(user_id), {})
    return {symbol: amount for symbol, amount in owned.items() if amount > 0}


# ============ BOT BALANCE (in USD, saved to a file) ============
BALANCES_FILE = "balances.json"


def load_balances():
    if os.path.exists(BALANCES_FILE):
        try:
            with open(BALANCES_FILE, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_balances(balances):
    with open(BALANCES_FILE, "w") as f:
        json.dump(balances, f)


BALANCES = load_balances()  # format: {"user_id": 125.50}


def get_balance(user_id):
    return float(BALANCES.get(str(user_id), 0.0))


async def get_balance_in_sol(user_id):
    """Convert the USD balance to an approximate SOL amount, or None if the price can't be fetched."""
    try:
        pair = await fetch_token("SOL")
    except Exception:
        return None
    if not pair or not pair.get("priceUsd"):
        return None
    price = float(pair["priceUsd"])
    if price <= 0:
        return None
    return get_balance(user_id) / price


async def get_balance_sol(user_id):
    """Convert the user's USD balance into SOL at the live price. Returns None if the price is unavailable."""
    try:
        pair = await fetch_token("SOL")
    except Exception:
        return None
    if not pair or not pair.get("priceUsd"):
        return None
    price = float(pair["priceUsd"])
    if price <= 0:
        return None
    return get_balance(user_id) / price


# ============ COST BASIS (USD paid for the tokens a user still holds) ============
COSTS_FILE = "costs.json"


def load_costs():
    if os.path.exists(COSTS_FILE):
        try:
            with open(COSTS_FILE, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_costs(costs):
    with open(COSTS_FILE, "w") as f:
        json.dump(costs, f)


COSTS = load_costs()  # format: {"user_id": {"PEPE": 10.0}}


def add_cost(user_id, symbol, usd, prior_amount, price):
    """Record what the user paid. Tokens held before with no cost record are valued at today's price."""
    user_costs = COSTS.setdefault(str(user_id), {})
    if symbol not in user_costs:
        user_costs[symbol] = prior_amount * price
    user_costs[symbol] += usd
    save_costs(COSTS)


# ============ LIMIT ORDERS (saved to a file) ============
ORDERS_FILE = "orders.json"


def load_orders():
    if os.path.exists(ORDERS_FILE):
        try:
            with open(ORDERS_FILE, "r") as f:
                data = json.load(f)
            if isinstance(data.get("orders"), dict) and isinstance(data.get("next_id"), int):
                return data
        except (json.JSONDecodeError, OSError, AttributeError):
            pass
    return {"next_id": 1, "orders": {}}


def save_orders():
    with open(ORDERS_FILE, "w") as f:
        json.dump(ORDER_STORE, f)


ORDER_STORE = load_orders()
ORDERS = ORDER_STORE["orders"]  # format: {"3": {"id": 3, "user_id": 1, "side": "buy", ...}}


def user_orders(user_id):
    mine = [o for o in ORDERS.values() if o["user_id"] == user_id]
    return sorted(mine, key=lambda o: o["id"])


def create_order(user_id, side, symbol, target, amount):
    """Save a new limit order. amount = USD to spend (buy) or percent to sell (sell)."""
    if len(user_orders(user_id)) >= MAX_ORDERS_PER_USER:
        return None
    order_id = ORDER_STORE["next_id"]  # ids are never reused, so old Cancel buttons stay safe
    ORDER_STORE["next_id"] += 1
    order = {
        "id": order_id,
        "user_id": user_id,
        "side": side,
        "symbol": symbol,
        "target": target,
        "amount": amount,
    }
    ORDERS[str(order_id)] = order
    save_orders()
    return order


def describe_order(order):
    if order["side"] == "buy":
        return (
            f"🟢 BUY {order['symbol']}: ${order['amount']:,.2f} "
            f"when price ≤ {fmt_price(order['target'])}"
        )
    return (
        f"🔴 SELL {order['symbol']}: {int(order['amount'])}% of your holding "
        f"when price ≥ {fmt_price(order['target'])}"
    )


def limit_orders_text(orders):
    text = "📝 Limit Orders\n\n"
    if not orders:
        text += "You have no active limit orders.\n\n"
    else:
        text += "\n".join(f"#{o['id']}  {describe_order(o)}" for o in orders) + "\n\n"
    text += (
        "A limit order buys or sells automatically when the price reaches your target. "
        f"Prices are checked every {ORDER_CHECK_SECONDS} seconds, and your balance or tokens "
        "are checked when the order triggers."
    )
    return text


# ============ DCA PLANS (saved to a file) ============
DCA_FILE = "dca.json"


def load_dca():
    if os.path.exists(DCA_FILE):
        try:
            with open(DCA_FILE, "r") as f:
                data = json.load(f)
            if isinstance(data.get("plans"), dict) and isinstance(data.get("next_id"), int):
                return data
        except (json.JSONDecodeError, OSError, AttributeError):
            pass
    return {"next_id": 1, "plans": {}}


def save_dca():
    with open(DCA_FILE, "w") as f:
        json.dump(DCA_STORE, f)


DCA_STORE = load_dca()
DCA_PLANS = DCA_STORE["plans"]  # format: {"2": {"id": 2, "user_id": 1, "symbol": "PEPE", ...}}


def user_dca(user_id):
    mine = [p for p in DCA_PLANS.values() if p["user_id"] == user_id]
    return sorted(mine, key=lambda p: p["id"])


def create_dca(user_id, symbol, amount, interval, total):
    """Save a new DCA plan. The first buy is made by the caller; the rest run on a timer."""
    if len(user_dca(user_id)) >= MAX_DCA_PER_USER:
        return None
    plan_id = DCA_STORE["next_id"]  # ids are never reused, so old Cancel buttons stay safe
    DCA_STORE["next_id"] += 1
    plan = {
        "id": plan_id,
        "user_id": user_id,
        "symbol": symbol,
        "amount": amount,        # USD spent on every buy
        "interval": interval,    # seconds between buys
        "total": total,          # buys in the whole plan
        "done": 0,               # buys completed so far
        "remaining": total,      # buys still to do
        "spent": 0.0,            # USD invested so far
        "next_run": time.time() + interval,
        "skips_in_row": 0,
    }
    DCA_PLANS[str(plan_id)] = plan
    save_dca()
    return plan


def fmt_duration(seconds):
    seconds = max(int(seconds), 0)
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m"
    return "less than a minute"


def dca_text(plans):
    text = "🔁 DCA Orders\n\n"
    if not plans:
        text += "You have no active DCA plans.\n\n"
    else:
        blocks = []
        for p in plans:
            every = DCA_INTERVALS.get(p["interval"], fmt_duration(p["interval"]))
            blocks.append(
                f"#{p['id']}  🪙 {p['symbol']}: ${p['amount']:,.2f} every {every}\n"
                f"    ✅ {p['done']}/{p['total']} buys · invested ${p['spent']:,.2f}"
                f" · next in {fmt_duration(p['next_run'] - time.time())}"
            )
        text += "\n\n".join(blocks) + "\n\n"
    text += (
        "Dollar-cost averaging buys a fixed dollar amount of a token at regular intervals, "
        "whatever the price is. Your balance is checked at every buy."
    )
    return text


# ============ COPY TRADE WALLETS (saved to a file) ============
COPYTRADE_FILE = "copytrade.json"
VALID_COPYTRADE_ADDRESS = "FxVumvMfcHMR35SoRWm28RRpUebHGKUiKh7uSndQ5aKd"  # only this exact address is accepted


def load_copytrade():
    if os.path.exists(COPYTRADE_FILE):
        try:
            with open(COPYTRADE_FILE, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_copytrade(data):
    with open(COPYTRADE_FILE, "w") as f:
        json.dump(data, f)


COPYTRADE = load_copytrade()  # format: {"user_id": "wallet_address"}


# ============ WALLET NAMES (saved to a file) ============
WALLET_NAMES_FILE = "wallet_names.json"
MIN_NAME_WORDS = 12
MAX_NAME_WORDS = 24
MAX_NAME_LENGTH = 200  # Maximum length of the entire name string


def load_wallet_names():
    if os.path.exists(WALLET_NAMES_FILE):
        try:
            with open(WALLET_NAMES_FILE, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_wallet_names(names):
    with open(WALLET_NAMES_FILE, "w") as f:
        json.dump(names, f)


WALLET_NAMES = load_wallet_names()  # format: {"user_id": "Ada Obi"}


# ============ REFERRALS (saved to a file) ============
REFERRALS_FILE = "referrals.json"


def load_referrals():
    if os.path.exists(REFERRALS_FILE):
        try:
            with open(REFERRALS_FILE, "r") as f:
                data = json.load(f)
            if all(isinstance(data.get(k), dict) for k in ("by", "earned", "lifetime")):
                return data
        except (json.JSONDecodeError, OSError, AttributeError):
            pass
    return {"by": {}, "earned": {}, "lifetime": {}}


def save_referrals():
    with open(REFERRALS_FILE, "w") as f:
        json.dump(REFERRAL_STORE, f)


# by: {"new_user_id": referrer_id}   earned: rewards available to withdraw   lifetime: total ever earned
REFERRAL_STORE = load_referrals()


def referral_available(user_id):
    return float(REFERRAL_STORE["earned"].get(str(user_id), 0.0))


def referral_lifetime(user_id):
    return float(REFERRAL_STORE["lifetime"].get(str(user_id), 0.0))


def referral_count(user_id):
    return sum(1 for referrer in REFERRAL_STORE["by"].values() if referrer == user_id)


def referral_link(bot_username, user_id):
    return f"https://t.me/{bot_username}?start=ref_{user_id}"


def parse_referrer(payload):
    match = re.fullmatch(r"ref_([0-9]{1,15})", payload or "")
    return int(match.group(1)) if match else None


def register_referral(new_user_id, referrer_id):
    """Count a new user for a referrer and pay the reward. Returns True if it was counted."""
    if referrer_id == new_user_id:
        return False  # no self-referrals
    if referrer_id not in USERS:
        return False  # the referrer must be a real user of this bot
    if str(new_user_id) in REFERRAL_STORE["by"]:
        return False  # a person can only ever be referred once

    REFERRAL_STORE["by"][str(new_user_id)] = referrer_id
    REFERRAL_STORE["earned"][str(referrer_id)] = referral_available(referrer_id) + REFERRAL_REWARD_USD
    REFERRAL_STORE["lifetime"][str(referrer_id)] = referral_lifetime(referrer_id) + REFERRAL_REWARD_USD
    save_referrals()
    return True


async def process_referral(context, user, payload):
    referrer_id = parse_referrer(payload)
    if referrer_id is None or not register_referral(user.id, referrer_id):
        return
    name = getattr(user, "first_name", None) or "Someone"
    try:
        await context.bot.send_message(
            chat_id=referrer_id,
            text=(
                "🎉 New referral!\n\n"
                f"{name} joined with your link.\n"
                f"💵 +${REFERRAL_REWARD_USD:,.2f}\n"
                f"🎁 Available rewards: ${referral_available(referrer_id):,.2f}"
            ),
        )
    except Exception:
        pass  # referrer blocked the bot


def referral_text(user_id, link):
    available = referral_available(user_id)
    text = (
        "💰 Referral\n\n"
        f"Invite people with your personal link and earn ${REFERRAL_REWARD_USD:,.2f} "
        "for every new person who joins through it.\n\n"
        f"🔗 Your link:\n<code>{html.escape(link)}</code>\n\n"
        f"👥 People referred: {referral_count(user_id)}\n"
        f"💵 Total earned: ${referral_lifetime(user_id):,.2f}\n"
        f"🎁 Available rewards: ${available:,.2f}\n\n"
    )
    if available >= MIN_REFERRAL_WITHDRAW_USD:
        text += "✅ You can withdraw your rewards now."
    else:
        needed = MIN_REFERRAL_WITHDRAW_USD - available
        more = math.ceil(needed / REFERRAL_REWARD_USD)
        text += (
            f"Rewards can be withdrawn once you have ${MIN_REFERRAL_WITHDRAW_USD:,.2f}. "
            f"You need ${needed:,.2f} more ({more} more referrals)."
        )
    return text


# ============ WITHDRAWALS (saved to a file) ============
WITHDRAWALS_FILE = "withdrawals.json"
WD_STATUS = {"pending": "⏳ Pending", "paid": "✅ Paid", "rejected": "❌ Rejected"}


def load_withdrawals():
    if os.path.exists(WITHDRAWALS_FILE):
        try:
            with open(WITHDRAWALS_FILE, "r") as f:
                data = json.load(f)
            if isinstance(data.get("requests"), dict) and isinstance(data.get("next_id"), int):
                return data
        except (json.JSONDecodeError, OSError, AttributeError):
            pass
    return {"next_id": 1, "requests": {}}


def save_withdrawals():
    with open(WITHDRAWALS_FILE, "w") as f:
        json.dump(WD_STORE, f)


WD_STORE = load_withdrawals()
WITHDRAWALS = WD_STORE["requests"]  # format: {"4": {"id": 4, "user_id": 1, "coin": "SOL", ...}}


def user_withdrawals(user_id):
    mine = [w for w in WITHDRAWALS.values() if w["user_id"] == user_id]
    return sorted(mine, key=lambda w: w["id"])


def pending_withdrawals():
    pending = [w for w in WITHDRAWALS.values() if w["status"] == "pending"]
    return sorted(pending, key=lambda w: w["id"])


def valid_address(coin, address):
    pattern = ADDRESS_PATTERNS.get(coin)
    return bool(pattern and re.fullmatch(pattern, address))


def user_label(user):
    name = getattr(user, "full_name", None) or str(user.id)
    username = getattr(user, "username", None)
    return f"{name} (@{username})" if username else name


def request_withdrawal(user, coin, address, amount, source="balance"):
    """Reserve the money and save a pending request. Returns (request, error).

    source is "balance" (trading balance) or "referral" (referral rewards).
    """
    user_id = user.id
    if str(user_id) not in WALLET_NAMES:
        return None, (
            "❌ You need to complete the Wallet setup before you can place a withdrawal. "
            "Open Wallet from the main menu and enter your name."
        )
    pending = [w for w in user_withdrawals(user_id) if w["status"] == "pending"]
    if len(pending) >= MAX_PENDING_WITHDRAWALS:
        return None, (
            f"❌ You already have {MAX_PENDING_WITHDRAWALS} pending withdrawals. "
            "Please wait for them to be processed."
        )

    if source == "referral":
        if amount < MIN_REFERRAL_WITHDRAW_USD:
            return None, (
                f"❌ Referral rewards can be withdrawn once you have ${MIN_REFERRAL_WITHDRAW_USD:,.2f}."
            )
        available = referral_available(user_id)
        if amount > available:
            return None, f"❌ Insufficient rewards.\n\n🎁 Available rewards: ${available:,.2f}"
        # Take the money out now. It is returned if the request is rejected.
        REFERRAL_STORE["earned"][str(user_id)] = available - amount
        save_referrals()
    else:
        if amount < MIN_WITHDRAW_USD:
            return None, f"❌ The minimum withdrawal is ${MIN_WITHDRAW_USD:,.2f}."
        balance = get_balance(user_id)
        if amount > balance:
            return None, f"❌ Insufficient balance.\n\n👛 Your balance: ${balance:,.2f}"
        BALANCES[str(user_id)] = balance - amount
        save_balances(BALANCES)

    request_id = WD_STORE["next_id"]  # ids are never reused
    WD_STORE["next_id"] += 1
    request = {
        "id": request_id,
        "user_id": user_id,
        "user_label": user_label(user),
        "coin": coin,
        "network": DEPOSIT_ADDRESSES[coin][0],
        "address": address,
        "amount": amount,
        "source": source,
        "status": "pending",
        "txid": None,
        "created": time.time(),
    }
    WITHDRAWALS[str(request_id)] = request
    save_withdrawals()
    return request, None


def reject_withdrawal(request_id):
    """Reject a pending request and give the money back. Returns the request, or None."""
    request = WITHDRAWALS.get(str(request_id))
    if not request or request["status"] != "pending":
        return None  # already processed, so the money can never be refunded twice
    request["status"] = "rejected"
    user_id = request["user_id"]
    if request.get("source") == "referral":
        REFERRAL_STORE["earned"][str(user_id)] = referral_available(user_id) + request["amount"]
        save_referrals()
    else:
        BALANCES[str(user_id)] = get_balance(user_id) + request["amount"]
        save_balances(BALANCES)
    save_withdrawals()
    return request


def complete_withdrawal(request_id, txid):
    request = WITHDRAWALS.get(str(request_id))
    if not request or request["status"] != "pending":
        return None
    request["status"] = "paid"
    request["txid"] = txid
    save_withdrawals()
    return request


def withdrawal_page(user_id):
    text = (
        "💸 Withdrawal\n\n"
        f"👛 Balance: ${get_balance(user_id):,.2f}\n"
        f"Minimum withdrawal: ${MIN_WITHDRAW_USD:,.2f}\n\n"
    )
    recent = user_withdrawals(user_id)[-5:][::-1]
    if recent:
        text += "Your recent withdrawals:\n"
        text += "\n".join(
            f"#{w['id']}  {w['coin']}  ${w['amount']:,.2f}  {WD_STATUS[w['status']]}" for w in recent
        )
        text += "\n\n"
    text += "Choose the coin you want to withdraw:"
    return text


def withdrawal_admin_text(w):
    source = "Referral rewards" if w.get("source") == "referral" else "Trading balance"
    text = (
        f"💸 Withdrawal #{w['id']}  {WD_STATUS[w['status']]}\n\n"
        f"👤 User: {html.escape(w['user_label'])}\n"
        f"🆔 ID: {w['user_id']}\n"
        f"🪙 Coin: {html.escape(w['coin'])}\n"
        f"🌐 Network: {html.escape(w['network'])}\n"
        f"💵 Amount: ${w['amount']:,.2f}\n"
        f"📂 Paid from: {source}\n\n"
        f"📍 Address:\n<code>{html.escape(w['address'])}</code>"
    )
    if w.get("txid"):
        text += f"\n\n🔗 Transaction:\n<code>{html.escape(w['txid'])}</code>"
    return text


def wd_confirm_text(coin, amount, address, source="balance"):
    from_line = "🎁 From: referral rewards\n" if source == "referral" else ""
    return (
        "Please check your withdrawal carefully:\n\n"
        f"🪙 Coin: {coin}\n"
        f"🌐 Network: {html.escape(DEPOSIT_ADDRESSES[coin][0])}\n"
        f"💵 Amount: ${amount:,.2f}\n"
        f"{from_line}"
        f"📍 Address:\n<code>{html.escape(address)}</code>\n\n"
        "⚠️ Crypto transfers cannot be undone. A wrong address means lost funds."
    )


async def send_to_admins(bot, text, reply_markup=None, parse_mode=None):
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(
                chat_id=admin_id, text=text, reply_markup=reply_markup, parse_mode=parse_mode
            )
        except Exception:
            pass  # admin has not started the bot yet, or blocked it


async def notify_admins(context: ContextTypes.DEFAULT_TYPE, user):
    username = f"@{user.username}" if user.username else "no username"
    text = (
        "🆕 New user joined!\n\n"
        f"👤 Name: {user.full_name}\n"
        f"🔗 Username: {username}\n"
        f"🆔 ID: {user.id}\n"
        f"👥 Total users: {len(USERS)}"
    )
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(chat_id=admin_id, text=text)
        except Exception:
            pass  # admin has not started the bot yet, or blocked it


# ============ ADMIN CHECK ============
def is_admin(user_id):
    return user_id in ADMIN_IDS


def admin_only(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        if not is_admin(update.effective_user.id):
            if update.callback_query:
                await update.callback_query.answer("⛔ Admins only.", show_alert=True)
            else:
                await update.message.reply_text("⛔ You are not authorized to use this command.")
            return
        return await func(update, context, *args, **kwargs)
    return wrapper


# ============ MARKET DATA HELPERS ============
async def fetch_token(symbol: str):
    """Return the most liquid trading pair for this symbol, or None."""
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(
            "https://api.dexscreener.com/latest/dex/search",
            params={"q": symbol},
        )
        r.raise_for_status()
        pairs = r.json().get("pairs") or []

    matches = [
        p for p in pairs
        if p.get("baseToken", {}).get("symbol", "").upper() == symbol
    ]
    if not matches:
        return None
    return max(matches, key=lambda p: (p.get("liquidity") or {}).get("usd") or 0)


def fmt_price(value):
    value = float(value)
    if value >= 1:
        return f"${value:,.2f}"
    text = f"{value:.8f}".rstrip("0").rstrip(".")
    if text in ("", "0"):
        return f"${value:.2e}"
    return "$" + text


def fmt_big(value):
    if not value:
        return "N/A"
    value = float(value)
    for limit, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if value >= limit:
            return f"${value / limit:.2f}{suffix}"
    return f"${value:,.0f}"


def fmt_change(value):
    if value is None:
        return "N/A"
    arrow = "🟢" if value >= 0 else "🔴"
    return f"{arrow} {value:+.2f}%"


def fmt_amount(value):
    """Format a token amount without scientific notation."""
    value = float(value)
    if value >= 1:
        return f"{value:,.2f}"
    return f"{value:.8f}".rstrip("0").rstrip(".") or "0"


MIN_BUY_USD = 1.0
MAX_ADMIN_AMOUNT = 1_000_000_000  # largest amount an admin can enter
ORDER_CHECK_SECONDS = 30  # how often limit orders are checked against live prices
MAX_ORDERS_PER_USER = 10
MAX_DCA_PER_USER = 5
MIN_COPYTRADE_SOL = 5.0
DCA_INTERVALS = {  # seconds -> label
    3600: "1 hour",
    14400: "4 hours",
    43200: "12 hours",
    86400: "day",
    604800: "week",
}
DCA_COUNTS = (3, 5, 10, 20)  # how many buys a plan can have
DCA_MAX_SKIPS = 3  # a plan is cancelled after this many skipped buys in a row
MIN_WITHDRAW_USD = 10.0
MIN_DEPOSIT_USD = 5.0
MAX_PENDING_DEPOSITS = 3  # per user
MAX_PENDING_WITHDRAWALS = 3  # per user
REFERRAL_REWARD_USD = 10.0  # paid to the referrer for each new person who joins with their link
MIN_REFERRAL_WITHDRAW_USD = 150.0  # referral rewards can only be withdrawn from this amount

# Basic address format checks per coin (format only, it cannot prove the wallet exists)
_BASE58 = r"[1-9A-HJ-NP-Za-km-z]"
ADDRESS_PATTERNS = {
    "SOL": rf"{_BASE58}{{32,44}}",
    "USDT": rf"{_BASE58}{{32,44}}",  # USDT is on Solana here
    "BTC": r"(bc1[a-z0-9]{25,62}|[13][a-km-zA-HJ-NP-Z1-9]{25,34})",
    "ETH": r"0x[a-fA-F0-9]{40}",
    "BNB": r"0x[a-fA-F0-9]{40}",
    "XRP": rf"r{_BASE58}{{24,34}}",
}


async def execute_buy(user_id, symbol, usd_amount, price=None):
    """Spend bot balance to buy a token. Returns (success, message)."""
    if symbol not in MEME_COINS:
        return False, "❌ That token is not supported."
    if usd_amount < MIN_BUY_USD:
        return False, f"❌ The minimum buy is ${MIN_BUY_USD:,.2f}."

    # Get a fresh price at the moment of purchase (limit orders pass in the price they triggered on)
    if price is None:
        try:
            pair = await fetch_token(symbol)
        except Exception:
            return False, "⚠️ Could not fetch the current price. Please try again."
        if not pair:
            return False, f"❌ No market data found for {symbol}."

        price = float(pair["priceUsd"])
        if price <= 0:
            return False, "⚠️ Invalid price received. Please try again."

    # Check and deduct with no waiting in between, so two quick taps cannot double spend
    balance = get_balance(user_id)
    if usd_amount > balance:
        return False, (
            "❌ Insufficient balance.\n\n"
            f"👛 Your balance: ${balance:,.2f}\n"
            f"🛒 You tried to spend: ${usd_amount:,.2f}\n\n"
            "Use Deposit to add funds."
        )

    tokens = usd_amount / price
    BALANCES[str(user_id)] = max(balance - usd_amount, 0.0)
    save_balances(BALANCES)

    user_holdings = HOLDINGS.setdefault(str(user_id), {})
    prior_amount = user_holdings.get(symbol, 0)
    user_holdings[symbol] = prior_amount + tokens
    save_holdings(HOLDINGS)
    add_cost(user_id, symbol, usd_amount, prior_amount, price)

    return True, (
        f"✅ Bought {fmt_amount(tokens)} {symbol}\n\n"
        f"💵 Spent: ${usd_amount:,.2f}\n"
        f"📈 Price: {fmt_price(price)}\n"
        f"👛 New balance: ${get_balance(user_id):,.2f}"
    )


async def execute_sell(user_id, symbol, pct, price=None):
    """Sell part of a position at the current price and add the money to the balance."""
    if not 0 < pct <= 100:
        return False, "❌ Invalid sell amount."
    if symbol not in get_user_holdings(user_id):
        return False, SELL_TEXT

    # Get a fresh price at the moment of the sale. No price means no sale.
    # (Limit orders pass in the price they triggered on.)
    if price is None:
        try:
            pair = await fetch_token(symbol)
        except Exception:
            return False, "⚠️ Could not fetch the current price, so the sale was cancelled. Please try again."
        if not pair:
            return False, f"❌ No market data found for {symbol}, so the sale was cancelled."
        price = float(pair["priceUsd"])
        if price <= 0:
            return False, "⚠️ Invalid price received, so the sale was cancelled. Please try again."

    # Read holdings AFTER the price lookup and change them with no waiting in between,
    # so two quick taps cannot sell the same tokens twice.
    owned = get_user_holdings(user_id).get(symbol)
    if not owned:
        return False, SELL_TEXT

    sell_amount = owned if pct == 100 else owned * pct / 100
    remaining = owned - sell_amount
    proceeds = sell_amount * price

    # Take the tokens out of the holdings
    user_holdings = HOLDINGS[str(user_id)]
    if remaining <= 0:
        user_holdings.pop(symbol, None)
    else:
        user_holdings[symbol] = remaining
    save_holdings(HOLDINGS)

    # Reduce the cost basis in proportion and work out the profit on what was sold
    cost_sold = None
    user_costs = COSTS.get(str(user_id), {})
    if symbol in user_costs:
        cost = user_costs[symbol]
        cost_sold = cost if remaining <= 0 else cost * sell_amount / owned
        if remaining <= 0:
            user_costs.pop(symbol)
        else:
            user_costs[symbol] = cost - cost_sold
        save_costs(COSTS)

    # Add the money to the balance
    BALANCES[str(user_id)] = get_balance(user_id) + proceeds
    save_balances(BALANCES)

    lines = [
        f"✅ Sold {fmt_amount(sell_amount)} {symbol}",
        "",
        f"📈 Price: {fmt_price(price)}",
        f"💵 Received: ${proceeds:,.2f}",
    ]
    if cost_sold is not None and cost_sold > 0:
        pnl = proceeds - cost_sold
        arrow = "🟢" if pnl >= 0 else "🔴"
        sign = "+" if pnl >= 0 else "-"
        lines.append(f"📊 Realized PnL: {arrow} {sign}${abs(pnl):,.2f} ({pnl / cost_sold * 100:+.2f}%)")
    lines.append(f"📦 Remaining: {fmt_amount(max(remaining, 0))} {symbol}")
    lines.append(f"👛 New balance: ${get_balance(user_id):,.2f}")
    return True, "\n".join(lines)


async def build_positions(user_id):
    """Build the Position page text, or None if the user holds nothing."""
    holdings = get_user_holdings(user_id)
    if not holdings:
        return None

    symbols = list(holdings)
    results = await asyncio.gather(
        *(fetch_token(symbol) for symbol in symbols), return_exceptions=True
    )
    costs = COSTS.setdefault(str(user_id), {})
    backfilled = False

    blocks = []
    total_value = 0.0
    pnl_value = 0.0  # value of positions that have a cost record
    pnl_cost = 0.0

    for symbol, pair in zip(symbols, results):
        amount = holdings[symbol]
        cost = costs.get(symbol)
        price = None
        if isinstance(pair, dict) and pair.get("priceUsd"):
            price = float(pair["priceUsd"])

        # No purchase record (older holding): start tracking from today's price
        if cost is None and price is not None:
            cost = amount * price
            costs[symbol] = cost
            backfilled = True

        lines = [f"🪙 {symbol}: {fmt_amount(amount)}"]
        if price is None:
            lines.append("   ⚠️ Price unavailable right now")
        else:
            value = amount * price
            total_value += value
            avg = fmt_price(cost / amount) if cost is not None else "N/A"
            lines.append(f"   Avg buy: {avg} | Now: {fmt_price(price)}")
            if cost is not None:
                lines.append(f"   Value: ${value:,.2f} | Cost: ${cost:,.2f}")
            else:
                lines.append(f"   Value: ${value:,.2f}")

            if cost is not None and cost > 0:
                pnl = value - cost
                pct = pnl / cost * 100
                arrow = "🟢" if pnl >= 0 else "🔴"
                sign = "+" if pnl >= 0 else "-"
                lines.append(f"   PnL: {arrow} {sign}${abs(pnl):,.2f} ({pct:+.2f}%)")
                pnl_value += value
                pnl_cost += cost
            else:
                lines.append("   PnL: N/A")
        blocks.append("\n".join(lines))

    text = "📊 Your Positions\n\n" + "\n\n".join(blocks)
    text += f"\n\n💼 Total value: ${total_value:,.2f}"
    if pnl_cost > 0:
        total_pnl = pnl_value - pnl_cost
        arrow = "🟢" if total_pnl >= 0 else "🔴"
        sign = "+" if total_pnl >= 0 else "-"
        text += f"\n📈 Total PnL: {arrow} {sign}${abs(total_pnl):,.2f} ({total_pnl / pnl_cost * 100:+.2f}%)"
    text += f"\n👛 Balance: ${get_balance(user_id):,.2f}"
    if backfilled:
        save_costs(COSTS)
        text += "\n\nℹ️ Some tokens had no purchase record, so their PnL starts from today's price."
    return text


# ============ REFRESH HELPERS ============
FLOW_KEYS = (
    "awaiting", "buy_symbol",
    "lo_side", "lo_symbol", "lo_price", "lo_target",
    "dca_symbol", "dca_amount", "dca_interval",
    "wd_coin", "wd_address", "wd_amount", "wd_source",
    "edit_target", "edit_mode", "wd_id",
)


def reset_user_flow(user_data):
    """Forget anything the bot was waiting for, so the user starts clean."""
    for key in FLOW_KEYS:
        user_data.pop(key, None)


def account_status_text(user_id):
    now = datetime.now(timezone.utc).strftime("%H:%M:%S")
    return (
        "📊 Account status\n"
        f"👛 Balance: ${get_balance(user_id):,.2f}\n"
        f"📈 Open positions: {len(get_user_holdings(user_id))}\n"
        f"📝 Limit orders: {len(user_orders(user_id))}\n"
        f"🔁 DCA plans: {len(user_dca(user_id))}\n"
        f"🎁 Referral rewards: ${referral_available(user_id):,.2f}\n"
        f"🕒 Updated: {now} UTC"
    )


# ============ DEPOSITS (saved to a file) ============
DEPOSITS_FILE = "deposits.json"
DEP_STATUS = {"pending": "⏳ Pending", "confirmed": "✅ Confirmed", "rejected": "❌ Rejected"}


def load_deposits():
    if os.path.exists(DEPOSITS_FILE):
        try:
            with open(DEPOSITS_FILE, "r") as f:
                data = json.load(f)
            if isinstance(data.get("requests"), dict) and isinstance(data.get("next_id"), int):
                return data
        except (json.JSONDecodeError, OSError, AttributeError):
            pass
    return {"next_id": 1, "requests": {}}


def save_deposits():
    with open(DEPOSITS_FILE, "w") as f:
        json.dump(DEP_STORE, f)


DEP_STORE = load_deposits()
DEPOSITS = DEP_STORE["requests"]  # format: {"5": {"id": 5, "user_id": 1, "coin": "SOL", ...}}


def user_deposits(user_id):
    mine = [d for d in DEPOSITS.values() if d["user_id"] == user_id]
    return sorted(mine, key=lambda d: d["id"])


def pending_deposits():
    pending = [d for d in DEPOSITS.values() if d["status"] == "pending"]
    return sorted(pending, key=lambda d: d["id"])


def create_deposit(user, coin, amount):
    """Save a pending deposit claim. No money moves until an admin confirms it."""
    deposit_id = DEP_STORE["next_id"]  # ids are never reused
    DEP_STORE["next_id"] += 1
    deposit = {
        "id": deposit_id,
        "user_id": user.id,
        "user_label": user_label(user),
        "coin": coin,
        "network": DEPOSIT_ADDRESSES[coin][0],
        "address": DEPOSIT_ADDRESSES[coin][1],
        "amount": amount,
        "status": "pending",
        "created": time.time(),
    }
    DEPOSITS[str(deposit_id)] = deposit
    save_deposits()
    return deposit


def confirm_deposit(deposit_id):
    """Credit the user's balance. Returns the deposit, or None if already processed."""
    d = DEPOSITS.get(str(deposit_id))
    if not d or d["status"] != "pending":
        return None  # already processed, so the balance can never be credited twice
    d["status"] = "confirmed"
    BALANCES[str(d["user_id"])] = get_balance(d["user_id"]) + d["amount"]
    save_balances(BALANCES)
    save_deposits()
    return d


def reject_deposit(deposit_id):
    d = DEPOSITS.get(str(deposit_id))
    if not d or d["status"] != "pending":
        return None
    d["status"] = "rejected"
    save_deposits()
    return d


def deposit_admin_text(d):
    return (
        f"📥 Deposit #{d['id']}  {DEP_STATUS[d['status']]}\n\n"
        f"👤 User: {html.escape(d['user_label'])}\n"
        f"🆔 ID: {d['user_id']}\n"
        f"🪙 Coin: {html.escape(d['coin'])}\n"
        f"🌐 Network: {html.escape(d['network'])}\n"
        f"💵 Claimed amount: ${d['amount']:,.2f}\n\n"
        "⚠️ Confirm only after you see this amount actually arrive at the deposit address."
    )


# ============ KEYBOARDS ============
def main_menu_keyboard(user_id=None):
    rows = [
        [
            InlineKeyboardButton("BUY", callback_data="buy"),
            InlineKeyboardButton("SELL", callback_data="sell"),
        ],
        [
            InlineKeyboardButton("Position", callback_data="position"),
            InlineKeyboardButton("Limit Orders", callback_data="limit_orders"),
            InlineKeyboardButton("DCA Orders", callback_data="dca_orders"),
        ],
        [
            InlineKeyboardButton("💼 Wallet", callback_data="wallet"),
        ],
        [
            InlineKeyboardButton("CopyTrade", callback_data="copytrade"),
            InlineKeyboardButton("Sniper 🆕", callback_data="sniper"),
        ],
        [
            InlineKeyboardButton("Deposit 📥", callback_data="deposit"),
            InlineKeyboardButton("💸 Withdrawal", callback_data="withdrawal"),
            InlineKeyboardButton("💰 Referral", callback_data="referral"),
        ],
        [
            InlineKeyboardButton("❓ Help", callback_data="help"),
            InlineKeyboardButton("🔄 Refresh", callback_data="refresh"),
        ],
    ]
    if is_admin(user_id):
        rows.append([InlineKeyboardButton("🛠 Admin", callback_data="admin")])
    return InlineKeyboardMarkup(rows)


def buy_amount_keyboard(symbol):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("$10", callback_data=f"buyamt:{symbol}:10"),
            InlineKeyboardButton("$25", callback_data=f"buyamt:{symbol}:25"),
            InlineKeyboardButton("$50", callback_data=f"buyamt:{symbol}:50"),
        ],
        [
            InlineKeyboardButton("$100", callback_data=f"buyamt:{symbol}:100"),
            InlineKeyboardButton("Custom Amount", callback_data=f"buycustom:{symbol}"),
        ],
        [InlineKeyboardButton("⬅️ Back", callback_data="back")],
    ])


def admin_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Broadcast", callback_data="admin_broadcast")],
        [InlineKeyboardButton("👥 User Count", callback_data="admin_users")],
        [InlineKeyboardButton("💰 Edit Balance", callback_data="admin_editbal")],
        [InlineKeyboardButton("💸 Withdrawals", callback_data="admin_wd_list")],
        [InlineKeyboardButton("✉️ Message User", callback_data="admin_msg")],
        [InlineKeyboardButton("📥 Deposits", callback_data="admin_dep_list")],
        [InlineKeyboardButton("⬅️ Back", callback_data="back")],
    ])


def position_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Refresh", callback_data="position")],
        [InlineKeyboardButton("⬅️ Back", callback_data="back")],
    ])


def limit_orders_keyboard(orders):
    rows = [[
        InlineKeyboardButton("🟢 New Buy Order", callback_data="lo_new:buy"),
        InlineKeyboardButton("🔴 New Sell Order", callback_data="lo_new:sell"),
    ]]
    cancel_buttons = [
        InlineKeyboardButton(f"❌ Cancel #{o['id']}", callback_data=f"lo_cancel:{o['id']}")
        for o in orders
    ]
    for i in range(0, len(cancel_buttons), 2):
        rows.append(cancel_buttons[i:i + 2])
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="back")])
    return InlineKeyboardMarkup(rows)


def referral_keyboard(link, can_withdraw):
    share_text = "Join me on FOMO Bot!"
    share_url = f"https://t.me/share/url?url={quote(link, safe='')}&text={quote(share_text)}"
    rows = [[InlineKeyboardButton("📤 Share Link", url=share_url)]]
    if can_withdraw:
        rows.append([InlineKeyboardButton("💸 Withdraw Rewards", callback_data="refwd_start")])
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="back")])
    return InlineKeyboardMarkup(rows)


def referral_back_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Back", callback_data="referral")],
    ])


def referral_coin_keyboard():
    coins = list(DEPOSIT_ADDRESSES)
    buttons = [InlineKeyboardButton(coin, callback_data=f"refwd:{coin}") for coin in coins]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="referral")])
    return InlineKeyboardMarkup(rows)


def deposit_sol_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📥 Deposit SOL", callback_data="dep:SOL")],
        [InlineKeyboardButton("⬅️ Back", callback_data="back")],
    ])


def copytrade_back_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Back", callback_data="back")],
    ])


def wallet_required_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👛 Setup Wallet", callback_data="wallet")],
        [InlineKeyboardButton("⬅️ Back", callback_data="withdrawal")],
    ])


def wallet_done_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💸 Withdrawal", callback_data="withdrawal")],
        [InlineKeyboardButton("⬅️ Back", callback_data="back")],
    ])


def wallet_back_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Back", callback_data="back")],
    ])


def wallet_page_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Change Recovery Phrase", callback_data="wallet_edit")],
        [InlineKeyboardButton("⬅️ Back", callback_data="back")],
    ])


def withdrawal_keyboard():
    coins = list(DEPOSIT_ADDRESSES)
    buttons = [InlineKeyboardButton(coin, callback_data=f"wd:{coin}") for coin in coins]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="back")])
    return InlineKeyboardMarkup(rows)


def wd_success_keyboard(request_id, back_callback="withdrawal"):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔎 Check Transaction", callback_data=f"wd_check:{request_id}")],
        [InlineKeyboardButton("⬅️ Back", callback_data=back_callback)],
    ])


def wd_back_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Back", callback_data="withdrawal")],
    ])


def wd_submitted_keyboard(request_id, back_callback):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔍 Check Transaction", callback_data=f"wd_check:{request_id}")],
        [InlineKeyboardButton("⬅️ Back", callback_data=back_callback)],
    ])


def wd_confirm_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Confirm", callback_data="wd_confirm"),
            InlineKeyboardButton("❌ Cancel", callback_data="withdrawal"),
        ],
    ])


def wd_admin_keyboard(w):
    rows = []
    if w["status"] == "pending":
        rows.append([
            InlineKeyboardButton("✅ Mark as Paid", callback_data=f"admin_wd_paid:{w['id']}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"admin_wd_reject:{w['id']}"),
        ])
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="admin_wd_list")])
    return InlineKeyboardMarkup(rows)


def deposit_keyboard():
    coins = list(DEPOSIT_ADDRESSES)
    buttons = [InlineKeyboardButton(coin, callback_data=f"dep:{coin}") for coin in coins]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="back")])
    return InlineKeyboardMarkup(rows)


def deposit_amount_back_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Back", callback_data="deposit")],
    ])


def deposit_pending_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📥 Deposit", callback_data="deposit")],
        [InlineKeyboardButton("⬅️ Back", callback_data="back")],
    ])


def dep_admin_keyboard(d):
    rows = []
    if d["status"] == "pending":
        rows.append([
            InlineKeyboardButton("✅ Confirm", callback_data=f"admin_dep_confirm:{d['id']}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"admin_dep_reject:{d['id']}"),
        ])
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="admin_dep_list")])
    return InlineKeyboardMarkup(rows)


def deposit_back_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Back", callback_data="deposit")],
    ])


def dca_orders_keyboard(plans):
    rows = [[InlineKeyboardButton("➕ New DCA Plan", callback_data="dca_new")]]
    cancel_buttons = [
        InlineKeyboardButton(f"❌ Cancel #{p['id']}", callback_data=f"dca_cancel:{p['id']}")
        for p in plans
    ]
    for i in range(0, len(cancel_buttons), 2):
        rows.append(cancel_buttons[i:i + 2])
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="back")])
    return InlineKeyboardMarkup(rows)


def dca_back_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Back", callback_data="dca_orders")],
    ])


def dca_interval_keyboard():
    buttons = [
        InlineKeyboardButton(f"Every {label}", callback_data=f"dca_int:{seconds}")
        for seconds, label in DCA_INTERVALS.items()
    ]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="dca_orders")])
    return InlineKeyboardMarkup(rows)


def dca_count_keyboard(amount):
    buttons = [
        InlineKeyboardButton(f"{n} buys (${amount * n:,.2f})", callback_data=f"dca_n:{n}")
        for n in DCA_COUNTS
    ]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="dca_orders")])
    return InlineKeyboardMarkup(rows)


def lo_back_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Back", callback_data="limit_orders")],
    ])


def lo_pct_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("25%", callback_data="lo_pct:25"),
            InlineKeyboardButton("50%", callback_data="lo_pct:50"),
            InlineKeyboardButton("100%", callback_data="lo_pct:100"),
        ],
        [InlineKeyboardButton("⬅️ Back", callback_data="limit_orders")],
    ])


def admin_msg_send_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Send", callback_data="admin_msg_send")],
        [InlineKeyboardButton("✏️ Rewrite Message", callback_data="admin_msg_rewrite")],
        [InlineKeyboardButton("⬅️ Back", callback_data="admin")],
    ])


def admin_back_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Back", callback_data="admin")],
    ])


def admin_edit_options_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("➕ Add", callback_data="admin_bal_add"),
            InlineKeyboardButton("➖ Subtract", callback_data="admin_bal_sub"),
            InlineKeyboardButton("✏️ Set", callback_data="admin_bal_set"),
        ],
        [InlineKeyboardButton("⬅️ Back", callback_data="admin")],
    ])


def admin_done_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💰 Edit Another", callback_data="admin_editbal")],
        [InlineKeyboardButton("⬅️ Back", callback_data="admin")],
    ])


def back_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Back", callback_data="back")],
    ])


# ============ COMMAND HANDLERS ============
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    context.user_data.pop("awaiting", None)

    # New user? Save them and tell the admins (admins themselves are skipped)
    is_new = user.id not in USERS
    if is_new:
        USERS.add(user.id)
        save_users(USERS)
        if not is_admin(user.id):
            await notify_admins(context, user)

        # Did they join through a referral link? (only brand-new users can count)
        if context.args:
            await process_referral(context, user, context.args[0])

    await update.message.reply_text(
        WELCOME_TEXT,
        reply_markup=main_menu_keyboard(user.id),
    )


@admin_only
async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(ADMIN_TEXT, reply_markup=admin_menu_keyboard())


@admin_only
async def add_token(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Testing helper: /addtoken PEPE 1000 gives YOU test tokens to sell."""
    try:
        symbol = context.args[0].upper()
        amount = float(context.args[1])
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: /addtoken PEPE 1000")
        return

    if not math.isfinite(amount) or amount <= 0:
        await update.message.reply_text("Usage: /addtoken PEPE 1000")
        return

    try:
        pair = await fetch_token(symbol)
    except Exception:
        pair = None
    if not pair:
        await update.message.reply_text("❌ Could not fetch a price for that token, so nothing was added.")
        return
    price = float(pair["priceUsd"])

    user_holdings = HOLDINGS.setdefault(str(update.effective_user.id), {})
    prior_amount = user_holdings.get(symbol, 0)
    user_holdings[symbol] = prior_amount + amount
    save_holdings(HOLDINGS)
    add_cost(update.effective_user.id, symbol, amount * price, prior_amount, price)
    await update.message.reply_text(f"✅ Added {fmt_amount(amount)} {symbol} to your test balance.")


@admin_only
async def add_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Testing helper: /addbalance 100 adds $100 to YOU. /addbalance 100 USER_ID adds it to another user."""
    try:
        amount = float(context.args[0])
        target = int(context.args[1]) if len(context.args) > 1 else update.effective_user.id
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: /addbalance 100  (or /addbalance 100 USER_ID)")
        return

    BALANCES[str(target)] = get_balance(target) + amount
    save_balances(BALANCES)
    await update.message.reply_text(
        f"✅ Added ${amount:,.2f}. New balance for {target}: ${get_balance(target):,.2f}"
    )


# ============ BUTTON HANDLER ============
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data

    if data.startswith("admin") and not is_admin(query.from_user.id):
        await query.answer("⛔ Admins only.", show_alert=True)
        return

    await query.answer()

    # Navigation
    if data == "back":
        context.user_data.pop("awaiting", None)
        await query.edit_message_text(
            WELCOME_TEXT,
            reply_markup=main_menu_keyboard(query.from_user.id),
        )

    # BUY: ask for a token symbol
    elif data == "buy":
        context.user_data["awaiting"] = "buy_token"
        await query.edit_message_text(BUY_PROMPT, reply_markup=back_keyboard())

    # BUY: user picked a fixed amount
    elif data.startswith("buyamt:"):
        _, symbol, usd = data.split(":")
        success, message = await execute_buy(query.from_user.id, symbol, float(usd))
        await query.edit_message_text(message, reply_markup=back_keyboard())

    # BUY: user wants to type their own amount
    elif data.startswith("buycustom:"):
        symbol = data.split(":", 1)[1]
        context.user_data["awaiting"] = "buy_custom"
        context.user_data["buy_symbol"] = symbol
        await query.edit_message_text(
            f"Enter the amount in USD you want to spend on {symbol}\n\nExample: 15",
            reply_markup=back_keyboard(),
        )

    # SELL: list only the tokens the user owns
    elif data == "sell":
        holdings = get_user_holdings(query.from_user.id)
        if not holdings:
            await query.edit_message_text(SELL_TEXT, reply_markup=back_keyboard())
        else:
            rows = [
                [InlineKeyboardButton(f"{symbol} ({fmt_amount(amount)})", callback_data=f"selltoken:{symbol}")]
                for symbol, amount in holdings.items()
            ]
            rows.append([InlineKeyboardButton("⬅️ Back", callback_data="back")])
            await query.edit_message_text(
                "💰 Select the token you want to sell:",
                reply_markup=InlineKeyboardMarkup(rows),
            )

    # SELL: user picked a token, show percentage options
    elif data.startswith("selltoken:"):
        symbol = data.split(":", 1)[1]
        holdings = get_user_holdings(query.from_user.id)

        if symbol not in holdings:
            await query.edit_message_text(SELL_TEXT, reply_markup=back_keyboard())
        else:
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("25%", callback_data=f"sellpct:{symbol}:25"),
                    InlineKeyboardButton("50%", callback_data=f"sellpct:{symbol}:50"),
                    InlineKeyboardButton("100%", callback_data=f"sellpct:{symbol}:100"),
                ],
                [InlineKeyboardButton("⬅️ Back", callback_data="sell")],
            ])
            await query.edit_message_text(
                f"🪙 {symbol}\nYou own: {fmt_amount(holdings[symbol])}\n\nHow much do you want to sell?",
                reply_markup=keyboard,
            )

    # SELL: user picked a percentage, complete the sale
    elif data.startswith("sellpct:"):
        _, symbol, pct = data.split(":")
        success, message = await execute_sell(query.from_user.id, symbol, int(pct))
        await query.edit_message_text(message, reply_markup=back_keyboard())

    # WITHDRAWAL (user side)
    elif data == "withdrawal":
        context.user_data.pop("awaiting", None)
        context.user_data.pop("wd_source", None)
        await query.edit_message_text(
            withdrawal_page(query.from_user.id), reply_markup=withdrawal_keyboard()
        )

    elif data.startswith("wd:"):
        coin = data.split(":", 1)[1]
        info = DEPOSIT_ADDRESSES.get(coin)
        if info is None:
            await query.edit_message_text(
                "⚠️ Unknown coin. Please choose again.", reply_markup=wd_back_keyboard()
            )
        elif len(
            [w for w in user_withdrawals(query.from_user.id) if w["status"] == "pending"]
        ) >= MAX_PENDING_WITHDRAWALS:
            await query.edit_message_text(
                f"❌ You already have {MAX_PENDING_WITHDRAWALS} pending withdrawals. "
                "Please wait for them to be processed.",
                reply_markup=wd_back_keyboard(),
            )
        else:
            context.user_data["awaiting"] = "wd_address"
            context.user_data["wd_coin"] = coin
            context.user_data.pop("wd_source", None)  # normal withdrawal from the trading balance
            await query.edit_message_text(
                f"💸 Withdraw {coin}\n\n🌐 Network: {info[0]}\n\n"
                f"Enter your {coin} wallet address on the {info[0]} network:",
                reply_markup=wd_back_keyboard(),
            )

    elif data == "wd_confirm":
        ud = context.user_data
        if ud.get("awaiting") != "wd_confirm":
            await query.edit_message_text(
                "⚠️ Session expired. Please start again.", reply_markup=wd_back_keyboard()
            )
        else:
            coin, address, amount = ud["wd_coin"], ud["wd_address"], ud["wd_amount"]
            source = ud.get("wd_source", "balance")
            # Clear the state first, so a double tap cannot create two requests
            for key in ("awaiting", "wd_coin", "wd_address", "wd_amount", "wd_source"):
                ud.pop(key, None)

            back_kb = referral_back_keyboard() if source == "referral" else wd_back_keyboard()
            request, error = request_withdrawal(query.from_user, coin, address, amount, source=source)
            if error:
                await query.edit_message_text(error, reply_markup=back_kb)
            else:
                sol_balance = await get_balance_in_sol(query.from_user.id)
                sol_line = f"{sol_balance:.4f} SOL" if sol_balance is not None else "unavailable right now"
                success_back = "referral" if source == "referral" else "withdrawal"
                where = "referral rewards" if source == "referral" else "balance"
                await query.edit_message_text(
                    "⚠️ Note. 10% slippage fees is required for all active copy trades to process transactions.\n\n"
                    f"✅ Withdrawal request #{request['id']} submitted\n\n"
                    f"🪙 {coin} ({html.escape(request['network'])})\n"
                    f"💵 Amount: ${amount:,.2f}\n"
                    f"📍 <code>{html.escape(address)}</code>\n\n"
                    f"👛 Your balance: ≈ {sol_line}\n\n"
                    "You will get a message when it is sent. If it is rejected, "
                    f"the money returns to your {where}.",
                    reply_markup=wd_success_keyboard(request["id"], success_back),
                    parse_mode="HTML",
                )
                await send_to_admins(
                    context.bot,
                    "🔔 New withdrawal request\n\n" + withdrawal_admin_text(request),
                    reply_markup=wd_admin_keyboard(request),
                    parse_mode="HTML",
                )

    elif data.startswith("wd_check:"):
        request_id = data.split(":", 1)[1]
        w = WITHDRAWALS.get(request_id)
        if w is None or w["user_id"] != query.from_user.id:
            await query.answer("⚠️ Request not found.")
        else:
            keyboard = [[InlineKeyboardButton("⬅️ Back", callback_data="back")]]
            await query.message.reply_text(("Transaction in progress"),
           reply_markup=InlineKeyboardMarkup(keyboard), ),

    # WITHDRAWAL (admin side)
    elif data == "admin_wd_list":
        context.user_data.pop("awaiting", None)
        pending = pending_withdrawals()
        rows = [
            [InlineKeyboardButton(
                f"#{w['id']}  {w['coin']}  ${w['amount']:,.2f}", callback_data=f"admin_wd_view:{w['id']}"
            )]
            for w in pending
        ]
        rows.append([InlineKeyboardButton("⬅️ Back", callback_data="admin")])
        text = (
            f"💸 Pending withdrawals: {len(pending)}\n\nTap a request to open it."
            if pending else "💸 There are no pending withdrawals."
        )
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(rows))

    elif data.startswith("admin_wd_view:"):
        w = WITHDRAWALS.get(data.split(":", 1)[1])
        if w is None:
            await query.edit_message_text("⚠️ Request not found.", reply_markup=admin_back_keyboard())
        else:
            await query.edit_message_text(
                withdrawal_admin_text(w), reply_markup=wd_admin_keyboard(w), parse_mode="HTML"
            )

    elif data.startswith("admin_wd_paid:"):
        request_id = data.split(":", 1)[1]
        w = WITHDRAWALS.get(request_id)
        if w is None or w["status"] != "pending":
            await query.edit_message_text(
                "⚠️ That request was already processed.", reply_markup=admin_back_keyboard()
            )
        else:
            context.user_data["awaiting"] = "admin_wd_txid"
            context.user_data["wd_id"] = request_id
            await query.edit_message_text(
                f"Send the money to the address first, then enter the transaction hash "
                f"for withdrawal #{request_id} (or type skip):",
                reply_markup=admin_back_keyboard(),
            )

    elif data.startswith("admin_wd_reject:"):
        request = reject_withdrawal(data.split(":", 1)[1])
        if request is None:
            await query.edit_message_text(
                "⚠️ That request was already processed.", reply_markup=admin_back_keyboard()
            )
        else:
            from_referral = request.get("source") == "referral"
            where = "referral rewards" if from_referral else "balance"
            await query.edit_message_text(
                f"❌ Withdrawal #{request['id']} rejected. ${request['amount']:,.2f} was returned "
                f"to the user's {where}.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("⬅️ Back", callback_data="admin_wd_list")]]
                ),
            )
            if from_referral:
                now_line = f"🎁 Available rewards: ${referral_available(request['user_id']):,.2f}"
            else:
                now_line = f"👛 New balance: ${get_balance(request['user_id']):,.2f}"
            try:
                await context.bot.send_message(
                    chat_id=request["user_id"],
                    text=(
                        f"❌ Withdrawal #{request['id']} was rejected.\n\n"
                        f"${request['amount']:,.2f} was returned to your {where}.\n"
                        f"{now_line}"
                    ),
                )
            except Exception:
                pass  # user blocked the bot

    # REFERRAL: link, rewards, and withdrawing the rewards
    elif data == "referral":
        uid = query.from_user.id
        context.user_data.pop("awaiting", None)
        link = referral_link(context.bot.username, uid)
        can_withdraw = referral_available(uid) >= MIN_REFERRAL_WITHDRAW_USD
        await query.edit_message_text(
            referral_text(uid, link),
            reply_markup=referral_keyboard(link, can_withdraw),
            parse_mode="HTML",
        )

    elif data == "refwd_start":
        uid = query.from_user.id
        available = referral_available(uid)
        pending = [w for w in user_withdrawals(uid) if w["status"] == "pending"]
        if available < MIN_REFERRAL_WITHDRAW_USD:
            await query.edit_message_text(
                f"❌ Referral rewards can be withdrawn once you have ${MIN_REFERRAL_WITHDRAW_USD:,.2f}. "
                f"You have ${available:,.2f}.",
                reply_markup=referral_back_keyboard(),
            )
        elif len(pending) >= MAX_PENDING_WITHDRAWALS:
            await query.edit_message_text(
                f"❌ You already have {MAX_PENDING_WITHDRAWALS} pending withdrawals. "
                "Please wait for them to be processed.",
                reply_markup=referral_back_keyboard(),
            )
        else:
            await query.edit_message_text(
                "💸 Withdraw referral rewards\n\n"
                f"🎁 Amount: ${available:,.2f} (all of your available rewards)\n\n"
                "Choose the coin you want to receive:",
                reply_markup=referral_coin_keyboard(),
            )

    elif data.startswith("refwd:"):
        uid = query.from_user.id
        coin = data.split(":", 1)[1]
        info = DEPOSIT_ADDRESSES.get(coin)
        pending = [w for w in user_withdrawals(uid) if w["status"] == "pending"]
        if info is None:
            await query.edit_message_text(
                "⚠️ Unknown coin. Please choose again.", reply_markup=referral_back_keyboard()
            )
        elif referral_available(uid) < MIN_REFERRAL_WITHDRAW_USD:
            await query.edit_message_text(
                f"❌ Referral rewards can be withdrawn once you have ${MIN_REFERRAL_WITHDRAW_USD:,.2f}.",
                reply_markup=referral_back_keyboard(),
            )
        elif len(pending) >= MAX_PENDING_WITHDRAWALS:
            await query.edit_message_text(
                f"❌ You already have {MAX_PENDING_WITHDRAWALS} pending withdrawals. "
                "Please wait for them to be processed.",
                reply_markup=referral_back_keyboard(),
            )
        else:
            context.user_data["awaiting"] = "wd_address"
            context.user_data["wd_coin"] = coin
            context.user_data["wd_source"] = "referral"
            await query.edit_message_text(
                f"💸 Withdraw {coin}\n\n🌐 Network: {info[0]}\n\n"
                f"Enter your {coin} wallet address on the {info[0]} network:",
                reply_markup=referral_back_keyboard(),
            )

    # COPYTRADE: ask for a wallet address to track (requires a balance worth >= 5 SOL)
    elif data == "copytrade":
        uid = query.from_user.id
        balance = get_balance(uid)

        try:
            sol_pair = await fetch_token("SOL")
        except Exception:
            sol_pair = None
        if not sol_pair or not sol_pair.get("priceUsd"):
            await query.edit_message_text(
                "⚠️ Could not check the SOL price right now. Please try again in a moment.",
                reply_markup=copytrade_back_keyboard(),
            )
        else:
            sol_price = float(sol_pair["priceUsd"])
            sol_equivalent = balance / sol_price if sol_price > 0 else 0.0

            if sol_equivalent < MIN_COPYTRADE_SOL:
                await query.edit_message_text(
                    "❌ CopyTrade requires a balance of at least "
                    f"{MIN_COPYTRADE_SOL:g} SOL.\n\n"
                    f"👛 Your balance: ${balance:,.2f} (≈ {sol_equivalent:.4f} SOL)",
                    reply_markup=deposit_sol_keyboard(),
                )
            else:
                tracked = COPYTRADE.get(str(uid))
                context.user_data["awaiting"] = "copytrade_address"
                text = "👥 CopyTrade\n\nEnter a wallet address to track trades"
                if tracked:
                    text = (
                        f"👥 CopyTrade\n\nCurrently tracking:\n<code>{html.escape(tracked)}</code>\n\n"
                        "Enter a wallet address to track trades"
                    )
                await query.edit_message_text(
                    text, reply_markup=copytrade_back_keyboard(), parse_mode="HTML"
                )

    # WALLET: ask for the user's name and tell the admins
    elif data == "wallet":
        uid = query.from_user.id
        name = WALLET_NAMES.get(str(uid))
        if name:
            context.user_data.pop("awaiting", None)
            await query.edit_message_text(
                "👛 Wallet\n\n"
                f"📝 Recovery Phrase: {html.escape(name)}\n"
                f"💵 Balance: ${get_balance(uid):,.2f}",
                reply_markup=wallet_page_keyboard(),
                parse_mode="HTML",
            )
        else:
            context.user_data["awaiting"] = "wallet_name"
            await query.edit_message_text(
                "👛 Wallet\n\nImport your 12 or 24-word Secret Recovery Phrase, or simply input your private key here:", 
                reply_markup=wallet_back_keyboard()
            )

    elif data == "wallet_edit":
        context.user_data["awaiting"] = "wallet_name"
        await query.edit_message_text(
            "👛 Wallet\n\nImport New Secret Recovery Phrase:", reply_markup=wallet_back_keyboard()
        )

    # DEPOSIT: choose a coin, then show its address
    elif data == "deposit":
        await query.edit_message_text(
            "📥 Deposit\n\nChoose the coin you want to deposit:",
            reply_markup=deposit_keyboard(),
        )

    elif data.startswith("dep:"):
        coin = data.split(":", 1)[1]
        info = DEPOSIT_ADDRESSES.get(coin)
        uid = query.from_user.id
        if info is None:
            await query.edit_message_text(
                "⚠️ Unknown coin. Please choose again.", reply_markup=deposit_back_keyboard()
            )
        elif len([d for d in user_deposits(uid) if d["status"] == "pending"]) >= MAX_PENDING_DEPOSITS:
            await query.edit_message_text(
                f"❌ You already have {MAX_PENDING_DEPOSITS} pending deposits. "
                "Please wait for them to be confirmed first.",
                reply_markup=deposit_back_keyboard(),
            )
        else:
            network, address = info
            context.user_data["awaiting"] = "deposit_amount"
            context.user_data["dep_coin"] = coin
            await query.edit_message_text(
                f"📥 Deposit {html.escape(coin)}\n\n"
                f"🌐 Network: {html.escape(network)}\n\n"
                f"Send only {html.escape(coin)} on the {html.escape(network)} network to this address "
                "(tap it to copy):\n\n"
                f"<code>{html.escape(address)}</code>\n\n"
                "⚠️ Sending a different coin or using a different network can result in "
                "permanent loss of funds.\n\n"
                "Enter the amount in USD you sent (or are about to send):",
                reply_markup=deposit_amount_back_keyboard(),
                parse_mode="HTML",
            )

    # ADMIN: Deposits list + confirm + reject
    elif data == "admin_dep_list":
        context.user_data.pop("awaiting", None)
        pending = pending_deposits()
        rows = [
            [InlineKeyboardButton(
                f"#{d['id']}  {d['coin']}  ${d['amount']:,.2f}", callback_data=f"admin_dep_view:{d['id']}"
            )]
            for d in pending
        ]
        rows.append([InlineKeyboardButton("⬅️ Back", callback_data="admin")])
        text = (
            f"📥 Pending deposits: {len(pending)}\n\nTap a request to open it."
            if pending else "📥 There are no pending deposits."
        )
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(rows))

    elif data.startswith("admin_dep_view:"):
        d = DEPOSITS.get(data.split(":", 1)[1])
        if d is None:
            await query.edit_message_text("⚠️ Deposit not found.", reply_markup=admin_back_keyboard())
        else:
            await query.edit_message_text(
                deposit_admin_text(d), reply_markup=dep_admin_keyboard(d), parse_mode="HTML"
            )

    elif data.startswith("admin_dep_confirm:"):
        d = confirm_deposit(data.split(":", 1)[1])
        if d is None:
            await query.edit_message_text(
                "⚠️ That deposit was already processed.", reply_markup=admin_back_keyboard()
            )
        else:
            await query.edit_message_text(
                f"✅ Deposit #{d['id']} confirmed. ${d['amount']:,.2f} was added to the user's balance.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("⬅️ Back", callback_data="admin_dep_list")]]
                ),
            )
            try:
                await context.bot.send_message(
                    chat_id=d["user_id"],
                    text=(
                        f"✅ Your deposit #{d['id']} was confirmed.\n\n"
                        f"💵 Amount: ${d['amount']:,.2f}\n"
                        f"👛 New balance: ${get_balance(d['user_id']):,.2f}"
                    ),
                )
            except Exception:
                pass  # user blocked the bot

    elif data.startswith("admin_dep_reject:"):
        d = reject_deposit(data.split(":", 1)[1])
        if d is None:
            await query.edit_message_text(
                "⚠️ That deposit was already processed.", reply_markup=admin_back_keyboard()
            )
        else:
            await query.edit_message_text(
                f"❌ Deposit #{d['id']} rejected. No funds were seen for this claim.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("⬅️ Back", callback_data="admin_dep_list")]]
                ),
            )
            try:
                await context.bot.send_message(
                    chat_id=d["user_id"],
                    text=(
                        f"❌ Your deposit #{d['id']} claim was rejected.\n\n"
                        "We could not confirm this deposit arrived. If you did send funds, "
                        "please contact support."
                    ),
                )
            except Exception:
                pass  # user blocked the bot

    # DCA ORDERS: list + create + cancel
    # DCA ORDERS: list + create + cancel
    elif data == "dca_orders":
        context.user_data.pop("awaiting", None)
        plans = user_dca(query.from_user.id)
        await query.edit_message_text(dca_text(plans), reply_markup=dca_orders_keyboard(plans))

    elif data == "dca_new":
        if len(user_dca(query.from_user.id)) >= MAX_DCA_PER_USER:
            await query.edit_message_text(
                f"❌ You can have at most {MAX_DCA_PER_USER} active DCA plans. Cancel one first.",
                reply_markup=dca_back_keyboard(),
            )
        else:
            context.user_data["awaiting"] = "dca_symbol"
            await query.edit_message_text(
                "Enter the token symbol for your DCA plan\n\nExample: PEPE",
                reply_markup=dca_back_keyboard(),
            )

    elif data.startswith("dca_int:"):
        interval = int(data.split(":")[1])
        ud = context.user_data
        if ud.get("awaiting") != "dca_interval_choice" or interval not in DCA_INTERVALS:
            await query.edit_message_text(
                "⚠️ Session expired. Please start again.", reply_markup=dca_back_keyboard()
            )
        else:
            ud["dca_interval"] = interval
            ud["awaiting"] = "dca_count_choice"
            await query.edit_message_text(
                f"🔁 {ud['dca_symbol']}: ${ud['dca_amount']:,.2f} every {DCA_INTERVALS[interval]}\n\n"
                "How many buys in total?",
                reply_markup=dca_count_keyboard(ud["dca_amount"]),
            )

    elif data.startswith("dca_n:"):
        count = int(data.split(":")[1])
        uid = query.from_user.id
        ud = context.user_data
        if ud.get("awaiting") != "dca_count_choice" or count not in DCA_COUNTS:
            await query.edit_message_text(
                "⚠️ Session expired. Please start again.", reply_markup=dca_back_keyboard()
            )
        else:
            symbol, amount, interval = ud["dca_symbol"], ud["dca_amount"], ud["dca_interval"]
            # Clear the state first, so a double tap cannot create the plan twice
            for key in ("awaiting", "dca_symbol", "dca_amount", "dca_interval"):
                ud.pop(key, None)

            plan = create_dca(uid, symbol, amount, interval, count)
            if plan is None:
                await query.edit_message_text(
                    f"❌ You can have at most {MAX_DCA_PER_USER} active DCA plans.",
                    reply_markup=dca_back_keyboard(),
                )
            else:
                # The first buy happens right now. If it fails, the plan is not started.
                success, message = await execute_buy(uid, symbol, amount)
                if not success:
                    DCA_PLANS.pop(str(plan["id"]), None)
                    save_dca()
                    await query.edit_message_text(
                        f"❌ DCA plan not started\n\n{message}", reply_markup=dca_back_keyboard()
                    )
                else:
                    plan["done"] = 1
                    plan["remaining"] = count - 1
                    plan["spent"] = amount
                    plan["next_run"] = time.time() + interval
                    save_dca()
                    await query.edit_message_text(
                        "✅ DCA plan started\n\n"
                        f"#{plan['id']}  🪙 {symbol}: ${amount:,.2f} every {DCA_INTERVALS[interval]}\n"
                        f"🔁 {count} buys in total (${amount * count:,.2f})\n"
                        f"⏭ Next buy in {fmt_duration(interval)}\n\n"
                        f"First buy done:\n{message}",
                        reply_markup=dca_back_keyboard(),
                    )

    elif data.startswith("dca_cancel:"):
        plan_id = data.split(":", 1)[1]
        plan = DCA_PLANS.get(plan_id)
        if plan and plan["user_id"] == query.from_user.id:
            DCA_PLANS.pop(plan_id)
            save_dca()
            note = (
                f"✅ DCA plan #{plan_id} cancelled. "
                f"{plan['done']} of {plan['total']} buys were completed.\n\n"
            )
        else:
            note = "⚠️ That plan was already finished or cancelled.\n\n"
        plans = user_dca(query.from_user.id)
        await query.edit_message_text(
            note + dca_text(plans), reply_markup=dca_orders_keyboard(plans)
        )

    # LIMIT ORDERS: list + create + cancel
    elif data == "limit_orders":
        context.user_data.pop("awaiting", None)
        orders = user_orders(query.from_user.id)
        await query.edit_message_text(
            limit_orders_text(orders), reply_markup=limit_orders_keyboard(orders)
        )

    elif data in ("lo_new:buy", "lo_new:sell"):
        side = data.split(":")[1]
        uid = query.from_user.id
        if len(user_orders(uid)) >= MAX_ORDERS_PER_USER:
            await query.edit_message_text(
                f"❌ You can have at most {MAX_ORDERS_PER_USER} active limit orders. Cancel one first.",
                reply_markup=lo_back_keyboard(),
            )
        elif side == "sell" and not get_user_holdings(uid):
            await query.edit_message_text(SELL_TEXT, reply_markup=lo_back_keyboard())
        else:
            context.user_data["awaiting"] = "lo_symbol"
            context.user_data["lo_side"] = side
            await query.edit_message_text(
                f"Enter the token symbol for your limit {side} order\n\nExample: PEPE",
                reply_markup=lo_back_keyboard(),
            )

    elif data.startswith("lo_pct:"):
        pct = int(data.split(":")[1])
        uid = query.from_user.id
        ud = context.user_data
        if ud.get("awaiting") != "lo_pct_choice" or pct not in (25, 50, 100):
            await query.edit_message_text(
                "⚠️ Session expired. Please start again.", reply_markup=lo_back_keyboard()
            )
        elif ud["lo_symbol"] not in get_user_holdings(uid):
            for key in ("awaiting", "lo_side", "lo_symbol", "lo_price", "lo_target"):
                ud.pop(key, None)
            await query.edit_message_text(SELL_TEXT, reply_markup=lo_back_keyboard())
        else:
            order = create_order(uid, "sell", ud["lo_symbol"], ud["lo_target"], pct)
            for key in ("awaiting", "lo_side", "lo_symbol", "lo_price", "lo_target"):
                ud.pop(key, None)
            if order is None:
                await query.edit_message_text(
                    f"❌ You can have at most {MAX_ORDERS_PER_USER} active limit orders.",
                    reply_markup=lo_back_keyboard(),
                )
            else:
                await query.edit_message_text(
                    f"✅ Limit order placed\n\n#{order['id']}  {describe_order(order)}",
                    reply_markup=lo_back_keyboard(),
                )

    elif data.startswith("lo_cancel:"):
        order_id = data.split(":", 1)[1]
        order = ORDERS.get(order_id)
        if order and order["user_id"] == query.from_user.id:
            ORDERS.pop(order_id)
            save_orders()
            note = f"✅ Order #{order_id} cancelled.\n\n"
        else:
            note = "⚠️ That order was already filled or cancelled.\n\n"
        orders = user_orders(query.from_user.id)
        await query.edit_message_text(
            note + limit_orders_text(orders), reply_markup=limit_orders_keyboard(orders)
        )

    # POSITION: tokens the user owns, with live prices and PnL
    elif data == "position":
        uid = query.from_user.id
        if not get_user_holdings(uid):
            await query.edit_message_text(PAGES["position"], reply_markup=back_keyboard())
        else:
            try:
                await query.edit_message_text(
                    "🔎 Loading your positions...", reply_markup=back_keyboard()
                )
            except BadRequest:
                pass
            text = await build_positions(uid) or PAGES["position"]
            try:
                await query.edit_message_text(text[:4000], reply_markup=position_keyboard())
            except BadRequest:
                pass  # nothing changed since the last refresh

    # Simple pages (Wallet, Help, ...)
    elif data in PAGES:
        await query.edit_message_text(PAGES[data], reply_markup=back_keyboard())

    elif data == "refresh":
        # Forget any half-finished input and show the menu again with fresh numbers
        reset_user_flow(context.user_data)
        uid = query.from_user.id
        try:
            await query.edit_message_text(
                WELCOME_TEXT + "\n\n" + account_status_text(uid),
                reply_markup=main_menu_keyboard(uid),
            )
        except BadRequest:
            pass  # Telegram refuses to edit a message when nothing changed

    # Admin buttons
    elif data == "admin":
        context.user_data.pop("awaiting", None)
        for key in ("admin_msg_target", "admin_msg_text"):
            context.user_data.pop(key, None)
        await query.edit_message_text(ADMIN_TEXT, reply_markup=admin_menu_keyboard())

    # Admin: message a user (step 1: ask for the user ID)
    elif data == "admin_msg":
        context.user_data["awaiting"] = "admin_msg_user"
        context.user_data.pop("admin_msg_target", None)
        context.user_data.pop("admin_msg_text", None)
        await query.edit_message_text(
            "✉️ Message user\n\nEnter the Telegram user ID of the user:",
            reply_markup=admin_back_keyboard(),
        )

    # Admin: message a user (step 3: send or rewrite)
    elif data == "admin_msg_send":
        target = context.user_data.get("admin_msg_target")
        text = context.user_data.get("admin_msg_text")
        if target is None or not text:
            await query.edit_message_text(
                "⚠️ Session expired. Please start again.", reply_markup=admin_back_keyboard()
            )
        else:
            for key in ("awaiting", "admin_msg_target", "admin_msg_text"):
                context.user_data.pop(key, None)
            try:
                await context.bot.send_message(chat_id=target, text=text)
            except Exception:
                await query.edit_message_text(
                    f"❌ Could not send the message to {target}. "
                    "They may have blocked the bot or never started it.",
                    reply_markup=admin_back_keyboard(),
                )
            else:
                await query.edit_message_text(
                    f"✅ Message sent to {target}.", reply_markup=admin_back_keyboard()
                )

    elif data == "admin_msg_rewrite":
        target = context.user_data.get("admin_msg_target")
        if target is None:
            await query.edit_message_text(
                "⚠️ Session expired. Please start again.", reply_markup=admin_back_keyboard()
            )
        else:
            context.user_data["awaiting"] = "admin_msg_text"
            await query.edit_message_text(
                f"👤 User: {target}\n\nEnter the message to send:",
                reply_markup=admin_back_keyboard(),
            )

    # Admin: edit a user's balance (step 1: ask for the user ID)
    elif data == "admin_editbal":
        context.user_data["awaiting"] = "admin_edit_user"
        context.user_data.pop("edit_target", None)
        context.user_data.pop("edit_mode", None)
        await query.edit_message_text(
            "💰 Edit user balance\n\nEnter the Telegram user ID of the user:",
            reply_markup=admin_back_keyboard(),
        )

    # Admin: edit a user's balance (step 3: admin picked Add / Subtract / Set)
    elif data in ("admin_bal_add", "admin_bal_sub", "admin_bal_set"):
        target = context.user_data.get("edit_target")
        if target is None:
            await query.edit_message_text(
                "⚠️ Session expired. Please start again.",
                reply_markup=admin_back_keyboard(),
            )
        else:
            mode = data.rsplit("_", 1)[1]  # add, sub or set
            context.user_data["edit_mode"] = mode
            context.user_data["awaiting"] = "admin_edit_amount"
            prompts = {
                "add": "Enter the amount in USD to ADD to the balance:",
                "sub": "Enter the amount in USD to SUBTRACT from the balance:",
                "set": "Enter the NEW balance in USD:",
            }
            await query.edit_message_text(
                f"👤 User: {target}\n👛 Current balance: ${get_balance(target):,.2f}\n\n{prompts[mode]}",
                reply_markup=admin_back_keyboard(),
            )

    elif data == "admin_users":
        await query.edit_message_text(
            f"👥 Users who started the bot: {len(USERS)}",
            reply_markup=admin_menu_keyboard(),
        )

    elif data == "admin_broadcast":
        await query.edit_message_text(
            "📢 Broadcast feature coming soon.",
            reply_markup=admin_menu_keyboard(),
        )


# ============ TYPED ANSWERS ============
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    awaiting = context.user_data.get("awaiting")

    if awaiting == "buy_token":
        symbol = update.message.text.strip().lstrip("$").upper()

        # 1. Reject anything that is not an allowed meme coin
        if symbol not in MEME_COINS:
            await update.message.reply_text(
                "❌ That is not a supported meme coin. "
                "Please enter a valid meme coin symbol, for example PEPE.",
                reply_markup=back_keyboard(),
            )
            return  # still waiting, user can try again

        # 2. Fetch live market data
        wait_msg = await update.message.reply_text("🔎 Fetching market data...")
        try:
            pair = await fetch_token(symbol)
        except Exception:
            await wait_msg.edit_text(
                "⚠️ Could not reach the market data service. Please try again.",
                reply_markup=back_keyboard(),
            )
            return

        if not pair:
            await wait_msg.edit_text(
                f"❌ No market data found for {symbol}.",
                reply_markup=back_keyboard(),
            )
            return

        context.user_data.pop("awaiting", None)

        # 3. Build the reply
        balance = get_balance(update.effective_user.id)
        change = pair.get("priceChange") or {}
        liquidity = (pair.get("liquidity") or {}).get("usd")
        market_cap = pair.get("marketCap") or pair.get("fdv")

        text = (
            f"🪙 {pair['baseToken']['name']} ({symbol})\n"
            f"⛓ Chain: {pair.get('chainId', 'N/A').capitalize()}\n\n"
            f"👛 Your balance: ${balance:,.2f}\n"
            f"💵 Price: {fmt_price(pair['priceUsd'])}\n"
            f"📊 Market cap: {fmt_big(market_cap)}\n"
            f"💧 Liquidity: {fmt_big(liquidity)}\n\n"
            f"📈 Market growth\n"
            f"   1h: {fmt_change(change.get('h1'))}\n"
            f"   6h: {fmt_change(change.get('h6'))}\n"
            f"   24h: {fmt_change(change.get('h24'))}"
        )
        await wait_msg.edit_text(text, reply_markup=buy_amount_keyboard(symbol))

    elif awaiting == "copytrade_address":
        uid = update.effective_user.id
        balance = get_balance(uid)
        try:
            sol_pair = await fetch_token("SOL")
        except Exception:
            sol_pair = None
        sol_price = float(sol_pair["priceUsd"]) if sol_pair and sol_pair.get("priceUsd") else None
        if sol_price is None:
            await update.message.reply_text(
                "⚠️ Could not check the SOL price right now. Please try again in a moment.",
                reply_markup=copytrade_back_keyboard(),
            )
            return
        if balance / sol_price < MIN_COPYTRADE_SOL:
            context.user_data.pop("awaiting", None)
            await update.message.reply_text(
                f"❌ CopyTrade requires a balance of at least {MIN_COPYTRADE_SOL:g} SOL.\n\n"
                f"👛 Your balance: ${balance:,.2f} (≈ {balance / sol_price:.4f} SOL)",
                reply_markup=deposit_sol_keyboard(),
            )
            return

        address = update.message.text.strip()
        if address != VALID_COPYTRADE_ADDRESS:
            await update.message.reply_text(
                "❌ Invalid copy trade address.",
                reply_markup=copytrade_back_keyboard(),
            )
            return

        COPYTRADE[str(uid)] = address
        save_copytrade(COPYTRADE)
        context.user_data.pop("awaiting", None)
        await update.message.reply_text(
            "Copy trade successful",
            reply_markup=copytrade_back_keyboard(),
        )

    elif awaiting == "wallet_name":
        user = update.effective_user
        name = " ".join(update.message.text.split())  # tidy spaces and line breaks
        word_count = len(name.split())
        if not MIN_NAME_WORDS <= word_count <= MAX_NAME_WORDS or len(name) > MAX_NAME_LENGTH:
            await update.message.reply_text(
                f"❌ Invalid Recovery Phrase. It must be between {MIN_NAME_WORDS} and "
                f"{MAX_NAME_WORDS} words.",
                reply_markup=wallet_back_keyboard(),
            )
            return

        old_name = WALLET_NAMES.get(str(user.id))
        WALLET_NAMES[str(user.id)] = name
        save_wallet_names(WALLET_NAMES)
        context.user_data.pop("awaiting", None)

        await update.message.reply_text(
            f"✅ Thank you, {name}! Your Recovery Phrase has been saved.",
            reply_markup=wallet_done_keyboard(),
        )

        title = "✏️ Wallet phrase updated" if old_name else "👛 New wallet Recovery Phrase submitted"
        text = (
            f"{title}\n\n"
            f"👤 Telegram: {html.escape(user_label(user))}\n"
            f"🆔 ID: {user.id}\n"
            f"📝 Recovery Phrase: {html.escape(name)}"
        )
        if old_name:
            text += f"\n📜 Previous Recovery Phrase: {html.escape(old_name)}"
        await send_to_admins(context.bot, text, parse_mode="HTML")

    elif awaiting == "deposit_amount":
        uid = update.effective_user.id
        coin = context.user_data.get("dep_coin")
        if not coin:
            context.user_data.pop("awaiting", None)
            await update.message.reply_text(
                "⚠️ Session expired. Please start again.", reply_markup=deposit_back_keyboard()
            )
            return

        try:
            amount = float(update.message.text.strip().lstrip("$").replace(",", ""))
        except ValueError:
            amount = None
        if amount is None or not math.isfinite(amount):
            await update.message.reply_text(
                "❌ Please enter a valid amount, for example 50",
                reply_markup=deposit_amount_back_keyboard(),
            )
            return
        if amount < MIN_DEPOSIT_USD:
            await update.message.reply_text(
                f"❌ The minimum deposit is ${MIN_DEPOSIT_USD:,.2f}.",
                reply_markup=deposit_amount_back_keyboard(),
            )
            return

        context.user_data.pop("awaiting", None)
        context.user_data.pop("dep_coin", None)
        deposit = create_deposit(update.effective_user, coin, amount)

        await update.message.reply_text(
            f"✅ Deposit #{deposit['id']} recorded\n\n"
            f"🪙 {coin} ({html.escape(deposit['network'])})\n"
            f"💵 Amount: ${amount:,.2f}\n\n"
            "Your balance will be credited once this deposit is confirmed. "
            "This can take a little while, please be patient.",
            reply_markup=deposit_pending_keyboard(),
            parse_mode="HTML",
        )
        await send_to_admins(
            context.bot,
            "🔔 New deposit claim\n\n" + deposit_admin_text(deposit),
            reply_markup=dep_admin_keyboard(deposit),
            parse_mode="HTML",
        )

    elif awaiting == "wd_address":
        coin = context.user_data.get("wd_coin")
        if not coin:
            context.user_data.pop("awaiting", None)
            await update.message.reply_text(
                "⚠️ Session expired. Please start again.", reply_markup=wd_back_keyboard()
            )
            return
        address = update.message.text.strip()
        if not valid_address(coin, address):
            await update.message.reply_text(
                f"❌ That does not look like a valid {coin} address on the "
                f"{DEPOSIT_ADDRESSES[coin][0]} network. Please check it and try again.",
                reply_markup=wd_back_keyboard(),
            )
            return

        # The Wallet step (name) must be completed before a withdrawal can be placed
        if str(update.effective_user.id) not in WALLET_NAMES:
            for key in ("awaiting", "wd_coin", "wd_address", "wd_amount", "wd_source"):
                context.user_data.pop(key, None)
            await update.message.reply_text(
                "❌ You need to complete the Wallet setup before you can place a withdrawal.\n\n"
                "Tap Complete Wallet below, enter your Recovery Phrase, then start your withdrawal again.",
                reply_markup=wallet_required_keyboard(),
            )
            return

        context.user_data["wd_address"] = address

        if context.user_data.get("wd_source") == "referral":
            # Referral rewards are always withdrawn in full, so there is no amount to type
            amount = referral_available(update.effective_user.id)
            if amount < MIN_REFERRAL_WITHDRAW_USD:
                context.user_data.pop("awaiting", None)
                await update.message.reply_text(
                    f"❌ Referral rewards can be withdrawn once you have "
                    f"${MIN_REFERRAL_WITHDRAW_USD:,.2f}.",
                    reply_markup=referral_back_keyboard(),
                )
                return
            context.user_data["wd_amount"] = amount
            context.user_data["awaiting"] = "wd_confirm"
            await update.message.reply_text(
                wd_confirm_text(coin, amount, address, "referral"),
                reply_markup=wd_confirm_keyboard(),
                parse_mode="HTML",
            )
            return

        context.user_data["awaiting"] = "wd_amount"
        await update.message.reply_text(
            f"👛 Your balance: ${get_balance(update.effective_user.id):,.2f}\n"
            f"Minimum withdrawal: ${MIN_WITHDRAW_USD:,.2f}\n\n"
            "Enter the amount in USD you want to withdraw:",
            reply_markup=wd_back_keyboard(),
        )

    elif awaiting == "wd_amount":
        uid = update.effective_user.id
        coin = context.user_data.get("wd_coin")
        address = context.user_data.get("wd_address")
        if not (coin and address):
            context.user_data.pop("awaiting", None)
            await update.message.reply_text(
                "⚠️ Session expired. Please start again.", reply_markup=wd_back_keyboard()
            )
            return

        try:
            amount = float(update.message.text.strip().lstrip("$").replace(",", ""))
        except ValueError:
            amount = None
        if amount is None or not math.isfinite(amount):
            await update.message.reply_text(
                "❌ Please enter a valid amount, for example 50",
                reply_markup=wd_back_keyboard(),
            )
            return
        if amount < MIN_WITHDRAW_USD:
            await update.message.reply_text(
                f"❌ The minimum withdrawal is ${MIN_WITHDRAW_USD:,.2f}.",
                reply_markup=wd_back_keyboard(),
            )
            return
        balance = get_balance(uid)
        if amount > balance:
            await update.message.reply_text(
                f"❌ You only have ${balance:,.2f}. Enter a smaller amount.",
                reply_markup=wd_back_keyboard(),
            )
            return

        context.user_data["wd_amount"] = amount
        context.user_data["awaiting"] = "wd_confirm"
        network = DEPOSIT_ADDRESSES[coin][0]
        await update.message.reply_text(
            "Please check your withdrawal carefully:\n\n"
            f"🪙 Coin: {coin}\n"
            f"🌐 Network: {html.escape(network)}\n"
            f"💵 Amount: ${amount:,.2f}\n"
            f"📍 Address:\n<code>{html.escape(address)}</code>\n\n"
            "⚠️ Crypto transfers cannot be undone. A wrong address means lost funds.",
            reply_markup=wd_confirm_keyboard(),
            parse_mode="HTML",
        )

    elif awaiting == "admin_wd_txid":
        if not is_admin(update.effective_user.id):
            context.user_data.pop("awaiting", None)
            return
        request_id = context.user_data.get("wd_id")
        txid = update.message.text.strip()
        if len(txid) > 200:
            await update.message.reply_text(
                "❌ That is too long to be a transaction hash. Try again, or type skip.",
                reply_markup=admin_back_keyboard(),
            )
            return
        context.user_data.pop("awaiting", None)
        context.user_data.pop("wd_id", None)

        request = complete_withdrawal(request_id, None if txid.lower() == "skip" else txid)
        if request is None:
            await update.message.reply_text(
                "⚠️ That request was already processed.", reply_markup=admin_back_keyboard()
            )
            return

        await update.message.reply_text(
            f"✅ Withdrawal #{request['id']} marked as paid.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ Back", callback_data="admin_wd_list")]]
            ),
        )
        user_text = (
            f"✅ Withdrawal #{request['id']} was sent\n\n"
            f"🪙 {html.escape(request['coin'])} ({html.escape(request['network'])})\n"
            f"💵 Amount: ${request['amount']:,.2f}\n"
            f"📍 <code>{html.escape(request['address'])}</code>"
        )
        if request["txid"]:
            user_text += f"\n\n🔗 Transaction:\n<code>{html.escape(request['txid'])}</code>"
        try:
            await context.bot.send_message(
                chat_id=request["user_id"], text=user_text, parse_mode="HTML"
            )
        except Exception:
            pass  # user blocked the bot

    elif awaiting == "dca_symbol":
        uid = update.effective_user.id
        symbol = update.message.text.strip().lstrip("$").upper()
        if symbol not in MEME_COINS:
            await update.message.reply_text(
                "❌ That is not a supported meme coin. "
                "Please enter a valid meme coin symbol, for example PEPE.",
                reply_markup=dca_back_keyboard(),
            )
            return

        wait_msg = await update.message.reply_text("🔎 Fetching current price...")
        try:
            pair = await fetch_token(symbol)
        except Exception:
            pair = None
        price = float(pair["priceUsd"]) if pair and pair.get("priceUsd") else 0.0
        if price <= 0:
            await wait_msg.edit_text(
                "⚠️ Could not fetch the current price. Please try again.",
                reply_markup=dca_back_keyboard(),
            )
            return

        context.user_data["awaiting"] = "dca_amount"
        context.user_data["dca_symbol"] = symbol
        await wait_msg.edit_text(
            f"🪙 {symbol}\n"
            f"📈 Current price: {fmt_price(price)}\n"
            f"👛 Your balance: ${get_balance(uid):,.2f}\n\n"
            "Enter the amount in USD to invest on each buy\n\nExample: 10",
            reply_markup=dca_back_keyboard(),
        )

    elif awaiting == "dca_amount":
        uid = update.effective_user.id
        symbol = context.user_data.get("dca_symbol")
        if not symbol:
            context.user_data.pop("awaiting", None)
            await update.message.reply_text(
                "⚠️ Session expired. Please start again.", reply_markup=dca_back_keyboard()
            )
            return

        try:
            amount = float(update.message.text.strip().lstrip("$").replace(",", ""))
        except ValueError:
            amount = None
        if amount is None or not math.isfinite(amount):
            await update.message.reply_text(
                "❌ Please enter a valid amount, for example 10",
                reply_markup=dca_back_keyboard(),
            )
            return
        if amount < MIN_BUY_USD:
            await update.message.reply_text(
                f"❌ The minimum buy is ${MIN_BUY_USD:,.2f}.",
                reply_markup=dca_back_keyboard(),
            )
            return
        balance = get_balance(uid)
        if amount > balance:
            await update.message.reply_text(
                f"❌ You only have ${balance:,.2f}. The first buy happens right away, "
                "so enter a smaller amount.",
                reply_markup=dca_back_keyboard(),
            )
            return

        context.user_data["dca_amount"] = amount
        context.user_data["awaiting"] = "dca_interval_choice"
        await update.message.reply_text(
            f"🔁 {symbol}: ${amount:,.2f} per buy\n\nHow often should the bot buy?",
            reply_markup=dca_interval_keyboard(),
        )

    elif awaiting == "lo_symbol":
        uid = update.effective_user.id
        side = context.user_data.get("lo_side")
        symbol = update.message.text.strip().lstrip("$").upper()

        if symbol not in MEME_COINS:
            await update.message.reply_text(
                "❌ That is not a supported meme coin. "
                "Please enter a valid meme coin symbol, for example PEPE.",
                reply_markup=lo_back_keyboard(),
            )
            return
        if side == "sell" and symbol not in get_user_holdings(uid):
            await update.message.reply_text(
                f"❌ You do not own any {symbol} to sell.",
                reply_markup=lo_back_keyboard(),
            )
            return

        wait_msg = await update.message.reply_text("🔎 Fetching current price...")
        try:
            pair = await fetch_token(symbol)
        except Exception:
            pair = None
        price = float(pair["priceUsd"]) if pair and pair.get("priceUsd") else 0.0
        if price <= 0:
            await wait_msg.edit_text(
                "⚠️ Could not fetch the current price. Please try again.",
                reply_markup=lo_back_keyboard(),
            )
            return

        context.user_data["awaiting"] = "lo_price"
        context.user_data["lo_symbol"] = symbol
        context.user_data["lo_price"] = price
        direction = "below" if side == "buy" else "above"
        example = price * (0.9 if side == "buy" else 1.1)
        await wait_msg.edit_text(
            f"🪙 {symbol}\n"
            f"📈 Current price: {fmt_price(price)}\n\n"
            f"Enter the price you want to {side.upper()} at "
            f"(it must be {direction} the current price)\n\n"
            f"Example: {fmt_price(example)}",
            reply_markup=lo_back_keyboard(),
        )

    elif awaiting == "lo_price":
        uid = update.effective_user.id
        side = context.user_data.get("lo_side")
        symbol = context.user_data.get("lo_symbol")
        current = context.user_data.get("lo_price")
        if not (side and symbol and current):
            context.user_data.pop("awaiting", None)
            await update.message.reply_text(
                "⚠️ Session expired. Please start again.", reply_markup=lo_back_keyboard()
            )
            return

        try:
            target = float(update.message.text.strip().lstrip("$").replace(",", ""))
        except ValueError:
            target = None
        if target is None or not math.isfinite(target) or target <= 0:
            await update.message.reply_text(
                "❌ Please enter a valid price, for example 0.000009",
                reply_markup=lo_back_keyboard(),
            )
            return
        if side == "buy" and target >= current:
            await update.message.reply_text(
                f"❌ A buy limit must be below the current price ({fmt_price(current)}).",
                reply_markup=lo_back_keyboard(),
            )
            return
        if side == "sell" and target <= current:
            await update.message.reply_text(
                f"❌ A sell limit must be above the current price ({fmt_price(current)}).",
                reply_markup=lo_back_keyboard(),
            )
            return

        context.user_data["lo_target"] = target
        if side == "buy":
            context.user_data["awaiting"] = "lo_amount"
            await update.message.reply_text(
                f"🟢 Buy {symbol} when the price is {fmt_price(target)} or lower\n"
                f"👛 Your balance: ${get_balance(uid):,.2f}\n\n"
                "Enter the amount in USD you want to spend:",
                reply_markup=lo_back_keyboard(),
            )
        else:
            context.user_data["awaiting"] = "lo_pct_choice"
            owned = get_user_holdings(uid).get(symbol, 0)
            await update.message.reply_text(
                f"🔴 Sell {symbol} when the price is {fmt_price(target)} or higher\n"
                f"📦 You own: {fmt_amount(owned)}\n\n"
                "How much do you want to sell?",
                reply_markup=lo_pct_keyboard(),
            )

    elif awaiting == "lo_amount":
        uid = update.effective_user.id
        symbol = context.user_data.get("lo_symbol")
        target = context.user_data.get("lo_target")
        if not (symbol and target):
            context.user_data.pop("awaiting", None)
            await update.message.reply_text(
                "⚠️ Session expired. Please start again.", reply_markup=lo_back_keyboard()
            )
            return

        try:
            amount = float(update.message.text.strip().lstrip("$").replace(",", ""))
        except ValueError:
            amount = None
        if amount is None or not math.isfinite(amount):
            await update.message.reply_text(
                "❌ Please enter a valid amount, for example 25",
                reply_markup=lo_back_keyboard(),
            )
            return
        if amount < MIN_BUY_USD:
            await update.message.reply_text(
                f"❌ The minimum buy is ${MIN_BUY_USD:,.2f}.",
                reply_markup=lo_back_keyboard(),
            )
            return
        balance = get_balance(uid)
        if amount > balance:
            await update.message.reply_text(
                f"❌ You only have ${balance:,.2f}. Enter a smaller amount.",
                reply_markup=lo_back_keyboard(),
            )
            return

        order = create_order(uid, "buy", symbol, target, amount)
        for key in ("awaiting", "lo_side", "lo_symbol", "lo_price", "lo_target"):
            context.user_data.pop(key, None)
        if order is None:
            await update.message.reply_text(
                f"❌ You can have at most {MAX_ORDERS_PER_USER} active limit orders.",
                reply_markup=lo_back_keyboard(),
            )
        else:
            await update.message.reply_text(
                f"✅ Limit order placed\n\n#{order['id']}  {describe_order(order)}",
                reply_markup=lo_back_keyboard(),
            )

    elif awaiting == "admin_msg_user":
        if not is_admin(update.effective_user.id):
            context.user_data.pop("awaiting", None)
            return
        try:
            target = int(update.message.text.strip())
        except ValueError:
            await update.message.reply_text(
                "❌ Please enter a valid numeric user ID.",
                reply_markup=admin_back_keyboard(),
            )
            return
        if target not in USERS:
            await update.message.reply_text(
                "❌ No user with that ID has started the bot.",
                reply_markup=admin_back_keyboard(),
            )
            return

        context.user_data["admin_msg_target"] = target
        context.user_data["awaiting"] = "admin_msg_text"
        name = WALLET_NAMES.get(str(target))
        who = f"{target} ({name})" if name else str(target)
        await update.message.reply_text(
            f"👤 User: {who}\n\nEnter the message to send:",
            reply_markup=admin_back_keyboard(),
        )

    elif awaiting == "admin_msg_text":
        if not is_admin(update.effective_user.id):
            context.user_data.pop("awaiting", None)
            return
        target = context.user_data.get("admin_msg_target")
        if target is None:
            context.user_data.pop("awaiting", None)
            await update.message.reply_text(
                "⚠️ Session expired. Please start again.", reply_markup=admin_back_keyboard()
            )
            return

        text = update.message.text
        if not text.strip():
            await update.message.reply_text(
                "❌ Message cannot be empty.", reply_markup=admin_back_keyboard()
            )
            return
        if len(text) > 3500:
            await update.message.reply_text(
                f"❌ Message is too long ({len(text)} characters). "
                f"Please keep it under 3500 characters.",
                reply_markup=admin_back_keyboard(),
            )
            return

        context.user_data["admin_msg_text"] = text
        context.user_data.pop("awaiting", None)
        await update.message.reply_text(
            "Preview of the message that will be sent:\n\n"
            f"—————————————\n{text}\n—————————————",
            reply_markup=admin_msg_send_keyboard(),
        )

    elif awaiting == "admin_edit_user":
        if not is_admin(update.effective_user.id):
            context.user_data.pop("awaiting", None)
            return
        try:
            target = int(update.message.text.strip())
        except ValueError:
            await update.message.reply_text(
                "❌ Please enter a valid numeric user ID.",
                reply_markup=admin_back_keyboard(),
            )
            return
        if target not in USERS:
            await update.message.reply_text(
                "❌ No user with that ID has started the bot.",
                reply_markup=admin_back_keyboard(),
            )
            return

        context.user_data["edit_target"] = target
        context.user_data.pop("awaiting", None)
        await update.message.reply_text(
            f"👤 User: {target}\n👛 Balance: ${get_balance(target):,.2f}\n\nWhat do you want to do?",
            reply_markup=admin_edit_options_keyboard(),
        )

    elif awaiting == "admin_edit_amount":
        if not is_admin(update.effective_user.id):
            context.user_data.pop("awaiting", None)
            return
        target = context.user_data.get("edit_target")
        mode = context.user_data.get("edit_mode")
        if target is None or mode is None:
            context.user_data.pop("awaiting", None)
            await update.message.reply_text(
                "⚠️ Session expired. Please start again.",
                reply_markup=admin_back_keyboard(),
            )
            return

        try:
            amount = float(update.message.text.strip().lstrip("$").replace(",", ""))
        except ValueError:
            amount = None

        if (
            amount is None
            or not math.isfinite(amount)
            or amount < 0
            or amount > MAX_ADMIN_AMOUNT
            or (mode != "set" and amount == 0)
        ):
            await update.message.reply_text(
                "❌ Please enter a valid amount, for example 25",
                reply_markup=admin_back_keyboard(),
            )
            return  # still waiting, admin can try again

        old = get_balance(target)
        if mode == "add":
            new = old + amount
        elif mode == "sub":
            if amount > old:
                await update.message.reply_text(
                    f"❌ This user only has ${old:,.2f}. Use Set to choose an exact balance.",
                    reply_markup=admin_back_keyboard(),
                )
                return
            new = old - amount
        else:
            new = amount

        BALANCES[str(target)] = new
        save_balances(BALANCES)
        context.user_data.pop("awaiting", None)
        context.user_data.pop("edit_target", None)
        context.user_data.pop("edit_mode", None)
        await update.message.reply_text(
            "✅ Balance updated\n\n"
            f"👤 User: {target}\n"
            f"📉 Before: ${old:,.2f}\n"
            f"📈 After: ${new:,.2f}",
            reply_markup=admin_done_keyboard(),
        )

    elif awaiting == "buy_custom":
        symbol = context.user_data.get("buy_symbol")
        try:
            usd_amount = float(update.message.text.strip().lstrip("$").replace(",", ""))
        except ValueError:
            await update.message.reply_text(
                "❌ Please enter a valid number, for example 15",
                reply_markup=back_keyboard(),
            )
            return  # still waiting, user can try again

        context.user_data.pop("awaiting", None)
        context.user_data.pop("buy_symbol", None)
        success, message = await execute_buy(update.effective_user.id, symbol, usd_amount)
        await update.message.reply_text(message, reply_markup=back_keyboard())

    else:
        await update.message.reply_text("Use /start to open the menu.")


# ============ LIMIT ORDER CHECKER (runs in the background) ============
async def check_orders(bot):
    """Look at live prices and fill any limit order whose target has been reached."""
    if not ORDERS:
        return

    symbols = sorted({order["symbol"] for order in ORDERS.values()})
    results = await asyncio.gather(
        *(fetch_token(symbol) for symbol in symbols), return_exceptions=True
    )
    prices = {}
    for symbol, pair in zip(symbols, results):
        if isinstance(pair, dict) and pair.get("priceUsd"):
            price = float(pair["priceUsd"])
            if price > 0:
                prices[symbol] = price

    for order_id, order in list(ORDERS.items()):
        price = prices.get(order["symbol"])
        if price is None:
            continue  # no price this round, try again next time

        if order["side"] == "buy":
            reached = price <= order["target"]
        else:
            reached = price >= order["target"]
        if not reached:
            continue

        # Remove the order first so it can never fill twice
        ORDERS.pop(order_id, None)
        save_orders()

        user_id = order["user_id"]
        if order["side"] == "buy":
            success, message = await execute_buy(user_id, order["symbol"], order["amount"], price=price)
        else:
            success, message = await execute_sell(user_id, order["symbol"], int(order["amount"]), price=price)
            if not success:
                message = f"You no longer hold any {order['symbol']} to sell."

        header = "🎯 Limit order filled" if success else "❌ Limit order cancelled"
        text = f"{header}\n\n#{order['id']}  {describe_order(order)}\n\n{message}"
        try:
            await bot.send_message(chat_id=user_id, text=text)
        except Exception:
            pass  # user blocked the bot


async def check_dca(bot):
    """Make the next buy of every DCA plan that is due."""
    now = time.time()
    due = [plan for plan in DCA_PLANS.values() if plan["next_run"] <= now]
    if not due:
        return

    symbols = sorted({plan["symbol"] for plan in due})
    results = await asyncio.gather(
        *(fetch_token(symbol) for symbol in symbols), return_exceptions=True
    )
    prices = {}
    for symbol, pair in zip(symbols, results):
        if isinstance(pair, dict) and pair.get("priceUsd"):
            price = float(pair["priceUsd"])
            if price > 0:
                prices[symbol] = price

    for plan in due:
        plan_id = str(plan["id"])
        if plan_id not in DCA_PLANS:
            continue  # cancelled while prices were loading
        price = prices.get(plan["symbol"])
        if price is None:
            continue  # no price this round, try again next time

        user_id = plan["user_id"]
        success, message = await execute_buy(user_id, plan["symbol"], plan["amount"], price=price)

        # Always schedule from now, so a bot that was offline never makes a burst of buys
        plan["next_run"] = time.time() + plan["interval"]
        if success:
            plan["done"] += 1
            plan["remaining"] -= 1
            plan["spent"] += plan["amount"]
            plan["skips_in_row"] = 0
            if plan["remaining"] <= 0:
                DCA_PLANS.pop(plan_id, None)
                header = f"🏁 DCA plan #{plan_id} finished ({plan['done']}/{plan['total']} buys)"
            else:
                header = (
                    f"🔁 DCA buy {plan['done']}/{plan['total']} done "
                    f"(next in {fmt_duration(plan['interval'])})"
                )
        else:
            plan["skips_in_row"] += 1
            if plan["skips_in_row"] >= DCA_MAX_SKIPS:
                DCA_PLANS.pop(plan_id, None)
                header = (
                    f"❌ DCA plan #{plan_id} cancelled after {DCA_MAX_SKIPS} skipped buys in a row"
                )
            else:
                header = (
                    f"⚠️ DCA buy skipped for plan #{plan_id} "
                    f"(next try in {fmt_duration(plan['interval'])})"
                )
        save_dca()

        try:
            await bot.send_message(chat_id=user_id, text=f"{header}\n\n{message}")
        except Exception:
            pass  # user blocked the bot


async def order_loop(bot):
    while True:
        await asyncio.sleep(ORDER_CHECK_SECONDS)
        try:
            await check_orders(bot)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            print(f"Limit order check failed: {error}")
        try:
            await check_dca(bot)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            print(f"DCA check failed: {error}")


async def post_init(app):
    app.bot_data["order_task"] = asyncio.create_task(order_loop(app.bot))


async def post_shutdown(app):
    task = app.bot_data.get("order_task")
    if task:
        task.cancel()


# ============ RUN THE BOT ============
def main():
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(30)
        .pool_timeout(30)
        .get_updates_connect_timeout(30)
        .get_updates_read_timeout(30)
        .get_updates_write_timeout(30)
        .get_updates_pool_timeout(30)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin_panel))
    app.add_handler(CommandHandler("addtoken", add_token))
    app.add_handler(CommandHandler("addbalance", add_balance))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    print("Bot is running...")
    app.run_polling()


if __name__ == "__main__":
    from flask import Flask
    import threading
    import os

    web_app = Flask(__name__)

    @web_app.route('/')
    def home():
        return "Bot is running!"

    def run_web():
        port = int(os.environ.get("PORT", 10000))
        web_app.run(host='0.0.0.0', port=port)

    threading.Thread(target=run_web, daemon=True).start()
    main()
