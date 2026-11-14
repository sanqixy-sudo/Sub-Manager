"""Build pinned upstream sing-box with per-user StatsService (GPL-3.0-or-later)."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import shutil

VERSION = 'v1.14.4'
MODULE = 'github.com/sagernet/sing-box'
TAGS = 'with_v2ray_api,with_utls'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--go', default='go')
    parser.add_argument('--platforms', nargs='+', default=['linux/amd64', 'linux/arm64'])
    parser.add_argument('--output', default=str(Path(__file__).resolve().parents[1]/'agent/bin'))
    args = parser.parse_args()
    if Path(args.go).is_file():
        args.go = str(Path(args.go).resolve())
    result = subprocess.run([args.go, 'mod', 'download', '-json', MODULE+'@'+VERSION], capture_output=True)
    if result.returncode:
        raise SystemExit(result.stdout.decode(errors='replace') + result.stderr.decode(errors='replace'))
    module = json.loads(result.stdout)
    source = module['Dir']
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(Path(source)/'LICENSE', output/'sing-box-LICENSE')
    shutil.copyfile(module['Zip'], output/'sing-box-source.zip')
    for platform in args.platforms:
        system, arch = platform.split('/')
        if system not in {'windows','linux'} or arch not in {'amd64','arm64'}:
            raise ValueError('Unsupported build target')
        env = {**os.environ, 'CGO_ENABLED':'0', 'GOOS':system, 'GOARCH':arch}
        name = f'sing-box-{system}-{arch}' + ('.exe' if system == 'windows' else '')
        subprocess.run([args.go, 'build', '-mod=readonly', '-trimpath', '-tags', TAGS,
                        '-ldflags', '-s -w -buildid= -X github.com/sagernet/sing-box/constant.Version=1.14.4-submanager.1',
                        '-o', str(output/name), './cmd/sing-box'], cwd=source, env=env, check=True)
        print('Built '+name, flush=True)


if __name__ == '__main__':
    main()
