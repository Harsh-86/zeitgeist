"""Environment-driven settings shared by all services."""

import os
from dataclasses import dataclass

RAW_TOPIC = "raw.events"
CLAIMS_TOPIC = "extracted.claims"
LLM_TOPIC = "llm.queue"


@dataclass(frozen=True)
class Settings:
    kafka_bootstrap: str
    neo4j_uri: str
    neo4j_user: str
    neo4j_password: str
    poll_interval_seconds: int
    state_path: str
    sampler_min_score: float
    llm_max_calls_per_day: int
    sampler_budget_state_path: str
    llm_budget_state_path: str
    anthropic_api_key: str
    llm_model: str
    er_max_calls_per_day: int
    er_budget_state_path: str
    resolver_interval_seconds: int
    er_min_confidence: float
    er_min_events: int
    metrics_port: int
    ask_token: str
    ask_max_calls_per_day: int
    ask_budget_state_path: str

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            kafka_bootstrap=os.getenv("KAFKA_BOOTSTRAP", "localhost:9092"),
            neo4j_uri=os.getenv("NEO4J_URI", "bolt://localhost:7687"),
            neo4j_user=os.getenv("NEO4J_USER", "neo4j"),
            neo4j_password=os.getenv("NEO4J_PASSWORD", "zeitgeist-dev"),
            poll_interval_seconds=int(os.getenv("POLL_INTERVAL_SECONDS", "60")),
            state_path=os.getenv("STATE_PATH", "state/ingestor.json"),
            sampler_min_score=float(os.getenv("SAMPLER_MIN_SCORE", "1.2")),
            llm_max_calls_per_day=int(os.getenv("LLM_MAX_CALLS_PER_DAY", "400")),
            sampler_budget_state_path=os.getenv(
                "SAMPLER_BUDGET_STATE_PATH", "state/sampler_budget.json"
            ),
            llm_budget_state_path=os.getenv(
                "LLM_BUDGET_STATE_PATH", "state/llm_budget.json"
            ),
            anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
            llm_model=os.getenv("LLM_MODEL", "claude-haiku-4-5"),
            er_max_calls_per_day=int(os.getenv("ER_MAX_CALLS_PER_DAY", "100")),
            er_budget_state_path=os.getenv("ER_BUDGET_STATE_PATH", "state/er_budget.json"),
            resolver_interval_seconds=int(os.getenv("RESOLVER_INTERVAL_SECONDS", "3600")),
            er_min_confidence=float(os.getenv("ER_MIN_CONFIDENCE", "0.8")),
            er_min_events=int(os.getenv("ER_MIN_EVENTS", "3")),
            metrics_port=int(os.getenv("METRICS_PORT", "0")),
            ask_token=os.getenv("ASK_TOKEN", ""),
            ask_max_calls_per_day=int(os.getenv("ASK_MAX_CALLS_PER_DAY", "50")),
            ask_budget_state_path=os.getenv("ASK_BUDGET_STATE_PATH", "state/ask_budget.json"),
        )
