#![allow(unexpected_cfgs)]

use std::path::PathBuf;
use std::process::{Child, Command};
use std::sync::Mutex;
use std::thread;
use std::time::Duration;
use tauri::Manager;

// ── State ─────────────────────────────────────────────────────────────────────

struct Server(Mutex<Option<Child>>);

// ── Paths ─────────────────────────────────────────────────────────────────────

/// Python source root: Tauri resources dir in production, project root in dev.
fn find_resources(handle: &tauri::AppHandle) -> PathBuf {
    if let Ok(r) = handle.path().resource_dir() {
        if r.join("app.py").exists() {
            return r;
        }
    }
    if let Ok(cwd) = std::env::current_dir() {
        if cwd.join("app.py").exists() {
            return cwd.clone();
        }
        if let Some(parent) = cwd.parent() {
            if parent.join("app.py").exists() {
                return parent.to_path_buf();
            }
        }
    }
    PathBuf::from(".")
}

/// User-writable data dir: project root in dev, Application Support in production.
fn data_dir(is_dev: bool, resources: &PathBuf) -> PathBuf {
    if is_dev {
        return resources.clone();
    }
    let home = std::env::var("HOME").unwrap_or_else(|_| ".".to_string());
    let d = PathBuf::from(home).join("Library/Application Support/ThreatBrowser");
    std::fs::create_dir_all(&d).ok();
    d
}

fn find_python() -> String {
    for p in ["/opt/homebrew/bin/python3", "/usr/local/bin/python3", "/usr/bin/python3"] {
        if std::path::Path::new(p).exists() {
            return p.to_string();
        }
    }
    "python3".to_string()
}

// ── Server management ─────────────────────────────────────────────────────────

fn port_in_use(port: u16) -> bool {
    std::net::TcpStream::connect(("127.0.0.1", port)).is_ok()
}

/// A healthy ThreatBrowser server answers GET / with "200 OK".
/// Distinguishes our live server from a stale/broken process squatting the port.
fn server_healthy(port: u16) -> bool {
    use std::io::{Read, Write};
    let mut s = match std::net::TcpStream::connect(("127.0.0.1", port)) {
        Ok(s) => s,
        Err(_) => return false,
    };
    s.set_read_timeout(Some(Duration::from_secs(3))).ok();
    if s.write_all(b"GET / HTTP/1.0\r\nHost: localhost\r\n\r\n").is_err() {
        return false;
    }
    let mut buf = [0u8; 64];
    match s.read(&mut buf) {
        Ok(n) => buf[..n].windows(7).any(|w| w == b"200 OK\r" || w == b"200 OK\n")
            || std::str::from_utf8(&buf[..n]).map(|t| t.contains("200")).unwrap_or(false),
        Err(_) => false,
    }
}

/// Kill any process bound to the port (orphaned server from a previous crash/force-quit).
#[cfg(target_os = "macos")]
fn kill_orphan_on_port(port: u16) {
    if let Ok(out) = Command::new("/usr/sbin/lsof")
        .args(["-ti", &format!("tcp:{port}")])
        .output()
    {
        for pid in String::from_utf8_lossy(&out.stdout).split_whitespace() {
            let _ = Command::new("/bin/kill").args(["-9", pid]).status();
        }
    }
}

#[cfg(not(target_os = "macos"))]
fn kill_orphan_on_port(_: u16) {}

fn playwright_browsers_path() -> PathBuf {
    // Playwright stores browsers in ~/Library/Caches/ms-playwright by default on macOS.
    // When running from a PyInstaller bundle the driver would otherwise look inside the
    // temp extraction dir (_MEIPASS/playwright/driver/package/.local-browsers/) which
    // never contains any browser executables.
    let home = std::env::var("HOME").unwrap_or_else(|_| ".".to_string());
    PathBuf::from(home).join("Library/Caches/ms-playwright")
}

fn server_tmp_dir(data: &PathBuf) -> PathBuf {
    // PyInstaller --onefile unpacks into $TMPDIR/_MEIxxxx. macOS periodically deletes
    // files under /var/folders/.../T/ that have not been touched for a few days, which
    // silently strips a long-running server of its CA bundle, static/ assets and dylibs.
    // Unpacking under Application Support instead keeps them for the process's lifetime.
    let tmp = data.join("tmp");
    let _ = std::fs::create_dir_all(&tmp);
    tmp
}

