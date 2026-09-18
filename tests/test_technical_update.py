"""Behavioral regression tests for persistence, cancellation and cookie ownership."""
import asyncio
import os
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer
from unittest.mock import patch
from hypothesis import given, strategies as st
from pydantic import ValidationError

from helper import BOT, load_bot


COOKIE = "# Netscape HTTP Cookie File\n.instagram.com\tTRUE\t/\tTRUE\t2000000000\tsessionid\ttest\n"


@pytest.fixture
def bot():
    mod = load_bot()
    mod.db_init()
    mod.WORK_DIR = mod._test_tmp
    yield mod
    con = getattr(mod._db_local, "con", None)
    if con:
        con.close()


def message(uid=42, chat_id=None):
    return SimpleNamespace(from_user=SimpleNamespace(id=uid),
                           chat=SimpleNamespace(id=chat_id or uid, type="private"),
                           reply=AsyncMock())


def test_cookie_age_survives_redeploy_and_duplicate_upload(bot, monkeypatch):
    monkeypatch.setattr(bot.time, "time", lambda: 1700000000)
    bot.store_cookies(COOKIE)
    assert bot.cookies_status()["uploaded_at"] == 1700000000
    os.utime(bot.COOKIES_FILE, (1800000000, 1800000000))
    restarted = load_bot(STATS_DB=bot.STATS_DB, COOKIES_FILE=bot.COOKIES_FILE)
    restarted.db_init()
    monkeypatch.setattr(bot.time, "time", lambda: 1800000000)
    restarted.store_cookies(COOKIE.replace("\n", "\r\n"))
    assert restarted.cookies_status()["updated"] == 1700000000
    assert restarted.cookies_status()["expires"] == 2000000000
    restarted.db_conn().close()


def test_legacy_cookie_date_is_unknown(bot):
    Path(bot.COOKIES_FILE).write_text(COOKIE, encoding="utf-8")
    assert bot.cookies_status()["updated"] is None
    assert bot.cookies_status()["authentication"] == "unverified"


def test_invalid_cookie_upload_preserves_working_file(bot):
    bot.store_cookies(COOKIE)
    with pytest.raises(ValueError):
        bot.store_cookies("broken")
    assert Path(bot.COOKIES_FILE).read_text() == COOKIE


@given(st.permutations(["# comment", ".instagram.com\tTRUE\t/\tTRUE\t2000000000\tsessionid\tx",
                        ".instagram.com\tTRUE\t/\tTRUE\t2000000000\tcsrftoken\ty"]))
def test_cookie_fingerprint_ignores_order_and_comments(rows):
    assert BOT.cookie_fingerprint("\n".join(rows)) == BOT.cookie_fingerprint(
        "\r\n".join(reversed(rows)))


@pytest.mark.asyncio
async def test_cookie_snapshot_is_private_and_source_unchanged(bot):
    bot.store_cookies(COOKIE)
    script = "import pathlib,sys; pathlib.Path(sys.argv[-1]).write_text('modified')"
    async with bot.managed_process([sys.executable, "-c", script, "--cookies", bot.COOKIES_FILE]) as proc:
        await proc.wait()
        assert proc.returncode == 0
    assert Path(bot.COOKIES_FILE).read_text() == COOKIE
    assert not list(Path(bot.WORK_DIR).glob("vbot_process_*"))


@pytest.mark.asyncio
async def test_cancelled_process_is_reaped(bot):
    proc = None
    with pytest.raises(asyncio.CancelledError):
        async with bot.managed_process([sys.executable, "-c", "import time; time.sleep(60)"]) as proc:
            raise asyncio.CancelledError()
    assert proc.returncode is not None
    assert not list(Path(bot.WORK_DIR).glob("vbot_process_*"))


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["err_age", "err_private", "err_gone", "err_geo"])
async def test_access_failures_do_not_retry_quality(bot, reason):
    async def failure(*args):
        bot._FAIL_REASON.set(reason)
        return None, "blocked"
    bot.ytdlp_download = AsyncMock(side_effect=failure)
    assert await bot.try_ytdlp_send(None, message(), "https://www.tiktok.com/@u/video/1",
                                     [720, 480, 360], False) == ("fail", None)
    assert bot.ytdlp_download.await_count == 1


