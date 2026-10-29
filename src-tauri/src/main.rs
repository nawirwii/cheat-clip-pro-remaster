#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use tauri::Manager;
use tauri_plugin_shell::ShellExt;

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .setup(|app| {
            let handle = app.handle().clone();
            tauri::async_runtime::spawn(async move {
                let sidecar = handle.shell().sidecar("cheat-clip-pro-backend")
                    .expect("backend sidecar is not bundled");
                let (_rx, _child) = sidecar.spawn().expect("failed to start backend sidecar");
                for _ in 0..60 {
                    if ureq::get("http://127.0.0.1:8000/api/health").call().is_ok() {
                        if let Some(window) = handle.get_webview_window("main") {
                            let _ = window.navigate("http://127.0.0.1:8000".parse().unwrap());
                        }
                        break;
                    }
                    std::thread::sleep(std::time::Duration::from_millis(500));
                }
            });
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running Cheat Clip Pro");
}