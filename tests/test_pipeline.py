"""Unit tests for the processing layer.

These are pure-Python tests: no FFmpeg, no network, no model downloads. The
heavier end-to-end checks that actually shell out to FFmpeg live in
`scripts/smoke_test.py` so that `python -m unittest` stays fast.
"""

from __future__ import annotations

import io
import unittest

from app.api.jobs import compute_overall
from app.api.multipart import iter_parts, parse_boundary
from app.captions import Phrase, Word, enforce_minimum_display, group_words, words_from_text
from app.models import (
    CaptionSettings,
    EffectSettings,
    JobStage,
    SilenceSettings,
    VideoEffect,
    ViewportSettings,
)
from app.processor import effects as fx
from app.processor.ass_render import build_ass, outline_to_pixels, shadow_to_pixels
from app.processor.silence import build_keep_intervals, parse_silence_log


def make_words(text: str, step: float = 0.4) -> list[Word]:
    return [
        Word(text=token, start=index * step, end=index * step + step * 0.9)
        for index, token in enumerate(text.split())
    ]


def caption_settings(**overrides) -> CaptionSettings:
    """Caption settings tuned for grouping tests (casing off unless asked for)."""
    base = {"uppercase": False, "max_chars_per_phrase": 999}
    base.update(overrides)
    return CaptionSettings(**base)


def silence_settings(**overrides) -> SilenceSettings:
    base = {
        "min_silence_duration": 0.0,
        "pad_before": 0.0,
        "pad_after": 0.0,
        "min_segment_duration": 0.0,
    }
    base.update(overrides)
    return SilenceSettings(**base)


class CaptionGroupingTests(unittest.TestCase):
    def test_never_exceeds_max_words(self):
        words = make_words("this is the most important thing you need to understand right now")
        for maximum in (1, 2, 3, 4, 5):
            phrases = group_words(words, caption_settings(max_words_per_phrase=maximum))
            for phrase in phrases:
                self.assertLessEqual(
                    len(phrase.text.split()),
                    maximum,
                    f"max_words={maximum} violated by {phrase.text!r}",
                )

    def test_default_three_word_grouping(self):
        words = make_words("this is the most important thing you need to understand")
        phrases = group_words(words, caption_settings(max_words_per_phrase=3))
        self.assertTrue(phrases)
        self.assertEqual(phrases[0].text, "this is the")
        # Every word survives grouping, in order.
        rebuilt = " ".join(p.text for p in phrases)
        self.assertEqual(rebuilt.split(), [w.text for w in words])

    def test_three_words_is_the_shipped_default(self):
        self.assertEqual(CaptionSettings().max_words_per_phrase, 3)

    def test_sentence_boundary_breaks_phrase(self):
        words = make_words("stop here. now we continue")
        phrases = group_words(words, caption_settings())
        self.assertGreaterEqual(len(phrases), 2)
        self.assertTrue(phrases[0].text.startswith("stop here"))

    def test_long_pause_breaks_phrase(self):
        words = [
            Word(text="before", start=0.0, end=0.4),
            Word(text="after", start=3.0, end=3.4),  # 2.6s gap
        ]
        phrases = group_words(words, caption_settings())
        self.assertEqual(len(phrases), 2)

    def test_character_budget_breaks_long_words(self):
        words = make_words("extraordinarily complicated terminology")
        phrases = group_words(
            words, caption_settings(max_words_per_phrase=3, max_chars_per_phrase=20)
        )
        for phrase in phrases:
            self.assertLessEqual(len(phrase.text), 40)
        self.assertGreater(len(phrases), 1)

    def test_phrase_times_are_monotonic(self):
        words = make_words("one two three four five six seven eight")
        phrases = group_words(words, caption_settings())
        for earlier, later in zip(phrases, phrases[1:]):
            self.assertLessEqual(earlier.end, later.start + 1e-6)
        for phrase in phrases:
            self.assertLess(phrase.start, phrase.end)

    def test_uppercase_setting_is_applied(self):
        phrases = group_words(make_words("quiet words"), caption_settings(uppercase=True))
        self.assertEqual(phrases[0].text, "QUIET WORDS")

    def test_empty_input_yields_no_phrases(self):
        self.assertEqual(group_words([], caption_settings()), [])

    def test_minimum_display_extends_flash_frames(self):
        phrases = [
            Phrase(text="hi", start=0.0, end=0.05),
            Phrase(text="there", start=2.0, end=2.02),
        ]
        adjusted = enforce_minimum_display(phrases, minimum=0.3)
        self.assertGreaterEqual(adjusted[0].end - adjusted[0].start, 0.29)
        self.assertGreaterEqual(adjusted[1].end - adjusted[1].start, 0.29)

    def test_minimum_display_never_overlaps_the_next_card(self):
        phrases = [
            Phrase(text="a", start=0.0, end=0.05),
            Phrase(text="b", start=0.10, end=0.60),
        ]
        adjusted = enforce_minimum_display(phrases, minimum=0.3)
        self.assertLessEqual(adjusted[0].end, adjusted[1].start + 1e-9)

    def test_words_from_text_spans_clip_duration(self):
        words = words_from_text("alpha beta gamma delta", 8.0)
        self.assertEqual([w.text for w in words], ["alpha", "beta", "gamma", "delta"])
        self.assertAlmostEqual(words[0].start, 0.0, places=3)
        self.assertLessEqual(words[-1].end, 8.0 + 1e-6)
        for earlier, later in zip(words, words[1:]):
            self.assertLessEqual(earlier.end, later.start + 1e-6)

    def test_manual_transcript_respects_the_three_word_limit(self):
        words = words_from_text(
            "this is the most important thing you need to understand right now", 10.0
        )
        phrases = group_words(words, caption_settings(max_words_per_phrase=3))
        for phrase in phrases:
            self.assertLessEqual(len(phrase.text.split()), 3)


