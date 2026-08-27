const CARDS = [
  {
    icon: '📡',
    title: 'GDELT firehose',
    body: 'Machine-read world news — ~130,000 events a day, a fresh batch every 15 minutes.',
  },
  {
    icon: '🚚',
    title: 'Self-hosted Kafka',
    body: 'Durable belts between every stage: at-least-once delivery + idempotent writes = effectively-once.',
  },
  {
    icon: '🧠',
    title: 'LLM enrichment tier',
    body: 'claude-haiku on a hard daily budget — $0.003 per enriched claim, measured.',
  },
  {
    icon: '🕸️',
    title: 'Temporal Neo4j graph',
    body: '317,000+ events, never overwritten. Entity resolution via reversible aliases.',
  },
]

export default function HowItWorks() {
  return (
    <section className="section">
      <h2>How it works</h2>
      <div className="cards">
        {CARDS.map((card) => (
          <article className="card" key={card.title}>
            <div className="card-icon" aria-hidden="true">
              {card.icon}
            </div>
            <h3>{card.title}</h3>
            <p>{card.body}</p>
          </article>
        ))}
      </div>
      <p className="how-footer">Total bill: ~$24/month including the AI.</p>
    </section>
  )
}
