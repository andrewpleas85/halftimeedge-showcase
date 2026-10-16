#!/usr/bin/env python3
"""Recover the real price of settled bets from the raw Odds API snapshots.

WHY: bets_v2.ev_odds_decimal is empty for most live-loop bets, so units were
computed at an assumed -110. A live NHL puck line is routinely -475 for the +1.5
side, which turns a "60% winner" into a loser. run.py already stores the full
Odds API payload (with prices) in raw_odds_snapshots at halftime, so the price
that was actually on the board can be read back for each bet.

MATCHING (all must hold, otherwise the bet is reported UNMATCHED, never guessed):
  snapshot  : sport + game_id containing "<AWAY>@<HOME>" and the bet's date;
              phase 'halftime' (the FIRST price seen, closest to bet time)
  outcome   : the bet team's own outcome on the right market
  line      : outcome point == the bet's line (to 0.01). A snapshot whose line
              differs is reported as LINE_MOVED and not used for units.
  book      : fanduel first, then draftkings (same preference as run.py)

Usage:
  python scripts/backfill_bet_prices.py --strategy ML_HT_NHL_SPREAD CFB_LOW_IMPLIED_H2_OVER
  python scripts/backfill_bet_prices.py --strategy ... --apply   # writes ev_odds_* only
"""
import argparse, json, os, sqlite3, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, 'data', 'analysis.db')
REAL = "(dynamo_pk IS NULL OR dynamo_pk NOT LIKE 'OBS#%')"
BOOKS = ('fanduel', 'draftkings')


def am2dec(a):
    a = float(a)
    return 1 + a / 100.0 if a > 0 else 1 + 100.0 / abs(a)


def find_snapshot(con, sport, away, home, date):
    # game ids are written both as sport_AWAY@HOME_date and sport_date_AWAY@HOME
    pats = (f"{sport}_{away}@{home}_{date}", f"{sport}_{date}_{away}@{home}")
    for ph in ('halftime', 'halftime_late'):
        for p in pats:
            r = con.execute("""SELECT raw_json FROM raw_odds_snapshots
                               WHERE sport=? AND phase=? AND game_id=? LIMIT 1""",
                            (sport, ph, p)).fetchone()
            if r:
                return ph, r[0]
    return None, None


def price_for(ev, market, name, point):
    """(american_price, book, point_used, exact) for one outcome."""
    best = None
    for bk in BOOKS:
        for b in ev.get('bookmakers', []):
            if b.get('key') != bk:
                continue
            for m in b.get('markets', []):
                if m.get('key') != market:
                    continue
                for o in m.get('outcomes', []):
                    if o.get('name') != name or o.get('price') is None:
                        continue
                    pt = o.get('point')
                    if pt is not None and point is not None and abs(float(pt) - point) < 0.01:
                        return o['price'], bk, float(pt), True
                    if best is None:
                        best = (o['price'], bk, pt, False)
    return best


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--strategy', nargs='+', required=True)
    ap.add_argument('--apply', action='store_true')
    a = ap.parse_args()
    con = sqlite3.connect(DB, timeout=60)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA busy_timeout=60000')
    for sk in a.strategy:
        rows = [dict(r) for r in con.execute(f"""
            SELECT id,sport,game_date,home_team,away_team,bet_team,bet_side,bet_type,
                   bet_line,bet_result,pnl_units,ev_odds_decimal
            FROM bets_v2 WHERE {REAL} AND strategy_key=? AND bet_result IN ('W','L')
            ORDER BY game_date""", (sk,))]
        print(f"\n=== {sk}: {len(rows)} settled bets ===")
        stat = dict(matched=0, nosnap=0, nooutcome=0, moved=0)
        w = l = 0; units = stored = be_sum = 0.0
        examples = []
        for r in rows:
            sport = (r['sport'] or '').lower()
            ph, raw = find_snapshot(con, sport, r['away_team'], r['home_team'], r['game_date'])
            if not raw:
                stat['nosnap'] += 1; continue
            try:
                d = json.loads(raw); ev = (d.get('data') or [d])[0]
            except Exception:
                stat['nosnap'] += 1; continue
            hn, an = ev.get('home_team'), ev.get('away_team')
            side = (r['bet_side'] or '').lower(); bt = (r['bet_team'] or '').upper()
            line = r['bet_line']
            if r['bet_type'] == 'total':
                market = 'totals'
                name = 'Over' if side == 'over' else 'Under'
                pt = line
            else:
                market = 'spreads'
                is_home = side == 'home' or bt == 'HOME' or bt == (r['home_team'] or '').upper()
                name = hn if is_home else an
                pt = line if is_home else (-line if line is not None else None)
            pr = price_for(ev, market, name, pt) if line is not None else None
            if not pr:
                stat['nooutcome'] += 1; continue
            price, book, pt_used, exact = pr
            if not exact:
                stat['moved'] += 1; continue
            dec = am2dec(price)
            stat['matched'] += 1
            win = r['bet_result'] == 'W'
            w += win; l += (not win)
            units += (dec - 1) if win else -1.0
            stored += r['pnl_units'] or 0.0
            be_sum += 1.0 / dec
            if len(examples) < 4:
                examples.append((r['game_date'], r['away_team'], r['home_team'], side or bt, line, price, book, r['bet_result']))
            if a.apply and not r['ev_odds_decimal']:
                con.execute("""UPDATE bets_v2 SET ev_odds_decimal=?, ev_odds_source='raw_snapshot_backfill',
                               ev_odds_book=? WHERE id=?""", (round(dec, 6), book, r['id']))
        n = stat['matched']
        print(f"  matched to a real price: {n}/{len(rows)}   "
              f"(no snapshot {stat['nosnap']}, outcome absent {stat['nooutcome']}, line moved {stat['moved']})")
        if n:
            print(f"  record on matched bets: {w}-{l}  win {100*w/n:.1f}%   average breakeven {100*be_sum/n:.1f}%")
            print(f"  units at the REAL price: {units:+.2f}u     (same bets, stored/assumed: {stored:+.2f}u)")
            for e in examples:
                print(f"    e.g. {e}")
    if a.apply:
        con.commit(); print("\napplied: wrote ev_odds_decimal / ev_odds_source / ev_odds_book only (pnl_units untouched)")
    con.close()


if __name__ == '__main__':
    sys.exit(main())
