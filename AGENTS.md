# AGENTS.md

Правила для AI-агентов, работающих с этим репозиторием.

## Что это

VoicePad — саундпад для Windows с озвучкой текста через Fish Audio API.
Пользователь пишет фразу, программа синтезирует речь и проигрывает её одновременно
в виртуальный микрофон (VB-CABLE) и в наушники, чтобы звук попал в игру или Discord.

## Стек и структура

- `voicepad.py` — всё приложение в одном файле (Python 3.12, customtkinter).
- `installer.iss` — скрипт Inno Setup для установщика. Версия приложения — `MyAppVersion`.
- `build.bat` — локальная сборка exe через PyInstaller.
- `build_installer.bat` — локальная сборка exe + установщика.
- `.github/workflows/build.yml` — CI: сборка exe и установщика при публикации релиза.
- `index.html` — страница скачивания (GitHub Pages), ссылки ведут на `releases/latest/download`.
- `CNAME` — домен GitHub Pages (`voicepad.cc.cd`). **Не удалять и не менять без просьбы.**

## Запуск из исходников

```
pip install customtkinter sounddevice soundfile numpy requests keyboard
python voicepad.py
```

## Сборка

- exe: `build.bat` (PyInstaller, `--onefile --noconsole`, иконка `voicepad.ico`).
- установщик: `build_installer.bat` (нужен Inno Setup 6).
- В CI то же самое делает `.github/workflows/build.yml` при `release: published`.

## Релизы

1. Поднять `MyAppVersion` в `installer.iss`.
2. Закоммитить и запушить в `main`.
3. Создать тег `v<версия>` и опубликовать релиз:
   `gh release create v1.0.4 --title "VoicePad 1.0.4" --notes "..."`
4. Workflow сам соберёт и прикрепит `VoicePad-Setup.exe` и `VoicePad.exe`.

Теги в истории разнобойные (`v1.0`, `v1.0.1`, `v.1.0.3`) — для новых релизов использовать `v1.0.N` без точки после `v`.

## Правила работы

- Сборка Windows-only. Не переписывать приложение под другие ОС.
- Не менять `AppId` в `installer.iss` — иначе установщик не увидит старые версии.
- Не коммитить `dist/`, `build/`, `installer/`, `*.spec`, `config.json`, `tts/`.
- `git push --force`, `git reset --hard`, `git clean`, удаление релизов — только по явной просьбе.
- Пользовательские данные лежат в `%USERPROFILE%\.voicepad` (настройки, ключ API, озвучка) — в репозиторий не попадают.
- Секреты (API-ключ Fish Audio) в коде и логах не хранить.
- Пользователь общается по-русски, интерфейс и README — на русском.

## Проверка перед коммитом

- `python -c "import ast; ast.parse(open('voicepad.py', encoding='utf-8').read())"` — синтаксис.
- Приложение поддерживает self-test: при переменной окружения `VOICEPAD_SMOKE=<файл>`
  оно запускается, пишет `ok` в файл и завершается (используется в CI).
