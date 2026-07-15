"""FastAPI router for managing skill assets under .doyoutrade/skills/."""

from __future__ import annotations

import base64
import logging
import re
import shutil
import yaml
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from fastapi import APIRouter, Body, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from doyoutrade.api._skill_paths import SkillPathError, detect_mime, resolve_skill_root, resolve_inside
from doyoutrade.skills import load_skills
from doyoutrade.skills.parser import parse_skill_file
from doyoutrade.observability import get_tracer

_tracer = get_tracer(__name__)

logger = logging.getLogger(__name__)

MAX_FILE_BYTES = 1 << 20  # 1 MiB

SkillsRootResolver = Callable[[], Path]


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _file_tree(skill_root: Path) -> list[dict]:
    def walk(p: Path) -> list[dict]:
        out: list[dict] = []
        for child in sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name)):
            if child.name.startswith("."):
                continue
            rel = child.relative_to(skill_root).as_posix()
            stat = child.stat()
            node = {
                "name": child.name,
                "path": rel,
                "kind": "dir" if child.is_dir() else "file",
                "size": stat.st_size,
                "mtime": _iso(stat.st_mtime),
                "mime": None if child.is_dir() else detect_mime(child),
            }
            if child.is_dir():
                node["children"] = walk(child)
            out.append(node)
        return out
    return walk(skill_root)


def _read_frontmatter(skill_md: Path) -> dict:
    skill = parse_skill_file(skill_md, relative_path=skill_md.parent.relative_to(skill_md.parent.parent))
    if skill is None:
        raise HTTPException(status_code=500, detail="invalid SKILL.md frontmatter")
    return {"name": skill.name, "description": skill.description, "license": skill.license}


# ---------------------------------------------------------------------------
# Request body schemas for write endpoints
# ---------------------------------------------------------------------------

class FileWriteBody(BaseModel):
    content: str
    encoding: str = "utf-8"  # "utf-8" or "base64"
    if_unmodified_since: str | None = None


class FileCreateBody(BaseModel):
    path: str
    kind: str  # "file" | "dir"
    content: str | None = None
    encoding: str = "utf-8"


class FileRenameBody(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    from_: str = Field(alias="from")
    to: str


def _refuse_skill_md(rel: str) -> None:
    """Raise 400 if the path is the top-level SKILL.md (may not be deleted or renamed)."""
    if Path(rel).name == "SKILL.md" and Path(rel).parent == Path("."):
        raise HTTPException(status_code=400, detail="cannot delete or rename SKILL.md")


# ---------------------------------------------------------------------------
# Skill-level CRUD models and helpers
# ---------------------------------------------------------------------------

_FOLDER_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_\-]*$")


class SkillCreateBody(BaseModel):
    folder_name: str
    name: str
    description: str
    license: str | None = None


class SkillRenameBody(BaseModel):
    new_folder_name: str


class FrontmatterBody(BaseModel):
    name: str | None = None
    description: str | None = None
    license: str | None = None


def _read_state(root: Path) -> dict:
    f = root / "skills_state.yaml"
    if not f.is_file():
        return {}
    return yaml.safe_load(f.read_text(encoding="utf-8")) or {}


def _write_state(root: Path, state: dict) -> None:
    f = root / "skills_state.yaml"
    tmp = f.with_suffix(".yaml.tmp")
    tmp.write_text(yaml.safe_dump(state, sort_keys=False, allow_unicode=True), encoding="utf-8")
    tmp.replace(f)


def _rename_in_disabled(root: Path, old_name: str, new_name: str) -> None:
    state = _read_state(root)
    disabled = state.get("disabled")
    if isinstance(disabled, list) and old_name in disabled:
        state["disabled"] = [new_name if x == old_name else x for x in disabled]
        _write_state(root, state)


def _split_frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        raise HTTPException(status_code=400, detail="SKILL.md missing frontmatter")
    parts = text.split("---", 2)
    if len(parts) < 3:
        raise HTTPException(status_code=400, detail="SKILL.md frontmatter malformed")
    meta = yaml.safe_load(parts[1]) or {}
    body = parts[2].lstrip("\n")
    return meta, body


def _join_frontmatter(meta: dict, body: str) -> str:
    return "---\n" + yaml.safe_dump(meta, sort_keys=False, allow_unicode=True) + "---\n\n" + body


def _read_disabled(root: Path) -> set[str]:
    state = _read_state(root)
    raw = state.get("disabled", [])
    return {x for x in raw if isinstance(x, str)} if isinstance(raw, list) else set()


