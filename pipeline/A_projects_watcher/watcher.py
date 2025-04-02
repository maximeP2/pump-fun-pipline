# %%
import asyncio
import json
import base64
import struct
import websockets
import time
from solders.transaction import VersionedTransaction
from solders.pubkey import Pubkey
from config import PUMP_PROGRAM
import os
from collections import deque
SOLANA_NODE_WSS_ENDPOINT = os.environ["SOLANA_NODE_WSS_ENDPOINT"]

# Assure-toi que ces variables sont chargées
# SOLANA_NODE_WSS_ENDPOINT, PUMP_PROGRAM

CREATE_DISCRIMINATOR = struct.pack("<Q", 8576854823835016728)

# IDL préchargée pour ne pas relire à chaque fois
IDL_CACHE = None

def load_idl(file_path='idl/pump_fun_idl.json'):
    global IDL_CACHE
    if IDL_CACHE is None:
        with open(file_path, 'r') as f:
            IDL_CACHE = json.load(f)
    return IDL_CACHE

def decode_create_instruction(ix_data, ix_def, accounts):
    args = {}
    offset = 8
    for arg in ix_def['args']:
        if arg['type'] == 'string':
            length = struct.unpack_from('<I', ix_data, offset)[0]
            offset += 4
            value = ix_data[offset:offset+length].decode('utf-8')
            offset += length
        elif arg['type'] == 'publicKey':
            value = base64.b64encode(ix_data[offset:offset+32]).decode('utf-8')
            offset += 32
        else:
            raise ValueError(f"Unsupported type: {arg['type']}")
        args[arg['name']] = value

    args['mint'] = str(accounts[0])
    args['bondingCurve'] = str(accounts[2])
    args['associatedBondingCurve'] = str(accounts[3])
    args['user'] = str(accounts[7])
    return args

# %%
async def watch_new_projects(queue: asyncio.Queue, filters=None, debug=False):
    filters = filters or {}
    idl = load_idl()
    create_ix_def = next(ix for ix in idl['instructions'] if ix['name'] == 'create')
    recent_mints = deque(maxlen=1000)

    subscription_message = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "blockSubscribe",
        "params": [
            {"mentionsAccountOrProgram": str(PUMP_PROGRAM)},
            {
                "commitment": "confirmed",
                "encoding": "base64",
                "showRewards": False,
                "transactionDetails": "full",
                "maxSupportedTransactionVersion": 0
            }
        ]
    })

    while True:
        try:
            async with websockets.connect(SOLANA_NODE_WSS_ENDPOINT) as websocket:
                await websocket.send(subscription_message)
                if debug:
                    print("✅ Subscribed to Pump.fun token creations.")

                last_ping = time.time()
                while True:
                    if time.time() - last_ping > 20:
                        await websocket.ping()
                        last_ping = time.time()

                    try:
                        message = await asyncio.wait_for(websocket.recv(), timeout=30)
                        data = json.loads(message)
                        if data.get("method") != "blockNotification":
                            continue

                        transactions = data["params"]["result"]["value"]["block"].get("transactions", [])
                        for tx in transactions:
                            try:
                                tx_b64 = tx["transaction"][0]
                                tx_raw = base64.b64decode(tx_b64)

                                # ✅ Pré-filtrage sur les bytes directement
                                if CREATE_DISCRIMINATOR not in tx_raw:
                                    continue
                                transaction = VersionedTransaction.from_bytes(tx_raw)
                                account_keys = transaction.message.account_keys

                                for ix in transaction.message.instructions:
                                    if ix.data[:8] != CREATE_DISCRIMINATOR:
                                        continue

                                    program_id = str(account_keys[ix.program_id_index])
                                    if program_id != str(PUMP_PROGRAM):
                                        continue

                                    accounts = [str(account_keys[i]) for i in ix.accounts if i < len(account_keys)]
                                    token_data = decode_create_instruction(ix.data, create_ix_def, accounts)

                                    mint = token_data["mint"]
                                    if mint in recent_mints:
                                        continue
                                    recent_mints.append(mint)

                                    if len(recent_mints) > 1000:
                                        recent_mints = set(list(recent_mints)[-500:])

                                    name_match = True
                                    user_match = True

                                    if "name_contains" in filters:
                                        name = token_data.get("name", "") + token_data.get("symbol", "")
                                        name_match = filters["name_contains"].lower() in name.lower()

                                    if "creator_address" in filters:
                                        user_match = token_data.get("user", "") == filters["creator_address"]

                                    if name_match and user_match:
                                        if debug:
                                            print(f"\n🎯 New project passed filters:")
                                            print(json.dumps(token_data, indent=2))
                                        await queue.put(token_data)

                            except Exception as parse_error:
                                if debug:
                                    print(f"⚠️ Parse error: {parse_error}")

                    except asyncio.TimeoutError:
                        if debug:
                            print("⌛ Timeout, sending ping...")
                        await websocket.ping()
                        last_ping = time.time()

        except Exception as e:
            print(f"🔌 WebSocket connection error: {e}")
            print("🔁 Reconnecting in 5 seconds...")
            await asyncio.sleep(5)

