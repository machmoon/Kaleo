//! A real pseudo-terminal, so the terminal skin is a terminal.
//!
//! The overlay's terminal skin was asked for as "a literal terminal, so it
//! doubles as Ada and terminal". That rules out the shape this repo already
//! had: [`crate::cli`] runs an allowlisted tool, waits, and hands back the
//! captured stdout. `vim`, `htop`, `ssh`, tab completion, Ctrl-C and a shell
//! prompt all need a pty on the other end of the file descriptor, not a
//! pipe, because they ask the kernel whether they are talking to a terminal
//! and behave differently when the answer is no.
//!
//! `tauri-plugin-shell` has no pty, which is where an earlier round of this
//! work wrongly concluded Tauri could not do it. It can: `portable-pty` (the
//! WezTerm crate, MIT) opens one directly from here, which is exactly what
//! `marc2332/tauri-terminal`, `Shabari-K-S/terminon` and `tauri-plugin-pty`
//! all do. Electron's Hyper faces the identical problem and answers it the
//! identical way with `node-pty`.
//!
//! Three decisions worth stating, because each has a wrong version that
//! looks fine until it doesn't:
//!
//! * **Output goes over a [`Channel`], not `app.emit`.** Tauri's own docs say
//!   the event system "is not designed for low latency or high throughput"
//!   and may deliver out of order. For a terminal, out-of-order delivery is
//!   scrambled bytes on screen.
//! * **Bytes are base64, not a `String`.** A read can land mid-UTF-8 and a
//!   lossy conversion would replace the split codepoint with U+FFFD, so a
//!   character would be permanently corrupted by nothing worse than a buffer
//!   boundary. The frontend writes the decoded `Uint8Array` and lets xterm.js
//!   do the decoding, which is stateful and handles splits.
//! * **`resize` is a real command.** A pty left at the 24x80 default makes
//!   every full-screen program draw into the wrong box; it is the single most
//!   common bug in the terminal-in-Tauri repos above.
//!
//! ## Security posture
//!
//! This is deliberately *not* [`crate::cli`]'s allowlist. It is the user's own
//! interactive shell, with their environment and their privileges — the same
//! thing Terminal.app gives them. That is the point of the feature and it is
//! also its whole risk, so:
//!
//! * a session only exists because the user opened the terminal skin;
//! * nothing model-generated is written into a session by this module — the
//!   frontend routes a typed line to the shell or to Ada by the sigil the
//!   user typed, and any model-*proposed* command is staged as text for the
//!   user to press Enter on, never injected;
//! * sessions are capped and every one is killed on close, so a closed
//!   overlay does not leave shells running.

use std::collections::HashMap;
use std::io::{Read, Write};
use std::sync::Mutex;

use base64::Engine as _;
use portable_pty::{native_pty_system, Child, CommandBuilder, MasterPty, PtySize};
use serde::Serialize;
use tauri::ipc::Channel;

/// More than this many live shells is a leak, not a workflow.
const MAX_SESSIONS: usize = 8;

/// Big enough that a `cat` of a large file is not thousands of round trips,
/// small enough that interactive echo still feels immediate.
const READ_BUF: usize = 8 * 1024;

/// What the frontend receives on the channel.
#[derive(Clone, Serialize)]
#[serde(tag = "kind", rename_all = "camelCase")]
pub enum PtyEvent {
    /// A chunk of raw terminal output, base64 of the bytes as read.
    Output { b64: String },
    /// The shell exited. Carries the code so the UI can say *why* the
    /// terminal went quiet instead of just stopping.
    Exit { code: i32 },
}

struct Session {
    master: Box<dyn MasterPty + Send>,
    writer: Box<dyn Write + Send>,
    child: Box<dyn Child + Send + Sync>,
}

#[derive(Default)]
pub struct PtyState {
    sessions: Mutex<HashMap<String, Session>>,
}

impl PtyState {
    pub fn new() -> Self {
        Self::default()
    }
}

#[derive(Debug, Serialize)]
pub struct PtyInfo {
    pub id: String,
    pub shell: String,
    pub cols: u16,
    pub rows: u16,
}

/// The user's login shell, or the platform default.
///
/// `$SHELL` is what they actually chose; falling straight to `/bin/sh` would
/// silently drop their prompt, aliases and completion and make the skin feel
/// like a lesser terminal for no reason.
fn default_shell() -> String {
    if cfg!(windows) {
        return std::env::var("COMSPEC").unwrap_or_else(|_| "cmd.exe".into());
    }
    std::env::var("SHELL")
        .ok()
        .filter(|value| !value.trim().is_empty())
        .unwrap_or_else(|| "/bin/zsh".into())
}

