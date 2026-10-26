"""Append-only, tamper-evident trade audit ledger.

One JSON object per line (JSONL). Each entry carries a SHA-256 hash chained to the
previous entry's hash, so any edit, deletion, or reordering of a past line breaks the
chain from that point on — `verify()` reports the first bad sequence number.

Why a separate ledger and not the existing `trading_memory.json`:
  - the memory store is MUTABLE state (capped at `max_records`, rewritten on every save),
    so it can never prove what the bot actually did on the exchange.
  - this file is append-only: nothing is ever rewritten or truncated at runtime, so it
    survives as an audit trail even if the learning memory is reset or corrupted.

The ledger records every exchange order attempt (success or failure) at the ccxt
boundary, so no code path that places an order can bypass it.
"""
import hashlib
import json
import os
import sys
import threading
from datetime import datetime, timezone

# fcntl is Unix-only; on Windows use a no-op lock
try:
    import fcntl
except ImportError:
    fcntl = None

import logging
logger = logging.getLogger(__name__)

from src.config import config

_GENESIS = "0" * 64


def _canonical(body: dict) -> str:
    """Deterministic JSON so the hash is reproducible across processes/platforms."""
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


class TradeLedger:
    """Append-only JSONL ledger with a SHA-256 hash chain."""

    def __init__(self, path: str = None):
        self.path = path or getattr(config, "ledger_file", "trade_ledger.jsonl")
        self._lock = threading.Lock()
        self._seq = 0
        self._last_hash = _GENESIS
        self._size = 0
        self._load_tail()

    def _load_tail(self):
        """Continue the chain after a restart: recover the last seq + hash."""
        try:
            if not os.path.exists(self.path):
                return
            self._size = os.path.getsize(self.path)
            last = None
            with open(self.path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        last = line
            if last:
                rec = json.loads(last)
                self._seq = int(rec.get("seq", 0))
                self._last_hash = str(rec.get("hash", _GENESIS))
        except Exception:
            # A corrupt tail must not stop trading; verification will surface it.
            pass

    def _resync_if_grown(self):
        """Re-read the authoritative tail when ANOTHER process appended.

        Two processes (the bot + any CLI/script) each keep an in-memory seq; without this
        they collide and break the chain. Cheap: only re-reads when the file grew.
        """
        try:
            cur = os.path.getsize(self.path) if os.path.exists(self.path) else 0
            if cur > self._size:
                self._load_tail()
        except Exception as e:
            logger.debug(f"Ledger resync failed: {e}")

    def append(self, event: str, **fields) -> dict:
        """Append one immutable entry. Never raises into the trading path."""
        try:
            with self._lock:
                _dir = os.path.dirname(os.path.abspath(self.path)) or "."
                os.makedirs(_dir, exist_ok=True)
                # Cross-process lock (fcntl on Linux/Pi; best-effort elsewhere) so a
                # concurrent writer cannot interleave between our tail-read and write.
                lockf = open(self.path, "a+", encoding="utf-8")
                try:
                    if fcntl:
                        fcntl.flock(lockf.fileno(), fcntl.LOCK_EX)
                    self._resync_if_grown()
                    self._seq += 1
                    body = {
                        "seq": self._seq,
                        "ts": datetime.now(timezone.utc).timestamp(),
                        "datetime": datetime.now(timezone.utc).isoformat(),
                        "event": event,
                        "data": fields,
                        "prev": self._last_hash,
                    }
                    h = hashlib.sha256((self._last_hash + _canonical(body)).encode("utf-8")).hexdigest()
                    body["hash"] = h
                    line = json.dumps(body, ensure_ascii=False, default=str)
                    lockf.write(line + "\n")
                    lockf.flush()
                    os.fsync(lockf.fileno())
                    self._last_hash = h
                    self._size = os.path.getsize(self.path)
                    return body
                finally:
                    if fcntl:
                        fcntl.flock(lockf.fileno(), fcntl.LOCK_UN)
                    lockf.close()
        except Exception as e:
            logger.debug(f"Ledger append failed: {e}")
            return {}

    def verify(self) -> dict:
        """Replay the chain. Returns {ok, entries, first_bad_seq, reason}."""
        prev = _GENESIS
        seq = 0
        if not os.path.exists(self.path):
            return {"ok": True, "entries": 0, "first_bad_seq": None,
                    "reason": "empty ledger (no orders recorded yet)"}
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                for raw in f:
                    raw = raw.strip()
                    if not raw:
                        continue
                    rec = json.loads(raw)
                    seq += 1
                    if int(rec.get("seq", -1)) != seq:
                        return {"ok": False, "entries": seq, "first_bad_seq": seq,
                                "reason": f"seq gap/mismatch at {seq}"}
                    if str(rec.get("prev")) != prev:
                        return {"ok": False, "entries": seq, "first_bad_seq": seq,
                                "reason": f"prev-hash mismatch at {seq}"}
                    h = rec.pop("hash", None)
                    expect = hashlib.sha256((prev + _canonical(rec)).encode("utf-8")).hexdigest()
                    if h != expect:
                        return {"ok": False, "entries": seq, "first_bad_seq": seq,
                                "reason": f"hash mismatch at {seq}"}
                    prev = h
        except Exception as e:
            return {"ok": False, "entries": seq, "first_bad_seq": seq + 1, "reason": str(e)}
        return {"ok": True, "entries": seq, "first_bad_seq": None, "reason": "chain intact"}

    def tail(self, n: int = 20) -> list:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                lines = [ln for ln in f if ln.strip()]
            return [json.loads(ln) for ln in lines[-n:]]
        except Exception:
            return []

    def stats(self) -> dict:
        """O(1) summary for the dashboard (last seq is the entry count)."""
        return {"entries": self._seq, "last_hash": (self._last_hash or "")[:12], "path": self.path}


ledger = TradeLedger()
