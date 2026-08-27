const STEPS = [
  {
    number: '01',
    label: 'ingest',
    hue: 170,
    title: 'GDELT firehose',
    body: 'Machine-read world news: ~130,000 events a day, a fresh batch every 15 minutes.',
  },
  {
    number: '02',
    label: 'stream',
    hue: 120,
    title: 'Self-hosted Kafka',
    body: 'Durable belts between every stage. At-least-once delivery + idempotent writes = effectively-once.',
  },
  {
    number: '03',
    label: 'enrich',
    hue: 55,
    title: 'Claude, on an allowance',
    body: 'A paced daily budget — $0.003 per enriched claim, measured. The bill cannot surprise.',
  },
  {
    number: '04',
    label: 'remember',
    hue: 15,
    title: 'Temporal Neo4j graph',
    body: 'Every event timestamped, none overwritten — the graph contains its own history. Aliases, never merges.',
  },
]

export default function HowItWorks() {
  return (
    <section className="section">
      <p className="eyebrow">The pipeline</p>
      <h2>How it works</h2>
      <div className="steps">
        {STEPS.map((step) => (
          <article
            className="step"
            key={step.number}
            style={{ '--step-hue': step.hue } as React.CSSProperties}
          >
            <div className="step-head">
              <span className="step-number">{step.number}</span>
              <span className="step-label">{step.label}</span>
            </div>
            <h3>{step.title}</h3>
            <p>{step.body}</p>
          </article>
        ))}
      </div>
      <p className="how-footer">Runs on a €6 server in Helsinki. ~$24/month all-in, AI included.</p>
    </section>
  )
}
