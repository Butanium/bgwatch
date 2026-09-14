#!/usr/bin/env python3
"""A job that logs progress, hits a traceback mid-way but keeps running, stalls, then
exits 0. Used by the bgwatch tests and by the whowill adoption experiment.

usage: fake_job.py [--steps N] [--dt S] [--crash-at STEP] [--stall S] [--exit-code C]
"""
import argparse
import sys
import time

p = argparse.ArgumentParser()
p.add_argument("--steps", type=int, default=12)
p.add_argument("--dt", type=float, default=0.5)
p.add_argument("--crash-at", type=int, default=5)
p.add_argument("--stall", type=float, default=0, help="seconds of silence after the crash")
p.add_argument("--exit-code", type=int, default=0)
a = p.parse_args()

for step in range(1, a.steps + 1):
    print(f"step {step}/{a.steps} loss=0.{100 - step:02d} error_rate=0.01", flush=True)
    if step == a.crash_at:
        print("Traceback (most recent call last):", flush=True)
        print('  File "train.py", line 88, in <module>', flush=True)
        print("    run(cfg)", flush=True)
        print('  File "train.py", line 41, in run', flush=True)
        print("    x = batch[key]", flush=True)
        print("KeyError: 'labels'", flush=True)
        print("ERROR: eval step skipped, continuing", flush=True)
        if a.stall:
            time.sleep(a.stall)
    time.sleep(a.dt)
print("done", flush=True)
sys.exit(a.exit_code)
