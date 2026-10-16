#!/usr/bin/env python3
"""Settle bets the live loop left pending, by looking up ESPN for the bet's OWN date.

WHY THIS EXISTS
`_settle_pending_bets` in local_ops/run.py only ever selects
`WHERE game_date = date.today()`, and it keys final scores by MATCHUP with no
date attached. Both facts matter:

1. The loop spans midnight (10:00-02:00 CT). A game that ends after 00:00 has a
   game_date of YESTERDAY while date.today() has already rolled over, so the bet
   is never selected. That is exactly how MLB_F5_HOT_OVER on MIL@SD 2026-10-06
   sat pending while `[final] MIL@SD: 3-4` was sitting in the same log file.

2. Widening that query to include yesterday would be WRONG. Baseball and hockey
   play series, so the same matchup recurs on consecutive days -- MIL@SD was
   played on both 2026-10-06 and 2026-10-07. With a date-less matchup key,
   yesterday's pending bet would be settled using today's final.

This script is date-correct by construction: it asks ESPN for the scoreboard of
the bet's own game_date and matches within that day only.

GRADING CONVENTION
Settles TOTALS on the full-game score, which is what every sport's recorded
history actually matches (mlb/nhl/cfb/nfl/soccer 99-100%, wnba 94%, cbb 86%).
It deliberately SKIPS spread bets: halftime spread results are corrupted
(favourites 27.8%, dogs 64.4%, totals 51.3% as a control), so writing through
that path would make a grading defect permanent. See
project_spread_grading_bug_is_systemic.

Usage:
    python scripts/settle_stale_bets.py            # dry run
    python scripts/settle_stale_bets.py --apply
"""
import argparse
import datetime as dt
import json
import os
import re
import sqlite3
import sys
import unicodedata
import urllib.request

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(_ROOT, 'data', 'analysis.db')
REAL = "(dynamo_pk IS NULL OR dynamo_pk NOT LIKE 'OBS#%')"

SPORT_PATH = {
    'mlb': 'baseball/mlb', 'baseball_mlb': 'baseball/mlb',
    'nhl': 'hockey/nhl', 'nba': 'basketball/nba', 'wnba': 'basketball/wnba',
    'nfl': 'football/nfl', 'cfb': 'football/college-football',
}
# ESPN's abbreviations are not ours. See project_espn_cfb_limit_truncation for
# the college-football query rules; these are the team-name aliases.
ALIAS = {'NOP': 'NO', 'NOR': 'NO', 'JAC': 'JAX', 'WAS': 'WSH', 'LVR': 'LV',
         'LA': 'LAR', 'SD': 'LAC', 'NYK': 'NY', 'GSW': 'GS', 'PHO': 'PHX',
         'CON': 'CONN', 'CWS': 'CHW'}

_cache = {}


def _norm(t):
    t = (t or '').upper().strip()
    return ALIAS.get(t, t)


def _cn(s):
    s = unicodedata.normalize('NFD', s or '')
    s = ''.join(c for c in s if unicodedata.category(c) != 'Mn')
    return re.sub(r'[^a-z]', '', s.lower())


def _scoreboard(sport, ds):
    """ESPN scoreboard for one day. Never pass limit >= 501 on college football."""
    key = (sport, ds)
    if key in _cache:
        return _cache[key]
    path = SPORT_PATH.get(sport)
    if not path:
        _cache[key] = {}
        return {}
    qs = '&groups=90' if 'college-football' in path else '&limit=400'
    url = (f"https://site.api.espn.com/apis/site/v2/sports/{path}"
           f"/scoreboard?dates={ds}{qs}")
    try:
        # No User-Agent: ESPN 403s a custom UA on urllib. project_espn_ua_block.
        data = json.loads(urllib.request.urlopen(url, timeout=25).read())
    except Exception as e:
        print(f"    [espn] {sport} {ds}: {e}")
        data = {}
    _cache[key] = data
    return data


