#!/usr/bin/env python3
"""Forward-track an EXTERNAL picks service and grade it ourselves.

WHY THIS EXISTS
sportsbettingaianalyzer.com publishes daily picks with a stated probability and
reports its own win rate. We cannot audit that record: its by-sport numbers
disagree between two endpoints (NHL 47.9% vs 55.8%), its history endpoint needs
a login, and its "edge" column is `probability - 50` rather than a gap against
the price. So this records what it says AT THE MOMENT it says it, then grades
each pick against real final scores at the odds quoted. Nothing is edited later.

WHAT IT STORES (data/external_picks.db, deliberately separate from analysis.db —
that file is 11 GB and the live loop writes to it):
  raw_pulls   one row per API pull, with the untouched response body
  picks       one parsed row per pick, with implied prob, stated prob, the gap
              against the price, and (after grading) result + units

QUOTA: the free tier is 6 requests/day. A pull is ONE request. Grading uses ESPN
only and costs the service nothing.

Usage:
  python scripts/track_external_picks.py --pull            # 1 API request
  python scripts/track_external_picks.py --grade           # ESPN only
  python scripts/track_external_picks.py --report
  python scripts/track_external_picks.py --selftest        # no API, no writes
"""
import argparse
import datetime as dt
import json
import math
import os
import re
import sqlite3
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, 'data', 'external_picks.db')
BASE = os.environ.get('SPORTS_BETTING_API_URL', 'https://sportsbettingaianalyzer.com')
KEY = os.environ.get('SPORTS_BETTING_API_KEY', '')
# The service's Cloudflare rule rejects the literal "Python-urllib/x" agent
# (HTTP 403, error 1010). Identify honestly instead; do not impersonate a browser.
UA = 'halftimeedge-tracker/1.0 (personal research, 1 request/day)'

