import { useMemo } from "react"

import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import type { FileHotspot } from "@/lib/api"

// Single-hue sequential ramp (light → dark orange): magnitude of the hotspot
// score. Fixed across themes so a cell reads the same in light and dark; the
// text colour flips at the midpoint for contrast.
const HOTSPOT_RAMP = ["#fde7d2", "#fbc28f", "#f59446", "#d9631a", "#9a3f0c"]

function hotspotLevel(score: number, max: number): number {
  if (max <= 0 || score <= 0) return 0
  return Math.min(4, Math.floor((score / max) * 5))
}

interface Rect {
  x: number
  y: number
  w: number
  h: number
}

interface Placed<T> extends Rect {
  item: T
}

// Squarified treemap (Bruls, Huizing, van Wijk) over a unit-less rectangle.
function squarify<T>(
  items: { value: number; item: T }[],
  box: Rect
): Placed<T>[] {
  const out: Placed<T>[] = []
  const total = items.reduce((s, i) => s + i.value, 0)
  if (total <= 0 || items.length === 0) return out
  const scale = (box.w * box.h) / total
  const nodes = items.map((i) => ({ area: i.value * scale, item: i.item }))
  let rect = { ...box }
  let row: typeof nodes = []

  const worst = (r: typeof nodes, side: number) => {
    const s = r.reduce((a, n) => a + n.area, 0)
    let max = 0
    let min = Infinity
    for (const n of r) {
      max = Math.max(max, n.area)
      min = Math.min(min, n.area)
    }
    return Math.max(
      (side * side * max) / (s * s),
      (s * s) / (side * side * min)
    )
  }

  const layoutRow = (r: typeof nodes) => {
    const s = r.reduce((a, n) => a + n.area, 0)
    if (rect.w >= rect.h) {
      const w = s / rect.h
      let y = rect.y
      for (const n of r) {
        const h = n.area / w
        out.push({ x: rect.x, y, w, h, item: n.item })
        y += h
      }
      rect = { x: rect.x + w, y: rect.y, w: rect.w - w, h: rect.h }
    } else {
      const h = s / rect.w
      let x = rect.x
      for (const n of r) {
        const w = n.area / h
        out.push({ x, y: rect.y, w, h, item: n.item })
        x += w
      }
      rect = { x: rect.x, y: rect.y + h, w: rect.w, h: rect.h - h }
    }
  }

  for (const node of nodes) {
    const side = Math.min(rect.w, rect.h)
    if (row.length === 0 || worst([...row, node], side) <= worst(row, side)) {
      row.push(node)
    } else {
      layoutRow(row)
      row = [node]
    }
  }
  if (row.length) layoutRow(row)
  return out
}

function basename(path: string): string {
  const i = path.lastIndexOf("/")
  return i >= 0 ? path.slice(i + 1) : path
}

/** Treemap of hotspot files: area = hotspot score, colour = score bucket. */
export function HotspotTreemap({
  files,
  maxScore,
  height = 320,
  onSelect,
}: {
  files: FileHotspot[]
  maxScore: number
  height?: number
  onSelect?: (path: string) => void
}) {
  // Lay out in percentage space so the map is responsive without measuring.
  const cells = useMemo(
    () =>
      squarify(
        files
          .filter((f) => f.score > 0)
          .map((f) => ({ value: f.score, item: f })),
        { x: 0, y: 0, w: 100, h: 100 }
      ),
    [files]
  )

  return (
    <div
      className="relative w-full overflow-hidden rounded-md bg-background"
      style={{ height }}
      role="img"
      aria-label="Treemap of change hotspots: larger and darker cells are hotter files"
    >
      {cells.map(({ x, y, w, h, item }) => {
        const level = hotspotLevel(item.score, maxScore)
        const dark = level >= 3
        const showLabel = w > 7 && h > 7
        return (
          <Tooltip key={item.path}>
            <TooltipTrigger asChild>
              <button
                type="button"
                onClick={() => onSelect?.(item.path)}
                className="absolute overflow-hidden rounded-[4px] p-1 text-left text-[11px] leading-tight transition-[filter] outline-none hover:brightness-95 focus-visible:ring-2 focus-visible:ring-ring"
                style={{
                  left: `calc(${x}% + 1px)`,
                  top: `calc(${y}% + 1px)`,
                  width: `calc(${w}% - 2px)`,
                  height: `calc(${h}% - 2px)`,
                  background: HOTSPOT_RAMP[level],
                  color: dark ? "#fff" : "#1f1f1f",
                }}
              >
                {showLabel && (
                  <>
                    <span className="block truncate font-medium">
                      {basename(item.path)}
                    </span>
                    <span className="block truncate opacity-80">
                      {item.changes} changes
                    </span>
                  </>
                )}
              </button>
            </TooltipTrigger>
            <TooltipContent>
              <div className="space-y-0.5 text-xs">
                <div className="font-mono font-medium">{item.path}</div>
                <div>
                  Score {item.score.toFixed(1)} · {item.changes} changes ·{" "}
                  {item.lines_changed.toLocaleString()} lines
                </div>
                <div>
                  {item.findings} findings · {item.loc.toLocaleString()} LOC ·{" "}
                  {item.symbols} symbols
                </div>
              </div>
            </TooltipContent>
          </Tooltip>
        )
      })}
    </div>
  )
}

/** Legend for the treemap ramp. */
export function HotspotLegend() {
  return (
    <div className="flex items-center gap-2 text-xs text-muted-foreground">
      <span>Cooler</span>
      <div className="flex gap-0.5">
        {HOTSPOT_RAMP.map((c) => (
          <span
            key={c}
            className="h-3 w-5 rounded-[2px]"
            style={{ background: c }}
          />
        ))}
      </div>
      <span>Hotter</span>
      <span className="ml-2">Area = hotspot score</span>
    </div>
  )
}
