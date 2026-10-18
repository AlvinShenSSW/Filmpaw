//! Session credentials and the server's atomic transfer/exit admission check.

use crate::opener::{post_json, ApiError};
use crate::worker_process::WorkerProcess;
use std::time::Duration;

pub fn valid_session_token(token: &str) -> bool {
    token.len() == 64 && token.bytes().all(|b| b.is_ascii_hexdigit())
}

pub fn new_session_token() -> Result<String, getrandom::Error> {
    let mut bytes = [0u8; 32];
    getrandom::fill(&mut bytes)?;
    Ok(bytes.iter().map(|byte| format!("{byte:02x}")).collect())
}

#[derive(Clone, Debug, PartialEq, serde::Deserialize, serde::Serialize)]
#[serde(rename_all = "lowercase")]
pub enum ExitOperation {
    Transfer,
    Cleanup,
}

#[derive(Clone, Debug, PartialEq, serde::Deserialize, serde::Serialize)]
#[serde(deny_unknown_fields)]
pub struct ExitStatus {
    pub safe: bool,
    pub task_id: Option<String>,
    pub operation: Option<ExitOperation>,
}

pub fn prepare_exit(port: u16, token: &str, timeout: Duration) -> Result<ExitStatus, ApiError> {
    let payload = post_json(port, token, "/api/archive/prepare-exit", "{}", timeout)?;
    parse_exit_status(&payload)
}

/// Only the actual worker's captured OS identity can prove that it has died.
/// A launcher exit or an unreachable HTTP server is insufficient evidence.
pub fn prepare_exit_for_worker(
    worker: &WorkerProcess,
    port: u16,
    token: &str,
    timeout: Duration,
) -> Result<ExitStatus, ApiError> {
    match worker.is_running()? {
        false => Ok(ExitStatus {
            safe: true,
            task_id: None,
            operation: None,
        }),
        true => prepare_exit(port, token, timeout),
    }
}

#[derive(Default)]
pub struct StartupHandshake {
    port: Option<u16>,
    worker_pid: Option<u32>,
}

impl StartupHandshake {
    pub fn observe(&mut self, line: &str) -> Result<Option<(u16, u32)>, String> {
        if let Some(value) = line.trim().strip_prefix("FILMPAW_PORT=") {
            self.port = Some(
                value
                    .parse::<u16>()
                    .ok()
                    .filter(|v| *v != 0)
                    .ok_or_else(|| "invalid FILMPAW_PORT handshake".to_string())?,
            );
        }
        if let Some(value) = line.trim().strip_prefix("FILMPAW_PID=") {
            self.worker_pid = Some(
                value
                    .parse::<u32>()
                    .ok()
                    .filter(|v| *v != 0)
                    .ok_or_else(|| "invalid FILMPAW_PID handshake".to_string())?,
            );
        }
        Ok(self.port.zip(self.worker_pid))
    }
}