ESPN = 'https://site.api.espn.com/apis/site/v2/sports'
SPORT_PATH = {
    'NHL': ('hockey/nhl', ''), 'MLB': ('baseball/mlb', ''),
    'NFL': ('football/nfl', ''), 'NBA': ('basketball/nba', ''),
    # college football: NEVER limit>=501 (silently truncates to 25); groups=90
    # returns every division. See project_espn_cfb_limit_truncation.
    'NCAAFB': ('football/college-football', '&groups=90'),
    'NCAAB': ('basketball/mens-college-basketball', '&groups=50&limit=400'),
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_pulls (
    id INTEGER PRIMARY KEY, pulled_at TEXT, endpoint TEXT, http_status INTEGER, body TEXT);
CREATE TABLE IF NOT EXISTS picks (
    id INTEGER PRIMARY KEY,
    pull_id INTEGER, pull_date TEXT, sport TEXT, bet_type TEXT, pick_name TEXT,
    side TEXT, team TEXT, away TEXT, home TEXT, line REAL, odds INTEGER,
    confidence TEXT, edge_score REAL, their_prob REAL, implied_prob REAL, gap REAL,
    market_reference INTEGER, raw_json TEXT,
    parse_status TEXT,                      -- ok | unparsed
    result TEXT, units REAL, final_away INTEGER, final_home INTEGER,
    grade_note TEXT, graded_at TEXT,
    UNIQUE(pull_date, sport, bet_type, pick_name, line, odds));
"""


# ───────────────────────── math ─────────────────────────
def implied(odds):
    return (-odds) / ((-odds) + 100.0) if odds < 0 else 100.0 / (odds + 100.0)


def dec(odds):
    return 1 + (100.0 / -odds if odds < 0 else odds / 100.0)


def wilson(w, n, z=1.96):
    if not n:
        return (0.0, 0.0)
    p = w / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


# ───────────────────────── parsing ─────────────────────────
def norm_bet_type(s):
    s = (s or '').lower().replace('_', ' ').strip()
    if s in ('moneyline', 'ml', 'h2h', 'money line'):
        return 'moneyline'
    if s in ('total', 'totals', 'over/under', 'ou', 'over under'):
        return 'total'
    if s in ('spread', 'run line', 'runline', 'puck line', 'puckline', 'ats'):
        return 'spread'
    return s


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def parse_pick(p):
    """Return a dict of structured fields; parse_status='unparsed' when unsure.
    Never guesses: an ungradable pick is reported, not silently invented."""
    name = (p.get('pick_name') or p.get('team') or '').strip()
    bt = norm_bet_type(p.get('bet_type'))
    out = dict(pick_name=name, bet_type=bt, side=None, team=None, away=None,
               home=None, line=None, parse_status='unparsed')
    line_field = _num(p.get('line_value') if p.get('line_value') not in (None, '') else p.get('line'))
    if bt == 'total':
        m = re.match(r'^\s*(O|U|Over|Under)\s*([0-9]+(?:\.[0-9]+)?)\s+(.+?)\s+(?:@|at|vs\.?)\s+(.+?)\s*$', name, re.I)
        if not m:
            # live format: "O6.5 (Away Team/Home Team)"
            m = re.match(r'^\s*(O|U|Over|Under)\s*([0-9]+(?:\.[0-9]+)?)\s*\(\s*(.+?)\s*/\s*(.+?)\s*\)\s*$', name, re.I)
        if m:
            out.update(side='over' if m.group(1).lower().startswith('o') else 'under',
                       line=float(m.group(2)), away=m.group(3), home=m.group(4),
                       parse_status='ok')
    elif bt == 'spread':
        m = re.match(r'^\s*(.+?)\s*([+-][0-9]+(?:\.[0-9]+)?)\s*$', name)
        if m:
            out.update(team=m.group(1), line=float(m.group(2)), parse_status='ok')
        elif line_field is not None and name:
            out.update(team=re.sub(r'\s*(spread|run ?line|puck ?line)\s*$', '', name, flags=re.I),
                       line=line_field, parse_status='ok')
    elif bt == 'moneyline':
        m = re.match(r'^\s*(.+?)\s+(?:ML|moneyline)\s*$', name, re.I)
        out.update(team=(m.group(1) if m else name) or None,
                   parse_status='ok' if (m or name) else 'unparsed')
    return out


# ───────────────────────── ESPN matching ─────────────────────────
_ALIAS = {'st': 'state', 'la': 'los angeles', 'ny': 'new york', 'n y': 'new york'}


def nm(s):
    s = unicodedata.normalize('NFD', s or '')
    s = ''.join(c for c in s if unicodedata.category(c) != 'Mn').lower()
    s = re.sub(r"[^a-z0-9 ]", ' ', s)
    toks = []
    for t in s.split():
        toks.extend(_ALIAS.get(t, t).split())
    return ' '.join(toks)


def team_names(t):
    return {nm(t.get(k)) for k in ('displayName', 'shortDisplayName', 'location', 'name', 'abbreviation')
            if t.get(k)} - {''}


# The service abbreviates a leading compass/region word to one letter
# ("W Kentucky", "N Illinois", "S Alabama"). Try every plausible expansion.
_LEAD = {'w': ('western', 'west'), 'e': ('eastern', 'east'), 'n': ('northern', 'north'),
         's': ('southern', 'south'), 'c': ('central',), 'm': ('middle',)}


def _variants(pt):
    out = [pt]
    toks = pt.split()
    if len(toks) > 1 and toks[0] in _LEAD:
        out += [' '.join([w] + toks[1:]) for w in _LEAD[toks[0]]]
    return out


def team_matches(pick_team, t):
    pt = nm(pick_team)
    if not pt:
        return False
    cands = team_names(t)
    for v in _variants(pt):
        for cand in cands:
            if v == cand or cand.startswith(v + ' ') or v.startswith(cand + ' '):
                return True
    return False


def fetch_scoreboard(sport, ds):
    path, extra = SPORT_PATH.get(sport, (None, ''))
    if not path:
        return []
    url = f"{ESPN}/{path}/scoreboard?dates={ds}{extra}"
    try:
        # NO User-Agent: ESPN 403s a custom agent on urllib (project_espn_ua_block).
        data = json.loads(urllib.request.urlopen(url, timeout=25).read())
    except Exception as e:
        print(f"    [espn] {sport} {ds}: {e}")
        return []
    return data.get('events', [])


def event_teams(ev):
    comp = ev['competitions'][0]
    t = {c['homeAway']: c for c in comp['competitors']}
    return comp, t['away']['team'], t['home']['team'], t['away'], t['home']


def find_event(events, row):
    for ev in events:
        try:
            comp, a, h, ac, hc = event_teams(ev)
        except Exception:
            continue
        if row['bet_type'] == 'total':
            if team_matches(row['away'], a) and team_matches(row['home'], h):
                return ev
        else:
            if team_matches(row['team'], a) or team_matches(row['team'], h):
                return ev
    return None


def grade_row(row, ev):
    """(result, units, away_score, home_score, note) or None if the game isn't final."""
    comp, a, h, ac, hc = event_teams(ev)
    st = comp.get('status', {}).get('type', {})
    if 'FINAL' not in (st.get('name') or '').upper():
        return None
    sa, sh = int(ac['score']), int(hc['score'])
    note = ''
    # Shootout: the box score gives the winner one extra goal that no book
    # counts toward totals or the puck line. ML still settles on the winner.
    if row['sport'] == 'NHL' and row['bet_type'] in ('total', 'spread') \
            and 'SO' in (st.get('detail') or st.get('shortDetail') or '').upper():
        if sh > sa:
            sh -= 1
        elif sa > sh:
            sa -= 1
        note = 'shootout goal removed'
    bt = row['bet_type']
    if bt == 'total':
        tot = sa + sh
        if tot == row['line']:
            res = 'P'
        elif row['side'] == 'over':
            res = 'W' if tot > row['line'] else 'L'
        else:
            res = 'W' if tot < row['line'] else 'L'
    else:
        is_home = team_matches(row['team'], h)
        margin = (sh - sa) if is_home else (sa - sh)
        if bt == 'moneyline':
            full_a, full_h = int(ac['score']), int(hc['score'])
            m2 = (full_h - full_a) if is_home else (full_a - full_h)
            res = 'P' if m2 == 0 else ('W' if m2 > 0 else 'L')
        else:
            s = margin + row['line']
            res = 'P' if s == 0 else ('W' if s > 0 else 'L')
    d = dec(row['odds'])
    units = (d - 1) if res == 'W' else (-1.0 if res == 'L' else 0.0)
    return res, round(units, 4), sa, sh, note


# ───────────────────────── commands ─────────────────────────
def connect():
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    con = sqlite3.connect(DB, timeout=30)
    con.executescript(SCHEMA)
    return con


def cmd_pull():
    if not KEY:
        print("SPORTS_BETTING_API_KEY not set (source ~/.config/sports-betting-mcp/env)")
        return 1
    con = connect()
    url = f"{BASE}/xk/p"
    req = urllib.request.Request(url, headers={'X-API-Key': KEY, 'User-Agent': UA})
    status, body = 0, ''
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            status, body = r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        status, body = e.code, e.read().decode()[:2000]
    except Exception as e:
        status, body = -1, f"{type(e).__name__}: {e}"
    now = dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    cur = con.execute("INSERT INTO raw_pulls(pulled_at,endpoint,http_status,body) VALUES (?,?,?,?)",
                      (now, '/xk/p', status, body))
    pull_id = cur.lastrowid
    con.commit()
    if status == 429:
        print("quota reached (429); nothing to record today")
        return 0
    if status != 200:
        print(f"pull failed: HTTP {status}: {body[:200]}")
        return 1
    try:
        data = json.loads(body)
    except Exception:
        print("response was not JSON; raw body saved, nothing parsed")
        return 1
    picks = data if isinstance(data, list) else data.get('picks', [])
    local_date = dt.datetime.now().strftime('%Y-%m-%d')
    new = unparsed = 0
    for p in picks:
        f = parse_pick(p)
        odds = p.get('odds')
        try:
            odds = int(float(odds))
        except (TypeError, ValueError):
            odds = None
        edge = _num(p.get('edge_score') if p.get('edge_score') is not None else p.get('edge'))
        their = (edge + 50.0) if edge is not None else None   # the service's own conversion
        imp = implied(odds) * 100 if odds else None
        gap = (their - imp) if (their is not None and imp is not None) else None
        mref = 1 if (gap is not None and abs(gap) < 0.15) else 0
        sport = (p.get('sport') or '').upper()
        try:
            cur2 = con.execute("""INSERT OR IGNORE INTO picks
              (pull_id,pull_date,sport,bet_type,pick_name,side,team,away,home,line,odds,confidence,
               edge_score,their_prob,implied_prob,gap,market_reference,raw_json,parse_status)
              VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (pull_id, local_date, sport, f['bet_type'], f['pick_name'], f['side'], f['team'],
                         f['away'], f['home'], f['line'] if f['line'] is not None else _num(p.get('line_value')),
                         odds, p.get('confidence'), edge, their, imp, gap, mref,
                         json.dumps(p), f['parse_status']))
            new += cur2.rowcount if cur2.rowcount and cur2.rowcount > 0 else 0
        except sqlite3.Error as e:
            print("insert error:", e)
        if f['parse_status'] != 'ok':
            unparsed += 1
    con.commit()
    n = con.execute("SELECT COUNT(*) FROM picks WHERE pull_id=?", (pull_id,)).fetchone()[0]
    print(f"pulled {len(picks)} picks -> {n} new rows ({unparsed} could not be parsed)")
    return 0


def cmd_grade():
    con = connect()
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute(
        """SELECT * FROM picks WHERE result IS NULL AND parse_status='ok' AND odds IS NOT NULL
           ORDER BY pull_date""")]
    print(f"{len(rows)} ungraded picks")
    cache = {}
    done = wait = lost = 0
    today = dt.date.today()
    for r in rows:
        d0 = dt.date.fromisoformat(r['pull_date'])
        found = None
        for off in (0, 1, -1):
            ds = (d0 + dt.timedelta(days=off)).strftime('%Y%m%d')
            key = (r['sport'], ds)
            if key not in cache:
                cache[key] = fetch_scoreboard(r['sport'], ds)
            ev = find_event(cache[key], r)
            if ev is not None:
                found = ev
                break
        if found is None:
            if (today - d0).days >= 3:
                con.execute("UPDATE picks SET result='X', units=0, grade_note=? , graded_at=datetime('now') WHERE id=?",
                            ('no matching game found after 3 days', r['id']))
                lost += 1
            continue
        g = grade_row(r, found)
        if g is None:
            wait += 1
            continue
        res, units, sa, sh, note = g
        con.execute("""UPDATE picks SET result=?,units=?,final_away=?,final_home=?,grade_note=?,
                       graded_at=datetime('now') WHERE id=?""", (res, units, sa, sh, note, r['id']))
        done += 1
    con.commit()
    print(f"graded {done}, not final yet {wait}, unmatched after 3 days {lost}")
    return 0


def cmd_report():
    con = connect()
    con.row_factory = sqlite3.Row
    allr = [dict(r) for r in con.execute("SELECT * FROM picks")]
    graded = [r for r in allr if r['result'] in ('W', 'L', 'P')]
    print(f"picks recorded: {len(allr)}   graded: {len(graded)}   "
          f"pending: {sum(1 for r in allr if r['result'] is None)}   "
          f"unparsed: {sum(1 for r in allr if r['parse_status'] != 'ok')}")
    if not graded:
        return 0

    def line(label, rows):
        w = sum(1 for r in rows if r['result'] == 'W')
        l = sum(1 for r in rows if r['result'] == 'L')
        n = w + l
        if not n:
            return
        u = sum(r['units'] for r in rows)
        be = sum(implied(r['odds']) for r in rows) / len(rows)
        lo, hi = wilson(w, n)
        print(f"  {label:34} {w:>3}-{l:<3} {100*w/n:5.1f}%  [{100*lo:.0f}-{100*hi:.0f}]  "
              f"breakeven {100*be:4.1f}%  units {u:+7.2f}  ROI {100*u/len(rows):+6.1f}%")

    print("\nALL / by bet type / by sport / by confidence (units at the QUOTED odds, 1u flat)")
    line('ALL', graded)
    for k in sorted({r['bet_type'] for r in graded}):
        line(f'  type={k}', [r for r in graded if r['bet_type'] == k])
    for k in sorted({r['sport'] for r in graded}):
        line(f'  sport={k}', [r for r in graded if r['sport'] == k])
    for k in sorted({r['confidence'] for r in graded if r['confidence']}):
        line(f'  confidence={k}', [r for r in graded if r['confidence'] == k])
    print("\nMODEL CLAIMS vs MARKET-REFERENCE ROWS (their prob == the price's implied prob)")
    line('market-reference rows', [r for r in graded if r['market_reference']])
    line('rows with a stated gap', [r for r in graded if not r['market_reference']])
    print("\nCALIBRATION: stated probability vs what happened")
    edges = [(0, 45), (45, 52), (52, 58), (58, 65), (65, 75), (75, 101)]
    print(f"  {'stated prob':14} {'n':>4} {'mean stated':>12} {'actual win%':>12}")
    for lo_, hi_ in edges:
        b = [r for r in graded if r['their_prob'] is not None and lo_ <= r['their_prob'] < hi_ and r['result'] in ('W', 'L')]
        if b:
            w = sum(1 for r in b if r['result'] == 'W')
            ms = sum(r['their_prob'] for r in b) / len(b)
            print(f"  {f'{lo_}-{hi_}':14} {len(b):>4} {ms:>11.1f}% {100*w/len(b):>11.1f}%")
    last = sorted(graded, key=lambda r: r['id'])[-20:]
    w = sum(1 for r in last if r['result'] == 'W')
    l = sum(1 for r in last if r['result'] == 'L')
    print(f"\nlast {len(last)} graded: {w}-{l}  (picks are correlated, ~3 bet types per game; "
          f"effective sample is smaller)")
    return 0


def cmd_selftest():
    print("parser:")
    cases = [
        ('total', 'O50.5 Kennesaw St @ Missouri St'), ('total', 'U6.5 DAL @ COL'),
        ('spread', 'Missouri St +3.5'), ('spread', 'Washington -1.5'),
        ('moneyline', 'Kennesaw St ML'), ('moneyline', 'LA Dodgers ML'),
        ('total', 'garbage'), ('prop', 'whatever'),
    ]
    for bt, nme in cases:
        f = parse_pick({'bet_type': bt, 'pick_name': nme})
        print(f"  {bt:10} {nme:34} -> {f['parse_status']:9} side={f['side']} team={f['team']} "
              f"line={f['line']} away={f['away']} home={f['home']}")
    print("\nteam matching against today's REAL ESPN scoreboards (no grading):")
    tests = [('NHL', 'Washington'), ('NHL', 'MTL'), ('NHL', 'Colorado'), ('NHL', 'DAL'),
             ('MLB', 'LA Dodgers'), ('MLB', 'Milwaukee'),
             ('NCAAFB', 'Missouri St'), ('NCAAFB', 'Kennesaw St'), ('NCAAFB', 'Sam Houston'),
             ('NCAAFB', 'W Kentucky')]
    ds = dt.datetime.now().strftime('%Y%m%d')
    for sport, team in tests:
        evs = fetch_scoreboard(sport, ds)
        hit = None
        for ev in evs:
            try:
                comp, a, h, _, _ = event_teams(ev)
            except Exception:
                continue
            if team_matches(team, a) or team_matches(team, h):
                hit = f"{a.get('abbreviation')}@{h.get('abbreviation')}"
                break
        print(f"  {sport:7} {team:14} -> {hit or 'NO MATCH'}   ({len(evs)} events)")
    print("\ngrading arithmetic (synthetic finals):")
    def fake(sa, sh, status='STATUS_FINAL', detail='Final'):
        return {'competitions': [{'status': {'type': {'name': status, 'detail': detail}},
                'competitors': [
                    {'homeAway': 'away', 'score': str(sa), 'team': {'displayName': 'Montreal Canadiens', 'abbreviation': 'MTL', 'shortDisplayName': 'Canadiens', 'location': 'Montreal', 'name': 'Canadiens'}},
                    {'homeAway': 'home', 'score': str(sh), 'team': {'displayName': 'Washington Capitals', 'abbreviation': 'WSH', 'shortDisplayName': 'Capitals', 'location': 'Washington', 'name': 'Capitals'}}]}]}
    base = dict(sport='NHL', odds=-110)
    checks = [
        (dict(base, bet_type='total', side='over', line=6.5, away='MTL', home='WSH'), fake(3, 4), 'W'),
        (dict(base, bet_type='total', side='under', line=6.5, away='MTL', home='WSH'), fake(3, 4), 'L'),
        (dict(base, bet_type='total', side='over', line=7.0, away='MTL', home='WSH'), fake(3, 4), 'P'),
        (dict(base, bet_type='spread', team='Washington', line=-1.5), fake(1, 3), 'W'),
        (dict(base, bet_type='spread', team='Washington', line=-1.5), fake(2, 3), 'L'),
        (dict(base, bet_type='moneyline', team='MTL'), fake(2, 1), 'W'),
        # shootout: 3-2 SO win for WSH is a ONE-goal game, so -1.5 loses, total is 4
        (dict(base, bet_type='spread', team='Washington', line=-1.5), fake(2, 3, detail='Final/SO'), 'L'),
        (dict(base, bet_type='total', side='over', line=4.5, away='MTL', home='WSH'), fake(2, 3, detail='Final/SO'), 'L'),
    ]
    bad = 0
    for row, ev, want in checks:
        g = grade_row(row, ev)
        got = g[0] if g else None
        ok = got == want
        bad += (not ok)
        print(f"  {row['bet_type']:10} {str(row.get('side') or row.get('team')):11} line={row.get('line')} "
              f"-> {got} (want {want}) {'OK' if ok else 'FAIL'}")
    print(f"  not-final game returns: {grade_row(dict(base, bet_type='moneyline', team='MTL'), fake(0, 0, status='STATUS_IN_PROGRESS'))}")
    return 1 if bad else 0


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument('--pull', action='store_true')
    g.add_argument('--grade', action='store_true')
    g.add_argument('--report', action='store_true')
    g.add_argument('--selftest', action='store_true')
    a = ap.parse_args()
    sys.exit(cmd_pull() if a.pull else cmd_grade() if a.grade
             else cmd_report() if a.report else cmd_selftest())
