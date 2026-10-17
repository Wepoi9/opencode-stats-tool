"""opencode_stats.py の計測ロジックに対する unittest。外部通信なし。"""
import queue
import unittest

from opencode_stats import (
    calc_speed, calc_hit, calc_stats, message_to_sample,
    model_key, Aggregation, fmt_speed, Service, ServiceError,
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


def bare_app(service):
    """Tk を起動せず、テスト対象の最小状態だけを用意した App を返す。"""
    from opencode_stats import App, Aggregation
    app = App.__new__(App)
    app.q = queue.Queue()
    app.apiq = queue.Queue()
    app.pending_syncs = set()
    app.service = service
    app.target_sid = 's1'
    app.session_titles = {}
    app.target_child = False
    app.session_tokens = None
    app.agg = Aggregation()
    app.status = 'idle'
    app.stats_error = False
    app.last_key = None
    app.last_speed = None
    app.last_seconds = None
    app.last_method = None
    return app


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


class SyncTargetTests(unittest.TestCase):
    def test_dedupes_pending_requests_for_same_sid(self):
        app = bare_app(StubService())
        app.sync_target('s1')
        app.sync_target('s1')
        app.sync_target('s2')
        self.assertEqual(app.apiq.qsize(), 2)
        self.assertEqual(app.pending_syncs, {'s1', 's2'})

    def test_discarded_on_processing(self):
        app = bare_app(StubService())
        app.sync_target('s1')
        app.pending_syncs.discard('s1')
        app.sync_target('s1')
        self.assertEqual(app.apiq.qsize(), 2)

    def test_ignores_invalid_sid(self):
        app = bare_app(StubService())
        app.sync_target(None)
        app.sync_target('')
        self.assertEqual(app.apiq.qsize(), 0)
        self.assertEqual(app.pending_syncs, set())


class SyncErrorTests(unittest.TestCase):
    def test_failed_sync_reports_error(self):
        app = bare_app(StubService(fail=True))
        app.run_job('target-sync', 's1')
        kind, _, _ = app.q.get_nowait()
        self.assertEqual(kind, 'sync-error')
        self.assertTrue(app.q.empty())

    def test_failed_refresh_reports_error(self):
        app = bare_app(StubService(fail=True))
        app.run_job('refresh-all', None)
        kind, _, _ = app.q.get_nowait()
        self.assertEqual(kind, 'sync-error')

    def test_successful_sync_does_not_report(self):
        app = bare_app(StubService())
        app.run_job('target-sync', 's1')
        kind, param, _ = app.q.get_nowait()
        self.assertEqual(kind, 'target-stats')
        self.assertEqual(param, 's1')
        self.assertTrue(app.q.empty())

    def test_error_state_roundtrip(self):
        app = bare_app(StubService())
        app.stats_error = True
        app.dispatch('sync-error', None, None)
        self.assertTrue(app.stats_error)
        info = {'title': 't', 'messages': []}
        app.on_target_stats('s2', info)
        self.assertTrue(app.stats_error)
        app.on_target_stats('s1', info)
        self.assertFalse(app.stats_error)


class StubService:
    def __init__(self, fail=False):
        self.fail = fail

    def session_info(self, sid):
        if self.fail:
            raise ServiceError('HTTP 500')
        return {'title': 't', 'tokens': None}

    def session_messages(self, sid):
        return []

    def active_sessions(self):
        if self.fail:
            raise ServiceError('HTTP 500')
        return []


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
