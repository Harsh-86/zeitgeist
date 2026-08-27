import { useEffect, useRef, useState } from 'react'

interface WireClaim {
  detail: string | null
  tier?: string
}

const TICKER_SIZE = 14
const MAX_SENTENCE = 180

const clean = (detail: string): string => {
  const text = detail.trim()
  return text.length > MAX_SENTENCE ? `${text.slice(0, MAX_SENTENCE - 1)}…` : text
}

/**
 * The AI wire: one-sentence facts extracted by the LLM tier from real
 * articles, scrolling across the hero's bottom edge like a newsroom wire.
 * Seeds from /recent?tier=llm, then live llm-tier websocket claims queue in
 * a buffer flushed only when the marquee loop completes — no mid-scroll jumps.
 */
export default function Ticker() {
  const [items, setItems] = useState<string[]>([])
  const bufferRef = useRef<string[]>([])

  useEffect(() => {
    let cancelled = false

    fetch(`/recent?limit=${TICKER_SIZE * 2}&tier=llm`)
      .then((res) => res.json())
      .then((body: { claims: WireClaim[] }) => {
        if (cancelled) return
        const sentences = body.claims
          .map((c) => c.detail)
          .filter((d): d is string => Boolean(d && d.trim()))
          .map(clean)
        setItems([...new Set(sentences)].slice(0, TICKER_SIZE))
      })
      .catch((err) => console.error('wire preload failed', err))

    const proto = location.protocol === 'https:' ? 'wss' : 'ws'
    const ws = new WebSocket(`${proto}://${location.host}/ws/claims`)
    ws.onmessage = (msg) => {
      try {
        const claim = JSON.parse(msg.data) as WireClaim
        if (claim.tier === 'llm' && claim.detail?.trim()) {
          bufferRef.current.push(clean(claim.detail))
        }
      } catch {
        /* malformed frame: ignore */
      }
    }

    return () => {
      cancelled = true
      ws.close()
    }
  }, [])

  const flushBuffer = () => {
    if (bufferRef.current.length === 0) return
    setItems((prev) =>
      [...new Set([...bufferRef.current.reverse(), ...prev])].slice(0, TICKER_SIZE)
    )
    bufferRef.current = []
  }

  if (items.length === 0) return null

  return (
    <div className="ticker" aria-hidden="true">
      <span className="ticker-tag">ai wire</span>
      <div className="ticker-viewport">
        <div className="ticker-track" onAnimationIteration={flushBuffer}>
          {[0, 1].map((copy) => (
            <div className="ticker-run" key={copy}>
              {items.map((sentence, i) => (
                <span className="ticker-item" key={`${copy}-${i}`}>
                  {sentence}
                </span>
              ))}
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}
