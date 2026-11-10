"""Build NeuroQuant and, optionally, its Inno Setup installer for Windows."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
APP_NAME = "NeuroQuant"
APP_VERSION = "6.0"
PROJECT_PYTHON = ROOT / "venv" / "Scripts" / "python.exe"
REQUIRED_INPUTS = (
    ROOT / "main.py",
    ROOT / "NeuroQuant.spec",
    ROOT / "assets" / "logo" / "colored.ico",
    ROOT / "data" / "models" / "neuroquant_3dunet.onnx",
    ROOT / "data" / "models" / "neuroquant_3dunet.onnx.data",
)
REQUIRED_MODULES = (
    "PySide6", "numpy", "SimpleITK", "matplotlib", "onnxruntime", "skimage", "scipy",
    "firebase_admin", "google.cloud.firestore",
)
INNO_CANDIDATES = (
    Path(r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe"),
    Path(r"C:\Program Files\Inno Setup 6\ISCC.exe"),
)


def selected_python() -> str:
    return str(PROJECT_PYTHON) if PROJECT_PYTHON.is_file() else sys.executable


def check_inputs() -> None:
    missing = [path for path in REQUIRED_INPUTS if not path.is_file()]
    if missing:
        formatted = "\n".join(f"  - {path.relative_to(ROOT)}" for path in missing)
        raise SystemExit(f"Required build inputs are missing:\n{formatted}")


def check_pyinstaller() -> None:
    python = selected_python()
    try:
        subprocess.run(
            [python, "-m", "PyInstaller", "--version"],
            cwd=ROOT,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("PyInstaller is missing; installing it in the selected build environment...")
        try:
            subprocess.run(
                [python, "-m", "pip", "install", "PyInstaller>=6.11,<7"],
                cwd=ROOT,
                check=True,
            )
            subprocess.run(
                [python, "-m", "PyInstaller", "--version"], cwd=ROOT, check=True,
                stdout=subprocess.DEVNULL,
            )
        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
            raise SystemExit(
                "Could not install PyInstaller. Check that this Python environment has pip "
                "and internet access, then install PyInstaller and rerun build.py."
            ) from exc


def check_runtime_modules() -> None:
    python = selected_python()
    probe = """
import importlib.util, sys
missing = []
for name in sys.argv[1:]:
    try:
        if importlib.util.find_spec(name) is None:
            missing.append(name)
    except (ImportError, ModuleNotFoundError):
        missing.append(name)
print("\\n".join(missing))
"""
    def find_missing() -> list[str]:
        result = subprocess.run(
            [python, "-c", probe, *REQUIRED_MODULES], cwd=ROOT, text=True,
            capture_output=True, check=True,
        )
        return [name for name in result.stdout.splitlines() if name.strip()]

    missing = find_missing()
    if missing:
        requirements = ROOT / "requirements.txt"
        print(f"Installing application packages from {requirements.name}...")
        try:
            subprocess.run(
                [python, "-m", "pip", "install", "-r", str(requirements)],
                cwd=ROOT, check=True,
            )
            missing = find_missing()
        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
            raise SystemExit(
                "Could not install application dependencies. Check pip and internet access, "
                "then run 'python -m pip install -r requirements.txt'."
            ) from exc
    if missing:
        names = ", ".join(missing)
        raise SystemExit(
            f"Required application packages are missing from {python}: {names}. "
            "Install the application's dependencies into that environment before building."
        )


def clean_build_artifacts() -> None:
    # Preserve NeuroQuant.spec: it is a maintained project configuration file.
    for path in (ROOT / "build", ROOT / "dist" / APP_NAME):
        if path.is_dir():
            print(f"Removing {path.relative_to(ROOT)}")
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()


def build(*, clean: bool = True) -> Path:
    check_inputs()
    check_pyinstaller()
    check_runtime_modules()
    python = selected_python()

    if clean:
        clean_build_artifacts()

    command = [
        python,
        "-m",
        "PyInstaller",
        "--clean",
        "--noconfirm",
        "--distpath",
        str(ROOT / "dist"),
        "--workpath",
        str(ROOT / "build"),
        str(ROOT / "NeuroQuant.spec"),
    ]

    print("Building NeuroQuant one-folder distribution...")
    subprocess.run(command, cwd=ROOT, check=True)

    output_dir = ROOT / "dist" / APP_NAME
    executable = output_dir / f"{APP_NAME}.exe"
    if not executable.is_file():
        raise SystemExit(f"PyInstaller completed but expected executable was not found: {executable}")

    # Copy local configuration beside the executable; it is intentionally not bundled.
    env_file = ROOT / ".env"
    if env_file.is_file():
        shutil.copy2(env_file, output_dir / env_file.name)
        print("Copied .env beside the executable. Keep this file private.")

    print(f"Build complete: {output_dir}")
    return output_dir


def find_inno_compiler() -> Path | None:
    configured = os.environ.get("INNO_SETUP_ISCC")
    if configured:
        path = Path(configured).expanduser()
        if path.is_file():
            return path
    on_path = shutil.which("ISCC.exe") or shutil.which("ISCC")
    if on_path:
        return Path(on_path)
    return next((path for path in INNO_CANDIDATES if path.is_file()), None)


def build_inno_installer() -> Path:
    script = ROOT / "installer.iss"
    if not script.is_file():
        raise SystemExit(f"Inno Setup script was not found: {script}")
    compiler = find_inno_compiler()
    if compiler is None:
        raise SystemExit(
            "The app was built, but Inno Setup Compiler (ISCC.exe) was not found. "
            "Install Inno Setup 6, or set INNO_SETUP_ISCC to the full path of ISCC.exe, "
            "then rerun 'python build.py --installer'."
        )

    print(f"Creating version {APP_VERSION} installer with Inno Setup...")
    try:
        subprocess.run(
            [str(compiler), f"/DAppVersion={APP_VERSION}", str(script)],
            cwd=ROOT,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        raise SystemExit(
            f"Inno Setup failed with exit code {exc.returncode}. Check that the app build exists "
            "and review the compiler messages above."
        ) from exc

    output = ROOT / "Output" / f"NeuroQuant_Installer_{APP_VERSION}.exe"
    if not output.is_file():
        raise SystemExit(f"Inno Setup completed but the expected installer was not found: {output}")
    print(f"Installer complete: {output}")
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-clean", action="store_true", help="Keep previous PyInstaller build intermediates")
    parser.add_argument("--installer", action="store_true", help="Compile installer.iss after building the app (requires Inno Setup 6)")
    args = parser.parse_args()

    if sys.platform != "win32":
        raise SystemExit("The NeuroQuant Windows build must be run on Windows.")
    os.chdir(ROOT)
    build(clean=not args.no_clean)
    if args.installer:
        build_inno_installer()


if __name__ == "__main__":
    main()
