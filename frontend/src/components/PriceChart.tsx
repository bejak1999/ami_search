import { useMemo } from 'react'
import {
  Area,
  CartesianGrid,
  ComposedChart,
  Line,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip as ReTooltip,
  XAxis,
  YAxis,
} from 'recharts'
import type { PricePoint } from '@/api/types'
import { compactMoney, dateTime, money } from '@/lib/format'

/**
 * Price history is a step chart, not a smooth line.
 *
 * A price is a fact that holds until it changes. Interpolating between two
 * observations would draw prices that never existed, which matters when the
 * whole point is deciding whether today is cheap.
 */
export function PriceChart({
  points,
  currency,
  targetPrice,
  height = 260,
}: {
  points: PricePoint[]
  currency: string
  targetPrice?: number | null
  height?: number
}) {
  const data = useMemo(() => {
    const rows = points
      .filter((point) => point.price !== null)
      .map((point) => ({
        t: new Date(point.recorded_at).getTime(),
        price: point.price as number,
        inStock: point.in_stock,
        status: point.sale_status,
      }))
    // Carry the last observation forward so the line reaches "now" instead of
    // stopping at whenever the price last moved.
    if (rows.length) {
      const last = rows[rows.length - 1]
      if (Date.now() - last.t > 3600_000) {
        rows.push({ ...last, t: Date.now() })
      }
    }
    return rows
  }, [points])

  if (data.length === 0) {
    return (
      <div className="grid h-40 place-items-center rounded-control border border-dashed border-line text-sm text-faint">
        No price history recorded yet
      </div>
    )
  }

  const prices = data.map((row) => row.price)
  const min = Math.min(...prices)
  const max = Math.max(...prices)
  const pad = Math.max((max - min) * 0.15, max * 0.05)

  return (
    <ResponsiveContainer width="100%" height={height}>
      <ComposedChart data={data} margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
        <defs>
          <linearGradient id="priceFill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="rgb(var(--c-accent))" stopOpacity={0.28} />
            <stop offset="100%" stopColor="rgb(var(--c-accent))" stopOpacity={0} />
          </linearGradient>
        </defs>

        <CartesianGrid
          stroke="rgb(var(--c-line))"
          strokeDasharray="3 4"
          vertical={false}
          opacity={0.7}
        />
        <XAxis
          dataKey="t"
          type="number"
          scale="time"
          domain={['dataMin', 'dataMax']}
          tickFormatter={(value) =>
            new Intl.DateTimeFormat(undefined, { month: 'short', year: '2-digit' }).format(value)
          }
          stroke="rgb(var(--c-faint))"
          tick={{ fontSize: 11 }}
          tickLine={false}
          axisLine={false}
          minTickGap={40}
        />
        <YAxis
          domain={[Math.max(0, min - pad), max + pad]}
          tickFormatter={(value) => compactMoney(value, currency)}
          stroke="rgb(var(--c-faint))"
          tick={{ fontSize: 11 }}
          tickLine={false}
          axisLine={false}
          width={64}
        />

        <ReTooltip
          cursor={{ stroke: 'rgb(var(--c-accent))', strokeWidth: 1, strokeDasharray: '4 4' }}
          contentStyle={{
            background: 'rgb(var(--c-surface))',
            border: '1px solid rgb(var(--c-line))',
            borderRadius: 12,
            fontSize: 12,
            boxShadow: 'var(--shadow-pop)',
          }}
          labelFormatter={(value) => dateTime(new Date(Number(value)))}
          formatter={(value: number, _name, entry: any) => [
            `${money(value, currency)}${entry?.payload?.inStock ? ' · in stock' : ''}`,
            'Price',
          ]}
        />

        {targetPrice ? (
          <ReferenceLine
            y={targetPrice}
            stroke="rgb(var(--c-positive))"
            strokeDasharray="5 4"
            label={{
              value: `Target ${money(targetPrice, currency)}`,
              position: 'insideTopRight',
              fill: 'rgb(var(--c-positive))',
              fontSize: 11,
            }}
          />
        ) : null}

        <Area
          type="stepAfter"
          dataKey="price"
          stroke="none"
          fill="url(#priceFill)"
          isAnimationActive={false}
        />
        <Line
          type="stepAfter"
          dataKey="price"
          stroke="rgb(var(--c-accent))"
          strokeWidth={2}
          dot={false}
          activeDot={{ r: 4, strokeWidth: 0 }}
          isAnimationActive={false}
        />
      </ComposedChart>
    </ResponsiveContainer>
  )
}
