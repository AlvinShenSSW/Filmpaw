//! Opening folders from the SHELL instead of the sidecar (#31).
//!
//! Windows only lets the foreground process, the process that received the last
//! input event, or a process started by the foreground process activate a
//! window. When the user clicks, those rights belong to `filmpaw.exe` — but the
//! `ShellExecuteW` call used to happen in the sidecar, a separate background
//! process holding none of them, so Explorer opened *behind* the app and merely
//! blinked in the taskbar. Launching from here fixes that.
//!
//! The server stays the validation authority: these functions send the SAME
//! intent parameters the UI used to send over HTTP (an id, a subdir name — never
//! a path), ask the server to resolve them, and open only what it returns. A
//! generic `open_path(path)` command would have let the WebView bypass the
//! server's containment guard entirely, so it deliberately does not exist.
//!
//! The HTTP client is hand-rolled over `TcpStream` on purpose: it can only ever
//! talk to `127.0.0.1:<handshake port>` and it has no redirect support to
//! disable — properties this needs, and that a general-purpose client would only
//! give us by configuration.

use std::io::{Read, Write};
use std::net::{Ipv4Addr, SocketAddr, SocketAddrV4, TcpStream};
use std::time::Duration;

/// Mirrors the server's `{status, detail}` so the existing UI error mapping
/// (`detailOf(e)` reads `.detail`) keeps working unchanged across `invoke`.
#[derive(Debug, Clone, PartialEq, serde::Serialize)]
pub struct ApiError {
    pub status: u16,
    pub detail: String,
}

impl ApiError {
    fn local(detail: impl Into<String>) -> Self {
        // 0 = never reached the server (connect/timeout/malformed reply). The UI
        // shows `detail`; the status only distinguishes local from server-sent.
        Self {
            status: 0,
            detail: detail.into(),
        }
    }
}

/// Total budget for connect + write + read. The command runs off the UI thread,
/// but a hung share must still not wedge the click forever.
pub const TIMEOUT: Duration = Duration::from_secs(5);

/// Injection seam: tests count calls and force failures without opening windows.
pub trait Launcher {
    fn open(&self, path: &str) -> Result<(), String>;
}

/// POST to the sidecar and return the response body.
///
/// `Connection: close` makes the body end at EOF, so no chunked/keep-alive
/// parsing is needed. Any non-2xx — including a 3xx, which is never followed —
/// comes back as an `ApiError`.
pub fn post_json(port: u16, path: &str, body: &str, timeout: Duration) -> Result<String, ApiError> {
    let addr = SocketAddr::V4(SocketAddrV4::new(Ipv4Addr::LOCALHOST, port));
    let mut stream = TcpStream::connect_timeout(&addr, timeout)
        .map_err(|e| ApiError::local(format!("无法连接本地服务: {e}")))?;
    stream
        .set_read_timeout(Some(timeout))
        .and_then(|_| stream.set_write_timeout(Some(timeout)))
        .map_err(|e| ApiError::local(format!("本地服务连接超时设置失败: {e}")))?;

    let req = format!(
        "POST {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nContent-Type: application/json\r\n\
         Content-Length: {len}\r\nConnection: close\r\n\r\n{body}",
        len = body.len()
    );
    stream
        .write_all(req.as_bytes())
        .map_err(|e| ApiError::local(format!("本地服务请求失败: {e}")))?;

    let mut raw = Vec::new();
    stream
        .read_to_end(&mut raw)
        .map_err(|e| ApiError::local(format!("本地服务无响应: {e}")))?;
    let text = String::from_utf8_lossy(&raw).into_owned();

    let (head, payload) = text
        .split_once("\r\n\r\n")
        .ok_or_else(|| ApiError::local("本地服务返回了不完整的响应"))?;
    let status: u16 = head
        .split_whitespace()
        .nth(1)
        .and_then(|s| s.parse().ok())
        .ok_or_else(|| ApiError::local("本地服务返回了无法解析的状态行"))?;

    if !(200..300).contains(&status) {
        let detail = serde_json::from_str::<serde_json::Value>(payload)
            .ok()
            .and_then(|v| v.get("detail").and_then(|d| d.as_str().map(str::to_owned)))
            .unwrap_or_else(|| format!("server 返回 {status}"));
        return Err(ApiError { status, detail });
    }
    Ok(payload.to_owned())
}

