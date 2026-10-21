"""A self-contained 3D view of a part: one HTML file, no server, no network.

Small enough for a chat side panel (which refuses HTML over 512 KB): Three.js (vendored, MIT)
is stored gzip-compressed, the mesh is quantized to 16 bits per axis, delta + zigzag + varint
encoded and gzipped. The page unpacks both with DecompressionStream and loads the modules from
blob: URLs. The controls follow CADRelay's GlbViewer, rebuilt without React.
"""
from __future__ import annotations

import base64
import gzip
import json
from functools import lru_cache
from pathlib import Path

import numpy as np

VENDOR = Path(__file__).parent / "vendor"
PANEL_LIMIT = 512 * 1024   # Claude's desktop side panel opens HTML only up to this size


def _gz64(data: bytes) -> str:
    return base64.b64encode(gzip.compress(data, 9, mtime=0)).decode("ascii")


@lru_cache(maxsize=1)
def _vendor() -> str:
    return json.dumps({
        "core": _gz64((VENDOR / "three.core.min.js").read_bytes()),
        "three": _gz64((VENDOR / "three.module.min.js").read_bytes()),
        "controls": _gz64((VENDOR / "OrbitControls.js").read_bytes()),
    })


def _varints(values: np.ndarray) -> bytes:
    """Signed integers -> zigzag -> LEB128 varints."""
    zig = ((values.astype(np.int64) << 1) ^ (values.astype(np.int64) >> 63)).astype(np.uint64)
    out = bytearray()
    for x in zig.tolist():
        while x >= 0x80:
            out.append((x & 0x7F) | 0x80)
            x >>= 7
        out.append(x)
    return bytes(out)


def encode_mesh(vertices: np.ndarray, faces: np.ndarray) -> dict:
    """Vertices in order of first use, 16-bit over the bounding box, deltas per axis; face indices as deltas."""
    v = np.asarray(vertices, dtype=float)
    f = np.asarray(faces, dtype=np.int64)
    used, first = np.unique(f.ravel(), return_index=True)
    order = used[np.argsort(first)]
    remap = np.full(len(v), -1, dtype=np.int64)
    remap[order] = np.arange(len(order))
    v, f = v[order], remap[f]
    lo = v.min(axis=0)
    span = np.maximum(v.max(axis=0) - lo, 1e-9)
    q = np.round((v - lo) / span * 65535).astype(np.int64)
    dv = np.diff(q, axis=0, prepend=np.zeros((1, 3), dtype=np.int64))
    di = np.diff(f.ravel(), prepend=0)
    return {"lo": lo.tolist(), "span": span.tolist(), "vertices": int(len(order)), "triangles": int(len(f)),
            "v": _gz64(_varints(dv.T.ravel())), "f": _gz64(_varints(di))}


def build_html(vertices: np.ndarray, faces: np.ndarray, meta: dict) -> str:
    data = {"meta": meta, "mesh": encode_mesh(vertices, faces)}
    title = meta.get("title") or meta.get("part") or "part"
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    return (_TEMPLATE
            .replace("%%TITLE%%", _html_escape(f"{title} · {meta.get('version', '')}".strip(" ·")))
            .replace("%%VENDOR%%", _vendor())
            .replace("%%DATA%%", payload))


def write(path: Path, vertices: np.ndarray, faces: np.ndarray, meta: dict) -> int:
    """Write view.html; returns its size in bytes."""
    html = build_html(vertices, faces, meta)
    path.write_text(html, encoding="utf-8")
    return path.stat().st_size


def _html_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


