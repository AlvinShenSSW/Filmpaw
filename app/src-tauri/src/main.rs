// Filmpaw Tauri shell: spawn the Python sidecar server, read FILMPAW_PORT
// from its stdout, expose the port to the UI (init script + IPC command),
// and kill the child on exit.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::io::{BufRead, BufReader, Read};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::Duration;

use tauri::{Manager, WebviewUrl, WebviewWindowBuilder};

struct ServerState {
    child: Mutex<Option<Child>>,
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
    };

    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .manage(state)
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
                    kill_server_tree(&mut child);
                }
            }
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
