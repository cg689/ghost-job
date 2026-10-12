"""用本机 Chrome 和拦截的网页验证真实 DOM 跳转，不连接招聘网站或发送消息。

未安装 Chrome 的环境自动跳过；普通单元测试不依赖浏览器。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlparse

import pytest
from patchright.async_api import Error as PlaywrightError
from patchright.async_api import async_playwright

from job import models as models_pkg
from job.boss.jobs import JobScraper
from job.models import init_db, reset_engine
from job.models.job import JobRow

if TYPE_CHECKING:
    from patchright.async_api import Route

    from job.boss.jobs import Job
    from job.boss.session import BossSession

LIST_URL = "https://ghost-job.test/web/geek/job?query=network&city=101250100"

LIST_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8"></head><body>
<ul id="jobs"></ul>
<div class="job-detail-box"><button class="op-btn-chat">立即沟通</button></div>
<script>
const button = document.querySelector('.op-btn-chat');
fetch('/joblist.json').then(r => r.json()).then(payload => {
  for (const job of payload.zpData.jobList) {
    const li = document.createElement('li');
    li.className = 'job-card-box';
    li.dataset.jobid = job.encryptJobId;
    li.textContent = job.jobName;
    li.onclick = async () => {
      await fetch('/job/detail.json?id=' + job.encryptJobId);
      button.dataset.job = job.encryptJobId;
    };
    document.querySelector('#jobs').append(li);
  }
});
button.onclick = async () => {
  const {mode} = await (await fetch('/send?id=' + button.dataset.job)).json();
  if (mode === 'direct') {
    location.href = '/web/geek/chat?bossId=test';
  } else {
    const dialog = document.createElement('div');
    dialog.className = 'dialog';
    dialog.textContent = mode === 'limit' ? '今日沟通已达上限，明天再来' : '沟通成功';
    if (mode === 'stay') {
      const stay = document.createElement('button');
      stay.textContent = '留在此页';
      stay.onclick = () => dialog.remove();
      dialog.append(stay);
    }
    document.body.append(dialog);
  }
};
</script></body></html>"""


@pytest.fixture()
def tmp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(models_pkg, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(models_pkg, "DATA_DIR", tmp_path)
    reset_engine()
    init_db()
    yield
    reset_engine()


@pytest.mark.parametrize("mode", ["direct", "stay", "limit"])
def test_real_browser_communication_navigation(tmp_db, mode: str):
    async def run():
        async with async_playwright() as playwright:
            try:
                browser = await playwright.chromium.launch(
                    channel="chrome", headless=True
                )
            except PlaywrightError as exc:
                if "not found" in str(exc) or "doesn't exist" in str(exc):
                    pytest.skip("Chrome is not installed")
                raise
            try:
                page = await browser.new_page()
                # 超过 MAX_SCROLLS 的跳转次数仍能处理完，恢复不能消耗下滑加载次数。
                rows = [
                    {
                        "encryptJobId": f"job-{i}",
                        "jobName": f"网络工程师 {i}",
                        "brandName": f"公司 {i}",
                    }
                    for i in range(21 if mode == "direct" else 2)
                ]
                requests: list[str] = []
                loads: list[str] = []

                async def route_request(route: Route):
                    request_url = route.request.url
                    parsed = urlparse(request_url)
                    # 所有页面请求都在本机拦截，包括聊天页；禁止落到真实网络。
                    if parsed.hostname != "ghost-job.test":
                        await route.abort()
                    elif parsed.path == "/web/geek/job":
                        loads.append(request_url)
                        await route.fulfill(content_type="text/html", body=LIST_HTML)
                    elif parsed.path == "/joblist.json":
                        rows.reverse()
                        await route.fulfill(json={"zpData": {"jobList": rows}})
                    elif parsed.path == "/job/detail.json":
                        await route.fulfill(
                            json={
                                "zpData": {"jobInfo": {"postDescription": "网络运维"}}
                            }
                        )
                    elif parsed.path == "/send":
                        requests.append(parse_qs(parsed.query)["id"][0])
                        await route.fulfill(json={"mode": mode})
                    elif parsed.path == "/web/geek/chat":
                        await route.fulfill(
                            content_type="text/html", body="<h1>模拟聊天页</h1>"
                        )
                    else:
                        await route.abort()

                await page.route("**/*", route_request)
                scraper = JobScraper(cast("BossSession", None))
                scraper._pause = AsyncMock()
                scraper._load_more = AsyncMock()
                page.on("response", scraper._on_list_response)
                results: list[Job] = []
                try:
                    state = await asyncio.wait_for(
                        scraper._search_city(page, LIST_URL, results, None), 45
                    )
                finally:
                    page.remove_listener("response", scraper._on_list_response)
                if mode == "limit":
                    assert state == "limit"
                    assert results == [] and scraper._today == 0
                    assert len(requests) == 1
                else:
                    assert state == "done"
                    assert len(results) == scraper._today == len(rows)
                    assert len(requests) == len(set(requests)) == len(rows)
                    for job in results:
                        row = JobRow.get_dict(job.job_id)
                        assert row is not None and row["result"] == "已投递"
                    assert loads == [LIST_URL] * (
                        len(rows) + 1 if mode == "direct" else 1
                    )
            finally:
                await browser.close()

    asyncio.run(run())
