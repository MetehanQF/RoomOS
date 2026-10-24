import { describe, expect, it } from 'vitest'
// @ts-expect-error Node types are intentionally not part of the browser production build.
import fs from 'node:fs'

const css = fs.readFileSync(new URL('./styles.css', import.meta.url), 'utf8')

function minimum(selector: string) {
  const start = css.indexOf(`${selector} {`)
  const rule = start < 0 ? '' : css.slice(start, css.indexOf('}', start))
  return Number(rule.match(/min-height:\s*(\d+)px/)?.[1] ?? 0)
}

describe('Phone 2 panel touch target contract', () => {
  it.each([
    ['.home-panel-link', 48],
    ['.panel-health', 48],
    ['.panel-power', 56],
    ['.panel-segmented button', 48],
    ['.panel-more', 48],
    ['.panel-section-head a', 48],
    ['.panel-scenes button', 64],
    ['.panel-routine-list li', 48],
    ['.panel-dock > button,.panel-dock > a', 52],
    ['.panel-segment button', 48],
    ['.panel-toggle', 60],
    ['.panel-fullscreen button', 52],
  ])('%s keeps at least a %d px target', (selector, expected) => {
    expect(minimum(selector)).toBeGreaterThanOrEqual(expected)
  })

  it('keeps fixed-size light and sheet controls at least 48 px', () => {
    expect(css).toMatch(/\.panel-feedback button\s*\{[^}]*width:\s*48px;[^}]*min-width:\s*48px;[^}]*height:\s*48px;/)
    expect(css).toMatch(/\.panel-power\s*\{[^}]*min-width:\s*76px;[^}]*min-height:\s*56px;/)
    expect(css).toMatch(/\.panel-card \.swatch\s*\{[^}]*min-width:\s*48px;/)
    expect(css).toMatch(/\.light-range\s*\{[^}]*height:\s*48px;/)
    expect(css).toMatch(/\.light-feedback button,\s*\.sheet-close\s*\{[^}]*width:\s*48px;[^}]*height:\s*48px;/)
  })
})