class SilenceTests(unittest.TestCase):
    LOG = """
[silencedetect @ 0x1] silence_start: 1.5
[silencedetect @ 0x1] silence_end: 3.0 | silence_duration: 1.5
[silencedetect @ 0x1] silence_start: 6.25
[silencedetect @ 0x1] silence_end: 7.5 | silence_duration: 1.25
"""

    def test_parse_silence_log(self):
        self.assertEqual(parse_silence_log(self.LOG), [(1.5, 3.0), (6.25, 7.5)])

    def test_unterminated_silence_is_ignored(self):
        # A silence_start with no matching silence_end cannot be trusted, so it
        # is dropped rather than guessed at.
        self.assertEqual(parse_silence_log("silence_start: 4.0\n"), [])

    def test_empty_log_means_no_silence(self):
        self.assertEqual(parse_silence_log(""), [])

    def test_keep_intervals_are_the_complement(self):
        keeps = build_keep_intervals(
            [(1.5, 3.0), (6.25, 7.5)], 10.0, silence_settings()
        )
        self.assertEqual(keeps, [(0.0, 1.5), (3.0, 6.25), (7.5, 10.0)])

    def test_short_silences_are_left_alone(self):
        # A 0.2s breath is shorter than the minimum, so nothing is cut.
        keeps = build_keep_intervals(
            [(2.0, 2.2)], 10.0, silence_settings(min_silence_duration=0.45)
        )
        self.assertEqual(keeps, [(0.0, 10.0)])

    def test_padding_shrinks_the_cut_not_the_speech(self):
        keeps = build_keep_intervals(
            [(2.0, 5.0)], 8.0, silence_settings(pad_before=0.1, pad_after=0.2)
        )
        # Speech before the silence keeps 0.2s of tail; speech after keeps 0.1s of head.
        self.assertAlmostEqual(keeps[0][1], 2.2, places=6)
        self.assertAlmostEqual(keeps[1][0], 4.9, places=6)

    def test_keeps_never_overlap_or_reverse(self):
        keeps = build_keep_intervals(
            [(1.0, 1.2), (1.3, 1.5), (2.0, 9.0)],
            10.0,
            silence_settings(pad_before=0.15, pad_after=0.15, min_segment_duration=0.05),
        )
        for start, end in keeps:
            self.assertLess(start, end)
        for earlier, later in zip(keeps, keeps[1:]):
            self.assertLessEqual(earlier[1], later[0] + 1e-9)

    def test_total_silence_falls_back_to_keeping_everything(self):
        # A music-only or silent clip must not render an empty video.
        keeps = build_keep_intervals([(0.0, 10.0)], 10.0, silence_settings())
        self.assertEqual(keeps, [(0.0, 10.0)])

    def test_no_silence_keeps_everything(self):
        self.assertEqual(build_keep_intervals([], 10.0, silence_settings()), [(0.0, 10.0)])

    def test_zero_duration_keeps_nothing(self):
        self.assertEqual(build_keep_intervals([], 0.0, silence_settings()), [])