fn parse_exit_status(payload: &str) -> Result<ExitStatus, ApiError> {
    let status: ExitStatus = serde_json::from_str(payload)
        .map_err(|_| ApiError::local("无法确认传输状态，已保留窗口，请稍后重试"))?;
    // Only an explicit, internally consistent answer can permit destruction.
    if (status.safe && status.task_id.is_none() && status.operation.is_none())
        || (!status.safe && status.task_id.as_ref().is_some_and(|id| !id.is_empty()))
    {
        Ok(status)
    } else {
        Err(ApiError::local("传输状态异常，已保留窗口，请稍后重试"))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{BufRead, Read, Write};
    use std::net::{Ipv4Addr, TcpListener};
    use std::process::{Child, Command, Stdio};

    // The test binary's fixture is a real child process, never a sidecar: no
    // database or NAS access. The guard cleans it up even after a failed assert.
    struct TestChild(Child);
    impl Drop for TestChild {
        fn drop(&mut self) {
            let _ = self.0.kill();
            let _ = self.0.wait();
        }
    }
    fn fixture_child(mode: &str) -> TestChild {
        TestChild(
            Command::new(std::env::current_exe().unwrap())
                .args([
                    "--exact",
                    "lifecycle::tests::sidecar_child_fixture",
                    "--nocapture",
                ])
                .env("FILMPAW_TEST_CHILD_MODE", mode)
                .stdin(Stdio::piped())
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .spawn()
                .unwrap(),
        )
    }

    #[test]
    fn sidecar_child_fixture() {
        if std::env::var("FILMPAW_TEST_CHILD_MODE").as_deref() == Ok("wait") {
            let _ = std::io::stdin().read_exact(&mut [0u8]);
        }
    }

    #[cfg(windows)]
    #[test]
    fn a_confirmed_dead_worker_can_exit_without_a_working_server() {
        let mut child = fixture_child("wait");
        let worker = WorkerProcess::capture(child.0.id()).unwrap();
        child.0.kill().unwrap();
        let deadline = std::time::Instant::now() + Duration::from_secs(5);
        while child.0.try_wait().unwrap().is_none() {
            assert!(std::time::Instant::now() < deadline, "fixture did not exit");
            std::thread::sleep(Duration::from_millis(10));
        }
        let status = prepare_exit_for_worker(&worker, 0, "", Duration::from_millis(50)).unwrap();
        assert!(status.safe);
        assert!(status.task_id.is_none());
    }

    #[cfg(windows)]
    #[test]
    fn an_unreachable_but_living_worker_cannot_be_killed() {
        let mut child = fixture_child("wait");
        let worker = WorkerProcess::capture(child.0.id()).unwrap();
        let result =
            prepare_exit_for_worker(&worker, 0, &"a".repeat(64), Duration::from_millis(50));
        assert!(result.is_err());
        assert!(child.0.try_wait().unwrap().is_none());
    }

    #[cfg(windows)]
    #[test]
    fn exit_check_authenticates_and_never_treats_busy_as_safe() {
        let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).unwrap();
        let port = listener.local_addr().unwrap().port();
        let server = std::thread::spawn(move || {
            let (mut socket, _) = listener.accept().unwrap();
            let mut reader = std::io::BufReader::new(socket.try_clone().unwrap());
            let mut request = String::new();
            loop {
                let mut line = String::new();
                reader.read_line(&mut line).unwrap();
                let done = line == "\r\n";
                request.push_str(&line);
                if done {
                    break;
                }
            }
            let mut body = [0u8; 2];
            reader.read_exact(&mut body).unwrap();
            assert_eq!(&body, b"{}");
            socket.write_all(b"HTTP/1.1 200 OK\r\nConnection: close\r\n\r\n{\"safe\":false,\"task_id\":\"active-1\"}").unwrap();
            // Keep the fixture alive until the client has read our FIN, so
            // Windows does not race the response with a connection reset.
            socket
                .set_read_timeout(Some(Duration::from_secs(5)))
                .unwrap();
            socket.shutdown(std::net::Shutdown::Write).unwrap();
            let _ = reader.read_to_end(&mut Vec::new());
            request
        });
        let token = "a".repeat(64);
        let child = fixture_child("wait");
        let worker = WorkerProcess::capture(child.0.id()).unwrap();
        let result =
            prepare_exit_for_worker(&worker, port, &token, Duration::from_secs(1)).unwrap();
        assert!(!result.safe);
        assert_eq!(result.task_id.as_deref(), Some("active-1"));
        let request = server.join().unwrap();
        assert!(request.starts_with("POST /api/archive/prepare-exit HTTP/1.1\r\n"));
        assert!(request.contains(&format!("X-Filmpaw-Session: {token}\r\n")));
    }

    #[test]
    fn handshake_waits_for_both_port_and_actual_worker_identity() {
        let mut handshake = StartupHandshake::default();
        assert_eq!(handshake.observe("FILMPAW_PORT=12345\n").unwrap(), None);
        assert_eq!(handshake.observe("server log\n").unwrap(), None);
        assert_eq!(
            handshake.observe("FILMPAW_PID=67890\n").unwrap(),
            Some((12345, 67890))
        );
        assert!(StartupHandshake::default()
            .observe("FILMPAW_PID=0")
            .is_err());
        assert!(StartupHandshake::default()
            .observe("FILMPAW_PORT=0")
            .is_err());
        assert!(StartupHandshake::default()
            .observe("FILMPAW_PID=invalid")
            .is_err());
    }

    #[cfg(windows)]
    #[test]
    fn dead_wrapper_does_not_make_a_living_worker_safe_to_exit() {
        let mut wrapper = fixture_child("wait");
        let mut child = fixture_child("wait");
        let worker = WorkerProcess::capture(child.0.id()).unwrap();
        wrapper.0.kill().unwrap();
        wrapper.0.wait().unwrap();
        assert!(wrapper.0.try_wait().unwrap().is_some());
        assert!(
            prepare_exit_for_worker(&worker, 0, &"a".repeat(64), Duration::from_millis(50))
                .is_err()
        );
        assert!(worker.is_running().unwrap());
        assert!(child.0.try_wait().unwrap().is_none());
    }

    #[cfg(windows)]
    #[test]
    fn unknown_process_identity_fails_closed() {
        assert!(WorkerProcess::capture(0).is_err());
    }

    #[cfg(windows)]
    #[test]
    fn held_identity_only_terminates_the_captured_worker() {
        let mut child = fixture_child("wait");
        let mut unrelated = fixture_child("wait");
        let worker = WorkerProcess::capture(child.0.id()).unwrap();
        worker.terminate().unwrap();
        child.0.wait().unwrap();
        assert!(!worker.is_running().unwrap());
        // A repeated cleanup never opens or kills another PID.
        worker.terminate().unwrap();
        assert!(unrelated.0.try_wait().unwrap().is_none());
    }

    #[test]
    fn each_session_uses_a_fresh_os_random_credential() {
        let first = new_session_token().unwrap();
        let second = new_session_token().unwrap();
        assert!(valid_session_token(&first));
        assert!(valid_session_token(&second));
        assert_ne!(first, second);
        assert!(!valid_session_token(""));
        assert!(!valid_session_token(&format!("{}\r\n", "a".repeat(62))));
        assert!(valid_session_token(&"AbCdEf0123456789".repeat(4)));
    }

    #[test]
    fn only_explicit_idle_status_allows_exit() {
        assert!(
            parse_exit_status(r#"{"safe":true,"task_id":null}"#)
                .unwrap()
                .safe
        );
        assert!(
            !parse_exit_status(r#"{"safe":false,"task_id":"task-1"}"#)
                .unwrap()
                .safe
        );
        for payload in [
            "{}",
            "not-json",
            r#"{"safe":"true"}"#,
            r#"{"safe":true,"task_id":"task-1"}"#,
            r#"{"safe":false,"task_id":null}"#,
            r#"{"safe":false,"task_id":""}"#,
        ] {
            assert!(parse_exit_status(payload).is_err(), "{payload}");
        }
    }

    #[test]
    fn busy_exit_preserves_the_operation_that_owns_the_task() {
        for operation in ["cleanup", "transfer"] {
            let payload =
                format!(r#"{{"safe":false,"task_id":"active-1","operation":"{operation}"}}"#);
            let status = parse_exit_status(&payload).unwrap();
            assert!(!status.safe);
            assert_eq!(status.task_id.as_deref(), Some("active-1"));
            assert_eq!(
                serde_json::to_value(status).unwrap()["operation"],
                operation
            );
        }
    }

    #[test]
    fn legacy_busy_and_idle_responses_remain_compatible() {
        for payload in [
            r#"{"safe":false,"task_id":"legacy-1"}"#,
            r#"{"safe":false,"task_id":"legacy-1","operation":null}"#,
        ] {
            let status = parse_exit_status(payload).unwrap();
            assert!(!status.safe);
            assert_eq!(status.task_id.as_deref(), Some("legacy-1"));
        }
        for payload in [
            r#"{"safe":true,"task_id":null}"#,
            r#"{"safe":true,"task_id":null,"operation":null}"#,
        ] {
            assert!(parse_exit_status(payload).unwrap().safe);
        }
    }

    #[test]
    fn inconsistent_or_unknown_exit_operations_never_allow_close() {
        for payload in [
            r#"{"safe":true,"task_id":null,"operation":"cleanup"}"#,
            r#"{"safe":true,"task_id":null,"operation":"transfer"}"#,
            r#"{"safe":false,"task_id":null,"operation":"cleanup"}"#,
            r#"{"safe":false,"task_id":"","operation":"cleanup"}"#,
            r#"{"safe":false,"task_id":"active-1","operation":"unknown"}"#,
            r#"{"safe":true,"task_id":null,"operation":null,"extra":true}"#,
        ] {
            assert!(parse_exit_status(payload).is_err(), "{payload}");
        }
    }
}
