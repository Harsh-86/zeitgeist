"""Resolver service: periodically screens entities for genericness and judges
same-entity candidate pairs, writing ALIAS_OF / ER_JUDGED edges into the graph.

Canonical processing order per cycle (see run_cycle):
  1. Generic screening: every never-screened entity is spent-budget-gated,
     screened by the judge, then marked (unless the judge's response failed
     to parse, in which case it is retried next cycle -- the budget spend is
     never refunded).
  2. Pair judging: candidate same-entity pairs (minus pairs already judged)
     are spent-budget-gated, judged, and always recorded; a SAME verdict at
     or above cfg.er_min_confidence additionally writes an ALIAS_OF edge.
  3. The cycle stops instantly the moment the budget is exhausted -- no
     further reads or judge calls are made for the remainder of the cycle.
"""

import logging
import sys
import time
from pathlib import Path

import anthropic
from neo4j import GraphDatabase

from zeitgeist.budget import DailyBudget
from zeitgeist.config import Settings
from zeitgeist.graph.main import RETRYABLE, _run_with_retry
from zeitgeist.resolver import candidates, graph
from zeitgeist.resolver.judge import ErJudge

logger = logging.getLogger("zeitgeist.resolver")

_SAMPLE_RELATIONS_LIMIT = 5


def run_cycle(session, judge: ErJudge, budget: DailyBudget, cfg) -> dict:
    """Run one resolver cycle: screen unscreened entities, then judge
    candidate pairs, stopping instantly once the daily budget is exhausted.

    Returns a counters dict: screened/judged/aliased/skipped/budget_left.
    """
    screened = 0
    judged = 0
    aliased = 0
    skipped = 0
    budget_exhausted = False

    unscreened = graph.fetch_unscreened_entities(session, min_events=cfg.er_min_events)
    for name, _count in unscreened:
        if not budget.try_spend():
            budget_exhausted = True
            break

        relations = graph.fetch_sample_relations(session, name, limit=_SAMPLE_RELATIONS_LIMIT)
        verdict, _usage = judge.screen_generic(name, relations)
        if verdict is None:
            skipped += 1
            continue

        graph.mark_generic(session, name, verdict.generic, verdict.confidence)
        screened += 1

    if not budget_exhausted:
        entities = graph.fetch_entities(session, min_events=cfg.er_min_events)
        already_judged = graph.fetch_judged_pairs(session)
        pairs = candidates.candidate_pairs(entities)

        for pair in pairs:
            if frozenset((pair[0], pair[1])) in already_judged:
                continue

            if not budget.try_spend():
                budget_exhausted = True
                break

            a_relations = graph.fetch_sample_relations(
                session, pair[0], limit=_SAMPLE_RELATIONS_LIMIT
            )
            b_relations = graph.fetch_sample_relations(
                session, pair[1], limit=_SAMPLE_RELATIONS_LIMIT
            )
            verdict, _usage = judge.judge_pair(pair[0], pair[1], a_relations, b_relations)
            if verdict is None:
                skipped += 1
                continue

            graph.record_judgment(
                session,
                a=pair[0],
                b=pair[1],
                verdict=verdict.verdict,
                confidence=verdict.confidence,
            )
            judged += 1

            if verdict.verdict == "SAME" and verdict.confidence >= cfg.er_min_confidence:
                graph.write_alias(session, alias_name=pair[0], canonical_name=pair[1])
                aliased += 1

    counters = {
        "screened": screened,
        "judged": judged,
        "aliased": aliased,
        "skipped": skipped,
        "budget_left": not budget_exhausted,
    }
    logger.info(
        "resolver cycle: screened=%d judged=%d aliased=%d skipped=%d budget_left=%s",
        screened,
        judged,
        aliased,
        skipped,
        counters["budget_left"],
    )
    return counters


def run_cycle_with_retry(driver, session, judge, budget, cfg, sleep=time.sleep):
    """Run one resolver cycle, retrying in place on transient Neo4j errors.

    Mirrors zeitgeist.graph.main's retry-in-place pattern: on a RETRYABLE
    exception, log a warning, sleep, close the old session (ignoring close
    errors), open a fresh session from driver, and retry the *entire* cycle
    from scratch. Budget spends already made before the failure are not
    refunded (DailyBudget's accounting is honest, not exact, under failure).

    Returns (session, counters) -- session is the (possibly recreated) one
    that ultimately succeeded.
    """
    while True:
        try:
            counters = run_cycle(session, judge, budget, cfg)
            return session, counters
        except RETRYABLE as exc:
            logger.warning("neo4j unavailable (%s); retrying cycle in 2s", exc)
            sleep(2)
            try:
                session.close()
            except Exception:
                logger.debug("session close failed during retry", exc_info=True)
            session = driver.session()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    settings = Settings.from_env()

    if not settings.anthropic_api_key:
        logger.error("ANTHROPIC_API_KEY not set; exiting")
        sys.exit(1)

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    judge = ErJudge(client, settings.llm_model)

    driver = GraphDatabase.driver(
        settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password)
    )
    session = driver.session()
    session = _run_with_retry(driver, session, graph.ensure_schema)

    budget = DailyBudget(Path(settings.er_budget_state_path), settings.er_max_calls_per_day)

    logger.info("resolver starting (interval=%ds)", settings.resolver_interval_seconds)
    while True:
        try:
            session, _counters = run_cycle_with_retry(driver, session, judge, budget, settings)
        except Exception:
            logger.exception("resolver cycle failed; continuing after interval")
        time.sleep(settings.resolver_interval_seconds)


if __name__ == "__main__":
    main()
