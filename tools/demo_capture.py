"""Демо-режим для скриншотов: вымышленные фразы, без доступа к реальным данным.

Запуск: python tools/demo_capture.py
Переменная VOICEPAD_DEMO_TAB сразу открывает нужный раздел, например:
    $env:VOICEPAD_DEMO_TAB='Настройки'; python tools/demo_capture.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Патчим пути ДО импорта UI: модули берут свои копии констант при импорте.
import voicepad_config as cfg

DEMO = Path(tempfile.mkdtemp(prefix="voicepad-demo-"))
TTS = DEMO / "tts"
TTS.mkdir(parents=True, exist_ok=True)
cfg.APP_DIR = DEMO
cfg.CFG_PATH = DEMO / "config.json"
cfg.CFG_BACKUP = DEMO / "config.json.bak"
cfg.KEY_PATH = DEMO / "key.dat"
cfg.TTS_DIR = TTS

import numpy as np
import soundfile as sf
import voicepad_audio  # noqa: F401  (инициализация не нужна, но импорт безобиден)
import voicepad_ui

voicepad_ui.APP_DIR = DEMO
voicepad_ui.TTS_DIR = TTS


def demo_wav(name, freq):
    data, sr = voicepad_audio.make_tone(seconds=0.35, freq=freq)
    path = TTS / name
    sf.write(path, data, sr)
    return str(path)


hit = demo_wav("demo-hit.wav", 660)
clap = demo_wav("demo-clap.wav", 880)
ready = demo_wav("demo-ready.wav", 520)

demo = dict(cfg.DEFAULT_CFG)
demo.update({
    "model": cfg.DEFAULT_MODEL,
    "cable_device": "CABLE Input (VB-Audio Virtual Cable)",
    "monitor_device": "Наушники (демо)",
    "mic_device": "Микрофон (демо)",
    "passthrough": True,
    "vol_cable": 0.9,
    "vol_monitor": 0.8,
    "sounds": [
        {"name": "Удар", "path": hit, "hotkey": "F1"},
        {"name": "Хлопок", "path": clap, "hotkey": "F2"},
        {"name": "Сигнал", "path": ready, "hotkey": "F3"},
    ],
    "phrases": [
        {"text": "Привет, это VoicePad!", "voice": "Стандартный", "voice_id": "",
         "path": ready, "hotkey": "F4"},
        {"text": "Погнали в матч!", "voice": "Стандартный", "voice_id": "",
         "path": hit, "hotkey": "F5"},
        {"text": "Стоп, я вернусь через минуту", "voice": "Стандартный", "voice_id": "",
         "path": clap, "hotkey": "F6"},
    ],
    "history": [
        {"text": "Привет, это VoicePad!", "voice": "Стандартный", "voice_id": "", "path": ready},
        {"text": "Погнали в матч!", "voice": "Стандартный", "voice_id": "", "path": hit},
    ],
    "stop_hotkey": "Esc",
})
cfg.CFG_PATH.write_text(json.dumps(demo, ensure_ascii=False, indent=2), "utf-8")

app = voicepad_ui.App()
tab = os.environ.get("VOICEPAD_DEMO_TAB")
if tab:
    app.after(600, lambda: app.tabs.set(tab))
app.mainloop()
