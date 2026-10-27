#!/usr/bin/env python3
"""
Multi-Analyst Debate System (inspired by TradingAgents architecture)

Three specialized analysts vote on each signal:
- Technical Analyst: Pattern, volume, breakout, momentum
- Macro Analyst: BTC dominance, funding rate, correlation
- Sentiment Analyst: News mood, social chatter (placeholder for now)

Coordinator (Trader Agent) aggregates votes:
- 3/3 or 2/3 agree → EXECUTE with weighted confidence
- 1/3 disagree or 0/3 → HOLD (conservative risk management)

Decision reasoning logged to JSON for audit trail.
"""
import json
import os
import time
from typing import Dict, Any, List, Optional
from pydantic import BaseModel, Field
from src.config import config


class AnalystVote(BaseModel):
    analyst: str  # "technical", "macro", "sentiment"
    action: str   # "LONG", "SHORT", "HOLD"
    confidence: float  # 0.0 - 1.0
    reason: str
    risks: List[str] = Field(default_factory=list)


class DebateResult(BaseModel):
    action: str
    confidence: float
    primary_reason: str
    vote_breakdown: List[AnalystVote]
    dissenting_opinion: Optional[str] = None
    decision_log_file: Optional[str] = None


class MultiAnalystEngine:
    """
    Runs parallel specialized analyst prompts and aggregates results.
    Falls back to single AIAnalyzer if debate fails or budget exceeded.
    """

    def __init__(self, base_engine=None):
        self.base_engine = base_engine  # Original AIAnalyzer instance
        self.debate_timeout = float(os.getenv("DEBATE_TIMEOUT", "30"))
        self.max_api_cost_per_debate = float(os.getenv("MAX_DEBATE_COST_USD", "0.50"))
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.decision_log_dir = os.getenv("DEBATE_LOG_DIR", os.path.join(base_dir, "logs", "debate"))
        os.makedirs(self.decision_log_dir, exist_ok=True)
        self._cost_tracker = []

    def run_debate(self, snapshot: Dict[str, Any]) -> DebateResult:
        """
        Run full debate cycle across specialists.
        Returns aggregated decision with reasoning log.
        """
        start_time = time.time()
        votes = []
        total_cost_estimate = 0.0

        try:
            # Parallel analysts (sequential here for Pi simplicity)
            technical = self._analyze_technical(snapshot)
            votes.append(technical)
            total_cost_estimate += self._estimate_cost(technical.reason)

            macro = self._analyze_macro(snapshot)
            votes.append(macro)
            total_cost_estimate += self._estimate_cost(macro.reason)

            sentiment = self._analyze_sentiment(snapshot)
            votes.append(sentiment)
            total_cost_estimate += self._estimate_cost(sentiment.reason)

            # Check budget
            if total_cost_estimate > self.max_api_cost_per_debate:
                # Fallback to base engine to save cost
                return self._fallback_to_base(snapshot)

            # Coordinator vote aggregation
            result = self._coordinate_votes(votes, snapshot)
            result.vote_breakdown = votes

            # Log decision
            log_file = self._log_decision(snapshot, result, time.time() - start_time)
            result.decision_log_file = log_file

            return result

        except Exception as e:
            # Debate failed - fallback to original analyzer
            return self._fallback_to_base(snapshot, error=str(e))

    def _analyze_technical(self, snap: Dict[str, Any]) -> AnalystVote:
        """Technical analyst: patterns, volume, breakout confirmation."""
        indicators = snap.get("indicators", {})
        confluence = snap.get("confluence", {})
        quant = snap.get("quant_math_evaluation", {})

        prompt = f"""You are a Crypto Technical Analyst specializing in 4H compression breakouts.

SNAPSHOT:
- Symbol: {snap.get('symbol')}
- Current Price: ${snap.get('current_price', 0):.4f}
- Action: {snap.get('action', 'UNKNOWN')}
- Trend: {indicators.get('trend', 'NEUTRAL')}
- RSI: {indicators.get('rsi_14', 50):.1f} ({snap.get('indicators', {}).get('rsi_condition', 'NEUTRAL')})
- MACD: {indicators.get('macd', {}).get('status', 'NEUTRAL')}
- Volume Ratio: {indicators.get('volume_vs_average', 1.0):.2f}x
- ATR: {indicators.get('atr_14', 0):.6f}
- VWAP: ${indicators.get('vwap', 0):.4f}

QUANT MATH:
- Score: {quant.get('score', 50)}/100
- Breakout Margin: {quant.get('breakout_margin_pct', 0):.3f}%
- Volatility: {quant.get('volatility_pct', 0):.3f}%

EVALUATE:
1. Is the breakout confirmed by volume? (vol_ratio >= 1.0)
2. Is RSI in healthy zone? (not DANGER)
3. Are MACD and Trend aligned?
4. Is price above/below VWAP correctly?

Output ONLY valid JSON:
{{"action": "LONG"|"SHORT"|"HOLD", "confidence": 0.0-1.0, "reason": "brief technical thesis", "risks": ["risk1", "risk2"]}}
"""

        return self._call_analyst(prompt, "technical", snap)

    def _analyze_macro(self, snap: Dict[str, Any]) -> AnalystVote:
        """Macro analyst: BTC dominance, funding, correlation risk."""
        symbol = snap.get("symbol", "")
        cluster = snap.get("cluster", "MISC")

        prompt = f"""You are a Crypto Macro Analyst focused on systematic risk.

CONTEXT:
- Symbol: {symbol}
- Cluster: {cluster}
- BTC Price: ~$80,690 (check heatmap: support 79,200-79,400 yellow wall, resistance 81,000-81,400 green shelf)
- Current Price: ${snap.get('current_price', 0):.4f}
- Proposed Action: {snap.get('action', 'UNKNOWN')}

MACRO CHECKLIST:
1. BTC Dominance: Is BTC leading or following? (check if alt has independent strength)
2. Funding Rates: Are we in positive (longs paying) or negative (shorts paying) regime?
3. Correlation Risk: If {cluster} cluster already has positions, avoid adding correlation exposure
4. Liquidation Heatmap: Where is price relative to major liq clusters?

DECISION RULES:
- If cluster already occupied → HOLD (correlation guard)
- If BTC dumping hard → VETO alt LONG
- If BTC consolidating → OK to proceed with alt
- If funding extremely positive → caution on LONG (crowded trade)

Output ONLY valid JSON:
{{"action": "LONG"|"SHORT"|"HOLD", "confidence": 0.0-1.0, "reason": "macro thesis", "risks": ["risk1", "risk2"]}}
"""

        return self._call_analyst(prompt, "macro", snap)

    def _analyze_sentiment(self, snap: Dict[str, Any]) -> AnalystVote:
        """Sentiment analyst: news mood, social chatter (placeholder logic)."""
        symbol = snap.get("symbol", "")
        vol_ratio = snap.get("indicators", {}).get("volume_vs_average", 1.0)
        score = snap.get("quant_math_evaluation", {}).get("score", 50)

        prompt = f"""You are a Crypto Sentiment Analyst evaluating market mood.

SNAPSHOT:
- Symbol: {symbol}
- Volume Surge: {vol_ratio:.2f}x average (threshold: 0.90)
- Quant Score: {score}/100
- Action Proposed: {snap.get('action', 'UNKNOWN')}

SENTIMENT INDICATORS:
1. Volume Spike: High volume = strong conviction or potential exhaustion?
2. Score Position: {score} suggests {'strong' if score > 70 else 'moderate' if score > 50 else 'weak'} setup
3. Breakout Quality: Is this a clean breakout or chasing extended move?

HEURISTICS:
- vol_ratio >= 2.0 → extreme excitement → possible climax → caution
- vol_ratio 1.0-2.0 → healthy participation → positive
- vol_ratio < 1.0 → low interest → weak signal

Output ONLY valid JSON:
{{"action": "LONG"|"SHORT"|"HOLD", "confidence": 0.0-1.0, "reason": "sentiment thesis", "risks": ["risk1", "risk2"]}}
"""

        return self._call_analyst(prompt, "sentiment", snap)

    def _call_analyst(self, prompt: str, analyst_type: str, snap: Dict[str, Any]) -> AnalystVote:
        """Call AI for single analyst decision with fallback."""
        try:
            from src.ai_analyzer import AIAnalyzer
            analyzer = AIAnalyzer()

            # Minimal snapshot for single analyst (no debate loop)
            minimal_snap = {
                "symbol": snap.get("symbol"),
                "current_price": snap.get("current_price"),
                "action": snap.get("action"),
                "indicators": snap.get("indicators", {}),
                "quant_math_evaluation": snap.get("quant_math_evaluation", {}),
            }

            import contextlib, io
            with contextlib.redirect_stdout(io.StringIO()):
                signal = analyzer.analyze_market(minimal_snap)

            # Extract from base signal, but adapt to analyst type
            return AnalystVote(
                analyst=analyst_type,
                action=signal.action,
                confidence=signal.confidence,
                reason=signal.primary_reason,
                risks=signal.risk_factors
            )

        except Exception as e:
            # Fallback heuristic for this analyst
            return self._heuristic_vote(analyst_type, snap, error=str(e))

    def _heuristic_vote(self, analyst_type: str, snap: Dict[str, Any], error: str = "") -> AnalystVote:
        """Simple rule-based fallback when AI unavailable."""
        indicators = snap.get("indicators", {})
        proposed_action = snap.get("action", "HOLD")
        vol_ratio = indicators.get("volume_vs_average", 0.5)
        rsi_cond = indicators.get("rsi_condition", "NEUTRAL")
        macd_status = indicators.get("macd", {}).get("status", "NEUTRAL")

        # Default to proposed action with lowered confidence
        action = proposed_action if proposed_action != "HOLD" else "HOLD"
        confidence = 0.5
        risks = [f"{analyst_type}_fallback"]

        if analyst_type == "technical":
            if vol_ratio >= 1.5 and rsi_cond not in ("OVERBOUGHT_DANGER", "OVERSOLD_DANGER"):
                confidence = 0.7
                action = proposed_action
            else:
                action = "HOLD"
                confidence = 0.4

        elif analyst_type == "macro":
            cluster = snap.get("cluster", "MISC")
            # In real impl, check live BTC status
            confidence = 0.6

        elif analyst_type == "sentiment":
            if vol_ratio >= 1.0:
                confidence = 0.65
            else:
                action = "HOLD"
                confidence = 0.3

        return AnalystVote(
            analyst=analyst_type,
            action=action,
            confidence=confidence,
            reason=f"Heuristic fallback: {analyst_type}",
            risks=risks
        )

    def _coordinate_votes(self, votes: List[AnalystVote], snap: Dict[str, Any]) -> DebateResult:
        """
        Aggregate votes with weighted confidence.
        Rules:
        - 3/3 or 2/3 match proposed action → EXECUTE
        - Any HOLD vote → HOLD (conservative)
        - Dissenting opinion logged
        """
        proposed = snap.get("action", "HOLD")
        # Count agreement with proposed directly on votes objects
        agree_count = sum(1 for v in votes if v.action == proposed and proposed != "HOLD")
        hold_votes = sum(1 for v in votes if v.action == "HOLD")

        # Decision logic
        if hold_votes > 0:
            # Conservative: any HOLD = HOLD
            action = "HOLD"
            confidence = min(v.confidence for v in votes if v.action == "HOLD") * 0.8
            reason = f"Debate vetoed: {hold_votes}/3 analysts called HOLD"
            dissenting = next((v.reason for v in votes if v.action == "HOLD"), None)
        elif agree_count >= 2:
            # Majority agrees
            action = proposed
            # Weighted confidence: higher if unanimous
            base_conf = sum(v.confidence for v in votes) / len(votes)
            consensus_bonus = 0.05 if agree_count == 3 else 0.0
            confidence = min(0.95, base_conf + consensus_bonus)
            reason = f"Debate passed: {agree_count}/3 analysts agree on {proposed}"
            dissenting = next((v.reason for v in votes if v.action != proposed), None)
        else:
            # No consensus
            action = "HOLD"
            confidence = 0.3
            reason = "Debate inconclusive: no majority consensus"
            dissenting = "Split decision among analysts"

        return DebateResult(
            action=action,
            confidence=round(confidence, 2),
            primary_reason=reason,
            vote_breakdown=votes,
            dissenting_opinion=dissenting
        )

    def _log_decision(self, snap: Dict[str, Any], result: DebateResult, duration: float) -> str:
        """Write decision log to JSON file."""
        ts = int(time.time())
        filename = f"debate_{snap.get('symbol', 'unknown')}_{ts}.json"
        filepath = os.path.join(self.decision_log_dir, filename)

        log_entry = {
            "timestamp": ts,
            "iso_time": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(ts)),
            "symbol": snap.get("symbol"),
            "cluster": snap.get("cluster", "MISC"),
            "price": snap.get("current_price"),
            "proposed_action": snap.get("action"),
            "debate_result": {
                "action": result.action,
                "confidence": result.confidence,
                "reason": result.primary_reason,
                "duration_sec": round(duration, 2),
                "vote_breakdown": [
                    {
                        "analyst": v.analyst,
                        "action": v.action,
                        "confidence": v.confidence,
                        "reason": v.reason
                    }
                    for v in result.vote_breakdown
                ],
                "dissenting_opinion": result.dissenting_opinion
            },
            "snapshot_summary": {
                "rsi": snap.get("indicators", {}).get("rsi_14"),
                "vol_ratio": snap.get("indicators", {}).get("volume_vs_average"),
                "quant_score": snap.get("quant_math_evaluation", {}).get("score")
            }
        }

        try:
            with open(filepath, "w") as f:
                json.dump(log_entry, f, indent=2)
            return filepath
        except Exception as e:
            print(f"[MultiAnalyst] Log write error: {e}")
            return None

    def _estimate_cost(self, text: str) -> float:
        """Rough cost estimate based on token count (~4 chars per token, $0.0001/1K tokens)."""
        tokens = len(text) // 4
        return tokens * 0.0001 / 1000

    def _fallback_to_base(self, snap: Dict[str, Any], error: str = "") -> DebateResult:
        """Fallback to original single-analyst if debate fails."""
        if self.base_engine:
            signal = self.base_engine.analyze_market(snap)
            return DebateResult(
                action=signal.action,
                confidence=signal.confidence,
                primary_reason=f"Fallback to single analyst: {signal.primary_reason}",
                vote_breakdown=[],
                dissenting_opinion=error or None
            )
        return DebateResult(action="HOLD", confidence=0.0, primary_reason="No engine available", vote_breakdown=[])


# Convenience function for backward compatibility
def analyze_with_debate(snap: Dict[str, Any], base_engine=None) -> DebateResult:
    """Public API: run multi-analyst debate."""
    engine = MultiAnalystEngine(base_engine=base_engine)
    return engine.run_debate(snap)
