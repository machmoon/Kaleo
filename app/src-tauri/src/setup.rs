//! The first-launch gate.
//!
//! On a fresh install the strip stays hidden and the dashboard opens at
//! `/welcome` as a Regular (Dock-bearing, focusable) app until the Setup
//! Assistant finishes or is skipped; every launch after that comes up exactly
//! as before -- strip visible, Accessory policy, dashboard at `/workbench`.
//! The one fact that decides which launch this is lives in `settings.json`
//! (`tauri-plugin-store`, in `app_data_dir`) under `setup.completed`, and it
//! is read here, in Rust, before any webview has booted -- the frontend cannot
//! cover that window, which is the same reason `lib.rs` sets the activation
//! policy in `setup()` rather than leaving it to the `set_app_icon_visibility`
//! command.
//!
//! Two rules keep the gate from stranding the user. The store is built here
//! with autosave *before* the JS side ever calls `load("settings.json")`: the
//! plugin caches one `Store` per path and ignores the options of every later
//! load, so if JS won the race its writes would sit in memory and never reach
//! disk. And closing the dashboard mid-setup finishes the setup rather than
//! hiding the only window: an Accessory app whose strip is hidden and whose
//! dashboard is closed has nothing left to click on.
//!
//! The JS side (`src/lib/settings/store.ts`) writes the same file and the same
//! keys; the vocabulary is frozen in the plan (contract C1/C3). Rust reads
//! only `setup.completed` to decide the gate. `setup.version` is a hint the JS
//! side uses to offer "Run Setup Again" and is written, never read, here.

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use serde::Serialize;
use serde_json::{json, Value};
use tauri::{AppHandle, Emitter, LogicalSize, Manager, Runtime, Size, WebviewWindow};
use tauri_plugin_store::{Store, StoreExt};

#[cfg(target_os = "macos")]
use tauri_nspanel::ManagerExt;

/// The settings file, relative to `app_data_dir`
/// (`~/Library/Application Support/com.silkscreen.kaleo/settings.json` on
/// macOS). `hardening.test.ts` pins that the JS side never loads any other
/// path.
pub const STORE_FILE: &str = "settings.json";

/// Written into `setup.version` by `setup_finish`. Bump when a new wizard
/// step should be offered to existing users; the JS side compares against
/// its own `SETUP_VERSION` to decide whether to suggest "Run Setup Again".
/// The Rust gate deliberately does not read it.
pub const SETUP_VERSION: u64 = 1;

/// Debounce for the store's autosave. Every `set` from either side schedules
/// a write this long after the last one, so a burst of wizard writes is one
/// disk write.
const AUTO_SAVE: Duration = Duration::from_millis(100);

/// Emitted to every window when the setup state changes (contract C3).
pub const CHANGED_EVENT: &str = "kaleo-setup-changed";

/// The wizard route the dashboard opens at while gating.
pub const WELCOME_URL: &str = "/welcome";
/// The dashboard's ordinary route.
pub const WORKBENCH_URL: &str = "/workbench";

const KEY_COMPLETED: &str = "setup.completed";
const KEY_VERSION: &str = "setup.version";
const KEY_REMAINING: &str = "setup.remaining";
const KEY_SKIPPED: &str = "setup.skipped";
const KEY_COMPLETED_AT: &str = "setup.completedAt";

/// Process-wide setup state, managed in `lib.rs`.
pub struct SetupState {
    /// True from launch until `reveal()` on a gated launch, and again from
    /// `setup_restart` until the next `setup_finish`.
    pub in_setup: AtomicBool,
    /// What the frontend last asked `set_app_icon_visibility` for while
    /// `in_setup` was true. The wizard runs as a Regular app regardless, so
    /// the request is recorded here and applied once by `reveal()`.
    pub requested_icon_visible: AtomicBool,
    /// Whether `requested_icon_visible` was ever written. With nothing
    /// recorded, `reveal()` leaves the policy alone rather than guessing.
    pub icon_requested: AtomicBool,
    /// Serialises `setup_finish`. Both webviews run `initSettings()` and an
    /// existing-user install has each of them call `setup_finish` at boot,
    /// possibly at the same moment; the second must observe the first's
    /// `setup.completed` rather than race it to a double write and a double
    /// event.
    pub finish_lock: Mutex<()>,
}

