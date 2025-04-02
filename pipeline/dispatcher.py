import asyncio
import json
import base64
import struct
import time
from collections import defaultdict,deque
from solders.transaction import VersionedTransaction
from config import PUMP_PROGRAM

CREATE_DISCRIMINATOR = struct.pack("<Q", 8576854823835016728)
BUY_DISCRIMINATOR = struct.pack("<Q", 16927863322537952870)
SELL_DISCRIMINATOR = struct.pack("<Q", 12502976635542562355)





class ProjectDispatcher:
    def __init__(self):
        self.watcher_queue = asyncio.Queue()
        self.monitor_queues = defaultdict(asyncio.Queue)
        self.monitored_projects = set()
        self.project_definitions = {}  # mint -> project (with name, etc.)
        self.mint_index_by_discriminator = self._load_mint_indexes()
        self.seen_signatures = deque(maxlen=10000)  # for duplicate filtering
        self.last_activity = defaultdict(lambda: 0)  # mint -> last activity timestamp

    def _load_mint_indexes(self, idl_path='idl/pump_fun_idl.json'):
        with open(idl_path, 'r') as f:
            idl = json.load(f)

        result = {}
        for instr in idl['instructions']:
            name = instr['name']
            for ix_discrim, ix_name in [
                (BUY_DISCRIMINATOR, "buy"),
                (SELL_DISCRIMINATOR, "sell")
            ]:
                if name == ix_name:
                    for idx, acc in enumerate(instr['accounts']):
                        if acc['name'] == 'mint':
                            result[ix_discrim] = idx
                            break
                    else:
                        print(f"[⚠️] Warning: 'mint' not found in instruction '{ix_name}'")
        return result

    def record_activity(self, mint):
        self.last_activity[mint] = time.time()

    async def register_project(self, project):
        mint = project["mint"]
        self.monitored_projects.add(mint)
        self.project_definitions[mint] = project
        print(f"✅ Registered project for monitoring: {project['name']} ({mint})")

    async def unregister_project(self, mint):
        self.monitored_projects.discard(mint)
        self.monitor_queues.pop(mint, None)
        self.project_definitions.pop(mint, None)

    async def dispatch_transaction(self, raw_tx):
        raw_bytes = base64.b64decode(raw_tx)

        if not any(d in raw_bytes for d in [CREATE_DISCRIMINATOR, BUY_DISCRIMINATOR, SELL_DISCRIMINATOR]):
            return

        try:
            transaction = VersionedTransaction.from_bytes(raw_bytes)
        except Exception as e:
            print(f"[⚠️] Failed to parse transaction: {e}")
            return

        sig = str(transaction.signatures[0])
        if sig in self.seen_signatures:
            return  # Duplicate, already processed
        self.seen_signatures.append(sig)

        keys = transaction.message.account_keys

        for ix in transaction.message.instructions:
            discriminator = ix.data[:8]
            program_id = str(keys[ix.program_id_index])

            if program_id != str(PUMP_PROGRAM):
                continue

            if discriminator == CREATE_DISCRIMINATOR:
                await self.watcher_queue.put((transaction, ix, discriminator))

            elif discriminator in [BUY_DISCRIMINATOR, SELL_DISCRIMINATOR]:
                mint_idx = self.mint_index_by_discriminator.get(discriminator)
                if mint_idx is None:
                    print(f"[⚠️] No mint index configured for discriminator: {discriminator.hex()}")
                    continue

                accounts = [str(keys[i]) for i in ix.accounts if i < len(keys)]
                if mint_idx >= len(accounts):
                    print(f"[⚠️] mint_idx {mint_idx} out of bounds for accounts list of length {len(accounts)}")
                    continue

                mint = accounts[mint_idx]

                if mint in self.monitored_projects:
                    self.record_activity(mint)  # ✅ marquer activité
                    await self.monitor_queues[mint].put((transaction, ix, discriminator))
