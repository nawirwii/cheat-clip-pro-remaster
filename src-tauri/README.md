# Windows desktop packaging

The Windows workflow builds the frontend, creates a PyInstaller backend bundle,
then packages both with Tauri. The portable ZIP is assembled from the Tauri
bundle output and is uploaded to the GitHub Release.