impl Default for SetupState {
    fn default() -> Self {
        SetupState {
            in_setup: AtomicBool::new(false),
            // The frontend's default (`DEFAULT_CUSTOMIZABLE_STATE.appIcon`)
            // is visible; matched here so an unrecorded reveal and a
            // recorded-default reveal agree.
            requested_icon_visible: AtomicBool::new(true),
            icon_requested: AtomicBool::new(false),
            finish_lock: Mutex::new(()),
        }
    }
}

impl SetupState {
    pub fn in_setup(&self) -> bool {
        self.in_setup.load(Ordering::SeqCst)
    }

    /// Record a Dock-icon request made during setup; applied by `reveal()`.
    pub fn record_icon_request(&self, visible: bool) {
        self.requested_icon_visible.store(visible, Ordering::SeqCst);
        self.icon_requested.store(true, Ordering::SeqCst);
    }
}

/// The one handle to `settings.json`. Builds the store with autosave on
/// first use and returns the cached instance afterwards: the plugin keeps one
/// `Store` per path, and `StoreBuilder::build` refuses a path that is already
/// loaded, so the lookup comes first.
pub fn store<R: Runtime>(app: &AppHandle<R>) -> Result<Arc<Store<R>>, String> {
    if let Some(existing) = app.get_store(STORE_FILE) {
        return Ok(existing);
    }
    app.store_builder(STORE_FILE)
        .auto_save(AUTO_SAVE)
        .build()
        .map_err(|e| format!("Failed to open {}: {}", STORE_FILE, e))
}

/// `setup.completed != true`, and nothing else: a missing key, a wrong
/// type, and `false` all gate.
///
/// A store that cannot be opened at all does **not** gate. The wizard could
/// still run, but `setup_finish` could never persist and every later launch
/// would gate again -- an app that keeps hiding its strip because a file is
/// unwritable is worse than one that launches as it always did and logs why.
pub fn needs_setup<R: Runtime>(app: &AppHandle<R>) -> bool {
    match store(app) {
        Ok(store) => store.get(KEY_COMPLETED).and_then(|v| v.as_bool()) != Some(true),
        Err(e) => {
            eprintln!("Setup gate skipped, {}", e);
            false
        }
    }
}

/// Decide the launch policy before the event loop starts and remember the
/// decision in `SetupState`. Returns whether this launch is gated.
///
/// `App::set_activation_policy` (not the `AppHandle` one the command uses)
/// sets the policy on the runtime before the loop runs, which is exactly the
/// window the frontend cannot cover -- the reason the fixed Accessory line
/// this replaced lived here too. A gated launch is Regular for the whole
/// wizard: it has a Dock icon and can take focus, so the Setup Assistant
/// window behaves like one.
pub fn apply_launch_policy(app: &mut tauri::App) -> bool {
    let gating = needs_setup(app.handle());
    app.state::<SetupState>()
        .in_setup
        .store(gating, Ordering::SeqCst);
    #[cfg(target_os = "macos")]
    app.set_activation_policy(if gating {
        tauri::ActivationPolicy::Regular
    } else {
        tauri::ActivationPolicy::Accessory
    });
    gating
}

/// Whether the strip is currently hidden, as Tauri sees it. An unreadable
/// state reads as hidden so the caller errs towards showing it.
pub fn strip_hidden<R: Runtime>(app: &AppHandle<R>) -> bool {
    app.get_webview_window("main")
        .and_then(|w| w.is_visible().ok())
        .map(|visible| !visible)
        .unwrap_or(true)
}

/// Show the strip the way the toggle shortcut does: `WebviewWindow::show`
/// for Tauri's own visibility state, then the panel ordered front
/// *regardless* of activation. Never `RawNSPanel::show`, which also makes the
/// panel key -- fine for a hotkey the user just pressed, wrong for a reveal
/// that happens while they are reading the dashboard. Safe to call on a
/// strip that is already showing: both native calls are idempotent.
pub fn show_strip<R: Runtime>(app: &AppHandle<R>) -> Result<(), String> {
    let window = app
        .get_webview_window("main")
        .ok_or_else(|| "Main window not found".to_string())?;
    window
        .show()
        .map_err(|e| format!("Failed to show main window: {}", e))?;
    #[cfg(target_os = "macos")]
    {
        match app.get_webview_panel("main") {
            Ok(panel) => panel.order_front_regardless(),
            Err(e) => eprintln!("Main panel not found, strip shown as a plain window: {:?}", e),
        }
    }
    if let Ok(mut is_hidden) = app.state::<crate::shortcuts::WindowVisibility>().is_hidden.lock() {
        *is_hidden = false;
    }
    Ok(())
}

