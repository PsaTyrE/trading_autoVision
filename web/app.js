// State & Config
let currentMode = "live";
let currentQuoteAsset = "USDT";
let isRunning = false;
let ws = null;
let currentSymbol = "";

// Elements Cache
const botStatusPill = document.getElementById("bot-status-pill");
const botStatusText = document.getElementById("bot-status-text");
const headerWalletAvailable = document.getElementById("header-wallet-available");
const headerWalletTotal = document.getElementById("header-wallet-total");
const cardMarginUsed = document.getElementById("card-margin-used");

const cardModeBadge = document.getElementById("card-mode-badge");
const cardSymbol = document.getElementById("card-symbol");
const cardCurrentPrice = document.getElementById("card-current-price");
const cardProfit = document.getElementById("card-profit");
const cardMatchedTrades = document.getElementById("card-matched-trades");
const cardUnrealizedPnl = document.getElementById("card-unrealized-pnl");
const cardPositionContracts = document.getElementById("card-position-contracts");
const cardOpenOrdersCount = document.getElementById("card-open-orders-count");
const cardGridStep = document.getElementById("card-grid-step");

const inputSymbol = document.getElementById("input-symbol");
const inputInvestment = document.getElementById("input-investment");
const inputGrids = document.getElementById("input-grids");
const inputStopLoss = document.getElementById("input-stoploss");
const checkAutoPick = document.getElementById("check-autopick");
const btnStartBot = document.getElementById("btn-start-bot");
const btnStopBot = document.getElementById("btn-stop-bot");
const botActionHint = document.getElementById("bot-action-hint");

const ladderRangeLabel = document.getElementById("ladder-range-label");
const gridLadderDisplay = document.getElementById("grid-ladder-display");
const scannerTableBody = document.getElementById("scanner-table-body");
const logTerminal = document.getElementById("log-terminal");

let lastBotState = null;
let selectedLadderCoin = null;

window.switchLadderTab = function(sym) {
  selectedLadderCoin = sym;
  if (lastBotState) {
    renderLadder(lastBotState);
  }
};

// Quote Asset Switching (USDT vs USDC)
async function setQuoteAsset(asset, triggerRefresh = true) {
  currentQuoteAsset = (asset || "USDT").toUpperCase();
  const btnUsdt = document.getElementById("btn-quote-usdt");
  const btnUsdc = document.getElementById("btn-quote-usdc");
  if (btnUsdt) btnUsdt.classList.toggle("active", currentQuoteAsset === "USDT");
  if (btnUsdc) btnUsdc.classList.toggle("active", currentQuoteAsset === "USDC");

  const headerQuote = document.getElementById("header-wallet-quote");
  if (headerQuote) headerQuote.textContent = currentQuoteAsset;

  const labelInv = document.getElementById("label-investment");
  if (labelInv) labelInv.textContent = `Total Margin (${currentQuoteAsset})`;

  // If Coin Drawer market category is currently USDT or USDC, sync it too
  if (typeof currentMarketCategory !== "undefined" && (currentMarketCategory === "USDT" || currentMarketCategory === "USDC")) {
    currentMarketCategory = currentQuoteAsset;
    document.querySelectorAll("#market-category-pills button").forEach(btn => {
      if (btn.dataset.cat === currentQuoteAsset) {
        btn.style.background = "rgba(6,182,212,0.2)";
        btn.style.borderColor = "var(--accent-cyan)";
        btn.style.color = "var(--accent-cyan)";
      } else if (btn.dataset.cat === "USDT" || btn.dataset.cat === "USDC") {
        btn.style.background = "";
        btn.style.borderColor = "";
        btn.style.color = "";
      }
    });
  }

  if (triggerRefresh) {
    appendLog(`Margin Asset dialihkan ke ${currentQuoteAsset}... Mengambil saldo & koin Binance...`, "info");
    try {
      const res = await fetch("/api/quote_asset/switch", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ quote_asset: currentQuoteAsset })
      });
      const data = await res.json();
      if (data.success && data.state) {
        updateUIState(data.state);
        const wb = (data.state.wallet_balance !== undefined) ? data.state.wallet_balance.toFixed(2) : "0.00";
        const ab = (data.state.available_balance !== undefined) ? data.state.available_balance.toFixed(2) : "0.00";
        appendLog(`🟢 Margin Asset disinkronkan ke ${currentQuoteAsset}! Saldo: $${wb} ${currentQuoteAsset} (Tersedia: $${ab})`, "success");
      }
    } catch (e) {
      console.warn("Error switching quote asset:", e);
      appendLog(`❌ Gagal mengubah Margin Asset: ${e.message}`, "error");
    }
    refreshScanner(true);
    fetchLiveStatus();
  }
}

// Mode Switching
async function setMode(mode, force = false) {
  if (!force && currentMode === mode && (!lastBotState || lastBotState.mode === mode) && isRunning) {
    appendLog(`○ Bot sedang aktif berjalan di mode ${mode.toUpperCase()}. Klik STOP BOT jika ingin mengganti mode.`, "info");
    return;
  }

  // If bot is currently running, gracefully stop previous mode bot first
  if (isRunning) {
    appendLog(`Menghentikan bot ${currentMode.toUpperCase()} untuk berpindah ke mode ${mode.toUpperCase()}...`, "warning");
    try {
      await fetch("/api/bot/stop", { method: "POST" });
    } catch (e) {
      console.warn("Error stopping bot on mode switch:", e);
    }
    isRunning = false;
    if (btnStartBot) {
      btnStartBot.innerHTML = `<span>🚀</span><span>START BOT</span>`;
      btnStartBot.disabled = false;
    }
    if (btnStopBot) btnStopBot.disabled = true;
    botStatusPill.className = "status-pill stopped";
    botStatusText.textContent = "OFFLINE";
    if (botActionHint) {
      botActionHint.textContent = `○ Bot standby di mode ${mode.toUpperCase()} - Klik START BOT untuk mulai`;
      botActionHint.style.color = "var(--text-dim)";
    }
  }

  currentMode = mode;
  document.querySelectorAll(".mode-btn[data-mode]").forEach(btn => {
    btn.classList.toggle("active", btn.dataset.mode === mode);
  });
  if (cardModeBadge) {
    cardModeBadge.textContent = mode.toUpperCase();
    cardModeBadge.className = `ladder-badge ${mode === 'demo' ? 'badge-buy' : (mode === 'live' ? 'badge-sell' : 'badge-sim')}`;
  }
  appendLog(`Trading environment beralih ke ${mode.toUpperCase()}! Menghubungkan ke server Binance...`, "info");

  try {
    const res = await fetch("/api/mode/switch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode: mode })
    });
    const data = await res.json();
    if (data.success && data.state) {
      updateUIState(data.state);
      const q = data.state.quote_asset || currentQuoteAsset || "USDT";
      const wb = (data.state.wallet_balance !== undefined) ? data.state.wallet_balance.toFixed(2) : "0.00";
      const ab = (data.state.available_balance !== undefined) ? data.state.available_balance.toFixed(2) : "0.00";
      if (mode === "simulation") {
        appendLog(`🟢 Mode SIMULASI aktif! Saldo virtual: $${wb} ${q}`, "success");
      } else {
        appendLog(`🟢 Berhasil terhubung ke Binance ${mode.toUpperCase()}! Saldo: $${wb} ${q} (Tersedia: $${ab})`, "success");
      }
    } else {
      appendLog(`⚠️ Gagal beralih mode: ${data.error || 'Respon server tidak valid'}`, "warning");
    }
  } catch (e) {
    console.warn("Error switching mode:", e);
    appendLog(`❌ Gagal terhubung ke Binance saat beralih ke ${mode.toUpperCase()}: ${e.message}`, "error");
  }

  // Refresh trading data & positions for selected mode
  refreshTradingData();
  fetchLiveStatus();
}

let pendingSwitchSymbol = null;
let hubFilter = "ALL";

function onHubFilterChange(val) {
  hubFilter = val;
  refreshTradingData();
}

async function selectSymbol(sym, autoRun = false) {
  if (!sym) return;
  inputSymbol.value = sym;
  currentSymbol = sym;
  checkAutoPick.checked = false;

  // Highlight row in scanner table if exists
  const cleanKey = sym.replace(/[^a-zA-Z0-9]/g, '');
  document.querySelectorAll(".bulls-table tr").forEach(tr => tr.classList.remove("selected-row"));
  const row = document.getElementById(`scanner-row-${cleanKey}`);
  if (row) row.classList.add("selected-row");
  
  document.querySelectorAll(".btn-pick").forEach(btn => btn.classList.remove("active"));
  const btn = document.getElementById(`btn-pick-${cleanKey}`);
  if (btn) btn.classList.add("active");

  // Immediately update header card with selected coin
  cardSymbol.textContent = sym;
  appendLog(`Target koin dipilih: ${sym}. Mengambil data live Binance...`, "info");

  // Fetch real-time live ticker and calculate preview grid ladder from Binance!
  try {
    const inv = (inputInvestment && parseFloat(inputInvestment.value) > 0) ? parseFloat(inputInvestment.value) : (lastBotState && lastBotState.available_balance > 0 ? parseFloat((lastBotState.available_balance * 0.85).toFixed(2)) : 50.0);
    const grd = parseInt(inputGrids.value) || 8;
    const res = await fetch(`/api/ticker?symbol=${encodeURIComponent(sym)}&mode=${currentMode}&investment=${inv}&grid_count=${grd}`);
    const data = await res.json();
    if (data.success) {
      cardCurrentPrice.textContent = `Price: $${data.price.toFixed(5)} (${data.change_24h >= 0 ? '+' : ''}${data.change_24h}%)`;
      if (!isRunning) {
        renderLadder({
          symbol: sym,
          current_price: data.price,
          grid_summary: data.grid_summary,
          active_grids: data.active_grids
        });
      }
    }
  } catch (e) {
    console.warn("Ticker preview error:", e);
  }

  // If Bot is currently RUNNING:
  if (isRunning) {
    if (autoRun) {
      appendLog(`Mengalihkan bot ke ${sym}... Membatalkan order lama & deploy grid baru.`, "warning");
      await handleStartBot();
      refreshTradingData();
      return;
    } else {
      pendingSwitchSymbol = sym;
      const modal = document.getElementById("coin-switch-modal");
      const desc = document.getElementById("switch-modal-desc");
      if (modal && desc) {
        desc.textContent = `Bot saat ini sedang aktif di Binance pada ${cardSymbol.textContent || 'koin sebelumnya'}. Apakah Anda ingin langsung membatalkan order lama dan menyebarkan grid baru di ${sym}?`;
        modal.style.display = "flex";
      }
      return;
    }
  }

  // Dynamically update margin currency label based on quote (USDT or USDC)
  const quote = sym.includes('/') ? sym.split('/')[1] : (sym.endsWith('USDC') ? 'USDC' : 'USDT');
  const labelInv = document.getElementById("label-investment");
  if (labelInv) labelInv.textContent = `Total Margin (${quote})`;

  // If Bot is STOPPED:
  const baseOnly = sym.includes('/') ? sym.split('/')[0] : sym.replace('/USDT', '').replace('USDT', '').replace('/USDC', '').replace('USDC', '');
  if (autoRun) {
    appendLog(`Meluncurkan bot pada ${sym}...`, "info");
    await handleStartBot();
  } else {
    appendLog(`🎯 Target koin dipilih: ${sym}. Klik START BOT untuk meluncurkan grid di Binance.`, "info");
    if (btnStartBot) {
      btnStartBot.innerHTML = `<span>🚀</span><span>START ON ${baseOnly}</span>`;
      btnStartBot.style.boxShadow = "0 0 25px var(--accent-green-glow)";
    }
  }
  refreshTradingData();
}

