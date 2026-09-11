use std::os::windows::process::CommandExt;
use std::net::{SocketAddr, TcpStream};
use std::process::{Child, Command};
use std::sync::Mutex;
use std::thread;
use std::time::Duration;
use tauri::{Manager, WindowEvent};

struct BackendProcess(Mutex<Option<Child>>);

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_http::init())
        .plugin(tauri_plugin_process::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .setup(|app| {
            let backend_path = app.path().resource_dir()?.join("backend").join("bioagri-backend.exe");
            let child = Command::new(&backend_path)
                .current_dir(backend_path.parent().expect("backend resource directory"))
                .creation_flags(0x08000000)
                .spawn()
                .map_err(|error| format!("Could not start {}: {error}", backend_path.display()))?;
            app.manage(BackendProcess(Mutex::new(Some(child))));
            let address: SocketAddr = "127.0.0.1:47831".parse().expect("valid backend address");
            let mut ready = false;
            for _ in 0..200 {
                if TcpStream::connect_timeout(&address, Duration::from_millis(150)).is_ok() {
                    ready = true;
                    break;
                }
                thread::sleep(Duration::from_millis(150));
            }
            if !ready {
                return Err("The BioAgri calculation engine did not become ready.".into());
            }
            Ok(())
        })
        .on_window_event(|window, event| {
            if matches!(event, WindowEvent::Destroyed) {
                if let Some(state) = window.try_state::<BackendProcess>() {
                    if let Ok(mut guard) = state.0.lock() {
                        if let Some(mut child) = guard.take() {
                            let _ = Command::new("taskkill")
                                .args(["/PID", &child.id().to_string(), "/T", "/F"])
                                .creation_flags(0x08000000)
                                .status();
                            let _ = child.kill();
                            let _ = child.wait();
                        }
                    }
                }
            }
        })
        .run(tauri::generate_context!())
        .expect("error while running BioAgri Process Simulator");
}