@pytest.mark.asyncio
async def test_history_is_private_and_restart_does_not_resend(bot):
    for uid, cid in [(42, 42), (43, 43), (42, -100)]:
        job = bot._new_job(message(uid, cid), "youtube", "video")
        job["url"] = "https://youtu.be/example"
        await bot.save_job(job)
    await bot.recover_jobs()
    own = await bot.job_history(42)
    assert len(own) == 1
    assert own[0]["status"] == "interrupted"
    assert len(await bot.job_history(777, admin=True)) == 3
    assert not bot._job_tasks


@pytest.mark.asyncio
async def test_process_persists_completion(bot):
    bot.url_is_safe = AsyncMock(return_value=True)
    bot.free_space = lambda: None
    bot._do_process = AsyncMock(return_value=("sent", "youtube"))
    result = await bot.process_url(None, message(), "https://youtu.be/example", True, False)
    assert result == ("sent", "youtube")
    assert (await bot.job_history(42))[0]["status"] == "done"
    for task in list(bot._background):
        task.cancel()
    await asyncio.gather(*bot._background, return_exceptions=True)


def test_migration_rolls_back_failed_step(bot):
    def broken(con):
        con.execute("CREATE TABLE should_not_exist(x)")
        raise RuntimeError("test failure")
    con = bot.db_conn()
    version = con.execute("PRAGMA user_version").fetchone()[0]
    bot.MIGRATIONS += (broken,)
    with pytest.raises(RuntimeError):
        bot.db_migrate(con)
    assert con.execute("PRAGMA user_version").fetchone()[0] == version
    assert not con.execute("SELECT name FROM sqlite_master WHERE name='should_not_exist'").fetchall()


def test_newer_schema_is_refused(bot):
    con = sqlite3.connect(":memory:")
    con.execute("PRAGMA user_version=999")
    with pytest.raises(RuntimeError):
        bot.db_migrate(con)
    con.close()


@pytest.mark.parametrize("data", [[], {"url": 12}, {"url": "x", "quality": -1},
                                  {"url": "x", "abr": 10000}, {"url": "x", "mode": "bad"}])
def test_download_schema_rejects_bad_input(bot, data):
    with pytest.raises(ValidationError):
        bot.DownloadRequest.model_validate(data)


def test_existing_ui_auto_values_remain_compatible(bot):
    data = bot.DownloadRequest.model_validate({"url": "https://youtu.be/test", "quality": "", "abr": ""})
    assert data.quality is None and data.abr is None


@pytest_asyncio.fixture
async def api(bot):
    bot.WEBAPP_ENABLED = True
    bot.verify_webapp_init_data = lambda token, _: {"id": int(token)} if token.isdigit() else None
    bot.resolve_access = lambda *args: "admin" if args[0] == 777 else "extended"
    with patch.object(bot.web, "AppRunner") as runner, patch.object(bot.web, "TCPSite") as site:
        runner.return_value.setup = AsyncMock()
        site.return_value.start = AsyncMock()
        await bot.start_web_server(SimpleNamespace(send_message=AsyncMock()))
        client = TestClient(TestServer(runner.call_args.args[0]))
    await client.start_server()
    yield client
    await client.close()
    pending = list(bot._background)
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)


@pytest.mark.asyncio
async def test_history_api_requires_auth_and_hides_other_users(bot, api):
    for uid in (42, 43):
        job = bot._new_job(message(uid), "youtube", "video")
        await bot.save_job(job)
    assert (await api.get("/api/history")).status == 403
    response = await api.get("/api/history", headers={"X-Telegram-Init-Data": "42"})
    body = await response.json()
    assert response.status == 200
    assert len(body["jobs"]) == 1
    assert body["jobs"][0]["user_id"] == 42
    response = await api.get("/api/history?before=nan", headers={"X-Telegram-Init-Data": "42"})
    assert response.status == 400


