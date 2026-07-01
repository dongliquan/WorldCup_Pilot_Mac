# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for Windows (single-file .exe) — World Cup Pilot
# Build:  pyinstaller --noconfirm worldcup_win.spec
# Output: dist\WorldCupPilot.exe

import os

block_cipher = None

datas = [
    ('worldcup.html', '.'),
    ('assets', 'assets'),
]

a = Analysis(
    ['worldcup.py'],
    pathex=['.'],
    binaries=[],
    datas=datas,
    # server is imported dynamically; pull in the pywebview Windows (EdgeChromium) backend
    hiddenimports=['server', 'webview.platforms.edgechromium', 'clr'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['AppKit', 'Cocoa', 'WebKit', 'objc', 'Foundation'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='WorldCupPilot',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='icon.ico' if os.path.exists('icon.ico') else None,  # 아이콘 파일이 있을 때만 설정
)
