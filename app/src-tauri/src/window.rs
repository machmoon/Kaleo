#[cfg(target_os = "macos")]
use tauri::LogicalPosition;
use tauri::{App, AppHandle, Manager, Runtime, WebviewWindow, WebviewWindowBuilder};

// Where the bar's top edge sits: `TOP_OFFSET` *physical* pixels below the top
// of the monitor. This is a position, not a size, and the frontend's
// `TOP_OFFSET_PX` (app/src/lib/overlay-dock.ts) must be the same number — the
// first "top" dock decision re-applies this position, so a mismatch is an
// unexplained hop the moment anything triggers a dock apply.
//
// It is NOT the collapsed window height (`OVERLAY_COLLAPSED_HEIGHT` = 58 in
// app/src/hooks/useOverlayHeight.ts, mirrored by `app.height` in
// tauri.conf.json). The two were both 54 once, purely by coincidence, and a
// sweep that raised the *height* to 58 raised this with it and put a 4 px hop
// on the first dock apply. Different quantities, different units (physical
// pixels here, logical there): change one without the other.
const TOP_OFFSET: i32 = 54;

/// Sets up the main window with custom positioning
pub fn setup_main_window(app: &mut App) -> Result<(), Box<dyn std::error::Error>> {
    // Try different possible window labels
    let window = app
        .get_webview_window("main")
        .or_else(|| app.get_webview_window("kaleo"))
        .or_else(|| {
            // Get the first window if specific labels don't work
            app.webview_windows().values().next().cloned()
        })
        .ok_or("No window found")?;

    position_window_top_center(&window, TOP_OFFSET)?;

    // Set window as non-focusable on Windows
    // #[cfg(target_os = "windows")]
    // {
    //     let _ = window.set_focusable(false);
    // }

    Ok(())
}

/// Positions a window at the top center of the screen with a specified Y offset
pub fn position_window_top_center(
    window: &WebviewWindow,
    y_offset: i32,
) -> Result<(), Box<dyn std::error::Error>> {
    // Get the primary monitor
    if let Some(monitor) = window.primary_monitor()? {
        let monitor_size = monitor.size();
        let window_size = window.outer_size()?;

        // Calculate center X position
        let center_x = (monitor_size.width as i32 - window_size.width as i32) / 2;

        // Set the window position
        window.set_position(tauri::Position::Physical(tauri::PhysicalPosition {
            x: center_x,
            y: y_offset,
        }))?;
    }

    Ok(())
}

/// Future function for centering window completely (both X and Y)
#[allow(dead_code)]
pub fn center_window_completely(window: &WebviewWindow) -> Result<(), Box<dyn std::error::Error>> {
    if let Some(monitor) = window.primary_monitor()? {
        let monitor_size = monitor.size();
        let window_size = window.outer_size()?;

        let center_x = (monitor_size.width as i32 - window_size.width as i32) / 2;
        let center_y = (monitor_size.height as i32 - window_size.height as i32) / 2;

        window.set_position(tauri::Position::Physical(tauri::PhysicalPosition {
            x: center_x,
            y: center_y,
        }))?;
    }

    Ok(())
}

/// The bar's full width; the frontend's `OVERLAY_WIDTH` must match.
const OVERLAY_WIDTH: f64 = 600.0;

/// Resize the overlay. `width` is optional so the historical callers keep
/// the full-width bar; the idle pill passes its own, narrower width so the
/// transparent window stops swallowing clicks meant for KiCad beneath it.
///
/// Kept as the size-only path. Prefer `set_window_frame`, which also carries
/// the origin — see its comment for why the two must arrive together.
#[tauri::command]
pub fn set_window_height(
    window: tauri::WebviewWindow,
    height: u32,
    width: Option<u32>,
) -> Result<(), String> {
    set_window_frame(window, height, width, None, None)
}