class EffectTimelineTests(unittest.TestCase):
    def test_auto_timeline_is_deterministic(self):
        settings = EffectSettings(mode="auto", auto_zoom_amount=0.06, auto_cycle_seconds=8.0)
        signature = lambda tl: [(e.start, e.end, e.type, e.scale, e.scale_to) for e in tl]
        self.assertEqual(
            signature(fx.auto_timeline(12.0, settings)),
            signature(fx.auto_timeline(12.0, settings)),
        )

    def test_auto_timeline_covers_the_clip_without_gaps(self):
        timeline = fx.auto_timeline(11.0, EffectSettings(mode="auto"))
        self.assertAlmostEqual(timeline[0].start, 0.0, places=6)
        self.assertAlmostEqual(timeline[-1].end, 11.0, places=6)
        for earlier, later in zip(timeline, timeline[1:]):
            self.assertAlmostEqual(earlier.end, later.start, places=6)

    def test_auto_zoom_stays_subtle(self):
        settings = EffectSettings(mode="auto", auto_zoom_amount=0.06)
        for effect in fx.auto_timeline(30.0, settings):
            for value in (effect.scale, effect.scale_to or effect.scale):
                self.assertGreaterEqual(value, 1.0)
                self.assertLessEqual(value, 1.07)

    def test_none_mode_is_static(self):
        self.assertTrue(fx.is_static(fx.resolve_timeline(10.0, EffectSettings(mode="none"))))

    def test_auto_mode_is_not_static(self):
        self.assertFalse(fx.is_static(fx.resolve_timeline(20.0, EffectSettings(mode="auto"))))

    def test_manual_timeline_is_honoured(self):
        settings = EffectSettings(
            mode="manual",
            effects=(
                VideoEffect(start=0.0, end=3.0, type="normal", scale=1.0),
                VideoEffect(start=3.0, end=7.0, type="zoom_in", scale=1.0, scale_to=1.1),
            ),
        )
        timeline = fx.resolve_timeline(7.0, settings)
        self.assertTrue(any(e.type == "zoom_in" for e in timeline))
        self.assertFalse(fx.is_static(timeline))

    def test_expressions_are_frame_time_based_and_balanced(self):
        timeline = fx.resolve_timeline(12.0, EffectSettings(mode="auto"))
        exprs = fx.build_expressions(timeline, fps=30)
        self.assertEqual(set(exprs), {"z", "x", "y"})
        for expression in exprs.values():
            self.assertNotIn(" ", expression)
            self.assertEqual(expression.count("("), expression.count(")"))
        # zoompan has no `t`, so time must come from the output frame index.
        self.assertIn("on/30", exprs["z"])
        # The crop window is centred, which is what keeps the square fixed.
        self.assertIn("(iw-iw/zoom)/2", exprs["x"])
        self.assertIn("(ih-ih/zoom)/2", exprs["y"])

    def test_pan_effects_produce_offsets(self):
        for effect_type in ("pan_left", "pan_right", "pan_up", "pan_down"):
            settings = EffectSettings(
                mode="manual",
                effects=(VideoEffect(start=0.0, end=4.0, type=effect_type, scale=1.1),),
            )
            timeline = fx.resolve_timeline(4.0, settings)
            self.assertFalse(fx.is_static(timeline), effect_type)

    def test_every_declared_effect_type_resolves(self):
        from app.models import EFFECT_TYPES

        for effect_type in EFFECT_TYPES:
            settings = EffectSettings(
                mode="manual",
                effects=(VideoEffect(start=0.0, end=2.0, type=effect_type, scale=1.05),),
            )
            exprs = fx.build_expressions(fx.resolve_timeline(2.0, settings), fps=30)
            self.assertEqual(set(exprs), {"z", "x", "y"}, effect_type)


