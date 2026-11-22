"""Pure, conservative name extraction from explicitly calibrated roster rows.

No default screen coordinates or team semantics are assumed. ``queryable``
means suitable for subsequent exact-name resolution, not verified identity.
The caller must independently check capture identity and temporal stability.
"""
from __future__ import annotations

import math
import re
import unicodedata
from collections import defaultdict
from collections.abc import Mapping

_TEAMS = ("Alpha", "Bravo")
_MESSAGES = {
    "overlapping_regions": "名单行框有重叠，需重新校准。",
    "boundary_crossing": "识别文字跨越名单行框边界，未自动采用。",
    "ocr_line_crosses_rows": "同一识别行跨越本队多个名单行，需核对行位置。",
    "overlapping_words": "同一语言的文字框重叠，无法确认姓名边界。",
    "multiple_text_lines": "一个名单行框内出现多行候选文字，需重新校准或确认。",
    "spacing_uncertain": "姓名由多个识别片段拼接，原始空格位置未核实。",
    "truncated_name": "姓名含省略号，可能被界面截断，不能自动查询。",
    "numeric_name": "纯数字可能是姓名或界面数值，需要手动确认。",
    "control_characters": "候选包含不可见控制字符，需要手动确认。",
    "no_name": "本行未取得可用姓名。",
    "duplicate_name": "不同名单行出现相同候选姓名，不能自动确认归属。",
    "multiple_variants": "存在多个姓名候选，需完整进行精确名称消歧。",
    "malformed_ocr": "部分识别结果缺少有效文字坐标，已忽略。",
    "placeholder_name": "该栏显示 Player name 占位文字，等待真实昵称。",
    "single_cjk_glyph": "只识别到一个汉字，可能漏字；请手动核对完整昵称。",
    "crop_metadata_mismatch": "裁剪来源、索引或像素范围与当前姓名校准不一致，未自动采用。",
    "contained_name_fragment": "该短候选为更长候选的连续片段，文字框位于其 1 像素测量容差内；保留供手动核对，不单独自动查询。",
    "detached_numeric_texture": "姓名框远端独立数字片段与已由多个识别结果印证的姓名分离，已保留为背景噪声记录。",
    "script_projection_mismatch": "该识别结果把多次观测到的相邻汉字区域读成其他文字，保留原始结果供手动核对。",
    "ocr_inserted_spacing": "字符框紧邻且另有原样或坐标支持的无空格候选；OCR插入的间隔保留作诊断。",
    "supplemental_unusable": "附加亮度视图出现数字或不完整片段，保留诊断；未替代原始双视图已有的可查询姓名。",
}
_GEOMETRY = {"overlapping_regions", "boundary_crossing", "ocr_line_crosses_rows",
             "overlapping_words", "multiple_text_lines", "spacing_uncertain", "crop_metadata_mismatch"}
_SUPPLEMENTAL = {"light_text_120_3x", "soft_text_185_3x"}
_BLOCKING = _GEOMETRY | {"truncated_name", "control_characters", "duplicate_name", "numeric_name", "placeholder_name", "single_cjk_glyph"}
_CJK_GAP = re.compile(r"(?<=[\u3400-\u4dbf\u4e00-\u9fff\U00020000-\U000323af])\s+(?=[\u3400-\u4dbf\u4e00-\u9fff\U00020000-\U000323af])")
_UI_VALUE = re.compile(r"(?:\d+(?:[.,]\d+)?\s*[%％]|\d+\s*[/／]\s*\d+|\d{1,2}:\d{2}(?::\d{2})?|(?:lv\.?|level|等级|级别)\s*\d+)", re.IGNORECASE)


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _box(value, *, normalized=False):
    if isinstance(value, Mapping):
        value = [value.get(key) for key in ("x", "y", "width", "height")]
    if not isinstance(value, (list, tuple)) or len(value) != 4 or not all(_number(n) for n in value):
        raise ValueError("区域必须为 [x, y, width, height] 四个有限数值")
    x, y, width, height = map(float, value)
    if min(x, y) < 0 or min(width, height) <= 0:
        raise ValueError("区域位置不能为负，宽高必须为正")
    if normalized and (x + width > 1 + 1e-9 or y + height > 1 + 1e-9):
        raise ValueError("归一化区域必须完整位于 0–1 图像范围内")
    return [x, y, width, height]


def _contains(outer, inner):
    return (inner[0] >= outer[0] - 1e-7 and inner[1] >= outer[1] - 1e-7 and
            inner[0] + inner[2] <= outer[0] + outer[2] + 1e-7 and
            inner[1] + inner[3] <= outer[1] + outer[3] + 1e-7)


def _intersection(a, b):
    return max(0.0, min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0])) * max(
        0.0, min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1]))


def _union(boxes):
    x = min(b[0] for b in boxes)
    y = min(b[1] for b in boxes)
    return [x, y, max(b[0] + b[2] for b in boxes) - x, max(b[1] + b[3] for b in boxes) - y]


def _issue(row, code):
    if code not in row["issue_codes"]:
        row["issue_codes"].append(code)


