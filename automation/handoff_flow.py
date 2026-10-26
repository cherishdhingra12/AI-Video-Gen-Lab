"""handoff_flow.py — finish radio-ki-yaadein's last clips on Google Flow (owner's call, 2026-10-24).

The reel is locked on googlevids, and all three googlevids wallets are monthly-dry until
Nov 1 (the site's own words). The owner chose to finish the remaining clips on Flow
instead of waiting: Vids and Flow are the same Veo generation on Google's side, so the
reel's look stays consistent — the one-look rule is kept in spirit, on the owner's word.

This does NOT move the lock: project_site() still says googlevids because that column is
clip history, and rewriting it would lie about where clips 0-1 came from. Each remaining
queued clip is driven by process_job() itself — the exact production machinery (cookie
refresh, identity check, credits observation, poll, download, walls -> cooldown + requeue)
— with Brain.pick(strict=True, want_site="flow") supplying the Flow account via the house
rotation (a1 -> a2 -> a3 -> a1, empty or cooling accounts skipped automatically). If Flow
runs out of daily credits mid-reel, the clip requeues with no lost retry and the script
stops; a later invocation (or the next day) simply continues where it left off.

    python3 automation/handoff_flow.py > /tmp/flow_handoff.log 2>&1 &
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import runner                                 # noqa: E402
from state import State                       # noqa: E402
from learn import Brain                       # noqa: E402

PROJECT = "radio-ki-yaadein"


def main():
    st, br = State(), Brain()
    while True:
        todo = [j for j in st.project_clips(PROJECT) if j["status"] != "done"]
        if not todo:
            print(f"handoff: {PROJECT} has every clip it needs")
            break
        job = min(todo, key=lambda j: j["idx"])
        if job["status"] != "queued":
            print(f"handoff: clip #{job['idx']} was '{job['status']}' — re-queueing it")
            st.set_status(job["id"], "queued")
        pay = br.pick(want_site="flow", strict=True, need=1, cost_of=runner._adapter)
        if not pay:
            print("handoff: flow has no payable account right now — stopping here; render later")
            break
        print(f"handoff: clip #{job['idx']} -> {pay[0]}/{pay[1]}")
        r = runner.process_job(st, br, job, headless=True, pick_override=pay)
        print(f"handoff: clip #{job['idx']} -> {r}")
        if r != "done":                       # the clip requeued itself (wall/cooldown/dry)
            break
    runner.stitch_done_projects(st)
    st.close()


if __name__ == "__main__":
    main()
