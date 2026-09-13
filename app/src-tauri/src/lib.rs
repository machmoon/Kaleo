// Learn more about Tauri commands at https://tauri.app/develop/calling-rust/
mod cli;
mod desk;
mod setup;
mod pty;
mod shortcuts;
mod tray;
mod wake;
mod window;
use std::sync::Mutex;
use tauri::{AppHandle, Manager, WebviewWindow};

#[cfg(target_os = "macos")]
#[allow(deprecated)]
use tauri_nspanel::{cocoa::appkit::NSWindowCollectionBehavior, panel_delegate, WebviewWindowExt};

#[tauri::command]
fn get_app_version() -> String {
    env!("CARGO_PKG_VERSION").to_string()
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let mut builder = tauri::Builder::default()
        .manage(wake::WakeState::default())
        .manage(shortcuts::WindowVisibility {
            is_hidden: Mutex::new(false),
        })
        .manage(shortcuts::RegisteredShortcuts::default())
        .manage(shortcuts::MoveWindowState::default())
        .manage(pty::PtyState::new())
        .manage(setup::SetupState::default())
        .plugin(tauri_plugin_opener::init())
        // Both must be registered before `.setup`: `setup::apply_launch_policy`
        // opens settings.json through the store plugin, and the wizard's
        // notification step needs the notification plugin's commands.
        //
        // The notification plugin is also what makes banners work at all: the
        // JS half was already in package.json, but `sendNotification` is
        // `new window.Notification(...)` over a `window.Notification` this
        // plugin's Rust half replaces -- and macOS WKWebView does not implement
        // Web Notifications, so without this every banner was silently dropped
        // while the settings pane reported success. The store plugin likewise:
        // `src/lib/settings/store.ts` dynamically imports it, so without the
        // Rust half every setting resolved and then wrote nowhere.
        .plugin(tauri_plugin_store::Builder::new().build())
        .plugin(tauri_plugin_notification::init())
        // fs before dialog: the dialog plugin extends the fs runtime scope
        // (each user-picked path becomes writable), so it must find a managed
        // fs scope when it initialises.
        .plugin(tauri_plugin_fs::init())
        .plugin(tauri_plugin_dialog::init())
        // The fork deliberately ships no updater: upstream's endpoint was
        // removed from tauri.conf.json so a build phones nobody. Registering
        // the plugin anyway makes it read a `plugins.updater` block that is
        // no longer there, and its deserialiser panics on the null at
        // startup -- so the app must not register what it does not configure.
        .plugin(tauri_plugin_http::init())
        .plugin(tauri_plugin_shell::init()); // Add shell plugin
    #[cfg(target_os = "macos")]
    {
        builder = builder.plugin(tauri_nspanel::init());
    }
    let mut builder = builder
        .invoke_handler(tauri::generate_handler![
            get_app_version,
            cli::list_cli_tools,
            cli::run_cli,
            desk::capture_desk_context,
            wake::wake_status,
            wake::wake_start,
            wake::wake_stop,
            wake::wake_debug_trigger,
            window::set_window_height,
            window::set_window_frame,
            window::open_dashboard,
            window::toggle_dashboard,
            window::move_window,
            shortcuts::check_shortcuts_registered,
            shortcuts::get_registered_shortcuts,
            shortcuts::update_shortcuts,
            shortcuts::validate_shortcut_key,
            shortcuts::set_app_icon_visibility,
            shortcuts::set_always_on_top,
            shortcuts::exit_app,
            tray::tray_set_state,
            // The terminal skin's real pty. Unlike `cli::run_cli` these are
            // the user's own shell with their own privileges, so they only
            // exist while that skin is open; see `pty.rs`.
            pty::pty_open,
            pty::pty_write,
            pty::pty_resize,
            pty::pty_close,
            pty::pty_sessions,
            // The first-launch gate; see `setup.rs`.
            setup::setup_status,
            setup::setup_finish,
            setup::setup_restart,
            setup::setup_window_mode,
            setup::kaleo_has_focus,
        ])
        .setup(|app| {
            wake::hydrate_env();
            // macOS only: the activation policy was previously set nowhere but
            // `shortcuts::set_app_icon_visibility`, which the frontend calls
            // only once the webview has booted -- so the very first launch came
            // up as a Regular app and stole focus. `setup::apply_launch_policy`
            // decides here, before the event loop starts (the window the
            // frontend cannot cover): Accessory on an ordinary launch, Regular
            // on a gated first launch so the Setup Assistant can take focus.
            // It also opens settings.json with autosave before any webview has
            // booted -- the store plugin caches one Store per path and ignores
            // the options of every later `load`, so a JS-first load would leave
            // the wizard's writes in memory and never on disk.
            let gating = setup::apply_launch_policy(app);
            // Setup main window positioning. The window is created hidden
            // (`visible: false` in tauri.conf.json) and shown below only when
            // not gating, so a first launch never flashes the strip.
            window::setup_main_window(app).expect("Failed to setup main window");
            #[cfg(target_os = "macos")]
            init(app.app_handle());
            // The menu bar icon, after the panel exists: its left click
            // toggles that window. A failure here is reported, not fatal --
            // the overlay works without a status item.
            if let Err(e) = tray::build(app.handle()) {
                eprintln!("Failed to build the tray icon: {}", e);
            }
            #[cfg(all(target_os = "macos", debug_assertions))]
            set_dev_dock_icon();
            let app_handle = app.handle();
            let dashboard_url = if gating {
                setup::WELCOME_URL
            } else {
                setup::WORKBENCH_URL
            };
            eprintln!("[boot-probe] gating={} dashboard_url={}", gating, dashboard_url);
            if app_handle.get_webview_window("dashboard").is_none() {
                match window::create_dashboard_window(&app_handle, dashboard_url) {
                    Ok(dashboard) if gating => {
                        // The Setup Assistant shape from the first frame, so the
                        // frontend's own `setup_window_mode` on route enter is a
                        // no-op rather than a visible resize.
                        if let Err(e) =
                            setup::apply_window_mode(&dashboard, setup::WindowMode::Welcome)
                        {
                            eprintln!("Failed to size the Setup Assistant window: {}", e);
                        }
                        if let Err(e) = dashboard.show().and_then(|_| dashboard.set_focus()) {
                            eprintln!("Failed to focus the Setup Assistant window: {}", e);
                        }
                    }
                    Ok(_) => {}
                    Err(e) => eprintln!("Failed to pre-create dashboard window on startup: {}", e),
                }
            }
            if !gating {
                // `reveal()` does this at the end of the wizard; on every other
                // launch the strip appears here, through the same path.
                if let Err(e) = setup::show_strip(&app_handle) {
                    eprintln!("Failed to show the strip on startup: {}", e);
                }
            }

            #[cfg(desktop)]
            {
                use tauri_plugin_autostart::MacosLauncher;

                #[allow(deprecated, unexpected_cfgs)]
                if let Err(e) = app.handle().plugin(tauri_plugin_autostart::init(
                    MacosLauncher::LaunchAgent,
                    Some(vec![]),
                )) {
                    eprintln!("Failed to initialize autostart plugin: {}", e);
                }
            }

            // Initialize global shortcut plugin with centralized handler
            app.handle()
                .plugin(
                    tauri_plugin_global_shortcut::Builder::new()
                        .with_handler(move |app, shortcut, event| {
                            use tauri_plugin_global_shortcut::{Shortcut, ShortcutState};

                            let action_id = {
                                let state = app.state::<shortcuts::RegisteredShortcuts>();
                                let registered = match state.shortcuts.lock() {
                                    Ok(guard) => guard,
                                    Err(poisoned) => {
                                        eprintln!("Mutex poisoned in handler, recovering...");
                                        poisoned.into_inner()
                                    }
                                };

                                registered.iter().find_map(|(action_id, shortcut_str)| {
                                    if let Ok(s) = shortcut_str.parse::<Shortcut>() {
                                        if &s == shortcut {
                                            return Some(action_id.clone());
                                        }
                                    }
                                    None
                                })
                            };

                            if let Some(action_id) = action_id {
                                match event.state() {
                                    ShortcutState::Pressed => {
                                        if let Some(direction) =
                                            action_id.strip_prefix("move_window_")
                                        {
                                            shortcuts::start_move_window(app, direction);
                                        } else {
                                            eprintln!("Shortcut triggered: {}", action_id);
                                            shortcuts::handle_shortcut_action(app, &action_id);
                                        }
                                    }
                                    ShortcutState::Released => {
                                        if let Some(direction) =
                                            action_id.strip_prefix("move_window_")
                                        {
                                            shortcuts::stop_move_window(app, direction);
                                        }
                                    }
                                }
                            }
                        })
                        .build(),
                )
                .expect("Failed to initialize global shortcut plugin");
            if let Err(e) = shortcuts::setup_global_shortcuts(app.handle()) {
                eprintln!("Failed to setup global shortcuts: {}", e);
            }
            Ok(())
        });

    // Add macOS-specific permissions plugin
    #[cfg(target_os = "macos")]
    {
        builder = builder.plugin(tauri_plugin_macos_permissions::init());
    }

    builder
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}

