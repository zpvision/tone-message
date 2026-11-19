"""Regression checks with the real tokenizer only; no inference or weights."""
import unittest
import unicodedata

from app.context_budget import ContextBudget
from app.config import Settings


class ContextTests(unittest.TestCase):
    def setUp(self):
        self.counter = ContextBudget(Settings().context_tokenizer_dir)
        self.params = {'thinking': False, 'num_predict': 128, 'num_ctx': 4096}

    def test_long_russian_is_not_utf8_bytes(self):
        system = 'Определи тональность сообщения клиента. ' * 70
        self.assertGreater(len(system.encode()), 4096)
        budget = self.counter.count(system, 'Спасибо за отчёт! 😀', self.params)
        self.assertGreater(budget['remaining_tokens'], 0)
        self.assertLess(budget['input_tokens'], 2000)

    def test_exact_boundary_and_no_truncation(self):
        system, user = 'Верни JSON {"label":"neutral"}', 'Строка ' * 5000
        original = self.counter.count(system, user, self.params)
        self.assertLess(original['remaining_tokens'], 0)
        exact = {**self.params, 'num_ctx': original['required_tokens']}
        self.assertEqual(self.counter.count(system, user, exact)['remaining_tokens'], 0)
        exact['num_ctx'] -= 1
        self.assertEqual(self.counter.count(system, user, exact)['remaining_tokens'], -1)
        self.assertTrue(self.counter.render(system, user, False).count(user) == 1)

    def test_thinking_and_literal_braces(self):
        system, user = '{"label":"…"}\n', '{скобки}\n{text} 😀'
        off = self.counter.render(system, user, False)
        on = self.counter.render(system, user, True)
        self.assertIn(system, off)
        self.assertIn(user + ' /no_think', off)
        self.assertIn(user + ' /think', on)
        self.assertTrue(off.endswith('<think>\n\n</think>\n\n'))
        self.assertTrue(on.endswith('<|im_start|>assistant\n'))
        self.assertNotEqual(self.counter.count(system, user, self.params)['input_tokens'],
                            self.counter.count(system, user, {**self.params, 'thinking': True})['input_tokens'])

    def test_other_model_fails_closed(self):
        with self.assertRaisesRegex(ValueError, 'model_not_validated'):
            self.counter.metadata({'digest': 'different-model'})

    def test_decomposed_unicode_is_counted_without_normalizing_input(self):
        system, text = 'Верни JSON.', 'Пожалуи\u0306ста, пришлите отче\u0308т.'
        composed = unicodedata.normalize('NFC', text)
        self.assertNotEqual(text, composed)
        budget = self.counter.count(system, text, self.params)
        self.assertIsNone(self.counter.tokenizer.normalizer)
        self.assertNotEqual(budget['input_tokens'], self.counter.count(system, composed, self.params)['input_tokens'])
        rendered = self.counter.render(system, text, False)
        encoded = self.counter.tokenizer.encode(rendered, add_special_tokens=False)
        self.assertEqual(self.counter.tokenizer.decode(encoded.ids, skip_special_tokens=False), rendered)
        tight = {**self.params, 'num_ctx': budget['required_tokens'] - 1}
        self.assertEqual(self.counter.count(system, text, tight)['remaining_tokens'], -1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