@pytest.mark.asyncio
async def test_cancel_is_owner_only_and_persists(bot, api):
    entered = asyncio.Event()
    async def slow(*args):
        entered.set()
        await asyncio.Event().wait()
    bot.url_is_safe = AsyncMock(return_value=True)
    bot.free_space = lambda: None
    bot._do_process = slow
    task = bot.spawn(bot.process_url(None, message(), "https://youtu.be/test", True, False))
    await asyncio.wait_for(entered.wait(), timeout=3)
    job = next(iter(bot._progress.values()))
    route = "/api/jobs/" + job["id"] + "/cancel"
    assert (await api.post(route, headers={"X-Telegram-Init-Data": "43"})).status == 404
    assert (await api.post(route, headers={"X-Telegram-Init-Data": "42"})).status == 202
    assert (await asyncio.wait_for(task, timeout=3))[0] == "cancelled"
    assert (await bot.job_history(42))[0]["status"] == "cancelled"


@pytest.mark.asyncio
async def test_download_api_rejects_non_object_json(api):
    for body in ([], None, 1, "text"):
        assert (await api.post("/api/download", json=body)).status == 400


@pytest.mark.asyncio
async def test_stale_progress_cannot_overwrite_completion(bot):
    job = bot._new_job(message(), "youtube", "video")
    stale = dict(job)
    job.update(status="done", updated=job["updated"] + 1)
    await bot.save_job(job)
    await bot.save_job(stale)
    assert (await bot.job_history(42))[0]["status"] == "done"


def test_failure_types_are_actionable(bot):
    assert not bot.failure_info("Login required").retryable
    assert bot.failure_info("HTTP Error 429").kind == "rate_limited"
    assert bot.failure_info("connection reset").retryable


def test_migration_backup_preserves_old_schema(bot):
    con = bot.db_conn()
    old = len(bot.MIGRATIONS) - 1
    con.execute("PRAGMA user_version=%d" % old)
    con.commit()
    bot.db_init()
    copy = sqlite3.connect(bot.STATS_DB + ".schema-v%d.bak" % old)
    assert copy.execute("PRAGMA user_version").fetchone()[0] == old
    copy.close()


@given(st.sampled_from(["nan", "inf", "-inf", "Infinity", "NaN"]))
def test_progress_rejects_non_finite_values(value):
    assert BOT._num(value) is None


@pytest.mark.parametrize("domain,key", [(".youtube.com", "SID"), (".tiktok.com", "sid_tt"),
                                        (".x.com", "auth_token"), (".reddit.com", "reddit_session")])
def test_cookie_services_are_recognized(bot, domain, key):
    ok, info = bot.parse_cookies_txt(COOKIE.replace(".instagram.com", domain).replace("sessionid", key))
    assert ok and info["services"]


def test_csrf_only_or_wrong_domain_is_not_login(bot):
    assert not bot.parse_cookies_txt(COOKIE.replace("sessionid", "csrftoken"))[0]
    assert not bot.parse_cookies_txt(COOKIE.replace(".instagram.com", ".evilinstagram.com"))[0]
    assert not bot.parse_cookies_txt(COOKIE.replace("2000000000", "garbage"))[0]


@pytest.mark.asyncio
async def test_full_queue_does_not_create_jobs(bot, api):
    bot.RUNTIME.user_queue_limit = 1
    bot._new_job(message(), "youtube", "video")
    response = await api.post("/api/download", json={"initData": "42", "url": "https://youtu.be/test"})
    assert response.status == 429
    assert len(bot._progress) == 1


@pytest.mark.asyncio
async def test_retry_only_failed_owned_jobs(bot, api):
    old = bot._new_job(message(), "youtube", "video")
    old.update(url="https://youtu.be/test", status="error")
    await bot.save_job(old)
    url = "/api/jobs/" + old["id"] + "/retry"
    assert (await api.post(url, headers={"X-Telegram-Init-Data": "43"})).status == 404
    bot.process_url = AsyncMock()
    response = await api.post(url, headers={"X-Telegram-Init-Data": "42"})
    assert response.status == 202
    body = await response.json()
    assert body["job_id"] != old["id"]
    assert (await api.post(url, headers={"X-Telegram-Init-Data": "42"})).status == 409


@pytest.mark.asyncio
async def test_batch_is_committed_before_acknowledgement(bot, api):
    bot.process_url = AsyncMock()
    response = await api.post("/api/download", json={"initData": "42", "url": "https://youtu.be/test",
                                                    "quality": "", "abr": ""})
    assert response.status == 200
    body = await response.json()
    rows = await bot.job_history(42)
    assert rows[0]["id"] == body["job_ids"][0]
    assert rows[0]["url"] == "https://youtu.be/test"


