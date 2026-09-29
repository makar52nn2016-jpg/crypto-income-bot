#!/usr/bin/env python3
"""
Multi-Strategy Crypto Income Bot
Combines 5 income sources:
1. Funding Rate Arbitrage (BingX/Bybit/OKX) — 10-30% APY, delta-neutral
2. Cross-Exchange Spreads (4 exchanges) — buy low, sell high
3. Base Chain DEX Arbitrage (Aerodrome vs Uniswap) — AERO/WETH spread
4. GitHub Bounty Monitor (agent-bounties) — paid tasks $0.90-20 USDC
5. Clanker Token Monitor — new Base memecoin launches

Run: python3 bot.py
"""

import ccxt
import json
import time
import os
import requests
from datetime import datetime, timezone

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
MIN_FUNDING_APY = 10.0
MIN_CROSS_SPREAD = 0.05
MIN_DEX_SPREAD = 0.30
SCAN_INTERVAL = 300  # 5 minutes

ALCHEMY_BASE = "https://base-mainnet.g.alchemy.com/v2/alch_BUo0TYqkD24rLEzrz4U3n"
WETH = "0x4200000000000000000000000000000000000006"
USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
AERO = "0x940181a94a35a4569e4529a3cdfb74e38fd98631"
AERO_FACTORY = "0x420DD381b31aEf6683db6B902084cB0FFECe40Da"
UNI_V3_FACTORY = "0x33128a8fC17869897dcE68Ed026d694621f6FDfD"
ZERO_ADDR = "0x0000000000000000000000000000000000000000"

def stamp():
    return datetime.now(timezone.utc).strftime("%H:%M:%S")

