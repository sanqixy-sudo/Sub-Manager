"""Offline full /data backup. Stop the old container before invoking this CLI."""
from __future__ import annotations

import argparse
import shutil
import sqlite3
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
        with sqlite3.connect(f'file:{(source / "submanager.db").as_posix()}?mode=ro', uri=True) as current:
            with sqlite3.connect(destination / 'submanager.db') as copied:
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
