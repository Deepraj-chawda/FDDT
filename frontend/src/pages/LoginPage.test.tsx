import { render, screen } from "@testing-library/react"
import { MemoryRouter, Route, Routes } from "react-router-dom"
import { describe, expect, it, vi } from "vitest"

import { LoginPage } from "@/pages/LoginPage"

vi.mock("@/hooks/useAuth", () => ({
  useAuth: () => ({
    token: "t",
    user: { is_platform_admin: false },
    isLoadingUser: false,
    login: vi.fn(),
  }),
}))

function renderLogin(state: unknown) {
  render(
    <MemoryRouter initialEntries={[{ pathname: "/login", state }]}>
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route path="/dashboard" element={<p>dashboard</p>} />
        <Route path="/cases/:id" element={<p>case page</p>} />
      </Routes>
    </MemoryRouter>,
  )
}

describe("LoginPage once signed in", () => {
  it("returns to the deep link it was sent from", () => {
    renderLogin({ from: "/cases/42" })
    expect(screen.getByText("case page")).toBeTruthy()
  })

  it("goes home without a deep link", () => {
    renderLogin(null)
    expect(screen.getByText("dashboard")).toBeTruthy()
  })

  it.each(["//evil.example/x", "https://evil.example", "/login"])("ignores an unsafe return path %s", (from) => {
    renderLogin({ from })
    expect(screen.getByText("dashboard")).toBeTruthy()
  })
})
