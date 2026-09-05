import os
import threading
import time
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
import numpy as np
import pandas as pd
import requests
import yfinance as yf

# --- TELEGRAM CONFIG ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")

# Teeno users ki Chat IDs yahan list me set karo
ALLOWED_CHAT_IDS = [
    "1345385952",              # Aapki Chat ID (Admin)
    "849346521",     # Second person ki Chat ID daalo
    "8548337411"      # Third person ki Chat ID daalo
]

# --- RISK & ALLOCATION CONFIG ---
RISK_CONFIG = {
    "trade_allocation_pct": 0.05,     # Har trade me amount ka exactly 5% lagega
    "max_open_trades": 5,             # Max 5 open trades allowed
    "daily_drawdown_limit_pct": 0.06  # Din ka max 6% loss circuit breaker
}

# --- PAPER TRADING ACCOUNT ---
PAPER_ACCOUNT = {
    "starting_balance": 10000.0,
    "cash": 10000.0,
    "realized_pnl": 0.0,
    "daily_starting_balance": 10000.0,
    "current_day": datetime.utcnow().day,
    "trading_halted_today": False
}

# Targets R:R Ratio: [TP1 = 1:2, TP2 = 1:2, TP3 = 1:5]
TARGET_RR_MULTIPLIERS = [2.0, 2.0, 5.0]

STRATEGIES = {
    "SCALP": {
        "label": "⚡ [SCALP - 15M/5M/1M]",
        "htf_interval": "15m", "htf_period": "5d",
        "mtf_interval": "5m", "mtf_period": "5d",
        "entry_interval": "1m", "entry_period": "1d",
        "adx_min": 23,
        "tp_multipliers": TARGET_RR_MULTIPLIERS
    },
    "INTRADAY": {
        "label": "⏱️ [INTRADAY - 1D/4H/1H to 5M/15M]",
        "htf_interval": "1d", "htf_period": "60d",
        "mtf_interval": "1h", "mtf_period": "30d",
        "entry_interval": "5m", "entry_period": "5d",
        "adx_min": 22,
        "tp_multipliers": TARGET_RR_MULTIPLIERS
    },
    "SWING": {
        "label": "🌊 [SWING - 1W/1D to 1H]",
        "htf_interval": "1wk", "htf_period": "2y",
        "mtf_interval": "1d", "mtf_period": "1y",
        "entry_interval": "1h", "entry_period": "1mo",
        "adx_min": 20,
        "tp_multipliers": TARGET_RR_MULTIPLIERS
    }
}

SYMBOLS = {}
active_positions = {strat: {} for strat in STRATEGIES}
EXCLUDED_STABLES = {"USDT", "USDC", "DAI", "BUSD", "FDUSD", "TUSD", "USDD", "PYUSD"}

def get_total_equity():
    invested = sum(
        p["trade_val"] for s in active_positions.values() for p in s.values() if p["side"] is not None
    )
    return PAPER_ACCOUNT["cash"] + invested

def get_open_positions_count():
    return sum(1 for s in active_positions.values() for p in s.values() if p["side"] is not None)

def check_daily_drawdown_circuit_breaker():
    global PAPER_ACCOUNT
    today = datetime.utcnow().day

    if today != PAPER_ACCOUNT["current_day"]:
        PAPER_ACCOUNT["current_day"] = today
        PAPER_ACCOUNT["daily_starting_balance"] = get_total_equity()
        PAPER_ACCOUNT["trading_halted_today"] = False

    current_equity = get_total_equity()
    daily_loss = PAPER_ACCOUNT["daily_starting_balance"] - current_equity
    max_allowed_loss = PAPER_ACCOUNT["daily_starting_balance"] * RISK_CONFIG["daily_drawdown_limit_pct"]

    if daily_loss >= max_allowed_loss and not PAPER_ACCOUNT["trading_halted_today"]:
        PAPER_ACCOUNT["trading_halted_today"] = True
        send_telegram(
            f"🚨 *RISK CIRCUIT BREAKER TRIGGERED*\n\n"
            f"Daily loss exceeded 6% limit (${daily_loss:,.2f}). Trading halted for today."
        )

