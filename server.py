"""
World Cup Pilot — local HTTP server.

Serves the single-page UI (worldcup.html) and a small JSON API that proxies
and caches Football-Data.org (https://docs.football-data.org/) so the native
window can render fixtures, group standings and team details.

Local / personal use only. The Football-Data.org token lives in config.json
next to this file (or next to the .exe when bundled) and is read at startup.

API (all JSON unless noted):
  GET  /                      -> worldcup.html
  GET  /assets/<file>         -> static asset (background image, logo, ...)
  GET  /api/status            -> { token_set, mock, competition, season, dates }
  GET  /api/matches           -> { dates: [...], matches: [...], source }
  GET  /api/standings         -> { groups: [...], source }
  GET  /api/team?id=<id>      -> { team: {...}, source }
  POST /api/refresh           -> clears the on-disk cache
"""
import calendar
import hashlib
import json
import math
import os
import queue
import random
import re
import threading
import time
import unicodedata

# global throttle for TheSportsDB (free tier rate-limits aggressively)
_tsdb_lock = threading.Lock()
_tsdb_last = [0.0]


def _tsdb_throttle():
    with _tsdb_lock:
        dt = time.time() - _tsdb_last[0]
        if dt < 0.5:
            time.sleep(0.5 - dt)
        _tsdb_last[0] = time.time()
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

# ---- request-scoped memo ----------------------------------------------------
# ThreadingHTTPServer runs one thread per request, but a single request (e.g. predict_match
# or compute_accuracy) calls the heavy match-list parsers 10+ times — each re-reading and
# re-parsing the same disk-cached JSON. A thread-local memo, cleared at the start of every
# request (do_GET/do_POST), collapses those to one parse without changing cross-request
# freshness. A force-revalidate caller (ttl=0) bypasses the memo so /api/refresh stays live.
_req_local = threading.local()


def _req_memo(key, producer, bypass=False):
    cache = getattr(_req_local, "cache", None)
    if cache is None:
        cache = {}
        _req_local.cache = cache
    if not bypass and key in cache:
        return cache[key]
    val = producer()
    cache[key] = val
    return val


def _req_memo_clear():
    _req_local.cache = {}

# ---- paths (overridable by the launcher when frozen) ------------------------
ROOT = os.path.dirname(os.path.abspath(__file__))
HTML = os.path.join(ROOT, "worldcup.html")
ASSETS_DIR = os.path.join(ROOT, "assets")
CACHE_DIR = os.path.join(ROOT, "cache")
CONFIG_PATH = os.path.join(ROOT, "config.json")

API_BASE = "https://api.football-data.org/v4"

DEFAULTS = {
    "groq_api_key": "",       # console.groq.com (free) → enables the ⚡ Groq pick
    "openai_api_key": "",     # OpenAI (paid) → enables the 💬 ChatGPT pick
    "competition": "WC",
    "season": 2026,
    "cache_ttl_seconds": 120,
    "use_mock_when_unavailable": True,
    # "현지시간" 토글이 쓰는 개최지 시간대 (football-data 가 경기장 정보를 주지 않으므로
    # 대회 개최 권역 기준 단일값. 2026 월드컵=북미, 기본 미 동부)
    "venue_timezone": "America/New_York",
}


def load_config():
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg.update(json.load(f))
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"[warn] bad config.json: {e}")
    if cfg.get("grop_api_key") and not cfg.get("groq_api_key"):   # tolerate a common typo
        cfg["groq_api_key"] = cfg["grop_api_key"]
    for k in ("groq_api_key", "openai_api_key"):
        v = (cfg.get(k) or "").strip()
        cfg[k] = "" if v.startswith("<") or v.upper().startswith("PUT_") else v
    return cfg


CONFIG = load_config()


def load_venues():
    """{cities: {city: tz}, matches: {matchId: city}} from assets/venues.json."""
    try:
        with open(os.path.join(ASSETS_DIR, "venues.json"), "r", encoding="utf-8") as f:
            v = json.load(f)
            return {"cities": v.get("cities", {}), "matches": v.get("matches", {})}
    except Exception:
        return {"cities": {}, "matches": {}}


VENUES = load_venues()


def load_ranking():
    """{country: rank} FIFA ranking snapshot from assets/fifa_ranking.json."""
    try:
        with open(os.path.join(ASSETS_DIR, "fifa_ranking.json"), "r", encoding="utf-8") as f:
            return json.load(f).get("ranks", {})
    except Exception:
        return {}


RANKING = load_ranking()


def load_ranking_history():
    """{year(str): {country: rank}} — FIFA ranking as of each past edition."""
    try:
        with open(os.path.join(ASSETS_DIR, "fifa_ranking_history.json"), "r", encoding="utf-8") as f:
            return json.load(f).get("byYear", {})
    except Exception:
        return {}


RANKING_HISTORY = load_ranking_history()


def load_country_info():
    try:
        with open(os.path.join(ASSETS_DIR, "country_info.json"), "r", encoding="utf-8") as f:
            return json.load(f).get("data", {})
    except Exception:
        return {}


COUNTRY = load_country_info()


def _country_norm():
    if getattr(_country_norm, "src", None) is not COUNTRY:
        cache = {}
        for k, v in COUNTRY.items():
            cache.setdefault(_norm(k), v)
            cache.setdefault(_canon(k), v)   # alias-aware: Turkey↔Türkiye, Cape Verde Islands↔Cape Verde, …
        _country_norm.cache = cache
        _country_norm.src = COUNTRY
    return _country_norm.cache


_UK_SUBDIV = {"scotland": "gb-sct", "england": "gb-eng", "wales": "gb-wls", "northernireland": "gb-nir"}


def country_info(name):
    if not name:
        return None
    c = _country_norm()
    info = c.get(_canon(name)) or c.get(_norm(name))
    sub = _UK_SUBDIV.get(_canon(name))                  # UK home nations have their own flag (not the Union Jack)
    if info and sub and info.get("iso2") == "gb":
        info = {**info, "iso2": sub}                     # copy → don't mutate the shared cache
    return info


def _ranking_norm():
    """Lazy normalized lookup (built after _norm exists; rebuilt if RANKING swaps)."""
    if getattr(_ranking_norm, "src", None) is not RANKING:
        _ranking_norm.cache = {_norm(k): v for k, v in RANKING.items()}
        _ranking_norm.src = RANKING
    return _ranking_norm.cache


def _ranking_hist_norm(year):
    """Normalized lookup for a given edition year, or None if no data for that year."""
    cache = getattr(_ranking_hist_norm, "cache", None)
    if cache is None:
        cache = _ranking_hist_norm.cache = {}
    key = str(year)
    if key not in cache:
        ranks = RANKING_HISTORY.get(key)
        cache[key] = {_norm(k): v for k, v in ranks.items()} if ranks else None
    return cache[key]


def rank_for(name, year=None):
    """FIFA ranking for a team. Current edition → current snapshot; past edition →
    that year's ranking if we have it, else None (ranking didn't exist / no data)."""
    if not name:
        return None
    if year is not None and str(year) != str(CONFIG.get("season")):
        hist = _ranking_hist_norm(year)
        return hist.get(_norm(name)) if hist else None
    return _ranking_norm().get(_norm(name))


def token_ok():
    return bool(CONFIG.get("football_data_token"))


# ---- football-data.org client with disk cache -------------------------------
def _cache_file(key):
    return os.path.join(CACHE_DIR, f"{key}.json")


def _read_cache(key, ttl):
    path = _cache_file(key)
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return None, None
    age = time.time() - st.st_mtime
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None, None
    fresh = age <= ttl
    return data, fresh


def _write_cache(key, data):
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = _cache_file(key)
    # Atomic: write to a unique temp file then os.replace (atomic on POSIX + Windows).
    # ThreadingHTTPServer means concurrent requests may write the same key; without this a
    # reader could observe a half-written file (caught as a parse error → stale/None fallback).
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except Exception as e:
        print(f"[warn] cache write {key}: {e}")
        try:
            os.remove(tmp)
        except OSError:
            pass


def fd_get(path, cache_key, ttl=None):
    """GET {API_BASE}{path} with token, disk-cached. Returns (data, source).

    source: "live" | "cache" | "mock". Falls back to stale cache, then mock.
    """
    if ttl is None:
        ttl = int(CONFIG.get("cache_ttl_seconds", 120))
    cached, fresh = _read_cache(cache_key, ttl)
    if cached is not None and fresh:
        return cached, "cache"

    if not token_ok():
        if cached is not None:
            return cached, "cache"
        return None, "mock"

    url = f"{API_BASE}{path}"
    req = urllib.request.Request(url, headers={
        "X-Auth-Token": CONFIG["football_data_token"],
        "User-Agent": "WorldCupPilot/1.0",
    })
    try:
        with urllib.request.urlopen(req, timeout=12) as r:
            data = json.loads(r.read().decode("utf-8"))
        _write_cache(cache_key, data)
        return data, "live"
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as e:
        print(f"[warn] football-data {path}: {e}")
        if cached is not None:
            return cached, "cache"
        return None, "mock"


# ---- normalization ----------------------------------------------------------
def _crest(team, area_flag=None):
    return team.get("crest") or area_flag or ""


def normalize_team(raw):
    area = raw.get("area", {}) or {}
    squad = [{"id": p.get("id"), "name": p.get("name"),
              "position": p.get("position"), "nationality": p.get("nationality"),
              "dateOfBirth": p.get("dateOfBirth")} for p in (raw.get("squad") or [])]
    coach = raw.get("coach") or {}
    return {
        "id": raw.get("id"), "name": raw.get("name"), "tla": raw.get("tla"),
        "rank": rank_for(raw.get("name")),
        "crest": _crest(raw, area.get("flag")),
        "area": {"name": area.get("name"), "flag": area.get("flag")},
        "founded": raw.get("founded"), "address": raw.get("address"),
        "coach": {"name": coach.get("name"), "nationality": coach.get("nationality")},
        "squad": squad,
    }


def unique_dates(matches):
    return sorted({(m["utcDate"] or "")[:10] for m in matches if m.get("utcDate")})


# ---- mock data (used until a valid token / coverage is available) -----------
def _flag(code):
    return f"https://flagcdn.com/w160/{code}.png"


def mock_team(team_id):
    names = {9004: ("Netherlands", "nl"), 9005: ("England", "gb-eng"),
             9008: ("Korea Republic", "kr")}
    name, code = names.get(int(team_id), ("Sample National Team", "un"))
    return {"id": int(team_id), "name": name, "tla": name[:3].upper(),
            "crest": _flag(code), "area": {"name": name, "flag": _flag(code)},
            "founded": 1889, "coach": {"name": "—", "nationality": name},
            "squad": [{"id": 1, "name": "Player One", "position": "Goalkeeper",
                       "nationality": name, "dateOfBirth": "1995-01-01"},
                      {"id": 2, "name": "Player Two", "position": "Defence",
                       "nationality": name, "dateOfBirth": "1997-03-12"},
                      {"id": 3, "name": "Player Three", "position": "Midfield",
                       "nationality": name, "dateOfBirth": "1998-07-20"},
                      {"id": 4, "name": "Player Four", "position": "Offence",
                       "nationality": name, "dateOfBirth": "2000-11-05"}]}


