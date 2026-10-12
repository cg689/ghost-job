"""沟通直接跳转、列表变化和恢复失败的回归测试，不访问真实 BOSS。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, Mock

import pytest
from patchright.async_api import Locator, Page
from patchright.async_api import TimeoutError as PlaywrightTimeoutError

from job import models as models_pkg
from job.boss.jobs import Job, JobScraper
from job.models import init_db, reset_engine
from job.models.job import JobRow

if TYPE_CHECKING:
    from job.boss.session import BossSession

LIST_URL = "https://www.zhipin.com/web/geek/job?query=network&city=101250100"
CHAT_URL = "https://www.zhipin.com/web/geek/chat?bossId=test"


@pytest.fixture()
def tmp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(models_pkg, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(models_pkg, "DATA_DIR", tmp_path)
    reset_engine()
    init_db()
    yield
    reset_engine()


def _apply_page():
    page = Mock()
    page.url = LIST_URL
    button = Mock()
    button.inner_text = AsyncMock(return_value="立即沟通")
    button.click = AsyncMock()
    limit = Mock()
    limit.first.is_visible = AsyncMock(return_value=False)
    stay = Mock()
    stay.first.click = AsyncMock()
    dialog = stay.or_.return_value.first
    dialog.wait_for = AsyncMock()
    page.locator.side_effect = lambda selector: (
        Mock(first=button)
        if selector == JobScraper.CHAT_BTN
        else Mock(filter=Mock(return_value=limit))
    )
    page.get_by_text.return_value = stay
    page.wait_for_url = AsyncMock()
    return page, button, stay, limit, dialog


@pytest.mark.parametrize("delayed", [False, True])
def test_apply_accepts_direct_chat_navigation_and_cleans_up_waiters(delayed: bool):
    async def run():
        scraper = JobScraper(cast("BossSession", None))
        page, button, stay, _limit, dialog = _apply_page()
        cancelled = asyncio.Event()

        async def wait_dialog(**_kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        async def navigate(*_args, **_kwargs):
            page.url = CHAT_URL

        dialog.wait_for.side_effect = wait_dialog
        if delayed:
            page.wait_for_url.side_effect = navigate
        else:
            button.click.side_effect = navigate
        assert await asyncio.wait_for(scraper._apply(cast(Page, page)), 2) == ""
        stay.first.click.assert_not_awaited()
        if delayed:
            assert cancelled.is_set()
        else:
            page.wait_for_url.assert_not_awaited()

    asyncio.run(run())


@pytest.mark.parametrize("limited", [False, True])
def test_apply_keeps_stay_dialog_and_daily_limit_behavior(limited: bool):
    async def run():
        scraper = JobScraper(cast("BossSession", None))
        page, _button, stay, limit, _dialog = _apply_page()
        limit.first.is_visible.return_value = limited
        cancelled = asyncio.Event()

        async def wait_chat(*_args, **_kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        page.wait_for_url.side_effect = wait_chat
        result = await asyncio.wait_for(scraper._apply(cast(Page, page)), 2)
        assert cancelled.is_set()
        assert scraper._limit_hit is limited
        if limited:
            assert "上限" in result
            stay.first.click.assert_not_awaited()
        else:
            assert result == ""
            stay.first.click.assert_awaited_once()

    asyncio.run(run())


def test_unconfirmed_communication_is_not_reported_as_success():
    scraper = JobScraper(cast("BossSession", None))
    page, _button, stay, _limit, dialog = _apply_page()
    dialog.wait_for.side_effect = PlaywrightTimeoutError("no confirmation dialog")
    page.wait_for_url.side_effect = PlaywrightTimeoutError("no chat navigation")
    result = asyncio.run(scraper._apply(cast(Page, page)))
    assert result.startswith("投递失败：")
    stay.first.click.assert_not_awaited()


def test_stop_interrupts_both_communication_waiters():
    async def run():
        scraper = JobScraper(cast("BossSession", None))
        page, _button, stay, _limit, dialog = _apply_page()
        entered = asyncio.Event()
        cancelled: list[bool] = []

        async def block(*_args, **_kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.append(True)

        dialog.wait_for.side_effect = block
        page.wait_for_url.side_effect = block
        task = asyncio.create_task(scraper._apply(cast(Page, page)))
        await asyncio.wait_for(entered.wait(), 2)
        scraper.request_stop()
        assert "未确认" in await asyncio.wait_for(task, 2)
        assert len(cancelled) == 2
        stay.first.click.assert_not_awaited()

    asyncio.run(run())


def _search_page(scraper: JobScraper, jobs: list[Job]):
    page = Mock()
    page.url = LIST_URL
    cards = Mock()
    cards.count = AsyncMock(
        side_effect=lambda: len(jobs) if page.url == LIST_URL else 0
    )
    cards.nth.side_effect = lambda index: Mock(
        get_attribute=AsyncMock(return_value=jobs[index].job_id)
    )
    login = Mock()
    login.count = AsyncMock(return_value=0)
    page.locator.side_effect = lambda selector: (
        cards if selector == scraper.CARD else login
    )

    async def goto(url, **_kwargs):
        page.url = url
        # 恢复时重排列表，确保依赖职位 ID 去重，不能继续用跳转前的卡片序号。
        jobs.reverse()
        scraper._listed.update((job.job_id, job) for job in jobs)

    page.goto = AsyncMock(side_effect=goto)
    page.wait_for_selector = AsyncMock()
    scraper._open_detail = AsyncMock(side_effect=lambda _page, _card, job: job)
    scraper._pause = AsyncMock()
    scraper._load_more = AsyncMock()
    return page, cards


def test_search_restores_filters_and_sends_each_job_once_after_chat_redirect(
    tmp_db, monkeypatch: pytest.MonkeyPatch
):
    scraper = JobScraper(cast("BossSession", None))
    monkeypatch.setattr(scraper, "MAX_SCROLLS", 1)
    jobs = [
        Job(job_id=f"job-{i}", title=f"网络工程师 {i}", company=f"公司 {i}")
        for i in range(3)
    ]
    page, _cards = _search_page(scraper, jobs)
    applied: list[str] = []

    async def detail(_page, _card, job):
        applied.append(job.job_id)
        return job

    async def apply(_page):
        page.url = CHAT_URL
        return ""

    scraper._open_detail = AsyncMock(side_effect=detail)
    scraper._apply = AsyncMock(side_effect=apply)
    rest = AsyncMock()
    scraper._rest_after = rest
    results: list[Job] = []
    state = asyncio.run(scraper._search_city(cast(Page, page), LIST_URL, results, None))
    assert state == "done"
    assert len(results) == len(set(applied)) == len(applied) == 3
    assert scraper._today == 3
    assert rest.await_count == 3
    assert [call.args[0] for call in page.goto.await_args_list] == [LIST_URL] * 4
    for job in results:
        row = JobRow.get_dict(job.job_id)
        assert row is not None and row["result"] == "已投递"


def test_repeated_redirects_do_not_exhaust_budget_reloading_the_second_page(
    tmp_db, monkeypatch: pytest.MonkeyPatch
):
    scraper = JobScraper(cast("BossSession", None))
    monkeypatch.setattr(scraper, "MAX_SCROLLS", 2)
    all_jobs = [
        Job(job_id=f"job-{i}", title=f"网络工程师 {i}", company=f"公司 {i}")
        for i in range(3)
    ]
    visible = all_jobs[:1]
    page, _cards = _search_page(scraper, visible)
    original_goto = page.goto.side_effect

    async def goto(url, **kwargs):
        visible[:] = all_jobs[:1]
        await original_goto(url, **kwargs)

    async def load_more(_page):
        visible[:] = all_jobs[:]
        scraper._listed.update((job.job_id, job) for job in visible)

    async def apply(_page):
        page.url = CHAT_URL
        return ""

    page.goto.side_effect = goto
    scraper._load_more = AsyncMock(side_effect=load_more)
    scraper._apply = AsyncMock(side_effect=apply)
    results: list[Job] = []
    state = asyncio.run(scraper._search_city(cast(Page, page), LIST_URL, results, None))
    assert state == "done"
    assert {job.job_id for job in results} == {job.job_id for job in all_jobs}
    assert scraper._apply.await_count == scraper._today == 3


@pytest.mark.parametrize("login", [False, True])
def test_recovery_failure_preserves_completed_job_and_stops_city(tmp_db, login: bool):
    scraper = JobScraper(cast("BossSession", None))
    jobs = [Job(job_id="sent", title="网络工程师", company="公司")]
    page, _cards = _search_page(scraper, jobs)
    original_goto = page.goto.side_effect
    visits = 0

    async def goto(url, **kwargs):
        nonlocal visits
        visits += 1
        await original_goto(url, **kwargs)
        if visits > 1:
            if login:
                page.url = "https://www.zhipin.com/passport/verify"
            else:
                page.wait_for_selector.side_effect = PlaywrightTimeoutError(
                    "list unavailable"
                )

    async def apply(_page):
        page.url = CHAT_URL
        return ""

    page.goto.side_effect = goto
    scraper._apply = AsyncMock(side_effect=apply)
    results: list[Job] = []
    state = asyncio.run(scraper._search_city(cast(Page, page), LIST_URL, results, None))
    assert state == ("need_login" if login else "load_failed")
    assert [job.job_id for job in results] == ["sent"]
    scraper._apply.assert_awaited_once()


def test_detail_navigation_is_not_mistaken_for_successful_communication(tmp_db):
    scraper = JobScraper(cast("BossSession", None))
    page, _cards = _search_page(scraper, [Job(job_id="unsent", title="网络工程师")])

    async def detail(_page, _card, job):
        page.url = CHAT_URL
        return job

    scraper._open_detail = AsyncMock(side_effect=detail)
    scraper._apply = AsyncMock()
    results: list[Job] = []
    state = asyncio.run(scraper._search_city(cast(Page, page), LIST_URL, results, None))
    assert state == "load_failed"
    assert results == [] and scraper._today == 0
    assert JobRow.get_dict("unsent") is None
    scraper._apply.assert_not_awaited()


def test_disappearing_card_does_not_raise_attribute_timeout(tmp_db):
    scraper = JobScraper(cast("BossSession", None))
    page, cards = _search_page(scraper, [Job(job_id="gone", title="网络工程师")])
    cards.nth.return_value = Mock()
    cards.nth.side_effect = None
    card = cards.nth.return_value
    card.get_attribute = AsyncMock(
        side_effect=PlaywrightTimeoutError("nth(29) disappeared")
    )
    scraper._apply = AsyncMock()
    done: set[str] = set()
    assert asyncio.run(scraper._scrape_cards(cast(Page, page), done, None)) == []
    assert done == set()
    card.get_attribute.assert_awaited_once_with("data-jobid", timeout=2_000)
    scraper._apply.assert_not_awaited()


def test_shortened_list_stops_before_reading_missing_card(tmp_db):
    scraper = JobScraper(cast("BossSession", None))
    jobs = [
        Job(job_id=f"job-{i}", title=f"网络工程师 {i}", company=f"公司 {i}")
        for i in range(2)
    ]
    page, cards = _search_page(scraper, jobs)
    scraper._listed.update((job.job_id, job) for job in jobs)

    async def apply(_page):
        jobs.pop()
        return ""

    scraper._apply = AsyncMock(side_effect=apply)
    results = asyncio.run(scraper._scrape_cards(cast(Page, page), set(), None))
    assert len(results) == 1
    assert cards.nth.call_count == 1


def test_listed_job_still_uses_order_when_attribute_is_absent():
    scraper = JobScraper(cast("BossSession", None))
    job = Job(job_id="fallback", title="网络工程师")
    scraper._listed[job.job_id] = job
    card = Mock(get_attribute=AsyncMock(return_value=None))
    assert asyncio.run(scraper._listed_job_for(cast(Locator, card), 0)) == job