/// Resize the overlay and, in the **same operation**, move it.
///
/// `width` is logical pixels like `set_window_height`; `x`/`y` are the window's
/// top-left in *physical* pixels, the frame `dockPosition` and Tauri's
/// `setPosition` both work in. Both must be present to move; either missing is
/// a plain resize, which is what the historical callers get.
///
/// Why this exists. `set_size` is anchored at the window's top-left, so
/// collapsing the bar 600 → 132 logical pixels moves its visual centre 234 px
/// to the left; the dock then re-centres it in a *separate* native call, up to
/// 400 ms later. The pill's start and end positions are identical — the entire
/// visible jump is the gap between two operations. It cannot be closed by
/// ordering two `invoke`s from the frontend: in the pinned `tao` (Cargo.lock:
/// 0.34.2) `set_inner_size` goes through `util::set_content_size_async` and
/// `set_outer_position` through `util::set_frame_top_left_point_async`, each
/// dispatching its own fire-and-forget block onto the main GCD queue. Two
/// calls are two queued blocks and the split frame between them is structural,
/// so the fix has to be one block that sets the whole frame.
#[tauri::command]
pub fn set_window_frame(
    window: tauri::WebviewWindow,
    height: u32,
    width: Option<u32>,
    x: Option<i32>,
    y: Option<i32>,
) -> Result<(), String> {
    use tauri::{LogicalSize, Size};

    let size = LogicalSize::new(
        width.map(f64::from).unwrap_or(OVERLAY_WIDTH),
        f64::from(height),
    );

    match (x, y) {
        (Some(x), Some(y)) => apply_frame(&window, size, x, y),
        _ => window
            .set_size(Size::Logical(size))
            .map_err(|e| format!("Failed to resize window: {}", e)),
    }
}

/// Size and origin in one operation.
///
/// macOS gets the atomic path below. Everywhere else this is still two calls —
/// the split frame is a macOS/`tao` artifact (two GCD blocks), and X11/Win32
/// have their own single-shot APIs that `tao` does not expose either. Doing the
/// resize first and the move second keeps the behaviour these platforms have
/// today rather than inventing a new one that cannot be tested here.
#[cfg(not(target_os = "macos"))]
fn apply_frame(
    window: &tauri::WebviewWindow,
    size: tauri::LogicalSize<f64>,
    x: i32,
    y: i32,
) -> Result<(), String> {
    use tauri::{PhysicalPosition, Position, Size};

    window
        .set_size(Size::Logical(size))
        .map_err(|e| format!("Failed to resize window: {}", e))?;
    window
        .set_position(Position::Physical(PhysicalPosition { x, y }))
        .map_err(|e| format!("Failed to move window: {}", e))
}

#[cfg(target_os = "macos")]
fn apply_frame(
    window: &tauri::WebviewWindow,
    size: tauri::LogicalSize<f64>,
    x: i32,
    y: i32,
) -> Result<(), String> {
    let scale = window
        .scale_factor()
        .map_err(|e| format!("Failed to read the scale factor: {}", e))?;
    let scale = if scale.is_finite() && scale > 0.0 { scale } else { 1.0 };
    let current = window
        .inner_size()
        .map_err(|e| format!("Failed to read the window size: {}", e))?;

    // Grow and shrink are not symmetric; see `macos_frame::set_frame`.
    let growing = size.width * scale > f64::from(current.width) + 0.5
        || size.height * scale > f64::from(current.height) + 0.5;

    let ns_window = window
        .ns_window()
        .map_err(|e| format!("Failed to reach the native window: {}", e))?;
    let handle = macos_frame::MainThreadWindow(ns_window);

    // Logical (point) coordinates, the frame AppKit works in. `tao` divides by
    // the *window's* scale factor for exactly this conversion
    // (`platform_impl::macos::window::set_outer_position`), so we do too — the
    // positions the dock reads back come from `tao` and the two must agree.
    let left = f64::from(x) / scale;
    let top = f64::from(y) / scale;
    let (w, h) = (size.width, size.height);

    window
        .run_on_main_thread(move || unsafe {
            // Move the wrapper, not the pointer: capturing `handle.0` directly
            // captures a bare `*mut c_void`, which is not `Send`.
            let handle = handle;
            macos_frame::set_frame(handle.0, left, top, w, h, growing);
        })
        .map_err(|e| format!("Failed to set the window frame: {}", e))
}

