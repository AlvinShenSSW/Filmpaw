// Filmpaw Tauri shell: spawn the Python sidecar server, read FILMPAW_PORT
// from its stdout, expose the port to the UI (init script + IPC command),
// and kill the child on exit.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::io::{BufRead, BufReader, Read};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use tauri::{Manager, State, WebviewUrl, WebviewWindowBuilder};

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
        .stderr(Stdio::inherit())
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

    // Keep draining stdout forever so a future print() in the server can
    // never fill the pipe buffer and block the whole process.
    std::thread::spawn(move || {
        let mut sink = Vec::with_capacity(4096);
        loop {
            sink.clear();
            match reader.by_ref().take(4096).read_to_end(&mut sink) {
                Ok(0) | Err(_) => break,
                Ok(_) => {}
            }
        }
    });

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
            // Init script runs before page scripts on EVERY page load — no
            // injection race, unlike a one-shot eval after window creation.
            WebviewWindowBuilder::new(app, "main", WebviewUrl::default())
                .title("Filmpaw")
                .inner_size(1200.0, 800.0)
                .min_inner_size(960.0, 640.0)
                .initialization_script(&format!("window.__FILMPAW_PORT__ = {port};"))
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
                    let _ = child.kill();
                }
            }
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