/// Leave setup: apply the Dock choice recorded during the wizard (once, via
/// `set_dock_visibility`, which unlike a policy change does not drop the key
/// window), show the strip, and tell every webview. Safe to call twice: the
/// second call finds `in_setup` already false, no icon request left to apply
/// (`swap` cleared it), and a strip that `show_strip` leaves where it is.
pub fn reveal<R: Runtime>(app: &AppHandle<R>, payload: Value) -> Result<(), String> {
    let state = app.state::<SetupState>();
    state.in_setup.store(false, Ordering::SeqCst);

    if state.icon_requested.swap(false, Ordering::SeqCst) {
        let visible = state.requested_icon_visible.load(Ordering::SeqCst);
        #[cfg(target_os = "macos")]
        {
            if let Err(e) = app.set_dock_visibility(visible) {
                eprintln!("Failed to apply the recorded Dock visibility: {}", e);
            }
        }
        #[cfg(not(target_os = "macos"))]
        {
            if let Err(e) = crate::shortcuts::apply_app_icon_visibility(app, visible) {
                eprintln!("Failed to apply the recorded icon visibility: {}", e);
            }
        }
    }

    show_strip(app)?;

    app.emit(CHANGED_EVENT, payload)
        .map_err(|e| format!("Failed to emit {}: {}", CHANGED_EVENT, e))
}

/// Write the completion record and flush it. `skipped` is the list of card
/// ids the user set up later (`google|microsoft|stripe|notifications|voice`),
/// stored verbatim for the Done screen and Settings to read back.
fn persist_completion<R: Runtime>(app: &AppHandle<R>, skipped: &[String]) -> Result<(), String> {
    let store = store(app)?;
    store.set(KEY_COMPLETED, json!(true));
    store.set(KEY_VERSION, json!(SETUP_VERSION));
    store.set(KEY_SKIPPED, json!(skipped));
    store.set(KEY_COMPLETED_AT, json!(now_millis()));
    // Autosave would get there within `AUTO_SAVE`; the explicit save is so
    // the record is on disk before the strip appears, and before a quit that
    // follows the last wizard click by less than the debounce.
    store
        .save()
        .map_err(|e| format!("Failed to save {}: {}", STORE_FILE, e))
}

fn now_millis() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0)
}

/// The dashboard closed while the wizard was still open. Everything the user
/// had not reached (`setup.remaining`, kept current by the JS side after every
/// step) becomes `setup.skipped`, the run counts as finished, and the strip is
/// revealed -- only then does the caller hide the window. Called from the
/// close handler in `window.rs`.
pub fn finish_from_close<R: Runtime>(app: &AppHandle<R>) -> Result<(), String> {
    let remaining: Vec<String> = store(app)?
        .get(KEY_REMAINING)
        .and_then(|v| v.as_array().cloned())
        .unwrap_or_default()
        .into_iter()
        .filter_map(|v| v.as_str().map(str::to_string))
        .collect();
    persist_completion(app, &remaining)?;
    reveal(
        app,
        json!({ "completed": true, "skipped": remaining, "reason": "closed" }),
    )
}

#[derive(Debug, Clone, Serialize)]
pub struct SetupStatus {
    pub completed: bool,
    pub in_setup: bool,
}

/// `{completed, in_setup}` -- `completed` is read from disk every call so a
/// file deleted while the app runs is reported honestly.
#[tauri::command]
pub fn setup_status<R: Runtime>(app: AppHandle<R>) -> Result<SetupStatus, String> {
    let in_setup = app.state::<SetupState>().in_setup();
    Ok(SetupStatus {
        completed: !needs_setup(&app),
        in_setup,
    })
}

