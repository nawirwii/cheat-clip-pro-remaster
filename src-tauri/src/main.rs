//#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::sync::{Arc, Mutex};
use tauri::Manager;
use tauri_plugin_shell::ShellExt;

fn main() {
    eprintln!("[main] Starting Cheat Clip Pro v0.0.75...");

    // v0.0.74: the backend readiness wait no longer blocks the main thread
    // (see setup below — the window is shown immediately and a watcher
    // thread navigates it once the backend answers). Kept from v0.0.65:
    // keep the sidecar child handle so the backend can be killed on exit.
    // Before this, spawn() returned (_rx, _child) and _child was discarded,
    // with no exit handler at all — so closing the app window left
    // cheat-clip-pro-backend (plus its yt-dlp / ffmpeg children, which
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

            // v0.0.74: DO NOT block the main thread waiting for the backend.
            // The old code polled /api/health in a 30 s loop right here, so
            // the window could not pump messages and Windows labelled it
            // "Not Responding" — and if the backend needed more than 30 s
            // (cold start of a ~369 MB onefile bundle under Defender), the
            // window opened blank with no navigation at all. Now the window
            // is shown immediately (below) and a background watcher thread
            // polls the backend and navigates the window once it is healthy.
            let backend_spawned = backend_child_setup.lock().unwrap().is_some();
            if backend_spawned {
                let watcher_handle = handle.clone();
                let watcher_child = backend_child_setup.clone();
                std::thread::spawn(move || {
                    // Read the backend PID once, for the crash check below.
                    let backend_pid = watcher_child
                        .lock()
                        .ok()
                        .and_then(|guard| guard.as_ref().map(|child| child.pid()));

                    let mut backend_ready = false;
                    // 240 x 500 ms = up to 120 s: a cold onefile start under
                    // Windows Defender can take well over the old 30 s.
                    for i in 0..240u32 {
                        if ureq::get("http://127.0.0.1:8000/api/health")
                            .call()
                            .is_ok()
                        {
                            backend_ready = true;
                            eprintln!("[setup] Backend is healthy!");
                            break;
                        }
                        // Every ~10 s: progress line + crash check (the child
                        // handle exposes no try_wait, so ask the OS whether
                        // the PID is still alive). If the backend died on its
                        // own, stop waiting and say so instead of sitting
                        // silent for the full timeout.
                        if i % 20 == 19 {
                            eprintln!(
                                "[setup] Masih menunggu backend... ({} dtk)",
                                (i + 1) / 2
                            );
                            if let Some(pid) = backend_pid {
                                if !pid_alive(pid) {
                                    eprintln!("[setup] ERROR: Proses backend berhenti sendiri saat startup (PID {} tidak lagi berjalan) — aplikasi tidak akan termuat. Coba tutup lalu buka lagi aplikasinya.", pid);
                                    return;
                                }
                            }
                        }
                        std::thread::sleep(std::time::Duration::from_millis(500));
                    }

                    if backend_ready {
                        if let Some(window) = watcher_handle.get_webview_window("main") {
                            let _ = window.navigate("http://127.0.0.1:8000".parse().unwrap());
                        } else {
                            eprintln!("[setup] ERROR: main window not found!");
                        }
                    } else {
                        eprintln!("[setup] WARNING: Backend tidak siap dalam 120 detik. Jendela tetap terbuka tapi aplikasi mungkin kosong — coba tutup lalu buka lagi. Jika terus gagal, periksa apakah port 8000 dipakai aplikasi lain.");
                    }
                });
            } else {
                eprintln!("[setup] ERROR: Backend tidak berjalan (gagal di-spawn) — jendela dibuka tanpa aplikasi.");
            }

            // Show window immediately — it stays responsive while the
            // watcher thread waits for the backend in the background.
            if let Some(window) = handle.get_webview_window("main") {
                let _ = window.set_title("Cheat Clip Pro");
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

/// Is a process with this PID still running? Used by the startup watcher
/// to notice a backend that died on its own. (Windows only — other
/// platforms always answer "yes" so the watcher keeps waiting.)
#[cfg(windows)]
fn pid_alive(pid: u32) -> bool {
    let pid_str = pid.to_string();
    let pid_filter = format!("PID eq {}", pid);
    match std::process::Command::new("tasklist")
        .args(["/FI", pid_filter.as_str(), "/NH"])
        .output()
    {
        Ok(out) => {
            let text = String::from_utf8_lossy(&out.stdout);
            text.contains(pid_str.as_str())
        }
        // If tasklist itself fails, assume alive and keep waiting.
        Err(_) => true,
    }
}

#[cfg(not(windows))]
fn pid_alive(_pid: u32) -> bool {
    true
}
