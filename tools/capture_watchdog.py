#!/usr/bin/env python3
"""Alert when a sport played games but we captured no halftimes.

WHY THIS EXISTS
On 2026-09-14 ESPN withdrew support for `dates=YYYYMMDD-YYYYMMDD` on the
scoreboard endpoint. Two football-only code paths used a range, so NFL and CFB
halftime capture silently went to zero — 110 CFB captures on 09-12 became 4 on
09-19, NFL 25 became 0 — and nobody noticed for nine days. The loop stayed
`active (running)`, threw no exception, and kept writing final scores, so every
surface anyone looks at said the system was healthy.

The existing blind-loop watchdog fires on `checked=0`. That never tripped here:
the loop was checking 127 games a cycle. It was checking them and resolving none.

So this watchdog asks the only question that matters: ESPN says N games finished
today — how many of them did we hold a halftime score for? A sport that played
games and captured nothing is the alarm.

Run it after the day's games are done (the timer fires at 02:00 local, after the
loop's own stop) and it reports on the day that just ended.

Usage:
    python scripts/capture_watchdog.py                 # yesterday, all sports
    python scripts/capture_watchdog.py --date 2026-09-20
    python scripts/capture_watchdog.py --discord       # post failures to Discord
"""
import argparse
import datetime as dt
import json
import os
import sqlite3
import sys
import urllib.request

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(_ROOT, 'data', 'analysis.db')

# games_v2 sport key -> ESPN scoreboard path. MLB's key differs from the rest.
SPORTS = {
    'nfl':  ('football/nfl', 'nfl'),
    'cfb':  ('football/college-football', 'cfb'),
    'baseball_mlb': ('baseball/mlb', 'baseball_mlb'),
    'wnba': ('basketball/wnba', 'wnba'),
    'nba':  ('basketball/nba', 'nba'),
    'nhl':  ('hockey/nhl', 'nhl'),
    # Added 2026-10-05. These were absent, so the two highest-volume halftime
    # books (cbb 752 settled bets, wcbb 350) had no capture alarm at all. Their
    # season opens in November; _scoreboard_qs supplies the groups=50 they need.
    'cbb':  ('basketball/mens-college-basketball', 'cbb'),
    'wcbb': ('basketball/womens-college-basketball', 'wcbb'),
}

# ALERT ON A HARD ZERO, NOT ON A RATE.
#
# A precise capture *rate* cannot be computed reliably here and chasing one made
# this watchdog cry wolf in testing: games_v2 still stores some games under two
# spellings (so row counts exceeded 100%), ESPN's own CFB total for a Saturday
# disagrees with ours because late kickoffs straddle two calendar days, and NHL
# is deliberately not polled until October.
#
# The failure this exists to catch was never subtle — it was NFL 25 captures ->
# 0 and CFB 110 -> 4 while a full slate played. So: alert only when a sport
# played a real slate and we captured almost nothing. That signal survives every
# counting problem above, and a watchdog nobody mutes is worth more than a
# precise one nobody reads.
MIN_SLATE = 5      # fewer finished games than this: too small to judge
ALERT_AT_OR_BELOW = 2   # captured this many or fewer off a real slate -> alarm

# Sports we deliberately do not poll right now, where zero captures is correct
# behaviour rather than a fault.
#
# NHL was muted here on 2026-09-24 because it was the offseason and 91% of its
# scout snapshots were phantom games (project_nhl_phantom_polling said to disable
# until October). UNMUTED 2026-10-02: the season is under way, NHL captured 8 of
# 8 finished games on 10-01, and we have placed NHL bets on five of the last
# eight days. Leaving it muted would mean an NHL capture failure went unreported
# — which is the exact blind spot this script exists to close.
MUTED: set = set()


# Team spellings that mean the same club, so two rows for one game collapse to one.
_FOLD = {
    'NOP': 'NO', 'NOR': 'NO', 'JAC': 'JAX', 'WAS': 'WSH', 'LVR': 'LV',
    'OAK': 'LV', 'STL': 'LAR', 'LA': 'LAR', 'SD': 'LAC', 'TAM': 'TB',
    'KAN': 'KC', 'SFO': 'SF', 'GNB': 'GB', 'NWE': 'NE', 'ARZ': 'ARI',
    'NY': 'NYL', 'NYK': 'NYL', 'PHO': 'PHX', 'GSW': 'GS', 'CON': 'CONN',
}


def _fold(name: str) -> str:
    """Collapse a team spelling far enough that duplicates land together."""
    n = (name or '').strip().upper()
    n = _FOLD.get(n, n)
    # Full names ("Baltimore Ravens") vs abbreviations ("BAL") never fold into
    # each other; take the first word so at least the obvious pairs collapse.
    return n.split()[0] if ' ' in n else n


