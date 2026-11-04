#!/usr/bin/env python3
"""Meeuw Weerbriefing — haalt alle cijferbronnen op en rekent ze uit.

Gebruik:
  python3 tools/bereken.py --briefing 2026-11-05:vooraf[,2026-11-04:ochtend] \
      [--vooruitzicht 2026-11-07,2026-11-08] --uit <map>

Schrijft <map>/samenvatting.json en <map>/samenvatting.txt, en downloadt de
BLIPMAP-meteogrammen en DWD-kaarten naar <map> (die bekijkt het model zelf).
Alle tijden lokaal (Europe/Brussels). Wind in kt, hoogtes in m boven de grond.
"""
import argparse, json, math, os, statistics as st, subprocess, sys, time
from datetime import datetime, date, timedelta, timezone
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Brussels")
LAT, LON = 51.2647, 4.7533
UA = "MeeuwWeerbriefing/1.0 github.com/jbve/meeuwweer"
BAAN = 50  # 05/23
MODELLEN = ["icon_d2", "ecmwf_ifs025", "meteofrance_arome_france_hd"]
MNAAM = {"best": "Open-Meteo best match", "icon_d2": "ICON-D2", "ecmwf_ifs025": "ECMWF IFS",
         "meteofrance_arome_france_hd": "AROME", "metno": "MET Norway"}
BLOKKEN = [("09–11", [9, 10]), ("11–13", [11, 12]), ("13–15", [13, 14]), ("15–", [15, 16])]
T0 = time.time()
LOG = []


def log(s):
    LOG.append(s)
    print(s, file=sys.stderr)


def curl(url, out=None, ua=None, pogingen=3):
    """curl met timeout 40 s; bij verbindingsfout één keer opnieuw. Geeft (ok, tekst|pad, fout)."""
    cmd = ["curl", "-sS", "-m", "40", "-w", "\n%{http_code}"]
    if ua:
        cmd += ["-A", ua]
    if out:
        cmd += ["-o", out]
    cmd.append(url)
    fout = ""
    for i in range(pogingen):
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode == 0:
            body, _, code = r.stdout.rpartition("\n")
            if code.startswith("2"):
                return True, (out if out else body), ""
            fout = "HTTP " + code
            if code == "429" or "limit" in body.lower():
                return False, body, fout  # daglimiet: niet opnieuw
        else:
            fout = (r.stderr.strip() or "curl fout %d" % r.returncode)[:200]
        time.sleep(4)
    return False, "", fout


def jhaal(url, naam, ua=None):
    ok, body, fout = curl(url, ua=ua)
    if not ok:
        status = "daglimiet" if ("limit" in (body or "").lower() or "429" in fout) else (fout or "fout")
        log(f"{naam}: {status}")
        return None, status
    try:
        j = json.loads(body)
    except Exception as e:
        log(f"{naam}: geen JSON ({e})")
        return None, "geen JSON"
    if isinstance(j, dict) and j.get("error"):
        status = "daglimiet" if "limit" in str(j.get("reason", "")).lower() else "fout: " + str(j.get("reason", ""))[:120]
        log(f"{naam}: {status}")
        return None, status
    return j, "ok"


def kt(kmh):
    return None if kmh is None else kmh / 1.852


def xw(spd, d):
    if spd is None or d is None:
        return None
    return abs(spd * math.sin(math.radians(d - BAAN)))


def baan_voor(d, spd):
    if d is None or spd is None:
        return "?"
    if spd < 4:
        return "variabel/licht"
    # grens 140° / 320°: wind tussen 320° en 140° (via noord/oost) → 05, anders 23
    return "05" if (d >= 320 or d < 140) else "23"


def vrichting(dirs, spds):
    """Vectorgemiddelde richting."""
    u = v = 0.0
    for d, s in zip(dirs, spds):
        if d is None or s is None:
            continue
        u += s * math.sin(math.radians(d))
        v += s * math.cos(math.radians(d))
    if u == 0 and v == 0:
        return None
    return (math.degrees(math.atan2(u, v)) + 360) % 360


def med(xs):
    xs = [x for x in xs if x is not None]
    return st.median(xs) if xs else None


def mx(rr, k):
    xs = [r.get(k) for r in rr if r.get(k) is not None]
    return rnd(max(xs)) if xs else None


def r50(x):
    return None if x is None else int(round(x / 50.0) * 50)


def rnd(x, n=0):
    return None if x is None else (round(x, n) if n else int(round(x)))


