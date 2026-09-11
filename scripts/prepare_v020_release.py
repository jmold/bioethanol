from pathlib import Path

replacements = {
    "api.py": [
        ('APP_VERSION = "0.19.1"', 'APP_VERSION = "0.20.0"'),
        ('MODEL_VERSION = "0.19.0"', 'MODEL_VERSION = "0.20.0"'),
    ],
    "api_v020.py": [
        ('0.20.0-alpha2', '0.20.0'),
    ],
    "connected_dynamic_engine.py": [
        ('0.20.0-alpha2', '0.20.0'),
    ],
    "test_connected_dynamic_engine.py": [
        ('0.20.0-alpha2', '0.20.0'),
    ],
    "test_v020_api.py": [
        ('0.20.0-alpha2', '0.20.0'),
    ],
    "src-tauri/tauri.conf.json": [
        ('"version": "0.19.1"', '"version": "0.20.0"'),
    ],
    "src-tauri/Cargo.toml": [
        ('version = "0.19.1"', 'version = "0.20.0"'),
    ],
    "package.json": [
        ('"version": "0.3.0"', '"version": "0.20.0"'),
    ],
    "RUN_SIMULATOR_WINDOWS.bat": [
        ('BioAgri Process Simulator V0.19', 'BioAgri Process Simulator V0.20'),
    ],
    "README.md": [
        ('App version **0.19.1**', 'App version **0.20.0**'),
        ('Engineering model **0.19.0**', 'Engineering model **0.20.0**'),
        ('V0.19 Animated Digital Twin', 'V0.20 Connected Digital Twin'),
    ],
}

for filename, pairs in replacements.items():
    path = Path(filename)
    text = path.read_text(encoding="utf-8")
    original = text
    for old, new in pairs:
        text = text.replace(old, new)
    if text != original:
        path.write_text(text, encoding="utf-8")
        print(f"updated {filename}")
    else:
        print(f"no change {filename}")
