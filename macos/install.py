#!/usr/bin/env python3
"""Install this user's app/worker; keep configuration and remote data intact."""
import argparse
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import socket
import subprocess
import sys
import tempfile


def run(args, **kwargs):
    return subprocess.run([str(x) for x in args], check=True, **kwargs)


def atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--wheel', type=Path, required=True)
    parser.add_argument('--app-bundle', type=Path, required=True)
    parser.add_argument('--env-file', type=Path, required=True)
    parser.add_argument('--wheelhouse', type=Path)
    parser.add_argument('--source', type=Path, default=Path.home()/'Desktop')
    parser.add_argument('--remote-root')
    parser.add_argument('--no-start', action='store_true')
    args = parser.parse_args()
    if sys.platform != 'darwin':
        raise SystemExit('The menu bar application requires macOS')
    for path in (args.wheel, args.app_bundle, args.env_file, args.source):
        if not path.exists():
            raise SystemExit(f'Required input does not exist: {path}')
    support = Path.home()/'Library/Application Support/YandexBackup'
    app = Path.home()/'Applications/Яндекс Бэкап.app'
    agents = Path.home()/'Library/LaunchAgents'
    support.mkdir(parents=True, exist_ok=True, mode=0o700)
    domain = f'gui/{os.getuid()}'
    labels = ['local.egor.yandexbackup.worker', 'local.egor.yandexbackup.menu']
    # Re-running the installer only restarts these two generated jobs.
    for label in labels:
        subprocess.run(['launchctl', 'bootout', f'{domain}/{label}'], capture_output=True)
    venv = support/'venv'
    if not (venv/'bin/python').exists():
        run([sys.executable, '-m', 'venv', str(venv)])
    command = [venv/'bin/python', '-m', 'pip', 'install', '--upgrade', '--disable-pip-version-check']
    if args.wheelhouse:
        command += ['--no-index', '--find-links', args.wheelhouse.resolve()]
    command += [args.wheel.resolve()]
    run(command)
    bundle_info = plistlib.loads((args.app_bundle/'Contents/Info.plist').read_bytes())
    if bundle_info.get('CFBundleIdentifier') != 'local.egor.yandexbackup':
        raise SystemExit('Unexpected source application bundle')
    app.parent.mkdir(parents=True, exist_ok=True)
    if app.exists():
        existing = plistlib.loads((app/'Contents/Info.plist').read_bytes())
        if existing.get('CFBundleIdentifier') != 'local.egor.yandexbackup':
            raise SystemExit('A different application already occupies the destination')
        shutil.rmtree(app)
    shutil.copytree(args.app_bundle, app)
    config_path = support/'config.json'
    if not config_path.exists():
        # Read defaults from the newly installed package, not a duplicate list.
        process = run([venv/'bin/python', '-c',
                       'import json; from yadisk_client.service import DEFAULT_EXCLUDES; print(json.dumps(DEFAULT_EXCLUDES))'],
                      capture_output=True, text=True)
        machine = re.sub(r'[^A-Za-z0-9_-]+', '-', socket.gethostname().removesuffix('.local')).strip('-') or 'Mac'
        config = {'source': str(args.source.resolve()),
                  'remote_root': args.remote_root or f'/Backups/{machine}/Desktop',
                  'env_file': str(args.env_file.resolve()), 'interval_seconds': 900,
                  'workers': 4, 'network_retry_seconds': 60, 'timeout': 120,
                  'exclude': json.loads(process.stdout)}
        atomic(config_path, (json.dumps(config, ensure_ascii=False, indent=2)+'\n').encode())
    agent_definitions = [
        {'Label': labels[0], 'ProgramArguments': [str(venv/'bin/python'), '-m', 'yadisk_client.service',
             '--config-dir', str(support)], 'RunAtLoad': True, 'KeepAlive': True,
         'ThrottleInterval': 30, 'ProcessType': 'Background', 'ExitTimeOut': 180,
         'WorkingDirectory': str(support), 'Umask': 0o077,
         'StandardOutPath': str(support/'worker.stdout.log'),
         'StandardErrorPath': str(support/'worker.stderr.log')},
        {'Label': labels[1], 'ProgramArguments': [str(app/'Contents/MacOS'/bundle_info['CFBundleExecutable']),
             '--config-dir', str(support)], 'RunAtLoad': True, 'ProcessType': 'Interactive',
         'LimitLoadToSessionType': 'Aqua', 'Umask': 0o077,
         'StandardOutPath': str(support/'menu.stdout.log'),
         'StandardErrorPath': str(support/'menu.stderr.log')},
    ]
    for job in agent_definitions:
        atomic(agents/(job['Label']+'.plist'), plistlib.dumps(job))
        if not args.no_start:
            run(['launchctl', 'bootstrap', domain, agents/(job['Label']+'.plist')])
    print(json.dumps({'app': str(app), 'config': str(config_path),
                      'started': not args.no_start, 'worker': str(venv/'bin/python')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
