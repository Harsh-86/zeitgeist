import { useEffect, useRef, useState } from 'react'
import LiveGraph from './LiveGraph'

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

  return <span className="stat">{shown.toLocaleString('en-US')}</span>
}

export default function Hero() {
  const stats = useStats()

  return (
    <section className="hero">
      <LiveGraph className="hero-graph" />
      <div className="hero-overlay">
        <h1 className="hero-title">zeitgeist</h1>
        <p className="hero-headline">Watch the world&apos;s news become a knowledge graph — live</p>
        <div className="hero-counters">
          <div>
            entities: {stats ? <CountUp value={stats.entities} /> : <span className="stat">–</span>}
          </div>
          <div>
            events: {stats ? <CountUp value={stats.events} /> : <span className="stat">–</span>}
          </div>
        </div>
      </div>
      <div className="scroll-hint">scroll to explore ↓</div>
    </section>
  )
}
