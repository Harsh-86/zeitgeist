import { useEffect, useRef, useState } from 'react'
import LiveGraph from './LiveGraph'
import Ticker from './Ticker'

interface Stats {
  entities: number
  events: number
}

function useStats(): Stats | null {
  const [stats, setStats] = useState<Stats | null>(null)

  useEffect(() => {
    let cancelled = false
    const refresh = () =>
      fetch('/stats')
        .then((res) => res.json())
        .then((body: Stats) => {
          if (!cancelled) setStats(body)
        })
        .catch((err) => console.error('failed to fetch /stats', err))
    refresh()
    const timer = setInterval(refresh, 10_000)
    return () => {
      cancelled = true
      clearInterval(timer)
    }
  }, [])

  return stats
}

/** Counts up from 0 on first load (~1.5s, ease-out); later refreshes just swap the number. */
function CountUp({ value }: { value: number }) {
  const [shown, setShown] = useState(0)
  const animatedOnce = useRef(false)

  useEffect(() => {
    if (animatedOnce.current) {
      setShown(value)
      return
    }
    animatedOnce.current = true
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
      setShown(value)
      return
    }
    const started = performance.now()
    const duration = 1500
    let frame = 0
    const tick = (now: number) => {
      const t = Math.min((now - started) / duration, 1)
      const eased = 1 - (1 - t) ** 3 // ease-out cubic
      setShown(Math.round(value * eased))
      if (t < 1) frame = requestAnimationFrame(tick)
    }
    frame = requestAnimationFrame(tick)
    return () => cancelAnimationFrame(frame)
  }, [value])

  return <>{shown.toLocaleString('en-US')}</>
}

export default function Hero() {
  const stats = useStats()

  return (
    <section className="hero">
      <LiveGraph className="hero-graph" ambient />
      <div className="hero-scrim" />
      <div className="hero-overlay">
        <div className="hero-block">
          <p className="eyebrow">Live from the GDELT firehose · every 15 minutes</p>
          <h1 className="hero-title">zeitgeist</h1>
          <p className="hero-headline">The world&apos;s news, becoming a knowledge graph.</p>
          <div className="hero-stats">
            <div className="hero-stat">
              <span className="hero-stat-value">
                {stats ? <CountUp value={stats.events} /> : '—'}
              </span>
              <span className="hero-stat-label">events on the board</span>
            </div>
            <div className="hero-stat">
              <span className="hero-stat-value">
                {stats ? <CountUp value={stats.entities} /> : '—'}
              </span>
              <span className="hero-stat-label">entities</span>
            </div>
            <div className="live-badge">
              <span className="live-dot" />
              live
            </div>
          </div>
        </div>
      </div>
      <Ticker />
    </section>
  )
}
