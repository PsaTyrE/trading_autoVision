import logging
from typing import Dict, Any, Optional
from pydantic import BaseModel, Field
from src.config import config

logger = logging.getLogger("hybrid_engine")

class QuantEvaluation(BaseModel):
    score: float = Field(description="Quantitative bullish score 0 to 100")
    trend_bias: str = Field(description="BULLISH, BEARISH, or NEUTRAL")
    action: str = Field(description="LONG, SHORT, or HOLD")
    rsi: float = Field(default=50.0)
    is_overbought: bool = Field(default=False)
    is_oversold: bool = Field(default=False)
    volatility_pct: float = Field(default=2.0)
    recommended_stop_loss: float = Field(default=0.0)
    recommended_take_profit: float = Field(default=0.0, description="Quantitative Asymmetric Take-Profit Target based on R:R >= 2.0 and Resistance")
    target_rr_ratio: float = Field(default=2.0, description="Mathematical Risk-to-Reward Ratio")
    recommended_grid_count: int = Field(default=8)
    profit_multiplier: float = Field(default=1.0, description="Quantitative profit target expansion multiplier for high profit")
    sizing_multiplier: float = Field(default=1.0, description="Mathematical position sizing factor based on pattern expectancy")
    reasons: list[str] = Field(default_factory=list)

class ConsensusDecision(BaseModel):
    action: str = Field(description="Final action: LONG, HOLD, or SHORT")
    status: str = Field(description="FULL_CONSENSUS, DEFENSIVE_GRID, GUARDRAIL_OVERRIDE, or QUANT_FAILOVER")
    confidence_pct: int = Field(description="Combined confidence percentage 0 - 100")
    stop_loss: float = Field(description="Harmonized strict stop-loss price")
    take_profit: float = Field(default=0.0, description="Harmonized strict take-profit price based on Market Structure & AI/Quant Consensus")
    risk_reward_ratio: float = Field(default=2.0, description="Effective Risk-to-Reward ratio (Reward / Risk)")
    grid_count: int = Field(default=8, description="Optimized grid line count")
    profit_multiplier: float = Field(default=1.0, description="Dynamic profit expansion target")
    sizing_multiplier: float = Field(default=1.0, description="Dynamic position size calibration")
    explanation: str = Field(description="Detailed consensus rationale")
    quant_score: float = Field(default=0.0)
    ai_confidence: float = Field(default=0.0)
    ai_model_used: str = Field(default="Quant Math Engine")
    is_failover_active: bool = Field(default=False)