function closeSwitchModal() {
  const modal = document.getElementById("coin-switch-modal");
  if (modal) modal.style.display = "none";
  pendingSwitchSymbol = null;
}

async function confirmSwitchCoin() {
  closeSwitchModal();
  if (pendingSwitchSymbol) {
    inputSymbol.value = pendingSwitchSymbol;
    currentSymbol = pendingSwitchSymbol;
  }
  appendLog(`Mengganti koin trading ke ${currentSymbol}...`, "info");
  await handleStartBot();
  refreshTradingData();
}

function toggleAutoPick(checked) {
  if (checked) {
    appendLog("Auto-Pick #1 Bullish Coin enabled. Scanner will select coin upon bot launch.", "info");
  }
}

// WebSocket Setup
function initWebSocket() {
  try {
    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    let host = window.location.host;
    if (window.location.hostname === "0.0.0.0" || !window.location.hostname) {
      const port = window.location.port ? `:${window.location.port}` : ":8000";
      host = `127.0.0.1${port}`;
    }
    const wsUrl = `${protocol}//${host}/ws`;

    ws = new WebSocket(wsUrl);

    ws.onopen = () => {
      console.log("[WS] Connected to Bot WebSocket");
    };

    ws.onmessage = (event) => {
      try {
        const msg = JSON.parse(event.data);
        if (msg.event === "INITIAL_STATE") {
          updateUIState(msg.data);
          if (Array.isArray(msg.data.logs) && logTerminal && logTerminal.children.length === 0) {
            msg.data.logs.forEach(l => renderLogEntry(l));
          }
        } else if (msg.event === "STATE_UPDATE") {
          updateUIState(msg.data);
        } else if (msg.event === "LOG") {
          renderLogEntry(msg.data);
        }
      } catch (e) {
        console.error("[WS] Error parsing message:", e);
      }
    };

    ws.onclose = () => {
      setTimeout(initWebSocket, 4000);
    };

    ws.onerror = (err) => {
      console.warn("[WS] Error (fallback to HTTP polling active):", err);
    };
  } catch (err) {
    console.warn("[WS] WebSocket initialization failed, using HTTP polling:", err);
  }
}