# ---- data assembly ----------------------------------------------------------
def _utc_epoch(iso):
    """ESPN UTC timestamp ('2026-07-19T19:00Z') -> epoch seconds, or None."""
    if not iso:
        return None
    for fmt in ("%Y-%m-%dT%H:%MZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return calendar.timegm(time.strptime(iso, fmt))
        except ValueError:
            continue
    return None


def _live_ttl(season):
    """Adaptive cache lifetime for the current edition. The schedule and finished results never
    change, so we only re-fetch ESPN when live state actually can: poll tightly while a match is
    in play (or just finished — knockout slots are filling in), and otherwise hold the cache until
    just before the next kickoff. Between match days this means ~no API calls at all; a fully
    finished edition caches permanently. Finished scores ride along in the held list — no refetch."""
    short = int(CONFIG.get("cache_ttl_seconds", 60))
    cached, _ = _read_cache(f"espn-year-{season}", 10 ** 9)
    if not cached:
        return short
    now = time.time()
    any_unfinished = False
    recent_finish = False
    next_kick = None
    for ev in cached.get("events", []):
        name = (((ev.get("status") or {}).get("type") or {}).get("name") or "")
        state = (((ev.get("status") or {}).get("type") or {}).get("state") or "")
        ts = _utc_epoch(ev.get("date"))
        if state == "in" or any(k in name for k in ("HALF", "IN_PROGRESS", "OVERTIME", "SHOOTOUT", "PROGRESS")):
            return short                                       # live now → track closely
        if "FINAL" in name or name == "STATUS_FULL_TIME":
            if ts is not None and 0 <= now - ts <= 4 * 3600:   # just finished → downstream bracket
                recent_finish = True                           # slots may still be populating
            continue
        any_unfinished = True
        if ts is not None and ts <= now and now - ts <= 4 * 3600:
            return short                                       # kickoff passed, not yet live/final → poll
        if ts is not None and ts > now and (next_kick is None or ts < next_kick):
            next_kick = ts
    if not any_unfinished:
        return 10 ** 9                                         # whole edition final → permanent
    if recent_finish:
        return max(short, 300)                                 # settle window: catch slot fill-ins
    if next_kick is None:
        return 6 * 3600                                        # nothing parseable upcoming → hold
    return int(max(short, min(next_kick - now - 120, 6 * 3600)))  # hold until ~2 min pre-kickoff


def get_matches():
    """Current edition fixtures/scores — ESPN, adaptive cache (see _live_ttl). No token, no mock."""
    season = str(CONFIG.get("season"))
    return get_matches_espn(season, ttl=_live_ttl(season))


def _espn_status(s):
    t = (s or {}).get("type") or {}
    n = t.get("name", "") or ""
    state = t.get("state", "") or ""
    # live FIRST: a level knockout heading into ET/PK stays state="in" (and STATUS_SHOOTOUT) — it is
    # NOT finished even though ESPN may flash STATUS_FULL_TIME. Only completed/post is truly final.
    if state == "in" or any(k in n for k in ("HALF", "IN_PROGRESS", "OVERTIME", "SHOOTOUT", "PROGRESS")):
        return "IN_PLAY"
    if t.get("completed") is True or state == "post" or "FINAL" in n or n == "STATUS_FULL_TIME":
        return "FINISHED"
    return "SCHEDULED"


_ESPN_STAGE = {"group-stage": "GROUP_STAGE", "round-of-32": "LAST_32", "round-of-16": "LAST_16",
               "quarterfinals": "QUARTER_FINALS", "semifinals": "SEMI_FINALS",
               "3rd-place-match": "THIRD_PLACE", "final": "FINAL"}


def get_matches_espn(year, ttl=10 ** 9):
    """Full match list from ESPN for an edition (one call per year). ttl is small for
    the live edition (scores change) and permanent for finished past editions.
    Memoized per request (ttl=0 bypasses) — the predict/accuracy paths call this many times."""
    return _req_memo(("matches_espn", str(year)),
                     lambda: _get_matches_espn(year, ttl), bypass=(ttl == 0))


def _get_matches_espn(year, ttl):
    # limit=300: ESPN's scoreboard defaults to 100 events → the 2026 WC has 104 (incl. SF/3rd/final),
    # so without this the last 4 knockout matches get silently dropped.
    data = http_json(f"{ESPN_BASE}/scoreboard?dates={year}&limit=300", f"espn-year-{year}", ttl=ttl)
    out = []
    for ev in (data or {}).get("events", []):
        comp = (ev.get("competitions") or [{}])[0]
        stage = _ESPN_STAGE.get((ev.get("season") or {}).get("slug"))
        cs = {c.get("homeAway"): c for c in comp.get("competitors", [])}
        h, a = cs.get("home", {}), cs.get("away", {})

        def team(c):
            t = c.get("team", {}) or {}
            return {"id": t.get("id"), "name": t.get("displayName"), "tla": t.get("abbreviation"),
                    "crest": t.get("logo"), "rank": rank_for(t.get("displayName"), year)}

        def sc(c):
            try:
                return int(c.get("score"))
            except (TypeError, ValueError):
                return None

        def sho(c):   # penalty-shootout score (knockout matches level after ET)
            try:
                return int(c.get("shootoutScore"))
            except (TypeError, ValueError):
                return None
        v = comp.get("venue", {}) or {}
        city = (v.get("address", {}) or {}).get("city")
        st = ev.get("status") or {}
        stype = st.get("type") or {}
        out.append({"id": ev.get("id"), "utcDate": ev.get("date"), "status": _espn_status(ev.get("status")),
                    "stage": stage, "group": None, "matchday": None,
                    "venueCity": city, "venueTz": VENUES["cities"].get(city),
                    # live clock / phase (kickoff, 1st/2nd half, HT, ET, PEN…) for in-play matches
                    "clock": st.get("displayClock"), "period": st.get("period"),
                    "detail": stype.get("shortDetail") or stype.get("detail"),
                    "statusState": stype.get("state"),
                    "home": team(h), "away": team(a),
                    "score": {"home": sc(h), "away": sc(a), "winner": None,
                              "pens": ({"home": sho(h), "away": sho(a)}
                                       if (sho(h) is not None or sho(a) is not None) else None)}})
    out.sort(key=lambda x: x["utcDate"] or "")
    return {"dates": unique_dates(out), "matches": out, "source": "espn"}


def get_standings_espn(year, ttl=10 ** 9):
    """Group standings for an edition from ESPN. ttl small for the live edition."""
    data = http_json(f"https://site.api.espn.com/apis/v2/sports/soccer/fifa.world/standings?season={year}",
                     f"espn-standings-{year}", ttl=ttl)
    groups = []
    for ch in (data or {}).get("children", []):
        rows = []
        for e in (ch.get("standings") or {}).get("entries", []):
            t = e.get("team", {}) or {}
            st = {s.get("name"): s.get("value") for s in e.get("stats", [])}
            i = lambda k: int(st.get(k, 0) or 0)
            rows.append({"position": i("rank") or None,
                         "team": {"id": t.get("id"), "name": t.get("displayName"), "tla": t.get("abbreviation"),
                                  "crest": (t.get("logos") or [{}])[0].get("href"),
                                  "rank": rank_for(t.get("displayName"), year)},
                         "playedGames": i("gamesPlayed"), "won": i("wins"), "draw": i("ties"), "lost": i("losses"),
                         "goalsFor": i("pointsFor"), "goalsAgainst": i("pointsAgainst"),
                         "goalDifference": i("pointDifferential"), "points": i("points")})
        rows.sort(key=lambda r: r["position"] or 99)
        groups.append({"group": ch.get("name"), "table": rows})
    return {"groups": groups, "source": "espn"}


_saving = set()


def save_edition(year):
    """Past editions are fixed → persist the whole snapshot (matches, standings, every
    match detail) permanently in the background. Videos are NOT saved (fetched on click)."""
    marker = f"edition-saved-{year}"
    done, _ = _read_cache(marker, 10 ** 9)
    if done:
        return {"saved": True}
    if year in _saving:
        return {"saving": True}
    _saving.add(year)

    def run():
        try:
            data = get_matches_espn(year)          # cached permanently
            get_standings_espn(year)               # cached permanently
            for m in data.get("matches", []):
                get_match_espn(str(m["id"]))       # caches each match's detail (events/venue)
            _write_cache(marker, {"done": True, "matches": len(data.get("matches", []))})
            print(f"[info] edition {year} snapshot saved ({len(data.get('matches', []))} matches)")
        except Exception as e:
            print(f"[warn] save_edition {year}: {e}")
        finally:
            _saving.discard(year)
    threading.Thread(target=run, daemon=True).start()
    return {"saving": True}


def get_standings():
    """Current edition group standings — ESPN, short cache (live). No token, no mock.
    Enriched with each team's yellow/red card totals for display."""
    season = str(CONFIG.get("season"))
    data = get_standings_espn(season, ttl=_live_ttl(season))
    try:
        tot = _team_card_totals_nonblocking(season)
        for g in data.get("groups", []):
            for r in g.get("table", []):
                c = tot.get(_canon((r.get("team") or {}).get("name")), {})
                r["yc"], r["rc"] = c.get("y", 0), c.get("r", 0)
    except Exception as e:
        print(f"[warn] standings cards: {e}")
    return data


def _rank_group_2026(names, pts, gf, ga, cards, res):
    """Order one group best→worst by the FIFA World Cup 2026 key:
    points → head-to-head (pts → GD → GF among the tied teams) → overall GD → overall GF →
    fair play (fewer cards). `res` = (home, away, hs, as) results among these teams (played + simulated)."""
    def ov(n):                                  # overall fallback, higher = better
        return (gf[n] - ga[n], gf[n], -cards.get(n, 0))

    def resolve(group):
        if len(group) == 1:
            return list(group)
        gs = set(group)
        mp = {n: [0, 0, 0] for n in group}      # head-to-head [pts, gd, gf]
        for h, a, hs, asc in res:
            if h in gs and a in gs:
                mp[h][2] += hs; mp[a][2] += asc
                mp[h][1] += hs - asc; mp[a][1] += asc - hs
                if hs > asc:
                    mp[h][0] += 3
                elif asc > hs:
                    mp[a][0] += 3
                else:
                    mp[h][0] += 1; mp[a][0] += 1
        srt = sorted(group, key=lambda n: (mp[n][0], mp[n][1], mp[n][2]), reverse=True)
        out, i = [], 0
        while i < len(srt):
            j = i + 1
            while j < len(srt) and (mp[srt[j]][0], mp[srt[j]][1], mp[srt[j]][2]) == \
                    (mp[srt[i]][0], mp[srt[i]][1], mp[srt[i]][2]):
                j += 1
            cl = srt[i:j]
            if len(cl) > 1 and len(cl) < len(group):
                out.extend(resolve(cl))          # partly separated → re-apply head-to-head
            elif len(cl) > 1:
                out.extend(sorted(cl, key=ov, reverse=True))   # unbreakable by H2H → overall stats
            else:
                out.append(cl[0])
            i = j
        return out

    bp, out, i = sorted(names, key=lambda n: pts[n], reverse=True), [], 0
    while i < len(bp):                            # cluster by equal points, then resolve each block
        j = i + 1
        while j < len(bp) and pts[bp[j]] == pts[bp[i]]:
            j += 1
        cl = bp[i:j]
        out.extend(resolve(cl) if len(cl) > 1 else [cl[0]])
        i = j
    return out


def third_place_odds(N=4000):
    """Monte-Carlo advancement odds per team (2026 format: top 2 of each group + 8 best 3rds → R32).

    For each of N trials:
      1. every UNPLAYED group game gets a scoreline from independent Poisson draws whose means come
         from each side's goals-for/against pace so far (shrunk to the tournament avg), with a mild
         home edge;
      2. each group is ranked by the FIFA key (points → goal difference → goals for → fair play /
         fewer cards);
      3. the 12 third-placed teams are ranked by the same key and the best 8 advance.

    Returns, per team:
      adv  — P(reach R32)               %    (top-2 OR a best-8 third)
      pos  — {p1,p2,p3,p4}              %    final group-position distribution
             t3                         %    P(finish 3rd AND make the best-8)
      could3 — teams that can still finish 3rd in their group
    Recomputed only when a match finishes (cached by the finished-results signature)."""
    sig = _group_stage_signature()              # group-stage only → stable through the knockouts
    cached, _ = _read_cache("advodds", 10 ** 9)
    if cached is not None and cached.get("sig") == sig:
        return cached.get("data")
    base, grp, cards = {}, {}, {}
    for g in get_standings().get("groups", []):
        L = re.sub(r"(?i)^group\s*", "", g.get("group") or "").strip()
        for r in g.get("table", []):
            nm = (r.get("team") or {}).get("name")
            if not nm:
                continue
            base[nm] = {"pts": r.get("points", 0) or 0, "gf": r.get("goalsFor", 0) or 0,
                        "ga": r.get("goalsAgainst", 0) or 0, "p": r.get("playedGames", 0) or 0}
            cards[nm] = (r.get("yc", 0) or 0) + 3 * (r.get("rc", 0) or 0)
            grp.setdefault(L, []).append(nm)
    nm2L = {nm: L for L, names in grp.items() for nm in names}
    # head-to-head needs the played results among each group's teams
    fixed_by_L = {}
    for m in get_matches().get("matches", []):
        if m.get("stage") != "GROUP_STAGE" or m.get("status") != "FINISHED":
            continue
        h = (m.get("home") or {}).get("name"); a = (m.get("away") or {}).get("name")
        sc = m.get("score") or {}; hs, asc = sc.get("home"), sc.get("away")
        if h in base and a in base and hs is not None and asc is not None and nm2L.get(h):
            fixed_by_L.setdefault(nm2L[h], []).append((h, a, hs, asc))
    AVG, KK = 1.3, 2.0   # shrink goal pace toward the tournament average
    rate = {nm: {"atk": (b["gf"] + AVG * KK) / (b["p"] + KK), "dfn": (b["ga"] + AVG * KK) / (b["p"] + KK)}
            for nm, b in base.items()}
    rem = [((m.get("home") or {}).get("name"), (m.get("away") or {}).get("name"))
           for m in get_matches().get("matches", [])
           if m.get("stage") == "GROUP_STAGE" and m.get("status") != "FINISHED"
           and (m.get("home") or {}).get("name") in base and (m.get("away") or {}).get("name") in base]

    def pois(lam):
        lam = min(lam, 8.0); cut = math.exp(-lam); k, p = 0, 1.0
        while True:
            k += 1; p *= random.random()
            if p <= cut:
                return k - 1
    key = lambda n, pts, gf, ga: (pts[n], gf[n] - ga[n], gf[n], -cards.get(n, 0))
    adv = {nm: 0 for nm in base}
    third_cnt = {nm: 0 for nm in base}     # how often a team finishes 3rd in its group
    # final group-position distribution: index 0..3 = 1st..4th
    posc = {nm: [0, 0, 0, 0] for nm in base}
    t3adv = {nm: 0 for nm in base}         # finished 3rd AND made the best-8 (advanced as a third)
    for _ in range(N):
        pts = {nm: base[nm]["pts"] for nm in base}
        gf = {nm: base[nm]["gf"] for nm in base}
        ga = {nm: base[nm]["ga"] for nm in base}
        simres = {L: list(v) for L, v in fixed_by_L.items()}   # head-to-head results, this trial
        for h, a in rem:
            lh = max(.2, (rate[h]["atk"] + rate[a]["dfn"]) / 2 * 1.08)   # mild home edge
            la = max(.2, (rate[a]["atk"] + rate[h]["dfn"]) / 2 * 0.94)
            gh, gaw = pois(lh), pois(la)
            gf[h] += gh; ga[h] += gaw; gf[a] += gaw; ga[a] += gh
            if gh > gaw:
                pts[h] += 3
            elif gaw > gh:
                pts[a] += 3
            else:
                pts[h] += 1; pts[a] += 1
            L = nm2L.get(h)
            if L:
                simres.setdefault(L, []).append((h, a, gh, gaw))
        adv_set, thirds = set(), []
        for L, names in grp.items():
            order = _rank_group_2026(names, pts, gf, ga, cards, simres.get(L, []))
            for i, n in enumerate(order[:4]):
                posc[n][i] += 1                    # record final position 1st..4th
            if order:
                adv_set.add(order[0])
            if len(order) > 1:
                adv_set.add(order[1])
            if len(order) > 2:
                thirds.append(order[2]); third_cnt[order[2]] += 1
        thirds.sort(key=lambda n: key(n, pts, gf, ga), reverse=True)
        best3 = set(thirds[:8])                     # the 8 best third-placed teams advance
        for n in best3:
            adv_set.add(n); t3adv[n] += 1
        for n in adv_set:
            adv[n] += 1
    pct = lambda c: round(100 * c / N)
    out = {"adv": {nm: pct(adv[nm]) for nm in base},
           # per-team final-position probabilities + P(advance as a 3rd place)
           "pos": {nm: {"p1": pct(posc[nm][0]), "p2": pct(posc[nm][1]),
                        "p3": pct(posc[nm][2]), "p4": pct(posc[nm][3]),
                        "t3": pct(t3adv[nm])} for nm in base},
           "could3": [nm for nm in base if third_cnt[nm] > 0]}   # teams that can still finish 3rd
    _write_cache("advodds", {"sig": sig, "data": out})
    return out


_advtrend = set()


def advancement_trend(N=3000):
    """R32 advancement story for the current edition, stepped PER ROUND-3 MATCH (calendar dates
    mix groups): baseline = end of round 2, then each round-3 match applied in kickoff order with
    the rest Monte-Carlo'd. Per checkpoint: every team's advancement probability + each
    third-placed team's wildcard bingo vs the other groups' 3rd places. Cached by the
    finished-results signature (recomputed only on a new result)."""
    sig = _group_stage_signature()              # group-stage only → stable through the knockouts
    cached, _ = _read_cache("advtrend", 10 ** 9)
    if cached is not None and cached.get("sig") == sig:
        return cached.get("data")
    season = str(CONFIG.get("season"))
    team2L = {}
    for g in (get_standings_espn(season) or {}).get("groups", []):
        L = re.sub(r"(?i)^group\s*", "", g.get("group") or "").strip()
        for r in g.get("table", []):
            nm = (r.get("team") or {}).get("name")
            if nm:
                team2L[nm] = L
    groups = {}
    for nm, L in team2L.items():
        groups.setdefault(L, []).append(nm)
    GL = sorted(groups)
    if not team2L:
        return {"labels": [], "marks": [], "teams": [], "boards": {}, "order": [],
                "groupThirds": [], "groupsList": GL}
    cards = {nm: 0 for nm in team2L}
    gs = []
    for m in get_matches().get("matches", []):
        if m.get("stage") != "GROUP_STAGE":
            continue
        h = (m.get("home") or {}).get("name"); a = (m.get("away") or {}).get("name")
        if h not in team2L or a not in team2L:
            continue
        sc = m.get("score") or {}
        gs.append({"utc": m.get("utcDate") or "", "L": team2L[h], "h": h, "a": a,
                   "hs": sc.get("home"), "as": sc.get("away"),
                   "fin": m.get("status") == "FINISHED" and sc.get("home") is not None})
    # assign each group-stage match a matchday (1/2/3) by kickoff order within its group
    bygrp = {}
    for r in gs:
        bygrp.setdefault(r["L"], []).append(r)
    for arr in bygrp.values():
        arr.sort(key=lambda r: r["utc"])
        for i, r in enumerate(arr):
            r["md"] = i // 2 + 1
    # Round-3 view: x-axis is PER MATCH (calendar dates mix groups). Baseline = end of round 2,
    # then step through each round-3 match in kickoff order; the rest of round 3 is simulated.
    base_apply = [r for r in gs if r.get("md", 9) <= 2 and r["fin"] and r["hs"] is not None]
    r3fin = sorted([r for r in gs if r.get("md") == 3 and r["fin"] and r["hs"] is not None], key=lambda r: r["utc"])
    always_rem = [r for r in gs if not (r["fin"] and r["hs"] is not None)]
    labels = ["2R"] + [str(i + 1) for i in range(len(r3fin))]
    marks = [None] + [{"h": r["h"], "a": r["a"], "hs": r["hs"], "as": r["as"], "L": r["L"]} for r in r3fin]

    def pois(lam):
        lam = min(lam, 8.0); cut = math.exp(-lam); k, p = 0, 1.0
        while True:
            k += 1; p *= random.random()
            if p <= cut:
                return k - 1
    tkey = lambda b, n: (b[n]["pts"], b[n]["gf"] - b[n]["ga"], b[n]["gf"])
    AVG, KK = 1.3, 2.0

    def state_after(k):                          # results through round-3 match #k applied; rest simulated
        base = {nm: {"pts": 0, "gf": 0, "ga": 0, "p": 0} for nm in team2L}
        fixed = {L: [] for L in groups}
        for r in base_apply + r3fin[:k]:
            h, a, hs, asc = r["h"], r["a"], r["hs"], r["as"]
            base[h]["gf"] += hs; base[h]["ga"] += asc; base[h]["p"] += 1
            base[a]["gf"] += asc; base[a]["ga"] += hs; base[a]["p"] += 1
            base[h]["pts"] += 3 if hs > asc else 1 if hs == asc else 0
            base[a]["pts"] += 3 if asc > hs else 1 if asc == hs else 0
            fixed[r["L"]].append((h, a, hs, asc))
        rem = [(r["h"], r["a"], r["L"]) for r in (r3fin[k:] + always_rem)]
        orders = {L: _rank_group_2026(names, {n: base[n]["pts"] for n in names},
                  {n: base[n]["gf"] for n in names}, {n: base[n]["ga"] for n in names}, cards, fixed.get(L, []))
                  for L, names in groups.items()}
        return base, orders, fixed, rem

    states = [state_after(k) for k in range(len(r3fin) + 1)]
    trend = {nm: [] for nm in team2L}
    for base, orders, fixed, rem in states:
        rate = {nm: {"atk": (b["gf"] + AVG * KK) / (b["p"] + KK), "dfn": (b["ga"] + AVG * KK) / (b["p"] + KK)}
                for nm, b in base.items()}
        adv = {nm: 0 for nm in team2L}
        for _ in range(N):
            pts = {nm: base[nm]["pts"] for nm in team2L}
            gf = {nm: base[nm]["gf"] for nm in team2L}; ga = {nm: base[nm]["ga"] for nm in team2L}
            simres = {L: list(v) for L, v in fixed.items()}
            for h, a, L in rem:
                lh = max(.2, (rate[h]["atk"] + rate[a]["dfn"]) / 2 * 1.08)
                la = max(.2, (rate[a]["atk"] + rate[h]["dfn"]) / 2 * 0.94)
                gh, gaw = pois(lh), pois(la)
                gf[h] += gh; ga[h] += gaw; gf[a] += gaw; ga[a] += gh
                if gh > gaw:
                    pts[h] += 3
                elif gaw > gh:
                    pts[a] += 3
                else:
                    pts[h] += 1; pts[a] += 1
                simres.setdefault(L, []).append((h, a, gh, gaw))
            adv_set, thirds = set(), []
            for L, names in groups.items():
                order = _rank_group_2026(names, pts, gf, ga, cards, simres.get(L, []))
                if order:
                    adv_set.add(order[0])
                if len(order) > 1:
                    adv_set.add(order[1])
                if len(order) > 2:
                    thirds.append(order[2])
            thirds.sort(key=lambda n: (pts[n], gf[n] - ga[n], gf[n]), reverse=True)
            for n in thirds[:8]:
                adv_set.add(n)
            for n in adv_set:
                adv[n] += 1
        for nm in team2L:
            trend[nm].append(round(100 * adv[nm] / N, 1))

    # shared per-day 3rd place of each group (so boards don't repeat it)
    groupThirds = []
    for base, orders, _f, _r in states:
        row = {}
        for L in GL:
            od = orders[L]; third = od[2] if len(od) > 2 else None
            t = base[third] if third else None
            row[L] = {"team": third, "has": bool(third) and (t["p"] > 0 if t else False),
                      "pts": t["pts"] if t else 0, "gd": (t["gf"] - t["ga"]) if t else 0, "gf": t["gf"] if t else 0}
        groupThirds.append(row)

    # focus = every team that finished 3rd in its group; sorted by final 3rd-place key
    fbase, forders = states[-1][0], states[-1][1]
    finals3 = [forders[L][2] for L in groups if len(forders[L]) > 2]
    finals3.sort(key=lambda n: tkey(fbase, n), reverse=True)
    advancers = set(finals3[:8])

    boards = {}
    for F in finals3:
        gF = team2L[F]
        days = []
        for di, (base, orders, _f, _r) in enumerate(states):
            bF = base[F]; lineF = [bF["pts"], bF["gf"] - bF["ga"], bF["gf"]]
            favL = []
            for L in GL:
                if L == gF:
                    continue
                c = groupThirds[di][L]
                if c["has"] and bF["p"] > 0 and tuple(lineF) > (c["pts"], c["gd"], c["gf"]):
                    favL.append(L)
            thirds_now = [orders[L][2] for L in groups if len(orders[L]) > 2 and base[orders[L][2]]["p"] > 0]
            rank3 = None
            if F in thirds_now:
                ts = sorted(thirds_now, key=lambda n: tkey(base, n), reverse=True)
                rank3 = [ts.index(F) + 1, len(ts)]
            days.append({"adv": trend[F][di], "line": lineF, "favL": favL, "fav": len(favL),
                         "rank3": rank3, "grpRank": orders[gF].index(F) + 1 if F in orders[gF] else None})
        ft = boards
        boards[F] = {"group": gF, "advanced": F in advancers,
                     "finalRank3": days[-1]["rank3"], "peak": max(trend[F]),
                     "days": days}

    teams = [{"name": nm, "group": team2L[nm], "trend": trend[nm], "third": nm in boards}
             for nm in team2L]
    data = {"labels": labels, "marks": marks, "groupsList": GL, "teams": teams, "order": finals3,
            "groupThirds": groupThirds, "boards": boards}
    _write_cache("advtrend", {"sig": sig, "data": data})
    return data


def get_team(team_id):
    raw, source = fd_get(f"/teams/{team_id}", f"team-{team_id}", ttl=10 ** 9)  # squad/coach static
    if raw is None and CONFIG.get("use_mock_when_unavailable", True):
        raw, source = mock_team(team_id), "mock"
    if raw is None:
        return {"team": None, "source": "mock"}
    team = normalize_team(raw)
    team["info"] = country_info(team.get("name"))   # capital/population/area + World Cup history
    # squad from ESPN (free): photo, height/weight, availability; AF fills face photos by jersey number
    try:
        roster = espn_roster(team.get("name"), season=CONFIG.get("season"))
    except Exception as e:
        print(f"[warn] espn roster: {e}")
        roster = None
    if roster:
        # photos: cache-only for a fast response; fetch the rest in the background for next time
        for pl in roster:
            if not pl.get("photo"):
                ph = tsdb_player_cached(pl.get("name"))
                if ph:
                    pl["photo"] = ph
        warm_photos_bg([pl.get("name") for pl in roster if not pl.get("photo")])
        team["squad"] = roster
        team["hasPhotos"] = True
    return {"team": team, "source": source}


# ---- match detail: ESPN (events/venue/live) + venue image + Open-Meteo ------
ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world"

# team-name aliases so football-data names match ESPN's
_ALIAS = {
    "turkey": "turkiye", "southkorea": "korearepublic", "korea": "korearepublic",
    "republicofkorea": "korearepublic", "unitedstates": "usa", "us": "usa",
    "ivorycoast": "cotedivoire", "czechia": "czechrepublic", "congodr": "drcongo",
    "democraticrepublicofcongo": "drcongo", "capeverdeislands": "capeverde",
    "iranislamicrepublic": "iran", "iriran": "iran", "bosniaherzegovina": "bosnia",
}


def _norm(s):
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return "".join(ch for ch in s.lower() if ch.isalnum())


def _canon(name):
    n = _norm(name)
    return _ALIAS.get(n, n)


def http_json(url, cache_key, ttl, headers=None):
    cached, fresh = _read_cache(cache_key, ttl)
    if cached is not None and fresh:
        return cached
    if "thesportsdb.com" in url:   # respect the free tier's rate limit
        _tsdb_throttle()
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "WorldCupPilot/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=12) as r:
            data = json.loads(r.read().decode("utf-8"))
        _write_cache(cache_key, data)
        return data
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, ValueError) as e:
        print(f"[warn] {cache_key}: {e}")
        return cached