_TEMPLATE = r"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>%%TITLE%%</title>
<style>
:root { --bg:#f4f6f8; --panel:#ffffffee; --ink:#0f172a; --muted:#64748b; --line:#d7dee5; --accent:#147a68; --btn:#eef2f5; --btn-on:#147a68; --btn-on-ink:#fff; }
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) { --bg:#0f141a; --panel:#161d26ee; --ink:#e5e7eb; --muted:#94a3b8; --line:#2a3542; --accent:#34d399; --btn:#1f2935; --btn-on:#34d399; --btn-on-ink:#0f141a; } }
:root[data-theme="dark"] { --bg:#0f141a; --panel:#161d26ee; --ink:#e5e7eb; --muted:#94a3b8; --line:#2a3542; --accent:#34d399; --btn:#1f2935; --btn-on:#34d399; --btn-on-ink:#0f141a; }
* { box-sizing: border-box; }
html, body { margin:0; height:100%; background:var(--bg); color:var(--ink); font:14px/1.4 system-ui, -apple-system, "Segoe UI", sans-serif; overflow:hidden; }
#stage { position:fixed; inset:0; }
#stage canvas { display:block; width:100%; height:100%; touch-action:none; }
.bar { position:fixed; left:12px; right:12px; top:12px; display:flex; flex-wrap:wrap; gap:6px; align-items:center; pointer-events:none; }
.group { display:flex; gap:4px; padding:4px; background:var(--panel); border:1px solid var(--line); border-radius:10px; pointer-events:auto; backdrop-filter: blur(6px); }
button { font:inherit; font-size:13px; color:var(--ink); background:var(--btn); border:0; border-radius:7px; padding:5px 9px; cursor:pointer; }
button:hover { filter:brightness(0.96); }
button.on { background:var(--btn-on); color:var(--btn-on-ink); }
#info { position:fixed; right:12px; bottom:12px; width:min(340px, calc(100vw - 24px)); max-height:55vh; overflow:auto; background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:12px 14px; backdrop-filter: blur(6px); }
#info h1 { font-size:15px; margin:0 0 2px; }
#info .sub { color:var(--muted); font-size:12px; margin-bottom:8px; }
#info table { border-collapse:collapse; width:100%; font-size:12.5px; }
#info td { padding:2px 0; vertical-align:top; }
#info td:first-child { color:var(--muted); padding-right:10px; white-space:nowrap; }
#info details { margin-top:8px; }
#info summary { cursor:pointer; color:var(--muted); font-size:12.5px; }
#info .p td:first-child { font-family:ui-monospace, Consolas, monospace; color:var(--ink); }
#info .p td.c { color:var(--muted); font-size:11.5px; }
#measure { position:fixed; left:12px; bottom:12px; background:var(--panel); border:1px solid var(--accent); border-radius:10px; padding:8px 12px; display:none; font-variant-numeric: tabular-nums; }
#hint { color:var(--muted); font-size:12px; }
#err { position:fixed; inset:0; display:none; place-items:center; color:var(--muted); }
#loading { position:fixed; inset:0; display:grid; place-items:center; color:var(--muted); padding:24px; text-align:center; }
@media (max-width: 640px) { #info { max-height:34vh; } button { padding:5px 7px; } }
</style>
<script type="application/json" id="datum-vendor">%%VENDOR%%</script>
<script type="application/json" id="datum-data">%%DATA%%</script>
</head>
<body>
<div id="stage"></div>
<div class="bar">
  <div class="group" id="views">
    <button data-view="iso" class="on" title="1">3D</button>
    <button data-view="front" title="2">Спереди</button>
    <button data-view="right" title="3">Справа</button>
    <button data-view="top" title="4">Сверху</button>
  </div>
  <div class="group" id="toggles">
    <button data-t="grid" class="on" title="G">Сетка</button>
    <button data-t="axes" class="on">Оси</button>
    <button data-t="box">Габарит</button>
    <button data-t="wire">Каркас</button>
    <button data-t="sketch">Эскиз</button>
    <button data-t="measure" title="M">Замер</button>
  </div>
  <div class="group">
    <button id="light" title="Свет">Свет: студия</button>
    <button id="theme">Фон</button>
    <button id="shot">Снимок</button>
    <button id="full">⛶</button>
  </div>
</div>
<div id="measure"></div>
<div id="info"></div>
<div id="err">WebGL недоступен в этом окне — откройте файл в браузере.</div>
<div id="loading">Загрузка…</div>
<script type="text/plain" id="datum-app">
import * as THREE from "three";
import { OrbitControls } from "three/addons/OrbitControls.js";

const DATA = JSON.parse(document.getElementById("datum-data").textContent);
const META = DATA.meta || {};
const LIGHTS = [
  { id:"studio", label:"студия", hemi:0.95, sky:0xffffff, ground:0xc7d2fe, key:0.9, keyPos:[4,8,5], fill:0.25, fillPos:[-5,3,-4], exposure:1 },
  { id:"technical", label:"техсвет", hemi:1.15, sky:0xffffff, ground:0xe2e8f0, key:0.55, keyPos:[2,6,7], fill:0.35, fillPos:[-4,4,-3], exposure:0.96 },
  { id:"contrast", label:"контраст", hemi:0.55, sky:0xf8fafc, ground:0x94a3b8, key:1.35, keyPos:[5,7,3], fill:0.12, fillPos:[-3,2,-5], exposure:1.04 },
  { id:"flat", label:"ровный", hemi:1.35, sky:0xffffff, ground:0xffffff, key:0.15, keyPos:[0,6,4], fill:0.15, fillPos:[0,3,-4], exposure:0.98 },
];

const fmt = (v, d = 2) => (Math.round(v * 10 ** d) / 10 ** d).toLocaleString("ru-RU", { maximumFractionDigits: d });
const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;" }[c]));

// ---------- scene ----------
const stage = document.getElementById("stage");
let renderer;
try {
  renderer = new THREE.WebGLRenderer({ antialias: true, preserveDrawingBuffer: true });
} catch (e) {
  document.getElementById("err").style.display = "grid";
  throw e;
}
renderer.toneMapping = THREE.ACESFilmicToneMapping;
renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
stage.appendChild(renderer.domElement);

const scene = new THREE.Scene();
const hemi = new THREE.HemisphereLight(0xffffff, 0xc7d2fe, 0.9);
const key = new THREE.DirectionalLight(0xffffff, 0.9);
const fill = new THREE.DirectionalLight(0xffffff, 0.25);
scene.add(hemi, key, fill);

let geometry = new THREE.BufferGeometry();
geometry.setAttribute("position", new THREE.BufferAttribute(window.DATUM_MESH.positions, 3));
geometry.setIndex(new THREE.BufferAttribute(window.DATUM_MESH.indices, 1));
geometry = geometry.toNonIndexed();          // flat normals: crisp CAD faces
geometry.rotateX(-Math.PI / 2);              // part is Z-up (OpenSCAD); the scene is Y-up
geometry.computeVertexNormals();
geometry.computeBoundingBox();

const material = new THREE.MeshStandardMaterial({ color: 0xdba642, roughness: 0.6, metalness: 0.05, side: THREE.DoubleSide });
const model = new THREE.Mesh(geometry, material);
scene.add(model);

const box = geometry.boundingBox;
const size = box.getSize(new THREE.Vector3());
const center = box.getCenter(new THREE.Vector3());
const radius = Math.max(size.length() / 2, 0.5);

// part frame: x = X, y = -Z, z = Y
const partSize = { x: size.x, y: size.z, z: size.y };

function niceStep(v) {
  const p = 10 ** Math.floor(Math.log10(v)), n = v / p;
  return (n < 1.5 ? 1 : n < 3.5 ? 2 : n < 7.5 ? 5 : 10) * p;
}
const cell = niceStep(Math.max(size.x, size.z, 1) / 8);
const gridSize = Math.ceil((Math.max(size.x, size.z) * 2.2) / cell) * cell;
const isDark = () => document.documentElement.dataset.theme ? document.documentElement.dataset.theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
let grid = null;
function makeGrid() {
  const visible = grid ? grid.visible : true;
  if (grid) { scene.remove(grid); grid.geometry.dispose(); grid.material.dispose(); }
  grid = new THREE.GridHelper(gridSize, Math.round(gridSize / cell), isDark() ? 0x5b6b7f : 0x94a3b8, isDark() ? 0x253041 : 0xd7dee5);
  grid.position.set(center.x, box.min.y - 0.001, center.z);
  grid.visible = visible;
  scene.add(grid);
}
makeGrid();

const axes = new THREE.Group();
{
  const L = radius * 0.6, o = new THREE.Vector3(box.min.x, box.min.y, box.max.z);
  const mk = (dir, color) => {
    const g = new THREE.BufferGeometry().setFromPoints([o, o.clone().addScaledVector(dir, L)]);
    return new THREE.Line(g, new THREE.LineBasicMaterial({ color, depthTest: false }));
  };
  axes.add(mk(new THREE.Vector3(1, 0, 0), 0xdc2626), mk(new THREE.Vector3(0, 0, -1), 0x16a34a), mk(new THREE.Vector3(0, 1, 0), 0x2563eb));
  axes.renderOrder = 5;
}
scene.add(axes);

const boxHelper = new THREE.Box3Helper(box, 0x147a68);
boxHelper.visible = false;
scene.add(boxHelper);

let edges = null;
function setSketch(on) {
  if (edges) { scene.remove(edges); edges.geometry.dispose(); edges = null; }
  material.transparent = on; material.opacity = on ? 0.06 : 1; material.depthWrite = !on; material.needsUpdate = true;
  if (!on) return;
  edges = new THREE.LineSegments(new THREE.EdgesGeometry(geometry, 28), new THREE.LineBasicMaterial({ color: isDark() ? 0xe5e7eb : 0x111827 }));
  scene.add(edges);
}

// ---------- camera ----------
let camera, view = "iso";
const controls = new OrbitControls(new THREE.PerspectiveCamera(), renderer.domElement);
controls.enableDamping = true;

function setView(name) {
  view = name;
  const w = stage.clientWidth || 800, h = stage.clientHeight || 600, aspect = w / h, d = radius * 3;
  const planar = { front: { off:[0,0,d], up:[0,1,0], fit:[size.x,size.y] }, right: { off:[d,0,0], up:[0,1,0], fit:[size.z,size.y] }, top: { off:[0,d,0], up:[0,0,-1], fit:[size.x,size.z] } }[name];
  if (planar) {
    const fh = Math.max(planar.fit[1], planar.fit[0] / aspect, radius * 0.25) * 1.25, fw = fh * aspect;
    camera = new THREE.OrthographicCamera(-fw / 2, fw / 2, fh / 2, -fh / 2, 0.01, radius * 50);
    camera.position.set(center.x + planar.off[0], center.y + planar.off[1], center.z + planar.off[2]);
    camera.up.set(...planar.up);
  } else {
    camera = new THREE.PerspectiveCamera(45, aspect, Math.max(radius / 200, 0.01), radius * 60);
    camera.position.set(center.x + d * 0.75, center.y + d * 0.55, center.z + d * 0.75);
    camera.up.set(0, 1, 0);
  }
  camera.lookAt(center);
  camera.updateProjectionMatrix();
  controls.object = camera;
  controls.target.copy(center);
  controls.enableRotate = !planar;
  controls.update();
  document.querySelectorAll("#views button").forEach((b) => b.classList.toggle("on", b.dataset.view === name));
}

function resize() {
  renderer.setSize(stage.clientWidth, stage.clientHeight, false);
  setView(view);
}
window.addEventListener("resize", resize);

// ---------- light & theme ----------
let lightIndex = 0;
function applyLight() {
  const p = LIGHTS[lightIndex];
  hemi.color.setHex(p.sky); hemi.groundColor.setHex(p.ground); hemi.intensity = p.hemi;
  key.intensity = p.key; key.position.set(...p.keyPos).multiplyScalar(radius);
  fill.intensity = p.fill; fill.position.set(...p.fillPos).multiplyScalar(radius);
  renderer.toneMappingExposure = p.exposure;
  document.getElementById("light").textContent = "Свет: " + p.label;
}
function applyTheme() {
  scene.background = new THREE.Color(getComputedStyle(document.documentElement).getPropertyValue("--bg").trim() || "#f4f6f8");
  makeGrid();
  if (edges) setSketch(true);
}
document.getElementById("light").onclick = () => { lightIndex = (lightIndex + 1) % LIGHTS.length; applyLight(); };
document.getElementById("theme").onclick = () => {
  document.documentElement.dataset.theme = isDark() ? "light" : "dark";
  applyTheme();
};
matchMedia("(prefers-color-scheme: dark)").addEventListener?.("change", applyTheme);

// ---------- toggles ----------
const state = { grid: true, axes: true, box: false, wire: false, sketch: false, measure: false };
function toggle(name) {
  state[name] = !state[name];
  if (name === "grid") grid.visible = state.grid;
  if (name === "axes") axes.visible = state.axes;
  if (name === "box") boxHelper.visible = state.box;
  if (name === "wire") { material.wireframe = state.wire; if (state.wire && state.sketch) toggle("sketch"); }
  if (name === "sketch") { setSketch(state.sketch); if (state.sketch && state.wire) toggle("wire"); }
  if (name === "measure" && !state.measure) clearMeasure();
  document.querySelector(`[data-t="${name}"]`).classList.toggle("on", state[name]);
  document.getElementById("hint").textContent = state.measure ? "Замер: щёлкните две точки на детали" : "";
}
document.querySelectorAll("#toggles button").forEach((b) => (b.onclick = () => toggle(b.dataset.t)));
document.querySelectorAll("#views button").forEach((b) => (b.onclick = () => setView(b.dataset.view)));

// ---------- measure ----------
const raycaster = new THREE.Raycaster();
const marks = new THREE.Group();
scene.add(marks);
let points = [];
function clearMeasure() {
  marks.children.forEach((c) => c.geometry.dispose());
  marks.clear(); points = [];
  document.getElementById("measure").style.display = "none";
}
let downAt = null;
renderer.domElement.addEventListener("pointerdown", (e) => (downAt = [e.clientX, e.clientY]));
renderer.domElement.addEventListener("pointerup", (e) => {
  if (!state.measure || !downAt || Math.hypot(e.clientX - downAt[0], e.clientY - downAt[1]) > 4) return;
  const r = renderer.domElement.getBoundingClientRect();
  raycaster.setFromCamera(new THREE.Vector2(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1), camera);
  const hit = raycaster.intersectObject(model)[0];
  if (!hit) return;
  if (points.length >= 2) clearMeasure();
  points.push(hit.point.clone());
  const dot = new THREE.Mesh(new THREE.SphereGeometry(radius * 0.015, 16, 12), new THREE.MeshBasicMaterial({ color: 0x147a68, depthTest: false }));
  dot.position.copy(hit.point); dot.renderOrder = 10; marks.add(dot);
  if (points.length === 2) {
    const [a, b] = points;
    const line = new THREE.Line(new THREE.BufferGeometry().setFromPoints([a, b]), new THREE.LineBasicMaterial({ color: 0x147a68, depthTest: false }));
    line.renderOrder = 9; marks.add(line);
    const d = b.clone().sub(a);
    const out = document.getElementById("measure");
    out.innerHTML = `<b>${fmt(a.distanceTo(b))} мм</b> &nbsp; Δx ${fmt(Math.abs(d.x))} · Δy ${fmt(Math.abs(d.z))} · Δz ${fmt(Math.abs(d.y))}`;
    out.style.display = "block";
  }
});

// ---------- misc ----------
document.getElementById("shot").onclick = () => {
  renderer.render(scene, camera);
  const a = document.createElement("a");
  a.href = renderer.domElement.toDataURL("image/png");
  a.download = `${META.part || "part"}-${META.version || "view"}.png`;
  a.click();
};
document.getElementById("full").onclick = () => (document.fullscreenElement ? document.exitFullscreen() : document.documentElement.requestFullscreen?.());
window.addEventListener("keydown", (e) => {
  if (e.target.closest?.("input, textarea")) return;
  const views = { Digit1: "iso", Digit2: "front", Digit3: "right", Digit4: "top" };
  if (views[e.code]) setView(views[e.code]);
  else if (e.code === "KeyG") toggle("grid");
  else if (e.code === "KeyM") toggle("measure");
  else if (e.code === "KeyF") setView(view);
  else if (e.code === "Escape") clearMeasure();
});

// ---------- info panel ----------
{
  const rep = META.report || {};
  const rows = [
    ["Габарит", `${fmt(partSize.x)} × ${fmt(partSize.y)} × ${fmt(partSize.z)} мм`],
    ["Треугольников", (rep.triangles ?? geometry.attributes.position.count / 3).toLocaleString("ru-RU")],
  ];
  if (rep.watertight !== undefined) rows.push(["Замкнута", rep.watertight ? "да" : "нет"]);
  if (rep.volume_mm3) rows.push(["Объём", `${fmt(rep.volume_mm3 / 1000, 2)} см³`]);
  if (rep.bodies > 1) rows.push(["Тел", rep.bodies]);
  const params = (META.params || []).filter((p) => p.literal);
  const paramRows = params.map((p) => `<tr><td>${esc(p.name)}</td><td>${esc(p.raw)}</td><td class="c">${esc(p.comment || "")}</td></tr>`).join("");
  document.getElementById("info").innerHTML =
    `<h1>${esc(META.title || META.part || "Деталь")}</h1>` +
    `<div class="sub">${esc([META.version, META.created && META.created.slice(0, 16).replace("T", " "), META.note].filter(Boolean).join(" · "))}</div>` +
    `<table>${rows.map(([k, v]) => `<tr><td>${k}</td><td>${v}</td></tr>`).join("")}</table>` +
    (paramRows ? `<details${params.length <= 12 ? " open" : ""}><summary>Параметры (${params.length})</summary><table class="p">${paramRows}</table></details>` : "") +
    `<div id="hint"></div>`;
}

applyLight();
applyTheme();
resize();
renderer.setAnimationLoop(() => { controls.update(); renderer.render(scene, camera); });
document.getElementById("loading").remove();
</script>
<script type="module">
// Unpack Three.js and the mesh, then run the app from blob: URLs (no import map, no network).
const say = (text) => { document.getElementById("loading").textContent = text; };
async function gunzip(b64) {
  const bytes = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
  const stream = new Blob([bytes]).stream().pipeThrough(new DecompressionStream("gzip"));
  return new Uint8Array(await new Response(stream).arrayBuffer());
}
const text = async (b64) => new TextDecoder().decode(await gunzip(b64));
const moduleUrl = (source) => URL.createObjectURL(new Blob([source], { type: "text/javascript" }));
function varints(bytes, n) {            // zigzag LEB128 -> signed numbers
  const out = new Float64Array(n);
  let i = 0;
  for (let k = 0; k < n; k++) {
    let x = 0, mul = 1, b;
    do { b = bytes[i++]; x += (b & 0x7f) * mul; mul *= 128; } while (b & 0x80);
    out[k] = x % 2 ? -(x + 1) / 2 : x / 2;
  }
  return out;
}
try {
  if (typeof DecompressionStream === "undefined") throw new Error("этот браузер не умеет DecompressionStream");
  const V = JSON.parse(document.getElementById("datum-vendor").textContent);
  const M = JSON.parse(document.getElementById("datum-data").textContent).mesh;
  const nv = M.vertices, nf = M.triangles;
  const dv = varints(await gunzip(M.v), nv * 3), di = varints(await gunzip(M.f), nf * 3);
  const positions = new Float32Array(nv * 3);
  for (let a = 0; a < 3; a++) {
    let acc = 0;
    const scale = M.span[a] / 65535;
    for (let i = 0; i < nv; i++) { acc += dv[a * nv + i]; positions[i * 3 + a] = M.lo[a] + acc * scale; }
  }
  const indices = new Uint32Array(nf * 3);
  for (let i = 0, acc = 0; i < nf * 3; i++) { acc += di[i]; indices[i] = acc; }
  window.DATUM_MESH = { positions, indices };
  const core = moduleUrl(await text(V.core));
  const three = moduleUrl((await text(V.three)).replaceAll('"three/core"', JSON.stringify(core)));
  const controls = moduleUrl((await text(V.controls)).replace(/from\s*['"]three['"]/g, "from " + JSON.stringify(three)));
  const app = document.getElementById("datum-app").textContent
    .replace('from "three/addons/OrbitControls.js"', "from " + JSON.stringify(controls))
    .replace('from "three"', "from " + JSON.stringify(three));
  await import(moduleUrl(app));
} catch (e) {
  say("Не удалось открыть 3D: " + e.message);
  console.error(e);
}
</script>
</body>
</html>
"""
