//! The menu bar (tray) icon: Hardy up beside Cold Turkey, Claude and Box.
//!
//! One status item with two jobs. The glyph itself says whether Hardy's
//! microphone is open -- a filled orb while it listens, an outline ring while
//! it is muted -- so the menu bar shows the mic state the way Claude's icon
//! shows its own. And it carries the quick actions: left click toggles the
//! overlay, right click opens a menu with show/hide, the listening switch,
//! the dashboard, about and quit.
//!
//! Two honesty rules, both about the "Hardy listening" check item:
//!
//! * The mark follows the *real* microphone state, which lives in React
//!   (`useWakeWord`), not in this file. Clicking the item only emits
//!   [`TOGGLE_EVENT`]; the front end flips the ear and answers with
//!   `tray_set_state`, and only that call moves the mark. macOS/`muda` toggle a
//!   check item's own state on click, so the handler puts it straight back --
//!   a check mark that changed because it was clicked, rather than because the
//!   mic opened, would be a lie about an open microphone.
//! * The window's visibility is read from the window (or, on Windows, from
//!   the `display:none` workaround state), never assumed from the last click.
//!
//! Icons are `include_bytes!` so the binary needs no resource lookup at the
//! moment the tray is built; see `icons/tray/gen.py` for how they were drawn.
//! On macOS they are **template images** (black on transparent) and the system
//! tints them for a light or dark menu bar; `tray-icon` drops the template flag
//! whenever the icon is swapped, so [`apply`] restores it after every swap.
//! Everywhere else the coloured (copper) variants are used, since template
//! rendering is a macOS-only concept.

use std::sync::Mutex;

use tauri::{
    image::Image,
    menu::{CheckMenuItem, Menu, MenuEvent, MenuItem, PredefinedMenuItem},
    tray::{MouseButton, MouseButtonState, TrayIcon, TrayIconBuilder, TrayIconEvent},
    AppHandle, Emitter, Manager, Runtime,
};

/// Emitted to the main window when "Hardy listening" is clicked. The front end
/// answers by flipping the ear and calling `tray_set_state`.
pub const TOGGLE_EVENT: &str = "tray-ada-toggle";

/// The tray icon's id (one per app; `Manager::tray_by_id`).
pub const TRAY_ID: &str = "kaleo";

pub const ID_SHOW: &str = "tray-show";
pub const ID_LISTENING: &str = "tray-listening";
pub const ID_DASHBOARD: &str = "tray-dashboard";
pub const ID_ABOUT: &str = "tray-about";
pub const ID_QUIT: &str = "tray-quit";

pub const LABEL_SHOW: &str = "Show Hardy";
pub const LABEL_HIDE: &str = "Hide Hardy";
pub const LABEL_LISTENING: &str = "Hardy listening";
pub const LABEL_DASHBOARD: &str = "Open dashboard";
pub const LABEL_ABOUT: &str = "About Hardy";
pub const LABEL_QUIT: &str = "Quit Hardy";

#[cfg(target_os = "macos")]
const ICON_IDLE: &[u8] = include_bytes!("../icons/tray/idleTemplate@2x.png");
#[cfg(target_os = "macos")]
const ICON_LIVE: &[u8] = include_bytes!("../icons/tray/liveTemplate@2x.png");
#[cfg(not(target_os = "macos"))]
const ICON_IDLE: &[u8] = include_bytes!("../icons/tray/idle-colour.png");
#[cfg(not(target_os = "macos"))]
const ICON_LIVE: &[u8] = include_bytes!("../icons/tray/live-colour.png");

/// What the menu bar currently claims. Both facts arrive from outside this
/// file: `listening` from the front end, `visible` from the window.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct TraySnapshot {
    pub listening: bool,
    pub visible: bool,
}

/// The tray's handles, managed on the app so the shortcut path and the
/// `tray_set_state` command can reach the same items.
pub struct TrayState<R: Runtime> {
    icon: TrayIcon<R>,
    show: MenuItem<R>,
    listening: CheckMenuItem<R>,
    snapshot: Mutex<TraySnapshot>,
}

/// The glyph for a microphone state: the filled orb while it is open.
pub fn icon_for(listening: bool) -> tauri::Result<Image<'static>> {
    Image::from_bytes(if listening { ICON_LIVE } else { ICON_IDLE })
}

/// The label of the show/hide item for a window state.
pub fn show_label(visible: bool) -> &'static str {
    if visible {
        LABEL_HIDE
    } else {
        LABEL_SHOW
    }
}

/// The tooltip: the app and its version, so hovering answers "which build".
pub fn tooltip() -> String {
    format!("Hardy {}", env!("CARGO_PKG_VERSION"))
}

