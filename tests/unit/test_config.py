from zeitgeist.config import Settings


def test_defaults(monkeypatch):
    for var in ("KAFKA_BOOTSTRAP", "NEO4J_URI", "NEO4J_USER", "NEO4J_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    settings = Settings.from_env()
    assert settings.kafka_bootstrap == "localhost:9092"
    assert settings.neo4j_uri == "bolt://localhost:7687"
    assert settings.poll_interval_seconds == 60


def test_env_overrides(monkeypatch):
    monkeypatch.setenv("KAFKA_BOOTSTRAP", "kafka:9092")
    monkeypatch.setenv("POLL_INTERVAL_SECONDS", "15")
    settings = Settings.from_env()
    assert settings.kafka_bootstrap == "kafka:9092"
    assert settings.poll_interval_seconds == 15
