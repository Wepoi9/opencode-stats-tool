"""opencode_stats.py の計測ロジックに対する unittest。外部通信なし。"""
import unittest

from opencode_stats import (
    calc_speed, calc_hit, calc_stats, message_to_sample,
    model_key, Aggregation, fmt_speed, Service,
)


def message(mid, created=1000, streamed=None, completed=None, output=100,
            reasoning=20, model=None, read=0, write=0, input=500):
    if model is None:
        model = {'id': 'M'}
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


class CalcStatsTests(unittest.TestCase):
    def test_speed_only_shape(self):
        stat = calc_stats(message('m', completed=6000, streamed=4000))
        self.assertEqual(set(stat), {'key', 'speed', 'seconds', 'method'})
        self.assertEqual(stat['key'], (1000, 'm'))
        self.assertEqual(stat['method'], 'measured')
        self.assertAlmostEqual(stat['speed'], 40.0)


class AggregationTests(unittest.TestCase):
    def test_ascending_replay_adopts_samples(self):
        agg = Aggregation()
        self.assertTrue(agg.update(message('a', completed=3000, streamed=2000)))
        self.assertTrue(agg.update(message('b', created=5000, completed=7000,
                                           streamed=6000)))
        self.assertAlmostEqual(agg.average(), 120.0)
        self.assertEqual(agg.newest_key, (5000, 'b'))

    def test_stale_message_rejected_before_any_reset(self):
        agg = Aggregation()
        agg.update(message('new', created=5000, model={'id': 'B'},
                           completed=9000, streamed=8000))
        samples_before = list(agg.samples)
        self.assertFalse(agg.update(message(
            'old', created=1000, model={'id': 'A'},
            completed=2000, streamed=1500)))
        self.assertEqual(agg.samples, samples_before)
        self.assertEqual(agg.model, model_key({'id': 'B'}))

    def test_duplicate_message_not_counted_twice(self):
        agg = Aggregation()
        msg = message('a', completed=3000, streamed=2000)
        agg.update(msg)
        self.assertFalse(agg.update(msg))
        self.assertEqual(len(agg.samples), 1)

    def test_model_change_resets(self):
        agg = Aggregation()
        agg.update(message('a', model={'id': 'A'}, completed=3000, streamed=2000))
        agg.update(message('b', model={'id': 'B'}, completed=9000,
                           streamed=2000, output=60, reasoning=0))
        self.assertEqual(agg.model, model_key({'id': 'B'}))
        self.assertEqual(len(agg.samples), 1)
        self.assertAlmostEqual(agg.average(), 60.0)

    def test_newest_key_advances_without_sample(self):
        agg = Aggregation()
        agg.update(message('a', completed=3000, streamed=2000))
        before = list(agg.samples)
        self.assertTrue(agg.update(message('run', created=5000, completed=None,
                                           streamed=None, output=None)))
        self.assertEqual(agg.samples, before)
        self.assertEqual(agg.newest_key, (5000, 'run'))

    def test_rejects_missing_id_or_time(self):
        agg = Aggregation()
        self.assertFalse(agg.update({'id': '', 'model': 'M', 'tokens': {},
                                     'time': {}}))
        self.assertFalse(agg.update(message('a', created=None, completed=3000,
                                            streamed=2000)))

    def test_capacity_ten(self):
        agg = Aggregation()
        for index in range(12):
            agg.update(message('m%d' % index, created=index * 1000,
                               completed=index * 1000 + 500, streamed=index * 1000 + 400,
                               output=60, reasoning=0))
        self.assertEqual(len(agg.samples), 10)
        self.assertEqual(agg.samples[0][1], 'm2')

    def test_weighted_not_simple_mean(self):
        agg = Aggregation()
        agg.update(message('a', created=1000, completed=4000, streamed=2000,
                           output=1000, reasoning=0))   # 1000 tok / 1s
        agg.update(message('b', created=5000, completed=7000, streamed=6000,
                           output=10, reasoning=0))     # 10 tok / 1s
        agg.update(message('c', created=8000, completed=12000, streamed=10000,
                           output=1000, reasoning=0))   # 1000 tok / 2s
        self.assertAlmostEqual(agg.average(), 2010.0 / 4.0)


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