fn field(payload: &str, key: &str) -> Result<String, ApiError> {
    serde_json::from_str::<serde_json::Value>(payload)
        .ok()
        .and_then(|v| v.get(key).and_then(|s| s.as_str().map(str::to_owned)))
        .filter(|s| !s.is_empty())
        .ok_or_else(|| ApiError::local(format!("本地服务返回的响应缺少 {key}")))
}

/// Percent-encode a path segment. Performer ids are UUIDs today, but the id
/// reaches us from the WebView — it must never be able to alter the request line.
fn encode_segment(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for b in s.bytes() {
        match b {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'.' | b'_' | b'~' => {
                out.push(b as char)
            }
            _ => out.push_str(&format!("%{b:02X}")),
        }
    }
    out
}

/// Open one performer folder. Returns Err only when the SERVER rejected or was
/// unreachable — a launch that fails afterwards is logged and swallowed, keeping
/// the fire-and-forget behaviour the UI already relies on.
pub fn open_performer_with<L: Launcher>(
    port: u16,
    performer_id: &str,
    launcher: &L,
    timeout: Duration,
) -> Result<(), ApiError> {
    let path = format!("/api/performers/{}/resolve", encode_segment(performer_id));
    let payload = post_json(port, &path, "", timeout)?;
    let target = field(&payload, "performer_path")?;
    if let Err(e) = launcher.open(&target) {
        eprintln!("[filmpaw] could not open {target}: {e}");
    }
    Ok(())
}

/// Open the local folder and the performer folder together. Both launches are
/// attempted: one failing must not cost the user the other window.
pub fn open_pair_with<L: Launcher>(
    port: u16,
    subdir: &str,
    performer_id: &str,
    launcher: &L,
    timeout: Duration,
) -> Result<(), ApiError> {
    let body = serde_json::json!({ "subdir": subdir, "performer_id": performer_id }).to_string();
    let payload = post_json(port, "/api/resolve-pair", &body, timeout)?;
    let local = field(&payload, "local_path")?;
    let performer = field(&payload, "performer_path")?;
    for target in [&local, &performer] {
        if let Err(e) = launcher.open(target) {
            eprintln!("[filmpaw] could not open {target}: {e}");
        }
    }
    Ok(())
}

/// Real launcher: `ShellExecuteW`, the wide-character API.
///
/// NOT `explorer.exe <path>`: explorer treats a COMMA as an argument separator,
/// so `…\沙月恵奈,月野かすみ` opened the wrong folder (#28).
pub struct ShellLauncher;

impl Launcher for ShellLauncher {
    #[cfg(windows)]
    fn open(&self, path: &str) -> Result<(), String> {
        use std::os::windows::ffi::OsStrExt;
        use windows_sys::Win32::UI::Shell::ShellExecuteW;
        use windows_sys::Win32::UI::WindowsAndMessaging::SW_SHOWNORMAL;

        let wide: Vec<u16> = std::ffi::OsStr::new(path)
            .encode_wide()
            .chain(std::iter::once(0))
            .collect();
        let verb: Vec<u16> = "open\0".encode_utf16().collect();
        // SAFETY: both pointers are NUL-terminated UTF-16 buffers that outlive
        // the call; the remaining arguments are documented-null.
        let rc = unsafe {
            ShellExecuteW(
                std::ptr::null_mut(),
                verb.as_ptr(),
                wide.as_ptr(),
                std::ptr::null(),
                std::ptr::null(),
                SW_SHOWNORMAL,
            )
        };
        // ShellExecuteW returns >32 on success (it is an HINSTANCE-shaped error
        // code, not a handle).
        if rc as isize > 32 {
            Ok(())
        } else {
            Err(format!("ShellExecuteW 返回 {}", rc as isize))
        }
    }