def _regions(regions, image_size):
    if not isinstance(regions, Mapping) or set(regions) != set(_TEAMS):
        raise ValueError("请明确配置 Alpha 和 Bravo 两方名单区域")
    width, height = image_size
    rows = []
    for team in _TEAMS:
        config = regions[team]
        if isinstance(config, Mapping):
            explicit = config.get("rows")
            if isinstance(explicit, (list, tuple)):
                boxes = explicit
            else:
                if explicit not in (None, 5):
                    raise ValueError("每方必须配置五个姓名行")
                area = _box(config.get("bbox"), normalized=True)
                boxes = [[area[0], area[1] + i * area[3] / 5, area[2], area[3] / 5] for i in range(5)]
        elif isinstance(config, (list, tuple)) and len(config) == 5 and all(isinstance(item, (list, tuple, Mapping)) for item in config):
            boxes = config
        else:
            area = _box(config, normalized=True)
            boxes = [[area[0], area[1] + i * area[3] / 5, area[2], area[3] / 5] for i in range(5)]
        if len(boxes) != 5:
            raise ValueError("每方必须配置五个姓名行")
        for index, value in enumerate(boxes):
            normal = _box(value, normalized=True)
            pixel = [normal[0] * width, normal[1] * height, normal[2] * width, normal[3] * height]
            rows.append({"team": team, "index": index, "bbox": pixel, "normalized_bbox": normal,
                         "variants": [], "ignored": [], "issue_codes": [], "issues": [],
                         "valid": False, "queryable": False, "name": None, "auto_confirmable": False})
    for i, first in enumerate(rows):
        for second in rows[i + 1:]:
            if _intersection(first["bbox"], second["bbox"]) > 1e-6:
                _issue(first, "overlapping_regions")
                _issue(second, "overlapping_regions")
    return rows


def _calibrated_crops(frame, rows, image_size):
    """Validate worker crop provenance without relaxing ordinary image geometry.

    The worker rounds a calibrated rectangle outwards to physical pixels before
    cropping. A remapped OCR word can therefore be <1 px outside the floating
    rectangle while still wholly inside its actual source crop. Only a matching
    region declaration and the worker's exact source_bbox permit that boundary.
    Original row bboxes remain untouched for overlap/cross-row checks.
    """
    if frame.get("ocr_method") != "calibrated_name_regions":
        return None
    if (frame.get("coordinate_system") != "original_image_pixels"
            or tuple(image_size) != (frame.get("image_width"), frame.get("image_height"))
            or any(value != int(value) for value in image_size)):
        raise ValueError("crop image coordinate system does not match")
    declared = frame.get("regions")
    layout = frame.get("region_layout")
    if not isinstance(declared, (list, tuple)) or len(declared) != len(rows) or not isinstance(layout, (list, tuple)) or not layout:
        raise ValueError("crop layout and region declarations are required")
    by_position = {(row["team"], row["index"]): index for index, row in enumerate(rows)}
    source_rows = {}
    used = set()
    width, height = map(int, image_size)
    for roi_index, region in enumerate(declared):
        if (not isinstance(region, Mapping) or type(region.get("index")) is not int
                or not isinstance(region.get("team"), str)):
            raise ValueError("invalid declared crop index")
        row_index = by_position.get((region.get("team"), region["index"]))
        if row_index is None or row_index in used:
            raise ValueError("crop declarations do not match calibrated rows")
        normalized = _box(region.get("bbox"), normalized=True)
        expected = rows[row_index]["normalized_bbox"]
        if any(not math.isclose(a, b, rel_tol=0, abs_tol=1e-12) for a, b in zip(normalized, expected)):
            raise ValueError("crop normalized region does not match current calibration")
        x = max(0, math.floor(expected[0] * width + 1e-7))
        y = max(0, math.floor(expected[1] * height + 1e-7))
        right = min(width, math.ceil((expected[0] + expected[2]) * width - 1e-7))
        bottom = min(height, math.ceil((expected[1] + expected[3]) * height - 1e-7))
        if right <= x or bottom <= y:
            raise ValueError("crop contains no pixels")
        source_rows[roi_index] = row_index, [x, y, right - x, bottom - y]
        used.add(row_index)
    crops = {}
    for tile in layout:
        if not isinstance(tile, Mapping) or type(tile.get("roi_index")) is not int:
            raise ValueError("invalid layout crop index")
        roi_index = tile["roi_index"]
        preprocessing = tile.get("preprocessing")
        if (roi_index not in source_rows or not isinstance(preprocessing, str)
                or preprocessing not in ({"colour_3x", "light_text_140_3x"} | _SUPPLEMENTAL)):
            raise ValueError("unrecognized crop source")
        key = roi_index, preprocessing
        if key in crops:
            raise ValueError("duplicate crop source")
        row_index, expected = source_rows[roi_index]
        source = _box(tile.get("source_bbox"))
        if source != expected:
            raise ValueError("crop source_bbox does not match worker pixel rounding")
        crops[key] = row_index, source
    return crops


