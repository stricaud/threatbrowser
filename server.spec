from PyInstaller.utils.hooks import collect_submodules

block_cipher = None

# Collect all sub-packages for the async frameworks so nothing is missed
hidden = []
for pkg in ['uvicorn', 'anyio', 'starlette', 'fastapi']:
    hidden += collect_submodules(pkg)

hidden += [
    'feedparser',
    'bs4', 'bs4.builder', 'bs4.formatter',
    'html2text',
    'h2', 'hpack', 'hyperframe',
    'sniffio', 'h11',
    'multipart', 'python_multipart',
    'httpx',
    'curl_cffi', 'curl_cffi.requests',
    'pymisp',
    'brotli',
]

a = Analysis(
    ['app.py'],
    pathex=['.'],
    binaries=[],
    datas=[('static', 'static')],
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
