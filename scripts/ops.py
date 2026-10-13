"""Private maintenance CLI; run on the gateway host, never expose it as an API."""

import argparse
import datetime
import os
import sqlite3
import sys
from pathlib import Path


def db_path():
    return Path(os.getenv('DB_PATH', '/data/alerts.sqlite3'))


def connect():
    path = db_path()
    if not path.is_file():
        raise RuntimeError(f'Database does not exist: {path}')
    c = sqlite3.connect(str(path), timeout=10)
    c.row_factory = sqlite3.Row
    return c


def backup(destination):
    target = Path(destination)
    if not target.parent.is_dir():
        raise RuntimeError('Backup directory does not exist')
    if target.exists():
        raise RuntimeError('Refusing to overwrite an existing backup')
    old_umask = os.umask(0o077)
    try:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
    finally:
        os.umask(old_umask)
    try:
        with connect() as source, sqlite3.connect(str(target)) as dest:
            source.backup(dest)
            if dest.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise RuntimeError('Backup integrity verification failed')
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    print(f'Backup created successfully: {target} (mode 0600)')


def stats():
    with connect() as c:
        entries = c.execute('SELECT status,COUNT(*) AS n FROM alerts GROUP BY status ORDER BY status').fetchall()
        attempts = c.execute('SELECT COUNT(*) AS n FROM delivery_attempts').fetchone()['n']
    print('Alert counts (no message text):')
    for row in entries:
        print(f"  {row['status']}: {row['n']}")
    print(f'  delivery attempts in database: {attempts}')


def acknowledge(alert_id, reason):
    if len(reason.strip()) < 8 or len(reason) > 200:
        raise RuntimeError('Reason must contain between 8 and 200 characters')
    reason = reason.strip().replace('\n', ' ')
    at = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')
    with connect() as c:
        c.execute('BEGIN IMMEDIATE')
        row = c.execute('SELECT status FROM alerts WHERE id=?', (alert_id,)).fetchone()
        if not row or row['status'] not in ('uncertain', 'failed', 'expired'):
            raise RuntimeError('Alert missing or not an unresolved terminal alert')
        c.execute("UPDATE alerts SET status='acknowledged',last_error=? WHERE id=?",
                  (f'Operator acknowledgement at {at}: {reason}', alert_id))
    print(f'Alert {alert_id} acknowledged (NOT resent; delivery not guaranteed).')


def main(argv=None):
    parser = argparse.ArgumentParser(description='Private Free SMS Gateway maintenance')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('stats', help='Print counts by alert status, no SMS body')
    back = sub.add_parser('backup', help='Consistent SQLite online backup (WAL aware)')
    back.add_argument('destination', help='Existing directory, new .sqlite3 file')
    ack = sub.add_parser('acknowledge', help='Manually resolve a verified uncertain/failed/expired alert')
    ack.add_argument('alert_id', type=int)
    ack.add_argument('--reason', required=True, help='Reason for operator acknowledgement')
    args = parser.parse_args(argv)
    try:
        if args.command == 'stats':
            stats()
        elif args.command == 'backup':
            backup(args.destination)
        else:
            acknowledge(args.alert_id, args.reason)
    except (OSError, sqlite3.Error, RuntimeError) as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
