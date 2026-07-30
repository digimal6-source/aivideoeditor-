"""Preset store.

Presets are plain JSON on disk (no database). The built-in "My Default" preset
is always present and cannot be deleted or overwritten; user presets are merged
on top of it.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from .errors import NotFoundError, ValidationError
from .models import Preset, default_preset
from .settings import Settings

MAX_PRESETS = 50


class PresetStore:
    def __init__(self, settings: Settings) -> None:
        self.path: Path = settings.presets_file
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    # -- persistence -----------------------------------------------------

    def _read_raw(self) -> list[dict]:
        if not self.path.is_file():
            return []
        try:
            data = json.loads(self.path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        return data if isinstance(data, list) else []

    def _write_raw(self, payload: list[dict]) -> None:
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2), "utf-8")
        os.replace(tmp, self.path)

    # -- api -------------------------------------------------------------

    def list(self) -> list[Preset]:
        presets = [default_preset()]
        for item in self._read_raw():
            try:
                preset = Preset.from_dict(item)
            except ValidationError:
                continue
            if preset.id == "my-default":
                continue
            presets.append(preset)
        return presets

    def get(self, preset_id: str) -> Preset:
        for preset in self.list():
            if preset.id == preset_id:
                return preset
        raise NotFoundError(f"Preset '{preset_id}' was not found.")

    def save(self, preset: Preset) -> Preset:
        if preset.id == "my-default":
            raise ValidationError("The built-in 'My Default' preset cannot be overwritten. Save it under a new name.")
        with self._lock:
            raw = [item for item in self._read_raw() if item.get("id") != preset.id]
            if len(raw) >= MAX_PRESETS:
                raise ValidationError(f"You already have {MAX_PRESETS} presets. Delete one first.")
            raw.append(preset.to_dict())
            self._write_raw(raw)
        return preset

    def delete(self, preset_id: str) -> None:
        if preset_id == "my-default":
            raise ValidationError("The built-in 'My Default' preset cannot be deleted.")
        with self._lock:
            raw = self._read_raw()
            remaining = [item for item in raw if item.get("id") != preset_id]
            if len(remaining) == len(raw):
                raise NotFoundError(f"Preset '{preset_id}' was not found.")
            self._write_raw(remaining)