// UI State Updates
function updateUIState(state) {
  if (!state) return;

  isRunning = state.is_running;
  currentSymbol = state.symbol || currentSymbol;

  // Header & Status Pill
  if (isRunning) {
    botStatusPill.className = "status-pill running";
    botStatusText.textContent = "RUNNING";
    if (btnStartBot) btnStartBot.disabled = true;
    if (btnStopBot) btnStopBot.disabled = false;
    if (botActionHint) {
      botActionHint.textContent = "● Bot sedang aktif berjalan (Live di Binance)";
      botActionHint.style.color = "var(--accent-green)";
    }
    const badge = document.getElementById("control-active-badge");
    if (badge) {
      badge.textContent = "ACTIVE";
      badge.style.color = "var(--accent-green)";
    }
  } else {
    botStatusPill.className = "status-pill stopped";
    botStatusText.textContent = "OFFLINE";
    if (btnStartBot) btnStartBot.disabled = false;
    if (btnStopBot) btnStopBot.disabled = true;
    if (botActionHint) {
      botActionHint.textContent = "○ Bot sedang berhenti (Standby) - Klik START BOT untuk mulai";
      botActionHint.style.color = "var(--text-dim)";
    }
    const badge = document.getElementById("control-active-badge");
    if (badge) {
      badge.textContent = "STANDBY";
      badge.style.color = "var(--text-dim)";
    }
  }

  // Sync Mode Badge & Toggle Buttons with active state
  if (state.mode) {
    currentMode = state.mode;
    document.querySelectorAll(".mode-btn[data-mode]").forEach(btn => {
      btn.classList.toggle("active", btn.dataset.mode === state.mode);
    });
    if (cardModeBadge) {
      cardModeBadge.textContent = state.mode.toUpperCase();
      cardModeBadge.className = `ladder-badge ${state.mode === 'demo' ? 'badge-buy' : (state.mode === 'live' ? 'badge-sell' : 'badge-sim')}`;
    }
  }

  // Sync Quote Asset (USDT / USDC) only when bot is actively running
  if (isRunning && state.quote_asset && state.quote_asset !== currentQuoteAsset) {
    setQuoteAsset(state.quote_asset, false);
  } else if (!isRunning) {
    // When bot is stopped, respect user's active UI selection
    const btnUsdt = document.getElementById("btn-quote-usdt");
    const btnUsdc = document.getElementById("btn-quote-usdc");
    if (btnUsdt) btnUsdt.classList.toggle("active", currentQuoteAsset === "USDT");
    if (btnUsdc) btnUsdc.classList.toggle("active", currentQuoteAsset === "USDC");
    const headerQuote = document.getElementById("header-wallet-quote");
    if (headerQuote) headerQuote.textContent = currentQuoteAsset;
    const labelInv = document.getElementById("label-investment");
    if (labelInv) labelInv.textContent = `Total Margin (${currentQuoteAsset})`;
  }

  const quoteCurr = state.currency || state.quote_asset || currentQuoteAsset || "USDT";

  // Balances & Prices
  const totalBal = state.wallet_balance !== undefined ? state.wallet_balance : 0.0;
  const availBal = state.available_balance !== undefined ? state.available_balance : totalBal;
  const marginUsed = Math.max(0.0, totalBal - availBal);

  if (headerWalletAvailable) {
    headerWalletAvailable.textContent = `$${availBal.toFixed(2)}`;
  }
  if (headerWalletTotal) {
    headerWalletTotal.textContent = `$${totalBal.toFixed(2)}`;
  }
  const _cfFm = document.getElementById("cf-free-margin");
  if (_cfFm) _cfFm.textContent = `$${availBal.toFixed(2)}`;
  if (cardMarginUsed) {
    cardMarginUsed.textContent = `Margin: $${marginUsed.toFixed(2)}`;
  }
  
  lastBotState = state;

  // Dynamic wallet margin sync for user input: strictly follow actual Binance wallet!
  if (!isRunning && inputInvestment && document.activeElement !== inputInvestment) {
    if (availBal > 0) {
      inputInvestment.value = (availBal * 0.85).toFixed(2);
    } else {
      inputInvestment.value = "0.00";
    }
  }

  const _cfInstr = document.getElementById("cf-instrument");
  if (state.basket && state.basket.length > 1) {
    cardSymbol.innerHTML = state.basket.map(c => 
      `<span style="font-size: 0.8rem; background: rgba(6,182,212,0.18); border: 1px solid rgba(6,182,212,0.4); padding: 2px 7px; border-radius: 4px; margin-right: 4px; color: var(--accent-cyan);">${c.symbol.replace('/USDT','').replace('/USDC','')}</span>`
    ).join('');
    if (_cfInstr) _cfInstr.textContent = state.basket.map(c => c.symbol.replace('/USDT','').replace('/USDC','')).join(' · ');
  } else {
    cardSymbol.textContent = state.symbol || `-- / ${quoteCurr}`;
    if (_cfInstr) _cfInstr.textContent = (state.symbol || "--").replace('/USDT','').replace('/USDC','');
  }
  cardCurrentPrice.textContent = `Price: $${state.current_price?.toFixed(5) || "0.00000"}`;
  const _cfAi = document.getElementById("cf-ai-signal");
  if (_cfAi && state.basket && state.basket[0]) {
    const _b = state.basket[0];
    _cfAi.textContent = `${_b.consensus_status || "STANDBY"} · Q${_b.quant_score || 0}% · AI${Math.round((_b.ai_confidence || 0) * 100)}%`;
  }

  // Konfluensi Multi-Timeframe: 1H macro + primary-timeframe trend/RSI + AI/Quant consensus.
  // 1D is intentionally NOT fetched (no new network call in the live path) — the 1H row is the
  // macro view; data comes from state.basket (per-coin mtf_1h / mtf_primary). None -> '--'.
  const _mtfTrendClass = (t) => {
    const u = String(t || "").toUpperCase();
    if (u === "BULLISH") return "trend-bullish";
    if (u === "BEARISH") return "trend-bearish";
    return "trend-neutral";
  };
  const _mtfSet = (trendEl, rsiEl, obj, fallbackRsi) => {
    if (!trendEl) return;
    if (!obj) { trendEl.textContent = "--"; trendEl.className = "trend-neutral"; }
    else {
      trendEl.textContent = obj.trend || "NEUTRAL";
      trendEl.className = _mtfTrendClass(obj.trend);
    }
    if (rsiEl) {
      const r = obj && obj.rsi !== undefined && obj.rsi !== null ? obj.rsi : fallbackRsi;
      rsiEl.textContent = `RSI ${r !== undefined && r !== null ? Number(r).toFixed(2) : "--"}`;
    }
  };
  const _mtfCoin = (state.basket && state.basket[0]) ? state.basket[0] : null;
  _mtfSet(document.getElementById("mtf-1h-trend"), document.getElementById("mtf-1h-rsi"), _mtfCoin ? _mtfCoin.mtf_1h : null, null);
  _mtfSet(document.getElementById("mtf-primary-trend"), document.getElementById("mtf-primary-rsi"), _mtfCoin ? _mtfCoin.mtf_primary : null, null);
  const _mtfTf = document.getElementById("mtf-tf-label");
  if (_mtfTf && _mtfCoin && _mtfCoin.mtf_primary && _mtfCoin.mtf_primary.tf) {
    _mtfTf.textContent = String(_mtfCoin.mtf_primary.tf).toUpperCase();
  }
  const _mtfAi = document.getElementById("mtf-ai-status");
  if (_mtfAi) {
    _mtfAi.textContent = _mtfCoin
      ? `${_mtfCoin.consensus_status || "STANDBY"} · Q${_mtfCoin.quant_score || 0}% · AI${Math.round((_mtfCoin.ai_confidence || 0) * 100)}%`
      : "--";
  }

  // Risk per Transaksi (read-only, additive): current risk the entry was sized for, read from
  // basket[0] exactly like mtf_1h / mtf_primary above. No literals — every value comes from the
  // payload; absent data renders '--'. 'QUANT' = sized by the quant engine; 'MIN_NOTIONAL_GUARD'
  // = base qty was clamped UP to the venue minimum-notional floor (or skipped when unaffordable).
  const _risk = _mtfCoin;
  const _num = (v) => (v === null || v === undefined || v === "" || isNaN(Number(v))) ? null : Number(v);
  const _riskPctEl = document.getElementById("risk-pct");
  const _riskUsdEl = document.getElementById("risk-usd");
  const _riskSrcEl = document.getElementById("risk-sizing-source");
  const _riskMinEl = document.getElementById("risk-min-notional");
  const _riskFeeEl = document.getElementById("risk-fee-pct");
  const _riskPct = _num(_risk ? _risk.risk_pct : null);
  const _riskUsd = _num(_risk ? _risk.risk_usd : null);
  const _riskMin = _num(_risk ? _risk.risk_min_notional : null);
  const _riskFee = _num(_risk ? _risk.risk_fee_pct : null);
  if (_riskPctEl) _riskPctEl.textContent = _riskPct !== null ? `${_riskPct.toFixed(2)}%` : "--";
  if (_riskUsdEl) _riskUsdEl.textContent = _riskUsd !== null ? `${quoteCurr} $${_riskUsd.toFixed(2)}` : "--";
  if (_riskSrcEl) {
    const _src = _risk ? _risk.risk_sizing_source : null;
    if (_src === "QUANT" || _src === "MIN_NOTIONAL_GUARD" || _src === "NO_ENTRY") {
      _riskSrcEl.textContent = _src === "NO_ENTRY" ? "BELUM ADA ENTRY" : _src;
      _riskSrcEl.className = `cf-v mono trend-${_src === "QUANT" ? "bullish" : (_src === "MIN_NOTIONAL_GUARD" ? "bearish" : "neutral")}`;
    } else {
      _riskSrcEl.textContent = "--";
      _riskSrcEl.className = "cf-v mono trend-neutral";
    }
  }
  if (_riskMinEl) _riskMinEl.textContent = _riskMin !== null ? `Min $${_riskMin.toFixed(2)}` : "Min --";
  if (_riskFeeEl) _riskFeeEl.textContent = _riskFee !== null ? `Fee ${_riskFee.toFixed(3)}%` : "Fee --";

  // Profits
  const profitStr = (state.realized_profit >= 0 ? "+" : "") + `$${state.realized_profit?.toFixed(4) || "0.00"} ${quoteCurr}`;
  cardProfit.textContent = profitStr;
  cardProfit.className = `metric-value mono ${state.realized_profit >= 0 ? "profit-green" : "profit-red"}`;
  const dailyCount = state.daily_trades || 0;
  const maxTrades = state.max_daily_trades || 30;
  cardMatchedTrades.textContent = `${state.matched_trades || 0} Matched Trades | Hari ini: ${dailyCount}/${maxTrades}`;

  // Update Profit & Loss (P&L) Terminal stats and initial logs
  updatePnlHeaderStats();
  if (pnlLogs.length === 0 && Array.isArray(state.logs)) {
    state.logs.forEach(l => {
      if (l.type === 'tp' || l.type === 'cl' || (l.text && (l.text.includes('TAKE-PROFIT') || l.text.includes('CUT-LOSS') || l.text.includes('Profit:') || l.text.includes('Loss:')))) {
        processPnlLogEntry(l);
      }
    });
  }

  // Update Forex Factory Macro News Pill
  const newsPill = document.getElementById("news-status-pill");
  const newsText = document.getElementById("news-status-text");
  const newsIcon = document.getElementById("news-status-icon");
  if (newsText && state.news_guard) {
    const ng = state.news_guard;
    if (ng.is_danger) {
      const imm = ng.imminent_event;
      const titleClean = imm && imm.title ? (imm.title.length > 30 ? imm.title.substring(0, 28).trim() + "..." : imm.title) : "Red News";
      if (newsIcon) newsIcon.textContent = "🔴";
      newsText.textContent = `FF: ${titleClean} (${imm ? imm.minutes_away : 0}m)`;
      if (newsPill) {
        newsPill.style.background = "rgba(239,68,68,0.2)";
        newsPill.style.borderColor = "#ef4444";
        newsPill.style.color = "#f87171";
        newsPill.title = ng.caution_reason || "Bahaya: Berita High-Impact Mendatang!";
      }
    } else {
      const up = ng.upcoming_events && ng.upcoming_events.length > 0 ? ng.upcoming_events[0] : null;
      if (newsIcon) newsIcon.textContent = "📰";
      if (up) {
        const hrs = (up.minutes_away / 60).toFixed(1);
        const titleClean = up.title ? (up.title.length > 30 ? up.title.substring(0, 28).trim() + "..." : up.title) : "News";
        newsText.textContent = `FF: ${titleClean} (${hrs}h)`;
      } else {
        newsText.textContent = `Forex Factory: Aman`;
      }
      if (newsPill) {
        newsPill.style.background = "rgba(59,130,246,0.12)";
        newsPill.style.borderColor = "rgba(59,130,246,0.3)";
        newsPill.style.color = "var(--accent-cyan)";
        newsPill.title = ng.caution_reason || "Kondisi macro aman";
      }
    }
  }

  // Update Self-Learning Memory Pill
  const memPill = document.getElementById("memory-status-pill");
  const memText = document.getElementById("memory-status-text");
  const memIcon = document.getElementById("memory-status-icon");
  if (memText && state.memory_stats) {
    const ms = state.memory_stats;
    const exp = ms.total_experiences || 0;
    const wr = ms.win_rate_pct !== undefined ? ms.win_rate_pct : 0;
    const cdCount = ms.active_cooldowns_count || 0;
    // AUDIT FIX: surface the live expectancy scoreboard (PF / payoff / avg-R) in the tooltip.
    const perf = state.performance || {};
    const _pf = (perf.profit_factor === null || perf.profit_factor === undefined) ? "-" : perf.profit_factor;
    const _po = (perf.payoff_ratio === null || perf.payoff_ratio === undefined) ? "-" : perf.payoff_ratio;
    const _ar = (perf.avg_r_multiple === null || perf.avg_r_multiple === undefined) ? "-" : perf.avg_r_multiple;
    const perfTip = ` | Live: ${perf.closed_trades || 0} closed, WR ${perf.win_rate_pct || 0}%, PF ${_pf}, Payoff ${_po}, AvgR ${_ar}`;

    if (memIcon) memIcon.textContent = "🧠";
    if (cdCount > 0) {
      memText.textContent = `Memory: ${exp} Exp (${wr}%) | ${cdCount} Cooldown`;
      if (memPill) {
        memPill.style.background = "rgba(234,179,8,0.15)";
        memPill.style.borderColor = "rgba(234,179,8,0.4)";
        memPill.style.color = "#facc15";
        memPill.title = `Self-Learning Memory: ${exp} trade tercatat (${wr}% Win Rate). ${cdCount} koin dalam karantina cooldown pasca-loss.${perfTip}`;
      }
    } else {
      memText.textContent = `Memory: ${exp} Exp (${wr}%)`;
      if (memPill) {
        memPill.style.background = "rgba(168,85,247,0.12)";
        memPill.style.borderColor = "rgba(168,85,247,0.3)";
        memPill.style.color = "#c084fc";
        memPill.title = `Self-Learning Memory: ${exp} trade tercatat (${wr}% Win Rate). Sistem terus mempelajari setiap pola pasar.${perfTip}`;
      }
    }
  }

  // Update Discipline Pill & Daily Stats
  const disciplinePill = document.getElementById("discipline-pill");
  const disciplineText = document.getElementById("discipline-text");
  const disciplineIcon = document.getElementById("discipline-icon");
  if (disciplineText) {
    const isLocked = (state.daily_status === "CIRCUIT_LOCKED");
    if (isLocked) {
      if (disciplineIcon) disciplineIcon.textContent = "🛑";
      disciplineText.textContent = `Disiplin: Terkunci (Max Loss 5%)`;
      if (disciplinePill) {
        disciplinePill.style.background = "rgba(239,68,68,0.15)";
        disciplinePill.style.borderColor = "rgba(239,68,68,0.4)";
        disciplinePill.style.color = "#ef4444";
      }
    } else {
      if (disciplineIcon) disciplineIcon.textContent = "📈";
      disciplineText.textContent = `Disiplin: ${dailyCount}/${maxTrades} (Aman)`;
      if (disciplinePill) {
        disciplinePill.style.background = "rgba(16,185,129,0.12)";
        disciplinePill.style.borderColor = "rgba(16,185,129,0.3)";
        disciplinePill.style.color = "var(--accent-green)";
      }
    }
  }

  // Positions
  const pnlStr = (state.unrealized_pnl >= 0 ? "+" : "") + `$${state.unrealized_pnl?.toFixed(4) || "0.00"} ${quoteCurr}`;
  cardUnrealizedPnl.textContent = pnlStr;
  cardUnrealizedPnl.style.color = state.unrealized_pnl >= 0 ? "var(--accent-green)" : "var(--accent-red)";
  cardPositionContracts.textContent = `${state.position_contracts || 0} Contracts (${state.leverage || 5}x Isolated)`;

  // Open Orders card is owned SOLELY by fetchOpenOrders() (/api/orders/open), which counts
  // REAL exchange orders (grid + STOP_MARKET/conditional). Do NOT also write it from the WS
  // state push here: the state value is grid-only and pushing a different number into the same
  // node made the card flicker 1<->0 between the WS push and the 8s poll. One source of truth.
  if (state.grid_summary && state.grid_summary.grid_step_usdt) {
    cardGridStep.textContent = `Step: $${state.grid_summary.grid_step_usdt}`;
    ladderRangeLabel.textContent = `Range: ${state.grid_summary.range || "--"}`;
  }

  // Update Stop-Loss placeholder if bot has an active stop loss
  const currentSL = state.stop_loss || (state.grid_summary && state.grid_summary.stop_loss);
  if (inputStopLoss && currentSL) {
    inputStopLoss.placeholder = `$${currentSL} (Auto)`;
  }

  // Render Grid Ladder
  renderLadder(state);
}

