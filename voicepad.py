"""
VoicePad — саундпад с озвучкой текста через Fish Audio (Windows).

Установка:   pip install customtkinter sounddevice soundfile numpy requests keyboard
Запуск:      python voicepad.py
Нужен виртуальный аудиокабель (например, бесплатный VB-CABLE): программа играет звук
в "CABLE Input", а в игре/Discord в качестве микрофона выбирается "CABLE Output".

Код разделён на модули:
    voicepad_config    — пути, конфигурация, DPAPI-ключ;
    voicepad_audio     — аудиоустройства, ресемплинг, Player;
    voicepad_platform  — Windows: тема/иконки окна и установка VB-CABLE;
    voicepad_ui        — интерфейс customtkinter.
Этот файл — тонкая точка входа и совместимые реэкспорты для тестов.
"""
import os
import traceback
from pathlib import Path

from voicepad_config import (
    API, APP_DIR, CFG_BACKUP, CFG_LOCK, CFG_PATH, CFG_VERSION, DEFAULT_CFG,
    DEFAULT_MODEL, HISTORY_MAX, KEY_PATH, NONE, TTS_DIR, APP_VERSION,
    _clean_str, delete_api_key, load_api_key, load_cfg, normalize_cfg,
    save_api_key, save_cfg,
)
from voicepad_audio import (
    Player, audio_error_text, default_device, device_name, find_input,
    find_inputs, list_devices, make_tone, open_output_stream, record_peak,
    resample, resolve_device, same_name_devices, verdict,
)
from voicepad_platform import (
    CABLE_HOSTS, CABLE_PAGE, CABLE_PUBLISHER, CABLE_SETUP, HTTP_HEADERS,
    MAX_DOWNLOAD, extract_cable_setup, fetch_official, find_cable_setup,
    install_cable, official_cable_url, program_dir, resource_path,
    run_elevated, set_window_icon, uninstall_cable, use_dark_titlebar,
    vbcable_dir, verify_cable_setup,
)
from voicepad_ui import App, NavView

__all__ = [name for name in dir() if not name.startswith("_") and name != "os"]


if __name__ == "__main__":
    app = App()
    smoke_file = os.environ.get("VOICEPAD_SMOKE")  # задаётся только автосборкой на GitHub
    if smoke_file:
        def finish():
            try:
                app.smoke_test()
                errors = "".join(app.callback_errors)
                result = "ok" if not errors else "errors:\n" + errors
            except Exception:
                result = "exception:\n" + traceback.format_exc()
            Path(smoke_file).write_text(result, "utf-8")
            app.destroy()
        app.after(3000, finish)
    app.mainloop()