def _clusters(words):
    """Group words by overlapping baseline bands, independent of OCR line IDs."""
    groups = []
    for word in sorted(words, key=lambda w: (w["bbox"][1] + w["bbox"][3] / 2, w["bbox"][0])):
        matches = []
        a = word["bbox"]
        for index, group in enumerate(groups):
            for other in group:
                b = other["bbox"]
                overlap = min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1])
                if overlap >= .45 * min(a[3], b[3]) and abs(a[1] + a[3] / 2 - b[1] - b[3] / 2) <= .75 * max(a[3], b[3]):
                    matches.append(index)
                    break
        if not matches:
            groups.append([word])
        else:
            joined = [word]
            for index in reversed(matches):
                joined.extend(groups.pop(index))
            groups.append(joined)
    return [sorted(group, key=lambda w: (w["bbox"][0], w["bbox"][1])) for group in groups]


def _cluster_text(words):
    original = " ".join(word["text"] for word in words)
    if len(words) == 1:
        return original, True
    if len({word["line_id"] for word in words}) == 1:
        line = words[0]["line_text"]
        pattern = r"\s*".join(re.escape(word["text"]) for word in words)
        matches = list(re.finditer(pattern, line))
        if len(matches) == 1:
            return matches[0].group().strip(), True
    return original, False


def _visibly_truncated(text):
    # A clipped three-dot ellipsis can itself lose its last dot in OCR.
    text = unicodedata.normalize("NFKC", text).strip()
    return text.endswith("..") or "..." in text or "⋯" in text


def _numeric(text):
    return any(char.isdecimal() for char in text) and all(char.isdecimal() or char.isspace() or char in ".,，+-−" for char in text)


def _without_detached_numeric_texture(words, peers, row):
    """Exclude only a remote numeric OCR line corroborated as banner noise.

    A nearby number, a number on the same OCR line, or an uncorroborated name
    retains the ordinary conservative checks. This is for validated crops only.
    Two other independent OCR result sets must contain the exact intact name.
    """
    numbers = [word for word in words if word["text"].isdecimal() and len(word["text"]) <= 2]
    remaining = [word for word in words if word not in numbers]
    if len(numbers) != 1 or not remaining or len({word["line_id"] for word in remaining}) != 1:
        return words
    noise = numbers[0]
    if any(word["line_id"] == noise["line_id"] for word in remaining):
        return words
    name, known = _cluster_text(remaining)
    bounds = _union([word["bbox"] for word in remaining])
    area, numeric = row["bbox"], noise["bbox"]
    gap = numeric[0] - (bounds[0] + bounds[2])
    if (not known or len(name) < 2 or not any(char.isalpha() for char in name)
            or _visibly_truncated(name)
            or numeric[0] < area[0] + area[2] * .8
            or gap < max(area[2] * .25, 4 * min(bounds[3], numeric[3]))):
        return words
    corroboration = set()
    for identity, other in peers:
        if other is words or len({word["line_id"] for word in other}) != 1:
            continue
        text, certain = _cluster_text(other)
        box = _union([word["bbox"] for word in other])
        if (certain and text == name and _intersection(bounds, box) >= .7 * min(bounds[2] * bounds[3], box[2] * box[3])):
            corroboration.add(identity)
    if len(corroboration) < 2:
        return words
    row["ignored"].append({"text": noise["text"], "bbox": numeric,
                           "reason": _MESSAGES["detached_numeric_texture"],
                           "corroborating_results": len(corroboration), "name": name})
    _issue(row, "detached_numeric_texture")
    return remaining


def _touching_cjk_spacing_candidate(words):
    """Offer a second spacing candidate using observed letters and tight boxes.

    WinRT inserts spaces between CJK characters and adjacent Latin runs. Only
    a <=1px (or 10% glyph-height) measured gap may be removed here. Originals
    remain required search alternatives; this never corrects a character.
    """
    if len(words) < 2 or len({word["line_id"] for word in words}) != 1:
        return None
    original, known = _cluster_text(words)
    if not known:
        return None
    spacing = re.fullmatch(r"(\s*)".join(re.escape(word["text"]) for word in words), original)
    if spacing is None:
        return None
    pieces = [words[0]["text"]]
    changed_mixed = False
    for index, (first, second) in enumerate(zip(words, words[1:])):
        a, b = first["bbox"], second["bbox"]
        cjk_left = bool(re.fullmatch(r"[\u3400-\u4dbf\u4e00-\u9fff]", first["text"][-1]))
        cjk_right = bool(re.fullmatch(r"[\u3400-\u4dbf\u4e00-\u9fff]", second["text"][0]))
        gap = b[0] - (a[0] + a[2])
        touching = -.1 <= gap <= max(1.0, .1 * min(a[3], b[3]))
        compact = touching and (cjk_left or cjk_right)
        separator = spacing.group(index + 1)
        pieces.append(("" if compact else separator) + second["text"])
        changed_mixed |= bool(separator) and compact and cjk_left != cjk_right
    candidate = "".join(pieces)
    return candidate if changed_mixed and candidate != original else None


def _han_dot_candidate(words):
    # Chinese WinRT can surround a middle dot with synthesized spaces just as
    # it separates Han glyphs. Require the dot and both sides in the pixels.
    chars = [word["text"] for word in words]
    text = "".join(chars)
    if not re.fullmatch(r"[㐀-鿿]+(?:·[㐀-鿿]+)+", text):
        return None
    if any(len(char) != 1 for char in chars) or len({word["line_id"] for word in words}) != 1:
        return None
    for left, right in zip(words, words[1:]):
        a, b = left["bbox"], right["bbox"]
        gap = b[0] - a[0] - a[2]
        if not -1 <= gap <= .25 * max(a[3], b[3]):
            return None
    return text


