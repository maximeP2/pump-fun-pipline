import asyncio
import base64
import aiohttp
import time
from config import  LAMPORTS_PER_SOL
from construct import Struct, Int64ul, Flag
import os

RPC_HTTP_ENDPOINT = os.environ["RPC_HTTP_ENDPOINT"]

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

    def __init__(self, data: bytes):
        parsed = self._STRUCT.parse(data[8:])
        self.virtual_token_reserves = parsed.virtual_token_reserves
        self.virtual_sol_reserves = parsed.virtual_sol_reserves
        self.token_total_supply = parsed.token_total_supply


def calculate_price(state: BondingCurveState) -> float:
    if state.virtual_token_reserves <= 0 or state.virtual_sol_reserves <= 0:
        raise ValueError("Invalid bonding curve state: zero reserves")
    return (state.virtual_sol_reserves / LAMPORTS_PER_SOL) / (
        state.virtual_token_reserves / 10**TOKEN_DECIMALS
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


async def bonding_curve_fetcher(dispatcher, debug=False):
    async with aiohttp.ClientSession() as session:
        last_fetch_time = {}
        last_sent_price = {}

        while True:
            projects = list(dispatcher.project_definitions.items())
            num_projects = len(projects)
            
            # Éviter une division par zéro ou une vitesse trop rapide
            delay_per_call = max(0.1, 1.0 / max(num_projects, 10))

            # Clean last_sent_price for removed projects
            monitored = set(dispatcher.project_definitions.keys())
            obsolete = set(last_sent_price.keys()) - monitored
            for mint in obsolete:
                del last_sent_price[mint]

            for mint, project in projects:
                bonding_curve_address = project.get("bondingCurve")
                queue = dispatcher.monitor_queues.get(mint)

                if not bonding_curve_address or not queue:
                    continue

                now = time.time()
                last_active = dispatcher.last_activity.get(mint, 0)
                if now - last_active > 10:
                    await asyncio.sleep(delay_per_call)
                    continue

                last_time = last_fetch_time.get(mint, 0)
                if now - last_time < 1.0:
                    await asyncio.sleep(delay_per_call)
                    continue  # On interroge max 1 fois par seconde

                try:
                    last_fetch_time[mint] = time.time()  # Met à jour immédiatement
                    raw = await get_account_data(session, bonding_curve_address)
                    curve = BondingCurveState(raw)
                    price = calculate_price(curve)

                    # Ne pas envoyer de mise à jour si le prix est inchangé
                    last_price = last_sent_price.get(mint)
                    if last_price is not None and abs(price - last_price) < 1e-10:
                        continue  # ❌ Pas de changement significatif

                    last_sent_price[mint] = price  # ✅ Met à jour le cache

                    await queue.put(("price_update", price))
                    if debug:
                        print(f"[📊] Price updated for {project['name']} ({mint}): {price:.9f} SOL")

                except Exception as e:
                    if debug:
                        print(f"[⚠️] Error fetching bonding curve for {mint}: {e}")

                await asyncio.sleep(delay_per_call)

            await asyncio.sleep(0.1)  # petite pause entre les cycles
