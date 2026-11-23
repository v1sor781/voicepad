"""Пути, конфигурация и безопасное хранение ключа VoicePad (Windows)."""
import base64
import copy
import ctypes
import json
import os
import subprocess
import sys
import threading
from pathlib import Path


API = "https://api.fish.audio"
APP_DIR = Path.home() / ".voicepad"
TTS_DIR = APP_DIR / "tts"
CFG_PATH = APP_DIR / "config.json"
CFG_BACKUP = APP_DIR / "config.json.bak"
KEY_PATH = APP_DIR / "key.dat"          # ключ зашифрован через DPAPI, не в config.json
TTS_DIR.mkdir(parents=True, exist_ok=True)

NONE = "— нет —"
HISTORY_MAX = 50
APP_VERSION = "0.1.3"
DEFAULT_MODEL = "s2.1-pro-free"  # бесплатная модель Fish Audio API; платная: "s2-pro"
CFG_VERSION = 1                   # версия схемы config.json (для будущих миграций)
CFG_LOCK = threading.RLock()      # общие изменения конфигурации и ключа

CABLE_PAGE = "https://vb-audio.com/Cable/"
CABLE_SETUP = "VBCABLE_Setup_x64.exe"
CABLE_HOSTS = frozenset(("vb-audio.com", "www.vb-audio.com"))
CABLE_PUBLISHER = "vbaudio"   # подпись VB-Audio: «VB-Audio Software» и похожие варианты
MAX_DOWNLOAD = 100 * 1024 * 1024  # защита от «HTML вместо ZIP» и бесконечных редиректов
HTTP_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) VoicePad"}


DEFAULT_CFG = {
    "cfg_version": CFG_VERSION,
    "api_key": "",  # только в памяти; на диск пишется зашифрованным в key.dat
    "model": DEFAULT_MODEL,  # заголовок "model" в запросе к Fish Audio
    "cable_device": "",  # куда играть "в микрофон" (CABLE Input)
    "monitor_device": "",  # твои динамики/наушники
    "mic_device": "",  # реальный микрофон (для пропуска в кабель)
    "passthrough": False,
    "vol_cable": 1.0,
    "vol_monitor": 0.8,
    "voices": [{"name": "Стандартный", "id": ""}],
    "voice": "Стандартный",
    "sounds": [],  # {"name", "path", "hotkey"}
    "phrases": [],  # {"text", "voice", "voice_id", "path", "hotkey"}
    "history": [],  # {"text", "voice", "voice_id", "path"}
    "stop_hotkey": "",
}

STR_KEYS = ("model", "cable_device", "monitor_device", "mic_device", "voice", "stop_hotkey")
BOOL_KEYS = ("passthrough",)
VOL_KEYS = ("vol_cable", "vol_monitor")
ITEM_FIELDS = {
    "voices": ("name", "id"),
    "sounds": ("name", "path", "hotkey"),
    "phrases": ("text", "voice", "voice_id", "path", "hotkey"),
    "history": ("text", "voice", "voice_id", "path"),
}


class _DataBlob(ctypes.Structure):
    """STRUCT для CryptProtectData/CryptUnprotectData (DPAPI Windows)."""
    _fields_ = [("cbData", ctypes.c_ulong), ("pbData", ctypes.c_void_p)]


def _dpapi(data, encrypt=True):
    """Шифрование/расшифровка привязано к учётной записи Windows."""
    if sys.platform != "win32":
        raise RuntimeError("DPAPI доступен только в Windows")
    src_buf = ctypes.create_string_buffer(data)
    src = _DataBlob(len(data), ctypes.cast(src_buf, ctypes.c_void_p))
    dst = _DataBlob(0, None)
    fn = ctypes.windll.crypt32.CryptProtectData if encrypt else ctypes.windll.crypt32.CryptUnprotectData
    if not fn(ctypes.byref(src), None, None, None, None, 0, ctypes.byref(dst)):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(dst.pbData, dst.cbData)
    finally:
        if dst.pbData:
            ctypes.windll.kernel32.LocalFree(ctypes.c_void_p(dst.pbData))


def _restrict_file(path):
    """Best effort: убираем наследование прав у файла с секретом."""
    if sys.platform != "win32" or not path.exists():
        return
    try:
        user = f"{os.environ.get('USERDOMAIN', '')}\\{os.environ.get('USERNAME', '')}".strip("\\")
        if user:
            # (OI)(CI) обязателен для каталога: иначе новые файлы не получат ACL
            rights = "(OI)(CI)F" if path.is_dir() else "F"
            subprocess.run(["icacls", str(path), "/inheritance:r", "/grant", f"{user}:{rights}"],
                           capture_output=True, creationflags=0x08000000, check=False)
    except Exception:
        pass


