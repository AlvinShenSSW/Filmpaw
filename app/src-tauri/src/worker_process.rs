//! Hold the real Python worker's identity, independently of its launcher.
//!
//! Both `uv run` and PyInstaller onefile can leave a surviving worker when the
//! immediate Child exits. On Windows an owned process handle names the same
//! kernel process throughout its lifetime; we never reopen a numeric PID when
//! deciding whether it is safe to exit or which process may be terminated.

use crate::opener::ApiError;

pub struct WorkerProcess {
    #[cfg(windows)]
    handle: std::os::windows::io::OwnedHandle,
}

impl WorkerProcess {
    pub fn capture(pid: u32) -> Result<Self, ApiError> {
        #[cfg(windows)]
        {
            use std::os::windows::io::FromRawHandle;
            use windows_sys::Win32::System::Threading::{
                OpenProcess, PROCESS_QUERY_LIMITED_INFORMATION, PROCESS_SYNCHRONIZE,
                PROCESS_TERMINATE,
            };
            // SAFETY: OpenProcess returns a new owned handle; it is converted
            // exactly once and automatically closed by OwnedHandle.
            let raw = unsafe {
                OpenProcess(
                    PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_SYNCHRONIZE | PROCESS_TERMINATE,
                    0,
                    pid,
                )
            };
            if raw.is_null() {
                return Err(ApiError::local("无法锁定本地服务进程身份，请重启应用"));
            }
            Ok(Self {
                handle: unsafe { std::os::windows::io::OwnedHandle::from_raw_handle(raw) },
            })
        }
        #[cfg(not(windows))]
        {
            let _ = pid;
            Err(ApiError::local("当前平台不支持可靠的服务进程身份检查"))
        }
    }

    pub fn is_running(&self) -> Result<bool, ApiError> {
        #[cfg(windows)]
        {
            use std::os::windows::io::AsRawHandle;
            use windows_sys::Win32::Foundation::{WAIT_OBJECT_0, WAIT_TIMEOUT};
            use windows_sys::Win32::System::Threading::WaitForSingleObject;
            // SAFETY: this is a live owned process handle with SYNCHRONIZE
            // access. A zero timeout only inspects, never blocks the UI.
            match unsafe { WaitForSingleObject(self.handle.as_raw_handle(), 0) } {
                WAIT_OBJECT_0 => Ok(false),
                WAIT_TIMEOUT => Ok(true),
                _ => Err(ApiError::local("无法确认本地服务是否仍在运行，已保留窗口")),
            }
        }
        #[cfg(not(windows))]
        Err(ApiError::local("当前平台不支持可靠的服务进程身份检查"))
    }

    /// Only the shell's authorized window-destruction path calls this, after
    /// the worker's admission check has allowed exit (or it has already died).
    pub fn terminate(&self) -> Result<(), ApiError> {
        if !self.is_running()? {
            return Ok(());
        }
        #[cfg(windows)]
        {
            use std::os::windows::io::AsRawHandle;
            use windows_sys::Win32::System::Threading::TerminateProcess;
            // SAFETY: the owned handle continues to name the captured worker,
            // even if the wrapper died or another process reused a PID.
            if unsafe { TerminateProcess(self.handle.as_raw_handle(), 0) } == 0 {
                return Err(ApiError::local("无法结束本地服务进程"));
            }
            Ok(())
        }
        #[cfg(not(windows))]
        Err(ApiError::local("当前平台不支持可靠的服务进程身份检查"))
    }
}
