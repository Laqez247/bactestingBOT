"""
audit_check.py — Integrity check for backtest result CSV files.

Usage:
    cd backtest && python audit_check.py                            # auto-finds latest trades CSV
    cd backtest && python audit_check.py results/trades_ITER_99_PHASE2_V2_CONFLUENCE.csv
"""
import sys
import os
import glob
import pandas as pd


def find_latest_trades_csv():
    """Find the most-recently modified trades_*.csv in results/."""
    pattern = os.path.join("results", "trades_ITER_*.csv")
    files = glob.glob(pattern)
    if not files:
        # Fall back to V4_FINAL for backward compat
        fallback = os.path.join("results", "trades_V4_FINAL.csv")
        if os.path.exists(fallback):
            return fallback
        raise FileNotFoundError(f"No trades CSV found in results/")
    return max(files, key=os.path.getmtime)


# Accept optional CLI argument for file path
if len(sys.argv) > 1:
    csv_path = sys.argv[1]
else:
    csv_path = find_latest_trades_csv()

print(f"Auditing: {csv_path}\n")
df = pd.read_csv(csv_path)

r_col = "r_multiple"

# CRITICAL CHECK: How are wins classified?
wins   = df[df[r_col] > 0]
losses = df[df[r_col] < 0]
be     = df[df[r_col] == 0]

print("=== WIN/LOSS Classification ===")
print(f"Total: {len(df)}")
print(f"Wins (r>0): {len(wins)} ({len(wins)/len(df)*100:.1f}%)")
print(f"Losses (r<0): {len(losses)} ({len(losses)/len(df)*100:.1f}%)")
print(f"Breakeven (r=0): {len(be)} ({len(be)/len(df)*100:.1f}%)")

# CRITICAL: TP1-hit + SL(breakeven) = ~0.5R -> counted as WIN
tp1_col = "tp1_hit" if "tp1_hit" in df.columns else None
if tp1_col:
    tp1_be = df[(df[tp1_col] == True) & (df["exit_reason"] == "SL")]
    print(f"\nTP1-hit + SL-at-breakeven: {len(tp1_be)} trades -> ALL counted as WINS (r~0.5)")
    real_trades = df[~((df[tp1_col] == True) & (df["exit_reason"] == "SL"))]
    real_wins   = real_trades[real_trades[r_col] > 0]
    print(f"\n=== ADJUSTED WIN RATE (excluding TP1+BE as separate category) ===")
    print(f"Clean trades: {len(real_trades)}")
    print(f"Clean wins: {len(real_wins)} ({len(real_wins)/len(real_trades)*100:.1f}%)")
else:
    tp1_be = df[0:0]  # empty

# TP2 full wins
tp2_wins = df[df["exit_reason"] == "TP2"]
print(f"\nTP2 full wins: {len(tp2_wins)} ({len(tp2_wins)/len(df)*100:.1f}%)")
print(f"  Avg R on TP2 wins: {tp2_wins[r_col].mean():.3f}")

# Pure SL losses (no TP1 hit)
if tp1_col:
    pure_sl = df[(df["exit_reason"] == "SL") & (df[tp1_col] == False)]
else:
    pure_sl = df[df["exit_reason"] == "SL"]
print(f"\nPure SL losses (no TP1): {len(pure_sl)} ({len(pure_sl)/len(df)*100:.1f}%)")
print(f"  Avg R: {pure_sl[r_col].mean():.3f}")

# Sanity: total R
timeout_r = df[df["exit_reason"] == "TIMEOUT"][r_col].sum()
print(f"\nTotal R earned: {df[r_col].sum():.3f}")
print(f"  From TP2 wins: {tp2_wins[r_col].sum():.3f}")
print(f"  From TP1+BE trades: {tp1_be[r_col].sum():.3f}")
print(f"  From pure SL losses: {pure_sl[r_col].sum():.3f}")
print(f"  From timeouts: {timeout_r:.3f}")

# Phase 2 override summary
if "override_type" in df.columns:
    print("\n=== PHASE 2 OVERRIDE BREAKDOWN ===")
    for ov_type, sub in df.groupby("override_type"):
        ov_wins = len(sub[sub[r_col] > 0])
        ov_wr   = ov_wins / len(sub) * 100
        ov_exp  = sub[r_col].mean()
        print(f"  {ov_type:<35} n={len(sub):>3}  WR={ov_wr:.1f}%  Avg_R={ov_exp:+.3f}")

print("\n=== SUSPICIOUS PATTERN CHECKS ===")

# 1. Lookahead: entry_bar before breakout_bar?
if "entry_bar" in df.columns and "breakout_bar" in df.columns:
    lookahead = df[df["entry_bar"] < df["breakout_bar"]]
    print(f"Entries before breakout bar: {len(lookahead)} (should be 0)")

# 2. Zone type distribution
print(f"\nZone type distribution:")
for zt, sub in df.groupby("retest_zone_type"):
    wr = len(sub[sub[r_col] > 0]) / len(sub) * 100
    print(f"  {zt}: n={len(sub)}, WR={wr:.1f}%, avg_r={sub[r_col].mean():.3f}")

# 3. Sweep type distribution
print(f"\nSweep type distribution:")
for st, sub in df.groupby("ssl_bsl_sweep_type"):
    wr = len(sub[sub[r_col] > 0]) / len(sub) * 100
    print(f"  {st}: n={len(sub)}, WR={wr:.1f}%, avg_r={sub[r_col].mean():.3f}")

# 4. Entry vs zone boundary check (first 20 trades)
print(f"\n=== ENTRY vs ZONE CHECK ===")
flagged = 0
for _, row in df.iterrows():
    if row["direction"] == "LONG":
        in_zone = row["retest_zone_bottom"] <= row["entry_price"] <= row["retest_zone_top"] + 2
    else:
        in_zone = row["retest_zone_bottom"] - 2 <= row["entry_price"] <= row["retest_zone_top"]
    if not in_zone:
        print(f"  WARNING: {row['setup_id']} entry={row['entry_price']} outside zone "
              f"[{row['retest_zone_bottom']}, {row['retest_zone_top']}]")
        flagged += 1
if flagged == 0:
    print("  All entries within expected zone range. ✓")

# 5. Monthly distribution
print(f"\n=== MONTHLY TRADE DISTRIBUTION ===")
df["timestamp"] = pd.to_datetime(df["timestamp"])
monthly = df.groupby(df["timestamp"].dt.to_period("M")).agg(
    n=(r_col, "count"),
    wr=(r_col, lambda x: (x > 0).mean() * 100),
    total_r=(r_col, "sum")
)
print(monthly.to_string())