// Render Interactive Visual Grid Ladder
function renderLadder(state) {
  const tabsContainer = document.getElementById("ladder-coin-tabs");
  let activeCoin = null;

  if (state && state.basket && state.basket.length > 1) {
    if (tabsContainer) {
      tabsContainer.style.display = "flex";
      if (!selectedLadderCoin || !state.basket.find(c => c.symbol === selectedLadderCoin)) {
        selectedLadderCoin = state.basket[0].symbol;
      }
      activeCoin = state.basket.find(c => c.symbol === selectedLadderCoin) || state.basket[0];

      tabsContainer.innerHTML = state.basket.map(c => {
        const isSel = c.symbol === activeCoin.symbol;
        const pnl = c.unrealized_pnl || 0;
        const pnlStr = (pnl >= 0 ? "+" : "") + `$${pnl.toFixed(2)}`;
        const pnlColor = pnl >= 0 ? "var(--accent-green)" : "var(--accent-red)";
        const bg = isSel ? "rgba(6,182,212,0.25)" : "rgba(255,255,255,0.04)";
        const border = isSel ? "1px solid var(--accent-cyan)" : "1px solid rgba(255,255,255,0.08)";
        return `
          <button type="button" class="quick-btn" style="background:${bg}; border:${border}; display:flex; align-items:center; gap:0.4rem; padding: 0.35rem 0.65rem; border-radius: 6px; cursor: pointer; white-space: nowrap;" onclick="switchLadderTab('${c.symbol}')">
            <span class="mono font-bold" style="color: ${isSel ? 'var(--accent-cyan)' : 'var(--text-bright)'}; font-size: 0.8rem;">${c.symbol.replace('/USDT','')}</span>
            <span class="mono" style="color: ${pnlColor}; font-size: 0.72rem;">${pnlStr}</span>
          </button>
        `;
      }).join('');
    }
  } else {
    if (tabsContainer) tabsContainer.style.display = "none";
  }

  const gridsSource = (activeCoin && activeCoin.active_grids && activeCoin.active_grids.length > 0)
    ? activeCoin.active_grids
    : (state ? state.active_grids : []);

  if (!state || !gridsSource || gridsSource.length === 0) {
    gridLadderDisplay.innerHTML = `
      <div style="text-align: center; color: var(--text-dim); padding: 2rem;">
        Nyalakan bot untuk melihat jaring undak beli dan jual secara live.
      </div>
    `;
    return;
  }

  const grids = [...gridsSource].sort((a, b) => b.price - a.price);
  const currentPrice = (activeCoin && activeCoin.current_price) || state.current_price || 0;
  const gridSummary = (activeCoin && activeCoin.grid_summary) || state.grid_summary || {};
  const slVal = (activeCoin && activeCoin.stop_loss) || state.stop_loss || (gridSummary && gridSummary.stop_loss);

  if (ladderRangeLabel && gridSummary.range) {
    const coinPrefix = activeCoin ? `[${activeCoin.symbol.replace('/USDT','')}] ` : "";
    ladderRangeLabel.textContent = `${coinPrefix}Range: ${gridSummary.range}`;
  }

  let html = "";

  // Position-level TP1 target (software-managed market cash-in, NOT a resting Binance order).
  const tp1Target = (activeCoin && activeCoin.tp1_price) || state.tp1_price || 0;
  const tp1Done = (activeCoin && activeCoin.tp1_filled) || false;
  if (tp1Target > 0) {
    html += `
      <div class="ladder-level" style="border-left-color: var(--accent-green); opacity: 0.9;">
        <span class="mono">🎯 TP1 Target: $${tp1Target} <span style="font-size: 0.7rem; color: var(--text-dim);">(cash-in 50%)</span></span>
        <span class="ladder-badge" style="background: rgba(16,185,129,0.2); color: var(--accent-green);">${tp1Done ? "TP1 HIT ✓" : "TARGET"}</span>
      </div>
    `;
  }

  // Upper Bound
  if (gridSummary && gridSummary.upper_price) {
    html += `
      <div class="ladder-level" style="border-left-color: var(--accent-magenta); opacity: 0.65;">
        <span class="mono">🔼 Upper Ceiling: $${gridSummary.upper_price}</span>
        <span class="ladder-badge badge-sell">MAX BOUND</span>
      </div>
    `;
  }

  let priceLineInserted = false;

  grids.forEach((g, idx) => {
    // Insert current market price marker between grids
    if (!priceLineInserted && currentPrice >= g.price) {
      html += `
        <div class="ladder-level current-price">
          <span class="mono">📍 LIVE PRICE: $${currentPrice.toFixed(5)}</span>
          <span class="ladder-badge" style="background: rgba(6,182,212,0.25); color: #38bdf8;">ACTIVE TICKER</span>
        </div>
      `;
      priceLineInserted = true;
    }

    const isBuy = g.side === "BUY";
    const badgeClass = isBuy ? "badge-buy" : "badge-sell";
    const levelClass = isBuy ? "buy-grid" : "sell-grid";
    const statusText = isBuy ? `BUY DIP (TP: $${g.target_sell_price || "-"})` : `SELL TAKE-PROFIT`;

    html += `
      <div class="ladder-level ${levelClass}">
        <span class="mono font-bold">$${g.price} <span style="font-size: 0.75rem; color: var(--text-dim);">(${g.quantity} Qty)</span></span>
        <span class="ladder-badge ${badgeClass}">${statusText}</span>
      </div>
    `;
  });

  if (!priceLineInserted) {
    html += `
      <div class="ladder-level current-price">
        <span class="mono">📍 LIVE PRICE: $${currentPrice.toFixed(5)}</span>
        <span class="ladder-badge" style="background: rgba(6,182,212,0.25); color: #38bdf8;">ACTIVE TICKER</span>
      </div>
    `;
  }

  // Stop Loss Floor
  if (slVal) {
    html += `
      <div class="ladder-level" style="border-left-color: var(--accent-red); opacity: 0.75;">
        <span class="mono">🛑 Safety Stop-Loss: $${slVal}</span>
        <span class="ladder-badge" style="background: rgba(239,68,68,0.2); color: var(--accent-red);">FLOOR PROTECTION</span>
      </div>
    `;
  }

  gridLadderDisplay.innerHTML = html;
}

// Action Handlers (Start & Stop)
async function handleStartBot() {
  const totalBal = lastBotState ? (lastBotState.wallet_balance || lastBotState.total_balance || 0) : 0;
  const availBal = lastBotState ? (lastBotState.available_balance || 0) : 0;
  const hasActivePos = lastBotState && Array.isArray(lastBotState.positions) && lastBotState.positions.length > 0;

  const currQ = currentQuoteAsset || "USDT";

  // Only block if NO active positions to resume AND balance is genuinely exhausted
  if (!hasActivePos && totalBal < 3.0 && availBal < 3.0) {
    if (currentMode === "live") {
      alert(`🛑 Saldo USD-M Futures LIVE Anda di Binance saat ini hanya $${availBal.toFixed(2)} ${currQ} (Total: $${totalBal.toFixed(2)}).\n\nSilakan transfer ${currQ} ke USD-M Futures terlebih dahulu sebelum memulai bot baru.`);
    } else {
      alert(`🛑 Saldo Testnet Futures tidak mencukupi ($${availBal.toFixed(2)} ${currQ}).`);
    }
    appendLog(`Gagal memulai bot: Saldo Binance tidak mencukupi (Tersedia: $${availBal.toFixed(2)} ${currQ} | Total: $${totalBal.toFixed(2)} ${currQ}).`, "error");
    return;
  }

  if (btnStartBot) btnStartBot.disabled = true;

  const rawSymbol = inputSymbol ? inputSymbol.value.trim().toUpperCase() : "";
  const autoPick = checkAutoPick ? (checkAutoPick.checked || !rawSymbol) : true;
  const symbol = autoPick ? null : rawSymbol;
  let investment = parseFloat(inputInvestment.value) || 0.0;
  if (investment <= 0 && lastBotState && lastBotState.available_balance > 0) {
    investment = parseFloat((lastBotState.available_balance * 0.85).toFixed(2));
  }
  const grids = parseInt(inputGrids.value) || 8;
  const selectBasketSize = document.getElementById("select-basket-size");
  const basketSize = selectBasketSize ? (parseInt(selectBasketSize.value) || 3) : 3;

  const displaySym = symbol || `Top ${basketSize} Bullish Basket`;
  const stopLoss = (inputStopLoss && inputStopLoss.value.trim()) ? parseFloat(inputStopLoss.value) : null;
  const slDisplay = stopLoss ? ` | SL: $${stopLoss}` : " | SL: Auto ATR";
  appendLog(`Memulai bot pada ${displaySym} [${currentMode.toUpperCase()}] ($${investment} ${currQ}, ${grids} grids${slDisplay})...`, "info");

  try {
    const res = await fetch("/api/bot/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        symbol: symbol,
        mode: currentMode,
        quote_asset: currQ,
        investment: investment,
        grid_count: grids,
        auto_pick: autoPick,
        stop_loss: stopLoss,
        basket_size: basketSize
      })
    });
    const data = await res.json();
    if (data.success) {
      isRunning = true;
      if (btnStartBot) btnStartBot.disabled = true;
      if (btnStopBot) btnStopBot.disabled = false;
      botStatusPill.className = "status-pill running";
      botStatusText.textContent = "RUNNING";
      if (botActionHint) {
        botActionHint.textContent = "● Bot sedang aktif berjalan (Live di Binance)";
        botActionHint.style.color = "var(--accent-green)";
      }
      const badge = document.getElementById("control-active-badge");
      if (badge) {
        badge.textContent = "ACTIVE";
        badge.style.color = "var(--accent-green)";
      }
      appendLog(data.message || "Bot starting...", "success");
    } else {
      appendLog(`Start failed: ${data.message}`, "error");
      if (btnStartBot) btnStartBot.disabled = false;
    }
  } catch (e) {
    appendLog(`Error starting bot: ${e}`, "error");
    if (btnStartBot) btnStartBot.disabled = false;
  }
}