def _variant(row, text, language, bbox, normalization, original):
    text = unicodedata.normalize("NFC", text.strip())
    codes = []
    if ''.join(unicodedata.normalize('NFKC', text).casefold().split()) == 'playername':
        codes.append('placeholder_name')
    if re.fullmatch(r'[\u3400-\u4dbf\u4e00-\u9fff]', text):
        codes.append('single_cjk_glyph')
    if _visibly_truncated(text):
        codes.append("truncated_name")
    if any(unicodedata.category(char) in {"Cc", "Cf"} for char in text):
        codes.append("control_characters")
    if _numeric(text):
        codes.append("numeric_name")
    for code in codes:
        _issue(row, code)
    existing = next((item for item in row["variants"] if item["text"] == text), None)
    if existing is None:
        existing = {"text": text, "languages": [], "bbox": bbox, "queryable": not codes,
                    "manual_only": bool(codes), "normalization": normalization,
                    "original_texts": [], "issue_codes": codes}
        row["variants"].append(existing)
    else:
        existing["bbox"] = _union([existing["bbox"], bbox])
        if normalization == "none":
            existing["normalization"] = "none"
    if language not in existing["languages"]:
        existing["languages"].append(language)
    if original not in existing["original_texts"]:
        existing["original_texts"].append(original)
    return existing


def _cjk_count(text):
    return len(re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff]", text))


def _confirmed_cjk_spans(observations):
    """Require matching single glyphs at the same pixels in distinct views.

    This is script evidence, not evidence that one Han spelling is correct.
    Isolated Han guesses inside Latin nicknames do not qualify.
    """
    glyphs = []
    for observation in observations:
        if observation["language"] != "zh-Hans-CN":
            continue
        for word in observation["words"]:
            if len(word["text"]) != 1 or _cjk_count(word["text"]) != 1:
                continue
            box = word["bbox"]
            if not .65 <= box[2] / box[3] <= 1.35:
                continue
            match = next((item for item in glyphs if item["text"] == word["text"] and
                          _intersection(item["bbox"], box) >= .7 * min(item["bbox"][2] * item["bbox"][3], box[2] * box[3])), None)
            if match is None:
                match = {"text": word["text"], "bbox": box, "views": set()}
                glyphs.append(match)
            match["views"].add(observation["preprocessing"])
    reliable = sorted((item for item in glyphs if len(item["views"]) >= 2), key=lambda item: item["bbox"][0])
    spans = []
    run = []
    for item in reliable:
        if run:
            a, b = run[-1]["bbox"], item["bbox"]
            gap = b[0] - a[0] - a[2]
            aligned = abs(a[1] + a[3] / 2 - b[1] - b[3] / 2) <= .25 * min(a[3], b[3])
            if not (-.5 <= gap <= .25 * min(a[3], b[3]) and aligned):
                if len(run) >= 2:
                    spans.append(run)
                run = []
        run.append(item)
    if len(run) >= 2:
        spans.append(run)
    return spans


