# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['app.py'],
    pathex=[],
    binaries=[],
    datas=[('templates', 'templates'), ('static', 'static')],
    hiddenimports=['simple_websocket', 'engineio.async_drivers.threading', 'scp', 'appdirs', 'parkterm', 'parkterm.config', 'parkterm.legacy_ssh', 'parkterm.ssh_session', 'parkterm.file_manager', 'parkterm.capture', 'parkterm.server', 'parkterm.main'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', '_tkinter', 'setuptools', 'pip', 'distutils', 'unittest', 'test', 'curses', 'ftplib', 'imaplib', 'smtplib', 'telnetlib', 'nntplib', 'xmlrpc', 'pydoc_data', 'antigravity', 'turtle'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='ParkTerm_V2.0.7',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['app.ico'],
)
