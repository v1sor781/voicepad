"""
VoicePad — саундпад с озвучкой текста через Fish Audio (Windows).

Установка:   pip install customtkinter sounddevice soundfile numpy requests keyboard
Запуск:      python voicepad.py
Нужен виртуальный аудиокабель (например, бесплатный VB-CABLE): программа играет звук
в "CABLE Input", а в игре/Discord в качестве микрофона выбирается "CABLE Output".
"""
import copy
import io
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
import zipfile
from pathlib import Path
from urllib.parse import urljoin
from tkinter import filedialog, messagebox

import customtkinter as ctk
import keyboard
import numpy as np
import requests
import sounddevice as sd
import soundfile as sf

API = "https://api.fish.audio"
APP_DIR = Path.home() / ".voicepad"
TTS_DIR = APP_DIR / "tts"
CFG_PATH = APP_DIR / "config.json"
TTS_DIR.mkdir(parents=True, exist_ok=True)

NONE = "— нет —"
HISTORY_MAX = 50
APP_VERSION = "1.0.6"
DEFAULT_MODEL = "s2.1-pro-free"  # бесплатная модель Fish Audio API; платная: "s2-pro"

# ---- палитра интерфейса (совпадает со страницей скачивания)
BG = "#0e1116"
SURFACE = "#151a21"
CARD = "#1b212b"
CARD_HOVER = "#232b38"
BORDER = "#28303d"
TEXT = "#e6ebf2"
MUTED = "#8a94a6"
ACCENT = "#5b8cff"
ACCENT_HOVER = "#7aa2ff"
ACCENT_SOFT = "#243356"
DANGER = "#e05a5a"
DANGER_HOVER = "#c94848"
DANGER_SOFT = "#3a2226"
SUCCESS = "#42c98a"
DEFAULT_CFG = {
    "api_key": "",
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


def load_cfg():
    cfg = copy.deepcopy(DEFAULT_CFG)
    if CFG_PATH.exists():
        try:
            cfg.update(json.loads(CFG_PATH.read_text("utf-8")))
        except Exception:
            pass
    return cfg


def save_cfg(cfg):
    CFG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), "utf-8")


# ---------------------------------------------------------------- аудио

def list_devices(kind):
    """{название: индекс} для 'input' / 'output'.

    Сначала Windows WASAPI: там полные названия устройств (MME обрезает их до 31
    символа, из-за чего наушники легко перепутать с динамиками) и работает
    автоматическая конвертация частоты. Если WASAPI нет — берём MME.
    """
    res = {}
    try:
        apis = sd.query_hostapis()
        devices = sd.query_devices()
        for api_name in ("Windows WASAPI", "MME"):
            api = next((i for i, h in enumerate(apis) if h["name"] == api_name), None)
            if api is None:
                continue
            added = 0
            for i, d in enumerate(devices):
                if d["hostapi"] != api or d[f"max_{kind}_channels"] <= 0:
                    continue
                name = d["name"].strip()
                label = name
                if label in res:
                    label = f"{name} [{api_name.split()[-1]}]"
                if label in res:
                    continue
                res[label] = i
                added += 1
            if added:
                break   # одного хост-API достаточно, иначе в списке будут дубли
    except Exception:
        pass  # нет аудиоустройств или сбой PortAudio: программа всё равно должна запуститься
    return res


def device_name(devices, idx):
    """Название устройства по индексу (чтобы сохранить его в настройках)."""
    for name, i in devices.items():
        if i == idx:
            return name
    return ""


def resolve_device(value, kind):
    """Индекс устройства по сохранённому названию.

    Понимает и старые названия от MME: они обрезаны до 31 символа, поэтому
    сравниваем ещё и по началу строки.
    """
    if not value:
        return None
    devices = list_devices(kind)
    if value in devices:
        return devices[value]

    low = value.strip().lower()
    for name, idx in devices.items():
        if name.strip().lower() == low:
            return idx
    for name, idx in devices.items():
        n = name.strip().lower()
        if n.startswith(low) or low.startswith(n):
            return idx
    for name, idx in devices.items():
        if low in name.strip().lower():
            return idx
    return None


def open_output_stream(dev, sr, channels):
    """Поток вывода: WASAPI в общем режиме с конвертацией частоты, иначе обычный."""
    try:
        return sd.OutputStream(device=dev, samplerate=sr, channels=channels, dtype="float32",
                               extra_settings=sd.WasapiSettings(auto_convert=True))
    except Exception:
        return sd.OutputStream(device=dev, samplerate=sr, channels=channels, dtype="float32")


def default_device(kind):
    """Основное устройство Windows (WASAPI): 'input' или 'output'. None, если его нет."""
    try:
        apis = sd.query_hostapis()
        api = next((h for h in apis if h["name"] == "Windows WASAPI"), None)
        if api is not None:
            idx = api[f"default_{kind}_device"]
            if idx is not None and idx >= 0:
                return idx
    except Exception:
        pass
    try:
        idx = sd.default.device[0 if kind == "input" else 1]
        if idx is not None and idx >= 0:
            return idx
    except Exception:
        pass
    return None


def resample(data, sr, new_sr):
    """Линейно пересчитывает частоту дискретизации (нужно, если устройство не умеет sr)."""
    if sr == new_sr or not len(data):
        return data
    frames = max(1, int(round(len(data) * new_sr / sr)))
    x_old = np.linspace(0.0, 1.0, len(data), endpoint=False)
    x_new = np.linspace(0.0, 1.0, frames, endpoint=False)
    cols = [np.interp(x_new, x_old, data[:, c]) for c in range(data.shape[1])]
    return np.column_stack(cols).astype(np.float32)


CABLE_PAGE = "https://vb-audio.com/Cable/"
CABLE_SETUP = "VBCABLE_Setup_x64.exe"
HTTP_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) VoicePad"}