def espn_find(date_yyyymmdd, home, away):
    """Find the ESPN event on a date whose two teams match home/away."""
    data = http_json(f"{ESPN_BASE}/scoreboard?dates={date_yyyymmdd}",
                     f"espn-sb-{date_yyyymmdd}", ttl=30)
    if not data:
        return None
    want = {_canon(home), _canon(away)}
    for ev in data.get("events", []):
        comp = (ev.get("competitions") or [{}])[0]
        names = {_canon(c.get("team", {}).get("displayName")) for c in comp.get("competitors", [])}
        if want & names == want:
            return ev
    return None


def _ml_to_decimal(ml):
    """American moneyline -> decimal odds (배당)."""
    if ml in (None, ""):
        return None
    try:
        ml = float(ml)
    except (TypeError, ValueError):
        return None
    return round((ml / 100 + 1) if ml > 0 else (100 / abs(ml) + 1), 2)


def espn_events(espn_id, home_team_id, away_team_id):
    data = http_json(f"{ESPN_BASE}/summary?event={espn_id}", f"espn-sum-{espn_id}", ttl=30)
    if not data:
        return [], None, None
    out = []
    for k in data.get("keyEvents", []):
        ttext = (k.get("type", {}) or {}).get("text", "")
        low = ttext.lower()
        if not any(w in low for w in ("goal", "card", "penalty")):
            continue
        tid = str((k.get("team", {}) or {}).get("id") or "")
        side = "home" if tid == str(home_team_id) else ("away" if tid == str(away_team_id) else None)
        players = [a.get("athlete", {}).get("displayName") for a in k.get("participants", [])]
        out.append({
            "minute": (k.get("clock", {}) or {}).get("displayValue", ""),
            "type": ttext, "side": side,
            "player": next((p for p in players if p), ""),
            "text": k.get("text", ""),
        })
    venue = (data.get("gameInfo", {}) or {}).get("venue", {})
    # 1X2 betting odds from the pickcenter (DraftKings etc.)
    odds = None
    pc = data.get("pickcenter") or []
    if pc:
        p = pc[0]
        home = _ml_to_decimal((p.get("homeTeamOdds") or {}).get("moneyLine"))
        draw = _ml_to_decimal((p.get("drawOdds") or {}).get("moneyLine"))
        away = _ml_to_decimal((p.get("awayTeamOdds") or {}).get("moneyLine"))
        if any(v is not None for v in (home, draw, away)):
            odds = {"provider": (p.get("provider") or {}).get("name"),
                    "home": home, "draw": draw, "away": away}
    return out, venue, odds


def _wiki_summary(name):
    """Wikipedia REST page summary (cached) — has image + geo coordinates."""
    if not name:
        return None
    title = urllib.parse.quote(name.replace(" ", "_"))
    return http_json(f"https://en.wikipedia.org/api/rest_v1/page/summary/{title}",
                     f"wiki-{_norm(name)}", ttl=10 ** 9)


def wiki_image(name):
    """Stadium photo from the Wikipedia page summary (free, reliable for venues)."""
    data = _wiki_summary(name)
    if not data:
        return None
    return ((data.get("originalimage") or {}).get("source")
            or (data.get("thumbnail") or {}).get("source"))


def venue_aerial(name):
    """Aerial/satellite view of the stadium (Esri World Imagery, free, no key),
    centered on the venue's Wikipedia coordinates. Falls back to None if no coords."""
    data = _wiki_summary(name)
    c = (data or {}).get("coordinates") or {}
    lat, lon = c.get("lat"), c.get("lon")
    if lat is None or lon is None:
        return None
    dlat, dlon = 0.0016, 0.0046          # wide banner framing (~whole stadium + surroundings)
    bbox = f"{lon - dlon},{lat - dlat},{lon + dlon},{lat + dlat}"
    return ("https://services.arcgisonline.com/arcgis/rest/services/World_Imagery/MapServer/export"
            f"?bbox={bbox}&bboxSR=4326&imageSR=4326&size=1280,440&format=jpg&f=image")


def wiki_player_photo(name):
    """Best-effort player photo from Wikipedia when ESPN/TheSportsDB have none.
    Finds the footballer's page via search, then takes its summary image (CC-licensed)."""
    if not name:
        return None
    cache_key = f"wikipl-{_norm(name)}"
    cached, fresh = _read_cache(cache_key, 10 ** 9)
    if cached is not None and fresh:
        return cached.get("photo")
    photo = None
    q = urllib.parse.quote(f"{name} footballer")
    s = http_json(f"https://en.wikipedia.org/w/rest.php/v1/search/page?q={q}&limit=1",
                  f"wikipl-s-{_norm(name)}", ttl=10 ** 9)
    pages = (s or {}).get("pages") or []
    if pages:
        title = pages[0].get("key") or pages[0].get("title")
        if title:
            photo = wiki_image(title)
        if not photo:
            th = (pages[0].get("thumbnail") or {}).get("url")
            if th:
                photo = ("https:" + th) if th.startswith("//") else th
    _write_cache(cache_key, {"photo": photo})
    return photo


def geocode(city):
    if not city:
        return None
    url = f"https://geocoding-api.open-meteo.com/v1/search?name={urllib.parse.quote(city)}&count=1&language=en"
    data = http_json(url, f"geo-{_norm(city)}", ttl=10 ** 9)
    res = (data or {}).get("results") or []
    if not res:
        return None
    return (res[0]["latitude"], res[0]["longitude"], res[0].get("elevation"))


def weather_at(city, utc_iso):
    coord = geocode(city)
    if not coord:
        return {}
    lat, lon, elev = coord
    day, hour = utc_iso[:10], utc_iso[11:13]
    base = ("https://api.open-meteo.com/v1/forecast"
            f"?latitude={lat}&longitude={lon}"
            "&hourly=temperature_2m,relative_humidity_2m,wind_speed_10m"
            f"&start_date={day}&end_date={day}&timezone=UTC")
    data = http_json(base, f"wx2-{_norm(city)}-{day}", ttl=1800)
    h = (data or {}).get("hourly") or {}
    times = h.get("time") or []
    target = f"{day}T{hour}:00"
    idx = next((i for i, t in enumerate(times) if t == target), None)
    if idx is None and times:
        idx = 0
    if idx is None:
        return {}
    return {"temp": (h.get("temperature_2m") or [None])[idx],
            "humidity": (h.get("relative_humidity_2m") or [None])[idx],
            "wind": (h.get("wind_speed_10m") or [None])[idx],
            "alt": elev}


def youtube_first_video(query):
    """First YouTube videoId for a query (scraped from results HTML, no API key)."""
    key = "yt-" + _norm(query)[:60]
    cached, fresh = _read_cache(key, 86400)   # 1 day — FIFA may upload newer/better videos
    if cached and cached.get("videoId") and fresh:
        return cached["videoId"]
    url = ("https://www.youtube.com/results?search_query="
           + urllib.parse.quote(query) + "&hl=en&gl=US")
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
    })
    try:
        with urllib.request.urlopen(req, timeout=12) as r:
            html = r.read().decode("utf-8", "ignore")
    except Exception as e:
        print(f"[warn] youtube search: {e}")
        return cached.get("videoId") if cached else None
    m = re.search(r'"videoId":"([A-Za-z0-9_-]{11})"', html)
    vid = m.group(1) if m else None
    if vid:
        _write_cache(key, {"videoId": vid})
    return vid


ESPN_POS = {"G": "Goalkeeper", "D": "Defence", "M": "Midfield", "F": "Offence",
            "Goalkeeper": "Goalkeeper", "Defender": "Defence",
            "Midfielder": "Midfield", "Forward": "Offence", "Attacker": "Offence"}


def _in_to_cm(v):
    try:
        return round(float(v) * 2.54)
    except (TypeError, ValueError):
        return None


def _lb_to_kg(v):
    try:
        return round(float(v) * 0.4536)
    except (TypeError, ValueError):
        return None


def espn_team_id(name):
    data = http_json(f"{ESPN_BASE}/teams", "espn-teams", ttl=30 * 86400)
    try:
        teams = data["sports"][0]["leagues"][0]["teams"]
    except (TypeError, KeyError, IndexError):
        return None
    want = _canon(name)
    for x in teams:
        t = x.get("team", {}) or {}
        if want in (_canon(t.get("displayName")), _canon(t.get("shortDisplayName")), _canon(t.get("name"))):
            return t.get("id")
    return None


def espn_roster(name, season=None):
    """Squad from ESPN for a given edition (season). dateOfBirth → age AT that tournament."""
    tid = espn_team_id(name)
    if not tid:
        return None
    qs = f"?season={season}" if season else ""
    # current edition → re-sync once a day (picks up newly-added ESPN World Cup headshots +
    # injury/availability changes); finished past editions never change → permanent cache
    ttl = 86400 if (not season or str(season) == str(CONFIG.get("season"))) else 10 ** 9
    data = http_json(f"{ESPN_BASE}/teams/{tid}/roster{qs}", f"espn-roster-{tid}-{season or 'cur'}", ttl=ttl)
    if not data:
        return None
    # availability (injury/suspension) only matters for the current edition;
    # for past editions everyone listed actually took part — ignore today's status
    is_current = (not season) or str(season) == str(CONFIG.get("season"))
    out = []
    for a in data.get("athletes", []):
        pos = a.get("position", {}) or {}
        inj = a.get("injuries") or []
        st = a.get("status", {}) or {}
        if not is_current:
            avail, status_text = True, None
        elif inj:
            avail, status_text = False, "부상"
        elif st.get("type") and st.get("type") != "active":
            avail, status_text = False, st.get("name")
        else:
            avail, status_text = True, None
        bp = a.get("birthPlace") or {}
        photo = (a.get("headshot") or {}).get("href")   # ESPN soccer headshots are sparse; TheSportsDB fills most
        dob = (a.get("dateOfBirth") or "")[:10]
        age = a.get("age")
        if season and dob[:4].isdigit():        # age at the tournament, not today
            age = int(season) - int(dob[:4])
        out.append({
            "id": a.get("id"),
            "name": a.get("displayName") or a.get("fullName"),
            "number": a.get("jersey"),
            "position": ESPN_POS.get(pos.get("abbreviation")) or ESPN_POS.get(pos.get("name")) or "기타",
            "age": age,
            "dateOfBirth": dob,
            "photo": photo,
            "height": _in_to_cm(a.get("height")),
            "weight": _lb_to_kg(a.get("weight")),
            "birthPlace": ", ".join(x for x in (bp.get("city"), bp.get("country")) if x),
            "club": (a.get("defaultTeam") or {}).get("displayName"),
            "available": avail,
            "statusText": status_text,
        })
    return out or None


TSDB = "https://www.thesportsdb.com/api/v1/json/3"


def tsdb_player(name):
    """{photo, club} for a player by name from TheSportsDB (free, no daily cap), cached."""
    if not name:
        return {}
    data = http_json(f"{TSDB}/searchplayers.php?p={urllib.parse.quote(name)}",
                     f"tsdb-pl-{_norm(name)}", ttl=10 ** 9)
    for p in (data or {}).get("player") or []:
        return {"photo": p.get("strThumb") or p.get("strCutout"), "club": p.get("strTeam")}
    return {}


def tsdb_player_cached(name):
    """Cache-only (no network) photo lookup — keeps team detail fast."""
    if not name:
        return None
    cached, _ = _read_cache(f"tsdb-pl-{_norm(name)}", 10 ** 9)
    for p in (cached or {}).get("player") or []:
        return p.get("strThumb") or p.get("strCutout")
    return None


# single background worker drains a photo-warm queue (avoids a thread storm + dedupes)
_warm_q = queue.Queue()
_warm_seen = set()


def _warm_worker():
    while True:
        name = _warm_q.get()
        try:
            r = tsdb_player(name)            # throttled fetch + permanent cache (resolves the URL)
            if r and r.get("photo"):
                fetch_image(r["photo"])      # …and download the bytes into the /img disk cache
        except Exception:
            pass
        _warm_q.task_done()


threading.Thread(target=_warm_worker, daemon=True).start()


def warm_photos_bg(names):
    """Queue missing player photos for the single background worker (gentle, deduped)."""
    for n in names:
        if n and n not in _warm_seen:
            _warm_seen.add(n)
            _warm_q.put(n)


# separate queue: pre-download already-known image URLs (ESPN headshots, resolved photos) to disk
_img_q = queue.Queue()
_img_seen = set()


def _img_warm_worker():
    while True:
        u = _img_q.get()
        try:
            fetch_image(u)       # download once → /img then serves from disk, no repeat remote hits
        except Exception:
            pass
        _img_q.task_done()


threading.Thread(target=_img_warm_worker, daemon=True).start()


def warm_images_bg(urls):
    """Background-download a batch of image URLs into the local cache (deduped)."""
    for u in urls:
        if u and isinstance(u, str) and u.startswith("http") and u not in _img_seen:
            _img_seen.add(u)
            _img_q.put(u)


_photo_sync_done = set()   # canon team names whose squad photos are fully cached


def _photo_sync_worker():
    """While the app runs, keep downloading EVERY current-edition player's photo until they're
    all cached. Works through one team at a time (its own throttled pass, so the on-demand
    photo queue stays responsive); re-checks periodically for teams added later (knockouts)."""
    time.sleep(8)                                   # let the server finish booting
    while True:
        _req_memo_clear()                           # long-lived thread: drop last pass's memo so ttl=600 re-checks
        try:
            year = CONFIG.get("season")
            data = get_matches_espn(str(year), ttl=600) or {}
            teams, seen = [], set()
            for m in data.get("matches", []):
                for sd in ("home", "away"):
                    nm = (m.get(sd) or {}).get("name")
                    if nm and _canon(nm) not in seen:
                        seen.add(_canon(nm)); teams.append(nm)
            for tn in teams:
                cn = _canon(tn)
                if cn in _photo_sync_done:
                    continue
                try:
                    roster = espn_roster(tn, season=year) or []
                except Exception:
                    continue
                ok = True
                for p in roster:
                    nm, ph = p.get("name"), p.get("photo")
                    try:
                        if not ph:                  # no ESPN headshot → resolve via TheSportsDB
                            r = tsdb_player(nm)      # throttled internally (shared lock)
                            ph = r.get("photo") if r else None
                        if ph:
                            if not fetch_image(ph):  # download bytes; None = failed (retry next pass)
                                ok = False
                        else:
                            ok = False               # no photo found anywhere yet
                    except Exception:
                        ok = False
                if ok:
                    _photo_sync_done.add(cn)         # fully cached → skip next passes
        except Exception as e:
            print(f"[warn] photo sync: {e}")
        time.sleep(900)                             # re-sync every 15 min while open


threading.Thread(target=_photo_sync_worker, daemon=True).start()


