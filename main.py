import sys
import os
import ctypes
from pathlib import Path


def resource_path(*parts):
    """Resolve bundled resources for source runs and PyInstaller one-folder builds."""
    bundle_dir = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return bundle_dir.joinpath(*parts)

# Ensure 'src' directory is in Python module search path
SRC_DIR = Path(__file__).resolve().parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from PySide6.QtWidgets import QApplication, QDialog
from PySide6.QtGui import QIcon
from PySide6.QtCore import QTimer

# Import the Database init and the new Authentication Dialog
from database.db_manager import init_db
from ui.splash_screen import NeuroQuantSplashScreen
from ui.main_window import NeuroQuantMainWindow, AuthenticationDialog

def resolve_logo_path():
    """Locate the colored logo in assets/logo/ with or without file extensions."""
    possible_paths = [
        resource_path("assets", "logo", "colored.png"),
        resource_path("assets", "logo", "colored.jpg"),
        resource_path("assets", "logo", "colored.jpeg"),
        Path("assets/logo/colored.png"),
        Path("assets/logo/colored.jpg"),
        Path("assets/logo/colored.jpeg"),
        Path("assets/logo/colored"),
        Path(__file__).resolve().parent / "assets" / "logo" / "colored.png",
        Path(__file__).resolve().parent / "assets" / "logo" / "colored.jpg",
        Path(__file__).resolve().parent / "assets" / "logo" / "colored",
    ]
    for p in possible_paths:
        if p.exists() and p.is_file():
            return str(p)
    logo_dir = Path("assets/logo")
    if logo_dir.exists():
        for f in logo_dir.glob("colored*"):
            if f.is_file():
                return str(f)
    return None

def main():
    # Windows groups taskbar windows by this AppUserModelID. Set it before any
    # Qt windows are created so the process uses the NeuroQuant icon identity.
    if sys.platform == "win32":
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("NeuroQuant.NeuroOncology.Platform.6.0")
        except Exception:
            pass
    app = QApplication(sys.argv)
    # Keep the application alive while the splash screen hands off to the main window.
    app.setQuitOnLastWindowClosed(False)
    
    # 1. Initialize the SQLite database and users table first
    init_db()

    # Set application-wide OS and taskbar icon
    icon_path = resource_path("assets", "logo", "colored.ico")
    logo_path = resolve_logo_path()
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))
    elif logo_path:
        app.setWindowIcon(QIcon(logo_path))

    # 2. Force encrypted authentication BEFORE the splash screen appears
    login_prompt = AuthenticationDialog()
    if login_prompt.exec() != QDialog.Accepted:
        # If the user clicks exit, hits escape, or fails to login, close the app entirely.
        sys.exit(0)

    # FIX: Retrieve the username set by AuthenticationDialog or FirstRunSetupDialog
    authenticated_user = getattr(login_prompt, "authenticated_user", "admin") or "admin"
    cloud_password = getattr(login_prompt, "cloud_password", "")

    if getattr(login_prompt, "authenticated_role", "clinician") == "radiologist":
        from ui.radiologist_window import RadiologistUploadWindow
        app.setQuitOnLastWindowClosed(True)
        radiologist_window = RadiologistUploadWindow(login_prompt.radiologist_session)
        radiologist_window.show()
        return app.exec()

    # 3. If authentication is successful, proceed to the splash screen
    splash = NeuroQuantSplashScreen()
    splash.show()

    main_win = [None]

    # Staged 5.0-second pipeline steps (total = 5000 ms)
    def step_1():
        splash.set_progress(20, "Loading ONNX Runtime execution engine...")

    def step_2():
        splash.set_progress(42, "Calibrating 3D mesh rendering & multi-planar viewports...")

    def step_3():
        splash.set_progress(65, "Connecting to patient history database & WHO CNS profiling...")
        # FIX: Pass the authenticated user into NeuroQuantMainWindow
        main_win[0] = NeuroQuantMainWindow(authenticated_user=authenticated_user, cloud_password=cloud_password)

    def step_4():
        splash.set_progress(88, "Finalizing UI workspace & volumetric layers...")

    def step_finish():
        splash.set_progress(100, "System ready.")
        splash.close()
        if main_win[0]:
            QTimer.singleShot(0, main_win[0].start_workstation)

    # 5-second timeline: 1000ms -> 2200ms -> 3400ms -> 4300ms -> 5000ms
    QTimer.singleShot(1000, step_1)
    QTimer.singleShot(2200, step_2)
    QTimer.singleShot(3400, step_3)
    QTimer.singleShot(4300, step_4)
    QTimer.singleShot(5000, step_finish)

    sys.exit(app.exec())

if __name__ == "__main__":
    main()
