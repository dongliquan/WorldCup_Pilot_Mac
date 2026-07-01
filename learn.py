"""Learn-from-history: replay every past World Cup with the live prediction model, compare
the predicted SCORELINE (the new attack/defence + Poisson part) to the actual scoreline, and
grid-search the scoreline parameters (base goals, strength->goals tilt, tilt cap) that best fit
9 editions of real results. The winning params get baked into PRED_K in worldcup.html.

Outcome weights (elo/home/draw shape) were already tuned by backtest.py — kept fixed here so
this run focuses on the scoreline. Run with the app serving on :8770.

Metric = mean Poisson negative log-likelihood of the actual goals under (lh, la); we also report
exact-score hit-rate and outcome hit-rate so the gain is interpretable.
"""
import json
import math
import urllib.request

BASE = "http://127.0.0.1:8770"
EDITIONS = [2026, 2022, 2018, 2014, 2010, 2006, 2002, 1998, 1994]

# fixed, already-tuned outcome weights (must mirror PRED_DEFAULT / PRED_K in worldcup.html)
W = dict(elo=6, home=3)
SCALE, HOMEBASE = 250, 10


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=120) as r:
        return json.loads(r.read().decode("utf-8"))


def init_elo(rank):
    return 1500 + (50 - min(max(rank or 60, 1), 130)) * 7


def pois(k, l):
    return math.exp(-l) * l ** k / math.factorial(k)


def edition_samples(year):
    """Replay one edition in date order, emitting per-match pre-kickoff state: Elo gap and the
    two sides' running attack/defence form (goals for/against per game BEFORE this match)."""
    data = get(f"/api/matches?year={year}")
    fin = [m for m in data["matches"]
           if m.get("status") == "FINISHED"
           and (m.get("score") or {}).get("home") is not None
           and (m.get("score") or {}).get("away") is not None]
    fin.sort(key=lambda m: m.get("utcDate") or "")
    elo, form, out = {}, {}, []
    for m in fin:
        hn, an = m["home"]["name"], m["away"]["name"]
        eh = elo.get(hn, init_elo(m["home"].get("rank")))
        ea = elo.get(an, init_elo(m["away"].get("rank")))
        fh = form.get(hn, {"p": 0, "gf": 0, "ga": 0})
        fa = form.get(an, {"p": 0, "gf": 0, "ga": 0})
        sh, sa = m["score"]["home"], m["score"]["away"]
        out.append({"eh": eh, "ea": ea, "fh": dict(fh), "fa": dict(fa),
                    "sh": sh, "sa": sa,
                    "host": 1 if year == 2026 and m["home"].get("name") in
                    ("United States", "Mexico", "Canada") else 0})
        # update Elo (margin-weighted) and running form
        ex = 1 / (1 + 10 ** ((ea - eh) / 400))
        s = 1.0 if sh > sa else 0.5 if sh == sa else 0.0
        k = 32 * (math.log(abs(sh - sa) + 1) + 1)
        elo[hn] = eh + k * (s - ex)
        elo[an] = ea + k * ((1 - s) - (1 - ex))
        form[hn] = {"p": fh["p"] + 1, "gf": fh["gf"] + sh, "ga": fh["ga"] + sa}
        form[an] = {"p": fa["p"] + 1, "gf": fa["gf"] + sa, "ga": fa["ga"] + sh}
    return out


def lambdas(s, P):
    """Replicate computePrediction's scoreline lambdas with tunable params P."""
    dr = W["elo"] * ((s["eh"] - 1500) / 8 - (s["ea"] - 1500) / 8) + W["home"] * HOMEBASE + (60 if s["host"] else 0)
    tilt = max(-P["cap"], min(P["cap"], dr / P["tscale"]))
    fh, fa = s["fh"], s["fa"]
    atkh = fh["gf"] / fh["p"] if fh["p"] > 0 else P["avg"]
    dfnh = fh["ga"] / fh["p"] if fh["p"] > 0 else P["avg"]
    atka = fa["gf"] / fa["p"] if fa["p"] > 0 else P["avg"]
    dfna = fa["ga"] / fa["p"] if fa["p"] > 0 else P["avg"]
    lh = max(0.2, ((atkh + dfna) / 2) * (1 + tilt))
    la = max(0.2, ((atka + dfnh) / 2) * (1 - tilt))
    return lh, la


def mode_score(lh, la):
    best, sH, sA = -1, 1, 1
    for i in range(8):
        for j in range(8):
            pr = pois(i, lh) * pois(j, la)
            if pr > best + 1e-9 or (pr > best - 1e-9 and (i + j) > (sH + sA)):
                best = max(best, pr); sH, sA = i, j
    return sH, sA


def evaluate(samples, P):
    import collections
    nll = exact = ohit = 0
    dist = collections.Counter()
    for s in samples:
        lh, la = lambdas(s, P)
        nll += -math.log(max(1e-9, pois(s["sh"], lh))) - math.log(max(1e-9, pois(s["sa"], la)))
        sH, sA = mode_score(lh, la)
        dist[f"{sH}-{sA}"] += 1
        if sH == s["sh"] and sA == s["sa"]:
            exact += 1
        po = 1 if sH > sA else -1 if sA > sH else 0
        ao = 1 if s["sh"] > s["sa"] else -1 if s["sa"] > s["sh"] else 0
        if po == ao:
            ohit += 1
    n = len(samples) or 1
    return {"nll": nll / n, "exact": exact / n, "ohit": ohit / n, "dist": dist, "variety": len(dist)}


def score_objective(r):
    """What we MAXIMISE: get the actual scoreline right (exact) and at least the winner right
    (outcome), with a small bonus for producing a realistic SPREAD of scorelines (not all 1-0).
    NLL is only a mild tie-breaker — pure NLL collapses everything to low scores."""
    return r["exact"] * 2.0 + r["ohit"] * 1.0 + min(r["variety"], 12) * 0.01 - r["nll"] * 0.02


def main():
    samples = []
    for y in EDITIONS:
        try:
            s = edition_samples(y)
            samples += s
            print(f"  {y}: {len(s)} matches")
        except Exception as e:
            print(f"  {y}: skip ({e})")
    print(f"total: {len(samples)} matches\n")

    base = {"avg": 1.35, "tscale": 250, "cap": 0.85}
    b = evaluate(samples, base)
    print(f"BEFORE  avg=1.35 tscale=250 cap=0.85 -> exact={b['exact']:.3f} outcome={b['ohit']:.3f} "
          f"NLL={b['nll']:.3f} variety={b['variety']}\n")

    best = (-9e9, None)
    for avg in (1.15, 1.25, 1.35, 1.45, 1.55, 1.65):
        for tscale in (180, 220, 250, 300, 350):
            for cap in (0.6, 0.75, 0.85, 0.95, 1.05):
                P = {"avg": avg, "tscale": tscale, "cap": cap}
                obj = score_objective(evaluate(samples, P))
                if obj > best[0]:
                    best = (obj, P)
    P = best[1]
    a = evaluate(samples, P)
    print(f"LEARNED {P} -> exact={a['exact']:.3f} outcome={a['ohit']:.3f} "
          f"NLL={a['nll']:.3f} variety={a['variety']}")
    top = ", ".join(f"{k}:{v}" for k, v in a["dist"].most_common(8))
    print(f"predicted-score spread (top 8): {top}")
    print(f"\napply to worldcup.html: PRED_K.avg={P['avg']}, tilt cap={P['cap']}, tilt scale={P['tscale']}")
    json.dump(P, open("learned_params.json", "w"))


if __name__ == "__main__":
    main()