class QuantMathEngine:
    """
    Pure mathematical quantitative engine.
    Calculates statistical and technical indicators without external network dependencies:
    - Trend Alignment (EMA 20 vs EMA 50)
    - Momentum & Boundaries (RSI 14)
    - Moving Average Convergence Divergence (MACD)
    - Volatility & Risk (ATR 14 & Bollinger Bands Width)
    - Volume Surge Confirmation (Volume vs 20-SMA)
    """

    @staticmethod
    def evaluate(snapshot: Dict[str, Any], leverage: int = 5, memory: Optional[Any] = None) -> QuantEvaluation:
        price = float(snapshot.get("current_price", 0.0))
        if price <= 0:
            return QuantEvaluation(
                score=50.0, trend_bias="NEUTRAL", action="HOLD",
                recommended_stop_loss=0.0, reasons=["Invalid market price"]
            )

        indicators = snapshot.get("indicators", {})
        trend = snapshot.get("trend_bias", "NEUTRAL")
        rsi = float(indicators.get("rsi_14", 50.0))
        raw_atr = float(indicators.get("atr_14", 0.0) or 0.0)
        atr = raw_atr if raw_atr > 0 else max(price * 0.015, 0.0001)
        vol_ratio = float(indicators.get("volume_vs_average", 1.0))
        
        macd_info = indicators.get("macd", {})
        macd_status = macd_info.get("status", "NORMAL") if isinstance(macd_info, dict) else "NORMAL"
        macd_hist = float(macd_info.get("histogram", 0.0)) if isinstance(macd_info, dict) else 0.0

        bb = indicators.get("bollinger_bands", {})
        bb_upper = float(bb.get("upper", price * 1.05)) if isinstance(bb, dict) else price * 1.05
        bb_lower = float(bb.get("lower", price * 0.95)) if isinstance(bb, dict) else price * 0.95

        volatility_pct = round((atr / max(price, 0.0001)) * 100, 2)
        score = 50.0
        reasons = []

        # 1. Trend Factor (+/- 25 pts)
        if trend == "BULLISH":
            score += 25.0
            reasons.append("EMA Alignment: Bullish (Price > EMA20 > EMA50)")
        elif trend == "BEARISH":
            score -= 25.0
            reasons.append("EMA Alignment: Bearish (Price < EMA20 < EMA50)")
        else:
            reasons.append("EMA Alignment: Netral / Konsolidasi")

        # 2. RSI Factor (+/- 20 pts)
        is_overbought = rsi >= float(getattr(config, "quant_rsi_overbought", 74.0))
        is_oversold = rsi <= float(getattr(config, "quant_rsi_oversold", 32.0))

        if is_overbought:
            score -= 20.0
            reasons.append(f"RSI Overbought ({rsi:.1f}) - Resiko pullback/puncak lokal tinggi")
        elif float(getattr(config, "quant_rsi_healthy_low", 45.0)) <= rsi <= float(getattr(config, "quant_rsi_healthy_high", 68.0)):
            score += 20.0
            reasons.append(f"RSI Sehat ({rsi:.1f}) - Ruang kenaikan momentum optimal")
        elif is_oversold:
            score -= 5.0
            reasons.append(f"RSI Oversold ({rsi:.1f}) - Potensi pantulan tapi tren rentan")
        else:
            reasons.append(f"RSI Netral ({rsi:.1f})")

        # 3. MACD Momentum (+/- 15 pts)
        if macd_status == "BULLISH_CROSS" or macd_hist > 0:
            score += 15.0
            reasons.append("MACD: Momentum Bullish aktif")
        elif macd_status == "BEARISH_CROSS" or macd_hist < 0:
            score -= 15.0
            reasons.append("MACD: Momentum Melemah (Bearish)")

        # 4. Volume Confirmation (+/- 10 pts) + P1 Volume Surge Boost
        vol_ratio = float(indicators.get("volume_vs_average", 1.0))
        volume_boost = 0.0
        # P1: Volume surge boost +5 when volRatio >= 2.0
        if vol_ratio >= float(getattr(config, "volume_surge_threshold", 2.0)):
            volume_boost = float(getattr(config, "volume_surge_boost", 5.0))
            score += volume_boost
            reasons.append(f"📈 Volume Surge Boost (+{volume_boost:.0f}) volRatio={vol_ratio:.1f}x")
        elif vol_ratio >= float(getattr(config, "quant_vol_strong_ratio", 1.2)):
            score += 10.0
            reasons.append(f"Volume Kuat ({vol_ratio:.1f}x rata-rata)")
        elif vol_ratio < float(getattr(config, "quant_vol_weak_ratio", 0.6)):
            score -= 10.0
            reasons.append(f"Volume Lemah ({vol_ratio:.1f}x rata-rata) - Rawan False Break")

        # 5. Bollinger Band Position Check
        if price > bb_upper:
            score -= 10.0
            reasons.append("Harga menembus Upper Bollinger Band (Peregangan ekstrim)")

        # 6. MULTI-TIMEFRAME (MTF 1-HOUR) MACRO TREND CONFLUENCE
        mtf_1h = snapshot.get("mtf_1h")
        if isinstance(mtf_1h, dict):
            macro_trend = mtf_1h.get("trend", "NEUTRAL")
            rsi_1h = float(mtf_1h.get("rsi", 50.0))
            if macro_trend == "BULLISH":
                score += 15.0
                reasons.append(f"🌐 [MTF 1-JAM BULLISH] Selaras dengan tren makro 1H (RSI 1H: {rsi_1h:.1f})")
            elif macro_trend == "BEARISH":
                score -= 30.0
                reasons.append(f"⚠️ [MTF 1-JAM BEARISH] Tren makro 1H menurun (RSI 1H: {rsi_1h:.1f}) - Melarang posisi LONG spekulatif")

        # 7. STRICT ANTI-PUCUK (EMA20 Distance & Overextension Guard)
        ema_20 = float(indicators.get("ema_20", price))
        dist_to_ema20_pct = ((price - ema_20) / max(ema_20, 1e-6)) * 100.0
        _overext = float(getattr(config, "ema_dist_overextended_pct", 1.5))
        _ideal_lo = float(getattr(config, "ema_dist_ideal_low", -0.6))
        _ideal_hi = float(getattr(config, "ema_dist_ideal_high", 1.0))
        if dist_to_ema20_pct > _overext:
            score -= 25.0
            reasons.append(f"⛔ [ANTI-PUCUK] Harga terentang +{dist_to_ema20_pct:.2f}% di atas EMA20. Resiko mean-reversion pullback tinggi!")
        elif _ideal_lo <= dist_to_ema20_pct <= _ideal_hi:
            score += 10.0
            reasons.append(f"🎯 [PULLBACK IDEAL] Harga stabil dekat support EMA20 (+{dist_to_ema20_pct:.2f}%)")

        # 8. QUANT MATH + SELF-LEARNING MEMORY BIDIRECTIONAL SYNERGY
        mem = memory or snapshot.get("memory")
        memory_synergy = None
        profit_mult = 1.0
        sizing_mult = 1.0
        sl_multiplier = float(getattr(config, "quant_sl_multiplier", 1.5))

        if mem:
            if hasattr(mem, "evaluate_quant_synergy"):
                memory_synergy = mem.evaluate_quant_synergy(snapshot)
            elif hasattr(mem, "evaluate_setup_similarity_penalty"):
                memory_synergy = mem.evaluate_setup_similarity_penalty(snapshot)

        if memory_synergy:
            # 8A. Penalty for similarity to past failures (Low Loss protection)
            if memory_synergy.get("penalty_points", 0) > 0:
                pts = float(memory_synergy["penalty_points"])
                score -= pts
                reasons.append(f"🧠 [QUANT-MEMORY GUARD] {memory_synergy.get('penalty_reason', memory_synergy.get('reason'))}")
                sl_multiplier = float(memory_synergy.get("sl_tightening_factor", 1.1))
                sizing_mult = min(sizing_mult, float(memory_synergy.get("sizing_multiplier", 0.7)))

            # 8B. Reward for similarity to winning setups (High Profit catalyst)
            elif memory_synergy.get("alpha_boost", 0) > 0:
                boost = float(memory_synergy["alpha_boost"])
                score += boost
                profit_mult = float(memory_synergy.get("profit_multiplier", 1.25))
                sizing_mult = max(sizing_mult, float(memory_synergy.get("sizing_multiplier", 1.2)))
                reasons.append(f"🎯 [QUANT-MEMORY ALPHA BOOST] {memory_synergy.get('boost_reason')}")

            # 8C. Dynamic Pattern Win-Rate Conviction
            cluster_wr = memory_synergy.get("cluster_win_rate")
            if cluster_wr is not None and memory_synergy.get("cluster_sample_size", 0) >= 2:
                reasons.append(f"📊 [PROBABILITAS POLA] Win-Rate Historis Cluster: {cluster_wr}% ({memory_synergy['cluster_sample_size']} sampel)")

        score = max(5.0, min(95.0, score))

        # Precision calculation
        precision = 7 if price < 0.001 else (6 if price < 0.01 else (5 if price < 0.1 else (4 if price < 1.0 else 2)))

        # DYNAMIC BREATHING-ROOM STOP-LOSS:
        min_sl_dist = max(price * config.min_stop_loss_pct, sl_multiplier * atr)
        raw_atr_sl = price - min_sl_dist

        # Structural Swing Low Invalidation:
        key_levels = snapshot.get("key_levels", {}) if isinstance(snapshot.get("key_levels"), dict) else {}
        recent_low = float(key_levels.get("recent_swing_low", 0.0) or 0.0)

        if 0 < recent_low < price:
            structural_sl = recent_low * (1.0 - config.pullback_entry_pct) # cushion below swing low
            if structural_sl <= (price - min_sl_dist):
                calculated_sl = round(structural_sl, precision)
                reasons.append(f"Structural Support Stop Loss: ${calculated_sl} (di bawah Swing Low ${recent_low})")
            else:
                calculated_sl = round(raw_atr_sl, precision)
                reasons.append(f"Volatility Stop Loss: ${calculated_sl} (Buffer aman {min_sl_dist / max(price, 0.0001) * 100:.1f}%)")
        else:
            calculated_sl = round(raw_atr_sl, precision)
            reasons.append(f"Volatility Stop Loss: ${calculated_sl} (Buffer aman {min_sl_dist / max(price, 0.0001) * 100:.1f}%)")
        
        # ASYMMETRIC TAKE PROFIT (Real Market Structure: R:R >= 2.0x & Resistance Swing High)
        recent_high = float(key_levels.get("recent_swing_high", 0.0) or 0.0)
        min_risk_dist = max(float(getattr(config, "min_risk_dist_atr_mult", 0.8)) * atr, price * float(getattr(config, "min_risk_dist_pct", 0.005)), 0.0001)
        risk_distance = max(price - calculated_sl, min_risk_dist)
        safe_risk_distance = max(float(risk_distance or 0.0001), 0.0001)

        # P2: DYNAMIC R:R BASED ON ATR — scale the reward target with the ATR regime.
        # atr_ratio > 1 (volatility expanding) => demand a larger R:R (bigger swings on
        # offer); atr_ratio < 1 (calm) => a tighter R:R still clears cost. Clamped so a
        # single spike cannot demand an unreachable target. All knobs config-driven.
        _atr_ratio_val = float(indicators.get("atr_ratio", 1.0) or 1.0)
        _rr_base = float(getattr(config, "target_rr_mult", 2.0))
        dynamic_rr = round(_rr_base * max(0.8, min(1.6, _atr_ratio_val)), 2)
        quant_target_distance = max(risk_distance * dynamic_rr * profit_mult, float(getattr(config, "tp_atr_mult", 1.8)) * atr * profit_mult)
        base_tp = price + quant_target_distance

        if recent_high > price and (recent_high - price) >= (1.5 * risk_distance):
            calculated_tp = round(recent_high, precision)
            actual_rr = round((recent_high - price) / safe_risk_distance, 2)
            reasons.append(f"Asymmetric Take Profit: ${calculated_tp} (Target Swing High Resistance, R:R {actual_rr}x)")
        else:
            calculated_tp = round(base_tp, precision)
            actual_rr = round(quant_target_distance / safe_risk_distance, 2)
            reasons.append(f"Asymmetric Take Profit: ${calculated_tp} (Target R:R {actual_rr}x, Multiplier {profit_mult}x)")

        # Grid count recommendation based on volatility
        if volatility_pct > float(getattr(config, "vol_high_pct", 3.5)):
            rec_grids = int(getattr(config, "grid_count_high_vol", 6))
        elif volatility_pct < float(getattr(config, "vol_low_pct", 1.5)):
            rec_grids = int(getattr(config, "grid_count_low_vol", 10))
        else:
            rec_grids = int(getattr(config, "grid_count_mid_vol", 8))

        # Quant Action Decision:
        action = "LONG" if score >= float(getattr(config, "quant_long_cutoff", 62.0)) else ("SHORT" if score <= float(getattr(config, "quant_short_cutoff", 35.0)) else "HOLD")
        # Enforce Anti-Pucuk and MTF Guardrail on LONG decisions
        if action == "LONG":
            if dist_to_ema20_pct > float(getattr(config, "ema_dist_overextended_pct", 1.5)):
                action = "HOLD"
                reasons.append(f"🛡️ [GUARDRAIL ANTI-PUCUK] Aksi LONG diubah menjadi HOLD karena harga berada +{dist_to_ema20_pct:.2f}% di pucuk.")
            elif isinstance(mtf_1h, dict) and mtf_1h.get("trend") == "BEARISH":
                action = "HOLD"
                reasons.append("🛡️ [GUARDRAIL MTF] Aksi LONG dibatalkan karena tren makro 1-Jam BEARISH.")
            elif bool(getattr(config, "hard_reject_consolidation", True)):
                _tb_u = str(trend).upper()
                if _tb_u == "NEUTRAL" or "NEUTRAL" in _tb_u or "CONSOLIDATION" in _tb_u:
                    action = "HOLD"
                    reasons.append(f"🛡️ [GUARDRAIL KONSOLIDASI] Aksi LONG diubah menjadi HOLD karena pasar sideways/konsolidasi (trend: {trend}).")

        return QuantEvaluation(
            score=round(score, 1),
            trend_bias=trend,
            action=action,
            rsi=round(rsi, 1),
            is_overbought=is_overbought,
            is_oversold=is_oversold,
            volatility_pct=volatility_pct,
            recommended_stop_loss=calculated_sl,
            recommended_take_profit=calculated_tp,
            target_rr_ratio=actual_rr,
            recommended_grid_count=rec_grids,
            profit_multiplier=profit_mult,
            sizing_multiplier=sizing_mult,
            reasons=reasons
        )