def find_final(sport, game_date, away, home):
    """(away_score, home_score) for a FINAL game on that exact date, else None."""
    ds = game_date.replace('-', '')
    for ev in _scoreboard(sport, ds).get('events', []):
        comp = ev['competitions'][0]
        if 'FINAL' not in comp.get('status', {}).get('type', {}).get('name', ''):
            continue
        t = {x['homeAway']: x for x in comp['competitors']}
        ha, hh = t['away']['team'], t['home']['team']
        if _norm(ha.get('abbreviation')) == _norm(away) and \
           _norm(hh.get('abbreviation')) == _norm(home):
            return int(t['away']['score']), int(t['home']['score'])
        if _cn(ha.get('displayName')) == _cn(away) and \
           _cn(hh.get('displayName')) == _cn(home):
            return int(t['away']['score']), int(t['home']['score'])
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--min-age-days', type=int, default=1,
                    help='only touch bets at least this old (default 1)')
    a = ap.parse_args()

    con = sqlite3.connect(DB, timeout=60)
    con.execute('PRAGMA busy_timeout=60000')
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute(f"""
        SELECT id, game_date, sport, home_team, away_team, strategy_key,
               bet_type, bet_side, bet_line,
               halftime_home_score AS hh, halftime_away_score AS ah
        FROM bets_v2
        WHERE {REAL} AND bet_result IS NULL
          AND game_date <= date('now', ?)
        ORDER BY game_date
    """, (f'-{a.min_age_days} day',))]

    print(f"{'APPLY' if a.apply else 'DRY RUN'} — {len(rows)} pending bets "
          f"older than {a.min_age_days}d\n")
    done = skip = 0
    for r in rows:
        tag = f"{r['game_date']} {r['sport']:5} {r['away_team']:>5}@{r['home_team']:<6}"
        if r['bet_type'] == 'spread':
            print(f"  SKIP spread (grading corrupted): {tag} {r['strategy_key']}")
            skip += 1
            continue
        f = find_final(r['sport'], r['game_date'], r['away_team'], r['home_team'])
        if not f:
            print(f"  no ESPN final: {tag}")
            skip += 1
            continue
        af, hf = f
        full = af + hf
        line = float(r['bet_line']) if r['bet_line'] is not None else None
        side = (r['bet_side'] or '').lower()
        if line is None or side not in ('over', 'under'):
            print(f"  SKIP unsupported: {tag} side={side!r} line={line!r}")
            skip += 1
            continue
        res = 'P' if full == line else (
            ('W' if full > line else 'L') if side == 'over' else
            ('W' if full < line else 'L'))
        pnl = 0.909 if res == 'W' else (-1.0 if res == 'L' else 0.0)
        # Derive 2H only when the halftime score is present AND possible.
        h2h = h2a = None
        if r['hh'] is not None and r['ah'] is not None:
            _h, _a = hf - int(r['hh']), af - int(r['ah'])
            if _h >= 0 and _a >= 0:
                h2h, h2a = _h, _a
            else:
                print(f"    note: impossible 2H for {tag} — leaving 2H NULL")
        print(f"  {res}  {tag} {r['strategy_key'][:26]:26} {side:5} {line:>6} "
              f"| final {af}-{hf} total {full}")
        if a.apply:
            con.execute("""
                UPDATE bets_v2
                SET second_half_home_score = COALESCE(?, second_half_home_score),
                    second_half_away_score = COALESCE(?, second_half_away_score),
                    bet_result=?, pnl_units=?, bet_status='settled',
                    settled_at=datetime('now'), updated_at=datetime('now')
                WHERE id=? AND bet_result IS NULL
            """, (h2h, h2a, res, pnl, r['id']))
        done += 1
    if a.apply:
        con.commit()
    con.close()
    print(f"\n{'settled' if a.apply else 'would settle'}: {done}   skipped: {skip}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