    #[cfg(not(windows))]
    fn open(&self, path: &str) -> Result<(), String> {
        std::process::Command::new(if cfg!(target_os = "macos") {
            "open"
        } else {
            "xdg-open"
        })
        .arg(path)
        .spawn()
        .map(|_| ())
        .map_err(|e| e.to_string())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::BufRead;
    use std::net::TcpListener;
    use std::sync::{Arc, Mutex};

    /// Records what would have been opened; can be told to fail for chosen paths.
    #[derive(Default)]
    struct FakeLauncher {
        opened: Arc<Mutex<Vec<String>>>,
        fail_containing: Option<String>,
    }

    impl Launcher for FakeLauncher {
        fn open(&self, path: &str) -> Result<(), String> {
            self.opened.lock().unwrap().push(path.to_owned());
            match &self.fail_containing {
                Some(needle) if path.contains(needle.as_str()) => Err("boom".into()),
                _ => Ok(()),
            }
        }
    }

    fn fake(fail_containing: Option<&str>) -> (FakeLauncher, Arc<Mutex<Vec<String>>>) {
        let opened = Arc::new(Mutex::new(Vec::new()));
        (
            FakeLauncher {
                opened: Arc::clone(&opened),
                fail_containing: fail_containing.map(str::to_owned),
            },
            opened,
        )
    }

    /// One-shot server returning `response` verbatim. Returns (port, joinhandle
    /// yielding the request the client sent).
    fn serve(response: &'static str) -> (u16, std::thread::JoinHandle<String>) {
        serve_delayed(response, Duration::ZERO)
    }

    fn serve_delayed(
        response: &'static str,
        delay: Duration,
    ) -> (u16, std::thread::JoinHandle<String>) {
        let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).unwrap();
        let port = listener.local_addr().unwrap().port();
        let handle = std::thread::spawn(move || {
            let (mut sock, _) = listener.accept().unwrap();
            let mut reader = std::io::BufReader::new(sock.try_clone().unwrap());
            let mut request = String::new();
            loop {
                let mut line = String::new();
                if reader.read_line(&mut line).unwrap_or(0) == 0 {
                    break;
                }
                let done = line == "\r\n";
                request.push_str(&line);
                if done {
                    break;
                }
            }
            // Read the body when the client announced one.
            if let Some(len) = request
                .to_lowercase()
                .split("content-length:")
                .nth(1)
                .and_then(|r| r.lines().next())
                .and_then(|v| v.trim().parse::<usize>().ok())
            {
                let mut body = vec![0u8; len];
                let _ = reader.read_exact(&mut body);
                request.push_str(&String::from_utf8_lossy(&body));
            }
            std::thread::sleep(delay);
            let _ = sock.write_all(response.as_bytes());
            request
        });
        (port, handle)
    }

    fn http(status: &str, body: &str) -> String {
        format!(
            "HTTP/1.1 {status}\r\nContent-Type: application/json\r\nConnection: close\r\n\r\n{body}"
        )
    }

    // ---------------------------------------------------------------- happy

    #[test]
    fn single_open_launches_the_path_the_server_returned() {
        let (port, h) = serve(Box::leak(
            http(
                "200 OK",
                r#"{"performer_path":"\\\\Ant\\Video Station\\Lisa"}"#,
            )
            .into_boxed_str(),
        ));
        let (l, opened) = fake(None);
        assert!(open_performer_with(port, "abc-123", &l, TIMEOUT).is_ok());
        let req = h.join().unwrap();
        assert!(
            req.starts_with("POST /api/performers/abc-123/resolve HTTP/1.1"),
            "{req}"
        );
        assert_eq!(*opened.lock().unwrap(), vec![r"\\Ant\Video Station\Lisa"]);
    }