class HybridConsensusEngine:
    """
    Arbiter coordinating 9router AI Engine and Mathematical Quant Engine.
    Ensures:
    1. Zero single point of failure (Failover to Quant if AI router is down).
    2. Zero hallucination risk (Quant guardrail intercepts unsafe AI calls).
    3. Maximum capital protection (Defensive mode when engines diverge).
    """

    def __init__(self, ai_analyzer=None, memory=None):
        self.ai_analyzer = ai_analyzer
        self.memory = memory

    def arbitrate(self, snapshot: Dict[str, Any], custom_stop_loss: Optional[float] = None) -> ConsensusDecision:
        # Step 1: Calculate Mathematical Quant Evaluation (with Self-Learning feedback)
        quant = QuantMathEngine.evaluate(snapshot, memory=self.memory)
        price = float(snapshot.get("current_price", 0.0))

        # Step 2: Query AI Engine with full Triad Context (Quant Math + Self-Learning Memory + News)
        ai_signal = None
        ai_error = None
        model_name = getattr(self.ai_analyzer, "model_name", "AI Router") if self.ai_analyzer else "AI Router"

        if self.ai_analyzer:
            try:
                ai_snap = dict(snapshot)
                ai_snap["quant_math_evaluation"] = {
                    "score": quant.score,
                    "trend_bias": quant.trend_bias,
                    "action": quant.action,
                    "rsi": quant.rsi,
                    "volatility_pct": quant.volatility_pct,
                    "recommended_stop_loss": quant.recommended_stop_loss,
                    "recommended_grid_count": quant.recommended_grid_count,
                    "profit_multiplier": quant.profit_multiplier,
                    "sizing_multiplier": quant.sizing_multiplier,
                    "reasons": quant.reasons
                }
                if self.memory and hasattr(self.memory, "get_recent_lessons_for_ai_prompt"):
                    ai_snap["self_learning_lessons"] = self.memory.get_recent_lessons_for_ai_prompt()
                if self.memory and hasattr(self.memory, "evaluate_quant_synergy"):
                    ai_snap["memory_synergy"] = self.memory.evaluate_quant_synergy(snapshot)

                ai_signal = self.ai_analyzer.analyze_market(ai_snap)
            except Exception as e:
                ai_error = str(e)
                logger.warning(f"AI Analyzer exception in hybrid consensus: {e}")

        # Step 3: Determine Consensus Matrix
        # Determine Stop Loss harmonization:
        final_sl = quant.recommended_stop_loss
        if custom_stop_loss and custom_stop_loss > 0:
            final_sl = float(custom_stop_loss)
        elif ai_signal and ai_signal.stop_loss and 0 < ai_signal.stop_loss < price:
            final_sl = max(quant.recommended_stop_loss, float(ai_signal.stop_loss))

        # Determine Take Profit harmonization:
        final_tp = quant.recommended_take_profit
        if ai_signal and ai_signal.take_profit_1 and ai_signal.take_profit_1 > price:
            final_tp = max(quant.recommended_take_profit, float(ai_signal.take_profit_1))
        
        risk_dist = max(price - final_sl, 0.0001)
        # AUDIT FIX: report RR NET of round-trip cost, matching what the entry gate compares against
        # min_risk_reward_ratio. A gross RR just above the minimum is a NEGATIVE-expectancy trade
        # once fees+slippage are paid; the AI prompt already demands net RR, so the number the gate
        # enforces must be net too. Prefer the LIVE taker fee injected into the snapshot.
        _lf = None
        try:
            _lf = (snapshot.get("fees") or {}).get("taker_fee_rate")
            _lf = float(_lf) if _lf is not None else None
        except Exception:
            _lf = None
        _taker = _lf if (_lf and _lf > 0) else float(getattr(config, "taker_fee_rate", 0.0004))
        _rt_cost = 2.0 * _taker + float(getattr(config, "expected_slippage_pct", 0.0005))
        _gross_rr = (final_tp - price) / risk_dist if final_tp > price else quant.target_rr_ratio
        effective_rr = round(max(_gross_rr - (_rt_cost * price) / risk_dist, 0.0), 2)

        # --- CASE A: AI OFFLINE / TIMEOUT / FAILOVER ---
        if not ai_signal or ai_error:
            expl = f"[FAILOVER AKTIF] AI Router tidak merespon ({ai_error or 'Offline'}). Quant Math Engine mengambil alih kendali penuh 100% secara presisi (Skor: {quant.score}% | {quant.trend_bias})."
            return ConsensusDecision(
                action=quant.action,
                status="QUANT_FAILOVER",
                confidence_pct=int(quant.score),
                stop_loss=final_sl,
                take_profit=final_tp,
                risk_reward_ratio=effective_rr,
                grid_count=quant.recommended_grid_count,
                profit_multiplier=quant.profit_multiplier,
                sizing_multiplier=quant.sizing_multiplier,
                explanation=expl,
                quant_score=quant.score,
                ai_confidence=0.0,
                ai_model_used="Quant Math Engine (Failover)",
                is_failover_active=True
            )

        ai_action = ai_signal.action.upper()
        _ai_conf = ai_signal.confidence
        ai_conf = _ai_conf if (_ai_conf is not None and 0.0 <= _ai_conf <= 1.0) else float(getattr(config, "fallback_ai_confidence", 0.78))
        ai_conf_pct = int(ai_conf * 100)
        ai_reason = ai_signal.primary_reason or "Analisis pola teknikal AI"

        # --- CASE B0: FOREX FACTORY RED NEWS GUARD (Macro Volatility Protection) ---
        news_guard = snapshot.get("news_guard", {}) if isinstance(snapshot.get("news_guard"), dict) else {}
        if news_guard.get("is_danger"):
            expl = f"[FOREX FACTORY RED NEWS GUARD] {news_guard.get('caution_reason')}. Sistem otomatis membatasi exposure ke DEFENSIVE GRID ketat untuk mencegah guncangan berita macro."
            defensive_sl = round(price * (1.0 - config.min_stop_loss_pct), 4) if final_sl <= 0 else max(final_sl, round(price * (1.0 - config.min_stop_loss_pct), 4))
            return ConsensusDecision(
                action="LONG",
                status="DEFENSIVE_GRID",
                confidence_pct=min(55, int((quant.score + ai_conf_pct) / 2)),
                stop_loss=defensive_sl,
                take_profit=final_tp,
                risk_reward_ratio=effective_rr,
                grid_count=6,
                profit_multiplier=1.0,
                sizing_multiplier=0.6,
                explanation=expl,
                quant_score=quant.score,
                ai_confidence=ai_conf,
                ai_model_used=model_name,
                is_failover_active=False
            )

        # --- CASE B: QUANT GUARDRAIL INTERCEPT (AI Hallucination & Anti-Pucuk Protection) ---
        if ai_action == "LONG" and (quant.action != "LONG" or quant.is_overbought or quant.trend_bias == "BEARISH" or quant.score < 50.0):
            guardrail_reasons = []
            if quant.action != "LONG":
                guardrail_reasons.append("Quant Action menolak LONG (Proteksi Anti-Pucuk / MTF)")
            if quant.is_overbought:
                guardrail_reasons.append(f"RSI Overbought ekstrim ({quant.rsi})")
            if quant.trend_bias == "BEARISH":
                guardrail_reasons.append("Tren EMA Bearish")
            if quant.score < 50.0:
                guardrail_reasons.append(f"Skor Quant Rendah ({quant.score}%)")

            reason_str = ", ".join(guardrail_reasons)
            expl = f"[GUARDRAIL AKTIF] AI merekomendasikan LONG ({ai_conf_pct}%), tetapi Quant Math Engine mendeteksi resiko tinggi: {reason_str}. Sistem menolak entry agresif demi melindungi modal."

            defensive_sl = round(price * (1.0 - config.min_stop_loss_pct), 4) if final_sl <= 0 else max(final_sl, round(price * (1.0 - config.min_stop_loss_pct), 4))
            return ConsensusDecision(
                action="HOLD",
                status="DEFENSIVE_GRID",
                confidence_pct=int((quant.score + ai_conf_pct) / 2),
                stop_loss=defensive_sl,
                take_profit=final_tp,
                risk_reward_ratio=effective_rr,
                grid_count=6,
                profit_multiplier=1.0,
                sizing_multiplier=0.0,
                explanation=expl,
                quant_score=quant.score,
                ai_confidence=ai_conf,
                ai_model_used=model_name,
                is_failover_active=False
            )

        # --- CASE B1: SIDEWAYS / CONSOLIDATION GUARDRAIL (P0.1) ---
        _tb_upper = str(quant.trend_bias).upper()
        _is_consol = _tb_upper == "NEUTRAL" or "NEUTRAL" in _tb_upper or "CONSOLIDATION" in _tb_upper
        if bool(getattr(config, "hard_reject_consolidation", True)) and _is_consol:
            expl = f"[KONSOLIDASI] Pasar sedang dalam fase sideways/konsolidasi (Quant: {quant.score}% | Trend: {quant.trend_bias} | AI: {ai_action} {ai_conf_pct}%). Menolak entry buy limit untuk melindungi modal."
            return ConsensusDecision(
                action="HOLD",
                status="CONSOLIDATION",
                confidence_pct=int((quant.score + ai_conf_pct) / 2),
                stop_loss=final_sl,
                take_profit=final_tp,
                risk_reward_ratio=effective_rr,
                grid_count=quant.recommended_grid_count,
                profit_multiplier=quant.profit_multiplier,
                sizing_multiplier=0.0,
                explanation=expl,
                quant_score=quant.score,
                ai_confidence=ai_conf,
                ai_model_used=model_name,
                is_failover_active=False
            )

        # --- CASE C: AI CAUTION GUARDRAIL (AI Protects Quant) ---
        # A veto is only as trustworthy as the AI's conviction: require HOLD/SHORT to also clear
        # ai_caution_min_confidence. 0.0 = legacy (any HOLD/SHORT vetoes a quant-bullish signal);
        # raising it stops a low-conviction AI from killing every edge-aligned candidate.
        _caution_floor = float(getattr(config, "ai_caution_min_confidence", 0.2) or 0.0)
        _quant_bull = float(getattr(config, "min_quant_score", 58.0))
        if quant.score >= _quant_bull and ai_action in ["HOLD", "SHORT"] and ai_conf >= _caution_floor:
            expl = f"[AI CAUTION] Indikator Quant bernilai positif (Skor: {quant.score}%), namun AI Router mendeteksi pelemahan/resistensi kuat: '{ai_reason}'. Disiplin sistem memilih HOLD untuk menghindari resiko pembalikan arah."
            return ConsensusDecision(
                action="HOLD",
                status="AI_CAUTION_OVERRIDE",
                confidence_pct=int((quant.score + ai_conf_pct) / 2),
                stop_loss=final_sl,
                take_profit=final_tp,
                risk_reward_ratio=effective_rr,
                grid_count=quant.recommended_grid_count,
                profit_multiplier=1.0,
                sizing_multiplier=0.0,
                explanation=expl,
                quant_score=quant.score,
                ai_confidence=ai_conf,
                ai_model_used=model_name,
                is_failover_active=False
            )

        # --- CASE D: FULL MUTUAL CONSENSUS (TRIAD AI + QUANT + MEMORY) ---
        if ai_action == "LONG" and quant.score >= _quant_bull:
            _qw = float(getattr(config, "quant_weight", 0.70))
            _aw = float(getattr(config, "ai_weight", 0.30))
            _tot = _qw + _aw
            combined_conf = int((quant.score * (_qw / _tot)) + (ai_conf_pct * (_aw / _tot)))
            expl = f"[KONSENSUS PENUH TRIAD] Sinergi Sempurna! AI Engine ({ai_conf_pct}%), Quant Math Engine (Skor: {quant.score}%), dan Self-Learning Memory sepakat sinyal BULLISH LONG (R:R {effective_rr}x | TP: ${final_tp}). Alasan AI: {ai_reason}."
            return ConsensusDecision(
                action="LONG",
                status="FULL_CONSENSUS",
                confidence_pct=combined_conf,
                stop_loss=final_sl,
                take_profit=final_tp,
                risk_reward_ratio=effective_rr,
                grid_count=quant.recommended_grid_count,
                profit_multiplier=quant.profit_multiplier,
                sizing_multiplier=quant.sizing_multiplier,
                explanation=expl,
                quant_score=quant.score,
                ai_confidence=ai_conf,
                ai_model_used=model_name,
                is_failover_active=False
            )

        # --- CASE D2: QUANT-LONG with a LOW-conviction AI HOLD (BUG 4 FIX) ---
        # Previously any AI HOLD (even 20% conviction) dropped straight to CASE E
        # (CONSOLIDATION-HOLD), so a strong quant LONG could never enter unless the
        # AI also shouted LONG. The AI-caution veto (CASE C) already gates a genuine
        # AI warning behind ai_caution_min_confidence; reaching here means the AI
        # HOLD is BELOW that floor — a non-vote, not a veto. Let the quant LONG
        # through at reduced sizing (config.quant_override_sizing_mult, default 0.5)
        # so a weak AI shrug cannot veto a strong measured signal.
        if quant.action == "LONG" and quant.score >= _quant_bull and ai_conf < _caution_floor:
            _ov_mult = float(getattr(config, "quant_override_sizing_mult", 0.5) or 0.5)
            expl = (f"[QUANT-LONG | AI NETRAL] Skor Quant kuat ({quant.score}%) dengan tren {quant.trend_bias}; "
                    f"AI tidak memveto (confidence {ai_conf_pct}% < ambang {int(_caution_floor*100)}%). "
                    f"Entry diizinkan dengan sizing dikecilkan {int(_ov_mult*100)}% karena AI netral. "
                    f"Alasan AI: {ai_reason}.")
            return ConsensusDecision(
                action="LONG",
                status="QUANT_LONG_AI_NEUTRAL",
                confidence_pct=int((quant.score + ai_conf_pct) / 2),
                stop_loss=final_sl,
                take_profit=final_tp,
                risk_reward_ratio=effective_rr,
                grid_count=quant.recommended_grid_count,
                profit_multiplier=quant.profit_multiplier,
                sizing_multiplier=_ov_mult,
                explanation=expl,
                quant_score=quant.score,
                ai_confidence=ai_conf,
                ai_model_used=model_name,
                is_failover_active=False
            )

        # --- CASE E: CONSOLIDATION / NEUTRAL ---
        expl = f"[KONSOLIDASI] Pasar sedang dalam fase sideways/konsolidasi (Quant: {quant.score}% | AI: {ai_action} {ai_conf_pct}%). Menahan entry baru untuk melindungi modal tersisa."
        return ConsensusDecision(
            action="HOLD",
            status="CONSOLIDATION",
            confidence_pct=int((quant.score + ai_conf_pct) / 2),
            stop_loss=final_sl,
            take_profit=final_tp,
            risk_reward_ratio=effective_rr,
            grid_count=quant.recommended_grid_count,
            profit_multiplier=quant.profit_multiplier,
            sizing_multiplier=0.0,
            explanation=expl,
            quant_score=quant.score,
            ai_confidence=ai_conf,
            ai_model_used=model_name,
            is_failover_active=False
        )