/// The one Objective-C call in this file: set an `NSWindow`'s whole frame at
/// once, with Core Animation's implicit actions off.
///
/// **Recipe and citation.** `Wox-launcher/Wox` (**GPL-3.0**, compatible with
/// this repo's licence) solves the same split-frame problem in
/// `wox.core/ui/runtime/native_darwin.m`. Its `wox_darwin_window_set_bounds`
/// (read at master on 2026-09-06) is, verbatim in shape:
///
/// ```objc
///   NSRect frame = NSMakeRect(x, desktop_top() - y - height, width, height);
///   [CATransaction begin];
///   [CATransaction setDisableActions:YES];
///   window->suppress_resize_render = true;
///   [window->window setFrame:frame display:NO];
///   window->suppress_resize_render = false;
///   render_resize_frame(window);      // one explicit synchronous present
///   [CATransaction commit];
/// ```
///
/// — all inside a `run_on_main_sync` block. Three things are taken from it: the
/// top-left → Cocoa bottom-left flip against the desktop height, the single
/// `setFrame:display:NO` for both axes, and the disabled-actions transaction
/// that stops Core Animation animating the layer to its new bounds behind our
/// back.
///
/// **Two honest corrections to the brief this was written from.** Wox's
/// `set_bounds` has *no* grow/shrink asymmetry and no regression test pinning
/// one — it renders synchronously in both directions, because it drives its own
/// IOSurface renderer and can suppress the resize-triggered render outright
/// (`suppress_resize_render`, `WoxContentView.setFrameSize:`). Nor does it call
/// `invalidateShadow` anywhere. Both are kept here anyway, for reasons that are
/// ours rather than Wox's:
///
/// * `render_after` (grow only) is the asymmetry. We host a `WKWebView`, not a
///   surface we can present on demand: on a grow the window is momentarily
///   larger than anything painted, so a synchronous `displayIfNeeded` inside
///   the transaction is worth asking for. On a shrink there is nothing to
///   reveal — the content already covers the smaller frame — and forcing a draw
///   mid-resize on a borderless transparent window is how the backdrop shows
///   through before the content catches up.
/// * `invalidateShadow` is `wails#4937`: `tao` never calls it, and a borderless
///   window keeps the shadow cached for its *old* shape after a resize. Ours is
///   `shadow: false` in `tauri.conf.json`, so this is belt-and-braces — cheap,
///   once, after the frame settles.
///
/// No new crate: the Objective-C runtime is three C functions and
/// `objc_msgSend` transmuted to the right signature, which is what the `objc`
/// crate does under its macros. Nothing here returns a struct, so the
/// `objc_msgSend_stret` split that x86_64 would otherwise force never arises.
#[cfg(target_os = "macos")]
mod macos_frame {
    use std::ffi::c_void;
    use std::os::raw::c_char;

    #[repr(C)]
    #[derive(Clone, Copy)]
    struct NSPoint {
        x: f64,
        y: f64,
    }

    #[repr(C)]
    #[derive(Clone, Copy)]
    struct NSSize {
        width: f64,
        height: f64,
    }

    #[repr(C)]
    #[derive(Clone, Copy)]
    struct NSRect {
        origin: NSPoint,
        size: NSSize,
    }

    type Id = *mut c_void;
    type Sel = *const c_void;

    /// An `NSWindow` pointer on its way to the main thread. Sending a raw
    /// pointer is the unsafe part; every *use* of it below happens inside
    /// `run_on_main_thread`, which is where AppKit requires it.
    pub struct MainThreadWindow(pub Id);
    unsafe impl Send for MainThreadWindow {}

    #[link(name = "objc", kind = "dylib")]
    extern "C" {
        fn objc_getClass(name: *const c_char) -> Id;
        fn sel_registerName(name: *const c_char) -> Sel;
        fn objc_msgSend();
    }

    #[link(name = "CoreGraphics", kind = "framework")]
    extern "C" {
        fn CGMainDisplayID() -> u32;
        fn CGDisplayPixelsHigh(display: u32) -> usize;
    }

    /// `name` must be NUL-terminated.
    unsafe fn sel(name: &[u8]) -> Sel {
        sel_registerName(name.as_ptr() as *const c_char)
    }

