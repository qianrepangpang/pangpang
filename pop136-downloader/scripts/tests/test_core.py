import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from pop136_core import (
    FILE_DOWNLOAD_TIMEOUT_SECONDS,
    cards_for_download,
    candidate_urls,
    existing_names,
    is_valid_download,
    next_page,
    pending_state_jobs,
    page_batch,
    profile_lock_message,
    repair_invalid_state_files,
    record_files_complete,
    record_finished_for_run,
    reactivate_timed_out_files,
    retry_delay_seconds,
    safe_filename,
    should_auto_resume,
    should_download,
    download_worker_count,
    stream_page_jobs,
    should_checkpoint,
    stable_concurrency_cap,
    next_concurrency_limit,
    browser_batch_size,
    close_extra_browser_pages,
    create_browser_pool_tabs,
    auto_resume_ready,
    is_html_challenge,
    is_verification_error,
    HtmlChallenge,
    Pop136Engine,
    remember_browser_routes,
    use_browser_route,
    valid_download_url,
    BROWSER_POOL_SIZE,
    DISPLAY_CHECK_SECONDS,
    CDP_CONNECT_TIMEOUT_MS,
    browser_launch_args,
    login_browser_launch_args,
    download_browser_headless,
    wait_for_full_card_page,
    start_browser_timeout_watchdog,
)