def tsdb_team_country(club):
    if not club:
        return None
    data = http_json(f"{TSDB}/searchteams.php?t={urllib.parse.quote(club)}",
                     f"tsdb-club-{_norm(club)}", ttl=10 ** 9)
    for t in (data or {}).get("teams") or []:
        if t.get("strSport") == "Soccer" and t.get("strCountry"):
            return t.get("strCountry")
    return None


def tsdb_player_clubinfo(name):
    info = tsdb_player(name)
    club = info.get("club")
    photo = info.get("photo") or wiki_player_photo(name)   # Wikipedia fallback for missing photos
    return {"club": club, "clubCountry": tsdb_team_country(club) if club else None, "photo": photo}


def player_names(name):
    """Player name in en/ko/ja/zh via Wikipedia language links (e.g. 久保建英 / 구보 다케후사)."""
    if not name:
        return {"en": name}
    res = {"en": name}
    try:
        url = ("https://en.wikipedia.org/w/api.php?action=query&prop=langlinks&redirects=1"
               "&lllimit=500&format=json&titles=" + urllib.parse.quote(name))
        d = http_json(url, f"pname-{_norm(name)}", ttl=10 ** 9)
        for p in (((d or {}).get("query") or {}).get("pages") or {}).values():
            ll = {x.get("lang"): x.get("*") for x in (p.get("langlinks") or [])}
            res = {"en": p.get("title") or name, "ko": ll.get("ko"), "ja": ll.get("ja"), "zh": ll.get("zh")}
            break
    except Exception:
        pass
    return res


def _parse_box_stats(data):
    """ESPN boxscore → {'home': {statName: value}, 'away': {...}} (possession, shots, corners, …)."""
    out = {"home": {}, "away": {}}
    for t in ((data.get("boxscore") or {}).get("teams") or []):
        side = t.get("homeAway")
        if side not in ("home", "away"):
            continue
        for s in (t.get("statistics") or []):
            out[side][s.get("name")] = s.get("displayValue")
    return out if (out["home"] or out["away"]) else None


def _parse_shootout(data, home_name, away_name):
    """ESPN summary 'shootout' → per-side kick list with player + made/missed, for the X·O timeline.
    Returns {'home':[{n,player,scored}], 'away':[...]} or None when there was no shootout."""
    sh = data.get("shootout") or []
    if not sh:
        return None
    out = {"home": [], "away": []}
    for blk in sh:
        side = ("home" if _canon(blk.get("team")) == _canon(home_name)
                else "away" if _canon(blk.get("team")) == _canon(away_name) else None)
        if not side:
            continue
        for s in (blk.get("shots") or []):
            out[side].append({"n": s.get("shotNumber"), "player": s.get("player"),
                              "scored": bool(s.get("didScore"))})
    return out if (out["home"] or out["away"]) else None


def _parse_subs(data, home_id, away_id):
    """ESPN keyEvents → {'home': [{min,in,inPhoto,out,outPhoto}], 'away': [...]} substitutions."""
    headshot = {}      # athlete id → headshot (same photos the lineup uses)
    for r in (data.get("rosters") or []):
        for e in (r.get("roster") or []):
            a = e.get("athlete") or {}
            hs = (a.get("headshot") or {}).get("href")
            if a.get("id") and hs:
                headshot[str(a.get("id"))] = hs

    def photo(a):
        return headshot.get(str(a.get("id") or "")) or tsdb_player_cached(a.get("displayName"))
    out = {"home": [], "away": []}
    for e in (data.get("keyEvents") or []):
        if (e.get("type") or {}).get("type") != "substitution":
            continue
        ps = e.get("participants") or []
        if len(ps) < 2:
            continue
        tid = str((e.get("team") or {}).get("id"))
        side = "home" if tid == str(home_id) else "away" if tid == str(away_id) else None
        if not side:
            continue
        pin, pout = ps[0].get("athlete") or {}, ps[1].get("athlete") or {}
        txt = (e.get("text") or "").lower()
        reason = "injury" if "injur" in txt else None      # ESPN: "… because of an injury"
        out[side].append({"min": (e.get("clock") or {}).get("displayValue") or "",
                          "in": pin.get("displayName"), "inPhoto": photo(pin),
                          "out": pout.get("displayName"), "outPhoto": photo(pout), "reason": reason})
    return out if (out["home"] or out["away"]) else None


def _enrich_subs(m):
    """Fill in sub players' photos from the (already-cached) TheSportsDB photos at response time,
    so finished-match subs pick up photos as they get warmed — without rewriting the permanent cache."""
    s = (m or {}).get("subs")
    if not s:
        return
    missing = []
    for arr in s.values():
        for x in arr:
            if not x.get("inPhoto"):
                x["inPhoto"] = tsdb_player_cached(x.get("in"))
                if not x["inPhoto"] and x.get("in"):
                    missing.append(x["in"])
            if not x.get("outPhoto"):
                x["outPhoto"] = tsdb_player_cached(x.get("out"))
                if not x["outPhoto"] and x.get("out"):
                    missing.append(x["out"])
    if missing:
        warm_photos_bg(missing)


def _parse_form(data, home_name, away_name):
    """ESPN lastFiveGames → {'home': [{r,score,opp,atVs,date}], 'away': [...]} (recent W/D/L)."""
    out = {"home": [], "away": []}
    for blk in (data.get("lastFiveGames") or []):
        nm = (blk.get("team") or {}).get("displayName")
        side = "home" if _canon(nm) == _canon(home_name) else "away" if _canon(nm) == _canon(away_name) else None
        if not side:
            continue
        for e in (blk.get("events") or [])[:5]:
            op = e.get("opponent") or {}
            out[side].append({"r": e.get("gameResult"), "score": e.get("score"),
                              "opp": op.get("abbreviation") or op.get("displayName"),
                              "atVs": e.get("atVs"), "date": (e.get("gameDate") or "")[:10]})
    return out if (out["home"] or out["away"]) else None


