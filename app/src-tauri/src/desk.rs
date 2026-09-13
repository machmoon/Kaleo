//! Screenshot + cursor for spoken “this” / “I don’t like this”.
//!
//! Uses macOS `screencapture -C` (cursor burned into the PNG) and AppKit via
//! `osascript` for the mouse point. Screen Recording TCC is required; refusal
//! is an honest error string on the returned struct, never a fake black frame.

use base64::{engine::general_purpose::STANDARD as B64, Engine as _};
use serde::Serialize;
use std::fs;
use std::process::Command;
use std::time::{SystemTime, UNIX_EPOCH};

#[derive(Debug, Clone, Serialize)]
pub struct DeskSnapshot {
    pub png_base64: String,
    pub cursor_x: f64,
    pub cursor_y: f64,
    pub width: u32,
    pub height: u32,
    pub error: String,
}

fn empty_error(msg: impl Into<String>) -> DeskSnapshot {
    DeskSnapshot {
        png_base64: String::new(),
        cursor_x: 0.0,
        cursor_y: 0.0,
        width: 0,
        height: 0,
        error: msg.into(),
    }
}

/// AppKit mouse location is bottom-left; PNG rows are top-left — flip with height.
fn mouse_location_top_left(screen_height: f64) -> Result<(f64, f64), String> {
    let output = Command::new("osascript")
        .args([
            "-e",
            "use framework \"AppKit\"",
            "-e",
            "set loc to current application's NSEvent's mouseLocation()",
            "-e",
            "return ((loc's x) as text) & \",\" & ((loc's y) as text)",
        ])
        .output()
        .map_err(|e| format!("could not read mouse location: {e}"))?;
    if !output.status.success() {
        return Err(format!(
            "osascript mouseLocation failed: {}",
            String::from_utf8_lossy(&output.stderr)
        ));
    }
    let text = String::from_utf8_lossy(&output.stdout);
    let mut parts = text.trim().split(',');
    let x: f64 = parts
        .next()
        .ok_or_else(|| "mouse location missing x".to_string())?
        .parse()
        .map_err(|_| "mouse location x was not a number".to_string())?;
    let y_bottom: f64 = parts
        .next()
        .ok_or_else(|| "mouse location missing y".to_string())?
        .parse()
        .map_err(|_| "mouse location y was not a number".to_string())?;
    Ok((x, screen_height - y_bottom))
}

fn png_size(bytes: &[u8]) -> Option<(u32, u32)> {
    // IHDR sits at byte 16 after the 8-byte signature + 8-byte chunk header.
    if bytes.len() < 24 || &bytes[0..8] != b"\x89PNG\r\n\x1a\n" {
        return None;
    }
    let w = u32::from_be_bytes([bytes[16], bytes[17], bytes[18], bytes[19]]);
    let h = u32::from_be_bytes([bytes[20], bytes[21], bytes[22], bytes[23]]);
    Some((w, h))
}

#[tauri::command]
pub fn capture_desk_context() -> DeskSnapshot {
    #[cfg(not(target_os = "macos"))]
    {
        return empty_error("desk capture is only implemented on macOS right now");
    }

    #[cfg(target_os = "macos")]
    {
        let stamp = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_millis())
            .unwrap_or(0);
        let path = std::env::temp_dir().join(format!("ada-desk-{stamp}.png"));
        let path_str = match path.to_str() {
            Some(s) => s.to_string(),
            None => return empty_error("could not build a temp path for the screenshot"),
        };

        // -x: no shutter sound. -C: draw the cursor into the image so “this”
        // is visible even when the model never reads the coordinate fields.
        let status = Command::new("screencapture")
            .args(["-x", "-C", &path_str])
            .status();

        match status {
            Ok(s) if s.success() => {}
            Ok(_) => {
                let _ = fs::remove_file(&path);
                return empty_error(
                    "Screen Recording is off for Hardy (screencapture refused). Enable it in System Settings → Privacy & Security → Screen Recording.",
                );
            }
            Err(e) => {
                return empty_error(format!("could not run screencapture: {e}"));
            }
        }

        let bytes = match fs::read(&path) {
            Ok(b) => b,
            Err(e) => {
                let _ = fs::remove_file(&path);
                return empty_error(format!("could not read screenshot: {e}"));
            }
        };
        let _ = fs::remove_file(&path);

        let (width, height) = match png_size(&bytes) {
            Some(wh) => wh,
            None => return empty_error("screencapture wrote something that is not a PNG"),
        };

        let (cursor_x, cursor_y) = match mouse_location_top_left(height as f64) {
            Ok(xy) => xy,
            Err(e) => return empty_error(e),
        };

        DeskSnapshot {
            png_base64: B64.encode(bytes),
            cursor_x,
            cursor_y,
            width,
            height,
            error: String::new(),
        }
    }
}
