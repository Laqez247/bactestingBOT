import pandas as pd

df = pd.read_csv('results/trades_V4_FINAL.csv')

# CRITICAL CHECK: How are wins classified?
wins = df[df['r_multiple']>0]
losses = df[df['r_multiple']<0]
be = df[df['r_multiple']==0]

print('=== WIN/LOSS Classification ===')
print(f'Total: {len(df)}')
print(f'Wins (r>0): {len(wins)} ({len(wins)/len(df)*100:.1f}%)')
print(f'Losses (r<0): {len(losses)} ({len(losses)/len(df)*100:.1f}%)')
print(f'Breakeven (r=0): {len(be)} ({len(be)/len(df)*100:.1f}%)')

# CRITICAL: TP1-hit + SL(breakeven) = ~0.5R -> counted as WIN
tp1_be = df[(df['tp1_hit']==True) & (df['exit_reason']=='SL')]
print(f'\nTP1-hit + SL-at-breakeven: {len(tp1_be)} trades -> ALL counted as WINS (r~0.5)')

# Without these trades, what is the real WR?
real_trades = df[~((df['tp1_hit']==True) & (df['exit_reason']=='SL'))]
real_wins = real_trades[real_trades['r_multiple']>0]
print(f'\n=== ADJUSTED WIN RATE (excluding TP1+BE as separate category) ===')
print(f'Clean trades: {len(real_trades)}')
print(f'Clean wins: {len(real_wins)} ({len(real_wins)/len(real_trades)*100:.1f}%)')

# TP2 full wins
tp2_wins = df[df['exit_reason']=='TP2']
r_col = 'r_multiple'
print(f'\nTP2 full wins: {len(tp2_wins)} ({len(tp2_wins)/len(df)*100:.1f}%)')
print(f'  Avg R on TP2 wins: {tp2_wins[r_col].mean():.3f}')

# Pure SL losses (no TP1 hit)
pure_sl = df[(df['exit_reason']=='SL') & (df['tp1_hit']==False)]
print(f'\nPure SL losses (no TP1): {len(pure_sl)} ({len(pure_sl)/len(df)*100:.1f}%)')
print(f'  Avg R: {pure_sl[r_col].mean():.3f}')

# Sanity: total R
timeout_r = df[df['exit_reason']=='TIMEOUT'][r_col].sum()
print(f'\nTotal R earned: {df[r_col].sum():.3f}')
print(f'  From TP2 wins: {tp2_wins[r_col].sum():.3f}')
print(f'  From TP1+BE trades: {tp1_be[r_col].sum():.3f}')
print(f'  From pure SL losses: {pure_sl[r_col].sum():.3f}')
print(f'  From timeouts: {timeout_r:.3f}')

# Check for SUSPICIOUS patterns
print('\n=== SUSPICIOUS PATTERN CHECKS ===')

# 1. Do any trades have entry_bar BEFORE breakout_bar? (lookahead)
lookahead = df[df['entry_bar'] < df['breakout_bar']]
print(f'Entries before breakout bar: {len(lookahead)} (should be 0)')

# 2. Check zone type distribution
print(f'\nZone type distribution:')
for zt, sub in df.groupby('retest_zone_type'):
    wr = len(sub[sub[r_col]>0])/len(sub)*100
    print(f'  {zt}: n={len(sub)}, WR={wr:.1f}%, avg_r={sub[r_col].mean():.3f}')

# 3. Sweep type distribution
print(f'\nSweep type distribution:')
for st, sub in df.groupby('ssl_bsl_sweep_type'):
    wr = len(sub[sub[r_col]>0])/len(sub)*100
    print(f'  {st}: n={len(sub)}, WR={wr:.1f}%, avg_r={sub[r_col].mean():.3f}')

# 4. Check entry_price vs zone boundaries
print(f'\n=== ENTRY vs ZONE CHECK ===')
for _, row in df.head(20).iterrows():
    if row['direction'] == 'LONG':
        in_zone = row['retest_zone_bottom'] <= row['entry_price'] <= row['retest_zone_top'] + 2
    else:
        in_zone = row['retest_zone_bottom'] - 2 <= row['entry_price'] <= row['retest_zone_top']
    if not in_zone:
        print(f'  WARNING: {row["setup_id"]} entry={row["entry_price"]} outside zone [{row["retest_zone_bottom"]}, {row["retest_zone_top"]}]')

# 5. Time distribution (checking for clustering)
print(f'\n=== MONTHLY TRADE DISTRIBUTION ===')
df['timestamp'] = pd.to_datetime(df['timestamp'])
monthly = df.groupby(df['timestamp'].dt.to_period('M')).agg(
    n=(r_col, 'count'),
    wr=(r_col, lambda x: (x>0).mean()*100),
    total_r=(r_col, 'sum')
)
print(monthly.to_string())