def load_api_key():
    """Возвращает расшифрованный ключ или пустую строку."""
    try:
        return _dpapi(base64.b64decode(KEY_PATH.read_bytes()), False).decode("utf-8")
    except (OSError, ValueError, RuntimeError):
        return ""


def save_api_key(key):
    """Сохраняет ключ зашифрованным; пустой ключ удаляет файл."""
    with CFG_LOCK:
        key = (key or "").strip()
        if not key:
            delete_api_key()
            return
        APP_DIR.mkdir(parents=True, exist_ok=True)
        first_time = not KEY_PATH.exists()
        KEY_PATH.write_bytes(base64.b64encode(_dpapi(key.encode("utf-8"), True)))
        if first_time:
            _restrict_file(KEY_PATH)   # icacls — дорого, вызываем только при создании файла


def delete_api_key():
    try:
        KEY_PATH.unlink(missing_ok=True)
    except OSError:
        pass


def _clean_str(value):
    return value if isinstance(value, str) else ("" if value is None else str(value))


def normalize_cfg(raw):
    """Приводит прочитанный JSON к ожидаемым типам и диапазонам; лишнее отбрасывает."""
    cfg = copy.deepcopy(DEFAULT_CFG)
    if not isinstance(raw, dict):
        return cfg
    for key in STR_KEYS:
        value = _clean_str(raw.get(key))
        cfg[key] = value or (DEFAULT_MODEL if key == "model" else "")
    for key in BOOL_KEYS:
        cfg[key] = raw.get(key) if isinstance(raw.get(key), bool) else DEFAULT_CFG[key]
    for key in VOL_KEYS:
        try:
            cfg[key] = min(1.0, max(0.0, float(raw.get(key))))
        except (TypeError, ValueError):
            cfg[key] = DEFAULT_CFG[key]
    for key, fields in ITEM_FIELDS.items():
        items = raw.get(key)
        if not isinstance(items, list):
            continue
        cleaned = []
        for item in items:
            if isinstance(item, dict):
                entry = {f: _clean_str(item.get(f)) for f in fields}
                if key != "voices" or entry["name"]:
                    cleaned.append(entry)
        cfg[key] = cleaned[:HISTORY_MAX] if key == "history" else cleaned
    if not cfg["voices"]:
        cfg["voices"] = copy.deepcopy(DEFAULT_CFG["voices"])
    if not any(v["name"] == cfg["voice"] for v in cfg["voices"]):
        cfg["voice"] = cfg["voices"][0]["name"]
    return cfg


def load_cfg():
    """Читает конфиг, восстанавливаясь из .bak; переносит старый plaintext-ключ в DPAPI."""
    with CFG_LOCK:
        raw = {}
        for path in (CFG_PATH, CFG_BACKUP):
            try:
                candidate = json.loads(path.read_text("utf-8"))
                if isinstance(candidate, dict):
                    raw = candidate
                    break
            except (OSError, ValueError):
                continue
        cfg = normalize_cfg(raw)
        legacy = raw.get("api_key")
        migrate = isinstance(legacy, str) and bool(legacy.strip()) and not KEY_PATH.exists()
        if migrate:
            try:
                save_api_key(legacy)
            except Exception:
                migrate = False
        cfg["api_key"] = load_api_key()
        if migrate:
            save_cfg(cfg)   # раз уже перенесли ключ — убираем его из JSON
        return cfg


def save_cfg(cfg):
    """Атомарная запись с резервной копией; ключ пишется отдельно, в зашифрованном виде."""
    with CFG_LOCK:
        data = {k: v for k, v in cfg.items() if k != "api_key"}
        text = json.dumps(data, ensure_ascii=False, indent=2)
        APP_DIR.mkdir(parents=True, exist_ok=True)
        tmp = CFG_PATH.with_name(CFG_PATH.name + ".tmp")
        try:
            if CFG_PATH.exists():
                # не shutil.copyfile: на некоторых Windows-ФС samefile() ложно
                # считает разные файлы одним из-за нулевых st_ino
                CFG_BACKUP.write_bytes(CFG_PATH.read_bytes())
            tmp.write_text(text, "utf-8")
            os.replace(tmp, CFG_PATH)
            _restrict_file(CFG_PATH)
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
        save_api_key(cfg.get("api_key", ""))


# ---------------------------------------------------------------- аудио
