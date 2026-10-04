import { API_BASE, fetchJson } from "./http"
import type { PackageSearchHit } from "./types"

export type SbomFormat = "cyclonedx" | "spdx"

// Cross-repo package search.
export const packagesApi = {
  searchPackages: (params: {
    name?: string
    version?: string
    kind?: string
    is_dev?: boolean
  }) => {
    const qs = new URLSearchParams()
    if (params.name) qs.set("name", params.name)
    if (params.version) qs.set("version", params.version)
    if (params.kind) qs.set("kind", params.kind)
    if (params.is_dev !== undefined) qs.set("is_dev", String(params.is_dev))
    return fetchJson<PackageSearchHit[]>(
      `/api/packages/search?${qs.toString()}`
    )
  },

  // SBOM downloads are plain links so the browser handles the file and the
  // session cookie rides along. Without owner/repo: every tracked repository.
  sbomUrl: (format: SbomFormat, owner?: string, repo?: string) => {
    const qs = new URLSearchParams({ format })
    const path =
      owner && repo
        ? `/api/repos/${encodeURIComponent(owner)}/${encodeURIComponent(repo)}/sbom`
        : "/api/sbom"
    return `${API_BASE}${path}?${qs.toString()}`
  },
}
