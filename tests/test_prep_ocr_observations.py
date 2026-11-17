"""Names must remain observed; only measured crop artifacts are filtered."""
from copy import deepcopy
import unittest

from ba_scout.prep_roster import extract_prep_roster
from tests.test_prep_roster import calibrated_frame, word


def numeric_texture(*, same_line=False, near=False, duplicate_peers=False):
    value, regions = calibrated_frame()
    target = value['results'][0]['lines'][5]
    source = value['region_layout'][5]['source_bbox']
    target['text'] = 'ABCDEF'
    target['words'] = [dict(word('ABCDEF', source[0] + 1, source[1] + 3, 60, 13), roi_index=5)]
    numeric = dict(word('4', source[0] + (68 if near else source[2] - 9), source[1] + 3, 8, 13), roi_index=5)
    for language in ('ru', 'ru' if duplicate_peers else 'zh-Hans-CN'):
        value['results'].append({'language': language, 'preprocessing': 'colour_3x',
                                 'lines': [deepcopy(target)]})
    if same_line:
        target['text'] = 'ABCDEF 4'
        target['words'].append(numeric)
    else:
        value['results'][0]['lines'].append({'text': '4', 'roi_index': 5, 'words': [numeric]})
    return value, regions


def mixed_name(gap=.5, *, split_lines=False, text='-SaMpLeX- 示 例'):
    value, regions = calibrated_frame()
    target = value['results'][0]['lines'][5]
    source = value['region_layout'][5]['source_bbox']
    x, y = source[0] + 1, source[1] + 3
    items = [dict(word('-SaMpLeX-', x, y, 87, 13), roi_index=5),
             dict(word('示', x + 87 + gap, y, 13, 13), roi_index=5),
             dict(word('例', x + 100 + gap, y, 13, 13), roi_index=5)]
    target.update(text=text, words=items)
    if split_lines:
        target.update(text='-SaMpLeX-', words=items[:1])
        value['results'][0]['lines'].append({'text': '示 例', 'roi_index': 5, 'words': items[1:]})
    return value, regions


def script_observations(*, views=2, competing='-SaMpLeX-$J*', second_nonhan=False):
    value, regions = mixed_name()
    value['results'][0]['language'] = 'zh-Hans-CN'
    target = value['results'][0]['lines'][5]
    if views == 2:
        for tile in deepcopy(value['region_layout']):
            tile['preprocessing'] = 'light_text_120_3x'
            value['region_layout'].append(tile)
        value['results'].append({'language': 'zh-Hans-CN', 'preprocessing': 'light_text_120_3x',
                                 'lines': [deepcopy(target)]})
    x, y = target['words'][0]['x'], target['words'][0]['y']
    for language in ('en-GB', 'ru') if second_nonhan else ('en-GB',):
        value['results'].append({'language': language, 'preprocessing': 'colour_3x',
                                'lines': [{'text': competing, 'roi_index': 5,
                                           'words': [dict(word(competing, x, y, 114, 13), roi_index=5)]}]})
    return value, regions


def supplemental_observation(text='11'):
    value, regions = calibrated_frame()
    for tile in deepcopy(value['region_layout']):
        tile['preprocessing'] = 'light_text_120_3x'
        value['region_layout'].append(tile)
    target = deepcopy(value['results'][0]['lines'][5])
    target['text'] = target['words'][0]['text'] = text
    value['results'].append({'language': 'en-GB', 'preprocessing': 'light_text_120_3x', 'lines': [target]})
    return value, regions


