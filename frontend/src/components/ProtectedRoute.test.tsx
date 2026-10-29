import { render, screen } from "@testing-library/react"
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom"
import { beforeEach, describe, expect, it, vi } from "vitest"

import { ProtectedRoute } from "@/components/ProtectedRoute"

const auth = vi.hoisted(() => ({ hadSession: false }))
vi.mock("@/hooks/useAuth", () => ({
  useAuth: () => ({ token: null, user: null, isLoadingUser: false }),
}))
vi.mock("@/lib/session", () => ({ hadSessionThisLoad: () => auth.hadSession }))

function LoginProbe() {
  const location = useLocation()
  return <p>login from={JSON.stringify(location.state)}</p>
}

function renderAt(path: string) {
  render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/login" element={<LoginProbe />} />
        <Route element={<ProtectedRoute />}>
          <Route path="/cases/:id" element={<p>case</p>} />
        </Route>
      </Routes>
    </MemoryRouter>,
  )
}

describe("ProtectedRoute", () => {
  beforeEach(() => {
    auth.hadSession = false
  })

  it("sends a link opened without a session to sign-in, remembering the link", () => {
    renderAt("/cases/abc?tab=docs")
    expect(screen.getByText(`login from=${JSON.stringify({ from: "/cases/abc?tab=docs" })}`)).toBeTruthy()
  })

  it("does not remember the page after a sign-out in this page load", () => {
    auth.hadSession = true
    renderAt("/cases/abc")
    expect(screen.getByText("login from=null")).toBeTruthy()
  })
})
