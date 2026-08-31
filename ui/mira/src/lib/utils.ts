import { clsx, type ClassValue } from "clsx"
import { twMerge } from "tailwind-merge"

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

// An `https` URL safe to put in an `href`, or "".
//
// Everything triage renders as a link comes from a platform API, but it
// reaches this component through a database, and an `href` is the one place
// where a stored string becomes executable: `javascript:` in an anchor runs in
// the dashboard's own origin, with an admin session attached. Anything that is
// not plainly `https` is rendered as text instead of as a link.
export function safeHref(value: unknown): string {
  const text = String(value ?? "").trim()
  if (!text) return ""
  try {
    const parsed = new URL(text)
    return parsed.protocol === "https:" ? parsed.toString() : ""
  } catch {
    return ""
  }
}
