import { Download } from "lucide-react"

import { Button } from "@/components/ui/button"
import { api } from "@/lib/api"

// Download links for the software bill of materials: one repository when
// owner/repo are given, every tracked repository otherwise.
export function SbomDownload({ owner, repo }: { owner?: string; repo?: string }) {
  return (
    <div className="flex flex-wrap items-center gap-2">
      <span className="text-xs text-muted-foreground">SBOM</span>
      <Button variant="outline" size="sm" asChild>
        <a href={api.sbomUrl("cyclonedx", owner, repo)} download>
          <Download className="h-4 w-4" />
          CycloneDX
        </a>
      </Button>
      <Button variant="outline" size="sm" asChild>
        <a href={api.sbomUrl("spdx", owner, repo)} download>
          <Download className="h-4 w-4" />
          SPDX
        </a>
      </Button>
    </div>
  )
}
