from PyInstaller.utils.hooks import collect_submodules, collect_data_files

block_cipher = None

# Collect all sub-packages for the async frameworks so nothing is missed
hidden = []
for pkg in ['uvicorn', 'anyio', 'starlette', 'fastapi', 'curl_cffi']:
    hidden += collect_submodules(pkg)

hidden += [
    'feedparser',
    'bs4', 'bs4.builder', 'bs4.formatter',
    'html2text',
    'h2', 'hpack', 'hyperframe',
    'sniffio', 'h11',
    'multipart', 'python_multipart',
    'httpx',
    'pymisp',
    # Response decompression backends advertised by urllib3 in Accept-Encoding.
    # If a backend is importable but its lib is missing, decompression of a
    # server response fails with "incorrect header check" / Brotli errors.
    'brotli', 'brotlicffi',
    'zstandard', 'zstd',
    'socks',  # PySocks, occasionally pulled in by requests
]

# Data files: CA cert bundles (certifi + curl_cffi each ship their own) and
# curl_cffi's compiled libcurl/cacert. Missing these is the #1 cause of
# "Could not find a suitable TLS CA certificate bundle" in a frozen build.
extra_datas = collect_data_files('certifi')
extra_datas += collect_data_files('curl_cffi')

# Build-identity stamp written by `make build-server`; lets the running server
# report its version via /api/ping. Optional — absent on bare pyinstaller runs.
import os as _os
if _os.path.exists('_build_id'):
    extra_datas += [('_build_id', '.')]

a = Analysis(
    ['app.py'],
    pathex=['.'],
    binaries=[],
    datas=[('static', 'static')] + extra_datas,
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    cipher=block_cipher,
)

pyz = PYZ(a.pure, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    name='threatbrowser-server',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    console=True,
)