    unsafe fn send(obj: Id, selector: Sel) {
        let f: extern "C" fn(Id, Sel) = std::mem::transmute(objc_msgSend as *const c_void);
        f(obj, selector);
    }

    unsafe fn send_bool(obj: Id, selector: Sel, value: bool) {
        // `BOOL` is `signed char` on x86_64 and `_Bool` on arm64; a byte-wide
        // integer argument is correct for both.
        let f: extern "C" fn(Id, Sel, i8) = std::mem::transmute(objc_msgSend as *const c_void);
        f(obj, selector, value as i8);
    }

    unsafe fn send_frame(obj: Id, selector: Sel, rect: NSRect, display: bool) {
        let f: extern "C" fn(Id, Sel, NSRect, i8) =
            std::mem::transmute(objc_msgSend as *const c_void);
        f(obj, selector, rect, display as i8);
    }

    /// Set the window's frame from a top-left origin in points.
    ///
    /// `left`/`top` are tao's screen frame (top-left origin, y downwards);
    /// `width`/`height` are the frame size. The window is borderless
    /// (`decorations: false` in tauri.conf.json), so its frame rect and its
    /// content rect are the same rectangle and no `frameRectForContentRect:`
    /// round trip is needed.
    ///
    /// # Safety
    /// `ns_window` must be a live `NSWindow` and this must run on the main
    /// thread.
    pub unsafe fn set_frame(
        ns_window: Id,
        left: f64,
        top: f64,
        width: f64,
        height: f64,
        render_after: bool,
    ) {
        if ns_window.is_null() {
            return;
        }
        // tao's own top-left → Cocoa conversion, reproduced so the positions
        // the dock reads back through tao agree with the ones we write:
        // `util::window_position` is `NSPoint(x, CGDisplay::main().pixels_high() - y)`.
        let desktop_top = CGDisplayPixelsHigh(CGMainDisplayID()) as f64;
        let frame = NSRect {
            origin: NSPoint {
                x: left,
                y: desktop_top - top - height,
            },
            size: NSSize { width, height },
        };

        let transaction = objc_getClass(b"CATransaction\0".as_ptr() as *const c_char);
        if !transaction.is_null() {
            send(transaction, sel(b"begin\0"));
            send_bool(transaction, sel(b"setDisableActions:\0"), true);
        }

        send_frame(ns_window, sel(b"setFrame:display:\0"), frame, false);
        if render_after {
            // Grow only: the window is briefly bigger than anything drawn.
            send(ns_window, sel(b"displayIfNeeded\0"));
        }

        if !transaction.is_null() {
            send(transaction, sel(b"commit\0"));
        }

        // wails#4937: `tao` never invalidates the shadow, so a borderless
        // window keeps the one cached for its previous shape.
        send(ns_window, sel(b"invalidateShadow\0"));
    }
}

#[tauri::command]
pub fn open_dashboard(app: tauri::AppHandle) -> Result<(), String> {
    show_dashboard_window(&app)
}

#[tauri::command]
pub fn toggle_dashboard(app: tauri::AppHandle) -> Result<(), String> {
    if let Some(dashboard_window) = app.get_webview_window("dashboard") {
        match dashboard_window.is_visible() {
            Ok(true) => {
                // Window is visible, hide it
                dashboard_window
                    .hide()
                    .map_err(|e| format!("Failed to hide dashboard window: {}", e))?;
            }
            Ok(false) => {
                // Window is hidden, show and focus it
                dashboard_window
                    .show()
                    .map_err(|e| format!("Failed to show dashboard window: {}", e))?;
                dashboard_window
                    .set_focus()
                    .map_err(|e| format!("Failed to focus dashboard window: {}", e))?;
            }
            Err(e) => {
                return Err(format!("Failed to check dashboard visibility: {}", e));
            }
        }
    } else {
        // Window doesn't exist, create and show it
        show_dashboard_window(&app)?;
    }

    Ok(())
}

