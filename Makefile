PYTHON      := python3
APP_NAME    := ThreatBrowser
RSVG        := /opt/homebrew/bin/rsvg-convert
SVG         := ios/ThreatBrowser-Icon.svg
ICONS_DIR   := src-tauri/icons
ICONSET     := /tmp/tb.iconset
DMG_OUT     := src-tauri/target/release/bundle/dmg

.PHONY: run install-deps check-deps install-tauri icons build-server dev build-dmg help

# ── Help ─────────────────────────────────────────────────────────────────────
help:
	@echo "ThreatBrowser — available targets"
	@echo ""
	@echo "  make run            Run the web server only (no desktop window)"
	@echo "  make dev            Open the desktop window in dev mode (live reload)"
	@echo "  make build-server   Bundle Python + deps into a self-contained binary"
	@echo "  make build-dmg      Build the macOS .app bundle and DMG installer"
	@echo "  make icons          Regenerate app icons from ios/ThreatBrowser-Icon.svg"
	@echo "  make install-deps   Install Python dependencies (pip)"
	@echo "  make check-deps     Verify the build interpreter has everything to bundle"
	@echo "  make install-tauri  Install the Tauri CLI via cargo (one-time)"
	@echo ""
	@echo "First-time setup:"
	@echo "  make install-deps && make install-tauri"
	@echo ""
	@echo "Build a distributable DMG (includes Installation Notes.txt):"
	@echo "  make build-dmg      (runs build-server + icons automatically)"
	@echo ""

# ── Run the server directly (no desktop window) ───────────────────────────────
run:
	$(PYTHON) app.py

# ── Install Python dependencies ───────────────────────────────────────────────
install-deps:
	$(PYTHON) -m pip install -r requirements.txt

# ── Verify the build interpreter can actually produce a working bundle ───────
# Run this on a fresh machine before build-dmg: a missing dep does not fail the
# PyInstaller build, it only breaks the installed .app at runtime.
check-deps:
	@$(PYTHON) -c "import importlib.util as u, sys; \
	mods=['bs4','certifi','curl_cffi','fastapi','feedparser','html2text','httpx','pydantic','pymisp','requests','uvicorn','PyInstaller']; \
	missing=[m for m in mods if u.find_spec(m) is None]; \
	print('interpreter:', sys.executable); \
	print('MISSING:', ', '.join(missing)) if missing else print('all bundling dependencies present'); \
	sys.exit(1 if missing else 0)"

# ── Install Tauri CLI (one-time) ──────────────────────────────────────────────
install-tauri:
	cargo install tauri-cli --version '^2' --locked

# ── Generate app icons from the SVG ──────────────────────────────────────────
icons: $(SVG)
	@mkdir -p $(ICONS_DIR)
	@echo "Generating PNG icons..."
	@$(RSVG) -w 32   -h 32   $(SVG) -o $(ICONS_DIR)/32x32.png
	@$(RSVG) -w 128  -h 128  $(SVG) -o $(ICONS_DIR)/128x128.png
	@$(RSVG) -w 256  -h 256  $(SVG) -o $(ICONS_DIR)/128x128@2x.png
	@echo "Generating ICNS..."
	@mkdir -p $(ICONSET)
	@for size in 16 32 128 256 512; do \
		$(RSVG) -w $$size -h $$size $(SVG) \
			-o $(ICONSET)/icon_$${size}x$${size}.png; \
		doubled=$$((size * 2)); \
		$(RSVG) -w $$doubled -h $$doubled $(SVG) \
			-o $(ICONSET)/icon_$${size}x$${size}@2x.png; \
	done
	@iconutil -c icns $(ICONSET) -o $(ICONS_DIR)/icon.icns
	@rm -rf $(ICONSET)
	@echo "Icons ready in $(ICONS_DIR)/"