def _prune_unsupported_observations(row):
    if _BLOCKING.intersection(row["issue_codes"]):
        return
    observations = [observation for variant in row["variants"] if variant["normalization"] == "none"
                    for observation in variant.get("observations", []) if observation.get("usable", True)]
    spans = _confirmed_cjk_spans(observations)
    for variant in row["variants"]:
        if not variant["queryable"] or not variant.get("observations"):
            continue
        usable_observations = [item for item in variant["observations"] if item.get("usable", True)]
        independent_nonhan = {(item["language"], item["preprocessing"]) for item in usable_observations
                              if item["language"] != "zh-Hans-CN"}
        if len(independent_nonhan) >= 2:
            continue
        supported = False
        for observation in usable_observations:
            matches = True
            for span in spans:
                box = _union([glyph["bbox"] for glyph in span])
                words = [word for word in observation["words"] if _intersection(word["bbox"], box) > 0]
                # A complete row candidate must account for this visible script
                # span. Keep differing Han characters and all outside text.
                if sum(_cjk_count(word["text"]) for word in words) < len(span):
                    candidate_box = _union([word["bbox"] for word in observation["words"]])
                    if _intersection(candidate_box, box) < .7 * box[2] * box[3]:
                        continue
                    # Verify the letters and punctuation outside the supported
                    # CJK span agree. Differing prefixes/suffixes are genuine
                    # alternatives, not an excuse to discard a whole name.
                    contexts = []
                    for original in observations:
                        left = [word for word in original["words"] if word["bbox"][0] + word["bbox"][2] <= box[0] + 1]
                        right = [word for word in original["words"] if word["bbox"][0] >= box[0] + box[2] - 1]
                        inside = [word for word in original["words"] if _intersection(word["bbox"], box) > 0]
                        if original["language"] == "zh-Hans-CN" and sum(_cjk_count(word["text"]) for word in inside) >= len(span):
                            contexts.append((_cluster_text(left)[0] if left else '', _cluster_text(right)[0] if right else '',
                                             _union([word["bbox"] for word in original["words"]])))
                    original_text = observation["text"]
                    if not any(original_text.startswith(left) and original_text.endswith(right)
                               and candidate_box[0] >= full_box[0] - 1
                               and candidate_box[0] + candidate_box[2] <= full_box[0] + full_box[2] + 1
                               for left, right, full_box in contexts):
                        continue
                    matches = False
                    break
            if matches:
                supported = True
                break
        if not supported:
            variant["queryable"] = False
            variant["manual_only"] = True
            variant["issue_codes"].append("script_projection_mismatch")
            _issue(row, "script_projection_mismatch")
    # Spacing may be normalized only when every actually observed separator
    # in this candidate corresponds to touching words and a compact candidate
    # was itself observed/derived from that same crop. Visible gaps survive.
    for variant in row["variants"]:
        if not variant["queryable"] or not any(char.isspace() for char in variant["text"]):
            continue
        compact = re.sub(r"\s+", "", variant["text"])
        alternatives = [other for other in row["variants"] if other["queryable"] and other["text"] == compact]
        if not alternatives:
            continue
        usable_observations = [item for item in variant.get("observations", []) if item.get("usable", True)]
        all_touching = bool(usable_observations)
        for observation in usable_observations:
            words = observation["words"]
            text, known = _cluster_text(words)
            if not known or len(words) < 2 or len({word["line_id"] for word in words}) != 1:
                all_touching = False
                break
            spacing = re.fullmatch(r"(\s*)".join(re.escape(word["text"]) for word in words), text)
            if spacing is None or any(any(char.isspace() for char in word["text"]) for word in words):
                all_touching = False
                break
            for index, (a, b) in enumerate(zip(words, words[1:])):
                gap = b["bbox"][0] - a["bbox"][0] - a["bbox"][2]
                if spacing.group(index + 1) and not -.1 <= gap <= min(1.0, .1 * min(a["bbox"][3], b["bbox"][3])):
                    all_touching = False
                    break
        if all_touching:
            variant["queryable"] = False
            variant["manual_only"] = True
            variant["issue_codes"].append("ocr_inserted_spacing")
            _issue(row, "ocr_inserted_spacing")


def _punctuation_extension(short_variant, long_variant):
    """Both observed names may be complete when only edge punctuation differs.

    This checks a literal relationship; it never strips punctuation, invents
    a candidate, or overrides a row's geometry/truncation safety checks.
    Letters, numbers, spaces and symbols in the added text do not qualify.
    """
    if _BLOCKING.intersection(short_variant["issue_codes"] + long_variant["issue_codes"]):
        return False
    short, long = short_variant["text"], long_variant["text"]
    if not any(char.isalnum() for char in short):
        return False
    start = long.find(short)
    if start < 0:
        return False
    added = long[:start] + long[start + len(short):]
    return bool(added) and all(unicodedata.category(char).startswith("P") for char in added)


def _suppress_contained_fragments(row):
    """Reduce crop OCR fragments, without treating longer names as verified.

    Only literal candidates participate: derived CJK whitespace alternatives
    are neither a reason to suppress another candidate nor suppressed here.
    Different OCR/preprocessing passes can shift text bounds by subpixels.
    Only this suppression comparison allows one original-image pixel around
    the longer text; source-crop and cross-row boundaries remain unchanged.
    Longer candidates still need exact-name resolution, not fuzzy matching.
    Separately observed literal names differing only by edge punctuation both
    remain candidates: neither observation proves which punctuation is real.
    """
    originals = [variant for variant in row["variants"] if variant["normalization"] == "none"
                 and "supplemental_unusable" not in variant["issue_codes"]]
    for short in originals:
        containing = [long["text"] for long in originals
                      if len(short["text"]) < len(long["text"])
                      and short["text"] in long["text"]
                      and not _punctuation_extension(short, long)
                      and _contains([long["bbox"][0] - 1, long["bbox"][1] - 1,
                                     long["bbox"][2] + 2, long["bbox"][3] + 2], short["bbox"])]
        if containing:
            short["queryable"] = False
            short["manual_only"] = True
            short["issue_codes"].append("contained_name_fragment")
            short["reason"] = _MESSAGES["contained_name_fragment"]
            short["contained_by"] = sorted(containing)
            _issue(row, "contained_name_fragment")


def _recover_corroborated_crop(row):
    """A fragment from another pass need not veto an intact repeated reading."""
    variants = row["variants"]
    originals = [v for v in variants if v["normalization"] == "none"]
    for issue in ("single_cjk_glyph", "spacing_uncertain"):
        if issue not in row["issue_codes"]:
            continue
        incomplete = [v for v in originals if (issue in v["issue_codes"] if issue == "single_cjk_glyph" else
                      any(not obs["spacing_known"] for obs in v.get("observations", [])))]
        if not incomplete or not all(any(
                len({obs["preprocessing"] for obs in other.get("observations", [])
                     if obs["spacing_known"] and obs.get("usable", True)}) >= 2
                for other in originals if other["text"] in v.get("contained_by", [])) for v in incomplete):
            continue
        invalid_obs = {id(obs) for v in incomplete for obs in v.get("observations", [])
                       if issue == "single_cjk_glyph" or not obs["spacing_known"]}
        for variant in variants:
            observed = variant.get("observations", [])
            if observed and all(id(obs) in invalid_obs for obs in observed):
                variant["queryable"] = False
                variant["manual_only"] = True
                if "contained_name_fragment" not in variant["issue_codes"]:
                    variant["issue_codes"].append("contained_name_fragment")
        row["issue_codes"].remove(issue)


