//! Allowlisted command-line tools Ada may run on this machine.
//!
//! The overlay is a desktop app sitting on a checkout: when something already
//! exists as a CLI (`python -m googleapps auth`, `kicad-cli`, …) the app should
//! be able to call it rather than reimplementing it. This module is the only
//! place that spawns processes for that — argv form only, no shell, and only
//! the named tools below. Arbitrary user strings never become a program name.

use serde::Serialize;
use std::env;
use std::ffi::OsString;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::Duration;

/// Hard ceiling so a hung `kicad-cli` cannot pin the overlay forever.
const DEFAULT_TIMEOUT_SECS: u64 = 300;

#[derive(Debug, Serialize)]
pub struct CliResult {
    pub tool: String,
    pub argv: Vec<String>,
    pub status: i32,
    pub stdout: String,
    pub stderr: String,
}

#[derive(Debug, Serialize)]
pub struct CliToolInfo {
    pub id: String,
    pub available: bool,
    pub detail: String,
}

fn repository_root() -> Result<PathBuf, String> {
    let candidate = env::var_os("ADA_REPO_ROOT")
        .filter(|value| !value.is_empty())
        .map(PathBuf::from)
        .or_else(|| env::var_os("SILKSCREEN_ROOT").filter(|v| !v.is_empty()).map(PathBuf::from))
        .unwrap_or_else(|| PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../.."));
    let root = candidate
        .canonicalize()
        .map_err(|error| format!("could not resolve the Hardy repository root: {error}"))?;
    if !root.join("pyproject.toml").is_file() {
        return Err(format!(
            "{} is not a Hardy checkout (pyproject.toml is missing); set ADA_REPO_ROOT",
            root.display()
        ));
    }
    Ok(root)
}

fn python_interpreter(root: &Path) -> Result<OsString, String> {
    if let Some(value) = env::var_os("ADA_PYTHON")
        .or_else(|| env::var_os("SILKSCREEN_PYTHON"))
        .filter(|value| !value.is_empty())
    {
        return Ok(value);
    }
    for relative in [".venv/bin/python", ".venv/Scripts/python.exe"] {
        let candidate = root.join(relative);
        if candidate.is_file() {
            return Ok(candidate.into_os_string());
        }
    }
    Err("Hardy's Python environment is missing; run ./scripts/install.sh or set ADA_PYTHON".into())
}

fn which(name: &str) -> Option<PathBuf> {
    let path = env::var_os("PATH")?;
    for dir in env::split_paths(&path) {
        let candidate = dir.join(name);
        if candidate.is_file() {
            return Some(candidate);
        }
        #[cfg(windows)]
        {
            let exe = dir.join(format!("{name}.exe"));
            if exe.is_file() {
                return Some(exe);
            }
        }
    }
    None
}

fn kicad_cli_path() -> Option<PathBuf> {
    if let Some(value) = env::var_os("KICAD_CLI").filter(|v| !v.is_empty()) {
        let path = PathBuf::from(value);
        if path.is_file() {
            return Some(path);
        }
    }
    if let Some(found) = which("kicad-cli") {
        return Some(found);
    }
    #[cfg(target_os = "macos")]
    {
        let mac = PathBuf::from(
            "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli",
        );
        if mac.is_file() {
            return Some(mac);
        }
    }
    #[cfg(windows)]
    {
        let win = PathBuf::from(
            r"C:\Program Files\KiCad\8.0\bin\kicad-cli.exe",
        );
        if win.is_file() {
            return Some(win);
        }
    }
    None
}

fn reject_unsafe_arg(arg: &str) -> Result<(), String> {
    // argv exec never goes through a shell, but refuse NUL and absurd length
    // so a bad caller cannot fill a pipe forever.
    if arg.contains('\0') {
        return Err("CLI argument must not contain a NUL byte".into());
    }
    if arg.len() > 8_192 {
        return Err("CLI argument is too long".into());
    }
    Ok(())
}

/// Tools Ada may invoke. The id is what the frontend passes as `tool`.
fn resolve_invocation(
    tool: &str,
    args: &[String],
) -> Result<(OsString, Vec<OsString>, PathBuf), String> {
    for arg in args {
        reject_unsafe_arg(arg)?;
    }
    let root = repository_root()?;
    match tool {
        "googleapps" | "silkscreen" | "python" => {
            let python = python_interpreter(&root)?;
            let mut argv: Vec<OsString> = Vec::new();
            match tool {
                "googleapps" => {
                    argv.push("-m".into());
                    argv.push("googleapps".into());
                }
                "silkscreen" => {
                    argv.push("-m".into());
                    argv.push("silkscreen".into());
                }
                "python" => {}
                _ => unreachable!(),
            }
            for arg in args {
                argv.push(arg.into());
            }
            // `python` with no module must not become an open interpreter.
            if tool == "python" && args.is_empty() {
                return Err("python requires a script or -m module argument".into());
            }
            if tool == "python" {
                let head = args.first().map(String::as_str).unwrap_or("");
                if head != "-m" && !head.ends_with(".py") {
                    return Err(
                        "python CLI is limited to `-m <module>` or a `.py` script".into(),
                    );
                }
            }
            Ok((python, argv, root))
        }
        "kicad-cli" => {
            let bin = kicad_cli_path().ok_or_else(|| {
                "kicad-cli not found; install KiCad or set KICAD_CLI".to_string()
            })?;
            let argv: Vec<OsString> = args.iter().map(|a| a.into()).collect();
            Ok((bin.into_os_string(), argv, root))
        }
        other => Err(format!(
            "unknown CLI tool '{other}'; allowed: googleapps, silkscreen, python, kicad-cli"
        )),
    }
}

fn load_dotenv_into(cmd: &mut Command, root: &Path) {
    // Mirror the CLIs: the service does not read .env, but `python -m googleapps`
    // / `silkscreen` do. When Ada spawns them it should see the same file.
    let path = root.join(".env");
    let Ok(text) = std::fs::read_to_string(&path) else {
        return;
    };
    for raw in text.lines() {
        let line = raw.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        let Some((key, value)) = line.split_once('=') else {
            continue;
        };
        let key = key.trim();
        if key.is_empty() || env::var_os(key).is_some() {
            // Process env wins over .env, same as a shell that already exported.
            continue;
        }
        let mut value = value.trim().to_string();
        if (value.starts_with('"') && value.ends_with('"'))
            || (value.starts_with('\'') && value.ends_with('\''))
        {
            value = value[1..value.len() - 1].to_string();
        }
        cmd.env(key, value);
    }
}

fn run_command(
    program: OsString,
    args: Vec<OsString>,
    root: &Path,
    timeout_secs: u64,
) -> Result<(i32, String, String), String> {
    use std::io::Read;
    use std::thread;

    let mut cmd = Command::new(&program);
    cmd.args(&args)
        .current_dir(root)
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped());
    load_dotenv_into(&mut cmd, root);

    let mut child = cmd
        .spawn()
        .map_err(|error| format!("could not start {}: {error}", program.to_string_lossy()))?;

    let mut stdout_pipe = child
        .stdout
        .take()
        .ok_or_else(|| "child stdout was not piped".to_string())?;
    let mut stderr_pipe = child
        .stderr
        .take()
        .ok_or_else(|| "child stderr was not piped".to_string())?;

    let stdout_thread = thread::spawn(move || {
        let mut buf = Vec::new();
        let _ = stdout_pipe.read_to_end(&mut buf);
        String::from_utf8_lossy(&buf).into_owned()
    });
    let stderr_thread = thread::spawn(move || {
        let mut buf = Vec::new();
        let _ = stderr_pipe.read_to_end(&mut buf);
        String::from_utf8_lossy(&buf).into_owned()
    });

    let started = std::time::Instant::now();
    let timeout = Duration::from_secs(timeout_secs.max(1));
    let status = loop {
        match child.try_wait() {
            Ok(Some(status)) => break status,
            Ok(None) => {
                if started.elapsed() > timeout {
                    let _ = child.kill();
                    let _ = child.wait();
                    return Err(format!(
                        "{} timed out after {timeout_secs}s",
                        program.to_string_lossy()
                    ));
                }
                thread::sleep(Duration::from_millis(50));
            }
            Err(error) => return Err(format!("could not wait on child: {error}")),
        }
    };

    let stdout = stdout_thread
        .join()
        .unwrap_or_else(|_| String::from("(stdout reader panicked)"));
    let stderr = stderr_thread
        .join()
        .unwrap_or_else(|_| String::from("(stderr reader panicked)"));
    Ok((status.code().unwrap_or(-1), stdout, stderr))
}

