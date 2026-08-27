import { useEffect, useRef, useState } from 'react'
import ForceGraph2D from 'react-force-graph-2d'
import type { ForceGraphMethods } from 'react-force-graph-2d'
import { heatOf, sizeOf, useGraphData } from '../hooks/useGraphData'
import type { GraphLink, GraphNode } from '../hooks/useGraphData'

interface Props {
  /** null (default) = LIVE mode; an ISO timestamp = REPLAY snapshot. */
  until?: string | null
  className?: string
  /**
   * Decorative background mode: no zoom/pan/drag/hover, no labels ever.
   * Without this the hero's full-viewport canvas eats the mouse wheel, so
   * "scrolling" zooms the graph — and crossing the zoom threshold labels
   * every node. A backdrop must never capture input.
   */
  ambient?: boolean
}

/** The force graph, sized to fill its container. */
export default function LiveGraph({ until = null, className, ambient = false }: Props) {
  const { data, maxVal } = useGraphData(until)
  const containerRef = useRef<HTMLDivElement>(null)
  const graphRef = useRef<ForceGraphMethods<GraphNode, GraphLink> | undefined>(undefined)
  const [size, setSize] = useState({ width: 0, height: 0 })

  // Ambient mode fills its container instead of leaving a centered blob:
  // once the simulation settles, zoom so the whole constellation spans the
  // viewport (slight overshoot past the edges reads as atmosphere).
  const fillViewport = () => {
    if (!ambient) return
    graphRef.current?.zoomToFit(600, -40)
  }

  useEffect(() => {
    const el = containerRef.current
    if (!el) return
    const measure = () => setSize({ width: el.clientWidth, height: el.clientHeight })
    measure()
    const observer = new ResizeObserver(measure)
    observer.observe(el)
    return () => observer.disconnect()
  }, [])

  return (
    <div ref={containerRef} className={className} style={ambient ? { pointerEvents: 'none' } : undefined}>
      {size.width > 0 && size.height > 0 && (
        <ForceGraph2D<GraphNode, GraphLink>
          ref={graphRef}
          onEngineStop={fillViewport}
          width={size.width}
          height={size.height}
          graphData={data}
          backgroundColor={ambient ? 'rgba(0,0,0,0)' : '#0b0e14'}
          linkDirectionalParticles={ambient ? 2 : 0}
          linkDirectionalParticleSpeed={0.0035}
          linkDirectionalParticleWidth={2.2}
          linkDirectionalParticleColor={() => '#7fd4a3'}
          enableZoomInteraction={!ambient}
          enablePanInteraction={!ambient}
          enableNodeDrag={!ambient}
          nodeLabel={ambient ? undefined : (node) => `${node.id} (${node.val} events)`}
          nodeVal={(node) => sizeOf(node.val)}
          nodeColor={(node) => heatOf(node.val, maxVal)}
          // Permanent labels on the busy hubs; label everything when zoomed
          // in. Labeling 100s of nodes at once is an unreadable word cloud.
          nodeCanvasObjectMode={() => 'after'}
          nodeCanvasObject={(node, ctx, globalScale) => {
            if (ambient) {
              // Soft halo on the hottest nodes: the hero's own lighting rig.
              if (node.val / maxVal < 0.35) return
              const r = Math.sqrt(sizeOf(node.val)) * 4
              const glow = ctx.createRadialGradient(
                node.x ?? 0, node.y ?? 0, r, node.x ?? 0, node.y ?? 0, r * 3.2
              )
              glow.addColorStop(0, 'rgba(127, 212, 163, 0.4)')
              glow.addColorStop(1, 'rgba(127, 212, 163, 0)')
              ctx.fillStyle = glow
              ctx.beginPath()
              ctx.arc(node.x ?? 0, node.y ?? 0, r * 3.2, 0, 2 * Math.PI)
              ctx.fill()
              return
            }
            const labelThreshold = globalScale >= 2.5 ? 1 : 25
            if (node.val < labelThreshold) return
            const radius = Math.sqrt(sizeOf(node.val)) * 4 // mirrors force-graph sizing
            const fontSize = Math.max(12 / globalScale, 2)
            ctx.font = `${fontSize}px ui-monospace, monospace`
            ctx.textAlign = 'center'
            ctx.textBaseline = 'top'
            ctx.fillStyle = 'rgba(230, 230, 230, 0.9)'
            ctx.fillText(node.id, node.x ?? 0, (node.y ?? 0) + radius + 1)
          }}
          linkColor={() => '#3a4a5a'}
        />
      )}
    </div>
  )
}
