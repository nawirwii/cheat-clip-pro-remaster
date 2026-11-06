//#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::sync::{Arc, Mutex};
use tauri::Manager;
use tauri_plugin_shell::ShellExt;

fn main() {
    eprintln!("[main] Starting Cheat Clip Pro v0.0.72...");

    // v0.0.72: no behaviour change here (console string bump only; the
    // real fix lives in backend/video_engine.py + backend/services/render_service.py). Kept from v0.0.65:
    // keep the sidecar child handle so the backend can be killed
    // on exit. Before this, spawn() returned (_rx, _child) and _child was
    // discarded, with no exit handler at all — so closing the app window
    // left cheat-clip-pro-backend (plus its yt-dlp / ffmpeg children, which
    // Tauri never tracked) running as orphans, still holding the single
    // render-queue lock. That is exactly what the user's Task Manager
    // screenshots showed: no app window, but backend (2) + yt-dlp (2) +
    // ffmpeg still alive and downloading.
    let backend_child = Arc::new(Mutex::new(None));
    let backend_child_setup = backend_child.clone();

    let app = tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .setup(move |app| {
            eprintln!("[setup] Running setup...");
            let handle = app.handle().clone();

            // Start sidecar BEFORE window renders anything
            match handle.shell().sidecar("cheat-clip-pro-backend") {
                Ok(sidecar) => {
                    eprintln!("[setup] Sidecar found, spawning backend...");
                    match sidecar.spawn() {
                        Ok((_rx, child)) => {
                            eprintln!("[setup] Backend spawned successfully.");
                            // Store the handle instead of dropping it.
                            *backend_child_setup.lock().unwrap() = Some(child);
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
        .build(tauri::generate_context!())
        .expect("error while running Cheat Clip Pro");

    app.run(move |_app_handle, event| {
        if let tauri::RunEvent::Exit = event {
            // App is exiting: stop the backend AND its whole process tree.
            // child.kill() alone only kills the direct backend process;
            // yt-dlp and ffmpeg spawned by the backend would survive it,
            // so on Windows use taskkill /T (tree) first, then kill() as
            // a fallback. take() makes this run at most once.
            if let Some(child) = backend_child.lock().unwrap().take() {
                #[cfg(windows)]
                {
                    let pid = child.pid();
                    let pid_str = pid.to_string();
                    eprintln!("[exit] Stopping backend process tree (PID {})...", pid);
                    let _ = std::process::Command::new("taskkill")
                        .args(["/PID", pid_str.as_str(), "/T", "/F"])
                        .output();
                }
                let _ = child.kill();
                eprintln!("[exit] Backend stopped.");
            }
        }
    });
}
