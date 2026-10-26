import json
import os
import time
import logging
from typing import Dict, Any, List
from datetime import datetime, timezone

from src.config import config

logger = logging.getLogger("self_learning_memory")

MEMORY_FILE = "trading_memory.json"

class SelfLearningMemory:
    """
    Episodic Self-Learning Memory & Adaptive Feedback Engine.
    
    Capabilities:
    1. Experiential Learning: Records feature snapshots of winning and losing setups.
    2. Mistake Invalidation: Identifies recurring losing patterns (e.g. low-volume pullbacks, false breakouts).
    3. Dynamic Symbol Cooldown: Automatically quarantines coins that trigger stop-losses (prevents revenge trading).
    4. Pattern Similarity Penalty: Penalizes setups that share >=80% similarity with recent failures.
    5. Adaptive LLM Context: Injects distilled lessons learned directly into the AI Analyzer prompt.
    """
    def __init__(self, memory_filepath: str = MEMORY_FILE, max_records: int = 150):
        self.memory_filepath = memory_filepath
        self.max_records = max_records
        self.trades_history: List[Dict[str, Any]] = []
        self.symbol_cooldowns: Dict[str, float] = {} # symbol -> cooldown expiry timestamp
        self.lessons_learned: List[str] = []
        self._load_memory()

    def _load_memory(self):
        """Loads memory state from persistent JSON file."""
        try:
            if os.path.exists(self.memory_filepath):
                with open(self.memory_filepath, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.trades_history = data.get("trades_history", [])[-self.max_records:]
                    self.symbol_cooldowns = data.get("symbol_cooldowns", {})
                    self.lessons_learned = data.get("lessons_learned", [])[-30:]
        except Exception as e:
            logger.debug(f"Could not load memory file: {e}")
            self.trades_history = []
            self.symbol_cooldowns = {}
            self.lessons_learned = []

    def _save_memory(self):
        """Persists memory state to JSON file."""
        try:
            # Clean expired cooldowns before saving
            now = time.time()
            active_cooldowns = {k: v for k, v in self.symbol_cooldowns.items() if v > now}
            data = {
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "trades_history": self.trades_history[-self.max_records:],
                "symbol_cooldowns": active_cooldowns,
                "lessons_learned": self.lessons_learned[-30:]
            }
            # AUDIT FIX: atomic write — a crash mid-write (or a concurrent web/bot writer)
            # used to corrupt the whole learning store. Write temp then os.replace().
            import tempfile
            _dir = os.path.dirname(os.path.abspath(self.memory_filepath)) or "."
            os.makedirs(_dir, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(dir=_dir, prefix=".mem_", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)
                os.replace(tmp_path, self.memory_filepath)
            except Exception:
                try:
                    os.remove(tmp_path)
                except Exception as e:
                    logger.debug(f"Temp memory cleanup failed: {e}")
                raise
        except Exception as e:
            logger.warning(f"Could not save memory file: {e}")

    def record_trade_outcome(self, symbol: str, snapshot: Dict[str, Any], pnl: float, exit_type: str,
                             entry_price: float = 0.0, stop_price: float = 0.0):
        """
        Records trade conclusion and distills lessons.
        exit_type: 'TAKE_PROFIT' or 'STOP_LOSS'

        P7 FORWARD-TEST: when entry_price and stop_price are supplied, the record also
        carries `r_multiple` = pnl / (risk in USDT). That makes LIVE results directly
        comparable to the backtest edge (+0.17R) instead of only a USD figure.
        """
        now = time.time()
        indicators = snapshot.get("indicators", {}) if snapshot else {}
        rsi = float(indicators.get("rsi_14", 50.0))
        vol = float(indicators.get("volume_vs_average", 1.0))
        trend = snapshot.get("trend_bias", "NEUTRAL") if snapshot else "NEUTRAL"
        price = float(snapshot.get("current_price", 0.0)) if snapshot else 0.0
        ema_20 = float(indicators.get("ema_20", price))
        dist_ema20 = round((price - ema_20) / price, 4) if price > 0 else 0.0

        is_win = (pnl > 0)

        # R-multiple (risk-normalised return). Only meaningful with a real risk distance.
        r_multiple = None
        risk_dist_pct = None
        entry_p = float(entry_price or 0.0)
        stop_p = float(stop_price or 0.0)
        if entry_p > 0 and stop_p > 0 and entry_p > stop_p:
            risk_dist_pct = (entry_p - stop_p) / entry_p
            notional = float(snapshot.get("position_notional_usdt", 0.0) or 0.0)
            risk_usdt = notional * risk_dist_pct if notional > 0 else 0.0
            if risk_usdt > 1e-9:
                r_multiple = round(pnl / risk_usdt, 4)

        # Distill episodic takeaway
        if is_win:
            lesson = f"Sukses ({symbol}): Profit +${pnl:.4f} diamankan (Exit: {exit_type}, RSI {rsi:.1f}, Vol {vol:.1f}x, Trend {trend})."
        else:
            # Stop loss penalty & lesson
            if vol < 0.8:
                reason_detail = f"volume lemah ({vol:.1f}x) rentan false-break"
            elif rsi > 60:
                reason_detail = f"RSI overextended ({rsi:.1f}) di area rawan puncak"
            elif dist_ema20 > 0.015:
                reason_detail = f"terlalu jauh di atas support EMA20 (+{dist_ema20*100:.1f}%)"
            else:
                reason_detail = f"pembalikan arah mendadak di bawah support"

            lesson = f"Evaluasi Rugi ({symbol}): Entry gagal ({reason_detail}). Loss: ${pnl:.2f}."
            
            # Apply 4-Hour Cooldown quarantine to this coin (strictly eliminate revenge trading)
            cooldown_seconds = 14400 # 4 hours
            self.symbol_cooldowns[symbol] = now + cooldown_seconds

        record = {
            "timestamp": now,
            "datetime": datetime.now(timezone.utc).isoformat(),
            "symbol": symbol,
            "exit_type": exit_type,
            "pnl": round(pnl, 4),
            "is_win": is_win,
            "r_multiple": r_multiple,
            "risk_dist_pct": round(risk_dist_pct, 5) if risk_dist_pct else None,
            "features": {
                "rsi_14": rsi,
                "volume_vs_average": vol,
                "dist_to_ema20": dist_ema20,
                "trend_bias": trend
            },
            "lesson": lesson
        }

        self.trades_history.append(record)
        self.lessons_learned.append(lesson)
        self._save_memory()
        return lesson

    def is_symbol_in_cooldown(self, symbol: str) -> bool:
        """Returns True if the symbol is in post-loss quarantine."""
        expiry = self.symbol_cooldowns.get(symbol, 0.0)
        return time.time() < expiry

    def get_symbol_cooldown_remaining_minutes(self, symbol: str) -> float:
        """Returns remaining cooldown minutes for a symbol."""
        expiry = self.symbol_cooldowns.get(symbol, 0.0)
        rem = expiry - time.time()
        return round(rem / 60.0, 1) if rem > 0 else 0.0

    def _extract_cand_features(self, snapshot: Dict[str, Any]) -> tuple:
        """Extracts normalized candidate features for similarity computation."""
        indicators = snapshot.get("indicators", {}) if snapshot else {}
        cand_rsi = float(indicators.get("rsi_14", 50.0))
        cand_vol = float(indicators.get("volume_vs_average", 1.0))
        cand_price = float(snapshot.get("current_price", 0.0) or 0.0) if snapshot else 0.0
        cand_ema20 = float(indicators.get("ema_20", cand_price))
        cand_dist_ema20 = (cand_price - cand_ema20) / cand_price if cand_price > 0 else 0.0
        return cand_rsi, cand_vol, cand_price, cand_dist_ema20

    def _calc_similarity(self, cand_rsi: float, cand_vol: float, cand_dist_ema20: float, feat: Dict[str, Any]) -> float:
        """Computes Euclidean normalized distance and returns percentage similarity (0 - 100%)."""
        rsi_diff = abs(cand_rsi - feat.get("rsi_14", 50.0)) / 40.0
        vol_diff = abs(cand_vol - feat.get("volume_vs_average", 1.0)) / 2.0
        dist_diff = abs(cand_dist_ema20 - feat.get("dist_to_ema20", 0.0)) / 0.04
        avg_dist = min(1.0, (rsi_diff * 0.45) + (vol_diff * 0.35) + (dist_diff * 0.20))
        return round((1.0 - avg_dist) * 100, 1)

    def evaluate_quant_synergy(self, snapshot: Dict[str, Any]) -> Dict[str, Any]:
        """
        Deep Mathematical Synergy between Quant Math and Self-Learning Memory:
        1. Low Loss Guard: Detects resemblance to recent stop-loss failures, penalizes score, & tightens SL buffer.
        2. High Profit Catalyst: Identifies resemblance to high-profit winners, awards Alpha Boost, & expands Take-Profit target.
        3. Cluster Expectancy: Evaluates local cluster win-rate for dynamic Kelly position sizing.
        """
        if not snapshot or not self.trades_history:
            return {
                "penalty_points": 0.0,
                "is_similar_to_failure": False,
                "is_penalized": False,
                "match_pct": 0.0,
                "max_similarity": 0.0,
                "reason": "",
                "alpha_boost": 0.0,
                "boost_reason": "",
                "profit_multiplier": 1.0,
                "sl_tightening_factor": 1.5,
                "sizing_multiplier": 1.0,
                "cluster_win_rate": None,
                "cluster_sample_size": 0
            }

        cand_rsi, cand_vol, cand_price, cand_dist_ema20 = self._extract_cand_features(snapshot)

        now = time.time()
        recent_losses = [
            t for t in self.trades_history
            if not t["is_win"] and (now - t["timestamp"] < 86400)
        ] or [t for t in self.trades_history if not t["is_win"]]
        all_wins = [t for t in self.trades_history if t["is_win"]]

        # 1. EVALUATE SIMILARITY TO RECENT FAILURES (Low-Loss Guard)
        highest_loss_sim = 0.0
        matched_loss = None
        for loss in recent_losses[-15:]:
            sim = self._calc_similarity(cand_rsi, cand_vol, cand_dist_ema20, loss.get("features", {}))
            if sim > highest_loss_sim:
                highest_loss_sim = sim
                matched_loss = loss

        penalty_pts = 0.0
        penalty_reason = ""
        is_penalized = False
        sl_tightening = 1.5

        # COLD-START GUARD: with only a handful of trades the "similarity to a past
        # loss" signal is statistically meaningless noise — penalising on it locks
        # the bot out of the market (death spiral). Require a real sample first.
        min_sample = int(os.getenv("MEMORY_GUARD_MIN_SAMPLE", "15"))
        guard_active = len(self.trades_history) >= min_sample

        if guard_active and highest_loss_sim >= 80.0 and matched_loss:
            # TIME-DECAY: a loss from weeks ago is weaker evidence than one from
            # an hour ago. Scale the penalty by how fresh the matched loss is.
            age_h = max(0.0, (now - float(matched_loss.get("timestamp", now))) / 3600.0)
            decay = 0.5 ** (age_h / 72.0)      # halves every 3 days
            penalty_pts = round((18.0 if highest_loss_sim >= 88.0 else 12.0) * decay, 2)
            if penalty_pts >= 2.0:             # ignore negligible stale penalties
                is_penalized = True
                sl_tightening = 1.1  # Tighten SL buffer (Save margin!)
                penalty_reason = (
                    f"Setup mirip {highest_loss_sim}% dengan kegagalan {matched_loss['symbol']} "
                    f"(Loss ${matched_loss['pnl']}, {age_h:.0f}j lalu). Skor Quant dipotong -{penalty_pts} poin."
                )
            else:
                penalty_pts = 0.0

        # 2. EVALUATE SIMILARITY TO PROVEN WINNERS (High-Profit Catalyst)
        highest_win_sim = 0.0
        matched_win = None
        for win in all_wins[-20:]:
            sim = self._calc_similarity(cand_rsi, cand_vol, cand_dist_ema20, win.get("features", {}))
            if sim > highest_win_sim:
                highest_win_sim = sim
                matched_win = win

        alpha_boost = 0.0
        boost_reason = ""
        profit_mult = 1.0

        if highest_win_sim >= 75.0 and matched_win and not is_penalized:
            alpha_boost = 10.0 if highest_win_sim >= 85.0 else 6.0
            profit_mult = 1.30 if highest_win_sim >= 85.0 else 1.18 # Expand profit target!
            boost_reason = f"Setup memiliki kemiripan {highest_win_sim}% dengan pola juara {matched_win['symbol']} (Profit +${matched_win['pnl']:.2f}). Quant Alpha Boost +{alpha_boost} pts (Target Profit x{profit_mult})."

        # 3. STATISTICAL CLUSTER WIN-RATE & KELLY SIZING
        cluster = []
        for t in self.trades_history[-30:]:
            sim = self._calc_similarity(cand_rsi, cand_vol, cand_dist_ema20, t.get("features", {}))
            if sim >= 70.0:
                cluster.append(t)

        cluster_wr = None
        cluster_expectancy = None
        sizing_mult = 1.0
        if len(cluster) >= int(getattr(config, "memory_cluster_min_sample", 10)):
            wins_count = sum(1 for c in cluster if c["is_win"])
            cluster_wr = round((wins_count / len(cluster)) * 100, 1)
            # AUDIT FIX: size on risk-normalised EXPECTANCY (R-multiple), not raw USD pnl.
            # Raw pnl is not comparable across symbols/sizes; R makes clusters comparable.
            r_vals = [float(c.get("r_multiple", 0.0) or 0.0) for c in cluster if c.get("r_multiple") is not None]
            if r_vals:
                cluster_expectancy = round(sum(r_vals) / len(r_vals), 4)
            else:
                cluster_expectancy = round(sum(float(c.get("pnl", 0.0)) for c in cluster) / len(cluster), 6)
            if cluster_expectancy > 0 and cluster_wr >= 60.0:
                sizing_mult = 1.20
                profit_mult = max(profit_mult, 1.25)
            elif cluster_expectancy <= 0:
                sizing_mult = 0.70
                sl_tightening = min(sl_tightening, 1.1)

        return {
            "penalty_points": penalty_pts,
            "is_similar_to_failure": is_penalized,
            "is_penalized": is_penalized,
            "match_pct": highest_loss_sim if is_penalized else highest_win_sim,
            "max_similarity": (highest_loss_sim if is_penalized else highest_win_sim) / 100.0,
            "reason": penalty_reason if is_penalized else boost_reason,
            "penalty_reason": penalty_reason,
            "alpha_boost": alpha_boost,
            "boost_reason": boost_reason,
            "profit_multiplier": profit_mult,
            "sl_tightening_factor": sl_tightening,
            "sizing_multiplier": sizing_mult,
            "cluster_win_rate": cluster_wr,
            "cluster_expectancy": cluster_expectancy,
            "cluster_sample_size": len(cluster),
            "matched_symbol": matched_loss["symbol"] if (is_penalized and matched_loss) else (matched_win["symbol"] if matched_win else None)
        }

    def evaluate_setup_similarity_penalty(self, snapshot: Dict[str, Any]) -> Dict[str, Any]:
        """Backwards compatibility alias for evaluate_quant_synergy."""
        return self.evaluate_quant_synergy(snapshot)

    def get_recent_lessons_for_ai_prompt(self, limit: int = 3) -> str:
        """
        Returns recent lessons to inject into the AI prompt.

        COLD-START GUARD: with only a handful of trades, "here are my losses" is noise, not
        signal — a 0%-win-rate feed poisons the AI into vetoing everything (death spiral).
        Require the same min-sample the score penalty uses before feeding losses; below it,
        report a neutral calibration status so the AI judges the setup on its own merits.
        """
        min_sample = int(getattr(config, "memory_lesson_feed_min_sample", 15) or 15)
        n = len(self.trades_history)
        if n < min_sample:
            wins = sum(1 for t in self.trades_history if t["is_win"])
            return (f"Calibration active: only {n}/{min_sample} trades recorded — sample too small "
                    f"to draw lessons from (wins {wins}/{n}). Judge this setup on its own merits.")
        recent_losses = [t for t in self.trades_history if not t["is_win"]]
        if not recent_losses:
            return "No recent mistakes recorded. Trade discipline active."
        return "\n".join(f"- {l['lesson']}" for l in recent_losses[-limit:])

    def get_summary_stats(self) -> Dict[str, Any]:
        """Returns memory metrics for Web Dashboard."""
        total = len(self.trades_history)
        wins = sum(1 for t in self.trades_history if t["is_win"])
        losses = total - wins
        win_rate = round((wins / total) * 100, 1) if total > 0 else 0.0
        total_pnl = round(sum(t["pnl"] for t in self.trades_history), 2)
        
        now = time.time()
        active_cooldowns = [
            f"{sym} ({round((exp - now)/60.0, 1)}m)"
            for sym, exp in self.symbol_cooldowns.items()
            if exp > now
        ]

        return {
            "total_experiences": total,
            "total_wins": wins,
            "total_losses": losses,
            "win_rate_pct": win_rate,
            "total_memory_pnl": total_pnl,
            "active_cooldowns": active_cooldowns,
            "active_cooldowns_count": len(active_cooldowns),
            "cooldowns_count": len(active_cooldowns),
            "recent_lessons": self.lessons_learned[-5:]
        }
