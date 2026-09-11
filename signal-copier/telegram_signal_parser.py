"""
telegram_signal_parser.py
──────────────────────────
A minimal, single-user CLI tool that:

  1. Logs into ONE Telegram account (interactive OTP/2FA prompt via Telethon).
  2. Lists the groups/channels you're a member of and lets you pick which
     ones to monitor.
  3. Listens for new messages in those chats and parses them for trading
     signals (BUY/SELL, symbol, entry, SL, TPs).
  4. Prints each parsed signal to the console.

No JSON user store, no multi-session management, no database — everything
lives in memory for the life of the process. Meant as a compact reference
implementation, e.g. for an article, not for production use.

Setup:
    pip install telethon python-dotenv

    Create a .env file next to this script with:
        TELEGRAM_API_ID=123456
        TELEGRAM_API_HASH=your_api_hash_here

    (Get these from https://my.telegram.org)

Run:
    python telegram_signal_parser.py
"""

import asyncio
import os
import re
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient, events
from config import instruments

load_dotenv(Path(__file__).resolve().parent / ".env")

API_ID = os.environ.get("TELEGRAM_API_ID")
API_HASH = os.environ.get("TELEGRAM_API_HASH")

if not API_ID or not API_HASH:
    raise SystemExit(
        "Missing TELEGRAM_API_ID / TELEGRAM_API_HASH. Add them to a .env file."
    )

SESSION_NAME = "signal_parser_session"

#── SIGNAL PARSING ───────────────────────────────────────────────────────
BUY_KEYWORDS = ["buy", "long", "buying"]
SELL_KEYWORDS = ["sell", "short", "selling"]
BUY_PENDING_KEYWORDS = ["buy stop", "buy limit"]
SELL_PENDING_KEYWORDS = ["sell stop", "sell limit"]
SKIP_KEYWORDS = [
    "soon", "maybe", "potential", "possible", "if", "when", "could", "might",
    "expect", "looking to", "watching", "considering", "planning to",
    "hope to", "thinking about", "potentially",
]
ENTRY_KEYWORDS = ["entry", "entries", "zone", "price", "entry zone", "entry price"]
SL_KEYWORDS = ["sl", "stoploss", "stop loss", "stop-loss", "stop", "stops"]
TP_KEYWORDS = ["tp", "takeprofit", "take profit", "take-profit", "target", "targets"]


def normalize_number(num_str: str):
    num_str = num_str.lower().strip()
    multiplier = 1
    if num_str.endswith("k"):
        multiplier, num_str = 1_000, num_str[:-1]
    elif num_str.endswith("m"):
        multiplier, num_str = 1_000_000, num_str[:-1]
    try:
        return float(num_str) * multiplier
    except ValueError:
        return None


