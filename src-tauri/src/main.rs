#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use tauri::Manager;
use tauri_plugin_shell::ShellExt;

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .setup(|app| {
            let handle = app.handle().clone();

            // Start sidecar BEFORE window renders anything
            let sidecar = handle.shell().sidecar("cheat-clip-pro-backend")
                .expect("backend sidecar is not bundled");
            let (_rx, _child) = sidecar.spawn()
                .expect("failed to start backend sidecar");

            // Synchronous health check — block window until backend is ready
            let mut backend_ready = false;
            for _i in 0..60u32 {
                if ureq::get("http://127.0.0.1:8000/api/health").call().is_ok() {
                    backend_ready = true;
                    break;
                }
                std::thread::sleep(std::time::Duration::from_millis(500));
            }

            // Show window only after backend is confirmed healthy
            if let Some(window) = handle.get_webview_window("main") {
                let _ = window.set_title("Cheat Clip Pro");
                if backend_ready {
                    let _ = window.navigate("http://127.0.0.1:8000".parse().unwrap());
                }
                let _ = window.show();
            } else {
                eprintln!("ERROR: main window not found at startup");
            }

            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running Cheat Clip Pro");
}
