# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for macOS (.app bundle) — World Cup Pilot New

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
    hiddenimports=['server'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True, name='World Cup Pilot New',
    debug=False, bootloader_ignore_signals=False, strip=False, upx=True,
    console=False, disable_windowed_traceback=False, argv_emulation=False,
    target_arch=None, codesign_identity=None, entitlements_file=None,
)

coll = COLLECT(exe, a.binaries, a.zipfiles, a.datas, strip=False, upx=True,
               upx_exclude=[], name='World Cup Pilot New')

app = BUNDLE(
    coll,
    name='World Cup Pilot New.app',
    icon='icon.icns' if os.path.exists('icon.icns') else None,
    bundle_identifier='com.worldcup.pilot.new',
    info_plist={
        'NSHighResolutionCapable': True,
        'LSMinimumSystemVersion': '11.0',
        'NSHumanReadableCopyright': 'World Cup Pilot — personal use',
    },
)
