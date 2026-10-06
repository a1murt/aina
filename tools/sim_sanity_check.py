"""Rough 1-minute fluid model used to pick parameters in config/simulation.yaml.

NOT the virtual plant: services/sim is a unit-level SimPy DES (SPEC §6) and must pass its own
calibration test (FR-SIM-03). This script only shows that calibration_targets are reachable with
the current parameters and documents their intended semantics (wear only for wear_reasons,
degraded mode for class B, filter dP limit, repaint consuming paint capacity, shift B defect factor).

Checked results (20 working days): throughput 114.3-114.5 units/shift; defect rate WELD 2.3%,
PAINT 4.4-4.9%, ASSY 1.3%; unplanned downtime 12-41 min per area per day.

Run from repo root:  python -I tools/sim_sanity_check.py config [seed]
"""
import math, random, sys
import yaml
cfgdir = sys.argv[1]
sim = yaml.safe_load(open(f"{cfgdir}/simulation.yaml", encoding="utf-8"))
plant = yaml.safe_load(open(f"{cfgdir}/plant.yaml", encoding="utf-8"))
rng = random.Random(int(sys.argv[2]) if len(sys.argv) > 2 else 42)
lines = []
for a in plant["areas"]:
    for l in a.get("lines", []):
        lines.append(l)
names = [l["code"] for l in lines]
ict = {l["code"]: l["ict_seconds"] for l in lines}
equip = [(l["code"], e) for l in lines for e in l["equipment"]]
F, M, D = sim["failures"], sim["microstops"], sim["degradation"]
gain = D["wear_hazard_gain"]
st = {e["code"]: {"line": ln, "type": e["type"], "cls": e["criticality"], "deg": e["degraded_capacity"], "d": 0.2 if e["type"] in D else 0.0,
                  "rem": 0.0, "micro": 0.0} for ln, e in equip}