/// Persist `setup.*`, save, reveal the strip, apply the recorded activation
/// choice and emit `kaleo-setup-changed {completed: true, skipped}`.
///
/// Idempotent. Both webviews call this at boot on an existing-user install
/// (`initSettings()` in each, concurrently), so when `setup.completed` is
/// already true and no wizard is open, nothing is rewritten -- not
/// `completedAt`, not `skipped` -- no event is emitted, and the only work is
/// showing the strip if it happens to be hidden. A finish that arrives while
/// `in_setup` is true (Run Setup Again) is a real completion and is recorded
/// again. The lock makes the second of two simultaneous boot calls see the
/// first's write instead of racing it.
#[tauri::command]
pub fn setup_finish<R: Runtime>(app: AppHandle<R>, skipped: Vec<String>) -> Result<(), String> {
    let state = app.state::<SetupState>();
    let _guard = state
        .finish_lock
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner());
    let already_completed = !needs_setup(&app);
    if already_completed && !state.in_setup() {
        if strip_hidden(&app) {
            show_strip(&app)?;
        }
        return Ok(());
    }
    persist_completion(&app, &skipped)?;
    reveal(&app, json!({ "completed": true, "skipped": skipped }))
}

/// Re-enter setup from Settings: `in_setup` on, Regular policy, dashboard
/// shown and focused. The client navigates to `/welcome` itself and the strip
/// stays where it is; nothing on disk changes until `setup_finish`.
#[tauri::command]
pub fn setup_restart<R: Runtime>(app: AppHandle<R>) -> Result<(), String> {
    app.state::<SetupState>()
        .in_setup
        .store(true, Ordering::SeqCst);
    #[cfg(target_os = "macos")]
    {
        app.set_activation_policy(tauri::ActivationPolicy::Regular)
            .map_err(|e| format!("Failed to set activation policy: {}", e))?;
    }
    crate::window::show_dashboard_window(&app)
}

/// How the dashboard window is sized: the Setup Assistant sheet, or the
/// ordinary workbench.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum WindowMode {
    Welcome,
    Normal,
}

impl WindowMode {
    fn parse(mode: &str) -> Result<Self, String> {
        match mode {
            "welcome" => Ok(WindowMode::Welcome),
            "normal" => Ok(WindowMode::Normal),
            other => Err(format!(
                "Unknown window mode '{}': expected 'welcome' or 'normal'",
                other
            )),
        }
    }
}

/// The dashboard's geometry per mode. `/welcome` is an 820 x 620 fixed,
/// centred sheet -- the macOS Setup Assistant shape; `/workbench` is the
/// 1200 x 800 resizable window `create_dashboard_window` builds. The 800 x 600
/// minimum from the builder stays in force in both, and both sizes clear it.
pub fn apply_window_mode<R: Runtime>(window: &WebviewWindow<R>, mode: WindowMode) -> Result<(), String> {
    let (width, height, resizable) = match mode {
        WindowMode::Welcome => (820.0, 620.0, false),
        WindowMode::Normal => (1200.0, 800.0, true),
    };
    // Resizable first when growing back, so the size change is not refused
    // by a fixed-size window; the order does not matter when shrinking.
    if resizable {
        window
            .set_resizable(true)
            .map_err(|e| format!("Failed to make the dashboard resizable: {}", e))?;
    }
    window
        .set_size(Size::Logical(LogicalSize::new(width, height)))
        .map_err(|e| format!("Failed to resize the dashboard: {}", e))?;
    if !resizable {
        window
            .set_resizable(false)
            .map_err(|e| format!("Failed to fix the dashboard size: {}", e))?;
    }
    window
        .center()
        .map_err(|e| format!("Failed to centre the dashboard: {}", e))
}

/// Called by the frontend on entering and leaving `/welcome`.
#[tauri::command]
pub fn setup_window_mode<R: Runtime>(app: AppHandle<R>, mode: String) -> Result<(), String> {
    let mode = WindowMode::parse(&mode)?;
    let window = app
        .get_webview_window("dashboard")
        .ok_or_else(|| "Dashboard window not found".to_string())?;
    apply_window_mode(&window, mode)
}

/// Whether Kaleo is frontmost: either window focused. The strip is a
/// non-activating panel, so `document.hasFocus()` inside it never means the
/// app is in front; the notification gate asks this instead.
#[tauri::command]
pub fn kaleo_has_focus<R: Runtime>(app: AppHandle<R>) -> bool {
    ["dashboard", "main"].iter().any(|label| {
        app.get_webview_window(label)
            .and_then(|w| w.is_focused().ok())
            .unwrap_or(false)
    })
}