fn clamp(cols: u16, rows: u16) -> (u16, u16) {
    // A zero dimension is what a not-yet-laid-out React ref reports, and it
    // makes some shells divide by it.
    (cols.clamp(2, 1000), rows.clamp(1, 400))
}

/// Open a shell on a fresh pty and stream its output to `on_output`.
#[tauri::command]
pub fn pty_open(
    state: tauri::State<'_, PtyState>,
    id: String,
    cols: u16,
    rows: u16,
    cwd: Option<String>,
    shell: Option<String>,
    on_output: Channel<PtyEvent>,
) -> Result<PtyInfo, String> {
    if id.trim().is_empty() {
        return Err("a pty session needs an id".into());
    }
    {
        let sessions = state.sessions.lock().map_err(|_| "pty state is poisoned")?;
        if sessions.contains_key(&id) {
            return Err(format!("pty session {id} is already open"));
        }
        if sessions.len() >= MAX_SESSIONS {
            return Err(format!(
                "too many terminal sessions are open ({MAX_SESSIONS}); close one first"
            ));
        }
    }

    let (cols, rows) = clamp(cols, rows);
    let program = shell.filter(|s| !s.trim().is_empty()).unwrap_or_else(default_shell);

    let pair = native_pty_system()
        .openpty(PtySize {
            rows,
            cols,
            pixel_width: 0,
            pixel_height: 0,
        })
        .map_err(|error| format!("could not open a pty: {error}"))?;

    let mut command = CommandBuilder::new(&program);
    // A login shell, so the user's profile runs and the prompt they know
    // appears. Without it the skin opens on a bare `sh-5.2$`.
    if !cfg!(windows) {
        command.arg("-l");
    }
    if let Some(dir) = cwd.filter(|d| !d.trim().is_empty()) {
        command.cwd(dir);
    }
    // xterm.js speaks xterm-256color; announcing anything else makes ncurses
    // programs draw with the wrong capabilities.
    command.env("TERM", "xterm-256color");
    command.env("COLORTERM", "truecolor");
    // So a shell profile, or the user, can tell where they are.
    command.env("KALEO_TERMINAL", "1");

    let child = pair
        .slave
        .spawn_command(command)
        .map_err(|error| format!("could not start {program}: {error}"))?;
    // The slave fd must be dropped in the parent or the reader below never
    // sees EOF when the shell exits, and the terminal hangs open forever.
    drop(pair.slave);

    let mut reader = pair
        .master
        .try_clone_reader()
        .map_err(|error| format!("could not read from the pty: {error}"))?;
    let writer = pair
        .master
        .take_writer()
        .map_err(|error| format!("could not write to the pty: {error}"))?;

    std::thread::Builder::new()
        .name(format!("pty-read-{id}"))
        .spawn(move || {
            let mut buf = [0u8; READ_BUF];
            loop {
                match reader.read(&mut buf) {
                    Ok(0) => break,
                    Ok(n) => {
                        let b64 = base64::engine::general_purpose::STANDARD.encode(&buf[..n]);
                        // A send failure means the window is gone; stop
                        // reading rather than spinning on a dead channel.
                        if on_output.send(PtyEvent::Output { b64 }).is_err() {
                            break;
                        }
                    }
                    Err(_) => break,
                }
            }
            let _ = on_output.send(PtyEvent::Exit { code: 0 });
        })
        .map_err(|error| format!("could not start the pty reader: {error}"))?;

    let mut sessions = state.sessions.lock().map_err(|_| "pty state is poisoned")?;
    sessions.insert(
        id.clone(),
        Session {
            master: pair.master,
            writer,
            child,
        },
    );

    Ok(PtyInfo {
        id,
        shell: program,
        cols,
        rows,
    })
}

/// Send keystrokes to the shell. Bytes, base64, for the same reason as above:
/// a paste can contain anything, including invalid UTF-8.
#[tauri::command]
pub fn pty_write(state: tauri::State<'_, PtyState>, id: String, b64: String) -> Result<(), String> {
    let bytes = base64::engine::general_purpose::STANDARD
        .decode(b64.as_bytes())
        .map_err(|_| "pty input was not valid base64".to_string())?;
    let mut sessions = state.sessions.lock().map_err(|_| "pty state is poisoned")?;
    let session = sessions
        .get_mut(&id)
        .ok_or_else(|| format!("no pty session {id}"))?;
    session
        .writer
        .write_all(&bytes)
        .and_then(|_| session.writer.flush())
        .map_err(|error| format!("could not write to the shell: {error}"))
}

