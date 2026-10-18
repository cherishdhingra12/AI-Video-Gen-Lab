#!/usr/bin/env bash
# Run every OFFLINE self-check: no browser, no site, no LLM/vision gateway.
#
#   ./check.sh                 # offline suite (this is the gate)
#   ./check.sh --with-network  # + agents.py + vision.py live round-trips (spends quota)
#   PYTHON=/path/to/python3 ./check.sh    # force an interpreter
#
# Exits non-zero if any check fails.
set -uo pipefail
cd "$(dirname "$0")"

# Pick an interpreter that has the runtime deps; fall back to plain python3 and say so.
if [ -n "${PYTHON:-}" ]; then
    PY="$PYTHON"
else
    PY=""
    for c in python3 /usr/local/bin/python3 /opt/homebrew/bin/python3; do
        command -v "$c" >/dev/null 2>&1 || continue
        if [ -z "$PY" ]; then PY="$c"; fi
        if "$c" -c "import playwright, PIL, numpy, browser_cookie3" >/dev/null 2>&1; then
            PY="$c"; break
        fi
    done
fi
[ -n "$PY" ] || { echo "no python3 found"; exit 2; }
echo "python: $PY ($("$PY" -V 2>&1))"
"$PY" -c "import playwright, PIL, numpy, browser_cookie3" >/dev/null 2>&1 \
    || echo "WARN: browser deps missing in this interpreter — browser runs (runner/login) will fail; self-checks still pass"

# The project promises `python3 <file>`: prove the modules still IMPORT under whatever plain
# `python3` is on PATH (a 3.10+ annotation crashes 3.9 at def time, which py_compile misses).
PATH_PY="$(command -v python3 || true)"

fails=0
run() {                                   # run <label> <cmd...>
    printf '\n== %s\n' "$1"
    shift
    if "$@"; then :; else
        fails=$((fails + 1))
        echo "FAILED: $*"
    fi
}

if [ -n "$PATH_PY" ]; then
    run "python3-compat (imports under $($PATH_PY -V 2>&1))" \
        "$PATH_PY" -c "import sys; sys.path[:0] = ['automation', 'automation/sites']; import vision, state, learn, base, qc, brain, agents, music, pipeline"
else
    echo "== python3-compat\nSKIP: no python3 on PATH"
fi

run "state (jobs/resume/credits/lessons/qc-verdicts)" "$PY" automation/state.py --selfcheck
run "learn (backoff + scheduler routing)"             "$PY" automation/learn.py --selfcheck
run "login (credit seeding + unclear reads)"          "$PY" automation/login.py --selfcheck
run "agents (style-anchored clip prompts)"            "$PY" agents.py --selfcheck
run "pipeline (idea -> a unique project folder)"      "$PY" pipeline.py --selfcheck
run "runner (resume, wall->requeue, stitch, gate)"    "$PY" automation/runner.py --selfcheck
run "stitch (xfade offsets, 9:16 normalize)"          "$PY" stitch.py
run "qc (craft gate catches bad reels)"               "$PY" automation/qc.py --selfcheck
run "caption (empty-spot placement + Hindi render)"   "$PY" automation/caption.py --selfcheck
run "assemble (clips -> reel, auto-placed cards)"     "$PY" automation/assemble.py --selfcheck
run "music (measure + fit the score, no filename guessing)" "$PY" automation/music.py --selfcheck
run "publishing (cover + sheet from the reel itself)"  "$PY" automation/publishing.py --selfcheck
run "vision (offline parsing)"                        "$PY" automation/vision.py --selfcheck
run "measure (per-agent model scoring + picks)"       "$PY" automation/measure.py --selfcheck
run "crisis (a stop reaches a person AND the brain)"  "$PY" automation/crisis.py --selfcheck
run "site adapter base (self-heal click)"             "$PY" automation/sites/base.py
run "brain (report/reflect/propose/harvest)"          "$PY" brain.py --selfcheck

if command -v node >/dev/null 2>&1; then
    run "opencode brain plugin (node)" node automation/test_brain_plugin.mjs
else
    echo -e "\n== opencode brain plugin\nSKIP: node not on PATH"
fi

if [ "${1:-}" = "--with-network" ]; then
    run "agents (live LLM end-to-end)"  "$PY" agents.py
    run "vision (live gateway round-trip)" "$PY" automation/vision.py
else
    echo -e "\n(live gateway checks skipped — ./check.sh --with-network to run them)"
fi

echo
if [ "$fails" -eq 0 ]; then
    echo "ALL OFFLINE SELF-CHECKS PASSED"
else
    echo "$fails CHECK(S) FAILED"
    exit 1
fi