def get_top_crypto_symbols(limit=50):
    global SYMBOLS, active_positions
    try:
        url = "https://api.coingecko.com/api/v3/coins/markets"
        params = {
            "vs_currency": "usd",
            "order": "market_cap_desc",
            "per_page": limit,
            "page": 1,
            "sparkline": "false"
        }
        res = requests.get(url, params=params, timeout=15).json()
        
        new_symbols = {}
        for item in res:
            sym = item.get("symbol", "").upper()
            if sym and sym not in EXCLUDED_STABLES:
                display_name = f"{sym}/USD"
                yf_ticker = f"{sym}-USD"
                new_symbols[display_name] = yf_ticker

        if new_symbols:
            SYMBOLS = new_symbols
            for strat in STRATEGIES:
                for name in SYMBOLS:
                    if name not in active_positions[strat]:
                        active_positions[strat][name] = {
                            "side": None, "entry": 0.0, "sl": 0.0, "tp": [],
                            "best_price": 0.0, "atr": 0.0, "units": 0.0, 
                            "trade_val": 0.0, "tp1_hit": False
                        }
            print(f"✅ Loaded {len(SYMBOLS)} Top Cryptos.")
    except Exception as e:
        print(f"CoinGecko Error: {e}")
        if not SYMBOLS:
            default_list = ["BTC", "ETH", "SOL", "BNB", "XRP", "ADA", "DOGE", "AVAX", "LINK", "SUI", "NEAR", "DOT"]
            SYMBOLS = {f"{s}/USD": f"{s}-USD" for s in default_list}
            for strat in STRATEGIES:
                for name in SYMBOLS:
                    active_positions[strat][name] = {
                        "side": None, "entry": 0.0, "sl": 0.0, "tp": [],
                        "best_price": 0.0, "atr": 0.0, "units": 0.0, 
                        "trade_val": 0.0, "tp1_hit": False
                    }

def send_telegram(message, chat_id=None):
    """chat_id specify kiya toh usko jayega, warna sabhi allowed users ko alert jayega"""
    if not TELEGRAM_TOKEN:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    target_ids = [chat_id] if chat_id else ALLOWED_CHAT_IDS

    for uid in target_ids:
        if uid and not uid.startswith("USER_"):
            payload = {"chat_id": uid, "text": message, "parse_mode": "Markdown"}
            try:
                requests.post(url, json=payload, timeout=10)
            except Exception as e:
                print(f"Telegram error for {uid}: {e}")

def handle_incoming_users():
    last_update_id = 0
    while True:
        try:
            if not TELEGRAM_TOKEN:
                time.sleep(5)
                continue
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
            params = {"offset": last_update_id + 1, "timeout": 20}
            resp = requests.get(url, params=params, timeout=25).json()

            if "result" in resp:
                for update in resp["result"]:
                    last_update_id = update["update_id"]
                    if "message" in update and "text" in update["message"]:
                        sender_id = str(update["message"]["chat"]["id"])
                        user_name = update["message"]["from"].get("first_name", "Trader")

                        # Teeno allowed users me se koi bhi message kare
                        if sender_id in ALLOWED_CHAT_IDS:
                            active_count = get_open_positions_count()
                            total_equity = get_total_equity()
                            status_txt = "🔴 Circuit Halted" if PAPER_ACCOUNT["trading_halted_today"] else "🟢 Active"
                            send_telegram(
                                f"💼 *Dashboard (5% Allocation Rule)*\n\n"
                                f"👤 User: {user_name}\n"
                                f"💰 *Available Cash:* ${PAPER_ACCOUNT['cash']:,.2f}\n"
                                f"📊 *Total Portfolio:* ${total_equity:,.2f}\n"
                                f"📈 *Realized P&L:* ${PAPER_ACCOUNT['realized_pnl']:+,.2f}\n"
                                f"📦 *Open Trades:* {active_count}/{RISK_CONFIG['max_open_trades']}\n"
                                f"⚡ *Status:* {status_txt}",
                                chat_id=sender_id
                            )
                        else:
                            send_telegram("🔒 Access Restricted.", chat_id=sender_id)
        except Exception as e:
            print(f"Listener issue: {e}")
        time.sleep(2)

def calculate_atr_and_adx(df, length=14):
    high = pd.Series(np.array(df['High']).flatten(), index=df.index)
    low = pd.Series(np.array(df['Low']).flatten(), index=df.index)
    close = pd.Series(np.array(df['Close']).flatten(), index=df.index)

    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = tr.rolling(length).mean()

    up_move = high - high.shift(1)
    down_move = low.shift(1) - low

    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0).flatten()
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0).flatten()

    plus_di = 100 * (pd.Series(plus_dm, index=df.index).rolling(length).mean() / atr)
    minus_di = 100 * (pd.Series(minus_dm, index=df.index).rolling(length).mean() / atr)

    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di)
    adx = dx.rolling(length).mean()
    return atr, adx