def _parse_h2h(data, home_name, away_name):
    """ESPN headToHeadGames → home-perspective tally + recent meetings."""
    blk = (data.get("headToHeadGames") or [None])[0]
    if not blk:
        return None
    ref_home = _canon((blk.get("team") or {}).get("displayName")) == _canon(home_name)

    def _i(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return 0
    ev, w, dr, l = [], 0, 0, 0
    for e in (blk.get("events") or [])[:6]:
        opp_id = str((e.get("opponent") or {}).get("id") or "")
        ref_was_home = str(e.get("homeTeamId")) != opp_id
        hs, aw = _i(e.get("homeTeamScore")), _i(e.get("awayTeamScore"))
        ref_sc, opp_sc = (hs, aw) if ref_was_home else (aw, hs)
        hsc, asc = (ref_sc, opp_sc) if ref_home else (opp_sc, ref_sc)   # current home / away goals
        hr = "W" if hsc > asc else "L" if asc > hsc else "D"
        if hr == "W":
            w += 1
        elif hr == "L":
            l += 1
        else:
            dr += 1
        ev.append({"date": (e.get("gameDate") or "")[:10], "hs": hsc, "as": asc, "r": hr})
    return {"tally": {"w": w, "d": dr, "l": l}, "events": ev} if ev else None


def get_match_espn(espn_id):
    """Match detail for a past-edition match (ESPN event id): score, events, venue, weather, stats.
    Memoized per request — _card_suspensions/_player_ratings call this once per finished match."""
    return _req_memo(("match_espn", str(espn_id)), lambda: _get_match_espn(espn_id))


def _get_match_espn(espn_id):
    saved, _ = _read_cache(f"match-final-{espn_id}", 10 ** 9)
    if saved is not None:
        subs_old = bool(saved.get("subs")) and any("reason" not in x for arr in saved["subs"].values() for x in arr)
        h2h_ev = (saved.get("h2h") or {}).get("events") or []
        h2h_old = bool(h2h_ev) and "hs" not in h2h_ev[0]
        need = (not saved.get("stats") or not saved.get("subs") or subs_old
                or "form" not in saved or "h2h" not in saved or h2h_old or "shootout" not in saved)
        if need:   # backfill/upgrade older cached finals (one-time per match, then served from cache)
            try:
                d2 = http_json(f"{ESPN_BASE}/summary?event={espn_id}", f"espn-sum-{espn_id}", ttl=10 ** 9)
                hn, an = (saved.get("home") or {}).get("name"), (saved.get("away") or {}).get("name")
                st = _parse_box_stats(d2 or {})
                sb = _parse_subs(d2 or {}, (saved.get("home") or {}).get("id"), (saved.get("away") or {}).get("id"))
                if st:
                    saved["stats"] = st
                if sb:
                    saved["subs"] = sb
                saved["form"] = _parse_form(d2 or {}, hn, an)
                saved["h2h"] = _parse_h2h(d2 or {}, hn, an)
                saved["shootout"] = _parse_shootout(d2 or {}, hn, an)
                _write_cache(f"match-final-{espn_id}", saved)
            except Exception:
                pass
        _enrich_subs(saved)
        return {"match": saved}
    data = http_json(f"{ESPN_BASE}/summary?event={espn_id}", f"espn-sum-{espn_id}", ttl=60)
    if not data:
        return {"match": None}
    comp = ((data.get("header") or {}).get("competitions") or [{}])[0]
    cs = {c.get("homeAway"): c for c in comp.get("competitors", [])}

    def team(c):
        t = c.get("team", {}) or {}
        return {"id": t.get("id"), "name": t.get("displayName") or t.get("name"),
                "tla": t.get("abbreviation"), "crest": (t.get("logos") or [{}])[0].get("href"), "rank": None}

    def sc(c):
        try:
            return int(c.get("score"))
        except (TypeError, ValueError):
            return None

    def sho(c):
        try:
            return int(c.get("shootoutScore"))
        except (TypeError, ValueError):
            return None
    h, a = cs.get("home", {}), cs.get("away", {})
    status = _espn_status(comp.get("status"))
    events, venue, odds = espn_events(espn_id, (h.get("team") or {}).get("id"), (a.get("team") or {}).get("id"))
    vaddr = (venue or {}).get("address", {}) or {}
    vname, vcity = (venue or {}).get("fullName"), vaddr.get("city")
    utc = comp.get("date") or ""
    out = {"id": espn_id, "home": team(h), "away": team(a), "utcDate": utc, "status": status,
           "score": {"home": sc(h), "away": sc(a),
                     "pens": ({"home": sho(h), "away": sho(a)}
                              if (sho(h) is not None or sho(a) is not None) else None)},
           "group": None, "espnMatched": True, "espnId": espn_id,
           "events": events, "odds": odds, "attendance": None, "stats": _parse_box_stats(data),
           "subs": _parse_subs(data, (h.get("team") or {}).get("id"), (a.get("team") or {}).get("id")),
           "form": _parse_form(data, team(h)["name"], team(a)["name"]),
           "h2h": _parse_h2h(data, team(h)["name"], team(a)["name"]),
           "shootout": _parse_shootout(data, team(h)["name"], team(a)["name"]),
           "venue": {"name": vname, "city": vcity, "country": vaddr.get("country"),
                     "image": ((venue_aerial(vname) or wiki_image(vname)) if vname else None),
                     "capacity": None, "surface": None},
           "weather": weather_at(vcity.split(",")[0].strip(), utc) if (vcity and utc) else {}}
    if status == "FINISHED":
        _write_cache(f"match-final-{espn_id}", out)
    _enrich_subs(out)
    return {"match": out}


def get_team_by_name(name, year=None):
    """Past-edition team detail: country info + WC history + that edition's ESPN squad (당시 나이)."""
    iso = (country_info(name) or {}).get("iso2")
    team = {"id": name, "name": name, "tla": None, "rank": rank_for(name, year),
            "crest": f"https://flagcdn.com/w160/{iso}.png" if iso else None,
            "area": {"name": name, "flag": None}, "founded": None, "coach": {"name": None},
            "info": country_info(name), "squad": [], "hasPhotos": False,
            "elo": round(_team_elos(year or CONFIG.get("season")).get(_norm(name), _init_elo(rank_for(name, year)))
                         + _recent_form_offset(name, year or CONFIG.get("season"))),
            "scorers": _team_scorers(name, year or CONFIG.get("season")),
            "cal": _team_calibration(name, year or CONFIG.get("season"), "~")}
    try:
        roster = espn_roster(name, season=year)
        if roster:
            for pl in roster:
                if not pl.get("photo"):
                    ph = tsdb_player_cached(pl.get("name"))
                    if ph:
                        pl["photo"] = ph
            warm_photos_bg([pl.get("name") for pl in roster if not pl.get("photo")])
            warm_images_bg([pl.get("photo") for pl in roster if pl.get("photo")])   # pre-download photo bytes
            gtally = {_norm(k): v for k, v in _team_goal_tally(name, year or CONFIG.get("season")).items()}
            career = _team_goal_tally_career(name, block=False)   # don't block the response on the heavy all-time tally
            if career is None:                                    # not cached yet → warm it for next time
                career = {}
                threading.Thread(target=lambda: _team_goal_tally_career(name, block=True), daemon=True).start()
            pcards = _team_player_cards(name, year or CONFIG.get("season"))   # yellow/red cards this edition
            for pl in roster:
                nk = _norm(pl.get("name") or "")
                pl["wcGoals"] = gtally.get(nk, 0)          # this edition's goals
                pl["wcGoalsAll"] = career.get(nk, 0)       # all-time World Cup goals (fills in after warm)
                c = pcards.get(nk, {})
                pl["yc"] = c.get("y", 0)                   # yellow cards (경고)
                pl["rc"] = c.get("r", 0)                   # red cards (퇴장)
            team["squad"] = roster
            team["hasPhotos"] = True
    except Exception as e:
        print(f"[warn] team_by_name roster: {e}")
    return {"team": team, "source": "espn"}


def get_match(mid):
    """All match detail comes from ESPN now (the match id is an ESPN event id).
    get_match_espn already serves finished matches from a permanent cache."""
    return get_match_espn(str(mid))


def _espn_event_for(m):
    """Find the ESPN event for a football-data match over its UTC date +/- 1."""
    utc = m.get("utcDate") or ""
    if not utc:
        return None
    base = utc[:10].replace("-", "")
    epoch = time.mktime(time.strptime(utc[:10], "%Y-%m-%d"))
    for dt in (base, time.strftime("%Y%m%d", time.localtime(epoch + 86400)),
               time.strftime("%Y%m%d", time.localtime(epoch - 86400))):
        ev = espn_find(dt, m["home"]["name"], m["away"]["name"])
        if ev:
            return ev
    return None


def build_venues():
    """Map every football-data match id -> host city (via ESPN) and persist it to
    assets/venues.json, so '현지' time becomes per-match accurate. Dev-time step;
    the generated file is then bundled. Returns stats incl. any unknown cities."""
    global VENUES
    path = os.path.join(ASSETS_DIR, "venues.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            doc = json.load(f)
    except Exception:
        doc = {"cities": VENUES.get("cities", {}), "matches": {}}
    cities = doc.get("cities", {})
    by_lower = {k.lower(): k for k in cities}

    mapping, unknown = {}, {}
    stats = {"total": 0, "mapped": 0, "no_event": 0, "unknown_city": 0}
    for m in get_matches()["matches"]:
        stats["total"] += 1
        ev = _espn_event_for(m)
        if not ev:
            stats["no_event"] += 1
            continue
        comp = (ev.get("competitions") or [{}])[0]
        city = ((comp.get("venue", {}) or {}).get("address", {}) or {}).get("city", "")
        key = (city or "").split(",")[0].strip()
        canon = by_lower.get(key.lower())
        if not canon:
            stats["unknown_city"] += 1
            unknown[key] = unknown.get(key, 0) + 1
            continue
        mapping[str(m["id"])] = canon
        stats["mapped"] += 1

    doc["matches"] = mapping
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
    VENUES = load_venues()
    stats["unknown"] = unknown
    return stats


# ---- match prediction (app's own model, not ESPN odds) ---------------------
# Combines: FIFA ranking + this edition's form (points/goal diff from games already
# played) + squad injuries + cards (reds = suspensions). No betting odds involved.
_HOSTS_2026 = {"usa", "canada", "mexico"}


def _team_form_before(name, year, before_utc):
    """Team's record from matches played strictly BEFORE before_utc — a genuine
    PRE-MATCH form (no leakage of the match being predicted or anything later).
    First match → played 0 (nothing to reflect yet)."""
    cn = _canon(name)
    played = pts = gf = ga = 0
    for m in get_matches_espn(str(year), ttl=300).get("matches", []):
        if m.get("status") != "FINISHED" or (m.get("utcDate") or "") >= (before_utc or "~"):
            continue
        sc = m.get("score") or {}
        sh, sa = sc.get("home"), sc.get("away")
        if sh is None or sa is None:
            continue
        if _canon((m.get("home") or {}).get("name")) == cn:
            my, opp = sh, sa
        elif _canon((m.get("away") or {}).get("name")) == cn:
            my, opp = sa, sh
        else:
            continue
        played += 1
        gf += my
        ga += opp
        pts += 3 if my > opp else 1 if my == opp else 0
    return {"played": played, "points": pts, "gf": gf, "ga": ga, "gd": gf - ga}


def _card_suspensions(team_name, year, before_utc):
    """Set of normalized player names suspended for this team's match at `before_utc`, applying
    FIFA card rules chronologically:
      • two yellows in separate matches → banned the next match, then the yellow count resets
      • a red (straight or second-yellow) → banned the next match
      • a ban is served in the very next match, then cleared
      • single yellow cards are wiped once the knockout stage begins (carry-over stops there)."""
    cn = _canon(team_name)
    ms = [m for m in get_matches_espn(str(year), ttl=300).get("matches", [])
          if m.get("status") == "FINISHED"
          and (m.get("utcDate") or "") < (before_utc or "~")
          and cn in (_canon((m.get("home") or {}).get("name")), _canon((m.get("away") or {}).get("name")))]
    ms.sort(key=lambda m: m.get("utcDate") or "")
    yc = {}            # running single-yellow count per player (between resets)
    pending = set()    # players banned for the team's NEXT match
    yc_reset = False
    POST_QF = {"SEMI_FINALS", "THIRD_PLACE", "FINAL"}
    for m in ms:
        if not yc_reset and (m.get("stage") or "") in POST_QF:
            yc = {}; yc_reset = True            # FIFA: single yellows are wiped after the quarter-finals
        pending = set()                          # last match's bans were served in it → clear
        side = "home" if _canon((m.get("home") or {}).get("name")) == cn else "away"
        det = (get_match_espn(str(m.get("id"))) or {}).get("match") or {}
        reds, yel = set(), {}
        for e in det.get("events", []):
            if e.get("side") != side:
                continue
            tp = (e.get("type") or "").lower()
            who = _norm(e.get("player") or "")
            if not who or "card" not in tp:
                continue
            if "red" in tp:                       # straight red or yellow-red sending-off
                reds.add(who)
            elif "yellow" in tp:
                yel[who] = yel.get(who, 0) + 1
        for who, n in yel.items():
            yc[who] = yc.get(who, 0) + n
            if yc[who] >= 2:                      # accumulation → ban next match, reset count
                pending.add(who); yc[who] = 0
        for who in reds:
            pending.add(who); yc[who] = 0
    return pending


def _player_ratings(team_name, year):
    """Per-player importance (≈1.0 average, up to ~2.0 for stars) from ESPN per-match stats this
    edition — goals/assists/shots/saves up, fouls down. Used to weight injury/suspension impact
    so losing a high-rated player hurts the prediction more than losing a fringe player."""
    cn = _canon(team_name)
    key = f"pratings-{cn}-{year}"
    cached, fresh = _read_cache(key, 1800)
    if cached is not None and fresh:
        return cached

    def num(v):
        try:
            return float(str(v).replace(",", ""))
        except Exception:
            return 0.0
    acc = {}
    for m in get_matches_espn(str(year), ttl=300).get("matches", []):
        if m.get("status") != "FINISHED":
            continue
        side = ("home" if _canon((m.get("home") or {}).get("name")) == cn
                else "away" if _canon((m.get("away") or {}).get("name")) == cn else None)
        if not side:
            continue
        data = http_json(f"{ESPN_BASE}/summary?event={m.get('id')}", f"espn-sum-{m.get('id')}", ttl=10 ** 9)
        for r in (data or {}).get("rosters", []):
            if r.get("homeAway") != side:
                continue
            for e in r.get("roster", []):
                nn = _norm((e.get("athlete") or {}).get("displayName") or "")
                if not nn:
                    continue
                st = {s.get("name"): num(s.get("value") if s.get("value") is not None else s.get("displayValue"))
                      for s in (e.get("stats") or [])}
                a = acc.setdefault(nn, {"g": 0, "as": 0, "sot": 0, "sv": 0, "f": 0, "app": 0})
                a["g"] += st.get("totalGoals", st.get("goals", 0))
                a["as"] += st.get("goalAssists", 0)
                a["sot"] += st.get("shotsOnTarget", 0)
                a["sv"] += st.get("saves", 0)
                a["f"] += st.get("foulsCommitted", 0)
                a["app"] += st.get("appearances", 0) or 1
    out = {}
    for nn, a in acc.items():
        app = max(a["app"], 1)
        score = (a["g"] * 4 + a["as"] * 2.5 + a["sot"] * 0.4 + a["sv"] * 0.25 - a["f"] * 0.1) / app
        out[nn] = round(max(1.0, min(2.0, 1.0 + score * 0.12)), 3)   # baseline 1.0, star up to ~2.0
    _write_cache(key, out)
    return out


def _injury_impact(team_name, year, ratings):
    """Weighted count of unavailable players (each weighted by their importance rating)."""
    try:
        roster = espn_roster(team_name, season=year) or []
    except Exception:
        roster = []
    return round(sum(ratings.get(_norm(p.get("name") or ""), 1.0)
                     for p in roster if p.get("available") is False), 2)


_CONFED = {}
for _c, _names in {
    "uefa": "germany france spain england netherlands belgium croatia portugal switzerland austria sweden norway scotland czechia turkiye turkey bosniaherzegovina",
    "conmebol": "brazil argentina uruguay ecuador colombia paraguay",
    "concacaf": "mexico unitedstates canada panama haiti curacao",
    "afc": "japan southkorea korearepublic iran saudiarabia qatar australia uzbekistan jordan iraq",
    "caf": "morocco senegal tunisia egypt algeria ivorycoast ghana southafrica capeverde capeverdeislands congodr",
    "ofc": "newzealand",
}.items():
    for _n in _names.split():
        _CONFED[_n] = "fifa.worldq." + _c


def _team_recent_matches(name, year, before_utc="~"):
    """Recent national-team matches (friendlies + WC qualifiers) before the tournament,
    from ESPN — used to seed a pre-tournament 'recent form' Elo prior."""
    tid = espn_team_id(name)
    if not tid:
        return []
    leagues = ["fifa.friendly"]
    q = _CONFED.get(_norm(name))
    if q:
        leagues.append(q)
    out = []
    for lg in leagues:
        data = http_json(f"https://site.api.espn.com/apis/site/v2/sports/soccer/{lg}/teams/{tid}/schedule",
                         f"sched-{lg}-{tid}", ttl=86400)
        for ev in (data or {}).get("events", []):
            comp = (ev.get("competitions") or [{}])[0]
            if not (((comp.get("status") or {}).get("type") or {}).get("completed")):
                continue
            date = ev.get("date") or ""
            if not date or date >= (before_utc or "~"):
                continue
            comps = comp.get("competitors", [])
            mine = next((c for c in comps if str((c.get("team") or {}).get("id")) == str(tid)), None)
            opp = next((c for c in comps if c is not mine), None)
            if not mine or not opp:
                continue

            def _sc(c):
                s = c.get("score")
                if isinstance(s, dict):
                    s = s.get("value")
                try:
                    return int(float(s))
                except (TypeError, ValueError):
                    return None
            mg, og = _sc(mine), _sc(opp)
            if mg is None or og is None:
                continue
            out.append({"date": date, "opp": (opp.get("team") or {}).get("displayName"),
                        "my": mg, "og": og, "home": mine.get("homeAway") == "home"})
    return out


def _recent_form_offset(name, year, before_utc="~"):
    """Elo prior (±) from the team's last ~12 internationals vs FIFA-rank expectation.
    Cached ~1 day. Positive = recent over-performance."""
    key = f"recentform-{_norm(name)}-{year}"
    cached, fresh = _read_cache(key, 86400)
    if cached is not None and fresh:
        return cached.get("off", 0.0)
    ms = sorted([m for m in _team_recent_matches(name, year, before_utc) if m["date"]],
                key=lambda m: m["date"], reverse=True)[:12]
    if not ms:
        _write_cache(key, {"off": 0.0})
        return 0.0
    tr = min(max(rank_for(name, year) or 60, 1), 130)
    DECAY = 0.8                      # latest internationals weigh much more than older ones
    resid = wsum = 0.0
    for i, m in enumerate(ms):       # ms is most-recent-first
        wi = DECAY ** i
        orr = min(max(rank_for(m["opp"], year) or 60, 1), 130)
        d = (orr - tr) * 3 + (25 if m["home"] else -25)
        we = 1 / (1 + 10 ** (-d / 300))
        ep = 3 * we * 0.76 + 0.24
        ap = 3 if m["my"] > m["og"] else 1 if m["my"] == m["og"] else 0
        resid += wi * (ap - ep)
        wsum += wi
    off = max(-150.0, min(150.0, (resid / wsum) * 40))
    _write_cache(key, {"off": round(off, 1)})
    return round(off, 1)


def _team_attack_defense(name, year, before_utc="~"):
    """Attack / defense / goalkeeper profile from the last ~10 internationals (friendlies +
    qualifiers) BEFORE the tournament. Lets a side be strong in attack yet leaky at the back
    (→ open 3:1-type scores). RECENCY-WEIGHTED: the latest games (especially the previous match)
    count far more than older ones (exponential decay). Shrunk toward the league mean so small
    samples aren't noisy.
      atk = goals scored per game (attack)      mid = control / dominance (goal margin per game)
      dfn = goals conceded per game (defense)   gk  = clean-sheet rate (goalkeeper)
    Cached ~1 day."""
    key = f"atkdef-{_norm(name)}-{year}"
    cached, fresh = _read_cache(key, 86400)
    if cached is not None and fresh:
        return cached
    ms = sorted([m for m in _team_recent_matches(name, year, before_utc) if m["date"]],
                key=lambda m: m["date"], reverse=True)[:10]   # most recent first
    MEAN, K, DECAY = 1.35, 3, 0.72   # K = shrinkage pseudo-games; DECAY → recent games weigh much more
    if not ms:
        res = {"atk": None, "mid": None, "dfn": None, "gk": None, "n": 0}
        _write_cache(key, res)
        return res
    n = len(ms)
    w = [DECAY ** i for i in range(n)]   # i=0 is the latest match → highest weight
    wsum = sum(w)
    wgf = sum(wi * m["my"] for wi, m in zip(w, ms))
    wga = sum(wi * m["og"] for wi, m in zip(w, ms))
    wgd = sum(wi * (m["my"] - m["og"]) for wi, m in zip(w, ms))
    wcs = sum(wi for wi, m in zip(w, ms) if m["og"] == 0)
    res = {"atk": round((wgf + MEAN * K) / (wsum + K), 2),
           "mid": round(wgd / (wsum + K), 2),            # +ve = controls games (dominance)
           "dfn": round((wga + MEAN * K) / (wsum + K), 2),
           "gk": round(wcs / wsum, 2), "n": n}
    _write_cache(key, res)
    return res


def _init_elo(rank):
    """Pre-tournament Elo seeded from FIFA ranking → Strength reflects absolute strength."""
    return 1500 + (50 - min(max(int(rank or 60), 1), 130)) * 7


def _team_elos(year, before_utc="~"):
    """Per-team strength: seeded from FIFA rank, then updated by ACTUAL results (Elo).
    each finished match (chronological, only those before before_utc → pre-match)
    updates both teams, weighted by goal margin and opponent strength."""
    elos = {}
    matches = [m for m in get_matches_espn(str(year), ttl=300).get("matches", [])
               if m.get("status") == "FINISHED" and (m.get("utcDate") or "") < before_utc
               and (m.get("score") or {}).get("home") is not None
               and (m.get("score") or {}).get("away") is not None]
    matches.sort(key=lambda m: m.get("utcDate") or "")
    for m in matches:
        hnm, anm = (m.get("home") or {}).get("name") or "", (m.get("away") or {}).get("name") or ""
        hn, an = _norm(hnm), _norm(anm)
        sh, sa = m["score"]["home"], m["score"]["away"]
        Rh = elos.get(hn, _init_elo(rank_for(hnm, year)))   # seed from FIFA rank on first appearance
        Ra = elos.get(an, _init_elo(rank_for(anm, year)))
        eh = 1 / (1 + 10 ** ((Ra - Rh) / 400))
        sH = 1.0 if sh > sa else 0.5 if sh == sa else 0.0
        k = 32 * (math.log(abs(sh - sa) + 1) + 1)        # bigger swing for bigger wins
        elos[hn] = Rh + k * (sH - eh)
        elos[an] = Ra + k * ((1 - sH) - (1 - eh))
    return elos


def _team_calibration(name, year, before_utc):
    """Per-team auto-calibration: average (actual points − points expected from FIFA rank
    + home) over this edition's matches so far. Positive = over-performing its seeding;
    negative = under-performing. Recomputed from results (always current, pre-match only)."""
    cn = _canon(name)
    tr_ = min(max(rank_for(name, year) or 60, 1), 130)
    resid, n = 0.0, 0
    for m in get_matches_espn(str(year), ttl=300).get("matches", []):
        if m.get("status") != "FINISHED" or (m.get("utcDate") or "") >= (before_utc or "~"):
            continue
        sc = m.get("score") or {}
        sh, sa = sc.get("home"), sc.get("away")
        if sh is None or sa is None:
            continue
        h, a = m.get("home") or {}, m.get("away") or {}
        if _canon(h.get("name")) == cn:
            home, opp, my, og = True, a.get("name"), sh, sa
        elif _canon(a.get("name")) == cn:
            home, opp, my, og = False, h.get("name"), sa, sh
        else:
            continue
        or_ = min(max(rank_for(opp, year) or 60, 1), 130)
        d = (or_ - tr_) * 3 + (25 if home else -25)        # higher → this team favored
        we = 1 / (1 + 10 ** (-d / 300))
        ep = 3 * we * 0.76 + 0.24                          # expected points (draw rate ~0.24)
        ap = 3 if my > og else 1 if my == og else 0
        resid += ap - ep
        n += 1
    return round(resid / n, 2) if n else 0.0


def _days_between(a, b):
    try:
        fa = time.mktime(time.strptime(a[:10], "%Y-%m-%d"))
        fb = time.mktime(time.strptime(b[:10], "%Y-%m-%d"))
        return int(round((fa - fb) / 86400))
    except Exception:
        return None


def _haversine_km(c1, c2):
    if not c1 or not c2:
        return None
    lat1, lon1, lat2, lon2 = c1[0], c1[1], c2[0], c2[1]
    dlat, dlon = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2)
    return int(round(2 * 6371 * math.asin(math.sqrt(a))))


def _rest_travel(nm, year, utc, this_co):
    """(rest_days, travel_km) for a team going into this match: days since their previous
    match and distance from that venue to this one. (None if it's their first match.)"""
    cn = _canon(nm)
    cand = [m for m in get_matches_espn(str(year), ttl=300).get("matches", [])
            if m.get("status") == "FINISHED" and (m.get("utcDate") or "") < utc
            and cn in (_canon((m.get("home") or {}).get("name")), _canon((m.get("away") or {}).get("name")))]
    if not cand:
        return (None, None)
    cand.sort(key=lambda m: m.get("utcDate") or "")
    last = cand[-1]
    rest = _days_between(utc, last.get("utcDate") or utc)
    pc = (last.get("venueCity") or "").split(",")[0].strip()
    travel = _haversine_km(geocode(pc) if pc else None, this_co)
    return (rest, travel)


def predict_match(mid):
    detail = (get_match_espn(str(mid)) or {}).get("match") or {}
    h = (detail.get("home") or {}).get("name")
    a = (detail.get("away") or {}).get("name")
    if not h or not a:
        return {"available": False}
    year = CONFIG.get("season")
    utc = detail.get("utcDate") or "~"                    # only use info from before kickoff
    fh, fa = _team_form_before(h, year, utc), _team_form_before(a, year, utc)
    # per-player ratings (from past-match stats) weight how much each absence hurts
    rat_h, rat_a = _player_ratings(h, year), _player_ratings(a, year)
    injh, inja = _injury_impact(h, year, rat_h), _injury_impact(a, year, rat_a)
    elos = _team_elos(year, utc)                          # results-based team strength (pre-match)
    # suspensions for THIS match: 2-yellow accumulation + red cards, served then cleared (FIFA rules)
    suh = round(sum(rat_h.get(nn, 1.0) for nn in _card_suspensions(h, year, utc)), 2)
    sua = round(sum(rat_a.get(nn, 1.0) for nn in _card_suspensions(a, year, utc)), 2)
    # lineup-aware: once the official XI is published, a side that rests/benches stars is weakened
    # (deficit = best available XI rating − actual starters' rating; folded into the injury term)
    try:
        lu = get_lineup(str(mid))
        if lu.get("available"):
            def _xi_deficit(team_name, ratings):
                sd = next((s for s in (lu.get("home"), lu.get("away"))
                           if s and _canon(s.get("team")) == _canon(team_name)), None)
                starters = [p for p in (sd or {}).get("players", []) if p.get("starter")] if sd else []
                if len(starters) < 7:
                    return 0.0
                n = min(11, len(starters))
                act = sum(ratings.get(_norm(p.get("name") or ""), 1.0) for p in starters[:n])
                susp = _card_suspensions(team_name, year, utc)
                avail = sorted((ratings.get(_norm(p.get("name") or ""), 1.0)
                                for p in (espn_roster(team_name, season=year) or [])
                                if p.get("available") is not False
                                and _norm(p.get("name") or "") not in susp), reverse=True)
                best = sum(avail[:n]) if avail else act
                return round(max(0.0, best - act), 2)
            injh += _xi_deficit(h, rat_h)
            inja += _xi_deficit(a, rat_a)
    except Exception as e:
        print(f"[warn] lineup-adjust {mid}: {e}")
    # minor factors: rest days, travel distance (since previous match) + match weather
    this_city = ((detail.get("venue") or {}).get("city") or "").split(",")[0].strip()
    this_co = geocode(this_city) if this_city else None
    rh, th = _rest_travel(h, year, utc, this_co)
    ra, ta = _rest_travel(a, year, utc, this_co)
    rest_travel = {h: (rh, th), a: (ra, ta)}
    # raw pre-match stats only — probabilities/scoreline computed client-side so the
    # user can re-weight the factors live (see computePrediction in worldcup.html)
    def stat(nm, f, inj, susp):
        sc = _team_scorers(nm, year, 1)
        rest, travel = rest_travel.get(nm, (None, None))
        elo_v = round(elos.get(_norm(nm), _init_elo(rank_for(nm, year))) + _recent_form_offset(nm, year, utc))   # rank-seed + recent form + results
        ad = _team_attack_defense(nm, year, utc)   # recent attack / defense / goalkeeper
        return {"name": nm, "rank": rank_for(nm, year) or 60, "elo": elo_v,
                "played": f["played"], "points": f["points"], "gd": f["gd"],
                "gf": f["gf"], "ga": f["ga"], "inj": inj, "susp": susp,
                "host": 1 if _canon(nm) in _HOSTS_2026 else 0,
                "top": sc[0] if sc else None, "rest": rest, "travel": travel,
                "cal": _team_calibration(nm, year, utc),
                "atk": ad.get("atk"), "mid": ad.get("mid"), "dfn": ad.get("dfn"), "gk": ad.get("gk")}
    return {"available": True, "status": detail.get("status"), "weather": detail.get("weather") or {},
            "home": stat(h, fh, injh, suh), "away": stat(a, fa, inja, sua)}


# ---- real LLM picks: ask an external model (Gemini / OpenAI) for a prediction ----
def _llm_prompt(mid):
    """Build a pre-kickoff scouting brief from the same factors the app model uses."""
    pr = predict_match(mid)
    if not pr.get("available"):
        return None, None, None

    def desc(t):
        top = (t.get("top") or {}).get("name")
        return (f"{t['name']} — FIFA rank {t.get('rank')}, strength(Elo) {t.get('elo')}, "
                f"recent attack {t.get('atk')} goals/game, midfield control {t.get('mid')}, "
                f"defense {t.get('dfn')} conceded/game, clean-sheet rate {t.get('gk')}, "
                f"injuries {t.get('inj')}, suspended {t.get('susp')}, "
                f"{'host nation, ' if t.get('host') else ''}top scorer {top or 'n/a'}")
    h, a = pr["home"], pr["away"]
    wx = pr.get("weather") or {}
    prompt = (
        "You are an expert football analyst predicting a 2026 FIFA World Cup match BEFORE kickoff. "
        "Weigh squad strength, recent form, injuries, suspensions, home advantage and big-game pedigree.\n"
        f"HOME — {desc(h)}\nAWAY — {desc(a)}\n"
        f"Conditions: temp {wx.get('temp')}C, humidity {wx.get('humidity')}%, wind {wx.get('wind')}km/h.\n"
        'Respond with ONLY a JSON object, no prose: '
        '{"winner":"home|draw|away","scoreHome":<int>,"scoreAway":<int>,'
        '"confidence":<int 0-100>,"reason":"one short sentence"}')
    return prompt, h["name"], a["name"]


def _parse_llm_pick(txt):
    j = None
    try:
        j = json.loads(txt)
    except Exception:
        m = re.search(r"\{.*\}", txt or "", re.S)
        if m:
            try:
                j = json.loads(m.group(0))
            except Exception:
                j = None
    if not isinstance(j, dict):
        return None
    w = str(j.get("winner") or "").lower()
    if w not in ("home", "away", "draw"):
        w = "draw"

    def _i(x, lo, hi, d):
        try:
            return max(lo, min(hi, int(round(float(x)))))
        except (TypeError, ValueError):
            return d
    return {"winner": w, "scoreHome": _i(j.get("scoreHome"), 0, 9, 0),
            "scoreAway": _i(j.get("scoreAway"), 0, 9, 0),
            "confidence": _i(j.get("confidence"), 0, 100, 50),
            "reason": (str(j.get("reason") or "")).strip()[:200]}


def _http_post_json(url, body, headers, timeout=25):
    # browser-like UA so Cloudflare-fronted APIs (e.g. Groq) don't bot-block the default urllib UA
    base = {"Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) WorldCupPilot/1.0"}
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                 headers={**base, **headers}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _groq_pick(prompt):
    # Groq (console.groq.com) — free, fast, OpenAI-compatible; runs Llama 3.3.
    key = (CONFIG.get("groq_api_key") or CONFIG.get("grop_api_key")
           or os.environ.get("GROQ_API_KEY") or "").strip()
    if not key:
        return {"available": False, "reason": "no_key"}
    body = {"model": "llama-3.3-70b-versatile",
            "messages": [{"role": "system", "content": "You are an expert football analyst. Respond only with JSON."},
                         {"role": "user", "content": prompt}],
            "response_format": {"type": "json_object"}, "temperature": 0.4}
    data = _http_post_json("https://api.groq.com/openai/v1/chat/completions", body,
                           {"Authorization": f"Bearer {key}"})
    txt = (((data.get("choices") or [{}])[0].get("message") or {}).get("content"))
    pick = _parse_llm_pick(txt)
    return {"available": True, **pick} if pick else {"available": False, "reason": "parse"}


def _openai_pick(prompt):
    key = (CONFIG.get("openai_api_key") or os.environ.get("OPENAI_API_KEY") or "").strip()
    if not key:
        return {"available": False, "reason": "no_key"}
    # A GitHub token (ghp_… / github_pat_…) means GitHub Models — free GPT-4o, OpenAI-compatible.
    # An OpenAI key (sk-…) hits OpenAI directly (paid).
    if key.startswith("ghp_") or key.startswith("github_pat_"):
        url, model = "https://models.github.ai/inference/chat/completions", "openai/gpt-4o-mini"
    else:
        url, model = "https://api.openai.com/v1/chat/completions", "gpt-4o-mini"
    body = {"model": model,
            "messages": [{"role": "system", "content": "You are an expert football analyst. Respond only with JSON."},
                         {"role": "user", "content": prompt}],
            "response_format": {"type": "json_object"}, "temperature": 0.4}
    data = _http_post_json(url, body, {"Authorization": f"Bearer {key}"})
    txt = (((data.get("choices") or [{}])[0].get("message") or {}).get("content"))
    pick = _parse_llm_pick(txt)
    return {"available": True, **pick} if pick else {"available": False, "reason": "parse"}


_ai_revalidating = set()


def _ai_fetch(provider, prompt, hn, an):
    try:
        res = _groq_pick(prompt) if provider == "groq" else _openai_pick(prompt)
    except Exception as e:
        code = getattr(e, "code", None)
        print(f"[warn] ai_pick {provider}: {e}")
        res = {"available": False, "reason": (f"http_{code}" if code else "error")}
    res["provider"] = provider
    res["home"], res["away"] = hn, an
    return res


def _ai_revalidate_bg(mid, provider, ckey):
    """Stale cache is served immediately; this refreshes it in the background and replaces it
    only on a successful re-fetch (a failed/rate-limited call leaves the old pick intact)."""
    if ckey in _ai_revalidating:
        return
    _ai_revalidating.add(ckey)

    def run():
        try:
            prompt, hn, an = _llm_prompt(mid)
            if prompt:
                res = _ai_fetch(provider, prompt, hn, an)
                if res.get("available"):
                    _write_cache(ckey, res)
        finally:
            _ai_revalidating.discard(ckey)
    threading.Thread(target=run, daemon=True).start()


def ai_pick(mid, provider):
    """Cached real-LLM pick. provider = 'groq' | 'openai'.
    • groq   — live: show the cached pick now, refresh stale ones in the background (replace only on success).
    • openai — ChatGPT picks are entered MANUALLY (no working API), so this is CACHE-ONLY: it never calls
      the API and never writes, so a hand-cached pick is never overwritten or blanked."""
    provider = "openai" if provider == "openai" else "groq"
    ckey = f"aipick-{provider}-{mid}"
    if provider == "openai":
        cached, _ = _read_cache(ckey, 10 ** 9)
        if cached is not None and cached.get("available"):
            return cached
        return {"available": False, "reason": "manual", "provider": "openai"}
    det = (get_match_espn(str(mid)) or {}).get("match") or {}
    ttl = 10 ** 9 if det.get("status") == "FINISHED" else 21600
    cached, fresh = _read_cache(ckey, ttl)
    if cached is not None and cached.get("available"):
        if not fresh:
            _ai_revalidate_bg(mid, provider, ckey)    # 1) show cache now  2) refresh in background
        return cached
    # no usable cache yet → fetch synchronously this once
    prompt, hn, an = _llm_prompt(mid)
    if not prompt:
        return {"available": False, "reason": "no_match", "provider": provider}
    res = _ai_fetch(provider, prompt, hn, an)
    if res.get("available"):
        _write_cache(ckey, res)                       # 3) success → cache for next time
    return res


# ---- prediction accuracy scoreboard (My Pick vs DraftKings vs Gemini vs ChatGPT) ----
_ACC_W = {"elo": 6, "inj": 4, "susp": 5, "home": 3}
_ACC_K = {"homeBase": 10, "scale": 250, "db": 0.20, "ds": 450, "avg": 1.30,
          "drawClose": 0.08, "drawCloseScale": 160, "tiltScale": 220, "tiltCap": 0.95,
          "formK": 1.5, "host": 60}


def _model_pick_py(pr):
    """Server-side replica of computePrediction (default weights) → {winner, scoreHome, scoreAway}."""
    if not pr.get("available"):
        return None
    h, a = pr["home"], pr["away"]
    W, K = _ACC_W, {**_ACC_K, **model_params()}   # apply Refresh-tuned params

    def trate(s):
        return (W["elo"] * (((s.get("elo") or 1500) - 1500) / 8) - W["inj"] * (s.get("inj") or 0) * 6
                - W["susp"] * (s.get("susp") or 0) * 12 + (s.get("cal") or 0) * 15)
    dr = trate(h) - trate(a) + W["home"] * K["homeBase"] + (K["host"] if h.get("host") else 0)
    if h.get("mid") is not None and a.get("mid") is not None:
        dr += 10 * max(-3, min(3, h["mid"] - a["mid"]))
    if h.get("rest") is not None and a.get("rest") is not None:
        dr += 4 * max(-4, min(4, h["rest"] - a["rest"]))
    if h.get("travel") is not None and a.get("travel") is not None:
        dr -= 6 * max(-3, min(3, (h["travel"] - a["travel"]) / 1000))
    we = 1 / (1 + 10 ** (-dr / K["scale"]))
    pd = min(.55, K["db"] * math.exp(-(dr / K["ds"]) ** 2) + K["drawClose"] * math.exp(-(dr / K["drawCloseScale"]) ** 2))
    home, draw, away = (1 - pd) * we, pd, (1 - pd) * (1 - we)
    Kf = K["formK"]
    eg = lambda to, pri, n: (to + pri * Kf) / (n + Kf)
    atk = lambda t: eg(t.get("gf", 0) or 0, t["atk"] if t.get("atk") is not None else K["avg"], t.get("played", 0) or 0)
    dfn = lambda t: eg(t.get("ga", 0) or 0, t["dfn"] if t.get("dfn") is not None else K["avg"], t.get("played", 0) or 0)
    gk = lambda t: max(.82, min(1.18, 1 - (t["gk"] - .35) * .5)) if t.get("gk") is not None else 1
    tilt = max(-K["tiltCap"], min(K["tiltCap"], dr / K["tiltScale"]))
    wx = pr.get("weather") or {}                 # heat/humidity/wind dampen goals (match computePrediction)
    damp = 1.0
    if wx.get("temp") is not None and wx["temp"] >= 30: damp -= 0.08
    if wx.get("humidity") is not None and wx["humidity"] >= 75: damp -= 0.06
    if wx.get("wind") is not None and wx["wind"] >= 30: damp -= 0.06
    damp = max(0.8, damp)
    lh = max(.2, ((atk(h) + dfn(a)) / 2) * (1 + tilt) * gk(a) * damp)
    la = max(.2, ((atk(a) + dfn(h)) / 2) * (1 - tilt) * gk(h) * damp)
    oc = "home" if home >= draw and home >= away else "away" if away >= draw else "draw"
    # The shown scoreline must agree with the W/D/L pick: an unconstrained Poisson mode can be a
    # draw (1-1) even when a side is favored, which looked like a wrong pick on the Accuracy tab.
    # Pick the most likely scoreline consistent with the predicted outcome.
    cons = (lambda i, j: i > j) if oc == "home" else (lambda i, j: j > i) if oc == "away" else (lambda i, j: i == j)
    best = -1; sH, sA = (1, 0) if oc == "home" else (0, 1) if oc == "away" else (1, 1)
    for i in range(7):
        for j in range(7):
            if not cons(i, j):
                continue
            p2 = (math.exp(-lh) * lh ** i / math.factorial(i)) * (math.exp(-la) * la ** j / math.factorial(j))
            if p2 > best + 1e-9 or (p2 > best - 1e-9 and i + j > sH + sA):
                best = max(best, p2); sH, sA = i, j
    return {"winner": oc, "scoreHome": sH, "scoreAway": sA}


def _model_pick_cached(mid):
    """Model pick for a FINISHED match never changes → cache permanently (predict_match is the
    slow part of the accuracy tab; this makes repeat loads instant)."""
    ckey = f"modelpick-{mid}"
    cached, _ = _read_cache(ckey, 10 ** 9)
    if cached is not None:
        return cached or None
    try:
        mp = _model_pick_py(predict_match(mid))
    except Exception:
        mp = None
    _write_cache(ckey, mp or {})
    return mp


def _ai_pick_cached(mid, provider):
    provider = "openai" if provider == "openai" else "groq"
    cached, _ = _read_cache(f"aipick-{provider}-{mid}", 10 ** 9)   # finished-match picks are permanent
    return cached


_PRED_KEYS = ("model", "dk", "groq", "openai")


def _blank_tally():
    return {k: {"hit": 0, "exact": 0, "n": 0} for k in _PRED_KEYS}


def _is_placeholder_team(name):
    """Knockout slots read 'Group A 2nd Place' / 'Round of 32 1 Winner' until the bracket is
    decided — those aren't real teams, so don't predict on them."""
    return bool(re.search(r"winner|place|group|runner|tbd|/", name or "", re.I))


def compute_accuracy():
    """Tally every finished current-edition match by ROUND: outcome-hit & exact-score per
    predictor. My Pick + DraftKings compute instantly; Gemini/ChatGPT use ALREADY-CACHED picks
    only (no API calls). Whole result cached 5 min so the tab is snappy."""
    cached, fresh = _read_cache("accuracy", 300)
    if cached is not None and fresh:
        return cached
    allm = list(get_matches().get("matches", []))
    allm.sort(key=lambda m: m.get("utcDate") or "")
    team_grp = {}      # team name → group letter (so each round can be sub-grouped by group)
    try:
        for gr in (get_standings_espn(str(CONFIG.get("season"))) or {}).get("groups", []):
            letter = re.sub(r"(?i)^group\s*", "", gr.get("group") or "").strip()
            for r in gr.get("table", []):
                nm = (r.get("team") or {}).get("name")
                if nm:
                    team_grp[nm] = letter
    except Exception:
        pass
    overall = _blank_tally()
    groups, gindex, appear = [], {}, {}
    nfin = 0
    warm_pending = 0

    def grp(rkey, rnd, stage):
        if rkey not in gindex:
            gindex[rkey] = len(groups)
            groups.append({"round": rnd, "stage": stage, "predictors": _blank_tally(), "matches": []})
        return groups[gindex[rkey]]

    def tally(g, key, ok, exact):
        for t in (overall[key], g["predictors"][key]):
            t["n"] += 1
            if ok:
                t["hit"] += 1
            if exact:
                t["exact"] += 1

    def res_of(sh, sa):   # outcome implied by a scoreline
        return "home" if sh > sa else "away" if sa > sh else "draw"

    def pick_outcome(p):  # a predictor's W/D/L call: its STATED winner, else inferred from its score.
        # The model/AI give an explicit winner; a Poisson-mode scoreline alone can read as a draw
        # even when a side is favored, so the stated winner is the real pick to grade.
        w = p.get("winner")
        return w if w in ("home", "away", "draw") else res_of(p.get("scoreHome"), p.get("scoreAway"))

    for m in allm:
        sc = m.get("score") or {}
        finished = m.get("status") == "FINISHED" and sc.get("home") is not None
        mid = str(m["id"])
        hn, an = m["home"]["name"], m["away"]["name"]
        stage = m.get("stage") or ""
        if stage == "GROUP_STAGE":
            rnd = max(appear.get(hn, 0), appear.get(an, 0)) + 1
            appear[hn] = appear.get(hn, 0) + 1
            appear[an] = appear.get(an, 0) + 1
            rkey = ("g", rnd)
        else:
            rnd = None
            rkey = ("k", stage)
            # skip not-yet-decided knockout slots (placeholder names until the bracket is set)
            if not finished and (not (hn and an) or _is_placeholder_team(hn) or _is_placeholder_team(an)):
                continue
        g = grp(rkey, rnd, stage)
        if finished:
            nfin += 1
            ah, aa = sc["home"], sc["away"]
            pens = sc.get("pens") or {}
            ph, pa = pens.get("home"), pens.get("away")
            if ah == aa and ph is not None and pa is not None:
                ao = "home" if ph > pa else "away"     # knockout level after ET → shootout winner advanced
                actual = f"{ah}-{aa} (PK {ph}-{pa})"
            else:
                ao = "home" if ah > aa else "away" if aa > ah else "draw"
                actual = f"{ah}-{aa}"
            row = {"home": hn, "away": an, "actual": actual, "ao": ao}
            mp = _model_pick_cached(mid)
            if mp:
                ok = pick_outcome(mp) == ao
                ex = mp["scoreHome"] == ah and mp["scoreAway"] == aa
                tally(g, "model", ok, ex)
                row["model"] = f'{mp["scoreHome"]}-{mp["scoreAway"]}'; row["model_ok"] = ok; row["model_ex"] = ex
            det = (get_match_espn(mid) or {}).get("match") or {}
            o = det.get("odds") or {}
            if o.get("home") and o.get("away"):
                cand = {k: v for k, v in {"home": o.get("home"), "draw": o.get("draw"), "away": o.get("away")}.items() if v}
                dp = min(cand, key=cand.get)
                tally(g, "dk", dp == ao, False)
                row["dk"] = dp; row["dk_ok"] = dp == ao
            for pv in ("groq", "openai"):
                r = _ai_pick_cached(mid, pv)
                if r and r.get("available"):
                    ok = pick_outcome(r) == ao
                    ex = r.get("scoreHome") == ah and r.get("scoreAway") == aa
                    tally(g, pv, ok, ex)
                    row[pv] = f'{r.get("scoreHome")}-{r.get("scoreAway")}'; row[pv + "_ok"] = ok; row[pv + "_ex"] = ex
        else:
            # upcoming match → show the predictions only (no actual result yet, not scored)
            row = {"home": hn, "away": an, "pending": True}
            mp = _model_pick_cached(mid)
            if mp:
                row["model"] = f'{mp["scoreHome"]}-{mp["scoreAway"]}'
            det = (get_match_espn(mid) or {}).get("match") or {}
            o = det.get("odds") or {}
            if o.get("home") and o.get("away"):
                cand = {k: v for k, v in {"home": o.get("home"), "draw": o.get("draw"), "away": o.get("away")}.items() if v}
                row["dk"] = min(cand, key=cand.get)
            for pv in ("groq", "openai"):
                r = _ai_pick_cached(mid, pv)
                if r and r.get("available"):
                    row[pv] = f'{r.get("scoreHome")}-{r.get("scoreAway")}'
                elif pv == "groq":
                    warm_pending += 1   # groq pick not cached yet → background-warm it
        if stage == "GROUP_STAGE":
            row["grp"] = team_grp.get(hn) or team_grp.get(an) or ""
        g["matches"].append(row)
    rem = sum(max(0, nfin - overall[pv]["n"]) for pv in ("groq", "openai"))
    out = {"predictors": overall, "rounds": groups, "total": nfin,
           "aiRemaining": rem, "warmPending": warm_pending}
    _write_cache("accuracy", out)
    return out


_grading = set()


def grade_ai_bg():
    """Background-fill AI picks for all finished matches (paced to dodge free-tier 429)."""
    if _grading:
        return False
    _grading.add(1)

    def run():
        try:
            def _warmable(m):
                if m.get("status") == "FINISHED" and (m.get("score") or {}).get("home") is not None:
                    return True
                # upcoming match with REAL teams (group stage always; knockout once the bracket is set)
                hn, an = m["home"].get("name"), m["away"].get("name")
                return (m.get("status") == "SCHEDULED" and hn and an
                        and not _is_placeholder_team(hn) and not _is_placeholder_team(an))
            mats = [m for m in get_matches().get("matches", []) if _warmable(m)]
            blocked = set()        # a provider that 429s is rate-limited org-wide → stop hammering it
            for m in mats:
                for pv in ("openai", "groq"):
                    if pv in blocked or _ai_pick_cached(str(m["id"]), pv):
                        continue
                    r = ai_pick(str(m["id"]), pv)
                    if r.get("available"):
                        time.sleep(1.2)              # steady pacing → stay under per-minute limits
                    elif r.get("reason") == "http_429":
                        blocked.add(pv)              # skip this provider for the rest of the run; retry next cycle
                    # other errors: just skip this match for this provider
        finally:
            try:
                os.remove(_cache_file("accuracy"))   # force the scoreboard to recompute with new picks
            except OSError:
                pass
            _grading.discard(1)
    threading.Thread(target=run, daemon=True).start()
    return True


def _accuracy_warm_worker():
    """Pre-compute the scoreboard in the background so the Accuracy tab opens instantly
    (warms the per-match model-pick cache + the 5-min accuracy cache)."""
    time.sleep(12)
    while True:
        _req_memo_clear()                           # long-lived thread: fresh memo each cycle
        try:
            acc = compute_accuracy()
            if acc.get("aiRemaining", 0) > 0 or acc.get("warmPending", 0) > 0:
                grade_ai_bg()   # newly-finished matches → grade; upcoming → pre-warm groq picks
        except Exception as e:
            print(f"[warn] accuracy warm: {e}")
        try:
            advancement_trend()   # warm the day-by-day advancement trend (heavy; cached by signature)
        except Exception as e:
            print(f"[warn] advtrend warm: {e}")
        time.sleep(240)


threading.Thread(target=_accuracy_warm_worker, daemon=True).start()


# ---- self-tuning: re-fit the Elo x Score shape params on real results (triggered by Refresh) ----
_MODEL_DEFAULTS = {"avg": 1.30, "tiltScale": 220, "tiltCap": 0.95, "formK": 1.5}
_TUNE_EDITIONS = [2026, 2022, 2018, 2014, 2010, 2006, 2002, 1998, 1994]
_tuning = set()


def _model_params_path():
    return os.path.join(os.path.dirname(CONFIG_PATH) or ROOT, "model_params.json")


def model_params():
    try:
        with open(_model_params_path(), encoding="utf-8") as f:
            return {**_MODEL_DEFAULTS, **json.load(f)}
    except Exception:
        return dict(_MODEL_DEFAULTS)


def _tune_state_path():
    return os.path.join(os.path.dirname(CONFIG_PATH) or ROOT, "tune_state.json")


def _finished_signature():
    """Fingerprint of THIS edition's finished results (past WCs never change) — used to skip a
    re-tune when nothing new has finished since the last one."""
    try:
        fin = sorted((str(m["id"]), (m.get("score") or {}).get("home"), (m.get("score") or {}).get("away"))
                     for m in get_matches().get("matches", [])
                     if m.get("status") == "FINISHED" and (m.get("score") or {}).get("home") is not None)
        return hashlib.md5(json.dumps(fin).encode("utf-8")).hexdigest()
    except Exception:
        return ""


def _group_stage_signature():
    """Fingerprint of just the GROUP-STAGE finished results. Group-stage-only views (3rd-place
    odds, the advancement race) depend on nothing else, so keying their cache on this keeps them
    stable through the knockouts instead of recomputing every time a knockout match finishes."""
    try:
        fin = sorted((str(m["id"]), (m.get("score") or {}).get("home"), (m.get("score") or {}).get("away"))
                     for m in get_matches().get("matches", [])
                     if m.get("stage") == "GROUP_STAGE" and m.get("status") == "FINISHED"
                     and (m.get("score") or {}).get("home") is not None)
        return hashlib.md5(json.dumps(fin).encode("utf-8")).hexdigest()
    except Exception:
        return ""


def _read_tune_sig():
    try:
        with open(_tune_state_path(), encoding="utf-8") as f:
            return json.load(f).get("sig")
    except Exception:
        return None


def _write_tune_sig(sig):
    try:
        with open(_tune_state_path(), "w", encoding="utf-8") as f:
            json.dump({"sig": sig}, f)
    except OSError:
        pass


def _tune_samples():
    """Replay every finished match (this edition + past WCs) → per-match pre-kickoff state
    (Elo gap + running form). Elo/form don't depend on the tuned shape params, so we build
    these once and score many param sets against them cheaply."""
    samples = []
    for yr in _TUNE_EDITIONS:
        try:
            data = get_matches() if str(yr) == str(CONFIG.get("season")) else get_matches_espn(str(yr))
        except Exception:
            continue
        fin = [m for m in data.get("matches", [])
               if m.get("status") == "FINISHED" and (m.get("score") or {}).get("home") is not None]
        fin.sort(key=lambda m: m.get("utcDate") or "")
        elo, form = {}, {}
        for m in fin:
            hn, an = m["home"]["name"], m["away"]["name"]
            eh = elo.get(hn, _init_elo(m["home"].get("rank")))
            ea = elo.get(an, _init_elo(m["away"].get("rank")))
            fh = form.get(hn, {"p": 0, "gf": 0, "ga": 0})
            fa = form.get(an, {"p": 0, "gf": 0, "ga": 0})
            sh, sa = m["score"]["home"], m["score"]["away"]
            host = 1 if (yr == 2026 and _canon(hn) in _HOSTS_2026) else 0
            samples.append((eh, ea, dict(fh), dict(fa), host, sh, sa))
            exp = 1 / (1 + 10 ** ((ea - eh) / 400))
            s = 1.0 if sh > sa else 0.5 if sh == sa else 0.0
            kk = 32 * (math.log(abs(sh - sa) + 1) + 1)
            elo[hn] = eh + kk * (s - exp); elo[an] = ea + kk * ((1 - s) - (1 - exp))
            form[hn] = {"p": fh["p"] + 1, "gf": fh["gf"] + sh, "ga": fh["ga"] + sa}
            form[an] = {"p": fa["p"] + 1, "gf": fa["gf"] + sa, "ga": fa["ga"] + sh}
    return samples


def _scoreline(eh, ea, host, fh, fa, P):
    """Pre-kickoff expected goals (lh, la) and the modal scoreline (pH, pA) for the
    Elo + home + form subset of the runtime model (computePrediction in worldcup.html).
    The tuner scores params with THIS, then they are served by the JS runtime, so the two
    must stay in lockstep — test_model_consistency.py asserts they agree."""
    A, TS, TC, K = P["avg"], P["tiltScale"], P["tiltCap"], P["formK"]
    dr = 0.75 * (eh - ea) + 30 + (60 if host else 0)   # mirrors teamRating (elo w=6, home w=3) + host
    tilt = max(-TC, min(TC, dr / TS))
    atkh = (fh["gf"] + A * K) / (fh["p"] + K); dfnh = (fh["ga"] + A * K) / (fh["p"] + K)
    atka = (fa["gf"] + A * K) / (fa["p"] + K); dfna = (fa["ga"] + A * K) / (fa["p"] + K)
    lh = max(.2, ((atkh + dfna) / 2) * (1 + tilt)); la = max(.2, ((atka + dfnh) / 2) * (1 - tilt))
    best = -1; pH = pA = 1
    for i in range(7):
        pi = math.exp(-lh) * lh ** i / math.factorial(i)
        for j in range(7):
            pr = pi * (math.exp(-la) * la ** j / math.factorial(j))
            if pr > best + 1e-9 or (pr > best - 1e-9 and i + j > pH + pA):
                best = pr if pr > best else best; pH, pA = i, j
    return lh, la, pH, pA


def _score_params(samples, P):
    oh = ex = 0; mae = 0.0; n = len(samples) or 1
    for eh, ea, fh, fa, host, sh, sa in samples:
        _, _, pH, pA = _scoreline(eh, ea, host, fh, fa, P)
        po = "home" if pH > pA else "away" if pA > pH else "draw"
        ao = "home" if sh > sa else "away" if sa > sh else "draw"
        oh += po == ao; ex += pH == sh and pA == sa; mae += abs(pH - sh) + abs(pA - sa)
    return oh / n, ex / n, mae / (2 * n)


def tune_model():
    """Grid-search the shape params to best fit real results; persist for future predictions."""
    if _tuning:
        return False
    _tuning.add(1)

    def run():
        try:
            sig = _finished_signature()
            if sig and sig == _read_tune_sig():       # (A) no new results since last tune → skip the heavy work
                print("[info] Elo x Score tune skipped — no new results since last tune")
                return
            samples = _tune_samples()
            if len(samples) < 30:
                return
            best = None
            for A in (1.25, 1.35, 1.45):
                for TS in (200, 240, 280):
                    for TC in (0.7, 0.85, 1.0):
                        for K in (1.0, 1.5, 2.0):
                            P = {"avg": A, "tiltScale": TS, "tiltCap": TC, "formK": K}
                            oh, exr, mae = _score_params(samples, P)
                            obj = oh + 1.5 * exr - 0.15 * mae   # reward winner+exact, penalise score distance
                            if best is None or obj > best[0]:
                                best = (obj, P)
            changed = best[1] != model_params()
            with open(_model_params_path(), "w", encoding="utf-8") as f:
                json.dump(best[1], f)
            _write_tune_sig(sig)                     # remember these results so a repeat Refresh is a no-op
            if changed:                              # params moved → model picks must recompute
                try:
                    for fn in os.listdir(CACHE_DIR):
                        if fn.startswith("modelpick-") or fn == "accuracy.json":
                            os.remove(os.path.join(CACHE_DIR, fn))
                except OSError:
                    pass
                try:
                    compute_accuracy()               # (B) rebuild picks here (background) so the tab opens instantly
                except Exception as e:
                    print(f"[warn] post-tune rewarm: {e}")
            print(f"[info] Elo x Score tuned on {len(samples)} matches → {best[1]} (obj {best[0]:.3f})")
        finally:
            _tuning.discard(1)
    threading.Thread(target=run, daemon=True).start()
    return True


def _team_player_cards(team_name, year):
    """{normalized player: {'y':yellows,'r':reds}} accumulated across this edition's
    finished matches — yellow accumulation can force a suspension next match."""
    cn = _canon(team_name)
    tally = {}
    for m in get_matches_espn(str(year), ttl=300).get("matches", []):
        if m.get("status") != "FINISHED":
            continue
        side = ("home" if _canon((m.get("home") or {}).get("name")) == cn
                else "away" if _canon((m.get("away") or {}).get("name")) == cn else None)
        if not side:
            continue
        det = (get_match_espn(str(m.get("id"))) or {}).get("match") or {}
        for e in det.get("events", []):
            if e.get("side") != side:
                continue
            tp = (e.get("type") or "").lower()
            who = _norm(e.get("player") or "")
            if not who or "card" not in tp:          # must be a card event (avoids 'sco-RED', etc.)
                continue
            t = tally.setdefault(who, {"y": 0, "r": 0})
            if "red" in tp:                          # "red card" or "yellow-red card" → sending-off
                t["r"] += 1
            elif "yellow" in tp:
                t["y"] += 1
    return tally


def _team_card_totals(year):
    """{canon team name: {'y':yellows,'r':reds}} for the whole edition — one pass over finished
    matches' events (so the group table can show each team's discipline)."""
    key = f"cardtotals-{year}"
    cached, fresh = _read_cache(key, 300)
    if cached is not None and fresh:
        return cached
    tot = {}
    for m in get_matches_espn(str(year), ttl=300).get("matches", []):
        if m.get("status") != "FINISHED":
            continue
        det = (get_match_espn(str(m.get("id"))) or {}).get("match") or {}
        hc = _canon((m.get("home") or {}).get("name"))
        ac = _canon((m.get("away") or {}).get("name"))
        for e in det.get("events", []):
            tp = (e.get("type") or "").lower()
            if "card" not in tp:
                continue
            cn = hc if e.get("side") == "home" else ac if e.get("side") == "away" else None
            if not cn:
                continue
            t = tot.setdefault(cn, {"y": 0, "r": 0})
            if "red" in tp:
                t["r"] += 1
            elif "yellow" in tp:
                t["y"] += 1
    _write_cache(key, tot)
    return tot


_cardtotals_warming = set()


def _team_card_totals_nonblocking(year):
    """Card totals without blocking the request: serve any cached value (even stale),
    otherwise warm it in the background and return {} for now (fills on next refresh).
    Prevents a cold Groups view from stalling on ~dozens of match-summary fetches."""
    key = f"cardtotals-{year}"
    cached, _ = _read_cache(key, 10 ** 9)          # accept any age
    if cached is not None:
        return cached
    if year not in _cardtotals_warming:
        _cardtotals_warming.add(year)

        def run():
            try:
                _team_card_totals(year)
            except Exception as e:
                print(f"[warn] cardtotals warm {year}: {e}")
            finally:
                _cardtotals_warming.discard(year)
        threading.Thread(target=run, daemon=True).start()
    return {}


def _team_goal_tally(name, year):
    """{player displayName: goals} for a team this edition (excludes own goals)."""
    cn = _canon(name)
    tally = {}
    for m in get_matches_espn(str(year), ttl=300).get("matches", []):
        if m.get("status") != "FINISHED":
            continue
        side = ("home" if _canon((m.get("home") or {}).get("name")) == cn
                else "away" if _canon((m.get("away") or {}).get("name")) == cn else None)
        if not side:
            continue
        det = (get_match_espn(str(m.get("id"))) or {}).get("match") or {}
        for e in det.get("events", []):
            if e.get("side") != side:
                continue
            tp = (e.get("type") or "").lower()
            if ("goal" in tp or "penalty" in tp) and not any(x in tp for x in ("missed", "saved", "own")):
                p = e.get("player")
                if p:
                    tally[p] = tally.get(p, 0) + 1
    return tally


def _team_scorers(name, year, top=3):
    """Top scorers of a team this edition (star players)."""
    arr = sorted(({"name": k, "goals": v} for k, v in _team_goal_tally(name, year).items()), key=lambda x: -x["goals"])
    return arr[:top]


_WC_EDITIONS = [2026, 2022, 2018, 2014, 2010, 2006, 2002, 1998, 1994]
_career_cache = {}


def _team_goal_tally_career(name, block=True):
    """{player(normalized): goals} summed across ALL World Cup editions — a player's all-time
    WC goal count. EXPENSIVE first time (9 editions × every match detail), so it's disk-cached
    (30 days) and computed in the background: block=False returns None until it's ready, so the
    team-detail response never waits 60s+ on it."""
    cn = _canon(name)
    if cn in _career_cache:
        return _career_cache[cn]
    cached, _fresh = _read_cache(f"career-{cn}", 30 * 86400)
    if cached is not None:
        _career_cache[cn] = cached
        return cached
    if not block:
        return None                      # not ready → caller skips it and warms in background
    career = {}
    for yr in _WC_EDITIONS:
        try:
            for k, v in _team_goal_tally(name, yr).items():
                nk = _norm(k)
                career[nk] = career.get(nk, 0) + v
        except Exception:
            continue
    _career_cache[cn] = career
    _write_cache(f"career-{cn}", career)
    return career


def get_lineup(espn_id):
    """Actual lineup (starting XI + subs) from ESPN, with each player's accumulated
    cards this tournament (selection context)."""
    data = http_json(f"{ESPN_BASE}/summary?event={espn_id}", f"espn-sum-{espn_id}", ttl=120)
    rosters = (data or {}).get("rosters") or []
    if not rosters:
        return {"available": False}
    year = CONFIG.get("season")

    def side(r):
        tname = (r.get("team") or {}).get("displayName") or ""
        cards = _team_player_cards(tname, year)
        rost = espn_roster(tname, season=year) or []
        rost_photo = {_norm(p.get("name") or ""): p.get("photo") for p in rost if p.get("photo")}
        players, missing = [], []
        for e in r.get("roster", []):
            ath = e.get("athlete", {}) or {}
            nm = ath.get("displayName")
            nn = _norm(nm or "")
            c = cards.get(nn, {})
            # photo priority: ESPN headshot → cached TheSportsDB → squad-roster headshot
            photo = (ath.get("headshot") or {}).get("href") or tsdb_player_cached(nm) or rost_photo.get(nn)
            if not photo and nm:
                missing.append(nm)
            players.append({"name": nm, "num": e.get("jersey"),
                            "pos": (e.get("position") or {}).get("abbreviation"),
                            "fp": e.get("formationPlace"),
                            "starter": bool(e.get("starter")), "photo": photo,
                            "subOut": bool(e.get("subbedOut")), "subIn": bool(e.get("subbedIn")),
                            "y": c.get("y", 0), "r": c.get("r", 0)})
        warm_photos_bg(missing)                                              # resolve+download missing photos
        warm_images_bg([p["photo"] for p in players if p.get("photo")])      # pre-cache shown photos
        # currently unavailable players (injury / suspension) from the squad
        unavailable = []
        try:
            for p in (espn_roster(tname, season=year) or []):
                if p.get("available") is False:
                    unavailable.append({"name": p.get("name"), "status": p.get("statusText")})
        except Exception:
            pass
        return {"team": tname, "formation": r.get("formation"),
                "players": players, "out": unavailable}

    home = next((r for r in rosters if r.get("homeAway") == "home"), rosters[0])
    away = next((r for r in rosters if r.get("homeAway") == "away"),
                rosters[1] if len(rosters) > 1 else rosters[0])
    h, a = side(home), side(away)
    # only treat as the REAL lineup once the official XI (starters) is published; until then → predicted
    has_xi = any(p.get("starter") for p in h["players"]) or any(p.get("starter") for p in a["players"])
    if not has_xi:
        return {"available": False}
    warm_images_bg([pl.get("photo") for sd in (h, a) for pl in sd.get("players", []) if pl.get("photo")])
    return {"available": True, "home": h, "away": a}


# ---- predicted lineup for an upcoming (not-yet-played) match -----------------
def _bk_abbr(ab):
    """position abbreviation (G/RB/CM/ST…) → bucket 0=GK 1=DEF 2=MID 3=FWD."""
    ab = (ab or "").upper()
    if ab in ("G", "GK"):
        return 0
    if ab[:1] == "F" or ab in ("ST", "CF", "W", "WG", "RW", "LW", "SS") or ab.endswith("W"):
        return 3
    if "M" in ab:
        return 2
    return 1


def _bk_kr(posk):
    """roster position label (ESPN_POS values) → bucket 0=GK 1=DEF 2=MID 3=FWD."""
    return {"Goalkeeper": 0, "Defence": 1, "Midfield": 2, "Offence": 3,
            "골키퍼": 0, "수비수": 1, "미드필더": 2, "공격수": 3}.get(posk, 2)


def _prev_match_for(name, year, before_utc):
    """The team's most recent FINISHED match before `before_utc` (for the base XI)."""
    cn = _canon(name)
    best = None
    for m in get_matches_espn(str(year), ttl=300).get("matches", []):
        if m.get("status") != "FINISHED":
            continue
        if cn not in (_canon((m.get("home") or {}).get("name")), _canon((m.get("away") or {}).get("name"))):
            continue
        d = m.get("utcDate") or ""
        if d and (before_utc == "~" or d < before_utc) and (best is None or d > best[0]):
            best = (d, m)
    return best[1] if best else None


def _formation_for_gap(gap):
    """Pick a shape based on the strength gap vs the opponent (gap = my Elo − opp Elo).
    Behind a much stronger side → defensive; against a weaker side → attacking.
    Returns [GK, DEF, MID, FWD] summing to 11."""
    if gap <= -110:
        return [1, 5, 4, 1]      # park the bus
    if gap <= -45:
        return [1, 5, 3, 2]
    if gap >= 110:
        return [1, 3, 4, 3]      # all-out attack
    if gap >= 45:
        return [1, 4, 3, 3]
    return [1, 4, 4, 2]          # balanced


def _predict_side_xi(name, year, before_utc, gap=0):
    """Predicted starting XI: last match's XI minus injured/suspended, reshaped for the opponent
    (formation adapts to the strength gap), gaps filled from the squad by position."""
    roster = espn_roster(name, season=year) or []
    susp_set = _card_suspensions(name, year, before_utc)   # FIFA card rules (accumulation + reset)
    ratings = _player_ratings(name, year)                  # past-match player ratings → pick better fill-ins
    rost_by_norm = {_norm(p.get("name") or ""): p for p in roster}
    # who is out: injuries (roster availability) + suspensions
    out, inj_norm, susp_norm = [], set(), set()
    for p in roster:
        if p.get("available") is False:
            nn = _norm(p.get("name") or "")
            inj_norm.add(nn)
            out.append({"name": p.get("name"), "status": p.get("statusText") or "부상", "reason": "inj"})
    for nn in susp_set:
        if nn in inj_norm:
            continue
        susp_norm.add(nn)
        pp = rost_by_norm.get(nn)
        out.append({"name": (pp or {}).get("name") or nn.title(),
                    "status": "출장정지", "reason": "susp"})
    unavailable = inj_norm | susp_norm

    target = _formation_for_gap(gap)                       # shape adapts to the opponent
    xi, used, counts = [], set(), [0, 0, 0, 0]
    prev = _prev_match_for(name, year, before_utc)
    base = []
    if prev:
        lu = get_lineup(str(prev.get("id")))
        if lu.get("available"):
            for sd in (lu.get("home"), lu.get("away")):
                if sd and _canon(sd.get("team")) == _canon(name):
                    base = [pl for pl in sd.get("players", []) if pl.get("starter")]
                    break
    for pl in base:                                        # keep available regulars, within the shape
        nn = _norm(pl.get("name") or "")
        if nn in unavailable:
            continue
        b = _bk_abbr(pl.get("pos"))
        if counts[b] >= target[b]:
            continue                                       # this line is full for the chosen shape
        xi.append({"name": pl.get("name"), "num": pl.get("num"), "pos": pl.get("pos"),
                   "photo": pl.get("photo"), "starter": True})
        used.add(nn); counts[b] += 1

    # fill remaining slots per bucket from available squad players
    pool = {0: [], 1: [], 2: [], 3: []}
    for p in roster:
        nn = _norm(p.get("name") or "")
        if nn in unavailable or nn in used:
            continue
        pool[_bk_kr(p.get("position"))].append(p)
    for b in pool:   # highest-rated available player first, then by shirt number
        pool[b].sort(key=lambda p: (-ratings.get(_norm(p.get("name") or ""), 1.0),
                                    int(p["number"]) if str(p.get("number") or "").isdigit() else 99))
    counts = [0, 0, 0, 0]
    for pl in xi:
        counts[_bk_abbr(pl.get("pos"))] += 1
    for b in range(4):
        while counts[b] < target[b] and pool[b]:
            p = pool[b].pop(0)
            xi.append({"name": p.get("name"), "num": p.get("number"),
                       "pos": ["G", "D", "M", "F"][b], "photo": p.get("photo"), "starter": True})
            used.add(_norm(p.get("name") or "")); counts[b] += 1
    # top up to 11 from any leftover available players
    leftover = [p for b in range(4) for p in pool[b]]
    while len(xi) < 11 and leftover:
        p = leftover.pop(0)
        nn = _norm(p.get("name") or "")
        if nn in used:
            continue
        xi.append({"name": p.get("name"), "num": p.get("number"),
                   "pos": ["G", "D", "M", "F"][_bk_kr(p.get("position"))],
                   "photo": p.get("photo"), "starter": True})
        used.add(nn)
    # a short predicted bench from the next available players
    bench = []
    for p in leftover:
        nn = _norm(p.get("name") or "")
        if nn in used:
            continue
        bench.append({"name": p.get("name"), "num": p.get("number"),
                      "pos": ["G", "D", "M", "F"][_bk_kr(p.get("position"))],
                      "photo": p.get("photo"), "starter": False})
        used.add(nn)
        if len(bench) >= 7:
            break
    form = f"{target[1]}-{target[2]}-{target[3]}"
    players = xi + bench
    cards = _team_player_cards(name, year)                 # cumulative card counts for display
    for pl in players:                                     # attach card counts (shown on pitch/bench)
        c = cards.get(_norm(pl.get("name") or ""), {})
        pl["y"], pl["r"] = c.get("y", 0), c.get("r", 0)
    warm_photos_bg([pl["name"] for pl in players if not pl.get("photo") and pl.get("name")])
    warm_images_bg([pl["photo"] for pl in players if pl.get("photo")])
    return {"team": name, "formation": form, "players": players, "out": out, "predicted": True}


def predict_lineup(mid):
    detail = (get_match_espn(str(mid)) or {}).get("match") or {}
    h = (detail.get("home") or {}).get("name")
    a = (detail.get("away") or {}).get("name")
    if not h or not a:
        return {"available": False}
    year = CONFIG.get("season")
    utc = detail.get("utcDate") or "~"
    elos = _team_elos(year, utc)

    def strength(nm):
        return elos.get(_norm(nm), _init_elo(rank_for(nm, year))) + _recent_form_offset(nm, year, utc)
    gh, ga = strength(h), strength(a)
    return {"available": True, "predicted": True,
            "home": _predict_side_xi(h, year, utc, gh - ga),
            "away": _predict_side_xi(a, year, utc, ga - gh)}


# ---- local image cache (download remote photos once; serve from disk) -------
def _img_ext(url):
    low = url.lower()
    for e in (".jpg", ".jpeg", ".webp", ".gif", ".svg"):
        if e in low:
            return ".jpg" if e == ".jpeg" else e
    return ".png"


def fetch_image(url):
    """Local path of the cached image; download once if missing. A failed download
    is retried at most once a day (so missing 'latest' photos sync daily, not every view)."""
    if not url or not url.startswith("http"):
        return None
    ext = _img_ext(url)
    h = hashlib.md5(url.encode("utf-8")).hexdigest()
    path = os.path.join(CACHE_DIR, "img", h + ext)
    fail = path + ".fail"
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return path
    try:
        if os.path.exists(fail) and (time.time() - os.path.getmtime(fail)) < 86400:
            return None
    except OSError:
        pass
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "WorldCupPilot/1.0"})
        with urllib.request.urlopen(req, timeout=12) as r:
            data = r.read()
        if not data:
            raise ValueError("empty")
        with open(path, "wb") as f:
            f.write(data)
        if os.path.exists(fail):
            os.remove(fail)
        return path
    except Exception as e:
        try:
            open(fail, "w").close()
        except OSError:
            pass
        print(f"[warn] img {url[:60]}: {e}")
        return None