def _write_disabled(root: Path, names: set[str]) -> None:
    state = _read_state(root)
    state["disabled"] = sorted(names)
    _write_state(root, state)


def _frontmatter_name(skill_root: Path) -> str:
    text = (skill_root / "SKILL.md").read_text(encoding="utf-8")
    meta, _ = _split_frontmatter(text)
    name = meta.get("name")
    if not isinstance(name, str):
        raise HTTPException(status_code=500, detail="SKILL.md frontmatter has no name")
    return name


def _invalidate_slash_cache() -> None:
    from doyoutrade.assistant import slash_commands as sc
    if hasattr(sc, "invalidate_skill_commands_cache"):
        sc.invalidate_skill_commands_cache()
    else:
        sc._skill_commands_cache = None  # fallback


def build_skills_router(skills_root_resolver: SkillsRootResolver) -> APIRouter:
    router = APIRouter()

    @router.get("/skills")
    async def list_skills_route():
        root = skills_root_resolver()
        skills = load_skills(root)
        return [
            {
                "folder_name": s.skill_path or s.skill_dir.name,
                "frontmatter": {
                    "name": s.name,
                    "description": s.description,
                    "license": s.license,
                },
                "enabled": s.enabled,
                "relative_path": s.relative_path.as_posix(),
                "locked": False,
            }
            for s in skills
        ]

    @router.get("/skills/{skill_id}")
    async def get_skill_detail(skill_id: str):
        root = skills_root_resolver()
        try:
            skill_root = resolve_skill_root(root, skill_id)
        except SkillPathError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not skill_root.is_dir():
            raise HTTPException(status_code=404, detail=f"skill not found: {skill_id}")
        skill_md = skill_root / "SKILL.md"
        if not skill_md.is_file():
            raise HTTPException(status_code=404, detail=f"SKILL.md missing in {skill_id}")
        return {
            "folder_name": skill_id,
            "frontmatter": _read_frontmatter(skill_md),
            "tree": _file_tree(skill_root),
        }

    @router.get("/skills/{skill_id}/files")
    async def get_skill_file(skill_id: str, path: str = Query(...)):
        root = skills_root_resolver()
        try:
            skill_root = resolve_skill_root(root, skill_id)
            target = resolve_inside(skill_root, path)
        except SkillPathError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not target.is_file():
            raise HTTPException(status_code=404, detail=f"file not found: {path}")
        stat = target.stat()
        if stat.st_size > MAX_FILE_BYTES:
            raise HTTPException(status_code=413, detail="file too large; edit locally")
        mime = detect_mime(target)
        if mime.startswith("text/") or mime in ("application/json", "application/yaml"):
            content = target.read_text(encoding="utf-8")
            encoding = "utf-8"
        else:
            content = base64.b64encode(target.read_bytes()).decode("ascii")
            encoding = "base64"
        return {
            "path": path,
            "content": content,
            "encoding": encoding,
            "size": stat.st_size,
            "mtime": _iso(stat.st_mtime),
            "mime": mime,
        }

    @router.put("/skills/{skill_id}/files")
    async def put_skill_file(skill_id: str, path: str = Query(...), body: FileWriteBody = Body(...)):
        with _tracer.start_as_current_span("skill.update") as span:
            span.set_attribute("skill.id", skill_id)
            span.set_attribute("skill.path", path)
            span.set_attribute("skill.op", "put_file")
            root = skills_root_resolver()
            try:
                skill_root = resolve_skill_root(root, skill_id)
                target = resolve_inside(skill_root, path)
            except SkillPathError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            if target.exists() and target.is_dir():
                raise HTTPException(status_code=409, detail="target is a directory")
            if body.if_unmodified_since and target.exists():
                current_mtime = _iso(target.stat().st_mtime)
                if current_mtime != body.if_unmodified_since:
                    raise HTTPException(
                        status_code=409,
                        detail={"reason": "mtime_conflict", "mtime": current_mtime},
                    )
            target.parent.mkdir(parents=True, exist_ok=True)
            if body.encoding == "base64":
                target.write_bytes(base64.b64decode(body.content))
            else:
                target.write_text(body.content, encoding="utf-8")
            logger.info(
                "skill.write",
                extra={"op": "put_file", "skill_id": skill_id, "path": path, "actor": "api"},
            )
            return {
                "path": path,
                "size": target.stat().st_size,
                "mtime": _iso(target.stat().st_mtime),
            }

    @router.post("/skills/{skill_id}/files", status_code=201)
    async def post_skill_file(skill_id: str, body: FileCreateBody = Body(...)):
        with _tracer.start_as_current_span("skill.create") as span:
            span.set_attribute("skill.id", skill_id)
            span.set_attribute("skill.path", body.path)
            span.set_attribute("skill.op", "create")
            root = skills_root_resolver()
            try:
                skill_root = resolve_skill_root(root, skill_id)
                target = resolve_inside(skill_root, body.path)
            except SkillPathError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            if target.exists():
                raise HTTPException(status_code=409, detail="path already exists")
            if body.kind == "dir":
                target.mkdir(parents=True, exist_ok=False)
            elif body.kind == "file":
                target.parent.mkdir(parents=True, exist_ok=True)
                if body.encoding == "base64" and body.content is not None:
                    target.write_bytes(base64.b64decode(body.content))
                else:
                    target.write_text(body.content or "", encoding="utf-8")
            else:
                raise HTTPException(status_code=400, detail=f"unknown kind: {body.kind}")
            logger.info(
                "skill.write",
                extra={"op": "create", "skill_id": skill_id, "path": body.path, "kind": body.kind, "actor": "api"},
            )
            return {"path": body.path, "kind": body.kind}

    @router.post("/skills/{skill_id}/files/rename")
    async def rename_skill_file(skill_id: str, body: FileRenameBody = Body(...)):
        with _tracer.start_as_current_span("skill.rename") as span:
            span.set_attribute("skill.id", skill_id)
            span.set_attribute("skill.path", body.from_)
            span.set_attribute("skill.op", "rename_file")
            root = skills_root_resolver()
            try:
                skill_root = resolve_skill_root(root, skill_id)
                src = resolve_inside(skill_root, body.from_)
                dst = resolve_inside(skill_root, body.to)
            except SkillPathError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            _refuse_skill_md(body.from_)
            _refuse_skill_md(body.to)
            if not src.exists():
                raise HTTPException(status_code=404, detail=f"source not found: {body.from_}")
            if dst.exists():
                raise HTTPException(status_code=409, detail="destination already exists")
            dst.parent.mkdir(parents=True, exist_ok=True)
            src.rename(dst)
            logger.info(
                "skill.write",
                extra={"op": "rename_file", "skill_id": skill_id, "from": body.from_, "to": body.to, "actor": "api"},
            )
            return {"from": body.from_, "to": body.to}

    @router.delete("/skills/{skill_id}/files", status_code=204)
    async def delete_skill_file(skill_id: str, path: str = Query(...)):
        with _tracer.start_as_current_span("skill.delete") as span:
            span.set_attribute("skill.id", skill_id)
            span.set_attribute("skill.path", path)
            span.set_attribute("skill.op", "delete_file")
            root = skills_root_resolver()
            try:
                skill_root = resolve_skill_root(root, skill_id)
                target = resolve_inside(skill_root, path)
            except SkillPathError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            _refuse_skill_md(path)
            if not target.exists():
                raise HTTPException(status_code=404, detail=f"not found: {path}")
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
            logger.info(
                "skill.write",
                extra={"op": "delete_file", "skill_id": skill_id, "path": path, "actor": "api"},
            )
            return None

    # -----------------------------------------------------------------------
    # Skill-level CRUD endpoints
    # -----------------------------------------------------------------------

    @router.post("/skills", status_code=201)
    async def create_skill(body: SkillCreateBody):
        with _tracer.start_as_current_span("skill.create") as span:
            span.set_attribute("skill.id", body.folder_name)
            span.set_attribute("skill.op", "create_skill")
            root = skills_root_resolver()
            if not _FOLDER_NAME_RE.match(body.folder_name):
                raise HTTPException(status_code=400, detail="invalid folder_name")
            skill_root = root / body.folder_name
            if skill_root.exists():
                raise HTTPException(status_code=409, detail="skill already exists")
            skill_root.mkdir(parents=True)
            meta: dict = {"name": body.name, "description": body.description}
            if body.license:
                meta["license"] = body.license
            skeleton = _join_frontmatter(meta, f"# {body.name}\n\n<!-- TODO: 描述这个 skill 的用法 -->\n")
            (skill_root / "SKILL.md").write_text(skeleton, encoding="utf-8")
            _invalidate_slash_cache()
            logger.info("skill.write", extra={"op": "create_skill", "skill_id": body.folder_name, "actor": "api"})
            return {"skill_id": body.folder_name}

    @router.post("/skills/{skill_id}/rename")
    async def rename_skill_folder(skill_id: str, body: SkillRenameBody):
        with _tracer.start_as_current_span("skill.rename") as span:
            span.set_attribute("skill.id", skill_id)
            span.set_attribute("skill.op", "rename_skill")
            root = skills_root_resolver()
            try:
                old_root = resolve_skill_root(root, skill_id)
            except SkillPathError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            if not old_root.is_dir():
                raise HTTPException(status_code=404, detail="skill not found")
            if not _FOLDER_NAME_RE.match(body.new_folder_name):
                raise HTTPException(status_code=400, detail="invalid new_folder_name")
            new_root = root / body.new_folder_name
            if new_root.exists():
                raise HTTPException(status_code=409, detail="destination already exists")
            old_root.rename(new_root)
            _invalidate_slash_cache()
            logger.info("skill.write", extra={"op": "rename_skill", "from": skill_id, "to": body.new_folder_name, "actor": "api"})
            return {"skill_id": body.new_folder_name}

    @router.delete("/skills/{skill_id}", status_code=204)
    async def delete_skill(skill_id: str):
        with _tracer.start_as_current_span("skill.delete") as span:
            span.set_attribute("skill.id", skill_id)
            span.set_attribute("skill.op", "delete_skill")
            root = skills_root_resolver()
            try:
                skill_root = resolve_skill_root(root, skill_id)
            except SkillPathError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            if not skill_root.is_dir():
                raise HTTPException(status_code=404, detail="skill not found")
            shutil.rmtree(skill_root)
            _invalidate_slash_cache()
            logger.info("skill.write", extra={"op": "delete_skill", "skill_id": skill_id, "actor": "api"})
            return None

    @router.put("/skills/{skill_id}/frontmatter")
    async def update_frontmatter(skill_id: str, body: FrontmatterBody):
        with _tracer.start_as_current_span("skill.update") as span:
            span.set_attribute("skill.id", skill_id)
            span.set_attribute("skill.op", "update_frontmatter")
            root = skills_root_resolver()
            try:
                skill_root = resolve_skill_root(root, skill_id)
            except SkillPathError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            skill_md = skill_root / "SKILL.md"
            if not skill_md.is_file():
                raise HTTPException(status_code=404, detail="SKILL.md missing")
            text = skill_md.read_text(encoding="utf-8")
            meta, md_body = _split_frontmatter(text)
            old_name = meta.get("name")
            if body.name is not None:
                meta["name"] = body.name
            if body.description is not None:
                meta["description"] = body.description
            if body.license is not None:
                meta["license"] = body.license
            skill_md.write_text(_join_frontmatter(meta, md_body), encoding="utf-8")
            if body.name is not None and isinstance(old_name, str) and old_name != body.name:
                _rename_in_disabled(root, old_name, body.name)
            _invalidate_slash_cache()
            logger.info("skill.write", extra={"op": "update_frontmatter", "skill_id": skill_id, "actor": "api"})
            return {"frontmatter": meta}

    @router.post("/skills/{skill_id}/disable")
    async def disable_skill(skill_id: str):
        with _tracer.start_as_current_span("skill.update") as span:
            span.set_attribute("skill.id", skill_id)
            span.set_attribute("skill.op", "disable")
            root = skills_root_resolver()
            try:
                skill_root = resolve_skill_root(root, skill_id)
            except SkillPathError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            if not skill_root.is_dir():
                raise HTTPException(status_code=404, detail="skill not found")
            name = _frontmatter_name(skill_root)
            disabled = _read_disabled(root)
            disabled.add(name)
            _write_disabled(root, disabled)
            _invalidate_slash_cache()
            logger.info("skill.write", extra={"op": "disable", "skill_id": skill_id, "actor": "api"})
            return {"name": name, "enabled": False}

    @router.post("/skills/{skill_id}/enable")
    async def enable_skill(skill_id: str):
        with _tracer.start_as_current_span("skill.update") as span:
            span.set_attribute("skill.id", skill_id)
            span.set_attribute("skill.op", "enable")
            root = skills_root_resolver()
            try:
                skill_root = resolve_skill_root(root, skill_id)
            except SkillPathError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            if not skill_root.is_dir():
                raise HTTPException(status_code=404, detail="skill not found")
            name = _frontmatter_name(skill_root)
            disabled = _read_disabled(root)
            disabled.discard(name)
            _write_disabled(root, disabled)
            _invalidate_slash_cache()
            logger.info("skill.write", extra={"op": "enable", "skill_id": skill_id, "actor": "api"})
            return {"name": name, "enabled": True}

    return router
