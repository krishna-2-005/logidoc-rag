import { screen } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"
import { SettingsPage } from "@/pages/Settings"
import { REVIEWER_SESSION, mockApi, renderPage } from "@/test/utils"

afterEach(() => vi.unstubAllGlobals())

const SETTINGS = {
  auth_mode: "required", resolution_needs_approval: false, minimum_note_length: 10, resolution_needs_evidence: true,
  upload_limit_mb: 25, max_files_per_upload: 20, language_model: "nemotron-3-nano:4b", embedding_model: "nomic-embed-text",
  ocr: "not available", currency_handling: "never converted", editable_here: false,
}

describe("Settings: the release", () => {
  it("shows the release the server runs", async () => {
    mockApi({ "/api/session": REVIEWER_SESSION, "/api/settings": { ...SETTINGS, release: "1.0.0-rc.1" } })
    renderPage(<SettingsPage />)

    expect(await screen.findByText("1.0.0-rc.1")).toBeInTheDocument()
    expect(screen.getByText("Release")).toBeInTheDocument()
  })

  it("shows no release row for a server that does not report one", async () => {
    mockApi({ "/api/session": REVIEWER_SESSION, "/api/settings": SETTINGS })
    renderPage(<SettingsPage />)

    expect(await screen.findByText("Sign-in")).toBeInTheDocument()
    expect(screen.queryByText("Release")).not.toBeInTheDocument()
  })
})
