// Filmpaw Tauri shell: spawn the Python sidecar server, read FILMPAW_PORT
// from its stdout, expose the port to the UI via the `server_port` command,
// and kill the child on exit.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::io::{BufRead, BufReader};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use tauri::{Manager, State};

struct ServerState {
    port: u16,
    child: Mutex<Option<Child>>,
}

#[tauri::command]
fn server_port(state: State<ServerState>) -> u16 {
    state.port
}

/// Spawn the server. Dev: `uv run --project <server dir> filmpaw-server`.
/// Packaged (#7): the FILMPAW_SERVER_BIN env var / bundled sidecar path.
fn spawn_server() -> (Child, u16) {
    let (program, args): (String, Vec<String>) = match std::env::var("FILMPAW_SERVER_BIN") {
        Ok(bin) => (bin, vec![]),
        Err(_) => {
            let server_dir = std::env::current_dir()
                .expect("cwd")
                .parent()
                .expect("app dir")
                .join("server")
                .to_string_lossy()
                .into_owned();
            (
                "uv".into(),
                vec!["run".into(), "--project".into(), server_dir, "filmpaw-server".into()],
            )
        }
    };

    let mut child = Command::new(&program)
        .args(&args)
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .unwrap_or_else(|e| panic!("failed to spawn server ({program}): {e}"));

    let stdout = child.stdout.take().expect("child stdout piped");
    let mut reader = BufReader::new(stdout);
    let deadline = Instant::now() + Duration::from_secs(30);
    let mut line = String::new();
    let port = loop {
        line.clear();
        if Instant::now() > deadline {
            let _ = child.kill();
            panic!("server did not announce FILMPAW_PORT within 30s");
        }
        match reader.read_line(&mut line) {
            Ok(0) => {
                let _ = child.kill();
                panic!("server exited before announcing its port");
            }
            Ok(_) => {
                if let Some(rest) = line.trim().strip_prefix("FILMPAW_PORT=") {
                    match rest.parse::<u16>() {
                        Ok(p) => break p,
                        Err(e) => {
                            let _ = child.kill();
                            panic!("bad FILMPAW_PORT value {rest:?}: {e}");
                        }
                    }
                }
            }
            Err(e) => {
                let _ = child.kill();
                panic!("failed reading server stdout: {e}");
            }
        }
    };
    (child, port)
}

fn main() {
    let (child, port) = spawn_server();
    let state = ServerState {
        port,
        child: Mutex::new(Some(child)),
    };

    tauri::Builder::default()
        .manage(state)
        .invoke_handler(tauri::generate_handler![server_port])
        .setup(move |app| {
            // Inject the port before the page loads so api.ts can read it.
            if let Some(win) = app.get_webview_window("main") {
                let _ = win.eval(&format!("window.__FILMPAW_PORT__ = {port};"));
            }
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
                    let _ = child.kill();
                }
            }
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
