# Datum

Параметрические 3D-детали для агентов и людей. Агент меняет параметры детали одной командой и получает новую версию: STL, превью, сводку размеров, сечения с отверстиями и самодостаточный 3D-просмотр, который человек открывает прямо из чата.

Вырос из CADRelay (MakeLayer, веб-сервис обмена моделями): оттуда конвертер форматов, чтение превью из 3MF и вьюер. Веб-сервис, вход, роли и базы данных убраны: для локальной работы они не нужны.

## Как устроено

```
<дом>/parts/<деталь>/part.json          что за деталь, какая версия текущая
<дом>/parts/<деталь>/part.scad          исходник (или model.<ext> для импортированной сетки)
<дом>/parts/<деталь>/versions/v003/     сборка, после неё не меняется:
    source.scad  model.stl  preview_*.png  view.html  report.json  version.json  build.log
```

- **Исходник — единственный источник правды.** Параметры — верхнеуровневые присваивания `.scad` с комментарием. `set` правит число прямо в файле, комментарий остаётся, и файл открывается в OpenSCAD с текущими значениями. Производные (`L = L_BODY + BOSS_H;`) меняются через то, от чего зависят.
- **Версии не меняются.** Каждая сборка — новая папка со снимком исходника. Упавшая сборка тоже остаётся, с ошибками и логом, но текущей остаётся последняя удачная.
- **Базы данных нет.** Папки и JSON, как у доски. Быстро, без сервера, переносится копированием.
- **`view.html` — один файл без сервера и без интернета.** Внутри Three.js и сетка. Виды 3D / спереди / справа / сверху, сетка, оси, габарит, каркас, эскиз рёбрами, свет, тёмный фон, замер по двум точкам, снимок.

## Установка

Нужны Python 3.10+ и [OpenSCAD](https://openscad.org). Зависимости — только `numpy` и `trimesh`; для STEP/IGES по желанию `gmsh`.

```bash
python -m venv .venv
.venv/bin/pip install -e .          # Windows: .venv\Scripts\pip install -e .
.venv/bin/pip install -e ".[step]"  # если нужен импорт STEP/IGES
datum doctor
```

Где искать OpenSCAD, Datum решает сам: `$DATUM_OPENSCAD`, потом `PATH`, потом обычные места установки на Windows, macOS и Linux. На Linux без дисплея PNG рендерятся через `xvfb-run`, если он стоит. Библиотеки (BOSL2 и др.) — через `OPENSCADPATH` или `$DATUM_OPENSCADPATH`.

Дом с деталями: `--home`, потом `$DATUM_HOME`, потом `~/Datum`.

## Команды

```bash
datum new vtulka --from vtulka.scad --title "Втулка M5"   # из исходника; без --from — из шаблона
datum new kryshka --from lid.3mf                          # импорт сетки: STL, 3MF, OBJ, GLB, STEP
datum params vtulka                                       # имя = значение // комментарий
datum set vtulka HEX_S=8.3 fit=0.1 --note "под гайку"     # правка + новая версия
datum measure vtulka --cut z=5,17.5,20                    # габарит и сечения
datum section vtulka --at 17.5                            # «отверстие: 6-gon, S8.3»
datum view vtulka --open                                  # 3D-просмотр в браузере
datum versions vtulka
datum export vtulka out.3mf
```

У каждой команды есть `--json` для машинного ответа.

## MCP-сервер

Те же операции для любого MCP-клиента; превью приходят агенту картинками.

```bash
claude mcp add datum -- datum-mcp                 # Claude Code
```

```toml
# Codex: ~/.codex/config.toml
[mcp_servers.datum]
command = "datum-mcp"
```

Инструменты: `doctor`, `list_parts`, `show_part`, `part_params`, `new_part`, `set_params`, `build_part`, `part_versions`, `section`, `measure`, `view_path`, `export_mesh`.

## Переименование

Имя проекта задано в одном месте — `src/datum/__init__.py`, — от него зависят команды, переменные окружения и папка по умолчанию. Если имя окажется занято:

```bash
python tools/rename.py kalibr --dry-run   # посмотреть, что изменится
python tools/rename.py kalibr             # переименовать: пакет, команды, DATUM_* -> KALIBR_*, документы
```

## Тесты

```bash
python -m unittest discover -s tests
```

Тесты сборки пропускаются, если OpenSCAD не установлен. CI гоняет остальные на Linux, macOS и Windows, а сборку — на Linux с OpenSCAD и xvfb.

## Дальше

- Fusion 360 вторым движком, через его MCP-коннектор: твёрдые тела, STEP, CAM.
- Упаковка в 3MF по шаблону проекта слайсера (профиль QIDI, PETG).
- Галерея всех деталей и версий: маленький локальный сервер.
- Согласование версий (принять / отклонить) — если понадобится, как было в CADRelay.

Three.js — MIT, `src/datum/vendor/THREE_LICENSE`.