# ── Bundle Python + all dependencies into a single executable ────────────────
# Produces: src-tauri/binaries/threatbrowser-server-{target-triple}
# This is bundled by Tauri into Contents/MacOS/ so the app works standalone.
# Everything below runs through `$(PYTHON) -m ...` on purpose. A Mac commonly
# has several python3 on PATH, and a bare `pip3` / `pyinstaller` can easily
# belong to different ones — deps land in interpreter A while PyInstaller
# freezes interpreter B, producing an .app that fails at runtime with
# ModuleNotFoundError. server.spec re-checks this and hard-fails if it happens.
build-server:
	@echo "Build interpreter: $$($(PYTHON) -c 'import sys; print(sys.executable)')"
	@echo "Installing dependencies + PyInstaller..."
	@$(PYTHON) -m pip install -r requirements.txt --quiet
	@$(PYTHON) -m pip install pyinstaller --quiet
	@echo "Stamping build id..."
	@printf '%s' "$$(git describe --always --dirty 2>/dev/null || date +%Y%m%d%H%M%S)" > _build_id
	@echo "Bundling Python server..."
	@$(PYTHON) -m PyInstaller server.spec --distpath dist --workpath /tmp/tb_pyinstaller --clean --noconfirm
	@TARGET=$$(rustc -vV | sed -n 's/^host: //p'); \
	 mkdir -p src-tauri/binaries; \
	 cp dist/threatbrowser-server src-tauri/binaries/threatbrowser-server-$$TARGET; \
	 chmod +x src-tauri/binaries/threatbrowser-server-$$TARGET; \
	 echo "Server binary ready: src-tauri/binaries/threatbrowser-server-$$TARGET"

# ── Run in dev mode (python3 fallback, no bundled binary needed) ──────────────
# Ensures a placeholder binary exists so tauri's build script doesn't complain.
dev: icons
	@TARGET=$$(rustc -vV | sed -n 's/^host: //p'); \
	 BIN="src-tauri/binaries/threatbrowser-server-$$TARGET"; \
	 if [ ! -f "$$BIN" ]; then \
	   mkdir -p src-tauri/binaries; \
	   printf '#!/bin/sh\n# placeholder\n' > "$$BIN"; chmod +x "$$BIN"; \
	   echo "Created placeholder binary (run 'make build-server' before packaging)"; \
	 fi
	cargo tauri dev

# ── Build the macOS .app bundle and DMG (with installer notes) ───────────────
# Tauri already produces a proper installer DMG: it has the "Applications" drag
# target and the styled window that opens on mount. We must NOT rebuild it from
# scratch (an earlier `cp -R` + `hdiutil create` dropped that window and mangled
# the app). Instead, add INSTALL_NOTES.txt *non-destructively*: convert Tauri's
# DMG to read-write, drop the note file in, and recompress. The app bytes,
# code signature, Applications symlink and window layout are preserved verbatim.
build-dmg: icons build-server
	cargo tauri build
	@echo "Adding installation notes to the Tauri DMG (window preserved)..."
	@TAURI_DMG=$$(ls $(DMG_OUT)/$(APP_NAME)_*.dmg 2>/dev/null | head -1); \
	 if [ -z "$$TAURI_DMG" ]; then echo "ERROR: no DMG found in $(DMG_OUT)"; exit 1; fi; \
	 FINAL=$(DMG_OUT)/$(APP_NAME)-install.dmg; \
	 RW=/tmp/tb_dmg_rw.dmg; \
	 MNT=/tmp/tb_dmg_mount; \
	 rm -f "$$RW"; rm -rf "$$MNT"; mkdir -p "$$MNT"; \
	 hdiutil convert "$$TAURI_DMG" -format UDRW -o "$$RW" -ov -quiet; \
	 hdiutil attach "$$RW" -mountpoint "$$MNT" -nobrowse -quiet; \
	 cp INSTALL_NOTES.txt "$$MNT/Installation Notes.txt" || { echo "ERROR: DMG has no room for notes"; hdiutil detach "$$MNT" -quiet; exit 1; }; \
	 hdiutil detach "$$MNT" -quiet; \
	 rm -f "$$FINAL"; \
	 hdiutil convert "$$RW" -format UDZO -o "$$FINAL" -ov -quiet; \
	 rm -f "$$RW"; \
	 echo ""; \
	 echo "DMG ready (drag-to-Applications window intact): $$FINAL"
