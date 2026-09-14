use std::os::windows::process::CommandExt;
use std::net::{SocketAddr, TcpStream};
use std::process::{Child, Command};
use std::sync::Mutex;
use std::thread;
use std::time::Duration;
use tauri::{Manager, WindowEvent};
use serde::Serialize;
use std::fs;
use std::path::PathBuf;

#[derive(Serialize)]
struct FileDialogResult {
    path: String,
    content: String,
}

#[tauri::command]
fn open_flowsheet_file() -> Result<Option<FileDialogResult>, String> {
    let Some(path) = rfd::FileDialog::new()
        .add_filter("BioAgri flowsheet", &["bioagri.json", "json"])
        .add_filter("JSON", &["json"])
        .pick_file()
    else {
        return Ok(None);
    };
    let content = fs::read_to_string(&path)
        .map_err(|e| format!("Could not read {}: {e}", path.display()))?;
    Ok(Some(FileDialogResult {
        path: path.to_string_lossy().to_string(),
        content,
    }))
}

#[tauri::command]
fn save_flowsheet_file(path: Option<String>, suggested_name: String, content: String) -> Result<Option<String>, String> {
    let target = if let Some(existing) = path.filter(|p| !p.trim().is_empty()) {
        PathBuf::from(existing)
    } else {
        let safe_name = if suggested_name.trim().is_empty() { "Untitled flowsheet".to_string() } else { suggested_name };
        let filename = if safe_name.to_lowercase().ends_with(".json") { safe_name } else { format!("{safe_name}.bioagri.json") };
        let Some(selected) = rfd::FileDialog::new()
            .add_filter("BioAgri flowsheet", &["bioagri.json", "json"])
            .set_file_name(filename)
            .save_file()
        else {
            return Ok(None);
        };
        selected
    };
    fs::write(&target, content)
        .map_err(|e| format!("Could not save {}: {e}", target.display()))?;
    Ok(Some(target.to_string_lossy().to_string()))
}



struct BackendProcess(Mutex<Option<Child>>);

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![open_flowsheet_file, save_flowsheet_file])
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