def get_trend(ticker_symbol, period, interval):
    try:
        data = yf.download(ticker_symbol, period=period, interval=interval, progress=False)
        if data is None or len(data) < 30:
            return "NEUTRAL"

        df = data.copy()
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        close_series = pd.Series(np.array(df['Close']).flatten(), index=df.index)
        ema50 = close_series.ewm(span=min(50, len(close_series)), adjust=False).mean()
        last_close = float(close_series.iloc[-1])
        last_ema = float(ema50.iloc[-1])

        if last_close > last_ema:
            return "BULLISH"
        elif last_close < last_ema:
            return "BEARISH"
        return "NEUTRAL"
    except Exception:
        return "NEUTRAL"

def execute_exit(strat_key, strat_label, name, exit_price, reason, partial=False):
    pos = active_positions[strat_key][name]
    exit_units = pos["units"] * 0.5 if partial else pos["units"]
    exit_val = pos["trade_val"] * 0.5 if partial else pos["trade_val"]

    if pos["side"] == "BUY":
        pnl = (exit_price - pos["entry"]) * exit_units
    else:
        pnl = (pos["entry"] - exit_price) * exit_units

    PAPER_ACCOUNT["cash"] += (exit_val + pnl)
    PAPER_ACCOUNT["realized_pnl"] += pnl
    pnl_pct = (pnl / exit_val) * 100 if exit_val > 0 else 0.0

    if partial:
        pos["units"] -= exit_units
        pos["trade_val"] -= exit_val
        send_telegram(
            f"🎯 *PARTIAL 50% PROFIT SECURED (1:2 R:R)*\n\n"
            f"🪙 Asset: {name}\n"
            f"💵 Exit: ${exit_price:.4f}\n"
            f"💰 *Partial P&L:* *${pnl:+,.2f} ({pnl_pct:+.2f}%)*\n"
            f"🛡️ *SL shifted to Breakeven:* ${pos['entry']:.4f}\n"
            f"Remaining 50% running risk-free for TP3 (1:5)! 🚀"
        )
    else:
        icon = "🟢 PROFIT HIT" if pnl >= 0 else "🔴 STOP LOSS HIT"
        send_telegram(
            f"{icon}\n\n"
            f"🏷️ Strategy: {strat_label}\n"
            f"🪙 Asset: {name} ({pos['side']})\n"
            f"💵 Entry: ${pos['entry']:.4f} | Exit: ${exit_price:.4f}\n"
            f"📊 *Net P&L:* *${pnl:+,.2f} ({pnl_pct:+.2f}%)*\n"
            f"🛑 Reason: {reason}\n\n"
            f"💼 *Available Cash:* *${PAPER_ACCOUNT['cash']:,.2f}*"
        )
        active_positions[strat_key][name] = {
            "side": None, "entry": 0.0, "sl": 0.0, "tp": [],
            "best_price": 0.0, "atr": 0.0, "units": 0.0, 
            "trade_val": 0.0, "tp1_hit": False
        }

