# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['universal_converter.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=['pylibjpeg', 'libjpeg', 'openjpeg'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='UniversalConverter',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['app_icon.icns'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='UniversalConverter',
)
app = BUNDLE(
    coll,
    name='Universal Converter.app',
    icon='app_icon.icns',
    bundle_identifier='com.bilalsana.universalconverter',
    info_plist={
        'CFBundleDisplayName': 'Universal Converter',
        'CFBundleName': 'Universal Converter',
        'CFBundleShortVersionString': '2.0',
        'CFBundleVersion': '2.0',
        'NSHighResolutionCapable': True,
    },
)
