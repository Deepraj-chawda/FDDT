import { beforeEach, describe, expect, it, vi } from "vitest"

import { listAllCases, listCasesPage } from "@/api/cases"
import type { CaseListItem } from "@/types/case"

const apiFetch = vi.hoisted(() => vi.fn())
vi.mock("@/api/client", () => ({ apiFetch, API_BASE_URL: "", ApiError: Error, parseErrorDetail: vi.fn() }))

describe("listCasesPage", () => {
  beforeEach(() => apiFetch.mockReset())

  it("sends only the filters that are set", async () => {
    apiFetch.mockResolvedValue({ items: [], total: 0, limit: 50, offset: 0 })
    await listCasesPage("t", { q: "INV-1", status_group: "open", risk: undefined, actionable: false, limit: 50, offset: 100 }, "co-1")
    const url = new URL(apiFetch.mock.calls[0][0], "http://x")
    expect(url.pathname).toBe("/cases/page")
    expect(Object.fromEntries(url.searchParams)).toEqual({
      q: "INV-1", status_group: "open", limit: "50", offset: "100", company_id: "co-1",
    })
  })
})

describe("listAllCases", () => {
  beforeEach(() => apiFetch.mockReset())

  it("reads page after page until it has every case", async () => {
    const item = (id: string) => ({ id }) as CaseListItem
    apiFetch
      .mockResolvedValueOnce({ items: [item("a"), item("b")], total: 3, limit: 200, offset: 0 })
      .mockResolvedValueOnce({ items: [item("c")], total: 3, limit: 200, offset: 2 })
    const all = await listAllCases("t", { mine: true })
    expect(all.map((c) => c.id)).toEqual(["a", "b", "c"])
    expect(apiFetch).toHaveBeenCalledTimes(2)
    expect(apiFetch.mock.calls[1][0]).toContain("offset=2")
  })
})