/// Build the status item and its menu, and manage the handles on the app.
pub fn build<R: Runtime>(app: &AppHandle<R>) -> tauri::Result<()> {
    let show = MenuItem::with_id(app, ID_SHOW, LABEL_HIDE, true, None::<&str>)?;
    let listening =
        CheckMenuItem::with_id(app, ID_LISTENING, LABEL_LISTENING, true, false, None::<&str>)?;
    let dashboard = MenuItem::with_id(app, ID_DASHBOARD, LABEL_DASHBOARD, true, None::<&str>)?;
    let about = MenuItem::with_id(app, ID_ABOUT, LABEL_ABOUT, true, None::<&str>)?;
    let quit = MenuItem::with_id(app, ID_QUIT, LABEL_QUIT, true, None::<&str>)?;
    let menu = Menu::with_items(
        app,
        &[
            &show,
            &listening,
            &dashboard,
            &PredefinedMenuItem::separator(app)?,
            &about,
            &quit,
        ],
    )?;

    let icon = TrayIconBuilder::with_id(TRAY_ID)
        .icon(icon_for(false)?)
        // A no-op off macOS; there the coloured PNG is used as drawn.
        .icon_as_template(cfg!(target_os = "macos"))
        .tooltip(tooltip())
        .menu(&menu)
        // Left click is the overlay toggle; the menu is on the right button.
        .show_menu_on_left_click(false)
        .on_menu_event(on_menu_event)
        .on_tray_icon_event(|tray, event| {
            if let TrayIconEvent::Click {
                button: MouseButton::Left,
                button_state: MouseButtonState::Up,
                ..
            } = event
            {
                let app = tray.app_handle();
                crate::shortcuts::toggle_overlay(app);
                sync_visible(app);
            }
        })
        .build(app)?;

    app.manage(TrayState {
        icon,
        show,
        listening,
        snapshot: Mutex::new(TraySnapshot {
            listening: false,
            visible: true,
        }),
    });
    // The overlay starts visible in tauri.conf.json, but read it rather than
    // assume it, so the first label is right on a platform that differs.
    sync_visible(app);
    Ok(())
}

fn on_menu_event<R: Runtime>(app: &AppHandle<R>, event: MenuEvent) {
    match event.id().as_ref() {
        ID_SHOW => {
            crate::shortcuts::toggle_overlay(app);
            sync_visible(app);
        }
        ID_LISTENING => {
            // Ask; do not assume. The mark is put back to the last state the
            // front end reported, and moves only when it reports the next.
            if let Some(state) = app.try_state::<TrayState<R>>() {
                let snapshot = *lock(&state.snapshot);
                if let Err(e) = state.listening.set_checked(snapshot.listening) {
                    eprintln!("[tray] could not restore the listening mark: {e}");
                }
            }
            if let Err(e) = app.emit_to("main", TOGGLE_EVENT, ()) {
                eprintln!("[tray] could not emit {TOGGLE_EVENT}: {e}");
            }
        }
        ID_DASHBOARD => {
            if let Err(e) = crate::window::show_dashboard_window(app) {
                eprintln!("[tray] could not open the dashboard: {e}");
            }
        }
        ID_ABOUT => show_about(app),
        ID_QUIT => app.exit(0),
        _ => {}
    }
}

fn show_about<R: Runtime>(app: &AppHandle<R>) {
    use tauri_plugin_dialog::{DialogExt, MessageDialogKind};
    app.dialog()
        .message(format!(
            "Hardy {}\n\nDescribe a board; the engine designs it; review it in KiCad.\nA GPL-3.0 fork of Pluely.",
            env!("CARGO_PKG_VERSION")
        ))
        .title(LABEL_ABOUT)
        .kind(MessageDialogKind::Info)
        .show(|_| {});
}

fn lock(snapshot: &Mutex<TraySnapshot>) -> std::sync::MutexGuard<'_, TraySnapshot> {
    snapshot.lock().unwrap_or_else(|poisoned| poisoned.into_inner())
}

