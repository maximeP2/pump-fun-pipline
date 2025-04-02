import asyncio
import time
import struct
import json
from collections import deque
from config import LAMPORTS_PER_SOL

BUY_DISCRIMINATOR = struct.pack("<Q", 16927863322537952870)
SELL_DISCRIMINATOR = struct.pack("<Q", 12502976635542562355)
TOKEN_DECIMALS = 6

def log(msg, debug=True):
    if debug:
        print(f"[DEBUG] {msg}")


def avg_price(history):
    if not history:
        return None
    total_sol = sum(sol for sol, _ in history)
    total_tokens = sum(tokens for _, tokens in history)
    return total_sol / total_tokens if total_tokens > 0 else None


def update_aggregate_per_second(state_map, key, timestamp, value):
    sec = int(timestamp)
    agg_key = f"agg_{key}_per_sec"
    last_key = f"last_agg_ts_{key}"

    if agg_key not in state_map:
        state_map[agg_key] = {}
        state_map[last_key] = sec - 1

    if sec not in state_map[agg_key]:
        last_sec = state_map[last_key]
        for missing in range(last_sec + 1, sec):
            state_map[agg_key][missing] = 0.0

    state_map[agg_key][sec] = state_map[agg_key].get(sec, 0.0) + value
    state_map[last_key] = sec

# ici on process des instructions
async def monitor_project(project, dispatcher, thresholds=None, debug=False):


    thresholds = thresholds or {
        "min_holders": 15,
        "holder_check_sec": 20,
        "price_min_increase": 0.20,
        "price_check_sec": 10
    }

    mint = project["mint"]
    start_time = time.time()

    state_map = {
        "balances": {},
        "holder_count": 0,
        "price": None,
        "price_tx_estimate": None,
        "buy_history": [],
        "sell_history": [],
        "price_history": deque(maxlen=30),
        "price_tx_history": deque(maxlen=30),
        "buyer_history": deque(maxlen=30),
        "volume_history": deque(maxlen=30),
        "tx_count": 0
    }

    should_exit = asyncio.Event()

    async def evaluate_rules():
        while not should_exit.is_set():
            await asyncio.sleep(0.5)
            now = time.time()
            if now - start_time >= 10 and state_map["holder_count"] == 0:
                log(f"💀 {project['name']} ({mint}) - No holders after 10s", debug)
                should_exit.set(); return
            if now - start_time >= thresholds["holder_check_sec"] and state_map["holder_count"] < thresholds["min_holders"]:
                log(f"⛔ {project['name']} ({mint}) - Not enough holders after {thresholds['holder_check_sec']}s", debug)
                should_exit.set(); return
            # if now - start_time >= thresholds["price_check_sec"] and state_map["price_history"]:
            #     expected = state_map["price_history"][0][1] * (1 + thresholds["price_min_increase"])
            #     if state_map["price"] and state_map["price"] < expected:
            #         log(f"📉 {project['name']} ({mint}) - Price hasn't risen enough", debug)
            #         should_exit.set(); return

    asyncio.create_task(evaluate_rules())

    monitor_queues = dispatcher.monitor_queues[mint]

    while not should_exit.is_set():
        event = await monitor_queues.get()

        if isinstance(event, tuple) and event and event[0] == "price_update":
            _, new_price = event
            timestamp = time.time()
            state_map["price"] = new_price
            state_map["price_history"].append((timestamp, new_price))
            update_aggregate_per_second(state_map, "price", timestamp, new_price)
            continue

        transaction, instruction, discriminator = event
        keys = transaction.message.account_keys
        accounts = [str(keys[i]) for i in instruction.accounts if i < len(keys)]
        actor = accounts[6] if len(accounts) > 6 else "unknown"
        timestamp = time.time()

        state_map["tx_count"] += 1
        update_aggregate_per_second(state_map, "tx_count", timestamp, 1)

        if discriminator == BUY_DISCRIMINATOR:
            token_amount = struct.unpack_from("<Q", instruction.data, 8)[0] / 10**TOKEN_DECIMALS
            sol_amount = struct.unpack_from("<Q", instruction.data, 16)[0] / LAMPORTS_PER_SOL

            prev = state_map["balances"].get(actor, 0)
            new = prev + token_amount
            state_map["balances"][actor] = new
            if prev == 0:
                state_map["holder_count"] += 1
                log(f"👤 New holder (+1) {project['name']} → total: {state_map['holder_count']}", debug)

            state_map["buy_history"].append((sol_amount, token_amount))
            state_map["volume_history"].append((timestamp, sol_amount))
            update_aggregate_per_second(state_map, "volume", timestamp, sol_amount)
            update_aggregate_per_second(state_map, "buyers", timestamp, 1)

            log(f"🟢 Buy {sol_amount:.9f} SOL | {token_amount:.9f} tokens", debug)

        elif discriminator == SELL_DISCRIMINATOR:
            token_amount = struct.unpack_from("<Q", instruction.data, 8)[0] / 10**TOKEN_DECIMALS
            sol_amount = struct.unpack_from("<Q", instruction.data, 16)[0] / LAMPORTS_PER_SOL

            prev = state_map["balances"].get(actor, 0)
            new = max(prev - token_amount, 0)
            state_map["balances"][actor] = new
            if prev > 0 and new == 0:
                state_map["holder_count"] = max(state_map["holder_count"] - 1, 0)
                log(f"👤 Holder exited (-1) {project['name']} → total: {state_map['holder_count']}", debug)

            state_map["sell_history"].append((timestamp, token_amount))
            update_aggregate_per_second(state_map, "sellers", timestamp, 1)
            update_aggregate_per_second(state_map, "volume_sell", timestamp, sol_amount)

            log(f"🔴 Sell {sol_amount:.9f} SOL | {token_amount:.9f} tokens", debug)

        est_price = avg_price(state_map["buy_history"])
        if est_price:
            state_map["price_tx_estimate"] = est_price
            state_map["price_tx_history"].append((timestamp, est_price))

    log(f"🛑 Monitoring stopped for {project['name']} ({mint}) | {state_map['tx_count']} tx processed", debug)

    await dispatcher.unregister_project(mint)

    log(f"$$$ Nomber of registred projects : {len(dispatcher.monitored_projects)} ", debug)

    # Optionnel : sauvegarder l'état si nécessaire
    # with open(f"logs/{mint}_{int(time.time())}.json", "w") as f:
    #     json.dump(state_map, f, indent=2)