# ---- HTTP -------------------------------------------------------------------
_ASSET_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".gif": "image/gif", ".svg": "image/svg+xml", ".webp": "image/webp",
                ".ico": "image/x-icon"}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path, ctype):
        try:
            with open(path, "rb") as f:
                body = f.read()
        except FileNotFoundError:
            self._json(404, {"error": "not found"})
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        _req_memo_clear()   # fresh per-request memo (this thread may serve keep-alive requests)
        path = urlparse(self.path).path
        if path in ("/", "/index.html", "/worldcup.html"):
            return self._file(HTML, "text/html; charset=utf-8")
        if path.startswith("/assets/"):
            name = os.path.basename(path)
            full = os.path.join(ASSETS_DIR, name)
            ext = os.path.splitext(name)[1].lower()
            return self._file(full, _ASSET_TYPES.get(ext, "application/octet-stream"))
        if path == "/img":
            u = (parse_qs(urlparse(self.path).query).get("u") or [""])[0]
            local = fetch_image(u) if u else None
            if not local:
                if u:                                  # fall back to the original remote URL
                    self.send_response(302)
                    self.send_header("Location", u)
                    self.end_headers()
                    return
                return self._json(404, {"error": "missing u"})
            try:
                with open(local, "rb") as f:
                    body = f.read()
            except OSError:
                return self._json(404, {"error": "not found"})
            self.send_response(200)
            self.send_header("Content-Type", _ASSET_TYPES.get(os.path.splitext(local)[1].lower(), "image/png"))
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "public, max-age=31536000, immutable")
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/status":
            data = get_matches()
            return self._json(200, {
                "token_set": token_ok(),
                "mock": data["source"] == "mock",
                "competition": CONFIG.get("competition"),
                "season": CONFIG.get("season"),
                "venue_timezone": CONFIG.get("venue_timezone", "America/New_York"),
                "dates": data["dates"],
            })
        if path == "/api/matches":
            q = parse_qs(urlparse(self.path).query)
            year = (q.get("year") or [""])[0]
            if year and year != str(CONFIG.get("season")):
                return self._json(200, get_matches_espn(year))
            return self._json(200, get_matches())
        if path == "/api/standings":
            q = parse_qs(urlparse(self.path).query)
            year = (q.get("year") or [""])[0]
            if year and year != str(CONFIG.get("season")):
                return self._json(200, get_standings_espn(year))
            return self._json(200, get_standings())
        if path == "/api/team":
            q = parse_qs(urlparse(self.path).query)
            tid = (q.get("id") or [""])[0]
            name = (q.get("name") or [""])[0]
            year = (q.get("year") or [""])[0]
            if name:
                return self._json(200, get_team_by_name(name, year or None))
            if not tid:
                return self._json(400, {"error": "missing id"})
            return self._json(200, get_team(tid))
        if path == "/api/wiki-image":
            q = parse_qs(urlparse(self.path).query)
            title = (q.get("title") or [""])[0]
            return self._json(200, {"image": wiki_image(title) if title else None})
        if path == "/api/playerclub":
            q = parse_qs(urlparse(self.path).query)
            name = (q.get("name") or [""])[0]
            try:
                return self._json(200, tsdb_player_clubinfo(name))
            except Exception as e:
                print(f"[warn] playerclub: {e}")
                return self._json(200, {"club": None, "clubCountry": None})
        if path == "/api/playername":
            q = parse_qs(urlparse(self.path).query)
            name = (q.get("name") or [""])[0]
            try:
                return self._json(200, player_names(name))
            except Exception as e:
                print(f"[warn] playername: {e}")
                return self._json(200, {"en": name})
        if path == "/api/advodds":
            try:
                return self._json(200, third_place_odds())
            except Exception as e:
                print(f"[warn] advodds: {e}")
                return self._json(200, {})
        if path == "/api/advtrend":
            try:
                return self._json(200, advancement_trend())
            except Exception as e:
                print(f"[warn] advtrend: {e}")
                return self._json(200, {"teams": [], "boards": {}, "order": [], "labels": []})
        if path == "/api/match":
            q = parse_qs(urlparse(self.path).query)
            mid = (q.get("id") or [""])[0]
            if not mid:
                return self._json(400, {"error": "missing id"})
            return self._json(200, get_match(mid))
        if path == "/api/highlight":
            q = parse_qs(urlparse(self.path).query)
            query = (q.get("q") or [""])[0]
            if not query:
                return self._json(400, {"error": "missing q"})
            return self._json(200, {"videoId": youtube_first_video(query)})
        if path == "/api/predict":
            q = parse_qs(urlparse(self.path).query)
            mid = (q.get("id") or [""])[0]
            if not mid:
                return self._json(400, {"error": "missing id"})
            try:
                return self._json(200, predict_match(mid))
            except Exception as e:
                print(f"[warn] predict {mid}: {e}")
                return self._json(200, {"available": False})
        if path == "/api/model-params":
            return self._json(200, {**model_params(), "tuning": len(_tuning) > 0})
        if path == "/api/accuracy":
            try:
                res = dict(compute_accuracy())
                res["grading"] = len(_grading) > 0      # live (not cached)
                return self._json(200, res)
            except Exception as e:
                print(f"[warn] accuracy: {e}")
                return self._json(200, {"predictors": {}, "rounds": [], "total": 0})
        if path == "/api/aipick":
            q = parse_qs(urlparse(self.path).query)
            mid = (q.get("id") or [""])[0]
            provider = (q.get("p") or ["groq"])[0]
            if not mid:
                return self._json(400, {"error": "missing id"})
            try:
                return self._json(200, ai_pick(mid, provider))
            except Exception as e:
                print(f"[warn] aipick {provider} {mid}: {e}")
                return self._json(200, {"available": False, "reason": "error", "provider": provider})
        if path == "/api/lineup":
            q = parse_qs(urlparse(self.path).query)
            mid = (q.get("id") or [""])[0]
            if not mid:
                return self._json(400, {"error": "missing id"})
            try:
                res = get_lineup(mid)
                if not res.get("available"):           # not played yet → predicted XI
                    res = predict_lineup(mid)
                return self._json(200, res)
            except Exception as e:
                print(f"[warn] lineup {mid}: {e}")
                return self._json(200, {"available": False})
        return self._json(404, {"error": "not found"})

    def do_POST(self):
        _req_memo_clear()   # fresh per-request memo
        path = urlparse(self.path).path
        if path == "/api/grade-ai":
            started = grade_ai_bg()
            return self._json(200, {"ok": True, "started": started})
        if path == "/api/refresh":
            # Don't delete caches (that blanks the UI if a fetch fails). Instead force-revalidate
            # live data: http_json overwrites on success and KEEPS the old data on failure.
            season = str(CONFIG.get("season"))
            for fn in (lambda: get_matches_espn(season, ttl=0),
                       lambda: get_standings_espn(season, ttl=0)):
                try:
                    fn()
                except Exception as e:
                    print(f"[warn] refresh revalidate: {e}")
            try:
                os.remove(_cache_file("accuracy"))   # recompute the scoreboard from preserved picks
            except OSError:
                pass
            tune_model()                             # re-tune Elo x Score on the latest real results
            return self._json(200, {"ok": True, "tuning": True})
        if path == "/api/save-edition":
            q = parse_qs(urlparse(self.path).query)
            year = (q.get("year") or [""])[0]
            return self._json(200, save_edition(year) if year else {"error": "missing year"})
        if path == "/api/build-venues":
            try:
                return self._json(200, {"ok": True, "stats": build_venues()})
            except Exception as e:
                return self._json(500, {"ok": False, "error": str(e)})
        return self._json(404, {"error": "not found"})