/// Push a snapshot onto the status item: icon, check mark and show/hide label.
///
/// The icon is only swapped when the mic state changed, because on macOS a
/// swap rebuilds the `NSImage` and drops the template flag (`tray-icon`'s
/// `set_icon` passes `is_template = false`), so every swap is followed by
/// `set_icon_as_template(true)` there.
pub fn apply<R: Runtime>(app: &AppHandle<R>, next: TraySnapshot) -> Result<(), String> {
    let Some(state) = app.try_state::<TrayState<R>>() else {
        // No tray was built (a headless test, or the build failed and was
        // reported at startup): nothing to update, and not an error.
        return Ok(());
    };
    let previous = {
        let mut guard = lock(&state.snapshot);
        std::mem::replace(&mut *guard, next)
    };

    if previous.listening != next.listening {
        let image = icon_for(next.listening).map_err(|e| format!("tray icon: {e}"))?;
        state
            .icon
            .set_icon(Some(image))
            .map_err(|e| format!("tray icon: {e}"))?;
        #[cfg(target_os = "macos")]
        state
            .icon
            .set_icon_as_template(true)
            .map_err(|e| format!("tray icon template: {e}"))?;
    }
    state
        .listening
        .set_checked(next.listening)
        .map_err(|e| format!("tray listening item: {e}"))?;
    state
        .show
        .set_text(show_label(next.visible))
        .map_err(|e| format!("tray show item: {e}"))?;
    Ok(())
}

/// Whether the overlay is on screen, read the way the platform hides it: the
/// window itself everywhere but Windows, where the hide shortcut leaves the
/// window shown and blanks the webview instead (`WindowVisibility`).
pub fn overlay_visible<R: Runtime>(app: &AppHandle<R>) -> bool {
    #[cfg(target_os = "windows")]
    {
        if let Some(state) = app.try_state::<crate::shortcuts::WindowVisibility>() {
            let hidden = state
                .is_hidden
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner());
            return !*hidden;
        }
    }
    app.get_webview_window("main")
        .and_then(|window| window.is_visible().ok())
        .unwrap_or(true)
}

/// Re-read the window's visibility and update the show/hide label; called
/// after anything that hides or shows the overlay natively.
pub fn sync_visible<R: Runtime>(app: &AppHandle<R>) {
    let listening = app
        .try_state::<TrayState<R>>()
        .map(|state| lock(&state.snapshot).listening)
        .unwrap_or(false);
    let next = TraySnapshot {
        listening,
        visible: overlay_visible(app),
    };
    if let Err(e) = apply(app, next) {
        eprintln!("[tray] could not sync visibility: {e}");
    }
}

/// The front end's report: the ear's real state and, on Windows, whether the
/// webview is blanked. Both change in React (`useTrayState`), and this is the
/// only path that moves the check mark.
#[tauri::command]
pub fn tray_set_state<R: Runtime>(
    app: AppHandle<R>,
    listening: bool,
    visible: bool,
) -> Result<(), String> {
    // Off Windows the window's own visibility is the truth and the front end
    // cannot see a native hide, so it is read here rather than trusted.
    let visible = if cfg!(target_os = "windows") {
        visible
    } else {
        overlay_visible(&app)
    };
    apply(&app, TraySnapshot { listening, visible })
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Every menu id, in menu order.
    const MENU_IDS: [&str; 5] = [ID_SHOW, ID_LISTENING, ID_DASHBOARD, ID_ABOUT, ID_QUIT];

    #[test]
    fn menu_ids_are_distinct_and_prefixed() {
        let mut seen = std::collections::HashSet::new();
        for id in MENU_IDS {
            assert!(id.starts_with("tray-"), "{id}");
            assert!(seen.insert(id), "duplicate menu id {id}");
        }
    }

    #[test]
    fn labels_name_the_app_and_the_ear() {
        assert_eq!(show_label(true), "Hide Hardy");
        assert_eq!(show_label(false), "Show Hardy");
        assert_eq!(LABEL_LISTENING, "Hardy listening");
        assert_eq!(LABEL_DASHBOARD, "Open dashboard");
        assert_eq!(LABEL_ABOUT, "About Hardy");
        assert_eq!(LABEL_QUIT, "Quit Hardy");
        assert!(tooltip().starts_with("Hardy "));
    }

    #[test]
    fn toggle_event_matches_the_frontend_seam() {
        // `useTrayState.ts` listens for this exact name.
        assert_eq!(TOGGLE_EVENT, "tray-ada-toggle");
    }

    #[test]
    fn both_icons_decode_and_differ() {
        let idle = icon_for(false).expect("idle icon decodes");
        let live = icon_for(true).expect("live icon decodes");
        assert_eq!((idle.width(), idle.height()), (live.width(), live.height()));
        assert_ne!(idle.rgba(), live.rgba(), "the two mic states must look different");
    }

    /// The template-image contract: every visible pixel is black, so macOS
    /// can tint the glyph for a light or dark menu bar. A coloured pixel would
    /// render as a grey smudge.
    #[cfg(target_os = "macos")]
    #[test]
    fn macos_icons_are_template_images() {
        for listening in [false, true] {
            let image = icon_for(listening).unwrap();
            for px in image.rgba().chunks(4) {
                if px[3] > 0 {
                    assert_eq!(&px[..3], &[0, 0, 0], "non-black pixel in a template icon");
                }
            }
        }
    }
}
