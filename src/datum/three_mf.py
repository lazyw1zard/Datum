"""3MF without extra packages: read the mesh (components, transforms, units), write a plain one, put a mesh
into a slicer project used as a template, and pull the embedded thumbnail. The thumbnail part comes from
CADRelay as it was."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile, ZIP_DEFLATED
import io
import re

import numpy as np

CORE_NS = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
PROD_NS = "http://schemas.microsoft.com/3dmanufacturing/production/2015/06"
MODEL_REL_TYPE = "http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"
UNIT_MM = {"micron": 0.001, "millimeter": 1.0, "centimeter": 10.0, "inch": 25.4, "foot": 304.8, "meter": 1000.0}

THUMBNAIL_REL_TYPE = "http://schemas.openxmlformats.org/package/2006/relationships/metadata/thumbnail"
SUPPORTED_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


@dataclass(slots=True)
class ExtractedThumbnail:
    # Единый формат результата извлечения миниатюры из 3MF.
    payload: bytes
    filename: str
    content_type: str


def _normalize_zip_name(name: str) -> str:
    normalized = name.replace("\\", "/").strip()
    normalized = normalized.lstrip("/").removeprefix("./")
    return normalized


def _safe_thumbnail_filename(name: str, fallback_ext: str) -> str:
    base = Path(name).name or f"thumbnail{fallback_ext}"
    safe = "".join(ch for ch in base if ch.isalnum() or ch in {"-", "_", "."}).strip(".")
    if not safe:
        return f"thumbnail{fallback_ext}"
    ext = Path(safe).suffix.lower()
    if ext not in SUPPORTED_IMAGE_EXTENSIONS:
        stem = Path(safe).stem or "thumbnail"
        return f"{stem}{fallback_ext}"
    return safe


def _detect_content_type(payload: bytes) -> tuple[str, str] | None:
    if payload.startswith(PNG_SIGNATURE):
        return ".png", "image/png"
    if payload.startswith(b"\xff\xd8"):
        return ".jpg", "image/jpeg"
    if len(payload) >= 12 and payload[:4] == b"RIFF" and payload[8:12] == b"WEBP":
        return ".webp", "image/webp"
    return None


def _read_thumbnail_relation_target(archive: ZipFile) -> str | None:
    try:
        rel_payload = archive.read("_rels/.rels")
    except KeyError:
        return None

    try:
        root = ElementTree.fromstring(rel_payload)
    except ElementTree.ParseError:
        return None

    for rel in root.findall(".//{*}Relationship"):
        if rel.attrib.get("Type", "").strip() != THUMBNAIL_REL_TYPE:
            continue
        target = rel.attrib.get("Target", "").strip()
        if not target:
            continue
        return _normalize_zip_name(target)
    return None


def _candidate_thumbnail_names(archive: ZipFile) -> list[str]:
    # Сначала берём путь из relationship, затем fallback по имени файлов в архиве.
    candidates: list[str] = []
    rel_target = _read_thumbnail_relation_target(archive)
    if rel_target:
        candidates.append(rel_target)

    for name in archive.namelist():
        normalized = _normalize_zip_name(name)
        if not normalized or normalized.endswith("/"):
            continue
        lowered = normalized.lower()
        ext = Path(lowered).suffix
        if ext not in SUPPORTED_IMAGE_EXTENSIONS:
            continue
        if lowered.startswith("metadata/") or "thumbnail" in lowered:
            candidates.append(normalized)

    # Убираем дубли, сохраняя порядок.
    return list(dict.fromkeys(candidates))


def extract_thumbnail_from_3mf(payload: bytes, *, max_bytes: int) -> ExtractedThumbnail | None:
    # Пытаемся достать встроенную миниатюру из 3MF (zip-пакета).
    try:
        with ZipFile(io.BytesIO(payload)) as archive:
            for name in _candidate_thumbnail_names(archive):
                normalized = _normalize_zip_name(name)
                try:
                    info = archive.getinfo(normalized)
                except KeyError:
                    continue
                if info.file_size <= 0 or info.file_size > max_bytes:
                    continue
                with archive.open(info) as fp:
                    image_payload = fp.read(max_bytes + 1)
                if not image_payload or len(image_payload) > max_bytes:
                    continue
                detected = _detect_content_type(image_payload)
                if detected is None:
                    continue
                ext, content_type = detected
                filename = _safe_thumbnail_filename(normalized, ext)
                return ExtractedThumbnail(
                    payload=image_payload,
                    filename=filename,
                    content_type=content_type,
                )
    except (BadZipFile, ValueError):
        return None

    return None


# ---------- mesh reading ----------

def _matrix(text: str | None) -> np.ndarray:
    """3MF transform "m00 m01 m02 m10 ... m32" (row vectors: p' = p @ M) -> 4x4."""
    m = np.eye(4)
    if text:
        values = [float(v) for v in text.split()]
        if len(values) == 12:
            m[:3, :3] = np.array(values[:9]).reshape(3, 3)
            m[3, :3] = values[9:]
    return m


def _root_model_name(archive: ZipFile) -> str:
    try:
        root = ElementTree.fromstring(archive.read("_rels/.rels"))
        for rel in root.findall(".//{*}Relationship"):
            if rel.attrib.get("Type", "").strip() == MODEL_REL_TYPE:
                return _normalize_zip_name(rel.attrib.get("Target", ""))
    except (KeyError, ElementTree.ParseError):
        pass
    return "3D/3dmodel.model"


def read_mesh(path: str | Path) -> tuple[np.ndarray, np.ndarray]:
    """All build items of a 3MF as one triangle soup in millimetres: (vertices Nx3, faces Mx3)."""
    with ZipFile(path) as archive:
        documents: dict[str, ElementTree.Element] = {}

        def document(name: str) -> ElementTree.Element:
            name = _normalize_zip_name(name)
            if name not in documents:
                documents[name] = ElementTree.fromstring(archive.read(name))
            return documents[name]

        def find_object(doc_name: str, object_id: str) -> ElementTree.Element:
            for obj in document(doc_name).iter(f"{{{CORE_NS}}}object"):
                if obj.attrib.get("id") == object_id:
                    return obj
            raise ValueError(f"3MF object {object_id} not found in {doc_name}")

        verts: list[np.ndarray] = []
        faces: list[np.ndarray] = []
        count = 0

        def emit(doc_name: str, object_id: str, transform: np.ndarray, depth: int = 0) -> None:
            nonlocal count
            if depth > 16:
                raise ValueError("3MF components nest too deep")
            obj = find_object(doc_name, object_id)
            mesh = obj.find(f"{{{CORE_NS}}}mesh")
            if mesh is not None:
                v = np.array([[float(e.attrib["x"]), float(e.attrib["y"]), float(e.attrib["z"])]
                              for e in mesh.iter(f"{{{CORE_NS}}}vertex")], dtype=float)
                t = np.array([[int(e.attrib["v1"]), int(e.attrib["v2"]), int(e.attrib["v3"])]
                              for e in mesh.iter(f"{{{CORE_NS}}}triangle")], dtype=np.int64)
                if len(v) and len(t):
                    homogeneous = np.hstack([v, np.ones((len(v), 1))]) @ transform
                    verts.append(homogeneous[:, :3])
                    faces.append(t + count)
                    count += len(v)
            for comp in obj.iter(f"{{{CORE_NS}}}component"):
                target_doc = comp.attrib.get(f"{{{PROD_NS}}}path") or doc_name
                emit(target_doc, comp.attrib["objectid"], _matrix(comp.attrib.get("transform")) @ transform, depth + 1)

        root_name = _root_model_name(archive)
        root = document(root_name)
        scale = UNIT_MM.get(root.attrib.get("unit", "millimeter"), 1.0)
        for item in root.iter(f"{{{CORE_NS}}}item"):
            item_doc = item.attrib.get(f"{{{PROD_NS}}}path") or root_name
            emit(item_doc, item.attrib["objectid"], _matrix(item.attrib.get("transform")))
    if not verts:
        raise ValueError("3MF has no mesh in its build")
    return np.vstack(verts) * scale, np.vstack(faces)


# ---------- mesh writing ----------

_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>'
    '<Default Extension="png" ContentType="image/png"/>'
    "</Types>"
)


