"""Работа с аудиоустройствами, ресемплинг и воспроизведение VoicePad."""
import re
import threading
import time
from pathlib import Path

import numpy as np
import sounddevice as sd
import soundfile as sf


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


def same_name_devices(idx, kind):
    """Все индексы устройств с тем же именем (разные host-API), WASAPI первым.

    MME обрезает названия до 31 символа, поэтому имя от MME не совпадает с полным
    именем от WASAPI. Считаем такие имена одним устройством: одно — начало другого.
    """
    try:
        base = sd.query_devices(idx).get("name", "").strip().lower()
        if not base:
            return [idx]
    except Exception:
        return [idx]

    def same(name):
        n = name.strip().lower()
        if n == base:
            return True
        # обрезанное имя MME: сравниваем по началу, но только для длинных названий,
        # чтобы «Микрофон» не совпал со всеми микрофонами сразу
        return len(n) >= 20 and (n.startswith(base) or base.startswith(n))

    result = []
    try:
        for i, d in enumerate(sd.query_devices()):
            if d.get(f"max_{kind}_channels", 0) > 0 and same(d.get("name", "")):
                result.append(i)
    except Exception:
        return [idx]
    if idx not in result:
        result.insert(0, idx)
    try:
        wasapi = next((n for n, h in enumerate(sd.query_hostapis()) if h["name"] == "Windows WASAPI"), None)
        if wasapi is not None:
            result.sort(key=lambda i: 0 if sd.query_devices(i).get("hostapi") == wasapi else 1)
    except Exception:
        pass
    return result or [idx]


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
    """Безлимитный ресемплинг через FFT: без «лестницы» линейной интерполяции
    и с подавлением частот выше нового Найквиста при понижении sr.

    Работает с mono и многоканальными массивами; короткие/пустые буферы не ломаются.
    """
    data = np.asarray(data)
    if not len(data) or sr <= 0 or new_sr <= 0 or sr == new_sr:
        return data.astype(np.float32, copy=False)
    frames = max(1, int(round(len(data) * new_sr / sr)))
    x = np.asarray(data, dtype=np.float64)
    flat = x.ndim == 1
    if flat:
        x = x[:, None]

    # мягкое затухание краёв уменьшает разрыв на стыке периодического FFT
    fade = min(64, len(x) // 8)
    if fade > 1:
        x = x.copy()
        ramp = np.linspace(0.0, 1.0, fade)
        x[:fade] *= ramp[:, None]
        x[-fade:] *= ramp[::-1, None]

    spec = np.fft.rfft(x, axis=0)
    if new_sr < sr:
        freqs = np.fft.rfftfreq(len(x), d=1.0 / sr)
        spec[freqs > (new_sr / 2.0) * 0.95] = 0.0   # антиалиасинг до децимации
    out_spec = np.zeros((frames // 2 + 1, x.shape[1]), dtype=np.complex128)
    take = min(out_spec.shape[0], spec.shape[0])
    out_spec[:take] = spec[:take]
    y = np.fft.irfft(out_spec, n=frames, axis=0)
    y = np.clip(y, -1.0, 1.0).astype(np.float32)
    return y[:, 0] if flat else y



SILENT_LEVEL = 0.003   # ниже: полная тишина (около -50 дБ)
OK_LEVEL = 0.02        # выше: сигнал слышно нормально (около -34 дБ)

PA_HINTS = {
    -9999: "устройство занято другой программой или недоступно",
    -9998: "устройство не поддерживает нужное число каналов",
    -9997: "устройство не поддерживает такую частоту дискретизации",
    -9996: "устройства нет в системе — возможно, оно отключилось",
    -9993: "устройство не поддерживает такой формат",
    -9992: "не удалось запустить поток: проверьте, что устройство не занято",
}


def audio_error_text(exc):
    """Текст ошибки звука: сообщение драйвера плюс понятная подсказка по коду PortAudio."""
    text = str(exc).strip() or exc.__class__.__name__
    low = text.lower()
    # у WASAPI PaErrorCode -9999 может скрывать конкретную причину — читаем её из текста
    if "audclnt_e_unsupported_format" in low or "unsupported format" in low:
        hint = "формат не поддерживается устройством: проверьте формат по умолчанию в свойствах устройства Windows"
    elif "audclnt_e_device_in_use" in low or "device in use" in low:
        hint = "устройство занято другой программой"
    elif "audclnt_e_device_invalidated" in low or "device invalidated" in low:
        hint = "устройство отключилось или стало недоступно"
    elif "0x00000490" in low or "wdmsyncioctl" in low or "deviceiocontrol gle" in low:
        hint = "драйвер устройства не отвечает (часто помогает перезагрузка VB-CABLE или ПК)"
    elif "audclnt_e_exclusive_mode_not_allowed" in low:
        hint = "эксклюзивный режим запрещён: отключите его в свойствах устройства"
    else:
        match = re.search(r"PaErrorCode\s*(-?\d+)", text)
        hint = PA_HINTS.get(int(match.group(1))) if match else None
        if not hint and "busy" in low:
            hint = "устройство занято другой программой"
    return text + (f" — {hint}" if hint else "")



def find_input(substr):
    """Индекс устройства записи, в названии которого есть substr (например, 'cable output')."""
    for name, idx in list_devices("input").items():
        if substr in name.lower():
            return idx
    return None


def find_inputs(substr):
    """Все индексы устройств записи, в названии которых есть substr (по всем host-API)."""
    result = []
    try:
        for i, d in enumerate(sd.query_devices()):
            if d.get("max_input_channels", 0) > 0 and substr in d.get("name", "").lower():
                result.append(i)
    except Exception:
        pass
    # сначала WASAPI (обычно стабильнее), потом остальные
    try:
        wasapi = next((n for n, h in enumerate(sd.query_hostapis()) if h["name"] == "Windows WASAPI"), None)
        if wasapi is not None:
            result.sort(key=lambda i: 0 if sd.query_devices(i).get("hostapi") == wasapi else 1)
    except Exception:
        pass
    return result


def record_peak(dev, seconds, on_level):
    """Слушает устройство записи и возвращает максимальный уровень 0..1.
    on_level(v) вызывается для каждого блока (для индикатора уровня).
    Если драйвер не берёт текущий формат, пробуем другие каналы и частоты."""
    try:
        info = sd.query_devices(dev)
    except Exception:
        info = {}
    default_sr = int(info.get("default_samplerate") or 48000)
    max_ch = int(info.get("max_input_channels") or 1)
    peak = [0.0]

    def cb(indata, frames, t, status):
        if not frames:
            return
        lvl = float(np.abs(indata).max())
        peak[0] = max(peak[0], lvl)
        on_level(lvl)

    rates = []
    for rate in (default_sr, 48000, 44100, 32000, 22050, 16000):
        if rate and rate not in rates:
            rates.append(rate)
    channels = []
    for ch in (min(max_ch, 2), 1, 2):
        if 1 <= ch <= max(max_ch, 2) and ch not in channels:
            channels.append(ch)
    dtypes = ("float32", "int32", "int16")
    modes = (
        ("wasapi", lambda: sd.WasapiSettings(auto_convert=True, exclusive=False)),
        ("default", lambda: None),
        ("wasapi-excl", lambda: sd.WasapiSettings(auto_convert=False, exclusive=True)),
    )

    errors = []
    seen = set()
    for ch in channels:
        for rate in rates:
            for dtype in dtypes:
                for mode_name, mode_fn in modes:
                    key = (ch, rate, dtype, mode_name)
                    if key in seen:
                        continue
                    seen.add(key)
                    try:
                        extra = mode_fn()
                    except Exception:
                        continue
                    try:
                        stream = sd.InputStream(device=dev, channels=ch, samplerate=rate,
                                                dtype=dtype, extra_settings=extra, callback=cb)
                        with stream:
                            time.sleep(seconds)
                        return peak[0]
                    except Exception as e:
                        errors.append(f"{ch}ch {rate}Hz {dtype} {mode_name}: {audio_error_text(e)}")
                        if peak[0] > 0:
                            return peak[0]      # сигнал уже шёл, ошибка не критична
    # показываем разные причины, а не повтор одного и того же
    unique = []
    for msg in errors:
        reason = msg.split(": ", 1)[1] if ": " in msg else msg
        short = msg.split(": ", 1)[0] + ": " + (reason[:120] + "…" if len(reason) > 120 else reason)
        if short not in unique:
            unique.append(short)
        if len(unique) >= 6:
            break
    if not unique:
        unique = ["неизвестная причина"]
    tried = f" (пробовано вариантов: {len(errors)}, частоты: {', '.join(str(r) for r in rates)} Гц)" if errors else ""
    dev_info = ""
    try:
        di = sd.query_devices(dev)
        host = ""
        try:
            host = sd.query_hostapis(di.get("hostapi"))["name"]
        except Exception:
            pass
        dev_info = (f" Устройство: «{di.get('name', dev)}» ({host}), "
                    f"каналов записи {di.get('max_input_channels')}, "
                    f"частота по умолчанию {int(di.get('default_samplerate') or 0)} Гц.")
    except Exception:
        pass
    raise RuntimeError("устройство записи не открылось (" + "; ".join(unique) + ")" + tried + "." + dev_info)


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
        self._token_lock = threading.Lock()
        self.pt = None

    def _next_token(self):
        with self._token_lock:
            self.token += 1
            return self.token

    def _token(self):
        with self._token_lock:
            return self.token

    def stop(self):
        self._next_token()

    def play(self, path):
        token = self._next_token()
        threading.Thread(target=self._run, args=(path, token), daemon=True).start()

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
        # Общий барьер: все выходы начинают писать одновременно. Аппаратной
        # синхронизации разные устройства не гарантируют, но старт совпадает.
        # abort снимает ожидание сразу, если одно из устройств не открылось.
        barrier = threading.Barrier(len(targets))
        abort = threading.Event()
        for dev, vol in targets:
            threading.Thread(target=self._out, args=(dev, data, sr, vol, token, barrier, abort),
                             daemon=True).start()

    def beep(self, dev, seconds=0.8):
        """Короткий писк в конкретное устройство — кнопка «проверить» рядом с выбором."""
        token = self._next_token()
        data, sr = make_tone(seconds=seconds)
        threading.Thread(target=self._out, args=(dev, data, sr, 0.6, token), daemon=True).start()

    def _out(self, dev, data, sr, vol, token, barrier=None, abort=None):
        try:
            info = sd.query_devices(dev)
        except Exception:
            info = {}
        max_ch = int(info.get("max_output_channels") or 0)
        if max_ch <= 0:
            if abort is not None:
                abort.set()
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
                        if barrier is not None:
                            while abort is None or not abort.is_set():
                                try:
                                    barrier.wait(timeout=0.1)
                                    break
                                except threading.BrokenBarrierError:
                                    break  # другое устройство не открылось — играем сами
                        for i in range(0, len(chunk), 2048):
                            if token != self._token():
                                s.abort()
                                return
                            s.write(np.ascontiguousarray(chunk[i:i + 2048]))
                    return
                except Exception as e:
                    if token != self._token():
                        return
                    last = e
        if abort is not None:
            abort.set()
        name = info.get("name") or "устройство вывода"
        reason = audio_error_text(last) if last is not None else "неизвестная причина"
        self.report(f"Не удалось вывести звук в «{name}»: {reason}. "
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
