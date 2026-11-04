"""来源统计聚合、实际 PNG 像素与文字边界测试。"""

import copy
import importlib.util
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "src/nonebot_plugin_group_guard/charts.py"
SCRATCH = Path(tempfile.gettempdir())
FONT = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts/msyh.ttc"


def load_module():
    if not MODULE_PATH.exists():
        return None
    spec = importlib.util.spec_from_file_location("_guard_charts_test", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


CHARTS = load_module()


class ChartTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(CHARTS, "来源图表模块尚未实现")

    def render(self, records, **kwargs):
        data = CHARTS.render_chart("示例群", records, **kwargs)
        self.assertIsInstance(data, bytes)
        self.assertTrue(data.startswith(b"\x89PNG\r\n\x1a\n"))
        with Image.open(io.BytesIO(data)) as image:
            self.assertEqual(image.format, "PNG")
            image.load()
            return image.convert("RGB")

    def test_summarize_counts_descending_and_ties_by_name(self):
        records = {
            "a": {"category": "B"}, "b": {"category": "A"},
            "c": {"category": "B"}, "d": {"category": "A"},
            "e": {"category": "C"},
        }
        self.assertEqual(CHARTS.summarize(records), [("A", 2), ("B", 2), ("C", 1)])

    def test_missing_empty_or_invalid_categories_are_unknown_people(self):
        records = {
            "a": {}, "b": {"category": None}, "c": {"category": "  "},
            "d": None, "e": {"category": ["bad"]}, "f": {"category": "  示例  "},
        }
        self.assertEqual(CHARTS.summarize(records), [("未知", 5), ("示例", 1)])
        self.assertEqual(sum(count for _, count in CHARTS.summarize(records)), len(records))

    def test_explicit_empty_source_is_not_replaced_by_approval_rule_or_category(self):
        records = {
            "a": {"category": "知识题", "source_category": None, "approval_rule": "知识题"},
            "b": {"category": "人工审核", "source_category": ""},
            "c": {"category": "旧名称", "source_category": "GitHub"},
            "d": {"category": "旧来源"},
        }
        self.assertEqual(CHARTS.summarize(records), [("GitHub", 1), ("旧来源", 1)])

    def test_empty_records_produce_empty_summary_and_fixed_size_png(self):
        self.assertEqual(CHARTS.summarize({}), [])
        image = self.render({})
        self.assertEqual(image.size, (960, 1320))
        self.assertGreater(len(image.getcolors(960 * 1320)), 5)

    def test_render_does_not_change_input_records(self):
        records = {"a": {"category": "示例", "accept_time": "2025-01-01 12:00:00"}}
        original = copy.deepcopy(records)
        self.render(records)
        self.assertEqual(records, original)

    def test_background_is_preserved_above_bottom_card(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="guard-chart-", dir=SCRATCH) as directory:
            path = Path(directory) / "neutral.png"
            Image.new("RGB", (120, 80), (21, 54, 87)).save(path)
            image = self.render({"a": {"category": "A"}}, background_path=str(path))
        self.assertEqual(image.size, (960, 1320))
        self.assertEqual(image.getpixel((20, 20)), (21, 54, 87))
        self.assertEqual(image.getpixel((480, 500)), (21, 54, 87))
        self.assertNotEqual(image.getpixel((480, 1220)), (21, 54, 87))

    def test_unreadable_background_and_font_fall_back(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="guard-chart-", dir=SCRATCH) as directory:
            path = Path(directory) / "broken.png"
            path.write_text("not an image", encoding="utf-8")
            image = self.render({"a": {"category": "示例"}}, font_path=str(Path(directory) / "missing.ttf"), background_path=str(path))
        self.assertEqual(image.size, (960, 1320))

    def test_actual_bar_pixels_are_sorted_and_proportional(self):
        records = {}
        for name, count in [("A", 5), ("B", 3), ("C", 1)]:
            for index in range(count):
                records[f"{name}-{index}"] = {"category": name}
        image = self.render(records)
        blue = (91, 155, 213)
        pixels = image.load()
        row_widths = []
        for y in range(image.height):
            xs = [x for x in range(image.width) if pixels[x, y] == blue]
            row_widths.append(max(xs) - min(xs) + 1 if xs else 0)
        bars = []
        for y, width in enumerate(row_widths):
            if width and (y == 0 or not row_widths[y - 1]):
                bars.append(width)
        self.assertEqual(len(bars), 3)
        self.assertGreater(bars[0], bars[1])
        self.assertGreater(bars[1], bars[2])
        self.assertAlmostEqual(bars[1] / bars[0], 3 / 5, delta=0.025)
        self.assertAlmostEqual(bars[2] / bars[0], 1 / 5, delta=0.025)

    def test_long_categories_are_fully_drawn_inside_canvas(self):
        long_name = "这是用于检查分类文字自动换行且不会被裁切的中性来源分类" * 4
        records = {"a": {"category": long_name}, "b": {"category": "第二分类"}}
        drawn = []
        original = ImageDraw.ImageDraw.text

        def record_text(draw, xy, text, *args, **kwargs):
            bounds = draw.textbbox(xy, text, font=kwargs.get("font"), anchor=kwargs.get("anchor"))
            drawn.append((str(text), bounds))
            return original(draw, xy, text, *args, **kwargs)

        with patch.object(ImageDraw.ImageDraw, "text", record_text):
            image = self.render(records, font_path=str(FONT) if FONT.exists() else "")
        joined = "".join(text for text, _ in drawn)
        self.assertIn(long_name, joined)
        self.assertIn("第二分类", joined)
        for text, (left, top, right, bottom) in drawn:
            with self.subTest(text=text):
                self.assertGreaterEqual(left, 0)
                self.assertGreaterEqual(top, 0)
                self.assertLessEqual(right, image.width)
                self.assertLessEqual(bottom, image.height)

    def test_many_categories_fit_fixed_canvas_without_dropping_labels(self):
        records = {str(index): {"category": f"来源-{index:02d}"} for index in range(24)}
        labels = []
        original = ImageDraw.ImageDraw.text

        def record_text(draw, xy, text, *args, **kwargs):
            labels.append(str(text))
            bounds = draw.textbbox(xy, text, font=kwargs.get("font"), anchor=kwargs.get("anchor"))
            self.assertGreaterEqual(bounds[0], 0)
            self.assertGreaterEqual(bounds[1], 0)
            self.assertLessEqual(bounds[2], 960)
            self.assertLessEqual(bounds[3], 1320)
            return original(draw, xy, text, *args, **kwargs)

        with patch.object(ImageDraw.ImageDraw, "text", record_text):
            image = self.render(records)
        self.assertEqual(image.size, (960, 1320))
        for record in records.values():
            self.assertIn(record["category"], "".join(labels))

    def test_original_custom_watermark_text_and_style_are_preserved(self):
        watermark = "©PL!HS ©S ©2023 BNML ©ODD No. Generated by KhBot"
        drawn = []
        original = ImageDraw.ImageDraw.text

        def record_text(draw, xy, text, *args, **kwargs):
            if text == watermark:
                drawn.append((xy, kwargs))
            return original(draw, xy, text, *args, **kwargs)

        with patch.object(ImageDraw.ImageDraw, "text", record_text):
            self.render({"a": {"category": "示例"}})
        self.assertEqual(len(drawn), 1, "原插件的自定义水印必须实际绘制一次")
        xy, style = drawn[0]
        # 原图 120 dpi 下的 16 pt，对应 27 px，右下锚点保留 0.93 / 0.03。
        self.assertEqual(xy, (893, 1280))
        self.assertEqual(style["anchor"], "rb")
        self.assertEqual(style["fill"], "#333333")
        self.assertEqual(style["stroke_width"], 1)
        self.assertEqual(style["stroke_fill"], "#F5F5F5")
        self.assertEqual(style["font"].size, 27)

    def test_watermark_footer_pixels_match_original_for_empty_and_dense_charts(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        watermark = "©PL!HS ©S ©2023 BNML ©ODD No. Generated by KhBot"
        background_color = (21, 54, 87)
        font_path = str(FONT) if FONT.exists() else ""
        with tempfile.TemporaryDirectory(prefix="guard-watermark-", dir=SCRATCH) as directory:
            background = Path(directory) / "neutral.png"
            Image.new("RGB", (960, 1320), background_color).save(background)
            expected = Image.new("RGB", (960, 1320), background_color)
            draw = ImageDraw.Draw(expected)
            draw.text((893, 1280), watermark, font=CHARTS._font(27, font_path),
                      fill="#F5F5F5", anchor="rb", stroke_width=1, stroke_fill="#333333")
            footer = (0, 1255, 960, 1320)
            expected_pixels = expected.crop(footer).tobytes()
            for records in ({}, {str(i): {"category": f"来源-{i:02d}"} for i in range(24)}):
                with self.subTest(categories=len(records)):
                    image = self.render(records, font_path=font_path, background_path=str(background))
                    self.assertEqual(image.crop(footer).tobytes(), expected_pixels,
                                     "底部水印不能缺失或被统计卡片覆盖")
                    self.assertNotEqual(image.crop(footer).tobytes(),
                                        Image.new("RGB", image.crop(footer).size, background_color).tobytes())

    def test_watermark_chooses_color_from_its_local_background_not_whole_image(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        watermark = "©PL!HS ©S ©2023 BNML ©ODD No. Generated by KhBot"
        original = ImageDraw.ImageDraw.text
        with tempfile.TemporaryDirectory(prefix="guard-watermark-colors-", dir=SCRATCH) as directory:
            for local, global_color, expected in (
                ((250, 250, 250), (8, 8, 8), "#333333"),
                ((8, 8, 8), (250, 250, 250), "#F5F5F5"),
            ):
                background = Path(directory) / "mixed.png"
                image = Image.new("RGB", (960, 1320), global_color)
                ImageDraw.Draw(image).rectangle((0, 1255, 959, 1319), fill=local)
                image.save(background)
                captured = []

                def record_text(draw, xy, text, *args, **kwargs):
                    if text == watermark:
                        captured.append(kwargs["fill"])
                    return original(draw, xy, text, *args, **kwargs)

                with self.subTest(local=local), patch.object(ImageDraw.ImageDraw, "text", record_text):
                    self.render({"a": {"category": "示例"}}, background_path=str(background))
                    self.assertEqual(captured, [expected])

    def test_watermark_has_clear_gap_below_statistics_card(self):
        card_edges, watermark_bounds = [], []
        original_rectangle = ImageDraw.ImageDraw.rounded_rectangle
        original_text = ImageDraw.ImageDraw.text

        def record_rectangle(draw, xy, *args, **kwargs):
            card_edges.append(xy[3])
            return original_rectangle(draw, xy, *args, **kwargs)

        def record_text(draw, xy, text, *args, **kwargs):
            if 'Generated by KhBot' in text:
                watermark_bounds.append(draw.textbbox(xy, text, font=kwargs['font'],
                    anchor=kwargs['anchor'], stroke_width=kwargs.get('stroke_width', 0)))
            return original_text(draw, xy, text, *args, **kwargs)

        with patch.object(ImageDraw.ImageDraw, 'rounded_rectangle', record_rectangle), \
                patch.object(ImageDraw.ImageDraw, 'text', record_text):
            self.render({'a': {'category': '示例'}})
        self.assertEqual(len(card_edges), 1)
        self.assertEqual(len(watermark_bounds), 1)
        self.assertGreater(watermark_bounds[0][1], card_edges[0] + 2)

    def test_render_is_deterministic_for_same_inputs(self):
        records = {"a": {"category": "示例"}}
        self.assertEqual(CHARTS.render_chart("示例群", records), CHARTS.render_chart("示例群", records))


if __name__ == "__main__":
    unittest.main()
