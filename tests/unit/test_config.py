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


def test_sampler_defaults(monkeypatch):
    for var in ("SAMPLER_MIN_SCORE", "LLM_MAX_CALLS_PER_DAY", "SAMPLER_BUDGET_STATE_PATH"):
        monkeypatch.delenv(var, raising=False)
    settings = Settings.from_env()
    assert settings.sampler_min_score == 1.2
    assert settings.llm_max_calls_per_day == 400
    assert settings.sampler_budget_state_path == "state/sampler_budget.json"


def test_sampler_env_overrides(monkeypatch):
    monkeypatch.setenv("SAMPLER_MIN_SCORE", "0.8")
    monkeypatch.setenv("LLM_MAX_CALLS_PER_DAY", "50")
    monkeypatch.setenv("SAMPLER_BUDGET_STATE_PATH", "state/custom_budget.json")
    settings = Settings.from_env()
    assert settings.sampler_min_score == 0.8
    assert settings.llm_max_calls_per_day == 50
    assert settings.sampler_budget_state_path == "state/custom_budget.json"
