import requests

print("=== Hyperliquid leaderboard test ===")
try:
    r = requests.get("https://stats-data.hyperliquid.xyz/Mainnet/leaderboard", timeout=30)
    print(f"  status: {r.status_code}, size: {len(r.content)//1024} KB")
    if r.status_code == 200:
        data = r.json()
        rows = data.get("leaderboardRows", [])
        print(f"  total traders: {len(rows)}")
        if rows:
            sample = rows[0]
            print(f"  sample keys: {list(sample.keys())[:6]}")
            print(f"  sample address: {sample.get('ethAddress','')[:20]}")
            perfs = sample.get("windowPerformances", [])
            print(f"  windowPerformances count: {len(perfs)}")
            if perfs:
                print(f"  perf entry shape: {perfs[0]}")
except Exception as e:
    print(f"  ERROR: {e}")

print()
print("=== Hyperliquid info endpoint test ===")
try:
    r = requests.post("https://api.hyperliquid.xyz/info", json={"type": "allMids"}, timeout=10)
    print(f"  status: {r.status_code}")
    if r.status_code == 200:
        mids = r.json()
        print(f"  mid prices count: {len(mids)}")
        print(f"  BTC={mids.get('BTC')} ETH={mids.get('ETH')} SOL={mids.get('SOL')}")
except Exception as e:
    print(f"  ERROR: {e}")

print()
print("=== Test reading one trader's positions ===")
try:
    if rows:
        wallet = rows[0]["ethAddress"]
        r = requests.post("https://api.hyperliquid.xyz/info",
                          json={"type": "clearinghouseState", "user": wallet}, timeout=10)
        print(f"  status: {r.status_code}")
        if r.status_code == 200:
            state = r.json()
            asset_pos = state.get("assetPositions", [])
            print(f"  account value: ${float(state.get('marginSummary', {}).get('accountValue', 0)):.2f}")
            print(f"  open positions: {len(asset_pos)}")
            for ap in asset_pos[:3]:
                p = ap.get("position", {})
                print(f"    {p.get('coin')}: szi={p.get('szi')} entry=${p.get('entryPx')} "
                      f"lev={p.get('leverage',{}).get('value')}x upnl=${p.get('unrealizedPnl')}")
except Exception as e:
    print(f"  ERROR: {e}")
