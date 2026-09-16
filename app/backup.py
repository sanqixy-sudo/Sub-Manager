"""Offline full /data backup. Stop the old container before invoking this CLI."""
from __future__ import annotations

import argparse
from contextlib import closing
import shutil
import sqlite3
import tempfile
from pathlib import Path


def backup_data(source: Path, destination: Path) -> None:
    source, destination = source.resolve(strict=True), destination.resolve()
    if destination == source or source in destination.parents:
        raise ValueError('Backup destination must be outside the data directory')
    if destination.exists():
        raise ValueError('Backup destination already exists; refusing to overwrite')
    if any(p.is_symlink() for p in source.rglob('*')):
        raise ValueError('Data contains symlinks; review them before backup')
    shutil.copytree(source, destination, ignore=shutil.ignore_patterns('submanager.db', 'submanager.db-wal', 'submanager.db-shm'))
    if (source / 'submanager.db').exists():
        # The service is stopped. Copy DB/WAL to writable staging so SQLite can
        # rebuild its shared-memory index without touching the read-only source.
        with tempfile.TemporaryDirectory(prefix='.sqlite-snapshot-', dir=destination) as staging:
            snapshot = Path(staging) / 'submanager.db'
            shutil.copyfile(source / 'submanager.db', snapshot)
            wal = source / 'submanager.db-wal'
            if wal.exists():
                shutil.copyfile(wal, Path(staging) / wal.name)
            with closing(sqlite3.connect(snapshot)) as current:
                with closing(sqlite3.connect(destination / 'submanager.db')) as copied:
                    current.backup(copied)
                    if copied.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                        raise RuntimeError('Backup integrity check failed')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('destination', type=Path)
    args = parser.parse_args()
    backup_data(args.source, args.destination)
    print('Full data backup completed and SQLite integrity verified')
