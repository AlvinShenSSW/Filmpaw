// Filmpaw Tauri shell: spawn the Python sidecar server, read FILMPAW_PORT
// from its stdout, expose the port to the UI (init script + IPC command),
// and kill the child on exit.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::io::{BufRead, BufReader, Read};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::Duration;

use tauri::{Manager, WebviewUrl, WebviewWindowBuilder};

mod opener;
use opener::{ApiError, ShellLauncher, TIMEOUT};

struct ServerState {
    child: Mutex<Option<Child>>,
    port: u16,
}

/// The sidecar must still be alive before we ask it anything: after it dies the
/// port can be recycled by an unrelated process, and a stale port is exactly the
/// input we must not send an open request to.
fn live_port(state: &ServerState) -> Result<u16, ApiError> {
    let mut guard = state.child.lock().map_err(|_| ApiError {
        status: 0,
        detail: "本地服务状态不可用".into(),
    })?;
    match guard.as_mut() {
        // try_wait() -> Ok(None) means "still running".
        Some(child) => match child.try_wait() {
            Ok(None) => Ok(state.port),
            _ => Err(ApiError {
                status: 0,
                detail: "本地服务已退出, 请重启应用".into(),
            }),
        },
        None => Err(ApiError {
            status: 0,
            detail: "本地服务已退出, 请重启应用".into(),
        }),
    }
}

/// Open one performer folder. Takes an id, never a path — the server resolves
/// it, so the WebView cannot use this to open arbitrary directories (#31).
#[tauri::command]
async fn open_performer(
    state: tauri::State<'_, ServerState>,
    performer_id: String,
) -> Result<(), ApiError> {
    let port = live_port(&state)?;
    // Blocking socket I/O off the UI thread.
    tauri::async_runtime::spawn_blocking(move || {
        opener::open_performer_with(port, &performer_id, &ShellLauncher, TIMEOUT)
    })
    .await
    .map_err(|e| ApiError {
        status: 0,
        detail: format!("打开任务失败: {e}"),
    })?
}

/// Open the local folder and the performer folder together. Takes the subdir
/// NAME and an id — the approved anchor lives server-side (#31).
#[tauri::command]
async fn open_pair(
    state: tauri::State<'_, ServerState>,
    subdir: String,
    performer_id: String,
) -> Result<(), ApiError> {
    let port = live_port(&state)?;
    tauri::async_runtime::spawn_blocking(move || {
        opener::open_pair_with(port, &subdir, &performer_id, &ShellLauncher, TIMEOUT)
    })
    .await
    .map_err(|e| ApiError {
        status: 0,
        detail: format!("打开任务失败: {e}"),
    })?
}

/// Kill the sidecar and its whole process tree. In dev the server runs as
/// `uv run …` → python grandchild; child.kill() alone would orphan python.
fn kill_server_tree(child: &mut Child) {
    #[cfg(windows)]
    {
        let _ = Command::new("taskkill")
            .args(["/PID", &child.id().to_string(), "/T", "/F"])
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status();
    }
    let _ = child.kill();
}

/// Spawn the server. Resolution order:
/// 1. FILMPAW_SERVER_BIN env override (tests / custom setups)
/// 2. packaged: the bundled sidecar next to the app exe
/// 3. dev: `uv run --project <server dir> filmpaw-server`
fn server_command() -> (String, Vec<String>) {
    if let Ok(bin) = std::env::var("FILMPAW_SERVER_BIN") {
        return (bin, vec![]);
    }
    if !cfg!(debug_assertions) {
        // Packaged: the sidecar MUST sit next to the exe. Never fall back to
        // the dev `uv` path in release — it would mask a missing sidecar with
        // a confusing spawn failure on user machines.
        let exe = std::env::current_exe().expect("current_exe");
        let sidecar = exe.parent().expect("exe dir").join("filmpaw-server.exe");
        if !sidecar.exists() {
            panic!(
                "packaged sidecar filmpaw-server.exe not found next to {}",
                exe.display()
            );
        }
        return (sidecar.to_string_lossy().into_owned(), vec![]);
    }
    let server_dir = std::env::current_dir()
        .expect("cwd")
        .parent()
        .expect("app dir")
        .join("server")
        .to_string_lossy()
        .into_owned();
    (
        "uv".into(),
        vec![
            "run".into(),
            "--project".into(),
            server_dir,
            "filmpaw-server".into(),
        ],
    )
}

