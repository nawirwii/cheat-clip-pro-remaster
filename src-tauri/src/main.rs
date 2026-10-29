//#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use tauri::Manager;
use tauri_plugin_shell::ShellExt;

fn main() {
    eprintln!("[main] Starting Cheat Clip Pro v0.0.12...");

    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .setup(|app| {
            eprintln!("[setup] Running setup...");
            let handle = app.handle().clone();

            // Start sidecar BEFORE window renders anything
            match handle.shell().sidecar("cheat-clip-pro-backend") {
                Ok(sidecar) => {
                    eprintln!("[setup] Sidecar found, spawning backend...");
                    match sidecar.spawn() {
                        Ok((_rx, _child)) => {
                            eprintln!("[setup] Backend spawned successfully.");
                        }
                        Err(e) => {
                            eprintln!("[setup] Failed to spawn backend: {}", e);
                        }
                    }
                }
                Err(e) => {
                    eprintln!("[setup] Sidecar not found: {}", e);
                }
            }

            // Health check
            let mut backend_ready = false;
            for _i in 0..60u32 {
                if ureq::get("http://127.0.0.1:8000/api/health").call().is_ok() {
                    backend_ready = true;
                    eprintln!("[setup] Backend is healthy!");
                    break;
                }
                std::thread::sleep(std::time::Duration::from_millis(500));
            }

            if !backend_ready {
                eprintln!("[setup] WARNING: Backend did not become ready in time.");
            }

            // Show window
            if let Some(window) = handle.get_webview_window("main") {
                let _ = window.set_title("Cheat Clip Pro");
                if backend_ready {
                    let _ = window.navigate("http://127.0.0.1:8000".parse().unwrap());
                }
                let _ = window.show();
                eprintln!("[setup] Window shown.");
            } else {
                eprintln!("[setup] ERROR: main window not found!");
            }

            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running Cheat Clip Pro");
}