def windwoord(d):
    if d is None:
        return "?"
    w = ["noord", "noord-noordoost", "noordoost", "oost-noordoost", "oost", "oost-zuidoost", "zuidoost", "zuid-zuidoost",
         "zuid", "zuid-zuidwest", "zuidwest", "west-zuidwest", "west", "west-noordwest", "noordwest", "noord-noordwest"]
    return w[int((d + 11.25) // 22.5) % 16]


def kompas(d):
    if d is None:
        return "?"
    w = ["N", "NNO", "NO", "ONO", "O", "OZO", "ZO", "ZZO", "Z", "ZZW", "ZW", "WZW", "W", "WNW", "NW", "NNW"]
    return w[int((d + 11.25) // 22.5) % 16]


# ───────────────────────── reeksen per model ─────────────────────────

def reeks_openmeteo(h, suffix=""):
    """dict uur(datetime lokaal) → waarden in kt."""
    uit = {}
    if not h:
        return uit
    for i, t in enumerate(h["time"]):
        dt = datetime.fromisoformat(t).replace(tzinfo=TZ)

        def g(k):
            arr = h.get(k + suffix)
            return arr[i] if arr is not None and i < len(arr) else None
        uit[dt] = {
            "T": g("temperature_2m"), "Td": g("dew_point_2m"), "L": g("cloud_cover_low"),
            "M": g("cloud_cover_mid"), "H": g("cloud_cover_high"), "vis": g("visibility"),
            "ws": kt(g("wind_speed_10m")), "wd": g("wind_direction_10m"), "gu": kt(g("wind_gusts_10m")),
            "sw": g("shortwave_radiation"), "pr": g("precipitation"), "pp": g("precipitation_probability"),
            "cape": g("cape"), "blh": g("boundary_layer_height"), "w85": kt(g("wind_speed_850hPa")),
            "d85": g("wind_direction_850hPa"), "p": g("pressure_msl"), "fz": g("freezing_level_height"),
            "t925": g("temperature_925hPa"), "t850": g("temperature_850hPa"),
            "z925": g("geopotential_height_925hPa"), "z850": g("geopotential_height_850hPa"),
        }
    return uit


def reeks_metno(j):
    uit = {}
    if not j:
        return uit
    for x in j["properties"]["timeseries"]:
        dt = datetime.fromisoformat(x["time"].replace("Z", "+00:00")).astimezone(TZ)
        d = x["data"]["instant"]["details"]
        n1 = x["data"].get("next_1_hours", {}).get("details", {})
        n6 = x["data"].get("next_6_hours", {}).get("details", {})
        gu = d.get("wind_speed_of_gust")
        uit[dt.replace(minute=0, second=0)] = {
            "T": d.get("air_temperature"), "Td": d.get("dew_point_temperature"),
            "L": d.get("cloud_area_fraction_low"), "M": d.get("cloud_area_fraction_medium"),
            "H": d.get("cloud_area_fraction_high"), "fog": d.get("fog_area_fraction"),
            "ws": None if d.get("wind_speed") is None else d["wind_speed"] * 1.944,
            "gu": None if not gu else gu * 1.944, "wd": d.get("wind_from_direction"),
            "pr": n1.get("precipitation_amount", n6.get("precipitation_amount") if n6 else None),
            "p": d.get("air_pressure_at_sea_level"), "uurlijks": "next_1_hours" in x["data"],
        }
    return uit


def basis(r):
    if r.get("T") is None or r.get("Td") is None or r.get("L") is None:
        return None
    if r["L"] < 25:
        return None
    return max(0.0, (r["T"] - r["Td"]) * 125)


# ───────────────────────── briefingdag ─────────────────────────

def briefingdag(dag, typ, R, daily):
    """R: dict modelnaam → reeks."""
    d0 = datetime.combine(dag, datetime.min.time()).replace(tzinfo=TZ)
    uren = {h: d0 + timedelta(hours=h) for h in range(0, 24)}
    zon = daily.get(dag.isoformat(), {})
    zonder = zon.get("sunset", "17:00")

    # per uur tabel
    tabel = []
    for h in range(5, 19):
        rij = {"uur": h}
        for m, rs in R.items():
            r = rs.get(uren[h])
            if not r:
                continue
            b = basis(r)
            rij[m] = {k: (rnd(v, 1) if isinstance(v, float) else v) for k, v in
                      [("T", r.get("T")), ("Td", r.get("Td")), ("L", r.get("L")), ("M", r.get("M")), ("H", r.get("H")),
                       ("basis", r50(b)), ("ws", r.get("ws")), ("gu", r.get("gu")), ("wd", r.get("wd")),
                       ("pr", r.get("pr")), ("vis_km", None if r.get("vis") is None else round(r["vis"] / 1000, 1)),
                       ("sw", r.get("sw")), ("fog", r.get("fog"))] if v is not None}
        tabel.append(rij)

    def licht(hrs):
        rood, oranje, vis, gu, xs = [], [], [], [], []
        for m, rs in R.items():
            pr = 0.0
            for h in hrs:
                r = rs.get(uren[h])
                if not r:
                    continue
                if r.get("vis") is not None: vis.append(r["vis"])
                bb = basis(r)
                if bb is not None and r["L"] >= 40:
                    (rood if bb < 300 else oranje if bb < 500 else []).append(f"basis {r50(bb)} m ({MNAAM.get(m, m)})")
                if r.get("ws") is not None and xw(r["ws"], r.get("wd")) > 12: rood.append("gem. zijwind > 12 kt")
                if r.get("gu") is not None:
                    if r["gu"] > 25: rood.append("vlagen > 25 kt")
                    elif r["gu"] >= 20: oranje.append("vlagen ≥ 20 kt")
                    if xw(r["gu"], r.get("wd")) > 12: oranje.append("zijwind in vlagen > 12 kt")
                    if r.get("ws") is not None and r["gu"] - r["ws"] >= 10: oranje.append("vlagen ≥ 10 kt boven gemiddelde")
                pr += r.get("pr") or 0
                if (r.get("pp") or 0) >= 40: oranje.append("neerslagkans ≥ 40 %")
            if pr >= 1.0: rood.append(f"neerslag ≥ 1 mm ({MNAAM.get(m, m)})")
            elif pr > 0: oranje.append(f"lichte neerslag ({MNAAM.get(m, m)})")
        if vis and min(vis) < 1500: rood.append("zicht < 1,5 km")
        elif vis and min(vis) < 5000: oranje.append("zicht < 5 km")
        kleur = "rood" if rood else "oranje" if oranje else "groen"
        return kleur, sorted(set(rood or oranje))

    blokken = []
    for naam, hrs in BLOKKEN:
        if naam == "15–":
            naam = "15–" + zonder
        ws, gu, wd, xm, xg, bas, low, pr, pp, vis, sw = [], [], [], [], [], [], [], [], [], [], []
        per_model = {}
        for m, rs in R.items():
            mb = {"basis": [], "L": [], "pr": 0.0, "gu": [], "xg": []}
            for h in hrs:
                r = rs.get(uren[h])
                if not r:
                    continue
                if r.get("ws") is not None:
                    ws.append(r["ws"]); wd.append(r.get("wd"))
                    xm.append(xw(r["ws"], r.get("wd")))
                if r.get("gu") is not None:
                    gu.append(r["gu"]); xg.append(xw(r["gu"], r.get("wd"))); mb["gu"].append(r["gu"]); mb["xg"].append(xw(r["gu"], r.get("wd")))
                b = basis(r)
                if b is not None:
                    bas.append((b, r["L"], m)); mb["basis"].append(b)
                if r.get("L") is not None:
                    low.append(r["L"]); mb["L"].append(r["L"])
                if r.get("pr") is not None:
                    pr.append(r["pr"]); mb["pr"] += r["pr"]
                if r.get("pp") is not None:
                    pp.append(r["pp"])
                if r.get("vis") is not None:
                    vis.append(r["vis"])
                if r.get("sw") is not None:
                    sw.append(r["sw"])
            per_model[m] = {"laagste_basis": r50(min(mb["basis"])) if mb["basis"] else None,
                            "max_laag": max(mb["L"]) if mb["L"] else None, "neerslag_mm": round(mb["pr"], 1),
                            "max_vlaag": rnd(max(mb["gu"])) if mb["gu"] else None,
                            "max_xvlaag": rnd(max(mb["xg"])) if mb["xg"] else None}
        mws, mgu = med(ws), med(gu)
        richting = vrichting(wd, ws)
        mxm, mxg = med([x for x in xm if x is not None]), med([x for x in xg if x is not None])
        worst = min(bas, key=lambda x: x[0]) if bas else None
        minvis = min(vis) if vis else None
        voorstel, reden = licht(hrs)
        per_uur = {f"{h} u": licht([h])[0] for h in hrs}
        blokken.append({
            "blok": naam, "baan": baan_voor(richting, mws),
            "wind": (f"{int(round(richting / 10.0) * 10) % 360:03d}°/{rnd(mws)} ({rnd(mgu) if mgu is not None else '–'})" if richting is not None and mws is not None and mws >= 4
                     else f"variabel/licht ({rnd(mgu) if mgu is not None else '–'})"),
            "xwind": f"{baan_voor(richting, mws) if baan_voor(richting, mws) != 'variabel/licht' else '05'} · {rnd(mxm)} ({rnd(mxg) if mxg is not None else '–'})",
            "max_vlaag": rnd(max(gu)) if gu else None, "max_xvlaag": rnd(max(x for x in xg if x is not None)) if xg else None,
            "laagste_basis_slechtste_model": None if not worst else {"m": r50(worst[0]), "laag_pct": worst[1], "model": MNAAM.get(worst[2], worst[2])},
            "lage_bewolking_bereik": [min(low), max(low)] if low else None,
            "zicht_min_km": None if minvis is None else round(minvis / 1000, 1),
            "zon_wm2_med": rnd(med(sw)), "neerslagkans_max": max(pp) if pp else None,
            "per_model": {MNAAM.get(m, m): v for m, v in per_model.items()},
            "voorstel_verkeerslicht": voorstel, "voorstel_per_uur": per_uur, "reden": reden or ["niets beperkend in de modellen"],
        })

    # METEO uit best match (of MET Norway)
    bron = "best" if R.get("best") else "metno"
    rs = R.get(bron, {})
    dag_uren = [(h, rs.get(uren[h])) for h in range(9, 18) if rs.get(uren[h])]
    meteo = {"bron": MNAAM[bron]}
    if dag_uren:
        Tmax = max(dag_uren, key=lambda x: x[1]["T"] if x[1]["T"] is not None else -99)
        meteo["temp"] = f"max {rnd(daily.get(dag.isoformat(), {}).get('tmax', Tmax[1]['T']))} °C (rond {Tmax[0]} u)"
        tds = [r["Td"] for _, r in dag_uren if r.get("Td") is not None]
        spreid = [r["T"] - r["Td"] for _, r in dag_uren if r.get("T") is not None and r.get("Td") is not None]
        meteo["dauwpunt"] = f"{rnd(min(tds))}–{rnd(max(tds))} °C" + (", verschil met temperatuur blijft < 2 °C" if spreid and max(spreid) < 2 else "")
        r13 = rs.get(uren[13]) or {}
        if r13.get("t925") is not None and r13.get("t850") is not None and r13.get("z925") and r13.get("z850"):
            t1015 = r13["t925"] + (r13["t850"] - r13["t925"]) * (1015 - r13["z925"]) / (r13["z850"] - r13["z925"])
            krit = rnd(t1015 + 9.8 + 1)
            Tm = max(r["T"] for _, r in dag_uren)
            gehaald = next((h for h, r in dag_uren if r["T"] >= krit), None)
            meteo["kritisch"] = f"{krit} °C — " + (f"gehaald rond {gehaald} u" if gehaald else f"wordt niet gehaald (max {rnd(Tm)} °C), geen thermiek")
            if r13["t850"] > r13["t925"]:
                meteo["kritisch"] += f"; inversie tussen ongeveer {r50(r13['z925'])} en {r50(r13['z850'])} m"
            meteo["t925_t850_13u"] = [r13["t925"], r13["t850"]]
        else:
            meteo["kritisch"] = "onbekend (gegevens niet beschikbaar)"
        meteo["vriesniveau"] = f"{int(round(r13['fz'] / 100.0) * 100)} m" if r13.get("fz") else "onbekend (gegevens niet beschikbaar)"
        p9, p13, p17 = (rs.get(uren[9]) or {}).get("p"), r13.get("p"), (rs.get(uren[17]) or {}).get("p")
        if p13 is not None:
            tr = (p17 - p9) if (p9 is not None and p17 is not None) else 0
            meteo["qnh"] = f"{rnd(p13)} hPa, " + ("stabiel" if abs(tr) <= 2 else ("stijgend" if tr > 0 else "dalend") + f" ({'+' if tr > 0 else '−'}{rnd(abs(tr))} hPa tot 17 u)")
        w85 = [(h, r.get("w85"), r.get("d85")) for h, r in dag_uren if r.get("w85") is not None]
        if w85:
            meteo["wind_850"] = f"{kompas(w85[0][2])} {rnd(w85[0][1])} kt om {w85[0][0]} u → {kompas(w85[-1][2])} {rnd(w85[-1][1])} kt om {w85[-1][0]} u; max {rnd(max(x[1] for x in w85))} kt"
        blh = [r.get("blh") for _, r in dag_uren if r.get("blh") is not None]
        if blh: meteo["grenslaag_max_m"] = r50(max(blh))
        cape = [r.get("cape") for m2 in R.values() for h, r in ((h, m2.get(uren[h])) for h in range(9, 18)) if r and r.get("cape") is not None]
        if cape: meteo["cape_max_alle_modellen"] = rnd(max(cape))
        waarsch = []
        Tm = max(r["T"] for _, r in dag_uren)
        if Tm >= 30: waarsch.append(f"Hitte: max {rnd(Tm)} °C — veel drinken, hoofddeksel, cockpit niet laten opwarmen, langere startrol")
        elif Tm >= 25: waarsch.append("Warm: veel drinken en hoofddeksel")
        if r13.get("fz") and r13["fz"] < 1000: waarsch.append("Vriesniveau laag: kans op ijsafzetting in wolken/neerslag")
        if waarsch: meteo["waarschuwing_kandidaat"] = waarsch

    # mist en rijp (03–09 u, alle modellen)
    mist = {}
    for m, rs2 in R.items():
        hits, rijp = [], False
        for h in range(3, 10):
            r = rs2.get(uren[h])
            if not r or r.get("T") is None or r.get("Td") is None:
                continue
            sp = r["T"] - r["Td"]
            helder = (r.get("L") is not None and r["L"] < 30)
            if (sp <= 1 and (r.get("ws") or 0) < 5 and helder) or (r.get("fog") or 0) > 0:
                hits.append(h)
            if r["T"] <= 2 and (sp <= 1 or helder):
                rijp = True
        mist[MNAAM.get(m, m)] = {"mist_uren": hits, "rijp": rijp}
    mn = [r["T"] for m, rs2 in R.items() for h in range(5, 10) if rs2.get(uren[h]) and rs2[uren[h]].get("T") is not None for r in [rs2[uren[h]]]]
    mist["ochtendminimum_alle_modellen"] = rnd(min(mn), 1) if mn else None

    return {"datum": dag.isoformat(), "type": typ, "zonOp": zon.get("sunrise"), "zonOnder": zonder,
            "baan_per_blok": [b["baan"] for b in blokken], "blokken": blokken, "meteo": meteo,
            "mist_rijp": mist, "uurtabel": tabel}


# ───────────────────────── vooruitzicht ─────────────────────────

def vooruitzichtdag(dag, Rdet, ens):
    d0 = datetime.combine(dag, datetime.min.time()).replace(tzinfo=TZ)
    uren = [d0 + timedelta(hours=h) for h in range(9, 18)]
    det = {}
    for m, rs in Rdet.items():
        rr = [rs.get(u) for u in uren if rs.get(u)]
        if not rr:
            continue
        bas = [basis(r) for r in rr]
        det[MNAAM.get(m, m)] = {
            "neerslag_mm": round(sum((r.get("pr") or 0) for r in rr), 1),
            "laag_gem_pct": rnd(med([r.get("L") for r in rr])),
            "laagste_basis_m": r50(min([b for b in bas if b is not None])) if any(b is not None for b in bas) else None,
            "wind_kt": rnd(med([r.get("ws") for r in rr])), "richting": windwoord(vrichting([r.get("wd") for r in rr], [r.get("ws") for r in rr])),
            "max_vlaag_kt": rnd(max([r["gu"] for r in rr if r.get("gu") is not None], default=0)) or None,
            "max_xvlaag_kt": rnd(max([x for x in (xw(r.get("gu"), r.get("wd")) for r in rr) if x is not None], default=None)) if any(r.get("gu") for r in rr) else None,
            "max_gem_zijwind_kt": rnd(max([xw(r.get("ws"), r.get("wd")) or 0 for r in rr])),
            "cape_max": mx(rr, "cape"), "grenslaag_max_m": r50(mx(rr, "blh")),
            "zon_max_wm2": mx(rr, "sw"), "tmax": rnd(max(r["T"] for r in rr if r.get("T") is not None)),
            "wind_850_max_kt": mx(rr, "w85"),
        }
        och = [rs.get(d0 + timedelta(hours=h)) for h in (5, 6, 7, 8)]
        och = [r for r in och if r and r.get("T") is not None]
        if och:
            det[MNAAM.get(m, m)]["ochtendmin"] = rnd(min(r["T"] for r in och), 1)
            det[MNAAM.get(m, m)]["mist_ochtend"] = any((r["T"] - (r.get("Td") or -99)) <= 1 and (r.get("ws") or 0) < 5 for r in och)
    kansen = None
    if ens:
        h = ens["hourly"]
        idx = {datetime.fromisoformat(t).replace(tzinfo=TZ): i for i, t in enumerate(h["time"])}
        leden = sorted({k.split("_member")[1] for k in h if "_member" in k})
        leden = [""] + ["_member" + x for x in leden]
        tel = {"laagdek": 0, "regen": 0, "vlagen": 0, "zijwind": 0, "ochtendmist": 0}
        n = 0
        for lid in leden:
            def v(var, u):
                arr = h.get(var + lid)
                i = idx.get(u)
                return None if arr is None or i is None else arr[i]
            L = [v("cloud_cover_low", u) for u in uren]
            if any(x is None for x in L):
                continue
            n += 1
            T = [v("temperature_2m", u) for u in uren]; Td = [v("dew_point_2m", u) for u in uren]
            sp = [a - b for a, b in zip(T, Td) if a is not None and b is not None]
            if st.mean(L) >= 60 and sp and st.mean(sp) <= 3: tel["laagdek"] += 1
            if sum((v("precipitation", u) or 0) for u in uren) >= 1: tel["regen"] += 1
            G = [v("wind_gusts_10m", u) or 0 for u in uren]
            if max(G) > 20: tel["vlagen"] += 1
            if max(g * abs(math.sin(math.radians((v("wind_direction_10m", u) or 0) - BAAN))) for g, u in zip(G, uren)) > 12: tel["zijwind"] += 1
            om = [(v("temperature_2m", d0 + timedelta(hours=hh)), v("dew_point_2m", d0 + timedelta(hours=hh)), v("wind_speed_10m", d0 + timedelta(hours=hh))) for hh in (7, 8, 9)]
            if any(a is not None and b is not None and c is not None and a - b <= 1 and c < 5 for a, b, c in om): tel["ochtendmist"] += 1
        if n:
            p = {k: int(round(100.0 * c / n / 10.0) * 10) for k, c in tel.items()}
            kansen = {"leden": n, **p,
                      "tekst": f"laag dek {p['laagdek']} % · regen {p['regen']} % · vlagen > 20 kt {p['vlagen']} % · zijwind > 12 kt {p['zijwind']} % · ochtendmist {p['ochtendmist']} %"}
            if p["laagdek"] >= 70 or p["regen"] >= 60 or p["vlagen"] >= 50: kansen["kleur_voorstel"] = "rood"
            elif p["laagdek"] <= 30 and p["regen"] <= 20 and p["vlagen"] <= 20 and p["zijwind"] <= 20: kansen["kleur_voorstel"] = "groen (als geen buien in de modellen)"
            else: kansen["kleur_voorstel"] = "oranje"
            vals = list(p.values())
            kansen["zekerheid_voorstel"] = "laag" if any(40 <= x <= 60 for x in vals) else ("hoog" if all(x <= 20 or x >= 80 for x in vals) else "matig")
    return {"datum": dag.isoformat(), "dagen_vooruit": None, "modellen": det, "ensemble": kansen}


# ───────────────────────── hoofdprogramma ─────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--briefing", default="", help="DATUM:type[,DATUM:type]")
    ap.add_argument("--vooruitzicht", default="", help="DATUM[,DATUM]")
    ap.add_argument("--uit", required=True)
    a = ap.parse_args()
    os.makedirs(a.uit, exist_ok=True)
    vandaag = datetime.now(TZ).date()
    bdagen = [(date.fromisoformat(x.split(":")[0]), x.split(":")[1]) for x in a.briefing.split(",") if x]
    vdagen = [date.fromisoformat(x) for x in a.vooruitzicht.split(",") if x]
    status, uit = {}, {"gemaakt": datetime.now(TZ).isoformat(timespec="seconds"), "briefings": [], "vooruitzicht": []}

    if bdagen:
        s, e = min(d for d, _ in bdagen).isoformat(), max(d for d, _ in bdagen).isoformat()
        ok, metar, f = curl("https://aviationweather.gov/api/data/metar?ids=EBBR,EHEH,EBAW&format=raw&hours=3")
        uit["metar"] = metar.strip() if ok else "niet bereikbaar: " + f
        ok, taf, f = curl("https://aviationweather.gov/api/data/taf?ids=EBBR,EHEH,EBAW&format=raw")
        uit["taf"] = taf.strip() if ok else "niet bereikbaar: " + f
        j1a, status["openmeteo"] = jhaal(
            "https://api.open-meteo.com/v1/forecast?latitude=51.2647&longitude=4.7533&hourly=temperature_2m,dew_point_2m,cloud_cover_low,cloud_cover_mid,cloud_cover_high,cloud_cover,visibility,wind_speed_10m,wind_direction_10m,wind_gusts_10m,wind_speed_850hPa,wind_direction_850hPa,boundary_layer_height,cape,lifted_index,shortwave_radiation,precipitation,precipitation_probability,pressure_msl,freezing_level_height,temperature_925hPa,temperature_850hPa,geopotential_height_925hPa,geopotential_height_850hPa&daily=sunrise,sunset,temperature_2m_max&timezone=Europe/Brussels"
            + f"&start_date={s}&end_date={e}", "Open-Meteo best match")
        j1b = None
        if status["openmeteo"] != "daglimiet":
            j1b, st1b = jhaal(
                "https://api.open-meteo.com/v1/forecast?latitude=51.2647&longitude=4.7533&hourly=temperature_2m,dew_point_2m,cloud_cover_low,cloud_cover_mid,cloud_cover_high,visibility,wind_speed_10m,wind_direction_10m,wind_gusts_10m,cape,shortwave_radiation,precipitation&models=icon_d2,ecmwf_ifs025,meteofrance_arome_france_hd&timezone=Europe/Brussels"
                + f"&start_date={s}&end_date={e}", "Open-Meteo modelvergelijking")
            status["openmeteo_modellen"] = st1b
    jm, status["metno"] = jhaal(f"https://api.met.no/weatherapi/locationforecast/2.0/complete?lat=51.26&lon=4.75", "MET Norway", ua=UA)
    uit["metno_updated_at"] = jm["properties"]["meta"]["updated_at"] if jm else None

    if bdagen:
        daily = {}
        if j1a:
            for i, t in enumerate(j1a["daily"]["time"]):
                daily[t] = {"sunrise": j1a["daily"]["sunrise"][i][11:16], "sunset": j1a["daily"]["sunset"][i][11:16], "tmax": j1a["daily"]["temperature_2m_max"][i]}
        R = {}
        if j1a: R["best"] = reeks_openmeteo(j1a["hourly"])
        if j1b:
            for m in MODELLEN:
                rs = reeks_openmeteo(j1b["hourly"], "_" + m)
                if any(v.get("T") is not None for v in rs.values()):
                    R[m] = {k: v for k, v in rs.items() if v.get("T") is not None}
        if jm: R["metno"] = reeks_metno(jm)
        for d, typ in bdagen:
            uit["briefings"].append(briefingdag(d, typ, R, daily))
        uit["bronnen_beschikbaar"] = [MNAAM.get(m, m) for m in R]
        # kaarten
        kaarten = []
        for d, typ in bdagen:
            n = (d - vandaag).days
            for mod in ("GFS", "ICON"):
                pad = os.path.join(a.uit, f"blipmap_{mod}_NL{n}.png")
                ok, _, f = curl(f"https://blipmaps.nl/NL4KM{mod}/NL+{n}/meteogram_Oostmalle_Zoersel.png", out=pad)
                kaarten.append({"bron": f"BLIPMAP {mod} dag +{n} ({d})", "pad": pad if ok else None, "fout": f})
        pad = os.path.join(a.uit, "dwd_analyse.png")
        ok, _, f = curl("https://www.dwd.de/DWD/wetter/wv_spez/hobbymet/wetterkarten/bwk_bodendruck_na_ana.png", out=pad)
        if ok:
            try:  # verklein de grote analysekaart tot West-Europa
                from PIL import Image
                im = Image.open(pad); w, hh = im.size
                im.convert("RGB").crop((int(w * .3), int(hh * .2), int(w * .75), int(hh * .65))).save(pad.replace(".png", "_west.jpg"), quality=80)
                im.convert("RGB").crop((0, int(hh * .92), int(w * .2), hh)).save(pad.replace(".png", "_tijd.jpg"))
                kaarten.append({"bron": "DWD analyse (West-Europa)", "pad": pad.replace(".png", "_west.jpg")})
                kaarten.append({"bron": "DWD analyse (tijdstempel)", "pad": pad.replace(".png", "_tijd.jpg")})
            except Exception as e:
                kaarten.append({"bron": "DWD analyse", "pad": pad, "fout": "verkleinen mislukt: " + str(e)[:80]})
        else:
            kaarten.append({"bron": "DWD analyse", "pad": None, "fout": f})
        nu_utc = datetime.now(timezone.utc)
        run = nu_utc.replace(hour=0 if nu_utc.hour < 12 else 12, minute=0, second=0, microsecond=0) - timedelta(hours=6)
        for d, typ in bdagen + [(x, "vooruitzicht") for x in vdagen]:
            if typ == "ochtend":
                continue
            doel = datetime.combine(d, datetime.min.time(), tzinfo=timezone.utc) + timedelta(hours=12)
            stappen = sorted([12, 24, 36, 48, 60, 72, 84, 96], key=lambda s: abs((run + timedelta(hours=s) - doel).total_seconds()))
            for s in stappen[:2]:
                pad = os.path.join(a.uit, f"dwd_verwachting_{s:03d}.png")
                if os.path.exists(pad):
                    break
                ok, _, f = curl(f"https://www.dwd.de/DWD/wetter/wv_spez/hobbymet/wetterkarten/ico_tkboden_na_{s:03d}.png", out=pad)
                try:
                    from PIL import Image
                    Image.open(pad).verify()
                    kaarten.append({"bron": f"DWD verwachting +{s} h (bedoeld voor {d} 12 UTC; controleer VT onderaan)", "pad": pad})
                    break
                except Exception:
                    continue
        uit["kaarten"] = kaarten

    if vdagen:
        s, e = min(vdagen).isoformat(), max(vdagen).isoformat()
        jv, status["openmeteo_vooruitzicht"] = jhaal(
            "https://api.open-meteo.com/v1/forecast?latitude=51.2647&longitude=4.7533&hourly=temperature_2m,dew_point_2m,cloud_cover_low,cloud_cover_mid,cloud_cover_high,visibility,wind_speed_10m,wind_direction_10m,wind_gusts_10m,wind_speed_850hPa,wind_direction_850hPa,cape,shortwave_radiation,precipitation,precipitation_probability,boundary_layer_height&models=ecmwf_ifs025,icon_seamless,gfs_seamless&timezone=Europe/Brussels"
            + f"&start_date={s}&end_date={e}", "Open-Meteo vooruitzicht") if status.get("openmeteo") != "daglimiet" else (None, "daglimiet")
        je, status["ensemble"] = jhaal(
            "https://ensemble-api.open-meteo.com/v1/ensemble?latitude=51.2647&longitude=4.7533&hourly=cloud_cover_low,precipitation,wind_speed_10m,wind_gusts_10m,wind_direction_10m,temperature_2m,dew_point_2m&models=ecmwf_ifs025&wind_speed_unit=kn&timezone=Europe/Brussels"
            + f"&start_date={s}&end_date={e}", "Open-Meteo ensemble")
        Rd = {}
        if jv:
            for m in ["ecmwf_ifs025", "icon_seamless", "gfs_seamless"]:
                rs = reeks_openmeteo(jv["hourly"], "_" + m)
                rs = {k: v for k, v in rs.items() if v.get("T") is not None}
                if rs: Rd[m] = rs
        if jm: Rd["metno"] = reeks_metno(jm)
        MNAAM.update({"icon_seamless": "ICON", "gfs_seamless": "GFS"})
        for d in vdagen:
            v = vooruitzichtdag(d, Rd, je)
            v["dagen_vooruit"] = (d - vandaag).days
            uit["vooruitzicht"].append(v)
    else:
        status["ensemble"] = "niet nodig"

    uit["status"] = status
    uit["meldingen"] = LOG
    uit["looptijd_s"] = round(time.time() - T0)
    with open(os.path.join(a.uit, "samenvatting.json"), "w") as f:
        json.dump(uit, f, ensure_ascii=False, indent=1)
    # compacte tekstversie (zonder uurtabel) om te lezen
    kort = json.loads(json.dumps(uit))
    for b in kort["briefings"]:
        b.pop("uurtabel", None)
    with open(os.path.join(a.uit, "samenvatting.txt"), "w") as f:
        json.dump(kort, f, ensure_ascii=False, indent=1)
    print(json.dumps(kort, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
