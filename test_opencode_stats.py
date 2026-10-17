"""opencode_stats.py の計測ロジックに対する unittest。外部通信なし。"""
import unittest

from opencode_stats import (
    calc_speed, calc_hit, find_completed, message_to_sample, model_key,
    Aggregation, fmt_speed, Service,
)


def message(mid, created=1000, streamed=None, completed=None, output=100,
            reasoning=20, model='M', read=0, write=0, input=500):
    tokens = None
    if output is not None:
        tokens = {'output': output, 'reasoning': reasoning, 'input': input,
                  'cache': {'read': read, 'write': write}}
    time_field = {'created': created, 'completed': completed}
    if streamed is not None:
        time_field['streamed'] = streamed
    return {'id': mid, 'type': 'assistant', 'model': model,
            'tokens': tokens, 'time': time_field}


class CalcSpeedTests(unittest.TestCase):
    def test_prefers_streamed(self):
        speed, seconds, method = calc_speed({'output': 100, 'reasoning': 20},
                                            1000, 4000, 9000)
        self.assertEqual(method, 'measured')
        self.assertAlmostEqual(seconds, 3.0)
        self.assertAlmostEqual(speed, 40.0)

    def test_fallback_to_completed_without_streamed(self):
        speed, seconds, method = calc_speed({'output': 80, 'reasoning': 0},
                                            1000, None, 5000)
        self.assertEqual(method, 'fallback')
        self.assertAlmostEqual(seconds, 4.0)
        self.assertAlmostEqual(speed, 20.0)

    def test_rejects_missing_or_invalid(self):
        self.assertEqual(calc_speed(None, 1000, 2000, 3000), (None, None, None))
        self.assertEqual(calc_speed({'output': 0, 'reasoning': 0},
                                    1000, 2000, 3000), (None, None, None))
        self.assertEqual(calc_speed({'output': 8}, None, 2000, 3000),
                         (None, None, None))
        self.assertEqual(calc_speed({'output': 8}, 2000, 1000, None),
                         (None, None, None))
        self.assertEqual(calc_speed({'output': 8}, 1000, None, None),
                         (None, None, None))

    def test_rejects_out_of_range_duration(self):
        self.assertEqual(calc_speed({'output': 8}, 1000, 1040, None),
                         (None, None, None))
        self.assertEqual(calc_speed({'output': 8}, 0, 3_000_000, None),
                         (None, None, None))

    def test_hit_rate(self):
        self.assertAlmostEqual(
            calc_hit({'input': 500, 'cache': {'read': 750, 'write': 0}}), 60.0)
        self.assertIsNone(calc_hit({'input': 500, 'cache': {'read': 0, 'write': 0}}))


class MessageToSampleTests(unittest.TestCase):
    def test_completed_streamed_message_becomes_sample(self):
        sample = message_to_sample(message('msg_a', completed=6000, streamed=4000))
        self.assertEqual(sample[0], 1000)
        self.assertEqual(sample[1], 'msg_a')
        self.assertEqual(sample[2], 120)
        self.assertAlmostEqual(sample[3], 3.0)

    def test_skips_incomplete(self):
        self.assertIsNone(message_to_sample(message('msg_a', completed=None, streamed=3000)))

    def test_skips_without_streamed(self):
        self.assertIsNone(message_to_sample(message('msg_a', completed=5000, streamed=None)))

    def test_skips_zero_output(self):
        self.assertIsNone(message_to_sample(message(
            'msg_a', completed=5000, streamed=3000, output=0, reasoning=0)))


class FindCompletedTests(unittest.TestCase):
    def test_skips_non_assistant(self):
        messages = [{'id': 'u', 'type': 'user'},
                    message('a', completed=9000, streamed=3000)]
        self.assertEqual(find_completed(messages)['id'], 'a')

    def test_none_completed(self):
        self.assertIsNone(find_completed([message('a', completed=None)]))

    def test_returns_first_completed_in_desc_order(self):
        messages = [message('new', completed=9000, streamed=3000),
                    message('old', completed=5000, streamed=3000)]
        self.assertEqual(find_completed(messages)['id'], 'new')

    def test_completed_fallback_method_kept(self):
        messages = [message('a', completed=9000, streamed=None)]
        self.assertEqual(find_completed(messages)['id'], 'a')


