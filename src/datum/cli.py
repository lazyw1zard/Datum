"""Command line: the same operations as the MCP server, for any agent with a shell and for people."""
from __future__ import annotations

import argparse
import json
import sys

from . import NAME, __version__, ops, scad
from .engines.openscad import OpenSCADMissing, VIEWS
from .parts import PartError, Store


def _cuts(values: list[str]) -> list[tuple[str, float]]:
    cuts = []
    for item in values or []:
        axis, _, rest = item.partition("=")
        for at in rest.split(","):
            if at.strip():
                cuts.append((axis.strip().lower(), float(at)))
    return cuts


def _updates(pairs: list[str]) -> dict:
    updates = {}
    for pair in pairs:
        name, sep, value = pair.partition("=")
        if not sep or not name.strip():
            raise PartError(f"expected name=value, got {pair!r}")
        updates[name.strip()] = scad.parse_cli_value(value.strip())
    return updates


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog=NAME, description="Параметрические 3D-детали для агентов и людей.")
    p.add_argument("--home", help="папка с деталями (иначе $DATUM_HOME или ~/Datum)")
    p.add_argument("--json", action="store_true", help="ответ в JSON вместо текста")
    p.add_argument("--version", action="version", version=f"{NAME} {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("doctor", help="где дом, найден ли OpenSCAD и библиотеки")
    sub.add_parser("list", help="все детали")

    s = sub.add_parser("new", help="новая деталь: из шаблона, из .scad или из сетки (STL/3MF/OBJ/STEP)")
    s.add_argument("part")
    s.add_argument("--from", dest="source", help="исходный .scad или файл сетки")
    s.add_argument("--title")
    s.add_argument("--desc", default="")
    s.add_argument("--no-build", action="store_true")

    s = sub.add_parser("show", help="деталь и её текущая (или указанная) версия")
    s.add_argument("part")
    s.add_argument("--v", dest="ver")

    s = sub.add_parser("params", help="параметры из исходника: имя = значение // комментарий")
    s.add_argument("part")

    s = sub.add_parser("set", help="поменять параметры и собрать новую версию: set втулка HEX_S=8.3 fit=0.1")
    s.add_argument("part")
    s.add_argument("pairs", nargs="+", metavar="name=value")
    s.add_argument("--note", default="")
    s.add_argument("--force", action="store_true", help="разрешить заменить производный параметр литералом")
    s.add_argument("--no-build", action="store_true")

    s = sub.add_parser("build", help="собрать новую версию из текущего исходника")
    s.add_argument("part")
    s.add_argument("--note", default="")
    s.add_argument("--views", help=f"виды превью через запятую: {', '.join(VIEWS)}")

    s = sub.add_parser("pull", help="взять исходник заново оттуда, откуда пришла деталь (генератор переписал), "
                                    "и собрать, если он изменился")
    s.add_argument("part")
    s.add_argument("--note", default="")
    s.add_argument("--force", action="store_true", help="затереть правки исходника в самой детали после сборки")
    s.add_argument("--views", help=f"виды превью через запятую: {', '.join(VIEWS)}")

    s = sub.add_parser("versions", help="история версий")
    s.add_argument("part")

    s = sub.add_parser("section", help="сечение плоскостью: отверстия (Ø), шестигранники (S), контуры + SVG")
    s.add_argument("part")
    s.add_argument("--axis", default="z", choices=["x", "y", "z"])
    s.add_argument("--at", type=float, help="координата плоскости, мм (по умолчанию середина)")
    s.add_argument("--v", dest="ver")

    s = sub.add_parser("measure", help="габарит и сечения: measure втулка --cut z=1,10,20 --cut x=0")
    s.add_argument("part")
    s.add_argument("--cut", action="append", default=[], help="ось=координаты через запятую")
    s.add_argument("--v", dest="ver")

    s = sub.add_parser("view", help="путь к самодостаточному 3D-просмотру (view.html)")
    s.add_argument("part")
    s.add_argument("--v", dest="ver")
    s.add_argument("--open", action="store_true", help="открыть в браузере")
    s.add_argument("--refresh", action="store_true", help="пересобрать view.html этой версии (после обновления Datum)")

    s = sub.add_parser("export", help="сохранить сетку версии: .stl .3mf .glb .obj .ply")
    s.add_argument("part")
    s.add_argument("out")
    s.add_argument("--v", dest="ver")
    s.add_argument("--template", help="проект слайсера .3mf: сетка встанет вместо его объекта, "
                                      "настройки печати и место на столе останутся")
    return p


def run(args: argparse.Namespace) -> dict:
    store = Store(args.home)
    cmd = args.cmd
    if cmd == "doctor":
        return ops.doctor(store)
    if cmd == "list":
        return ops.list_parts(store)
    if cmd == "new":
        return ops.new(store, args.part, source=args.source, title=args.title, description=args.desc,
                       build_after=not args.no_build)
    if cmd == "show":
        return ops.show(store, args.part, args.ver)
    if cmd == "params":
        return ops.params(store, args.part)
    if cmd == "set":
        return ops.set_params(store, args.part, _updates(args.pairs), force=args.force, note=args.note,
                              build_after=not args.no_build)
    if cmd == "build":
        views = [v.strip() for v in args.views.split(",")] if args.views else None
        return ops.build(store, args.part, note=args.note, views=views)
    if cmd == "pull":
        views = [v.strip() for v in args.views.split(",")] if args.views else None
        return ops.pull(store, args.part, note=args.note, force=args.force, views=views)
    if cmd == "versions":
        return ops.versions(store, args.part)
    if cmd == "section":
        return ops.section(store, args.part, args.axis, args.at, args.ver)
    if cmd == "measure":
        return ops.measure(store, args.part, _cuts(args.cut), args.ver)
    if cmd == "view":
        return ops.view(store, args.part, args.ver, open_browser=args.open, refresh=args.refresh)
    if cmd == "export":
        return ops.export(store, args.part, args.out, args.ver, template=args.template)
    raise PartError(f"unknown command {cmd}")


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # Windows consoles default to a legacy code page
        except (AttributeError, ValueError):
            pass
    args = parser().parse_args(argv)
    try:
        result = run(args)
    except (PartError, OpenSCADMissing, KeyError, ValueError) as exc:
        message = exc.args[0] if isinstance(exc, KeyError) and exc.args else str(exc)
        if args.json:
            print(json.dumps({"ok": False, "error": message}, ensure_ascii=False))
        else:
            print(f"{NAME}: {message}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps({k: v for k, v in result.items() if k != "text"}, ensure_ascii=False, indent=2, default=str))
    else:
        print(result["text"])
    return 0 if result.get("ok", True) else 1


if __name__ == "__main__":
    sys.exit(main())