class AssRenderTests(unittest.TestCase):
    def test_ui_units_map_to_renderer_pixels(self):
        # The user's "0.5 outline" becomes a hairline border, not 0.5 raw pixels.
        self.assertAlmostEqual(
            outline_to_pixels(CaptionSettings(outline_width=0.5, font_size=62)), 2.5, places=4
        )
        # Outline scales with font size, so styling survives a size change.
        self.assertAlmostEqual(
            outline_to_pixels(CaptionSettings(outline_width=0.5, font_size=124)), 5.0, places=4
        )
        self.assertAlmostEqual(
            shadow_to_pixels(CaptionSettings(shadow_offset=3.0, shadow_strength=1.0, font_size=62)),
            3.0,
            places=4,
        )
        self.assertAlmostEqual(
            shadow_to_pixels(CaptionSettings(shadow_offset=3.0, shadow_strength=2.0, font_size=62)),
            6.0,
            places=4,
        )

    def test_outline_is_never_negative(self):
        self.assertGreaterEqual(outline_to_pixels(CaptionSettings(outline_width=-5)), 0.0)

    def test_ass_playres_matches_the_square_viewport(self):
        phrases = [Phrase(text="hello there", start=0.0, end=1.0)]
        ass = build_ass(phrases, CaptionSettings(), viewport_size=1000, font_family="DejaVu Sans")
        # Captions are authored in viewport space; libass clips them to the square.
        self.assertIn("PlayResX: 1000", ass)
        self.assertIn("PlayResY: 1000", ass)
        self.assertIn("ScaledBorderAndShadow: yes", ass)
        self.assertIn("[Events]", ass)
        self.assertIn("Dialogue:", ass)

    def test_ass_playres_follows_a_resized_viewport(self):
        ass = build_ass([], CaptionSettings(), viewport_size=720, font_family="DejaVu Sans")
        self.assertIn("PlayResX: 720", ass)
        self.assertIn("PlayResY: 720", ass)

    def test_ass_escapes_characters_that_would_become_override_tags(self):
        phrases = [Phrase(text="a {b} c\\d", start=0.0, end=1.0)]
        ass = build_ass(phrases, CaptionSettings(), viewport_size=1000, font_family="DejaVu Sans")
        dialogue = [line for line in ass.splitlines() if line.startswith("Dialogue:")][0]
        self.assertNotIn("{b}", dialogue)
        self.assertNotIn("\\d", dialogue)

    def test_selected_font_family_is_used(self):
        ass = build_ass([], CaptionSettings(), viewport_size=1000, font_family="Indivisible")
        self.assertIn("Style: Caption,Indivisible,", ass)

    def test_zero_length_and_blank_phrases_are_dropped(self):
        phrases = [
            Phrase(text="keep", start=0.0, end=1.0),
            Phrase(text="", start=1.0, end=2.0),
            Phrase(text="reversed", start=3.0, end=3.0),
        ]
        ass = build_ass(phrases, CaptionSettings(), viewport_size=1000, font_family="DejaVu Sans")
        self.assertEqual(ass.count("Dialogue:"), 1)

    def test_no_phrases_still_produces_a_valid_script(self):
        ass = build_ass([], CaptionSettings(), viewport_size=1000, font_family="DejaVu Sans")
        self.assertIn("[Script Info]", ass)
        self.assertIn("[V4+ Styles]", ass)
        self.assertIn("[Events]", ass)


class ViewportTests(unittest.TestCase):
    def test_default_viewport_is_centered_horizontally(self):
        self.assertEqual(ViewportSettings(size=1000).resolved_x(1080), 40)

    def test_explicit_x_is_respected(self):
        self.assertEqual(ViewportSettings(size=800, x=100).resolved_x(1080), 100)

    def test_viewport_larger_than_canvas_is_rejected(self):
        with self.assertRaises(Exception):
            ViewportSettings(size=1200).validate_against_canvas(1080, 1920)

    def test_viewport_below_the_canvas_bottom_is_rejected(self):
        with self.assertRaises(Exception):
            ViewportSettings(size=1000, y=1500).validate_against_canvas(1080, 1920)

    def test_default_viewport_fits_the_default_canvas(self):
        ViewportSettings().validate_against_canvas(1080, 1920)  # must not raise