fn spawn_server(resources: &PathBuf, data: &PathBuf) -> Option<Child> {
    if port_in_use(7474) {
        if server_healthy(7474) {
            return None; // a healthy ThreatBrowser server is already running
        }
        // Stale/broken process squatting the port — reclaim it.
        kill_orphan_on_port(7474);
        thread::sleep(Duration::from_millis(500));
    }

    let pw_path = playwright_browsers_path();
    let tmp_dir = server_tmp_dir(data);

    // In release builds, prefer the self-contained PyInstaller binary bundled
    // alongside this executable in Contents/MacOS/.
    // Guard against the placeholder script (< 1 MB) being mistaken for the real binary.
    #[cfg(not(debug_assertions))]
    {
        let bin = std::env::current_exe().ok()
            .and_then(|e| e.parent().map(|d| d.join("threatbrowser-server")));
        let real_bin = bin.filter(|p| {
            p.metadata().map(|m| m.len() > 1_000_000).unwrap_or(false)
        });
        if let Some(b) = real_bin {
            return Command::new(&b)
                .env("TMPDIR",                   &tmp_dir)
                .env("TB_DB",                    data.join("threatbrowser.db"))
                .env("TB_CACHE",                 data.join("cache"))
                .env("TB_CONTENT",               data.join("content"))
                .env("PLAYWRIGHT_BROWSERS_PATH", &pw_path)
                .spawn()
                .ok();
        }
    }

    // Development fallback: launch the bare Python source tree.
    Command::new(find_python())
        .arg(resources.join("app.py"))
        .env("TMPDIR",                   &tmp_dir)
        .env("TB_DB",                    data.join("threatbrowser.db"))
        .env("TB_CACHE",                 data.join("cache"))
        .env("TB_CONTENT",               data.join("content"))
        .env("TB_STATIC",                resources.join("static"))
        .env("PLAYWRIGHT_BROWSERS_PATH", &pw_path)
        .spawn()
        .ok()
}

fn wait_for_server(port: u16) {
    for _ in 0..60 {
        if std::net::TcpStream::connect(("127.0.0.1", port)).is_ok() {
            return;
        }
        thread::sleep(Duration::from_millis(500));
    }
}

// ── Dock badge (macOS) ────────────────────────────────────────────────────────

#[cfg(target_os = "macos")]
fn set_dock_badge(count: u64) {
    let label = if count > 0 { count.to_string() } else { String::new() };
    unsafe {
        use std::ffi::CString;
        use objc::runtime::Class;
        use objc::{msg_send, sel, sel_impl};
        let c = CString::new(label).unwrap();
        let app: *mut objc::runtime::Object =
            msg_send![Class::get("NSApplication").unwrap(), sharedApplication];
        let tile: *mut objc::runtime::Object = msg_send![app, dockTile];
        let s: *mut objc::runtime::Object = msg_send![
            Class::get("NSString").unwrap(),
            stringWithUTF8String: c.as_ptr()
        ];
        let _: () = msg_send![tile, setBadgeLabel: s];
    }
}

#[cfg(not(target_os = "macos"))]
fn set_dock_badge(_: u64) {}

fn new_article_count(db: &PathBuf) -> u64 {
    rusqlite::Connection::open(db)
        .and_then(|c| c.query_row(
            "SELECT COUNT(*) FROM articles WHERE status='new'",
            [],
            |r| r.get::<_, u64>(0),
        ))
        .unwrap_or(0)
}

// ── Entry point ───────────────────────────────────────────────────────────────

pub fn run() {
    tauri::Builder::default()
        .manage(Server(Mutex::new(None)))
        .setup(|app| {
            let handle    = app.handle().clone();
            let is_dev    = cfg!(debug_assertions);
            let resources = find_resources(&handle);
            let data      = data_dir(is_dev, &resources);
            let db_path   = data.join("threatbrowser.db");

            let child = spawn_server(&resources, &data);
            *app.state::<Server>().0.lock().unwrap() = child;

            wait_for_server(7474);

            thread::spawn(move || loop {
                set_dock_badge(new_article_count(&db_path));
                thread::sleep(Duration::from_secs(30));
            });

            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("failed to build ThreatBrowser")
        .run(|handle, event| {
            if let tauri::RunEvent::Exit = event {
                if let Ok(mut g) = handle.state::<Server>().0.lock() {
                    if let Some(c) = g.as_mut() {
                        let _ = c.kill();
                    }
                }
            }
        });
}
