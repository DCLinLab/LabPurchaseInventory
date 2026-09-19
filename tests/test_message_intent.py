import unittest

from message_intent import is_shortage_report


class IntentTests(unittest.TestCase):
    def test_real_channel_shortages(self):
        for caption in ['Midiprep P2 runs out.', 'Running out of OCT.', 'Out of 50 ml falcon tubes',
                        '<@U123> only one bottle of trypsin left', 'We need more LB powder',
                        '200ul tips are running low', 'Think we have very few 1.5 mL microcentrifuge tubes.',
                        'GluMAx and P/S will run out', 'I used all of them to make some new LB',
                        'Can we buy more sprayer from Amazon?', 'Bottle is empty', 'No tubes left']:
            with self.subTest(caption=caption): self.assertTrue(is_shortage_report(caption))

    def test_shortage_with_negative_future_or_question_arrival_stays_shortage(self):
        for caption in ["Running out, haven't received the replacement", 'Running low; has it arrived?',
                        'Out of tubes; replacement will arrive tomorrow', 'Out of tubes; not delivered yet',
                        'Out of tubes; waiting for the order to arrive']:
            with self.subTest(caption=caption): self.assertTrue(is_shortage_report(caption))

    def test_actual_deliveries_remain_eligible(self):
        for caption in ['', 'Put it in 365', '15mL tubes arrive', 'New methanol',
                        'We ran out; replacement arrived', 'Received tubes because we were running low']:
            with self.subTest(caption=caption): self.assertFalse(is_shortage_report(caption))