class CropObservationTests(unittest.TestCase):
    def parse(self, pair):
        return extract_prep_roster(*pair)['rows'][5]

    def test_remote_numeric_line_with_two_independent_name_observations_is_recorded_as_noise(self):
        row = self.parse(numeric_texture())
        self.assertTrue(row['queryable'])
        self.assertEqual([v['text'] for v in row['variants']], ['ABCDEF'])
        self.assertIn('detached_numeric_texture', row['issue_codes'])
        self.assertEqual(row['ignored'][0]['text'], '4')
        self.assertEqual(row['ignored'][0]['corroborating_results'], 2)

    def test_same_ocr_line_numeric_suffix_is_never_removed(self):
        row = self.parse(numeric_texture(same_line=True))
        self.assertNotIn('detached_numeric_texture', row['issue_codes'])
        self.assertIn('ABCDEF 4', [v['text'] for v in row['variants']])
        self.assertFalse(next(v for v in row['variants'] if v['text'] == 'ABCDEF')['queryable'])

    def test_nearby_detached_number_remains_uncertain(self):
        row = self.parse(numeric_texture(near=True))
        self.assertFalse(row['queryable'])
        self.assertIn('spacing_uncertain', row['issue_codes'])

    def test_duplicate_language_preprocessing_does_not_count_as_independent_support(self):
        row = self.parse(numeric_texture(duplicate_peers=True))
        self.assertFalse(row['queryable'])
        self.assertNotIn('detached_numeric_texture', row['issue_codes'])

    def test_one_other_observation_is_insufficient(self):
        value, regions = numeric_texture()
        value['results'].pop()
        self.assertFalse(self.parse((value, regions))['queryable'])

    def test_unverified_crop_cannot_apply_numeric_texture_exception(self):
        value, regions = numeric_texture()
        value.pop('ocr_method')
        row = self.parse((value, regions))
        self.assertFalse(row['queryable'])
        self.assertNotIn('detached_numeric_texture', row['issue_codes'])

    def test_nonnumeric_remote_text_is_never_dropped(self):
        value, regions = numeric_texture()
        noise = value['results'][0]['lines'][-1]
        noise['text'] = noise['words'][0]['text'] = 'A'
        self.assertFalse(self.parse((value, regions))['queryable'])

    def test_boundary_violation_still_blocks_a_corroborated_name(self):
        value, regions = numeric_texture()
        value['results'][1]['lines'][0]['words'][0]['x'] -= 10
        row = self.parse((value, regions))
        self.assertFalse(row['queryable'])
        self.assertIn('boundary_crossing', row['issue_codes'])

    def test_touching_mixed_script_words_keep_all_spacing_candidates(self):
        row = self.parse(mixed_name())
        variants = {v['text']: v for v in row['variants']}
        self.assertTrue(row['queryable'])
        self.assertEqual(set(variants), {'-SaMpLeX- 示 例', '-SaMpLeX- 示例', '-SaMpLeX-示例'})
        self.assertTrue(variants['-SaMpLeX-示例']['queryable'])
        self.assertTrue(all(variants[name]['manual_only'] for name in ('-SaMpLeX- 示 例', '-SaMpLeX- 示例')))
        self.assertTrue(all('ocr_inserted_spacing' in variants[name]['issue_codes'] for name in ('-SaMpLeX- 示 例', '-SaMpLeX- 示例')))
        self.assertEqual(variants['-SaMpLeX-示例']['normalization'], 'geometry_adjacent_whitespace_removed')
        self.assertFalse(row['auto_confirmable'])

    def test_visible_gap_does_not_generate_a_joined_mixed_name(self):
        row = self.parse(mixed_name(gap=5))
        self.assertNotIn('-SaMpLeX-示例', [v['text'] for v in row['variants']])

    def test_cross_line_spacing_does_not_generate_a_joined_mixed_name(self):
        row = self.parse(mixed_name(split_lines=True))
        self.assertFalse(row['queryable'])
        self.assertNotIn('-SaMpLeX-示例', [v['text'] for v in row['variants']])

    def test_already_joined_original_does_not_add_spaces(self):
        row = self.parse(mixed_name(text='-SaMpLeX-示例'))
        self.assertEqual([v['text'] for v in row['variants']], ['-SaMpLeX-示例'])

    def test_missing_crop_provenance_does_not_generate_a_mixed_spacing_alternative(self):
        value, regions = mixed_name()
        value.pop('ocr_method')
        row = self.parse((value, regions))
        self.assertNotIn('-SaMpLeX-示例', [v['text'] for v in row['variants']])

    def test_declared_new_threshold_uses_same_strict_source_geometry(self):
        value, regions = mixed_name()
        for tile in value['region_layout']:
            tile['preprocessing'] = 'light_text_120_3x'
        value['results'][0]['preprocessing'] = 'light_text_120_3x'
        self.assertTrue(self.parse((value, regions))['queryable'])
        value['region_layout'][5]['source_bbox'][2] += 1
        row = self.parse((value, regions))
        self.assertFalse(row['queryable'])
        self.assertIn('crop_metadata_mismatch', row['issue_codes'])

    def test_corroborated_han_span_rejects_one_wrong_language_projection_only(self):
        row = self.parse(script_observations())
        variants = {v['text']: v for v in row['variants']}
        self.assertTrue(variants['-SaMpLeX-示例']['queryable'])
        self.assertFalse(variants['-SaMpLeX-$J*']['queryable'])
        self.assertIn('script_projection_mismatch', variants['-SaMpLeX-$J*']['issue_codes'])
        self.assertTrue(variants['-SaMpLeX-$J*']['observations'])

    def test_one_han_view_is_not_script_confirmation(self):
        row = self.parse(script_observations(views=1))
        self.assertTrue(next(v for v in row['variants'] if v['text'] == '-SaMpLeX-$J*')['queryable'])

    def test_corroborated_latin_competitor_is_not_discarded(self):
        row = self.parse(script_observations(second_nonhan=True))
        self.assertTrue(next(v for v in row['variants'] if v['text'] == '-SaMpLeX-$J*')['queryable'])

    def test_alternate_outside_prefix_punctuation_is_not_discarded(self):
        row = self.parse(script_observations(competing='--SaMpLeX-$J*'))
        self.assertTrue(next(v for v in row['variants'] if v['text'] == '--SaMpLeX-$J*')['queryable'])

    def test_punctuation_extending_beyond_corroborated_name_is_not_discarded(self):
        value, regions = script_observations(competing='-SaMpLeX-$J*!')
        value['results'][-1]['lines'][0]['words'][0]['width'] += 4
        row = self.parse((value, regions))
        self.assertTrue(next(v for v in row['variants'] if v['text'] == '-SaMpLeX-$J*!')['queryable'])

    def test_single_word_starts_left_of_corroborated_name_is_not_discarded(self):
        value, regions = script_observations(competing='-SaMpLeX-$J*')
        for result in value['results'][:-1]:
            for line in result['lines']:
                if line.get('roi_index') == 5:
                    for item in line['words']:
                        item['x'] += 4
        row = self.parse((value, regions))
        self.assertTrue(next(v for v in row['variants'] if v['text'] == '-SaMpLeX-$J*')['queryable'])

    def test_different_real_han_characters_remain_candidates(self):
        row = self.parse(script_observations(competing='-SaMpLeX-示本'))
        self.assertTrue(next(v for v in row['variants'] if v['text'] == '-SaMpLeX-示本')['queryable'])

    def test_shifted_cjk_span_does_not_remove_a_name_elsewhere(self):
        value, regions = script_observations()
        for result in value['results']:
            if result['language'] != 'zh-Hans-CN':
                continue
            for line in result['lines']:
                if line.get('roi_index') == 5:
                    for item in line['words'][1:]:
                        item['x'] += 20
        row = self.parse((value, regions))
        self.assertTrue(next(v for v in row['variants'] if v['text'] == '-SaMpLeX-$J*')['queryable'])

    def test_spacing_inside_one_ocr_word_has_no_removable_geometry(self):
        value, regions = mixed_name()
        target = value['results'][0]['lines'][5]
        target['words'] = [dict(word('-SaMpLeX- 示 例', target['words'][0]['x'], target['words'][0]['y'], 114, 13), roi_index=5)]
        row = self.parse((value, regions))
        self.assertTrue(next(v for v in row['variants'] if v['text'] == '-SaMpLeX- 示 例')['queryable'])

    def test_single_han_guess_cannot_filter_latin_name(self):
        value, regions = script_observations()
        for result in value['results']:
            if result['language'] != 'zh-Hans-CN':
                continue
            for line in result['lines']:
                if line.get('roi_index') == 5:
                    line['words'] = line['words'][:2]
                    line['text'] = '-SaMpLeX- 示'
        row = self.parse((value, regions))
        self.assertTrue(next(v for v in row['variants'] if v['text'] == '-SaMpLeX-$J*')['queryable'])

    def test_unusable_numeric_supplement_does_not_poison_valid_original_name(self):
        row = self.parse(supplemental_observation())
        variants = {v['text']: v for v in row['variants']}
        self.assertTrue(variants['Bravo0']['queryable'])
        self.assertFalse(variants['11']['queryable'])
        self.assertIn('supplemental_unusable', variants['11']['issue_codes'])
        self.assertNotIn('numeric_name', row['issue_codes'])

    def test_without_a_valid_original_numeric_supplement_still_blocks(self):
        value, regions = supplemental_observation()
        value['results'][0]['lines'].pop(5)
        row = self.parse((value, regions))
        self.assertFalse(row['queryable'])
        self.assertIn('numeric_name', row['issue_codes'])

    def test_supplemental_ellipsis_still_blocks_entire_row(self):
        row = self.parse(supplemental_observation('Bravo0...'))
        self.assertFalse(row['queryable'])
        self.assertIn('truncated_name', row['issue_codes'])

    def test_supplemental_boundary_still_blocks_entire_row(self):
        value, regions = supplemental_observation()
        value['results'][-1]['lines'][0]['words'][0]['x'] -= 3
        row = self.parse((value, regions))
        self.assertFalse(row['queryable'])
        self.assertIn('boundary_crossing', row['issue_codes'])

    def test_valid_competing_supplement_remains_a_required_name(self):
        row = self.parse(supplemental_observation('Bravo0-'))
        self.assertEqual({v['text'] for v in row['variants'] if v['queryable']}, {'Bravo0', 'Bravo0-'})

    def test_split_supplement_cannot_suppress_original_as_a_fragment(self):
        value, regions = supplemental_observation('Bravo0')
        target = value['results'][-1]['lines'][0]
        first = target['words'][0]
        extra = dict(word('Extra', first['x'] + first['width'], first['y'], 35, first['height']), roi_index=5)
        value['results'][-1]['lines'].append({'text': 'Extra', 'roi_index': 5, 'words': [extra]})
        row = self.parse((value, regions))
        variants = {v['text']: v for v in row['variants']}
        self.assertTrue(variants['Bravo0']['queryable'])
        self.assertFalse(variants['Bravo0 Extra']['queryable'])

    def test_later_complete_observation_can_restore_a_supplemental_candidate(self):
        value, regions = supplemental_observation('Bravo0')
        target = value['results'][-1]['lines'][0]
        first = target['words'][0]
        extra = dict(word('Extra', first['x'] + first['width'], first['y'], 35, first['height']), roi_index=5)
        value['results'][-1]['lines'].append({'text': 'Extra', 'roi_index': 5, 'words': [extra]})
        value['results'].append({'language': 'ru', 'preprocessing': 'light_text_120_3x',
                                'lines': [{'text': 'Bravo0 Extra', 'roi_index': 5, 'words': [first, extra]}]})
        row = self.parse((value, regions))
        self.assertTrue(next(v for v in row['variants'] if v['text'] == 'Bravo0 Extra')['queryable'])


if __name__ == '__main__':
    unittest.main()
