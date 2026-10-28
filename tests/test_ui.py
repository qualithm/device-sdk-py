import unittest

from qualithm_device import ui


class Screen:
    """A framebuffer double that records each lit pixel and its colour."""

    def __init__(self):
        self.lit = {}

    def pixel(self, x, y, c):
        self.lit[x, y] = c


class UiTest(unittest.TestCase):
    def test_draws_every_icon_inside_its_box(self):
        for name in ui.ICONS:
            screen = Screen()
            ui.draw_icon(screen, name, 2, 3)
            self.assertTrue(screen.lit, name)
            for x, y in screen.lit:
                self.assertTrue(2 <= x < 2 + ui.ICON_SIZE, name)
                self.assertTrue(3 <= y < 3 + ui.ICON_SIZE, name)

    def test_draws_each_mark_at_its_size(self):
        for size in ui.MARKS:
            screen = Screen()
            ui.draw_mark(screen, size, 0, 0, 2)
            self.assertTrue(screen.lit)
            self.assertEqual(set(screen.lit.values()), {2})
            self.assertTrue(all(x < size and y < size for x, y in screen.lit))

    def test_sets_text_one_advance_per_character(self):
        screen = Screen()
        end = ui.text(screen, "0.4 BAR", 10, 0)
        self.assertEqual(end, 10 + 7 * ui.GLYPH_ADVANCE)
        self.assertTrue(all(y < ui.GLYPH_HEIGHT for _, y in screen.lit))

    def test_falls_back_for_a_character_outside_the_font(self):
        self.assertEqual(ui.glyph("é"), ui.glyph(ui.FALLBACK_CHAR))
        self.assertEqual(len(ui.glyph("A")), ui.GLYPH_WIDTH)

    def test_rejects_an_unknown_icon_or_mark(self):
        with self.assertRaises(ValueError):
            ui.draw_icon(Screen(), "missing", 0, 0)
        with self.assertRaises(ValueError):
            ui.draw_mark(Screen(), 7, 0, 0)