fn spawn_server() -> (Child, u16) {
    let (program, args) = server_command();

    let mut command = Command::new(&program);
    command.args(&args);
    // Dev-server CORS origins are trusted only when the shell says so.
    // Always set explicitly: a release build must override any inherited
    // FILMPAW_DEV=1 from the parent environment.
    command.env(
        "FILMPAW_DEV",
        if cfg!(debug_assertions) { "1" } else { "0" },
    );
    // The sidecar is a console-subsystem exe (PyInstaller default); spawned
    // from this GUI-subsystem shell Windows would allocate a visible console
    // window (#17). CREATE_NO_WINDOW suppresses it while keeping the stdout
    // pipe (the FILMPAW_PORT handshake) intact — chosen over PyInstaller
    // --noconsole, which detaches stdio and risks the handshake.
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        command.creation_flags(CREATE_NO_WINDOW);
    }
    let mut child = command
        .stdout(Stdio::piped())
        .stderr(Stdio::inherit())
        .spawn()
        .unwrap_or_else(|e| panic!("failed to spawn server ({program}): {e}"));

    let stdout = child.stdout.take().expect("child stdout piped");

    // Read stdout on a dedicated thread so the 30s startup deadline is
    // enforced even when the child produces NO output (a blocking read_line
    // on the main thread would never return to a deadline check). After the
    // port line, the same thread keeps draining stdout forever so a future
    // print() in the server can never fill the pipe buffer and block it.
    let (tx, rx) = std::sync::mpsc::channel::<Result<u16, String>>();
    std::thread::spawn(move || {
        let mut reader = BufReader::new(stdout);
        let mut line = String::new();
        loop {
            line.clear();
            match reader.read_line(&mut line) {
                Ok(0) => {
                    let _ = tx.send(Err("server exited before announcing its port".into()));
                    return;
                }
                Ok(_) => {
                    if let Some(rest) = line.trim().strip_prefix("FILMPAW_PORT=") {
                        let _ = tx.send(match rest.parse::<u16>() {
                            Ok(p) => Ok(p),
                            Err(e) => Err(format!("bad FILMPAW_PORT value {rest:?}: {e}")),
                        });
                        break;
                    }
                }
                Err(e) => {
                    let _ = tx.send(Err(format!("failed reading server stdout: {e}")));
                    return;
                }
            }
        }
        // Drain phase: swallow everything until EOF.
        let mut sink = Vec::with_capacity(4096);
        loop {
            sink.clear();
            match reader.by_ref().take(4096).read_to_end(&mut sink) {
                Ok(0) | Err(_) => return,
                Ok(_) => {}
            }
        }
    });

    let port = match rx.recv_timeout(Duration::from_secs(30)) {
        Ok(Ok(p)) => p,
        Ok(Err(msg)) => {
            let _ = child.kill();
            panic!("{msg}");
        }
        Err(_) => {
            let _ = child.kill();
            panic!("server did not announce FILMPAW_PORT within 30s");
        }
    };

    (child, port)
}

fn main() {
    let (child, port) = spawn_server();
    let state = ServerState {
        child: Mutex::new(Some(child)),
        port,
    };

    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .manage(state)
        .invoke_handler(tauri::generate_handler![open_performer, open_pair])
        .setup(move |app| {
            // Init script runs before page scripts on EVERY page load — no
            // injection race, unlike a one-shot eval after window creation.
            WebviewWindowBuilder::new(app, "main", WebviewUrl::default())
                .title("Filmpaw")
                .inner_size(1200.0, 800.0)
                .min_inner_size(960.0, 640.0)
                .initialization_script(format!("window.__FILMPAW_PORT__ = {port};"))
                .build()?;
            Ok(())
        })
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::Destroyed = event {
                let child = window
                    .state::<ServerState>()
                    .child
                    .lock()
                    .ok()
                    .and_then(|mut guard| guard.take());
                if let Some(mut child) = child {
                    kill_server_tree(&mut child);
                }
            }
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
