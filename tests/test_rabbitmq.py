"""Opt-in real RabbitMQ checks in a disposable vhost, without SMTP/model calls."""

from __future__ import annotations

import base64
import json
import os
import threading
import time
import uuid
from urllib.parse import quote, unquote, urlsplit, urlunsplit
from urllib.request import Request, urlopen

import pytest
from celery import Celery
from celery.contrib.testing.worker import start_worker
from celery.exceptions import Reject
from kombu import Queue

from deep_data_research_agent.workers.app import celery_app

pytestmark = pytest.mark.integration


def _wait_for(check, timeout=12):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(0.1)
    raise AssertionError("RabbitMQ did not reach the expected state")


@pytest.fixture
def rabbit_app():
    if os.environ.get("RUN_RABBITMQ_INTEGRATION") != "1":
        pytest.skip("Set RUN_RABBITMQ_INTEGRATION=1 for real RabbitMQ checks")
    parsed = urlsplit(celery_app.conf.broker_url)
    credentials = f"{unquote(parsed.username or '')}:{unquote(parsed.password or '')}"
    token = base64.b64encode(credentials.encode()).decode()
    management_url = os.environ.get(
        "TEST_RABBITMQ_MANAGEMENT_URL", "http://127.0.0.1:15672"
    ).rstrip("/")
    vhost = f"ddra_test_{uuid.uuid4().hex}"

    def api(path, method="GET", body=None):
        request = Request(
            f"{management_url}/api/{path}",
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": f"Basic {token}", "Content-Type": "application/json"},
            method=method,
        )
        with urlopen(request, timeout=5) as response:
            content = response.read()
            return json.loads(content) if content else None

    app = Celery("rabbitmq-tests")
    app.conf.update(
        dict(celery_app.conf),
        broker_url=urlunsplit(parsed._replace(path=f"/{vhost}")),
        include=(),
        imports=(),
    )
    created = False
    try:
        api(f"vhosts/{vhost}", "PUT", {})
        created = True
        api(
            f"permissions/{vhost}/{quote(unquote(parsed.username or ''), safe='')}",
            "PUT",
            {"configure": ".*", "write": ".*", "read": ".*"},
        )
        yield app, lambda name: api(f"queues/{vhost}/{name}")
    finally:
        app.close()
        celery_app.set_current()
        celery_app.set_default()
        # Remove only the unique namespace created by this test, never application data.
        assert vhost.startswith("ddra_test_")
        if created:
            api(f"vhosts/{vhost}", "DELETE")


def test_rabbitmq_ack_follows_task_completion(rabbit_app) -> None:
    app, queue_info = rabbit_app
    started = threading.Event()
    release = threading.Event()

    @app.task(name="ddra.test.block", shared=False, acks_late=True)
    def block(job_id):
        assert job_id == "test-job"
        started.set()
        assert release.wait(25)

    with start_worker(app, pool="solo", perform_ping_check=False):
        try:
            app.send_task(block.name, args=["test-job"], queue="memory", retry=False)
            assert started.wait(10)
            _wait_for(lambda: queue_info("memory").get("messages_unacknowledged") == 1)
        finally:
            release.set()
        _wait_for(lambda: queue_info("memory").get("messages") == 0)
        assert queue_info("memory.dead").get("messages", 0) == 0


@pytest.mark.parametrize("queue_name", ["memory", "mail", "maintenance"])
@pytest.mark.parametrize("explicit_reject", [True, False])
def test_rabbitmq_routes_rejected_tasks_to_matching_dead_queue(
    rabbit_app, queue_name, explicit_reject
) -> None:
    app, _queue_info = rabbit_app

    @app.task(name="ddra.test.fail", shared=False, acks_late=True)
    def fail(job_id):
        if explicit_reject:
            raise Reject("terminal failure", requeue=False)
        raise ValueError("unhandled failure")

    with start_worker(app, pool="solo", perform_ping_check=False):
        # A fresh vhost has no dead exchange/queues: producer declarations must create them.
        app.send_task(fail.name, args=["test-job"], queue=queue_name, retry=False)
        with app.connection_for_read() as connection:
            dead_queue = Queue(f"{queue_name}.dead")(connection.default_channel)
            message = _wait_for(lambda: dead_queue.get(accept=["json"]))
            try:
                assert message.payload[0] == ["test-job"]
                assert message.properties["delivery_mode"] == 2
                death = message.headers["x-death"][0]
                assert death["queue"] == queue_name
                assert death["reason"] == "rejected"
                for other in {"memory", "mail", "maintenance"} - {queue_name}:
                    assert Queue(f"{other}.dead")(connection.default_channel).get() is None
            finally:
                message.ack()


def test_rabbitmq_safe_retry_does_not_dead_letter(rabbit_app) -> None:
    app, queue_info = rabbit_app
    attempts = []
    completed = threading.Event()

    @app.task(name="ddra.test.retry", shared=False, bind=True, acks_late=True)
    def retry_once(self, job_id):
        attempts.append(self.request.retries)
        if self.request.retries == 0:
            raise self.retry(countdown=0.1, max_retries=1)
        assert job_id == "test-job"
        completed.set()

    with start_worker(app, pool="solo", perform_ping_check=False):
        app.send_task(retry_once.name, args=["test-job"], queue="mail", retry=False)
        assert completed.wait(10)
        _wait_for(lambda: queue_info("mail").get("messages") == 0)
        assert attempts == [0, 1]
        with app.connection_for_read() as connection:
            assert Queue("mail.dead")(connection.default_channel).get() is None
