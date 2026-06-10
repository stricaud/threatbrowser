PYTHON      := python3
APP_NAME    := ThreatBrowser
RSVG        := /opt/homebrew/bin/rsvg-convert
SVG         := ios/ThreatBrowser-Icon.svg
ICONS_DIR   := src-tauri/icons
ICONSET     := /tmp/tb.iconset
DMG_OUT     := src-tauri/target/release/bundle/dmg

.PHONY: run install-deps install-tauri icons build-server dev build-dmg help

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
	@echo "  make install-tauri  Install the Tauri CLI via cargo (one-time)"
	@echo ""
	@echo "First-time setup:"
	@echo "  make install-deps && make install-tauri"
	@echo ""
	@echo "Build a distributable DMG:"
	@echo "  make build-dmg      (runs build-server + icons automatically)"
	@echo ""

# ── Run the server directly (no desktop window) ───────────────────────────────
run:
	$(PYTHON) app.py

# ── Install Python dependencies ───────────────────────────────────────────────
install-deps:
	pip3 install -r requirements.txt

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
build-server:
	@echo "Installing PyInstaller..."
	@pip3 install pyinstaller --quiet
	@echo "Bundling Python server..."
	@pyinstaller server.spec --distpath dist --workpath /tmp/tb_pyinstaller --clean --noconfirm
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

# ── Build the macOS .app bundle and DMG ──────────────────────────────────────
build-dmg: icons build-server
	cargo tauri build
	@echo ""
	@echo "DMG is at: $(DMG_OUT)/$(APP_NAME)_*.dmg"