def parse_signal(message: str):
    """Return a parsed signal dict, or None if the message isn't a clear signal."""
    if not message:
        return None

    text = message.lower()

    if any(re.search(rf"\b{word}\b", text) for word in SKIP_KEYWORDS):
        return None  # too uncertain ("maybe", "looking to", etc.)

    # --- direction / order type -------------------------------------------------
    signal_type = None
    order_type = "MARKET"

    for word in BUY_PENDING_KEYWORDS + SELL_PENDING_KEYWORDS:
        if word in text:
            signal_type = word.upper()
            order_type = "PENDING"
            break

    if not signal_type:
        if any(word in text for word in BUY_KEYWORDS):
            signal_type = "BUY"
        elif any(word in text for word in SELL_KEYWORDS):
            signal_type = "SELL"

    # --- symbol -------------------------------------------------------------
    normalized_message = re.sub(r"([A-Z]{3,5})\s*/\s*([A-Z]{3,5})", r"\1\2", message.upper())
    clean_text = re.sub(r"[^a-zA-Z0-9\s&]", " ", normalized_message).lower()
    tokens = clean_text.split()
    instrument_set = set(i.lower() for i in instruments)

    symbol = None
    main_symbol = None
    aliases = {
        "gold": "XAUUSD", "xau": "XAUUSD", "silver": "XAGUSD", "xag": "XAGUSD",
        "oil": "USOIL", "nasdaq": "NAS100", "us100": "NAS100", "dow": "US30",
        "es": "US500", "s&p500": "US500", "s&p 500": "US500", "s&p": "US500",
        "sp": "US500", "btc": "BTCUSD", "bitcoin": "BTCUSD",
        "eth": "ETHUSD", "ethereum": "ETHUSD",
    }
    for token in tokens:
        if token in instrument_set:
            main_symbol = token.upper()
            symbol = aliases.get(token, main_symbol)
            break

    # --- stop loss ------------------------------------------------------------
    sl_positions = []
    for k in SL_KEYWORDS:
        for match in re.finditer(rf"\b{k}\b", text):
            pos = match.start()
            before = text[max(0, pos - 5):pos]
            if k in ("stop", "stops") and ("buy " in before or "sell " in before):
                continue
            sl_positions.append(pos)
    first_sl_pos = min(sl_positions) if sl_positions else None

    sl_value = None
    if first_sl_pos is not None:
        sl_zone = text[first_sl_pos:]
        sl_match = re.search(r"\d+\.?\d*[kKmM]?", sl_zone)
        if sl_match:
            between = sl_zone[:sl_match.start()]
            invalid = any(re.search(rf"\b{k}\b", between) for k in TP_KEYWORDS + ENTRY_KEYWORDS)
            if not invalid:
                sl_value = normalize_number(sl_match.group())

    # --- take profits -----------------------------------------------------------
    tp_positions = [text.find(k) for k in TP_KEYWORDS if text.find(k) != -1]
    first_tp_pos = min(tp_positions) if tp_positions else None

    tp_pattern_str = "|".join(k.replace(" ", r"\s*").replace("-", r"[-\s]*") for k in TP_KEYWORDS)
    tp_zone = text[first_tp_pos:] if first_tp_pos is not None else text
    tp_line_matches = re.findall(rf"\b({tp_pattern_str})(?:\d+)?[\s:=\-\.,@#\/]+([^\n]+)", tp_zone)

    tp_values = []
    for _, raw_seg in tp_line_matches:
        raw_seg = re.sub(r"\bor\b", " ", raw_seg.replace(",", ""))
        cursor = 0
        for part in re.split(r"[\/,\s]+", raw_seg):
            part = part.strip()
            if not part:
                continue
            pos = raw_seg.find(part, cursor)
            cursor = pos + len(part)
            before_text = raw_seg[:pos]
            if any(re.search(rf"\b{k}\b", before_text) for k in SL_KEYWORDS + ENTRY_KEYWORDS):
                continue
            val = normalize_number(part)
            if val is not None:
                tp_values.append(val)
    tp_values = list(dict.fromkeys(tp_values))

    # sanity-check TPs against SL/direction
    if tp_values and sl_value:
        valid_tps = []
        for tp in tp_values:
            if tp <= 0:
                continue
            if signal_type in ("BUY", "BUY STOP", "SELL LIMIT") and tp > sl_value:
                valid_tps.append(tp)
            elif signal_type in ("SELL", "SELL STOP", "BUY LIMIT") and sl_value > tp:
                valid_tps.append(tp)
        if not valid_tps:
            return None
        tp_values = valid_tps

    # --- entry price ------------------------------------------------------------
    entry = None
    entry_pattern = "|".join(k.replace(" ", r"\s*") for k in ENTRY_KEYWORDS)
    for match in re.finditer(rf"\b({entry_pattern})\b", text):
        after = text[match.end():]
        cuts = [after.find(k) for k in SL_KEYWORDS + TP_KEYWORDS if after.find(k) != -1]
        if cuts:
            after = after[:min(cuts)]
        if main_symbol:
            after = re.sub(rf"\b{re.escape(main_symbol.lower())}\b", "", after)
        nums = re.findall(r"\d+\.?\d*[kKmM]?", after)
        if nums:
            entry = normalize_number(nums[0])
            break

    if entry is None:
        split_index = min([p for p in (first_sl_pos, first_tp_pos, len(text)) if p is not None])
        zone = text[:split_index]
        if main_symbol:
            zone = re.sub(rf"\b{re.escape(main_symbol.lower())}\b", "", zone)
        nums = re.findall(r"\d+\.?\d*[kKmM]?", zone)
        entry = normalize_number(nums[0]) if nums else None

    # --- final validation ---------------------------------------------------
    if not (signal_type and symbol and sl_value):
        return None
    if order_type == "PENDING" and entry is None:
        return None

    return {
        "type": signal_type,
        "order_type": order_type,
        "symbol": symbol,
        "entry": entry,
        "sl": sl_value,
        "tps": tp_values,
    }


# ── CLI HELPERS ───────────────────────────────────────────────────────────

async def choose_chats(client: TelegramClient):
    """List the user's groups/channels and let them pick which to monitor."""
    print("\nFetching your groups and channels...")
    dialogs = []
    async for dialog in client.iter_dialogs():
        if dialog.is_group or dialog.is_channel:
            dialogs.append(dialog)

    if not dialogs:
        raise SystemExit("No groups or channels found on this account.")

    print("\nSelect chats to monitor:\n")
    for i, d in enumerate(dialogs, start=1):
        kind = "channel" if d.is_channel else "group"
        print(f"  [{i:>2}] {d.name}  ({kind})")

    raw = input("\nEnter numbers to monitor, comma-separated (e.g. 1,3,5): ").strip()
    indices = {int(x) for x in re.findall(r"\d+", raw)}

    chosen = [dialogs[i - 1] for i in indices if 1 <= i <= len(dialogs)]
    if not chosen:
        raise SystemExit("No valid selection made — exiting.")

    print("\nMonitoring:")
    for d in chosen:
        print(f"  • {d.name}")

    return [d.entity for d in chosen]


def print_signal(chat_name: str, signal: dict):
    tps = ", ".join(str(tp) for tp in signal["tps"]) or "—"
    print(
        "\n"
        f"┌─ SIGNAL from {chat_name} ─────────────────────────\n"
        f"│ Type:   {signal['type']} ({signal['order_type']})\n"
        f"│ Symbol: {signal['symbol']}\n"
        f"│ Entry:  {signal['entry']}\n"
        f"│ SL:     {signal['sl']}\n"
        f"│ TPs:    {tps}\n"
        "└──────────────────────────────────────────────────"
    )


# ── MAIN ──────────────────────────────────────────────────────────────────

async def main():
    client = TelegramClient(SESSION_NAME, API_ID, API_HASH)

    # client.start() handles the whole login flow interactively:
    # phone number → OTP code → 2FA password (if enabled).
    await client.start()
    print("Logged in.")

    monitored_entities = await choose_chats(client)
    chat_id_to_name = {e.id: getattr(e, "title", getattr(e, "username", str(e.id))) for e in monitored_entities}

    @client.on(events.NewMessage(chats=monitored_entities))
    async def handler(event):
        if not event.text:
            return
        signal = parse_signal(event.text)
        if signal:
            chat = await event.get_chat()
            chat_name = getattr(chat, "title", None) or getattr(chat, "username", None) or str(event.chat_id)
            print_signal(chat_name, signal)

    print("\nListening for signals... (Ctrl+C to stop)\n")
    await client.run_until_disconnected()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, EOFError):
        print("\nStopped.")
