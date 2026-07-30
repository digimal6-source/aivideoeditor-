"""Unit tests for the configuration foundation: timecodes, colours, models,
storage safety and the preset store.

Run with:  python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from io import BytesIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import colors, models, settings as settings_mod  # noqa: E402
from app.errors import NotFoundError, UnsupportedMediaError, ValidationError  # noqa: E402
from app.presets import PresetStore  # noqa: E402
from app.storage import LocalStorage, sanitize_filename, validate_id  # noqa: E402
from app.timecode import (  # noqa: E402
    format_ass_time,
    format_timecode,
    parse_timecode,
    validate_clip,
)


class TimecodeTests(unittest.TestCase):
    def test_parses_supported_formats(self):
        self.assertEqual(parse_timecode("01:02:03"), 3723.0)
        self.assertEqual(parse_timecode("01:02:03.500"), 3723.5)
        self.assertEqual(parse_timecode("02:30"), 150.0)
        self.assertEqual(parse_timecode("2:30.25"), 150.25)
        self.assertEqual(parse_timecode("90"), 90.0)
        self.assertEqual(parse_timecode("90.5"), 90.5)
        self.assertEqual(parse_timecode(12), 12.0)
        self.assertEqual(parse_timecode(12.75), 12.75)

    def test_rejects_bad_input(self):
        for bad in ("", "abc", "1:2:3:4", "00:99:00", "00:00:75", "-5", None, True, [1]):
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationError):
                    parse_timecode(bad)

    def test_formatting(self):
        self.assertEqual(format_timecode(3723.5), "01:02:03")
        self.assertEqual(format_timecode(3723.5, millis=True), "01:02:03.500")
        self.assertEqual(format_ass_time(3723.456), "1:02:03.46")
        self.assertEqual(format_ass_time(0), "0:00:00.00")

    def test_clip_validation(self):
        self.assertEqual(validate_clip(10.0, 40.0, 120.0), (10.0, 40.0))
        with self.assertRaises(ValidationError):
            validate_clip(40.0, 10.0, 120.0)          # end before start
        with self.assertRaises(ValidationError):
            validate_clip(10.0, 10.2, 120.0)          # too short
        with self.assertRaises(ValidationError):
            validate_clip(10.0, 700.0, 1200.0)        # too long
        with self.assertRaises(ValidationError):
            validate_clip(130.0, 140.0, 120.0)        # start past duration
        with self.assertRaises(ValidationError):
            validate_clip(10.0, 200.0, 120.0)         # end past duration
        # container rounding tolerance
        self.assertEqual(validate_clip(10.0, 120.1, 120.0), (10.0, 120.0))


class ColorTests(unittest.TestCase):
    def test_default_is_electric_violet(self):
        self.assertEqual(colors.DEFAULT_HIGHLIGHT_COLOR, "#8B5CF6")
        self.assertEqual(colors.HIGHLIGHT_PRESETS[0].name, "Electric Violet")

    def test_palette_entries_are_complete_and_valid(self):
        for entry in colors.palette():
            self.assertTrue(entry["id"] and entry["name"] and entry["hex"])
            self.assertTrue(colors.is_valid_hex(entry["hex"]))

    def test_normalize(self):
        self.assertEqual(colors.normalize_hex("#8b5cf6"), "#8B5CF6")
        self.assertEqual(colors.normalize_hex("8b5cf6"), "#8B5CF6")
        self.assertEqual(colors.normalize_hex("#abc"), "#AABBCC")
        self.assertEqual(colors.normalize_hex("electric-violet"), "#8B5CF6")

    def test_rejects_invalid_hex(self):
        for bad in ("#12345", "#GGGGGG", "blue", "", "#1234567"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationError):
                    colors.normalize_hex(bad)

    def test_conversions(self):
        self.assertEqual(colors.hex_to_rgb("#8B5CF6"), (139, 92, 246))
        # ASS is &HAABBGGRR
        self.assertEqual(colors.hex_to_ass("#8B5CF6"), "&H00F65C8B")
        self.assertEqual(colors.hex_to_ass("#FFFFFF"), "&H00FFFFFF")
        self.assertEqual(colors.hex_to_ffmpeg("#000000"), "0x000000")


class ModelTests(unittest.TestCase):
    def test_caption_defaults_max_three_words(self):
        caption = models.CaptionSettings.from_dict({})
        self.assertEqual(caption.max_words_per_phrase, 3)
        self.assertEqual(caption.outline_width, 0.5)

    def test_caption_alignment_codes(self):
        self.assertEqual(models.CaptionSettings.from_dict({"vertical": "bottom", "align": "center"}).ass_alignment(), 2)
        self.assertEqual(models.CaptionSettings.from_dict({"vertical": "top", "align": "left"}).ass_alignment(), 7)
        self.assertEqual(models.CaptionSettings.from_dict({"vertical": "middle", "align": "right"}).ass_alignment(), 6)

    def test_out_of_range_values_rejected(self):
        with self.assertRaises(ValidationError):
            models.CaptionSettings.from_dict({"maxWordsPerPhrase": 0})
        with self.assertRaises(ValidationError):
            models.CaptionSettings.from_dict({"fontSize": 5000})
        with self.assertRaises(ValidationError):
            models.OutputSettings.from_dict({"fps": 45})
        with self.assertRaises(ValidationError):
            models.OutputSettings.from_dict({"width": 1081})

    def test_output_presets(self):
        p30 = models.OutputSettings.from_dict({"presetId": "1080p30"})
        p60 = models.OutputSettings.from_dict({"presetId": "1080p60"})
        self.assertEqual((p30.width, p30.height, p30.fps), (1080, 1920, 30))
        self.assertEqual((p60.width, p60.height, p60.fps), (1080, 1920, 60))

    def test_hook_highlight_by_word_and_index(self):
        text = "Hiring Is Now The SLOWEST Way To Ship"
        by_word = models.HookSettings.from_dict({"text": text, "highlightWords": ["SLOWEST"]})
        self.assertEqual(by_word.highlight_indices, (4,))
        by_index = models.HookSettings.from_dict({"text": text, "highlightIndices": [0, 4]})
        self.assertEqual(by_index.highlight_indices, (0, 4))
        # out-of-range indices are ignored rather than crashing the render
        clamped = models.HookSettings.from_dict({"text": text, "highlightIndices": [99]})
        self.assertEqual(clamped.highlight_indices, ())

    def test_hook_default_highlight_colour(self):
        hook = models.HookSettings.from_dict({"text": "a b"})
        self.assertEqual(hook.highlight_color, "#8B5CF6")

    def test_viewport_must_fit_canvas(self):
        good = models.ViewportSettings.from_dict({"size": 1000, "y": 620})
        good.validate_against_canvas(1080, 1920)
        self.assertEqual(good.resolved_x(1080), 40)
        with self.assertRaises(ValidationError):
            models.ViewportSettings.from_dict({"size": 1000, "y": 1500}).validate_against_canvas(1080, 1920)

    def test_effect_validation(self):
        with self.assertRaises(ValidationError):
            models.VideoEffect.from_dict({"start": 5, "end": 5})
        effect = models.VideoEffect.from_dict({"start": "0:03", "end": "0:07", "type": "zoom_in", "scale": 1.1})
        self.assertEqual((effect.start, effect.end, effect.type, effect.scale), (3.0, 7.0, "zoom_in", 1.1))
        with self.assertRaises(ValidationError):
            models.VideoEffect.from_dict({"start": 0, "end": 1, "type": "barrel_roll"})

    def test_render_request_requires_source_and_clip(self):
        with self.assertRaises(ValidationError):
            models.RenderRequest.from_dict({"clip": {"start": 0, "end": 5}})
        with self.assertRaises(ValidationError):
            models.RenderRequest.from_dict({"sourceId": "src-1"})
        request = models.RenderRequest.from_dict(
            {"sourceId": "src-1", "clip": {"start": "0:10", "end": "0:40"}}, source_duration=120
        )
        self.assertEqual(request.clip.duration, 30.0)
        self.assertEqual(request.output.width, 1080)
        self.assertEqual(request.output.height, 1920)


class PresetSerializationTests(unittest.TestCase):
    def test_default_preset_roundtrip(self):
        original = models.default_preset()
        payload = original.to_dict()
        restored = models.Preset.from_dict(json.loads(json.dumps(payload)))
        self.assertEqual(restored.to_dict(), payload)

    def test_default_preset_values(self):
        preset = models.default_preset()
        self.assertEqual(preset.name, "My Default")
        self.assertEqual(preset.hook.font_id, "rubik-bold")
        self.assertEqual(preset.captions.font_id, "indivisible")
        self.assertEqual(preset.captions.highlight_color, "#8B5CF6")
        self.assertEqual(preset.captions.max_words_per_phrase, 3)
        self.assertEqual((preset.output.width, preset.output.height, preset.output.fps), (1080, 1920, 30))
        self.assertEqual(preset.viewport.background_color, "#000000")


class StorageTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        os.environ["DATA_DIR"] = str(root / "data")
        os.environ["FONTS_DIR"] = str(root / "fonts")
        settings_mod.reset_settings_cache()
        self.settings = settings_mod.get_settings()
        self.storage = LocalStorage(self.settings)

    def tearDown(self):
        self._tmp.cleanup()
        os.environ.pop("DATA_DIR", None)
        os.environ.pop("FONTS_DIR", None)
        settings_mod.reset_settings_cache()

    def test_sanitize_filename_blocks_traversal(self):
        self.assertEqual(sanitize_filename("../../etc/passwd"), "passwd")
        self.assertEqual(sanitize_filename(r"C:\evil\clip.mp4"), "clip.mp4")
        # a shell metacharacter survives only as a neutralised underscore
        self.assertEqual(sanitize_filename("my video;rm -rf.mp4"), "my video_rm -rf.mp4")
        # anything with a path separator keeps only the final segment
        self.assertEqual(sanitize_filename("a/b/c/../evil.mp4"), "evil.mp4")
        self.assertNotIn("\x00", sanitize_filename("bad\x00name.mp4"))

    def test_validate_id_rejects_traversal(self):
        for bad in ("../etc", "a/b", "short", "UPPER-CASE-ID", "", ".."):
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationError):
                    validate_id(bad)
        self.assertEqual(validate_id("src-0123abcd"), "src-0123abcd")

    def test_save_and_load_source(self):
        asset = self.storage.save_source(BytesIO(b"x" * 2048), "My Clip.MP4")
        self.assertEqual(asset.size_bytes, 2048)
        self.assertEqual(asset.extension, ".mp4")
        loaded = self.storage.load_source(asset.id)
        self.assertEqual(loaded.original_name, "My Clip.MP4")
        self.assertTrue(self.storage.source_path(asset.id).is_file())

    def test_rejects_unsupported_extension(self):
        with self.assertRaises(UnsupportedMediaError):
            self.storage.save_source(BytesIO(b"data"), "payload.exe")

    def test_rejects_empty_file(self):
        with self.assertRaises(ValidationError):
            self.storage.save_source(BytesIO(b""), "empty.mp4")

    def test_missing_source_raises_not_found(self):
        with self.assertRaises(NotFoundError):
            self.storage.load_source("src-deadbeef")

    def test_job_cleanup(self):
        workspace = self.storage.job_workspace("job-0123abcd")
        (workspace / "clip.mp4").write_bytes(b"tmp")
        output = self.storage.output_path("job-0123abcd")
        output.write_bytes(b"final")
        self.storage.cleanup_job("job-0123abcd", keep_output=True)
        self.assertFalse(workspace.exists())
        self.assertTrue(output.is_file())
        self.storage.cleanup_job("job-0123abcd", keep_output=False)
        self.assertFalse(output.exists())


class PresetStoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["DATA_DIR"] = str(Path(self._tmp.name) / "data")
        settings_mod.reset_settings_cache()
        self.store = PresetStore(settings_mod.get_settings())

    def tearDown(self):
        self._tmp.cleanup()
        os.environ.pop("DATA_DIR", None)
        settings_mod.reset_settings_cache()

    def test_default_always_present(self):
        presets = self.store.list()
        self.assertEqual(presets[0].id, "my-default")
        self.assertTrue(presets[0].built_in)

    def test_save_list_delete(self):
        preset = models.Preset.from_dict({"name": "Motivational", "captions": {"maxWordsPerPhrase": 2}})
        self.store.save(preset)
        ids = [p.id for p in self.store.list()]
        self.assertIn(preset.id, ids)
        self.assertEqual(self.store.get(preset.id).captions.max_words_per_phrase, 2)
        self.store.delete(preset.id)
        self.assertNotIn(preset.id, [p.id for p in self.store.list()])

    def test_builtin_is_protected(self):
        with self.assertRaises(ValidationError):
            self.store.delete("my-default")
        with self.assertRaises(ValidationError):
            self.store.save(models.Preset.from_dict({"id": "my-default", "name": "Hijack"}))

    def test_unknown_preset(self):
        with self.assertRaises(NotFoundError):
            self.store.get("nope")


if __name__ == "__main__":
    unittest.main(verbosity=2)
