from neo4j.exceptions import ServiceUnavailable

from tests.unit.test_models import make_event
from zeitgeist.extractor.rules import event_to_claims
from zeitgeist.graph.main import process_one, write_with_retry

CLAIM = event_to_claims(make_event())[0]


class FakeMessage:
    def __init__(self, value=None, error=None):
        self._value = value
        self._error = error

    def value(self):
        return self._value

    def error(self):
        return self._error


class FakeConsumer:
    def __init__(self):
        self.committed = []

    def commit(self, message=None, asynchronous=False):
        self.committed.append(message)


class FakeSession:
    def __init__(self, name, run=None):
        self.name = name
        self.closed = False
        self._run = run or (lambda *a, **k: None)

    def run(self, *args, **kwargs):
        return self._run(*args, **kwargs)

    def close(self):
        self.closed = True


class FakeDriver:
    """Fake driver whose .session() hands out a fresh FakeSession each call."""

    def __init__(self, run_factory):
        self.sessions_created = 0
        self._run_factory = run_factory

    def session(self):
        self.sessions_created += 1
        name = f"session-{self.sessions_created}"
        return FakeSession(name, run=self._run_factory(name))


def test_write_with_retry_retries_once_then_succeeds():
    calls = []

    def run_factory(name):
        def run(*args, **kwargs):
            calls.append(name)
            if len(calls) == 1:
                raise ServiceUnavailable("neo4j down")

        return run

    driver = FakeDriver(run_factory)
    old_session = FakeSession("session-0", run=run_factory("session-0"))
    sleeps = []

    result = write_with_retry(driver, old_session, CLAIM, sleep=sleeps.append)

    assert calls == ["session-0", "session-1"]
    assert old_session.closed is True
    assert result.name == "session-1"
    assert sleeps == [2]


def test_write_with_retry_propagates_non_retryable_exceptions():
    def run_factory(name):
        def run(*args, **kwargs):
            raise ValueError("bad claim")

        return run

    driver = FakeDriver(run_factory)
    session = FakeSession("session-0", run=run_factory("session-0"))

    try:
        write_with_retry(driver, session, CLAIM, sleep=lambda s: None)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError to propagate")


def test_write_with_retry_ignores_close_errors_on_old_session():
    calls = []

    def run_factory(name):
        def run(*args, **kwargs):
            calls.append(name)
            if len(calls) == 1:
                raise ServiceUnavailable("down")

        return run

    class BrokenCloseSession(FakeSession):
        def close(self):
            raise RuntimeError("close boom")

    driver = FakeDriver(run_factory)
    session = BrokenCloseSession("session-0", run=run_factory("session-0"))

    result = write_with_retry(driver, session, CLAIM, sleep=lambda s: None)
    assert result.name == "session-1"


def test_process_one_commits_poison_message_and_does_not_write():
    writes = []
    session = FakeSession("session-0", run=lambda *a, **k: writes.append(a))
    driver = FakeDriver(lambda name: lambda *a, **k: None)
    consumer = FakeConsumer()
    message = FakeMessage(value=b"not json at all", error=None)

    result = process_one(driver, session, consumer, message)

    assert result is session
    assert writes == []
    assert consumer.committed == [message]


def test_process_one_skips_and_does_not_commit_on_consumer_error():
    session = FakeSession("session-0")
    driver = FakeDriver(lambda name: lambda *a, **k: None)
    consumer = FakeConsumer()
    message = FakeMessage(value=None, error="broker down")

    result = process_one(driver, session, consumer, message)

    assert result is session
    assert consumer.committed == []


def test_process_one_writes_and_commits_valid_claim():
    session = FakeSession("session-0")
    driver = FakeDriver(lambda name: lambda *a, **k: None)
    consumer = FakeConsumer()
    message = FakeMessage(value=CLAIM.to_json(), error=None)

    result = process_one(driver, session, consumer, message)

    assert result is session
    assert consumer.committed == [message]
