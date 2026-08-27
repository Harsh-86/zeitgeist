import { useEffect, useState } from 'react'

export interface GraphNode {
  id: string
  val: number
}

export interface GraphLink {
  source: string
  target: string
  relation: string
}

export interface GraphData {
  nodes: GraphNode[]
  links: GraphLink[]
}

interface Claim {
  subject: string
  relation: string
  object: string | null
}

/** Browser sanity cap: stop growing the graph once it holds this many nodes. */
const NODE_CAP = 800

/**
 * Log scale tames the mega-hubs: a 300-event hub reads "clearly biggest,"
 * not "planet that swallows the map." Color carries intensity instead.
 */
export const sizeOf = (val: number): number => 1 + 3 * Math.log2(1 + val)

/** Heat ramp by activity: cool teal (quiet) -> green -> yellow -> hot red. */
export const heatOf = (val: number, maxVal: number): string => {
  const t = Math.log2(1 + val) / Math.log2(1 + maxVal)
  return `hsl(${Math.round(170 - 170 * t)}, 75%, ${Math.round(52 + 10 * t)}%)`
}

/**
 * Graph state + data plumbing.
 *
 * `until === null` is LIVE mode: preload `/recent` (reversed, so oldest
 * claims land first) and then append claims streaming over the websocket.
 * A non-null `until` is REPLAY mode: a single `/recent?until=<ISO>` snapshot,
 * and no websocket is opened.
 */
export function useGraphData(until: string | null): { data: GraphData; maxVal: number } {
  const [data, setData] = useState<GraphData>({ nodes: [], links: [] })
  const [maxVal, setMaxVal] = useState(1)

  useEffect(() => {
    let cancelled = false
    let ws: WebSocket | null = null
    const nodes = new Map<string, GraphNode>()
    const links: GraphLink[] = []
    let max = 1

    const ensureNode = (name: string) => {
      let node = nodes.get(name)
      if (!node) {
        // Browser sanity cap: past it, known entities keep heating up but no
        // new nodes (or their links) are added.
        if (nodes.size >= NODE_CAP) return null
        node = { id: name, val: 0 }
        nodes.set(name, node)
      }
      node.val += 1
      if (node.val > max) max = node.val
      return node
    }

    const addClaim = (claim: Claim) => {
      const subject = ensureNode(claim.subject)
      if (claim.object) {
        const object = ensureNode(claim.object)
        if (subject && object) {
          links.push({ source: claim.subject, target: claim.object, relation: claim.relation })
        }
      }
    }

    const commit = () => {
      if (cancelled) return
      // Fresh arrays (same node objects) so React re-renders while the force
      // simulation keeps the positions it already computed.
      setData({ nodes: [...nodes.values()], links: [...links] })
      setMaxVal(max)
    }

    const url = until === null ? '/recent' : `/recent?until=${encodeURIComponent(until)}`
    fetch(url)
      .then((res) => res.json())
      .then((body: { claims: Claim[] }) => {
        if (cancelled) return
        for (const claim of [...body.claims].reverse()) addClaim(claim)
        commit()
      })
      .catch((err) => console.error('failed to preload /recent', err))
      .finally(() => {
        if (cancelled || until !== null) return // replay: no live stream
        const proto = location.protocol === 'https:' ? 'wss' : 'ws'
        ws = new WebSocket(`${proto}://${location.host}/ws/claims`)
        ws.onmessage = (msg) => {
          addClaim(JSON.parse(msg.data) as Claim)
          commit()
        }
      })

    return () => {
      cancelled = true
      ws?.close()
    }
  }, [until])

  return { data, maxVal }
}
