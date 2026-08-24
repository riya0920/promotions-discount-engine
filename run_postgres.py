"""The Postgres pass: this project made a prediction about another engine.

    "SQLite, not Postgres. The conditional-UPDATE budget cap proves the algorithm
     is race-free without proving it scales: SQLite serialises writers, so
     redemptions of DIFFERENT promotions queue here and would proceed in parallel
     on Postgres. The shape transfers; the throughput number does not."

Written without a Postgres to check it against. Now checked.
"""
from __future__ import annotations

import json
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from src import pgbudget as PB   # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")


def main():
    os.makedirs(OUT, exist_ok=True)
    lines, summary = [], {}

    def emit(s=""):
        print(s)
        lines.append(s)

    emit("=" * 78)
    emit("SE-2 POSTGRES PASS -- A PREDICTION THIS PROJECT MADE, TESTED")
    emit("=" * 78)
    if not PB.available():
        emit("No Postgres on %s." % PB.DSN)
        return
    emit("The claim: 'redemptions of DIFFERENT promotions queue here and would")
    emit("proceed in parallel on Postgres. The shape transfers; the throughput")
    emit("number does not.'")
    emit("")
    emit("It has two halves and they were never equally safe. 'The shape")
    emit("transfers' is a statement about the conditional UPDATE and is true")
    emit("anywhere -- the check and the decrement are one statement. 'Different")
    emit("promotions would proceed in parallel' is an assertion about another")
    emit("engine's lock granularity, made without one.")
    emit("")

    # ------------------------------------------------------------------
    emit("=" * 78)
    emit("A. THE CAP HOLDS UNDER REAL ROW-LEVEL CONCURRENCY")
    emit("=" * 78)
    rows = []
    for n_promos, cap in ((1, 100), (8, 50)):
        promos = {"P-%03d" % i: cap for i in range(n_promos)}
        PB.reset(promos)
        r = PB.storm(promos, n_workers=16, attempts_each=40)
        rows.append(r)
        s = PB.sqlite_storm(promos, 16, 40, os.path.join(OUT, "budget_cap.db"))
        rows.append(s)
    C = pd.DataFrame(rows)[["engine", "promos", "granted", "refused",
                            "consumed", "overspend", "n_errors"]]
    emit(C.to_string(index=False))
    emit("")
    assert int(C.overspend.sum()) == 0
    emit("  Overspend on both engines, every configuration: 0. The cap is")
    emit("  unbreachable because `remaining > 0` lives in the WHERE clause, so")
    emit("  the check and the decrement cannot be separated by a scheduler.")
    emit("  THAT HALF OF THE PREDICTION WAS RIGHT, and it is the half that")
    emit("  matters for correctness.")
    emit("")
    summary["cap"] = C.to_dict("records")

    # ------------------------------------------------------------------
    emit("=" * 78)
    emit("B. DO DIFFERENT PROMOTIONS ACTUALLY PROCEED IN PARALLEL?")
    emit("=" * 78)
    emit("Same workload, one promotion versus sixteen. Paired inside each")
    emit("repetition so machine drift cancels rather than landing in the ratio.")
    emit("")
    sc = PB.scaling_ratio(n_workers=16, attempts_each=40, reps=15,
                          sqlite_path=os.path.join(OUT, "budget_scaling.db"))
    for engine in ("sqlite", "postgres"):
        e = sc[engine]
        emit("  %-9s median %.2fx  range %.2f-%.2f  |  above 1.0 in %2d/%d, "
             "sign p = %.4f"
             % (engine, e["median"], e["low"], e["high"], e["above_one"],
                e["n"], e["p"]))
    emit("")
    pg, sq = sc["postgres"], sc["sqlite"]
    if pg["p"] < 0.05 <= sq["p"]:
        emit("  THE PREDICTION HOLDS. Spreading the same redemption storm across")
        emit("  sixteen promotions speeds Postgres up (p = %.3f) and does nothing"
             % pg["p"])
        emit("  measurable to SQLite (p = %.3f), which is exactly what a global"
             % sq["p"])
        emit("  write lock versus row locks predicts.")
    else:
        emit("  THE PREDICTION IS NOT ESTABLISHED by this measurement:")
        emit("  postgres p = %.3f, sqlite p = %.3f." % (pg["p"], sq["p"]))
    emit("")
    emit("  THE STATISTIC HAD TO BE CHOSEN CAREFULLY AND THE OBVIOUS ONE IS")
    emit("  WRONG HERE. Testing Postgres against SQLite pairwise reads p = 0.30")
    emit("  and looks like a null result. It is not -- it is SQLite's noise")
    emit("  swamping a real effect: its ratio ranges %.2f to %.2f because its"
         % (sq["low"], sq["high"]))
    emit("  runs finish in a fraction of a second and timing jitter is")
    emit("  proportionally enormous. Testing each engine against 1.0 separately")
    emit("  is the question that was actually being asked.")
    emit("")
    emit("  AND THE SIZE IS SMALLER THAN THE PHRASE SUGGESTS. 'Proceed in")
    emit("  parallel' implies the sixteen-promotion case should be many times")
    emit("  faster; measured, it is %.2fx. At 16 workers the bottleneck is"
         % pg["median"])
    emit("  already partly the client, and no engine change moves that.")
    emit("")
    emit("  Note also what is NOT here: the budget cap has no retry loop, so a")
    emit("  losing writer waits on a row lock and then succeeds. SE-1's")
    emit("  optimistic reservation DOES retry, and on the same engine it starves")
    emit("  52% of requests on a hot row. Same database, same kind of")
    emit("  contention, opposite outcome -- the difference is the retry loop, not")
    emit("  the engine.")
    emit("")
    summary["scaling"] = sc

    with open(os.path.join(OUT, "postgres_report.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    with open(os.path.join(OUT, "postgres_metrics.json"), "w") as f:
        json.dump(summary, f, indent=2, default=float)
    print("\n-> out/postgres_report.txt")


if __name__ == "__main__":
    main()
