import { type ClassValue, clsx } from "clsx"
import { twMerge } from "tailwind-merge"

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

// Used for the small avatar chip in AppHeader — a name gets its first two
// initials ("Jane Doe" -> "JD"), an email (no full_name on file) falls
// back to its first character.
export function getInitials(nameOrEmail: string): string {
  const parts = nameOrEmail.trim().split(/\s+/)
  if (parts.length >= 2) {
    return (parts[0][0] + parts[1][0]).toUpperCase()
  }
  return nameOrEmail.charAt(0).toUpperCase()
}
