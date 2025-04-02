import asyncio
import base64
import json
import time
import struct
import aiohttp
import websockets
from collections import deque
from solders.transaction import VersionedTransaction
from solders.pubkey import Pubkey
from config import PUMP_PROGRAM, LAMPORTS_PER_SOL
from construct import Struct, Int64ul, Flag
import os

SOLANA_NODE_WSS_ENDPOINT = os.environ["SOLANA_NODE_WSS_ENDPOINT"]
RPC_HTTP_ENDPOINT = os.environ["RPC_HTTP_ENDPOINT"]

BUY_DISCRIMINATOR = struct.pack("<Q", 16927863322537952870)
SELL_DISCRIMINATOR = struct.pack("<Q", 12502976635542562355)
EXPECTED_DISCRIMINATOR = struct.pack("<Q", 6966180631402821399)
TOKEN_DECIMALS = 6

class BondingCurveState:
    _STRUCT = Struct(
        "virtual_token_reserves" / Int64ul,
        "virtual_sol_reserves" / Int64ul,
        "real_token_reserves" / Int64ul,
        "real_sol_reserves" / Int64ul,
        "token_total_supply" / Int64ul,
        "complete" / Flag
    )
    def __init__(self, data: bytes) -> None:
        parsed = self._STRUCT.parse(data[8:])
        self.virtual_token_reserves = parsed.virtual_token_reserves
        self.virtual_sol_reserves = parsed.virtual_sol_reserves
        self.token_total_supply = parsed.token_total_supply

def parse_bonding_curve(data: bytes) -> BondingCurveState:
    if data[:8] != EXPECTED_DISCRIMINATOR:
        raise ValueError("❌ Invalid curve state discriminator")
    return BondingCurveState(data)

def calculate_price(state: BondingCurveState) -> float:
    if state.virtual_token_reserves <= 0 or state.virtual_sol_reserves <= 0:
        raise ValueError("❌ Invalid bonding curve state: zero reserves")
    return (state.virtual_sol_reserves / LAMPORTS_PER_SOL) / (
        state.virtual_token_reserves / 10 ** TOKEN_DECIMALS
    )

async def get_account_data(session, pubkey: str) -> bytes:
    headers = {"Content-Type": "application/json"}
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getAccountInfo",
        "params": [
            str(pubkey),
            {
                "encoding": "base64",
                "commitment": "confirmed"
            }
        ]
    }
    async with session.post(RPC_HTTP_ENDPOINT, json=payload, headers=headers) as response:
        result = await response.json()
        value = result.get("result", {}).get("value", None)
        if not value or "data" not in value or not value["data"]:
            raise ValueError("Account not yet available or malformed.")
        try:
            data_base64 = value["data"][0]
            return base64.b64decode(data_base64)
        except Exception as e:
            raise ValueError(f"Failed to decode account info: {e}")

def avg_price(history):
    if not history:
        return None
    total_sol = sum(sol for sol, _ in history)
    total_tokens = sum(tokens for _, tokens in history)
    return total_sol / total_tokens if total_tokens > 0 else None


from collections import defaultdict

def update_aggregate_per_second(state_map, key, timestamp, value):
    """
    Incrémente la somme par seconde de manière robuste dans state_map.
    Crée automatiquement des zéros pour les secondes manquantes.
    """
    sec = int(timestamp)
    agg_key = f"agg_{key}_per_sec"
    last_key = f"last_agg_ts_{key}"

    if agg_key not in state_map:
        state_map[agg_key] = {}
        state_map[last_key] = sec - 1

    if sec not in state_map[agg_key]:
        # Ajout de zéros pour toutes les secondes manquantes
        last_sec = state_map[last_key]
        for missing in range(last_sec + 1, sec):
            state_map[agg_key][missing] = 0.0

    state_map[agg_key][sec] = state_map[agg_key].get(sec, 0.0) + value
    state_map[last_key] = sec


def is_rising(series):
    return all(x <= y for x, y in zip(series, series[1:]))

def check_aggregated_momentum(state_map, min_points=5, max_age_sec=7):
    now = int(time.time())

    def get_recent_values(agg_dict):
        recent = [(t, v) for t, v in sorted(agg_dict.items()) if now - t <= max_age_sec]
        if len(recent) < min_points:
            return None
        return [v for _, v in recent[-min_points:]]

    prices = get_recent_values(state_map.get("agg_price_per_sec", {}))
    buyers = get_recent_values(state_map.get("agg_buyers_per_sec", {}))
    volumes = get_recent_values(state_map.get("agg_volume_per_sec", {}))

    if not all([prices, buyers, volumes]):
        return False

    return all(x <= y for x, y in zip(prices, prices[1:])) and \
           all(x <= y for x, y in zip(buyers, buyers[1:])) and \
           all(x <= y for x, y in zip(volumes, volumes[1:]))