def send_telegram(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                      json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"}, timeout=10)
    except: pass

def eth_call(to, data):
    try:
        r = requests.post(ALCHEMY_BASE, json={"jsonrpc": "2.0", "method": "eth_call", "params": [{"to": to, "data": data}, "latest"], "id": 1})
        j = r.json()
        if j.get("error") or not j.get("result") or j["result"] == "0x": return None
        return j["result"]
    except: return None

# === STRATEGY 1: FUNDING RATE ARBITRAGE ===
def scan_funding_rates():
    results = []
    exchanges = {
        "BingX": ccxt.bingx({"enableRateLimit": True}),
        "Bybit": ccxt.bybit({"enableRateLimit": True, "options": {"defaultType": "linear"}}),
        "OKX": ccxt.okx({"enableRateLimit": True, "options": {"defaultType": "swap"}}),
    }
    pairs = ["BTC/USDT:USDT","ETH/USDT:USDT","SOL/USDT:USDT","XRP/USDT:USDT","DOGE/USDT:USDT",
             "BNB/USDT:USDT","ADA/USDT:USDT","AVAX/USDT:USDT","LINK/USDT:USDT","DOT/USDT:USDT",
             "LTC/USDT:USDT","BCH/USDT:USDT","NEAR/USDT:USDT","APT/USDT:USDT","OP/USDT:USDT",
             "ARB/USDT:USDT","INJ/USDT:USDT","SUI/USDT:USDT","SEI/USDT:USDT","TIA/USDT:USDT",
             "FIL/USDT:USDT","ATOM/USDT:USDT","JUP/USDT:USDT","PEPE/USDT:USDT","WIF/USDT:USDT"]
    
    for ex_name, ex in exchanges.items():
        try:
            ex.load_markets()
            for pair in pairs:
                try:
                    fr = ex.fetch_funding_rate(pair)
                    rate = float(fr.get("fundingRate", 0))
                    if abs(rate) > 0.0001:
                        annual = rate * 1095 * 100
                        if abs(annual) >= MIN_FUNDING_APY:
                            results.append({
                                "exchange": ex_name, "pair": pair,
                                "rate_8h": rate * 100, "annual_pct": annual,
                                "strategy": "Buy spot + Short perp" if rate > 0 else "Short spot + Long perp",
                                "daily_50_10x": abs(50 * 10 * rate * 3),
                                "monthly_50_10x": abs(50 * 10 * rate * 3 * 30),
                            })
                except: pass
        except: pass
    
    results.sort(key=lambda x: abs(x["annual_pct"]), reverse=True)
    return results

# === STRATEGY 2: CROSS-EXCHANGE SPREADS ===
def scan_cross_exchange():
    results = []
    exchanges = {
        "BingX": ccxt.bingx({"enableRateLimit": True}),
        "KuCoin": ccxt.kucoin({"enableRateLimit": True}),
        "OKX": ccxt.okx({"enableRateLimit": True}),
        "Bybit": ccxt.bybit({"enableRateLimit": True}),
    }
    coins = ["BTC/USDT","ETH/USDT","SOL/USDT","XRP/USDT","DOGE/USDT","BNB/USDT","ADA/USDT","AVAX/USDT","LINK/USDT","DOT/USDT"]
    prices = {}
    for coin in coins:
        prices[coin] = {}
        for ex_name, ex in exchanges.items():
            try:
                t = ex.fetch_ticker(coin)
                prices[coin][ex_name] = float(t["last"])
            except: pass
    for coin, ex_prices in prices.items():
        if len(ex_prices) < 2: continue
        sorted_ex = sorted(ex_prices.items(), key=lambda x: x[1])
        min_ex, min_price = sorted_ex[0]
        max_ex, max_price = sorted_ex[-1]
        spread = (max_price - min_price) / min_price * 100
        if spread >= MIN_CROSS_SPREAD:
            results.append({"coin": coin, "buy": min_ex, "sell": max_ex,
                           "buy_price": min_price, "sell_price": max_price,
                           "spread_pct": spread, "profit_50": 50 * spread / 100})
    results.sort(key=lambda x: x["spread_pct"], reverse=True)
    return results

# === STRATEGY 3: BASE CHAIN DEX ARBITRAGE ===
def scan_base_dex_arb():
    results = []
    pairs = [("AERO/WETH", AERO, WETH, 18, 18), ("WETH/USDC", WETH, USDC, 18, 6)]
    for name, token_in, token_out, in_dec, out_dec in pairs:
        try:
            aero_price = 0; aero_tvl = 0
            for stable in [False, True]:
                sh = "1" if stable else "0"
                data = "0x79bc57d5" + token_in.lower()[2:].zfill(64) + token_out.lower()[2:].zfill(64) + "0" * 63 + sh
                r = eth_call(AERO_FACTORY, data)
                if r:
                    pool = "0x" + r[-40:]
                    if pool != ZERO_ADDR:
                        t0 = eth_call(pool, "0x0dfe1681")
                        rr = eth_call(pool, "0x0902f1ac")
                        if t0 and rr:
                            t0a = "0x" + t0[-40:].lower()
                            h = rr[2:]
                            r0 = int(h[:64], 16); r1 = int(h[64:128], 16)
                            in0 = t0a == token_in.lower()
                            ri = r0 if in0 else r1; ro = r1 if in0 else r0
                            if ri > 0:
                                aero_price = (ro / 10**out_dec) / (ri / 10**in_dec)
                                aero_tvl = 2 * (r1 if in0 else r0) / 10**out_dec * 2650
                                break
            uni_price = 0
            for fee in [500, 3000, 10000]:
                fee_hex = hex(fee)[2:].zfill(6)
                data = "0x1698ee82" + token_in.lower()[2:].zfill(64) + token_out.lower()[2:].zfill(64) + "0" * 58 + fee_hex
                r = eth_call(UNI_V3_FACTORY, data)
                if r:
                    pool = "0x" + r[-40:]
                    if pool != ZERO_ADDR:
                        s0 = eth_call(pool, "0x3850c7bd")
                        if s0:
                            sq = int(s0[2:66], 16)
                            if sq > 0:
                                raw = (sq * sq) / (2**192)
                                t0 = eth_call(pool, "0x0dfe1681")
                                if t0:
                                    in0 = "0x" + t0[-40:].lower() == token_in.lower()
                                    adj = 10 ** (in_dec - out_dec)
                                    uni_price = raw * adj if in0 else (1/raw) * adj
                                    break
            if aero_price > 0 and uni_price > 0:
                spread = abs(aero_price - uni_price) / min(aero_price, uni_price) * 100
                net = spread - 0.3
                if net > 0 and net < 100:
                    results.append({"pair": name, "aero_price": aero_price, "uni_price": uni_price,
                                   "spread_pct": spread, "net_spread_pct": net,
                                   "buy_dex": "Aerodrome" if aero_price < uni_price else "Uniswap",
                                   "sell_dex": "Uniswap" if aero_price < uni_price else "Aerodrome",
                                   "tvl": aero_tvl, "profit_flash_500": 500 * net / 100 - 0.25 - 0.01})
        except: pass
    results.sort(key=lambda x: x.get("net_spread_pct", 0), reverse=True)
    return results

# === STRATEGY 4: GITHUB BOUNTIES ===
def scan_github_bounties():
    results = []
    try:
        r = requests.get("https://api.agentbounties.app/v1/base/autonomous-bounties/feed?network=base-mainnet", timeout=15)
        data = r.json()
        bounties = data if isinstance(data, list) else data.get("bounties", data.get("items", []))
        for b in bounties:
            reward = int(b.get("solver_reward", "0")) / 1e6
            status = b.get("status", "?")
            if status in ("claimable", "ready_to_earn", "escrowed") and reward > 0:
                results.append({"reward_usdc": reward, "status": status})
    except: pass
    return results

# === STRATEGY 5: CLANKER TOKENS ===
def scan_clanker_tokens():
    results = []
    try:
        r = requests.get("https://clanker.world/api/tokens?order=desc&limit=5", timeout=15)
        data = r.json()
        for t in data.get("data", [])[:5]:
            mcap = t.get("related", {}).get("market", {}).get("marketCap", 0)
            if mcap > 10000:
                results.append({"symbol": t.get("symbol", "?"), "market_cap": mcap,
                               "verified": t.get("tags", {}).get("verified", False)})
    except: pass
    return results

# === MAIN ===
def run_cycle():
    print(f"\n{'='*70}")
    print(f"📊 CRYPTO INCOME BOT — {stamp()}")
    print(f"{'='*70}")
    
    # 1. Funding
    print(f"\n--- 1. FUNDING RATE ARBITRAGE ---")
    funding = scan_funding_rates()
    print(f"   Found {len(funding)} opportunities (>={MIN_FUNDING_APY}% APY)")
    for f in funding[:5]:
        print(f"   🔥 {f['exchange']:5s} {f['pair']:20s} {f['annual_pct']:>6.1f}% APY | {f['strategy']} | ${f['daily_50_10x']:.4f}/day ($50@10x)")
    
    # 2. Cross-exchange
    print(f"\n--- 2. CROSS-EXCHANGE SPREADS ---")
    cross = scan_cross_exchange()
    print(f"   Found {len(cross)} spreads (>={MIN_CROSS_SPREAD}%)")
    for c in cross[:3]:
        print(f"   💱 {c['coin']:10s} {c['buy']:>5s}→{c['sell']:>5s} {c['spread_pct']:>6.3f}% | profit ${c['profit_50']:.4f}/$50")
    
    # 3. Base DEX
    print(f"\n--- 3. BASE CHAIN DEX ARBITRAGE ---")
    base_arb = scan_base_dex_arb()
    print(f"   Found {len(base_arb)} DEX spreads (>={MIN_DEX_SPREAD}%)")
    for b in base_arb[:3]:
        print(f"   ⚡ {b['pair']:10s} {b['buy_dex']:>10s}→{b['sell_dex']:>10s} {b['net_spread_pct']:>6.3f}% | flash profit ${b['profit_flash_500']:.4f}")
    
    # 4. Bounties
    print(f"\n--- 4. GITHUB BOUNTIES ---")
    bounties = scan_github_bounties()
    total_bounty = sum(b["reward_usdc"] for b in bounties)
    print(f"   Found {len(bounties)} bounties | Total: ${total_bounty:.2f} USDC")
    for b in bounties[:5]:
        print(f"   🏆 ${b['reward_usdc']:.2f} USDC | {b['status']}")
    
    # 5. Clanker
    print(f"\n--- 5. CLANKER NEW TOKENS ---")
    clanker = scan_clanker_tokens()
    print(f"   Found {len(clanker)} high-mcap tokens")
    for t in clanker[:3]:
        print(f"   🚀 {t['symbol']:8s} MCAP ${t['market_cap']:>10.0f} | {'✓' if t['verified'] else '⚠'}")
    
    # Summary
    fund_daily = sum(f['daily_50_10x'] for f in funding[:3])
    cross_daily = sum(c['profit_50'] for c in cross[:3]) * 2
    dex_daily = sum(b['profit_flash_500'] for b in base_arb[:3]) * 10
    
    print(f"\n{'='*70}")
    print(f"📊 DAILY INCOME POTENTIAL (with $50 capital, 10x leverage):")
    print(f"   Funding rates:    ${fund_daily:.4f}/day → ${fund_daily*30:.2f}/month")
    print(f"   Cross-exchange:   ${cross_daily:.4f}/day → ${cross_daily*30:.2f}/month")
    print(f"   Base DEX arb:     ${dex_daily:.4f}/day → ${dex_daily*30:.2f}/month")
    print(f"   Bounties:         ${total_bounty:.2f} one-time")
    print(f"   ─────────────────────────")
    total_daily = fund_daily + cross_daily + dex_daily
    print(f"   TOTAL:            ${total_daily:.4f}/day = ${total_daily*30:.2f}/month")
    print(f"   + Bounties:       ${total_bounty:.2f} one-time")
    print(f"   ─────────────────────────")
    print(f"   GRAND TOTAL:      ${total_daily*30 + total_bounty:.2f}/month")
    print(f"{'='*70}")
    
    # Telegram alerts
    high = [f for f in funding if abs(f['annual_pct']) > 20]
    if high:
        msg = "🔥 HIGH FUNDING RATE ALERT!\n\n"
        for f in high[:3]:
            msg += f"{f['exchange']} {f['pair']}: {f['annual_pct']:.1f}% APY\n  {f['strategy']}\n  Daily $50@10x: ${f['daily_50_10x']:.4f}\n\n"
        send_telegram(msg)

def main():
    print("=" * 70)
    print("🚀 CRYPTO INCOME BOT — Multi-Strategy Monitor")
    print("=" * 70)
    print("Strategies: 1.Funding Arb 2.Cross-Exchange 3.Base DEX 4.Bounties 5.Clanker")
    print(f"Scan: every {SCAN_INTERVAL}s | Telegram: {'✅' if TELEGRAM_TOKEN else '❌'}")
    print("=" * 70)
    while True:
        try: run_cycle()
        except Exception as e: print(f"[ERROR] {e}")
        time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    main()