/// `tauri dev` runs the bare debug binary rather than a `.app` bundle, so
/// macOS has no Info.plist to read an icon from and the Dock shows the
/// generic executable icon. Hand NSApplication the same PNG the bundle
/// carries so a dev-mode Dock shows the Kaleo mark. Release builds are
/// bundled and already get the icon from `bundle.icon`, hence the
/// `debug_assertions` gate at the call site.
#[cfg(all(target_os = "macos", debug_assertions))]
#[allow(deprecated)]
fn set_dev_dock_icon() {
    use tauri_nspanel::cocoa::{
        appkit::{NSApp, NSApplication, NSImage},
        base::nil,
        foundation::{NSData, NSUInteger},
    };

    const ICON: &[u8] = include_bytes!("../icons/icon.png");
    unsafe {
        let data = NSData::dataWithBytes_length_(
            nil,
            ICON.as_ptr() as *const std::ffi::c_void,
            ICON.len() as NSUInteger,
        );
        let image = NSImage::initWithData_(NSImage::alloc(nil), data);
        if image != nil {
            NSApp().setApplicationIconImage_(image);
        }
    }
}

#[cfg(target_os = "macos")]
#[allow(deprecated, unexpected_cfgs)]
fn init(app_handle: &AppHandle) {
    let window: WebviewWindow = app_handle.get_webview_window("main").unwrap();

    let panel = window.to_panel().unwrap();

    let delegate = panel_delegate!(MyPanelDelegate {
        window_did_become_key,
        window_did_resign_key
    });

    let handle = app_handle.to_owned();

    delegate.set_listener(Box::new(move |delegate_name: String| {
        match delegate_name.as_str() {
            "window_did_become_key" => {
                let app_name = handle.package_info().name.to_owned();

                println!("[info]: {:?} panel becomes key window!", app_name);
            }
            "window_did_resign_key" => {
                println!("[info]: panel resigned from key window!");
            }
            _ => (),
        }
    }));

    // Set the window to float level
    #[allow(non_upper_case_globals)]
    const NSFloatWindowLevel: i32 = 4;
    panel.set_level(NSFloatWindowLevel);
    // NSPanel hides itself when its app deactivates, and every stage that
    // opens KiCad or a CAD viewer deactivates us. The strip must stay put.
    panel.set_hides_on_deactivate(false);

    #[allow(non_upper_case_globals)]
    const NSWindowStyleMaskNonActivatingPanel: i32 = 1 << 7;
    panel.set_style_mask(NSWindowStyleMaskNonActivatingPanel);

    #[allow(deprecated)]
    panel.set_collection_behaviour(
        NSWindowCollectionBehavior::NSWindowCollectionBehaviorFullScreenAuxiliary
            | NSWindowCollectionBehavior::NSWindowCollectionBehaviorCanJoinAllSpaces,
    );

    panel.set_delegate(delegate);
}
