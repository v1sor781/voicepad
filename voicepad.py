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
import re
import shutil
import subprocess
import sys
import threading
import time
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
DEFAULT_MODEL = "s2.1-pro-free"  # бесплатная модель Fish Audio API; платная: "s2-pro"
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
    """{имя: индекс} для 'input' / 'output'. Берём только MME — он самый совместимый."""
    api = next((i for i, h in enumerate(sd.query_hostapis()) if h["name"] == "MME"), None)
    res = {}
    for i, d in enumerate(sd.query_devices()):
        if d[f"max_{kind}_channels"] > 0 and (api is None or d["hostapi"] == api):
            res[d["name"]] = i
    return res


CABLE_PAGE = "https://vb-audio.com/Cable/"
CABLE_SETUP = "VBCABLE_Setup_x64.exe"


def program_dir():
    """Папка, где лежит программа (exe или скрипт)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


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
    html = requests.get(CABLE_PAGE, timeout=30).text
    m = re.search(r'href="([^"]*VBCABLE_Driver_Pack[^"]*\.zip)"', html, re.I)
    if not m:
        raise RuntimeError("не нашёл ссылку на скачивание")
    say("Скачиваю VB-CABLE…")
    data = requests.get(urljoin(CABLE_PAGE, m.group(1)), timeout=120).content
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

    def _run(self, path, token):
        try:
            data, sr = sf.read(path, dtype="float32", always_2d=True)
        except Exception as e:
            self.report(f"Не удалось прочитать {Path(path).name}: {e}")
            return
        outs = list_devices("output")
        started = False
        for dev_key, vol_key in (("cable_device", "vol_cable"), ("monitor_device", "vol_monitor")):
            idx = outs.get(self.cfg[dev_key])
            if idx is not None:
                started = True
                threading.Thread(
                    target=self._out, args=(idx, data, sr, self.cfg[vol_key], token), daemon=True
                ).start()
        if not started:
            self.report("Выберите устройства вывода во вкладке «Настройки»")

    def _out(self, dev, data, sr, vol, token):
        ch = min(data.shape[1], 2)
        buf = np.clip(data[:, :ch] * vol, -1.0, 1.0)
        try:
            with sd.OutputStream(samplerate=sr, device=dev, channels=ch, dtype="float32") as s:
                for i in range(0, len(buf), 2048):
                    if token != self.token:
                        s.abort()
                        return
                    s.write(np.ascontiguousarray(buf[i:i + 2048]))
        except Exception as e:
            self.report(f"Ошибка вывода: {e}")

    def restart_passthrough(self):
        """Пропускает реальный микрофон в кабель, чтобы в игре слышали и тебя, и звуки."""
        if self.pt:
            try:
                self.pt.abort()
                self.pt.close()
            except Exception:
                pass
            self.pt = None
        if not self.cfg["passthrough"]:
            return
        o = list_devices("output").get(self.cfg["cable_device"])
        i = list_devices("input").get(self.cfg["mic_device"])
        if o is None or i is None:
            self.report("Для пропуска микрофона выберите микрофон и кабель")
            return

        def cb(indata, outdata, frames, t, status):
            outdata[:] = indata

        try:
            self.pt = sd.Stream(samplerate=48000, device=(i, o), channels=1, dtype="float32", callback=cb)
            self.pt.start()
        except Exception as e:
            self.report(f"Пропуск микрофона не запустился: {e}")


# ---------------------------------------------------------------- интерфейс

class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")
        self.title("VoicePad")
        self.geometry("780x680")
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
        if not any("cable" in n.lower() for n in list_devices("output")):
            self.after(600, self.show_cable_hint)

    # ---- служебное
    def report(self, msg):
        self.after(0, lambda: self.status.configure(text=msg))

    def auth(self):
        return {"Authorization": f"Bearer {self.cfg['api_key']}"}

    def _guess_devices(self):
        outs = list_devices("output")
        if not self.cfg["cable_device"] or self.cfg["cable_device"] not in outs:
            self.cfg["cable_device"] = next((n for n in outs if "cable input" in n.lower()), "")
        if not self.cfg["monitor_device"] or self.cfg["monitor_device"] not in outs:
            try:
                self.cfg["monitor_device"] = sd.query_devices(sd.default.device[1])["name"]
            except Exception:
                self.cfg["monitor_device"] = ""

    def show_cable_hint(self):
        win = ctk.CTkToplevel(self)
        win.title("Нужен виртуальный микрофон")
        win.geometry("460x330")
        win.attributes("-topmost", True)
        ctk.CTkLabel(
            win, wraplength=420, justify="left",
            text=("Не нашёл виртуальный микрофон.\n\n"
                  "Windows не даёт программе говорить в настоящий микрофон напрямую, поэтому нужен "
                  "бесплатный драйвер VB-CABLE от VB-Audio. Программа скачает его с официального "
                  "сайта и запустит установку. Windows попросит подтвердить права администратора "
                  "и установку драйвера."),
        ).pack(padx=20, pady=(20, 8))
        lbl = ctk.CTkLabel(win, text="", wraplength=420, text_color="#9ab")
        lbl.pack(pady=4)
        btn = ctk.CTkButton(win, text="Установить автоматически")
        btn.configure(command=lambda: self.install_cable_async(btn, lbl))
        btn.pack(pady=4)
        ctk.CTkButton(win, text="Скачать вручную", fg_color="#444", hover_color="#555",
                      command=lambda: webbrowser.open(CABLE_PAGE)).pack(pady=4)
        ctk.CTkButton(win, text="Закрыть", fg_color="#444", hover_color="#555",
                      command=win.destroy).pack(pady=4)

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

    def on_close(self):
        save_cfg(self.cfg)
        self.engine.stop()
        keyboard.unhook_all()
        self.destroy()

    # ---- хоткеи
    def rebind_all(self):
        keyboard.unhook_all_hotkeys()
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
        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.pack(fill="x", padx=12, pady=(10, 0))
        ctk.CTkButton(bar, text="■ Стоп", width=90, fg_color="#b33", hover_color="#922",
                      command=self.engine.stop).pack(side="left")
        self.stop_hk_btn = ctk.CTkButton(bar, width=140, fg_color="#444", hover_color="#555",
                                         text=self._hk_text(self.cfg["stop_hotkey"], "Клавиша «Стоп»"),
                                         command=self.set_stop_hotkey)
        self.stop_hk_btn.pack(side="left", padx=8)

        self.status = ctk.CTkLabel(self, text="", anchor="w", text_color="#9ab")
        self.status.pack(side="bottom", fill="x", padx=14, pady=6)

        self.tabs = ctk.CTkTabview(self)
        self.tabs.pack(fill="both", expand=True, padx=12, pady=8)
        for name in ("Звуки", "Фразы", "Голос ИИ", "История", "Настройки"):
            self.tabs.add(name)
        self._build_sounds(self.tabs.tab("Звуки"))
        self._build_phrases(self.tabs.tab("Фразы"))
        self._build_tts(self.tabs.tab("Голос ИИ"))
        self._build_history(self.tabs.tab("История"))
        self._build_settings(self.tabs.tab("Настройки"))

    @staticmethod
    def _hk_text(hk, empty):
        return hk if hk else empty

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
        ctk.CTkButton(tab, text="+ Добавить звук", command=self.add_sounds).pack(anchor="w")
        self.list = ctk.CTkScrollableFrame(tab)
        self.list.pack(fill="both", expand=True, pady=8)

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
        for s in self.cfg["sounds"]:
            row = ctk.CTkFrame(self.list, fg_color="transparent")
            row.pack(fill="x", pady=2)
            ctk.CTkButton(row, text=f"▶  {s['name']}", anchor="w",
                          command=lambda p=s["path"]: self.engine.play(p)).pack(side="left", fill="x", expand=True)
            ctk.CTkButton(row, text=self._hk_text(s["hotkey"], "Клавиша"), width=110,
                          fg_color="#444", hover_color="#555",
                          command=lambda s=s: self.set_sound_hotkey(s)).pack(side="left", padx=4)
            ctk.CTkButton(row, text="✕", width=34, fg_color="#633", hover_color="#a44",
                          command=lambda s=s: self.remove_sound(s)).pack(side="left")

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
        row = ctk.CTkFrame(tab, fg_color="transparent")
        row.pack(fill="x")
        ctk.CTkLabel(row, text="Голос:").pack(side="left")
        self.voice_var = ctk.StringVar(value=self.cfg["voice"])
        self.voice_menu = ctk.CTkOptionMenu(row, variable=self.voice_var, width=260,
                                            values=[v["name"] for v in self.cfg["voices"]],
                                            command=self.on_voice_change)
        self.voice_menu.pack(side="left", padx=8)
        self.voice_menus.append(self.voice_menu)

        self.textbox = ctk.CTkTextbox(tab, height=130)
        self.textbox.pack(fill="x", pady=8)
        self.textbox.bind("<Control-Return>", lambda e: (self.speak(), "break")[1])

        row = ctk.CTkFrame(tab, fg_color="transparent")
        row.pack(fill="x")
        self.save_var = ctk.BooleanVar(value=False)
        ctk.CTkSwitch(row, text="Сохранять фразу в «Звуки»", variable=self.save_var).pack(side="left")
        self.speak_btn = ctk.CTkButton(row, text="Озвучить и воспроизвести", command=self.speak)
        self.speak_btn.pack(side="right")
        ctk.CTkButton(row, text="★ В фразы", width=90, fg_color="#444", hover_color="#555",
                      command=self.phrase_from_textbox).pack(side="right", padx=6)

        ctk.CTkLabel(tab, text="Найти голос на Fish Audio", anchor="w",
                     font=ctk.CTkFont(weight="bold")).pack(fill="x", pady=(14, 2))
        row = ctk.CTkFrame(tab, fg_color="transparent")
        row.pack(fill="x")
        self.search_entry = ctk.CTkEntry(row, placeholder_text="название / тема голоса")
        self.search_entry.pack(side="left", fill="x", expand=True)
        self.search_entry.bind("<Return>", lambda e: self.search_voices())
        self.lang_var = ctk.StringVar(value="ru")
        ctk.CTkOptionMenu(row, variable=self.lang_var, width=90,
                          values=["любой", "ru", "en", "uk", "ja", "zh"]).pack(side="left", padx=6)
        ctk.CTkButton(row, text="Найти", width=80, command=self.search_voices).pack(side="left")
        self.results = ctk.CTkScrollableFrame(tab, height=130)
        self.results.pack(fill="x", pady=6)

        row = ctk.CTkFrame(tab, fg_color="transparent")
        row.pack(fill="x")
        self.id_entry = ctk.CTkEntry(row, placeholder_text="ссылка fish.audio/m/… или ID голоса")
        self.id_entry.pack(side="left", fill="x", expand=True)
        self.name_entry = ctk.CTkEntry(row, width=130, placeholder_text="имя (необяз.)")
        self.name_entry.pack(side="left", padx=6)
        ctk.CTkButton(row, text="Добавить", width=80, command=self.add_voice_from_entry).pack(side="left")

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
        for it in items:
            title, vid = it.get("title", "?"), it.get("_id", "")
            row = ctk.CTkFrame(self.results, fg_color="transparent")
            row.pack(fill="x", pady=1)
            ctk.CTkLabel(row, text=title[:46], anchor="w").pack(side="left", fill="x", expand=True)
            ctk.CTkButton(row, text="+ Добавить", width=100,
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
        row = ctk.CTkFrame(tab, fg_color="transparent")
        row.pack(fill="x")
        self.phrase_entry = ctk.CTkEntry(row, placeholder_text="текст фразы")
        self.phrase_entry.pack(side="left", fill="x", expand=True)
        self.phrase_entry.bind("<Return>", lambda e: self.add_phrase_from_entry())
        self.phrase_voice_var = ctk.StringVar(value=self.cfg["voice"])
        menu = ctk.CTkOptionMenu(row, variable=self.phrase_voice_var, width=170,
                                 values=[v["name"] for v in self.cfg["voices"]])
        menu.pack(side="left", padx=6)
        self.voice_menus.append(menu)
        ctk.CTkButton(row, text="+ Добавить", width=100, command=self.add_phrase_from_entry).pack(side="left")
        ctk.CTkLabel(tab, anchor="w", justify="left", text_color="#9ab", wraplength=700,
                     text="Первое нажатие озвучивает фразу, дальше она играет мгновенно из сохранённого "
                          "файла (без трат). ↻ — озвучить заново.").pack(fill="x", pady=(6, 0))
        self.phrase_list = ctk.CTkScrollableFrame(tab)
        self.phrase_list.pack(fill="both", expand=True, pady=8)

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
        for ph in self.cfg["phrases"]:
            row = ctk.CTkFrame(self.phrase_list, fg_color="transparent")
            row.pack(fill="x", pady=2)
            ctk.CTkButton(row, text=f"▶  {ph['text'][:60]}", anchor="w", height=36,
                          command=lambda ph=ph: self.play_phrase(ph)).pack(side="left", fill="x", expand=True)
            ctk.CTkButton(row, text=self._hk_text(ph["hotkey"], "Клавиша"), width=110, height=36,
                          fg_color="#444", hover_color="#555",
                          command=lambda ph=ph: self.set_phrase_hotkey(ph)).pack(side="left", padx=4)
            ctk.CTkButton(row, text="↻", width=34, height=36, fg_color="#444", hover_color="#555",
                          command=lambda ph=ph: self.play_phrase(ph, force=True)).pack(side="left")
            ctk.CTkButton(row, text="✕", width=34, height=36, fg_color="#633", hover_color="#a44",
                          command=lambda ph=ph: self.remove_phrase(ph)).pack(side="left", padx=(4, 0))

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
        top = ctk.CTkFrame(tab, fg_color="transparent")
        top.pack(fill="x")
        ctk.CTkLabel(top, text=f"Последние {HISTORY_MAX} озвучек", anchor="w").pack(side="left")
        ctk.CTkButton(top, text="Очистить", width=90, fg_color="#444", hover_color="#555",
                      command=self.clear_history).pack(side="right")
        self.hist_list = ctk.CTkScrollableFrame(tab)
        self.hist_list.pack(fill="both", expand=True, pady=8)

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
        for e in self.cfg["history"]:
            row = ctk.CTkFrame(self.hist_list, fg_color="transparent")
            row.pack(fill="x", pady=2)
            ctk.CTkButton(row, text=f"▶  {e['text'][:44]}", anchor="w",
                          command=lambda p=e["path"]: self.engine.play(p)).pack(side="left", fill="x", expand=True)
            ctk.CTkLabel(row, text=e["voice"][:14], width=100, text_color="#889").pack(side="left")
            ctk.CTkButton(row, text="★", width=34, fg_color="#444", hover_color="#555",
                          command=lambda e=e: self.add_phrase(e["text"], e["voice"], e["voice_id"], e["path"])
                          ).pack(side="left")
            ctk.CTkButton(row, text="↩", width=34, fg_color="#444", hover_color="#555",
                          command=lambda e=e: self.history_to_textbox(e)).pack(side="left", padx=4)
            ctk.CTkButton(row, text="✕", width=34, fg_color="#633", hover_color="#a44",
                          command=lambda e=e: self.remove_history(e)).pack(side="left")

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
        def label(text):
            ctk.CTkLabel(tab, text=text, anchor="w").pack(fill="x", pady=(8, 0))

        label("API-ключ Fish Audio (fish.audio/app/api-keys)")
        self.key_entry = ctk.CTkEntry(tab, show="•")
        self.key_entry.insert(0, self.cfg["api_key"])
        self.key_entry.pack(fill="x")

        label("Модель (заголовок model)")
        self.model_entry = ctk.CTkEntry(tab)
        self.model_entry.insert(0, self.cfg["model"])
        self.model_entry.pack(fill="x")

        outs = [NONE] + list(list_devices("output"))
        ins = [NONE] + list(list_devices("input"))
        self.cable_var = ctk.StringVar(value=self.cfg["cable_device"] or NONE)
        self.monitor_var = ctk.StringVar(value=self.cfg["monitor_device"] or NONE)
        self.mic_var = ctk.StringVar(value=self.cfg["mic_device"] or NONE)

        label("Виртуальный микрофон — выход в кабель (CABLE Input)")
        ctk.CTkOptionMenu(tab, variable=self.cable_var, values=outs).pack(fill="x")
        self.vol_cable = ctk.CTkSlider(tab, from_=0, to=1, command=lambda v: self.cfg.update(vol_cable=float(v)))
        self.vol_cable.set(self.cfg["vol_cable"])
        self.vol_cable.pack(fill="x", pady=4)

        label("Мои наушники / динамики (чтобы слышать самому)")
        ctk.CTkOptionMenu(tab, variable=self.monitor_var, values=outs).pack(fill="x")
        self.vol_monitor = ctk.CTkSlider(tab, from_=0, to=1, command=lambda v: self.cfg.update(vol_monitor=float(v)))
        self.vol_monitor.set(self.cfg["vol_monitor"])
        self.vol_monitor.pack(fill="x", pady=4)

        self.pt_switch = ctk.CTkSwitch(tab, text="Пропускать мой реальный микрофон в кабель")
        if self.cfg["passthrough"]:
            self.pt_switch.select()
        self.pt_switch.pack(anchor="w", pady=(10, 2))
        ctk.CTkOptionMenu(tab, variable=self.mic_var, values=ins).pack(fill="x")

        row = ctk.CTkFrame(tab, fg_color="transparent")
        row.pack(pady=(14, 0))
        ctk.CTkButton(row, text="Установить VB-CABLE", fg_color="#444", hover_color="#555",
                      command=self.install_cable_async).pack(side="left", padx=4)
        ctk.CTkButton(row, text="Удалить VB-CABLE", fg_color="#444", hover_color="#555",
                      command=self.uninstall_cable_async).pack(side="left", padx=4)
        ctk.CTkButton(tab, text="Сохранить", command=self.save_settings).pack(pady=14)

    def save_settings(self):
        un = lambda v: "" if v == NONE else v
        self.cfg.update(
            api_key=self.key_entry.get().strip(),
            model=self.model_entry.get().strip() or DEFAULT_MODEL,
            cable_device=un(self.cable_var.get()),
            monitor_device=un(self.monitor_var.get()),
            mic_device=un(self.mic_var.get()),
            passthrough=bool(self.pt_switch.get()),
        )
        save_cfg(self.cfg)
        self.engine.restart_passthrough()
        self.report("Настройки сохранены")


if __name__ == "__main__":
    App().mainloop()