def test_openapi_matches_download_validation(bot):
    schema = bot.jobs_openapi()
    assert schema["components"]["schemas"]["DownloadRequest"]["additionalProperties"] is False
    assert "/api/jobs/{job_id}/retry" in schema["paths"]


@pytest.mark.asyncio
async def test_title_cache_is_bounded_and_invalidated_by_cookie_change(bot):
    bot.capture_process = AsyncMock(return_value=(b"Example title\n", None))
    assert await bot.fetch_title("https://youtu.be/test") == "Example title"
    assert await bot.fetch_title("https://youtu.be/test") == "Example title"
    assert bot.capture_process.await_count == 1
    bot.store_cookies(COOKIE)
    await bot.fetch_title("https://youtu.be/test")
    assert bot.capture_process.await_count == 2
    for i in range(260):
        await bot.fetch_title("https://youtu.be/test%d" % i)
    assert len(bot._title_cache) == 256


@pytest.mark.asyncio
async def test_failed_upload_is_retained_and_can_be_sent_without_download(bot):
    path = Path(bot.WORK_DIR) / "video.mp4"
    path.write_bytes(b"already downloaded")
    job = bot._new_job(message(), "youtube", "video")
    bot._JOB.set(job)
    with pytest.raises(TimeoutError):
        await bot.deliver_file(AsyncMock(side_effect=TimeoutError()), path, "video")
    assert job["delivery_uncertain"] is True
    kept = bot.retained_media_path(job["retained"])
    assert kept.read_bytes() == b"already downloaded"
    assert not path.exists()
    bot.send_video_with_meta = AsyncMock()
    assert await bot.send_retained(message(), job["retained"]) == "sent"
    bot.send_video_with_meta.assert_awaited_once()
    assert not kept.exists()


def test_retained_storage_limit_and_expiry(bot, monkeypatch):
    path = Path(bot.WORK_DIR) / "video.mp4"
    path.write_bytes(b"x" * (1024 * 1024 + 1))
    bot.RUNTIME.retention_max_mb = 1
    assert bot.retain_failed_upload(path, "video", "a" * 12) is None
    path.write_bytes(b"small")
    item = bot.retain_failed_upload(path, "video", "b" * 12)
    kept = bot.retained_media_path(item)
    monkeypatch.setattr(bot.time, "time", lambda: item["expires"] + 1)
    bot.clean_retained_media()
    assert not kept.exists()
    assert bot.retained_media_path({"name": "../cookies.txt", "expires": 1e12}) is None


@pytest.mark.asyncio
async def test_uncertain_delivery_needs_explicit_confirmation(bot, api):
    old = bot._new_job(message(), "youtube", "video")
    old.update(url="https://youtu.be/test", status="error", delivery_uncertain=True)
    await bot.save_job(old)
    url = "/api/jobs/" + old["id"] + "/retry"
    headers = {"X-Telegram-Init-Data": "42"}
    response = await api.post(url, headers=headers)
    assert response.status == 409
    assert (await response.json())["error"] == "confirm_duplicate_risk"
    bot.process_url = AsyncMock()
    assert (await api.post(url, headers=headers, json={"confirm_duplicate_risk": True})).status == 202


def test_concurrent_jobs_reserve_disk_headroom(bot):
    bot.RUNTIME.disk_reserve_mb = 64
    bot.free_space = lambda: bot.MIN_FREE_SPACE + 100 * 1024 * 1024
    assert bot.reserve_job_disk({"id": "first"})
    assert not bot.reserve_job_disk({"id": "second"})
    bot._disk_reservations.pop("first")
    assert bot.reserve_job_disk({"id": "second"})


@pytest.mark.asyncio
async def test_history_cursor_preserves_jobs_with_equal_timestamps(bot):
    for i in range(3):
        job = bot._new_job(message(), "youtube", "video")
        job.update(id=f"{i:012x}", ts=1700000000)
        await bot.save_job(job)
    first = await bot.job_history(42, limit=2)
    second = await bot.job_history(42, limit=2, before=first[-1]["ts"],
                                   before_id=first[-1]["id"])
    assert [j["id"] for j in first + second] == [f"{i:012x}" for i in (2, 1, 0)]