def seed_cache():
    """Copy committed results from dist/cache into the writable runtime cache if missing:
    AI picks (can't be re-fetched), model picks, and the finished-match computations
    (accuracy, 3rd-place odds, the advancement race). Lets a fresh cache load them instantly
    instead of recomputing — the Accuracy/Groups tabs would otherwise come up empty or stall."""
    src = os.path.join(ROOT, "dist", "cache")
    if not os.path.isdir(src) or os.path.abspath(src) == os.path.abspath(CACHE_DIR):
        return
    os.makedirs(CACHE_DIR, exist_ok=True)
    import shutil
    keep = lambda fn: fn.endswith(".json") and (
        fn.startswith(("aipick-", "modelpick-")) or fn in ("advtrend.json", "advodds.json", "accuracy.json"))
    n = 0
    for fn in os.listdir(src):
        if keep(fn) and not os.path.exists(os.path.join(CACHE_DIR, fn)):
            try:
                shutil.copy2(os.path.join(src, fn), os.path.join(CACHE_DIR, fn)); n += 1
            except OSError:
                pass
    if n:
        print(f"[info] seeded {n} cached results from dist/cache")


def main():
    os.makedirs(CACHE_DIR, exist_ok=True)
    seed_cache()
    host, port = "127.0.0.1", 8770
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"World Cup Pilot server on http://{host}:{port}  (token_set={token_ok()})")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
