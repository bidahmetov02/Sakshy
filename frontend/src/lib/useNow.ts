import { useEffect, useState } from 'react'

/** The current time, re-read every `everyMs`, for durations the browser counts itself. */
export function useNow(everyMs = 5000): number {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), everyMs)
    return () => clearInterval(id)
  }, [everyMs])
  return now
}