booths = [c for c, s in st.items() if s["type"] == "booth"]
dp = {b: rng.uniform(150, 440) for b in booths}
pf = sim["paint_filters"]
def lognorm(med, sig): return med * math.exp(rng.gauss(0, sig))
buf_cap = {b["code"]: b["capacity"] for b in plant["buffers"]}
B = dict(sim["process"]["initial_buffers"])
order = ["WELD-1", "PAINT-1", "ASSY-1", "QC-1"]; bufs = ["BIW", "PBS", "EOL"]
days = 20; out_shift = []; defects = {"WELD": [0, 0], "PAINT": [0, 0], "ASSY": [0, 0]}
unpl = {"WELD": 0.0, "PAINT": 0.0, "ASSY": 0.0}
repaint_q = 0.0
for day in range(days):
    for shift in (0, 1):
        produced = 0.0
        for minute in range(480):
            # PM at shift A start
            if shift == 0 and minute == 0:
                for i, (c, s) in enumerate(st.items()):
                    if s["type"] == "robot" and (day + i) % 10 == 0: s["rem"] = max(s["rem"], 30); s["pm"] = True; s["d"] = max(0.05, s["d"] - D["robot"]["pm_reduction"])
                    if s["type"] == "conveyor" and (day + i) % 5 == 0: s["rem"] = max(s["rem"], 20); s["pm"] = True
            cap = {ln: 60.0 / ict[ln] / sim["process"]["cycle_noise"]["median_factor"] for ln in order}
            for c, s in st.items():
                t = s["type"]
                if s["rem"] <= 0 and s["micro"] <= 0:
                    s["pm"] = False
                    if t in F:
                        f = F[t]; lam0 = 1 / (f["mtbf_h"] * 60)
                        wear = set(f.get("wear_reasons", []))
                        for r, share in f["reasons"].items():
                            mult = 1 + gain * s["d"] ** 3 if r in wear else 1
                            if rng.random() < lam0 * share * mult:
                                s["rem"] = lognorm(f["mttr"]["median"], f["mttr"]["sigma"]); s["reason"] = r
                                if r in wear: s["wearfail"] = True
                                break
                        if "chain_break" in f and s["rem"] <= 0:
                            cb = f["chain_break"]
                            if rng.random() < (1 / (cb["mtbf_h"] * 60)) * (1 + gain * s["d"] ** 3):
                                s["rem"] = lognorm(cb["mttr"]["median"], cb["mttr"]["sigma"]); s["wearfail"] = True
                    if t in M and s["rem"] <= 0 and rng.random() < 1 / (M[t]["mtbf_h"] * 60):
                        s["micro"] = lognorm(M[t]["duration"]["median"], M[t]["duration"]["sigma"])
                    if t == "booth" and s["rem"] <= 0 and dp[c] >= pf["dp_limit_pa"]:
                        s["rem"] = lognorm(pf["replacement"]["median"], pf["replacement"]["sigma"]); dp[c] = pf["dp_start_pa"]
                down = s["rem"] > 0 or s["micro"] > 0
                if down:
                    factor = s["deg"]
                    cap[s["line"]] *= factor if s["cls"] in ("A", "B") else 1
                    if s["rem"] > 0 and not s.get("pm"):
                        area = s["line"].split("-")[0]
                        if area in unpl: unpl[area] += 1
                    if s["rem"] > 0:
                        s["rem"] -= 1
                        if s["rem"] <= 0 and s.pop("wearfail", False): s["d"] = D[t]["reset_after_repair"]
                    else:
                        s["micro"] -= 1
                else:
                    if t in D: s["d"] = min(1.0, s["d"] + rng.gammavariate(D[t]["shape"], D[t]["mean_rate_per_h"] / D[t]["shape"]) / 60)
                    if t == "booth": dp[c] += rng.gauss(pf["dp_rate_pa_per_h"]["mean"], 0.2) / 60
            # defect probabilities
            hum = 55 + 10 * math.sin(2 * math.pi * ((7 + shift * 8 + minute / 60) % 24) / 24) + rng.gauss(0, 1.5)
            robots = [s["d"] for s in st.values() if s["type"] == "robot"]
            pw = (sim["defects"]["WELD"]["base"] + sim["defects"]["WELD"]["robot_wear_gain"] * sum(robots) / len(robots))
            dpm = max(dp.values())
            pp = sim["defects"]["PAINT"]["base"] + sim["defects"]["PAINT"]["dp_gain"] * min(1, max(0, (dpm - 300) / 150)) + (sim["defects"]["PAINT"]["humidity_out_of_spec_add"] if not 45 <= hum <= 65 else 0)
            pa = sim["defects"]["ASSY"]["base"]
            sf = 1.1 if shift == 1 else 1.0
            pw, pp, pa = pw * sf, pp * sf, pa * sf
            # paint capacity consumed by repaint
            rep = 0.4 * pp
            capP = cap["PAINT-1"] * (1 - rep)
            x = {}
            x["WELD-1"] = min(cap["WELD-1"], buf_cap["BIW"] - B["BIW"] + capP)
            x["PAINT-1"] = min(capP, B["BIW"] + x["WELD-1"], buf_cap["PBS"] - B["PBS"] + cap["ASSY-1"])
            x["ASSY-1"] = min(cap["ASSY-1"], B["PBS"] + x["PAINT-1"], buf_cap["EOL"] - B["EOL"] + cap["QC-1"])
            x["QC-1"] = min(cap["QC-1"], B["EOL"] + x["ASSY-1"])
            B["BIW"] += x["WELD-1"] - x["PAINT-1"]; B["PBS"] += x["PAINT-1"] - x["ASSY-1"]; B["EOL"] += x["ASSY-1"] - x["QC-1"]
            produced += x["QC-1"]
            defects["WELD"][0] += x["WELD-1"] * pw; defects["WELD"][1] += x["WELD-1"]
            defects["PAINT"][0] += x["PAINT-1"] * pp; defects["PAINT"][1] += x["PAINT-1"]
            defects["ASSY"][0] += x["ASSY-1"] * pa; defects["ASSY"][1] += x["ASSY-1"]
        out_shift.append(produced)
import statistics as stt
print("throughput/shift mean %.1f  sd %.1f  min %.1f max %.1f" % (stt.mean(out_shift), stt.pstdev(out_shift), min(out_shift), max(out_shift)))
for a, (d, n) in defects.items(): print(a, "defect rate %.4f" % (d / n))
for a, m in unpl.items(): print(a, "unplanned down min/day %.1f" % (m / days))
print("targets:", sim["calibration_targets"])