async function handleStopBot() {
  if (btnStopBot) btnStopBot.disabled = true;

  appendLog("Mengirim instruksi STOP ke bot...", "warning");
  try {
    const res = await fetch("/api/bot/stop", { method: "POST" });
    const data = await res.json();
    appendLog(data.message || "Bot stopped.", "info");

    // Immediately reflect stopped state
    isRunning = false;
    cardSymbol.textContent = "-- / USDT";
    cardCurrentPrice.textContent = "Price: $0.00000";
    cardOpenOrdersCount.textContent = "0 Orders";
    if (inputSymbol) inputSymbol.value = "";
    if (btnStartBot) {
      btnStartBot.innerHTML = `<span>🚀</span><span>START BOT</span>`;
      btnStartBot.disabled = false;
    }
    if (btnStopBot) btnStopBot.disabled = true;
    botStatusPill.className = "status-pill stopped";
    botStatusText.textContent = "OFFLINE";
    if (botActionHint) {
      botActionHint.textContent = "○ Bot berhenti & Target Koin di-reset (Standby) - Klik START BOT untuk mulai";
      botActionHint.style.color = "var(--text-dim)";
    }
    const badge = document.getElementById("control-active-badge");
    if (badge) {
      badge.textContent = "STANDBY";
      badge.style.color = "var(--text-dim)";
    }
    const tabsContainer = document.getElementById("ladder-coin-tabs");
    if (tabsContainer) tabsContainer.style.display = "none";
    if (ladderRangeLabel) ladderRangeLabel.textContent = "Range: -- s/d --";
    gridLadderDisplay.innerHTML = `
      <div style="text-align: center; color: var(--text-dim); padding: 2rem;">
        Bot telah dihentikan secara aman & Target Koin di-reset.<br>
        <span style="font-size: 0.8rem; color: var(--accent-cyan); display: inline-block; margin-top: 0.5rem;">Pilih koin baru dari tabel scanner atau klik START BOT untuk mulai.</span>
      </div>
    `;
  } catch (e) {
    appendLog(`Error stopping bot: ${e}`, "error");
    if (btnStopBot) btnStopBot.disabled = false;
  }
}

// Fetch Market Scanner (Supports Dual Tabs: 💎 Alpha & 🔥 Top Bullish)
let activeScannerTab = 'alpha';

function switchScannerTab(tabType) {
  if (activeScannerTab === tabType && scannerTableBody && scannerTableBody.innerHTML.includes("<tr id=")) return;
  activeScannerTab = tabType;
  const btnAlpha = document.getElementById("tab-scan-alpha");
  const btnBullish = document.getElementById("tab-scan-bullish");
  const thMetric = document.getElementById("th-scanner-metric");

  if (tabType === 'bullish') {
    if (btnBullish) {
      btnBullish.style.color = "var(--accent-green)";
      btnBullish.style.borderColor = "var(--accent-green)";
      btnBullish.style.background = "rgba(16,185,129,0.12)";
      btnBullish.style.fontWeight = "700";
    }
    if (btnAlpha) {
      btnAlpha.style.color = "var(--text-dim)";
      btnAlpha.style.borderColor = "var(--border-color)";
      btnAlpha.style.background = "transparent";
      btnAlpha.style.fontWeight = "600";
    }
    if (thMetric) {
      thMetric.textContent = "24h Gain %";
      thMetric.title = "Kenaikan harga 24 jam & Skor Momentum Bullish";
    }
  } else {
    if (btnAlpha) {
      btnAlpha.style.color = "var(--accent-cyan)";
      btnAlpha.style.borderColor = "var(--accent-cyan)";
      btnAlpha.style.background = "rgba(6,182,212,0.12)";
      btnAlpha.style.fontWeight = "700";
    }
    if (btnBullish) {
      btnBullish.style.color = "var(--text-dim)";
      btnBullish.style.borderColor = "var(--border-color)";
      btnBullish.style.background = "transparent";
      btnBullish.style.fontWeight = "600";
    }
    if (thMetric) {
      thMetric.textContent = "Trend / Vol";
      thMetric.title = "Trend bias (non-BULLISH = edge-aligned) & rasio volume vs rata-rata";
    }
  }

  if (scannerTableBody) {
    const loadingText = (tabType === 'bullish') 
      ? "Memindai koin Top Bullish di Binance Futures..." 
      : "Memindai setup Asimetris (High Reward / Low Risk) di Binance...";
    scannerTableBody.innerHTML = `<tr><td colspan="4" style="text-align: center; color: var(--text-dim); padding: 1.5rem;">${loadingText}</td></tr>`;
  }
  refreshScanner(false);
}

async function refreshScanner(force = false) {
  if (force || !scannerTableBody.innerHTML.trim() || scannerTableBody.innerHTML.includes("Scanning") || scannerTableBody.innerHTML.includes("Memindai")) {
    const loadingText = (activeScannerTab === 'bullish') 
      ? "Memindai koin Top Bullish di Binance Futures..." 
      : "Memindai setup Asimetris (High Reward / Low Risk) di Binance...";
    scannerTableBody.innerHTML = `<tr><td colspan="4" style="text-align: center; color: var(--text-dim); padding: 1.5rem;">${loadingText}</td></tr>`;
  }

  try {
    let quote = "USDT";
    if (typeof currentQuoteAsset !== "undefined" && currentQuoteAsset) {
      quote = currentQuoteAsset;
    } else if (typeof currentMarketCategory !== "undefined" && currentMarketCategory && currentMarketCategory !== "ALL") {
      quote = currentMarketCategory;
    } else if (currentSymbol && currentSymbol.includes("/")) {
      quote = currentSymbol.split("/")[1].split(":")[0];
    } else if (inputSymbol && inputSymbol.value && inputSymbol.value.includes("/")) {
      quote = inputSymbol.value.split("/")[1].split(":")[0];
    }
    const res = await fetch(`/api/scanner?quote=${encodeURIComponent(quote)}&type=${activeScannerTab}${force ? '&force=true' : ''}`);
    const data = await res.json();

    if (!data.success || !data.coins || data.coins.length === 0) {
      if (force || !scannerTableBody.innerHTML.includes("<tr id=")) {
        scannerTableBody.innerHTML = `<tr><td colspan="4" style="text-align: center; color: var(--text-dim);">Tidak ada koin yang memenuhi kriteria saat ini.</td></tr>`;
      }
      return;
    }

    let rows = "";
    data.coins.forEach(c => {
      const cleanKey = c.symbol.replace(/[^a-zA-Z0-9]/g, '');
      const isSelected = (currentSymbol === c.symbol || (inputSymbol && inputSymbol.value === c.symbol));
      let metricHtml = "";

      if (activeScannerTab === 'bullish') {
        const change = c['24h_change_pct'] || 0;
        const sub = c.bullish_score ? `Skor: ${c.bullish_score}` : `RSI: ${c.rsi_14 || '--'}`;
        metricHtml = `
          <span class="font-bold" style="color: var(--accent-green);">+${change}%</span>
          <span style="font-size: 0.70rem; color: #848e9c; display: block;">${sub}</span>
        `;
      } else {
        const tb = c.trend_bias || '--';
        const tbColor = (tb === 'BULLISH') ? 'var(--accent-green)' : (tb === 'BEARISH') ? '#ef4444' : 'var(--accent-cyan)';
        const vol = (c.volume_ratio != null) ? `${c.volume_ratio}x` : '--';
        metricHtml = `
          <span class="font-bold" style="color: ${tbColor};">${tb}</span>
          <span style="font-size: 0.70rem; color: #848e9c; display: block;">Vol: ${vol}</span>
        `;
      }

      rows += `
        <tr id="scanner-row-${cleanKey}" class="${isSelected ? 'selected-row' : ''}">
          <td class="mono font-bold" style="color: var(--text-main);">${c.symbol}</td>
          <td class="mono">$${c.price}</td>
          <td class="mono" style="line-height: 1.2;">
            ${metricHtml}
          </td>
          <td style="white-space: nowrap;">
            <button type="button" id="btn-pick-${cleanKey}" class="btn-pick ${isSelected ? 'active' : ''}" onclick="selectSymbol('${c.symbol}')">Select</button>
            <button type="button" class="btn-run-coin" title="Pilih & Langsung Jalankan Bot pada koin ini" onclick="selectSymbol('${c.symbol}', true)">🚀 Run</button>
          </td>
        </tr>
      `;
    });
    scannerTableBody.innerHTML = rows;

    // Dynamically populate Quick Coin Buttons from live Binance scan!
    const quickContainer = document.getElementById("quick-coins-container");
    if (quickContainer && data.coins.length > 0) {
      const label = (activeScannerTab === 'bullish') ? "🔥 Top Bullish:" : "💎 Edge-Aligned:";
      let btns = `<span style="font-size: 0.75rem; color: var(--text-dim); align-self: center;">${label}</span>`;
      data.coins.slice(0, 5).forEach(c => {
        const clean = c.symbol.split('/')[0];
        if (activeScannerTab === 'bullish') {
          const change = c['24h_change_pct'] || 0;
          btns += `<button type="button" class="quick-btn" title="Kenaikan 24h: +${change}%" onclick="selectSymbol('${c.symbol}')">${clean} (+${change}%)</button>`;
        } else {
          const tb = c.trend_bias || '--';
          const vol = (c.volume_ratio != null) ? `${c.volume_ratio}x` : '--';
          btns += `<button type="button" class="quick-btn" title="Trend: ${tb} | Volume: ${vol}" onclick="selectSymbol('${c.symbol}')">${clean} (${tb} ${vol})</button>`;
        }
      });
      quickContainer.innerHTML = btns;
    }
  } catch (e) {
    scannerTableBody.innerHTML = `<tr><td colspan="4" style="text-align: center; color: var(--accent-red);">Error scanning market: ${e}</td></tr>`;
  }
}

// Log Functions
function appendLog(text, logType = "info") {
  const now = new Date();
  const timeStr = now.toTimeString().split(" ")[0];
  renderLogEntry({ time: timeStr, text: text, type: logType });
}

function renderLogEntry(entry) {
  if (!entry) return;
  const div = document.createElement("div");
  div.className = `log-entry ${entry.type || "info"}`;
  div.innerHTML = `
    <span class="log-time">[${entry.time}]</span>
    <span>${entry.text}</span>
  `;
  logTerminal.appendChild(div);
  logTerminal.scrollTop = logTerminal.scrollHeight;

  // Process P&L events for dedicated Log Terminal Profit & Loss
  processPnlLogEntry(entry);

  // Update heartbeat badge on tick events
  const hbBadge = document.getElementById("terminal-heartbeat-badge");
  if (hbBadge && entry.type === "tick") {
    hbBadge.textContent = `Tick: ${entry.time}`;
  }
}

function clearLogs() {
  logTerminal.innerHTML = "";
}

// ==========================================
// REAL-TIME PROFIT & LOSS (P&L) TERMINAL
// ==========================================
let pnlLogs = [];
let pnlFilter = "ALL";
let pnlWins = 0;
let pnlLosses = 0;

function filterPnlLogs(filterType) {
  pnlFilter = filterType;
  document.getElementById("pnl-filter-all")?.classList.toggle("active", filterType === "ALL");
  document.getElementById("pnl-filter-profit")?.classList.toggle("active", filterType === "PROFIT");
  document.getElementById("pnl-filter-loss")?.classList.toggle("active", filterType === "LOSS");
  renderPnlTerminal();
}