def _mesh_xml(vertices: np.ndarray, faces: np.ndarray, prefix: str = "") -> str:
    vtext = "".join(f'<{prefix}vertex x="{x:.7g}" y="{y:.7g}" z="{z:.7g}"/>' for x, y, z in vertices)
    ttext = "".join(f'<{prefix}triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in faces)
    return (f"<{prefix}mesh><{prefix}vertices>{vtext}</{prefix}vertices>"
            f"<{prefix}triangles>{ttext}</{prefix}triangles></{prefix}mesh>")


def write_mesh(path: str | Path, vertices: np.ndarray, faces: np.ndarray, *, name: str = "part",
               thumbnail_png: bytes | None = None) -> None:
    """A plain 3MF with one object, millimetres. Slicers add their own settings when they open it."""
    model = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<model unit="millimeter" xml:lang="en-US" xmlns="{CORE_NS}">'
        f'<metadata name="Title">{_xml_escape(name)}</metadata>'
        f'<resources><object id="1" type="model" name="{_xml_escape(name)}">{_mesh_xml(vertices, faces)}'
        '</object></resources><build><item objectid="1"/></build></model>'
    )
    rels = ['<Relationship Target="/3D/3dmodel.model" Id="rel0" Type="' + MODEL_REL_TYPE + '"/>']
    if thumbnail_png:
        rels.append(f'<Relationship Target="/Metadata/thumbnail.png" Id="rel1" Type="{THUMBNAIL_REL_TYPE}"/>')
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _CONTENT_TYPES)
        archive.writestr("_rels/.rels", '<?xml version="1.0" encoding="UTF-8"?>\n'
                         '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                         + "".join(rels) + "</Relationships>")
        archive.writestr("3D/3dmodel.model", model)
        if thumbnail_png:
            archive.writestr("Metadata/thumbnail.png", thumbnail_png)


def _xml_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


# ---------- a slicer project as a template ----------

# Per-object data slicers keep beside the model that depend on the mesh. The source offset comes from PrusaSlicer
# and lives on in the Bambu Studio family (Orca, QIDI Studio, ...); the face count is Bambu's; the triangle range
# of each volume is PrusaSlicer's. Other slicers keep nothing of the kind there and need only the mesh.
_OBJECT_CONFIGS = ("Metadata/Slic3r_PE_model.config", "Metadata/model_settings.config")
_PRINT_SETTINGS = ("Metadata/Slic3r_PE.config", "Metadata/project_settings.config", "Cura/")


def replace_mesh(template: str | Path, out: str | Path, vertices: np.ndarray, faces: np.ndarray) -> dict:
    """Put a mesh (millimetres) into a slicer project saved as 3MF, in place of its only object.

    Everything else stays as the human left it: print settings, the plate, the object's placement and scale.
    The new mesh sits in the object the way the old one did (centre in plan, bottom at the same height), so it
    lands on the same spot of the bed. The model file is edited as text, so whatever the slicer wrote around
    the mesh survives byte for byte; the per-object data in _OBJECT_CONFIGS are brought in line with the mesh."""
    template, out = Path(template), Path(out)
    with ZipFile(template) as archive:
        infos = archive.infolist()
        entries = {i.filename: archive.read(i.filename) for i in infos}
        root_name = _root_model_name(archive)
    names = {_normalize_zip_name(n): n for n in entries}

    def document(name: str) -> ElementTree.Element:
        return ElementTree.fromstring(entries[names[_normalize_zip_name(name)]])

    items = list(document(root_name).iter(f"{{{CORE_NS}}}item"))
    if len(items) != 1:
        raise ValueError(f"a template must hold one object on the plate, this one has {len(items)}")
    doc_name = items[0].attrib.get(f"{{{PROD_NS}}}path") or root_name
    object_id = items[0].attrib["objectid"]
    transform = _matrix(items[0].attrib.get("transform"))
    for _ in range(16):                                 # down the components to the one mesh
        obj = next((o for o in document(doc_name).iter(f"{{{CORE_NS}}}object") if o.attrib.get("id") == object_id),
                   None)
        if obj is None:
            raise ValueError(f"3MF object {object_id} not found in {doc_name}")
        mesh = obj.find(f"{{{CORE_NS}}}mesh")
        components = obj.findall(f"{{{CORE_NS}}}components/{{{CORE_NS}}}component")
        if mesh is not None and not components:
            break
        if mesh is not None or len(components) != 1:
            raise ValueError("the template object has several parts (modifiers or a multi-part object); "
                             "one mesh is expected")
        transform = _matrix(components[0].attrib.get("transform")) @ transform
        doc_name = components[0].attrib.get(f"{{{PROD_NS}}}path") or doc_name
        object_id = components[0].attrib["objectid"]
    else:
        raise ValueError("3MF components nest too deep")

    unit = UNIT_MM.get(document(doc_name).attrib.get("unit", "millimeter"), 1.0)
    old = np.array([[float(e.attrib[k]) for k in "xyz"] for e in mesh.iter(f"{{{CORE_NS}}}vertex")])
    new = np.asarray(vertices, float) / unit
    if len(old):
        centre_old, centre_new = (old.min(axis=0) + old.max(axis=0)) / 2, (new.min(axis=0) + new.max(axis=0)) / 2
        shift = np.r_[centre_old[:2] - centre_new[:2], old[:, 2].min() - new[:, 2].min()]
    else:
        shift = np.zeros(3)
    updated = {}
    for config_name in _OBJECT_CONFIGS:
        if config_name not in names:
            continue
        text = entries[names[config_name]].decode("utf-8")
        if len(re.findall(r"<volume\b", text)) > 1 or len(re.findall(r"<part\b", text)) > 1:
            raise ValueError("the template object has several parts (modifiers or a multi-part object); "
                             "one mesh is expected")
        text = re.sub(r'(<volume\b[^>]*?\blastid=")\d+', lambda m: f"{m.group(1)}{len(faces) - 1}", text)
        text = re.sub(r'face_count="\d+"', f'face_count="{len(faces)}"', text)
        for axis, value in zip("xyz", -shift):
            value = round(float(value), 6) + 0.0            # no "-0" and no float dust
            text = re.sub(rf'(key="source_offset_{axis}" value=")[^"]*(")',
                          lambda m, v=value: f"{m.group(1)}{v:.9g}{m.group(2)}", text)
        updated[names[config_name]] = text.encode("utf-8")
    entries.update(updated)
    key = names[_normalize_zip_name(doc_name)]
    entries[key] = _swap_mesh(entries[key].decode("utf-8"), object_id, new + shift, faces).encode("utf-8")

    tmp = out.with_name(out.name + ".tmp")              # out may be the template itself
    with ZipFile(tmp, "w") as archive:
        for info in infos:
            archive.writestr(info, entries[info.filename])
    tmp.replace(out)
    return {"model": _normalize_zip_name(doc_name), "triangles": int(len(faces)),
            "scale": [round(float(s), 4) for s in np.linalg.norm(transform[:3, :3], axis=1)],
            "print_settings": any(n.startswith(p) for n in names for p in _PRINT_SETTINGS)}


def _swap_mesh(text: str, object_id: str, vertices: np.ndarray, faces: np.ndarray) -> str:
    head = re.search(rf'<(\w+:)?object\b[^>]*?\bid="{re.escape(object_id)}"', text)
    if head is None:
        raise ValueError(f"3MF object {object_id} not found")
    prefix = head.group(1) or ""
    start = text.index(f"<{prefix}mesh", head.end())
    end = text.index(f"</{prefix}mesh>", start) + len(f"</{prefix}mesh>")
    return text[:start] + _mesh_xml(vertices, faces, prefix) + text[end:]
