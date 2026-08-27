import { useEffect, useRef, useState } from 'react'
import LiveGraph from './LiveGraph'

const HOURS = 7 * 24
const HOUR_MS = 3_600_000
const DEBOUNCE_MS = 300
// Snapshot "now" once (page load == explorer mount) so the hour grid stays stable.
const MOUNTED_AT = Date.now()

export default function GraphExplorer() {
  const [sliderHour, setSliderHour] = useState(HOURS)
  const [until, setUntil] = useState<string | null>(null) // null = LIVE
  const [liveEpoch, setLiveEpoch] = useState(0)
  const debounceRef = useRef<number | undefined>(undefined)

  const onSlide = (hour: number) => {
    setSliderHour(hour)
    window.clearTimeout(debounceRef.current)
    debounceRef.current = window.setTimeout(() => {
      setUntil(new Date(MOUNTED_AT - (HOURS - hour) * HOUR_MS).toISOString())
    }, DEBOUNCE_MS)
  }

  const goLive = () => {
    window.clearTimeout(debounceRef.current)
    setSliderHour(HOURS)
    setUntil(null)
    // Remount the graph so the /recent preload + websocket re-init runs cleanly.
    setLiveEpoch((n) => n + 1)
  }

  useEffect(() => () => window.clearTimeout(debounceRef.current), [])

  const label =
    until === null ? 'LIVE — streaming new claims' : `as of ${new Date(until).toUTCString()}`

  return (
    <section className="section">
      <h2>Graph explorer</h2>
      <p className="explorer-hint">
        Drag the slider to replay the last 7 days hour by hour. Hover a node for its event count.
      </p>
      <LiveGraph key={`graph-${liveEpoch}`} until={until} className="explorer-graph" />
      <div className="explorer-controls">
        <button
          type="button"
          className={until === null ? 'live-button active' : 'live-button'}
          onClick={goLive}
        >
          LIVE
        </button>
        <input
          type="range"
          min={0}
          max={HOURS}
          step={1}
          value={sliderHour}
          onChange={(e) => onSlide(Number(e.target.value))}
          aria-label="replay the graph as of a past hour"
        />
        <span className="explorer-label">{label}</span>
      </div>
    </section>
  )
}
