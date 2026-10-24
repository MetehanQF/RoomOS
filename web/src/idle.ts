export const IDLE_AFTER_MS = 5 * 60 * 1_000

export class IdleController {
  private timer: ReturnType<typeof setTimeout> | null = null
  private idle = false
  constructor(private onChange: (idle: boolean) => void, private timeout = IDLE_AFTER_MS) {}
  start() { this.activity() }
  activity() {
    if (this.timer) clearTimeout(this.timer)
    if (this.idle) { this.idle = false; this.onChange(false) }
    this.timer = setTimeout(() => { this.idle = true; this.onChange(true) }, this.timeout)
  }
  stop() { if (this.timer) clearTimeout(this.timer); this.timer = null }
}
