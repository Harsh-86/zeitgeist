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


def test_llm_defaults(monkeypatch):
    for var in ("ANTHROPIC_API_KEY", "LLM_MODEL"):
        monkeypatch.delenv(var, raising=False)
    settings = Settings.from_env()
    assert settings.anthropic_api_key == ""
    assert settings.llm_model == "claude-haiku-4-5"


def test_llm_env_overrides(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("LLM_MODEL", "claude-opus-5")
    settings = Settings.from_env()
    assert settings.anthropic_api_key == "sk-ant-test"
    assert settings.llm_model == "claude-opus-5"


def test_llm_budget_state_path_default(monkeypatch):
    monkeypatch.delenv("LLM_BUDGET_STATE_PATH", raising=False)
    settings = Settings.from_env()
    assert settings.llm_budget_state_path == "state/llm_budget.json"


def test_llm_budget_state_path_env_override(monkeypatch):
    monkeypatch.setenv("LLM_BUDGET_STATE_PATH", "state/custom_llm_budget.json")
    settings = Settings.from_env()
    assert settings.llm_budget_state_path == "state/custom_llm_budget.json"
