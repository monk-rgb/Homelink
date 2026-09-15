"""Payout worker.

Runs the 24-hour payout release process. Wire this into any scheduler (Windows
Task Scheduler, cron, a systemd timer, or your host's cron add-on) so it runs
every few minutes. It is idempotent: each payout is claimed atomically, so it
is safe to run as often as you like and safe to run two copies at once.

Usage:
    python payout_worker.py            # process all due payouts once
    python payout_worker.py --loop 300 # keep running, every 300 seconds

The HTTP endpoint POST /internal/payouts/run does the same thing when you
prefer to trigger it from a hosted cron service instead of a local process.
"""

import argparse
import sys
import time
import traceback

import app as appmod


def run_once():
    try:
        results = appmod.process_due_payouts()
    except Exception:  # never let one bad payout kill the loop
        traceback.print_exc()
        return []
    if results:
        actions = {}
        for r in results:
            actions[r.get('action', 'unknown')] = actions.get(r.get('action', 'unknown'), 0) + 1
        print('payouts processed:', actions, flush=True)
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description='Homelink Paystack payout worker')
    parser.add_argument('--loop', type=int, default=0,
                        help='run continuously, sleeping this many seconds between passes')
    parser.add_argument('--once', action='store_true', help='process due payouts once (default)')
    args = parser.parse_args(argv)

    if args.loop and args.loop > 0:
        print('payout worker started; interval', args.loop, 'seconds', flush=True)
        while True:
            run_once()
            time.sleep(args.loop)
    run_once()
    return 0


if __name__ == '__main__':
    sys.exit(main())
