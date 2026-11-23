"""Windows-специфичные функции: иконки/тема окна и установка VB-CABLE."""
import io
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests

from voicepad_config import (APP_DIR, CABLE_HOSTS, CABLE_PAGE, CABLE_PUBLISHER,
                             CABLE_SETUP, HTTP_HEADERS, MAX_DOWNLOAD)


def program_dir():
    """Папка, где лежит программа (exe или скрипт)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


def resource_path(name):
    """Файл, упакованный в exe (или лежащий рядом со скриптом)."""
    return Path(getattr(sys, "_MEIPASS", program_dir())) / name


def use_dark_titlebar(window):
    """Тёмная полоса заголовка окна: приложение не светится белым на фоне системы."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        user32 = ctypes.windll.user32
        hwnd = user32.GetParent(window.winfo_id()) or window.winfo_id()
        dwm = ctypes.windll.dwmapi
        on = ctypes.c_int(1)
        for attr in (20, 19):            # DWMWA_USE_IMMERSIVE_DARK_MODE (Win11 / Win10)
            if dwm.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(on), ctypes.sizeof(on)) == 0:
                break
        caption = ctypes.c_int(0x00130E0B)   # COLORREF: цвет фона приложения
        border = ctypes.c_int(0x00392C24)    # COLORREF: цвет рамки
        dwm.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(caption), ctypes.sizeof(caption))
        dwm.DwmSetWindowAttribute(hwnd, 34, ctypes.byref(border), ctypes.sizeof(border))
    except Exception:
        pass


def set_window_icon(window, ico):
    """Ставит иконку окну: через tkinter и напрямую через Windows API (CTk иногда подменяет свою)."""
    use_dark_titlebar(window)
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


def official_cable_url(url):
    """Только HTTPS и только сайт VB-Audio в каталоге /Cable/."""
    try:
        p = urlparse(url)
    except ValueError:
        return False
    return (p.scheme == "https" and (p.hostname or "").lower() in CABLE_HOSTS
            and p.path.lower().startswith("/cable/"))


def fetch_official(url, max_bytes=MAX_DOWNLOAD):
    """Скачивает с официального сайта, проверяя адрес, редиректы и размер."""
    if not official_cable_url(url):
        raise RuntimeError("ссылка ведёт не на официальный сайт VB-Audio — скачивание отменено")
    with requests.get(url, headers=HTTP_HEADERS, timeout=(15, 90), stream=True) as r:
        r.raise_for_status()
        if not official_cable_url(r.url):
            raise RuntimeError("сервер перенаправил на сторонний сайт — скачивание отменено")
        chunks, total = [], 0
        for part in r.iter_content(64 * 1024):
            total += len(part)
            if total > max_bytes:
                raise RuntimeError("файл слишком большой — скачивание отменено")
            chunks.append(part)
    return b"".join(chunks)


def verify_cable_setup(path):
    """Не запускаем elevated-установщик без действительной подписи VB-Audio."""
    try:
        with open(path, "rb") as f:
            header = f.read(2)
        if path.stat().st_size < 100_000 or header != b"MZ":
            raise RuntimeError("файл не похож на установщик Windows — запуск отменён")
    except OSError as e:
        raise RuntimeError(f"не удалось прочитать установщик: {e}") from e
    if sys.platform != "win32":
        return
    ps = ("$ErrorActionPreference='SilentlyContinue'; "
          "$s=Get-AuthenticodeSignature -FilePath '{0}'; "
          "try {{ $c=$s.SignerCertificate.Subject }} catch {{ $c='' }}; "
          "Write-Output ('STATUS=' + $s.Status); Write-Output ('SUBJECT=' + $c)"
          ).format(str(path).replace("'", "''"))
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                             capture_output=True, text=True, timeout=90,
                             creationflags=0x08000000, check=False)
    except Exception as e:
        raise RuntimeError(f"не удалось проверить подпись установщика: {e}") from e
    info = dict(line.split("=", 1) for line in out.stdout.splitlines() if "=" in line)
    status = info.get("STATUS", "").strip()
    subject = info.get("SUBJECT", "").strip()
    subject_normalized = subject.lower().replace("-", "").replace(" ", "")
    if status.lower() != "valid" or CABLE_PUBLISHER not in subject_normalized:
        raise RuntimeError("цифровая подпись VB-Audio не подтверждена — запуск отменён. "
                           "Скачайте установщик вручную с vb-audio.com/Cable")