#[tauri::command]
pub fn list_cli_tools() -> Vec<CliToolInfo> {
    let root = repository_root();
    let python = root.as_ref().ok().and_then(|r| python_interpreter(r).ok());
    vec![
        CliToolInfo {
            id: "googleapps".into(),
            available: python.is_some()
                && root
                    .as_ref()
                    .map(|r| r.join("googleapps").is_dir())
                    .unwrap_or(false),
            detail: "python -m googleapps (auth, check, run) — loads .env from the checkout"
                .into(),
        },
        CliToolInfo {
            id: "silkscreen".into(),
            available: python.is_some(),
            detail: "python -m silkscreen — board CLI".into(),
        },
        CliToolInfo {
            id: "python".into(),
            available: python.is_some(),
            detail: match &python {
                Some(p) => format!("interpreter {}", p.to_string_lossy()),
                None => "venv missing; set ADA_PYTHON".into(),
            },
        },
        CliToolInfo {
            id: "kicad-cli".into(),
            available: kicad_cli_path().is_some(),
            detail: kicad_cli_path()
                .map(|p| p.display().to_string())
                .unwrap_or_else(|| "not found".into()),
        },
    ]
}

#[tauri::command]
pub fn run_cli(
    tool: String,
    args: Vec<String>,
    timeout_secs: Option<u64>,
) -> Result<CliResult, String> {
    let timeout = timeout_secs.unwrap_or(DEFAULT_TIMEOUT_SECS);
    let (program, argv, root) = resolve_invocation(&tool, &args)?;
    let display_argv: Vec<String> = std::iter::once(program.to_string_lossy().into_owned())
        .chain(argv.iter().map(|a| a.to_string_lossy().into_owned()))
        .collect();
    let (status, stdout, stderr) = run_command(program, argv, &root, timeout)?;
    Ok(CliResult {
        tool,
        argv: display_argv,
        status,
        stdout,
        stderr,
    })
}
