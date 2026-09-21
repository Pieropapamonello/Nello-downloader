import tempfile
import unittest
from unittest.mock import patch

from voice_proofreading import apply_corrections, proofread


def match(text, original, replacements, kind='misspelling', rule='MORFOLOGIK_RULE_IT_IT'):
    start = text.index(original)
    return {'offset': len(text[:start].encode('utf-16-le')) // 2,
            'length': len(original.encode('utf-16-le')) // 2,
            'replacements': [{'value': value} for value in replacements],
            'rule': {'id': rule, 'issueType': kind}}


class ProofreadingTests(unittest.TestCase):
    def test_italian_grammar_and_typo_with_emoji_offsets(self):
        text = "🙂 Un'altro giorno devo fare la pedicur."
        changes = [match(text, "Un'altro", ['Un altro'], 'uncategorized', 'GR_04_001'),
                   match(text, 'pedicur', ['pedicure'])]
        self.assertEqual(apply_corrections(text, changes),
                         ('🙂 Un altro giorno devo fare la pedicure.', 2))

    def test_ambiguous_words_names_numbers_negations_and_diminutives_stay(self):
        text = 'Vito vede una piazzettina alle 9.30 e non deve venerla prendere.'
        changes = [match(text, 'Vito', ['Vita']), match(text, 'piazzettina', ['piazzetta']),
                   match(text, '9.30', ['9.00'], 'grammar'),
                   match(text, 'non deve', ['deve'], 'grammar'),
                   match(text, 'venerla', ['vederla', 'venirla'])]
        self.assertEqual(apply_corrections(text, changes), (text, 0))

    def test_memory_or_missing_checker_keeps_successful_transcript(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch('voice_proofreading.Path.is_file', return_value=False), \
                 patch('voice_proofreading.subprocess.Popen') as process:
                self.assertEqual(proofread('Ciao.', directory), 'Ciao.')
                process.assert_not_called()
            with patch('voice_proofreading.Path.is_file', return_value=True), \
                 patch('voice_proofreading.memory_pressure', return_value=True), \
                 patch('voice_proofreading.subprocess.Popen') as process:
                self.assertEqual(proofread('Ciao.', directory), 'Ciao.')
                process.assert_not_called()


if __name__ == '__main__':
    unittest.main()
