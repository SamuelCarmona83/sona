export function formatDuration(sec: number | undefined | null): string {
  if (!sec || sec < 0) return '—'
  const m = Math.floor(sec / 60)
  const s = sec % 60
  return `${m}:${String(s).padStart(2, '0')}`
}

export function formatBytes(bytes: number | undefined | null): string {
  if (!bytes || bytes <= 0) return '—'
  const units = ['B', 'KB', 'MB', 'GB']
  let n = bytes
  let i = 0
  while (n >= 1024 && i < units.length - 1) {
    n /= 1024
    i++
  }
  return `${n.toFixed(i === 0 ? 0 : 1)} ${units[i]}`
}

export function formatTimestamp(unix: number | undefined | null): string {
  if (!unix) return '—'
  return new Date(unix * 1000).toLocaleDateString('es', {
    day: '2-digit',
    month: 'short',
    year: '2-digit',
  })
}

export function formatDateTime(unix: number | undefined | null): string {
  if (!unix) return '—'
  return new Date(unix * 1000).toLocaleString('es', {
    day: '2-digit',
    month: 'short',
    hour: '2-digit',
    minute: '2-digit',
  })
}

export function formatDurationSec(sec: number | undefined | null): string {
  const s = Math.floor(Number(sec) || 0)
  if (s < 60) return `${s}s`
  const m = Math.floor(s / 60)
  const r = s % 60
  if (m < 60) return `${m}m ${r}s`
  const h = Math.floor(m / 60)
  return `${h}h ${m % 60}m`
}

/** Clock time only (timeline rail). */
export function formatClock(unix: number | undefined | null): string {
  if (!unix) return '—'
  return new Date(unix * 1000).toLocaleTimeString('es', {
    hour: '2-digit',
    minute: '2-digit',
  })
}

/** Gap since previous detection, e.g. "+3m" / "+45s". */
export function formatGapSince(sec: number | undefined | null): string {
  const s = Math.floor(Number(sec) || 0)
  if (s <= 0) return ''
  if (s < 60) return `+${s}s`
  const m = Math.floor(s / 60)
  const r = s % 60
  if (m < 60) return r ? `+${m}m ${r}s` : `+${m}m`
  const h = Math.floor(m / 60)
  return `+${h}h ${m % 60}m`
}

export function spotifyUrl(id?: string | null): string | null {
  return id ? `https://open.spotify.com/track/${id}` : null
}

export function youtubeUrl(entry: {
  webpage_url?: string
  video_id?: string
}): string | null {
  if (entry.webpage_url) return entry.webpage_url
  if (entry.video_id) return `https://www.youtube.com/watch?v=${entry.video_id}`
  return null
}