def _attach_independent_names(rows, observations):
    if not isinstance(observations, list) or len(observations) > len(rows):
        return
    by_slot = defaultdict(list)
    for obs in observations:
        if isinstance(obs, dict) and obs.get("team") in _TEAMS and type(obs.get("index")) is int:
            by_slot[(obs["team"], obs["index"])].append(obs)
    for row in rows:
        matches = by_slot[row["team"], row["index"]]
        if len(matches) != 1:
            continue
        obs = matches[0]
        name = obs.get("text")
        confidence = obs.get("confidence")
        if (not isinstance(name, str) or not 2 <= len(name.strip()) <= 128 or
                type(confidence) not in (int, float) or not .90 <= confidence <= 1 or obs.get("clipped") is not False):
            continue
        name = unicodedata.normalize("NFC", name.strip())
        if (any(unicodedata.category(c) in {"Cf", "Cs", "Cc"} for c in name) or
                _visibly_truncated(name) or
                "".join(name.casefold().split()) == "playername" or _UI_VALUE.fullmatch(name)):
            continue
        variants = row["variants"]
        match = next((v for v in variants if v["text"] == name), None)
        blockers = _BLOCKING.intersection(row["issue_codes"])
        # The auxiliary model is trained primarily on Chinese and English.
        # Its confidence cannot displace a competing Russian reading observed
        # by the native Russian recognizer in multiple preprocessing views.
        russian_conflict = False
        for variant in variants:
            letters = [c for c in variant["text"] if c.isalpha()]
            cyrillic = sum("CYRILLIC" in unicodedata.name(c, "") for c in letters)
            russian_views = {o["preprocessing"] for o in variant.get("observations", [])
                             if str(o["language"]).split("-")[0].lower() == "ru" and o.get("usable", True)}
            if (variant["queryable"] and variant["text"] != name and cyrillic >= 2 and
                    cyrillic * 2 >= len(letters) and len(russian_views) >= 2):
                russian_conflict = True
                break
        if russian_conflict:
            continue
        # Long digit-only handles must have been read in full in distinct
        # WinRT views in the calibrated name box as well as the independent model.
        if blockers == {"numeric_name"} and re.fullmatch(r"(?:[0-9]{6,20}|0[0-9]{2,4})", name) and match:
            if (match["bbox"][0] <= row["bbox"][0] + row["bbox"][2] * .08 and
                    len({o["preprocessing"] for o in match.get("observations", [])}) >= 2 and
                    all(v["text"] == name or "supplemental_unusable" in v["issue_codes"] or
                        (" " in v["text"] and _numeric(v["text"]) and
                         len(v["text"].replace(" ", "")) < len(name)) for v in variants)):
                row["issue_codes"].remove("numeric_name")
                match["issue_codes"].remove("numeric_name")
                match.update(queryable=True, manual_only=False)
                blockers = set()
        if blockers or not any(v["queryable"] for v in variants):
            continue
        if match and match["queryable"]:
            # The same whole spelling from two different recognizers is usable
            # on the first frame. Alternate single-engine readings are still
            # retained for manual inspection. BATrace must uniquely match it.
            row["corroborated_name"] = name
        elif not match:
            # A new spelling needs the next frame. The resolver checks rivals
            # as well and cannot publish a WinRT-only lookalike as this player.
            match = _variant(row, name, "PP-OCR", row["bbox"], "independent_crop", name)
            if not match["queryable"]:
                continue
        else:
            continue
        row["independent_name"] = name