function processPnlLogEntry(entry) {
  if (!entry || !entry.text) return;

  const isTp = entry.type === "tp" || entry.text.includes("TAKE-PROFIT") || entry.text.includes("TP TERCAPAI") || entry.text.includes("Profit:");
  const isCl = entry.type === "cl" || entry.text.includes("CUT-LOSS") || entry.text.includes("CIRCUIT BREAKER") || entry.text.includes("Realized Loss:");

  if (!isTp && !isCl) return;

  const category = isTp ? "PROFIT" : "LOSS";
  if (isTp) pnlWins++;
  if (isCl) pnlLosses++;

  const pnlEntry = {
    id: Date.now() + Math.random(),
    time: entry.time || new Date().toTimeString().split(" ")[0],
    text: entry.text,
    category: category,
    type: entry.type || (isTp ? "tp" : "cl")
  };

  pnlLogs.push(pnlEntry);
  if (pnlLogs.length > 200) pnlLogs.shift();

  updatePnlHeaderStats();
  renderPnlTerminal();
}

function updatePnlHeaderStats() {
  const statsBadge = document.getElementById("pnl-terminal-stats");
  if (statsBadge) {
    statsBadge.textContent = `${pnlWins} TP (Wins) / ${pnlLosses} CL (Losses)`;
  }
  const totalBadge = document.getElementById("pnl-terminal-total");
  if (totalBadge) {
    const profit = (lastBotState && lastBotState.realized_profit !== undefined) ? lastBotState.realized_profit : 0.0;
    const quote = (lastBotState && (lastBotState.currency || lastBotState.quote_asset)) || currentQuoteAsset || "USDT";
    totalBadge.textContent = `${profit >= 0 ? '+' : ''}$${profit.toFixed(4)} ${quote}`;
    totalBadge.className = `ladder-badge ${profit >= 0 ? 'badge-buy' : 'badge-sell'} mono font-bold`;
  }
}

function renderPnlTerminal() {
  const container = document.getElementById("pnl-log-terminal");
  if (!container) return;

  const filtered = pnlLogs.filter(item => {
    if (pnlFilter === "ALL") return true;
    return item.category === pnlFilter;
  });

  if (filtered.length === 0) {
    container.innerHTML = `
      <div id="pnl-empty-msg" style="text-align: center; color: var(--text-dim); padding: 1.5rem 0.5rem; font-size: 0.85rem;">
        <span>📊 Menunggu eksekusi Take-Profit (TP) atau Cut-Loss (CL) live di Binance...</span>
        <div style="font-size: 0.75rem; color: var(--text-muted); margin-top: 0.35rem;">Setiap pencapaian profit atau cut-loss akan tercatat otomatis di sini.</div>
      </div>
    `;
    return;
  }

  container.innerHTML = filtered.map(item => {
    const isProfit = item.category === "PROFIT";
    const badgeText = isProfit ? "PROFIT (TP)" : "LOSS (CL)";
    const badgeStyle = isProfit 
      ? "background: rgba(16,185,129,0.2); color: var(--accent-green); border: 1px solid var(--accent-green);" 
      : "background: rgba(239,68,68,0.2); color: var(--accent-red); border: 1px solid var(--accent-red);";
    const textStyle = isProfit ? "color: #34d399;" : "color: #f87171;";

    return `
      <div class="log-entry ${item.type}" style="display: flex; gap: 0.5rem; align-items: baseline; padding: 3px 0;">
        <span class="log-time" style="font-size: 0.75rem;">[${item.time}]</span>
        <span class="ladder-badge" style="${badgeStyle} font-size: 0.68rem; padding: 1px 5px; flex-shrink: 0;">${badgeText}</span>
        <span style="${textStyle} font-weight: 500;">${item.text}</span>
      </div>
    `;
  }).join("");

  container.scrollTop = container.scrollHeight;
}

function clearPnlLogs() {
  pnlLogs = [];
  pnlWins = 0;
  pnlLosses = 0;
  updatePnlHeaderStats();
  renderPnlTerminal();
}

function copyPnlLogs() {
  const container = document.getElementById("pnl-log-terminal");
  if (!container) return;
  const text = container.innerText;
  navigator.clipboard.writeText(text).then(() => {
    appendLog("📋 Log Terminal Profit & Loss berhasil disalin ke clipboard!", "success");
  }).catch(() => {
    appendLog("Gagal menyalin log P&L.", "error");
  });
}

async function fetchLiveStatus() {
  try {
    const q = currentQuoteAsset ? `?quote=${encodeURIComponent(currentQuoteAsset)}` : "";
    const res = await fetch(`/api/status${q}`);
    if (res.ok) {
      const data = await res.json();
      updateUIState(data);
    }
  } catch (e) {
    console.warn("Status poll error:", e);
  }
}

// Robust Local Timezone Formatter (converts Binance UTC to User's Local Time / WIB)
function formatTime(val, includeDate = false) {
  if (!val) return "--";
  try {
    let d;
    if (typeof val === "number") {
      d = new Date(val > 1e11 ? val : val * 1000);
    } else if (typeof val === "string") {
      let clean = val.trim();
      if (/^\d+$/.test(clean)) {
        const num = Number(clean);
        d = new Date(num > 1e11 ? num : num * 1000);
      } else {
        // Ensure UTC timezone is specified so browser converts to local time correctly
        if (!clean.endsWith("Z") && !clean.includes("+") && !clean.slice(10).includes("-")) {
          clean = clean.replace(" ", "T") + "Z";
        }
        d = new Date(clean);
      }
    } else {
      d = new Date(val);
    }

    if (!isNaN(d.getTime())) {
      const pad = (n) => String(n).padStart(2, "0");
      const hours = pad(d.getHours());
      const mins = pad(d.getMinutes());
      const secs = pad(d.getSeconds());
      const timeStr = `${hours}:${mins}:${secs}`;
      if (includeDate) {
        const year = d.getFullYear();
        const month = pad(d.getMonth() + 1);
        const day = pad(d.getDate());
        return `${year}-${month}-${day} ${timeStr}`;
      }
      return timeStr;
    }
  } catch (e) {}
  return String(val).slice(0, 19);
}

// Trading Data Hub: Tabs & Tables
let currentActiveTradeTab = "tab-positions";

function switchTradeTab(tabId) {
  currentActiveTradeTab = tabId;
  const buttons = document.querySelectorAll(".trade-tab-btn");
  buttons.forEach(btn => {
    btn.classList.toggle("active", btn.dataset.tab === tabId);
  });
  const contents = document.querySelectorAll(".tab-content");
  contents.forEach(content => {
    content.classList.toggle("active", content.id === tabId);
  });

  if (tabId === "tab-positions") fetchPositions();
  else if (tabId === "tab-open-orders") fetchOpenOrders();
  else if (tabId === "tab-order-history") fetchOrderHistory();
  else if (tabId === "tab-trade-history") fetchTradeHistory();
}

