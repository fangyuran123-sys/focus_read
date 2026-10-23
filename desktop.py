#!/usr/bin/env python3
"""Linux desktop entry; prefer Qt WebEngine, with a library-only Chrome fallback."""
import argparse
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys

BASE = Path(__file__).resolve().parent


def desktop_quote(value):
    return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"').replace('`', '\\`').replace('$', '\\$') + '"'


def install():
    home = Path(os.environ.get('XDG_DATA_HOME', Path.home() / '.local/share'))
    target = home / 'applications/kindle-desk.desktop'
    target.parent.mkdir(parents=True, exist_ok=True)
    # Desktop Entry expands % field codes even inside quoted arguments.
    command = ' '.join(desktop_quote(p).replace('%', '%%') for p in (sys.executable, BASE / 'desktop.py'))
    target.write_text('\n'.join([
        '[Desktop Entry]', 'Type=Application', 'Name=Kindle Desk',
        'Comment=Local personal library and Kindle manager',
        'Exec=' + command, 'Icon=' + str(BASE / 'web/icon.svg'),
        'Terminal=false', 'Categories=Office;Education;', 'StartupWMClass=kindle-desk', '',
    ]), encoding='utf-8')
    print('已安装应用菜单入口：' + str(target))
    return target


def launch():
    if importlib.util.find_spec('PyQt6') and importlib.util.find_spec('PyQt6.QtWebEngineWidgets'):
        subprocess.run(['bash', str(BASE / 'start.sh')], cwd=BASE, check=True)
        home = Path(os.environ.get('XDG_DATA_HOME', Path.home() / '.local/share')) / 'kindle-desk'
        home.mkdir(parents=True, exist_ok=True)
        with (home / 'desktop.log').open('a') as log:
            subprocess.Popen([sys.executable, str(BASE / 'desktop_browser.py')], cwd=BASE,
                             stdout=log, stderr=log, start_new_session=True)
        return
    print('未安装 PyQt6 WebEngine，使用仅图书馆模式。内置找书需要系统 python3-pyqt6.qtwebengine。')
    browser = next((shutil.which(name) for name in ('google-chrome', 'chromium', 'chromium-browser') if shutil.which(name)), None)
    if not browser:
        raise RuntimeError('桌面窗口需要 Chrome 或 Chromium。也可运行 ./start.sh 后用浏览器打开 http://127.0.0.1:8765。')
    subprocess.run(['bash', str(BASE / 'start.sh')], cwd=BASE, check=True)
    profile = Path(os.environ.get('XDG_DATA_HOME', Path.home() / '.local/share')) / 'kindle-desk/browser'
    profile.mkdir(parents=True, exist_ok=True)
    with (profile.parent / 'desktop.log').open('a') as log:
        subprocess.Popen([browser, '--app=http://127.0.0.1:8765', '--user-data-dir=' + str(profile),
                          '--class=kindle-desk', '--no-first-run', '--no-default-browser-check', '--window-size=1400,940'],
                         stdout=log, stderr=log, start_new_session=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--install', action='store_true', help='安装应用菜单入口，不启动窗口')
    args = parser.parse_args()
    try:
        if args.install:
            install()
        else:
            launch()
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        print('启动失败：' + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