#[tauri::command]
pub fn move_window(app: tauri::AppHandle, direction: String, step: i32) -> Result<(), String> {
    if let Some(window) = app.get_webview_window("main") {
        let current_pos = window
            .outer_position()
            .map_err(|e| format!("Failed to get window position: {}", e))?;

        let (new_x, new_y) = match direction.as_str() {
            "up" => (current_pos.x, current_pos.y - step),
            "down" => (current_pos.x, current_pos.y + step),
            "left" => (current_pos.x - step, current_pos.y),
            "right" => (current_pos.x + step, current_pos.y),
            _ => return Err(format!("Invalid direction: {}", direction)),
        };

        window
            .set_position(tauri::Position::Physical(tauri::PhysicalPosition {
                x: new_x,
                y: new_y,
            }))
            .map_err(|e| format!("Failed to set window position: {}", e))?;
    } else {
        return Err("Main window not found".to_string());
    }

    Ok(())
}

pub fn create_dashboard_window<R: Runtime>(
    app: &AppHandle<R>,
    url: &str,
) -> Result<WebviewWindow<R>, tauri::Error> {
    let base_builder =
        WebviewWindowBuilder::new(app, "dashboard", tauri::WebviewUrl::App(url.into()));

    #[cfg(target_os = "macos")]
    let base_builder = base_builder
        .title("Hardy — Dashboard")
        .center()
        .decorations(true)
        .inner_size(1200.0, 800.0)
        .min_inner_size(800.0, 600.0)
        .hidden_title(true)
        .title_bar_style(tauri::TitleBarStyle::Overlay)
        .content_protected(false)
        .visible(true)
        .traffic_light_position(LogicalPosition::new(14.0, 18.0));

    #[cfg(not(target_os = "macos"))]
    let base_builder = base_builder
        .title("Hardy — Dashboard")
        .center()
        .decorations(true)
        .inner_size(800.0, 600.0)
        .min_inner_size(800.0, 600.0)
        .content_protected(false)
        .visible(false);

    let window = base_builder.build()?;

    // Set up close event handler - hide window instead of destroying it
    setup_dashboard_close_handler(&window);

    Ok(window)
}

/// Sets up the close event handler for the dashboard window
fn setup_dashboard_close_handler<R: Runtime>(window: &WebviewWindow<R>) {
    let window_clone = window.clone();
    let app = window.app_handle().clone();
    window.on_window_event(move |event| {
        if let tauri::WindowEvent::CloseRequested { api, .. } = event {
            // Prevent the window from being destroyed
            api.prevent_close();
            // Mid-setup, closing FINISHES the setup first: on a gated launch the
            // strip is hidden and this is the only window, so an Accessory app
            // with neither has nothing left to click on. The hide goes ahead
            // even if that fails, and the strip is still attempted.
            if app.state::<crate::setup::SetupState>().in_setup() {
                if let Err(e) = crate::setup::finish_from_close(&app) {
                    eprintln!("Failed to finish setup on dashboard close: {}", e);
                    if let Err(e) = crate::setup::show_strip(&app) {
                        eprintln!("Failed to show the strip on dashboard close: {}", e);
                    }
                }
            }
            // Hide the window instead
            if let Err(e) = window_clone.hide() {
                eprintln!("Failed to hide dashboard window on close: {}", e);
            }
        }
    });
}

/// Shows the dashboard window and brings it to focus
pub fn show_dashboard_window<R: Runtime>(app: &AppHandle<R>) -> Result<(), String> {
    if let Some(dashboard_window) = app.get_webview_window("dashboard") {
        // Window exists, show and focus it
        dashboard_window
            .show()
            .map_err(|e| format!("Failed to show dashboard window: {}", e))?;
        dashboard_window
            .set_focus()
            .map_err(|e| format!("Failed to focus dashboard window: {}", e))?;
    } else {
        // Window doesn't exist, create it and then show it
        let window = create_dashboard_window(app, crate::setup::WORKBENCH_URL)
            .map_err(|e| format!("Failed to create dashboard window: {}", e))?;
        window
            .show()
            .map_err(|e| format!("Failed to show new dashboard window: {}", e))?;
        window
            .set_focus()
            .map_err(|e| format!("Failed to focus new dashboard window: {}", e))?;
    }
    Ok(())
}
