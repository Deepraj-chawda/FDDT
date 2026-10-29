import { cleanup } from "@testing-library/react"
import { afterEach } from "vitest"

// Unmount whatever a test rendered (no vitest globals, so RTL can't do it itself).
afterEach(cleanup)