class AggregationTests(unittest.TestCase):
    def test_average_and_usage(self):
        agg = Aggregation()
        agg.add_sample((1000, 'a', 700, 7.0))
        agg.add_sample((2000, 'b', 300, 3.0))
        self.assertAlmostEqual(agg.average(), 100.0)
        self.assertEqual(len(agg.samples), 2)
        self.assertIn('2回', agg.usage())

    def test_model_change_resets_average(self):
        agg = Aggregation()
        agg.add_message(message('a', model={'id': 'M'},
                                completed=3000, streamed=2000))
        agg.add_message(message('b', model={'id': 'OTHER'},
                                completed=4000, streamed=2000, output=60, reasoning=0))
        self.assertEqual(agg.model, model_key({'id': 'OTHER'}))
        self.assertEqual(len(agg.samples), 1)
        self.assertAlmostEqual(agg.average(), 60.0)

    def test_add_message_switches_model_and_recounts(self):
        agg = Aggregation()
        agg.add_message(message('a', model={'id': 'model-a'},
                                completed=3000, streamed=2000))
        self.assertAlmostEqual(agg.average(), 120.0)
        agg.add_message(message('b', model={'id': 'model-b'},
                                completed=4000, streamed=2000, output=60, reasoning=0))
        self.assertEqual(agg.model, model_key({'id': 'model-b'}))
        self.assertAlmostEqual(agg.average(), 60.0)
        self.assertEqual(len(agg.samples), 1)

    def test_add_message_guards(self):
        agg = Aggregation()
        self.assertTrue(agg.add_message(message(
            'a', model={'id': 'm'}, completed=4000, streamed=3000)))
        self.assertFalse(agg.add_message(message(
            'a', model={'id': 'm'}, completed=4000, streamed=3000)))
        self.assertFalse(agg.add_message(message(
            'c', model={'id': 'm'}, completed=None, streamed=3000)))

    def test_dedup_and_reject_older(self):
        agg = Aggregation()
        agg.add_sample((1000, 'a', 60, 6.0))
        self.assertFalse(agg.add_sample((1000, 'a', 60, 6.0)))
        self.assertTrue(agg.add_sample((3000, 'c', 60, 6.0)))
        self.assertEqual(agg.samples[-1][1], 'c')
        self.assertEqual(len(agg.samples), 2)

    def test_capacity_ten(self):
        agg = Aggregation()
        for index in range(12):
            agg.add_sample((index * 1000, 'm%d' % index, 60, 1.0))
        self.assertEqual(len(agg.samples), 10)
        self.assertEqual(agg.samples[0][1], 'm2')

    def test_weighted_not_simple_mean(self):
        agg = Aggregation()
        agg.add_sample((1000, 'a', 1000, 100.0))   # 10 tok/s
        agg.add_sample((2000, 'b', 10, 1.0))       # 10 tok/s
        agg.add_sample((3000, 'c', 1000, 2.0))     # 500 tok/s
        # 単純平均のtok/s平均ではない。合計tok / 合計秒。
        self.assertAlmostEqual(agg.average(), 2010.0 / 103.0)


class FmtSpeedTests(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(fmt_speed(None), '計測不可')
        self.assertEqual(fmt_speed(82.44), '82.4 tok/s')
        self.assertEqual(fmt_speed(1200.0), '1.2K tok/s')


class ServiceTests(unittest.TestCase):
    def test_session_messages_query(self):
        service = Service()
        seen = {}

        def fake_get(path):
            seen['path'] = path
            return {'data': []}

        service.get = fake_get
        self.assertEqual(service.session_messages('s1'), [])
        path = seen['path']
        self.assertIn('/api/session/s1/message', path)
        self.assertIn('type=assistant', path)
        self.assertIn('order=desc', path)
        self.assertIn('limit=20', path)

    def test_recent_sessions_query(self):
        service = Service()
        seen = {}

        def fake_get(path):
            seen['path'] = path
            return {'data': [{'id': 's1'}]}

        service.get = fake_get
        sessions = service.recent_sessions()
        self.assertEqual(seen['path'], '/api/session?limit=20')
        self.assertEqual(len(sessions), 1)


if __name__ == '__main__':
    unittest.main()
