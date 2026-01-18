import os
import time
from typing import Dict, Tuple

from flask import Flask, request, jsonify
from flask_cors import CORS

import base58
from solana.rpc.api import Client
from solana.rpc.types import TxOpts
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.system_program import transfer, TransferParams
from solders.transaction import Transaction

# =====================
# Flask app
# =====================
app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

# =====================
# Environment
# =====================
SOLANA_RPC_URL = os.getenv("SOLANA_RPC_URL", "https://api.devnet.solana.com")
REWARD_SOL_DEFAULT = float(os.getenv("REWARD_SOL_DEFAULT", "0.01"))
SECRET_B58 = os.getenv("REWARD_WALLET_SECRET_BASE58")

if not SECRET_B58:
    raise RuntimeError("Missing REWARD_WALLET_SECRET_BASE58")

LAMPORTS_PER_SOL = 1_000_000_000

# =====================
# Solana setup
# =====================
client = Client(SOLANA_RPC_URL)
secret_bytes = base58.b58decode(SECRET_B58)

if len(secret_bytes) != 64:
    raise RuntimeError("REWARD_WALLET_SECRET_BASE58 must decode to 64 bytes")

payer = Keypair.from_bytes(secret_bytes)
payer_pubkey = payer.pubkey()

# =====================
# Idempotency (demo only)
# =====================
PAID: Dict[Tuple[str, str], Tuple[str, float]] = {}
PAID_TTL_SEC = 60 * 60 * 24 * 7  # 7 days

def cleanup_paid():
    now = time.time()
    for k, (_, ts) in list(PAID.items()):
        if now - ts > PAID_TTL_SEC:
            PAID.pop(k, None)

# =====================
# Routes
# =====================
@app.get("/")
def root():
    return jsonify({"ok": True, "service": "solana-reward-api"})

@app.get("/health")
def health():
    return jsonify({
        "ok": True,
        "rpc": SOLANA_RPC_URL,
        "from_wallet": str(payer_pubkey),
        "default_reward_sol": REWARD_SOL_DEFAULT,
    })

@app.post("/reward/send")
def reward_send():
    cleanup_paid()
    body = request.get_json(silent=True) or {}

    receiver = body.get("receiver_wallet_address")
    amount_sol = body.get("amount_sol", REWARD_SOL_DEFAULT)
    idem_key = body.get("idempotency_key")

    if not receiver:
        return jsonify({"ok": False, "error": "Missing receiver_wallet_address"}), 400

    try:
        amount_sol = float(amount_sol)
        if amount_sol <= 0 or amount_sol > 0.5:
            return jsonify({"ok": False, "error": "amount_sol out of range"}), 400
    except Exception:
        return jsonify({"ok": False, "error": "Invalid amount_sol"}), 400

    try:
        to_pubkey = Pubkey.from_string(receiver)
    except Exception:
        return jsonify({"ok": False, "error": "Invalid receiver wallet address"}), 400

    if idem_key:
        key = (receiver, str(idem_key))
        if key in PAID:
            sig, _ = PAID[key]
            return jsonify({
                "ok": True,
                "signature": sig,
                "already_paid": True,
            })

    lamports = int(amount_sol * LAMPORTS_PER_SOL)

    try:
        blockhash = client.get_latest_blockhash().value.blockhash
        ix = transfer(
            TransferParams(
                from_pubkey=payer_pubkey,
                to_pubkey=to_pubkey,
                lamports=lamports,
            )
        )
        tx = Transaction.new_signed_with_payer(
            [ix],
            payer_pubkey,
            [payer],
            blockhash,
        )
        sig = client.send_transaction(
            tx,
            opts=TxOpts(skip_preflight=False, preflight_commitment="confirmed"),
        ).value
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

    if idem_key:
        PAID[(receiver, str(idem_key))] = (sig, time.time())

    return jsonify({
        "ok": True,
        "signature": sig,
        "from_wallet": str(payer_pubkey),
        "to_wallet": receiver,
        "amount_sol": amount_sol,
        "already_paid": False,
    })