    #[test]
    fn pair_sends_only_intent_parameters_and_opens_both() {
        let (port, h) = serve(Box::leak(
            http(
                "200 OK",
                r#"{"local_path":"C:\\Downloads\\写真\\胡桃さくら,新井リマ","performer_path":"\\\\Koala\\女优 I\\胡桃さくら"}"#,
            )
            .into_boxed_str(),
        ));
        let (l, opened) = fake(None);
        assert!(open_pair_with(port, "胡桃さくら,新井リマ", "pid-9", &l, TIMEOUT).is_ok());
        let req = h.join().unwrap();
        assert!(req.starts_with("POST /api/resolve-pair HTTP/1.1"), "{req}");
        // The shell must never send a PATH — only the subdir name and the id.
        assert!(
            req.contains(r#""subdir""#) && req.contains(r#""performer_id":"pid-9""#),
            "{req}"
        );
        assert!(!req.contains("local_dir"), "{req}");
        // Comma + non-ASCII survive verbatim into the launcher (#28 regression).
        assert_eq!(
            *opened.lock().unwrap(),
            vec![
                r"C:\Downloads\写真\胡桃さくら,新井リマ",
                r"\\Koala\女优 I\胡桃さくら"
            ]
        );
    }

    #[test]
    fn performer_id_is_percent_encoded_into_the_request_line() {
        let (port, h) = serve(Box::leak(
            http("200 OK", r#"{"performer_path":"C:\\x"}"#).into_boxed_str(),
        ));
        let (l, _) = fake(None);
        let _ = open_performer_with(port, "a b/../c", &l, TIMEOUT);
        let req = h.join().unwrap();
        assert!(
            req.starts_with("POST /api/performers/a%20b%2F..%2Fc/resolve"),
            "{req}"
        );
    }

    // ------------------------------------------------------- server rejects

    #[test]
    fn server_status_and_detail_reach_the_ui_unchanged() {
        for (status, code, detail) in [
            ("404 Not Found", 404u16, "表演者不存在"),
            ("409 Conflict", 409, "该记录已失效, 无法双开"),
            ("400 Bad Request", 400, "本地目录不存在 — 请重新选择"),
            (
                "422 Unprocessable Entity",
                422,
                "Extra inputs are not permitted",
            ),
        ] {
            let body = format!(r#"{{"detail":"{detail}"}}"#);
            let (port, _h) = serve(Box::leak(http(status, &body).into_boxed_str()));
            let (l, opened) = fake(None);
            let err = open_performer_with(port, "x", &l, TIMEOUT).unwrap_err();
            assert_eq!(
                err,
                ApiError {
                    status: code,
                    detail: detail.to_owned()
                }
            );
            assert!(
                opened.lock().unwrap().is_empty(),
                "nothing may be launched on {code}"
            );
        }
    }

    #[test]
    fn redirects_are_never_followed() {
        let (port, _h) =
            serve("HTTP/1.1 302 Found\r\nLocation: http://evil/\r\nConnection: close\r\n\r\n");
        let (l, opened) = fake(None);
        let err = open_performer_with(port, "x", &l, TIMEOUT).unwrap_err();
        assert_eq!(err.status, 302);
        assert!(opened.lock().unwrap().is_empty());
    }

    // -------------------------------------------------- malformed / no server

    #[test]
    fn malformed_or_incomplete_responses_never_launch() {
        let cases: [&'static str; 5] = [
            "HTTP/1.1 200 OK\r\nConnection: close\r\n\r\nnot json",
            "HTTP/1.1 200 OK\r\nConnection: close\r\n\r\n{\"wrong_key\":\"C:\\\\x\"}",
            "HTTP/1.1 200 OK\r\nConnection: close\r\n\r\n{\"performer_path\":123}",
            "HTTP/1.1 200 OK\r\nConnection: close\r\n\r\n{\"performer_path\":\"\"}",
            "garbage without a header break",
        ];
        for resp in cases {
            let (port, _h) = serve(resp);
            let (l, opened) = fake(None);
            let err = open_performer_with(port, "x", &l, TIMEOUT).unwrap_err();
            assert_eq!(err.status, 0, "{resp}");
            assert!(opened.lock().unwrap().is_empty(), "{resp}");
        }
    }

    #[test]
    fn dead_sidecar_fails_without_launching() {
        // Bind then drop: the port is now nobody's.
        let port = TcpListener::bind((Ipv4Addr::LOCALHOST, 0))
            .unwrap()
            .local_addr()
            .unwrap()
            .port();
        let (l, opened) = fake(None);
        let err = open_performer_with(port, "x", &l, Duration::from_millis(500)).unwrap_err();
        assert_eq!(err.status, 0);
        assert!(err.detail.contains("无法连接本地服务"), "{}", err.detail);
        assert!(opened.lock().unwrap().is_empty());
    }

    #[test]
    fn a_hanging_server_times_out_without_launching() {
        let (port, _h) = serve_delayed(
            Box::leak(http("200 OK", r#"{"performer_path":"C:\\x"}"#).into_boxed_str()),
            Duration::from_secs(3),
        );
        let (l, opened) = fake(None);
        let err = open_performer_with(port, "x", &l, Duration::from_millis(300)).unwrap_err();
        assert_eq!(err.status, 0);
        assert!(opened.lock().unwrap().is_empty());
    }

    // ------------------------------------------------------ launcher failure

    #[test]
    fn a_failed_launch_still_returns_ok_and_still_tries_the_second_target() {
        let (port, _h) = serve(Box::leak(
            http(
                "200 OK",
                r#"{"local_path":"C:\\local","performer_path":"\\\\nas\\perf"}"#,
            )
            .into_boxed_str(),
        ));
        let (l, opened) = fake(Some("C:\\local")); // first one blows up
                                                   // Server validation succeeded, so the click is NOT reported as an error.
        assert!(open_pair_with(port, "sub", "pid", &l, TIMEOUT).is_ok());
        assert_eq!(*opened.lock().unwrap(), vec![r"C:\local", r"\\nas\perf"]);
    }
}