class CoreTests(unittest.TestCase):
    def test_browser_keeps_one_primary_page_and_six_worker_tabs(self):
        self.assertEqual(BROWSER_POOL_SIZE, 6)

    def test_primary_page_is_activated_every_five_minutes(self):
        self.assertEqual(DISPLAY_CHECK_SECONDS, 5 * 60)

    def test_browser_session_connect_has_a_hard_timeout(self):
        self.assertEqual(CDP_CONNECT_TIMEOUT_MS, 30_000)

    def test_browser_timeout_watchdog_closes_stuck_target(self):
        closed = []
        timer, expired = start_browser_timeout_watchdog("target-1", 0.01, closed.append)
        self.assertTrue(expired.wait(1))
        timer.join(1)
        self.assertEqual(closed, ["target-1"])

    def test_cancelled_browser_timeout_watchdog_does_not_close_target(self):
        closed = []
        timer, expired = start_browser_timeout_watchdog("target-1", 1, closed.append)
        timer.cancel()
        timer.join(1)
        self.assertFalse(expired.is_set())
        self.assertEqual(closed, [])

    def test_invalid_download_urls_are_rejected(self):
        self.assertFalse(valid_download_url("https://imgyt2.pop-fashion.com/undefined"))
        self.assertFalse(valid_download_url("undefined"))
        self.assertTrue(valid_download_url("https://imgyt2.pop-fashion.com/path/file.ai"))

    def test_invalid_state_files_are_reset_for_detail_recollection(self):
        state = {
            "processed": {
                "810755": {
                    "selection_checked": True,
                    "files": [{"name": "810755_3", "url": "https://imgyt2.pop-fashion.com/undefined"}],
                }
            }
        }
        self.assertEqual(repair_invalid_state_files(state), 1)
        self.assertEqual(state["processed"]["810755"]["files"], [])
        self.assertEqual(state["processed"]["810755"]["status"], "retry_detail")

    def test_single_file_timeout_is_two_minutes(self):
        self.assertEqual(FILE_DOWNLOAD_TIMEOUT_SECONDS, 2 * 60)

    class FakeFirst:
        def wait_for(self, **_kwargs):
            return None

    class FakeCards:
        def __init__(self, counts):
            self.counts = iter(counts)
            self.current = 0
            self.first = CoreTests.FakeFirst()

        def count(self):
            self.current = next(self.counts, self.current)
            return self.current

    class FakePage:
        def __init__(self):
            self.scrolls = 0

        def evaluate(self, _script):
            self.scrolls += 1

        def wait_for_timeout(self, _milliseconds):
            return None

    def test_safe_filename_removes_windows_invalid_characters(self):
        self.assertEqual(safe_filename('  a<b>:c?.psd.  '), 'a_b__c_.psd')

    def test_existing_names_are_case_insensitive_and_ignore_temp_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'Flower.PSD').write_bytes(b'x')
            (root / 'Flower.PSD.part').write_bytes(b'x')
            self.assertEqual(existing_names(root), {'flower.psd'})

    def test_html_challenge_is_not_treated_as_a_download(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            fake = root / 'fake.jpg'
            fake.write_bytes(b'<script>challenge</script>')
            self.assertFalse(is_valid_download(fake))
            self.assertNotIn('fake.jpg', existing_names(root))

    def test_html_challenge_is_reported_as_manual_verification(self):
        with tempfile.TemporaryDirectory() as folder:
            fake = Path(folder) / 'challenge.png.part'
            fake.write_bytes(b'<!doctype html><html>verify</html>')
            self.assertTrue(is_html_challenge(fake))
        self.assertTrue(is_verification_error('服务器返回网页验证，需在浏览器手动完成验证'))
        self.assertFalse(is_verification_error('curl 返回代码 0'))

    def test_html_challenge_does_not_inherit_the_generic_retry_path(self):
        with self.assertRaises(HtmlChallenge):
            raise HtmlChallenge('服务器返回网页验证，需在浏览器手动完成验证')

    def test_successful_retry_clears_an_old_verification_error(self):
        state = {'processed': {'work': {'files': [{'name': 'image.jpg', 'error': '服务器返回网页验证'}]}}}
        Pop136Engine._set_file_status(
            state, {'id': 'work', 'name': 'image.jpg'}, 'downloaded', size=12
        )
        file = state['processed']['work']['files'][0]
        self.assertEqual(file['status'], 'downloaded')
        self.assertNotIn('error', file)

    def test_record_requires_real_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'one.jpg').write_bytes(b'\xff\xd8\xffimage')
            record = {'files': [{'name': 'one.jpg'}]}
            self.assertTrue(record_files_complete(root, record))
            (root / 'one.jpg').write_bytes(b'<html>blocked</html>')
            self.assertFalse(record_files_complete(root, record))

    def test_record_uses_prechecked_file_index(self):
        record = {'files': [{'name': 'one.jpg'}, {'name': 'two.png'}]}
        with patch('pop136_core.is_valid_download', side_effect=AssertionError('unexpected file read')):
            self.assertTrue(record_files_complete(Path('unused'), record, known={'one.jpg', 'two.png'}))
            self.assertFalse(record_files_complete(Path('unused'), record, known={'one.jpg'}))

    def test_psd_and_eps_are_excluded(self):
        self.assertFalse(should_download('design.PSD'))
        self.assertFalse(should_download('design.eps'))
        self.assertTrue(should_download('preview.jpg'))
        self.assertTrue(should_download('artwork.png'))

    def test_all_years_are_selected_in_page_order(self):
        cards = [
            {'id': 'new', 'date': '2026-01-01'},
            {'id': 'old', 'date': '2025-12-31'},
        ]
        self.assertEqual(cards_for_download(cards), cards)

    def test_automatic_retry_delay_is_bounded(self):
        self.assertEqual(retry_delay_seconds(1), 5)
        self.assertEqual(retry_delay_seconds(4), 20)
        self.assertEqual(retry_delay_seconds(99), 60)

    def test_existing_state_enables_automatic_resume(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            target = root / 'downloads'
            profile = root / 'profile'
            target.mkdir()
            profile.mkdir()
            self.assertFalse(should_auto_resume(target, profile))
            (target / '_download_state.json').write_text('{}', encoding='utf-8')
            self.assertTrue(should_auto_resume(target, profile))
            (target / '_download_complete.flag').write_text('1147', encoding='utf-8')
            self.assertFalse(should_auto_resume(target, profile))

    def test_empty_next_page_finishes_without_retrying_forever(self):
        class EmptyPage:
            def __init__(self):
                self.reload_count = 0

            def is_closed(self):
                return False

            def goto(self, *_args, **_kwargs):
                return None

            def reload(self, **_kwargs):
                self.reload_count += 1

            def locator(self, selector):
                if selector == 'body':
                    return self
                return self

            def inner_text(self):
                return '图案灵感库\n共 0 个'

            def count(self):
                return 0

            def evaluate(self, _script):
                return True

        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder)
            state = {'processed': {}, 'pageStats': {}, 'lastCompletedPage': 1146}
            engine = Pop136Engine(target, target, 'all', lambda _message: None, threading.Event())
            page = EmptyPage()
            with patch('pop136_core.wait_for_full_card_page', side_effect=RuntimeError('未看到图案列表')):
                self.assertTrue(engine._process_page(page, 1147, state))
            self.assertEqual(page.reload_count, 1)
            self.assertEqual(state['lastCompletedPage'], 1146)
            self.assertTrue((target / '_download_complete.flag').is_file())

    def test_full_page_is_collected_as_one_batch(self):
        pending = [{'id': str(index)} for index in range(60)]
        self.assertEqual(page_batch(pending), pending)

    def test_download_workers_match_actual_file_count(self):
        self.assertEqual(download_worker_count(87), 87)

    def test_each_card_is_submitted_before_collecting_the_next(self):
        events = []

        def collect(card):
            events.append(f'collect:{card}')
            return [f'file:{card}']

        def submit(job):
            events.append(f'submit:{job}')

        stream_page_jobs(['a', 'b'], collect, submit)
        self.assertEqual(events, [
            'collect:a', 'submit:file:a',
            'collect:b', 'submit:file:b',
        ])

    def test_streaming_state_is_checkpointed_in_small_batches(self):
        self.assertFalse(should_checkpoint(1))
        self.assertFalse(should_checkpoint(5))
        self.assertTrue(should_checkpoint(20))
        self.assertTrue(should_checkpoint(60))

    def test_stable_concurrency_is_capped_for_cdn_stability(self):
        self.assertEqual(stable_concurrency_cap(24), 8)
        self.assertEqual(stable_concurrency_cap(2), 4)
        self.assertEqual(stable_concurrency_cap(96), 8)

    def test_concurrency_rises_on_success_and_halves_on_failure(self):
        self.assertEqual(next_concurrency_limit(12, 12, 0, 24), 14)
        self.assertEqual(next_concurrency_limit(24, 24, 0, 24), 24)
        self.assertEqual(next_concurrency_limit(24, 20, 1, 24), 12)

    def test_browser_fallback_uses_stable_batches(self):
        self.assertEqual(browser_batch_size(1), 1)
        self.assertEqual(browser_batch_size(4), 4)
        self.assertEqual(browser_batch_size(24), 6)

    def test_browser_pool_uses_tabs_and_cleans_old_pages(self):
        class FakePage:
            def __init__(self, url):
                self.url = url
                self.closed = False

            def close(self):
                self.closed = True

            def wait_for_timeout(self, _milliseconds):
                return None

        class FakeSession:
            def __init__(self, context):
                self.context = context
                self.calls = []
                self.detached = False

            def send(self, method, params):
                self.calls.append((method, params))
                self.context.pages.append(FakePage(params['url']))
                return {'targetId': str(len(self.calls))}

            def detach(self):
                self.detached = True

        class FakeContext:
            def __init__(self, pages):
                self.pages = pages
                self.session = FakeSession(self)

            def new_cdp_session(self, _page):
                return self.session

        main = FakePage('https://yuntu.pop136.com/patternlibrary/')
        old_pages = [FakePage('about:blank'), FakePage('https://yuntu.pop136.com/patternlibrary/')]
        context = FakeContext([main, *old_pages])
        self.assertEqual(close_extra_browser_pages(context, main), 2)
        self.assertTrue(all(page.closed for page in old_pages))
        tabs = create_browser_pool_tabs(context, main, 4)
        self.assertEqual(len(tabs), 4)
        self.assertTrue(all(call[1]['newWindow'] is False for call in context.session.calls))
        self.assertTrue(context.session.detached)

    def test_download_browser_stays_visible(self):
        self.assertNotIn('--start-minimized', browser_launch_args())
        self.assertFalse(download_browser_headless())

    def test_login_browser_is_forced_onscreen(self):
        args = login_browser_launch_args(Path(r'H:\POP136浏览器登录状态'))
        self.assertIn('--window-position=60,60', args)
        self.assertIn('--window-size=1200,900', args)
        self.assertNotIn('--start-minimized', args)

    def test_browser_route_is_remembered_per_file_type(self):
        state = {}
        remember_browser_routes(state, [{'name': 'blocked.JPG'}, {'name': 'blocked.png'}])
        self.assertTrue(use_browser_route(state, 'next.jpg'))
        self.assertTrue(use_browser_route(state, 'next.PNG'))
        self.assertFalse(use_browser_route(state, 'vector.ai'))

    def test_ai_attachment_uses_http_even_when_browser_route_was_saved(self):
        state = {'browserOnlySuffixes': ['.ai', '.jpg']}
        self.assertFalse(use_browser_route(state, 'vector.ai'))
        self.assertTrue(use_browser_route(state, 'preview.jpg'))

    def test_timeout_is_skipped_only_for_current_run(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            state_path = root / '_download_state.json'
            state_path.write_text(
                '{"processed":{"one":{"files":[{"name":"one.ai","url":"https://example/one.ai","status":"skipped_timeout"}]}}}',
                encoding='utf-8',
            )
            state = {'processed': {'one': {'files': [
                {'name': 'one.ai', 'url': 'https://example/one.ai', 'status': 'skipped_timeout'}
            ]}}}
            self.assertTrue(record_finished_for_run(root, state['processed']['one'], 'one'))
            self.assertEqual(pending_state_jobs(root, state), [])
            self.assertEqual(reactivate_timed_out_files(state_path), 1)
            jobs = pending_state_jobs(root, json.loads(state_path.read_text(encoding='utf-8')))
            self.assertEqual([job['name'] for job in jobs], ['one.ai'])

    def test_unavailable_detail_is_deferred_only_for_current_run(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            state_path = root / '_download_state.json'
            state = {
                'processed': {
                    'old': {'files': [], 'status': 'detail_unavailable', 'error': '网站详情页返回 404'}
                }
            }
            state_path.write_text(json.dumps(state, ensure_ascii=False), encoding='utf-8')
            self.assertTrue(record_finished_for_run(root, state['processed']['old'], 'old'))
            self.assertEqual(reactivate_timed_out_files(state_path), 1)
            reloaded = json.loads(state_path.read_text(encoding='utf-8'))
            self.assertEqual(reloaded['processed']['old']['status'], 'retry_detail')

    def test_auto_resume_retries_only_when_safe(self):
        self.assertTrue(auto_resume_ready(True, False, False, False))
        self.assertFalse(auto_resume_ready(True, True, False, False))
        self.assertFalse(auto_resume_ready(True, False, True, False))
        self.assertFalse(auto_resume_ready(True, False, False, True))

    def test_pending_state_jobs_keep_original_record_order(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            state = {'processed': {
                'older': {'files': [{'name': 'same.jpg', 'url': 'https://example/older'}]},
                'newer': {'files': [{'name': 'same.jpg', 'url': 'https://example/newer'}]},
            }}
            jobs = pending_state_jobs(root, state)
            self.assertEqual([job['url'] for job in jobs], [
                'https://example/older', 'https://example/newer'
            ])

    def test_next_page_resumes_after_last_completed_page(self):
        self.assertEqual(next_page({'lastCompletedPage': 7}), 8)
        self.assertEqual(next_page({'page': 3}), 4)
        self.assertEqual(next_page({}), 1)

    def test_cached_page_advances_completed_checkpoint(self):
        class CachedPage:
            def is_closed(self):
                return False

            def goto(self, *_args, **_kwargs):
                return None

            def locator(self, _selector):
                return self

            def evaluate_all(self, _script):
                return [{'id': 'cached', 'index': 1, 'date': '2026-01-01'}]

        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder)
            (target / 'cached.jpg').write_bytes(b'\xff\xd8\xffimage')
            state = {
                'processed': {'cached': {'files': [{'name': 'cached.jpg'}]}},
                'pageStats': {},
                'lastCompletedPage': 1139,
            }
            engine = Pop136Engine(target, target, 'all', lambda _message: None, threading.Event())
            with patch('pop136_core.wait_for_full_card_page', return_value=1):
                engine._process_page(CachedPage(), 1140, state)
            self.assertTrue(state['processed']['cached']['completed'])
            self.assertEqual(state['pageStats']['1140']['completed'], 1)
            self.assertEqual(state['lastCompletedPage'], 1140)

    def test_candidate_urls_rotates_official_cdn_nodes(self):
        urls = candidate_urls('https://imgyt2.pop-fashion.com/path/file.psd')
        self.assertEqual([url.split('/')[2] for url in urls], [
            'imgyt2.pop-fashion.com', 'imgyt1.pop-fashion.com', 'imgyt3.pop-fashion.com'
        ])

    def test_candidate_urls_encode_unicode_paths_and_spaces(self):
        urls = candidate_urls(
            'https://imgyt2.pop-fashion.com/graphic/新建文件夹 (25)/big/file.jpg'
        )
        self.assertIn('%E6%96%B0%E5%BB%BA%E6%96%87%E4%BB%B6%E5%A4%B9%20%2825%29', urls[0])
        self.assertNotIn(' ', urls[0])
        self.assertNotIn('新建文件夹', urls[0])

    def test_missing_virtualized_card_reloads_page_and_uses_id_only(self):
        class Locator:
            def __init__(self, count):
                self._count = count
                self.first = self

            def count(self):
                return self._count

            def wait_for(self, **_kwargs):
                return None

        class Page:
            def __init__(self):
                self.reloaded = False
                self.selectors = []

            def locator(self, selector):
                self.selectors.append(selector)
                if selector == 'li[data-t="graphicitem"][data-index]':
                    return Locator(60)
                return Locator(1 if self.reloaded else 0)

            def goto(self, url, **_kwargs):
                self.reloaded = True
                self.url = url

        page = Page()
        engine = Pop136Engine(Path('.'), Path('.'), 'all', lambda _message: None, threading.Event())
        locator = engine._locate_card(page, {'id': '873319', 'index': 142072}, 2368)
        self.assertEqual(locator.count(), 1)
        self.assertTrue(page.url.endswith('/page_2368/#anchor'))
        self.assertIn('data-id="873319"', page.selectors[0])
        self.assertNotIn('data-index="142072"', page.selectors[0])

    def test_profile_lock_message_when_chrome_owns_profile(self):
        with tempfile.TemporaryDirectory() as folder:
            profile = Path(folder)
            self.assertIsNone(profile_lock_message(profile))
            (profile / 'lockfile').write_text('', encoding='utf-8')
            message = profile_lock_message(profile)
            self.assertIn('登录状态目录仍被 Chrome 占用', message)
            self.assertIn(str(profile), message)

    def test_full_page_scrolls_from_30_to_60_cards(self):
        page = self.FakePage()
        count = wait_for_full_card_page(page, self.FakeCards([30, 60]), 8)
        self.assertEqual(count, 60)
        self.assertEqual(page.scrolls, 1)

    def test_incomplete_page_stops_instead_of_skipping_items(self):
        with self.assertRaisesRegex(RuntimeError, '只加载到 30/60'):
            wait_for_full_card_page(self.FakePage(), self.FakeCards([30, 30, 30]), 8, scroll_rounds=2)


if __name__ == '__main__':
    unittest.main()