def _scoreboard_qs(path: str) -> str:
    """The query string that actually returns a full slate for this league.

    Two separate ESPN quirks, both verified 2026-10-05, both of which were
    silently shrinking this watchdog's own view of what played:

    college-football  limit >= 501 truncates to 25 "featured" events and voids
                      `groups`. The old '&limit=900' reported 25 of the 103
                      games on 2026-09-05. groups=90 selects every division.
    college-basketball  the DEFAULT page is ~1 game. Without groups=50 the
                      slate never reaches MIN_SLATE, so the watchdog could not
                      fire for CBB/WCBB at all. groups=50 returns 149 on
                      2026-01-10, and limit is honoured normally there.
    """
    if 'college-football' in path:
        # Deliberately NOT groups=90 here, though the abbrev map and the
        # settlement backfills do use it. groups=90 adds the lower divisions and
        # exactly doubles the denominator (2026-10-03: 54 FBS finals -> 108), so
        # the rate column would have read 49% while true coverage was 53/54.
        # The bare default is the bettable slate. Just never pass limit >= 501.
        return ''
    if 'college-basketball' in path:
        return '&groups=50&limit=900'
    return '&limit=400'


def espn_finals(path: str, ds: str) -> int:
    extra = _scoreboard_qs(path)
    url = (f"https://site.api.espn.com/apis/site/v2/sports/{path}"
           f"/scoreboard?dates={ds}{extra}")
    try:
        # No User-Agent: ESPN 403s a custom UA on urllib. See project_espn_ua_block.
        data = json.loads(urllib.request.urlopen(url, timeout=25).read())
    except Exception as e:
        print(f"  [{path}] ESPN fetch failed: {e}")
        return -1
    n = 0
    for ev in data.get('events', []):
        # EXHIBITIONS DO NOT COUNT. local_ops/run.py skips preseason games on
        # purpose (no model here is fitted on them), so counting them as ESPN
        # "finals" would make this alarm fire every day of the NBA preseason
        # for a capture gap we chose. A watchdog that is wrong on a schedule
        # gets muted, and then it is not a watchdog. Added 2026-10-06 after
        # 2026-10-05 alerted on 5 NBA finals that were all exhibitions.
        if ((ev.get('season') or {}).get('slug') or '').lower() == 'preseason':
            continue
        st = (ev.get('competitions') or [{}])[0].get('status', {})
        if 'FINAL' in st.get('type', {}).get('name', ''):
            n += 1
    return n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--date', default=None, help='YYYY-MM-DD (default: yesterday)')
    ap.add_argument('--discord', action='store_true')
    args = ap.parse_args()

    day = args.date or (dt.date.today() - dt.timedelta(days=1)).isoformat()
    ds = day.replace('-', '')
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)

    print(f"capture watchdog — {day}\n")
    print(f"  {'sport':14} {'ESPN finals':>11} {'our halftimes':>14} {'rate':>7}")
    alerts = []
    for key, (path, dbsport) in SPORTS.items():
        finals = espn_finals(path, ds)
        if finals <= 0:
            # 0 means the sport simply did not play. -1 means ESPN failed; either
            # way there is nothing to compare, so stay quiet rather than cry wolf.
            continue
        # COUNT DISTINCT GAMES, NOT ROWS. games_v2 still holds the same game under
        # more than one team spelling (NOP/NO, JAX/JAC, abbreviation vs full name),
        # and counting rows produced a 114% capture rate on a day that was fine —
        # a watchdog that reports impossible numbers gets ignored.
        pairs = con.execute("""
            SELECT DISTINCT upper(away_team), upper(home_team) FROM games_v2
            WHERE sport=? AND game_date=? AND home_ht_score IS NOT NULL""",
            (dbsport, day)).fetchall()
        got = len({(_fold(a), _fold(h)) for a, h in pairs})
        rate = min(got / finals, 1.0)
        flag = ''
        if key in MUTED:
            flag = '  (muted — not polled this season)'
        elif finals >= MIN_SLATE and got <= ALERT_AT_OR_BELOW:
            flag = '  <-- CAPTURE FAILURE'
            alerts.append((key, finals, got, rate))
        print(f"  {key:14} {finals:>11} {got:>14} {rate*100:>6.0f}%{flag}")
    con.close()

    if not alerts:
        print("\n  all sports above threshold")
        return 0

    msg = (f"**Halftime capture failure — {day}**\n" + "\n".join(
        f"`{k}`: ESPN had **{f}** finished games, we captured **{g}** halftimes"
        for k, f, g, r in alerts))
    print(f"\n{msg}")

    if args.discord:
        hook = os.environ.get('DISCORD_INTERNAL_WEBHOOK_URL') or os.environ.get('DISCORD_WEBHOOK_URL')
        if not hook:
            print("  [discord] no webhook configured")
        else:
            try:
                req = urllib.request.Request(
                    hook, data=json.dumps({'content': msg}).encode(),
                    headers={'Content-Type': 'application/json'})
                urllib.request.urlopen(req, timeout=15)
                print("  [discord] posted")
            except Exception as e:
                print(f"  [discord] post failed: {e}")
    return 1


if __name__ == '__main__':
    sys.exit(main())
