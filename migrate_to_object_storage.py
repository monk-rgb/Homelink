"""One-off helper: move existing local uploads into object storage.

Run this once after configuring S3_* variables, so photos and verification
documents created before object storage was enabled are not stranded on the
local disk (which a redeploy will wipe).

    python migrate_to_object_storage.py            # upload and delete local copies
    python migrate_to_object_storage.py --dry-run  # report only, change nothing
    python migrate_to_object_storage.py --keep     # upload but keep local copies

Safe to re-run: files already in the bucket are re-uploaded with the same key,
which is idempotent.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent

import storage  # noqa: E402


def collect_files(directory: Path) -> list[str]:
    if not directory.exists():
        return []
    return sorted(p.name for p in directory.iterdir() if p.is_file())


def migrate(directory: Path, visibility: str, dry_run: bool, keep: bool) -> int:
    files = collect_files(directory)
    if not files:
        print(f'  nothing to migrate in {directory}')
        return 0
    print(f'  {len(files)} file(s) in {directory} -> {visibility}')
    moved = 0
    for name in files:
        if dry_run:
            print(f'    would upload {name}')
            moved += 1
            continue
        try:
            key = storage.save_bytes(name, (directory / name).read_bytes(),
                                     visibility=visibility)
            if not keep:
                try:
                    (directory / name).unlink()
                except OSError:
                    pass
            print(f'    uploaded {name} -> {key}')
            moved += 1
        except Exception as exc:
            print(f'    FAILED {name}: {exc}')
    return moved


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run', action='store_true', help='report only')
    parser.add_argument('--keep', action='store_true', help='do not delete local copies')
    args = parser.parse_args()

    if not storage.s3_enabled():
        print('Object storage is not configured. Set S3_BUCKET, S3_ACCESS_KEY_ID and '
              'S3_SECRET_ACCESS_KEY (plus S3_ENDPOINT_URL for R2/Supabase) first.')
        return 1

    # Import the app to reuse its resolved directories, without starting a server.
    import app  # noqa: E402

    print(f'Object storage: {storage.describe_backend()}')
    print('Migrating existing local files:')
    total = migrate(app.UPLOADS, 'public', args.dry_run, args.keep)
    total += migrate(app.VERIFICATION_UPLOADS, 'private', args.dry_run, args.keep)
    verb = 'would be moved' if args.dry_run else 'moved'
    print(f'\nDone: {total} file(s) {verb}.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