async function fetchPositions() {
  const tbody = document.getElementById("table-positions-body");
  const badge = document.getElementById("badge-positions-count");
  try {
    const activeMode = (isRunning && lastBotState && lastBotState.mode) ? lastBotState.mode : currentMode;
    const res = await fetch(`/api/positions?mode=${activeMode}`);
    const data = await res.json();
    if (data.success && data.positions && data.positions.length > 0) {
      if (badge) badge.textContent = data.positions.length;
      let html = "";
      data.positions.forEach(p => {
        const sideClass = p.side === "LONG" ? "tag-buy" : "tag-sell";
        const pnlClass = p.unrealized_pnl >= 0 ? "profit-green" : "profit-red";
        const tp1 = p.tp1_price || 0;
        const slVal = p.stop_loss || 0;
        const tp1Cell = tp1 > 0
          ? `<span style="color: var(--accent-green); font-weight: 600;">TP $${tp1}${p.tp1_filled ? " ✓" : ""}</span>`
            + `<br><span style="font-size: 0.7rem; color: var(--text-dim);">${p.is_breakeven_active ? "BEP+ " : ""}SL $${slVal}${p.trailing_stop_price > 0 ? " • Trail $" + p.trailing_stop_price : ""}</span>`
          : `<span style="color: var(--text-dim);">-</span>`;
        html += `
          <tr>
            <td class="mono font-bold" style="color: var(--text-main);">${p.symbol}</td>
            <td><span class="${sideClass}">${p.side} ${p.leverage}x</span></td>
            <td class="mono">${p.contracts}</td>
            <td class="mono">$${p.entry_price}</td>
            <td class="mono">$${p.mark_price}</td>
            <td class="mono">${tp1Cell}</td>
            <td class="mono">$${p.margin}</td>
            <td class="mono font-bold ${pnlClass}">${p.unrealized_pnl >= 0 ? "+" : ""}$${p.unrealized_pnl} (${p.percentage}%)</td>
            <td class="mono" style="color: var(--accent-amber);">$${p.liquidation_price || "--"}</td>
            <td>
              <button type="button" class="quick-btn" style="color: var(--accent-red); border-color: rgba(239,68,68,0.3);" onclick="closePosition('${p.symbol}')">Market Close</button>
            </td>
          </tr>
        `;
      });
      tbody.innerHTML = html;
    } else {
      if (badge) badge.textContent = "0";
      tbody.innerHTML = `<tr><td colspan="10" style="text-align: center; color: var(--text-dim); padding: 1.5rem;">Tidak ada posisi terbuka saat ini (0 Contracts / FLAT).</td></tr>`;
    }
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="10" style="text-align: center; color: var(--accent-red);">Error loading positions: ${e}</td></tr>`;
  }
}

async function fetchOpenOrders() {
  const tbody = document.getElementById("table-open-orders-body");
  const badge = document.getElementById("badge-open-orders-count");
  if (tbody && (!tbody.innerHTML.trim() || tbody.innerHTML.includes("Tidak ada") || tbody.innerHTML.includes("Error"))) {
    tbody.innerHTML = `<tr><td colspan="10" style="text-align: center; color: var(--text-dim); padding: 1.5rem;">Memuat open orders dari Binance...</td></tr>`;
  }
  try {
    const symParam = hubFilter === "ALL" ? "ALL" : (currentSymbol || "");
    const activeMode = (isRunning && lastBotState && lastBotState.mode) ? lastBotState.mode : currentMode;
    const res = await fetch(`/api/orders/open?mode=${activeMode}&symbol=${encodeURIComponent(symParam)}`);
    const data = await res.json();
    if (data.success && data.orders && data.orders.length > 0) {
      // ONLY update the global tab badge & dashboard card if viewing ALL orders (not filtered by single coin)
      if (hubFilter === "ALL") {
        if (badge) badge.textContent = data.orders.length;
        if (cardOpenOrdersCount) cardOpenOrdersCount.textContent = `${data.orders.length} Orders`;
      }
      let html = "";
      data.orders.forEach(o => {
        const sideClass = o.side === "BUY" ? "tag-buy" : "tag-sell";
        const timeDisplay = formatTime(o.time);
        html += `
          <tr>
            <td class="mono" style="font-size: 0.78rem; color: var(--text-dim);">${timeDisplay}</td>
            <td class="mono font-bold">${o.symbol}</td>
            <td><span class="tag-status new">${o.type}</span></td>
            <td><span class="${sideClass}">${o.side}</span></td>
            <td class="mono font-bold">$${o.price}</td>
            <td class="mono">${o.amount}</td>
            <td class="mono">${o.filled}</td>
            <td class="mono">${o.remaining}</td>
            <td><span class="tag-status new">${o.status}</span></td>
            <td>
              <button type="button" class="quick-btn" style="color: var(--accent-red); border-color: rgba(239,68,68,0.3);" onclick="cancelOrder('${o.id}', '${o.symbol}')">Cancel</button>
            </td>
          </tr>
        `;
      });
      tbody.innerHTML = html;
    } else {
      if (hubFilter === "ALL") {
        if (badge) badge.textContent = "0";
        if (cardOpenOrdersCount) cardOpenOrdersCount.textContent = "0 Orders";
      }
      tbody.innerHTML = `<tr><td colspan="10" style="text-align: center; color: var(--text-dim); padding: 1.5rem;">Tidak ada open orders aktif di Binance.</td></tr>`;
    }
  } catch (e) {
    if (!isRunning) {
      tbody.innerHTML = `<tr><td colspan="10" style="text-align: center; color: var(--accent-red);">Error loading open orders: ${e}</td></tr>`;
    }
  }
}

async function fetchOrderHistory() {
  const tbody = document.getElementById("table-order-history-body");
  if (tbody && (!tbody.innerHTML.trim() || tbody.innerHTML.includes("Belum ada") || tbody.innerHTML.includes("Error"))) {
    tbody.innerHTML = `<tr><td colspan="9" style="text-align: center; color: var(--text-dim); padding: 1.5rem;">Memuat riwayat order dari Binance...</td></tr>`;
  }
  try {
    const symParam = hubFilter === "ALL" ? "ALL" : (currentSymbol || "");
    const activeMode = (isRunning && lastBotState && lastBotState.mode) ? lastBotState.mode : currentMode;
    const res = await fetch(`/api/orders/history?mode=${activeMode}&symbol=${encodeURIComponent(symParam)}`);
    const data = await res.json();
    if (data.success && data.history && data.history.length > 0) {
      let html = "";
      data.history.forEach(o => {
        const sideClass = o.side === "BUY" ? "tag-buy" : "tag-sell";
        const statusClass = o.status === "FILLED" ? "tag-status filled" : (o.status === "CANCELED" ? "tag-status canceled" : "tag-status new");
        const timeDisplay = formatTime(o.time, true);
        html += `
          <tr>
            <td class="mono" style="font-size: 0.78rem; color: var(--text-dim);">${timeDisplay}</td>
            <td class="mono" style="font-size: 0.75rem; color: var(--text-dim);">${o.id}</td>
            <td class="mono font-bold">${o.symbol}</td>
            <td>${o.type}</td>
            <td><span class="${sideClass}">${o.side}</span></td>
            <td class="mono">$${o.price}</td>
            <td class="mono">${o.amount}</td>
            <td class="mono">${o.filled}</td>
            <td><span class="${statusClass}">${o.status}</span></td>
          </tr>
        `;
      });
      tbody.innerHTML = html;
    } else {
      tbody.innerHTML = `<tr><td colspan="9" style="text-align: center; color: var(--text-dim); padding: 1.5rem;">Belum ada riwayat order.</td></tr>`;
    }
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="9" style="text-align: center; color: var(--accent-red);">Error loading order history: ${e}</td></tr>`;
  }
}

async function fetchTradeHistory() {
  const tbody = document.getElementById("table-trade-history-body");
  if (tbody && (!tbody.innerHTML.trim() || tbody.innerHTML.includes("Belum ada") || tbody.innerHTML.includes("Error"))) {
    tbody.innerHTML = `<tr><td colspan="8" style="text-align: center; color: var(--text-dim); padding: 1.5rem;">Memuat riwayat transaksi dari Binance...</td></tr>`;
  }
  try {
    const symParam = hubFilter === "ALL" ? "ALL" : (currentSymbol || "");
    const activeMode = (isRunning && lastBotState && lastBotState.mode) ? lastBotState.mode : currentMode;
    const res = await fetch(`/api/trades/history?mode=${activeMode}&symbol=${encodeURIComponent(symParam)}`);
    const data = await res.json();
    if (data.success && data.trades && data.trades.length > 0) {
      let html = "";
      data.trades.forEach(t => {
        const sideClass = t.side === "BUY" ? "tag-buy" : "tag-sell";
        const timeDisplay = formatTime(t.time, true);
        html += `
          <tr>
            <td class="mono" style="font-size: 0.78rem; color: var(--text-dim);">${timeDisplay}</td>
            <td class="mono" style="font-size: 0.75rem; color: var(--text-dim);">${t.id}</td>
            <td class="mono font-bold">${t.symbol}</td>
            <td><span class="${sideClass}">${t.side}</span></td>
            <td class="mono font-bold">$${t.price}</td>
            <td class="mono">${t.amount}</td>
            <td class="mono">$${Number(t.cost || 0).toFixed(2)}</td>
            <td class="mono" style="color: var(--text-dim);">${t.fee}</td>
          </tr>
        `;
      });
      tbody.innerHTML = html;
    } else {
      tbody.innerHTML = `<tr><td colspan="8" style="text-align: center; color: var(--text-dim); padding: 1.5rem;">Belum ada transaksi tereksekusi.</td></tr>`;
    }
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="8" style="text-align: center; color: var(--accent-red);">Error loading trade history: ${e}</td></tr>`;
  }
}

async function cancelOrder(orderId, symbol) {
  if (!confirm(`Batalkan order #${orderId} (${symbol}) di Binance?`)) return;
  appendLog(`Membatalkan order #${orderId}...`, "warning");
  try {
    const res = await fetch("/api/orders/cancel", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ order_id: orderId, symbol: symbol, mode: currentMode })
    });
    const data = await res.json();
    if (data.success) {
      appendLog(`Order #${orderId} berhasil dibatalkan di Binance.`, "success");
      fetchOpenOrders();
      fetchLiveStatus();
    } else {
      appendLog(`Gagal membatalkan order #${orderId}: ${data.error}`, "error");
    }
  } catch (e) {
    appendLog(`Error membatalkan order: ${e}`, "error");
  }
}