def extract_cable_setup(data, target):
    """Безопасная распаковка: только ожидаемый установщик, без выхода за папку."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, OSError) as e:
        raise RuntimeError("скачанный файл не является ZIP-архивом — запуск отменён") from e
    with archive:
        names = archive.namelist()
        member = next((n for n in names
                       if Path(n).name.lower() == CABLE_SETUP.lower() and not n.endswith("/")), None)
        if not member:
            raise RuntimeError(f"в архиве нет {CABLE_SETUP}")
        prefix = member[:len(member) - len(CABLE_SETUP)]
        root = target.resolve()
        total = 0
        for name in names:
            if name.endswith("/") or not name.startswith(prefix):
                continue
            rel = name[len(prefix):]
            if not rel or Path(rel).is_absolute() or ".." in Path(rel).parts or "\\" in rel:
                continue
            out = (target / rel).resolve()
            if out != root and root not in out.parents:
                continue
            try:
                content = archive.read(name)
            except (OSError, zipfile.BadZipFile):
                continue
            total += len(content)
            if total > MAX_DOWNLOAD:
                raise RuntimeError("архив слишком большой — распаковка отменена")
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(content)
    setup = target / CABLE_SETUP
    if not setup.exists():
        raise RuntimeError("установщик не удалось распаковать из архива")
    verify_cable_setup(setup)
    return setup


def run_elevated(exe, *args):
    """Запускает exe с правами администратора и ждёт завершения (Windows покажет запрос UAC)."""
    q = lambda x: str(x).replace("'", "''")
    arglist = ",".join(f"'{a}'" for a in args)
    cmd = ("$ErrorActionPreference='Stop'; "
           f"$p=Start-Process -FilePath '{q(exe)}' -WorkingDirectory '{q(exe.parent)}' "
           f"-ArgumentList {arglist} -Verb RunAs -Wait -PassThru; "
           "if($p){exit $p.ExitCode}else{exit 1}")
    proc = subprocess.run(["powershell", "-NoProfile", "-Command", cmd],
                          capture_output=True, text=True,
                          creationflags=0x08000000, check=False)
    if proc.returncode:
        detail = (proc.stderr or proc.stdout or "").strip()[:200]
        raise RuntimeError(f"установщик завершился с кодом {proc.returncode}. {detail}")


def install_cable(say):
    """Скачивает VB-CABLE только с сайта VB-Audio, проверяет подпись и запускает установку.
    Windows попросит права администратора и подтверждение установки драйвера."""
    say("Ищу VB-CABLE на сайте VB-Audio…")
    html = fetch_official(CABLE_PAGE, max_bytes=5 * 1024 * 1024).decode("utf-8", "replace")
    if "<html" not in html[:2000].lower():
        raise RuntimeError("сайт VB-Audio вернул не HTML — установка отменена")
    m = re.search(r'href=["\']([^"\']*VBCABLE_Driver_Pack[^"\']*\.zip)["\']', html, re.I)
    if not m:
        raise RuntimeError("не нашёл ссылку на скачивание — структура сайта могла измениться, "
                           "воспользуйтесь ручной установкой")
    url = urljoin(CABLE_PAGE, m.group(1))
    if not re.fullmatch(r"VBCABLE_Driver_Pack\d*\.zip", Path(urlparse(url).path).name, re.I):
        raise RuntimeError("найденная ссылка не похожа на архив VB-CABLE — установка отменена")
    say("Скачиваю VB-CABLE…")
    data = fetch_official(url)
    say("Проверяю подпись установщика…")
    setup = extract_cable_setup(data, vbcable_dir())
    say("Подтвердите запросы Windows (права администратора и установка драйвера)…")
    run_elevated(setup, "-i", "-h")


def uninstall_cable(say):
    setup = find_cable_setup()
    if not setup:
        raise RuntimeError("VoicePad не устанавливал VB-CABLE. Если он стоит у вас отдельно, "
                           "удалите его через «Установку и удаление программ»")
    verify_cable_setup(setup)
    say("Подтвердите запросы Windows…")
    run_elevated(setup, "-u", "-h")
    shutil.rmtree(setup.parent, ignore_errors=True)
