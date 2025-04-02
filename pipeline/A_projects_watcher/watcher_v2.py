# watcher.py
import asyncio
import json
import base64
import struct
from collections import deque
from config import PUMP_PROGRAM
from solders.transaction import VersionedTransaction

CREATE_DISCRIMINATOR = struct.pack("<Q", 8576854823835016728)


def log(msg, debug=True):
    if debug:
        print(f"[DEBUG] {msg}")


idl_cache = None


def load_idl(file_path='idl/pump_fun_idl.json'):
    global idl_cache
    if idl_cache is None:
        with open(file_path, 'r') as f:
            idl_cache = json.load(f)
    return idl_cache


def decode_create_instruction(ix_data, ix_def, accounts):
    args = {}
    offset = 8
    for arg in ix_def['args']:
        if arg['type'] == 'string':
            length = struct.unpack_from('<I', ix_data, offset)[0]
            offset += 4
            value = ix_data[offset:offset + length].decode('utf-8')
            offset += length
        elif arg['type'] == 'publicKey':
            value = base64.b64encode(ix_data[offset:offset + 32]).decode('utf-8')
            offset += 32
        else:
            raise ValueError(f"Unsupported type: {arg['type']}")
        args[arg['name']] = value

    args['mint'] = str(accounts[0])
    args['bondingCurve'] = str(accounts[2])
    args['associatedBondingCurve'] = str(accounts[3])
    args['user'] = str(accounts[7])
    return args


async def watch_new_projects(dispatcher, filters=None, debug=False):
    filters = filters or {}
    idl = load_idl()
    create_ix_def = next(ix for ix in idl['instructions'] if ix['name'] == 'create')
    recent_mints = deque(maxlen=1000)

    while True:
        transaction, instruction, discriminator = await dispatcher.watcher_queue.get()

        account_keys = transaction.message.account_keys
        program_id = str(account_keys[instruction.program_id_index])
        if program_id != str(PUMP_PROGRAM):
            continue

        accounts = [str(account_keys[i]) for i in instruction.accounts if i < len(account_keys)]
        token_data = decode_create_instruction(instruction.data, create_ix_def, accounts)

        mint = token_data["mint"]
        if mint in recent_mints:
            continue
        recent_mints.append(mint)

        name_match = True
        user_match = True

        if "name_contains" in filters:
            name = token_data.get("name", "") + token_data.get("symbol", "")
            name_match = filters["name_contains"].lower() in name.lower()

        if "creator_address" in filters:
            user_match = token_data.get("user", "") == filters["creator_address"]

        if name_match and user_match:
            log(f"🎯 New project registered: {token_data['name']} ({mint})", debug)
            await dispatcher.register_project(token_data)