def manage_trailing_sl_and_tps(strat_key, strat_label, name, curr_price):
    if name not in active_positions[strat_key]:
        return
    pos = active_positions[strat_key][name]
    if pos["side"] is None:
        return

    trailing_gap = pos["atr"] * 1.5

    if pos["side"] == "BUY":
        if (not pos["tp1_hit"]) and len(pos["tp"]) > 0 and (curr_price >= pos["tp"][0]):
            pos["tp1_hit"] = True
            pos["sl"] = max(pos["sl"], pos["entry"])
            execute_exit(strat_key, strat_label, name, curr_price, "TP1 Hit", partial=True)

        elif len(pos["tp"]) >= 3 and curr_price >= pos["tp"][2]:
            execute_exit(strat_key, strat_label, name, curr_price, "Final TP3 (1:5) Hit! 🏆")
            return

        if curr_price > pos["best_price"]:
            pos["best_price"] = curr_price
            new_sl = curr_price - trailing_gap
            if new_sl > pos["sl"]:
                pos["sl"] = new_sl
        elif curr_price <= pos["sl"]:
            execute_exit(strat_key, strat_label, name, curr_price, "Stop Loss Triggered")

    elif pos["side"] == "SELL":
        if (not pos["tp1_hit"]) and len(pos["tp"]) > 0 and (curr_price <= pos["tp"][0]):
            pos["tp1_hit"] = True
            pos["sl"] = min(pos["sl"], pos["entry"])
            execute_exit(strat_key, strat_label, name, curr_price, "TP1 Hit", partial=True)

        elif len(pos["tp"]) >= 3 and curr_price <= pos["tp"][2]:
            execute_exit(strat_key, strat_label, name, curr_price, "Final TP3 (1:5) Hit! 🏆")
            return

        if curr_price < pos["best_price"]:
            pos["best_price"] = curr_price
            new_sl = curr_price + trailing_gap
            if new_sl < pos["sl"]:
                pos["sl"] = new_sl
        elif curr_price >= pos["sl"]:
            execute_exit(strat_key, strat_label, name, curr_price, "Stop Loss Triggered")

