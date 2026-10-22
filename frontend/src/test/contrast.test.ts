import { readFileSync } from "node:fs"
import { resolve } from "node:path"
import { describe, expect, it } from "vitest"

// WCAG 2.x contrast of the design tokens in src/index.css
const css = readFileSync(resolve(process.cwd(), "src/index.css"), "utf-8")

function token(name: string): [number, number, number] {
  const match = new RegExp(`--${name}:\\s*(\\d+(?:\\.\\d+)?)\\s+(\\d+(?:\\.\\d+)?)%\\s+(\\d+(?:\\.\\d+)?)%`).exec(css)
  if (!match) throw new Error(`token ${name} not found`)
  return [Number(match[1]), Number(match[2]) / 100, Number(match[3]) / 100]
}

function luminance([h, s, l]: [number, number, number]): number {
  const a = s * Math.min(l, 1 - l)
  const f = (n: number) => {
    const k = (n + h / 30) % 12
    return l - a * Math.max(-1, Math.min(k - 3, 9 - k, 1))
  }
  const channel = (value: number) => (value <= 0.03928 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4)
  return 0.2126 * channel(f(0)) + 0.7152 * channel(f(8)) + 0.0722 * channel(f(4))
}

function contrast(foreground: string, background: string): number {
  const [a, b] = [luminance(token(foreground)), luminance(token(background))].sort((x, y) => y - x)
  return (a + 0.05) / (b + 0.05)
}

const PAIRS: [string, string][] = [
  ["foreground", "background"],
  ["card-foreground", "card"],
  ["muted-foreground", "background"],
  ["muted-foreground", "card"],
  ["muted-foreground", "muted"],
  ["primary-foreground", "primary"],
  ["secondary-foreground", "secondary"],
  ["destructive-foreground", "destructive"],
  ["navy-text", "navy"],
  ["ok", "ok-soft"],
  ["warn", "warn-soft"],
  ["bad", "bad-soft"],
  ["info", "info-soft"],
  ["primary", "background"],
]

describe("the colour tokens meet WCAG AA for text (4.5:1)", () => {
  it.each(PAIRS)("%s on %s", (foreground, background) => {
    expect(contrast(foreground, background)).toBeGreaterThanOrEqual(4.5)
  })
})
