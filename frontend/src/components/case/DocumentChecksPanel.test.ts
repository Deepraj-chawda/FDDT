import { describe, expect, it } from "vitest"

import { getCheckOverlays } from "@/components/case/DocumentChecksPanel"
import type { DocumentCheck } from "@/types/case"

const box = { page: 2, x: 0.1, y: 0.1, width: 0.2, height: 0.2 }

function check(fields: Partial<DocumentCheck>): DocumentCheck {
  return { id: "c", status: "completed", error_message: null, result: null, ...fields } as DocumentCheck
}

describe("getCheckOverlays", () => {
  it("draws a completed ELA finding's box", () => {
    const overlays = getCheckOverlays([
      check({
        check_type: "error_level_analysis",
        result: { details: [{ finding: "ela_hotspot", severity: "high", description: "Edited area", bounding_box: box }] },
      }),
    ])
    expect(overlays).toEqual([{ box, color: "destructive", label: "Edited area" }])
  })

  it("draws nothing for a check that has not completed", () => {
    const overlays = getCheckOverlays([
      check({
        check_type: "error_level_analysis",
        status: "running",
        result: { details: [{ finding: "x", severity: "high", description: "d", bounding_box: box }] },
      }),
    ])
    expect(overlays).toEqual([])
  })
})