def check_strategy_for_symbol(strat_key, cfg, name, ticker_symbol):
    try:
        check_daily_drawdown_circuit_breaker()
        if PAPER_ACCOUNT["trading_halted_today"]:
            return

        htf_trend = get_trend(ticker_symbol, cfg["htf_period"], cfg["htf_interval"])
        mtf_trend = get_trend(ticker_symbol, cfg["mtf_period"], cfg["mtf_interval"])

        if htf_trend != mtf_trend or htf_trend == "NEUTRAL":
            return

        data = yf.download(ticker_symbol, period=cfg["entry_period"], interval=cfg["entry_interval"], progress=False)
        if data is None or len(data) < 50:
            return

        df = data.copy()
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        for col in ['Open', 'High', 'Low', 'Close']:
            if isinstance(df[col], pd.DataFrame):
                df[col] = df[col].iloc[:, 0]

        close_series = pd.Series(np.array(df['Close']).flatten(), index=df.index)
        df['ema50'] = close_series.ewm(span=50, adjust=False).mean()
        df['ema93'] = close_series.ewm(span=93, adjust=False).mean()
        df['atr'], df['adx'] = calculate_atr_and_adx(df)

        df['swing_high'] = df['High'].iloc[-16:-1].max()
        df['swing_low'] = df['Low'].iloc[-16:-1].min()

        curr_price = float(df['Close'].iloc[-1])
        curr_open = float(df['Open'].iloc[-1])
        curr_adx = float(df['adx'].iloc[-1]) if not pd.isna(df['adx'].iloc[-1]) else 0.0
        curr_atr = float(df['atr'].iloc[-1]) if not pd.isna(df['atr'].iloc[-1]) else 0.0
        swing_h = float(df['swing_high'].iloc[-1])
        swing_l = float(df['swing_low'].iloc[-1])

        manage_trailing_sl_and_tps(strat_key, cfg["label"], name, curr_price)

        if get_open_positions_count() >= RISK_CONFIG["max_open_trades"]:
            return

        strong_trend = curr_adx > cfg["adx_min"]
        active_pos = active_positions[strat_key][name]["side"]
        tp_mults = cfg["tp_multipliers"]

        # BUY SETUP
        if (active_pos is None) and (htf_trend == "BULLISH") and strong_trend and (df['ema50'].iloc[-1] > df['ema93'].iloc[-1]) and (curr_price > swing_h) and (curr_price > curr_open):
            atr_sl = curr_price - (curr_atr * 1.5)
            sl = max(swing_l, atr_sl)
            risk_per_unit = curr_price - sl

            if risk_per_unit > 0:
                trade_cost = PAPER_ACCOUNT["cash"] * RISK_CONFIG["trade_allocation_pct"]

                if trade_cost > 10 and trade_cost <= PAPER_ACCOUNT["cash"]:
                    units = trade_cost / curr_price
                    PAPER_ACCOUNT["cash"] -= trade_cost
                    tp_targets = [curr_price + (risk_per_unit * m) for m in tp_mults]

                    active_positions[strat_key][name] = {
                        "side": "BUY", "entry": curr_price, "sl": sl, "tp": tp_targets,
                        "best_price": curr_price, "atr": curr_atr, "units": units,
                        "trade_val": trade_cost, "tp1_hit": False
                    }

                    send_telegram(
                        f"🚀 *BUY TRIGGERED (5% ALLOCATION)*\n\n"
                        f"🏷️ Strategy: {cfg['label']}\n"
                        f"🪙 Asset: {name}\n"
                        f"💵 Entry: ${curr_price:.4f}\n"
                        f"📦 *Position Size (5%):* ${trade_cost:,.2f} ({units:.4f} units)\n"
                        f"🛑 SL: ${sl:.4f}\n\n"
                        f"🎯 *TARGETS (R:R):*\n"
                        f"• TP 1 (1:2): ${tp_targets[0]:.4f} (50% Book + Breakeven SL)\n"
                        f"• TP 2 (1:2): ${tp_targets[1]:.4f}\n"
                        f"• TP 3 (1:5): ${tp_targets[2]:.4f} (Full Exit)\n\n"
                        f"💼 *Remaining Cash:* ${PAPER_ACCOUNT['cash']:,.2f}"
                    )

        # SELL SETUP
        elif (active_pos is None) and (htf_trend == "BEARISH") and strong_trend and (df['ema50'].iloc[-1] < df['ema93'].iloc[-1]) and (curr_price < swing_l) and (curr_price < curr_open):
            atr_sl = curr_price + (curr_atr * 1.5)
            sl = min(swing_h, atr_sl)
            risk_per_unit = sl - curr_price

            if risk_per_unit > 0:
                trade_cost = PAPER_ACCOUNT["cash"] * RISK_CONFIG["trade_allocation_pct"]

                if trade_cost > 10 and trade_cost <= PAPER_ACCOUNT["cash"]:
                    units = trade_cost / curr_price
                    PAPER_ACCOUNT["cash"] -= trade_cost
                    tp_targets = [curr_price - (risk_per_unit * m) for m in tp_mults]

                    active_positions[strat_key][name] = {
                        "side": "SELL", "entry": curr_price, "sl": sl, "tp": tp_targets,
                        "best_price": curr_price, "atr": curr_atr, "units": units,
                        "trade_val": trade_cost, "tp1_hit": False
                    }

                    send_telegram(
                        f"⚠️ *SHORT/SELL TRIGGERED (5% ALLOCATION)*\n\n"
                        f"🏷️ Strategy: {cfg['label']}\n"
                        f"🪙 Asset: {name}\n"
                        f"💵 Entry: ${curr_price:.4f}\n"
                        f"📦 *Position Size (5%):* ${trade_cost:,.2f} ({units:.4f} units)\n"
                        f"🛑 SL: ${sl:.4f}\n\n"
                        f"🎯 *TARGETS (R:R):*\n"
                        f"• TP 1 (1:2): ${tp_targets[0]:.4f} (50% Book + Breakeven SL)\n"
                        f"• TP 2 (1:2): ${tp_targets[1]:.4f}\n"
                        f"• TP 3 (1:5): ${tp_targets[2]:.4f} (Full Exit)\n\n"
                        f"💼 *Remaining Cash:* ${PAPER_ACCOUNT['cash']:,.2f}"
                    )

    except Exception as e:
        print(f"Error on [{strat_key}] {name}: {e}")

class HealthServer(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Multi-User Paper Trading Engine Active")

def run_server():
    port = int(os.getenv("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthServer)
    server.serve_forever()

if __name__ == "__main__":
    get_top_crypto_symbols(limit=50)

    threading.Thread(target=run_server, daemon=True).start()
    threading.Thread(target=handle_incoming_users, daemon=True).start()

    send_telegram(
        f"👥 *Multi-User Paper Trading Engine Live!*\n\n"
        f"• Authorized Users: {len(ALLOWED_CHAT_IDS)}\n"
        f"• Trade Sizing: 5% of Cash per Trade\n"
        f"• Targets: 1:2 (50% Exit) -> 1:5 (Runner)\n"
        f"• Scanning Top {len(SYMBOLS)} Cryptos 24/7."
    )

    last_list_refresh = time.time()

    while True:
        if time.time() - last_list_refresh > 21600:
            get_top_crypto_symbols(limit=50)
            last_list_refresh = time.time()

        for name, ticker in list(SYMBOLS.items()):
            for strat_key, cfg in STRATEGIES.items():
                check_strategy_for_symbol(strat_key, cfg, name, ticker)
                time.sleep(1.2)
        time.sleep(5)
