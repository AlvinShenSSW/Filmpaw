// Filmpaw Tauri shell: spawn the Python sidecar server, read FILMPAW_PORT
// and the actual worker PID from stdout, expose the port to the UI, and
// coordinate safe exit with that worker even if its launcher exits first.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::io::{BufRead, BufReader, Read};
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Mutex;
use std::time::Duration;

use tauri::{Emitter, Manager, WebviewUrl, WebviewWindowBuilder};

mod lifecycle;
mod opener;
mod worker_process;
use lifecycle::ExitStatus;
use opener::{ApiError, ShellLauncher, TIMEOUT};
use worker_process::WorkerProcess;

struct ServerState {
    child: Mutex<Option<Child>>,
    port: u16,
    session_token: String,
    close_check: AtomicBool,
    worker: WorkerProcess,
}

/// The sidecar must still be alive before we ask it anything: after it dies the
/// port can be recycled by an unrelated process, and a stale port is exactly the
/// input we must not send an open request to.
fn live_port(state: &ServerState) -> Result<u16, ApiError> {
    match state.worker.is_running()? {
        true => Ok(state.port),
        false => Err(ApiError {
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
    let token = state.session_token.clone();
    // Blocking socket I/O off the UI thread.
    tauri::async_runtime::spawn_blocking(move || {
        opener::open_performer_with(port, &token, &performer_id, &ShellLauncher, TIMEOUT)
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
    let token = state.session_token.clone();
    tauri::async_runtime::spawn_blocking(move || {
        opener::open_pair_with(
            port,
            &token,
            &subdir,
            &performer_id,
            &ShellLauncher,
            TIMEOUT,
        )
    })
    .await
    .map_err(|e| ApiError {
        status: 0,
        detail: format!("打开任务失败: {e}"),
    })?
}

/// The server closes task admission under its worker lock before saying safe.
/// A prior GET of the latest task cannot provide that guarantee: a transfer
/// could otherwise start between the GET and killing the sidecar.
#[tauri::command]
async fn close_if_idle(window: tauri::Window) -> Result<ExitStatus, ApiError> {
    let state = window.state::<ServerState>();
    if state.close_check.swap(true, Ordering::SeqCst) {
        return Err(ApiError::local("正在确认传输状态，请稍候"));
    }
    let result = async {
        let app = window.app_handle().clone();
        let status = tauri::async_runtime::spawn_blocking(move || {
            let state = app.state::<ServerState>();
            lifecycle::prepare_exit_for_worker(
                &state.worker,
                state.port,
                &state.session_token,
                TIMEOUT,
            )
        })
        .await
        .map_err(|e| ApiError::local(format!("无法确认传输状态: {e}")))??;
        if status.safe {
            window
                .destroy()
                .map_err(|e| ApiError::local(format!("关闭窗口失败: {e}")))?;
        }
        Ok(status)
    }
    .await;
    state.close_check.store(false, Ordering::SeqCst);
    result
}

#[derive(Clone, serde::Serialize)]
struct ClosePrompt {
    task_id: Option<String>,
    operation: Option<lifecycle::ExitOperation>,
    detail: Option<String>,
}

/// Kill the sidecar and its whole process tree. In dev the server runs as
/// `uv run …` → python grandchild; child.kill() alone would orphan python.
fn kill_server_tree(child: &mut Child) {
    // An exited child's numeric PID may already have been reused. Never issue
    // taskkill for a process whose exit we have positively observed.
    if !matches!(child.try_wait(), Ok(None)) {
        return;
    }
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

fn spawn_server(session_token: &str) -> (Child, u16, WorkerProcess) {
    let (program, args) = server_command();

    let mut command = Command::new(&program);
    command.args(&args);
    command.env("FILMPAW_SESSION_TOKEN", session_token);
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
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::inherit())
        .spawn()
        .unwrap_or_else(|e| panic!("failed to spawn server ({program}): {e}"));

    let stdout = child.stdout.take().expect("child stdout piped");

    // Read stdout on a dedicated thread so the 30s startup deadline is
    // enforced even when the child produces NO output (a blocking read_line
    // on the main thread would never return to a deadline check). After the
    // port/PID handshake, the same thread keeps draining stdout so a future
    // print() in the server can never fill the pipe buffer and block it.
    let (tx, rx) = std::sync::mpsc::channel::<Result<(u16, WorkerProcess), String>>();
    std::thread::spawn(move || {
        let mut reader = BufReader::new(stdout);
        let mut line = String::new();
        let mut handshake = lifecycle::StartupHandshake::default();
        loop {
            line.clear();
            match reader.read_line(&mut line) {
                Ok(0) => {
                    let _ = tx.send(Err(
                        "server exited before announcing its port and worker PID".into(),
                    ));
                    return;
                }
                Ok(_) => match handshake.observe(&line) {
                    Ok(Some((port, pid))) => {
                        let _ = tx.send(
                            WorkerProcess::capture(pid)
                                .map(|worker| (port, worker))
                                .map_err(|e| e.detail),
                        );
                        break;
                    }
                    Ok(None) => {}
                    Err(error) => {
                        let _ = tx.send(Err(error));
                        return;
                    }
                },
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

    let (port, worker) = match rx.recv_timeout(Duration::from_secs(30)) {
        Ok(Ok(result)) => result,
        Ok(Err(msg)) => {
            kill_server_tree(&mut child);
            panic!("{msg}");
        }
        Err(_) => {
            kill_server_tree(&mut child);
            panic!("server did not announce FILMPAW_PORT and FILMPAW_PID within 30s");
        }
    };

    (child, port, worker)
}

fn main() {
    let session_token = lifecycle::new_session_token().expect("OS random session credential");
    let (child, port, worker) = spawn_server(&session_token);
    let initialization = format!(
        "window.__FILMPAW_PORT__ = {port}; window.__FILMPAW_SESSION_TOKEN__ = {};",
        serde_json::to_string(&session_token).expect("session JSON")
    );
    let state = ServerState {
        child: Mutex::new(Some(child)),
        port,
        session_token,
        close_check: AtomicBool::new(false),
        worker,
    };

    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .manage(state)
        .invoke_handler(tauri::generate_handler![
            open_performer,
            open_pair,
            close_if_idle
        ])
        .setup(move |app| {
            // Init script runs before page scripts on EVERY page load — no
            // injection race, unlike a one-shot eval after window creation.
            WebviewWindowBuilder::new(app, "main", WebviewUrl::default())
                .title("Filmpaw")
                .inner_size(1200.0, 800.0)
                .min_inner_size(960.0, 640.0)
                .initialization_script(initialization)
                .build()?;
            Ok(())
        })
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();
                let window = window.clone();
                tauri::async_runtime::spawn(async move {
                    let prompt = match close_if_idle(window.clone()).await {
                        Ok(status) if status.safe => return,
                        Ok(status) => ClosePrompt {
                            task_id: status.task_id,
                            operation: status.operation,
                            detail: None,
                        },
                        Err(error) => ClosePrompt {
                            task_id: None,
                            operation: None,
                            detail: Some(error.detail),
                        },
                    };
                    let _ = window.emit("filmpaw-close-requested", prompt);
                });
            }
            if let tauri::WindowEvent::Destroyed = event {
                // The captured worker remains identifiable even if uv or the
                // PyInstaller bootloader exited before the window did.
                let _ = window.state::<ServerState>().worker.terminate();
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