def program_dir():
    """Папка, где лежит программа (exe или скрипт)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


def resource_path(name):
    """Файл, упакованный в exe (или лежащий рядом со скриптом)."""
    return Path(getattr(sys, "_MEIPASS", program_dir())) / name


def set_window_icon(window, ico):
    """Ставит иконку окну: через tkinter и напрямую через Windows API (CTk иногда подменяет свою)."""
    try:
        window.iconbitmap(str(ico))
    except Exception:
        pass
    if sys.platform != "win32":
        return
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        user32.GetParent.restype = ctypes.c_void_p
        user32.GetParent.argtypes = [ctypes.c_void_p]
        user32.LoadImageW.restype = ctypes.c_void_p
        user32.LoadImageW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, wintypes.UINT,
                                      ctypes.c_int, ctypes.c_int, wintypes.UINT]
        user32.SendMessageW.restype = ctypes.c_void_p
        user32.SendMessageW.argtypes = [ctypes.c_void_p, wintypes.UINT, ctypes.c_void_p, ctypes.c_void_p]
        hwnd = user32.GetParent(window.winfo_id()) or window.winfo_id()
        for icon_kind, metric in ((1, 11), (0, 49)):          # ICON_BIG / SM_CXICON, ICON_SMALL / SM_CXSMICON
            size = user32.GetSystemMetrics(metric)
            hicon = user32.LoadImageW(None, str(ico), 1, size, size, 0x10)   # IMAGE_ICON, LR_LOADFROMFILE
            if hicon:
                user32.SendMessageW(hwnd, 0x80, icon_kind, hicon)             # WM_SETICON
    except Exception:
        pass


def vbcable_dir():
    """Куда класть VB-CABLE: подпапка рядом с программой, а если туда нельзя писать — ~/.voicepad."""
    for base in (program_dir(), APP_DIR):
        d = base / "vbcable"
        try:
            d.mkdir(parents=True, exist_ok=True)
            probe = d / ".write_test"
            probe.write_text("1")
            probe.unlink()
            return d
        except OSError:
            continue
    raise RuntimeError("нет папки, куда можно сохранить VB-CABLE")


def find_cable_setup():
    for base in (program_dir(), APP_DIR):
        f = base / "vbcable" / CABLE_SETUP
        if f.exists():
            return f
    return None


def run_elevated(exe, *args):
    """Запускает exe с правами администратора и ждёт завершения (Windows покажет запрос UAC)."""
    q = lambda x: str(x).replace("'", "''")
    arglist = ",".join(f"'{a}'" for a in args)
    subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         f"Start-Process -FilePath '{q(exe)}' -WorkingDirectory '{q(exe.parent)}' "
         f"-ArgumentList {arglist} -Verb RunAs -Wait"],
        creationflags=0x08000000, check=False)


def install_cable(say):
    """Скачивает VB-CABLE с официального сайта VB-Audio в папку программы и запускает установку.
    Windows попросит права администратора и подтверждение установки драйвера."""
    say("Ищу VB-CABLE на сайте VB-Audio…")
    html = requests.get(CABLE_PAGE, headers=HTTP_HEADERS, timeout=30).text
    m = re.search(r'href=["\']([^"\']*VBCABLE_Driver_Pack[^"\']*\.zip)["\']', html, re.I)
    if not m:
        raise RuntimeError("не нашёл ссылку на скачивание")
    say("Скачиваю VB-CABLE…")
    data = requests.get(urljoin(CABLE_PAGE, m.group(1)), headers=HTTP_HEADERS, timeout=120).content
    target = vbcable_dir()
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = z.namelist()
        member = next((n for n in names if n.lower().endswith(CABLE_SETUP.lower())), None)
        if not member:
            raise RuntimeError("в архиве нет установщика")
        prefix = member[: len(member) - len(CABLE_SETUP)]
        for n in names:
            if n.startswith(prefix) and not n.endswith("/"):
                out = (target / n[len(prefix):]).resolve()
                if target.resolve() not in out.parents:
                    continue
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(z.read(n))
    say("Подтвердите запросы Windows (права администратора и установка драйвера)…")
    run_elevated(target / CABLE_SETUP, "-i", "-h")


def uninstall_cable(say):
    setup = find_cable_setup()
    if not setup:
        raise RuntimeError("VoicePad не устанавливал VB-CABLE. Если он стоит у вас отдельно, "
                           "удалите его через «Установку и удаление программ»")
    say("Подтвердите запросы Windows…")
    run_elevated(setup, "-u", "-h")
    shutil.rmtree(setup.parent, ignore_errors=True)


def note_label(parent, text, wraplength, **kw):
    """Подпись с переносом строк; justify поддерживается не во всех версиях customtkinter."""
    try:
        return ctk.CTkLabel(parent, text=text, wraplength=wraplength, justify="left", anchor="w", **kw)
    except ValueError:
        return ctk.CTkLabel(parent, text=text, wraplength=wraplength, anchor="w", **kw)


def card(parent, **kw):
    """Карточка в общем стиле приложения."""
    opts = dict(fg_color=CARD, corner_radius=12, border_width=1, border_color=BORDER)
    opts.update(kw)
    return ctk.CTkFrame(parent, **opts)


def card_title(parent, text):
    return ctk.CTkLabel(parent, text=text, anchor="w", text_color=TEXT,
                        font=("Segoe UI", 15, "bold"))


def muted_label(parent, text, **kw):
    kw.setdefault("text_color", MUTED)
    kw.setdefault("anchor", "w")
    kw.setdefault("font", ("Segoe UI", 12))
    kw.setdefault("justify", "left")
    try:
        return ctk.CTkLabel(parent, text=text, **kw)
    except ValueError:
        kw.pop("justify", None)
        return ctk.CTkLabel(parent, text=text, **kw)


SILENT_LEVEL = 0.003   # ниже: полная тишина (около -50 дБ)
OK_LEVEL = 0.02        # выше: сигнал слышно нормально (около -34 дБ)


def find_input(substr):
    """Индекс устройства записи, в названии которого есть substr (например, 'cable output')."""
    for name, idx in list_devices("input").items():
        if substr in name.lower():
            return idx
    return None


def record_peak(dev, seconds, on_level):
    """Слушает устройство записи и возвращает максимальный уровень 0..1.
    on_level(v) вызывается для каждого блока (для индикатора уровня)."""
    try:
        info = sd.query_devices(dev)
    except Exception:
        info = {}
    sr = int(info.get("default_samplerate") or 48000)
    ch = max(1, min(int(info.get("max_input_channels") or 1), 2))
    peak = [0.0]

    def cb(indata, frames, t, status):
        if not frames:
            return
        lvl = float(np.abs(indata).max())
        peak[0] = max(peak[0], lvl)
        on_level(lvl)

    def open_stream():
        try:
            return sd.InputStream(device=dev, channels=ch, samplerate=sr, dtype="float32",
                                  extra_settings=sd.WasapiSettings(auto_convert=True), callback=cb)
        except Exception:
            return sd.InputStream(device=dev, channels=ch, samplerate=sr, dtype="float32", callback=cb)

    with open_stream():
        time.sleep(seconds)
    return peak[0]


def make_tone(sr=44100, seconds=1.2, freq=440.0, amp=0.35):
    t = np.arange(int(sr * seconds)) / sr
    y = (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)
    fade = int(0.02 * sr)
    ramp = np.linspace(0, 1, fade, dtype=np.float32)
    y[:fade] *= ramp
    y[-fade:] *= ramp[::-1]
    return y.reshape(-1, 1), sr


def verdict(peak, what_ok, what_silent, what_quiet):
    """Превращает максимальный уровень в (слышно?, текст)."""
    pct = f"{peak * 100:.0f}%"
    if peak >= OK_LEVEL:
        return True, f"{what_ok} (максимальный уровень {pct})."
    if peak >= SILENT_LEVEL:
        return False, f"{what_quiet} (максимальный уровень всего {pct})."
    return False, what_silent if what_silent.endswith(".") else what_silent + "."


class Player:
    def __init__(self, cfg, report):
        self.cfg, self.report = cfg, report
        self.token = 0
        self.pt = None

    def stop(self):
        self.token += 1

    def play(self, path):
        self.token += 1
        threading.Thread(target=self._run, args=(path, self.token), daemon=True).start()

    def _targets(self):
        """[(устройство, громкость)] для вывода. Одно и то же устройство не играет дважды."""
        targets = {}
        for dev_key, vol_key in (("cable_device", "vol_cable"), ("monitor_device", "vol_monitor")):
            idx = resolve_device(self.cfg[dev_key], "output")
            if idx is None:
                continue
            vol = float(self.cfg.get(vol_key, 1.0))
            targets[idx] = max(vol, targets.get(idx, 0.0))
        return list(targets.items())

    def _run(self, path, token):
        try:
            data, sr = sf.read(path, dtype="float32", always_2d=True)
        except Exception as e:
            self.report(f"Не удалось прочитать {Path(path).name}: {e}")
            return
        targets = self._targets()
        if not targets:
            self.report("Не выбраны устройства вывода: откройте «Настройки» и выберите наушники и CABLE Input")
            return
        for dev, vol in targets:
            threading.Thread(target=self._out, args=(dev, data, sr, vol, token), daemon=True).start()

    def beep(self, dev, seconds=0.8):
        """Короткий писк в конкретное устройство — кнопка «проверить» рядом с выбором."""
        self.token += 1
        data, sr = make_tone(seconds=seconds)
        threading.Thread(target=self._out, args=(dev, data, sr, 0.6, self.token), daemon=True).start()

    def _out(self, dev, data, sr, vol, token):
        try:
            info = sd.query_devices(dev)
        except Exception:
            info = {}
        max_ch = int(info.get("max_output_channels") or 0)
        if max_ch <= 0:
            self.report("Устройство вывода недоступно. Выберите другое в «Настройки».")
            return
        ch = max(1, min(data.shape[1], max_ch, 2))
        buf = np.clip(data[:, :ch] * vol, -1.0, 1.0)
        variants = [(ch, buf)]
        if ch == 1 and max_ch >= 2:   # моно на стерео-устройство: некоторые драйверы так не умеют
            variants.append((2, np.repeat(buf, 2, axis=1)))

        default_sr = int(info.get("default_samplerate") or 0)
        last = None
        for channels, base in variants:
            attempts = [(sr, base)]
            if default_sr and default_sr != sr:
                attempts.append((default_sr, resample(base, sr, default_sr)))
            for rate, chunk in attempts:
                try:
                    with open_output_stream(dev, rate, channels) as s:
                        for i in range(0, len(chunk), 2048):
                            if token != self.token:
                                s.abort()
                                return
                            s.write(np.ascontiguousarray(chunk[i:i + 2048]))
                    return
                except Exception as e:
                    if token != self.token:
                        return
                    last = e
        name = info.get("name") or "устройство вывода"
        self.report(f"Не удалось вывести звук в «{name}»: {last}. "
                    "Выберите другое устройство в «Настройки».")

    def stop_passthrough(self):
        if self.pt:
            try:
                self.pt.abort()
                self.pt.close()
            except Exception:
                pass
            self.pt = None

    def restart_passthrough(self):
        """Пропускает реальный микрофон в кабель, чтобы в игре слышали и тебя, и звуки."""
        self.stop_passthrough()
        if not self.cfg["passthrough"]:
            return
        out = resolve_device(self.cfg["cable_device"], "output")
        mic = resolve_device(self.cfg["mic_device"], "input")
        if mic is None:
            self.report("Пропуск микрофона: выберите настоящий микрофон в «Настройки»")
            return
        if out is None:
            self.report("Пропуск микрофона: выберите CABLE Input в «Настройки»")
            return

        def cb(indata, outdata, frames, t, status):
            if indata.shape[1] == outdata.shape[1]:
                outdata[:] = indata
            else:
                outdata[:] = np.repeat(indata[:, :1], outdata.shape[1], axis=1)

        try:
            mic_sr = int(sd.query_devices(mic)["default_samplerate"])
        except Exception:
            mic_sr = 48000
        try:
            out_ch = max(1, min(2, int(sd.query_devices(out)["max_output_channels"])))
        except Exception:
            out_ch = 2
        wasapi = (sd.WasapiSettings(auto_convert=True), sd.WasapiSettings(auto_convert=True))
        attempts = [(mic_sr, wasapi), (48000, wasapi), (44100, wasapi),
                    (mic_sr, None), (48000, None)]
        last = None
        for rate, extra in attempts:
            try:
                self.pt = sd.Stream(samplerate=rate, device=(mic, out), channels=(1, out_ch),
                                    dtype="float32", extra_settings=extra, callback=cb)
                self.pt.start()
                return
            except Exception as e:
                last = e
                self.stop_passthrough()
        self.report(f"Пропуск микрофона не запустился: {last}")


# ---------------------------------------------------------------- интерфейс

class NavView(ctk.CTkFrame):
    """Боковое меню и область содержимого. API совместим с CTkTabview: add/tab/set/get."""

    def __init__(self, master, **kw):
        super().__init__(master, fg_color="transparent", **kw)
        self._frames = {}
        self._buttons = {}
        self._current = None
        self.on_show = {}   # раздел -> функция, вызываемая при открытии

        self.sidebar = ctk.CTkFrame(self, fg_color=SURFACE, corner_radius=14,
                                    border_width=1, border_color=BORDER, width=200)
        self.sidebar.pack(side="left", fill="y", padx=(0, 14))
        self.sidebar.pack_propagate(False)

        ctk.CTkLabel(self.sidebar, text="РАЗДЕЛЫ", anchor="w", text_color=MUTED,
                     font=("Segoe UI", 11, "bold")).pack(fill="x", padx=18, pady=(16, 8))

        self.content = ctk.CTkFrame(self, fg_color="transparent")
        self.content.pack(side="left", fill="both", expand=True)

    def add(self, name):
        if name in self._frames:
            return self._frames[name]
        frame = ctk.CTkFrame(self.content, fg_color="transparent")
        btn = ctk.CTkButton(self.sidebar, text=name, anchor="w", height=40, corner_radius=10,
                            fg_color="transparent", hover_color=CARD_HOVER, text_color=MUTED,
                            font=("Segoe UI", 14),
                            command=lambda n=name: self.set(n))
        btn.pack(fill="x", padx=10, pady=2)
        self._frames[name] = frame
        self._buttons[name] = btn
        if self._current is None:
            self.set(name)
        return frame

    def tab(self, name):
        return self._frames[name]

    def set(self, name):
        if name not in self._frames:
            return
        self._current = name
        for n, frame in self._frames.items():
            if n == name:
                frame.pack(fill="both", expand=True)
            else:
                frame.pack_forget()
        for n, btn in self._buttons.items():
            if n == name:
                btn.configure(fg_color=ACCENT_SOFT, text_color=TEXT)
            else:
                btn.configure(fg_color="transparent", text_color=MUTED)
        hook = self.on_show.get(name)
        if hook:
            try:
                hook()
            except Exception:
                pass

    def get(self):
        return self._current


class App(ctk.CTk):
    def __init__(self):
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")
        super().__init__()
        self.configure(fg_color=BG)
        self.callback_errors = []
        self.meter = {"running": False, "level": 0.0}
        self.title("VoicePad")
        self.geometry("960x780")
        self.minsize(860, 620)
        self.f_title = ctk.CTkFont(family="Segoe UI", size=18, weight="bold")
        self.f_h2 = ctk.CTkFont(family="Segoe UI", size=16, weight="bold")
        self.f_body = ctk.CTkFont(family="Segoe UI", size=13)
        self.f_small = ctk.CTkFont(family="Segoe UI", size=12)
        self.f_logo = ctk.CTkFont(family="Segoe UI", size=22, weight="bold")
        self.cfg = load_cfg()
        self.engine = Player(self.cfg, self.report)
        self.voice_menus = []
        self._guess_devices()
        self._build()
        self.refresh_sounds()
        self.refresh_phrases()
        self.refresh_history()
        self.rebind_all()
        self.engine.restart_passthrough()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.install_clipboard_shortcuts()
        ico = resource_path("voicepad.ico")
        if ico.exists():
            for delay in (250, 900):  # CTk ставит свою иконку с задержкой, поэтому повторяем
                self.after(delay, lambda: self.apply_window_icon(ico))
        if not any("cable" in n.lower() for n in list_devices("output")):
            self.after(600, self.show_cable_hint)

    # ---- служебное
    def report(self, msg):
        self.after(0, lambda: self.status.configure(text=msg, text_color=MUTED))

    def auth(self):
        return {"Authorization": f"Bearer {self.cfg['api_key']}"}

    def _guess_devices(self):
        """Подставляет устройства и заменяет старые MME-названия на полные имена WASAPI."""
        outs = list_devices("output")
        ins = list_devices("input")
        changed = False

        # старые названия обрезаны до 31 символа — приводим их к полным
        for key, devices, kind in (("cable_device", outs, "output"),
                                   ("monitor_device", outs, "output"),
                                   ("mic_device", ins, "input")):
            idx = resolve_device(self.cfg[key], kind)
            if idx is None:
                continue
            name = device_name(devices, idx)
            if name and name != self.cfg[key]:
                self.cfg[key] = name
                changed = True

        if resolve_device(self.cfg["cable_device"], "output") is None:
            name = next((n for n in outs if "cable input" in n.lower()), "")
            changed |= name != self.cfg["cable_device"]
            self.cfg["cable_device"] = name

        if resolve_device(self.cfg["monitor_device"], "output") is None:
            name = device_name(outs, default_device("output"))
            if not name or "cable" in name.lower():
                name = next((n for n in outs if "cable" not in n.lower()), "")
            changed |= name != self.cfg["monitor_device"]
            self.cfg["monitor_device"] = name

        if resolve_device(self.cfg["mic_device"], "input") is None:
            name = device_name(ins, default_device("input"))
            if not name or "cable" in name.lower():
                name = next((n for n in ins if "cable" not in n.lower()), "")
            changed |= name != self.cfg["mic_device"]
            self.cfg["mic_device"] = name

        if changed:
            save_cfg(self.cfg)

    def show_cable_hint(self):
        win = ctk.CTkToplevel(self)
        win.title("Нужен виртуальный микрофон")
        win.geometry("480x360")
        win.configure(fg_color=BG)
        win.attributes("-topmost", True)
        hint_ico = resource_path("voicepad.ico")
        if hint_ico.exists():
            win.after(300, lambda: set_window_icon(win, hint_ico))

        box = card(win)
        box.pack(fill="both", expand=True, padx=18, pady=18)
        inner = ctk.CTkFrame(box, fg_color="transparent")
        inner.pack(fill="both", expand=True, padx=18, pady=18)
        ctk.CTkLabel(inner, text="Нужен виртуальный микрофон", anchor="w",
                     text_color=TEXT, font=("Segoe UI", 16, "bold")).pack(fill="x")
        note_label(
            inner, wraplength=400, text_color=MUTED, font=("Segoe UI", 12),
            text=("Windows не даёт программе говорить в настоящий микрофон напрямую, поэтому нужен "
                  "бесплатный драйвер VB-CABLE от VB-Audio. Программа скачает его с официального "
                  "сайта и запустит установку. Windows попросит подтвердить права администратора "
                  "и установку драйвера."),
        ).pack(fill="x", pady=(8, 6))
        lbl = ctk.CTkLabel(inner, text="", wraplength=400, anchor="w", justify="left",
                           text_color=MUTED, font=("Segoe UI", 11))
        lbl.pack(fill="x", pady=4)
        btn = ctk.CTkButton(inner, text="Установить автоматически", height=38, corner_radius=9,
                            fg_color=ACCENT, hover_color=ACCENT_HOVER, text_color="#ffffff",
                            font=("Segoe UI", 13))
        btn.configure(command=lambda: self.install_cable_async(btn, lbl))
        btn.pack(fill="x", pady=(8, 4))
        ctk.CTkButton(inner, text="Скачать вручную", height=36, corner_radius=9,
                      fg_color=SURFACE, hover_color=CARD_HOVER, border_width=1, border_color=BORDER,
                      text_color=TEXT, font=("Segoe UI", 13),
                      command=lambda: webbrowser.open(CABLE_PAGE)).pack(fill="x", pady=4)
        ctk.CTkButton(inner, text="Закрыть", height=36, corner_radius=9,
                      fg_color="transparent", hover_color=CARD_HOVER,
                      text_color=MUTED, font=("Segoe UI", 13),
                      command=win.destroy).pack(fill="x", pady=4)

    def install_cable_async(self, btn=None, lbl=None):
        def safe(fn):
            try:
                fn()
            except Exception:
                pass  # окно могли закрыть

        def say(msg):
            self.report(msg)
            if lbl is not None:
                self.after(0, lambda: safe(lambda: lbl.configure(text=msg)))

        def worker():
            try:
                install_cable(say)
                say("Готово. Перезапустите VoicePad. Если виртуальный микрофон не появился, "
                    "перезагрузите компьютер.")
            except Exception as e:
                say(f"Не удалось установить автоматически: {e}. Попробуйте «Скачать вручную».")
            finally:
                if btn is not None:
                    self.after(0, lambda: safe(lambda: btn.configure(state="normal")))

        if btn is not None:
            btn.configure(state="disabled")
        threading.Thread(target=worker, daemon=True).start()

    def uninstall_cable_async(self):
        if not messagebox.askyesno(
                "Удалить VB-CABLE",
                "Удалить виртуальный микрофон VB-CABLE? Если им пользуются другие программы "
                "(OBS, Discord и т. п.), они его потеряют."):
            return

        def worker():
            try:
                uninstall_cable(self.report)
                self.report("Готово. Перезагрузите компьютер, чтобы завершить удаление драйвера.")
            except Exception as e:
                self.report(f"Не удалось удалить: {e}")

        threading.Thread(target=worker, daemon=True).start()

    def install_clipboard_shortcuts(self):
        """В Tk Ctrl+C/V/X/A не работают при русской раскладке: обрабатываем по коду клавиши."""
        def handler(event):
            if event.keysym.lower() in ("c", "v", "x", "a"):
                return None  # латинская раскладка: Tk справится сам
            w = event.widget
            action = {67: "<<Copy>>", 86: "<<Paste>>", 88: "<<Cut>>"}.get(event.keycode)
            try:
                if action:
                    w.event_generate(action)
                    return "break"
                if event.keycode == 65:
                    if w.winfo_class() == "Text":
                        w.tag_add("sel", "1.0", "end-1c")
                    else:
                        w.select_range(0, "end")
                    return "break"
            except Exception:
                pass
            return None
        self.bind_all("<Control-KeyPress>", handler)

    def report_callback_exception(self, exc, val, tb):
        """Ошибки в обработчиках кнопок: пишем в лог и в строку статуса, а не молча теряем."""
        self.callback_errors.append("".join(traceback.format_exception(exc, val, tb)))
        try:
            with open(APP_DIR / "error.log", "a", encoding="utf-8") as f:
                f.write(time.strftime("%Y-%m-%d %H:%M:%S") + "\n"
                        + "".join(traceback.format_exception(exc, val, tb)) + "\n")
        except OSError:
            pass
        try:
            self.status.configure(text=f"Ошибка: {val}", text_color=DANGER)
        except Exception:
            pass

    def smoke_test(self):
        """Автопроверка для сборки на GitHub: пробегает по вкладкам и основным действиям."""
        self.phrase_entry.insert(0, "тест")
        self.add_phrase_from_entry()
        self.id_entry.insert(0, "https://fish.audio/m/" + "a" * 32)
        self.add_voice_from_entry()
        for name in ("Звуки", "Фразы", "Голос ИИ", "История", "Настройки", "Проверка"):
            self.tabs.set(name)
            self.update()
        ok, msg = self.check_virtual_mic(lambda v: None)   # без аудиоустройств должно вернуть понятную ошибку
        assert isinstance(ok, bool) and msg, "проверка микрофона вернула мусор"
        self.key_var.set("smoke-key")
        assert self.cfg["api_key"] == "smoke-key", "ключ не применился сразу"
        self.key_var.set("")
        self.save_settings()
        self.remove_phrase(self.cfg["phrases"][-1])
        self.update()

    # ---- проверка микрофона
    def check_virtual_mic(self, on_level):
        """Играет тон в виртуальный микрофон и слушает CABLE Output. Возвращает (слышно?, текст)."""
        cable_in = resolve_device(self.cfg["cable_device"], "output")
        if cable_in is None:
            return False, ("Не выбран «Виртуальный микрофон — выход». Откройте «Настройки» и выберите "
                           "CABLE Input (если его нет в списке, установите VB-CABLE и перезагрузите ПК).")
        cable_out = find_input("cable output")
        if cable_out is None:
            return False, ("Не вижу устройство CABLE Output. Если VB-CABLE только что установлен, "
                           "перезагрузите компьютер.")
        data, sr = make_tone()
        self.engine.stop()
        token = self.engine.token
        targets = [(cable_in, self.cfg["vol_cable"])]
        monitor = resolve_device(self.cfg["monitor_device"], "output")
        if monitor is not None and monitor != cable_in:
            targets.append((monitor, self.cfg["vol_monitor"]))   # писк слышно и в наушниках

        def play():
            for dev, vol in targets:
                threading.Thread(target=self.engine._out, args=(dev, data, sr, vol, token), daemon=True).start()

        threading.Timer(0.3, play).start()
        peak = record_peak(cable_out, 2.0, on_level)
        return verdict(
            peak,
            "Работает: тестовый сигнал дошёл до виртуального микрофона. В игре выберите микрофон «CABLE Output»",
            ("Сигнал не дошёл до CABLE Output. Проверьте: в «Настройки» выбран CABLE Input, "
             "громкость выхода не на нуле, VB-CABLE установлен."),
            "Сигнал дошёл, но очень тихий. Увеличьте громкость «Виртуальный микрофон» в настройках")

    def check_game_hears(self, on_level):
        """Слушает CABLE Output: то, что услышит игра (ваш голос при включённом пропуске и звуки)."""
        cable_out = find_input("cable output")
        if cable_out is None:
            return False, "Не вижу устройство CABLE Output. Установите VB-CABLE и перезагрузите компьютер."
        peak = record_peak(cable_out, 6.0, on_level)
        hint = ("Тишина: на CABLE Output ничего не поступает. Нажмите любой звук во время проверки "
                "или включите «Пропускать мой реальный микрофон», выберите микрофон и скажите что-нибудь")
        return verdict(peak, "Игра слышит звук", hint, "Игра слышит звук, но очень тихо")

    def check_real_mic(self, on_level):
        """Слушает ваш настоящий микрофон (выбранный в настройках или основной в Windows)."""
        dev = resolve_device(self.cfg["mic_device"], "input")
        if dev is None:
            dev = default_device("input")
        if dev is None:
            return False, "Микрофон не найден. Выберите его в «Настройки» или подключите."
        peak = record_peak(dev, 6.0, on_level)
        silent = ("Микрофон молчит. Проверьте, что в «Настройки» выбран нужный микрофон, он включён "
                  "в Windows (Параметры → Система → Звук → Ввод) и разрешён для приложений "
                  "(Параметры → Конфиденциальность → Микрофон)")
        return verdict(peak, "Микрофон слышно", silent,
                       "Микрофон слышно, но очень тихо. Увеличьте его громкость в параметрах Windows")

    def _build_check(self, tab):
        body = ctk.CTkScrollableFrame(tab, fg_color="transparent", corner_radius=0)
        body.pack(fill="both", expand=True)
        ctk.CTkLabel(body, text="Проверка", anchor="w", text_color=TEXT,
                     font=self.f_h2).pack(anchor="w")
        muted_label(body, "Проверьте, слышит ли игра ваш голос и звуки через виртуальный микрофон."
                    ).pack(anchor="w", pady=(2, 0))

        meter = card(body)
        meter.pack(fill="x", pady=(14, 0))
        m = ctk.CTkFrame(meter, fg_color="transparent")
        m.pack(fill="x", padx=16, pady=16)
        card_title(m, "Уровень сигнала").pack(anchor="w")
        self.level_bar = ctk.CTkProgressBar(m, height=14, corner_radius=7,
                                            fg_color=SURFACE, progress_color=ACCENT)
        self.level_bar.set(0)
        self.level_bar.pack(fill="x", pady=(10, 10))
        self.test_result = note_label(m, wraplength=700, text="Результат появится здесь.",
                                      text_color=MUTED, font=self.f_small)
        self.test_result.pack(fill="x")

        tests = (
            ("virtual", "Тест сигнала", "Проиграет короткий писк через виртуальный микрофон и проверит, "
                                        "что он доходит до CABLE Output."),
            ("game", "Что слышит игра (6 секунд)", "Слушает CABLE Output: говорите в микрофон "
                                                   "(при включённом пропуске) или нажмите любой звук."),
            ("mic", "Проверить мой микрофон (6 секунд)", "Слушает ваш настоящий микрофон: скажите что-нибудь."),
        )
        for kind, title, desc in tests:
            c = card(body)
            c.pack(fill="x", pady=(10, 0))
            i = ctk.CTkFrame(c, fg_color="transparent")
            i.pack(fill="x", padx=16, pady=16)
            card_title(i, title).pack(anchor="w")
            muted_label(i, desc, wraplength=640).pack(anchor="w", pady=(4, 10))
            ctk.CTkButton(i, text="Запустить", width=130, height=36, corner_radius=9,
                          fg_color=SURFACE, hover_color=CARD_HOVER, border_width=1,
                          border_color=BORDER, text_color=TEXT, font=self.f_body,
                          command=lambda k=kind: self.start_test(k)).pack(anchor="w")

    def start_test(self, kind):
        if self.meter["running"]:
            return
        self.meter.update(running=True, level=0.0)
        intro = {"virtual": "Играю тестовый звук (вы услышите писк) и слушаю CABLE Output…",
                 "game": "Слушаю CABLE Output 6 секунд: говорите или нажмите любой звук…",
                 "mic": "Слушаю микрофон 6 секунд: скажите что-нибудь…"}[kind]
        self.test_result.configure(text=intro, text_color=MUTED)
        threading.Thread(target=self._test_worker, args=(kind,), daemon=True).start()
        self._poll_meter()

    def _test_worker(self, kind):
        def on_level(v):
            self.meter["level"] = max(self.meter["level"], v)

        try:
            func = {"virtual": self.check_virtual_mic, "game": self.check_game_hears, "mic": self.check_real_mic}[kind]
            ok, msg = func(on_level)
        except Exception as e:
            ok, msg = False, f"Не удалось выполнить проверку: {e}"
        self.meter["running"] = False
        self.after(0, lambda: self.show_test_result(ok, msg))

    def show_test_result(self, ok, msg):
        self.test_result.configure(text=("✅ " if ok else "❌ ") + msg,
                                   text_color=SUCCESS if ok else DANGER)
        self.report(msg)

    def _poll_meter(self):
        lvl = self.meter["level"]
        self.meter["level"] = lvl * 0.6          # плавное затухание индикатора
        db = 20 * np.log10(max(lvl, 1e-5))
        self.level_bar.set(min(1.0, max(0.0, (db + 60) / 60)))
        if self.meter["running"]:
            self.after(50, self._poll_meter)
        else:
            self.level_bar.set(0)

    def apply_window_icon(self, ico):
        set_window_icon(self, ico)

    def on_close(self):
        save_cfg(self.cfg)
        self.engine.stop()
        try:
            keyboard.unhook_all()
        except Exception:
            pass
        self.destroy()

    # ---- хоткеи
    def rebind_all(self):
        try:
            keyboard.unhook_all_hotkeys()
        except AttributeError:
            pass  # слушатель клавиатуры ещё не запущен, снимать нечего
        for s in self.cfg["sounds"]:
            if s.get("hotkey"):
                self._add_hotkey(s["hotkey"], lambda p=s["path"]: self.engine.play(p))
        for ph in self.cfg["phrases"]:
            if ph.get("hotkey"):
                self._add_hotkey(ph["hotkey"], lambda ph=ph: self.play_phrase(ph))
        if self.cfg["stop_hotkey"]:
            self._add_hotkey(self.cfg["stop_hotkey"], self.engine.stop)

    def _add_hotkey(self, hk, fn):
        try:
            keyboard.add_hotkey(hk, fn)
        except Exception as e:
            self.report(f"Не удалось назначить «{hk}»: {e}")

    def capture_hotkey(self, done):
        self.report("Нажмите сочетание клавиш… (Esc — снять назначение)")

        def worker():
            try:
                hk = keyboard.read_hotkey(suppress=False)
            except Exception as e:
                self.report(f"Ошибка: {e}")
                return
            self.after(0, lambda: done("" if hk == "esc" else hk))

        threading.Thread(target=worker, daemon=True).start()

    # ---- построение UI
    def _build(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        # шапка
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=18, pady=(16, 10))

        logo = ctk.CTkFrame(header, width=42, height=42, corner_radius=12, fg_color=ACCENT)
        logo.pack(side="left")
        logo.pack_propagate(False)
        ctk.CTkLabel(logo, text="V", text_color="#ffffff", font=self.f_logo).pack(expand=True)

        titles = ctk.CTkFrame(header, fg_color="transparent")
        titles.pack(side="left", padx=12)
        ctk.CTkLabel(titles, text="VoicePad", anchor="w", text_color=TEXT,
                     font=self.f_title).pack(anchor="w")
        ctk.CTkLabel(titles, text=f"саундпад и озвучка · v{APP_VERSION}", anchor="w",
                     text_color=MUTED, font=self.f_small).pack(anchor="w")

        right = ctk.CTkFrame(header, fg_color="transparent")
        right.pack(side="right")
        self.stop_hk_btn = ctk.CTkButton(right, width=150, height=34, corner_radius=9,
                                         fg_color=CARD, hover_color=CARD_HOVER,
                                         border_width=1, border_color=BORDER,
                                         text_color=MUTED, font=self.f_small,
                                         text=self._hk_text(self.cfg["stop_hotkey"], "Клавиша «Стоп»"),
                                         command=self.set_stop_hotkey)
        self.stop_hk_btn.pack(side="right", padx=(8, 0))
        ctk.CTkButton(right, text="■  Стоп", width=104, height=34, corner_radius=9,
                      fg_color=DANGER, hover_color=DANGER_HOVER, text_color="#ffffff",
                      font=self.f_body,
                      command=self.engine.stop).pack(side="right")

        # разделы
        self.tabs = NavView(self)
        self.tabs.grid(row=1, column=0, sticky="nsew", padx=18, pady=(0, 10))
        for name in ("Звуки", "Фразы", "Голос ИИ", "История", "Настройки", "Проверка"):
            self.tabs.add(name)
        self._build_sounds(self.tabs.tab("Звуки"))
        self._build_phrases(self.tabs.tab("Фразы"))
        self._build_tts(self.tabs.tab("Голос ИИ"))
        self._build_history(self.tabs.tab("История"))
        self._build_settings(self.tabs.tab("Настройки"))
        self._build_check(self.tabs.tab("Проверка"))
        self.tabs.on_show["Настройки"] = self.refresh_device_lists

        # строка состояния
        status_bar = ctk.CTkFrame(self, fg_color=SURFACE, corner_radius=10,
                                  border_width=1, border_color=BORDER, height=38)
        status_bar.grid(row=2, column=0, sticky="ew", padx=18, pady=(0, 14))
        status_bar.pack_propagate(False)
        ctk.CTkLabel(status_bar, text="●", text_color=SUCCESS,
                     font=("Segoe UI", 10)).pack(side="left", padx=(14, 6))
        self.status = ctk.CTkLabel(status_bar, text="Готово", anchor="w",
                                   text_color=MUTED, font=self.f_small)
        self.status.pack(side="left", fill="x", expand=True, padx=(0, 12))

    @staticmethod
    def _hk_text(hk, empty):
        return hk if hk else empty

    def _entry(self, parent, **kw):
        opts = dict(height=38, corner_radius=9, fg_color=SURFACE, border_width=1,
                    border_color=BORDER, text_color=TEXT, placeholder_text_color=MUTED,
                    font=self.f_body)
        opts.update(kw)
        return ctk.CTkEntry(parent, **opts)

    def _menu(self, parent, **kw):
        opts = dict(height=36, corner_radius=9, fg_color=SURFACE, button_color=CARD_HOVER,
                    button_hover_color=BORDER, text_color=TEXT, dropdown_fg_color=CARD,
                    dropdown_hover_color=CARD_HOVER, dropdown_text_color=TEXT, font=self.f_small)
        opts.update(kw)
        return ctk.CTkOptionMenu(parent, **opts)

    def set_stop_hotkey(self):
        def done(hk):
            self.cfg["stop_hotkey"] = hk
            save_cfg(self.cfg)
            self.stop_hk_btn.configure(text=self._hk_text(hk, "Клавиша «Стоп»"))
            self.rebind_all()
            self.report("Готово")
        self.capture_hotkey(done)

    # ---- вкладка «Звуки»
    def _build_sounds(self, tab):
        head = ctk.CTkFrame(tab, fg_color="transparent")
        head.pack(fill="x")
        left = ctk.CTkFrame(head, fg_color="transparent")
        left.pack(side="left", fill="x", expand=True)
        ctk.CTkLabel(left, text="Звуки", anchor="w", text_color=TEXT,
                     font=self.f_h2).pack(anchor="w")
        muted_label(left, "Короткие звуки и мемы. Нажмите ▶ или назначьте горячую клавишу."
                    ).pack(anchor="w", pady=(2, 0))
        ctk.CTkButton(head, text="+  Добавить звук", width=160, height=38, corner_radius=10,
                      fg_color=ACCENT, hover_color=ACCENT_HOVER, text_color="#ffffff",
                      font=self.f_body, command=self.add_sounds).pack(side="right")

        self.list = ctk.CTkScrollableFrame(tab, fg_color="transparent", corner_radius=0)
        self.list.pack(fill="both", expand=True, pady=(14, 0))

    def _empty_state(self, parent, title, text):
        box = ctk.CTkFrame(parent, fg_color="transparent")
        box.pack(fill="x", pady=30)
        ctk.CTkLabel(box, text=title, text_color=TEXT, font=self.f_h2).pack()
        muted_label(box, text, anchor="center", justify="center", wraplength=520
                    ).pack(pady=(6, 0))

    def add_sounds(self):
        paths = filedialog.askopenfilenames(
            filetypes=[("Аудио", "*.wav *.mp3 *.ogg *.flac"), ("Все файлы", "*.*")])
        for p in paths:
            self.cfg["sounds"].append({"name": Path(p).stem, "path": p, "hotkey": ""})
        save_cfg(self.cfg)
        self.refresh_sounds()

    def add_sound_item(self, name, path):
        self.cfg["sounds"].append({"name": name, "path": path, "hotkey": ""})
        save_cfg(self.cfg)
        self.refresh_sounds()

    def refresh_sounds(self):
        for w in self.list.winfo_children():
            w.destroy()
        if not self.cfg["sounds"]:
            self._empty_state(self.list, "Пока нет звуков",
                              "Нажмите «Добавить звук» и выберите файл wav, mp3, ogg или flac.")
            return
        for s in self.cfg["sounds"]:
            row = card(self.list)
            row.pack(fill="x", pady=4)
            ctk.CTkButton(row, text=f"▶   {s['name']}", anchor="w", height=40,
                          fg_color="transparent", hover_color=CARD_HOVER, text_color=TEXT,
                          font=self.f_body,
                          command=lambda p=s["path"]: self.engine.play(p)
                          ).pack(side="left", fill="x", expand=True, padx=(6, 0), pady=6)
            ctk.CTkButton(row, text=self._hk_text(s["hotkey"], "Клавиша"), width=120, height=32,
                          corner_radius=8, fg_color=SURFACE, hover_color=CARD_HOVER,
                          border_width=1, border_color=BORDER, text_color=MUTED, font=self.f_small,
                          command=lambda s=s: self.set_sound_hotkey(s)
                          ).pack(side="left", padx=4, pady=6)
            ctk.CTkButton(row, text="✕", width=34, height=32, corner_radius=8,
                          fg_color="transparent", hover_color=DANGER_SOFT, text_color=DANGER,
                          font=self.f_body,
                          command=lambda s=s: self.remove_sound(s)
                          ).pack(side="left", padx=(0, 6), pady=6)

    def set_sound_hotkey(self, s):
        def done(hk):
            s["hotkey"] = hk
            save_cfg(self.cfg)
            self.rebind_all()
            self.refresh_sounds()
            self.report("Готово")
        self.capture_hotkey(done)

    def remove_sound(self, s):
        self.cfg["sounds"].remove(s)
        save_cfg(self.cfg)
        self.rebind_all()
        self.refresh_sounds()
        self.gc(s["path"])

    # ---- вкладка «Голос ИИ»
    def _build_tts(self, tab):
        body = ctk.CTkScrollableFrame(tab, fg_color="transparent", corner_radius=0)
        body.pack(fill="both", expand=True)
        ctk.CTkLabel(body, text="Голос ИИ", anchor="w", text_color=TEXT,
                     font=self.f_h2).pack(anchor="w")
        muted_label(body, "Введите текст, выберите голос и нажмите «Озвучить». Ctrl+Enter — быстрый запуск."
                    ).pack(anchor="w", pady=(2, 0))

        main = card(body)
        main.pack(fill="x", pady=(14, 0))
        inner = ctk.CTkFrame(main, fg_color="transparent")
        inner.pack(fill="x", padx=16, pady=16)

        row = ctk.CTkFrame(inner, fg_color="transparent")
        row.pack(fill="x")
        ctk.CTkLabel(row, text="Голос", anchor="w", text_color=MUTED,
                     font=self.f_small).pack(side="left", padx=(0, 8))
        self.voice_var = ctk.StringVar(value=self.cfg["voice"])
        self.voice_menu = self._menu(row, variable=self.voice_var, width=280,
                                     values=[v["name"] for v in self.cfg["voices"]],
                                     command=self.on_voice_change)
        self.voice_menu.pack(side="left")
        self.voice_menus.append(self.voice_menu)

        self.textbox = ctk.CTkTextbox(inner, height=150, corner_radius=10, fg_color=SURFACE,
                                      border_width=1, border_color=BORDER, text_color=TEXT,
                                      font=self.f_body, wrap="word")
        self.textbox.pack(fill="x", pady=12)
        self.textbox.bind("<Control-Return>", lambda e: (self.speak(), "break")[1])

        row = ctk.CTkFrame(inner, fg_color="transparent")
        row.pack(fill="x")
        self.save_var = ctk.BooleanVar(value=False)
        ctk.CTkSwitch(row, text="Сохранять в «Звуки»", variable=self.save_var,
                      fg_color=BORDER, progress_color=ACCENT, button_color=TEXT,
                      button_hover_color="#ffffff", text_color=MUTED,
                      font=self.f_small).pack(side="left")
        self.speak_btn = ctk.CTkButton(row, text="Озвучить и воспроизвести", height=40,
                                       corner_radius=10, fg_color=ACCENT, hover_color=ACCENT_HOVER,
                                       text_color="#ffffff", font=self.f_body, command=self.speak)
        self.speak_btn.pack(side="right")
        ctk.CTkButton(row, text="★  В фразы", width=120, height=40, corner_radius=10,
                      fg_color=SURFACE, hover_color=CARD_HOVER, border_width=1, border_color=BORDER,
                      text_color=TEXT, font=self.f_body,
                      command=self.phrase_from_textbox).pack(side="right", padx=8)

        search = card(body)
        search.pack(fill="x", pady=(12, 0))
        s = ctk.CTkFrame(search, fg_color="transparent")
        s.pack(fill="x", padx=16, pady=16)
        card_title(s, "Найти голос на Fish Audio").pack(anchor="w")
        muted_label(s, "Поиск по названию или теме. Понравившийся голос добавляется одним нажатием."
                    ).pack(anchor="w", pady=(2, 10))
        row = ctk.CTkFrame(s, fg_color="transparent")
        row.pack(fill="x")
        self.search_entry = self._entry(row, placeholder_text="название / тема голоса")
        self.search_entry.pack(side="left", fill="x", expand=True)
        self.search_entry.bind("<Return>", lambda e: self.search_voices())
        self.lang_var = ctk.StringVar(value="ru")
        self._menu(row, variable=self.lang_var, width=100,
                   values=["любой", "ru", "en", "uk", "ja", "zh"]).pack(side="left", padx=8)
        ctk.CTkButton(row, text="Найти", width=90, height=38, corner_radius=9,
                      fg_color=ACCENT, hover_color=ACCENT_HOVER, text_color="#ffffff",
                      font=self.f_body, command=self.search_voices).pack(side="left")
        self.results = ctk.CTkScrollableFrame(s, height=140, fg_color=SURFACE, corner_radius=10)
        self.results.pack(fill="x", pady=(10, 0))

        add = card(body)
        add.pack(fill="x", pady=(12, 0))
        a = ctk.CTkFrame(add, fg_color="transparent")
        a.pack(fill="x", padx=16, pady=16)
        card_title(a, "Добавить голос по ссылке").pack(anchor="w")
        muted_label(a, "Ссылка вида fish.audio/m/… или 32-символьный ID голоса."
                    ).pack(anchor="w", pady=(2, 10))
        row = ctk.CTkFrame(a, fg_color="transparent")
        row.pack(fill="x")
        self.id_entry = self._entry(row, placeholder_text="ссылка fish.audio/m/… или ID голоса")
        self.id_entry.pack(side="left", fill="x", expand=True)
        self.name_entry = self._entry(row, width=150, placeholder_text="имя (необяз.)")
        self.name_entry.pack(side="left", padx=8)
        ctk.CTkButton(row, text="Добавить", width=100, height=38, corner_radius=9,
                      fg_color=ACCENT, hover_color=ACCENT_HOVER, text_color="#ffffff",
                      font=self.f_body, command=self.add_voice_from_entry).pack(side="left")

    def on_voice_change(self, name):
        self.cfg["voice"] = name
        save_cfg(self.cfg)

    def add_voice(self, name, vid):
        if not any(v["id"] == vid for v in self.cfg["voices"]):
            self.cfg["voices"].append({"name": name, "id": vid})
        else:
            name = next(v["name"] for v in self.cfg["voices"] if v["id"] == vid)
        self.cfg["voice"] = name
        save_cfg(self.cfg)
        for m in self.voice_menus:
            m.configure(values=[v["name"] for v in self.cfg["voices"]])
        self.voice_var.set(name)
        self.phrase_voice_var.set(name)
        self.report(f"Голос «{name}» добавлен")

    def add_voice_from_entry(self):
        m = re.search(r"[0-9a-f]{32}", self.id_entry.get().lower())
        if not m:
            self.report("Не нашёл ID голоса (32 символа) в ссылке")
            return
        vid = m.group(0)
        self.add_voice(self.name_entry.get().strip() or f"Голос {vid[:6]}", vid)

    def search_voices(self):
        q, lang = self.search_entry.get().strip(), self.lang_var.get()
        if not self.cfg["api_key"]:
            self.report("Сначала вставьте API-ключ во вкладке «Настройки»")
            return
        self.report("Ищу голоса…")
        threading.Thread(target=self._search_worker, args=(q, lang), daemon=True).start()

    def _search_worker(self, q, lang):
        params = {"page_size": 15, "page_number": 1, "sort_by": "score"}
        if q:
            params["title"] = q
        if lang != "любой":
            params["language"] = lang
        try:
            r = requests.get(f"{API}/model", headers=self.auth(), params=params, timeout=30)
            r.raise_for_status()
            items = r.json().get("items", [])
        except Exception as e:
            self.report(f"Поиск не удался: {e}")
            return
        self.after(0, lambda: self.show_results(items))

    def show_results(self, items):
        for w in self.results.winfo_children():
            w.destroy()
        if not items:
            muted_label(self.results, "Ничего не найдено. Попробуйте другой запрос или язык.",
                        anchor="center", justify="center", wraplength=480).pack(pady=16)
        for it in items:
            title, vid = it.get("title", "?"), it.get("_id", "")
            row = ctk.CTkFrame(self.results, fg_color="transparent")
            row.pack(fill="x", padx=8, pady=3)
            ctk.CTkLabel(row, text=title[:46], anchor="w", text_color=TEXT,
                         font=self.f_small).pack(side="left", fill="x", expand=True)
            ctk.CTkButton(row, text="+ Добавить", width=104, height=30, corner_radius=8,
                          fg_color=ACCENT_SOFT, hover_color=CARD_HOVER, text_color=TEXT,
                          font=self.f_small,
                          command=lambda t=title, v=vid: self.add_voice(t[:40], v)).pack(side="right")
        self.report(f"Найдено голосов: {len(items)}")

    def speak(self):
        text = self.textbox.get("1.0", "end").strip()
        if not text:
            return
        if not self.cfg["api_key"]:
            self.report("Вставьте API-ключ Fish Audio во вкладке «Настройки»")
            return
        name = self.voice_var.get()
        self.speak_btn.configure(state="disabled", text="Генерирую…")
        threading.Thread(target=self._speak_worker,
                         args=(text, name, self.voice_id(name), self.save_var.get()), daemon=True).start()

    def _speak_worker(self, text, voice, vid, save):
        try:
            path = self.tts_file(text, vid)
            self.engine.play(path)
            self.after(0, lambda: self.add_history(text, voice, vid, path))
            if save:
                name = text[:30] + ("…" if len(text) > 30 else "")
                self.after(0, lambda: self.add_sound_item(name, path))
            self.report("Готово")
        except Exception as e:
            self.report(f"Ошибка: {e}")
        finally:
            self.after(0, lambda: self.speak_btn.configure(state="normal", text="Озвучить и воспроизвести"))

    # ---- общее для озвучки
    def voice_id(self, name):
        return next((v["id"] for v in self.cfg["voices"] if v["name"] == name), "")

    def tts_file(self, text, vid):
        """Блокирующая озвучка, возвращает путь к wav. Вызывать только из потока."""
        if not self.cfg["api_key"]:
            raise RuntimeError("вставьте API-ключ во вкладке «Настройки»")
        body = {"text": text, "format": "wav"}
        if vid:
            body["reference_id"] = vid
        r = requests.post(f"{API}/v1/tts", json=body, timeout=120,
                          headers={**self.auth(), "model": self.cfg["model"]})
        if r.status_code != 200:
            raise RuntimeError(f"Fish Audio {r.status_code}: {r.text[:200]}")
        path = TTS_DIR / f"{int(time.time() * 1000)}.wav"
        path.write_bytes(r.content)
        return str(path)

    def gc(self, path):
        """Удаляет озвученный файл, если на него больше никто не ссылается."""
        p = Path(path)
        if p.parent != TTS_DIR or not p.exists():
            return
        used = {x["path"] for k in ("sounds", "phrases", "history") for x in self.cfg[k]}
        if str(p) not in used:
            try:
                p.unlink()
            except OSError:
                pass

    # ---- вкладка «Фразы»
    def _build_phrases(self, tab):
        head = ctk.CTkFrame(tab, fg_color="transparent")
        head.pack(fill="x")
        ctk.CTkLabel(head, text="Фразы", anchor="w", text_color=TEXT,
                     font=self.f_h2).pack(anchor="w")
        muted_label(head, "Фраза озвучивается один раз, дальше играет мгновенно из файла."
                    ).pack(anchor="w", pady=(2, 0))

        add = card(tab)
        add.pack(fill="x", pady=(14, 0))
        inner = ctk.CTkFrame(add, fg_color="transparent")
        inner.pack(fill="x", padx=14, pady=14)
        self.phrase_entry = self._entry(inner, placeholder_text="Текст фразы")
        self.phrase_entry.pack(side="left", fill="x", expand=True)
        self.phrase_entry.bind("<Return>", lambda e: self.add_phrase_from_entry())
        self.phrase_voice_var = ctk.StringVar(value=self.cfg["voice"])
        menu = self._menu(inner, variable=self.phrase_voice_var, width=170,
                          values=[v["name"] for v in self.cfg["voices"]])
        menu.pack(side="left", padx=8)
        self.voice_menus.append(menu)
        ctk.CTkButton(inner, text="+ Добавить", width=110, height=38, corner_radius=9,
                      fg_color=ACCENT, hover_color=ACCENT_HOVER, text_color="#ffffff",
                      font=self.f_body, command=self.add_phrase_from_entry).pack(side="left")

        muted_label(tab, "Первое нажатие озвучивает фразу, дальше — мгновенно и без трат. ↻ — озвучить заново.",
                    wraplength=720).pack(fill="x", pady=(10, 0))
        self.phrase_list = ctk.CTkScrollableFrame(tab, fg_color="transparent", corner_radius=0)
        self.phrase_list.pack(fill="both", expand=True, pady=(10, 0))

    def add_phrase(self, text, voice, vid, path=""):
        if any(p["text"] == text and p["voice_id"] == vid for p in self.cfg["phrases"]):
            self.report("Такая фраза уже есть")
            return
        self.cfg["phrases"].append(
            {"text": text, "voice": voice, "voice_id": vid, "path": path, "hotkey": ""})
        save_cfg(self.cfg)
        self.refresh_phrases()
        self.report("Фраза добавлена")

    def add_phrase_from_entry(self):
        text = self.phrase_entry.get().strip()
        if text:
            name = self.phrase_voice_var.get()
            self.add_phrase(text, name, self.voice_id(name))
            self.phrase_entry.delete(0, "end")

    def phrase_from_textbox(self):
        text = self.textbox.get("1.0", "end").strip()
        if text:
            name = self.voice_var.get()
            self.add_phrase(text, name, self.voice_id(name))

    def refresh_phrases(self):
        for w in self.phrase_list.winfo_children():
            w.destroy()
        if not self.cfg["phrases"]:
            self._empty_state(self.phrase_list, "Пока нет фраз",
                              "Введите текст выше и нажмите «Добавить» — фраза появится здесь.")
            return
        for ph in self.cfg["phrases"]:
            row = card(self.phrase_list)
            row.pack(fill="x", pady=4)
            ctk.CTkButton(row, text=f"▶   {ph['text'][:60]}", anchor="w", height=40,
                          fg_color="transparent", hover_color=CARD_HOVER, text_color=TEXT,
                          font=self.f_body,
                          command=lambda ph=ph: self.play_phrase(ph)
                          ).pack(side="left", fill="x", expand=True, padx=(6, 0), pady=6)
            ctk.CTkButton(row, text=self._hk_text(ph["hotkey"], "Клавиша"), width=120, height=32,
                          corner_radius=8, fg_color=SURFACE, hover_color=CARD_HOVER,
                          border_width=1, border_color=BORDER, text_color=MUTED, font=self.f_small,
                          command=lambda ph=ph: self.set_phrase_hotkey(ph)
                          ).pack(side="left", padx=4, pady=6)
            ctk.CTkButton(row, text="↻", width=34, height=32, corner_radius=8,
                          fg_color="transparent", hover_color=CARD_HOVER, text_color=MUTED,
                          font=self.f_body,
                          command=lambda ph=ph: self.play_phrase(ph, force=True)
                          ).pack(side="left", pady=6)
            ctk.CTkButton(row, text="✕", width=34, height=32, corner_radius=8,
                          fg_color="transparent", hover_color=DANGER_SOFT, text_color=DANGER,
                          font=self.f_body,
                          command=lambda ph=ph: self.remove_phrase(ph)
                          ).pack(side="left", padx=(4, 6), pady=6)

    def play_phrase(self, ph, force=False):
        threading.Thread(target=self._phrase_worker, args=(ph, force), daemon=True).start()

    def _phrase_worker(self, ph, force):
        try:
            if force or not (ph["path"] and Path(ph["path"]).exists()):
                self.report("Озвучиваю фразу…")
                old = ph["path"]
                ph["path"] = self.tts_file(ph["text"], ph["voice_id"])
                save_cfg(self.cfg)
                if old and old != ph["path"]:
                    self.gc(old)
            self.engine.play(ph["path"])
            self.report("Готово")
        except Exception as e:
            self.report(f"Ошибка: {e}")

    def set_phrase_hotkey(self, ph):
        def done(hk):
            ph["hotkey"] = hk
            save_cfg(self.cfg)
            self.rebind_all()
            self.refresh_phrases()
            self.report("Готово")
        self.capture_hotkey(done)

    def remove_phrase(self, ph):
        self.cfg["phrases"].remove(ph)
        save_cfg(self.cfg)
        self.rebind_all()
        self.refresh_phrases()
        if ph["path"]:
            self.gc(ph["path"])

    # ---- вкладка «История»
    def _build_history(self, tab):
        head = ctk.CTkFrame(tab, fg_color="transparent")
        head.pack(fill="x")
        left = ctk.CTkFrame(head, fg_color="transparent")
        left.pack(side="left", fill="x", expand=True)
        ctk.CTkLabel(left, text="История", anchor="w", text_color=TEXT,
                     font=self.f_h2).pack(anchor="w")
        muted_label(left, f"Последние {HISTORY_MAX} озвучек. ★ — в фразы, ↩ — в редактор."
                    ).pack(anchor="w", pady=(2, 0))
        ctk.CTkButton(head, text="Очистить", width=110, height=34, corner_radius=9,
                      fg_color=SURFACE, hover_color=DANGER_SOFT, border_width=1,
                      border_color=BORDER, text_color=MUTED, font=self.f_small,
                      command=self.clear_history).pack(side="right")
        self.hist_list = ctk.CTkScrollableFrame(tab, fg_color="transparent", corner_radius=0)
        self.hist_list.pack(fill="both", expand=True, pady=(14, 0))

    def add_history(self, text, voice, vid, path):
        h = self.cfg["history"]
        h.insert(0, {"text": text, "voice": voice, "voice_id": vid, "path": path})
        removed = h[HISTORY_MAX:]
        del h[HISTORY_MAX:]
        save_cfg(self.cfg)
        self.refresh_history()
        for e in removed:
            self.gc(e["path"])

    def refresh_history(self):
        for w in self.hist_list.winfo_children():
            w.destroy()
        if not self.cfg["history"]:
            self._empty_state(self.hist_list, "История пуста",
                              "Здесь появятся озвученные фразы — их можно прослушать снова или сохранить в «Фразы».")
            return
        for e in self.cfg["history"]:
            row = card(self.hist_list)
            row.pack(fill="x", pady=4)
            ctk.CTkButton(row, text=f"▶   {e['text'][:44]}", anchor="w", height=40,
                          fg_color="transparent", hover_color=CARD_HOVER, text_color=TEXT,
                          font=self.f_body,
                          command=lambda p=e["path"]: self.engine.play(p)
                          ).pack(side="left", fill="x", expand=True, padx=(6, 0), pady=6)
            ctk.CTkLabel(row, text=e["voice"][:14], width=110, anchor="w",
                         text_color=MUTED, font=self.f_small).pack(side="left")
            ctk.CTkButton(row, text="★", width=34, height=32, corner_radius=8,
                          fg_color="transparent", hover_color=CARD_HOVER, text_color=MUTED,
                          font=self.f_body,
                          command=lambda e=e: self.add_phrase(e["text"], e["voice"], e["voice_id"], e["path"])
                          ).pack(side="left", pady=6)
            ctk.CTkButton(row, text="↩", width=34, height=32, corner_radius=8,
                          fg_color="transparent", hover_color=CARD_HOVER, text_color=MUTED,
                          font=self.f_body,
                          command=lambda e=e: self.history_to_textbox(e)
                          ).pack(side="left", padx=4, pady=6)
            ctk.CTkButton(row, text="✕", width=34, height=32, corner_radius=8,
                          fg_color="transparent", hover_color=DANGER_SOFT, text_color=DANGER,
                          font=self.f_body,
                          command=lambda e=e: self.remove_history(e)
                          ).pack(side="left", padx=(0, 6), pady=6)

    def history_to_textbox(self, e):
        self.textbox.delete("1.0", "end")
        self.textbox.insert("1.0", e["text"])
        if any(v["name"] == e["voice"] for v in self.cfg["voices"]):
            self.voice_var.set(e["voice"])
        self.tabs.set("Голос ИИ")

    def remove_history(self, e):
        self.cfg["history"].remove(e)
        save_cfg(self.cfg)
        self.refresh_history()
        self.gc(e["path"])

    def clear_history(self):
        old, self.cfg["history"] = self.cfg["history"], []
        save_cfg(self.cfg)
        self.refresh_history()
        for e in old:
            self.gc(e["path"])

    # ---- вкладка «Настройки»
    def _build_settings(self, tab):
        body = ctk.CTkScrollableFrame(tab, fg_color="transparent", corner_radius=0)
        body.pack(fill="both", expand=True)
        ctk.CTkLabel(body, text="Настройки", anchor="w", text_color=TEXT,
                     font=self.f_h2).pack(anchor="w")
        muted_label(body, "Изменения сохраняются сразу. Ключ и модель применяются при вводе."
                    ).pack(anchor="w", pady=(2, 0))

        # Fish Audio
        api = card(body)
        api.pack(fill="x", pady=(14, 0))
        a = ctk.CTkFrame(api, fg_color="transparent")
        a.pack(fill="x", padx=16, pady=16)
        card_title(a, "Fish Audio").pack(anchor="w")
        muted_label(a, "API-ключ: fish.audio/app/api-keys. Хранится только на этом компьютере."
                    ).pack(anchor="w", pady=(2, 12))

        muted_label(a, "API-ключ").pack(anchor="w")
        self.key_var = ctk.StringVar(value=self.cfg["api_key"])
        self.key_entry = self._entry(a, show="•", textvariable=self.key_var)
        self.key_entry.pack(fill="x", pady=(4, 12))
        self.key_var.trace_add("write", lambda *_: self.live_save("api_key", self.key_var.get().strip()))

        muted_label(a, "Модель").pack(anchor="w")
        self.model_var = ctk.StringVar(value=self.cfg["model"])
        self.model_entry = self._entry(a, textvariable=self.model_var)
        self.model_entry.pack(fill="x", pady=(4, 0))
        self.model_var.trace_add("write", lambda *_: self.live_save("model", self.model_var.get().strip()))

        # Устройства
        dev = card(body)
        dev.pack(fill="x", pady=(12, 0))
        d = ctk.CTkFrame(dev, fg_color="transparent")
        d.pack(fill="x", padx=16, pady=16)
        card_title(d, "Устройства").pack(anchor="w")
        muted_label(d, "CABLE Input — виртуальный микрофон. В игре выбирается CABLE Output."
                    ).pack(anchor="w", pady=(2, 12))

        outs = [NONE] + list(list_devices("output"))
        ins = [NONE] + list(list_devices("input"))
        self.cable_var = ctk.StringVar(value=self.cfg["cable_device"] or NONE)
        self.monitor_var = ctk.StringVar(value=self.cfg["monitor_device"] or NONE)
        self.mic_var = ctk.StringVar(value=self.cfg["mic_device"] or NONE)

        head = ctk.CTkFrame(d, fg_color="transparent")
        head.pack(fill="x", pady=(0, 6))
        muted_label(head, "Если подключили наушники или микрофон при запущенной программе — "
                          "нажмите «Обновить».").pack(side="left", fill="x", expand=True)
        ctk.CTkButton(head, text="Обновить", width=104, height=30, corner_radius=8,
                      fg_color=SURFACE, hover_color=CARD_HOVER, border_width=1, border_color=BORDER,
                      text_color=TEXT, font=self.f_small,
                      command=self.refresh_device_lists).pack(side="right")

        muted_label(d, "Виртуальный микрофон — выход в кабель (CABLE Input)").pack(anchor="w")
        self.cable_menu, cable_test = self._device_row(d, self.cable_var, outs)
        cable_test.configure(command=lambda: self.test_output(self.cable_var))
        self.vol_cable = ctk.CTkSlider(d, from_=0, to=1, height=16, fg_color=BORDER,
                                       progress_color=ACCENT, button_color=TEXT,
                                       button_hover_color="#ffffff",
                                       command=lambda v: self.cfg.update(vol_cable=float(v)))
        self.vol_cable.set(self.cfg["vol_cable"])
        self.vol_cable.pack(fill="x", pady=(0, 12))

        muted_label(d, "Мои наушники / динамики (чтобы слышать самому)").pack(anchor="w")
        self.monitor_menu, monitor_test = self._device_row(d, self.monitor_var, outs)
        monitor_test.configure(command=lambda: self.test_output(self.monitor_var))
        self.vol_monitor = ctk.CTkSlider(d, from_=0, to=1, height=16, fg_color=BORDER,
                                         progress_color=ACCENT, button_color=TEXT,
                                         button_hover_color="#ffffff",
                                         command=lambda v: self.cfg.update(vol_monitor=float(v)))
        self.vol_monitor.set(self.cfg["vol_monitor"])
        self.vol_monitor.pack(fill="x", pady=(0, 12))
        for slider in (self.vol_cable, self.vol_monitor):
            try:   # громкость запоминается, когда отпускаем ползунок
                slider.bind("<ButtonRelease-1>", lambda _e: save_cfg(self.cfg))
            except Exception:
                pass

        self.pt_switch = ctk.CTkSwitch(d, text="Пропускать мой реальный микрофон в кабель",
                                       fg_color=BORDER, progress_color=ACCENT, button_color=TEXT,
                                       button_hover_color="#ffffff", text_color=TEXT,
                                       font=self.f_body, command=self.save_settings)
        if self.cfg["passthrough"]:
            self.pt_switch.select()
        self.pt_switch.pack(anchor="w", pady=(0, 8))
        self.mic_menu, mic_test = self._device_row(d, self.mic_var, ins, test_text="🎤")
        mic_test.configure(command=self.test_mic)

        # VB-CABLE
        cab = card(body)
        cab.pack(fill="x", pady=(12, 0))
        c = ctk.CTkFrame(cab, fg_color="transparent")
        c.pack(fill="x", padx=16, pady=16)
        card_title(c, "Виртуальный микрофон VB-CABLE").pack(anchor="w")
        muted_label(c, "Бесплатный драйвер VB-Audio. Нужен, чтобы игра слышала звуки и голос."
                    ).pack(anchor="w", pady=(2, 12))
        row = ctk.CTkFrame(c, fg_color="transparent")
        row.pack(fill="x")
        ctk.CTkButton(row, text="Установить VB-CABLE", height=38, corner_radius=9,
                      fg_color=SURFACE, hover_color=CARD_HOVER, border_width=1, border_color=BORDER,
                      text_color=TEXT, font=self.f_body,
                      command=self.install_cable_async).pack(side="left")
        ctk.CTkButton(row, text="Удалить", height=38, corner_radius=9,
                      fg_color="transparent", hover_color=DANGER_SOFT, text_color=DANGER,
                      font=self.f_body,
                      command=self.uninstall_cable_async).pack(side="left", padx=8)
        ctk.CTkButton(row, text="Сохранить", height=38, corner_radius=9,
                      fg_color=ACCENT, hover_color=ACCENT_HOVER, text_color="#ffffff",
                      font=self.f_body, command=self.save_settings).pack(side="right")

    def _device_row(self, parent, variable, values, test_text="▶"):
        """Список устройств и кнопка проверки рядом: длина списка больше не влияет на вёрстку."""
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", pady=(4, 6))
        menu = self._menu(row, variable=variable, values=values,
                          command=lambda _v: self.save_settings())
        menu.pack(side="left", fill="x", expand=True)
        btn = ctk.CTkButton(row, text=test_text, width=44, height=36, corner_radius=9,
                            fg_color=SURFACE, hover_color=CARD_HOVER, border_width=1,
                            border_color=BORDER, text_color=TEXT, font=self.f_small)
        btn.pack(side="left", padx=(8, 0))
        return menu, btn

    def test_output(self, variable):
        """Писк в выбранное устройство: сразу понятно, туда ли уходит звук."""
        dev = resolve_device(variable.get(), "output")
        if dev is None:
            self.report("Сначала выберите устройство вывода")
            return
        self.engine.beep(dev)
        try:
            name = sd.query_devices(dev)["name"]
        except Exception:
            name = variable.get()
        self.report(f"Писк в «{name}»")

    def test_mic(self):
        """Три секунды слушает выбранный микрофон и показывает уровень в строке состояния."""
        dev = resolve_device(self.mic_var.get(), "input")
        if dev is None:
            self.report("Сначала выберите микрофон")
            return
        self.report("Слушаю микрофон 3 секунды: скажите что-нибудь…")

        def worker():
            try:
                peak = record_peak(dev, 3.0, lambda _v: None)
            except Exception as e:
                self.report(f"Микрофон недоступен: {e}")
                return
            _ok, msg = verdict(
                peak,
                "Микрофон слышно. Если вас всё равно не слышно в игре — включите пропуск микрофона",
                "Микрофон молчит: проверьте, что он включён в Windows и разрешён приложениям",
                "Микрофон слышно, но очень тихо: увеличьте его громкость в параметрах Windows")
            self.report(msg)

        threading.Thread(target=worker, daemon=True).start()

    def refresh_device_lists(self):
        """Перечитывает устройства: Bluetooth-наушники видны только после переподключения."""
        self._guess_devices()
        outs = [NONE] + list(list_devices("output"))
        ins = [NONE] + list(list_devices("input"))
        self.cable_menu.configure(values=outs)
        self.monitor_menu.configure(values=outs)
        self.mic_menu.configure(values=ins)
        self.cable_var.set(self.cfg["cable_device"] or NONE)
        self.monitor_var.set(self.cfg["monitor_device"] or NONE)
        self.mic_var.set(self.cfg["mic_device"] or NONE)
        save_cfg(self.cfg)
        self.engine.restart_passthrough()
        self.report("Список устройств обновлён")

    def live_save(self, key, value):
        """Ключ и модель применяются сразу при вводе или вставке, без кнопки «Сохранить»."""
        self.cfg[key] = value or (DEFAULT_MODEL if key == "model" else "")
        save_cfg(self.cfg)

    def save_settings(self):
        un = lambda v: "" if v == NONE else v
        self.cfg.update(
            api_key=self.key_entry.get().strip(),
            model=self.model_entry.get().strip() or DEFAULT_MODEL,
            cable_device=un(self.cable_var.get()),
            monitor_device=un(self.monitor_var.get()),
            mic_device=un(self.mic_var.get()),
            passthrough=bool(self.pt_switch.get()),
            vol_cable=float(self.vol_cable.get()),
            vol_monitor=float(self.vol_monitor.get()),
        )
        save_cfg(self.cfg)
        self.engine.restart_passthrough()
        self.report("Настройки сохранены")


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