async function cancelAllOrders() {
  if (!confirm("Batalkan SEMUA open order aktif di Binance dan reset Target Koin?")) return;
  appendLog("Membatalkan semua open orders di Binance & me-reset target koin...", "warning");
  try {
    const res = await fetch(`/api/orders/cancel-all?mode=${currentMode}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" }
    });
    const data = await res.json();
    if (data.success) {
      appendLog(`Semua order (${data.cancelled_count || 0}) dibatalkan di Binance. Target Koin berhasil di-reset.`, "success");
      
      // Reset UI target display & ladder
      cardSymbol.textContent = "-- / USDT";
      cardCurrentPrice.textContent = "Price: $0.00000";
      cardOpenOrdersCount.textContent = "0 Orders";
      if (inputSymbol) inputSymbol.value = "";
      if (btnStartBot) {
        btnStartBot.innerHTML = `<span>🚀</span><span>START BOT</span>`;
        btnStartBot.disabled = false;
      }
      if (btnStopBot) btnStopBot.disabled = true;
      botStatusPill.className = "status-pill stopped";
      botStatusText.textContent = "OFFLINE";
      if (botActionHint) {
        botActionHint.textContent = "○ Semua order dibatalkan & Target Koin di-reset (Standby).";
        botActionHint.style.color = "var(--text-dim)";
      }
      const badge = document.getElementById("control-active-badge");
      if (badge) {
        badge.textContent = "STANDBY";
        badge.style.color = "var(--text-dim)";
      }

      const tabsContainer = document.getElementById("ladder-coin-tabs");
      if (tabsContainer) tabsContainer.style.display = "none";
      if (ladderRangeLabel) ladderRangeLabel.textContent = "Range: -- s/d --";

      gridLadderDisplay.innerHTML = `
        <div style="text-align: center; color: var(--text-dim); padding: 2rem;">
          Semua order telah dibatalkan & Target Koin di-reset.<br>
          <span style="font-size: 0.8rem; color: var(--accent-cyan); display: inline-block; margin-top: 0.5rem;">Pilih koin baru dari tabel scanner di kiri atau klik START BOT untuk mulai.</span>
        </div>
      `;

      fetchOpenOrders();
      fetchPositions();
      fetchLiveStatus();
    } else {
      appendLog(`Gagal membatalkan order: ${data.error}`, "error");
    }
  } catch (e) {
    appendLog(`Error membatalkan order: ${e}`, "error");
  }
}

async function closePosition(symbol) {
  if (!confirm(`Tutup posisi ${symbol} pada harga pasar (Market Close)?`)) return;
  appendLog(`Menutup posisi ${symbol} pada harga pasar...`, "warning");
  try {
    const res = await fetch("/api/position/close", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ symbol: symbol, mode: currentMode })
    });
    const data = await res.json();
    if (data.success) {
      appendLog(data.message, "success");
      fetchPositions();
      fetchLiveStatus();
    } else {
      appendLog(`Gagal menutup posisi: ${data.message || data.error}`, "error");
    }
  } catch (e) {
    appendLog(`Error menutup posisi: ${e}`, "error");
  }
}

function copyTerminalLogs() {
  const text = logTerminal.innerText;
  navigator.clipboard.writeText(text).then(() => {
    appendLog("📋 Log terminal berhasil disalin ke clipboard!", "success");
  }).catch(() => {
    appendLog("Gagal menyalin log terminal.", "error");
  });
}

function refreshTradingData() {
  fetchPositions();
  fetchOpenOrders();
  if (currentActiveTradeTab === "tab-order-history") fetchOrderHistory();
  if (currentActiveTradeTab === "tab-trade-history") fetchTradeHistory();
}

// ==========================================
// BINANCE MARKET PAIRS MODAL / DRAWER LOGIC
// ==========================================
let allBinanceMarketPairs = [];
let currentMarketCategory = "ALL";
let isFetchingMarketPairs = false;

async function openCoinDrawer() {
  const modal = document.getElementById("binance-market-modal");
  if (!modal) return;
  modal.style.display = "flex";
  
  const searchInput = document.getElementById("market-search-input");
  if (searchInput) {
    searchInput.value = "";
    setTimeout(() => searchInput.focus(), 80);
  }

  if (allBinanceMarketPairs.length === 0 && !isFetchingMarketPairs) {
    await fetchAllMarketPairs();
  } else {
    filterMarketPairs();
  }
}

function closeCoinDrawer() {
  const modal = document.getElementById("binance-market-modal");
  if (modal) modal.style.display = "none";
}

async function fetchAllMarketPairs(force = false) {
  isFetchingMarketPairs = true;
  const listEl = document.getElementById("market-pairs-list");
  if (listEl && allBinanceMarketPairs.length === 0) {
    listEl.innerHTML = `
      <div style="text-align: center; color: #848e9c; padding: 3rem 1rem;">
        <div style="font-size: 2rem; margin-bottom: 0.5rem;">⏳</div>
        <div>Memuat 700+ pair Binance Futures & Funding Rate...</div>
      </div>
    `;
  }

  try {
    const res = await fetch(`/api/market/all_pairs?force=${force}`);
    const data = await res.json();
    if (data.success && Array.isArray(data.pairs)) {
      allBinanceMarketPairs = data.pairs;
      const badge = document.getElementById("market-total-badge");
      if (badge) badge.textContent = `${allBinanceMarketPairs.length} Pairs`;
      filterMarketPairs();
    } else {
      if (listEl) listEl.innerHTML = `<div style="text-align: center; color: var(--accent-red); padding: 2rem;">Gagal memuat pasar: ${data.error || 'Unknown error'}</div>`;
    }
  } catch (e) {
    if (listEl) listEl.innerHTML = `<div style="text-align: center; color: var(--accent-red); padding: 2rem;">Error: ${e}</div>`;
  } finally {
    isFetchingMarketPairs = false;
  }
}

function setMarketCategory(cat) {
  currentMarketCategory = cat;
  document.querySelectorAll("#market-category-pills button").forEach(btn => {
    if (btn.dataset.cat === cat) {
      btn.style.background = "rgba(6,182,212,0.2)";
      btn.style.borderColor = "var(--accent-cyan)";
      btn.style.color = "var(--accent-cyan)";
    } else {
      btn.style.background = "";
      btn.style.borderColor = "";
      btn.style.color = "";
    }
  });
  filterMarketPairs();
}

function filterMarketPairs() {
  const searchInput = document.getElementById("market-search-input");
  const query = searchInput ? searchInput.value.trim().toUpperCase() : "";
  const listEl = document.getElementById("market-pairs-list");
  if (!listEl) return;

  if (allBinanceMarketPairs.length === 0) return;

  let filtered = allBinanceMarketPairs;

  if (currentMarketCategory === "USDT") {
    filtered = filtered.filter(p => p.quote === "USDT");
  } else if (currentMarketCategory === "USDC") {
    filtered = filtered.filter(p => p.quote === "USDC");
  } else if (currentMarketCategory === "GOLD") {
    filtered = filtered.filter(p => p.is_gold || p.symbol.includes("XAU") || p.symbol.includes("PAXG") || p.symbol.includes("GOLD"));
  } else if (currentMarketCategory === "GAINERS") {
    filtered = [...filtered].filter(p => p.change_pct > 0).sort((a,b) => b.change_pct - a.change_pct);
  } else if (currentMarketCategory === "VOLUME") {
    filtered = [...filtered].sort((a,b) => b.volume_usdt - a.volume_usdt);
  }

  if (query) {
    filtered = filtered.filter(p => p.symbol.toUpperCase().includes(query) || p.base.toUpperCase().includes(query));
  }

  if (filtered.length === 0) {
    const fallbackSym = query.includes("/") ? query : `${query}/USDT`;
    listEl.innerHTML = `
      <div style="text-align: center; color: #848e9c; padding: 2.5rem 1rem;">
        <div>Tidak ditemukan koin dengan kata kunci "<strong>${query}</strong>"</div>
        <div style="font-size: 0.82rem; margin-top: 0.5rem; color: var(--accent-cyan); cursor: pointer;" onclick="selectCustomSymbolFromSearch('${query}')">
          👉 Gunakan "${fallbackSym}" sebagai target koin
        </div>
      </div>
    `;
    return;
  }

  const displayPairs = filtered.slice(0, 100);
  let html = "";
  displayPairs.forEach(p => {
    const isUp = p.change_pct >= 0;
    const chgColor = isUp ? "#0ecb81" : "#f6465d";
    const chgSign = isUp ? "+" : "";
    const isSelected = (currentSymbol === p.symbol || (inputSymbol && inputSymbol.value === p.symbol));
    const frColor = p.funding_rate >= 0 ? "#848e9c" : "#f0b90b";
    const frSign = p.funding_rate > 0 ? "+" : "";
    const quoteLabel = p.quote || "USDT";

    html += `
      <div class="market-row" style="padding: 0.55rem 1.4rem; display: grid; grid-template-columns: 2.2fr 1.3fr 1.2fr 1.2fr 0.9fr; align-items: center; border-bottom: 1px solid rgba(255,255,255,0.04); cursor: pointer; transition: background 0.15s; ${isSelected ? 'background: rgba(6,182,212,0.12);' : ''}" onmouseover="this.style.background='#2b313a'" onmouseout="this.style.background='${isSelected ? 'rgba(6,182,212,0.12)' : 'transparent'}'" onclick="selectSymbolFromModal('${p.symbol}')">
        <div>
          <div style="display: flex; align-items: center; gap: 0.4rem;">
            <span style="font-weight: 700; color: #eaecef; font-size: 0.9rem;">${p.base}</span>
            <span style="font-size: 0.72rem; color: #848e9c;">/${quoteLabel} Perp</span>
            ${p.is_gold ? '<span style="font-size: 0.65rem; background: rgba(240,185,11,0.2); color: #f0b90b; padding: 1px 4px; border-radius: 3px;">GOLD</span>' : ''}
            ${p.quote === 'USDC' ? '<span style="font-size: 0.65rem; background: rgba(39,117,202,0.25); color: #3b82f6; padding: 1px 4px; border-radius: 3px;">USDC</span>' : ''}
          </div>
          <div style="font-size: 0.72rem; color: #848e9c; margin-top: 1px;">Vol: $${p.volume_str}</div>
        </div>
        <div class="mono" style="text-align: right; font-weight: 600; color: #eaecef; font-size: 0.88rem;">
          $${p.price < 0.1 ? p.price.toFixed(6) : (p.price < 1 ? p.price.toFixed(4) : p.price.toFixed(2))}
        </div>
        <div class="mono" style="text-align: right; font-weight: 700; color: ${chgColor}; font-size: 0.85rem;">
          ${chgSign}${p.change_pct}%
        </div>
        <div class="mono" style="text-align: right; font-size: 0.78rem; color: ${frColor};">
          ${frSign}${p.funding_rate.toFixed(4)}%
        </div>
        <div style="text-align: center;">
          <button type="button" class="quick-btn" style="padding: 0.25rem 0.55rem; font-size: 0.75rem; border-color: ${isSelected ? 'var(--accent-cyan)' : 'rgba(255,255,255,0.15)'}; color: ${isSelected ? 'var(--accent-cyan)' : '#eaecef'};" onclick="event.stopPropagation(); selectSymbolFromModal('${p.symbol}')">
            ${isSelected ? '✓ Aktif' : 'Pilih'}
          </button>
        </div>
      </div>
    `;
  });

  listEl.innerHTML = html;
}

function selectSymbolFromModal(sym) {
  closeCoinDrawer();
  selectSymbol(sym);
}

function selectCustomSymbolFromSearch(query) {
  if (!query) return;
  let clean = query.trim().toUpperCase();
  if (!clean.includes("/")) {
    const defaultQuote = (typeof currentMarketCategory !== "undefined" && currentMarketCategory === "USDC") ? "USDC" : "USDT";
    if (clean.endsWith("USDT")) clean = clean.slice(0, -4) + "/USDT";
    else if (clean.endsWith("USDC")) clean = clean.slice(0, -4) + "/USDC";
    else clean = clean + "/" + defaultQuote;
  }
  closeCoinDrawer();
  selectSymbol(clean);
}

function selectAutoPickFromModal() {
  closeCoinDrawer();
  toggleAutoPick(true);
}

// Expose functions globally on window object
window.openCoinDrawer = openCoinDrawer;
window.closeCoinDrawer = closeCoinDrawer;
window.setMarketCategory = setMarketCategory;
window.filterMarketPairs = filterMarketPairs;
window.selectSymbolFromModal = selectSymbolFromModal;
window.selectCustomSymbolFromSearch = selectCustomSymbolFromSearch;
window.selectAutoPickFromModal = selectAutoPickFromModal;
window.switchTradeTab = switchTradeTab;
window.refreshTradingData = refreshTradingData;
window.cancelOrder = cancelOrder;
window.cancelAllOrders = cancelAllOrders;
window.closePosition = closePosition;
window.copyTerminalLogs = copyTerminalLogs;
window.setMode = setMode;
window.setQuoteAsset = setQuoteAsset;
window.handleStartBot = handleStartBot;
window.handleStopBot = handleStopBot;
window.clearLogs = clearLogs;
window.refreshScanner = refreshScanner;
window.selectSymbol = selectSymbol;
window.onHubFilterChange = onHubFilterChange;
window.closeSwitchModal = closeSwitchModal;
window.confirmSwitchCoin = confirmSwitchCoin;
window.filterPnlLogs = filterPnlLogs;
window.clearPnlLogs = clearPnlLogs;
window.copyPnlLogs = copyPnlLogs;

// Initial Boot
document.addEventListener("DOMContentLoaded", () => {
  // Bind tab click events explicitly
  document.querySelectorAll(".trade-tab-btn").forEach(btn => {
    btn.addEventListener("click", (e) => {
      e.preventDefault();
      switchTradeTab(btn.dataset.tab);
    });
  });

  // 1. Immediately fetch status
  fetchLiveStatus();
  
  // 2. Init websocket
  initWebSocket();

  // 3. Init scanner
  refreshScanner();

  // 4. Init trading data hub (positions & open orders)
  refreshTradingData();

  // 5. Smart Polling intervals (WebSocket handles real-time streams; HTTP only serves as gentle fallback)
  setInterval(() => {
    const isWsAlive = ws && ws.readyState === WebSocket.OPEN;
    if (!isWsAlive) fetchLiveStatus();
  }, 5000);
  setInterval(fetchLiveStatus, 15000);
  setInterval(refreshTradingData, 8000);
});
