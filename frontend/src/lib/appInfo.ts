// The product's display name — the ONE place the frontend gets it from
// (nav wordmark, login screen, page footer, browser tab title via the
// `%APP_TITLE%` placeholder in index.html — see vite.config.ts). Mirrors
// backend/app/core/config.py's APP_NAME / APP_FULL_NAME.
//
// Display-level only: internal identifiers keep their original names on
// purpose (the `docauth` DB/user, container names and package folders).
//
// Deliberately free of `import.meta.env` and other browser-only APIs:
// vite.config.ts imports this file in Node too.
export const APP_NAME = "FDDT"
export const APP_FULL_NAME = "Fraud Document Detection Tool"
export const APP_TITLE = `${APP_NAME} — ${APP_FULL_NAME}`