def extract_prep_roster(ocr_results, regions, *, image_size=None):
    """Return ten calibrated rows and exact-name candidates, without any I/O.

    ``ocr_results`` is ScreenOCR's result dict (or its ``results`` list when
    explicit ``image_size=(width,height)`` is supplied). Each language contains
    ``lines`` with ``words[{text,x,y,width,height}]`` in original pixels.
    ``regions`` has Alpha/Bravo normalized [x,y,width,height] blocks, each split
    into five rows, or per-team ``{'rows': [five normalized rectangles]}``.

    Raises ValueError for absent/invalid calibration or image dimensions.
    A row can be queryable while other rows are absent. Variant disagreement
    needs exact-name resolution. Digit-only names need strong crop evidence.
    CJK spacing variants preserve both originals and compact alternatives.
    ``complete`` means every row has some candidate, not verified identities.
    """
    frame = ocr_results if isinstance(ocr_results, Mapping) else {}
    if image_size is None:
        image_size = (frame.get("image_width"), frame.get("image_height"))
    if not isinstance(image_size, (tuple, list)) or len(image_size) != 2 or not all(_number(v) and v > 0 for v in image_size):
        raise ValueError("请提供原始图像的有效宽高")
    rows = _regions(regions, image_size)
    try:
        crops = _calibrated_crops(frame, rows, image_size)
    except ValueError:
        # Declared but inconsistent crop provenance must not silently fall back
        # to some other row that happens to contain the mapped coordinates.
        crops = {}
        for row in rows:
            _issue(row, "crop_metadata_mismatch")
    results = frame.get("results", []) if isinstance(ocr_results, Mapping) else ocr_results
    if not isinstance(results, (list, tuple)):
        raise ValueError("OCR results 必须是语言结果列表")
    assigned = defaultdict(list)
    malformed = False
    for result_index, result in enumerate(results):
        if not isinstance(result, Mapping) or not isinstance(result.get("lines"), (list, tuple)):
            malformed = True
            continue
        language = str(result.get("language") or "unknown")
        for line_index, line in enumerate(result["lines"]):
            if not isinstance(line, Mapping) or not isinstance(line.get("words"), (list, tuple)):
                malformed = True
                continue
            touched = defaultdict(set)
            for item in line["words"]:
                if not isinstance(item, Mapping) or not isinstance(item.get("text"), str) or not item["text"].strip():
                    malformed = True
                    continue
                try:
                    bbox = _box(item)
                    if not _contains([0, 0, *image_size], bbox):
                        raise ValueError("文字坐标超出图像")
                except ValueError:
                    malformed = True
                    continue
                if crops is not None:
                    roi_index = item.get("roi_index")
                    line_roi = line.get("roi_index")
                    preprocessing = result.get("preprocessing")
                    crop = crops.get((roi_index, preprocessing)) if type(roi_index) is int and isinstance(preprocessing, str) else None
                    if type(line_roi) is not int or line_roi != roi_index or crop is None:
                        malformed = True
                        for row_index, row in enumerate(rows):
                            if _intersection(row["bbox"], bbox) > 1e-6 or (crop is not None and row_index == crop[0]):
                                _issue(row, "crop_metadata_mismatch")
                        continue
                    row_index, source_bbox = crop
                    row = rows[row_index]
                    other_rows = [other for other_index, other in enumerate(rows)
                                  if other_index != row_index and _intersection(other["bbox"], bbox) > 1e-6]
                    if not _contains(source_bbox, bbox) or other_rows:
                        _issue(row, "boundary_crossing")
                        for other in other_rows:
                            _issue(other, "boundary_crossing")
                        continue
                    assigned[(row_index, result_index, language)].append({
                        "text": item["text"].strip(), "bbox": bbox, "line_id": line_index,
                        "line_text": line.get("text") if isinstance(line.get("text"), str) else ""})
                    touched[row["team"]].add(row_index)
                    continue
                for row_index, row in enumerate(rows):
                    if _contains(row["bbox"], bbox):
                        assigned[(row_index, result_index, language)].append({
                            "text": item["text"].strip(), "bbox": bbox, "line_id": line_index,
                            "line_text": line.get("text") if isinstance(line.get("text"), str) else ""})
                        touched[row["team"]].add(row_index)
                    elif _intersection(row["bbox"], bbox) > 1e-6:
                        _issue(row, "boundary_crossing")
            for indices in touched.values():
                # Independently calibrated horizontal cards can legitimately
                # share one OCR line. Only vertically separate row bands imply
                # a line segmentation conflict; indices are stable labels.
                boxes = [rows[index]["bbox"] for index in indices]
                vertical_conflict = len(boxes) > 1 and max(b[1] for b in boxes) >= min(b[1] + b[3] for b in boxes)
                if vertical_conflict:
                    for row_index in indices:
                        _issue(rows[row_index], "ocr_line_crosses_rows")
    # Keep the original colour/140 passes authoritative for their existing
    # eligibility checks. A supplemental view cannot poison a valid base name
    # merely because that extra pass found only a numeric/spacing fragment.
    for (row_index, result_index, language), words in sorted(
            assigned.items(), key=lambda item: results[item[0][1]].get("preprocessing") in _SUPPLEMENTAL):
        row = rows[row_index]
        prior_issues = set(row["issue_codes"])
        previous_variants = {variant["text"] for variant in row["variants"]}
        primary_usable = (bool(crops) and results[result_index].get("preprocessing") in _SUPPLEMENTAL
                          and not _BLOCKING.intersection(prior_issues)
                          and any(variant["queryable"] and any(
                              observation["preprocessing"] not in _SUPPLEMENTAL for observation in variant.get("observations", []))
                                  for variant in row["variants"]))
        pass_observations = []
        if crops:
            words = _without_detached_numeric_texture(
                words, [((other_language, results[other_result].get("preprocessing")), other)
                        for (other_row, other_result, other_language), other in assigned.items()
                        if other_row == row_index], row)
        for index, first in enumerate(words):
            for second in words[index + 1:]:
                a, b = first["bbox"], second["bbox"]
                if _intersection(a, b) > .15 * min(a[2] * a[3], b[2] * b[3]):
                    _issue(row, "overlapping_words")
        name_clusters = 0
        for cluster in _clusters(words):
            text, spacing_known = _cluster_text(cluster)
            bbox = _union([word["bbox"] for word in cluster])
            if _UI_VALUE.fullmatch(text):
                row["ignored"].append({"text": text, "language": language, "bbox": bbox, "reason": "界面格式数值；不作为自动姓名候选"})
                continue
            name_clusters += 1
            if not spacing_known:
                _issue(row, "spacing_uncertain")
            observation = {"language": language, "preprocessing": results[result_index].get("preprocessing"),
                           "text": text, "words": cluster, "spacing_known": spacing_known}
            pass_observations.append(observation)
            _variant(row, text, language, bbox, "none", text).setdefault("observations", []).append(observation)
            compact = _CJK_GAP.sub("", text)
            if compact != text:
                _variant(row, compact, language, bbox, "cjk_adjacent_whitespace_removed", text).setdefault("observations", []).append(observation)
            if crops:
                compact_dot = _han_dot_candidate(cluster) if spacing_known else None
                if compact_dot and compact_dot != text:
                    _variant(row, compact_dot, language, bbox, "cjk_dot_spacing", text).setdefault("observations", []).append(observation)
                touching = _touching_cjk_spacing_candidate(cluster)
                if touching is not None:
                    _variant(row, touching, language, bbox, "geometry_adjacent_whitespace_removed", text).setdefault("observations", []).append(observation)
        if name_clusters > 1:
            _issue(row, "multiple_text_lines")
        added = set(row["issue_codes"]) - prior_issues
        soft_failures = {"numeric_name", "single_cjk_glyph", "spacing_uncertain", "multiple_text_lines"}
        if primary_usable and added & soft_failures and not (added & (_BLOCKING - soft_failures)):
            for observation in pass_observations:
                observation["usable"] = False
            for variant in row["variants"]:
                if variant["text"] not in previous_variants:
                    variant["queryable"] = False
                    variant["manual_only"] = True
                    variant["issue_codes"].append("supplemental_unusable")
            row["issue_codes"] = [issue for issue in row["issue_codes"] if issue not in added & soft_failures]
            _issue(row, "supplemental_unusable")
        elif not added & _BLOCKING:
            for variant in row["variants"]:
                if (variant["issue_codes"] == ["supplemental_unusable"] and
                        any(observation is current for observation in variant.get("observations", [])
                            for current in pass_observations)):
                    variant["issue_codes"].clear()
                    variant["queryable"] = True
                    variant["manual_only"] = False
    if crops:
        for row in rows:
            _suppress_contained_fragments(row)
            _recover_corroborated_crop(row)
            _prune_unsupported_observations(row)
        _attach_independent_names(rows, frame.get("auxiliary_names"))
    # Never resolve identical names to distinct player positions automatically.
    owners = defaultdict(set)
    for row_index, row in enumerate(rows):
        for variant in row["variants"]:
            # A shared suffix/fragment is not a second row's complete name.
            # Other unsafe candidates retain the existing conservative checks.
            if {"contained_name_fragment", "script_projection_mismatch", "ocr_inserted_spacing", "supplemental_unusable"}.intersection(variant["issue_codes"]):
                continue
            key = " ".join(unicodedata.normalize("NFKC", variant["text"]).casefold().split())
            owners[key].add(row_index)
    for indices in owners.values():
        if len(indices) > 1:
            for row_index in indices:
                _issue(rows[row_index], "duplicate_name")
    for row in rows:
        if not row["variants"]:
            _issue(row, "no_name")
        if len(row["variants"]) > 1:
            _issue(row, "multiple_variants")
        blocked = bool(_BLOCKING.intersection(row["issue_codes"]))
        for variant in row["variants"]:
            variant["languages"].sort()
            variant["original_texts"].sort()
            variant["queryable"] = variant["queryable"] and not blocked
            variant["manual_only"] = not variant["queryable"]
        row["variants"].sort(key=lambda value: (value["normalization"] != "none", value["text"]))
        usable = [v for v in row["variants"] if v["queryable"]]
        row["queryable"] = row["valid"] = bool(usable)
        row["auto_confirmable"] = bool(usable) and len(row["variants"]) == 1
        row["name"] = usable[0]["text"] if row["auto_confirmable"] else None
        row["issues"] = [_MESSAGES[code] for code in row["issue_codes"]]
        row['is_placeholder'] = 'placeholder_name' in row['issue_codes']
    codes = list(dict.fromkeys(code for row in rows for code in row["issue_codes"]))
    if malformed:
        codes.append("malformed_ocr")
    return {"schema": 1, "image_size": list(image_size), "rows": rows,
            "placeholder_counts": {team: sum(r['is_placeholder'] for r in rows if r['team'] == team) for team in _TEAMS},
            "teams": {team: [row for row in rows if row["team"] == team] for team in _TEAMS},
            "geometry_valid": not any("overlapping_regions" in row["issue_codes"] for row in rows),
            "row_geometry_complete": not any(_GEOMETRY.intersection(row["issue_codes"]) for row in rows),
            "complete": all(bool(row["variants"]) for row in rows),
            "auto_confirmable": not malformed and all(row["auto_confirmable"] for row in rows),
            "queryable_count": sum(row["queryable"] for row in rows),
            "issue_codes": codes, "issues": [_MESSAGES[code] for code in codes],
            "notes": ["名单区域由用户校准；Alpha/Bravo 标签不证明敌我身份。",
                      "queryable 仅表示候选结构可用于精确名称消歧，不证明玩家身份或本场阵容。",
                      "需要调用方核实当前游戏窗口、准备阶段及跨帧稳定性；本模块不判断画面场景。"]}
