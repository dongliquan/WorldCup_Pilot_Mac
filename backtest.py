"""Tune the global prediction weights on MANY past World Cups (large sample).
Elo is SEEDED FROM FIFA RANK (rank folded into Strength), so Strength is an intuitive
absolute strength. Then results update it. Grid-searches elo/home + shape params (incl.
the close-match draw boost) to minimise cross-entropy. Run with the app on :8770."""
import json
import math
import urllib.request

BASE = "http://127.0.0.1:8770"
EDITIONS = [2026, 2022, 2018, 2014, 2010, 2006, 2002, 1998, 1994]


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=90) as r:
        return json.loads(r.read().decode("utf-8"))


def init_elo(rank):
    return 1500 + (50 - min(max(rank or 60, 1), 130)) * 7


def edition_samples(year):
    data = get(f"/api/matches?year={year}")
    fin = [m for m in data["matches"]
           if m.get("status") == "FINISHED"
           and (m.get("score") or {}).get("home") is not None
           and (m.get("score") or {}).get("away") is not None]
    fin.sort(key=lambda m: m.get("utcDate") or "")
    elo, out = {}, []
    for m in fin:
        hn, an = m["home"]["name"], m["away"]["name"]
        eh = elo.get(hn, init_elo(m["home"].get("rank")))
        ea = elo.get(an, init_elo(m["away"].get("rank")))
        sh, sa = m["score"]["home"], m["score"]["away"]
        out.append({"eloH": eh, "eloA": ea, "out": "H" if sh > sa else "A" if sa > sh else "D"})
        ex = 1 / (1 + 10 ** ((ea - eh) / 400))
        s = 1.0 if sh > sa else 0.5 if sh == sa else 0.0
        k = 32 * (math.log(abs(sh - sa) + 1) + 1)
        elo[hn] = eh + k * (s - ex)
        elo[an] = ea + k * ((1 - s) - (1 - ex))
    return out


def probs(s, w):
    dr = w["elo"] * ((s["eloH"] - 1500) / 8 - (s["eloA"] - 1500) / 8) + w["home"] * 10
    we = 1 / (1 + 10 ** (-dr / w["scale"]))
    pd = min(0.55, w["db"] * math.exp(-((dr / w["ds"]) ** 2)) + w["dc"] * math.exp(-((dr / 160) ** 2)))
    return {"H": (1 - pd) * we, "D": pd, "A": (1 - pd) * (1 - we)}


def evaluate(samples, w):
    ll = correct = 0
    for s in samples:
        pr = probs(s, w)
        tot = sum(pr.values())
        pr = {k: v / tot for k, v in pr.items()}
        ll += -math.log(max(1e-9, pr[s["out"]]))
        if max(pr, key=pr.get) == s["out"]:
            correct += 1
    n = len(samples) or 1
    return ll / n, correct / n


def main():
    samples = []
    for y in EDITIONS:
        try:
            s = edition_samples(y)
            samples += s
            print(f"  {y}: {len(s)}")
        except Exception as e:
            print(f"  {y}: skip ({e})")
    print(f"total: {len(samples)}")

    best = (9e9, None)
    for elo in (3, 4, 5, 6, 8):
        for home in (1, 3, 5, 7):
            for scale in (250, 350, 450):
                for db in (0.20, 0.24, 0.28):
                    for ds in (350, 450):
                        for dc in (0.08, 0.12, 0.16):
                            w = {"elo": elo, "home": home, "scale": scale, "db": db, "ds": ds, "dc": dc}
                            ll = evaluate(samples, w)[0]
                            if ll < best[0]:
                                best = (ll, w)
    bw = best[1]
    print("BEST logloss=%.4f acc=%.3f %s" % (*evaluate(samples, bw), bw))
    import collections
    c = collections.Counter(s["out"] for s in samples)
    n = len(samples) or 1
    print("REF  uniform=%.4f base-rate=%.4f %s" % (math.log(3), -sum(c[k] * math.log(c[k] / n) for k in c) / n, dict(c)))


if __name__ == "__main__":
    main()
