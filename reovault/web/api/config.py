"""The whole of reovault.toml, as the Settings page sees it: every section and
field with its current value, where that value comes from (file, env var or
default), and whether the dashboard may change it. Writes go into the file
itself (see `reovault.config_store`).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from tomlkit.toml_document import TOMLDocument

from reovault.config import AlertRules, AlertsConfig, Settings, WebConfig
from reovault.config_store import (
    WEB_EDITABLE,
    ConfigConflictError,
    ConfigInvalidError,
    ConfigUnavailableError,
    env_locked,
    is_infra,
    set_fields,
    table_at,
)
from reovault.web.deps import AppState, State

router = APIRouter(prefix="/config", tags=["config"])

Kind = Literal["bool", "int", "float", "str", "path", "list"]

# Sections shown on the Settings page, in order. `devices`, `schedule`,
# `retention` and `alerts.rules` have their own forms and endpoints; they're
# listed here too so the page can show sources and locks for them.
SECTIONS: list[tuple[str, ...]] = [
    ("schedule",),
    ("retention",),
    ("alerts",),
    ("alerts", "rules"),
    ("web",),
    ("storage",),
    ("reolink_cli",),
]
# Never shown or written: a credential. (Alert secrets are file *paths*.)
HIDDEN = {("web", "password_hash")}


class FieldOut(BaseModel):
    key: str
    kind: Kind
    value: Any
    default: Any
    source: Literal["file", "env", "default"]
    env_var: str | None
    editable: bool
    restart: bool  # takes effect only after a restart


class SectionOut(BaseModel):
    path: list[str]
    fields: list[FieldOut]


class ConfigOut(BaseModel):
    path: str | None
    version: str
    writable: bool
    error: str | None
    restart_required: list[str]
    sections: list[SectionOut]


class ConfigUpdateIn(BaseModel):
    version: str
    values: dict[str, Any]


def write(
    st: AppState, mutate: Callable[[TOMLDocument], object], version: str | None = None
) -> None:
    """One place that turns config-file failures into HTTP answers."""
    try:
        st.config.update(mutate, expected_version=version)
    except ConfigUnavailableError as exc:
        raise HTTPException(409, str(exc)) from None
    except ConfigConflictError as exc:
        raise HTTPException(409, str(exc)) from None
    except ConfigInvalidError as exc:
        raise HTTPException(422, str(exc)) from None


def _kind(annotation: Any, value: Any) -> Kind:
    text = str(annotation)
    if "Path" in text:
        return "path"
    if isinstance(value, bool) or "bool" in text:
        return "bool"
    if "list" in text:
        return "list"
    if "float" in text:
        return "float"
    if "int" in text:
        return "int"
    return "str"


def _model_at(settings: Settings, path: tuple[str, ...]) -> BaseModel:
    obj: Any = settings
    for key in path:
        obj = getattr(obj, key)
    return obj  # type: ignore[no-any-return]


def _in_file(doc: TOMLDocument, path: tuple[str, ...], key: str) -> bool:
    node: Any = doc
    for part in path:
        if part not in node:
            return False
        node = node[part]
    return key in node


def _jsonable(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, BaseModel):
        return None  # nested tables are their own section
    return value


def config_out(st: AppState) -> ConfigOut:
    store = st.config
    doc = store.document()
    file_settings = store.current
    sections = []
    for path in SECTIONS:
        model = _model_at(file_settings, path)
        defaults = type(model)()
        fields = []
        for key, info in type(model).model_fields.items():
            if (*path, key) in HIDDEN or isinstance(getattr(model, key), BaseModel):
                continue
            env_var = env_locked(*path, key)
            infra = is_infra(path[0], key)
            source: Literal["file", "env", "default"] = (
                "env" if env_var else "file" if _in_file(doc, path, key) else "default"
            )
            fields.append(
                FieldOut(
                    key=key,
                    kind=_kind(info.annotation, getattr(model, key)),
                    value=_jsonable(getattr(model, key)),
                    default=_jsonable(getattr(defaults, key)),
                    source=source,
                    env_var=env_var,
                    editable=store.writable and not env_var and not infra,
                    restart=infra,
                )
            )
        sections.append(SectionOut(path=list(path), fields=fields))
    return ConfigOut(
        path=str(store.path) if store.path else None,
        version=store.version,
        writable=store.writable,
        error=store.error,
        restart_required=store.restart_required,
        sections=sections,
    )


@router.get("")
def get_config(st: State) -> ConfigOut:
    return config_out(st)


_MODELS: dict[str, tuple[tuple[str, ...], type[BaseModel]]] = {
    "alerts": (("alerts",), AlertsConfig),
    "alerts.rules": (("alerts", "rules"), AlertRules),
    "web": (("web",), WebConfig),
}


@router.put("/{section}", responses={409: {}, 422: {}})
def put_config(section: str, body: ConfigUpdateIn, st: State) -> ConfigOut:
    """Generic edit for the sections without a dedicated form. Only fields
    the view marks editable are accepted: never an env-set or infrastructure
    one, and never a nested table."""
    if section not in _MODELS:
        raise HTTPException(404, "Unknown or not generically editable section.")
    path, model = _MODELS[section]
    current = _model_at(st.settings, path)
    for key in body.values:
        info = model.model_fields.get(key)
        if info is None or isinstance(getattr(current, key), BaseModel):
            raise HTTPException(422, f"{'.'.join(path)}.{key} is not a setting.")
        if env_locked(*path, key):
            raise HTTPException(409, f"{key} is set by an environment variable.")
        if is_infra(path[0], key) or (path == ("web",) and key not in WEB_EDITABLE):
            raise HTTPException(409, f"{key} can only be changed in reovault.toml, then restart.")
    write(
        st,
        lambda doc: set_fields(table_at(doc, *path), body.values, model()),
        version=body.version,
    )
    return config_out(st)