async def monitor_project(project, out_queue: asyncio.Queue, thresholds=None, debug=False):
    thresholds = thresholds or {
        "min_holders": 15,
        "holder_check_sec": 20,
        "price_min_increase": 0.20,
        "price_check_sec": 10
    }

    mint = project["mint"]
    bonding_curve = project["bondingCurve"]
    start_time = time.time()

    state_map = {
        "buyers": set(),
        "sellers": set(),
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

    async with aiohttp.ClientSession() as session:
        for attempt in range(2):
            try:
                if attempt > 0:
                    await asyncio.sleep(1)
                raw = await get_account_data(session, bonding_curve)
                curve_state = parse_bonding_curve(raw)
                initial_price = calculate_price(curve_state)
                state_map["price"] = initial_price
                state_map["price_history"].append((time.time(), initial_price))
                break
            except Exception as e:
                if attempt == 1:
                    print(f"[❌] Failed to fetch initial bonding curve for {mint}: {e}")
                    return

    async def evaluate_rules():
        while not should_exit.is_set():
            await asyncio.sleep(0.5)
            now = time.time()
            if now - start_time >= 10 and state_map["holder_count"] == 0:
                should_exit.set(); return
            if now - start_time >= thresholds["holder_check_sec"] and state_map["holder_count"] < thresholds["min_holders"]:
                should_exit.set(); return
            if now - start_time >= thresholds["price_check_sec"]:
                expected = state_map["price_history"][0][1] * (1 + thresholds["price_min_increase"])
                if state_map["price"] < expected:
                    should_exit.set(); return
            if check_aggregated_momentum(state_map):
                print(f"🚀 STRATEGY MATCHED: {project['name']} {mint}")
                print(state_map)
                exit()

    asyncio.create_task(evaluate_rules())

    try:
        async with websockets.connect(SOLANA_NODE_WSS_ENDPOINT) as ws:
            await ws.send(json.dumps({
                "jsonrpc": "2.0",
                "id": 1,
                "method": "blockSubscribe",
                "params": [
                    {"mentionsAccountOrProgram": str(PUMP_PROGRAM)},
                    {"commitment": "confirmed", "encoding": "base64", "transactionDetails": "full", "maxSupportedTransactionVersion": 0}
                ]
            }))
            last_ping = time.time()
            while not should_exit.is_set():
                if time.time() - last_ping > 20:
                    await ws.ping(); last_ping = time.time()

                try:
                    raw_msg = await asyncio.wait_for(ws.recv(), timeout=30)
                    data = json.loads(raw_msg)
                    block = data.get("params", {}).get("result", {}).get("value", {}).get("block")
                    if not block:
                        continue

                    for tx in block.get("transactions", []):
                        try:
                            tx_bytes = base64.b64decode(tx["transaction"][0])
                            if not any(d in tx_bytes for d in [BUY_DISCRIMINATOR, SELL_DISCRIMINATOR]):
                                continue
                            transaction = VersionedTransaction.from_bytes(tx_bytes)
                            keys = transaction.message.account_keys
                            for ix in transaction.message.instructions:
                                discriminator = ix.data[:8]
                                if discriminator not in [BUY_DISCRIMINATOR, SELL_DISCRIMINATOR]: continue
                                if str(keys[ix.program_id_index]) != str(PUMP_PROGRAM): continue
                                accounts = [str(keys[i]) for i in ix.accounts if i < len(keys)]
                                if mint not in accounts: continue

                                actor = accounts[6] if len(accounts) > 6 else "unknown"
                                timestamp = time.time()
                                sec = int(timestamp)

                                state_map["tx_count"] += 1
                                update_aggregate_per_second(state_map, "tx_count", timestamp, 1)

                                if discriminator == BUY_DISCRIMINATOR:
                                    state_map["buyers"].add(actor)
                                    try:
                                        token_amount = struct.unpack_from("<Q", ix.data, 8)[0] / 10**TOKEN_DECIMALS
                                        sol_amount = struct.unpack_from("<Q", ix.data, 16)[0] / LAMPORTS_PER_SOL
                                        if token_amount > 0:
                                            state_map["buy_history"].append((sol_amount, token_amount))
                                            state_map["volume_history"].append((timestamp, sol_amount))
                                            update_aggregate_per_second(state_map, "volume", timestamp, sol_amount)
                                            update_aggregate_per_second(state_map, "buyers", timestamp, 1)
                                    except Exception:
                                        pass

                                elif discriminator == SELL_DISCRIMINATOR:
                                    state_map["sellers"].add(actor)

                                state_map["holder_count"] = len(state_map["buyers"] - state_map["sellers"])
                                state_map["buyer_history"].append((timestamp, len(state_map["buyers"])))

                                est_price = avg_price(state_map["buy_history"])
                                if est_price:
                                    state_map["price_tx_estimate"] = est_price
                                    state_map["price_tx_history"].append((timestamp, est_price))

                                # Récupération prix on-chain
                                if 'last_curve_fetch' not in state_map or timestamp - state_map["last_curve_fetch"] > 1:
                                    state_map["last_curve_fetch"] = timestamp
                                    async with aiohttp.ClientSession() as s:
                                        try:
                                            raw = await get_account_data(s, bonding_curve)
                                            curve_state = parse_bonding_curve(raw)
                                            new_price = calculate_price(curve_state)
                                            if abs(new_price - (state_map["price"] or 0)) > 1e-9:
                                                state_map["price"] = new_price
                                                state_map["price_history"].append((timestamp, new_price))
                                                update_aggregate_per_second(state_map, "price", timestamp, new_price)
                                        except Exception:
                                            pass

                                await out_queue.put({
                                    "mint": mint,
                                    "timestamp": timestamp,
                                    "price": state_map["price"],
                                    "price_tx_estimate": state_map["price_tx_estimate"],
                                    "holders": state_map["holder_count"],
                                    "tx_count": state_map["tx_count"],
                                    "buyers": list(state_map["buyers"]),
                                    "sellers": list(state_map["sellers"]),
                                    "project": project
                                })

                        except Exception:
                            continue
                except asyncio.TimeoutError:
                    await ws.ping()
                    last_ping = time.time()
    except Exception as websocket_error:
        print(f"[❌] WebSocket closed unexpectedly for {mint}: {websocket_error}")
        await asyncio.sleep(1)
        return