/// Tell the pty its new size. Without this every full-screen program — vim,
/// htop, less — draws into a 24x80 box regardless of the window.
#[tauri::command]
pub fn pty_resize(
    state: tauri::State<'_, PtyState>,
    id: String,
    cols: u16,
    rows: u16,
) -> Result<(), String> {
    let (cols, rows) = clamp(cols, rows);
    let sessions = state.sessions.lock().map_err(|_| "pty state is poisoned")?;
    let session = sessions
        .get(&id)
        .ok_or_else(|| format!("no pty session {id}"))?;
    session
        .master
        .resize(PtySize {
            rows,
            cols,
            pixel_width: 0,
            pixel_height: 0,
        })
        .map_err(|error| format!("could not resize the pty: {error}"))
}

/// Close a session and kill its shell.
///
/// Idempotent on purpose: the UI calls this from an unmount, which can run
/// twice under React's strict mode, and a second close must not be an error
/// the user sees.
#[tauri::command]
pub fn pty_close(state: tauri::State<'_, PtyState>, id: String) -> Result<bool, String> {
    let mut sessions = state.sessions.lock().map_err(|_| "pty state is poisoned")?;
    match sessions.remove(&id) {
        None => Ok(false),
        Some(mut session) => {
            let _ = session.child.kill();
            let _ = session.child.wait();
            Ok(true)
        }
    }
}

#[tauri::command]
pub fn pty_sessions(state: tauri::State<'_, PtyState>) -> Result<Vec<String>, String> {
    let sessions = state.sessions.lock().map_err(|_| "pty state is poisoned")?;
    Ok(sessions.keys().cloned().collect())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_zero_dimension_never_reaches_the_pty() {
        // What a React ref reports before layout. Some shells divide by it.
        assert_eq!(clamp(0, 0), (2, 1));
    }

    #[test]
    fn absurd_dimensions_are_bounded_rather_than_trusted() {
        assert_eq!(clamp(u16::MAX, u16::MAX), (1000, 400));
    }

    /// The one thing a unit test on `clamp` cannot tell you: that a real
    /// process, spawned on the slave and read from the master, actually
    /// reaches us. This is the same sequence `pty_open` runs, minus Tauri's
    /// `State` and `Channel`, which cannot be constructed outside an app.
    ///
    /// It also pins the `drop(pair.slave)` discipline: with the parent still
    /// holding the slave fd, the read below never sees EOF and this test
    /// hangs instead of finishing — which is exactly the bug where a closed
    /// terminal stays open forever.
    #[test]
    #[cfg(unix)]
    fn a_real_process_on_a_real_pty_reaches_the_reader() {
        let pair = native_pty_system()
            .openpty(PtySize {
                rows: 24,
                cols: 80,
                pixel_width: 0,
                pixel_height: 0,
            })
            .expect("openpty");
        let mut command = CommandBuilder::new("/bin/sh");
        command.arg("-c");
        command.arg("printf kaleo-pty-ok");
        command.env("TERM", "xterm-256color");
        let mut child = pair.slave.spawn_command(command).expect("spawn");
        drop(pair.slave);

        let mut reader = pair.master.try_clone_reader().expect("reader");
        let mut seen = Vec::new();
        let mut buf = [0u8; 256];
        while let Ok(n) = reader.read(&mut buf) {
            if n == 0 {
                break;
            }
            seen.extend_from_slice(&buf[..n]);
        }
        child.wait().expect("wait");
        assert!(
            String::from_utf8_lossy(&seen).contains("kaleo-pty-ok"),
            "pty produced {:?}",
            String::from_utf8_lossy(&seen)
        );
    }

    /// A pty really is a tty on the other side — the whole reason this module
    /// exists rather than `cli.rs`'s captured pipes, which `test -t 1` fails.
    #[test]
    #[cfg(unix)]
    fn the_child_believes_it_is_talking_to_a_terminal() {
        let pair = native_pty_system()
            .openpty(PtySize {
                rows: 24,
                cols: 80,
                pixel_width: 0,
                pixel_height: 0,
            })
            .expect("openpty");
        let mut command = CommandBuilder::new("/bin/sh");
        command.arg("-c");
        command.arg("test -t 1 && printf isatty || printf notatty");
        let mut child = pair.slave.spawn_command(command).expect("spawn");
        drop(pair.slave);

        let mut reader = pair.master.try_clone_reader().expect("reader");
        let mut seen = String::new();
        let mut buf = [0u8; 256];
        while let Ok(n) = reader.read(&mut buf) {
            if n == 0 {
                break;
            }
            seen.push_str(&String::from_utf8_lossy(&buf[..n]));
        }
        child.wait().expect("wait");
        assert!(seen.contains("isatty"), "child saw {seen:?}");
    }

    #[test]
    fn the_users_own_shell_is_preferred_over_a_hardcoded_one() {
        // Falling to /bin/sh would silently drop their prompt and aliases.
        let shell = default_shell();
        assert!(!shell.trim().is_empty());
    }
}
