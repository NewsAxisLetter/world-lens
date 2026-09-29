#!/usr/bin/env python3
"""Persist a failed fetch day without hiding which feeds were attempted."""
import argparse, json, os
from datetime import datetime, timezone
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fetch

def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument('--date', required=True)
    p.add_argument('--data-dir', default=fetch.DATA_DIR)
    p.add_argument('--feeds', default=fetch.FEEDS_PATH)
    args = p.parse_args(argv)
    feeds = fetch.load_feeds(args.feeds)
    stamp = fetch.utc_now_iso()
    statuses = [fetch.failed_status(f, 'fetch stage failed before feed completion') for f in feeds]
    day = fetch.build_day_summary([], statuses, stamp)
    day['date'] = args.date
    data = os.path.abspath(args.data_dir)
    days = os.path.join(data, 'days')
    months = os.path.join(data, 'months')
    os.makedirs(days, exist_ok=True)
    path = os.path.join(days, args.date + '.json')
    fetch.save_json(path, day)
    old_data, old_months = fetch.DATA_DIR, fetch.MONTHS_DIR
    try:
        fetch.DATA_DIR, fetch.MONTHS_DIR = data, months
        fetch.INDEX_PATH = os.path.join(data, 'index.json')
        fetch.update_month_shard(args.date, fetch.build_day_meta(day))
        fetch.update_index(args.date)
    finally:
        fetch.DATA_DIR, fetch.MONTHS_DIR = old_data, old_months
    print('recorded failed day: ' + path)
    return 0
if __name__ == '__main__': sys.exit(main())