class ProgressWeightingTests(unittest.TestCase):
    ORDER = [
        JobStage.ANALYZING,
        JobStage.EXTRACTING,
        JobStage.DETECTING_SILENCE,
        JobStage.REMOVING_SILENCE,
        JobStage.TRANSCRIBING,
        JobStage.BUILDING_CAPTIONS,
        JobStage.RENDERING_HOOK,
        JobStage.ENCODING,
        JobStage.FINALIZING,
    ]

    def test_progress_is_monotonic_across_stages(self):
        values = [compute_overall(stage, 0.0) for stage in self.ORDER]
        for earlier, later in zip(values, values[1:]):
            self.assertLessEqual(earlier, later)

    def test_bounds(self):
        self.assertGreaterEqual(compute_overall(JobStage.ANALYZING, 0.0), 0.0)
        self.assertEqual(compute_overall(JobStage.DONE, 0.0), 1.0)
        self.assertLessEqual(compute_overall(JobStage.FINALIZING, 1.0), 1.0)

    def test_within_stage_progress_advances_the_bar(self):
        self.assertLess(
            compute_overall(JobStage.ENCODING, 0.1), compute_overall(JobStage.ENCODING, 0.9)
        )

    def test_encoding_is_the_heaviest_stage(self):
        spans = {
            stage: compute_overall(stage, 1.0) - compute_overall(stage, 0.0)
            for stage in self.ORDER
        }
        self.assertEqual(max(spans, key=spans.get), JobStage.ENCODING)


class MultipartTests(unittest.TestCase):
    """The parser is streaming: each part must be consumed as it is yielded."""

    BOUNDARY = "----clipforgeTEST"

    def build_body(self, filename: str, payload: bytes) -> bytes:
        b = self.BOUNDARY.encode()
        return (
            b"--" + b + b"\r\n"
            b'Content-Disposition: form-data; name="note"\r\n\r\n'
            b"hello\r\n"
            b"--" + b + b"\r\n"
            b'Content-Disposition: form-data; name="file"; filename="'
            + filename.encode()
            + b'"\r\n'
            b"Content-Type: video/mp4\r\n\r\n" + payload + b"\r\n"
            b"--" + b + b"--\r\n"
        )

    def parse(self, body: bytes):
        """Consume every part in order, returning (name, filename, bytes)."""
        collected = []
        for part in iter_parts(
            io.BytesIO(body),
            content_type=f"multipart/form-data; boundary={self.BOUNDARY}",
            content_length=len(body),
        ):
            collected.append((part.name, part.filename, part.stream.read()))
        return collected

    def test_parse_boundary(self):
        self.assertEqual(parse_boundary('multipart/form-data; boundary="abc123"'), b"abc123")
        self.assertEqual(parse_boundary("multipart/form-data; boundary=abc123"), b"abc123")

    def test_non_multipart_is_rejected(self):
        with self.assertRaises(Exception):
            parse_boundary("application/json")

    def test_missing_boundary_is_rejected(self):
        with self.assertRaises(Exception):
            parse_boundary("multipart/form-data")

    def test_round_trip_preserves_bytes(self):
        payload = bytes(range(256)) * 500  # 128 KB, crosses the internal chunk size
        parts = self.parse(self.build_body("clip.mp4", payload))

        self.assertEqual(parts[0][0], "note")
        self.assertIsNone(parts[0][1])
        self.assertEqual(parts[0][2], b"hello")

        self.assertEqual(parts[1][0], "file")
        self.assertEqual(parts[1][1], "clip.mp4")
        self.assertEqual(parts[1][2], payload)

    def test_payload_containing_boundary_like_bytes(self):
        # Content with "--" runs must not terminate the part early.
        payload = b"--not-the-boundary--\r\n" * 200
        parts = self.parse(self.build_body("tricky.mp4", payload))
        self.assertEqual(parts[1][2], payload)

    def test_empty_file_part(self):
        parts = self.parse(self.build_body("empty.mp4", b""))
        self.assertEqual(parts[1][2], b"")

    def test_is_file_flag(self):
        body = self.build_body("clip.mp4", b"data")
        flags = []
        for part in iter_parts(
            io.BytesIO(body),
            content_type=f"multipart/form-data; boundary={self.BOUNDARY}",
            content_length=len(body),
        ):
            flags.append(part.is_file)
            part.stream.read()
        self.assertEqual(flags, [False, True])

    def test_skipped_part_does_not_corrupt_the_next_one(self):
        body = self.build_body("skip.mp4", b"x" * 5000)
        names = []
        for part in iter_parts(
            io.BytesIO(body),
            content_type=f"multipart/form-data; boundary={self.BOUNDARY}",
            content_length=len(body),
        ):
            names.append(part.name)  # deliberately never read part.stream
        self.assertEqual(names, ["note", "file"])


if __name__ == "__main__":
    unittest.main()
