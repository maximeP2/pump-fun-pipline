import asyncio
import json
import os
import time
import websockets
from config import PUMP_PROGRAM

SOLANA_NODE_WSS_ENDPOINT = os.environ["SOLANA_NODE_WSS_ENDPOINT"]

async def rpc_listener(dispatcher, debug=False):
    subscription_payload = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "blockSubscribe",
        "params": [
            {"mentionsAccountOrProgram": str(PUMP_PROGRAM)},
            {
                "commitment": "confirmed",
                "encoding": "base64",
                "transactionDetails": "full",
                "maxSupportedTransactionVersion": 0
            }
        ]
    })

    while True:
        try:
            async with websockets.connect(SOLANA_NODE_WSS_ENDPOINT, ping_interval=20, ping_timeout=20) as ws:
                await ws.send(subscription_payload)
                if debug:
                    print("📡 Connected to Solana WebSocket and subscribed.")

                last_ping = time.time()

                while True:
                    if time.time() - last_ping > 20:
                        await ws.ping()
                        last_ping = time.time()

                    try:
                        message = await asyncio.wait_for(ws.recv(), timeout=30)
                        data = json.loads(message)

                        block = data.get("params", {}).get("result", {}).get("value", {}).get("block")
                        if not block:
                            continue

                        for tx in block.get("transactions", []):
                            if not tx.get("meta") or tx["meta"].get("err") is not None:
                                continue  # skip transaction
                            raw_tx = tx["transaction"][0]  # base64-encoded
                            await dispatcher.dispatch_transaction(raw_tx)

                    except asyncio.TimeoutError:
                        if debug:
                            print("⌛ Timeout, sending ping...")
                        await ws.ping()
                        last_ping = time.time()

        except Exception as e:
            print(f"🔌 WebSocket connection error: {e}")
            print("🔁 Reconnecting in 5 seconds...")
            await asyncio.sleep(5)
